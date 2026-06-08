"""Unit tests for unified allocation optimizer."""

from datetime import datetime, timedelta

import numpy as np
import pytest

from src.constraints.constraint_manager import ConstraintManager
from src.models.route import Route
from src.models.vehicle import Vehicle
from src.config import CHARGE_SLOTS_PER_CHARGER, CHARGE_SLOT_MINUTES
from src.optimizer.cost_matrix import (
    BIG_VALUE,
    ChargeSchedulingContext,
    ModelDataBuilder,
    build_incompatible_route_pairs,
    charge_node_index,
)
from src.optimizer.unified_optimizer import (
    OptimizationConfig,
    UnifiedOptimizer,
    normalize_mode,
)


def _make_route(route_id: str, start: datetime, hours: float = 2.0, mileage: float = 50.0) -> Route:
    return Route(
        route_id=route_id,
        site_id=10,
        route_alias=route_id,
        route_status="A",
        plan_start_date_time=start,
        plan_end_date_time=start + timedelta(hours=hours),
        plan_mileage=mileage,
        n_orders=1,
    )


def _make_vehicle(vehicle_id: int, soc: float = 80.0) -> Vehicle:
    return Vehicle(
        vehicle_id=vehicle_id,
        site_id=10,
        active=True,
        VOR=False,
        charge_power_ac=11.0,
        charge_power_dc=50.0,
        battery_capacity=100.0,
        efficiency_kwh_mile=0.35,
        estimated_soc=soc,
    )


@pytest.fixture
def minimal_problem():
    base = datetime(2026, 6, 1, 6, 0, 0)
    routes = [
        _make_route("R1", base),
        _make_route("R2", base + timedelta(hours=3)),
        _make_route("R3", base + timedelta(hours=6)),
    ]
    vehicles = [_make_vehicle(1), _make_vehicle(2)]
    configs = {
        name: {"enabled": False, "params": {}, "penalty": 0}
        for name in [
            "energy_feasibility",
            "turnaround_time_strict",
            "turnaround_time_preferred",
            "shift_hours_strict",
            "minimum_soonness",
            "swap_minimization",
            "energy_optimization",
        ]
    }
    configs["energy_feasibility"]["enabled"] = True
    configs["turnaround_time_strict"]["enabled"] = True
    configs["turnaround_time_strict"]["params"] = {"minimum_minutes": 45}
    manager = ConstraintManager(configs)
    builder = ModelDataBuilder(vehicles, routes, manager, max_routes_per_vehicle=3)
    return builder.build()


def test_model_data_builder_shapes(minimal_problem):
    data = minimal_problem
    assert data.distance_matrix.shape == (3, 3)
    assert len(data.route_prizes) == 3
    assert data.energy_consumption.shape == (2, 3)
    assert len(data.node_durations) == 3
    assert data.node_durations[0] == pytest.approx(120.0)
    assert np.all(data.route_prizes >= 0)
    assert not data.enable_charge_scheduling


def _forty_eight_slots(base: datetime) -> list:
    return [
        base + timedelta(minutes=CHARGE_SLOT_MINUTES * i)
        for i in range(CHARGE_SLOTS_PER_CHARGER)
    ]


def test_integrated_model_data_builder_shapes(minimal_problem):
    base = datetime(2026, 6, 1, 6, 0, 0)
    slots = _forty_eight_slots(base)
    ctx = ChargeSchedulingContext(
        n_chargers=2,
        time_slots=slots,
        electricity_cost_per_slot=[-1.0] * CHARGE_SLOTS_PER_CHARGER,
        capacity_power_kw=[500.0] * CHARGE_SLOTS_PER_CHARGER,
        p_fixed_kw=50.0,
    )
    vehicles = minimal_problem.vehicles
    routes = minimal_problem.routes
    manager = ConstraintManager(
        {
            name: {"enabled": False, "params": {}, "penalty": 0}
            for name in [
                "energy_feasibility",
                "turnaround_time_strict",
                "turnaround_time_preferred",
                "shift_hours_strict",
                "minimum_soonness",
                "swap_minimization",
                "energy_optimization",
            ]
        }
    )
    builder = ModelDataBuilder(vehicles, routes, manager, max_routes_per_vehicle=3)
    data = builder.build(charge_context=ctx)
    assert data.n_timesteps == CHARGE_SLOTS_PER_CHARGER
    n_nodes = 3 + 2 * CHARGE_SLOTS_PER_CHARGER
    assert data.n_nodes == n_nodes
    assert data.distance_matrix.shape == (n_nodes, n_nodes)
    assert data.enable_charge_scheduling


def test_route_to_charge_forbids_slot_before_route_end(minimal_problem):
    """Charge slot starting before route end must be infeasible (BIG_VALUE arc)."""
    base = datetime(2026, 6, 1, 6, 0, 0)
    slots = _forty_eight_slots(base)
    ctx = ChargeSchedulingContext(
        n_chargers=1,
        time_slots=slots,
        electricity_cost_per_slot=[-1.0] * CHARGE_SLOTS_PER_CHARGER,
        capacity_power_kw=[500.0] * CHARGE_SLOTS_PER_CHARGER,
        p_fixed_kw=50.0,
    )
    routes = minimal_problem.routes
    vehicles = minimal_problem.vehicles
    manager = ConstraintManager(
        {
            name: {"enabled": False, "params": {}, "penalty": 0}
            for name in [
                "energy_feasibility",
                "turnaround_time_strict",
                "turnaround_time_preferred",
                "shift_hours_strict",
                "minimum_soonness",
                "swap_minimization",
                "energy_optimization",
            ]
        }
    )
    builder = ModelDataBuilder(vehicles, routes, manager, max_routes_per_vehicle=3)
    data = builder.build(charge_context=ctx)
    n_routes = len(routes)
    n_timesteps = len(slots)
    route0_end = routes[0].plan_end_date_time
    early_slot = next(
        i
        for i, t in enumerate(slots)
        if t < route0_end
    )
    cn_early = charge_node_index(n_routes, n_timesteps, 0, early_slot)
    assert data.distance_matrix[0, cn_early] >= BIG_VALUE


def test_integrated_node_durations_include_charge_slots(minimal_problem):
    base = datetime(2026, 6, 1, 6, 0, 0)
    slots = _forty_eight_slots(base)
    ctx = ChargeSchedulingContext(
        n_chargers=2,
        time_slots=slots,
        electricity_cost_per_slot=[-1.0] * CHARGE_SLOTS_PER_CHARGER,
        capacity_power_kw=[500.0] * CHARGE_SLOTS_PER_CHARGER,
        p_fixed_kw=50.0,
    )
    vehicles = minimal_problem.vehicles
    routes = minimal_problem.routes
    manager = ConstraintManager(
        {
            name: {"enabled": False, "params": {}, "penalty": 0}
            for name in [
                "energy_feasibility",
                "turnaround_time_strict",
                "turnaround_time_preferred",
                "shift_hours_strict",
                "minimum_soonness",
                "swap_minimization",
                "energy_optimization",
            ]
        }
    )
    builder = ModelDataBuilder(vehicles, routes, manager, max_routes_per_vehicle=3)
    data = builder.build(charge_context=ctx)
    assert len(data.node_durations) == data.n_nodes
    assert data.node_durations[0] == pytest.approx(120.0)
    assert np.all(
        data.node_durations[data.n_routes :] == float(CHARGE_SLOT_MINUTES)
    )


def test_charge_node_index_and_cross_charger_arcs(minimal_problem):
    base = datetime(2026, 6, 1, 6, 0, 0)
    slots = _forty_eight_slots(base)
    ctx = ChargeSchedulingContext(
        n_chargers=2,
        time_slots=slots,
        electricity_cost_per_slot=[-1.0] * CHARGE_SLOTS_PER_CHARGER,
        capacity_power_kw=[500.0] * CHARGE_SLOTS_PER_CHARGER,
        p_fixed_kw=50.0,
    )
    vehicles = minimal_problem.vehicles
    routes = minimal_problem.routes
    manager = ConstraintManager(
        {
            name: {"enabled": False, "params": {}, "penalty": 0}
            for name in [
                "energy_feasibility",
                "turnaround_time_strict",
                "turnaround_time_preferred",
                "shift_hours_strict",
                "minimum_soonness",
                "swap_minimization",
                "energy_optimization",
            ]
        }
    )
    builder = ModelDataBuilder(vehicles, routes, manager, max_routes_per_vehicle=3)
    data = builder.build(charge_context=ctx)
    n_routes = 3
    n_timesteps = len(slots)
    assert charge_node_index(n_routes, n_timesteps, 0, 0) == n_routes
    assert charge_node_index(n_routes, n_timesteps, 1, 2) == n_routes + n_timesteps + 2
    c0t0 = charge_node_index(n_routes, n_timesteps, 0, 0)
    c1t0 = charge_node_index(n_routes, n_timesteps, 1, 0)
    assert data.distance_matrix[c0t0, c1t0] >= BIG_VALUE


def test_variable_power_model_data_flag(minimal_problem):
    base = datetime(2026, 6, 1, 6, 0, 0)
    slots = _forty_eight_slots(base)
    ctx = ChargeSchedulingContext(
        n_chargers=2,
        time_slots=slots,
        electricity_cost_per_slot=[-0.5] * CHARGE_SLOTS_PER_CHARGER,
        capacity_power_kw=[500.0] * CHARGE_SLOTS_PER_CHARGER,
        p_fixed_kw=50.0,
        charger_max_power_kw=[50.0, 22.0],
        enable_variable_charger_power=True,
    )
    vehicles = minimal_problem.vehicles
    routes = minimal_problem.routes
    manager = ConstraintManager(
        {
            name: {"enabled": False, "params": {}, "penalty": 0}
            for name in [
                "energy_feasibility",
                "turnaround_time_strict",
                "turnaround_time_preferred",
                "shift_hours_strict",
                "minimum_soonness",
                "swap_minimization",
                "energy_optimization",
            ]
        }
    )
    builder = ModelDataBuilder(vehicles, routes, manager, max_routes_per_vehicle=3)
    data = builder.build(charge_context=ctx)
    assert data.enable_variable_charger_power
    assert data.charger_max_power_kw == [50.0, 22.0]
    assert data.electricity_price_per_slot[0] == 0.5
    assert np.all(data.node_rewards[data.n_routes :] == 0)


def test_incompatible_route_pairs_detect_overlap():
    base = datetime(2026, 6, 1, 6, 0, 0)
    r1_end = base + timedelta(hours=2)
    r2_start = base + timedelta(hours=1)
    starts = np.array(
        [base.timestamp() / 60.0, r2_start.timestamp() / 60.0], dtype=float
    )
    ends = np.array(
        [r1_end.timestamp() / 60.0, (r2_start + timedelta(hours=2)).timestamp() / 60.0],
        dtype=float,
    )
    origin = float(np.min(starts))
    pairs = build_incompatible_route_pairs(
        starts - origin, ends - origin, turnaround_minutes=45
    )
    assert pairs == [(0, 1)]


def test_model_data_includes_incompatible_pairs(minimal_problem):
    data = minimal_problem
    assert hasattr(data, "incompatible_route_pairs")


def test_route_count_constraint_is_per_vehicle_not_fleet_total():
    """Regression: fleet-wide sum(route_count) must not cap total routes at max_routes."""
    source = open(
        "src/optimizer/unified_optimizer.py", encoding="utf-8"
    ).read()
    assert "for route_count_v in route_count_terms:" in source
    assert "route_count_term <= max_routes" not in source


def test_normalize_mode_charger_allocation_requires_scheduling():
    with pytest.raises(ValueError, match="charge_scheduling"):
        normalize_mode(["allocation", "charger_allocation"])
    flags = normalize_mode(["charge_scheduling", "charger_allocation"])
    assert "charger_allocation" in flags


def test_charge_context_rejects_wrong_slot_count():
    base = datetime(2026, 6, 1, 6, 0, 0)
    short_slots = [base + timedelta(minutes=30 * i) for i in range(4)]
    with pytest.raises(ValueError, match="48"):
        ChargeSchedulingContext(
            n_chargers=1,
            time_slots=short_slots,
            electricity_cost_per_slot=[-1.0] * 4,
            capacity_power_kw=[500.0] * 4,
            p_fixed_kw=50.0,
        )


def test_unified_optimizer_greedy_or_hexaly(minimal_problem):
    optimizer = UnifiedOptimizer(OptimizationConfig(time_limit_seconds=5))
    result = optimizer.solve(minimal_problem)
    assert result.status in ("OPTIMAL", "FEASIBLE", "INFEASIBLE", "INCONSISTENT")
    assert result.routes_total == 3
    assert 0 <= result.routes_allocated <= 3
