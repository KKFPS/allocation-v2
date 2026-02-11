"""Sample test scenarios and usage examples."""
from datetime import datetime, timedelta
from tests.test_framework import AllocationTestFramework


def test_initial_morning_allocation():
    """Test initial morning allocation (04:30 AM)."""
    framework = AllocationTestFramework()
    
    start_time = datetime(2026, 2, 11, 4, 30, 0)
    
    result = framework.run_test_scenario(
        site_id=10,
        start_time=start_time,
        window_hours=18,
        trigger_type='initial'
    )
    
    assert result['success'], "Initial allocation failed"
    assert result['routes_allocated'] > 0, "No routes allocated"
    
    print(f"✓ Initial morning allocation test passed")


def test_mid_day_reallocation():
    """Test mid-day reallocation after route cancellation."""
    framework = AllocationTestFramework()
    
    start_time = datetime(2026, 2, 11, 11, 45, 0)
    
    result = framework.run_test_scenario(
        site_id=10,
        start_time=start_time,
        window_hours=18,
        trigger_type='cancellation'
    )
    
    assert result['success'], "Reallocation failed"
    
    print(f"✓ Mid-day reallocation test passed")


def test_custom_configuration():
    """Test allocation with custom constraint configuration."""
    framework = AllocationTestFramework()
    
    # Custom configuration with relaxed constraints
    custom_config = {
        'parameters': {
            'allocation_window_hours': 18,
            'max_routes_per_vehicle_in_window': 5,
            'constraint_turnaround_time_strict_enabled': 'true',
            'constraint_turnaround_time_strict_minimum_minutes': '30',  # Relaxed
            'constraint_energy_feasibility_enabled': 'true',
            'constraint_energy_feasibility_safety_margin_kwh': '3.0'  # Smaller margin
        },
        'enabled_vehicles': []  # All vehicles enabled
    }
    
    start_time = datetime(2026, 2, 11, 6, 0, 0)
    
    result = framework.run_test_scenario(
        site_id=10,
        start_time=start_time,
        window_hours=18,
        trigger_type='initial',
        custom_config=custom_config
    )
    
    print(f"✓ Custom configuration test passed")


def test_time_progression():
    """Test allocation across different times of day."""
    framework = AllocationTestFramework()
    
    base_date = datetime(2026, 2, 11)
    times = [
        (4, 30),   # Early morning
        (8, 0),    # Morning
        (12, 0),   # Noon
        (16, 0),   # Afternoon
        (20, 0)    # Evening
    ]
    
    scenarios = []
    for hour, minute in times:
        scenarios.append({
            'site_id': 10,
            'start_time': base_date.replace(hour=hour, minute=minute),
            'window_hours': 18,
            'trigger_type': 'initial'
        })
    
    results = framework.run_multiple_scenarios(scenarios)
    
    successful = sum(1 for r in results if r['success'])
    print(f"✓ Time progression test: {successful}/{len(scenarios)} successful")


if __name__ == '__main__':
    print("\nRunning sample tests...\n")
    
    # Run individual tests
    try:
        test_initial_morning_allocation()
    except Exception as e:
        print(f"✗ Initial morning allocation test failed: {e}")
    
    try:
        test_mid_day_reallocation()
    except Exception as e:
        print(f"✗ Mid-day reallocation test failed: {e}")
    
    try:
        test_custom_configuration()
    except Exception as e:
        print(f"✗ Custom configuration test failed: {e}")
    
    try:
        test_time_progression()
    except Exception as e:
        print(f"✗ Time progression test failed: {e}")
    
    print("\nAll tests completed!\n")
