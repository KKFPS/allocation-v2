"""Model data builders for route allocation and integrated charge scheduling."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

from src.config import (
    CHARGE_SLOT_MINUTES,
    CHARGE_SLOTS_PER_CHARGER,
    DEFAULT_TURNAROUND_TIME_MINUTES,
    PHASE1_TIE_BREAK_ROUTE_PRIZE,
)
from src.constraints.constraint_manager import ConstraintManager
from src.constraints.shift_hours import ShiftHoursStrictConstraint
from src.constraints.turnaround_time import TurnaroundTimeStrictConstraint
from src.models.route import Route
from src.models.vehicle import Vehicle
from src.utils.logging_config import logger

BIG_VALUE = 1_000_000


def build_incompatible_route_pairs(
    route_start_times: np.ndarray,
    route_end_times: np.ndarray,
    turnaround_minutes: int,
) -> List[Tuple[int, int]]:
    """
    Route pairs that cannot appear together on one vehicle sequence.

    Two routes are incompatible if neither can precede the other with required
    turnaround (includes time overlap).
    """
    pairs: List[Tuple[int, int]] = []
    n_routes = len(route_start_times)
    for r1 in range(n_routes):
        for r2 in range(r1 + 1, n_routes):
            gap_12 = float(route_start_times[r2] - route_end_times[r1])
            gap_21 = float(route_start_times[r1] - route_end_times[r2])
            if gap_12 < turnaround_minutes and gap_21 < turnaround_minutes:
                pairs.append((r1, r2))
    return pairs


def apply_incompatible_route_pair_constraints(m, vehicle_sequences, pairs) -> None:
    """Forbid assigning both routes of an incompatible pair to the same vehicle."""
    for seq in vehicle_sequences:
        for r1, r2 in pairs:
            m.constraint(
                m.not_(
                    m.and_(m.contains(seq, r1), m.contains(seq, r2)),
                )
            )


@dataclass
class AllocationModelData:
    """Inputs for route-only allocation (RouteAllocationOptimizer)."""

    vehicles: List[Vehicle]
    routes: List[Route]
    route_ids: List[str]
    distance_matrix: np.ndarray
    route_prizes: np.ndarray
    forbidden_nodes: Dict[int, Set[int]]
    mandatory_nodes: Dict[int, Set[int]]
    battery_start_soc: np.ndarray
    battery_max_soc: np.ndarray
    energy_consumption: np.ndarray
    metadata: Dict
    incompatible_route_pairs: List[Tuple[int, int]] = field(default_factory=list)


@dataclass
class ChargeSchedulingContext:
    """Homogeneous charge scheduling parameters (fixed P_fixed per slot)."""

    n_chargers: int
    time_slots: List[datetime]
    electricity_cost_per_slot: List[float]
    capacity_power_kw: List[float]
    p_fixed_kw: float
    charger_max_power_kw: Optional[List[float]] = None
    enable_variable_charger_power: bool = False

    def __post_init__(self) -> None:
        n_slots = len(self.time_slots)
        if n_slots != CHARGE_SLOTS_PER_CHARGER:
            raise ValueError(
                f"charge scheduling requires exactly {CHARGE_SLOTS_PER_CHARGER} "
                f"30-minute slots per charger, got {n_slots}"
            )
        if len(self.electricity_cost_per_slot) != n_slots:
            raise ValueError(
                "electricity_cost_per_slot length must match time_slots "
                f"({CHARGE_SLOTS_PER_CHARGER})"
            )
        if len(self.capacity_power_kw) != n_slots:
            raise ValueError(
                "capacity_power_kw length must match time_slots "
                f"({CHARGE_SLOTS_PER_CHARGER})"
            )


@dataclass
class OptimizationModelData:
    """Superset model inputs for integrated allocation + charge scheduling."""

    vehicles: List[Vehicle]
    routes: List[Route]
    route_ids: List[str]
    distance_matrix: np.ndarray
    route_prizes: np.ndarray
    forbidden_nodes: Dict[int, Set[int]]
    mandatory_nodes: Dict[int, Set[int]]
    battery_start_soc: np.ndarray
    battery_max_soc: np.ndarray
    energy_consumption: np.ndarray
    metadata: Dict
    enable_charge_scheduling: bool = False
    n_nodes: int = 0
    n_routes: int = 0
    n_chargers: int = 0
    n_timesteps: int = 0
    is_charge: np.ndarray = field(default_factory=lambda: np.array([]))
    node_rewards: np.ndarray = field(default_factory=lambda: np.array([]))
    p_fixed_kw: float = 0.0
    capacity_power_kw: List[float] = field(default_factory=list)
    enable_variable_charger_power: bool = False
    charger_max_power_kw: List[float] = field(default_factory=list)
    electricity_price_per_slot: List[float] = field(default_factory=list)
    charger_ids: List[int] = field(default_factory=list)
    incompatible_route_pairs: List[Tuple[int, int]] = field(default_factory=list)


def charge_node_index(n_routes: int, n_timesteps: int, charger: int, timestep: int) -> int:
    """Map (charger, timestep) to global node index."""
    return n_routes + charger * n_timesteps + timestep


class AllocationDataBuilder:
    """Builds route-only list-model inputs: distance matrix, prizes, SOC, forbidden nodes."""

    def __init__(
        self,
        vehicles: List[Vehicle],
        routes: List[Route],
        constraint_manager: ConstraintManager,
        max_routes_per_vehicle: int = 5,
        vehicle_charger_map: Optional[Dict[int, Optional[str]]] = None,
        mandatory_nodes: Optional[Dict[int, Set[int]]] = None,
    ):
        self.vehicles = vehicles
        self.routes = routes
        self.constraint_manager = constraint_manager
        self.max_routes_per_vehicle = max_routes_per_vehicle
        self.vehicle_charger_map = vehicle_charger_map or {}
        self.mandatory_nodes_override = mandatory_nodes or {}

        self.n_vehicles = len(vehicles)
        self.n_routes = len(routes)

        self.routes.sort(key=lambda r: r.plan_start_date_time)

    def _resolve_turnaround_minutes(self) -> int:
        for constraint in self.constraint_manager.get_enabled_constraints():
            if isinstance(constraint, TurnaroundTimeStrictConstraint):
                minimum = constraint.params.get(
                    "minimum_minutes", DEFAULT_TURNAROUND_TIME_MINUTES
                )
                try:
                    return int(minimum)
                except (TypeError, ValueError):
                    return DEFAULT_TURNAROUND_TIME_MINUTES
        return DEFAULT_TURNAROUND_TIME_MINUTES

    def _resolve_shift_hours_limit(self) -> float:
        for constraint in self.constraint_manager.get_enabled_constraints():
            if isinstance(constraint, ShiftHoursStrictConstraint):
                max_hours = constraint.params.get("max_hours", 16)
                try:
                    return float(max_hours) * 60.0
                except (TypeError, ValueError):
                    return 16.0 * 60.0
        return 16.0 * 60.0

    def build(self) -> AllocationModelData:
        """Build route-only model arrays and node constraints."""
        n_vehicles = self.n_vehicles
        n_routes = self.n_routes

        score_vr = np.zeros((n_vehicles, n_routes), dtype=float)
        feasible_vr = np.zeros((n_vehicles, n_routes), dtype=bool)
        energy_consumption = np.zeros((n_vehicles, n_routes), dtype=float)

        route_start_times = np.array(
            [r.plan_start_date_time.timestamp() / 60.0 for r in self.routes], dtype=float
        )
        route_end_times = np.array(
            [r.plan_end_date_time.timestamp() / 60.0 for r in self.routes], dtype=float
        )
        window_origin = 0.0
        if route_start_times.size:
            window_origin = float(np.min(route_start_times))
            route_start_times = route_start_times - window_origin
            route_end_times = route_end_times - window_origin

        turnaround_minutes = self._resolve_turnaround_minutes()
        shift_max_minutes = self._resolve_shift_hours_limit()

        for v_idx, vehicle in enumerate(self.vehicles):
            for r_idx, route in enumerate(self.routes):
                evaluation = self.constraint_manager.evaluate_sequence(
                    vehicle,
                    [route],
                    vehicle_charger_map=self.vehicle_charger_map,
                    all_routes=self.routes,
                    all_vehicles=self.vehicles,
                )
                if evaluation is None:
                    continue
                score_vr[v_idx, r_idx] = float(evaluation.get("total_cost", 0.0))
                feasible_vr[v_idx, r_idx] = bool(evaluation.get("is_feasible", False))
                energy_consumption[v_idx, r_idx] = float(
                    vehicle.calculate_energy_required(route.plan_mileage or 0.0)
                )

        distance_matrix = self._build_distance_matrix(
            route_start_times, route_end_times, turnaround_minutes
        )
        route_prizes = self._build_route_prizes(score_vr, feasible_vr)
        forbidden_nodes = self._build_forbidden_nodes(feasible_vr)
        mandatory_nodes = self._build_mandatory_nodes()
        battery_start_soc, battery_max_soc = self._build_battery_arrays()

        metadata = {
            "vehicles": n_vehicles,
            "routes": n_routes,
            "max_routes_per_vehicle": self.max_routes_per_vehicle,
            "feasible_assignments": int(np.sum(feasible_vr)),
            "turnaround_minutes": turnaround_minutes,
            "shift_max_minutes": shift_max_minutes,
            "big_value": BIG_VALUE,
            "window_origin_minutes": window_origin,
        }

        logger.info(
            "Built allocation model data: %s vehicles, %s routes, %s feasible pairs",
            n_vehicles,
            n_routes,
            metadata["feasible_assignments"],
        )
        metadata["route_prize_min"] = float(np.min(route_prizes)) if n_routes else 0.0
        metadata["route_prize_max"] = float(np.max(route_prizes)) if n_routes else 0.0
        metadata["route_prize_sum"] = float(np.sum(route_prizes)) if n_routes else 0.0
        feasible_scores = score_vr[feasible_vr]
        metadata["score_min_feasible"] = (
            float(np.min(feasible_scores)) if feasible_scores.size else None
        )
        metadata["score_max_feasible"] = (
            float(np.max(feasible_scores)) if feasible_scores.size else None
        )

        incompatible_pairs = build_incompatible_route_pairs(
            route_start_times, route_end_times, turnaround_minutes
        )
        metadata["incompatible_route_pairs"] = len(incompatible_pairs)
        if incompatible_pairs:
            logger.info(
                "Incompatible route pairs on same vehicle: %s (overlap / turnaround)",
                len(incompatible_pairs),
            )

        return AllocationModelData(
            vehicles=self.vehicles,
            routes=self.routes,
            route_ids=[r.route_id for r in self.routes],
            distance_matrix=distance_matrix,
            route_prizes=route_prizes,
            forbidden_nodes=forbidden_nodes,
            mandatory_nodes=mandatory_nodes,
            battery_start_soc=battery_start_soc,
            battery_max_soc=battery_max_soc,
            energy_consumption=energy_consumption,
            metadata=metadata,
            incompatible_route_pairs=incompatible_pairs,
        )

    def _build_distance_matrix(
        self,
        route_start_times: np.ndarray,
        route_end_times: np.ndarray,
        turnaround_minutes: int,
    ) -> np.ndarray:
        """Route-to-route transition costs; BIG_VALUE for infeasible arcs."""
        n_routes = self.n_routes
        dist = np.zeros((n_routes, n_routes), dtype=float)

        for r1 in range(n_routes):
            for r2 in range(n_routes):
                if r1 == r2:
                    dist[r1, r2] = BIG_VALUE
                    continue
                gap = float(route_start_times[r2] - route_end_times[r1])
                if gap < turnaround_minutes:
                    dist[r1, r2] = BIG_VALUE
                    route_a = self.routes[r1]
                    route_b = self.routes[r2]
                    if gap < 0:
                        reason = (
                            f"overlap: route {r2} starts {abs(gap):.0f} min before "
                            f"route {r1} ends"
                        )
                    else:
                        reason = (
                            f"turnaround: gap {gap:.0f} min < required {turnaround_minutes} min"
                        )
                    logger.debug(
                        "Infeasible arc [%s]->[%s] %s -> %s | %s",
                        r1,
                        r2,
                        route_a.route_id[:12],
                        route_b.route_id[:12],
                        reason,
                    )
                else:
                    dist[r1, r2] = gap

        return dist

    def _build_route_prizes(
        self, score_vr: np.ndarray, feasible_vr: np.ndarray
    ) -> np.ndarray:
        """Vehicle-agnostic route rewards (best feasible score per route)."""
        n_routes = self.n_routes
        prizes = np.zeros(n_routes, dtype=float)
        tie_break_count = 0
        for r_idx in range(n_routes):
            feasible_scores = score_vr[feasible_vr[:, r_idx], r_idx]
            if feasible_scores.size:
                best = float(np.max(feasible_scores))
                if best == 0.0:
                    prizes[r_idx] = PHASE1_TIE_BREAK_ROUTE_PRIZE
                    tie_break_count += 1
                else:
                    prizes[r_idx] = best
        if tie_break_count:
            logger.debug(
                "Applied tie-break prize %.4f to %s routes with neutral constraint score",
                PHASE1_TIE_BREAK_ROUTE_PRIZE,
                tie_break_count,
            )
        return prizes

    def _build_forbidden_nodes(self, feasible_vr: np.ndarray) -> Dict[int, Set[int]]:
        forbidden: Dict[int, Set[int]] = {}
        for v_idx in range(self.n_vehicles):
            forbidden[v_idx] = {
                r_idx for r_idx in range(self.n_routes) if not feasible_vr[v_idx, r_idx]
            }
        return forbidden

    def _build_mandatory_nodes(self) -> Dict[int, Set[int]]:
        mandatory: Dict[int, Set[int]] = {v_idx: set() for v_idx in range(self.n_vehicles)}
        for v_idx, nodes in self.mandatory_nodes_override.items():
            if 0 <= v_idx < self.n_vehicles:
                mandatory[v_idx] = set(nodes)
        return mandatory

    def _build_battery_arrays(self) -> Tuple[np.ndarray, np.ndarray]:
        start_soc = np.zeros(self.n_vehicles, dtype=float)
        max_soc = np.zeros(self.n_vehicles, dtype=float)
        for v_idx, vehicle in enumerate(self.vehicles):
            capacity = float(vehicle.battery_capacity or 0.0)
            max_soc[v_idx] = capacity
            if vehicle.estimated_soc is not None and vehicle.estimated_soc >= 0:
                start_soc[v_idx] = (float(vehicle.estimated_soc) / 100.0) * capacity
            else:
                start_soc[v_idx] = capacity
        return start_soc, max_soc


class ModelDataBuilder(AllocationDataBuilder):
    """Builds OptimizationModelData; extends route matrix when charge_context is set."""

    def build(
        self, charge_context: Optional[ChargeSchedulingContext] = None
    ) -> OptimizationModelData:
        allocation = super().build()
        if charge_context is None:
            return self._allocation_to_optimization(allocation)

        return self._build_integrated(allocation, charge_context)

    def _allocation_to_optimization(
        self, allocation: AllocationModelData
    ) -> OptimizationModelData:
        n_routes = len(allocation.routes)
        return OptimizationModelData(
            vehicles=allocation.vehicles,
            routes=allocation.routes,
            route_ids=allocation.route_ids,
            distance_matrix=allocation.distance_matrix,
            route_prizes=allocation.route_prizes,
            forbidden_nodes=allocation.forbidden_nodes,
            mandatory_nodes=allocation.mandatory_nodes,
            battery_start_soc=allocation.battery_start_soc,
            battery_max_soc=allocation.battery_max_soc,
            energy_consumption=allocation.energy_consumption,
            metadata=dict(allocation.metadata),
            incompatible_route_pairs=list(allocation.incompatible_route_pairs),
            enable_charge_scheduling=False,
            n_nodes=n_routes,
            n_routes=n_routes,
            n_chargers=0,
            n_timesteps=0,
            is_charge=np.zeros(n_routes, dtype=int),
            node_rewards=allocation.route_prizes.copy(),
            p_fixed_kw=0.0,
            capacity_power_kw=[],
        )

    def _build_integrated(
        self,
        allocation: AllocationModelData,
        ctx: ChargeSchedulingContext,
    ) -> OptimizationModelData:
        n_routes = len(allocation.routes)
        n_chargers = ctx.n_chargers
        n_timesteps = len(ctx.time_slots)
        n_nodes = n_routes + n_chargers * n_timesteps
        turnaround = int(allocation.metadata.get("turnaround_minutes", 45))

        route_start_times = np.array(
            [
                (r.plan_start_date_time.timestamp() / 60.0)
                - allocation.metadata.get("window_origin_minutes", 0.0)
                for r in allocation.routes
            ],
            dtype=float,
        )
        route_end_times = np.array(
            [
                (r.plan_end_date_time.timestamp() / 60.0)
                - allocation.metadata.get("window_origin_minutes", 0.0)
                for r in allocation.routes
            ],
            dtype=float,
        )
        slot_start_times = np.array(
            [
                (t.timestamp() / 60.0) - allocation.metadata.get("window_origin_minutes", 0.0)
                for t in ctx.time_slots
            ],
            dtype=float,
        )
        slot_duration = float(CHARGE_SLOT_MINUTES)

        dist = np.full((n_nodes, n_nodes), BIG_VALUE, dtype=float)
        rr = allocation.distance_matrix
        dist[:n_routes, :n_routes] = rr

        for r_idx in range(n_routes):
            for c in range(n_chargers):
                for t in range(n_timesteps):
                    cn = charge_node_index(n_routes, n_timesteps, c, t)
                    dist[r_idx, cn] = 0.0

        for c in range(n_chargers):
            for t in range(n_timesteps):
                cn = charge_node_index(n_routes, n_timesteps, c, t)
                dist[cn, cn] = BIG_VALUE
                if t + 1 < n_timesteps:
                    cn_next = charge_node_index(n_routes, n_timesteps, c, t + 1)
                    dist[cn, cn_next] = 0.0
                for c2 in range(n_chargers):
                    if c2 != c:
                        cn2 = charge_node_index(n_routes, n_timesteps, c2, t)
                        dist[cn, cn2] = BIG_VALUE

        for c in range(n_chargers):
            for t in range(n_timesteps):
                cn = charge_node_index(n_routes, n_timesteps, c, t)
                slot_end = slot_start_times[t] + slot_duration
                for r_idx in range(n_routes):
                    gap = float(route_start_times[r_idx] - slot_end)
                    if gap < turnaround:
                        dist[cn, r_idx] = BIG_VALUE
                        # if gap < 0:
                        #     logger.debug(
                        #         "Infeasible charge->route [%s]->[%s]: overlap %.0f min",
                        #         cn,
                        #         r_idx,
                        #         abs(gap),
                        #     )
                        # else:
                        #     logger.debug(
                        #         "Infeasible charge->route [%s]->[%s]: gap %.0f < %s",
                        #         cn,
                        #         r_idx,
                        #         gap,
                        #         turnaround,
                        #     )
                    else:
                        dist[cn, r_idx] = gap

        is_charge = np.array([0] * n_routes + [1] * (n_chargers * n_timesteps), dtype=int)
        price_per_slot = [
            abs(float(ctx.electricity_cost_per_slot[t]))
            for t in range(n_timesteps)
        ]
        if ctx.enable_variable_charger_power:
            charge_rewards = [0.0] * (n_chargers * n_timesteps)
        else:
            charge_rewards = [
                ctx.electricity_cost_per_slot[t]
                for _c in range(n_chargers)
                for t in range(n_timesteps)
            ]
        node_rewards = np.concatenate([allocation.route_prizes, np.array(charge_rewards)])
        charger_max = list(ctx.charger_max_power_kw or [])
        if len(charger_max) < n_chargers:
            charger_max.extend(
                [ctx.p_fixed_kw] * (n_chargers - len(charger_max))
            )

        energy = np.zeros((len(allocation.vehicles), n_nodes), dtype=float)
        energy[:, :n_routes] = allocation.energy_consumption

        metadata = dict(allocation.metadata)
        incompatible_pairs = list(allocation.incompatible_route_pairs)
        metadata["charge_nodes"] = n_chargers * n_timesteps
        metadata["charge_slot_minutes"] = CHARGE_SLOT_MINUTES
        metadata["charge_slots_per_charger"] = CHARGE_SLOTS_PER_CHARGER

        return OptimizationModelData(
            vehicles=allocation.vehicles,
            routes=allocation.routes,
            route_ids=allocation.route_ids,
            distance_matrix=dist,
            route_prizes=allocation.route_prizes,
            forbidden_nodes=allocation.forbidden_nodes,
            mandatory_nodes=allocation.mandatory_nodes,
            battery_start_soc=allocation.battery_start_soc,
            battery_max_soc=allocation.battery_max_soc,
            energy_consumption=energy,
            metadata=metadata,
            enable_charge_scheduling=True,
            n_nodes=n_nodes,
            n_routes=n_routes,
            n_chargers=n_chargers,
            n_timesteps=n_timesteps,
            is_charge=is_charge,
            node_rewards=node_rewards,
            p_fixed_kw=ctx.p_fixed_kw,
            capacity_power_kw=list(ctx.capacity_power_kw),
            enable_variable_charger_power=ctx.enable_variable_charger_power,
            charger_max_power_kw=charger_max[:n_chargers],
            electricity_price_per_slot=price_per_slot,
            incompatible_route_pairs=incompatible_pairs,
        )


# Backward-compatible aliases
Phase1ModelData = AllocationModelData
Phase1DataBuilder = AllocationDataBuilder
