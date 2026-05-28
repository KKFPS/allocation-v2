"""Allocation data builder for list-based optimization."""

from typing import Dict, List, Optional, Tuple

import numpy as np

from src.config import DEFAULT_TURNAROUND_TIME_MINUTES
from src.constraints.constraint_manager import ConstraintManager
from src.constraints.shift_hours import ShiftHoursStrictConstraint
from src.constraints.turnaround_time import (
    TurnaroundTimePreferredConstraint,
    TurnaroundTimeStrictConstraint,
)
from src.models.route import Route
from src.models.vehicle import Vehicle
from src.utils.logging_config import logger


class CostMatrixBuilder:
    """Builds per-vehicle/per-route data for list-based allocation."""
    
    def __init__(self, vehicles: List[Vehicle], routes: List[Route], 
                 constraint_manager: ConstraintManager, max_routes_per_vehicle: int = 5,
                 vehicle_charger_map: Dict[int, Optional[str]] = None):
        """
        Initialize cost matrix builder.
        
        Args:
            vehicles: List of available vehicles
            routes: List of routes to allocate
            constraint_manager: Constraint evaluation manager
            max_routes_per_vehicle: Maximum routes per vehicle in window
            vehicle_charger_map: Dict mapping vehicle_id -> charger_id or None (one vehicle per charger)
        """
        self.vehicles = vehicles
        self.routes = routes
        self.constraint_manager = constraint_manager
        self.max_routes_per_vehicle = max_routes_per_vehicle
        self.vehicle_charger_map = vehicle_charger_map or {}
        
        self.n_vehicles = len(vehicles)
        self.n_routes = len(routes)

        # Keep deterministic ordering for list variables.
        self.routes.sort(key=lambda r: r.plan_start_date_time)

    def _resolve_turnaround_minutes(self) -> int:
        """Resolve strict-turnaround minutes from active constraint config."""
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

    def _resolve_preferred_turnaround(self) -> Tuple[int, int, float, float]:
        """Resolve preferred turnaround parameters if configured."""
        for constraint in self.constraint_manager.get_enabled_constraints():
            if isinstance(constraint, TurnaroundTimePreferredConstraint):
                params = constraint.params
                return (
                    int(params.get("standard_minutes", 75)),
                    int(params.get("optimal_minutes", 90)),
                    float(params.get("penalty_standard", -2)),
                    float(params.get("penalty_optimal", -1)),
                )
        return 75, 90, -2.0, -1.0

    def _resolve_shift_hours_limit(self) -> float:
        """Resolve strict shift-hours cap in minutes."""
        for constraint in self.constraint_manager.get_enabled_constraints():
            if isinstance(constraint, ShiftHoursStrictConstraint):
                max_hours = constraint.params.get("max_hours", 16)
                try:
                    return float(max_hours) * 60.0
                except (TypeError, ValueError):
                    return 16.0 * 60.0
        return 16.0 * 60.0

    def build_allocation_data(
        self,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, Dict]:
        """Build list-model allocation inputs without route combination enumeration."""
        n_vehicles = self.n_vehicles
        n_routes = self.n_routes

        score_vr = np.zeros((n_vehicles, n_routes), dtype=float)
        feasible_vr = np.zeros((n_vehicles, n_routes), dtype=bool)
        route_energy_required = np.zeros((n_vehicles, n_routes), dtype=float)

        route_start_times = np.array(
            [r.plan_start_date_time.timestamp() / 60.0 for r in self.routes], dtype=float
        )
        route_end_times = np.array(
            [r.plan_end_date_time.timestamp() / 60.0 for r in self.routes], dtype=float
        )
        route_durations = np.array([float(r.duration_minutes) for r in self.routes], dtype=float)

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
                route_energy_required[v_idx, r_idx] = float(
                    vehicle.calculate_energy_required(route.plan_mileage)
                )

        metadata = {
            "vehicles": n_vehicles,
            "routes": n_routes,
            "max_routes_per_vehicle": self.max_routes_per_vehicle,
            "feasible_assignments": int(np.sum(feasible_vr)),
            "turnaround_minutes": self._resolve_turnaround_minutes(),
            "shift_max_minutes": self._resolve_shift_hours_limit(),
            "turnaround_preferred": self._resolve_preferred_turnaround(),
        }
        logger.info(
            "Built allocation data: %s vehicles, %s routes, %s feasible pairs",
            n_vehicles,
            n_routes,
            metadata["feasible_assignments"],
        )
        return (
            score_vr,
            feasible_vr,
            route_start_times,
            route_end_times,
            route_durations,
            route_energy_required,
            metadata,
        )

    def build_assignment_matrix(self) -> Tuple[np.ndarray, List, Dict]:
        """
        Legacy compatibility method.

        Returns single-route feasible assignments only. Route combinations are intentionally
        removed; multi-route sequencing is now handled natively by Hexaly list variables.
        """
        (
            score_vr,
            feasible_vr,
            _route_start_times,
            _route_end_times,
            _route_durations,
            _route_energy_required,
            metadata,
        ) = self.build_allocation_data()

        sequences: List[Tuple[int, List[Route], float]] = []
        sequence_costs: List[float] = []
        for v_idx, vehicle in enumerate(self.vehicles):
            for r_idx, route in enumerate(self.routes):
                if not feasible_vr[v_idx, r_idx]:
                    continue
                score = float(score_vr[v_idx, r_idx])
                sequences.append((vehicle.vehicle_id, [route], score))
                sequence_costs.append(score)

        metadata = {
            **metadata,
            "total_sequences": len(sequences),
        }
        return np.array(sequence_costs, dtype=float), sequences, metadata
