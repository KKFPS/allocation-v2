"""Debug logging and post-solve validation for Phase 1 allocation."""

from typing import Dict, List, Optional

import numpy as np

from src.optimizer.cost_matrix import Phase1ModelData
from src.utils.logging_config import logger


def log_phase1_model_inputs(
    model_data: Phase1ModelData,
    route_count_weight: Optional[float] = None,
) -> None:
    """Log model inputs to explain empty or low-allocation solutions."""
    n_v = len(model_data.vehicles)
    n_r = len(model_data.routes)
    prizes = model_data.route_prizes
    meta = model_data.metadata
    prize_min = float(np.min(prizes)) if n_r else 0.0
    prize_max = float(np.max(prizes)) if n_r else 0.0
    prize_sum = float(np.sum(prizes)) if n_r else 0.0
    score_min = meta.get("score_min_feasible")
    score_max = meta.get("score_max_feasible")

    logger.info(
        "Phase1 inputs: vehicles=%s routes=%s feasible_pairs=%s "
        "prizes[min,max,sum]=[%.2f,%.2f,%.2f] feasible_scores[min,max]=[%s,%s] "
        "route_count_weight=%s",
        n_v,
        n_r,
        meta.get("feasible_assignments"),
        prize_min,
        prize_max,
        prize_sum,
        f"{score_min:.2f}" if score_min is not None else "n/a",
        f"{score_max:.2f}" if score_max is not None else "n/a",
        f"{route_count_weight:.0f}" if route_count_weight is not None else "n/a",
    )

    if n_r and prize_max < 0:
        logger.warning(
            "All route prizes are negative (constraint penalties). "
            "Ensure route_count_weight (currently %s) is large enough to prefer assignments.",
            route_count_weight,
        )
    elif (
        n_r
        and prize_max > 0
        and prize_max <= 0.02
        and meta.get("feasible_assignments", 0) > 0
    ):
        logger.info(
            "Route prizes use tie-break values (~0.01) — primary objective signal is "
            "route_count_weight=%s.",
            f"{route_count_weight:.0f}" if route_count_weight is not None else "see config",
        )

    for r_idx, route in enumerate(model_data.routes):
        prize = float(prizes[r_idx]) if r_idx < len(prizes) else 0.0
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

    for v_idx, vehicle in enumerate(model_data.vehicles):
        forbidden = model_data.forbidden_nodes.get(v_idx, set())
        start_soc = float(model_data.battery_start_soc[v_idx])
        max_soc = float(model_data.battery_max_soc[v_idx])
        min_energy_on_seq = (
            float(np.min(model_data.energy_consumption[v_idx, :]))
            if n_r
            else 0.0
        )
        logger.debug(
            "Vehicle[%s] id=%s start_soc_kwh=%.1f max_soc_kwh=%.1f "
            "forbidden_routes=%s/%s min_route_energy_kwh=%.1f",
            v_idx,
            vehicle.vehicle_id,
            start_soc,
            max_soc,
            len(forbidden),
            n_r,
            min_energy_on_seq,
        )

    dist = model_data.distance_matrix
    big = meta.get("big_value", 1_000_000)
    n_r = len(model_data.routes)
    off_diag_infeasible = 0
    if n_r:
        for r1 in range(n_r):
            for r2 in range(n_r):
                if r1 != r2 and dist[r1, r2] >= big:
                    off_diag_infeasible += 1
    logger.debug(
        "Distance matrix summary: turnaround_min=%s shift_max_min=%s "
        "off_diagonal_infeasible=%s (see Infeasible arc DEBUG lines from builder)",
        meta.get("turnaround_minutes"),
        meta.get("shift_max_minutes"),
        off_diag_infeasible,
    )


def validate_phase1_result(
    model_data: Phase1ModelData,
    result,
    route_count_weight: float,
) -> List[str]:
    """
    Return human-readable validation warnings (also logged).

    Checks:
    - Zero allocation despite feasible pairs
    - Objective vs prize sum consistency
    - Per-route coverage
    """
    warnings: List[str] = []
    n_routes = len(model_data.routes)
    feasible_pairs = int(model_data.metadata.get("feasible_assignments", 0))

    if n_routes == 0:
        warnings.append("No routes in planning window.")
        return warnings

    if result.routes_allocated == 0 and feasible_pairs > 0:
        max_prize = float(np.max(model_data.route_prizes))
        msg = (
            f"Solver allocated 0/{n_routes} routes despite {feasible_pairs} feasible "
            f"(vehicle,route) pairs. max_route_prize={max_prize:.2f}, "
            f"route_count_weight={route_count_weight:.0f}. "
        )
        if max_prize < 0:
            msg += (
                "Likely cause: negative constraint scores — increase route_count_weight "
                "or shift prizes positive for maximization."
            )
        elif max_prize == 0:
            msg += (
                "Prizes are neutral (0); check SOC/shift/forbidden constraints if routes "
                f"still unassigned (route_count_weight={route_count_weight:.0f})."
            )
        warnings.append(msg)

    allocated_indices: set = set()
    for seq in result.vehicle_sequences.values():
        allocated_indices.update(seq)

    unallocated = [i for i in range(n_routes) if i not in allocated_indices]
    if unallocated:
        for r_idx in unallocated[:10]:
            route = model_data.routes[r_idx]
            prize = float(model_data.route_prizes[r_idx])
            any_feasible = any(
                r_idx not in model_data.forbidden_nodes.get(v, set())
                for v in range(len(model_data.vehicles))
            )
            logger.debug(
                "Unallocated route[%s] id=%s prize=%.2f had_feasible_vehicle=%s",
                r_idx,
                route.route_id,
                prize,
                any_feasible,
            )
        if len(unallocated) > 10:
            logger.debug("... and %s more unallocated routes", len(unallocated) - 10)

    prize_sum_allocated = sum(
        float(model_data.route_prizes[r]) for seq in result.vehicle_sequences.values() for r in seq
    )
    expected_route_bonus = route_count_weight * result.routes_allocated
    logger.info(
        "Phase1 validation: allocated=%s/%s prize_sum=%.2f route_bonus≈%.2f objective=%.2f",
        result.routes_allocated,
        n_routes,
        prize_sum_allocated,
        expected_route_bonus,
        result.objective_value,
    )

    for w in warnings:
        logger.warning("Phase1 validation: %s", w)

    return warnings
