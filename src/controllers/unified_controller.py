"""Unified controller - orchestrates combined allocation and scheduling optimization."""

from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from src.database.connection import db
from src.database.queries import Queries
from src.models.vehicle import Vehicle
from src.models.route import Route
from src.models.allocation import AllocationResult, RouteAllocation
from src.models.scheduler import (
    VehicleChargeState, RouteEnergyRequirement,
    VehicleAvailability, ChargeScheduleResult, VehicleChargeSchedule,
    ChargeSlot, RouteSourceMode, ScheduleReport, VehicleScheduleReport
)
from src.maf.parameter_parser import parse_maf_response, get_site_parameter, get_all_constraint_configs
from src.constraints.constraint_manager import ConstraintManager
from src.optimizer.cost_matrix import CostMatrixBuilder
from src.optimizer.unified_optimizer import (
    UnifiedOptimizer, UnifiedOptimizationConfig, UnifiedOptimizationResult, OptimizationMode
)
from src.config import (
    APPLICATION_NAME, DEFAULT_ALLOCATION_WINDOW_HOURS,
    DEFAULT_MAX_ROUTES_PER_VEHICLE, DEFAULT_RESERVE_VEHICLE_COUNT,
    DEFAULT_TURNAROUND_TIME_MINUTES
)
from src.utils.logging_config import logger


class UnifiedController:
    """Main controller for unified vehicle allocation and charge scheduling optimization."""
    
    def __init__(self, site_id: int, trigger_type: str = 'initial', schedule_id: Optional[int] = None):
        """
        Initialize unified controller.
        
        Args:
            site_id: Site identifier
            trigger_type: Type of allocation trigger
            schedule_id: Existing schedule ID (optional)
        """
        self.site_id = site_id
        self.trigger_type = trigger_type
        self.allocation_id = None
        self.schedule_id = schedule_id
        self.site_config = None
        self.constraint_manager = None
        self.fleet_avg_efficiency: float = 0.35  # Default fallback
        
        # Connect to database
        db.connect()
    
    def run_unified_optimization(
        self,
        current_time: Optional[datetime] = None,
        mode: str = 'integrated',
        config: Optional[UnifiedOptimizationConfig] = None,
        persist_to_database: bool = True,
        window_hours: Optional[float] = None
    ) -> Tuple[Optional[AllocationResult], Optional[ChargeScheduleResult], UnifiedOptimizationResult]:
        """
        Execute complete unified optimization workflow.
        
        Args:
            current_time: Current datetime (defaults to now)
            mode: Optimization mode ('allocation_only', 'scheduling_only', 'integrated')
            config: Optional optimization configuration
            persist_to_database: Whether to persist results to database
            window_hours: Optional planning window length in hours (overrides site/MAF default)
        
        Returns:
            Tuple of (AllocationResult, ChargeScheduleResult, UnifiedOptimizationResult)
        """
        if current_time is None:
            current_time = datetime.now()
        
        # Floor to 30-minute interval for scheduling consistency
        current_time = self._floor_to_30_min(current_time)
        
        # Parse mode
        opt_mode = self._parse_mode(mode)
        
        logger.info(f"Starting unified optimization for site {self.site_id}, mode={opt_mode.value}")
        
        try:
            # Phase 1: Initialization
            self._initialize_optimization(current_time, opt_mode, window_hours_override=window_hours)
            
            # Phase 2: Load configuration from MAF
            self._load_maf_configuration()
            
            # Phase 3: Define planning window and load data
            window_start, window_end, actual_hours = self._calculate_planning_window(
                current_time, window_hours_override=window_hours
            )
            
            logger.info(f"Planning window: {window_start} to {window_end} ({actual_hours:.1f} hours)")
            
            # Phase 4: Load vehicles and states
            vehicles = self._load_vehicles(current_time)
            vehicle_states = self._load_vehicle_states(vehicles, current_time)
            
            logger.info(f"Loaded {len(vehicles)} vehicles")

            # Filter out vehicles with estimated_soc = -111
            vehicles = [v for v in vehicles if vehicle_states[v.vehicle_id].current_soc_percent != -111]

            logger.info(f"Loaded {len(vehicles)} vehicles after filtering VOR vehicles (estimated_soc = -111)")

            
            # Phase 5: Prepare optimization inputs based on mode
            opt_inputs = self._prepare_optimization_inputs(
                opt_mode, vehicles, vehicle_states, window_start, window_end, current_time
            )
            
            # Phase 6: Run unified optimization
            if config is None:
                config = self._build_optimization_config(opt_mode)
            
            optimizer = UnifiedOptimizer(config)
            unified_result = optimizer.solve(**opt_inputs)
            
            logger.info(f"Optimization completed: status={unified_result.status}, "
                       f"objective={unified_result.objective_value:.2f}, "
                       f"solve_time={unified_result.solve_time_seconds:.2f}s")
            
            # Phase 7: Convert to legacy result formats
            allocation_result = None
            schedule_result = None
            
            if opt_mode in (OptimizationMode.ALLOCATION_ONLY, OptimizationMode.INTEGRATED):
                allocation_result = unified_result.to_allocation_result(
                    self.allocation_id, self.site_id, window_start, window_end,
                    opt_inputs.get('route_ids', [])
                )
                allocation_result.total_score = unified_result.allocation_score
                allocation_result.routes_allocated = unified_result.routes_allocated
                allocation_result.routes_in_window = unified_result.routes_total
            
            if opt_mode in (OptimizationMode.SCHEDULING_ONLY, OptimizationMode.INTEGRATED):
                schedule_result = unified_result.to_schedule_result(
                    self.schedule_id, self.site_id, window_start, window_end
                )
            
            # Phase 8: Validate and persist results
            if persist_to_database:
                if allocation_result and allocation_result.is_acceptable():
                    self._persist_allocation(allocation_result)
                    self._update_allocation_monitor(allocation_result)
                    logger.info(f"Allocation results persisted and validated")
                elif allocation_result:
                    logger.warning(f"Allocation result not acceptable, skipping persistence")
                    self._update_allocation_monitor(allocation_result)
                
                if schedule_result:
                    # Get energy requirements for validation
                    energy_requirements = opt_inputs.get('energy_requirements', {})
                    self._validate_schedule(schedule_result, energy_requirements)
                    self._persist_schedule(schedule_result, energy_requirements)
                    self._update_scheduler_status('completed', actual_hours)
                    logger.info(f"Schedule results persisted and validated")
            
            return allocation_result, schedule_result, unified_result
        
        except Exception as e:
            logger.error(f"Unified optimization failed: {e}", exc_info=True)
            # self._log_error(f"Unified optimization failed: {e}")
            
            # Update monitors with failure status
            if self.allocation_id:
                db.execute_query(
                    Queries.UPDATE_ALLOCATION_MONITOR,
                    ('F', 0.0, 0, 0, 0, self.allocation_id),
                    fetch=False
                )
            
            if self.schedule_id:
                self._update_scheduler_status('failed', None)
            
            raise
    
    def _floor_to_30_min(self, dt: datetime) -> datetime:
        """Floor datetime to 30-minute interval."""
        minute = (dt.minute // 30) * 30
        return dt.replace(minute=minute, second=0, microsecond=0)
    
    def _parse_mode(self, mode: str) -> OptimizationMode:
        """Parse mode string to OptimizationMode enum."""
        mode_map = {
            'allocation_only': OptimizationMode.ALLOCATION_ONLY,
            'allocation': OptimizationMode.ALLOCATION_ONLY,
            'scheduling_only': OptimizationMode.SCHEDULING_ONLY,
            'scheduling': OptimizationMode.SCHEDULING_ONLY,
            'integrated': OptimizationMode.INTEGRATED,
            'both': OptimizationMode.INTEGRATED
        }
        
        mode_lower = mode.lower()
        if mode_lower not in mode_map:
            raise ValueError(f"Invalid optimization mode: {mode}. "
                           f"Must be one of: {list(mode_map.keys())}")
        
        return mode_map[mode_lower]
    
    def _initialize_optimization(
        self, current_time: datetime, mode: OptimizationMode,
        window_hours_override: Optional[float] = None
    ):
        """Initialize allocation and/or scheduling monitor records."""
        window_hours = (
            window_hours_override
            if window_hours_override is not None
            else DEFAULT_ALLOCATION_WINDOW_HOURS
        )
        
        # Create allocation monitor if needed
        if mode in (OptimizationMode.ALLOCATION_ONLY, OptimizationMode.INTEGRATED):
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
        
        # Create or load scheduler config if needed
        if mode in (OptimizationMode.SCHEDULING_ONLY, OptimizationMode.INTEGRATED):
            if self.schedule_id:
                # Load existing config
                with db.get_cursor() as cur:
                    cur.execute(
                        Queries.GET_SCHEDULER_CONFIG,
                        (self.schedule_id,)
                    )
                    row = cur.fetchone()
                    if not row:
                        raise ValueError(f"Schedule ID {self.schedule_id} not found")
                    logger.info(f"Loaded scheduler config: schedule_id={self.schedule_id}")
            else:
                # Create new scheduler config
                with db.get_cursor() as cur:
                    # Insert scheduler config (device_id maps to site_id)
                    cur.execute(
                        Queries.CREATE_SCHEDULER,
                        (self.site_id, 'dynamic', 'running', True)
                    )
                    self.schedule_id = cur.fetchone()['schedule_id']
                    logger.info(f"Created scheduler config: schedule_id={self.schedule_id}")
    
    def _load_maf_configuration(self):
        """Load MAF parameters for site."""
        logger.info(f"Loading MAF configuration for {APPLICATION_NAME}")
        
        try:
            result = db.execute_query(
                Queries.CALL_GET_MODULE_PARAMS,
                (APPLICATION_NAME,),
                fetch=True
            )
            
            print(f"Result: {result[0]}")
            if result:
                json_params = result[0].get('sp_get_module_params')
                # logger.info(f"MAF name for site {self.site_id}: {name}")
                self.site_config = parse_maf_response(json_params)
                logger.info(f"Loaded MAF config: {len(self.site_config.get('parameters', {}))} parameters")
            else:
                logger.warning("No MAF configuration found, using defaults")
                self.site_config = {'parameters': {}, 'enabled_vehicles': []}
        
        except Exception as e:
            logger.error(f"Failed to load MAF configuration: {e}")
            self.site_config = {'parameters': {}, 'enabled_vehicles': []}
    
    def _calculate_planning_window(
        self, current_time: datetime,
        window_hours_override: Optional[float] = None
    ) -> Tuple[datetime, datetime, float]:
        """
        Calculate effective planning window based on data availability.
        
        Returns:
            Tuple of (start_time, end_time, actual_hours)
        """
        if window_hours_override is not None:
            window_hours = window_hours_override
        else:
            window_hours = get_site_parameter(
                self.site_config,
                'allocation_window_hours',
                DEFAULT_ALLOCATION_WINDOW_HOURS
            )
        
        planning_start = current_time
        planning_target_end = current_time + timedelta(hours=window_hours)
        
        # Check forecast and price horizons
        with db.get_cursor() as cur:
            cur.execute(
                Queries.GET_FORECAST_HORIZON,
                (self.site_id,)
            )
            forecast_row = cur.fetchone()
            max_forecast_time = forecast_row['max_forecast_time'] if forecast_row else None
            
            cur.execute(
                Queries.GET_PRICE_HORIZON
            )
            price_row = cur.fetchone()
            max_price_time = price_row['max_price_time'] if price_row else None
        
        # Effective end = earliest of target, forecast horizon, price horizon
        constraints = [planning_target_end]
        if max_forecast_time:
            constraints.append(max_forecast_time)
        if max_price_time:
            constraints.append(max_price_time)
        
        planning_end = min(constraints)
        actual_hours = (planning_end - planning_start).total_seconds() / 3600.0
        
        if actual_hours < window_hours:
            logger.warning(
                f"Planning window constrained by data availability: "
                f"requested={window_hours}h, actual={actual_hours:.1f}h"
            )

        if actual_hours < window_hours/2:
            raise ValueError(f"Planning window is less than half of the requested window: {actual_hours:.1f}h < {window_hours/2:.1f}h. Check electricity price and forecast data for the site {self.site_id}")
        
        return planning_start, planning_end, actual_hours
    
    def _calculate_fleet_efficiency(self):
        """Calculate fleet-wide average efficiency."""
        with db.get_cursor() as cur:
            cur.execute(
                Queries.GET_FLEET_EFFICIENCY,
                (self.site_id,)
            )
            row = cur.fetchone()
            if row and row['fleet_avg_efficiency']:
                self.fleet_avg_efficiency = float(row['fleet_avg_efficiency'])
                logger.info(f"Fleet average efficiency: {self.fleet_avg_efficiency:.3f} kWh/mile")
            else:
                logger.warning("No vehicle efficiency data, using default 0.35 kWh/mile")
    
    def _load_vehicles(self, as_of_time: Optional[datetime] = None) -> List[Vehicle]:
        """Load active vehicles for site."""
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
        """Load vehicle state from VSM."""
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
            # No VSM: treat as available from reference time
            if as_of_time is not None:
                vehicle.available_time = as_of_time
            vehicle.available_energy_kwh = vehicle.get_available_energy(reference_time)
    
    def _load_vehicle_states(
        self, vehicles: List[Vehicle], as_of: Optional[datetime] = None
    ) -> Dict[int, VehicleChargeState]:
        """Load charge state for all vehicles."""
        states = {}
        use_as_of = as_of is not None
        
        for vehicle in vehicles:
            if use_as_of:
                vsm_data = db.execute_query(
                    Queries.GET_VSM_AS_OF,
                    (vehicle.vehicle_id, as_of),
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
                estimated_soc = vsm['estimated_soc']
                estimated_soc_kwh = (estimated_soc / 100.0) * vehicle.battery_capacity
            else:
                estimated_soc = vehicle.estimated_soc if hasattr(vehicle, 'estimated_soc') else 50.0
                estimated_soc_kwh = (estimated_soc / 100.0) * vehicle.battery_capacity
            
            states[vehicle.vehicle_id] = VehicleChargeState(
                vehicle_id=vehicle.vehicle_id,
                current_soc_percent=estimated_soc,
                current_soc_kwh=estimated_soc_kwh,
                battery_capacity_kwh=vehicle.battery_capacity,
                is_connected=False,
                ac_charge_rate_kw=vehicle.charge_power_ac,
                dc_charge_rate_kw=vehicle.charge_power_dc,
            )
        
        return states
    
    def _prepare_optimization_inputs(
        self,
        mode: OptimizationMode,
        vehicles: List[Vehicle],
        vehicle_states: Dict[int, VehicleChargeState],
        window_start: datetime,
        window_end: datetime,
        current_time: datetime
    ) -> Dict:
        """Prepare optimization inputs based on mode."""
        opt_inputs = {}
        
        # Allocation inputs
        if mode in (OptimizationMode.ALLOCATION_ONLY, OptimizationMode.INTEGRATED):
            # Load routes
            routes = self._load_routes(window_start, window_end)
            logger.info(f"Loaded {len(routes)} routes for allocation")
            
            # Initialize constraint manager
            constraint_configs = get_all_constraint_configs(self.site_id, self.site_config)
            self.constraint_manager = ConstraintManager(constraint_configs)
            logger.info(f"Initialized {self.constraint_manager}")
            
            # Load vehicle charger locations
            vehicle_charger_map = self._load_vehicle_chargers(vehicles, current_time)
            
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
            
            opt_inputs['sequences'] = sequences
            opt_inputs['route_ids'] = route_ids
            opt_inputs['sequence_costs'] = sequence_costs
        
        # Scheduling inputs
        if mode in (OptimizationMode.SCHEDULING_ONLY, OptimizationMode.INTEGRATED):
            # Calculate fleet efficiency if not done
            if self.fleet_avg_efficiency == 0.35:
                self._calculate_fleet_efficiency()
            
            # Load vehicle routes
            vehicle_routes = self._load_vehicle_routes(vehicles, window_start, window_end)
            
            # Calculate energy requirements
            energy_requirements = self._calculate_energy_requirements(
                vehicle_routes, vehicle_states
            )
            
            # Calculate availability matrices
            availability_matrices = self._calculate_availability_matrices(
                vehicles, vehicle_states, vehicle_routes, window_start, window_end
            )
            
            # Build time slots
            time_slots = self._build_time_slots(window_start, window_end)
            
            # Load forecast and price data
            forecast_data = self._load_forecast_data(window_start, window_end)
            price_data = self._load_price_data(window_start, window_end)
            
            opt_inputs['schedule_id'] = self.schedule_id
            opt_inputs['vehicles'] = vehicles
            opt_inputs['vehicle_states'] = vehicle_states
            opt_inputs['energy_requirements'] = energy_requirements
            opt_inputs['availability_matrices'] = availability_matrices
            opt_inputs['time_slots'] = time_slots
            opt_inputs['forecast_data'] = forecast_data
            opt_inputs['price_data'] = price_data
        
        return opt_inputs
    
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
    
    def _load_vehicle_chargers(
        self, vehicles: List[Vehicle], reference_time: Optional[datetime] = None
    ) -> Dict[int, Optional[str]]:
        """Load vehicle charger locations within 18-hour window before reference time."""
        if not vehicles:
            return {}
        
        vehicle_ids = [v.vehicle_id for v in vehicles]
        vehicle_charger_map = db.get_vehicle_chargers_in_window(vehicle_ids, reference_time)
        
        logger.info(f"Loaded charger locations for {len(vehicle_charger_map)}/{len(vehicles)} vehicles")
        return vehicle_charger_map
    
    def _load_vehicle_routes(
        self, vehicles: List[Vehicle], planning_start: datetime, planning_end: datetime
    ) -> Dict[int, List[Route]]:
        """Load allocated routes for all vehicles within planning window using GET_ROUTES_FOR_SCHEDULING_ALLOCATED."""
        vehicle_routes = {v.vehicle_id: [] for v in vehicles}

        for vehicle in vehicles:
            rows = db.execute_query(
                Queries.GET_ROUTES_FOR_SCHEDULING_ALLOCATED,
                (vehicle.vehicle_id, planning_start, planning_end),
                fetch=True
            )
            for row in rows:
                route = Route(
                    route_id=row['route_id'],
                    site_id=row['site_id'],
                    route_alias=row.get('route_alias') or row['route_id'],
                    route_status=row.get('route_status', 'A'),
                    plan_start_date_time=row['plan_start_date_time'],
                    plan_end_date_time=row['plan_end_date_time'],
                    plan_mileage=row['plan_mileage'],
                    n_orders=row.get('n_orders', 0),
                    vehicle_id=row['vehicle_id'],
                    energy_kwh=None,
                )
                vehicle_routes[vehicle.vehicle_id].append(route)

        total_routes = sum(len(routes) for routes in vehicle_routes.values())
        logger.info(f"Loaded {total_routes} allocated routes across {len(vehicles)} vehicles")
        return vehicle_routes
    
    def _calculate_energy_requirements(
        self,
        vehicle_routes: Dict[int, List[Route]],
        vehicle_states: Dict[int, VehicleChargeState]
    ) -> Dict[int, List[RouteEnergyRequirement]]:
        """Calculate cumulative energy requirements for each vehicle's routes."""
        energy_requirements = {}
        
        for vehicle_id, routes in vehicle_routes.items():
            if not routes:
                energy_requirements[vehicle_id] = []
                continue
            
            reqs = []
            cumulative_energy = 0.0
            
            for seq_idx, route in enumerate(sorted(routes, key=lambda r: r.plan_start_date_time)):
                # Energy for this route
                if route.energy_kwh and route.energy_kwh > 0:
                    route_energy = route.energy_kwh
                elif route.plan_mileage and route.plan_mileage > 0:
                    route_energy = route.plan_mileage * self.fleet_avg_efficiency
                else:
                    route_energy = 0.0
                
                cumulative_energy += route_energy
                
                req = RouteEnergyRequirement(
                    route_id=route.route_id,
                    vehicle_id=vehicle_id,
                    plan_start_date_time=route.plan_start_date_time,
                    plan_end_date_time=route.plan_end_date_time,
                    plan_mileage=route.plan_mileage,
                    route_status=getattr(route, 'route_status', 'A'),
                    efficiency_kwh_mile=self.fleet_avg_efficiency,
                    route_energy_buffer_kwh=route_energy,
                    cumulative_energy_kwh=cumulative_energy,
                    route_sequence_index=seq_idx,
                )
                reqs.append(req)
            
            energy_requirements[vehicle_id] = reqs
        
        return energy_requirements
    
    def _calculate_availability_matrices(
        self,
        vehicles: List[Vehicle],
        vehicle_states: Dict[int, VehicleChargeState],
        vehicle_routes: Dict[int, List[Route]],
        planning_start: datetime,
        planning_end: datetime
    ) -> Dict[int, VehicleAvailability]:
        """Calculate time-slotted availability for each vehicle."""
        time_slots = self._build_time_slots(planning_start, planning_end)
        availability_matrices = {}
        
        for vehicle in vehicles:
            routes = vehicle_routes.get(vehicle.vehicle_id, [])
            availability = [True] * len(time_slots)
            
            # Mark slots as unavailable when vehicle is on route
            for route in routes:
                for t_idx, slot_time in enumerate(time_slots):
                    slot_end = slot_time + timedelta(minutes=30)
                    
                    # Check if slot overlaps with route
                    if (slot_time < route.plan_end_date_time and slot_end > route.plan_start_date_time):
                        availability[t_idx] = False
            
            availability_matrices[vehicle.vehicle_id] = VehicleAvailability(
                vehicle_id=vehicle.vehicle_id,
                time_slots=time_slots,
                availability_matrix=availability
            )
        
        return availability_matrices
    
    def _build_time_slots(self, start: datetime, end: datetime) -> List[datetime]:
        """Build 30-minute time slots."""
        slots = []
        current = start
        
        while current < end:
            slots.append(current)
            current += timedelta(minutes=30)
        
        return slots
    
    def _load_forecast_data(self, start: datetime, end: datetime) -> Dict[datetime, float]:
        """Load site energy forecast data."""
        forecast_data = {}
        
        with db.get_cursor() as cur:
            cur.execute(
                Queries.GET_FORECAST_DATA,
                (self.site_id, start, end)
            )
            
            for row in cur.fetchall():
                forecast_data[row['forecasted_date_time']] = row['forecasted_consumption']
        
        logger.info(f"Loaded {len(forecast_data)} forecast data points")
        return forecast_data
    
    def _load_price_data(self, start: datetime, end: datetime) -> Dict[datetime, Tuple[float, bool]]:
        """Load electricity price data."""
        price_data = {}
        
        with db.get_cursor() as cur:
            cur.execute(
                Queries.GET_PRICE_DATA,
                (start, end)
            )
            
            for row in cur.fetchall():
                price_data[row['date_time']] = (row['electricty_price_fixed'], row['triad'])
        
        logger.info(f"Loaded {len(price_data)} price data points")
        return price_data
    
    def _build_optimization_config(self, mode: OptimizationMode) -> UnifiedOptimizationConfig:
        """Build optimization configuration from MAF parameters."""
        config = UnifiedOptimizationConfig(mode=mode)
        
        # Load site capacity for scheduling
        if mode in (OptimizationMode.SCHEDULING_ONLY, OptimizationMode.INTEGRATED):
            with db.get_cursor() as cur:
                cur.execute(
                    Queries.GET_SITE_ASC,
                    (self.site_id,)
                )
                row = cur.fetchone()
                if row and row['ASC']:
                    config.site_capacity_kw = float(row['ASC'])
        
        return config
    
    def _persist_allocation(self, result: AllocationResult):
        """Persist allocation results to database."""
        logger.info(f"Persisting {len(result.allocations)} allocations")
        
        # Delete existing allocations for site
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
                'N',  # New status
                alloc.estimated_arrival,
                alloc.estimated_arrival_soc,
                -1,  # Sequence number placeholder
                alloc.vehicle_id  # Preferred vehicle
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
    
    def _validate_schedule(
        self,
        result: ChargeScheduleResult,
        energy_requirements: Dict[int, List[RouteEnergyRequirement]]
    ):
        """Validate schedule meets route energy requirements."""
        for vehicle_schedule in result.vehicle_schedules:
            vehicle_id = vehicle_schedule.vehicle_id
            reqs = energy_requirements.get(vehicle_id, [])
            
            if not reqs:
                continue

            # Final SOC = initial + energy scheduled (VehicleChargeSchedule has no final_soc_kwh)
            final_soc_kwh = vehicle_schedule.initial_soc_kwh + vehicle_schedule.total_energy_scheduled_kwh
            max_required = max(req.cumulative_energy_kwh for req in reqs)

            if final_soc_kwh < max_required - 1.0:  # 1 kWh tolerance
                logger.warning(
                    f"Vehicle {vehicle_id} may not meet energy requirements: "
                    f"final_soc={final_soc_kwh:.1f} kWh, required={max_required:.1f} kWh"
                )
    
    def _persist_schedule(
        self,
        result: ChargeScheduleResult,
        energy_requirements: Dict[int, List[RouteEnergyRequirement]]
    ):
        """Persist schedule results to database."""
        all_time_slots = self._build_time_slots(result.planning_start, result.planning_end)
        n_slots = len(all_time_slots)
        
        logger.info(
            f"[t_charge_schedule] Persisting: schedule_id={self.schedule_id} | "
            f"time_slots={n_slots} | vehicles={len(result.vehicle_schedules)}"
        )
        
        with db.get_cursor() as cur:
            # Delete existing schedule entries
            cur.execute(
                Queries.DELETE_CHARGE_SCHEDULE_BY_SCHEDULE_ID,
                (self.schedule_id,)
            )
            
            total_inserted = 0
            
            for vehicle_schedule in result.vehicle_schedules:
                vehicle_id = vehicle_schedule.vehicle_id
                connector_id = (
                    str(vehicle_schedule.assigned_charger_id)
                    if vehicle_schedule.assigned_charger_id is not None
                    else "1"
                )
                slot_power_map = {
                    slot.time_slot: slot.charge_power_kw
                    for slot in vehicle_schedule.charge_slots
                }
                for slot_time in all_time_slots:
                    charge_power = slot_power_map.get(slot_time, 0.0)
                    cur.execute(
                        Queries.INSERT_CHARGE_SCHEDULE,
                        (
                            self.schedule_id,
                            vehicle_id,
                            slot_time,
                            charge_power,
                            None,   # power_unit_id
                            True,   # charge_profile_flag
                            connector_id,
                            datetime.utcnow(),
                            250,    # capacity_line (required, not null)
                            None,   # opt_level
                        ),
                    )
                    total_inserted += 1
        
        logger.info(f"[t_charge_schedule] Persist complete: total_rows_inserted={total_inserted}")
    
    def _update_scheduler_status(self, status: str, actual_hours: Optional[float]):
        """Update scheduler status."""
        with db.get_cursor() as cur:
            cur.execute(
                Queries.UPDATE_SCHEDULER_STATUS,
                (status, self.schedule_id)
            )
        
        logger.info(f"Updated scheduler status: {status}")
    
    def get_schedule_report(
        self, schedule_id: int, timestamp: datetime
    ) -> ScheduleReport:
        """
        Produce a read-only report for a persisted schedule.
        
        Args:
            schedule_id: Schedule ID (from t_scheduler).
            timestamp: As-of time for vehicle state and report context.
        
        Returns:
            ScheduleReport with charging/allocation stats, charging time
            before first route and between routes, end-of-plan SOC, and other details.
        """
        # 1. Load schedule metadata
        config_rows = db.execute_query(
            Queries.GET_SCHEDULER_CONFIG,
            (schedule_id,),
            fetch=True
        )
        if not config_rows:
            raise ValueError(f"Schedule ID {schedule_id} not found")
        row = config_rows[0]
        site_id = row['device_id']
        schedule_status = row.get('status')
        
        # 2. Load charge schedule
        charge_rows = db.execute_query(
            Queries.GET_CHARGE_SCHEDULE_BY_SCHEDULE_ID,
            (schedule_id,),
            fetch=True
        )
        
        if not charge_rows:
            return ScheduleReport(
                schedule_id=schedule_id,
                site_id=site_id,
                report_timestamp=timestamp,
                schedule_status=schedule_status,
                notes=['No charge data for this schedule.']
            )
        
        # Derive planning window and per-vehicle charge data
        slot_duration_hours = 0.5  # 30 min
        all_slot_times = {r['charge_start_date_time'] for r in charge_rows}
        planning_start = min(all_slot_times)
        planning_end = max(all_slot_times) + timedelta(minutes=30)
        
        # Per vehicle: list of (slot_time, charge_power_kw), total energy
        vehicle_charge_slots: Dict[int, List[Tuple[datetime, float]]] = {}
        vehicle_energy: Dict[int, float] = {}
        for r in charge_rows:
            vid = r['vehicle_id']
            t = r['charge_start_date_time']
            p = float(r['charge_power'] or 0)
            if vid not in vehicle_charge_slots:
                vehicle_charge_slots[vid] = []
                vehicle_energy[vid] = 0.0
            vehicle_charge_slots[vid].append((t, p))
            vehicle_energy[vid] += p * slot_duration_hours
        
        total_energy_scheduled_kwh = sum(vehicle_energy.values())
        vehicles_scheduled = len(vehicle_charge_slots)
        
        # 3. Load routes: prefer allocated routes (t_route_allocated) for vehicle-route mapping
        allocated_route_rows = db.execute_query(
            Queries.GET_ALLOCATED_ROUTES_IN_WINDOW,
            (site_id, planning_start, planning_end),
            fetch=True
        )
        route_rows = db.execute_query(
            Queries.GET_ROUTES_IN_WINDOW,
            (site_id, planning_start, planning_end),
            fetch=True
        )
        routes = [Route(**r) for r in route_rows] if route_rows else []
        routes_in_window = len(routes)

        vehicle_routes: Dict[int, List[Route]] = {}
        if allocated_route_rows:
            allocated_routes = [Route(**r) for r in allocated_route_rows]
            for route in allocated_routes:
                vid = route.vehicle_id
                if vid is not None:
                    vehicle_routes.setdefault(vid, []).append(route)
            for vid in vehicle_routes:
                vehicle_routes[vid].sort(key=lambda r: r.plan_start_date_time)
        else:
            for route in routes:
                vid = route.vehicle_id
                if vid is not None:
                    vehicle_routes.setdefault(vid, []).append(route)
            for vid in vehicle_routes:
                vehicle_routes[vid].sort(key=lambda r: r.plan_start_date_time)

        vehicles_with_routes = len(vehicle_routes)

        # 4. Allocation stats (count from t_route_allocated in window)
        routes_allocated = None
        if routes:
            route_ids = [r.route_id for r in routes]
            alloc_rows = db.execute_query(
                Queries.GET_EXISTING_ALLOCATIONS,
                (site_id, route_ids),
                fetch=True
            )
            routes_allocated = len(alloc_rows) if alloc_rows else 0
        
        # 5. Vehicle state at timestamp (query expects as_of_time first, then vehicle_id)
        vehicle_state: Dict[int, dict] = {}
        for vid in vehicle_charge_slots:
            state_rows = db.execute_query(
                Queries.GET_VEHICLE_CHARGE_STATE_AS_OF,
                (timestamp, vid),
                fetch=True
            )
            if state_rows:
                s = state_rows[0]
                battery = float(s['battery_capacity'] or 0)
                soc_pct = float(s['estimated_soc']) if s.get('estimated_soc') is not None else None
                ac_kw = float(s['charge_power_ac'] or 0) or 11.0
                dc_kw = float(s['charge_power_dc'] or 0) or 50.0
                is_dc = s.get('is_dc_charger')
                eff = s.get('efficiency_kwh_mile')
                vehicle_state[vid] = {
                    'battery_capacity_kwh': battery,
                    'initial_soc_percent': soc_pct,
                    'initial_soc_kwh': (soc_pct / 100.0 * battery) if soc_pct is not None else None,
                    'charge_rate_kw': dc_kw if is_dc else ac_kw,
                    'efficiency_kwh_mile': float(eff) if eff is not None else None,
                }
        
        # Fleet efficiency for missing vehicle efficiency
        fleet_eff = self.fleet_avg_efficiency
        if fleet_eff == 0.35 and site_id:
            try:
                eff_rows = db.execute_query(Queries.GET_FLEET_EFFICIENCY, (site_id,), fetch=True)
                if eff_rows and eff_rows[0].get('fleet_avg_efficiency') is not None:
                    fleet_eff = float(eff_rows[0]['fleet_avg_efficiency'])
            except Exception:
                pass
        
        # 6 & 7. Per-vehicle: charging before first route, between routes, end SOC
        vehicle_reports: List[VehicleScheduleReport] = []
        total_charging_minutes_fleet = 0.0
        
        for vid in sorted(vehicle_charge_slots.keys()):
            slots = vehicle_charge_slots[vid]
            total_kwh = vehicle_energy[vid]
            state = vehicle_state.get(vid)
            initial_soc_kwh = state['initial_soc_kwh'] if state else None
            initial_soc_percent = state['initial_soc_percent'] if state else None
            battery_kwh = state['battery_capacity_kwh'] if state else None
            charge_rate_kw = (state['charge_rate_kw'] if state else 11.0) or 11.0
            efficiency = (state['efficiency_kwh_mile'] if state else None) or fleet_eff
            
            vroutes = vehicle_routes.get(vid, [])
            
            # Charging before first route
            charging_minutes_before_first_route = None
            charging_minutes_between_routes_list: List[float] = []
            if vroutes:
                first_start = vroutes[0].plan_start_date_time
                energy_before = sum(
                    p * slot_duration_hours
                    for t, p in slots
                    if t < first_start and p > 0
                )
                if charge_rate_kw > 0:
                    charging_minutes_before_first_route = (energy_before / charge_rate_kw) * 60.0
                else:
                    charging_minutes_before_first_route = 0.0
                
                # Between routes
                for i in range(len(vroutes) - 1):
                    gap_start = vroutes[i].plan_end_date_time
                    gap_end = vroutes[i + 1].plan_start_date_time
                    energy_between = sum(
                        p * slot_duration_hours
                        for t, p in slots
                        if gap_start <= t < gap_end and p > 0
                    )
                    if charge_rate_kw > 0:
                        mins = (energy_between / charge_rate_kw) * 60.0
                    else:
                        mins = 0.0
                    charging_minutes_between_routes_list.append(mins)
            else:
                # No routes: all charging is "before first route"
                if charge_rate_kw > 0 and total_kwh > 0:
                    charging_minutes_before_first_route = (total_kwh / charge_rate_kw) * 60.0
                else:
                    charging_minutes_before_first_route = 0.0
            
            total_between = sum(charging_minutes_between_routes_list)
            total_charging_minutes_fleet += (charging_minutes_before_first_route or 0) + total_between
            
            # Route energy consumed
            energy_required_for_routes_kwh = 0.0
            for r in vroutes:
                energy_required_for_routes_kwh += (r.plan_mileage or 0) * efficiency
            
            # End-of-plan SOC
            estimated_final_soc_kwh = None
            estimated_final_soc_percent = None
            if initial_soc_kwh is not None and battery_kwh and battery_kwh > 0:
                final_kwh = initial_soc_kwh + total_kwh - energy_required_for_routes_kwh
                final_kwh = max(0.0, min(battery_kwh, final_kwh))
                estimated_final_soc_kwh = final_kwh
                estimated_final_soc_percent = 100.0 * final_kwh / battery_kwh
            
            allocated_route_ids = [r.route_id for r in vroutes]
            allocated_routes = [
                {
                    'route_id': r.route_id,
                    'plan_start_date_time': r.plan_start_date_time.isoformat(),
                    'plan_end_date_time': r.plan_end_date_time.isoformat(),
                    'plan_mileage': r.plan_mileage,
                    'route_status': getattr(r, 'route_status', None),
                }
                for r in vroutes
            ]
            vehicle_reports.append(
                VehicleScheduleReport(
                    vehicle_id=vid,
                    initial_soc_kwh=initial_soc_kwh,
                    initial_soc_percent=initial_soc_percent,
                    battery_capacity_kwh=battery_kwh,
                    total_energy_scheduled_kwh=total_kwh,
                    charging_minutes_before_first_route=charging_minutes_before_first_route,
                    charging_minutes_between_routes=charging_minutes_between_routes_list,
                    total_charging_minutes_between_routes=total_between,
                    estimated_final_soc_kwh=estimated_final_soc_kwh,
                    estimated_final_soc_percent=estimated_final_soc_percent,
                    energy_required_for_routes_kwh=energy_required_for_routes_kwh,
                    charge_rate_kw=charge_rate_kw,
                    allocated_route_ids=allocated_route_ids,
                    routes_allocated_count=len(allocated_route_ids),
                    allocated_routes=allocated_routes,
                )
            )
        
        return ScheduleReport(
            schedule_id=schedule_id,
            site_id=site_id,
            report_timestamp=timestamp,
            planning_start=planning_start,
            planning_end=planning_end,
            schedule_status=schedule_status,
            vehicles_scheduled=vehicles_scheduled,
            total_energy_scheduled_kwh=total_energy_scheduled_kwh,
            routes_in_window=routes_in_window,
            routes_allocated=routes_allocated,
            vehicles_with_routes=vehicles_with_routes,
            total_charging_minutes_fleet=total_charging_minutes_fleet,
            vehicle_reports=vehicle_reports,
        )

    def _log_error(self, error_message: str):
        """Log error to database."""
        try:
            if self.allocation_id:
                db.execute_query(
                    Queries.INSERT_ALLOCATION_LOG,
                    (self.allocation_id, error_message),
                    fetch=False
                )
        except Exception as e:
            logger.error(f"Failed to log error: {e}")
    
    def close(self):
        """Close database connection."""
        db.close()
