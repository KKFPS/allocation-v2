"""Simple unit test for interval-based scheduling."""
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.optimizer import (
    UnifiedOptimizer, 
    UnifiedOptimizationConfig, 
    OptimizationMode
)
from src.models.vehicle import Vehicle
from src.models.scheduler import (
    VehicleChargeState, VehicleAvailability, ChargerPowerClass
)
from src.utils.logging_config import logger


def create_test_vehicle(vehicle_id: int) -> Vehicle:
    """Create a test vehicle."""
    return Vehicle(
        vehicle_id=vehicle_id,
        registration_number=f"TEST{vehicle_id:03d}",
        vehicle_type="Van",
        vin=f"VIN{vehicle_id:03d}",
        site_id=10,
        battery_capacity=75.0,
        charge_power_ac=11.0,
        charge_power_dc=50.0,
        efficiency_kwh_mile=0.35,
        estimated_soc=50.0,
        current_charger_id=None,
        enabled=True
    )


def create_test_vehicle_state(vehicle_id: int) -> VehicleChargeState:
    """Create a test vehicle charge state."""
    return VehicleChargeState(
        vehicle_id=vehicle_id,
        current_soc_percent=50.0,
        current_soc_kwh=37.5,  # 50% of 75 kWh
        battery_capacity_kwh=75.0,
        is_connected=True,
        charger_id=1,
        charger_type='AC',
        ac_charge_rate_kw=11.0,
        dc_charge_rate_kw=50.0,
        efficiency_kwh_mile=0.35,
        status='Idle'
    )


def create_time_slots(start_time: datetime, hours: int) -> List[datetime]:
    """Create 30-minute time slots."""
    slots = []
    current = start_time
    end = start_time + timedelta(hours=hours)
    
    while current < end:
        slots.append(current)
        current += timedelta(minutes=30)
    
    return slots


def create_availability_matrix(vehicle_id: int, time_slots: List[datetime]) -> VehicleAvailability:
    """Create availability matrix (all available)."""
    return VehicleAvailability(
        vehicle_id=vehicle_id,
        time_slots=time_slots,
        availability_matrix=[True] * len(time_slots)
    )


def create_price_data(time_slots: List[datetime]) -> Dict[datetime, tuple]:
    """Create simple price data."""
    price_data = {}
    for slot in time_slots:
        hour = slot.hour
        if 0 <= hour < 7:
            price = 0.10  # Night
        else:
            price = 0.20  # Day
        price_data[slot] = (price, False)  # (price, is_triad)
    return price_data


def test_scheduling_only_basic():
    """Test basic scheduling-only optimization with interval variables."""
    logger.info("\n" + "="*70)
    logger.info("TEST: Scheduling Only (Basic Interval Test)")
    logger.info("="*70)
    
    # Setup
    start_time = datetime(2026, 5, 21, 4, 0, 0)  # 4 AM
    n_vehicles = 3
    window_hours = 12
    
    # Create test data
    vehicles = [create_test_vehicle(i) for i in range(1, n_vehicles + 1)]
    vehicle_states = {v.vehicle_id: create_test_vehicle_state(v.vehicle_id) for v in vehicles}
    time_slots = create_time_slots(start_time, window_hours)
    price_data = create_price_data(time_slots)
    availability_matrices = {
        v.vehicle_id: create_availability_matrix(v.vehicle_id, time_slots)
        for v in vehicles
    }
    
    # Energy requirements (empty - just charge to target SOC)
    energy_requirements = {v.vehicle_id: [] for v in vehicles}
    
    # Create charger power classes
    site_chargers = [
        ChargerPowerClass(
            max_power_kw=7.0,
            count=2,
            charger_ids=[1, 2],
            is_dc=False
        ),
        ChargerPowerClass(
            max_power_kw=11.0,
            count=1,
            charger_ids=[3],
            is_dc=False
        )
    ]
    
    # Configure optimizer
    config = UnifiedOptimizationConfig(
        mode=OptimizationMode.SCHEDULING_ONLY,
        scheduling_time_limit=30,
        target_soc_percent=80.0,
        site_capacity_kw=100.0,
        enable_charger_allocation=True,
        makespan_penalty_weight=0.1
    )
    
    # Run optimization
    optimizer = UnifiedOptimizer(config)
    
    try:
        result = optimizer.solve(
            schedule_id=1,
            vehicles=vehicles,
            vehicle_states=vehicle_states,
            energy_requirements=energy_requirements,
            availability_matrices=availability_matrices,
            time_slots=time_slots,
            forecast_data={},
            price_data=price_data,
            site_chargers=site_chargers,
            fix_scheduling=False
        )
        
        # Validate results
        logger.info(f"\nStatus: {result.status}")
        logger.info(f"Solve Time: {result.solve_time_seconds:.2f}s")
        logger.info(f"Total Energy: {result.total_energy_kwh:.2f} kWh")
        logger.info(f"Total Cost: £{result.total_charging_cost:.2f}")
        logger.info(f"Vehicles Scheduled: {len(result.vehicle_schedules)}")
        
        # Check vehicle schedules
        for vs in result.vehicle_schedules:
            logger.info(
                f"  Vehicle {vs.vehicle_id}: {len(vs.charge_slots)} charge slots, "
                f"{vs.total_energy_scheduled_kwh:.2f} kWh, "
                f"Charger: {vs.assigned_charger_power_kw}kW ({vs.charger_type})"
            )
        
        # Basic validation
        assert result.status in ['OPTIMAL', 'FEASIBLE', 'feasible', 'optimal'], \
            f"Expected feasible/optimal solution, got {result.status}"
        assert len(result.vehicle_schedules) == n_vehicles, \
            f"Expected {n_vehicles} schedules, got {len(result.vehicle_schedules)}"
        assert result.total_energy_kwh >= 0, \
            f"Expected non-negative energy, got {result.total_energy_kwh}"
        
        logger.info("\n✓ TEST PASSED: Interval-based scheduling works!")
        return True
        
    except Exception as e:
        logger.error(f"\n✗ TEST FAILED: {e}", exc_info=True)
        return False


if __name__ == '__main__':
    success = test_scheduling_only_basic()
    sys.exit(0 if success else 1)
