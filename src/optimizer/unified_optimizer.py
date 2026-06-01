"""Integrated Hexaly optimizer: route allocation + homogeneous charge scheduling."""

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

import hexaly.optimizer as hx
import numpy as np

from src.config import (
    CHARGE_SLOT_MINUTES,
    DEFAULT_ROUTE_ENERGY_SAFETY_MARGIN_KWH,
    DEFAULT_TARGET_SOC_PERCENT,
    IS_HEXALY_ACTIVE,
    UNIFIED_INTEGRATED_TIME_LIMIT,
    UNIFIED_ROUTE_COUNT_WEIGHT,
    UNIFIED_SCHEDULING_TIME_LIMIT,
    UNIFIED_SOC_SHORTFALL_PENALTY,
)
from src.models.allocation import AllocationResult, RouteAllocation
from src.models.route import Route
from src.models.scheduler import ChargeScheduleResult, ChargeSlot, VehicleChargeSchedule
from src.models.vehicle import Vehicle
from src.optimizer.allocation_optimizer import route_transition_delay
from src.optimizer.cost_matrix import (
    BIG_VALUE,
    OptimizationModelData,
    apply_incompatible_route_pair_constraints,
    charge_node_index,
)
from src.optimizer.optimizer_debug import log_model_inputs, validate_optimization_result
from src.utils.logging_config import logger

MODE_FLAG_ALLOCATION = "allocation"
MODE_FLAG_CHARGE_SCHEDULING = "charge_scheduling"
MODE_FLAG_CHARGER_ALLOCATION = "charger_allocation"


def normalize_mode(mode: Optional[List[str]]) -> List[str]:
    """Normalize API mode flags; default allocation-only."""
    if not mode:
        return [MODE_FLAG_ALLOCATION]
    normalized = []
    for m in mode:
        key = (m or "").strip().lower()
        if key and key not in normalized:
            normalized.append(key)
    if not normalized:
        return [MODE_FLAG_ALLOCATION]
    allowed = {
        MODE_FLAG_ALLOCATION,
        MODE_FLAG_CHARGE_SCHEDULING,
        MODE_FLAG_CHARGER_ALLOCATION,
    }
    unknown = set(normalized) - allowed
    if unknown:
        raise ValueError(f"Unknown mode flags: {sorted(unknown)}")
    if MODE_FLAG_CHARGER_ALLOCATION in normalized and (
        MODE_FLAG_CHARGE_SCHEDULING not in normalized
    ):
        raise ValueError(
            "charger_allocation requires charge_scheduling in mode"
        )
    return normalized


@dataclass
class OptimizationConfig:
    """Configuration for integrated optimization."""

    time_limit_seconds: int = UNIFIED_INTEGRATED_TIME_LIMIT
    big_value_penalty: float = BIG_VALUE
    max_routes_per_vehicle: Optional[int] = None
    route_count_weight: float = UNIFIED_ROUTE_COUNT_WEIGHT
    p_fixed_kw: Optional[float] = None
    verbosity: int = 1
    mode_flags: Optional[List[str]] = None
    soc_shortfall_penalty: float = UNIFIED_SOC_SHORTFALL_PENALTY
    target_soc_percent: float = DEFAULT_TARGET_SOC_PERCENT
    route_energy_safety_margin_kwh: float = DEFAULT_ROUTE_ENERGY_SAFETY_MARGIN_KWH
    enable_variable_charger_power: bool = False


@dataclass
class OptimizationResult:
    """Result from integrated optimization."""

    status: str
    solve_time_seconds: float
    objective_value: float
    vehicle_route_sequences: Dict[int, List[int]] = field(default_factory=dict)
    charge_slots_assigned: Dict[int, List[Tuple[int, int, float]]] = field(
        default_factory=dict
    )
    routes_allocated: int = 0
    routes_total: int = 0
    allocation_score: float = 0.0

    def to_allocation_result(
        self,
        allocation_id: int,
        site_id: int,
        window_start: datetime,
        window_end: datetime,
        routes: List[Route],
        route_ids: List[str],
        vehicles: List[Vehicle],
        route_prizes: np.ndarray,
    ) -> AllocationResult:
        result = AllocationResult(
            allocation_id=allocation_id,
            site_id=site_id,
            run_datetime=datetime.now(),
            window_start=window_start,
            window_end=window_end,
            total_score=self.allocation_score,
            routes_in_window=len(route_ids),
            routes_allocated=self.routes_allocated,
            status="P" if self.status in ("OPTIMAL", "FEASIBLE") else "F",
        )

        route_by_idx = {idx: routes[idx] for idx in range(len(routes))}
        vehicle_by_idx = {idx: v for idx, v in enumerate(vehicles)}
        allocated_route_ids: Set[str] = set()

        for v_idx, node_indices in self.vehicle_route_sequences.items():
            vehicle = vehicle_by_idx.get(v_idx)
            if vehicle is None:
                continue
            n_on_vehicle = len(node_indices)
            for r_idx in node_indices:
                route = route_by_idx.get(r_idx)
                if route is None:
                    continue
                prize = float(route_prizes[r_idx]) if r_idx < len(route_prizes) else 0.0
                per_route_cost = prize / n_on_vehicle if n_on_vehicle else prize
                soc_pct = 80.0
                if vehicle.estimated_soc is not None:
                    soc_pct = float(vehicle.estimated_soc)
                result.add_allocation(
                    RouteAllocation(
                        route_id=route.route_id,
                        vehicle_id=vehicle.vehicle_id,
                        estimated_arrival=route.plan_end_date_time,
                        estimated_arrival_soc=soc_pct,
                        cost=per_route_cost,
                    )
                )
                allocated_route_ids.add(route.route_id)

        for route_id in route_ids:
            if route_id not in allocated_route_ids:
                result.mark_unallocated(route_id)

        return result

    def to_schedule_result(
        self,
        schedule_id: int,
        site_id: int,
        planning_start: datetime,
        planning_end: datetime,
        time_slots: List[datetime],
        vehicles: List[Vehicle],
        p_fixed_kw: float,
        charger_ids: Optional[List[int]] = None,
    ) -> ChargeScheduleResult:
        vehicle_by_idx = {idx: v for idx, v in enumerate(vehicles)}
        schedules: List[VehicleChargeSchedule] = []

        for v_idx, slots in self.charge_slots_assigned.items():
            vehicle = vehicle_by_idx.get(v_idx)
            if vehicle is None:
                continue
            charge_slots = []
            total_energy = 0.0
            for slot in slots:
                c, t, power_kw = slot[0], slot[1], slot[2]
                if t < len(time_slots):
                    slot_time = time_slots[t]
                    charge_slots.append(
                        ChargeSlot(time_slot=slot_time, charge_power_kw=power_kw)
                    )
                    total_energy += power_kw * (CHARGE_SLOT_MINUTES / 60.0)
            cap = float(vehicle.battery_capacity or 0.0)
            start_kwh = cap
            if vehicle.estimated_soc is not None and vehicle.estimated_soc >= 0:
                start_kwh = (float(vehicle.estimated_soc) / 100.0) * cap
            first_c = slots[0][0] if slots else None
            if first_c is not None and charger_ids and first_c < len(charger_ids):
                assigned_charger_id = charger_ids[first_c]
            elif first_c is not None:
                assigned_charger_id = first_c + 1
            else:
                assigned_charger_id = None
            schedules.append(
                VehicleChargeSchedule(
                    vehicle_id=vehicle.vehicle_id,
                    initial_soc_kwh=start_kwh,
                    charge_slots=charge_slots,
                    total_energy_scheduled_kwh=total_energy,
                    assigned_charger_id=assigned_charger_id,
                    assigned_charger_power_kw=slots[0][2] if slots else None,
                )
            )

        return ChargeScheduleResult(
            schedule_id=schedule_id,
            site_id=site_id,
            planning_start=planning_start,
            planning_end=planning_end,
            vehicle_schedules=schedules,
        )


class UnifiedOptimizer:
    """Integrated route + charge scheduling Hexaly model."""

    def __init__(self, config: Optional[OptimizationConfig] = None):
        self.config = config or OptimizationConfig()

    def solve(self, model_data: OptimizationModelData) -> OptimizationResult:
        logger.info(
            "UnifiedOptimizer.solve: vehicles=%s routes=%s nodes=%s charge=%s variable_power=%s",
            len(model_data.vehicles),
            model_data.n_routes,
            model_data.n_nodes,
            model_data.enable_charge_scheduling,
            model_data.enable_variable_charger_power,
        )
        log_model_inputs(model_data, self.config.route_count_weight)

        if not IS_HEXALY_ACTIVE:
            logger.warning("Hexaly not active — using greedy fallback")
            return self._greedy_fallback(model_data)

        return self._solve_hexaly(model_data)

    def _solve_hexaly(self, model_data: OptimizationModelData) -> OptimizationResult:
        start_time = time.time()
        n_vehicles = len(model_data.vehicles)
        n_routes = model_data.n_routes
        n_nodes = model_data.n_nodes
        shift_max = float(model_data.metadata.get("shift_max_minutes", 16 * 60))
        max_routes = self.config.max_routes_per_vehicle
        if max_routes is None:
            max_routes = int(model_data.metadata.get("max_routes_per_vehicle", n_routes))

        p_fixed = float(
            self.config.p_fixed_kw
            if self.config.p_fixed_kw is not None
            else model_data.p_fixed_kw
        )

        if n_nodes == 0 or n_vehicles == 0:
            return OptimizationResult(
                status="OPTIMAL",
                solve_time_seconds=time.time() - start_time,
                objective_value=0.0,
                routes_total=n_routes,
            )

        with hx.HexalyOptimizer() as optimizer:
            m = optimizer.model

            dist_arr = m.array(model_data.distance_matrix.tolist())
            node_reward = m.array(model_data.node_rewards.tolist())
            is_charge_arr = m.array(model_data.is_charge.tolist())
            energy_arr = m.array(model_data.energy_consumption.tolist())
            battery_start = m.array(model_data.battery_start_soc.tolist())
            battery_max = m.array(model_data.battery_max_soc.tolist())

            vehicle_sequences = [m.list(n_nodes) for _ in range(n_vehicles)]
            m.constraint(m.disjoint(vehicle_sequences))

            variable_power = (
                model_data.enable_variable_charger_power
                and model_data.enable_charge_scheduling
            )
            n_chargers = model_data.n_chargers
            n_timesteps = model_data.n_timesteps
            slot_hours = CHARGE_SLOT_MINUTES / 60.0
            charge_power_arr = None
            charge_power_vars: List[List] = []
            node_charger_arr = None
            node_timestep_arr = None

            if variable_power:
                node_to_charger = [0] * n_routes + [
                    c for c in range(n_chargers) for _t in range(n_timesteps)
                ]
                node_to_timestep = [0] * n_routes + [
                    t for c in range(n_chargers) for t in range(n_timesteps)
                ]
                node_charger_arr = m.array(node_to_charger)
                node_timestep_arr = m.array(node_to_timestep)
                charger_max = model_data.charger_max_power_kw or []
                power_grid = []
                for c in range(n_chargers):
                    max_p = max(1, int(round(float(charger_max[c]))))
                    power_grid.append(
                        [m.int(0, max_p) for _ in range(n_timesteps)]
                    )
                charge_power_vars = power_grid
                charge_power_arr = m.array(power_grid)
                for c in range(n_chargers):
                    max_p = max(1, int(round(float(charger_max[c]))))
                    for t in range(n_timesteps):
                        node = charge_node_index(n_routes, n_timesteps, c, t)
                        used_terms = [
                            m.contains(seq, node) for seq in vehicle_sequences
                        ]
                        used = (
                            used_terms[0]
                            if len(used_terms) == 1
                            else m.sum(used_terms)
                        )
                        m.constraint(m.at(charge_power_arr, c, t) <= max_p * used)
                logger.info(
                    "Phase 3 variable charger power: %s chargers × %s slots",
                    n_chargers,
                    n_timesteps,
                )

            for v_idx, forbidden in model_data.forbidden_nodes.items():
                if v_idx >= n_vehicles:
                    continue
                for node in forbidden:
                    m.constraint(m.not_(m.contains(vehicle_sequences[v_idx], node)))

            for v_idx, mandatory in model_data.mandatory_nodes.items():
                if v_idx >= n_vehicles:
                    continue
                for node in mandatory:
                    m.constraint(m.contains(vehicle_sequences[v_idx], node))

            if model_data.incompatible_route_pairs:
                apply_incompatible_route_pair_constraints(
                    m, vehicle_sequences, model_data.incompatible_route_pairs
                )

            route_count_terms = [
                m.sum(
                    seq,
                    m.lambda_function(
                        lambda node: m.iif(m.at(is_charge_arr, node) == 0, 1, 0)
                    ),
                )
                for seq in vehicle_sequences
            ]
            route_count_term = (
                route_count_terms[0]
                if len(route_count_terms) == 1
                else m.sum(route_count_terms)
            )

            for seq in vehicle_sequences:
                delay = route_transition_delay(m, seq, dist_arr)
                m.constraint(delay < self.config.big_value_penalty)
                m.constraint(delay <= shift_max)

            if max_routes < n_routes:
                for route_count_v in route_count_terms:
                    m.constraint(route_count_v <= max_routes)

            soc_shortfall_terms = []
            penalty_per_kwh = float(self.config.soc_shortfall_penalty)
            safety_margin = float(self.config.route_energy_safety_margin_kwh)
            target_soc_frac = float(self.config.target_soc_percent) / 100.0

            for v_idx, seq in enumerate(vehicle_sequences):
                start_kwh = m.at(battery_start, v_idx)
                max_kwh = m.at(battery_max, v_idx)
                target_kwh = max_kwh * target_soc_frac
                if variable_power:
                    soc_after = m.array(
                        m.range(0, m.count(seq)),
                        m.lambda_function(
                            lambda n, prev: m.iif(
                                m.at(is_charge_arr, seq[n]) > 0,
                                m.min(
                                    prev
                                    + m.at(
                                        charge_power_arr,
                                        m.at(node_charger_arr, seq[n]),
                                        m.at(node_timestep_arr, seq[n]),
                                    )
                                    * slot_hours,
                                    max_kwh,
                                ),
                                m.max(
                                    prev - m.at(energy_arr, v_idx, seq[n]), 0
                                ),
                            ),
                        ),
                        start_kwh,
                    )
                else:
                    soc_after = m.array(
                        m.range(0, m.count(seq)),
                        m.lambda_function(
                            lambda n, prev: m.iif(
                                m.at(is_charge_arr, seq[n]) > 0,
                                m.min(prev + p_fixed, max_kwh),
                                m.max(
                                    prev - m.at(energy_arr, v_idx, seq[n]), 0
                                ),
                            ),
                        ),
                        start_kwh,
                    )
                soc_before = m.array(
                    m.range(0, m.count(seq)),
                    m.lambda_function(lambda n, prev: prev),
                    start_kwh,
                )
                out_of_battery = m.sum(
                    m.range(0, m.count(seq)),
                    m.lambda_function(lambda n: m.max(0, -m.at(soc_after, n))),
                )
                m.constraint(out_of_battery == 0)
                m.constraint(
                    m.iif(
                        m.count(seq) > 0,
                        m.at(soc_after, m.count(seq) - 1),
                        max_kwh,
                    )
                    <= max_kwh + 1e-6
                )

                route_shortfall = m.sum(
                    m.range(0, m.count(seq)),
                    m.lambda_function(
                        lambda n: m.iif(
                            m.at(is_charge_arr, seq[n]) > 0,
                            0,
                            m.max(
                                0,
                                m.max(
                                    m.at(energy_arr, v_idx, seq[n]) + safety_margin,
                                    target_kwh,
                                )
                                - m.at(soc_before, n),
                            ),
                        )
                    ),
                )
                final_soc = m.iif(
                    m.count(seq) > 0,
                    m.at(soc_after, m.count(seq) - 1),
                    start_kwh,
                )
                final_shortfall = m.max(0, target_kwh - final_soc)
                soc_shortfall_terms.append(route_shortfall + final_shortfall)

            if model_data.enable_charge_scheduling and model_data.capacity_power_kw:
                for t in range(n_timesteps):
                    cap_kw = float(model_data.capacity_power_kw[t])
                    if variable_power and charge_power_arr is not None:
                        load_terms = [
                            m.at(charge_power_arr, c, t) for c in range(n_chargers)
                        ]
                        site_load = (
                            load_terms[0]
                            if len(load_terms) == 1
                            else m.sum(load_terms)
                        )
                        m.constraint(site_load < cap_kw)
                    else:
                        active_terms = []
                        for seq in vehicle_sequences:
                            for c in range(n_chargers):
                                node = charge_node_index(
                                    n_routes, n_timesteps, c, t
                                )
                                active_terms.append(m.contains(seq, node))
                        if active_terms:
                            active_count = (
                                active_terms[0]
                                if len(active_terms) == 1
                                else m.sum(active_terms)
                            )
                            m.constraint(active_count * p_fixed < cap_kw)

            reward_terms = [
                m.sum(seq, m.lambda_function(lambda node: m.at(node_reward, node)))
                for seq in vehicle_sequences
            ]
            prize_term = reward_terms[0] if len(reward_terms) == 1 else m.sum(reward_terms)
            w = float(self.config.route_count_weight)
            objective = w * route_count_term + prize_term
            if variable_power and charge_power_arr is not None:
                prices = model_data.electricity_price_per_slot
                cost_terms = []
                for c in range(n_chargers):
                    for t in range(n_timesteps):
                        price = float(prices[t]) if t < len(prices) else 0.0
                        cost_terms.append(
                            -price
                            * slot_hours
                            * m.at(charge_power_arr, c, t)
                        )
                charging_cost = (
                    cost_terms[0] if len(cost_terms) == 1 else m.sum(cost_terms)
                )
                objective = objective + charging_cost
            if penalty_per_kwh > 0 and soc_shortfall_terms:
                shortfall_term = (
                    soc_shortfall_terms[0]
                    if len(soc_shortfall_terms) == 1
                    else m.sum(soc_shortfall_terms)
                )
                objective = objective - penalty_per_kwh * shortfall_term
                logger.info(
                    "SOC soft penalty active: %.2f per kWh shortfall "
                    "(target_soc=%.0f%%, route_margin=%.1f kWh)",
                    penalty_per_kwh,
                    self.config.target_soc_percent,
                    safety_margin,
                )
            m.maximize(objective)

            logger.info(
                "Unified model built: vehicles=%s nodes=%s expressions=%s",
                n_vehicles,
                n_nodes,
                m.get_nb_expressions(),
            )
            m.close()
            optimizer.param.time_limit = self.config.time_limit_seconds
            optimizer.param.verbosity = self.config.verbosity
            optimizer.solve()

            sol = optimizer.solution
            status = (
                sol.status.name
                if hasattr(sol.status, "name")
                else str(sol.status).split(".")[-1]
            )
            obj_value = float(sol.get_value(objective)) if sol else 0.0

            route_sequences: Dict[int, List[int]] = {}
            charge_slots: Dict[int, List[Tuple[int, int, float]]] = {}
            total_routes = 0

            for v_idx, seq_var in enumerate(vehicle_sequences):
                collection = sol.get_value(seq_var)
                seq_vals = [int(x) for x in collection]
                routes_on_v: List[int] = []
                slots_on_v: List[Tuple[int, int, float]] = []
                for node in seq_vals:
                    if node < n_routes:
                        routes_on_v.append(node)
                    elif model_data.enable_charge_scheduling:
                        offset = node - n_routes
                        c = offset // model_data.n_timesteps
                        t = offset % model_data.n_timesteps
                        if variable_power and charge_power_vars:
                            power_kw = float(
                                sol.get_value(charge_power_vars[c][t])
                            )
                        else:
                            power_kw = p_fixed
                        slots_on_v.append((c, t, power_kw))
                if routes_on_v:
                    route_sequences[v_idx] = routes_on_v
                    total_routes += len(routes_on_v)
                if slots_on_v:
                    charge_slots[v_idx] = slots_on_v

            result = OptimizationResult(
                status=status,
                solve_time_seconds=time.time() - start_time,
                objective_value=obj_value,
                vehicle_route_sequences=route_sequences,
                charge_slots_assigned=charge_slots,
                routes_allocated=total_routes,
                routes_total=n_routes,
                allocation_score=obj_value,
            )
            validate_optimization_result(
                model_data, result, self.config.route_count_weight
            )
            return result

    def _greedy_fallback(self, model_data: OptimizationModelData) -> OptimizationResult:
        start_time = time.time()
        n_vehicles = len(model_data.vehicles)
        n_routes = model_data.n_routes
        assigned_routes: Set[int] = set()
        route_sequences: Dict[int, List[int]] = {}

        route_order = sorted(
            range(n_routes),
            key=lambda r: model_data.routes[r].plan_start_date_time,
        )
        w = float(self.config.route_count_weight)

        for r_idx in route_order:
            best_v = None
            best_score = -np.inf
            for v_idx in range(n_vehicles):
                if r_idx in model_data.forbidden_nodes.get(v_idx, set()):
                    continue
                if r_idx in assigned_routes:
                    continue
                score = w + float(model_data.route_prizes[r_idx])
                if score > best_score:
                    best_score = score
                    best_v = v_idx
            if best_v is not None:
                route_sequences.setdefault(best_v, []).append(r_idx)
                assigned_routes.add(r_idx)

        total_routes = sum(len(s) for s in route_sequences.values())
        obj = sum(
            float(model_data.route_prizes[r])
            for seq in route_sequences.values()
            for r in seq
        ) + w * total_routes

        return OptimizationResult(
            status="FEASIBLE",
            solve_time_seconds=time.time() - start_time,
            objective_value=obj,
            vehicle_route_sequences=route_sequences,
            routes_allocated=total_routes,
            routes_total=n_routes,
            allocation_score=obj,
        )
