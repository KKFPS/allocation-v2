"""Route allocation Hexaly optimizer (list-based VRP, route nodes only).

Frozen implementation — do not modify for charge scheduling extensions.
Use UnifiedOptimizer in unified_optimizer.py when charge_scheduling is enabled.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set

import hexaly.optimizer as hx
import numpy as np

from src.config import (
    IS_HEXALY_ACTIVE,
    UNIFIED_ALLOCATION_TIME_LIMIT,
    UNIFIED_ROUTE_COUNT_WEIGHT,
)
from src.models.allocation import AllocationResult, RouteAllocation
from src.models.route import Route
from src.models.vehicle import Vehicle
from src.optimizer.cost_matrix import (
    BIG_VALUE,
    AllocationModelData,
    apply_incompatible_route_pair_constraints,
)
from src.optimizer.optimizer_debug import (
    allocation_model_to_optimization_data,
    log_allocation_model_inputs,
    validate_allocation_solver_result,
    write_optimizer_debug_csv,
)
from src.utils.logging_config import logger

BIG_VALUE_PENALTY = BIG_VALUE


def route_transition_delay(m, seq, dist_arr):
    """
    Sum transition costs for consecutive pairs only (positions 1..count-1).

    Uses range(1, count(seq)) so lambdas never evaluate seq[i-1] at i=0.
    """
    return m.sum(
        m.range(1, m.count(seq)),
        m.lambda_function(lambda i: m.at(dist_arr, seq[i - 1], seq[i])),
    )


def sequence_service_time(m, seq, duration_arr):
    """Sum per-node service durations (route length, charge slot length) in minutes."""
    return m.sum(
        m.range(0, m.count(seq)),
        m.lambda_function(lambda i: m.at(duration_arr, seq[i])),
    )


def log_model_complexity(m, n_vehicles: int, n_routes: int) -> None:
    """Log Hexaly model size after build (before close/solve)."""
    logger.info(
        "Route allocation model built: vehicles=%s routes=%s expressions=%s "
        "decisions=%s constraints=%s",
        n_vehicles,
        n_routes,
        m.get_nb_expressions(),
        m.get_nb_decisions(),
        m.get_nb_constraints(),
    )


@dataclass
class AllocationConfig:
    """Configuration for route-only allocation."""

    time_limit_seconds: int = UNIFIED_ALLOCATION_TIME_LIMIT
    big_value_penalty: float = BIG_VALUE_PENALTY
    max_routes_per_vehicle: Optional[int] = None
    route_count_weight: float = UNIFIED_ROUTE_COUNT_WEIGHT
    verbosity: int = 1


@dataclass
class RouteAllocationSolverResult:
    """Result from route-only allocation optimization."""

    status: str
    solve_time_seconds: float
    objective_value: float
    vehicle_sequences: Dict[int, List[int]] = field(default_factory=dict)
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
        """Convert solver output to AllocationResult for persistence."""
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

        for v_idx, node_indices in self.vehicle_sequences.items():
            vehicle = vehicle_by_idx.get(v_idx)
            if vehicle is None:
                continue
            n_routes_on_vehicle = len(node_indices)
            for r_idx in node_indices:
                route = route_by_idx.get(r_idx)
                if route is None:
                    continue
                prize = float(route_prizes[r_idx]) if r_idx < len(route_prizes) else 0.0
                per_route_cost = prize / n_routes_on_vehicle if n_routes_on_vehicle else prize
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


class RouteAllocationOptimizer:
    """
    Route-only Hexaly model: disjoint vehicle sequences over route nodes.

    See docs/hexaly_model_structure_and_usage.md — Route allocation section.
    """

    def __init__(self, config: Optional[AllocationConfig] = None):
        self.config = config or AllocationConfig()

    def solve(self, model_data: AllocationModelData) -> RouteAllocationSolverResult:
        """Solve route allocation."""
        logger.info(
            "RouteAllocationOptimizer.solve: vehicles=%s routes=%s",
            model_data.metadata.get("vehicles"),
            model_data.metadata.get("routes"),
        )
        logger.debug("Allocation model metadata: %s", model_data.metadata)
        log_allocation_model_inputs(model_data, self.config.route_count_weight)

        if not IS_HEXALY_ACTIVE:
            logger.warning("Hexaly not active — using greedy fallback")
            return self._greedy_fallback(model_data)

        return self._solve_hexaly(model_data)

    def _solve_hexaly(self, model_data: AllocationModelData) -> RouteAllocationSolverResult:
        start_time = time.time()
        n_vehicles = len(model_data.vehicles)
        n_routes = len(model_data.routes)
        shift_max = float(model_data.metadata.get("shift_max_minutes", 16 * 60))
        max_routes = self.config.max_routes_per_vehicle
        if max_routes is None:
            max_routes = int(model_data.metadata.get("max_routes_per_vehicle", n_routes))

        if n_routes == 0 or n_vehicles == 0:
            return RouteAllocationSolverResult(
                status="OPTIMAL",
                solve_time_seconds=time.time() - start_time,
                objective_value=0.0,
                vehicle_sequences={},
                routes_allocated=0,
                routes_total=n_routes,
                allocation_score=0.0,
            )

        with hx.HexalyOptimizer() as optimizer:
            m = optimizer.model

            dist_arr = m.array(model_data.distance_matrix.tolist())
            node_reward = m.array(model_data.route_prizes.tolist())
            energy_arr = m.array(model_data.energy_consumption.tolist())
            battery_start = m.array(model_data.battery_start_soc.tolist())
            battery_max = m.array(model_data.battery_max_soc.tolist())
            duration_arr = m.array(model_data.node_durations.tolist())

            vehicle_sequences = [m.list(n_routes) for _ in range(n_vehicles)]
            m.constraint(m.disjoint(vehicle_sequences))

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

            for seq in vehicle_sequences:
                delay = route_transition_delay(m, seq, dist_arr)
                service = sequence_service_time(m, seq, duration_arr)
                total_shift = delay + service
                m.constraint(delay < self.config.big_value_penalty)
                if max_routes < n_routes:
                    m.constraint(m.count(seq) <= max_routes)
                m.constraint(total_shift <= shift_max)

            for v_idx, seq in enumerate(vehicle_sequences):
                start_kwh = m.at(battery_start, v_idx)
                max_kwh = m.at(battery_max, v_idx)
                soc_after = m.array(
                    m.range(0, m.count(seq)),
                    m.lambda_function(
                        lambda n, prev: prev - m.at(energy_arr, v_idx, seq[n]),
                    ),
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

            vehicle_reward_terms = [
                m.sum(seq, m.lambda_function(lambda j: m.at(node_reward, j)))
                for seq in vehicle_sequences
            ]
            prize_term = (
                vehicle_reward_terms[0]
                if len(vehicle_reward_terms) == 1
                else m.sum(vehicle_reward_terms)
            )
            route_count_term = m.sum([m.count(seq) for seq in vehicle_sequences])
            w = float(self.config.route_count_weight)
            objective = w * route_count_term + prize_term
            m.maximize(objective)

            model_stats = {
                "nb_expressions": m.get_nb_expressions(),
                "nb_decisions": m.get_nb_decisions(),
                "nb_constraints": m.get_nb_constraints(),
                "n_vehicle_sequences": n_vehicles,
            }
            log_model_complexity(m, n_vehicles, n_routes)
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

            vehicle_sequences_out: Dict[int, List[int]] = {}
            total_routes = 0
            for v_idx, seq_var in enumerate(vehicle_sequences):
                collection = sol.get_value(seq_var)
                seq_vals = [int(x) for x in collection]
                if seq_vals:
                    vehicle_sequences_out[v_idx] = seq_vals
                    total_routes += len(seq_vals)

            result = RouteAllocationSolverResult(
                status=status,
                solve_time_seconds=time.time() - start_time,
                objective_value=obj_value,
                vehicle_sequences=vehicle_sequences_out,
                routes_allocated=total_routes,
                routes_total=n_routes,
                allocation_score=obj_value,
            )
            warnings = validate_allocation_solver_result(
                model_data, result, self.config.route_count_weight
            )
            write_optimizer_debug_csv(
                allocation_model_to_optimization_data(model_data),
                config=self.config,
                result=result,
                model_stats=model_stats,
                validation_warnings=warnings,
            )
            return result

    def _greedy_fallback(self, model_data: AllocationModelData) -> RouteAllocationSolverResult:
        start_time = time.time()
        n_vehicles = len(model_data.vehicles)
        n_routes = len(model_data.routes)
        assigned_routes: Set[int] = set()
        vehicle_sequences: Dict[int, List[int]] = {}

        route_order = sorted(
            range(n_routes),
            key=lambda r: model_data.routes[r].plan_start_date_time,
        )

        for r_idx in route_order:
            best_v = None
            best_score = -np.inf
            for v_idx in range(n_vehicles):
                if r_idx in model_data.forbidden_nodes.get(v_idx, set()):
                    continue
                if r_idx in assigned_routes:
                    continue
                score = float(self.config.route_count_weight) + float(
                    model_data.route_prizes[r_idx]
                )
                if score > best_score:
                    best_score = score
                    best_v = v_idx
            if best_v is not None and best_score > -1e5:
                vehicle_sequences.setdefault(best_v, []).append(r_idx)
                assigned_routes.add(r_idx)

        total_routes = sum(len(s) for s in vehicle_sequences.values())
        obj = sum(
            float(model_data.route_prizes[r])
            for seq in vehicle_sequences.values()
            for r in seq
        )
        result = RouteAllocationSolverResult(
            status="FEASIBLE",
            solve_time_seconds=time.time() - start_time,
            objective_value=obj,
            vehicle_sequences=vehicle_sequences,
            routes_allocated=total_routes,
            routes_total=n_routes,
            allocation_score=obj,
        )
        warnings = validate_allocation_solver_result(
            model_data, result, self.config.route_count_weight
        )
        write_optimizer_debug_csv(
            allocation_model_to_optimization_data(model_data),
            config=self.config,
            result=result,
            validation_warnings=warnings,
        )
        return result


# Backward-compatible aliases
Phase1Config = AllocationConfig
Phase1Optimizer = RouteAllocationOptimizer
Phase1Result = RouteAllocationSolverResult
