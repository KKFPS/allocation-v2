"""Phase 1 allocation data builder for list-based Hexaly routing."""

from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

from src.config import DEFAULT_TURNAROUND_TIME_MINUTES, PHASE1_TIE_BREAK_ROUTE_PRIZE
from src.constraints.constraint_manager import ConstraintManager
from src.constraints.shift_hours import ShiftHoursStrictConstraint
from src.constraints.turnaround_time import TurnaroundTimeStrictConstraint
from src.models.route import Route
from src.models.vehicle import Vehicle
from src.utils.logging_config import logger

BIG_VALUE = 1_000_000


@dataclass
class Phase1ModelData:
    """Inputs for Phase1Optimizer."""

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


class Phase1DataBuilder:
    """Builds Phase 1 list-model inputs: distance matrix, prizes, SOC, forbidden nodes."""

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

    def build(self) -> Phase1ModelData:
        """Build all Phase 1 model arrays and node constraints."""
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
        }

        logger.info(
            "Built Phase 1 model data: %s vehicles, %s routes, %s feasible pairs",
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

        logger.debug(
            "distance_matrix infeasible arcs=%s, route_prize range=[%.2f, %.2f]",
            int(np.sum(distance_matrix >= BIG_VALUE)) - n_routes,
            metadata["route_prize_min"],
            metadata["route_prize_max"],
        )

        return Phase1ModelData(
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
        infeasible_off_diagonal = 0
        feasible_off_diagonal = 0

        for r1 in range(n_routes):
            for r2 in range(n_routes):
                if r1 == r2:
                    dist[r1, r2] = BIG_VALUE
                    continue
                gap = float(route_start_times[r2] - route_end_times[r1])
                route_a = self.routes[r1]
                route_b = self.routes[r2]
                if gap < turnaround_minutes:
                    dist[r1, r2] = BIG_VALUE
                    infeasible_off_diagonal += 1
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
                        "Infeasible arc [%s]->[%s] %s -> %s | %s | "
                        "end[%s]=%s start[%s]=%s",
                        r1,
                        r2,
                        route_a.route_id[:12],
                        route_b.route_id[:12],
                        reason,
                        r1,
                        route_a.plan_end_date_time,
                        r2,
                        route_b.plan_start_date_time,
                    )
                else:
                    dist[r1, r2] = gap
                    feasible_off_diagonal += 1
                    logger.debug(
                        "Feasible arc [%s]->[%s] gap=%.0f min (turnaround ok) "
                        "end[%s] -> start[%s]",
                        r1,
                        r2,
                        gap,
                        route_a.plan_end_date_time,
                        route_b.plan_start_date_time,
                    )

        logger.info(
            "Distance matrix %sx%s: %s feasible off-diagonal arcs, "
            "%s infeasible (self-loops=%s, turnaround/overlap=%s)",
            n_routes,
            n_routes,
            feasible_off_diagonal,
            infeasible_off_diagonal + n_routes,
            n_routes,
            infeasible_off_diagonal,
        )
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
        """Routes that vehicle v cannot visit."""
        forbidden: Dict[int, Set[int]] = {}
        for v_idx in range(self.n_vehicles):
            forbidden[v_idx] = {
                r_idx for r_idx in range(self.n_routes) if not feasible_vr[v_idx, r_idx]
            }
        return forbidden

    def _build_mandatory_nodes(self) -> Dict[int, Set[int]]:
        """Mandatory route indices per vehicle (empty unless overridden)."""
        mandatory: Dict[int, Set[int]] = {v_idx: set() for v_idx in range(self.n_vehicles)}
        for v_idx, nodes in self.mandatory_nodes_override.items():
            if 0 <= v_idx < self.n_vehicles:
                mandatory[v_idx] = set(nodes)
        return mandatory

    def _build_battery_arrays(self) -> Tuple[np.ndarray, np.ndarray]:
        """Per-vehicle SOC bounds in kWh."""
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
