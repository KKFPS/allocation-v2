"""Phase 1 Hexaly optimizer: route allocation only (list-based VRP)."""

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

import hexaly.optimizer as hx
import numpy as np

from src.config import (
    IS_HEXALY_ACTIVE,
    UNIFIED_ALLOCATION_TIME_LIMIT,
    UNIFIED_ROUTE_COUNT_WEIGHT,
)
from src.optimizer.phase1_debug import log_phase1_model_inputs, validate_phase1_result
from src.models.allocation import AllocationResult, RouteAllocation
from src.models.route import Route
from src.models.vehicle import Vehicle
from src.optimizer.cost_matrix import BIG_VALUE, Phase1ModelData
from src.utils.logging_config import logger

BIG_VALUE_PENALTY = BIG_VALUE


def _route_transition_delay(m, seq, dist_arr):
    """
    Sum transition costs for consecutive pairs only (positions 1..count-1).

    Uses range(1, count(seq)) so lambdas never evaluate seq[i-1] at i=0
    (Hexaly evaluates both iif branches; list[-1] at i=0 causes NaN risk).
    """
    return m.sum(
        m.range(1, m.count(seq)),
        m.lambda_function(lambda i: m.at(dist_arr, seq[i - 1], seq[i])),
    )


def _log_model_complexity(m, n_vehicles: int, n_routes: int) -> None:
    """Log Hexaly model size after build (before close/solve)."""
    logger.info(
        "Phase1 model built: vehicles=%s routes=%s expressions=%s decisions=%s constraints=%s",
        n_vehicles,
        n_routes,
        m.get_nb_expressions(),
        m.get_nb_decisions(),
        m.get_nb_constraints(),
    )


@dataclass
class Phase1Config:
    """Configuration for Phase 1 route allocation."""

    time_limit_seconds: int = UNIFIED_ALLOCATION_TIME_LIMIT
    big_value_penalty: float = BIG_VALUE_PENALTY
    max_routes_per_vehicle: Optional[int] = None
    route_count_weight: float = UNIFIED_ROUTE_COUNT_WEIGHT
    verbosity: int = 1


@dataclass
class Phase1Result:
    """Result from Phase 1 optimization."""

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
            for pos, r_idx in enumerate(node_indices):
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


class Phase1Optimizer:
    """
    Phase 1 Hexaly model: disjoint vehicle sequences over route nodes.

    Implements docs/hexaly_model_structure_and_usage.md Phase 1:
    - disjoint(vehicleSequence)
    - forbidden/mandatory contains constraints
    - routeDelay via distanceMatrix
    - SOC decrease-only with outOfBattery == 0
    - maximize sum of nodeReward on assigned routes
    """

    def __init__(self, config: Optional[Phase1Config] = None):
        self.config = config or Phase1Config()

    def solve(self, model_data: Phase1ModelData) -> Phase1Result:
        """Solve Phase 1 allocation."""
        logger.info(
            "Phase1Optimizer.solve: vehicles=%s routes=%s",
            model_data.metadata.get("vehicles"),
            model_data.metadata.get("routes"),
        )
        logger.debug("Phase1 model metadata: %s", model_data.metadata)
        log_phase1_model_inputs(model_data, self.config.route_count_weight)

        if not IS_HEXALY_ACTIVE:
            logger.warning("Hexaly not active — using greedy fallback")
            return self._greedy_fallback(model_data)

        return self._solve_hexaly(model_data)

    def _solve_hexaly(self, model_data: Phase1ModelData) -> Phase1Result:
        start_time = time.time()
        n_vehicles = len(model_data.vehicles)
        n_routes = len(model_data.routes)
        shift_max = float(model_data.metadata.get("shift_max_minutes", 16 * 60))
        max_routes = self.config.max_routes_per_vehicle
        if max_routes is None:
            max_routes = int(model_data.metadata.get("max_routes_per_vehicle", n_routes))

        logger.debug(
            "Building Hexaly model: n_vehicles=%s n_routes=%s shift_max=%.1f max_routes=%s",
            n_vehicles,
            n_routes,
            shift_max,
            max_routes,
        )

        if n_routes == 0 or n_vehicles == 0:
            logger.info("Skipping Hexaly solve: no routes or no vehicles")
            return Phase1Result(
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

            vehicle_sequences = [m.list(n_routes) for _ in range(n_vehicles)]
            m.constraint(m.disjoint(vehicle_sequences))
            logger.debug("Added disjoint(vehicleSequence) for %s vehicles", n_vehicles)

            forbidden_count = 0
            for v_idx, forbidden in model_data.forbidden_nodes.items():
                if v_idx >= n_vehicles:
                    continue
                for node in forbidden:
                    m.constraint(m.not_(m.contains(vehicle_sequences[v_idx], node)))
                    forbidden_count += 1

            mandatory_count = 0
            for v_idx, mandatory in model_data.mandatory_nodes.items():
                if v_idx >= n_vehicles:
                    continue
                for node in mandatory:
                    m.constraint(m.contains(vehicle_sequences[v_idx], node))
                    mandatory_count += 1

            logger.debug(
                "Forbidden constraints=%s mandatory constraints=%s",
                forbidden_count,
                mandatory_count,
            )

            route_delay_exprs = []
            for v_idx, seq in enumerate(vehicle_sequences):
                delay = _route_transition_delay(m, seq, dist_arr)
                route_delay_exprs.append(delay)
                m.constraint(delay < self.config.big_value_penalty)
                if max_routes < n_routes:
                    m.constraint(m.count(seq) <= max_routes)
                m.constraint(delay <= shift_max)

            logger.debug("Added routeDelay and shift constraints for %s vehicles", n_vehicles)

            for v_idx, seq in enumerate(vehicle_sequences):
                start_kwh = m.at(battery_start, v_idx)
                max_kwh = m.at(battery_max, v_idx)

                # Recursive array: soc_after[n] = soc_before[n] - energy[seq[n]];
                # soc_before[0] = battery start (third operand).
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

            logger.debug("Added SOC decrease-only constraints for %s vehicles", n_vehicles)

            # Sum per-vehicle rewards at build time (cannot index Python list with HxExpression in lambda)
            vehicle_reward_terms = [
                m.sum(
                    seq,
                    m.lambda_function(lambda j: m.at(node_reward, j)),
                )
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
            logger.debug(
                "Objective: maximize %.0f * route_count + sum(nodeReward)",
                w,
            )

            _log_model_complexity(m, n_vehicles, n_routes)
            m.close()
            optimizer.param.time_limit = self.config.time_limit_seconds
            logger.info(
                "Phase1 solve starting: time_limit=%ss",
                self.config.time_limit_seconds,
            )
            optimizer.param.verbosity = self.config.verbosity
            optimizer.solve()

            sol = optimizer.solution
            status = (
                sol.status.name
                if hasattr(sol.status, "name")
                else str(sol.status).split(".")[-1]
            )
            solve_time = time.time() - start_time
            obj_value = float(sol.get_value(objective)) if sol else 0.0
            route_count_solved = sum(
                len(list(sol.get_value(seq))) for seq in vehicle_sequences
            )
            logger.debug(
                "Solved route_count=%s objective=%.2f (weight=%.0f on routes)",
                route_count_solved,
                obj_value,
                w,
            )

            vehicle_sequences_out: Dict[int, List[int]] = {}
            total_routes = 0
            for v_idx, seq_var in enumerate(vehicle_sequences):
                collection = sol.get_value(seq_var)
                seq_vals = [int(x) for x in collection]
                if seq_vals:
                    vehicle_sequences_out[v_idx] = [int(x) for x in seq_vals]
                    total_routes += len(seq_vals)
                    vehicle_id = model_data.vehicles[v_idx].vehicle_id
                    logger.debug(
                        "Vehicle idx=%s id=%s sequence=%s reward=%.2f",
                        v_idx,
                        vehicle_id,
                        vehicle_sequences_out[v_idx],
                        sum(
                            float(model_data.route_prizes[r])
                            for r in vehicle_sequences_out[v_idx]
                        ),
                    )

            logger.info(
                "Phase1 solve complete: status=%s objective=%.2f routes=%s/%s time=%.2fs",
                status,
                obj_value,
                total_routes,
                n_routes,
                solve_time,
            )

            result = Phase1Result(
                status=status,
                solve_time_seconds=solve_time,
                objective_value=obj_value,
                vehicle_sequences=vehicle_sequences_out,
                routes_allocated=total_routes,
                routes_total=n_routes,
                allocation_score=obj_value,
            )
            validate_phase1_result(model_data, result, self.config.route_count_weight)
            return result

    def _greedy_fallback(self, model_data: Phase1ModelData) -> Phase1Result:
        """Greedy assignment when Hexaly is unavailable."""
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
                logger.debug(
                    "Greedy: route idx=%s -> vehicle idx=%s score=%.2f",
                    r_idx,
                    best_v,
                    best_score,
                )

        total_routes = sum(len(s) for s in vehicle_sequences.values())
        obj = sum(
            float(model_data.route_prizes[r])
            for seq in vehicle_sequences.values()
            for r in seq
        )
        solve_time = time.time() - start_time
        logger.info(
            "Greedy fallback: routes=%s/%s objective=%.2f time=%.2fs",
            total_routes,
            n_routes,
            obj,
            solve_time,
        )

        result = Phase1Result(
            status="FEASIBLE",
            solve_time_seconds=solve_time,
            objective_value=obj,
            vehicle_sequences=vehicle_sequences,
            routes_allocated=total_routes,
            routes_total=n_routes,
            allocation_score=obj,
        )
        validate_phase1_result(model_data, result, self.config.route_count_weight)
        return result
