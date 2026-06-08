"""Tests for optimizer debug CSV export."""

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from src.constraints.constraint_manager import ConstraintManager
from src.models.route import Route
from src.models.vehicle import Vehicle
from src.optimizer.cost_matrix import ModelDataBuilder
from src.optimizer.optimizer_debug import write_optimizer_debug_csv
from src.optimizer.unified_optimizer import OptimizationConfig, OptimizationResult


def _make_route(route_id: str, start: datetime) -> Route:
    return Route(
        route_id=route_id,
        site_id=10,
        route_alias=route_id,
        route_status="A",
        plan_start_date_time=start,
        plan_end_date_time=start + timedelta(hours=2),
        plan_mileage=40.0,
        n_orders=1,
    )


def _make_vehicle(vehicle_id: int) -> Vehicle:
    return Vehicle(
        vehicle_id=vehicle_id,
        site_id=10,
        active=True,
        VOR=False,
        charge_power_ac=11.0,
        charge_power_dc=50.0,
        battery_capacity=100.0,
        efficiency_kwh_mile=0.35,
        estimated_soc=80.0,
    )


def test_write_optimizer_debug_csv_sections(tmp_path):
    base = datetime(2026, 6, 1, 6, 0, 0)
    routes = [_make_route("R1", base), _make_route("R2", base + timedelta(hours=3))]
    vehicles = [_make_vehicle(1), _make_vehicle(2)]
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
    data = ModelDataBuilder(vehicles, routes, manager, max_routes_per_vehicle=2).build()
    out = tmp_path / "debug.csv"
    result = OptimizationResult(
        status="FEASIBLE",
        solve_time_seconds=1.5,
        objective_value=42.0,
        vehicle_route_sequences={0: [0], 1: [1]},
        routes_allocated=2,
        routes_total=2,
        allocation_score=42.0,
    )
    written = write_optimizer_debug_csv(
        data,
        config=OptimizationConfig(route_count_weight=100.0),
        result=result,
        model_stats={"nb_expressions": 10, "nb_decisions": 2, "nb_constraints": 5},
        validation_warnings=["example warning"],
        output_path=str(out),
    )
    assert written == str(out.resolve())
    text = out.read_text(encoding="utf-8")
    for section in (
        "[CONFIG_PARAMS]",
        "[MODEL_SUMMARY]",
        "[VEHICLES]",
        "[ROUTES]",
        "[NODES]",
        "[DISTANCE_MATRIX]",
        "[ENERGY_CONSUMPTION_KWH]",
        "[SOLVE_RESULT]",
        "[VEHICLE_SEQUENCES]",
        "[ROUTE_ALLOCATIONS]",
        "[MODEL_STATS]",
        "[VALIDATION_WARNINGS]",
    ):
        assert section in text
    assert "route_count_weight" in text
    assert "R1" in text
    assert "example warning" in text
