"""Regression tests for interval-model site capacity and charger-class rate capping."""
import unittest
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List

from src.config import IS_HEXALY_ACTIVE
from src.models.scheduler import ChargerPowerClass, VehicleAvailability, VehicleChargeState
from src.models.vehicle import Vehicle
from src.optimizer.unified_optimizer import (
    OptimizationMode,
    UnifiedOptimizationConfig,
    UnifiedOptimizer,
)


def _time_slots(start: datetime, hours: int) -> List[datetime]:
    slots = []
    current = start
    end = start + timedelta(hours=hours)
    while current < end:
        slots.append(current)
        current += timedelta(minutes=30)
    return slots


def _all_available(vehicle_id: int, slots: List[datetime]) -> VehicleAvailability:
    return VehicleAvailability(
        vehicle_id=vehicle_id,
        time_slots=slots,
        availability_matrix=[True] * len(slots),
    )


def _flat_prices(slots: List[datetime], price: float = 0.15) -> Dict[datetime, tuple]:
    return {slot: (price, False) for slot in slots}


def _aggregate_slot_power(schedules) -> Dict[datetime, float]:
    """Sum charge_power_kw per slot across vehicle schedules."""
    slot_power: Dict[datetime, float] = defaultdict(float)
    for schedule in schedules:
        for charge_slot in schedule.charge_slots:
            slot_power[charge_slot.time_slot] += charge_slot.charge_power_kw
    return slot_power


@unittest.skipIf(not IS_HEXALY_ACTIVE, "Hexaly not active")
class TestIntervalModelConstraints(unittest.TestCase):
    """Interval formulation: site capacity and effective charge rate."""

    def setUp(self):
        self.start = datetime(2026, 5, 21, 4, 0, 0)
        self.slots = _time_slots(self.start, 8)
        self.price_data = _flat_prices(self.slots)

    def test_charge_rate_capped_by_slower_charger_class(self):
        """22 kW vehicle on 7 kW charger must not exceed 7 kW implied draw."""
        vehicle = Vehicle(
            vehicle_id=1,
            registration_number="TEST001",
            vehicle_type="Van",
            vin="VIN001",
            site_id=10,
            battery_capacity=75.0,
            charge_power_ac=22.0,
            charge_power_dc=50.0,
            efficiency_kwh_mile=0.35,
            estimated_soc=20.0,
            current_charger_id=1,
            enabled=True,
        )
        state = VehicleChargeState(
            vehicle_id=1,
            current_soc_percent=20.0,
            current_soc_kwh=15.0,
            battery_capacity_kwh=75.0,
            is_connected=True,
            charger_id=1,
            charger_type="AC",
            ac_charge_rate_kw=22.0,
            dc_charge_rate_kw=50.0,
            efficiency_kwh_mile=0.35,
            status="Idle",
        )
        site_chargers = [
            ChargerPowerClass(
                max_power_kw=7.0,
                count=2,
                charger_ids=[1, 2],
                is_dc=False,
            ),
        ]
        config = UnifiedOptimizationConfig(
            mode=OptimizationMode.SCHEDULING_ONLY,
            scheduling_time_limit=60,
            target_soc_percent=80.0,
            site_capacity_kw=100.0,
            enable_charger_allocation=True,
            makespan_penalty_weight=0.0,
        )
        optimizer = UnifiedOptimizer(config)
        result = optimizer.solve(
            schedule_id=1,
            vehicles=[vehicle],
            vehicle_states={1: state},
            energy_requirements={1: []},
            availability_matrices={1: _all_available(1, self.slots)},
            time_slots=self.slots,
            forecast_data={},
            price_data=self.price_data,
            site_chargers=site_chargers,
        )
        self.assertTrue(result.vehicle_schedules)
        schedule = result.vehicle_schedules[0]
        if schedule.total_energy_scheduled_kwh < 0.01:
            self.skipTest("Solver scheduled no charging")
        for slot in schedule.charge_slots:
            self.assertLessEqual(
                slot.charge_power_kw,
                7.01,
                f"Implied power {slot.charge_power_kw} kW exceeds 7 kW charger class",
            )
        if schedule.assigned_charger_power_kw is not None:
            self.assertLessEqual(schedule.assigned_charger_power_kw, 7.0)

    def test_site_capacity_limits_aggregate_draw_when_nameplate_exceeds_contract(self):
        """Σ N_k·P_k > site_capacity_kw — per-slot draw must respect site cap."""
        vehicles = []
        states = {}
        availability = {}
        for vid in range(1, 5):
            vehicles.append(
                Vehicle(
                    vehicle_id=vid,
                    registration_number=f"T{vid:03d}",
                    vehicle_type="Van",
                    vin=f"V{vid:03d}",
                    site_id=10,
                    battery_capacity=100.0,
                    charge_power_ac=11.0,
                    charge_power_dc=50.0,
                    efficiency_kwh_mile=0.35,
                    estimated_soc=30.0,
                    enabled=True,
                )
            )
            states[vid] = VehicleChargeState(
                vehicle_id=vid,
                current_soc_percent=30.0,
                current_soc_kwh=30.0,
                battery_capacity_kwh=100.0,
                is_connected=False,
                charger_id=None,
                charger_type=None,
                ac_charge_rate_kw=11.0,
                dc_charge_rate_kw=50.0,
                efficiency_kwh_mile=0.35,
                status="Idle",
            )
            availability[vid] = _all_available(vid, self.slots)

        # 4 × 11 kW = 44 kW nameplate; contractual site cap = 20 kW
        site_chargers = [
            ChargerPowerClass(
                max_power_kw=11.0,
                count=4,
                charger_ids=[1, 2, 3, 4],
                is_dc=False,
            ),
        ]
        site_capacity_kw = 20.0
        config = UnifiedOptimizationConfig(
            mode=OptimizationMode.SCHEDULING_ONLY,
            scheduling_time_limit=90,
            target_soc_percent=70.0,
            site_capacity_kw=site_capacity_kw,
            enable_charger_allocation=True,
            makespan_penalty_weight=0.0,
        )
        optimizer = UnifiedOptimizer(config)
        result = optimizer.solve(
            schedule_id=2,
            vehicles=vehicles,
            vehicle_states=states,
            energy_requirements={vid: [] for vid in states},
            availability_matrices=availability,
            time_slots=self.slots,
            forecast_data={slot: 0.0 for slot in self.slots},
            price_data=self.price_data,
            site_chargers=site_chargers,
        )
        slot_power = _aggregate_slot_power(result.vehicle_schedules)
        for slot_time, total_kw in slot_power.items():
            self.assertLessEqual(
                total_kw,
                site_capacity_kw + 0.5,
                f"Slot {slot_time}: aggregate {total_kw:.1f} kW exceeds "
                f"site capacity {site_capacity_kw} kW",
            )


if __name__ == "__main__":
    unittest.main()
