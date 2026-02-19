"""Unified Hexaly optimizer for combined allocation and charge scheduling.

Provides a single optimization model that can run:
- Allocation only (fix_scheduling=True or no scheduling data provided)
- Scheduling only (fix_allocation=True with pre-allocated sequences)
- Integrated (both allocation and scheduling in weighted sum objective)

Objective (weighted sum):
    maximize: α * (route_allocation_score) - β * (charging_cost + shortfall_penalty)

Where:
    - route_allocation_score = W_route * routes_covered + sequence_scores
    - charging_cost = Σ (price + synthetic) * energy
    - shortfall_penalty = λ * Σ shortfall_from_target_soc
"""
import csv
import os
import hexaly.optimizer as hx
import numpy as np
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional, Any
from datetime import datetime

# Set to True to export all matrices and constraint params to a single CSV (decision vars left empty).
DEBUG_EXPORT_UNIFIED_MATRICES_CSV = True

from src.models.allocation import RouteAllocation, AllocationResult
from src.models.scheduler import (
    VehicleChargeState, RouteEnergyRequirement, VehicleAvailability,
    ChargeSlot, VehicleChargeSchedule, ChargeScheduleResult
)
from src.models.vehicle import Vehicle
from src.models.route import Route
from src.utils.logging_config import logger
from src.config import IS_HEXALY_ACTIVE


class OptimizationMode(Enum):
    """Optimization mode for unified optimizer."""
    ALLOCATION_ONLY = 'allocation_only'       # Only allocate routes to vehicles
    SCHEDULING_ONLY = 'scheduling_only'       # Only schedule charging (routes pre-allocated)
    INTEGRATED = 'integrated'                 # Both allocation and scheduling


@dataclass
class UnifiedOptimizationConfig:
    """Configuration for unified optimization."""
    
    mode: OptimizationMode = OptimizationMode.INTEGRATED
    
    # Time limits (seconds)
    allocation_time_limit: int = 30
    scheduling_time_limit: int = 300
    integrated_time_limit: int = 330
    
    # Allocation weights
    route_count_weight: float = 1e2  # Weight for route coverage priority
    allocation_score_weight: float = 1.0  # α: weight for allocation score term
    
    # Scheduling weights
    scheduling_cost_weight: float = 1.0  # β: weight for charging cost term
    target_soc_shortfall_penalty: float = 0.2  # λ: penalty per kWh shortfall
    triad_penalty_factor: float = 100.0  # Kept for API compatibility; not used in objective
    synthetic_time_price_factor: float = 0.01
    
    # Target SOC
    target_soc_percent: float = 75.0
    
    # Site capacity
    site_capacity_kw: float = 0.0


@dataclass
class UnifiedOptimizationResult:
    """Result from unified optimization."""
    
    mode: OptimizationMode
    status: str
    solve_time_seconds: float
    
    # Allocation results (when allocation enabled)
    selected_sequences: List[Tuple] = field(default_factory=list)
    allocation_score: float = 0.0
    routes_allocated: int = 0
    routes_total: int = 0
    
    # Scheduling results (when scheduling enabled)
    vehicle_schedules: List[VehicleChargeSchedule] = field(default_factory=list)
    total_charging_cost: float = 0.0
    total_energy_kwh: float = 0.0
    
    # Combined metrics
    objective_value: float = 0.0
    
    def to_allocation_result(self, allocation_id: int, site_id: int,
                             window_start: datetime, window_end: datetime,
                             all_route_ids: List[str]) -> AllocationResult:
        """Convert to legacy AllocationResult format."""
        result = AllocationResult(
            allocation_id=allocation_id,
            site_id=site_id,
            run_datetime=datetime.now(),
            window_start=window_start,
            window_end=window_end,
            total_score=self.allocation_score,
            routes_in_window=len(all_route_ids),
            status='P'
        )
        
        allocated_routes = set()
        for vehicle_id, route_sequence, cost in self.selected_sequences:
            for route in route_sequence:
                allocation = RouteAllocation(
                    route_id=route.route_id,
                    vehicle_id=vehicle_id,
                    estimated_arrival=route.plan_end_date_time,
                    estimated_arrival_soc=80.0,
                    cost=cost / len(route_sequence) if route_sequence else 0
                )
                result.add_allocation(allocation)
                allocated_routes.add(route.route_id)
        
        for route_id in all_route_ids:
            if route_id not in allocated_routes:
                result.mark_unallocated(route_id)
        
        return result
    
    def to_schedule_result(self, schedule_id: int, site_id: int,
                           planning_start: datetime, planning_end: datetime) -> ChargeScheduleResult:
        """Convert to legacy ChargeScheduleResult format."""
        hours = (planning_end - planning_start).total_seconds() / 3600.0
        return ChargeScheduleResult(
            schedule_id=schedule_id,
            site_id=site_id,
            vehicle_schedules=self.vehicle_schedules,
            planning_start=planning_start,
            planning_end=planning_end,
            actual_planning_window_hours=hours,
            total_cost=self.total_charging_cost,
            total_energy_kwh=self.total_energy_kwh,
            solve_time_seconds=self.solve_time_seconds,
            optimization_status=self.status,
            validation_passed=True,
            vehicles_scheduled=len(self.vehicle_schedules)
        )


def _export_unified_debug_matrices_csv(
    config: UnifiedOptimizationConfig,
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
    vehicle_to_idx = {v.vehicle_id: idx for idx, v in enumerate(vehicles)} if vehicles else {}

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

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)

        # --- CONFIG / CONSTRAINT PARAMS ---
        w.writerow(["[CONFIG_PARAMS]"])
        w.writerow(["param", "value"])
        w.writerow(["mode", config.mode.value])
        w.writerow(["allocation_time_limit", config.allocation_time_limit])
        w.writerow(["scheduling_time_limit", config.scheduling_time_limit])
        w.writerow(["integrated_time_limit", config.integrated_time_limit])
        w.writerow(["route_count_weight", config.route_count_weight])
        w.writerow(["allocation_score_weight", config.allocation_score_weight])
        w.writerow(["scheduling_cost_weight", config.scheduling_cost_weight])
        w.writerow(["target_soc_shortfall_penalty", config.target_soc_shortfall_penalty])
        w.writerow(["target_soc_percent", config.target_soc_percent])
        w.writerow(["site_capacity_kw", config.site_capacity_kw])
        w.writerow(["synthetic_time_price_factor", config.synthetic_time_price_factor])
        w.writerow(["n_sequences", n_sequences])
        w.writerow(["n_routes", n_routes])
        w.writerow(["n_slots", n_slots])
        w.writerow(["n_vehicles", n_vehicles])
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
                target_soc = (config.target_soc_percent / 100.0) * state.battery_capacity_kwh
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
        w.writerow(["site_capacity_kw", config.site_capacity_kw])
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


class UnifiedOptimizer:
    """
    Unified Hexaly optimizer for vehicle-route allocation and charge scheduling.
    
    Supports three modes:
    - ALLOCATION_ONLY: Maximize routes allocated, then score. No charging.
    - SCHEDULING_ONLY: Minimize charging cost for pre-allocated routes.
    - INTEGRATED: Weighted sum of allocation score and (negative) charging cost.
    
    Key features:
    - fix_allocation: Provide pre-allocated sequences to skip allocation phase
    - fix_scheduling: Set True to disable scheduling (allocation only)
    - Weighted sum objective allows tuning tradeoffs
    """
    
    def __init__(self, config: Optional[UnifiedOptimizationConfig] = None):
        """
        Initialize unified optimizer.
        
        Args:
            config: Optimization configuration. Uses defaults if not provided.
        """
        self.config = config or UnifiedOptimizationConfig()
    
    def solve(
        self,
        # Allocation inputs
        sequences: Optional[List[Tuple]] = None,
        route_ids: Optional[List[str]] = None,
        sequence_costs: Optional[np.ndarray] = None,
        # Scheduling inputs
        schedule_id: Optional[int] = None,
        vehicles: Optional[List[Vehicle]] = None,
        vehicle_states: Optional[Dict[int, VehicleChargeState]] = None,
        energy_requirements: Optional[Dict[int, List[RouteEnergyRequirement]]] = None,
        availability_matrices: Optional[Dict[int, VehicleAvailability]] = None,
        time_slots: Optional[List[datetime]] = None,
        forecast_data: Optional[Dict[datetime, float]] = None,
        price_data: Optional[Dict[datetime, Tuple[float, bool]]] = None,
        # Mode overrides
        fix_allocation: Optional[List[Tuple]] = None,
        fix_scheduling: bool = False,
    ) -> UnifiedOptimizationResult:
        """
        Solve unified allocation + scheduling optimization.
        
        Args:
            sequences: Allocation sequences (vehicle_id, route_sequence, cost)
            route_ids: All route IDs to allocate
            sequence_costs: Cost/score for each sequence
            schedule_id: Schedule identifier
            vehicles: Vehicles to schedule charging for
            vehicle_states: Current charge state per vehicle
            energy_requirements: Route energy requirements per vehicle
            availability_matrices: Time-slotted availability per vehicle
            time_slots: 30-minute time slots for scheduling
            forecast_data: Site demand forecast per slot
            price_data: (price, is_triad) per slot
            fix_allocation: Pre-allocated sequences (skips allocation optimization)
            fix_scheduling: If True, disable scheduling (allocation only)
        
        Returns:
            UnifiedOptimizationResult with allocation and/or scheduling outputs
        """
        # Determine effective mode
        mode = self._determine_mode(
            sequences, route_ids, vehicles, time_slots,
            fix_allocation, fix_scheduling
        )
        
        if DEBUG_EXPORT_UNIFIED_MATRICES_CSV:
            _export_unified_debug_matrices_csv(
                self.config,
                sequences=sequences,
                route_ids=route_ids,
                sequence_costs=sequence_costs,
                vehicles=vehicles,
                vehicle_states=vehicle_states,
                energy_requirements=energy_requirements,
                availability_matrices=availability_matrices,
                time_slots=time_slots,
                forecast_data=forecast_data,
                price_data=price_data,
            )
        
        logger.info(f"[UNIFIED] Starting optimization in mode: {mode.value}")
        
        if not IS_HEXALY_ACTIVE:
            logger.warning("Hexaly not active - using greedy fallback")
            return self._greedy_fallback(
                mode, sequences, route_ids, sequence_costs,
                schedule_id, vehicles, vehicle_states, energy_requirements,
                availability_matrices, time_slots, price_data,
                fix_allocation
            )
        
        try:
            if mode == OptimizationMode.ALLOCATION_ONLY:
                return self._solve_allocation_only(
                    sequences, route_ids, sequence_costs
                )
            elif mode == OptimizationMode.SCHEDULING_ONLY:
                return self._solve_scheduling_only(
                    schedule_id, vehicles, vehicle_states, energy_requirements,
                    availability_matrices, time_slots, forecast_data, price_data,
                    fix_allocation
                )
            else:  # INTEGRATED
                return self._solve_integrated(
                    sequences, route_ids, sequence_costs,
                    schedule_id, vehicles, vehicle_states, energy_requirements,
                    availability_matrices, time_slots, forecast_data, price_data
                )
        except Exception as e:
            logger.error(f"Unified optimization failed: {e}", exc_info=True)
            return self._greedy_fallback(
                mode, sequences, route_ids, sequence_costs,
                schedule_id, vehicles, vehicle_states, energy_requirements,
                availability_matrices, time_slots, price_data,
                fix_allocation
            )
    
    def _determine_mode(
        self,
        sequences: Optional[List[Tuple]],
        route_ids: Optional[List[str]],
        vehicles: Optional[List[Vehicle]],
        time_slots: Optional[List[datetime]],
        fix_allocation: Optional[List[Tuple]],
        fix_scheduling: bool
    ) -> OptimizationMode:
        """Determine effective optimization mode from inputs."""
        # Explicit mode from config
        if self.config.mode != OptimizationMode.INTEGRATED:
            return self.config.mode
        
        has_allocation_data = sequences is not None and route_ids is not None
        has_scheduling_data = vehicles is not None and time_slots is not None
        
        # Fixed allocation -> scheduling only
        if fix_allocation is not None:
            return OptimizationMode.SCHEDULING_ONLY
        
        # Fixed scheduling -> allocation only
        if fix_scheduling:
            return OptimizationMode.ALLOCATION_ONLY
        
        # Infer from available data
        if has_allocation_data and not has_scheduling_data:
            return OptimizationMode.ALLOCATION_ONLY
        elif has_scheduling_data and not has_allocation_data:
            return OptimizationMode.SCHEDULING_ONLY
        elif has_allocation_data and has_scheduling_data:
            return OptimizationMode.INTEGRATED
        else:
            raise ValueError("Insufficient data for any optimization mode")
    
    def _solve_allocation_only(
        self,
        sequences: List[Tuple],
        route_ids: List[str],
        sequence_costs: np.ndarray
    ) -> UnifiedOptimizationResult:
        """Solve allocation-only optimization."""
        with hx.HexalyOptimizer() as optimizer:
            model = optimizer.model
            
            n_sequences = len(sequences)
            n_routes = len(route_ids)
            
            logger.info(f"[UNIFIED:ALLOC] Building model: {n_sequences} sequences, {n_routes} routes")
            
            # Decision variables: binary selection for each sequence
            sequence_vars = [model.bool() for _ in range(n_sequences)]
            
            # Build mappings
            route_coverage = {route_id: [] for route_id in route_ids}
            vehicle_to_sequences = {}
            
            for seq_idx, (vehicle_id, route_sequence, cost) in enumerate(sequences):
                vehicle_to_sequences.setdefault(vehicle_id, []).append(seq_idx)
                for route in route_sequence:
                    if route.route_id in route_coverage:
                        route_coverage[route.route_id].append(seq_idx)
            
            # Constraint: Each vehicle used by at most one sequence
            for vehicle_id, seq_indices in vehicle_to_sequences.items():
                model.constraint(
                    model.sum([sequence_vars[i] for i in seq_indices]) <= 1
                )
            
            # Route coverage constraints and variables
            route_covered_vars = {}
            for route_id in route_ids:
                covering_sequences = route_coverage[route_id]
                if covering_sequences:
                    coverage_sum = model.sum([sequence_vars[idx] for idx in covering_sequences])
                    model.constraint(coverage_sum <= 1)
                    
                    route_covered = model.bool()
                    route_covered_vars[route_id] = route_covered
                    model.constraint(route_covered <= coverage_sum)
                    model.constraint(coverage_sum <= len(covering_sequences) * route_covered)
            
            # Objective: maximize routes covered + scores
            score_term = model.sum([
                sequence_vars[i] * float(sequence_costs[i]) 
                for i in range(n_sequences)
            ])
            
            if route_covered_vars:
                route_count_term = model.sum(list(route_covered_vars.values()))
                objective = self.config.route_count_weight * route_count_term + score_term
            else:
                objective = score_term
            
            model.maximize(objective)
            model.close()
            
            # Solve
            optimizer.param.time_limit = self.config.allocation_time_limit
            optimizer.solve()
            
            # Extract solution
            selected_indices = [i for i in range(n_sequences) if sequence_vars[i].value == 1]
            selected_sequences = [sequences[i] for i in selected_indices]
            total_score = sum(sequences[i][2] for i in selected_indices)
            routes_allocated = sum(
                1 for r in route_covered_vars if route_covered_vars[r].value == 1
            )
            
            solve_time = optimizer.statistics.running_time
            status = 'optimal' if optimizer.solution.status == hx.HxSolutionStatus.OPTIMAL else 'feasible'
            
            logger.info(
                f"[UNIFIED:ALLOC] Complete: {len(selected_sequences)} sequences, "
                f"{routes_allocated}/{n_routes} routes, score={total_score:.2f}"
            )
            
            return UnifiedOptimizationResult(
                mode=OptimizationMode.ALLOCATION_ONLY,
                status=status,
                solve_time_seconds=solve_time,
                selected_sequences=selected_sequences,
                allocation_score=total_score,
                routes_allocated=routes_allocated,
                routes_total=n_routes,
                objective_value=total_score
            )
    
    def _solve_scheduling_only(
        self,
        schedule_id: int,
        vehicles: List[Vehicle],
        vehicle_states: Dict[int, VehicleChargeState],
        energy_requirements: Dict[int, List[RouteEnergyRequirement]],
        availability_matrices: Dict[int, VehicleAvailability],
        time_slots: List[datetime],
        forecast_data: Dict[datetime, float],
        price_data: Dict[datetime, Tuple[float, bool]],
        fix_allocation: Optional[List[Tuple]] = None
    ) -> UnifiedOptimizationResult:
        """Solve scheduling-only optimization with fixed/pre-allocated routes."""
        with hx.HexalyOptimizer() as optimizer:
            model = optimizer.model
            
            n_slots = len(time_slots)
            n_vehicles = len(vehicles)
            
            logger.info(
                f"[UNIFIED:SCHED] Building model: {n_vehicles} vehicles, {n_slots} slots"
            )
            
            vehicle_to_idx = {v.vehicle_id: idx for idx, v in enumerate(vehicles)}
            
            # Bounds helpers
            def _max_charge_kw(v_idx: int) -> float:
                state = vehicle_states.get(vehicles[v_idx].vehicle_id)
                return state.ac_charge_rate_kw if state else 50.0
            
            def _max_cumulative_kwh(v_idx: int) -> float:
                state = vehicle_states.get(vehicles[v_idx].vehicle_id)
                if state:
                    return max(0.0, state.battery_capacity_kwh - state.current_soc_kwh)
                return 1000.0
            
            # Decision variables
            charge_power = [
                [model.float(0, _max_charge_kw(v_idx)) for v_idx in range(n_vehicles)]
                for _ in range(n_slots)
            ]
            
            cumulative_energy = [
                [model.float(0, _max_cumulative_kwh(v_idx)) for v_idx in range(n_vehicles)]
                for _ in range(n_slots)
            ]
            
            # Build objective: minimize charging cost + shortfall penalty
            cost_terms = []
            for t_idx, slot_time in enumerate(time_slots):
                price, _ = price_data.get(slot_time, (0.15, False))
                synthetic_price = self.config.synthetic_time_price_factor * (n_slots - t_idx) / n_slots
                slot_cost = price + synthetic_price
                
                for v_idx in range(n_vehicles):
                    energy_this_slot = charge_power[t_idx][v_idx] * 0.5
                    cost_terms.append(slot_cost * energy_this_slot)
            
            # Shortfall penalty (soft target SOC)
            shortfall_terms = []
            for v_idx, vehicle in enumerate(vehicles):
                state = vehicle_states.get(vehicle.vehicle_id)
                if not state:
                    continue
                
                target_soc_kwh = (self.config.target_soc_percent / 100.0) * state.battery_capacity_kwh
                max_shortfall = max(0.0, target_soc_kwh - state.current_soc_kwh)
                
                if max_shortfall > 0:
                    shortfall_v = model.float(0, max_shortfall)
                    model.constraint(
                        shortfall_v >= target_soc_kwh - state.current_soc_kwh - cumulative_energy[n_slots - 1][v_idx]
                    )
                    shortfall_terms.append(self.config.target_soc_shortfall_penalty * shortfall_v)
            
            if shortfall_terms:
                model.minimize(model.sum(cost_terms) + model.sum(shortfall_terms))
            else:
                model.minimize(model.sum(cost_terms))
            
            # Constraints
            self._add_scheduling_constraints(
                model, vehicles, vehicle_states, energy_requirements,
                availability_matrices, time_slots, forecast_data,
                charge_power, cumulative_energy, vehicle_to_idx
            )
            
            model.close()
            
            # Solve
            optimizer.param.time_limit = self.config.scheduling_time_limit
            optimizer.solve()
            
            # Extract solution
            solve_time = optimizer.statistics.running_time
            status = str(optimizer.solution.status)
            
            vehicle_schedules, total_cost, total_energy = self._extract_scheduling_solution(
                schedule_id, vehicles, vehicle_states, energy_requirements,
                time_slots, price_data, charge_power, cumulative_energy, vehicle_to_idx
            )
            
            logger.info(
                f"[UNIFIED:SCHED] Complete: {len(vehicle_schedules)} vehicles, "
                f"cost={total_cost:.2f}, energy={total_energy:.2f} kWh"
            )
            
            return UnifiedOptimizationResult(
                mode=OptimizationMode.SCHEDULING_ONLY,
                status=status,
                solve_time_seconds=solve_time,
                selected_sequences=fix_allocation or [],
                vehicle_schedules=vehicle_schedules,
                total_charging_cost=total_cost,
                total_energy_kwh=total_energy,
                objective_value=-total_cost  # Negative because we minimize cost
            )
    
    def _solve_integrated(
        self,
        sequences: List[Tuple],
        route_ids: List[str],
        sequence_costs: np.ndarray,
        schedule_id: int,
        vehicles: List[Vehicle],
        vehicle_states: Dict[int, VehicleChargeState],
        energy_requirements: Dict[int, List[RouteEnergyRequirement]],
        availability_matrices: Dict[int, VehicleAvailability],
        time_slots: List[datetime],
        forecast_data: Dict[datetime, float],
        price_data: Dict[datetime, Tuple[float, bool]]
    ) -> UnifiedOptimizationResult:
        """
        Solve integrated allocation + scheduling optimization.
        
        Uses weighted sum objective:
            maximize: α * allocation_score - β * charging_cost
        """
        with hx.HexalyOptimizer() as optimizer:
            model = optimizer.model
            
            n_sequences = len(sequences)
            n_routes = len(route_ids)
            n_slots = len(time_slots)
            n_vehicles = len(vehicles)
            
            logger.info(
                f"[UNIFIED:INTEGRATED] Building model: "
                f"{n_sequences} sequences, {n_routes} routes, "
                f"{n_vehicles} vehicles, {n_slots} slots"
            )
            
            vehicle_to_idx = {v.vehicle_id: idx for idx, v in enumerate(vehicles)}
            
            # ===== ALLOCATION VARIABLES =====
            sequence_vars = [model.bool() for _ in range(n_sequences)]
            
            # Build mappings
            route_coverage = {route_id: [] for route_id in route_ids}
            vehicle_to_sequences = {}
            
            for seq_idx, (vehicle_id, route_sequence, cost) in enumerate(sequences):
                vehicle_to_sequences.setdefault(vehicle_id, []).append(seq_idx)
                for route in route_sequence:
                    if route.route_id in route_coverage:
                        route_coverage[route.route_id].append(seq_idx)
            
            # Allocation constraints
            for vehicle_id, seq_indices in vehicle_to_sequences.items():
                model.constraint(
                    model.sum([sequence_vars[i] for i in seq_indices]) <= 1
                )
            
            route_covered_vars = {}
            for route_id in route_ids:
                covering_sequences = route_coverage[route_id]
                if covering_sequences:
                    coverage_sum = model.sum([sequence_vars[idx] for idx in covering_sequences])
                    model.constraint(coverage_sum <= 1)
                    
                    route_covered = model.bool()
                    route_covered_vars[route_id] = route_covered
                    model.constraint(route_covered <= coverage_sum)
                    model.constraint(coverage_sum <= len(covering_sequences) * route_covered)
            
            # ===== SCHEDULING VARIABLES =====
            def _max_charge_kw(v_idx: int) -> float:
                state = vehicle_states.get(vehicles[v_idx].vehicle_id)
                return state.ac_charge_rate_kw if state else 50.0
            
            def _max_cumulative_kwh(v_idx: int) -> float:
                state = vehicle_states.get(vehicles[v_idx].vehicle_id)
                if state:
                    return max(0.0, state.battery_capacity_kwh - state.current_soc_kwh)
                return 1000.0
            
            charge_power = [
                [model.float(0, _max_charge_kw(v_idx)) for v_idx in range(n_vehicles)]
                for _ in range(n_slots)
            ]
            
            cumulative_energy = [
                [model.float(0, _max_cumulative_kwh(v_idx)) for v_idx in range(n_vehicles)]
                for _ in range(n_slots)
            ]
            
            # ===== OBJECTIVE: WEIGHTED SUM =====
            
            # Allocation term: route count + sequence scores
            allocation_score_term = model.sum([
                sequence_vars[i] * float(sequence_costs[i])
                for i in range(n_sequences)
            ])
            
            if route_covered_vars:
                route_count_term = model.sum(list(route_covered_vars.values()))
                allocation_term = (
                    self.config.route_count_weight * route_count_term + 
                    allocation_score_term
                )
            else:
                allocation_term = allocation_score_term
            
            # Scheduling term: charging cost
            cost_terms = []
            for t_idx, slot_time in enumerate(time_slots):
                price, _ = price_data.get(slot_time, (0.15, False))
                synthetic_price = self.config.synthetic_time_price_factor * (n_slots - t_idx) / n_slots
                slot_cost = price + synthetic_price
                
                for v_idx in range(n_vehicles):
                    energy_this_slot = charge_power[t_idx][v_idx] * 0.5
                    cost_terms.append(slot_cost * energy_this_slot)
            
            # Shortfall penalty
            shortfall_terms = []
            for v_idx, vehicle in enumerate(vehicles):
                state = vehicle_states.get(vehicle.vehicle_id)
                if not state:
                    continue
                
                target_soc_kwh = (self.config.target_soc_percent / 100.0) * state.battery_capacity_kwh
                max_shortfall = max(0.0, target_soc_kwh - state.current_soc_kwh)
                
                if max_shortfall > 0:
                    shortfall_v = model.float(0, max_shortfall)
                    model.constraint(
                        shortfall_v >= target_soc_kwh - state.current_soc_kwh - cumulative_energy[n_slots - 1][v_idx]
                    )
                    shortfall_terms.append(self.config.target_soc_shortfall_penalty * shortfall_v)
            
            scheduling_term = model.sum(cost_terms)
            if shortfall_terms:
                scheduling_term = scheduling_term + model.sum(shortfall_terms)
            
            # Combined weighted sum: maximize allocation - cost
            # Note: We maximize, so subtract the cost term
            combined_objective = (
                self.config.allocation_score_weight * allocation_term -
                self.config.scheduling_cost_weight * scheduling_term
            )
            model.maximize(combined_objective)
            
            # ===== SCHEDULING CONSTRAINTS =====
            self._add_scheduling_constraints(
                model, vehicles, vehicle_states, energy_requirements,
                availability_matrices, time_slots, forecast_data,
                charge_power, cumulative_energy, vehicle_to_idx
            )
            
            model.close()
            
            # Solve
            optimizer.param.time_limit = self.config.integrated_time_limit
            optimizer.solve()
            
            # Extract allocation solution
            selected_indices = [i for i in range(n_sequences) if sequence_vars[i].value == 1]
            selected_sequences = [sequences[i] for i in selected_indices]
            allocation_score = sum(sequences[i][2] for i in selected_indices)
            routes_allocated = sum(
                1 for r in route_covered_vars if route_covered_vars[r].value == 1
            )
            
            # Extract scheduling solution
            vehicle_schedules, total_cost, total_energy = self._extract_scheduling_solution(
                schedule_id, vehicles, vehicle_states, energy_requirements,
                time_slots, price_data, charge_power, cumulative_energy, vehicle_to_idx
            )
            
            solve_time = optimizer.statistics.running_time
            status = str(optimizer.solution.status)
            
            logger.info(
                f"[UNIFIED:INTEGRATED] Complete: "
                f"{len(selected_sequences)} sequences, {routes_allocated}/{n_routes} routes, "
                f"alloc_score={allocation_score:.2f}, "
                f"charge_cost={total_cost:.2f}, energy={total_energy:.2f} kWh"
            )
            
            return UnifiedOptimizationResult(
                mode=OptimizationMode.INTEGRATED,
                status=status,
                solve_time_seconds=solve_time,
                selected_sequences=selected_sequences,
                allocation_score=allocation_score,
                routes_allocated=routes_allocated,
                routes_total=n_routes,
                vehicle_schedules=vehicle_schedules,
                total_charging_cost=total_cost,
                total_energy_kwh=total_energy,
                objective_value=allocation_score - total_cost
            )
    
    def _add_scheduling_constraints(
        self,
        model,
        vehicles: List[Vehicle],
        vehicle_states: Dict[int, VehicleChargeState],
        energy_requirements: Dict[int, List[RouteEnergyRequirement]],
        availability_matrices: Dict[int, VehicleAvailability],
        time_slots: List[datetime],
        forecast_data: Dict[datetime, float],
        charge_power: List[List],
        cumulative_energy: List[List],
        vehicle_to_idx: Dict[int, int]
    ):
        """Add scheduling constraints to model."""
        n_slots = len(time_slots)
        n_vehicles = len(vehicles)
        
        # 1. Cumulative Energy Calculation
        for v_idx, vehicle in enumerate(vehicles):
            state = vehicle_states.get(vehicle.vehicle_id)
            if not state:
                continue
            
            for t_idx in range(n_slots):
                if t_idx == 0:
                    model.constraint(
                        cumulative_energy[t_idx][v_idx] == charge_power[t_idx][v_idx] * 0.5
                    )
                else:
                    model.constraint(
                        cumulative_energy[t_idx][v_idx] == 
                        cumulative_energy[t_idx - 1][v_idx] + charge_power[t_idx][v_idx] * 0.5
                    )
        
        # 2. Route Energy Requirements (hard constraint)
        for vehicle in vehicles:
            v_idx = vehicle_to_idx.get(vehicle.vehicle_id)
            if v_idx is None:
                continue
            
            state = vehicle_states.get(vehicle.vehicle_id)
            requirements = energy_requirements.get(vehicle.vehicle_id, [])
            
            if not state or not requirements:
                continue
            
            for requirement in requirements:
                checkpoint_idx = self._find_time_slot_index(
                    requirement.plan_start_date_time, time_slots
                )
                
                if checkpoint_idx is None or checkpoint_idx == 0:
                    continue
                
                required_energy = max(
                    0.0,
                    requirement.cumulative_energy_kwh - state.current_soc_kwh
                )
                
                if required_energy > 0:
                    model.constraint(
                        cumulative_energy[checkpoint_idx - 1][v_idx] >= required_energy
                    )
        
        # 3. Site Capacity Constraint
        site_capacity_kw = self.config.site_capacity_kw
        for t_idx, slot_time in enumerate(time_slots):
            site_demand_kw = forecast_data.get(slot_time, 0.0) if forecast_data else 0.0
            available_capacity = max(0.0, site_capacity_kw - site_demand_kw)
            
            if available_capacity > 0:
                total_charging = model.sum(charge_power[t_idx])
                model.constraint(total_charging <= available_capacity)
        
        # 4. Maximum SOC (can't charge beyond battery capacity)
        for v_idx, vehicle in enumerate(vehicles):
            state = vehicle_states.get(vehicle.vehicle_id)
            if not state:
                continue
            
            max_energy_kwh = state.battery_capacity_kwh - state.current_soc_kwh
            
            for t_idx in range(n_slots):
                model.constraint(cumulative_energy[t_idx][v_idx] <= max_energy_kwh)
        
        # 5. Charge Rate Limits
        for v_idx, vehicle in enumerate(vehicles):
            state = vehicle_states.get(vehicle.vehicle_id)
            if not state:
                continue
            
            max_charge_rate = state.ac_charge_rate_kw
            
            for t_idx in range(n_slots):
                model.constraint(charge_power[t_idx][v_idx] <= max_charge_rate)
        
        # 6. Vehicle Availability
        for v_idx, vehicle in enumerate(vehicles):
            availability = availability_matrices.get(vehicle.vehicle_id)
            if not availability:
                continue
            
            for t_idx in range(n_slots):
                if not availability.availability_matrix[t_idx]:
                    model.constraint(charge_power[t_idx][v_idx] == 0)
    
    def _extract_scheduling_solution(
        self,
        schedule_id: int,
        vehicles: List[Vehicle],
        vehicle_states: Dict[int, VehicleChargeState],
        energy_requirements: Dict[int, List[RouteEnergyRequirement]],
        time_slots: List[datetime],
        price_data: Dict[datetime, Tuple[float, bool]],
        charge_power: List[List],
        cumulative_energy: List[List],
        vehicle_to_idx: Dict[int, int]
    ) -> Tuple[List[VehicleChargeSchedule], float, float]:
        """Extract scheduling solution from Hexaly variables."""
        vehicle_schedules = []
        total_cost = 0.0
        total_energy = 0.0
        n_slots = len(time_slots)
        
        for v_idx, vehicle in enumerate(vehicles):
            state = vehicle_states.get(vehicle.vehicle_id)
            requirements = energy_requirements.get(vehicle.vehicle_id, [])
            
            if not state:
                continue
            
            target_soc_kwh = (self.config.target_soc_percent / 100.0) * state.battery_capacity_kwh
            if requirements:
                route_energy = requirements[-1].cumulative_energy_kwh
                energy_needed = max(0.0, max(route_energy, target_soc_kwh) - state.current_soc_kwh)
            else:
                energy_needed = max(0.0, target_soc_kwh - state.current_soc_kwh)
            
            charge_slots = []
            cumulative = 0.0
            
            for t_idx, slot_time in enumerate(time_slots):
                power_kw = charge_power[t_idx][v_idx].value
                
                if power_kw > 0.01:
                    energy_kwh = power_kw * 0.5
                    cumulative += energy_kwh
                    
                    price, is_triad = price_data.get(slot_time, (0.15, False))
                    
                    charge_slot = ChargeSlot(
                        time_slot=slot_time,
                        charge_power_kw=power_kw,
                        cumulative_energy_kwh=cumulative,
                        electricity_price=price,
                        is_triad_period=is_triad
                    )
                    charge_slots.append(charge_slot)
                    
                    slot_cost = price + self.config.synthetic_time_price_factor * (n_slots - t_idx) / n_slots
                    total_cost += energy_kwh * slot_cost
                    total_energy += energy_kwh
            
            vehicle_schedule = VehicleChargeSchedule(
                vehicle_id=vehicle.vehicle_id,
                schedule_id=schedule_id,
                initial_soc_kwh=state.current_soc_kwh,
                target_soc_kwh=target_soc_kwh,
                total_energy_needed_kwh=energy_needed,
                route_checkpoints=requirements,
                has_routes=len(requirements) > 0,
                charge_slots=charge_slots,
                total_energy_scheduled_kwh=cumulative,
                assigned_charger_id=state.charger_id,
                charger_type=state.charger_type
            )
            vehicle_schedules.append(vehicle_schedule)
        
        return vehicle_schedules, total_cost, total_energy
    
    def _find_time_slot_index(self, target_time: datetime, 
                              time_slots: List[datetime]) -> Optional[int]:
        """Find index of time slot closest to target time."""
        for idx, slot_time in enumerate(time_slots):
            if slot_time >= target_time:
                return idx
        return None
    
    def _greedy_fallback(
        self,
        mode: OptimizationMode,
        sequences: Optional[List[Tuple]],
        route_ids: Optional[List[str]],
        sequence_costs: Optional[np.ndarray],
        schedule_id: Optional[int],
        vehicles: Optional[List[Vehicle]],
        vehicle_states: Optional[Dict[int, VehicleChargeState]],
        energy_requirements: Optional[Dict[int, List[RouteEnergyRequirement]]],
        availability_matrices: Optional[Dict[int, VehicleAvailability]],
        time_slots: Optional[List[datetime]],
        price_data: Optional[Dict[datetime, Tuple[float, bool]]],
        fix_allocation: Optional[List[Tuple]]
    ) -> UnifiedOptimizationResult:
        """Greedy fallback when Hexaly unavailable."""
        logger.warning(f"[UNIFIED] Using greedy fallback for mode: {mode.value}")
        
        # Allocation fallback
        selected_sequences = []
        allocation_score = 0.0
        routes_allocated = 0
        routes_total = len(route_ids) if route_ids else 0
        
        if mode in (OptimizationMode.ALLOCATION_ONLY, OptimizationMode.INTEGRATED):
            if sequences and route_ids and sequence_costs is not None:
                sorted_indices = np.argsort(sequence_costs)[::-1]
                covered_routes = set()
                used_vehicles = set()
                
                for idx in sorted_indices:
                    vehicle_id, route_sequence, cost = sequences[idx]
                    route_ids_in_seq = [r.route_id for r in route_sequence]
                    
                    if vehicle_id in used_vehicles:
                        continue
                    if any(rid in covered_routes for rid in route_ids_in_seq):
                        continue
                    
                    selected_sequences.append(sequences[idx])
                    covered_routes.update(route_ids_in_seq)
                    used_vehicles.add(vehicle_id)
                    allocation_score += cost
                    
                    if len(covered_routes) == len(route_ids):
                        break
                
                routes_allocated = len(covered_routes)
        elif fix_allocation:
            selected_sequences = fix_allocation
            allocation_score = sum(seq[2] for seq in fix_allocation)
            routes_allocated = sum(len(seq[1]) for seq in fix_allocation)
        
        # Scheduling fallback
        vehicle_schedules = []
        total_cost = 0.0
        total_energy = 0.0
        
        if mode in (OptimizationMode.SCHEDULING_ONLY, OptimizationMode.INTEGRATED):
            if vehicles and vehicle_states and time_slots and price_data:
                for vehicle in vehicles:
                    state = vehicle_states.get(vehicle.vehicle_id)
                    if not state:
                        continue
                    
                    requirements = energy_requirements.get(vehicle.vehicle_id, []) if energy_requirements else []
                    availability = availability_matrices.get(vehicle.vehicle_id) if availability_matrices else None
                    
                    target_soc_kwh = (self.config.target_soc_percent / 100.0) * state.battery_capacity_kwh
                    if requirements:
                        route_min = requirements[-1].cumulative_energy_kwh
                        target_energy_kwh = max(route_min, target_soc_kwh)
                    else:
                        target_energy_kwh = target_soc_kwh
                    
                    energy_needed = max(0.0, target_energy_kwh - state.current_soc_kwh)
                    
                    if energy_needed <= 0:
                        vehicle_schedule = VehicleChargeSchedule(
                            vehicle_id=vehicle.vehicle_id,
                            schedule_id=schedule_id or 0,
                            initial_soc_kwh=state.current_soc_kwh,
                            target_soc_kwh=target_energy_kwh,
                            total_energy_needed_kwh=0,
                            route_checkpoints=requirements,
                            has_routes=len(requirements) > 0
                        )
                        vehicle_schedules.append(vehicle_schedule)
                        continue
                    
                    # Sort slots by price
                    slot_prices = []
                    for idx, slot_time in enumerate(time_slots):
                        if availability and availability.availability_matrix[idx]:
                            price, is_triad = price_data.get(slot_time, (0.15, False))
                            effective_price = price
                            slot_prices.append((effective_price, idx, slot_time, price, is_triad))
                    
                    slot_prices.sort(key=lambda x: x[0])
                    
                    charge_slots = []
                    cumulative = 0.0
                    charge_rate = state.ac_charge_rate_kw
                    
                    for _, idx, slot_time, price, is_triad in slot_prices:
                        if cumulative >= energy_needed:
                            break
                        
                        energy_this_slot = min(charge_rate * 0.5, energy_needed - cumulative)
                        power_this_slot = energy_this_slot / 0.5
                        cumulative += energy_this_slot
                        
                        charge_slot = ChargeSlot(
                            time_slot=slot_time,
                            charge_power_kw=power_this_slot,
                            cumulative_energy_kwh=cumulative,
                            electricity_price=price,
                            is_triad_period=is_triad
                        )
                        charge_slots.append(charge_slot)
                        total_cost += energy_this_slot * price
                        total_energy += energy_this_slot
                    
                    charge_slots.sort(key=lambda x: x.time_slot)
                    
                    vehicle_schedule = VehicleChargeSchedule(
                        vehicle_id=vehicle.vehicle_id,
                        schedule_id=schedule_id or 0,
                        initial_soc_kwh=state.current_soc_kwh,
                        target_soc_kwh=target_energy_kwh,
                        total_energy_needed_kwh=energy_needed,
                        route_checkpoints=requirements,
                        has_routes=len(requirements) > 0,
                        charge_slots=charge_slots,
                        total_energy_scheduled_kwh=cumulative,
                        assigned_charger_id=state.charger_id,
                        charger_type=state.charger_type
                    )
                    vehicle_schedules.append(vehicle_schedule)
        
        return UnifiedOptimizationResult(
            mode=mode,
            status='greedy_fallback',
            solve_time_seconds=0.1,
            selected_sequences=selected_sequences,
            allocation_score=allocation_score,
            routes_allocated=routes_allocated,
            routes_total=routes_total,
            vehicle_schedules=vehicle_schedules,
            total_charging_cost=total_cost,
            total_energy_kwh=total_energy,
            objective_value=allocation_score - total_cost
        )
