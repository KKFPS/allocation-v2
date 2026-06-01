"""Unified controller — Phase 1 route allocation only."""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from src.config import (
    APPLICATION_NAME,
    DEFAULT_ALLOCATION_WINDOW_HOURS,
    DEFAULT_MAX_ROUTES_PER_VEHICLE,
    UNIFIED_ALLOCATION_TIME_LIMIT,
    UNIFIED_ROUTE_COUNT_WEIGHT,
)
from src.constraints.constraint_manager import ConstraintManager
from src.database.connection import db
from src.database.queries import Queries
from src.maf.parameter_parser import (
    get_all_constraint_configs,
    get_site_parameter,
    parse_maf_response,
)
from src.models.allocation import AllocationResult
from src.models.route import Route
from src.models.scheduler import ScheduleReport, VehicleScheduleReport
from src.models.vehicle import Vehicle
from src.optimizer.cost_matrix import Phase1DataBuilder
from src.optimizer.unified_optimizer import Phase1Config, Phase1Optimizer, Phase1Result
from src.utils.logging_config import logger


class UnifiedController:
    """Orchestrates MAF config, data load, Phase 1 optimization, and persistence."""

    def __init__(
        self,
        site_id: int,
        trigger_type: str = "initial",
        schedule_id: Optional[int] = None,
    ):
        self.site_id = site_id
        self.trigger_type = trigger_type
        self.schedule_id = schedule_id
        self.allocation_id: Optional[int] = None
        self.site_config: Optional[dict] = None
        self.constraint_manager: Optional[ConstraintManager] = None
        self.fleet_avg_efficiency: float = 0.35

        db.connect()

    def run_unified_optimization(
        self,
        current_time: Optional[datetime] = None,
        config: Optional[Phase1Config] = None,
        persist_to_database: bool = True,
        window_hours: Optional[float] = None,
    ) -> Tuple[AllocationResult, Phase1Result]:
        """
        Execute Phase 1 route allocation workflow.

        Returns:
            Tuple of (AllocationResult, Phase1Result)
        """
        if current_time is None:
            current_time = datetime.now()
        current_time = self._floor_to_30_min(current_time)

        logger.info("Starting Phase 1 allocation for site %s", self.site_id)

        try:
            self._initialize_optimization(current_time, window_hours_override=window_hours)
            self._load_maf_configuration()

            window_start, window_end, actual_hours = self._calculate_planning_window(
                current_time, window_hours_override=window_hours
            )
            logger.info(
                "Planning window: %s to %s (%.1f hours)",
                window_start,
                window_end,
                actual_hours,
            )

            vehicles = self._load_vehicles(current_time)
            vehicles = [
                v
                for v in vehicles
                if getattr(v, "estimated_soc", None) is None
                or float(v.estimated_soc) != -111
            ]
            logger.info("Loaded %s vehicles after VOR filter", len(vehicles))

            routes = self._load_routes(window_start, window_end)
            logger.info("Loaded %s routes for allocation", len(routes))

            constraint_configs = get_all_constraint_configs(self.site_id, self.site_config)
            self.constraint_manager = ConstraintManager(constraint_configs)

            max_routes = get_site_parameter(
                self.site_config,
                "max_routes_per_vehicle_in_window",
                DEFAULT_MAX_ROUTES_PER_VEHICLE,
            )
            vehicle_charger_map = self._load_vehicle_chargers(vehicles, current_time)

            builder = Phase1DataBuilder(
                vehicles=vehicles,
                routes=routes,
                constraint_manager=self.constraint_manager,
                max_routes_per_vehicle=max_routes,
                vehicle_charger_map=vehicle_charger_map,
            )
            model_data = builder.build()

            if config is None:
                config = Phase1Config(
                    time_limit_seconds=UNIFIED_ALLOCATION_TIME_LIMIT,
                    max_routes_per_vehicle=max_routes,
                    route_count_weight=UNIFIED_ROUTE_COUNT_WEIGHT,
                )
            else:
                config.max_routes_per_vehicle = config.max_routes_per_vehicle or max_routes
                if config.time_limit_seconds <= 0:
                    config.time_limit_seconds = UNIFIED_ALLOCATION_TIME_LIMIT

            optimizer = Phase1Optimizer(config)
            phase1_result = optimizer.solve(model_data)

            logger.info(
                "Optimization completed: status=%s objective=%.2f time=%.2fs",
                phase1_result.status,
                phase1_result.objective_value,
                phase1_result.solve_time_seconds,
            )

            allocation_result = phase1_result.to_allocation_result(
                allocation_id=self.allocation_id,
                site_id=self.site_id,
                window_start=window_start,
                window_end=window_end,
                routes=model_data.routes,
                route_ids=model_data.route_ids,
                vehicles=model_data.vehicles,
                route_prizes=model_data.route_prizes,
            )
            allocation_result.total_score = phase1_result.allocation_score
            allocation_result.routes_allocated = phase1_result.routes_allocated
            allocation_result.routes_in_window = phase1_result.routes_total

            if persist_to_database:
                if allocation_result.is_acceptable(min_score=-999999):
                    self._persist_allocation(allocation_result)
                    logger.info("Allocation results persisted")
                else:
                    logger.warning("Allocation result not acceptable, skipping persistence")
                self._update_allocation_monitor(allocation_result)

            return allocation_result, phase1_result

        except Exception as e:
            logger.error("Phase 1 allocation failed: %s", e, exc_info=True)
            if self.allocation_id:
                db.execute_query(
                    Queries.UPDATE_ALLOCATION_MONITOR,
                    ("F", 0.0, 0, 0, 0, self.allocation_id),
                    fetch=False,
                )
            raise

    def _floor_to_30_min(self, dt: datetime) -> datetime:
        minute = (dt.minute // 30) * 30
        return dt.replace(minute=minute, second=0, microsecond=0)

    def _initialize_optimization(
        self,
        current_time: datetime,
        window_hours_override: Optional[float] = None,
    ):
        window_hours = (
            window_hours_override
            if window_hours_override is not None
            else DEFAULT_ALLOCATION_WINDOW_HOURS
        )
        result = db.execute_query(
            Queries.CREATE_ALLOCATION_MONITOR,
            (
                self.site_id,
                "N",
                self.trigger_type,
                current_time,
                current_time,
                current_time + timedelta(hours=window_hours),
            ),
            fetch=True,
        )
        self.allocation_id = result[0]["allocation_id"]
        logger.info("Created allocation monitor: allocation_id=%s", self.allocation_id)

    def _load_maf_configuration(self):
        logger.info("Loading MAF configuration for %s", APPLICATION_NAME)
        try:
            result = db.execute_query(
                Queries.CALL_GET_MODULE_PARAMS,
                (APPLICATION_NAME,),
                fetch=True,
            )
            if result:
                json_params = result[0].get("sp_get_module_params")
                site_configs = parse_maf_response(json_params)
                self.site_config = site_configs.get(
                    self.site_id, {"parameters": {}, "enabled_vehicles": []}
                )
                if self.site_id not in site_configs and site_configs:
                    first_site = next(iter(site_configs.values()))
                    self.site_config = first_site
                logger.info(
                    "Loaded MAF config: %s parameters",
                    len(self.site_config.get("parameters", {})),
                )
            else:
                self.site_config = {"parameters": {}, "enabled_vehicles": []}
        except Exception as e:
            logger.error("Failed to load MAF configuration: %s", e)
            self.site_config = {"parameters": {}, "enabled_vehicles": []}

    def _calculate_planning_window(
        self,
        current_time: datetime,
        window_hours_override: Optional[float] = None,
    ) -> Tuple[datetime, datetime, float]:
        if window_hours_override is not None:
            window_hours = window_hours_override
        else:
            window_hours = get_site_parameter(
                self.site_config,
                "allocation_window_hours",
                DEFAULT_ALLOCATION_WINDOW_HOURS,
            )

        planning_start = current_time
        planning_target_end = current_time + timedelta(hours=window_hours)

        with db.get_cursor() as cur:
            cur.execute(Queries.GET_FORECAST_HORIZON, (self.site_id,))
            forecast_row = cur.fetchone()
            max_forecast_time = forecast_row["max_forecast_time"] if forecast_row else None

            cur.execute(Queries.GET_PRICE_HORIZON)
            price_row = cur.fetchone()
            max_price_time = price_row["max_price_time"] if price_row else None

        constraints = [planning_target_end]
        if max_forecast_time:
            constraints.append(max_forecast_time)
        if max_price_time:
            constraints.append(max_price_time)

        planning_end = min(constraints)
        actual_hours = (planning_end - planning_start).total_seconds() / 3600.0

        if actual_hours < window_hours:
            logger.warning(
                "Planning window constrained: requested=%sh actual=%.1fh",
                window_hours,
                actual_hours,
            )

        return planning_start, planning_end, actual_hours

    def _load_vehicles(self, as_of_time: Optional[datetime] = None) -> List[Vehicle]:
        rows = db.execute_query(
            Queries.GET_ACTIVE_VEHICLES,
            (self.site_id,),
            fetch=True,
        )
        vehicles = []
        enabled_vehicle_ids = self.site_config.get("enabled_vehicles", [])

        for row in rows:
            if enabled_vehicle_ids and row["vehicle_id"] not in enabled_vehicle_ids:
                continue
            vehicle = Vehicle(**row)
            self._load_vehicle_state(vehicle, as_of_time)
            vehicles.append(vehicle)
        return vehicles

    def _load_vehicle_state(self, vehicle: Vehicle, as_of_time: Optional[datetime] = None):
        reference_time = as_of_time if as_of_time is not None else datetime.now()
        if as_of_time is not None:
            vsm_data = db.execute_query(
                Queries.GET_VSM_AS_OF,
                (vehicle.vehicle_id, as_of_time),
                fetch=True,
            )
        else:
            vsm_data = db.execute_query(
                Queries.GET_LATEST_VSM,
                (vehicle.vehicle_id,),
                fetch=True,
            )

        if vsm_data:
            vsm = vsm_data[0]
            vehicle.current_status = vsm["status"]
            vehicle.current_route_id = vsm["route_id"]
            vehicle.estimated_soc = vsm["estimated_soc"]
            vehicle.return_eta = vsm["return_eta"]
            vehicle.return_soc = vsm["return_soc"]
            if vehicle.current_status == "On-Route" and vehicle.return_eta:
                vehicle.available_time = vehicle.return_eta
            else:
                vehicle.available_time = reference_time
            vehicle.available_energy_kwh = vehicle.get_available_energy(reference_time)
        else:
            if as_of_time is not None:
                vehicle.available_time = as_of_time
            vehicle.available_energy_kwh = vehicle.get_available_energy(reference_time)

    def _load_routes(self, window_start: datetime, window_end: datetime) -> List[Route]:
        rows = db.execute_query(
            Queries.GET_ROUTES_IN_WINDOW,
            (self.site_id, window_start, window_end),
            fetch=True,
        )
        return [Route(**row) for row in rows]

    def _load_vehicle_chargers(
        self, vehicles: List[Vehicle], reference_time: Optional[datetime] = None
    ) -> Dict[int, Optional[str]]:
        if not vehicles:
            return {}
        vehicle_ids = [v.vehicle_id for v in vehicles]
        vehicle_charger_map = db.get_vehicle_chargers_in_window(vehicle_ids, reference_time)
        logger.info(
            "Loaded charger locations for %s/%s vehicles",
            len(vehicle_charger_map),
            len(vehicles),
        )
        return vehicle_charger_map

    def _persist_allocation(self, result: AllocationResult):
        logger.info("Persisting %s allocations", len(result.allocations))
        db.execute_query(
            Queries.DELETE_SITE_ALLOCATIONS,
            (self.site_id,),
            fetch=False,
        )
        allocation_rows = []
        for alloc in result.allocations:
            row = (
                result.allocation_id,
                alloc.route_id,
                self.site_id,
                alloc.vehicle_id,
                "N",
                alloc.estimated_arrival,
                alloc.estimated_arrival_soc,
                -1,
                alloc.vehicle_id,
            )
            allocation_rows.append(row)

        if allocation_rows:
            db.execute_many(Queries.INSERT_ROUTE_ALLOCATED, allocation_rows)
            db.execute_many(Queries.INSERT_ROUTE_ALLOCATED_HISTORY, allocation_rows)
            logger.info("Persisted %s allocations", len(allocation_rows))

    def _update_allocation_monitor(self, result: AllocationResult):
        db.execute_query(
            Queries.UPDATE_ALLOCATION_MONITOR,
            (
                result.status,
                result.total_score,
                result.routes_in_window,
                result.routes_allocated,
                result.routes_overlapping_count,
                result.allocation_id,
            ),
            fetch=False,
        )
        logger.info(
            "Updated allocation monitor: status=%s score=%.2f",
            result.status,
            result.total_score,
        )

    def get_schedule_report(
        self, schedule_id: int, timestamp: datetime
    ) -> ScheduleReport:
        """Read-only schedule report from persisted charge schedule data."""
        config_rows = db.execute_query(
            Queries.GET_SCHEDULER_CONFIG,
            (schedule_id,),
            fetch=True,
        )
        if not config_rows:
            raise ValueError(f"Schedule ID {schedule_id} not found")
        row = config_rows[0]
        site_id = row["device_id"]
        schedule_status = row.get("status")

        charge_rows = db.execute_query(
            Queries.GET_CHARGE_SCHEDULE_BY_SCHEDULE_ID,
            (schedule_id,),
            fetch=True,
        )

        if not charge_rows:
            return ScheduleReport(
                schedule_id=schedule_id,
                site_id=site_id,
                report_timestamp=timestamp,
                schedule_status=schedule_status,
                notes=["No charge data for this schedule."],
            )

        slot_duration_hours = 0.5
        all_slot_times = {r["charge_start_date_time"] for r in charge_rows}
        planning_start = min(all_slot_times)
        planning_end = max(all_slot_times) + timedelta(minutes=30)

        vehicle_charge_slots: Dict[int, List[Tuple[datetime, float]]] = {}
        vehicle_energy: Dict[int, float] = {}
        vehicle_connector: Dict[int, int] = {}
        vehicle_charger_power: Dict[int, float] = {}

        for r in charge_rows:
            vid = r["vehicle_id"]
            t = r["charge_start_date_time"]
            p = float(r["charge_power"] or 0)
            connector_id = r.get("connector_id")
            charger_power = r.get("assigned_charger_power_kw")

            if vid not in vehicle_charge_slots:
                vehicle_charge_slots[vid] = []
                vehicle_energy[vid] = 0.0
            vehicle_charge_slots[vid].append((t, p))
            vehicle_energy[vid] += p * slot_duration_hours
            if connector_id and vid not in vehicle_connector:
                vehicle_connector[vid] = int(connector_id)
            if charger_power and vid not in vehicle_charger_power:
                vehicle_charger_power[vid] = float(charger_power)

        charger_power_map: Dict[int, float] = {}
        if vehicle_connector:
            charger_rows = db.execute_query(
                Queries.GET_SITE_CHARGERS,
                (site_id,),
                fetch=True,
            )
            for r in charger_rows:
                charger_power_map[r["charger_id"]] = float(r["max_power"] or 50.0)

        total_energy_scheduled_kwh = sum(vehicle_energy.values())
        vehicles_scheduled = len(vehicle_charge_slots)

        allocated_route_rows = db.execute_query(
            Queries.GET_ALLOCATED_ROUTES_IN_WINDOW,
            (site_id, planning_start, planning_end),
            fetch=True,
        )
        route_rows = db.execute_query(
            Queries.GET_ROUTES_IN_WINDOW,
            (site_id, planning_start, planning_end),
            fetch=True,
        )
        routes = [Route(**r) for r in route_rows] if route_rows else []
        routes_in_window = len(routes)

        vehicle_routes: Dict[int, List[Route]] = {}
        if allocated_route_rows:
            for route in [Route(**r) for r in allocated_route_rows]:
                vid = route.vehicle_id
                if vid is not None:
                    vehicle_routes.setdefault(vid, []).append(route)
            for vid in vehicle_routes:
                vehicle_routes[vid].sort(key=lambda r: r.plan_start_date_time)

        routes_allocated = None
        if routes:
            route_ids = [r.route_id for r in routes]
            alloc_rows = db.execute_query(
                Queries.GET_EXISTING_ALLOCATIONS,
                (site_id, route_ids),
                fetch=True,
            )
            routes_allocated = len(alloc_rows) if alloc_rows else 0

        fleet_eff = self.fleet_avg_efficiency
        try:
            eff_rows = db.execute_query(
                Queries.GET_FLEET_EFFICIENCY, (site_id,), fetch=True
            )
            if eff_rows and eff_rows[0].get("fleet_avg_efficiency") is not None:
                fleet_eff = float(eff_rows[0]["fleet_avg_efficiency"])
        except Exception:
            pass

        vehicle_reports: List[VehicleScheduleReport] = []
        total_charging_minutes_fleet = 0.0

        for vid in sorted(vehicle_charge_slots.keys()):
            slots = vehicle_charge_slots[vid]
            total_kwh = vehicle_energy[vid]
            state_rows = db.execute_query(
                Queries.GET_VEHICLE_CHARGE_STATE_AS_OF,
                (timestamp, vid),
                fetch=True,
            )
            initial_soc_kwh = None
            initial_soc_percent = None
            battery_kwh = None
            charge_rate_kw = 11.0
            efficiency = fleet_eff
            if state_rows:
                s = state_rows[0]
                battery_kwh = float(s["battery_capacity"] or 0)
                soc_pct = (
                    float(s["estimated_soc"])
                    if s.get("estimated_soc") is not None
                    else None
                )
                initial_soc_percent = soc_pct
                if soc_pct is not None and battery_kwh:
                    initial_soc_kwh = soc_pct / 100.0 * battery_kwh
                ac_kw = float(s["charge_power_ac"] or 0) or 11.0
                dc_kw = float(s["charge_power_dc"] or 0) or 50.0
                charge_rate_kw = dc_kw if s.get("is_dc_charger") else ac_kw
                if s.get("efficiency_kwh_mile"):
                    efficiency = float(s["efficiency_kwh_mile"])

            vroutes = vehicle_routes.get(vid, [])
            charging_minutes_before_first_route = 0.0
            charging_minutes_between_routes_list: List[float] = []

            if vroutes:
                first_start = vroutes[0].plan_start_date_time
                energy_before = sum(
                    p * slot_duration_hours
                    for t, p in slots
                    if t < first_start and p > 0
                )
                if charge_rate_kw > 0:
                    charging_minutes_before_first_route = (
                        energy_before / charge_rate_kw
                    ) * 60.0
                for i in range(len(vroutes) - 1):
                    gap_start = vroutes[i].plan_end_date_time
                    gap_end = vroutes[i + 1].plan_start_date_time
                    energy_between = sum(
                        p * slot_duration_hours
                        for t, p in slots
                        if gap_start <= t < gap_end and p > 0
                    )
                    mins = (
                        (energy_between / charge_rate_kw) * 60.0
                        if charge_rate_kw > 0
                        else 0.0
                    )
                    charging_minutes_between_routes_list.append(mins)

            total_between = sum(charging_minutes_between_routes_list)
            total_charging_minutes_fleet += (
                charging_minutes_before_first_route + total_between
            )

            energy_required_for_routes_kwh = sum(
                (r.plan_mileage or 0) * efficiency for r in vroutes
            )
            estimated_final_soc_kwh = None
            estimated_final_soc_percent = None
            if initial_soc_kwh is not None and battery_kwh and battery_kwh > 0:
                final_kwh = (
                    initial_soc_kwh
                    + total_kwh
                    - energy_required_for_routes_kwh
                )
                final_kwh = max(0.0, min(battery_kwh, final_kwh))
                estimated_final_soc_kwh = final_kwh
                estimated_final_soc_percent = 100.0 * final_kwh / battery_kwh

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
                    assigned_charger_power_kw=vehicle_charger_power.get(vid)
                    or (
                        charger_power_map.get(vehicle_connector.get(vid))
                        if vid in vehicle_connector
                        else None
                    ),
                    allocated_route_ids=[r.route_id for r in vroutes],
                    routes_allocated_count=len(vroutes),
                    allocated_routes=[
                        {
                            "route_id": r.route_id,
                            "plan_start_date_time": r.plan_start_date_time.isoformat(),
                            "plan_end_date_time": r.plan_end_date_time.isoformat(),
                            "plan_mileage": r.plan_mileage,
                            "route_status": getattr(r, "route_status", None),
                        }
                        for r in vroutes
                    ],
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
            vehicles_with_routes=len(vehicle_routes),
            total_charging_minutes_fleet=total_charging_minutes_fleet,
            vehicle_reports=vehicle_reports,
        )

    def close(self):
        db.close()
