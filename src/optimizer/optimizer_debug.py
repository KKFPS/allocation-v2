"""Debug logging, CSV export, and post-solve validation for the Hexaly optimizer."""

import csv
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

import numpy as np

from src.config import IS_HEXALY_ACTIVE, UNIFIED_OPTIMIZER_DEBUG_CSV
from src.optimizer.cost_matrix import (
    AllocationModelData,
    OptimizationModelData,
    charge_node_index,
)
from src.utils.logging_config import logger


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime, np.datetime64)):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return ";".join(_csv_value(v) for v in value)
    if isinstance(value, float):
        if np.isnan(value):
            return ""
        return repr(value)
    return str(value)


def _write_section(
    writer: csv.writer,
    title: str,
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
) -> None:
    writer.writerow([title])
    writer.writerow(list(headers))
    for row in rows:
        writer.writerow([_csv_value(cell) for cell in row])


def _node_label(model_data: OptimizationModelData, node_idx: int) -> str:
    n_routes = model_data.n_routes
    if node_idx < n_routes:
        return model_data.route_ids[node_idx]
    if not model_data.enable_charge_scheduling:
        return str(node_idx)
    offset = node_idx - n_routes
    charger = offset // model_data.n_timesteps
    timestep = offset % model_data.n_timesteps
    return f"charge_c{charger}_t{timestep}"


def _node_type(model_data: OptimizationModelData, node_idx: int) -> str:
    if node_idx < model_data.n_routes:
        return "route"
    return "charge"


def _config_param_rows(config: Any) -> List[List[Any]]:
    rows: List[List[Any]] = [["is_hexaly_active", IS_HEXALY_ACTIVE]]
    if config is None:
        return rows
    if is_dataclass(config):
        data = asdict(config)
    elif isinstance(config, dict):
        data = config
    else:
        return rows
    for key in sorted(data):
        rows.append([key, data[key]])
    return rows


def _model_summary_rows(model_data: OptimizationModelData) -> List[List[Any]]:
    return [
        ["n_vehicles", len(model_data.vehicles)],
        ["n_routes", model_data.n_routes],
        ["n_nodes", model_data.n_nodes],
        ["n_chargers", model_data.n_chargers],
        ["n_timesteps", model_data.n_timesteps],
        ["enable_charge_scheduling", model_data.enable_charge_scheduling],
        ["enable_variable_charger_power", model_data.enable_variable_charger_power],
        ["p_fixed_kw", model_data.p_fixed_kw],
    ]


def _vehicle_rows(model_data: OptimizationModelData) -> List[List[Any]]:
    rows: List[List[Any]] = []
    for v_idx, vehicle in enumerate(model_data.vehicles):
        rows.append(
            [
                v_idx,
                vehicle.vehicle_id,
                vehicle.active,
                vehicle.VOR,
                vehicle.battery_capacity,
                vehicle.efficiency_kwh_mile,
                vehicle.estimated_soc,
                vehicle.available_energy_kwh,
                vehicle.charge_power_ac,
                vehicle.charge_power_dc,
                vehicle.current_charger_id,
                vehicle.available_time,
            ]
        )
    return rows


def _route_rows(model_data: OptimizationModelData) -> List[List[Any]]:
    rows: List[List[Any]] = []
    for r_idx, route in enumerate(model_data.routes):
        duration = (
            float(model_data.node_durations[r_idx])
            if r_idx < len(model_data.node_durations)
            else route.duration_minutes
        )
        rows.append(
            [
                r_idx,
                route.route_id,
                route.plan_start_date_time,
                route.plan_end_date_time,
                route.plan_mileage,
                duration,
                float(model_data.route_prizes[r_idx])
                if r_idx < len(model_data.route_prizes)
                else 0.0,
            ]
        )
    return rows


def _node_rows(model_data: OptimizationModelData) -> List[List[Any]]:
    rows: List[List[Any]] = []
    for node_idx in range(model_data.n_nodes):
        node_type = _node_type(model_data, node_idx)
        charger = ""
        timestep = ""
        if node_type == "charge" and model_data.enable_charge_scheduling:
            offset = node_idx - model_data.n_routes
            charger = offset // model_data.n_timesteps
            timestep = offset % model_data.n_timesteps
        reward = (
            float(model_data.node_rewards[node_idx])
            if node_idx < len(model_data.node_rewards)
            else 0.0
        )
        duration = (
            float(model_data.node_durations[node_idx])
            if node_idx < len(model_data.node_durations)
            else 0.0
        )
        is_charge = (
            int(model_data.is_charge[node_idx])
            if node_idx < len(model_data.is_charge)
            else 0
        )
        rows.append(
            [
                node_idx,
                node_type,
                _node_label(model_data, node_idx),
                charger,
                timestep,
                duration,
                reward,
                is_charge,
            ]
        )
    return rows


def _assignment_rows(
    model_data: OptimizationModelData,
    assignments: Dict[int, Set[int]],
) -> List[List[Any]]:
    rows: List[List[Any]] = []
    for v_idx, nodes in sorted(assignments.items()):
        vehicle_id = (
            model_data.vehicles[v_idx].vehicle_id
            if v_idx < len(model_data.vehicles)
            else v_idx
        )
        for node_idx in sorted(nodes):
            rows.append(
                [
                    v_idx,
                    vehicle_id,
                    node_idx,
                    _node_label(model_data, node_idx),
                ]
            )
    return rows


def _battery_rows(model_data: OptimizationModelData) -> List[List[Any]]:
    rows: List[List[Any]] = []
    for v_idx, vehicle in enumerate(model_data.vehicles):
        rows.append(
            [
                v_idx,
                vehicle.vehicle_id,
                float(model_data.battery_start_soc[v_idx]),
                float(model_data.battery_max_soc[v_idx]),
            ]
        )
    return rows


def _energy_rows(model_data: OptimizationModelData) -> List[List[Any]]:
    rows: List[List[Any]] = []
    n_vehicles, n_nodes = model_data.energy_consumption.shape
    for v_idx in range(n_vehicles):
        vehicle_id = model_data.vehicles[v_idx].vehicle_id
        for node_idx in range(n_nodes):
            kwh = float(model_data.energy_consumption[v_idx, node_idx])
            if kwh == 0.0 and _node_type(model_data, node_idx) == "charge":
                continue
            rows.append(
                [
                    v_idx,
                    vehicle_id,
                    node_idx,
                    _node_label(model_data, node_idx),
                    kwh,
                ]
            )
    return rows


def _distance_matrix_rows(model_data: OptimizationModelData) -> List[List[Any]]:
    rows: List[List[Any]] = []
    dist = model_data.distance_matrix
    n_nodes = model_data.n_nodes
    for from_idx in range(n_nodes):
        for to_idx in range(n_nodes):
            rows.append(
                [
                    from_idx,
                    _node_label(model_data, from_idx),
                    to_idx,
                    _node_label(model_data, to_idx),
                    float(dist[from_idx, to_idx]),
                ]
            )
    return rows


def _charger_rows(model_data: OptimizationModelData) -> List[List[Any]]:
    rows: List[List[Any]] = []
    for c_idx in range(model_data.n_chargers):
        charger_id = (
            model_data.charger_ids[c_idx]
            if c_idx < len(model_data.charger_ids)
            else c_idx + 1
        )
        max_kw = (
            model_data.charger_max_power_kw[c_idx]
            if c_idx < len(model_data.charger_max_power_kw)
            else model_data.p_fixed_kw
        )
        rows.append([c_idx, charger_id, max_kw])
    return rows


def _slot_rows(
    values: Sequence[float], value_name: str
) -> List[List[Any]]:
    return [[t, values[t]] for t in range(len(values))]


def _result_rows(result: Any) -> List[List[Any]]:
    if result is None:
        return []
    sequences = getattr(result, "vehicle_route_sequences", None)
    if sequences is None:
        sequences = getattr(result, "vehicle_sequences", {})
    charge_slots = getattr(result, "charge_slots_assigned", {}) or {}
    return [
        ["status", getattr(result, "status", "")],
        ["solve_time_seconds", getattr(result, "solve_time_seconds", "")],
        ["objective_value", getattr(result, "objective_value", "")],
        ["routes_allocated", getattr(result, "routes_allocated", "")],
        ["routes_total", getattr(result, "routes_total", "")],
        ["allocation_score", getattr(result, "allocation_score", "")],
        ["vehicles_with_routes", len(sequences)],
        ["vehicles_with_charge_slots", len(charge_slots)],
    ]


def _sequence_result_rows(
    model_data: OptimizationModelData, result: Any
) -> List[List[Any]]:
    if result is None:
        return []
    sequences = getattr(result, "vehicle_route_sequences", None)
    if sequences is None:
        sequences = getattr(result, "vehicle_sequences", {})
    rows: List[List[Any]] = []
    for v_idx, node_indices in sorted(sequences.items()):
        vehicle_id = (
            model_data.vehicles[v_idx].vehicle_id
            if v_idx < len(model_data.vehicles)
            else v_idx
        )
        route_ids = [
            model_data.route_ids[n] for n in node_indices if n < model_data.n_routes
        ]
        rows.append(
            [
                v_idx,
                vehicle_id,
                ";".join(str(n) for n in node_indices),
                ";".join(route_ids),
                len(route_ids),
            ]
        )
    return rows


def _charge_slot_result_rows(
    model_data: OptimizationModelData, result: Any
) -> List[List[Any]]:
    if result is None:
        return []
    charge_slots = getattr(result, "charge_slots_assigned", {}) or {}
    rows: List[List[Any]] = []
    for v_idx, slots in sorted(charge_slots.items()):
        vehicle_id = (
            model_data.vehicles[v_idx].vehicle_id
            if v_idx < len(model_data.vehicles)
            else v_idx
        )
        for charger, timestep, power_kw in slots:
            node_idx = charge_node_index(
                model_data.n_routes, model_data.n_timesteps, charger, timestep
            )
            rows.append(
                [
                    v_idx,
                    vehicle_id,
                    charger,
                    timestep,
                    node_idx,
                    power_kw,
                ]
            )
    return rows


def _route_allocation_matrix_rows(
    model_data: OptimizationModelData, result: Any
) -> List[List[Any]]:
    if result is None:
        return []
    sequences = getattr(result, "vehicle_route_sequences", None)
    if sequences is None:
        sequences = getattr(result, "vehicle_sequences", {})
    allocated: Dict[int, Set[int]] = {r_idx: set() for r_idx in range(model_data.n_routes)}
    for v_idx, node_indices in sequences.items():
        for node_idx in node_indices:
            if node_idx < model_data.n_routes:
                allocated[node_idx].add(v_idx)
    rows: List[List[Any]] = []
    for r_idx, route_id in enumerate(model_data.route_ids):
        vehicle_ids = [
            model_data.vehicles[v].vehicle_id
            for v in sorted(allocated[r_idx])
            if v < len(model_data.vehicles)
        ]
        rows.append(
            [
                r_idx,
                route_id,
                ";".join(str(v) for v in sorted(allocated[r_idx])),
                ";".join(str(vid) for vid in vehicle_ids),
                1 if allocated[r_idx] else 0,
            ]
        )
    return rows


def write_optimizer_debug_csv(
    model_data: OptimizationModelData,
    config: Any = None,
    result: Any = None,
    model_stats: Optional[Dict[str, Any]] = None,
    validation_warnings: Optional[List[str]] = None,
    output_path: Optional[str] = None,
) -> Optional[str]:
    """
    Write all optimizer params and datapoints to a sectioned CSV file.

    Returns the path written, or None when export is disabled.
    """
    path = output_path if output_path is not None else UNIFIED_OPTIMIZER_DEBUG_CSV
    if not path:
        return None

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)

    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)

        _write_section(writer, "[CONFIG_PARAMS]", ["param", "value"], _config_param_rows(config))
        _write_section(
            writer, "[MODEL_SUMMARY]", ["metric", "value"], _model_summary_rows(model_data)
        )
        _write_section(
            writer,
            "[MODEL_METADATA]",
            ["key", "value"],
            [[k, v] for k, v in sorted(model_data.metadata.items())],
        )
        _write_section(
            writer,
            "[VEHICLES]",
            [
                "v_idx",
                "vehicle_id",
                "active",
                "vor",
                "battery_capacity_kwh",
                "efficiency_kwh_mile",
                "estimated_soc_pct",
                "available_energy_kwh",
                "charge_power_ac_kw",
                "charge_power_dc_kw",
                "current_charger_id",
                "available_time",
            ],
            _vehicle_rows(model_data),
        )
        _write_section(
            writer,
            "[ROUTES]",
            [
                "r_idx",
                "route_id",
                "plan_start",
                "plan_end",
                "plan_mileage",
                "duration_min",
                "prize",
            ],
            _route_rows(model_data),
        )
        _write_section(
            writer,
            "[NODES]",
            [
                "node_idx",
                "type",
                "label",
                "charger_idx",
                "timestep",
                "duration_min",
                "reward",
                "is_charge",
            ],
            _node_rows(model_data),
        )
        _write_section(
            writer,
            "[BATTERY_PER_VEHICLE]",
            ["v_idx", "vehicle_id", "start_soc_kwh", "max_soc_kwh"],
            _battery_rows(model_data),
        )
        _write_section(
            writer,
            "[FORBIDDEN_NODES]",
            ["v_idx", "vehicle_id", "node_idx", "node_label"],
            _assignment_rows(model_data, model_data.forbidden_nodes),
        )
        _write_section(
            writer,
            "[MANDATORY_NODES]",
            ["v_idx", "vehicle_id", "node_idx", "node_label"],
            _assignment_rows(model_data, model_data.mandatory_nodes),
        )
        _write_section(
            writer,
            "[INCOMPATIBLE_ROUTE_PAIRS]",
            ["route_a_idx", "route_b_idx", "route_a_id", "route_b_id"],
            [
                [
                    a,
                    b,
                    model_data.route_ids[a] if a < len(model_data.route_ids) else a,
                    model_data.route_ids[b] if b < len(model_data.route_ids) else b,
                ]
                for a, b in model_data.incompatible_route_pairs
            ],
        )
        _write_section(
            writer,
            "[ENERGY_CONSUMPTION_KWH]",
            ["v_idx", "vehicle_id", "node_idx", "node_label", "kwh"],
            _energy_rows(model_data),
        )
        _write_section(
            writer,
            "[NODE_DURATIONS_MIN]",
            ["node_idx", "node_label", "duration_min"],
            [
                [
                    node_idx,
                    _node_label(model_data, node_idx),
                    float(model_data.node_durations[node_idx]),
                ]
                for node_idx in range(len(model_data.node_durations))
            ],
        )
        _write_section(
            writer,
            "[DISTANCE_MATRIX]",
            ["from_idx", "from_label", "to_idx", "to_label", "cost_min"],
            _distance_matrix_rows(model_data),
        )
        _write_section(
            writer,
            "[CHARGERS]",
            ["charger_idx", "charger_id", "max_power_kw"],
            _charger_rows(model_data),
        )
        _write_section(
            writer,
            "[SITE_CAPACITY_KW]",
            ["timestep", "capacity_kw"],
            _slot_rows(model_data.capacity_power_kw, "capacity_kw"),
        )
        _write_section(
            writer,
            "[ELECTRICITY_PRICE_PER_SLOT]",
            ["timestep", "price"],
            _slot_rows(model_data.electricity_price_per_slot, "price"),
        )
        if model_stats:
            _write_section(
                writer,
                "[MODEL_STATS]",
                ["stat", "value"],
                [[k, v] for k, v in sorted(model_stats.items())],
            )
        _write_section(
            writer, "[SOLVE_RESULT]", ["metric", "value"], _result_rows(result)
        )
        _write_section(
            writer,
            "[VEHICLE_SEQUENCES]",
            ["v_idx", "vehicle_id", "node_indices", "route_ids", "route_count"],
            _sequence_result_rows(model_data, result),
        )
        _write_section(
            writer,
            "[CHARGE_SLOTS_ASSIGNED]",
            ["v_idx", "vehicle_id", "charger_idx", "timestep", "node_idx", "power_kw"],
            _charge_slot_result_rows(model_data, result),
        )
        _write_section(
            writer,
            "[ROUTE_ALLOCATIONS]",
            ["r_idx", "route_id", "vehicle_indices", "vehicle_ids", "allocated"],
            _route_allocation_matrix_rows(model_data, result),
        )
        if validation_warnings:
            _write_section(
                writer,
                "[VALIDATION_WARNINGS]",
                ["warning"],
                [[w] for w in validation_warnings],
            )

    logger.info("Optimizer debug CSV written to %s", out.resolve())
    return str(out.resolve())


def allocation_model_to_optimization_data(
    model_data: AllocationModelData,
) -> OptimizationModelData:
    n_routes = len(model_data.routes)
    return OptimizationModelData(
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
        incompatible_route_pairs=list(model_data.incompatible_route_pairs),
        enable_charge_scheduling=False,
        n_nodes=n_routes,
        n_routes=n_routes,
        n_chargers=0,
        n_timesteps=0,
        is_charge=np.zeros(n_routes, dtype=int),
        node_durations=model_data.node_durations.copy(),
        node_rewards=model_data.route_prizes.copy(),
    )


def log_allocation_model_inputs(
    model_data: AllocationModelData, route_count_weight: Optional[float] = None
) -> None:
    """Log route-only model inputs (wraps OptimizationModelData logger)."""
    log_model_inputs(
        allocation_model_to_optimization_data(model_data), route_count_weight
    )


def validate_allocation_solver_result(
    model_data: AllocationModelData, result, route_count_weight: float
) -> List[str]:
    """Validate route-only solver result."""
    opt = allocation_model_to_optimization_data(model_data)

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

    sequences = getattr(result, "vehicle_route_sequences", None) or {}
    allocated_route_indices: set = set()
    for seq in sequences.values():
        allocated_route_indices.update(seq)

    prize_sum_allocated = sum(
        float(model_data.route_prizes[r]) for seq in sequences.values() for r in seq
    )
    expected_route_bonus = route_count_weight * result.routes_allocated
    logger.info(
        "Validation: routes_allocated=%s/%s charge_slots=%s prize_sum=%.2f "
        "route_bonus≈%.2f objective=%.2f",
        result.routes_allocated,
        n_routes,
        getattr(result, "charge_slots_assigned", {}),
        prize_sum_allocated,
        expected_route_bonus,
        result.objective_value,
    )

    for w in warnings:
        logger.warning("Optimizer validation: %s", w)

    return warnings
