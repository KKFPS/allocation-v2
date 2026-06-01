"""Debug logging and post-solve validation for the Hexaly optimizer."""

from typing import Dict, List, Optional

import numpy as np

from src.optimizer.cost_matrix import AllocationModelData, OptimizationModelData
from src.utils.logging_config import logger


def log_allocation_model_inputs(
    model_data: AllocationModelData, route_count_weight: Optional[float] = None
) -> None:
    """Log route-only model inputs (wraps OptimizationModelData logger)."""
    opt = OptimizationModelData(
        vehicles=model_data.vehicles,
        routes=model_data.routes,
        route_ids=model_data.route_ids,
        distance_matrix=model_data.distance_matrix,
        route_prizes=model_data.route_prizes,
        forbidden_nodes=model_data.forbidden_nodes,
        mandatory_nodes=model_data.mandatory_nodes,
        battery_start_soc=model_data.battery_start_soc,
        battery_max_soc=model_data.battery_max_soc,
        energy_consumption=model_data.energy_consumption,
        metadata=dict(model_data.metadata),
        enable_charge_scheduling=False,
        n_nodes=len(model_data.routes),
        n_routes=len(model_data.routes),
        is_charge=np.zeros(len(model_data.routes), dtype=int),
        node_rewards=model_data.route_prizes.copy(),
    )
    log_model_inputs(opt, route_count_weight)


def validate_allocation_solver_result(
    model_data: AllocationModelData, result, route_count_weight: float
) -> List[str]:
    """Validate route-only solver result."""
    opt = OptimizationModelData(
        vehicles=model_data.vehicles,
        routes=model_data.routes,
        route_ids=model_data.route_ids,
        distance_matrix=model_data.distance_matrix,
        route_prizes=model_data.route_prizes,
        forbidden_nodes=model_data.forbidden_nodes,
        mandatory_nodes=model_data.mandatory_nodes,
        battery_start_soc=model_data.battery_start_soc,
        battery_max_soc=model_data.battery_max_soc,
        energy_consumption=model_data.energy_consumption,
        metadata=dict(model_data.metadata),
        n_nodes=len(model_data.routes),
        n_routes=len(model_data.routes),
        node_rewards=model_data.route_prizes.copy(),
    )

    class _Adapter:
        vehicle_route_sequences = result.vehicle_sequences
        routes_allocated = result.routes_allocated
        objective_value = result.objective_value
        charge_slots_assigned = {}

    return validate_optimization_result(opt, _Adapter(), route_count_weight)


def log_model_inputs(
    model_data: OptimizationModelData,
    route_count_weight: Optional[float] = None,
) -> None:
    """Log model inputs to explain empty or low-allocation solutions."""
    n_v = len(model_data.vehicles)
    n_r = model_data.n_routes
    meta = model_data.metadata
    prize_min = float(np.min(model_data.node_rewards[:n_r])) if n_r else 0.0
    prize_max = float(np.max(model_data.node_rewards[:n_r])) if n_r else 0.0
    prize_sum = float(np.sum(model_data.node_rewards[:n_r])) if n_r else 0.0
    score_min = meta.get("score_min_feasible")
    score_max = meta.get("score_max_feasible")

    logger.info(
        "Optimizer inputs: vehicles=%s routes=%s nodes=%s charge_scheduling=%s "
        "feasible_pairs=%s route_prizes[min,max,sum]=[%.2f,%.2f,%.2f] "
        "feasible_scores[min,max]=[%s,%s] route_count_weight=%s",
        n_v,
        n_r,
        model_data.n_nodes,
        model_data.enable_charge_scheduling,
        meta.get("feasible_assignments"),
        prize_min,
        prize_max,
        prize_sum,
        f"{score_min:.2f}" if score_min is not None else "n/a",
        f"{score_max:.2f}" if score_max is not None else "n/a",
        f"{route_count_weight:.0f}" if route_count_weight is not None else "n/a",
    )

    if model_data.enable_charge_scheduling:
        logger.info(
            "Charge scheduling: chargers=%s timesteps=%s p_fixed_kw=%.1f "
            "charge_prize_sum=%.4f",
            model_data.n_chargers,
            model_data.n_timesteps,
            model_data.p_fixed_kw,
            float(np.sum(model_data.node_rewards[model_data.n_routes :])),
        )

    if n_r and prize_max < 0:
        logger.warning(
            "All route prizes are negative (constraint penalties). "
            "Ensure route_count_weight (currently %s) is large enough.",
            route_count_weight,
        )
    elif (
        n_r
        and prize_max > 0
        and prize_max <= 0.02
        and meta.get("feasible_assignments", 0) > 0
    ):
        logger.info(
            "Route prizes use tie-break values — primary coverage signal is "
            "route_count_weight=%s.",
            f"{route_count_weight:.0f}" if route_count_weight is not None else "see config",
        )

    for r_idx, route in enumerate(model_data.routes):
        prize = float(model_data.route_prizes[r_idx]) if r_idx < len(model_data.route_prizes) else 0.0
        feasible_vehicles = [
            model_data.vehicles[v_idx].vehicle_id
            for v_idx in range(n_v)
            if r_idx not in model_data.forbidden_nodes.get(v_idx, set())
        ]
        logger.debug(
            "Route[%s] id=%s prize=%.2f feasible_vehicles=%s start=%s",
            r_idx,
            route.route_id,
            prize,
            feasible_vehicles,
            route.plan_start_date_time,
        )

    dist = model_data.distance_matrix
    big = meta.get("big_value", 1_000_000)
    off_diag_infeasible = 0
    if model_data.n_nodes:
        for r1 in range(model_data.n_nodes):
            for r2 in range(model_data.n_nodes):
                if r1 != r2 and dist[r1, r2] >= big:
                    off_diag_infeasible += 1
    logger.debug(
        "Distance matrix summary: turnaround_min=%s shift_max_min=%s "
        "off_diagonal_infeasible=%s",
        meta.get("turnaround_minutes"),
        meta.get("shift_max_minutes"),
        off_diag_infeasible,
    )


def validate_optimization_result(
    model_data: OptimizationModelData,
    result,
    route_count_weight: float,
) -> List[str]:
    """Return human-readable validation warnings (also logged)."""
    warnings: List[str] = []
    n_routes = model_data.n_routes
    feasible_pairs = int(model_data.metadata.get("feasible_assignments", 0))

    if n_routes == 0:
        warnings.append("No routes in planning window.")
        return warnings

    if result.routes_allocated == 0 and feasible_pairs > 0:
        max_prize = float(np.max(model_data.route_prizes)) if model_data.route_prizes.size else 0
        msg = (
            f"Solver allocated 0/{n_routes} routes despite {feasible_pairs} feasible "
            f"(vehicle,route) pairs. max_route_prize={max_prize:.2f}, "
            f"route_count_weight={route_count_weight:.0f}. "
        )
        if max_prize < 0:
            msg += "Likely cause: negative constraint scores."
        elif max_prize == 0:
            msg += (
                "Prizes are neutral; check SOC/shift/forbidden constraints."
            )
        warnings.append(msg)

    allocated_route_indices: set = set()
    for seq in result.vehicle_route_sequences.values():
        allocated_route_indices.update(seq)

    prize_sum_allocated = sum(
        float(model_data.route_prizes[r])
        for seq in result.vehicle_route_sequences.values()
        for r in seq
    )
    expected_route_bonus = route_count_weight * result.routes_allocated
    logger.info(
        "Validation: routes_allocated=%s/%s charge_slots=%s prize_sum=%.2f "
        "route_bonus≈%.2f objective=%.2f",
        result.routes_allocated,
        n_routes,
        result.charge_slots_assigned,
        prize_sum_allocated,
        expected_route_bonus,
        result.objective_value,
    )

    for w in warnings:
        logger.warning("Optimizer validation: %s", w)

    return warnings
