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
import hexaly.optimizer as hx
import numpy as np
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional, Any, Union
from datetime import datetime

from src.models.allocation import RouteAllocation, AllocationResult
from src.models.scheduler import (
    VehicleChargeState, RouteEnergyRequirement, VehicleAvailability,
    ChargeSlot, VehicleChargeSchedule, ChargeScheduleResult, Charger, ChargerPowerClass
)
from src.models.vehicle import Vehicle
from src.models.route import Route
from src.utils.logging_config import logger
from src.config import IS_HEXALY_ACTIVE
from src.optimizer.unified_optimizer_debug import (
    DEBUG_EXPORT_UNIFIED_MATRICES_CSV,
    export_unified_debug_matrices_csv,
)


class OptimizationMode(Enum):
    """Optimization mode for unified optimizer."""
    ALLOCATION_ONLY = 'allocation_only'       # Only allocate routes to vehicles
    SCHEDULING_ONLY = 'scheduling_only'       # Only schedule charging (routes pre-allocated)
    INTEGRATED = 'integrated'                 # Both allocation and scheduling


# API / controller mode flags (mode request array items)
MODE_FLAG_ALLOCATION = 'allocation'
MODE_FLAG_CHARGE_SCHEDULING = 'charge_scheduling'
MODE_FLAG_CHARGER_ALLOCATION = 'charger_allocation'
VALID_MODE_FLAGS = frozenset({
    MODE_FLAG_ALLOCATION,
    MODE_FLAG_CHARGE_SCHEDULING,
    MODE_FLAG_CHARGER_ALLOCATION,
})


def normalize_mode_input(mode: Any) -> List[str]:
    """Normalize mode from API array or legacy single string to flag list."""
    if mode is None:
        return [MODE_FLAG_ALLOCATION, MODE_FLAG_CHARGE_SCHEDULING, MODE_FLAG_CHARGER_ALLOCATION]

    if isinstance(mode, str):
        legacy_map = {
            'allocation_only': [MODE_FLAG_ALLOCATION],
            'allocation': [MODE_FLAG_ALLOCATION],
            'scheduling_only': [MODE_FLAG_CHARGE_SCHEDULING],
            'scheduling': [MODE_FLAG_CHARGE_SCHEDULING],
            'integrated': [
                MODE_FLAG_ALLOCATION,
                MODE_FLAG_CHARGE_SCHEDULING,
                MODE_FLAG_CHARGER_ALLOCATION,
            ],
            'both': [
                MODE_FLAG_ALLOCATION,
                MODE_FLAG_CHARGE_SCHEDULING,
                MODE_FLAG_CHARGER_ALLOCATION,
            ],
        }
        key = mode.strip().lower()
        if key in legacy_map:
            return legacy_map[key]
        if key in VALID_MODE_FLAGS:
            return [key]
        raise ValueError(
            f"Invalid optimization mode: {mode!r}. "
            f"Use one of {sorted(VALID_MODE_FLAGS)} or legacy: allocation_only, scheduling_only, integrated."
        )

    flags = []
    for item in mode:
        value = item.value if hasattr(item, 'value') else str(item)
        value = value.strip().lower()
        if value not in VALID_MODE_FLAGS:
            raise ValueError(
                f"Invalid mode flag: {value!r}. "
                f"Allowed: {', '.join(sorted(VALID_MODE_FLAGS))}"
            )
        if value not in flags:
            flags.append(value)
    return flags


def resolve_optimization_from_modes(
    mode_flags: List[str],
) -> Tuple[OptimizationMode, bool]:
    """
    Map mode flag list to solver OptimizationMode and charger-allocation toggle.

    Returns:
        (optimization_mode, enable_charger_allocation)
    """
    normalized = normalize_mode_input(mode_flags)
    flag_set = set(normalized)

    if MODE_FLAG_CHARGER_ALLOCATION in flag_set and MODE_FLAG_CHARGE_SCHEDULING not in flag_set:
        raise ValueError(
            "mode includes 'charger_allocation' but not 'charge_scheduling'. "
            "Charger allocation requires charge scheduling."
        )

    if MODE_FLAG_ALLOCATION not in flag_set and MODE_FLAG_CHARGE_SCHEDULING not in flag_set:
        raise ValueError(
            "mode must include at least one of: 'allocation', 'charge_scheduling'"
        )

    run_allocation = MODE_FLAG_ALLOCATION in flag_set
    run_scheduling = MODE_FLAG_CHARGE_SCHEDULING in flag_set
    enable_charger_allocation = MODE_FLAG_CHARGER_ALLOCATION in flag_set

    if run_allocation and run_scheduling:
        return OptimizationMode.INTEGRATED, enable_charger_allocation
    if run_allocation:
        return OptimizationMode.ALLOCATION_ONLY, False
    return OptimizationMode.SCHEDULING_ONLY, enable_charger_allocation


@dataclass
class UnifiedOptimizationConfig:
    """Configuration for unified optimization."""
    
    mode: OptimizationMode = OptimizationMode.INTEGRATED
    mode_flags: List[str] = field(default_factory=lambda: [
        MODE_FLAG_ALLOCATION,
        MODE_FLAG_CHARGE_SCHEDULING,
        MODE_FLAG_CHARGER_ALLOCATION,
    ])
    
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
    
    # Charger allocation
    enable_charger_allocation: bool = True  # Enable charger allocation constraints (C1-C5)
    
    # Interval scheduling parameters
    makespan_penalty_weight: float = 0.1  # Weight for completion time in objective
    min_session_duration_minutes: int = 30  # Minimum charging session length


@dataclass
class UnifiedOptimizationResult:
    """Result from unified optimization."""
    
    mode: List[str]
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
        mode_flags: Optional[List[str]] = None,
        # Legacy allocation inputs
        sequences: Optional[List[Tuple]] = None,
        route_ids: Optional[List[str]] = None,
        sequence_costs: Optional[np.ndarray] = None,
        # List-based allocation inputs
        allocation_routes: Optional[List[Route]] = None,
        score_vr: Optional[np.ndarray] = None,
        feasible_vr: Optional[np.ndarray] = None,
        route_start_times: Optional[np.ndarray] = None,
        route_end_times: Optional[np.ndarray] = None,
        route_durations: Optional[np.ndarray] = None,
        route_energy_required: Optional[np.ndarray] = None,
        allocation_metadata: Optional[Dict[str, Any]] = None,
        # Scheduling inputs
        schedule_id: Optional[int] = None,
        vehicles: Optional[List[Vehicle]] = None,
        vehicle_states: Optional[Dict[int, VehicleChargeState]] = None,
        energy_requirements: Optional[Dict[int, List[RouteEnergyRequirement]]] = None,
        availability_matrices: Optional[Dict[int, VehicleAvailability]] = None,
        time_slots: Optional[List[datetime]] = None,
        forecast_data: Optional[Dict[datetime, float]] = None,
        price_data: Optional[Dict[datetime, Tuple[float, bool]]] = None,
        site_chargers: Optional[List[ChargerPowerClass]] = None,
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
            site_chargers: List of available chargers at the site
            fix_allocation: Pre-allocated sequences (skips allocation optimization)
            fix_scheduling: If True, disable scheduling (allocation only)
        
        Returns:
            UnifiedOptimizationResult with allocation and/or scheduling outputs
        """
        # Determine effective mode flags
        configured_flags = mode_flags
        if configured_flags is None:
            configured_flags = getattr(self.config, "mode_flags", None)
        if configured_flags is None:
            configured_flags = normalize_mode_input(getattr(self.config, "mode", None))
        active_mode_flags = normalize_mode_input(configured_flags)

        if fix_allocation is not None:
            active_mode_flags = [MODE_FLAG_CHARGE_SCHEDULING]
        if fix_scheduling:
            active_mode_flags = [MODE_FLAG_ALLOCATION]

        run_allocation = MODE_FLAG_ALLOCATION in active_mode_flags
        run_scheduling = MODE_FLAG_CHARGE_SCHEDULING in active_mode_flags
        self.config.enable_charger_allocation = (
            self.config.enable_charger_allocation
            and MODE_FLAG_CHARGER_ALLOCATION in active_mode_flags
            and run_scheduling
        )

        legacy_sequences = sequences
        legacy_route_ids = route_ids
        legacy_sequence_costs = sequence_costs
        if (
            legacy_sequences is None
            and score_vr is not None
            and allocation_routes is not None
            and vehicles is not None
        ):
            legacy_sequence_costs, legacy_sequences = self._legacy_sequences_from_pair_scores(
                vehicles=vehicles,
                routes=allocation_routes,
                score_vr=score_vr,
                feasible_vr=feasible_vr,
            )
            legacy_route_ids = [route.route_id for route in allocation_routes]
        
        if DEBUG_EXPORT_UNIFIED_MATRICES_CSV:
            export_unified_debug_matrices_csv(
                self.config,
                sequences=legacy_sequences,
                route_ids=legacy_route_ids,
                sequence_costs=legacy_sequence_costs,
                vehicles=vehicles,
                vehicle_states=vehicle_states,
                energy_requirements=energy_requirements,
                availability_matrices=availability_matrices,
                time_slots=time_slots,
                forecast_data=forecast_data,
                price_data=price_data,
            )
        
        logger.info(f"[UNIFIED] Starting optimization with mode flags: {active_mode_flags}")
        
        if not IS_HEXALY_ACTIVE:
            logger.warning("Hexaly not active - using greedy fallback")
            return self._greedy_fallback(
                active_mode_flags, legacy_sequences, legacy_route_ids, legacy_sequence_costs,
                schedule_id, vehicles, vehicle_states, energy_requirements,
                availability_matrices, time_slots, price_data,
                fix_allocation
            )
        
        try:
            if run_allocation and not run_scheduling:
                result = self._solve_allocation_only(
                    vehicles=vehicles or [],
                    routes=allocation_routes or [],
                    score_vr=score_vr if score_vr is not None else np.zeros((0, 0)),
                    feasible_vr=feasible_vr if feasible_vr is not None else np.zeros((0, 0), dtype=bool),
                    route_start_times=route_start_times if route_start_times is not None else np.array([]),
                    route_end_times=route_end_times if route_end_times is not None else np.array([]),
                    allocation_metadata=allocation_metadata or {},
                )
                result.mode = active_mode_flags
                return result
            elif run_scheduling and not run_allocation:
                result = self._solve_scheduling_only(
                    schedule_id, vehicles, vehicle_states, energy_requirements,
                    availability_matrices, time_slots, forecast_data, price_data,
                    site_chargers, fix_allocation
                )
                result.mode = active_mode_flags
                return result
            else:
                result = self._solve_integrated(
                    vehicles=vehicles,
                    routes=allocation_routes or [],
                    score_vr=score_vr if score_vr is not None else np.zeros((0, 0)),
                    feasible_vr=feasible_vr if feasible_vr is not None else np.zeros((0, 0), dtype=bool),
                    route_start_times=route_start_times if route_start_times is not None else np.array([]),
                    route_end_times=route_end_times if route_end_times is not None else np.array([]),
                    route_durations=route_durations if route_durations is not None else np.array([]),
                    allocation_metadata=allocation_metadata or {},
                    schedule_id=schedule_id,
                    vehicle_states=vehicle_states,
                    energy_requirements=energy_requirements,
                    availability_matrices=availability_matrices,
                    time_slots=time_slots,
                    forecast_data=forecast_data,
                    price_data=price_data,
                    site_chargers=site_chargers,
                )
                result.mode = active_mode_flags
                return result
        except Exception as e:
            logger.error(f"Unified optimization failed: {e}", exc_info=True)
            return self._greedy_fallback(
                active_mode_flags, legacy_sequences, legacy_route_ids, legacy_sequence_costs,
                schedule_id, vehicles, vehicle_states, energy_requirements,
                availability_matrices, time_slots, price_data,
                fix_allocation
            )
    
    def _determine_mode(
        self,
        sequences: Optional[List[Tuple]],
        route_ids: Optional[List[str]],
        score_vr: Optional[np.ndarray],
        allocation_routes: Optional[List[Route]],
        vehicles: Optional[List[Vehicle]],
        time_slots: Optional[List[datetime]],
        fix_allocation: Optional[List[Tuple]],
        fix_scheduling: bool
    ) -> OptimizationMode:
        """Determine effective optimization mode from inputs."""
        # Explicit mode from config
        if self.config.mode != OptimizationMode.INTEGRATED:
            return self.config.mode
        
        has_legacy_allocation = sequences is not None and route_ids is not None
        has_list_allocation = score_vr is not None and allocation_routes is not None
        has_allocation_data = has_legacy_allocation or has_list_allocation
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
        vehicles: List[Vehicle],
        routes: List[Route],
        score_vr: np.ndarray,
        feasible_vr: np.ndarray,
        route_start_times: np.ndarray,
        route_end_times: np.ndarray,
        allocation_metadata: Dict[str, Any],
    ) -> UnifiedOptimizationResult:
        """Solve allocation-only optimization with native Hexaly list variables."""
        with hx.HexalyOptimizer() as optimizer:
            model = optimizer.model

            routes_by_vehicle, score_term = self._build_list_allocation_block(
                model=model,
                vehicles=vehicles,
                routes=routes,
                score_vr=score_vr,
                feasible_vr=feasible_vr,
                route_start_times=route_start_times,
                route_end_times=route_end_times,
                allocation_metadata=allocation_metadata,
            )

            route_count_term = model.sum([model.count(lst) for lst in routes_by_vehicle])
            objective = self.config.route_count_weight * route_count_term + score_term
            model.maximize(objective)
            model.close()

            optimizer.param.time_limit = self.config.allocation_time_limit
            optimizer.solve()

            selected_sequences = self._extract_list_allocation_solution(
                routes_by_vehicle=routes_by_vehicle,
                vehicles=vehicles,
                routes=routes,
                score_vr=score_vr,
                solution=optimizer.solution,
            )
            total_score = float(sum(seq[2] for seq in selected_sequences))
            routes_allocated = int(
                sum(len(route_sequence) for _, route_sequence, _ in selected_sequences)
            )

            solve_time = optimizer.statistics.running_time
            status = 'optimal' if optimizer.solution.status == hx.HxSolutionStatus.OPTIMAL else 'feasible'
            logger.info(
                f"[UNIFIED:ALLOC] Complete: {len(selected_sequences)} vehicles with assignments, "
                f"{routes_allocated}/{len(routes)} routes, score={total_score:.2f}"
            )

            return UnifiedOptimizationResult(
                mode=[MODE_FLAG_ALLOCATION],
                status=status,
                solve_time_seconds=solve_time,
                selected_sequences=selected_sequences,
                allocation_score=total_score,
                routes_allocated=routes_allocated,
                routes_total=len(routes),
                objective_value=total_score
            )

    def _build_list_allocation_block(
        self,
        model,
        vehicles: List[Vehicle],
        routes: List[Route],
        score_vr: np.ndarray,
        feasible_vr: np.ndarray,
        route_start_times: np.ndarray,
        route_end_times: np.ndarray,
        allocation_metadata: Dict[str, Any],
    ):
        """Build native list allocation variables/constraints and return score term."""
        n_vehicles = len(vehicles)
        n_routes = len(routes)
        routes_by_vehicle = [model.list(n_routes) for _ in range(n_vehicles)]
        score_array = model.array(score_vr)
        route_start_array = model.array(route_start_times)
        route_end_array = model.array(route_end_times)

        # Do NOT force full partition: some routes can be globally infeasible.
        # We enforce "at most one vehicle per route" and maximize assigned count.
        if n_routes > 0:
            for r_idx in range(n_routes):
                model.constraint(
                    model.sum([model.contains(route_list, r_idx) for route_list in routes_by_vehicle]) <= 1
                )

        turnaround_minutes = float(allocation_metadata.get("turnaround_minutes", 0))
        shift_max_minutes = float(allocation_metadata.get("shift_max_minutes", 16 * 60))
        max_routes_per_vehicle = int(
            allocation_metadata.get("max_routes_per_vehicle", max(1, n_routes))
        )
        standard_turnaround, optimal_turnaround, penalty_standard, penalty_optimal = allocation_metadata.get(
            "turnaround_preferred", (75, 90, -2.0, -1.0)
        )

        # Vehicle route count and route membership feasibility.
        for v_idx, route_list in enumerate(routes_by_vehicle):
            model.constraint(model.count(route_list) <= max_routes_per_vehicle)
            for r_idx in range(n_routes):
                if feasible_vr.size and not bool(feasible_vr[v_idx, r_idx]):
                    model.constraint(model.contains(route_list, r_idx) == 0)

            # Strict turnaround between consecutive routes.
            for i in range(1, n_routes):
                prev_route_idx = route_list[i - 1]
                curr_route_idx = route_list[i]
                gap_expr = model.at(route_start_array, curr_route_idx) - model.at(
                    route_end_array, prev_route_idx
                )
                model.constraint(
                    model.iif(
                        i < model.count(route_list),
                        gap_expr >= turnaround_minutes,
                        1,
                    )
                    == 1
                )

            # Strict shift-hours cap.
            if n_routes > 0:
                shift_duration_expr = model.at(
                    route_end_array, route_list[model.count(route_list) - 1]
                ) - model.at(route_start_array, route_list[0])
                model.constraint(
                    model.iif(
                        model.count(route_list) > 0,
                        shift_duration_expr <= shift_max_minutes,
                        1,
                    )
                    == 1
                )

        # Score includes per-route base score + preferred turnaround soft penalties.
        route_score_terms = []
        preferred_turnaround_terms = []
        for v_idx, route_list in enumerate(routes_by_vehicle):
            route_score_terms.append(
                model.sum(
                    route_list,
                    model.lambda_function(
                        # In this Hexaly runtime, lambda arg over list is the element value.
                        lambda route_idx: model.at(score_array, v_idx, route_idx)
                    ),
                )
            )
            for i in range(1, n_routes):
                prev_route_idx = route_list[i - 1]
                curr_route_idx = route_list[i]
                gap_expr = model.at(route_start_array, curr_route_idx) - model.at(
                    route_end_array, prev_route_idx
                )
                preferred_turnaround_terms.append(
                    model.iif(
                        i < model.count(route_list),
                        model.iif(
                            gap_expr < standard_turnaround,
                            penalty_standard,
                            model.iif(gap_expr < optimal_turnaround, penalty_optimal, 0.0),
                        ),
                        0.0,
                    )
                )

        score_term = model.sum(route_score_terms) if route_score_terms else 0.0
        if preferred_turnaround_terms:
            score_term = score_term + model.sum(preferred_turnaround_terms)
        return routes_by_vehicle, score_term

    def _extract_list_indices(self, list_var, solution=None) -> List[int]:
        """Extract integer values from a Hexaly list variable."""
        _ = solution  # Kept for call-site compatibility across Hexaly versions.
        solution_value = getattr(list_var, "value", None)
        if isinstance(solution_value, (list, tuple)):
            return [int(v) for v in solution_value]
        # Some Hexaly versions expose list variables as HxExpression without list APIs
        # when no feasible solution is available. Return empty assignment instead of raising.
        return []

    def _extract_list_allocation_solution(
        self,
        routes_by_vehicle: List,
        vehicles: List[Vehicle],
        routes: List[Route],
        score_vr: np.ndarray,
        solution=None,
    ) -> List[Tuple]:
        """Extract selected route lists into legacy selected_sequences format."""
        selected_sequences: List[Tuple] = []
        for v_idx, route_list in enumerate(routes_by_vehicle):
            route_indices = self._extract_list_indices(route_list, solution=solution)
            if not route_indices:
                continue
            assigned_routes = [routes[r_idx] for r_idx in route_indices]
            seq_score = float(sum(score_vr[v_idx, r_idx] for r_idx in route_indices))
            selected_sequences.append((vehicles[v_idx].vehicle_id, assigned_routes, seq_score))
        return selected_sequences

    def _legacy_sequences_from_pair_scores(
        self,
        vehicles: List[Vehicle],
        routes: List[Route],
        score_vr: np.ndarray,
        feasible_vr: Optional[np.ndarray],
    ) -> Tuple[np.ndarray, List[Tuple]]:
        """Build legacy single-route sequence representation from pairwise scores."""
        sequences: List[Tuple] = []
        sequence_costs: List[float] = []
        for v_idx, vehicle in enumerate(vehicles):
            for r_idx, route in enumerate(routes):
                if feasible_vr is not None and feasible_vr.size and not bool(feasible_vr[v_idx, r_idx]):
                    continue
                score = float(score_vr[v_idx, r_idx])
                sequences.append((vehicle.vehicle_id, [route], score))
                sequence_costs.append(score)
        return np.array(sequence_costs, dtype=float), sequences

    def _build_vehicle_charging_segments(
        self,
        vehicles: List[Vehicle],
        energy_requirements: Dict[int, List[RouteEnergyRequirement]],
        planning_start: datetime,
        planning_horizon_minutes: int,
    ) -> Dict[int, List[Tuple[int, int, str]]]:
        """Build charging segments: pre-first, between-routes, post-last."""
        segments_by_vehicle: Dict[int, List[Tuple[int, int, str]]] = {}

        for vehicle in vehicles:
            reqs = sorted(
                energy_requirements.get(vehicle.vehicle_id, []),
                key=lambda x: x.plan_start_date_time,
            )
            segments: List[Tuple[int, int, str]] = []
            if not reqs:
                segments.append((0, planning_horizon_minutes, "full_window"))
                segments_by_vehicle[vehicle.vehicle_id] = segments
                continue

            first_start = int((reqs[0].plan_start_date_time - planning_start).total_seconds() / 60.0)
            if first_start > 0:
                segments.append((0, min(first_start, planning_horizon_minutes), "pre_first"))

            for idx in range(len(reqs) - 1):
                seg_start = int((reqs[idx].plan_end_date_time - planning_start).total_seconds() / 60.0)
                seg_end = int((reqs[idx + 1].plan_start_date_time - planning_start).total_seconds() / 60.0)
                if seg_end > seg_start:
                    segments.append((max(0, seg_start), min(seg_end, planning_horizon_minutes), f"between_{idx}"))

            last_end = int((reqs[-1].plan_end_date_time - planning_start).total_seconds() / 60.0)
            if planning_horizon_minutes > last_end:
                segments.append((max(0, last_end), planning_horizon_minutes, "post_last"))

            # Filter zero/negative segments
            segments_by_vehicle[vehicle.vehicle_id] = [
                (s, e, label) for s, e, label in segments if e > s
            ]
        return segments_by_vehicle

    def _segment_index_for_slot(
        self,
        segments: List[Tuple[int, int, str]],
        slot_minute: int,
    ) -> Optional[int]:
        for idx, (start_min, end_min, _label) in enumerate(segments):
            if start_min <= slot_minute < end_min:
                return idx
        return None
    
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
        site_chargers: Optional[List[ChargerPowerClass]] = None,
        fix_allocation: Optional[List[Tuple]] = None
    ) -> UnifiedOptimizationResult:
        """Solve scheduling-only optimization with fixed/pre-allocated routes."""
        with hx.HexalyOptimizer() as optimizer:
            model = optimizer.model
            
            n_slots = len(time_slots)
            n_vehicles = len(vehicles)
            planning_horizon_minutes = n_slots * 30
            planning_start = time_slots[0] if time_slots else datetime.now()
            segments_by_vehicle = self._build_vehicle_charging_segments(
                vehicles=vehicles,
                energy_requirements=energy_requirements,
                planning_start=planning_start,
                planning_horizon_minutes=planning_horizon_minutes,
            )
            planning_horizon_minutes = n_slots * 30
            planning_start = time_slots[0] if time_slots else datetime.now()
            segments_by_vehicle = self._build_vehicle_charging_segments(
                vehicles=vehicles,
                energy_requirements=energy_requirements,
                planning_start=planning_start,
                planning_horizon_minutes=planning_horizon_minutes,
            )
            
            logger.info(
                f"[UNIFIED:SCHED] Building model: {n_vehicles} vehicles, {n_slots} slots"
            )
            
            vehicle_to_idx = {v.vehicle_id: idx for idx, v in enumerate(vehicles)}
            
            # Calculate planning horizon in minutes
            planning_horizon_minutes = n_slots * 30
            planning_start = time_slots[0] if time_slots else datetime.now()
            segments_by_vehicle = self._build_vehicle_charging_segments(
                vehicles=vehicles,
                energy_requirements=energy_requirements,
                planning_start=planning_start,
                planning_horizon_minutes=planning_horizon_minutes,
            )
            
            # Decision variables: interval and charger choice per segment
            charging_sessions = []
            energy_charged = []
            power_class_choice = []
            
            for v_idx, vehicle in enumerate(vehicles):
                state = vehicle_states.get(vehicle.vehicle_id)
                vehicle_segments = segments_by_vehicle.get(vehicle.vehicle_id, [(0, planning_horizon_minutes, "full_window")])
                vehicle_sessions = []
                vehicle_energies = []
                vehicle_choices = []

                if not state:
                    for seg_start, seg_end, _label in vehicle_segments:
                        session = model.interval_var(seg_start, seg_end)
                        session.duration_min = 0
                        session.duration_max = 0
                        vehicle_sessions.append(session)
                        vehicle_energies.append(model.float(0, 0))
                        vehicle_choices.append(model.int(0, 0))
                    charging_sessions.append(vehicle_sessions)
                    energy_charged.append(vehicle_energies)
                    power_class_choice.append(vehicle_choices)
                    continue
                
                # Calculate energy bounds
                max_energy_needed = max(0.0, state.battery_capacity_kwh - state.current_soc_kwh)
                charge_rate_kw = state.ac_charge_rate_kw
                
                # Calculate max duration (energy needed / charge rate) in minutes
                if max_energy_needed > 0 and charge_rate_kw > 0:
                    max_duration_minutes = int((max_energy_needed / charge_rate_kw) * 60)
                else:
                    max_duration_minutes = 0
                
                for seg_start, seg_end, _label in vehicle_segments:
                    session = model.interval_var(seg_start, seg_end)
                    session.duration_min = 0
                    session.duration_max = min(max_duration_minutes, max(0, seg_end - seg_start))
                    vehicle_sessions.append(session)

                    energy = model.float(0, max_energy_needed)
                    if charge_rate_kw > 0:
                        model.constraint(energy == (charge_rate_kw / 60.0) * model.length(session))
                    else:
                        model.constraint(energy == 0)
                    vehicle_energies.append(energy)

                    if site_chargers and self.config.enable_charger_allocation:
                        n_charger_classes = len(site_chargers)
                        vehicle_choices.append(model.int(0, n_charger_classes - 1))
                    else:
                        vehicle_choices.append(model.int(0, 0))

                charging_sessions.append(vehicle_sessions)
                energy_charged.append(vehicle_energies)
                power_class_choice.append(vehicle_choices)
            
            if site_chargers and self.config.enable_charger_allocation:
                n_charger_classes = len(site_chargers)
                total_chargers = sum(pc.count for pc in site_chargers)
                logger.info(f"[UNIFIED:SCHED] Using interval-based charger allocation: {n_vehicles} vehicles x {n_charger_classes} power classes ({total_chargers} chargers)")
            elif site_chargers and not self.config.enable_charger_allocation:
                logger.info(f"[UNIFIED:SCHED] Charger allocation DISABLED by config (site has {len(site_chargers)} power classes)")
            
            # Build objective: minimize charging cost + shortfall penalty
            # Calculate average price for simplicity (weighted by time would be more accurate)
            avg_price = sum(price_data.get(slot, (0.15, False))[0] for slot in time_slots) / len(time_slots) if time_slots else 0.15
            
            cost_terms = []
            for v_idx in range(n_vehicles):
                # Cost = average_price * energy_charged
                cost = avg_price * model.sum(energy_charged[v_idx]) if energy_charged[v_idx] else 0
                cost_terms.append(cost)
            
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
                    total_vehicle_energy = model.sum(energy_charged[v_idx]) if energy_charged[v_idx] else 0
                    model.constraint(
                        shortfall_v >= target_soc_kwh - state.current_soc_kwh - total_vehicle_energy
                    )
                    shortfall_terms.append(self.config.target_soc_shortfall_penalty * shortfall_v)
            
            # Optional makespan penalty
            makespan_terms = []
            if self.config.makespan_penalty_weight > 0:
                for v_idx in range(n_vehicles):
                    for session in charging_sessions[v_idx]:
                        makespan_terms.append(model.end(session))
            
            # Combine objective terms
            objective = model.sum(cost_terms)
            if shortfall_terms:
                objective = objective + model.sum(shortfall_terms)
            if makespan_terms:
                makespan = model.max(makespan_terms)
                objective = objective + self.config.makespan_penalty_weight * makespan
            
            model.minimize(objective)
            
            # Constraints
            self._add_interval_scheduling_constraints(
                model, vehicles, vehicle_states, energy_requirements,
                availability_matrices, time_slots, forecast_data,
                charging_sessions, energy_charged, vehicle_to_idx, site_chargers,
                power_class_choice, planning_horizon_minutes, segments_by_vehicle
            )
            
            model.close()
            
            # Solve
            optimizer.param.time_limit = self.config.scheduling_time_limit
            optimizer.solve()
            
            # Extract solution
            solve_time = optimizer.statistics.running_time
            status = str(optimizer.solution.status)
            
            vehicle_schedules, total_cost, total_energy = self._extract_interval_solution(
                schedule_id, vehicles, vehicle_states, energy_requirements,
                availability_matrices, time_slots, price_data, charging_sessions, energy_charged, vehicle_to_idx,
                site_chargers, power_class_choice, planning_horizon_minutes, model, segments_by_vehicle
            )
            
            logger.info(
                f"[UNIFIED:SCHED] Complete: {len(vehicle_schedules)} vehicles, "
                f"cost={total_cost:.2f}, energy={total_energy:.2f} kWh"
            )
            
            return UnifiedOptimizationResult(
                mode=[MODE_FLAG_CHARGE_SCHEDULING],
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
        vehicles: List[Vehicle],
        routes: List[Route],
        score_vr: np.ndarray,
        feasible_vr: np.ndarray,
        route_start_times: np.ndarray,
        route_end_times: np.ndarray,
        route_durations: np.ndarray,
        allocation_metadata: Dict[str, Any],
        schedule_id: int,
        vehicle_states: Dict[int, VehicleChargeState],
        energy_requirements: Dict[int, List[RouteEnergyRequirement]],
        availability_matrices: Dict[int, VehicleAvailability],
        time_slots: List[datetime],
        forecast_data: Dict[datetime, float],
        price_data: Dict[datetime, Tuple[float, bool]],
        site_chargers: Optional[List[ChargerPowerClass]] = None
    ) -> UnifiedOptimizationResult:
        """
        Solve integrated allocation + scheduling optimization.
        
        Uses weighted sum objective:
            maximize: α * allocation_score - β * charging_cost
        """
        with hx.HexalyOptimizer() as optimizer:
            model = optimizer.model

            n_routes = len(routes)
            n_slots = len(time_slots)
            n_vehicles = len(vehicles)
            planning_horizon_minutes = n_slots * 30
            planning_start = time_slots[0] if time_slots else datetime.now()
            segments_by_vehicle = self._build_vehicle_charging_segments(
                vehicles=vehicles,
                energy_requirements=energy_requirements,
                planning_start=planning_start,
                planning_horizon_minutes=planning_horizon_minutes,
            )
            planning_horizon_minutes = n_slots * 30
            planning_start = time_slots[0] if time_slots else datetime.now()
            segments_by_vehicle = self._build_vehicle_charging_segments(
                vehicles=vehicles,
                energy_requirements=energy_requirements,
                planning_start=planning_start,
                planning_horizon_minutes=planning_horizon_minutes,
            )
            planning_horizon_minutes = n_slots * 30
            planning_start = time_slots[0] if time_slots else datetime.now()
            segments_by_vehicle = self._build_vehicle_charging_segments(
                vehicles=vehicles,
                energy_requirements=energy_requirements,
                planning_start=planning_start,
                planning_horizon_minutes=planning_horizon_minutes,
            )

            logger.info(
                f"[UNIFIED:INTEGRATED] Building model: "
                f"{n_routes} routes, "
                f"{n_vehicles} vehicles, {n_slots} slots"
            )

            vehicle_to_idx = {v.vehicle_id: idx for idx, v in enumerate(vehicles)}

            routes_by_vehicle, allocation_score_term = self._build_list_allocation_block(
                model=model,
                vehicles=vehicles,
                routes=routes,
                score_vr=score_vr,
                feasible_vr=feasible_vr,
                route_start_times=route_start_times,
                route_end_times=route_end_times,
                allocation_metadata=allocation_metadata,
            )
            
            # ===== SCHEDULING VARIABLES =====
            # Calculate planning horizon in minutes
            planning_horizon_minutes = n_slots * 30
            planning_start = time_slots[0] if time_slots else datetime.now()
            segments_by_vehicle = self._build_vehicle_charging_segments(
                vehicles=vehicles,
                energy_requirements=energy_requirements,
                planning_start=planning_start,
                planning_horizon_minutes=planning_horizon_minutes,
            )
            
            # Interval variables for charging sessions
            charging_sessions = []
            energy_charged = []
            power_class_choice = []
            
            for v_idx, vehicle in enumerate(vehicles):
                state = vehicle_states.get(vehicle.vehicle_id)
                vehicle_segments = segments_by_vehicle.get(vehicle.vehicle_id, [(0, planning_horizon_minutes, "full_window")])
                vehicle_sessions = []
                vehicle_energies = []
                vehicle_choices = []

                if not state:
                    for seg_start, seg_end, _label in vehicle_segments:
                        session = model.interval_var(seg_start, seg_end)
                        session.duration_min = 0
                        session.duration_max = 0
                        vehicle_sessions.append(session)
                        vehicle_energies.append(model.float(0, 0))
                        vehicle_choices.append(model.int(0, 0))
                    charging_sessions.append(vehicle_sessions)
                    energy_charged.append(vehicle_energies)
                    power_class_choice.append(vehicle_choices)
                    continue
                
                # Calculate energy bounds
                max_energy_needed = max(0.0, state.battery_capacity_kwh - state.current_soc_kwh)
                charge_rate_kw = state.ac_charge_rate_kw
                
                # Calculate max duration (energy needed / charge rate) in minutes
                if max_energy_needed > 0 and charge_rate_kw > 0:
                    max_duration_minutes = int((max_energy_needed / charge_rate_kw) * 60)
                else:
                    max_duration_minutes = 0
                
                for seg_start, seg_end, _label in vehicle_segments:
                    session = model.interval_var(seg_start, seg_end)
                    session.duration_min = 0
                    session.duration_max = min(max_duration_minutes, max(0, seg_end - seg_start))
                    vehicle_sessions.append(session)

                    energy = model.float(0, max_energy_needed)
                    if charge_rate_kw > 0:
                        model.constraint(energy == (charge_rate_kw / 60.0) * model.length(session))
                    else:
                        model.constraint(energy == 0)
                    vehicle_energies.append(energy)

                    if site_chargers and self.config.enable_charger_allocation:
                        n_charger_classes = len(site_chargers)
                        vehicle_choices.append(model.int(0, n_charger_classes - 1))
                    else:
                        vehicle_choices.append(model.int(0, 0))

                charging_sessions.append(vehicle_sessions)
                energy_charged.append(vehicle_energies)
                power_class_choice.append(vehicle_choices)
            
            if site_chargers and self.config.enable_charger_allocation:
                n_charger_classes = len(site_chargers)
                total_chargers = sum(pc.count for pc in site_chargers)
                logger.info(f"[UNIFIED:INTEGRATED] Using interval-based charger allocation: {n_vehicles} vehicles x {n_charger_classes} power classes ({total_chargers} chargers)")
            elif site_chargers and not self.config.enable_charger_allocation:
                logger.info(f"[UNIFIED:INTEGRATED] Charger allocation DISABLED by config (site has {len(site_chargers)} power classes)")
            
            # ===== ROUTE EXECUTION INTERVALS =====
            route_intervals: Dict[int, List] = {}
            for v_idx, vehicle in enumerate(vehicles):
                vehicle_route_intervals = []
                route_list = routes_by_vehicle[v_idx]
                for r_idx in range(n_routes):
                    route_iv = model.interval_var(0, planning_horizon_minutes)
                    duration = int(route_durations[r_idx]) if len(route_durations) > r_idx else 0
                    route_iv.duration_min = duration
                    route_iv.duration_max = duration
                    model.constraint(model.if_present(route_iv) == model.contains(route_list, r_idx))
                    vehicle_route_intervals.append(route_iv)
                route_intervals[vehicle.vehicle_id] = vehicle_route_intervals
            
            # ===== NO-OVERLAP CONSTRAINTS =====
            # Vehicle cannot charge and execute routes simultaneously
            for v_idx, vehicle in enumerate(vehicles):
                if vehicle.vehicle_id not in route_intervals:
                    continue
                
                # Add no-overlap between charging session and each route
                for route_iv in route_intervals[vehicle.vehicle_id]:
                    for charge_iv in charging_sessions[v_idx]:
                        model.constraint(model.no_overlap(charge_iv, route_iv))
            
            logger.info(f"[UNIFIED:INTEGRATED] Added route execution intervals and no-overlap constraints for {len(route_intervals)} vehicles")
            
            # ===== OBJECTIVE: WEIGHTED SUM =====
            
            # Allocation term: route count + score term
            route_count_term = model.sum([model.count(lst) for lst in routes_by_vehicle])
            allocation_term = self.config.route_count_weight * route_count_term + allocation_score_term
            
            # Scheduling term: charging cost
            # Calculate average price for simplicity
            avg_price = sum(price_data.get(slot, (0.15, False))[0] for slot in time_slots) / len(time_slots) if time_slots else 0.15
            
            cost_terms = []
            for v_idx in range(n_vehicles):
                # Cost = average_price * energy_charged
                cost = avg_price * (model.sum(energy_charged[v_idx]) if energy_charged[v_idx] else 0)
                cost_terms.append(cost)
            
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
                    total_vehicle_energy = model.sum(energy_charged[v_idx]) if energy_charged[v_idx] else 0
                    model.constraint(shortfall_v >= target_soc_kwh - state.current_soc_kwh - total_vehicle_energy)
                    shortfall_terms.append(self.config.target_soc_shortfall_penalty * shortfall_v)
            
            scheduling_term = model.sum(cost_terms)
            if shortfall_terms:
                scheduling_term = scheduling_term + model.sum(shortfall_terms)
            
            # Optional makespan penalty
            if self.config.makespan_penalty_weight > 0:
                makespan_terms = []
                for v_idx in range(n_vehicles):
                    for session in charging_sessions[v_idx]:
                        makespan_terms.append(model.end(session))
                if makespan_terms:
                    makespan = model.max(makespan_terms)
                    scheduling_term = scheduling_term + self.config.makespan_penalty_weight * makespan
            
            # Combined weighted sum: maximize allocation - cost
            # Note: We maximize, so subtract the cost term
            combined_objective = (
                self.config.allocation_score_weight * allocation_term -
                self.config.scheduling_cost_weight * scheduling_term
            )
            model.maximize(combined_objective)
            
            # ===== SCHEDULING CONSTRAINTS =====
            self._add_interval_scheduling_constraints(
                model, vehicles, vehicle_states, energy_requirements,
                availability_matrices, time_slots, forecast_data,
                charging_sessions, energy_charged, vehicle_to_idx, site_chargers,
                power_class_choice, planning_horizon_minutes, segments_by_vehicle
            )
            
            model.close()
            
            # Solve
            optimizer.param.time_limit = self.config.integrated_time_limit
            optimizer.solve()
            
            selected_sequences = self._extract_list_allocation_solution(
                routes_by_vehicle=routes_by_vehicle,
                vehicles=vehicles,
                routes=routes,
                score_vr=score_vr,
                solution=optimizer.solution,
            )
            allocation_score = float(sum(seq[2] for seq in selected_sequences))
            routes_allocated = int(
                sum(len(route_sequence) for _, route_sequence, _ in selected_sequences)
            )
            
            # Extract scheduling solution
            vehicle_schedules, total_cost, total_energy = self._extract_interval_solution(
                schedule_id, vehicles, vehicle_states, energy_requirements,
                availability_matrices, time_slots, price_data, charging_sessions, energy_charged, vehicle_to_idx,
                site_chargers, power_class_choice, planning_horizon_minutes, model, segments_by_vehicle
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
                mode=[MODE_FLAG_ALLOCATION, MODE_FLAG_CHARGE_SCHEDULING]
                + ([MODE_FLAG_CHARGER_ALLOCATION] if self.config.enable_charger_allocation else []),
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
        vehicle_to_idx: Dict[int, int],
        site_chargers: Optional[List[ChargerPowerClass]] = None,
        charger_assigned: Optional[List[List]] = None,
        charger_power_to_idx: Optional[Dict[float, int]] = None,
        segments_by_vehicle: Optional[Dict[int, List[Tuple[int, int, str]]]] = None,
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
        
        # ===== CHARGER ALLOCATION CONSTRAINTS =====
        if site_chargers and charger_assigned and charger_power_to_idx:
            n_charger_classes = len(site_chargers)
            total_chargers = sum(pc.count for pc in site_chargers)
            logger.info(f"[UNIFIED] Adding charger allocation constraints for {n_charger_classes} power classes ({total_chargers} total chargers)")
            
            # Log details of each charger power class and their counts
            for pc_idx, power_class in enumerate(site_chargers):
                charger_type = 'DC' if power_class.is_dc else 'AC'
                logger.info(
                    f"[UNIFIED]   Power Class {pc_idx}: {power_class.max_power_kw}kW ({charger_type}) - "
                    f"Count: {power_class.count}, Charger IDs: {power_class.charger_ids}"
                )
            
            # Helper: Find power class index for a given charger_id
            def find_power_class_for_charger(charger_id: int) -> Optional[int]:
                for pc_idx, power_class in enumerate(site_chargers):
                    if charger_id in power_class.charger_ids:
                        return pc_idx
                return None
            
            # 7. Fixed Charger Power Class (Already Connected)
            # If vehicle is connected, assign to the power class of that charger
            fixed_vehicle_count = 0
            for v_idx, vehicle in enumerate(vehicles):
                state = vehicle_states.get(vehicle.vehicle_id)
                if state and state.charger_id is not None:
                    pc_idx = find_power_class_for_charger(state.charger_id)
                    if pc_idx is not None:
                        seg_assign = charger_assigned[v_idx]
                        if seg_assign and isinstance(seg_assign[0], list):
                            for s_idx in range(len(seg_assign)):
                                model.constraint(seg_assign[s_idx][pc_idx] == 1)
                                for other_pc_idx in range(n_charger_classes):
                                    if other_pc_idx != pc_idx:
                                        model.constraint(seg_assign[s_idx][other_pc_idx] == 0)
                        else:
                            model.constraint(seg_assign[pc_idx] == 1)
                            for other_pc_idx in range(n_charger_classes):
                                if other_pc_idx != pc_idx:
                                    model.constraint(seg_assign[other_pc_idx] == 0)
                        logger.debug(f"[UNIFIED] Fixed vehicle {vehicle.vehicle_id} to power class {site_chargers[pc_idx].max_power_kw}kW")
                        fixed_vehicle_count += 1
            
            if fixed_vehicle_count > 0:
                logger.info(f"[UNIFIED] Fixed {fixed_vehicle_count} vehicles to their currently connected charger power class")
            
            # 8. One Power Class Per Segment
            logger.info("[UNIFIED] Adding one-power-class-per-segment continuity constraints")
            for v_idx in range(n_vehicles):
                seg_assign = charger_assigned[v_idx]
                # nested [segment][class] in segment-aware mode, legacy [class] otherwise
                if seg_assign and isinstance(seg_assign[0], list):
                    for s_idx in range(len(seg_assign)):
                        model.constraint(model.sum(seg_assign[s_idx]) == 1)
                else:
                    model.constraint(model.sum(seg_assign) == 1)
            
            # 9. Charger Capacity Constraint
            # Link charge_power[t][v] to assigned power class's max_power
            for t_idx in range(n_slots):
                for v_idx in range(n_vehicles):
                    # Sum over all power classes: charge_power <= sum of (class_max * assigned_flag)
                    state = vehicle_states.get(vehicles[v_idx].vehicle_id)
                    vehicle_max_rate = state.ac_charge_rate_kw if state else 50.0
                    seg_assign = charger_assigned[v_idx]
                    if seg_assign and isinstance(seg_assign[0], list):
                        slot_min = t_idx * 30
                        segs = (segments_by_vehicle or {}).get(vehicles[v_idx].vehicle_id, [])
                        seg_idx = self._segment_index_for_slot(segs, slot_min)
                        if seg_idx is None:
                            model.constraint(charge_power[t_idx][v_idx] == 0)
                            continue
                        class_flags = seg_assign[seg_idx]
                    else:
                        class_flags = seg_assign
                    max_power_sum = model.sum([
                        min(vehicle_max_rate, site_chargers[pc_idx].max_power_kw) * class_flags[pc_idx]
                        for pc_idx in range(n_charger_classes)
                    ])
                    model.constraint(charge_power[t_idx][v_idx] <= max_power_sum)
            
            # 10. Nighttime Charger Continuity Constraint
            # If vehicle used a charger during last nighttime period, continue using same power class
            nighttime_continuity_count = 0
            for v_idx, vehicle in enumerate(vehicles):
                state = vehicle_states.get(vehicle.vehicle_id)
                if state and state.last_nighttime_charger_id is not None and state.charger_id is None:
                    # Vehicle charged during previous nighttime but not currently connected
                    # Must use same power class if charging during current nighttime hours
                    pc_idx = find_power_class_for_charger(state.last_nighttime_charger_id)
                    if pc_idx is not None:
                        # Check if any nighttime slots exist in planning window
                        has_nighttime = False
                        for t_idx in range(n_slots):
                            hour = time_slots[t_idx].hour
                            if (19 <= hour) or (hour < 7):
                                has_nighttime = True
                                break
                        
                        if has_nighttime:
                            # If charging during nighttime, must use last_nighttime power class
                            # This is enforced by fixing the power class assignment
                            seg_assign = charger_assigned[v_idx]
                            if seg_assign and isinstance(seg_assign[0], list):
                                for s_idx in range(len(seg_assign)):
                                    model.constraint(seg_assign[s_idx][pc_idx] == 1)
                                    for other_pc_idx in range(n_charger_classes):
                                        if other_pc_idx != pc_idx:
                                            model.constraint(seg_assign[s_idx][other_pc_idx] == 0)
                            else:
                                model.constraint(seg_assign[pc_idx] == 1)
                                for other_pc_idx in range(n_charger_classes):
                                    if other_pc_idx != pc_idx:
                                        model.constraint(seg_assign[other_pc_idx] == 0)
                            logger.debug(f"[UNIFIED] Enforcing nighttime continuity: vehicle {vehicle.vehicle_id} must use {site_chargers[pc_idx].max_power_kw}kW power class")
                            nighttime_continuity_count += 1
            
            if nighttime_continuity_count > 0:
                logger.info(f"[UNIFIED] Applied nighttime charger continuity constraint to {nighttime_continuity_count} vehicles")
            
            # 11. Time-Slot Charger Capacity Constraint
            # At each time slot, number of vehicles assigned to a power class that are available
            # (could potentially be connected/charging) cannot exceed the count of chargers in that power class
            logger.info(f"[UNIFIED] Adding time-slot charger count capacity constraints:")
            for pc_idx in range(n_charger_classes):
                power_class = site_chargers[pc_idx]
                logger.info(
                    f"[UNIFIED]   Power Class {pc_idx} ({power_class.max_power_kw}kW): "
                    f"max {power_class.count} vehicles can be assigned/connected per slot"
                )
            
            for t_idx in range(n_slots):
                for pc_idx in range(n_charger_classes):
                    # Count vehicles assigned to this power class that are available at this time slot
                    # A vehicle occupies a charger if: assigned to power class AND available at this slot
                    # (availability means the vehicle could be connected, regardless of charge_power)
                    
                    vehicles_occupying_charger = []
                    for v_idx in range(n_vehicles):
                        vehicle = vehicles[v_idx]
                        availability = availability_matrices.get(vehicle.vehicle_id)
                        
                        # If vehicle is available at this slot and assigned to this power class,
                        # it occupies a charger spot
                        if availability and availability.availability_matrix[t_idx]:
                            seg_assign = charger_assigned[v_idx]
                            if seg_assign and isinstance(seg_assign[0], list):
                                segs = (segments_by_vehicle or {}).get(vehicle.vehicle_id, [])
                                seg_idx = self._segment_index_for_slot(segs, t_idx * 30)
                                if seg_idx is not None:
                                    vehicles_occupying_charger.append(seg_assign[seg_idx][pc_idx])
                            else:
                                vehicles_occupying_charger.append(seg_assign[pc_idx])
                    
                    if vehicles_occupying_charger:
                        charger_count = site_chargers[pc_idx].count
                        model.constraint(model.sum(vehicles_occupying_charger) <= charger_count)
            
            logger.info(
                f"[UNIFIED] Added {n_slots * n_charger_classes} time-slot charger count constraints "
                f"({n_slots} slots × {n_charger_classes} power classes)"
            )
    
    def _add_interval_scheduling_constraints(
        self,
        model,
        vehicles: List[Vehicle],
        vehicle_states: Dict[int, VehicleChargeState],
        energy_requirements: Dict[int, List[RouteEnergyRequirement]],
        availability_matrices: Dict[int, VehicleAvailability],
        time_slots: List[datetime],
        forecast_data: Dict[datetime, float],
        charging_sessions: List,
        energy_charged: List,
        vehicle_to_idx: Dict[int, int],
        site_chargers: Optional[List[ChargerPowerClass]] = None,
        power_class_choice: Optional[List] = None,
        planning_horizon_minutes: int = 0,
        segments_by_vehicle: Optional[Dict[int, List[Tuple[int, int, str]]]] = None,
    ):
        """Add interval-based scheduling constraints to model."""
        n_vehicles = len(vehicles)
        
        # 1. Route Energy Requirements (hard constraint)
        # Charging must provide sufficient energy before route starts
        planning_start = time_slots[0] if time_slots else datetime.now()
        for vehicle in vehicles:
            v_idx = vehicle_to_idx.get(vehicle.vehicle_id)
            if v_idx is None:
                continue
            
            state = vehicle_states.get(vehicle.vehicle_id)
            requirements = energy_requirements.get(vehicle.vehicle_id, [])
            
            if not state or not requirements:
                continue
            
            for requirement in requirements:
                # Convert route start time to minutes from planning start
                time_diff = (requirement.plan_start_date_time - planning_start).total_seconds() / 60.0
                route_start_minutes = int(time_diff)
                
                if route_start_minutes <= 0:
                    continue
                
                required_energy = max(
                    0.0,
                    requirement.cumulative_energy_kwh - state.current_soc_kwh
                )
                
                if required_energy > 0:
                    relevant_energy_terms = []
                    for seg_idx, session in enumerate(charging_sessions[v_idx]):
                        relevant_energy_terms.append(
                            model.iif(
                                model.end(session) <= route_start_minutes,
                                energy_charged[v_idx][seg_idx],
                                0.0,
                            )
                        )
                    if relevant_energy_terms:
                        model.constraint(model.sum(relevant_energy_terms) >= required_energy)
        
        # 2. Maximum SOC (can't charge beyond battery capacity)
        # Already handled in energy_charged variable bounds
        
        # 3. Vehicle Availability Windows
        # Convert availability matrix to interval time bounds
        for v_idx, vehicle in enumerate(vehicles):
            availability = availability_matrices.get(vehicle.vehicle_id)
            if not availability:
                continue
            
            # Find availability windows (contiguous available periods)
            avail_matrix = availability.availability_matrix
            if not avail_matrix or len(avail_matrix) != len(time_slots):
                continue
            
            # Charging sessions must stay in globally available bounds.
            first_available = next((idx for idx, flag in enumerate(avail_matrix) if flag), None)
            last_available = next((idx for idx in range(len(avail_matrix) - 1, -1, -1) if avail_matrix[idx]), None)
            if first_available is not None and last_available is not None:
                earliest_start = first_available * 30
                latest_end = (last_available + 1) * 30
                for session in charging_sessions[v_idx]:
                    model.constraint(model.start(session) >= earliest_start)
                    model.constraint(model.end(session) <= latest_end)
        
        # 4. Charger Power Class Constraints
        if site_chargers and power_class_choice and self.config.enable_charger_allocation:
            n_charger_classes = len(site_chargers)
            total_chargers = sum(pc.count for pc in site_chargers)
            logger.info(f"[UNIFIED] Adding interval-based charger allocation constraints for {n_charger_classes} power classes ({total_chargers} total chargers)")
            
            # Log charger power class details
            for pc_idx, power_class in enumerate(site_chargers):
                charger_type = 'DC' if power_class.is_dc else 'AC'
                logger.info(
                    f"[UNIFIED]   Power Class {pc_idx}: {power_class.max_power_kw}kW ({charger_type}) - "
                    f"Count: {power_class.count}, Charger IDs: {power_class.charger_ids}"
                )
            
            # Helper: Find power class index for a given charger_id
            def find_power_class_for_charger(charger_id: int) -> Optional[int]:
                for pc_idx, power_class in enumerate(site_chargers):
                    if charger_id in power_class.charger_ids:
                        return pc_idx
                return None
            
            # 4a. Fixed Charger Power Class (Already Connected), pinned per segment.
            fixed_vehicle_count = 0
            for v_idx, vehicle in enumerate(vehicles):
                state = vehicle_states.get(vehicle.vehicle_id)
                if state and state.charger_id is not None:
                    pc_idx = find_power_class_for_charger(state.charger_id)
                    if pc_idx is not None:
                        for seg_choice in power_class_choice[v_idx]:
                            model.constraint(seg_choice == pc_idx)
                        logger.debug(f"[UNIFIED] Fixed vehicle {vehicle.vehicle_id} to power class {site_chargers[pc_idx].max_power_kw}kW")
                        fixed_vehicle_count += 1
            
            if fixed_vehicle_count > 0:
                logger.info(f"[UNIFIED] Fixed {fixed_vehicle_count} vehicles to their currently connected charger power class")
            
            # 4b. Nighttime continuity pinning per nighttime-overlapping segment.
            nighttime_continuity_count = 0
            for v_idx, vehicle in enumerate(vehicles):
                state = vehicle_states.get(vehicle.vehicle_id)
                if state and state.last_nighttime_charger_id is not None and state.charger_id is None:
                    pc_idx = find_power_class_for_charger(state.last_nighttime_charger_id)
                    if pc_idx is not None:
                        segments = (segments_by_vehicle or {}).get(vehicle.vehicle_id, [])
                        for seg_idx, (seg_start, seg_end, _label) in enumerate(segments):
                            overlaps_night = False
                            for slot in time_slots:
                                minute = int((slot - planning_start).total_seconds() / 60.0)
                                if seg_start <= minute < seg_end and ((19 <= slot.hour) or (slot.hour < 7)):
                                    overlaps_night = True
                                    break
                            if overlaps_night:
                                model.constraint(power_class_choice[v_idx][seg_idx] == pc_idx)
                                nighttime_continuity_count += 1
            
            if nighttime_continuity_count > 0:
                logger.info(f"[UNIFIED] Applied nighttime charger continuity constraint to {nighttime_continuity_count} vehicles")
            
            # 4c. Charger Power Limit based on assigned power class per segment.
            for v_idx, vehicle in enumerate(vehicles):
                state = vehicle_states.get(vehicle.vehicle_id)
                if not state:
                    continue
                
                vehicle_max_rate = state.ac_charge_rate_kw
                for seg_idx, session in enumerate(charging_sessions[v_idx]):
                    max_power_expr = model.sum([
                        min(vehicle_max_rate, site_chargers[pc_idx].max_power_kw) *
                        model.iif(power_class_choice[v_idx][seg_idx] == pc_idx, 1, 0)
                        for pc_idx in range(n_charger_classes)
                    ])
                    model.constraint(
                        energy_charged[v_idx][seg_idx] <= (max_power_expr / 60.0) * model.length(session)
                    )
            
            # 4d. Cumulative Charger Capacity Constraint using m.pulse()
            logger.info(f"[UNIFIED] Adding cumulative charger capacity constraints")
            for pc_idx, power_class in enumerate(site_chargers):
                usage_pulses = []
                
                for v_idx in range(n_vehicles):
                    for seg_idx, session in enumerate(charging_sessions[v_idx]):
                        is_assigned_to_class = model.iif(power_class_choice[v_idx][seg_idx] == pc_idx, 1, 0)
                        pulse = model.pulse(session, is_assigned_to_class)
                        usage_pulses.append(pulse)
                
                # Cumulative usage must not exceed charger count
                if usage_pulses:
                    cumulative_usage = model.sum(usage_pulses)
                    model.constraint(cumulative_usage <= power_class.count)
                    logger.info(
                        f"[UNIFIED]   Power Class {pc_idx} ({power_class.max_power_kw}kW): "
                        f"cumulative usage <= {power_class.count} chargers"
                    )
    
    def _extract_scheduling_solution(
        self,
        schedule_id: int,
        vehicles: List[Vehicle],
        vehicle_states: Dict[int, VehicleChargeState],
        energy_requirements: Dict[int, List[RouteEnergyRequirement]],
        availability_matrices: Dict[int, VehicleAvailability],
        time_slots: List[datetime],
        price_data: Dict[datetime, Tuple[float, bool]],
        charge_power: List[List],
        cumulative_energy: List[List],
        vehicle_to_idx: Dict[int, int],
        site_chargers: Optional[List[ChargerPowerClass]] = None,
        charger_assigned: Optional[List[List]] = None,
        segments_by_vehicle: Optional[Dict[int, List[Tuple[int, int, str]]]] = None,
    ) -> Tuple[List[VehicleChargeSchedule], float, float]:
        """Extract scheduling solution from Hexaly variables."""
        vehicle_schedules = []
        total_cost = 0.0
        total_energy = 0.0
        n_slots = len(time_slots)
        
        # Track charger allocation statistics
        charger_allocation_stats = {}
        if charger_assigned and site_chargers:
            for pc_idx, power_class in enumerate(site_chargers):
                charger_allocation_stats[pc_idx] = {
                    'power_kw': power_class.max_power_kw,
                    'type': 'DC' if power_class.is_dc else 'AC',
                    'count': power_class.count,
                    'vehicles': []
                }
        
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
            
            # Determine assigned charger power class for this vehicle
            assigned_charger_id = state.charger_id  # Default to current
            assigned_charger_type = state.charger_type
            assigned_charger_power_kw = None
            
            if charger_assigned and site_chargers:
                seg_assign = charger_assigned[v_idx]
                chosen_pc = None
                if seg_assign and isinstance(seg_assign[0], list):
                    # choose segment class corresponding to first charging slot, else first segment
                    first_charging_slot_idx = None
                    for t_idx in range(n_slots):
                        if charge_power[t_idx][v_idx].value > 0.01:
                            first_charging_slot_idx = t_idx
                            break
                    if first_charging_slot_idx is not None:
                        segs = (segments_by_vehicle or {}).get(vehicle.vehicle_id, [])
                        seg_idx = self._segment_index_for_slot(segs, first_charging_slot_idx * 30)
                        if seg_idx is not None:
                            for pc_idx in range(len(site_chargers)):
                                if seg_assign[seg_idx][pc_idx].value == 1:
                                    chosen_pc = pc_idx
                                    break
                    if chosen_pc is None and seg_assign:
                        for pc_idx in range(len(site_chargers)):
                            if seg_assign[0][pc_idx].value == 1:
                                chosen_pc = pc_idx
                                break
                else:
                    for pc_idx in range(len(site_chargers)):
                        if seg_assign[pc_idx].value == 1:
                            chosen_pc = pc_idx
                            break

                if chosen_pc is not None:
                    pc_idx = chosen_pc
                    power_class = site_chargers[pc_idx]
                    assigned_charger_power_kw = power_class.max_power_kw
                    assigned_charger_type = 'DC' if power_class.is_dc else 'AC'
                    
                    # If vehicle has a connected charger, keep that ID, otherwise pick first from power class
                    if assigned_charger_id is None and power_class.charger_ids:
                        assigned_charger_id = power_class.charger_ids[0]
                    
                    # Track allocation for statistics
                    charger_allocation_stats[pc_idx]['vehicles'].append({
                        'vehicle_id': vehicle.vehicle_id,
                        'charger_id': assigned_charger_id,
                        'energy_scheduled': cumulative
                    })
                    
                    logger.debug(
                        f"[UNIFIED] Vehicle {vehicle.vehicle_id} assigned to {assigned_charger_power_kw}kW "
                        f"({assigned_charger_type}) charger (ID: {assigned_charger_id}), "
                        f"scheduled {cumulative:.2f} kWh"
                    )
                # Segment continuity validation log
                if seg_assign and isinstance(seg_assign[0], list):
                    seg_trace = []
                    for s_idx, seg_class_flags in enumerate(seg_assign):
                        chosen = None
                        for pc_idx in range(len(site_chargers)):
                            if seg_class_flags[pc_idx].value == 1:
                                chosen = pc_idx
                                break
                        seg_trace.append((s_idx, chosen))
                    logger.info(f"[UNIFIED] Vehicle {vehicle.vehicle_id} segment class trace: {seg_trace}")
            
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
                assigned_charger_id=assigned_charger_id,
                charger_type=assigned_charger_type,
                assigned_charger_power_kw=assigned_charger_power_kw
            )
            vehicle_schedules.append(vehicle_schedule)
        
        # Log charger allocation summary
        if charger_allocation_stats:
            logger.info("[UNIFIED] Charger Allocation Results:")
            for pc_idx, stats in charger_allocation_stats.items():
                num_vehicles_assigned = len(stats['vehicles'])
                total_energy = sum(v['energy_scheduled'] for v in stats['vehicles'])
                logger.info(
                    f"[UNIFIED]   Power Class {pc_idx} ({stats['power_kw']}kW {stats['type']}): "
                    f"{num_vehicles_assigned} vehicles assigned to power class (max {stats['count']} chargers available), "
                    f"total energy scheduled: {total_energy:.2f} kWh"
                )
                if num_vehicles_assigned > 0:
                    vehicle_ids = [v['vehicle_id'] for v in stats['vehicles']]
                    logger.info(f"[UNIFIED]     Vehicles: {vehicle_ids}")
            
            # Validate time-slot constraints - check max simultaneous assignments per slot
            logger.info("[UNIFIED] Validating charger capacity constraints per time slot:")
            for pc_idx, stats in charger_allocation_stats.items():
                max_simultaneous = 0
                violation_detected = False
                
                for t_idx in range(n_slots):
                    # Count how many vehicles assigned to this power class are available at this slot
                    # (available = could be connected to charger, regardless of charging)
                    connected_count = 0
                    for v in stats['vehicles']:
                        v_idx = vehicle_to_idx.get(v['vehicle_id'])
                        if v_idx is not None:
                            vehicle = vehicles[v_idx]
                            availability = availability_matrices.get(vehicle.vehicle_id)
                            if availability and availability.availability_matrix[t_idx]:
                                connected_count += 1
                    
                    max_simultaneous = max(max_simultaneous, connected_count)
                    
                    if connected_count > stats['count']:
                        violation_detected = True
                        logger.error(
                            f"[UNIFIED] CONSTRAINT VIOLATION at slot {t_idx}: "
                            f"Power Class {pc_idx} ({stats['power_kw']}kW) has {connected_count} vehicles available/connected "
                            f"but only {stats['count']} chargers available"
                        )
                
                if not violation_detected:
                    logger.info(
                        f"[UNIFIED]   Power Class {pc_idx} ({stats['power_kw']}kW): "
                        f"max {max_simultaneous} vehicles connected simultaneously (limit: {stats['count']}) ✓"
                    )
                else:
                    logger.error(
                        f"[UNIFIED]   Power Class {pc_idx} ({stats['power_kw']}kW): "
                        f"VIOLATION - max {max_simultaneous} vehicles connected simultaneously (limit: {stats['count']}) ✗"
                    )
        
        return vehicle_schedules, total_cost, total_energy
    
    def _find_time_slot_index(self, target_time: datetime, 
                              time_slots: List[datetime]) -> Optional[int]:
        """Find index of time slot closest to target time."""
        for idx, slot_time in enumerate(time_slots):
            if slot_time >= target_time:
                return idx
        return None
    
    def _extract_interval_solution(
        self,
        schedule_id: int,
        vehicles: List[Vehicle],
        vehicle_states: Dict[int, VehicleChargeState],
        energy_requirements: Dict[int, List[RouteEnergyRequirement]],
        availability_matrices: Dict[int, VehicleAvailability],
        time_slots: List[datetime],
        price_data: Dict[datetime, Tuple[float, bool]],
        charging_sessions: List,
        energy_charged: List,
        vehicle_to_idx: Dict[int, int],
        site_chargers: Optional[List[ChargerPowerClass]] = None,
        power_class_choice: Optional[List] = None,
        planning_horizon_minutes: int = 0,
        model = None,
        segments_by_vehicle: Optional[Dict[int, List[Tuple[int, int, str]]]] = None,
    ) -> Tuple[List[VehicleChargeSchedule], float, float]:
        """Extract scheduling solution from interval variables."""
        from datetime import timedelta
        
        vehicle_schedules = []
        total_cost = 0.0
        total_energy = 0.0
        planning_start = time_slots[0] if time_slots else datetime.now()
        
        # Track charger allocation statistics
        charger_allocation_stats = {}
        if power_class_choice and site_chargers:
            for pc_idx, power_class in enumerate(site_chargers):
                charger_allocation_stats[pc_idx] = {
                    'power_kw': power_class.max_power_kw,
                    'type': 'DC' if power_class.is_dc else 'AC',
                    'count': power_class.count,
                    'vehicles': []
                }
        
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
            
            # Extract interval solution values across segments
            vehicle_segment_sessions = charging_sessions[v_idx]
            vehicle_segment_energy = energy_charged[v_idx]
            energy_scheduled = sum(e.value for e in vehicle_segment_energy)
            
            # If no charging scheduled, create empty schedule
            if energy_scheduled < 0.01:
                vehicle_schedule = VehicleChargeSchedule(
                    vehicle_id=vehicle.vehicle_id,
                    schedule_id=schedule_id,
                    initial_soc_kwh=state.current_soc_kwh,
                    target_soc_kwh=target_soc_kwh,
                    total_energy_needed_kwh=energy_needed,
                    route_checkpoints=requirements,
                    has_routes=len(requirements) > 0,
                    charge_slots=[],
                    total_energy_scheduled_kwh=0.0,
                    assigned_charger_id=state.charger_id,
                    charger_type=state.charger_type
                )
                vehicle_schedules.append(vehicle_schedule)
                continue
            
            # Determine assigned charger (primary segment, if any)
            assigned_charger_id = state.charger_id
            assigned_charger_type = state.charger_type
            assigned_charger_power_kw = None
            
            # Create ChargeSlot entries for 30-min intervals (for backwards compatibility)
            charge_slots = []
            cumulative = 0.0
            segment_class_trace = []
            for seg_idx, session in enumerate(vehicle_segment_sessions):
                seg_energy = vehicle_segment_energy[seg_idx].value
                if seg_energy < 0.01:
                    continue
                if model:
                    start_minutes = model.start(session).value
                    end_minutes = model.end(session).value
                    duration_minutes = max(0.0, model.length(session).value)
                else:
                    start_minutes = 0
                    end_minutes = int(seg_energy / state.ac_charge_rate_kw * 60) if state.ac_charge_rate_kw > 0 else 0
                    duration_minutes = end_minutes

                seg_start_dt = planning_start + timedelta(minutes=start_minutes)
                seg_end_dt = planning_start + timedelta(minutes=end_minutes)
                power_kw = (seg_energy / (duration_minutes / 60.0)) if duration_minutes > 0 else 0.0

                if power_class_choice and site_chargers:
                    pc_idx = int(power_class_choice[v_idx][seg_idx].value)
                    power_class = site_chargers[pc_idx]
                    assigned_charger_power_kw = power_class.max_power_kw
                    assigned_charger_type = 'DC' if power_class.is_dc else 'AC'
                    if assigned_charger_id is None and power_class.charger_ids:
                        assigned_charger_id = power_class.charger_ids[0]
                    charger_allocation_stats[pc_idx]['vehicles'].append({
                        'vehicle_id': vehicle.vehicle_id,
                        'charger_id': assigned_charger_id,
                        'energy_scheduled': seg_energy
                    })
                    segment_class_trace.append((seg_idx, pc_idx, start_minutes, end_minutes))

                current_time = seg_start_dt
                while current_time < seg_end_dt:
                    next_time = current_time + timedelta(minutes=30)
                    if next_time > seg_end_dt:
                        next_time = seg_end_dt

                    slot_duration_hours = (next_time - current_time).total_seconds() / 3600.0
                    energy_this_slot = power_kw * slot_duration_hours
                    cumulative += energy_this_slot
                    price, is_triad = price_data.get(current_time, (0.15, False))
                    charge_slots.append(
                        ChargeSlot(
                            time_slot=current_time,
                            charge_power_kw=power_kw,
                            cumulative_energy_kwh=cumulative,
                            electricity_price=price,
                            is_triad_period=is_triad,
                        )
                    )
                    total_cost += energy_this_slot * price
                    current_time = next_time

            if segment_class_trace:
                logger.info(
                    f"[UNIFIED] Vehicle {vehicle.vehicle_id} segment class trace: {segment_class_trace}"
                )
            
            total_energy += energy_scheduled
            
            vehicle_schedule = VehicleChargeSchedule(
                vehicle_id=vehicle.vehicle_id,
                schedule_id=schedule_id,
                initial_soc_kwh=state.current_soc_kwh,
                target_soc_kwh=target_soc_kwh,
                total_energy_needed_kwh=energy_needed,
                route_checkpoints=requirements,
                has_routes=len(requirements) > 0,
                charge_slots=charge_slots,
                total_energy_scheduled_kwh=energy_scheduled,
                assigned_charger_id=assigned_charger_id,
                charger_type=assigned_charger_type,
                assigned_charger_power_kw=assigned_charger_power_kw
            )
            vehicle_schedules.append(vehicle_schedule)
        
        # Log charger allocation summary
        if charger_allocation_stats:
            logger.info("[UNIFIED] Charger Allocation Results:")
            for pc_idx, stats in charger_allocation_stats.items():
                num_vehicles_assigned = len(stats['vehicles'])
                total_energy_class = sum(v['energy_scheduled'] for v in stats['vehicles'])
                logger.info(
                    f"[UNIFIED]   Power Class {pc_idx} ({stats['power_kw']}kW {stats['type']}): "
                    f"{num_vehicles_assigned} vehicles assigned, "
                    f"total energy: {total_energy_class:.2f} kWh"
                )
                if num_vehicles_assigned > 0:
                    vehicle_ids = [v['vehicle_id'] for v in stats['vehicles']]
                    logger.info(f"[UNIFIED]     Vehicles: {vehicle_ids}")
        
        return vehicle_schedules, total_cost, total_energy
    
    def _greedy_fallback(
        self,
        mode_flags: List[str],
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
        logger.warning(f"[UNIFIED] Using greedy fallback for mode flags: {mode_flags}")
        run_allocation = MODE_FLAG_ALLOCATION in mode_flags
        run_scheduling = MODE_FLAG_CHARGE_SCHEDULING in mode_flags
        
        # Allocation fallback
        selected_sequences = []
        allocation_score = 0.0
        routes_allocated = 0
        routes_total = len(route_ids) if route_ids else 0
        
        if run_allocation:
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
        
        if run_scheduling:
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
            mode=mode_flags,
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
