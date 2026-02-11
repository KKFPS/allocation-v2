"""Allocation controller - orchestrates the allocation process."""
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from src.database.connection import db
from src.database.queries import Queries
from src.models.vehicle import Vehicle
from src.models.route import Route
from src.models.allocation import AllocationResult, RouteAllocation
from src.maf.parameter_parser import parse_maf_response, get_site_parameter, get_all_constraint_configs
from src.constraints.constraint_manager import ConstraintManager
from src.optimizer.cost_matrix import CostMatrixBuilder
from src.optimizer.hexaly_solver import HexalySolver
from src.config import (
    APPLICATION_NAME, DEFAULT_ALLOCATION_WINDOW_HOURS,
    DEFAULT_MAX_ROUTES_PER_VEHICLE, DEFAULT_RESERVE_VEHICLE_COUNT,
    DEFAULT_TURNAROUND_TIME_MINUTES
)
from src.utils.logging_config import logger


class AllocationController:
    """Main controller for vehicle-route allocation process."""
    
    def __init__(self, site_id: int, trigger_type: str = 'initial'):
        """
        Initialize allocation controller.
        
        Args:
            site_id: Site identifier
            trigger_type: Type of allocation trigger
        """
        self.site_id = site_id
        self.trigger_type = trigger_type
        self.allocation_id = None
        self.site_config = None
        self.constraint_manager = None
        
        # Connect to database
        db.connect()
    
    def run_allocation(self, current_time: Optional[datetime] = None) -> AllocationResult:
        """
        Execute complete allocation workflow.
        
        Args:
            current_time: Current datetime (defaults to now)
        
        Returns:
            AllocationResult object
        """
        if current_time is None:
            current_time = datetime.now()
        
        logger.info(f"Starting allocation for site {self.site_id}, trigger={self.trigger_type}")
        
        try:
            # Phase 1: Initialization
            self._initialize_allocation(current_time)
            
            # Phase 2: Load configuration from MAF
            self._load_maf_configuration()
            
            # Phase 3: Define 18-hour window and load data
            window_start, window_end = self._calculate_allocation_window(current_time)
            vehicles = self._load_vehicles(current_time)
            routes = self._load_routes(window_start, window_end)
            
            logger.info(f"Window: {window_start} to {window_end}")
            logger.info(f"Loaded {len(vehicles)} vehicles, {len(routes)} routes")

            logger.info(f"Site config: {self.site_config}")
            # Phase 4: Initialize constraint manager
            constraint_configs = get_all_constraint_configs(self.site_id, self.site_config)
            self.constraint_manager = ConstraintManager(constraint_configs)
            logger.info(f"Initialized {self.constraint_manager}")
            
            # Phase 4.5: Load vehicle charger locations (time-aware)
            vehicle_charger_map = self._load_vehicle_chargers(vehicles, current_time)
            
            # Phase 5: Build cost matrix and optimize
            allocation_result = self._optimize_allocation(
                vehicles, routes, window_start, window_end, vehicle_charger_map
            )
            
            # Phase 6: Validate and persist results
            if allocation_result.is_acceptable():
                self._persist_allocation(allocation_result)
                allocation_result.status = 'A'
                logger.info(f"Allocation successful: {allocation_result}")
            else:
                allocation_result.status = 'F'
                logger.warning(f"Allocation rejected due to low score: {allocation_result.total_score}")
            
            # Phase 7: Update allocation monitor
            self._update_allocation_monitor(allocation_result)
            
            return allocation_result
        
        except Exception as e:
            logger.error(f"Allocation failed: {e}", exc_info=True)
            self._log_error(f"Allocation failed: {e}")
            
            # Create failed result
            result = AllocationResult(
                allocation_id=self.allocation_id or -1,
                site_id=self.site_id,
                run_datetime=current_time,
                window_start=current_time,
                window_end=current_time + timedelta(hours=18),
                status='F'
            )
            
            if self.allocation_id:
                self._update_allocation_monitor(result)
            
            raise
    
    def _initialize_allocation(self, current_time: datetime):
        """Create allocation monitor record."""
        window_hours = DEFAULT_ALLOCATION_WINDOW_HOURS
        
        result = db.execute_query(
            Queries.CREATE_ALLOCATION_MONITOR,
            (
                self.site_id,
                'N',  # New
                self.trigger_type,
                current_time,
                current_time,
                current_time + timedelta(hours=window_hours)
            ),
            fetch=True
        )
        
        self.allocation_id = result[0]['allocation_id']
        logger.info(f"Created allocation monitor: allocation_id={self.allocation_id}")
    
    def _load_maf_configuration(self):
        """Load MAF parameters for site."""
        logger.info(f"Loading MAF configuration for {APPLICATION_NAME}")
        
        try:
            result = db.execute_query(
                Queries.CALL_GET_MODULE_PARAMS,
                (APPLICATION_NAME,),
                fetch=True
            )
            
            if result:
                maf_json = result[0]['sp_get_module_params']
                site_configs = parse_maf_response(maf_json)
                self.site_config = site_configs.get(str(self.site_id), {})
                # logger.info(f"Loaded MAF configuration for site {self.site_id} : {self.site_config}")
            else:
                logger.warning("No MAF configuration found, using defaults")
                self.site_config = {'parameters': {}, 'enabled_vehicles': []}
        
        except Exception as e:
            logger.error(f"Failed to load MAF configuration: {e}")
            self.site_config = {'parameters': {}, 'enabled_vehicles': []}
    
    def _calculate_allocation_window(self, current_time: datetime):
        """Calculate 18-hour allocation window."""
        window_hours = get_site_parameter(
            self.site_config, 
            'allocation_window_hours', 
            DEFAULT_ALLOCATION_WINDOW_HOURS
        )
        
        window_start = current_time
        window_end = current_time + timedelta(hours=window_hours)
        
        return window_start, window_end
    
    def _load_vehicles(self, as_of_time: Optional[datetime] = None) -> List[Vehicle]:
        """Load active vehicles for site.
        
        Args:
            as_of_time: If set, vehicle state (estimated_soc, available_time) is
                evaluated at this time (e.g. allocation start from --start-time).
        """
        rows = db.execute_query(
            Queries.GET_ACTIVE_VEHICLES,
            (self.site_id,),
            fetch=True
        )
        
        vehicles = []
        enabled_vehicle_ids = self.site_config.get('enabled_vehicles', [])
        
        for row in rows:
            # Check if vehicle is MAF-enabled
            if enabled_vehicle_ids and row['vehicle_id'] not in enabled_vehicle_ids:
                continue
            
            vehicle = Vehicle(**row)
            
            # Load current state from VSM
            self._load_vehicle_state(vehicle, as_of_time)
            
            vehicles.append(vehicle)
        
        return vehicles
    
    def _load_vehicle_state(self, vehicle: Vehicle, as_of_time: Optional[datetime] = None):
        """Load vehicle state from VSM.
        
        When as_of_time is set (e.g. allocation start from --start-time), uses
        VSM state at or before that time for estimated_soc and sets available_time
        from that timestamp. When None, uses latest VSM and datetime.now().
        """
        reference_time = as_of_time if as_of_time is not None else datetime.now()
        if as_of_time is not None:
            vsm_data = db.execute_query(
                Queries.GET_VSM_AS_OF,
                (vehicle.vehicle_id, as_of_time),
                fetch=True
            )
        else:
            vsm_data = db.execute_query(
                Queries.GET_LATEST_VSM,
                (vehicle.vehicle_id,),
                fetch=True
            )
        
        if vsm_data:
            vsm = vsm_data[0]
            vehicle.current_status = vsm['status']
            vehicle.current_route_id = vsm['route_id']
            vehicle.estimated_soc = vsm['estimated_soc']
            vehicle.return_eta = vsm['return_eta']
            vehicle.return_soc = vsm['return_soc']
            
            # Calculate availability
            if vehicle.current_status == 'On-Route' and vehicle.return_eta:
                vehicle.available_time = vehicle.return_eta
            else:
                vehicle.available_time = reference_time
            vehicle.available_energy_kwh = vehicle.get_available_energy(reference_time)
        else:
            # No VSM at or before as_of_time: treat as available from reference time
            if as_of_time is not None:
                vehicle.available_time = as_of_time
            vehicle.available_energy_kwh = vehicle.get_available_energy(reference_time)
    
    def _load_routes(self, window_start: datetime, window_end: datetime) -> List[Route]:
        """Load routes within allocation window."""
        rows = db.execute_query(
            Queries.GET_ROUTES_IN_WINDOW,
            (self.site_id, window_start, window_end),
            fetch=True
        )
        
        routes = []
        for row in rows:
            route = Route(**row)
            routes.append(route)
        
        return routes
    
    def _load_vehicle_chargers(self, vehicles: List[Vehicle], 
                              reference_time: Optional[datetime] = None) -> Dict[int, Optional[str]]:
        """
        Load vehicle charger locations within 18-hour window before reference time.
        At most one vehicle per charger: if multiple vehicles used the same charger,
        only the one with the latest start_time keeps it; others get None.
        
        Args:
            vehicles: List of vehicles
            reference_time: Reference datetime (allocation start time from --start-time)
        
        Returns:
            Dict mapping vehicle_id -> charger_id or None (if vehicle lost charger to a later one)
        """
        if not vehicles:
            return {}
        
        vehicle_ids = [v.vehicle_id for v in vehicles]
        vehicle_charger_map = db.get_vehicle_chargers_in_window(vehicle_ids, reference_time)
        
        logger.info(f"Loaded charger locations for {len(vehicle_charger_map)}/{len(vehicles)} vehicles")
        logger.debug(f"Vehicle charger map: {vehicle_charger_map}")
        return vehicle_charger_map
    
    def _optimize_allocation(self, vehicles: List[Vehicle], routes: List[Route],
                           window_start: datetime, window_end: datetime,
                           vehicle_charger_map: Optional[Dict[int, Optional[str]]] = None) -> AllocationResult:
        """
        Run optimization to allocate routes to vehicles.
        
        Args:
            vehicles: Available vehicles
            routes: Routes to allocate
            window_start: Window start time
            window_end: Window end time
        
        Returns:
            AllocationResult
        """
        if not routes:
            logger.warning("No routes to allocate")
            return AllocationResult(
                allocation_id=self.allocation_id,
                site_id=self.site_id,
                run_datetime=datetime.now(),
                window_start=window_start,
                window_end=window_end,
                status='A'
            )
        
        # Build cost matrix
        max_routes = get_site_parameter(
            self.site_config,
            'max_routes_per_vehicle_in_window',
            DEFAULT_MAX_ROUTES_PER_VEHICLE
        )
        
        builder = CostMatrixBuilder(
            vehicles, routes, self.constraint_manager, max_routes,
            vehicle_charger_map=vehicle_charger_map or {}
        )
        
        sequence_costs, sequences, metadata = builder.build_assignment_matrix()
        route_ids = [r.route_id for r in routes]
        
        # Solve with Hexaly
        solver = HexalySolver(time_limit_seconds=30)
        solution = solver.solve(sequences, route_ids, sequence_costs)
        
        # Create allocation result
        result = solver.create_allocation_result(
            solution, self.allocation_id, self.site_id,
            window_start, window_end, route_ids
        )
        
        return result
    
    def _persist_allocation(self, result: AllocationResult):
        """
        Persist allocation results to database.
        
        Args:
            result: AllocationResult to persist
        """
        logger.info(f"Persisting {len(result.allocations)} allocations")
        
        # Delete existing allocations for site (replace)
        db.execute_query(
            Queries.DELETE_SITE_ALLOCATIONS,
            (self.site_id,),
            fetch=False
        )
        
        # Insert new allocations
        allocation_rows = []
        history_rows = []
        
        for alloc in result.allocations:
            row = (
                result.allocation_id,
                alloc.route_id,
                self.site_id,
                alloc.vehicle_id,
                'N',  # Status
                alloc.estimated_arrival,
                alloc.estimated_arrival_soc,
                -1,  # http_response (not sent yet)
                alloc.vehicle_id  # vehicle_id duplicate
            )
            allocation_rows.append(row)
            history_rows.append(row)
        
        if allocation_rows:
            db.execute_many(Queries.INSERT_ROUTE_ALLOCATED, allocation_rows)
            db.execute_many(Queries.INSERT_ROUTE_ALLOCATED_HISTORY, history_rows)
            
            logger.info(f"Persisted {len(allocation_rows)} allocations")
    
    def _update_allocation_monitor(self, result: AllocationResult):
        """Update allocation monitor with final results."""
        db.execute_query(
            Queries.UPDATE_ALLOCATION_MONITOR,
            (
                result.status,
                result.total_score,
                result.routes_in_window,
                result.routes_allocated,
                result.routes_overlapping_count,
                result.allocation_id
            ),
            fetch=False
        )
        
        logger.info(f"Updated allocation monitor: status={result.status}, score={result.total_score}")
    
    def _log_error(self, error_message: str):
        """Log error to database."""
        try:
            db.execute_query(
                Queries.INSERT_ERROR_LOG,
                (datetime.now(), 'AllocationController', error_message),
                fetch=False
            )
        except Exception as e:
            logger.error(f"Failed to log error to database: {e}")
    
    def close(self):
        """Close database connection."""
        db.close()
