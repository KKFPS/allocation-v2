"""Testing framework for unified optimizer."""
import argparse
import json
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import numpy as np

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.optimizer import (
    UnifiedOptimizer, 
    UnifiedOptimizationConfig, 
    UnifiedOptimizationResult,
    OptimizationMode
)
from src.controllers.unified_controller import UnifiedController
from src.models.vehicle import Vehicle
from src.models.route import Route
from src.models.scheduler import (
    VehicleChargeState, RouteEnergyRequirement, VehicleAvailability
)
from src.database.connection import db
from src.database.queries import Queries
from src.utils.logging_config import logger


class UnifiedOptimizerTestFramework:
    """Framework for testing unified optimizer with custom scenarios."""
    
    def __init__(self):
        """Initialize test framework."""
        self.test_results = []
        self.db_connected = False
        self.controller = None
    
    def connect_database(self, site_id: int):
        """Connect to database if needed."""
        if not self.db_connected:
            db.connect()
            self.db_connected = True
            self.site_id = site_id
    
    def run_test_scenario(
        self,
        site_id: int,
        start_time: datetime,
        mode: str = 'integrated',
        window_hours: int = 18,
        # Optimization weights
        allocation_weight: float = 1.0,
        scheduling_weight: float = 1.0,
        route_count_weight: float = 1e2,
        # Time limits
        allocation_time_limit: int = 30,
        scheduling_time_limit: int = 300,
        integrated_time_limit: int = 330,
        # Scheduling parameters
        target_soc_percent: float = 75.0,
        site_capacity_kw: float = 200.0,
        triad_penalty_factor: float = 100.0,
        synthetic_time_price_factor: float = 0.01,
        # Test configuration
        use_database: bool = True,
        custom_sequences: Optional[List[Tuple]] = None,
        custom_vehicles: Optional[List[Vehicle]] = None,
        persist_to_database: bool = False,
    ) -> Dict:
        """
        Run unified optimizer test scenario.
        
        Args:
            site_id: Site identifier
            start_time: Optimization start time
            mode: Optimization mode ('allocation_only', 'scheduling_only', 'integrated')
            window_hours: Planning window duration in hours
            allocation_weight: α weight for allocation score
            scheduling_weight: β weight for scheduling cost
            route_count_weight: Priority weight for route coverage
            allocation_time_limit: Time limit for allocation phase (seconds)
            scheduling_time_limit: Time limit for scheduling phase (seconds)
            integrated_time_limit: Time limit for integrated mode (seconds)
            target_soc_percent: Target SOC for vehicles
            site_capacity_kw: Available site capacity
            triad_penalty_factor: TRIAD period penalty
            synthetic_time_price_factor: Time preference factor
            use_database: Whether to load data from database
            custom_sequences: Custom allocation sequences (overrides database)
            custom_vehicles: Custom vehicles (overrides database)
        
        Returns:
            Test result dictionary
        """
        logger.info(f"\n{'='*70}")
        logger.info(f"UNIFIED OPTIMIZER TEST: Site {site_id}")
        logger.info(f"Mode: {mode.upper()}")
        logger.info(f"Start Time: {start_time}")
        logger.info(f"Window: {window_hours} hours")
        logger.info(f"Weights: α={allocation_weight}, β={scheduling_weight}")
        logger.info(f"Persist: {persist_to_database}")
        logger.info(f"{'='*70}\n")
        
        try:
            # Connect to database
            if use_database:
                self.connect_database(site_id)
            
            # Build optimization config
            opt_mode = self._parse_mode(mode)
            config = UnifiedOptimizationConfig(
                mode=opt_mode,
                allocation_time_limit=allocation_time_limit,
                scheduling_time_limit=scheduling_time_limit,
                integrated_time_limit=integrated_time_limit,
                allocation_score_weight=allocation_weight,
                scheduling_cost_weight=scheduling_weight,
                route_count_weight=route_count_weight,
                target_soc_percent=target_soc_percent,
                site_capacity_kw=site_capacity_kw,
                triad_penalty_factor=triad_penalty_factor,
                synthetic_time_price_factor=synthetic_time_price_factor
            )
            
            # Initialize unified controller
            self.controller = UnifiedController(
                site_id=site_id,
                trigger_type='test'
            )
            
            # Run unified optimization
            start_solve = time.time()
            
            allocation_result, schedule_result, unified_result = self.controller.run_unified_optimization(
                current_time=start_time,
                mode=mode,
                config=config,
                persist_to_database=persist_to_database
            )
            
            total_execution_time = time.time() - start_solve
            
            # Build test result
            test_result = {
                'site_id': site_id,
                'start_time': start_time,
                'window_hours': window_hours,
                'mode': mode,
                'status': unified_result.status,
                'solve_time_seconds': unified_result.solve_time_seconds,
                'total_execution_time': total_execution_time,
                'allocation_score': unified_result.allocation_score,
                'routes_allocated': unified_result.routes_allocated,
                'routes_total': unified_result.routes_total,
                'total_charging_cost': unified_result.total_charging_cost,
                'total_energy_kwh': unified_result.total_energy_kwh,
                'objective_value': unified_result.objective_value,
                'allocation_id': self.controller.allocation_id,
                'schedule_id': self.controller.schedule_id,
                'persisted': persist_to_database
            }
            
            self.test_results.append(test_result)
            self._print_test_summary(test_result)
            
            return test_result
            # Parse mode
            mode_enum = self._parse_mode(mode)
            
            # Create configuration
            config = UnifiedOptimizationConfig(
                mode=mode_enum,
                allocation_time_limit=allocation_time_limit,
                scheduling_time_limit=scheduling_time_limit,
                integrated_time_limit=integrated_time_limit,
                route_count_weight=route_count_weight,
                allocation_score_weight=allocation_weight,
                scheduling_cost_weight=scheduling_weight,
                target_soc_shortfall_penalty=0.2,
                triad_penalty_factor=triad_penalty_factor,
                synthetic_time_price_factor=synthetic_time_price_factor,
                target_soc_percent=target_soc_percent,
                site_capacity_kw=site_capacity_kw
            )
            
            # Initialize optimizer
            optimizer = UnifiedOptimizer(config)
            
            # Prepare data
            logger.info("Preparing optimization data...")
            opt_data = self._prepare_optimization_data(
                site_id, start_time, window_hours, mode_enum,
                use_database, custom_sequences, custom_vehicles
            )
            
            # Run optimization
            logger.info(f"Running optimization in {mode} mode...")
            exec_start = datetime.now()
            
            result = optimizer.solve(
                sequences=opt_data.get('sequences'),
                route_ids=opt_data.get('route_ids'),
                sequence_costs=opt_data.get('sequence_costs'),
                schedule_id=opt_data.get('schedule_id'),
                vehicles=opt_data.get('vehicles'),
                vehicle_states=opt_data.get('vehicle_states'),
                energy_requirements=opt_data.get('energy_requirements'),
                availability_matrices=opt_data.get('availability_matrices'),
                time_slots=opt_data.get('time_slots'),
                forecast_data=opt_data.get('forecast_data'),
                price_data=opt_data.get('price_data'),
                fix_allocation=opt_data.get('fix_allocation'),
                fix_scheduling=opt_data.get('fix_scheduling', False)
            )
            
            exec_end = datetime.now()
            total_execution_time = (exec_end - exec_start).total_seconds()
            
            # Persist to database if requested
            if persist_to_database and use_database and result.status not in ['FAILED', 'INFEASIBLE']:
                self._persist_results_to_database(
                    site_id, start_time, window_hours, result, opt_data
                )
            
            # Build test result
            test_result = self._build_test_result(
                site_id, start_time, window_hours, mode, config,
                opt_data, result, total_execution_time
            )
            
            self.test_results.append(test_result)
            
            # Print summary
            self._print_test_summary(test_result)
            
            return test_result
        
        except Exception as e:
            logger.error(f"Test scenario failed: {e}", exc_info=True)
            
            # Build error result
            error_result = {
                'site_id': site_id,
                'start_time': start_time,
                'window_hours': window_hours,
                'mode': mode,
                'status': 'failed',
                'error': str(e),
                'solve_time_seconds': 0.0,
                'total_execution_time': 0.0,
                'allocation_score': 0.0,
                'routes_allocated': 0,
                'routes_total': 0,
                'total_charging_cost': 0.0,
                'total_energy_kwh': 0.0,
                'objective_value': 0.0,
                'allocation_id': None,
                'schedule_id': None,
                'persisted': False
            }
            
            self.test_results.append(error_result)
            return error_result
            logger.error(f"Test failed: {e}", exc_info=True)
            
            test_result = {
                'site_id': site_id,
                'start_time': start_time.isoformat(),
                'mode': mode,
                'status': 'FAILED',
                'error': str(e),
                'success': False
            }
            
            self.test_results.append(test_result)
            return test_result
    
    def run_multiple_scenarios(self, scenarios: List[Dict]) -> List[Dict]:
        """
        Run multiple test scenarios.
        
        Args:
            scenarios: List of scenario configurations
        
        Returns:
            List of test results
        """
        logger.info(f"\n{'#'*70}")
        logger.info(f"RUNNING {len(scenarios)} UNIFIED OPTIMIZER TEST SCENARIOS")
        logger.info(f"{'#'*70}\n")
        
        results = []
        
        for i, scenario in enumerate(scenarios, 1):
            logger.info(f"\n--- Scenario {i}/{len(scenarios)} ---")
            result = self.run_test_scenario(**scenario)
            results.append(result)
        
        # Print overall summary
        self._print_overall_summary(results)
        
        return results
    
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
            raise ValueError(
                f"Invalid mode '{mode}'. Must be one of: "
                f"{', '.join(mode_map.keys())}"
            )
        
        return mode_map[mode_lower]
    
    def _prepare_optimization_data(
        self,
        site_id: int,
        start_time: datetime,
        window_hours: int,
        mode: OptimizationMode,
        use_database: bool,
        custom_sequences: Optional[List[Tuple]],
        custom_vehicles: Optional[List[Vehicle]]
    ) -> Dict:
        """Prepare optimization data from database or custom inputs."""
        opt_data = {}
        
        # Allocation data
        if mode in (OptimizationMode.ALLOCATION_ONLY, OptimizationMode.INTEGRATED):
            if custom_sequences:
                logger.info(f"Using {len(custom_sequences)} custom sequences")
                opt_data['sequences'] = custom_sequences
                
                # Extract route IDs
                route_ids = set()
                for _, route_seq, _ in custom_sequences:
                    for route in route_seq:
                        route_ids.add(route.route_id)
                opt_data['route_ids'] = list(route_ids)
                
                # Sequence costs
                opt_data['sequence_costs'] = np.array([seq[2] for seq in custom_sequences])
            
            elif use_database:
                logger.info("Loading allocation data from database...")
                self.connect_database(site_id)
                
                # Load routes in window
                window_end = start_time + timedelta(hours=window_hours)
                routes = self._get_routes_in_window(site_id, start_time, window_end)
                opt_data['route_ids'] = [r.route_id for r in routes]
                
                # Load vehicles
                vehicles = self._get_available_vehicles(site_id)
                for v in vehicles:
                    v.available_time = start_time
                
                # Generate sequences (simplified - normally from allocation controller)
                sequences = self._generate_test_sequences(vehicles, routes)
                opt_data['sequences'] = sequences
                opt_data['sequence_costs'] = np.array([seq[2] for seq in sequences])
                
                logger.info(
                    f"Loaded {len(routes)} routes, {len(vehicles)} vehicles, "
                    f"{len(sequences)} sequences"
                )
        
        # Scheduling data
        if mode in (OptimizationMode.SCHEDULING_ONLY, OptimizationMode.INTEGRATED):
            if custom_vehicles:
                vehicles = custom_vehicles
                logger.info(f"Using {len(custom_vehicles)} custom vehicles")
            elif use_database:
                self.connect_database(site_id)
                vehicles = self._get_available_vehicles(site_id)
                logger.info(f"Loaded {len(vehicles)} vehicles from database")
            else:
                raise ValueError("No vehicles provided for scheduling")
            for v in vehicles:
                v.available_time = getattr(v, 'available_time', None) or start_time
            opt_data['vehicles'] = vehicles
            opt_data['schedule_id'] = int(start_time.timestamp())
            
            # Generate time slots (30-minute intervals)
            time_slots = self._generate_time_slots(start_time, window_hours)
            opt_data['time_slots'] = time_slots
            
            # Vehicle states
            vehicle_states = self._get_vehicle_states(vehicles)
            opt_data['vehicle_states'] = vehicle_states
            
            # Energy requirements (from routes if integrated, else empty)
            if mode == OptimizationMode.INTEGRATED and opt_data.get('sequences'):
                energy_requirements = self._extract_energy_requirements(
                    opt_data['sequences'], vehicle_states
                )
            elif mode == OptimizationMode.SCHEDULING_ONLY and use_database:
                # Load allocated routes from database
                self.connect_database(site_id)
                energy_requirements = self._load_energy_requirements_from_db(
                    site_id, vehicles, window_hours
                )
            else:
                energy_requirements = {v.vehicle_id: [] for v in vehicles}
            
            opt_data['energy_requirements'] = energy_requirements
            
            # Availability matrices
            availability_matrices = self._generate_availability_matrices(
                vehicles, time_slots, energy_requirements
            )
            opt_data['availability_matrices'] = availability_matrices
            
            # Forecast data (site demand)
            forecast_data = self._generate_forecast_data(time_slots)
            opt_data['forecast_data'] = forecast_data
            
            # Price data (electricity prices + TRIAD flags)
            price_data = self._generate_price_data(time_slots)
            opt_data['price_data'] = price_data
            
            logger.info(
                f"Prepared scheduling data: {len(vehicles)} vehicles, "
                f"{len(time_slots)} time slots"
            )
        
        return opt_data
    
    def _generate_test_sequences(
        self, 
        vehicles: List[Vehicle], 
        routes: List[Route]
    ) -> List[Tuple]:
        """Generate simple test sequences (vehicle_id, route_sequence, cost)."""
        sequences = []
        
        for vehicle in vehicles:
            # Single route sequences
            for route in routes:
                if route.plan_mileage > 0:
                    energy_needed = route.plan_mileage * vehicle.efficiency_kwh_mile
                    if energy_needed <= vehicle.battery_capacity:
                        cost = 10.0  # Base score
                        sequences.append((vehicle.vehicle_id, [route], cost))
        
        return sequences
    
    def _persist_results_to_database(
        self,
        site_id: int,
        start_time: datetime,
        window_hours: int,
        result: UnifiedOptimizationResult,
        opt_data: Dict
    ):
        """Persist optimization results to database tables."""
        logger.info(
            "[DATABASE PERSISTENCE] Starting persistence: mode=%s | site_id=%s",
            result.mode.value, site_id
        )
        
        try:
            # Create allocation monitor entry if needed
            allocation_id = None
            schedule_id = opt_data.get('schedule_id')
            
            if result.mode in (OptimizationMode.ALLOCATION_ONLY, OptimizationMode.INTEGRATED):
                allocation_id = self._persist_allocation_results(
                    site_id, start_time, window_hours, result
                )
            
            if result.mode in (OptimizationMode.SCHEDULING_ONLY, OptimizationMode.INTEGRATED):
                self._persist_scheduling_results(
                    site_id, schedule_id, start_time, window_hours,
                    result, opt_data
                )
            
            logger.info(
                "[DATABASE PERSISTENCE] Complete: allocation_id=%s | schedule_id=%s",
                allocation_id, schedule_id
            )
        
        except Exception as e:
            logger.error(f"[DATABASE PERSISTENCE] Failed: {e}", exc_info=True)
            raise
    
    def _persist_allocation_results(
        self,
        site_id: int,
        start_time: datetime,
        window_hours: int,
        result: UnifiedOptimizationResult
    ) -> int:
        """Persist allocation results to t_route_allocated and t_route_allocated_history."""
        logger.info(
            "[t_route_allocated] Persisting allocation: routes_allocated=%s | sequences=%s",
            result.routes_allocated, len(result.selected_sequences)
        )
        
        # Create allocation monitor entry
        window_end = start_time + timedelta(hours=window_hours)
        allocation_result = db.execute_query(
            Queries.CREATE_ALLOCATION_MONITOR,
            (
                site_id,
                'P',  # Pending status
                'test',  # Trigger type
                datetime.now(),
                start_time,
                window_end
            ),
            fetch=True
        )
        
        allocation_id = allocation_result[0]['allocation_id']
        logger.info(
            "[t_allocation_monitor] Created: allocation_id=%s | window=%s to %s",
            allocation_id, start_time.isoformat(), window_end.isoformat()
        )
        
        # Delete existing allocations for site
        db.execute_query(
            Queries.DELETE_SITE_ALLOCATIONS,
            (site_id,),
            fetch=False
        )
        logger.info("[t_route_allocated] Deleted existing allocations for site_id=%s", site_id)
        
        # Prepare allocation rows
        allocation_rows = []
        history_rows = []
        
        for vehicle_id, route_sequence, cost in result.selected_sequences:
            for route in route_sequence:
                row = (
                    allocation_id,
                    route.route_id,
                    site_id,
                    vehicle_id,
                    'N',  # Status (not sent)
                    route.plan_end_date_time,  # estimated_arrival
                    80.0,  # estimated_arrival_soc (placeholder)
                    -1,  # http_response (not sent yet)
                    vehicle_id  # vehicle_id_actual
                )
                allocation_rows.append(row)
                history_rows.append(row)
        
        # Insert allocations
        if allocation_rows:
            db.execute_many(Queries.INSERT_ROUTE_ALLOCATED, allocation_rows)
            db.execute_many(Queries.INSERT_ROUTE_ALLOCATED_HISTORY, history_rows)
            
            logger.info(
                "[t_route_allocated] Inserted %s allocations (%s vehicles)",
                len(allocation_rows),
                len(set(row[3] for row in allocation_rows))
            )
            logger.info(
                "[t_route_allocated_history] Inserted %s history records",
                len(history_rows)
            )
        else:
            logger.warning("[t_route_allocated] No allocations to persist")
        
        # Update allocation monitor with final status
        db.execute_query(
            Queries.UPDATE_ALLOCATION_MONITOR,
            (
                'A',  # Accepted status
                result.allocation_score,
                result.routes_total,
                result.routes_allocated,
                0,  # routes_overlapping_count
                allocation_id
            ),
            fetch=False
        )
        logger.info(
            "[t_allocation_monitor] Updated: allocation_id=%s | status=A | score=%.2f",
            allocation_id, result.allocation_score
        )
        
        return allocation_id
    
    def _persist_scheduling_results(
        self,
        site_id: int,
        schedule_id: int,
        start_time: datetime,
        window_hours: int,
        result: UnifiedOptimizationResult,
        opt_data: Dict
    ):
        """Persist scheduling results to t_charge_schedule."""
        window_end = start_time + timedelta(hours=window_hours)
        time_slots = opt_data.get('time_slots', [])
        
        logger.info(
            "[t_charge_schedule] Persisting schedule: schedule_id=%s | "
            "vehicles=%s | time_slots=%s | window=%s to %s",
            schedule_id, len(result.vehicle_schedules), len(time_slots),
            start_time.isoformat(), window_end.isoformat()
        )
        
        with db.get_cursor() as cur:
            # Delete existing schedule data
            cur.execute(Queries.DELETE_CHARGE_SCHEDULE_BY_SCHEDULE_ID, (schedule_id,))
            deleted = cur.rowcount if hasattr(cur, 'rowcount') else 0
            logger.info(
                "[t_charge_schedule] Deleted existing rows for schedule_id=%s (count=%s)",
                schedule_id, deleted
            )
            
            total_inserted = 0
            total_charging_slots = 0
            
            # Insert one row per (vehicle, time_slot)
            for vehicle_schedule in result.vehicle_schedules:
                connector_id = (
                    str(vehicle_schedule.assigned_charger_id)
                    if vehicle_schedule.assigned_charger_id is not None
                    else "1"
                )
                
                # Map time_slot -> charge_power_kw
                power_by_slot = {
                    slot.time_slot: slot.charge_power_kw
                    for slot in (vehicle_schedule.charge_slots or [])
                }
                
                vehicle_inserted = 0
                vehicle_charging_slots = 0
                
                for slot_time in time_slots:
                    power_kw = power_by_slot.get(slot_time, 0.0)
                    
                    cur.execute(Queries.INSERT_CHARGE_SCHEDULE, (
                        schedule_id,
                        vehicle_schedule.vehicle_id,
                        slot_time,
                        power_kw,
                        None,  # power_unit_id
                        True,  # charge_profile_flag
                        connector_id,
                        datetime.utcnow(),
                        250,   # capacity_line
                        None,  # opt_level
                    ))
                    
                    vehicle_inserted += 1
                    if power_kw > 0.01:
                        vehicle_charging_slots += 1
                
                total_inserted += vehicle_inserted
                total_charging_slots += vehicle_charging_slots
                
                logger.info(
                    "[t_charge_schedule] vehicle_id=%s | total_slots=%s | "
                    "charging_slots=%s | total_energy=%.2f kWh | charger_id=%s",
                    vehicle_schedule.vehicle_id,
                    vehicle_inserted,
                    vehicle_charging_slots,
                    vehicle_schedule.total_energy_scheduled_kwh,
                    connector_id
                )
        
        logger.info(
            "[t_charge_schedule] Persist complete: schedule_id=%s | "
            "total_rows=%s | slots_with_charging=%s | total_energy=%.2f kWh | total_cost=£%.2f",
            schedule_id, total_inserted, total_charging_slots,
            result.total_energy_kwh, result.total_charging_cost
        )
    
    def _get_routes_in_window(
        self,
        site_id: int,
        start_time: datetime,
        end_time: datetime
    ) -> List[Route]:
        """Load routes from database in time window."""
        rows = db.execute_query(
            Queries.GET_ROUTES_IN_WINDOW,
            (site_id, start_time, end_time),
            fetch=True
        )
        
        routes = []
        for row in rows:
            route = Route(**row)
            routes.append(route)
        
        return routes
    
    def _get_available_vehicles(self, site_id: int) -> List[Vehicle]:
        """Load available vehicles from database."""
        rows = db.execute_query(
            Queries.GET_ACTIVE_VEHICLES,
            (site_id,),
            fetch=True
        )
        
        vehicles = []
        for row in rows:
            vehicle = Vehicle(**row)
            vehicles.append(vehicle)
        
        return vehicles
    
    def _generate_time_slots(
        self, 
        start_time: datetime, 
        window_hours: int
    ) -> List[datetime]:
        """Generate 30-minute time slots."""
        slots = []
        current = start_time
        end = start_time + timedelta(hours=window_hours)
        
        while current < end:
            slots.append(current)
            current += timedelta(minutes=30)
        
        return slots
    
    def _get_vehicle_states(
        self, 
        vehicles: List[Vehicle]
    ) -> Dict[int, VehicleChargeState]:
        """Get current vehicle charge states."""
        states = {}
        
        for vehicle in vehicles:
            # Use existing state or generate default
            current_soc_percent = vehicle.estimated_soc or 50.0
            current_soc_kwh = (current_soc_percent / 100.0) * vehicle.battery_capacity
            
            state = VehicleChargeState(
                vehicle_id=vehicle.vehicle_id,
                current_soc_percent=current_soc_percent,
                current_soc_kwh=current_soc_kwh,
                battery_capacity_kwh=vehicle.battery_capacity,
                is_connected=True,
                charger_id=vehicle.current_charger_id or 1,
                charger_type='AC',
                ac_charge_rate_kw=vehicle.charge_power_ac,
                dc_charge_rate_kw=vehicle.charge_power_dc,
                efficiency_kwh_mile=vehicle.efficiency_kwh_mile,
                status='Idle'
            )
            
            states[vehicle.vehicle_id] = state
        
        return states
    
    def _extract_energy_requirements(
        self,
        sequences: List[Tuple],
        vehicle_states: Dict[int, VehicleChargeState]
    ) -> Dict[int, List[RouteEnergyRequirement]]:
        """Extract energy requirements from sequences."""
        requirements = {}
        
        for vehicle_id, route_sequence, _ in sequences:
            if vehicle_id not in requirements:
                requirements[vehicle_id] = []
            
            state = vehicle_states.get(vehicle_id)
            if not state:
                continue
            
            cumulative_energy = 0.0
            for idx, route in enumerate(route_sequence):
                route_energy = route.plan_mileage * state.efficiency_kwh_mile * 1.15  # Safety factor
                cumulative_energy += route_energy
                
                req = RouteEnergyRequirement(
                    route_id=route.route_id,
                    vehicle_id=vehicle_id,
                    plan_start_date_time=route.plan_start_date_time,
                    plan_end_date_time=route.plan_end_date_time,
                    plan_mileage=route.plan_mileage,
                    route_status=route.route_status,
                    efficiency_kwh_mile=state.efficiency_kwh_mile,
                    route_energy_buffer_kwh=route_energy,
                    cumulative_energy_kwh=cumulative_energy,
                    route_sequence_index=idx,
                    is_back_to_back=False
                )
                
                requirements[vehicle_id].append(req)
        
        return requirements
    
    def _load_energy_requirements_from_db(
        self,
        site_id: int,
        vehicles: List[Vehicle],
        window_hours: int
    ) -> Dict[int, List[RouteEnergyRequirement]]:
        """Load energy requirements from database (pre-allocated routes)."""
        # This would query t_route_allocated or t_route_plan
        # For now, return empty (scheduling only without routes)
        return {v.vehicle_id: [] for v in vehicles}
    
    def _generate_availability_matrices(
        self,
        vehicles: List[Vehicle],
        time_slots: List[datetime],
        energy_requirements: Dict[int, List[RouteEnergyRequirement]]
    ) -> Dict[int, VehicleAvailability]:
        """Generate availability matrices for vehicles."""
        matrices = {}
        
        for vehicle in vehicles:
            # All slots available by default
            availability = [True] * len(time_slots)
            
            # Block slots during routes
            requirements = energy_requirements.get(vehicle.vehicle_id, [])
            for req in requirements:
                for idx, slot_time in enumerate(time_slots):
                    if req.plan_start_date_time <= slot_time < req.plan_end_date_time:
                        availability[idx] = False
            
            matrices[vehicle.vehicle_id] = VehicleAvailability(
                vehicle_id=vehicle.vehicle_id,
                time_slots=time_slots,
                availability_matrix=availability
            )
        
        return matrices
    
    def _generate_forecast_data(
        self, 
        time_slots: List[datetime]
    ) -> Dict[datetime, float]:
        """Generate site demand forecast (simplified)."""
        forecast = {}
        
        for slot in time_slots:
            hour = slot.hour
            
            # Lower demand during night, higher during day
            if 0 <= hour < 6:
                demand = 20.0  # Night
            elif 6 <= hour < 9:
                demand = 60.0  # Morning ramp
            elif 9 <= hour < 17:
                demand = 80.0  # Day
            elif 17 <= hour < 22:
                demand = 70.0  # Evening
            else:
                demand = 40.0  # Late evening
            
            forecast[slot] = demand
        
        return forecast
    
    def _generate_price_data(
        self, 
        time_slots: List[datetime]
    ) -> Dict[datetime, Tuple[float, bool]]:
        """Generate electricity price data with TRIAD flags."""
        price_data = {}
        
        for slot in time_slots:
            hour = slot.hour
            
            # Time-of-use pricing
            if 0 <= hour < 5:
                price = 0.10  # Off-peak
            elif 5 <= hour < 8:
                price = 0.15  # Shoulder
            elif 8 <= hour < 20:
                price = 0.25  # Peak
            else:
                price = 0.12  # Evening
            
            # TRIAD periods (example: winter peak hours)
            month = slot.month
            is_triad = (
                month in [11, 12, 1, 2] and  # Winter months
                17 <= hour < 19 and  # 5-7 PM
                slot.weekday() < 5  # Weekdays
            )
            
            price_data[slot] = (price, is_triad)
        
        return price_data
    
    def _build_test_result(
        self,
        site_id: int,
        start_time: datetime,
        window_hours: int,
        mode: str,
        config: UnifiedOptimizationConfig,
        opt_data: Dict,
        result: UnifiedOptimizationResult,
        total_execution_time: float
    ) -> Dict:
        """Build comprehensive test result dictionary."""
        test_result = {
            'site_id': site_id,
            'start_time': start_time.isoformat(),
            'window_hours': window_hours,
            'mode': mode,
            'status': result.status,
            'solve_time_seconds': result.solve_time_seconds,
            'total_execution_time_seconds': total_execution_time,
            'success': result.status not in ['FAILED', 'INFEASIBLE'],
            'objective_value': result.objective_value,
            
            # Configuration
            'config': {
                'allocation_weight': config.allocation_score_weight,
                'scheduling_weight': config.scheduling_cost_weight,
                'route_count_weight': config.route_count_weight,
                'target_soc_percent': config.target_soc_percent,
                'site_capacity_kw': config.site_capacity_kw,
            }
        }
        
        # Allocation metrics
        if result.mode in (OptimizationMode.ALLOCATION_ONLY, OptimizationMode.INTEGRATED):
            test_result['allocation'] = {
                'routes_total': result.routes_total,
                'routes_allocated': result.routes_allocated,
                'routes_unallocated': result.routes_total - result.routes_allocated,
                'allocation_percentage': (
                    (result.routes_allocated / result.routes_total * 100)
                    if result.routes_total > 0 else 0
                ),
                'allocation_score': result.allocation_score,
                'sequences_selected': len(result.selected_sequences),
                'vehicles_used': len(set(seq[0] for seq in result.selected_sequences))
            }
        
        # Scheduling metrics
        if result.mode in (OptimizationMode.SCHEDULING_ONLY, OptimizationMode.INTEGRATED):
            test_result['scheduling'] = {
                'vehicles_scheduled': len(result.vehicle_schedules),
                'total_energy_kwh': result.total_energy_kwh,
                'total_charging_cost': result.total_charging_cost,
                'avg_cost_per_kwh': (
                    result.total_charging_cost / result.total_energy_kwh
                    if result.total_energy_kwh > 0 else 0
                ),
                'vehicles_with_routes': sum(
                    1 for vs in result.vehicle_schedules if vs.has_routes
                ),
                'total_charge_slots': sum(
                    len(vs.charge_slots) for vs in result.vehicle_schedules
                )
            }
        
        return test_result
    
    def _print_test_summary(self, result: Dict):
        """Print test result summary."""
        print("\n" + "="*70)
        print("UNIFIED OPTIMIZER TEST RESULT")
        print("="*70)
        print(f"Site ID:           {result.get('site_id')}")
        print(f"Mode:              {result.get('mode').upper()}")
        print(f"Status:            {result.get('status')}")
        print(f"Success:           {'✓' if result.get('status') not in ['failed', 'FAILED', 'INFEASIBLE'] else '✗'}")
        print(f"Objective Value:   {result.get('objective_value', 0):.2f}")
        print(f"Solve Time:        {result.get('solve_time_seconds', 0):.2f}s")
        print(f"Total Time:        {result.get('total_execution_time', 0):.2f}s")
        
        # Allocation metrics
        mode_lower = result.get('mode', '').lower()
        if 'allocation' in mode_lower or 'integrated' in mode_lower:
            print("\nALLOCATION METRICS:")
            routes_total = result.get('routes_total', 0)
            routes_allocated = result.get('routes_allocated', 0)
            allocation_pct = (routes_allocated / routes_total * 100) if routes_total > 0 else 0.0
            print(f"  Routes Allocated:   {routes_allocated}/{routes_total} "
                  f"({allocation_pct:.1f}%)")
            print(f"  Allocation Score:   {result.get('allocation_score', 0):.2f}")
            print(f"  Allocation ID:      {result.get('allocation_id')}")
        
        # Scheduling metrics
        if 'scheduling' in mode_lower or 'integrated' in mode_lower:
            print("\nSCHEDULING METRICS:")
            print(f"  Total Energy:       {result.get('total_energy_kwh', 0):.2f} kWh")
            print(f"  Total Cost:         £{result.get('total_charging_cost', 0):.2f}")
            avg_cost = (result.get('total_charging_cost', 0) / result.get('total_energy_kwh', 1)) if result.get('total_energy_kwh', 0) > 0 else 0
            print(f"  Avg Cost/kWh:       £{avg_cost:.4f}")
            print(f"  Schedule ID:        {result.get('schedule_id')}")
        
        if result.get('persisted'):
            print(f"\n✓ Results persisted to database")
        
        if result.get('error'):
            print(f"\n✗ Error: {result.get('error')}")
        
        print("="*70 + "\n")
    
    def _print_overall_summary(self, results: List[Dict]):
        """Print overall test summary."""
        print("\n" + "#"*70)
        print("OVERALL UNIFIED OPTIMIZER TEST SUMMARY")
        print("#"*70)
        
        total = len(results)
        successful = sum(1 for r in results if r.get('status') not in ['failed', 'FAILED', 'INFEASIBLE'])
        failed = total - successful
        
        print(f"Total Scenarios:   {total}")
        print(f"Successful:        {successful}")
        print(f"Failed:            {failed}")
        print(f"Success Rate:      {(successful/total*100):.1f}%" if total > 0 else "N/A")
        
        if successful > 0:
            # Average metrics across successful tests
            success_results = [r for r in results if r.get('status') not in ['failed', 'FAILED', 'INFEASIBLE']]
            avg_obj = sum(r.get('objective_value', 0) for r in success_results) / successful
            avg_time = sum(r.get('solve_time_seconds', 0) for r in success_results) / successful
            
            print(f"\nAverage Objective: {avg_obj:.2f}")
            print(f"Average Solve Time: {avg_time:.2f}s")
            
            # Mode-specific averages
            allocation_results = [
                r for r in success_results 
                if 'allocation' in r.get('mode', '').lower() or 'integrated' in r.get('mode', '').lower()
            ]
            if allocation_results:
                total_routes = sum(r.get('routes_total', 0) for r in allocation_results)
                allocated = sum(r.get('routes_allocated', 0) for r in allocation_results)
                avg_alloc_pct = (allocated / total_routes * 100) if total_routes > 0 else 0.0
                print(f"\nAllocation Avg:    {avg_alloc_pct:.1f}% routes allocated")
            
            scheduling_results = [
                r for r in success_results 
                if 'scheduling' in r.get('mode', '').lower() or 'integrated' in r.get('mode', '').lower()
            ]
            if scheduling_results:
                avg_energy = sum(r.get('total_energy_kwh', 0) for r in scheduling_results) / len(scheduling_results)
                avg_cost = sum(r.get('total_charging_cost', 0) for r in scheduling_results) / len(scheduling_results)
                print(f"Scheduling Avg:    {avg_energy:.2f} kWh, £{avg_cost:.2f}")
        
        print("#"*70 + "\n")
    
    def export_results(self, filename: str = 'unified_optimizer_test_results.json'):
        """
        Export test results to JSON file.
        
        Args:
            filename: Output filename
        """
        with open(filename, 'w') as f:
            json.dump(self.test_results, f, indent=2)
        
        logger.info(f"Test results exported to {filename}")
    
    def close(self):
        """Close database connection and cleanup."""
    def close(self):
        """Close database connection and cleanup."""
        if self.controller:
            self.controller.close()
        elif self.db_connected:
            db.close()
        self.db_connected = False


def create_sample_scenarios() -> List[Dict]:
    """
    Create sample test scenarios for unified optimizer.
    
    Returns:
        List of scenario configurations
    """
    base_time = datetime(2026, 2, 16, 4, 30, 0)  # 4:30 AM today
    
    scenarios = [
        # Test 1: Allocation only
        {
            'site_id': 10,
            'start_time': base_time,
            'mode': 'allocation_only',
            'window_hours': 18,
            'allocation_time_limit': 30,
            'use_database': True,
            'persist_to_database': True
        },
        
        # Test 2: Scheduling only
        {
            'site_id': 10,
            'start_time': base_time,
            'mode': 'scheduling_only',
            'window_hours': 18,
            'scheduling_time_limit': 300,
            'target_soc_percent': 75.0,
            'site_capacity_kw': 200.0,
            'use_database': True,
            'persist_to_database': True
        },
        
        # Test 3: Integrated (balanced weights)
        {
            'site_id': 10,
            'start_time': base_time,
            'mode': 'integrated',
            'window_hours': 18,
            'allocation_weight': 1.0,
            'scheduling_weight': 1.0,
            'integrated_time_limit': 330,
            'use_database': True,
            'persist_to_database': True
        },
        
        # Test 4: Integrated (favor allocation)
        {
            'site_id': 10,
            'start_time': base_time + timedelta(hours=6),
            'mode': 'integrated',
            'window_hours': 18,
            'allocation_weight': 2.0,
            'scheduling_weight': 0.5,
            'integrated_time_limit': 330,
            'use_database': True,
            'persist_to_database': True
        },
        
        # Test 5: Integrated (favor scheduling)
        {
            'site_id': 10,
            'start_time': base_time + timedelta(hours=12),
            'mode': 'integrated',
            'window_hours': 18,
            'allocation_weight': 0.5,
            'scheduling_weight': 2.0,
            'integrated_time_limit': 330,
            'use_database': True,
            'persist_to_database': True
        }
    ]
    
    return scenarios


def main():
    """Main test runner."""
    parser = argparse.ArgumentParser(
        description='Unified Optimizer Test Framework',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Run sample scenarios
  python test_unified_optimizer.py --sample-scenarios
  
  # Test allocation only
  python test_unified_optimizer.py --site-id 10 --mode allocation_only \\
      --start-time "2026-02-16 04:30:00"
  
  # Test integrated mode with custom weights
  python test_unified_optimizer.py --site-id 10 --mode integrated \\
      --start-time "2026-02-16 04:30:00" \\
      --allocation-weight 2.0 --scheduling-weight 0.5
        '''
    )
    
    parser.add_argument('--site-id', type=int, help='Site ID to test')
    parser.add_argument('--start-time', type=str, help='Start time (YYYY-MM-DD HH:MM:SS)')
    parser.add_argument('--mode', type=str, default='integrated',
                       choices=['allocation_only', 'allocation', 'scheduling_only', 
                               'scheduling', 'integrated', 'both'],
                       help='Optimization mode')
    parser.add_argument('--window-hours', type=int, default=18,
                       help='Planning window duration in hours')
    
    # Weight parameters
    parser.add_argument('--allocation-weight', type=float, default=1.0,
                       help='α: weight for allocation score (default: 1.0)')
    parser.add_argument('--scheduling-weight', type=float, default=1.0,
                       help='β: weight for scheduling cost (default: 1.0)')
    parser.add_argument('--route-count-weight', type=float, default=100.0,
                       help='Priority weight for route coverage (default: 100)')
    
    # Time limits
    parser.add_argument('--allocation-time-limit', type=int, default=30,
                       help='Time limit for allocation (seconds, default: 30)')
    parser.add_argument('--scheduling-time-limit', type=int, default=300,
                       help='Time limit for scheduling (seconds, default: 300)')
    parser.add_argument('--integrated-time-limit', type=int, default=330,
                       help='Time limit for integrated mode (seconds, default: 330)')
    
    # Scheduling parameters
    parser.add_argument('--target-soc', type=float, default=75.0,
                       help='Target SOC percentage (default: 75)')
    parser.add_argument('--site-capacity', type=float, default=200.0,
                       help='Site capacity in kW (default: 200)')
    
    # Test options
    parser.add_argument('--sample-scenarios', action='store_true',
                       help='Run predefined sample scenarios')
    parser.add_argument('--no-database', action='store_true',
                       help='Do not load data from database (requires custom data)')
    parser.add_argument('--persist-to-database', action='store_true',
                       help='Persist results to t_route_allocated and t_charge_schedule tables')
    parser.add_argument('--export', type=str,
                       help='Export results to JSON file')
    
    args = parser.parse_args()
    
    framework = UnifiedOptimizerTestFramework()
    
    try:
        if args.sample_scenarios:
            # Run sample scenarios
            scenarios = create_sample_scenarios()
            results = framework.run_multiple_scenarios(scenarios)
        
        elif args.site_id and args.start_time:
            # Run single scenario
            start_time = datetime.strptime(args.start_time, '%Y-%m-%d %H:%M:%S')
            result = framework.run_test_scenario(
                site_id=args.site_id,
                start_time=start_time,
                mode=args.mode,
                window_hours=args.window_hours,
                allocation_weight=args.allocation_weight,
                scheduling_weight=args.scheduling_weight,
                route_count_weight=args.route_count_weight,
                allocation_time_limit=args.allocation_time_limit,
                scheduling_time_limit=args.scheduling_time_limit,
                integrated_time_limit=args.integrated_time_limit,
                target_soc_percent=args.target_soc,
                site_capacity_kw=args.site_capacity,
                use_database=not args.no_database,
                persist_to_database=args.persist_to_database
            )
        
        else:
            parser.print_help()
            return
        
        # Export results if requested
        if args.export:
            framework.export_results(args.export)
    
    finally:
        framework.close()


if __name__ == '__main__':
    main()
