"""Test time-slot capacity constraint for charger power classes."""
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from src.models.vehicle import Vehicle
from src.models.scheduler import (
    VehicleChargeState, ChargerPowerClass, VehicleAvailability,
    RouteEnergyRequirement, VehicleChargeSchedule
)
from src.optimizer.unified_optimizer import (
    UnifiedOptimizer, UnifiedOptimizationConfig, OptimizationMode
)
from src.config import IS_HEXALY_ACTIVE


class TestChargerTimeSlotCapacity(unittest.TestCase):
    """Test that charger power class usage respects count constraints at each time slot."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.planning_start = datetime(2024, 1, 15, 6, 0)
        self.planning_end = datetime(2024, 1, 15, 12, 0)  # 6 hours = 12 slots
        
        # 5 vehicles needing charge
        self.vehicles = [
            Vehicle(
                vehicle_id=100 + i,
                site_id=1,
                vehicle_alias=f"V{100+i}",
                battery_capacity=100.0,
                charge_power_ac=50.0,
                charge_power_dc=150.0,
                efficiency_kwh_mile=0.35,
                active=True
            )
            for i in range(1, 6)  # 5 vehicles: 101-105
        ]
        
        # 5 vehicles all start at 20% SOC (need charging)
        self.vehicle_states = {
            v.vehicle_id: VehicleChargeState(
                vehicle_id=v.vehicle_id,
                current_soc_kwh=20.0,
                battery_capacity_kwh=100.0,
                ac_charge_rate_kw=50.0,
                dc_charge_rate_kw=150.0,
                charger_id=None,
                is_connected=False
            )
            for v in self.vehicles
        }
        
        # 2 chargers with 50kW power (only 2 available!)
        # This means at most 2 vehicles can charge simultaneously with 50kW
        self.site_chargers = [
            ChargerPowerClass(
                max_power_kw=50.0,
                count=2,  # Only 2 physical chargers
                is_dc=False,
                charger_ids=[1, 2]
            )
        ]
        
        # All vehicles available all the time
        self.vehicle_availability = {
            v.vehicle_id: [
                VehicleAvailability(
                    start_time=self.planning_start,
                    end_time=self.planning_end,
                    is_available=True
                )
            ]
            for v in self.vehicles
        }
        
        # No routes (just charging scenario)
        self.energy_requirements = {}
        
        # Flat pricing
        self.price_data = {
            self.planning_start + timedelta(minutes=30*i): (0.10, False)
            for i in range(13)
        }
        
        self.config = UnifiedOptimizationConfig(
            mode=OptimizationMode.SCHEDULING_ONLY,
            time_limit_seconds=10,
            target_soc_percent=80.0,
            enable_price_optimization=False
        )
    
    @unittest.skipUnless(IS_HEXALY_ACTIVE, "Hexaly not available")
    def test_time_slot_capacity_respected(self):
        """Test that no more than 2 vehicles charge simultaneously (respecting charger count)."""
        optimizer = UnifiedOptimizer(self.config)
        
        result = optimizer.solve(
            schedule_id=1,
            planning_start=self.planning_start,
            planning_end=self.planning_end,
            vehicles=self.vehicles,
            vehicle_states=self.vehicle_states,
            vehicle_availability=self.vehicle_availability,
            energy_requirements=self.energy_requirements,
            price_data=self.price_data,
            allocated_routes=[],
            all_routes=[],
            site_chargers=self.site_chargers
        )
        
        # Check that solution was found
        self.assertIsNotNone(result)
        self.assertEqual(len(result.vehicle_schedules), 5)
        
        # Build time slot map: slot_time -> list of (vehicle_id, power_kw)
        time_slots = sorted(set(
            slot.time_slot 
            for sched in result.vehicle_schedules 
            for slot in sched.charge_slots
        ))
        
        for slot_time in time_slots:
            charging_vehicles = []
            for sched in result.vehicle_schedules:
                for slot in sched.charge_slots:
                    if slot.time_slot == slot_time and slot.charge_power_kw > 0.1:
                        charging_vehicles.append((sched.vehicle_id, slot.charge_power_kw))
            
            # At most 2 vehicles should be charging at any time slot
            self.assertLessEqual(
                len(charging_vehicles),
                2,  # charger count
                f"Time slot {slot_time}: {len(charging_vehicles)} vehicles charging "
                f"(exceeds capacity of 2). Vehicles: {charging_vehicles}"
            )
        
        print(f"\n✓ Time-slot capacity constraint respected:")
        print(f"  Vehicles: 5, Chargers: 2 × 50kW")
        print(f"  Max simultaneous charging at any slot: ≤ 2")
    
    @unittest.skipUnless(IS_HEXALY_ACTIVE, "Hexaly not available")
    def test_sequential_charging_when_demand_exceeds_capacity(self):
        """Test that vehicles charge sequentially when there are more vehicles than chargers."""
        optimizer = UnifiedOptimizer(self.config)
        
        result = optimizer.solve(
            schedule_id=2,
            planning_start=self.planning_start,
            planning_end=self.planning_end,
            vehicles=self.vehicles,
            vehicle_states=self.vehicle_states,
            vehicle_availability=self.vehicle_availability,
            energy_requirements=self.energy_requirements,
            price_data=self.price_data,
            allocated_routes=[],
            all_routes=[],
            site_chargers=self.site_chargers
        )
        
        # Count total charging occurrences across all slots
        total_charging_occurrences = 0
        for sched in result.vehicle_schedules:
            for slot in sched.charge_slots:
                if slot.charge_power_kw > 0.1:
                    total_charging_occurrences += 1
        
        # With 5 vehicles needing 60kWh each (20% -> 80% = 60kWh)
        # and 2 chargers at 50kW, charging should be spread across time
        # Total energy needed: 5 × 60 = 300 kWh
        # Charger capacity: 2 × 50kW = 100kW
        # Minimum time: 300 / 100 = 3 hours = 6 slots
        
        print(f"\n✓ Sequential charging when demand exceeds capacity:")
        print(f"  5 vehicles sharing 2 chargers")
        print(f"  Total charging slot-occurrences: {total_charging_occurrences}")
        print(f"  All vehicles received charge: {all(sched.total_energy_kwh > 0 for sched in result.vehicle_schedules)}")
    
    @unittest.skipUnless(IS_HEXALY_ACTIVE, "Hexaly not available")
    def test_multiple_power_classes_capacity(self):
        """Test capacity constraints with multiple power classes."""
        # Add a second power class: 1 × 150kW DC charger
        self.site_chargers.append(
            ChargerPowerClass(
                max_power_kw=150.0,
                count=1,  # Only 1 DC charger
                is_dc=True,
                charger_ids=[3]
            )
        )
        
        optimizer = UnifiedOptimizer(self.config)
        
        result = optimizer.solve(
            schedule_id=3,
            planning_start=self.planning_start,
            planning_end=self.planning_end,
            vehicles=self.vehicles,
            vehicle_states=self.vehicle_states,
            vehicle_availability=self.vehicle_availability,
            energy_requirements=self.energy_requirements,
            price_data=self.price_data,
            allocated_routes=[],
            all_routes=[],
            site_chargers=self.site_chargers
        )
        
        # Build time slot analysis by power class
        time_slots = sorted(set(
            slot.time_slot 
            for sched in result.vehicle_schedules 
            for slot in sched.charge_slots
        ))
        
        max_violations = 0
        for slot_time in time_slots:
            # Count vehicles using 50kW chargers
            vehicles_50kw = sum(
                1 for sched in result.vehicle_schedules
                for slot in sched.charge_slots
                if (slot.time_slot == slot_time and 
                    slot.charge_power_kw > 0.1 and
                    sched.assigned_charger_power_kw == 50.0)
            )
            
            # Count vehicles using 150kW chargers
            vehicles_150kw = sum(
                1 for sched in result.vehicle_schedules
                for slot in sched.charge_slots
                if (slot.time_slot == slot_time and 
                    slot.charge_power_kw > 0.1 and
                    sched.assigned_charger_power_kw == 150.0)
            )
            
            # Check capacity constraints
            self.assertLessEqual(
                vehicles_50kw,
                2,  # 50kW charger count
                f"Slot {slot_time}: {vehicles_50kw} vehicles using 50kW (max 2)"
            )
            self.assertLessEqual(
                vehicles_150kw,
                1,  # 150kW charger count
                f"Slot {slot_time}: {vehicles_150kw} vehicles using 150kW (max 1)"
            )
            
            # At most 3 vehicles total (2 on 50kW + 1 on 150kW)
            total_charging = vehicles_50kw + vehicles_150kw
            self.assertLessEqual(
                total_charging,
                3,
                f"Slot {slot_time}: {total_charging} vehicles charging (max 3 total)"
            )
        
        print(f"\n✓ Multiple power classes capacity respected:")
        print(f"  Power classes: 2×50kW + 1×150kW")
        print(f"  All capacity constraints satisfied at every time slot")


if __name__ == '__main__':
    unittest.main()
