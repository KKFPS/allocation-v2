"""Scheduler controller - orchestrates the charge scheduling process."""
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
import numpy as np

from src.database.connection import db
from src.database.queries import Queries
from src.models.scheduler import (
    SchedulerConfig, VehicleChargeState, RouteEnergyRequirement,
    VehicleAvailability, ChargeScheduleResult, VehicleChargeSchedule,
    ChargeSlot, ForecastDataHorizon, RouteSourceMode
)
from src.models.vehicle import Vehicle
from src.models.route import Route
from src.optimizer.charge_optimizer import ChargeOptimizer
from src.utils.logging_config import logger


class SchedulerController:
    """Main controller for charge scheduling process."""
    
    def __init__(self, schedule_id: Optional[int] = None, site_id: Optional[int] = None):
        """
        Initialize scheduler controller.
        
        Args:
            schedule_id: Existing schedule ID to execute
            site_id: Site ID for new schedule (if schedule_id not provided)
        """
        self.schedule_id = schedule_id
        self.site_id = site_id
        self.config: Optional[SchedulerConfig] = None
        self.fleet_avg_efficiency: float = 0.35  # Default fallback
        
        # Connect to database
        db.connect()
    
    def run_scheduling(self, current_time: Optional[datetime] = None,
                      route_source_mode: Optional[RouteSourceMode] = None) -> ChargeScheduleResult:
        """
        Execute complete scheduling workflow.
        
        Args:
            current_time: Current datetime (defaults to now)
            route_source_mode: Override route source configuration
        
        Returns:
            ChargeScheduleResult object
        """
        if current_time is None:
            current_time = datetime.utcnow()
        
        # Floor to 30-minute interval
        current_time = self._floor_to_30_min(current_time)
        
        try:
            # Load or create configuration
            if self.schedule_id:
                self.config = self._load_scheduler_config()
            else:
                self.config = self._create_scheduler_config(current_time)
                self.schedule_id = self.config.schedule_id
            
            # Override route source if provided
            if route_source_mode:
                self.config.route_source_mode = route_source_mode
            
            logger.info(f"Starting charge scheduling - Schedule ID: {self.schedule_id}, "
                       f"Site ID: {self.config.site_id}, "
                       f"Route Source: {self.config.route_source_mode.value}")
            
            # Validate configuration
            validation_errors = self.config.validate()
            if validation_errors:
                raise ValueError(f"Invalid configuration: {'; '.join(validation_errors)}")
            
            # Calculate planning window
            planning_start, planning_end, actual_hours = self._calculate_planning_window(current_time)
            
            if actual_hours < 4.0:
                raise ValueError(f"Planning window too short: {actual_hours:.1f}h < 4.0h minimum")
            
            logger.info(f"Planning window: {planning_start} to {planning_end} ({actual_hours:.1f} hours)")
            
            # Calculate fleet efficiency
            self._calculate_fleet_efficiency()
            
            # Load vehicles for site
            vehicles = self._load_vehicles()
            logger.info(f"Loaded {len(vehicles)} vehicles for scheduling")
            
            # Load vehicle charge states
            vehicle_states = self._load_vehicle_states(vehicles)
            
            # Load routes and calculate energy requirements
            vehicle_routes = self._load_vehicle_routes(vehicles, planning_start, planning_end)
            energy_requirements = self._calculate_energy_requirements(vehicle_routes, vehicle_states)
            
            # Calculate vehicle availability matrices
            availability_matrices = self._calculate_availability_matrices(
                vehicles, vehicle_states, vehicle_routes,
                planning_start, planning_end
            )
            
            # Load forecast and price data
            forecast_data = self._load_forecast_data(planning_start, planning_end)
            price_data = self._load_price_data(planning_start, planning_end)
            
            # Build time slots (30-minute intervals)
            time_slots = self._build_time_slots(planning_start, planning_end)
            
            # Run optimization
            logger.info(f"Starting optimization with {len(vehicles)} vehicles, "
                       f"{sum(len(routes) for routes in vehicle_routes.values())} total routes")
            
            optimization_result = self._run_optimization(
                vehicles, vehicle_states, energy_requirements,
                availability_matrices, forecast_data, price_data,
                time_slots
            )
            
            # Build result object
            result = ChargeScheduleResult(
                schedule_id=self.schedule_id,
                site_id=self.config.site_id,
                vehicle_schedules=optimization_result['vehicle_schedules'],
                planning_start=planning_start,
                planning_end=planning_end,
                actual_planning_window_hours=actual_hours,
                total_cost=optimization_result['total_cost'],
                total_energy_kwh=optimization_result['total_energy_kwh'],
                solve_time_seconds=optimization_result['solve_time_seconds'],
                optimization_status=optimization_result['status'],
                validation_passed=True,
                vehicles_scheduled=len(vehicles),
                routes_considered=sum(len(routes) for routes in vehicle_routes.values()),
                checkpoints_created=sum(len(req) for req in energy_requirements.values())
            )
            
            # Validate results
            self._validate_schedule(result, energy_requirements)
            
            # Persist to database
            self._persist_schedule(result, energy_requirements)
            
            # Update scheduler status
            self._update_scheduler_status('completed', actual_hours)
            
            logger.info(f"Scheduling completed successfully - Schedule ID: {self.schedule_id}")
            logger.info(f"Total cost: £{result.total_cost:.2f}, "
                       f"Total energy: {result.total_energy_kwh:.2f} kWh, "
                       f"Solve time: {result.solve_time_seconds:.2f}s")
            
            return result
            
        except Exception as e:
            logger.error(f"Scheduling failed: {str(e)}", exc_info=True)
            if self.schedule_id:
                self._update_scheduler_status('failed', None)
            raise
    
    def _floor_to_30_min(self, dt: datetime) -> datetime:
        """Floor datetime to 30-minute interval."""
        minute = (dt.minute // 30) * 30
        return dt.replace(minute=minute, second=0, microsecond=0)
    
    def _load_scheduler_config(self) -> SchedulerConfig:
        """Load existing scheduler configuration."""
        with db.get_cursor() as cur:
            cur.execute(Queries.GET_SCHEDULER_CONFIG, (self.schedule_id,))
            row = cur.fetchone()
            
            if not row:
                raise ValueError(f"Schedule ID {self.schedule_id} not found")
            
            config = SchedulerConfig(
                schedule_id=row[0],
                site_id=row[1],
                schedule_type=row[2],
                status=row[3],
                run_datetime=row[4],
                planning_window_hours=row[5] or 18.0,
                route_energy_safety_factor=row[6] or 1.15,
                min_departure_buffer_minutes=row[7] or 60,
                back_to_back_threshold_minutes=row[8] or 90,
                target_soc_percent=row[9] or 95.0,
                battery_factor=row[10] or 1.0,
                agreed_site_capacity_kva=row[11],
                power_factor=row[12] or 0.85,
                site_usage_factor=row[13] or 0.90,
                max_fast_chargers=row[14] or 0,
                time_limit_seconds=row[15] or 300,
                triad_penalty_factor=row[16] or 100.0,
                synthetic_time_price_factor=row[17] or 0.01,
                created_date_time=row[18],
                actual_planning_window_hours=row[19]
            )
            
            self.site_id = config.site_id
            return config
    
    def _create_scheduler_config(self, current_time: datetime) -> SchedulerConfig:
        """Create new scheduler configuration."""
        if not self.site_id:
            raise ValueError("site_id required for new schedule")
        
        config = SchedulerConfig(
            site_id=self.site_id,
            schedule_type='dynamic',
            status='running',
            run_datetime=current_time,
            created_date_time=current_time
        )
        
        # Insert into database
        with db.get_cursor() as cur:
            cur.execute(Queries.CREATE_SCHEDULER, (
                config.site_id,
                config.schedule_type,
                config.status,
                config.run_datetime,
                config.planning_window_hours,
                config.route_energy_safety_factor,
                config.min_departure_buffer_minutes,
                config.back_to_back_threshold_minutes,
                config.target_soc_percent,
                config.agreed_site_capacity_kva,
                config.power_factor,
                config.site_usage_factor,
                config.max_fast_chargers,
                config.time_limit_seconds,
                config.created_date_time
            ))
            
            config.schedule_id = cur.fetchone()[0]
            db.commit()
        
        logger.info(f"Created new scheduler config - Schedule ID: {config.schedule_id}")
        return config
    
    def _calculate_planning_window(self, current_time: datetime) -> Tuple[datetime, datetime, float]:
        """
        Calculate effective planning window based on data availability.
        
        Returns:
            Tuple of (start_time, end_time, actual_hours)
        """
        planning_start = current_time
        planning_target_end = current_time + timedelta(hours=self.config.planning_window_hours)
        
        # Check forecast horizon
        with db.get_cursor() as cur:
            cur.execute(Queries.GET_FORECAST_HORIZON, (self.config.site_id,))
            forecast_row = cur.fetchone()
            max_forecast_time = forecast_row[0] if forecast_row else None
            
            cur.execute(Queries.GET_PRICE_HORIZON)
            price_row = cur.fetchone()
            max_price_time = price_row[0] if price_row else None
        
        # Calculate effective end time
        constraints = [planning_target_end]
        if max_forecast_time:
            constraints.append(max_forecast_time)
        if max_price_time:
            constraints.append(max_price_time)
        
        planning_end = min(constraints)
        actual_hours = (planning_end - planning_start).total_seconds() / 3600.0
        
        # Log warnings if window reduced
        if actual_hours < self.config.planning_window_hours:
            logger.warning(f"Planning window reduced due to data availability: "
                          f"Configured: {self.config.planning_window_hours:.1f}h, "
                          f"Actual: {actual_hours:.1f}h")
            logger.warning(f"Forecast available until: {max_forecast_time}, "
                          f"Price available until: {max_price_time}")
        
        return planning_start, planning_end, actual_hours
    
    def _calculate_fleet_efficiency(self):
        """Calculate fleet-wide average efficiency."""
        with db.get_cursor() as cur:
            cur.execute(Queries.GET_FLEET_EFFICIENCY, (self.config.site_id,))
            row = cur.fetchone()
            
            vehicle_count = row[0] if row else 0
            fleet_avg = row[1] if row and row[1] else None
            
            if fleet_avg:
                self.fleet_avg_efficiency = fleet_avg
                logger.info(f"Fleet average efficiency: {fleet_avg:.3f} kWh/mile "
                           f"(from {vehicle_count} vehicles)")
            else:
                self.fleet_avg_efficiency = 0.35
                logger.warning(f"No vehicles with efficiency data - using default: 0.35 kWh/mile")
    
    def _load_vehicles(self) -> List[Vehicle]:
        """Load all vehicles for site."""
        vehicles = []
        
        with db.get_cursor() as cur:
            cur.execute(Queries.GET_ALL_VEHICLES_FOR_SCHEDULING, (self.config.site_id,))
            rows = cur.fetchall()
            
            for row in rows:
                vehicle = Vehicle(
                    vehicle_id=row[0],
                    site_id=row[1],
                    active=row[2],
                    VOR=row[3],
                    charge_power_ac=row[4] or 11.0,
                    charge_power_dc=row[5] or 50.0,
                    battery_capacity=row[6] or 80.0,
                    efficiency_kwh_mile=row[7],
                    telematic_label=row[8]
                )
                
                vehicles.append(vehicle)
        
        return vehicles
    
    def _load_vehicle_states(self, vehicles: List[Vehicle]) -> Dict[int, VehicleChargeState]:
        """Load current charge state for all vehicles."""
        states = {}
        
        for vehicle in vehicles:
            with db.get_cursor() as cur:
                cur.execute(Queries.GET_VEHICLE_CHARGE_STATE, (vehicle.vehicle_id,))
                row = cur.fetchone()
                
                if not row:
                    logger.warning(f"No state data for vehicle {vehicle.vehicle_id}")
                    continue
                
                estimated_soc = row[5] or 50.0  # Default to 50% if unknown
                battery_capacity = row[1] or 80.0
                current_soc_kwh = (estimated_soc / 100.0) * battery_capacity
                
                # Use vehicle-specific or fleet average efficiency
                efficiency = row[4] or self.fleet_avg_efficiency
                if not row[4]:
                    logger.warning(f"Vehicle {vehicle.vehicle_id}: Using fleet average efficiency "
                                 f"{self.fleet_avg_efficiency:.3f} kWh/mile")
                
                state = VehicleChargeState(
                    vehicle_id=vehicle.vehicle_id,
                    current_soc_percent=estimated_soc,
                    current_soc_kwh=current_soc_kwh,
                    battery_capacity_kwh=battery_capacity,
                    is_connected=row[10] is not None,  # Has charger_id
                    charger_id=row[10],
                    charger_type='DC' if row[11] else 'AC',
                    ac_charge_rate_kw=row[2] or 11.0,
                    dc_charge_rate_kw=row[3] or 50.0,
                    efficiency_kwh_mile=efficiency,
                    status=row[6],
                    current_route_id=row[7],
                    return_eta=row[8],
                    return_soc_percent=row[9]
                )
                
                states[vehicle.vehicle_id] = state
        
        return states
    
    def _load_vehicle_routes(self, vehicles: List[Vehicle],
                            planning_start: datetime, planning_end: datetime) -> Dict[int, List[Route]]:
        """
        Load routes for all vehicles within planning window.
        
        Supports both route_plan_only and allocated_routes modes.
        """
        vehicle_routes = {v.vehicle_id: [] for v in vehicles}
        
        # Select query based on route source mode
        if self.config.route_source_mode == RouteSourceMode.ROUTE_PLAN_ONLY:
            query = Queries.GET_ROUTES_FOR_SCHEDULING_ROUTE_PLAN
            logger.info("Using t_route_plan for vehicle-route mapping")
        else:
            query = Queries.GET_ROUTES_FOR_SCHEDULING_ALLOCATED
            logger.info("Using t_route_plan JOIN t_route_allocated for vehicle-route mapping")
        
        for vehicle in vehicles:
            with db.get_cursor() as cur:
                cur.execute(query, (vehicle.vehicle_id, planning_start, planning_end))
                rows = cur.fetchall()
                
                for row in rows:
                    route = Route(
                        route_id=row[0],
                        site_id=row[1],
                        vehicle_id=row[2],
                        route_status=row[3],
                        route_alias=row[4],
                        plan_start_date_time=row[5],
                        actual_start_date_time=row[6],
                        plan_end_date_time=row[7],
                        actual_end_date_time=row[8],
                        plan_mileage=row[9],
                        n_orders=row[10]
                    )
                    
                    vehicle_routes[vehicle.vehicle_id].append(route)
                
                if rows:
                    logger.debug(f"Vehicle {vehicle.vehicle_id}: {len(rows)} routes in planning window")
        
        total_routes = sum(len(routes) for routes in vehicle_routes.values())
        vehicles_with_routes = sum(1 for routes in vehicle_routes.values() if routes)
        logger.info(f"Loaded {total_routes} routes across {vehicles_with_routes} vehicles")
        
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
                continue
            
            state = vehicle_states.get(vehicle_id)
            if not state:
                logger.warning(f"No state for vehicle {vehicle_id} - skipping energy calculation")
                continue
            
            efficiency = state.efficiency_kwh_mile
            requirements = []
            cumulative_energy = 0.0
            
            # Sort routes by departure time
            sorted_routes = sorted(routes, key=lambda r: r.plan_start_date_time)
            
            for idx, route in enumerate(sorted_routes):
                # Calculate energy buffer for this route
                route_energy = (route.plan_mileage * efficiency * 
                              self.config.route_energy_safety_factor)
                cumulative_energy += route_energy
                
                # Check if back-to-back with next route
                is_back_to_back = False
                gap_minutes = None
                
                if idx < len(sorted_routes) - 1:
                    next_route = sorted_routes[idx + 1]
                    gap = next_route.plan_start_date_time - route.plan_end_date_time
                    gap_minutes = gap.total_seconds() / 60.0
                    
                    if gap_minutes < self.config.back_to_back_threshold_minutes:
                        is_back_to_back = True
                        logger.warning(f"Vehicle {vehicle_id}: Back-to-back routes detected - "
                                     f"Route {route.route_id} to {next_route.route_id}, "
                                     f"gap: {gap_minutes:.0f} min")
                
                requirement = RouteEnergyRequirement(
                    route_id=route.route_id,
                    vehicle_id=vehicle_id,
                    plan_start_date_time=route.plan_start_date_time,
                    plan_end_date_time=route.plan_end_date_time,
                    plan_mileage=route.plan_mileage,
                    route_status=route.route_status,
                    efficiency_kwh_mile=efficiency,
                    route_energy_buffer_kwh=route_energy,
                    cumulative_energy_kwh=cumulative_energy,
                    route_sequence_index=idx,
                    is_back_to_back=is_back_to_back,
                    gap_to_next_minutes=gap_minutes
                )
                
                requirements.append(requirement)
                
                logger.debug(f"Vehicle {vehicle_id}, Route {route.route_id}: "
                           f"{route.plan_mileage:.1f} mi, "
                           f"Energy: {route_energy:.2f} kWh, "
                           f"Cumulative: {cumulative_energy:.2f} kWh")
            
            energy_requirements[vehicle_id] = requirements
        
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
        # Build 30-minute time slots
        time_slots = self._build_time_slots(planning_start, planning_end)
        
        availability_matrices = {}
        
        for vehicle in vehicles:
            state = vehicle_states.get(vehicle.vehicle_id)
            routes = vehicle_routes.get(vehicle.vehicle_id, [])
            
            # Initialize all slots as available
            availability = [True] * len(time_slots)
            unavailable_periods = []
            
            # Mark VOR vehicles as completely unavailable
            if vehicle.VOR or (state and state.status == 'VOR'):
                availability = [False] * len(time_slots)
                unavailable_periods.append({
                    'reason': 'VOR',
                    'start': planning_start,
                    'end': planning_end
                })
            else:
                # Mark unavailable during on-route time (if currently on route)
                if state and state.status == 'On-Route' and state.return_eta:
                    for idx, slot in enumerate(time_slots):
                        if slot < state.return_eta:
                            availability[idx] = False
                    
                    unavailable_periods.append({
                        'reason': 'Currently on route',
                        'start': planning_start,
                        'end': state.return_eta
                    })
                
                # Mark unavailable during planned routes
                buffer_delta = timedelta(minutes=self.config.min_departure_buffer_minutes)
                
                for route in routes:
                    unavailable_start = route.plan_start_date_time - buffer_delta
                    unavailable_end = route.plan_end_date_time
                    
                    for idx, slot in enumerate(time_slots):
                        if unavailable_start <= slot < unavailable_end:
                            availability[idx] = False
                    
                    unavailable_periods.append({
                        'reason': f'Route {route.route_id}',
                        'start': unavailable_start,
                        'end': unavailable_end
                    })
            
            vehicle_availability = VehicleAvailability(
                vehicle_id=vehicle.vehicle_id,
                time_slots=time_slots,
                availability_matrix=availability,
                unavailable_periods=unavailable_periods
            )
            
            availability_matrices[vehicle.vehicle_id] = vehicle_availability
            
            available_slots = sum(availability)
            logger.debug(f"Vehicle {vehicle.vehicle_id}: {available_slots}/{len(time_slots)} slots available")
        
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
            cur.execute(Queries.GET_FORECAST_DATA, (self.config.site_id, start, end))
            rows = cur.fetchall()
            
            for row in rows:
                forecast_data[row[0]] = row[1]
        
        logger.info(f"Loaded {len(forecast_data)} forecast data points")
        return forecast_data
    
    def _load_price_data(self, start: datetime, end: datetime) -> Dict[datetime, Tuple[float, bool]]:
        """Load electricity price data."""
        price_data = {}
        
        with db.get_cursor() as cur:
            cur.execute(Queries.GET_PRICE_DATA, (start, end))
            rows = cur.fetchall()
            
            for row in rows:
                price_data[row[0]] = (row[1], row[2])  # (price, is_triad)
        
        logger.info(f"Loaded {len(price_data)} price data points")
        return price_data
    
    def _run_optimization(
        self,
        vehicles: List[Vehicle],
        vehicle_states: Dict[int, VehicleChargeState],
        energy_requirements: Dict[int, List[RouteEnergyRequirement]],
        availability_matrices: Dict[int, VehicleAvailability],
        forecast_data: Dict[datetime, float],
        price_data: Dict[datetime, Tuple[float, bool]],
        time_slots: List[datetime]
    ) -> Dict:
        """
        Run Hexaly optimization for charge scheduling.
        
        Returns:
            Dictionary with vehicle_schedules, total_cost, solve_time, status
        """
        logger.info("Initializing Hexaly charge optimizer...")
        
        # Initialize optimizer
        optimizer = ChargeOptimizer(time_limit_seconds=self.config.time_limit_seconds)
        
        # Run optimization
        result = optimizer.optimize(
            schedule_id=self.schedule_id,
            vehicles=vehicles,
            vehicle_states=vehicle_states,
            energy_requirements=energy_requirements,
            availability_matrices=availability_matrices,
            time_slots=time_slots,
            forecast_data=forecast_data,
            price_data=price_data,
            site_capacity_kw=self.config.site_capacity_kw,
            target_soc_percent=self.config.target_soc_percent,
            triad_penalty_factor=self.config.triad_penalty_factor,
            synthetic_time_price_factor=self.config.synthetic_time_price_factor
        )
        
        logger.info(f"Optimization completed: {result['status']}, "
                   f"Total cost: £{result['total_cost']:.2f}, "
                   f"Total energy: {result['total_energy_kwh']:.2f} kWh, "
                   f"Solve time: {result['solve_time_seconds']:.2f}s")
        
        return result
    
    def _validate_schedule(
        self,
        result: ChargeScheduleResult,
        energy_requirements: Dict[int, List[RouteEnergyRequirement]]
    ):
        """Validate schedule meets route energy requirements."""
        for vehicle_schedule in result.vehicle_schedules:
            requirements = energy_requirements.get(vehicle_schedule.vehicle_id, [])
            
            if not requirements:
                continue
            
            # Check each checkpoint
            for requirement in requirements:
                # Find cumulative energy at checkpoint time
                cumulative_at_checkpoint = vehicle_schedule.initial_soc_kwh
                
                for slot in vehicle_schedule.charge_slots:
                    if slot.time_slot >= requirement.plan_start_date_time:
                        break
                    cumulative_at_checkpoint = vehicle_schedule.initial_soc_kwh + slot.cumulative_energy_kwh
                
                # Verify sufficient energy
                if cumulative_at_checkpoint < requirement.cumulative_energy_kwh:
                    shortfall = requirement.cumulative_energy_kwh - cumulative_at_checkpoint
                    vehicle_schedule.meets_route_requirements = False
                    vehicle_schedule.energy_shortfall_kwh = max(vehicle_schedule.energy_shortfall_kwh, shortfall)
                    
                    result.validation_passed = False
                    result.validation_errors.append(
                        f"Vehicle {vehicle_schedule.vehicle_id}, Route {requirement.route_id}: "
                        f"Energy shortfall of {shortfall:.2f} kWh at departure"
                    )
    
    def _persist_schedule(
        self,
        result: ChargeScheduleResult,
        energy_requirements: Dict[int, List[RouteEnergyRequirement]]
    ):
        """Persist schedule results to database."""
        with db.get_cursor() as cur:
            # Delete existing schedule data
            cur.execute(Queries.DELETE_CHARGE_SCHEDULE_BY_SCHEDULE_ID, (self.schedule_id,))
            
            # Insert charge schedule
            for vehicle_schedule in result.vehicle_schedules:
                for slot in vehicle_schedule.charge_slots:
                    cur.execute(Queries.INSERT_CHARGE_SCHEDULE, (
                        self.schedule_id,
                        vehicle_schedule.vehicle_id,
                        slot.time_slot,
                        slot.charge_power_kw,
                        slot.cumulative_energy_kwh,
                        slot.electricity_price,
                        slot.site_demand_kw,
                        slot.is_triad_period,
                        vehicle_schedule.assigned_charger_id,
                        vehicle_schedule.charger_type,
                        datetime.utcnow()
                    ))
            
            # Insert route checkpoints
            for vehicle_id, requirements in energy_requirements.items():
                for requirement in requirements:
                    cur.execute(Queries.INSERT_ROUTE_CHECKPOINT, (
                        self.schedule_id,
                        vehicle_id,
                        requirement.route_id,
                        requirement.plan_start_date_time,
                        requirement.cumulative_energy_kwh,
                        requirement.route_energy_buffer_kwh,
                        requirement.efficiency_kwh_mile,
                        datetime.utcnow()
                    ))
            
            db.commit()
        
        logger.info(f"Persisted schedule to database - Schedule ID: {self.schedule_id}")
    
    def _update_scheduler_status(self, status: str, actual_hours: Optional[float]):
        """Update scheduler status."""
        with db.get_cursor() as cur:
            cur.execute(Queries.UPDATE_SCHEDULER_STATUS, (
                status,
                actual_hours,
                self.schedule_id
            ))
            db.commit()
