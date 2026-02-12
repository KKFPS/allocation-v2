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

    def close(self) -> None:
        """Release resources (e.g. database connection)."""
        db.close()

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
            
            # Load vehicle charge states (t_vsm AS_OF current_time for reproducible tests)
            vehicle_states = self._load_vehicle_states(vehicles, as_of=current_time)
            
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
            time_slot_hours = (planning_end - planning_start).total_seconds() / 3600.0
            logger.info(
                "[TIME SLOTS] planning window slots: count=%s, start=%s, end=%s (%.1fh); "
                "forecast_keys=%s, price_keys=%s",
                len(time_slots),
                time_slots[0].isoformat() if time_slots else None,
                time_slots[-1].isoformat() if time_slots else None,
                time_slot_hours,
                len(forecast_data),
                len(price_data),
            )

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
            
            # t_scheduler columns: schedule_id, device_id, scheduler_type, status, profile_end, created_datetime
            config = SchedulerConfig(
                schedule_id=row["schedule_id"],
                site_id=row["device_id"],
                schedule_type=row["scheduler_type"],
                status=row["status"],
                run_datetime=row["created_datetime"],
                created_date_time=row["created_datetime"],
                # planning/optimisation params use SchedulerConfig defaults (not in t_scheduler)
            )
            self.site_id = config.site_id

            # Load site capacity (Agreed Site Capacity, kVA) from t_site."ASC"
            cur.execute(Queries.GET_SITE_ASC, (config.site_id,))
            asc_row = cur.fetchone()
            if asc_row and asc_row.get("ASC") is not None:
                config.agreed_site_capacity_kva = float(asc_row["ASC"])
                logger.debug(f"Loaded site capacity from t_site: ASC={config.agreed_site_capacity_kva} kVA")
            else:
                logger.warning(f"No t_site.\"ASC\" for site_id={config.site_id}; site_capacity_kw will be 0")

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

        # Load site capacity from t_site."ASC" and insert into t_scheduler
        with db.get_cursor() as cur:
            cur.execute(Queries.GET_SITE_ASC, (self.site_id,))
            asc_row = cur.fetchone()
            if asc_row and asc_row.get("ASC") is not None:
                config.agreed_site_capacity_kva = float(asc_row["ASC"])
                logger.debug(f"Loaded site capacity from t_site: ASC={config.agreed_site_capacity_kva} kVA")
            else:
                logger.warning(f"No t_site.\"ASC\" for site_id={self.site_id}; site_capacity_kw will be 0")

            cur.execute(Queries.CREATE_SCHEDULER, (
                config.site_id,
                config.schedule_type,
                config.status,
                True,  # latest_schedule
            ))
            row = cur.fetchone()
            config.schedule_id = row["schedule_id"] if row else None

            logger.info(f"Created new scheduler config - Schedule ID: {config.schedule_id}")
        
        logger.info(f"Created new scheduler config - Schedule ID: {config.schedule_id}")
        return config
    
    def _calculate_planning_window(self, current_time: datetime) -> Tuple[datetime, datetime, float]:
        """
        Calculate effective planning window based on data availability.
        
        planning_end is capped by the earliest of: target end (start + 18h),
        max forecast timestamp in DB, and max price timestamp in DB. If forecast
        or price data only extend ~4 hours, the window shrinks to that horizon.
        
        Returns:
            Tuple of (start_time, end_time, actual_hours)
        """
        planning_start = current_time
        planning_target_end = current_time + timedelta(hours=self.config.planning_window_hours)
        
        # Check forecast and price horizons (latest timestamp available in DB)
        with db.get_cursor() as cur:
            cur.execute(Queries.GET_FORECAST_HORIZON, (self.config.site_id,))
            forecast_row = cur.fetchone()
            max_forecast_time = forecast_row["max_forecast_time"] if forecast_row else None

            cur.execute(Queries.GET_PRICE_HORIZON)
            price_row = cur.fetchone()
            max_price_time = price_row["max_price_time"] if price_row else None

        # Log data horizons (hours from planning_start) to diagnose short windows
        def _hours_from(t: datetime, base: datetime) -> Optional[float]:
            if t is None:
                return None
            return (t - base).total_seconds() / 3600.0

        forecast_hours = _hours_from(max_forecast_time, planning_start) if max_forecast_time else None
        price_hours = _hours_from(max_price_time, planning_start) if max_price_time else None
        target_hours = self.config.planning_window_hours

        logger.info(
            "[PLANNING WINDOW] planning_start=%s | target_end=%s (%.1fh) | "
            "forecast_horizon: max_time=%s (%.1fh from start) | "
            "price_horizon: max_time=%s (%.1fh from start)",
            planning_start.isoformat(),
            planning_target_end.isoformat(),
            target_hours,
            max_forecast_time.isoformat() if max_forecast_time else None,
            forecast_hours if forecast_hours is not None else float("nan"),
            max_price_time.isoformat() if max_price_time else None,
            price_hours if price_hours is not None else float("nan"),
        )

        # Effective end = earliest of target, forecast horizon, price horizon
        constraints = [planning_target_end]
        if max_forecast_time:
            constraints.append(max_forecast_time)
        if max_price_time:
            constraints.append(max_price_time)
        
        planning_end = min(constraints)
        actual_hours = (planning_end - planning_start).total_seconds() / 3600.0

        # Identify which constraint limited the window
        if planning_end == planning_target_end:
            limiting = "target (no horizon limit)"
        elif max_forecast_time and planning_end == max_forecast_time:
            limiting = "forecast horizon (DB has no forecast beyond this time)"
        elif max_price_time and planning_end == max_price_time:
            limiting = "price horizon (DB has no price data beyond this time)"
        else:
            limiting = "unknown"
        logger.info(
            "[PLANNING WINDOW] effective planning_end=%s | actual_hours=%.1f | limiting=%s",
            planning_end.isoformat(), actual_hours, limiting,
        )
        
        if actual_hours < self.config.planning_window_hours:
            logger.warning(
                "Planning window reduced due to data availability: "
                "configured=%.1fh, actual=%.1fh. "
                "Extend t_site_energy_forecast_history and/or t_multisite_electricity_price "
                "to cover full window for 18h schedules.",
                self.config.planning_window_hours, actual_hours,
            )
        
        return planning_start, planning_end, actual_hours
    
    def _calculate_fleet_efficiency(self):
        """Calculate fleet-wide average efficiency."""
        with db.get_cursor() as cur:
            cur.execute(Queries.GET_FLEET_EFFICIENCY, (self.config.site_id,))
            row = cur.fetchone()
            
            vehicle_count = row["vehicle_count"] if row else 0
            fleet_avg = row["fleet_avg_efficiency"] if row and row.get("fleet_avg_efficiency") else None
            
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
            cur.execute(Queries.GET_ACTIVE_VEHICLES, (self.config.site_id,))
            rows = cur.fetchall()
            
            for row in rows:
                vehicle = Vehicle(
                    vehicle_id=row["vehicle_id"],
                    site_id=row["site_id"],
                    active=row["active"],
                    VOR=row["VOR"],
                    charge_power_ac=row["charge_power_ac"] or 11.0,
                    charge_power_dc=row["charge_power_dc"] or 50.0,
                    battery_capacity=row["battery_capacity"] or 80.0,
                    efficiency_kwh_mile=row["efficiency_kwh_mile"],
                    telematic_label=row["telematic_label"]
                )
                
                vehicles.append(vehicle)
        
        return vehicles
    
    def _load_vehicle_states(
        self, vehicles: List[Vehicle], as_of: Optional[datetime] = None
    ) -> Dict[int, VehicleChargeState]:
        """Load charge state for all vehicles, optionally AS_OF a given time (e.g. current_time from tests)."""
        states = {}
        use_as_of = as_of is not None

        for vehicle in vehicles:
            with db.get_cursor() as cur:
                if use_as_of:
                    cur.execute(
                        Queries.GET_VEHICLE_CHARGE_STATE_AS_OF,
                        (as_of, vehicle.vehicle_id),
                    )
                else:
                    cur.execute(Queries.GET_VEHICLE_CHARGE_STATE, (vehicle.vehicle_id,))
                row = cur.fetchone()
                
                if not row:
                    logger.warning(f"No state data for vehicle {vehicle.vehicle_id}")
                    continue
                
                estimated_soc = row["estimated_soc"] or 50.0  # Default to 50% if unknown
                battery_capacity = row["battery_capacity"] or 80.0
                current_soc_kwh = (estimated_soc / 100.0) * battery_capacity

                # Use vehicle-specific or fleet average efficiency
                efficiency = row["efficiency_kwh_mile"] or self.fleet_avg_efficiency
                if not row.get("efficiency_kwh_mile"):
                    logger.warning(f"Vehicle {vehicle.vehicle_id}: Using fleet average efficiency "
                                 f"{self.fleet_avg_efficiency:.3f} kWh/mile")

                state = VehicleChargeState(
                    vehicle_id=vehicle.vehicle_id,
                    current_soc_percent=estimated_soc,
                    current_soc_kwh=current_soc_kwh,
                    battery_capacity_kwh=battery_capacity,
                    is_connected=row["charger_id"] is not None,
                    charger_id=row["charger_id"],
                    charger_type='DC' if row["is_dc_charger"] else 'AC',
                    ac_charge_rate_kw=row["charge_power_ac"] or 11.0,
                    dc_charge_rate_kw=row["charge_power_dc"] or 50.0,
                    efficiency_kwh_mile=efficiency,
                    status=row["status"],
                    current_route_id=row["current_route_id"],
                    return_eta=row["return_eta"],
                    return_soc_percent=row["return_soc"]
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
        
        # Select query based on route source mode (ROUTE_PLAN_ONLY/ ALLOCATED_ROUTES)
        if self.config.route_source_mode == RouteSourceMode.ALLOCATED_ROUTES:
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
                        route_id=row["route_id"],
                        site_id=row["site_id"],
                        vehicle_id=row["vehicle_id"],
                        route_status=row["route_status"],
                        route_alias=row["route_alias"],
                        plan_start_date_time=row["plan_start_date_time"],
                        actual_start_date_time=row["actual_start_date_time"],
                        plan_end_date_time=row["plan_end_date_time"],
                        actual_end_date_time=row["actual_end_date_time"],
                        plan_mileage=row["plan_mileage"],
                        n_orders=row["n_orders"]
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
        logger.debug(
            f"Availability matrices: planning_window=[{planning_start} to {planning_end}], "
            f"time_slots={len(time_slots)}, vehicles={len(vehicles)}"
        )
        availability_matrices = {}

        for vehicle in vehicles:
            state = vehicle_states.get(vehicle.vehicle_id)
            routes = vehicle_routes.get(vehicle.vehicle_id, [])
            logger.debug(
                f"Vehicle {vehicle.vehicle_id}: state={state.status if state else None}, "
                f"routes={len(routes)}, VOR={getattr(vehicle, 'VOR', False)}"
            )

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
                logger.debug(f"Vehicle {vehicle.vehicle_id}: marked all {len(time_slots)} slots unavailable (VOR) {state.status} {vehicle.VOR}")
            else:
                # Mark unavailable during on-route time (if currently on route)
                if state and state.status == 'On-Route' and state.return_eta:
                    on_route_slots = 0
                    for idx, slot in enumerate(time_slots):
                        if slot < state.return_eta:
                            availability[idx] = False
                            on_route_slots += 1
                    unavailable_periods.append({
                        'reason': 'Currently on route',
                        'start': planning_start,
                        'end': state.return_eta
                    })
                    logger.debug(
                        f"Vehicle {vehicle.vehicle_id}: On-Route until {state.return_eta}, "
                        f"{on_route_slots} slots unavailable"
                    )

                # Mark unavailable during planned routes
                buffer_delta = timedelta(minutes=self.config.min_departure_buffer_minutes)
                
                for route in routes:
                    unavailable_start = route.plan_start_date_time - buffer_delta
                    unavailable_end = route.plan_end_date_time
                    route_slots_marked = 0
                    for idx, slot in enumerate(time_slots):
                        if unavailable_start <= slot < unavailable_end:
                            availability[idx] = False
                            route_slots_marked += 1
                    unavailable_periods.append({
                        'reason': f'Route {route.route_id}',
                        'start': unavailable_start,
                        'end': unavailable_end
                    })
                    logger.debug(
                        f"Vehicle {vehicle.vehicle_id}: Route {route.route_id} "
                        f"[{unavailable_start} to {unavailable_end}] -> {route_slots_marked} slots unavailable"
                    )

            vehicle_availability = VehicleAvailability(
                vehicle_id=vehicle.vehicle_id,
                time_slots=time_slots,
                availability_matrix=availability,
                unavailable_periods=unavailable_periods
            )
            
            availability_matrices[vehicle.vehicle_id] = vehicle_availability

            available_slots = sum(availability)
            periods_summary = [
                f"{p['reason']}({p['start']}–{p['end']})" for p in unavailable_periods
            ]
            logger.debug(
                f"Vehicle {vehicle.vehicle_id}: {available_slots}/{len(time_slots)} slots available; "
                f"unavailable_periods=[{', '.join(periods_summary)}]"
            )

        logger.debug(f"Availability matrices computed for {len(availability_matrices)} vehicles")
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
        requested_hours = (end - start).total_seconds() / 3600.0

        with db.get_cursor() as cur:
            cur.execute(Queries.GET_FORECAST_DATA, (self.config.site_id, start, end))
            rows = cur.fetchall()

            for row in rows:
                forecast_data[row["forecasted_date_time"]] = row["forecasted_consumption"]

        if forecast_data:
            keys = sorted(forecast_data.keys())
            actual_start, actual_end = keys[0], keys[-1]
            actual_hours = (actual_end - actual_start).total_seconds() / 3600.0
            logger.info(
                "[FORECAST DATA HORIZON] requested: start=%s end=%s (%.1fh) | "
                "loaded: count=%s, actual_start=%s, actual_end=%s (span %.1fh)",
                start.isoformat(), end.isoformat(), requested_hours,
                len(forecast_data), actual_start.isoformat(), actual_end.isoformat(), actual_hours,
            )
            if actual_hours < requested_hours - 0.5:
                logger.warning(
                    "[FORECAST DATA HORIZON] Loaded horizon (%.1fh) is shorter than requested (%.1fh) "
                    "- missing slots will use 0.0 kW in optimizer",
                    actual_hours, requested_hours,
                )
        else:
            logger.warning(
                "[FORECAST DATA HORIZON] No forecast data for site_id=%s in [%s, %s]; "
                "optimizer will use 0.0 kW for all slots",
                self.config.site_id, start.isoformat(), end.isoformat(),
            )
        return forecast_data
    
    def _load_price_data(self, start: datetime, end: datetime) -> Dict[datetime, Tuple[float, bool]]:
        """Load electricity price data."""
        price_data = {}
        requested_hours = (end - start).total_seconds() / 3600.0

        with db.get_cursor() as cur:
            cur.execute(Queries.GET_PRICE_DATA, (start, end))
            rows = cur.fetchall()

            for row in rows:
                price_data[row["date_time"]] = (row["electricty_price_fixed"], row["triad"])

        if price_data:
            keys = sorted(price_data.keys())
            actual_start, actual_end = keys[0], keys[-1]
            actual_hours = (actual_end - actual_start).total_seconds() / 3600.0
            logger.info(
                "[PRICE DATA HORIZON] requested: start=%s end=%s (%.1fh) | "
                "loaded: count=%s, actual_start=%s, actual_end=%s (span %.1fh)",
                start.isoformat(), end.isoformat(), requested_hours,
                len(price_data), actual_start.isoformat(), actual_end.isoformat(), actual_hours,
            )
            if actual_hours < requested_hours - 0.5:
                logger.warning(
                    "[PRICE DATA HORIZON] Loaded horizon (%.1fh) is shorter than requested (%.1fh) "
                    "- missing slots will use default price in optimizer",
                    actual_hours, requested_hours,
                )
        else:
            logger.warning(
                "[PRICE DATA HORIZON] No price data in [%s, %s]; "
                "optimizer will use default price for all slots",
                start.isoformat(), end.isoformat(),
            )
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
            synthetic_time_price_factor=self.config.synthetic_time_price_factor,
            min_soc_percent=self.config.min_soc_percent
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
        """Persist schedule results to database.
        Each vehicle gets exactly one row per time slot in the planning window;
        slots where the vehicle does not charge are stored with charge_power 0.
        """
        planning_hours = (
            (result.planning_end - result.planning_start).total_seconds() / 3600.0
            if result.planning_start and result.planning_end
            else None
        )
        # Build full list of time slots (same 30-min grid as optimization)
        all_time_slots = self._build_time_slots(result.planning_start, result.planning_end)
        n_slots = len(all_time_slots)

        logger.info(
            "[t_charge_schedule] Persisting: schedule_id=%s | planning_start=%s | planning_end=%s | "
            "planning_hours=%.1f | time_slots=%s | vehicles=%s",
            self.schedule_id,
            result.planning_start.isoformat() if result.planning_start else None,
            result.planning_end.isoformat() if result.planning_end else None,
            planning_hours or 0.0,
            n_slots,
            len(result.vehicle_schedules),
        )

        with db.get_cursor() as cur:
            # Delete existing schedule data
            cur.execute(Queries.DELETE_CHARGE_SCHEDULE_BY_SCHEDULE_ID, (self.schedule_id,))
            deleted = cur.rowcount if getattr(cur, "rowcount", None) is not None else None
            logger.info(
                "[t_charge_schedule] Deleted existing rows for schedule_id=%s (rowcount=%s)",
                self.schedule_id, deleted,
            )

            total_inserted = 0
            # One row per (vehicle, time_slot); use 0 power where vehicle does not charge
            for vehicle_schedule in result.vehicle_schedules:
                connector_id = (
                    str(vehicle_schedule.assigned_charger_id)
                    if vehicle_schedule.assigned_charger_id is not None
                    else "1"
                )
                # Map time_slot -> charge_power_kw from optimizer output (only slots with power > 0)
                power_by_slot = {
                    slot.time_slot: slot.charge_power_kw
                    for slot in (vehicle_schedule.charge_slots or [])
                }
                vehicle_inserted = 0
                for slot_time in all_time_slots:
                    power_kw = power_by_slot.get(slot_time, 0.0)
                    cur.execute(Queries.INSERT_CHARGE_SCHEDULE, (
                        self.schedule_id,
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

                total_inserted += vehicle_inserted
                charging_slots = sum(1 for st in all_time_slots if power_by_slot.get(st, 0.0) > 0.01)
                logger.info(
                    "[t_charge_schedule] Inserted vehicle_id=%s | rows=%s (one per slot) | "
                    "slots_with_charge=%s | first_slot=%s | last_slot=%s | "
                    "total_energy_scheduled_kwh=%.2f",
                    vehicle_schedule.vehicle_id,
                    vehicle_inserted,
                    charging_slots,
                    all_time_slots[0].isoformat() if all_time_slots else None,
                    all_time_slots[-1].isoformat() if all_time_slots else None,
                    getattr(vehicle_schedule, "total_energy_scheduled_kwh", None) or 0.0,
                )

            # Insert route checkpoints
            # for vehicle_id, requirements in energy_requirements.items():
            #     for requirement in requirements:
            #         cur.execute(Queries.INSERT_ROUTE_CHECKPOINT, (
            #             self.schedule_id,
            #             vehicle_id,
            #             requirement.route_id,
            #             requirement.plan_start_date_time,
            #             requirement.cumulative_energy_kwh,
            #             requirement.route_energy_buffer_kwh,
            #             requirement.efficiency_kwh_mile,
            #             datetime.utcnow()
            #         ))

        logger.info(
            "[t_charge_schedule] Persist complete: schedule_id=%s | total_rows_inserted=%s",
            self.schedule_id, total_inserted,
        )
    
    def _update_scheduler_status(self, status: str, actual_hours: Optional[float]):
        """Update scheduler status (t_scheduler has status only; actual_hours not stored)."""
        with db.get_cursor() as cur:
            cur.execute(Queries.UPDATE_SCHEDULER_STATUS, (status, self.schedule_id))
