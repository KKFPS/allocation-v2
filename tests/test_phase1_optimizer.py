"""Unit tests for route-only allocation optimizer."""

from datetime import datetime, timedelta

import numpy as np
import pytest

from src.constraints.constraint_manager import ConstraintManager
from src.models.route import Route
from src.models.vehicle import Vehicle
from src.optimizer.allocation_optimizer import AllocationConfig, RouteAllocationOptimizer
from src.optimizer.cost_matrix import AllocationDataBuilder


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
    builder = AllocationDataBuilder(vehicles, routes, manager, max_routes_per_vehicle=3)
    return builder.build()


def test_allocation_data_builder_shapes(minimal_problem):
    data = minimal_problem
    assert data.distance_matrix.shape == (3, 3)
    assert len(data.route_prizes) == 3
    assert data.energy_consumption.shape == (2, 3)
    assert np.all(data.route_prizes >= 0)


def test_route_allocation_optimizer_greedy_or_hexaly(minimal_problem):
    optimizer = RouteAllocationOptimizer(AllocationConfig(time_limit_seconds=5))
    result = optimizer.solve(minimal_problem)
    assert result.status in ("OPTIMAL", "FEASIBLE", "INFEASIBLE", "INCONSISTENT")
    assert result.routes_total == 3
    assert 0 <= result.routes_allocated <= 3
