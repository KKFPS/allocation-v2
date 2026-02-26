"""Tests for charger allocation in unified optimizer."""
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
import numpy as np

from src.models.vehicle import Vehicle
from src.models.scheduler import (
    VehicleChargeState, Charger, VehicleAvailability,
    RouteEnergyRequirement, VehicleChargeSchedule
)
from src.optimizer.unified_optimizer import (
    UnifiedOptimizer, UnifiedOptimizationConfig, OptimizationMode
)
from src.config import IS_HEXALY_ACTIVE


class TestChargerAllocation(unittest.TestCase):
    """Test charger allocation functionality in unified optimizer."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.planning_start = datetime(2024, 1, 15, 6, 0)
        self.planning_end = datetime(2024, 1, 15, 18, 0)
        self.time_slots = self._build_time_slots()
        
        # Create test vehicles
        self.vehicles = [
            Vehicle(
                vehicle_id=101,
                site_id=1,
                vehicle_alias="V101",
                battery_capacity=100.0,
                charge_power_ac=11.0,
                charge_power_dc=50.0,
                efficiency_kwh_mile=0.35,
                active=True
            ),
            Vehicle(
                vehicle_id=102,
                site_id=1,
                vehicle_alias="V102",
                battery_capacity=100.0,
                charge_power_ac=11.0,
                charge_power_dc=50.0,
                efficiency_kwh_mile=0.35,
                active=True
            ),
            Vehicle(
                vehicle_id=103,
                site_id=1,
                vehicle_alias="V103",
                battery_capacity=100.0,
                charge_power_ac=22.0,
                charge_power_dc=100.0,
                efficiency_kwh_mile=0.35,
                active=True
            ),
        ]
        
        # Create test chargers
        self.site_chargers = [
            Charger(charger_id=1, site_id=1, max_power_kw=11.0, is_dc=False),
            Charger(charger_id=2, site_id=1, max_power_kw=22.0, is_dc=False),
            Charger(charger_id=3, site_id=1, max_power_kw=50.0, is_dc=True),
        ]
        
        # Create price data
        self.price_data = {
            slot: (0.15, False) for slot in self.time_slots
        }
        
        # Create forecast data
        self.forecast_data = {
            slot: 50.0 for slot in self.time_slots
        }
        
        self.config = UnifiedOptimizationConfig(
            mode=OptimizationMode.SCHEDULING_ONLY,
            scheduling_time_limit=30,
            target_soc_percent=80.0,
            site_capacity_kw=1000.0
        )
    
    def _build_time_slots(self):
        """Build 30-minute time slots."""
        slots = []
        current = self.planning_start
        while current < self.planning_end:
            slots.append(current)
            current += timedelta(minutes=30)
        return slots
    
    def _create_vehicle_state(self, vehicle_id: int, current_soc_percent: float,
                             charger_id: int = None, last_nighttime_charger_id: int = None):
        """Create a vehicle charge state."""
        vehicle = next(v for v in self.vehicles if v.vehicle_id == vehicle_id)
        return VehicleChargeState(
            vehicle_id=vehicle_id,
            current_soc_percent=current_soc_percent,
            current_soc_kwh=(current_soc_percent / 100.0) * vehicle.battery_capacity,
            battery_capacity_kwh=vehicle.battery_capacity,
            is_connected=(charger_id is not None),
            charger_id=charger_id,
            charger_type='AC' if charger_id in [1, 2] else 'DC' if charger_id == 3 else None,
            ac_charge_rate_kw=vehicle.charge_power_ac,
            dc_charge_rate_kw=vehicle.charge_power_dc,
            last_nighttime_charger_id=last_nighttime_charger_id
        )
    
    def _create_availability_matrix(self, vehicle_id: int, all_available: bool = True):
        """Create availability matrix for vehicle."""
        return VehicleAvailability(
            vehicle_id=vehicle_id,
            time_slots=self.time_slots,
            availability_matrix=[all_available] * len(self.time_slots)
        )
    
    @unittest.skipIf(not IS_HEXALY_ACTIVE, "Hexaly not active")
    def test_fixed_charger_assignment_when_connected(self):
        """Test that vehicles already connected to a charger keep that assignment."""
        vehicle_states = {
            101: self._create_vehicle_state(101, 50.0, charger_id=1),  # Already connected to charger 1
            102: self._create_vehicle_state(102, 50.0, charger_id=2),  # Already connected to charger 2
            103: self._create_vehicle_state(103, 50.0),  # Not connected
        }
        
        availability_matrices = {
            v.vehicle_id: self._create_availability_matrix(v.vehicle_id)
            for v in self.vehicles
        }
        
        optimizer = UnifiedOptimizer(self.config)
        result = optimizer.solve(
            schedule_id=1,
            vehicles=self.vehicles,
            vehicle_states=vehicle_states,
            energy_requirements={},
            availability_matrices=availability_matrices,
            time_slots=self.time_slots,
            forecast_data=self.forecast_data,
            price_data=self.price_data,
            site_chargers=self.site_chargers
        )
        
        # Check that connected vehicles kept their chargers
        schedules_by_id = {s.vehicle_id: s for s in result.vehicle_schedules}
        
        self.assertEqual(schedules_by_id[101].assigned_charger_id, 1,
                        "Vehicle 101 should keep charger 1")
        self.assertEqual(schedules_by_id[102].assigned_charger_id, 2,
                        "Vehicle 102 should keep charger 2")
        self.assertIsNotNone(schedules_by_id[103].assigned_charger_id,
                            "Vehicle 103 should be assigned a charger")
    
    @unittest.skipIf(not IS_HEXALY_ACTIVE, "Hexaly not active")
    def test_optimal_charger_selection_by_power_requirement(self):
        """Test that optimizer selects chargers based on power requirements."""
        # Vehicle 103 has 22kW AC capacity, should prefer charger 2 (22kW) over charger 1 (11kW)
        vehicle_states = {
            101: self._create_vehicle_state(101, 30.0),  # Low SOC, standard 11kW vehicle
            102: self._create_vehicle_state(102, 30.0),  # Low SOC, standard 11kW vehicle
            103: self._create_vehicle_state(103, 30.0),  # Low SOC, 22kW capable vehicle
        }
        
        availability_matrices = {
            v.vehicle_id: self._create_availability_matrix(v.vehicle_id)
            for v in self.vehicles
        }
        
        optimizer = UnifiedOptimizer(self.config)
        result = optimizer.solve(
            schedule_id=1,
            vehicles=self.vehicles,
            vehicle_states=vehicle_states,
            energy_requirements={},
            availability_matrices=availability_matrices,
            time_slots=self.time_slots,
            forecast_data=self.forecast_data,
            price_data=self.price_data,
            site_chargers=self.site_chargers
        )
        
        schedules_by_id = {s.vehicle_id: s for s in result.vehicle_schedules}
        
        # Vehicle 103 should get a higher power charger (2 or 3)
        v103_charger = schedules_by_id[103].assigned_charger_id
        self.assertIn(v103_charger, [2, 3],
                     "High-power vehicle should get higher power charger")
        
        # All vehicles should be assigned chargers
        for v_id in [101, 102, 103]:
            self.assertIsNotNone(schedules_by_id[v_id].assigned_charger_id,
                                f"Vehicle {v_id} should be assigned a charger")
    
    @unittest.skipIf(not IS_HEXALY_ACTIVE, "Hexaly not active")
    def test_charger_capacity_respected(self):
        """Test that charging power respects charger max_power."""
        vehicle_states = {
            103: self._create_vehicle_state(103, 30.0, charger_id=1),  # 22kW vehicle on 11kW charger
        }
        
        availability_matrices = {
            103: self._create_availability_matrix(103)
        }
        
        optimizer = UnifiedOptimizer(self.config)
        result = optimizer.solve(
            schedule_id=1,
            vehicles=[self.vehicles[2]],  # Only vehicle 103
            vehicle_states=vehicle_states,
            energy_requirements={},
            availability_matrices=availability_matrices,
            time_slots=self.time_slots,
            forecast_data=self.forecast_data,
            price_data=self.price_data,
            site_chargers=self.site_chargers
        )
        
        schedule = result.vehicle_schedules[0]
        
        # Check that charging power never exceeds charger capacity
        for slot in schedule.charge_slots:
            self.assertLessEqual(slot.charge_power_kw, 11.0,
                               f"Power {slot.charge_power_kw} exceeds charger 1 capacity (11kW)")
    
    @unittest.skipIf(not IS_HEXALY_ACTIVE, "Hexaly not active")
    def test_nighttime_charger_continuity(self):
        """Test that vehicles continue using same charger during nighttime if they used it before."""
        # Set planning window to include nighttime hours
        self.planning_start = datetime(2024, 1, 15, 19, 0)  # 7 PM
        self.planning_end = datetime(2024, 1, 16, 7, 0)  # 7 AM next day
        self.time_slots = self._build_time_slots()
        
        # Update price and forecast data
        self.price_data = {slot: (0.10, False) for slot in self.time_slots}
        self.forecast_data = {slot: 50.0 for slot in self.time_slots}
        
        # Vehicle 101 used charger 1 during previous nighttime period
        vehicle_states = {
            101: self._create_vehicle_state(101, 50.0, charger_id=None, last_nighttime_charger_id=1),
        }
        
        availability_matrices = {
            101: self._create_availability_matrix(101)
        }
        
        optimizer = UnifiedOptimizer(self.config)
        result = optimizer.solve(
            schedule_id=1,
            vehicles=[self.vehicles[0]],
            vehicle_states=vehicle_states,
            energy_requirements={},
            availability_matrices=availability_matrices,
            time_slots=self.time_slots,
            forecast_data=self.forecast_data,
            price_data=self.price_data,
            site_chargers=self.site_chargers
        )
        
        # Vehicle should be assigned to charger 1 (continuity during nighttime)
        schedule = result.vehicle_schedules[0]
        self.assertEqual(schedule.assigned_charger_id, 1,
                        "Vehicle should continue using charger 1 during nighttime")
    
    @unittest.skipIf(not IS_HEXALY_ACTIVE, "Hexaly not active")
    def test_morning_allocation_allowed(self):
        """Test that chargers can be allocated in the morning (outside nighttime hours)."""
        # Morning window: 6 AM to 12 PM
        self.planning_start = datetime(2024, 1, 15, 6, 0)
        self.planning_end = datetime(2024, 1, 15, 12, 0)
        self.time_slots = self._build_time_slots()
        
        self.price_data = {slot: (0.15, False) for slot in self.time_slots}
        self.forecast_data = {slot: 50.0 for slot in self.time_slots}
        
        # Vehicle not connected, no nighttime history
        vehicle_states = {
            101: self._create_vehicle_state(101, 40.0),
        }
        
        availability_matrices = {
            101: self._create_availability_matrix(101)
        }
        
        optimizer = UnifiedOptimizer(self.config)
        result = optimizer.solve(
            schedule_id=1,
            vehicles=[self.vehicles[0]],
            vehicle_states=vehicle_states,
            energy_requirements={},
            availability_matrices=availability_matrices,
            time_slots=self.time_slots,
            forecast_data=self.forecast_data,
            price_data=self.price_data,
            site_chargers=self.site_chargers
        )
        
        # Vehicle should be assigned a charger
        schedule = result.vehicle_schedules[0]
        self.assertIsNotNone(schedule.assigned_charger_id,
                           "Vehicle should be assigned a charger in the morning")
        self.assertGreater(schedule.total_energy_scheduled_kwh, 0,
                          "Vehicle should receive charging in the morning")
    
    @unittest.skipIf(not IS_HEXALY_ACTIVE, "Hexaly not active")
    def test_multiple_vehicles_per_charger_time_multiplexed(self):
        """Test that multiple vehicles can use the same charger at different times."""
        # Create scenario where vehicles are available at different times
        availability_101 = VehicleAvailability(
            vehicle_id=101,
            time_slots=self.time_slots,
            availability_matrix=[True if i < 12 else False for i in range(len(self.time_slots))]  # First half
        )
        
        availability_102 = VehicleAvailability(
            vehicle_id=102,
            time_slots=self.time_slots,
            availability_matrix=[False if i < 12 else True for i in range(len(self.time_slots))]  # Second half
        )
        
        vehicle_states = {
            101: self._create_vehicle_state(101, 40.0),
            102: self._create_vehicle_state(102, 40.0),
        }
        
        availability_matrices = {
            101: availability_101,
            102: availability_102,
        }
        
        optimizer = UnifiedOptimizer(self.config)
        result = optimizer.solve(
            schedule_id=1,
            vehicles=self.vehicles[:2],
            vehicle_states=vehicle_states,
            energy_requirements={},
            availability_matrices=availability_matrices,
            time_slots=self.time_slots,
            forecast_data=self.forecast_data,
            price_data=self.price_data,
            site_chargers=self.site_chargers
        )
        
        schedules_by_id = {s.vehicle_id: s for s in result.vehicle_schedules}
        
        # Both vehicles should be assigned chargers
        self.assertIsNotNone(schedules_by_id[101].assigned_charger_id)
        self.assertIsNotNone(schedules_by_id[102].assigned_charger_id)
        
        # Both should have received charging
        self.assertGreater(schedules_by_id[101].total_energy_scheduled_kwh, 0)
        self.assertGreater(schedules_by_id[102].total_energy_scheduled_kwh, 0)
    
    def test_no_chargers_available_graceful_handling(self):
        """Test that optimizer handles case with no chargers gracefully."""
        vehicle_states = {
            101: self._create_vehicle_state(101, 50.0),
        }
        
        availability_matrices = {
            101: self._create_availability_matrix(101)
        }
        
        optimizer = UnifiedOptimizer(self.config)
        
        # Run with empty chargers list
        result = optimizer.solve(
            schedule_id=1,
            vehicles=[self.vehicles[0]],
            vehicle_states=vehicle_states,
            energy_requirements={},
            availability_matrices=availability_matrices,
            time_slots=self.time_slots,
            forecast_data=self.forecast_data,
            price_data=self.price_data,
            site_chargers=[]  # No chargers
        )
        
        # Should complete without error, vehicle gets scheduled with default charger handling
        self.assertEqual(len(result.vehicle_schedules), 1)


if __name__ == '__main__':
    unittest.main()
