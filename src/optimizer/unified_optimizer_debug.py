"""Debug export for unified optimizer: dump matrices and constraint params to CSV.

Set DEBUG_EXPORT_UNIFIED_MATRICES_CSV = True to write a single CSV with all
model inputs and decision-variable placeholders (values left empty) on each solve.
"""
import csv
import os
from datetime import datetime
from typing import List, Dict, Tuple, Optional, Any

import numpy as np

from src.models.scheduler import (
    VehicleChargeState,
    RouteEnergyRequirement,
    VehicleAvailability,
)
from src.models.vehicle import Vehicle
from src.utils.logging_config import logger

# Set to True to export all matrices and constraint params to a single CSV (decision vars left empty).
DEBUG_EXPORT_UNIFIED_MATRICES_CSV = True


def export_unified_debug_matrices_csv(
    config: Any,
    *,
    sequences: Optional[List[Tuple]] = None,
    route_ids: Optional[List[str]] = None,
    sequence_costs: Optional[np.ndarray] = None,
    vehicles: Optional[List[Vehicle]] = None,
    vehicle_states: Optional[Dict[int, VehicleChargeState]] = None,
    energy_requirements: Optional[Dict[int, List[RouteEnergyRequirement]]] = None,
    availability_matrices: Optional[Dict[int, VehicleAvailability]] = None,
    time_slots: Optional[List[datetime]] = None,
    forecast_data: Optional[Dict[datetime, float]] = None,
    price_data: Optional[Dict[datetime, Tuple[float, bool]]] = None,
    filepath: Optional[str] = None,
) -> None:
    """
    Write a single CSV with all matrices and constraint params for debugging.
    Decision variables (sequence_selected, route_covered, charge_power,
    cumulative_energy, shortfall) are left empty.
    Only called when DEBUG_EXPORT_UNIFIED_MATRICES_CSV is True.
    """
    if filepath is None:
        filepath = os.path.join(os.getcwd(), "unified_optimizer_debug.csv")

    n_sequences = len(sequences) if sequences else 0
    n_routes = len(route_ids) if route_ids else 0
    n_slots = len(time_slots) if time_slots else 0
    n_vehicles = len(vehicles) if vehicles else 0

    # Allocation structures (when allocation data present)
    route_coverage: Dict[str, List[int]] = {}
    vehicle_to_sequences: Dict[int, List[int]] = {}
    if route_ids is not None:
        route_coverage = {rid: [] for rid in route_ids}
    if sequences is not None and route_ids is not None:
        for seq_idx, (vehicle_id, route_sequence, _) in enumerate(sequences):
            vehicle_to_sequences.setdefault(vehicle_id, []).append(seq_idx)
            for route in route_sequence:
                if route.route_id in route_coverage:
                    route_coverage[route.route_id].append(seq_idx)

    # Model stats: decision variable counts
    target_soc_percent = getattr(config, "target_soc_percent", 75.0)
    n_alloc_sequence_vars = n_sequences
    n_alloc_route_covered_vars = n_routes
    n_sched_charge_power_vars = n_slots * n_vehicles
    n_sched_cumulative_energy_vars = n_slots * n_vehicles
    n_sched_shortfall_vars = 0
    if vehicles and vehicle_states:
        for v in vehicles:
            state = vehicle_states.get(v.vehicle_id)
            if state:
                target_soc_kwh = (target_soc_percent / 100.0) * state.battery_capacity_kwh
                if target_soc_kwh - state.current_soc_kwh > 0:
                    n_sched_shortfall_vars += 1
    total_decision_vars = (
        n_alloc_sequence_vars + n_alloc_route_covered_vars
        + n_sched_charge_power_vars + n_sched_cumulative_energy_vars + n_sched_shortfall_vars
    )

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)

        # --- CONFIG / CONSTRAINT PARAMS ---
        w.writerow(["[CONFIG_PARAMS]"])
        w.writerow(["param", "value"])
        w.writerow(["mode", getattr(config.mode, "value", config.mode)])
        w.writerow(["allocation_time_limit", getattr(config, "allocation_time_limit", "")])
        w.writerow(["scheduling_time_limit", getattr(config, "scheduling_time_limit", "")])
        w.writerow(["integrated_time_limit", getattr(config, "integrated_time_limit", "")])
        w.writerow(["route_count_weight", getattr(config, "route_count_weight", "")])
        w.writerow(["allocation_score_weight", getattr(config, "allocation_score_weight", "")])
        w.writerow(["scheduling_cost_weight", getattr(config, "scheduling_cost_weight", "")])
        w.writerow(["target_soc_shortfall_penalty", getattr(config, "target_soc_shortfall_penalty", "")])
        w.writerow(["target_soc_percent", target_soc_percent])
        w.writerow(["site_capacity_kw", getattr(config, "site_capacity_kw", "")])
        w.writerow(["synthetic_time_price_factor", getattr(config, "synthetic_time_price_factor", "")])
        w.writerow(["n_sequences", n_sequences])
        w.writerow(["n_routes", n_routes])
        w.writerow(["n_slots", n_slots])
        w.writerow(["n_vehicles", n_vehicles])
        w.writerow([])

        # --- MODEL STATS (decision variables, etc.) ---
        w.writerow(["[MODEL_STATS]"])
        w.writerow(["stat", "value"])
        w.writerow(["allocation_sequence_vars", n_alloc_sequence_vars])
        w.writerow(["allocation_route_covered_vars", n_alloc_route_covered_vars])
        w.writerow(["scheduling_charge_power_vars", n_sched_charge_power_vars])
        w.writerow(["scheduling_cumulative_energy_vars", n_sched_cumulative_energy_vars])
        w.writerow(["scheduling_shortfall_vars", n_sched_shortfall_vars])
        w.writerow(["total_decision_variables", total_decision_vars])
        w.writerow([])

        # --- ALLOCATION: SEQUENCE COSTS ---
        if n_sequences > 0 and sequence_costs is not None:
            w.writerow(["[SEQUENCE_COSTS]"])
            w.writerow(["seq_idx"] + [f"seq_{i}" for i in range(n_sequences)])
            w.writerow(["cost"] + [float(sequence_costs[i]) for i in range(n_sequences)])
            w.writerow([])

        # --- ALLOCATION: SEQUENCE DETAILS (cost, selected = empty) ---
        if sequences is not None and route_ids is not None:
            w.writerow(["[SEQUENCE_DETAILS]"])
            w.writerow(["seq_idx", "vehicle_id", "route_ids", "cost", "selected"])
            for i in range(len(sequences)):
                vehicle_id, route_sequence, cost = sequences[i]
                route_ids_str = ";".join(r.route_id for r in route_sequence)
                w.writerow([i, vehicle_id, route_ids_str, float(cost), ""])
            w.writerow([])

        # --- ALLOCATION: ROUTE COVERAGE MATRIX ---
        if route_ids and n_sequences > 0:
            w.writerow(["[ROUTE_COVERAGE_MATRIX]"])
            w.writerow(["route_id"] + [f"seq_{i}" for i in range(n_sequences)])
            for rid in route_ids:
                row = [1 if seq_idx in route_coverage.get(rid, []) else 0 for seq_idx in range(n_sequences)]
                w.writerow([rid] + row)
            w.writerow([])

        # --- ALLOCATION: VEHICLE -> SEQUENCES ---
        if vehicle_to_sequences:
            w.writerow(["[VEHICLE_TO_SEQUENCES]"])
            w.writerow(["vehicle_id", "seq_indices"])
            for vid in sorted(vehicle_to_sequences.keys()):
                w.writerow([vid, ";".join(str(s) for s in vehicle_to_sequences[vid])])
            w.writerow([])

        # --- SCHEDULING: TIME SLOTS & PRICE / FORECAST ---
        if time_slots is not None:
            w.writerow(["[TIME_SLOTS_PRICE_FORECAST]"])
            w.writerow(["slot_idx", "datetime", "price", "is_triad", "forecast_demand_kw"])
            for t_idx, slot_time in enumerate(time_slots):
                price, is_triad = (price_data or {}).get(slot_time, (0.0, False))
                forecast = (forecast_data or {}).get(slot_time, 0.0)
                w.writerow([t_idx, slot_time.isoformat() if slot_time else "", price, is_triad, forecast])
            w.writerow([])

        # --- SCHEDULING: VEHICLE STATES (constraint params) ---
        if vehicles is not None and vehicle_states is not None:
            w.writerow(["[VEHICLE_STATES]"])
            w.writerow([
                "vehicle_id", "current_soc_kwh", "battery_capacity_kwh", "ac_charge_rate_kw",
                "target_soc_kwh", "max_cumulative_kwh"
            ])
            for v in vehicles:
                state = vehicle_states.get(v.vehicle_id)
                if not state:
                    w.writerow([v.vehicle_id, "", "", "", "", ""])
                    continue
                target_soc = (target_soc_percent / 100.0) * state.battery_capacity_kwh
                max_cum = max(0.0, state.battery_capacity_kwh - state.current_soc_kwh)
                w.writerow([
                    v.vehicle_id, state.current_soc_kwh, state.battery_capacity_kwh,
                    state.ac_charge_rate_kw, target_soc, max_cum
                ])
            w.writerow([])

        # --- SCHEDULING: ENERGY REQUIREMENTS ---
        if vehicles is not None and energy_requirements is not None:
            w.writerow(["[ENERGY_REQUIREMENTS]"])
            w.writerow(["vehicle_id", "route_id", "slot_idx", "plan_start", "cumulative_energy_kwh", "required_energy_kwh"])
            for v in vehicles:
                state = vehicle_states.get(v.vehicle_id) if vehicle_states else None
                reqs = energy_requirements.get(v.vehicle_id, [])
                for req in reqs:
                    slot_idx = ""
                    if time_slots:
                        for ti, st in enumerate(time_slots):
                            if st >= req.plan_start_date_time:
                                slot_idx = ti
                                break
                    required = max(0.0, req.cumulative_energy_kwh - (state.current_soc_kwh if state else 0))
                    w.writerow([
                        v.vehicle_id, req.route_id, slot_idx,
                        req.plan_start_date_time.isoformat() if req.plan_start_date_time else "",
                        req.cumulative_energy_kwh, required
                    ])
            w.writerow([])

        # --- SCHEDULING: AVAILABILITY MATRIX ---
        if vehicles is not None and availability_matrices is not None and time_slots is not None:
            w.writerow(["[AVAILABILITY_MATRIX]"])
            w.writerow(["vehicle_id"] + [f"slot_{i}" for i in range(n_slots)])
            for v in vehicles:
                av = availability_matrices.get(v.vehicle_id)
                row = [v.vehicle_id] + [
                    (1 if av.availability_matrix[i] else 0) if av and i < len(av.availability_matrix) else ""
                    for i in range(n_slots)
                ]
                w.writerow(row)
            w.writerow([])

        # --- SCHEDULING: SITE CAPACITY (single param) ---
        w.writerow(["[SITE_CAPACITY]"])
        w.writerow(["site_capacity_kw", getattr(config, "site_capacity_kw", "")])
        w.writerow([])

        # --- DECISION VARIABLES (structure only, values left empty) ---
        w.writerow(["[DECISION_SEQUENCE_SELECTED]"])
        w.writerow(["seq_idx", "selected"])
        for i in range(n_sequences):
            w.writerow([i, ""])
        w.writerow([])

        w.writerow(["[DECISION_ROUTE_COVERED]"])
        w.writerow(["route_id", "covered"])
        for rid in (route_ids or []):
            w.writerow([rid, ""])
        w.writerow([])

        if n_slots > 0 and n_vehicles > 0:
            w.writerow(["[DECISION_CHARGE_POWER_KW]"])
            w.writerow(["slot_idx", "vehicle_idx", "vehicle_id", "charge_power_kw"])
            for t_idx in range(n_slots):
                for v_idx, v in enumerate(vehicles or []):
                    w.writerow([t_idx, v_idx, v.vehicle_id, ""])
            w.writerow([])

            w.writerow(["[DECISION_CUMULATIVE_ENERGY_KWH]"])
            w.writerow(["slot_idx", "vehicle_idx", "vehicle_id", "cumulative_energy_kwh"])
            for t_idx in range(n_slots):
                for v_idx, v in enumerate(vehicles or []):
                    w.writerow([t_idx, v_idx, v.vehicle_id, ""])
            w.writerow([])

        w.writerow(["[DECISION_SHORTFALL_KWH]"])
        w.writerow(["vehicle_idx", "vehicle_id", "shortfall_kwh"])
        for v_idx, v in enumerate(vehicles or []):
            w.writerow([v_idx, v.vehicle_id, ""])

    logger.info(f"Unified optimizer debug CSV written: {filepath}")
