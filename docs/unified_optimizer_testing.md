# Unified Optimizer Test Framework

Comprehensive testing framework for the unified allocation and scheduling optimizer with configurable modes and start times.

## Overview

The `test_unified_optimizer.py` framework allows you to:
- Test allocation only (route-to-vehicle assignment)
- Test scheduling only (charge scheduling with pre-allocated routes)
- Test integrated mode (both allocation and scheduling in single model)
- Configure weighted sum objective (α * allocation_score - β * charging_cost)
- Run with defined start times and planning windows
- Export detailed results to JSON

## Quick Start

### Run Sample Scenarios
```bash
python tests/test_unified_optimizer.py --sample-scenarios
```

This runs 5 predefined scenarios covering all modes with different weight configurations.

### Test Single Scenario

**Allocation Only:**
```bash
python tests/test_unified_optimizer.py \
  --site-id 10 \
  --mode allocation_only \
  --start-time "2026-02-16 04:30:00" \
  --window-hours 18
```

**Scheduling Only:**
```bash
python tests/test_unified_optimizer.py \
  --site-id 10 \
  --mode scheduling_only \
  --start-time "2026-02-16 04:30:00" \
  --target-soc 95 \
  --site-capacity 200
```

**Integrated Mode (Balanced):**
```bash
python tests/test_unified_optimizer.py \
  --site-id 10 \
  --mode integrated \
  --start-time "2026-02-16 04:30:00" \
  --allocation-weight 1.0 \
  --scheduling-weight 1.0
```

**Integrated Mode (Favor Allocation):**
```bash
python tests/test_unified_optimizer.py \
  --site-id 10 \
  --mode integrated \
  --start-time "2026-02-16 04:30:00" \
  --allocation-weight 2.0 \
  --scheduling-weight 0.5
```

**Integrated Mode (Favor Scheduling):**
```bash
python tests/test_unified_optimizer.py \
  --site-id 10 \
  --mode integrated \
  --start-time "2026-02-16 04:30:00" \
  --allocation-weight 0.5 \
  --scheduling-weight 2.0
```

## Command Line Arguments

### Required (for single scenario)
- `--site-id`: Site ID to test
- `--start-time`: Optimization start time (format: "YYYY-MM-DD HH:MM:SS")

### Optimization Mode
- `--mode`: Mode to run (default: `integrated`)
  - `allocation_only` or `allocation`: Only route allocation
  - `scheduling_only` or `scheduling`: Only charge scheduling
  - `integrated` or `both`: Both phases in single model

### Planning Window
- `--window-hours`: Planning window duration in hours (default: 18)

### Objective Weights
- `--allocation-weight`: α weight for allocation score (default: 1.0)
- `--scheduling-weight`: β weight for scheduling cost (default: 1.0)
- `--route-count-weight`: Priority weight for route coverage (default: 100)

The objective function is:
```
maximize: α * (route_count_weight * routes_covered + sequence_scores) 
          - β * (charging_cost + shortfall_penalty)
```

**Examples:**
- `α=1.0, β=1.0`: Balanced (equal priority)
- `α=2.0, β=0.5`: Favor allocation (allocate more routes, less cost-optimal charging)
- `α=0.5, β=2.0`: Favor scheduling (may leave routes unallocated for better charging)

### Time Limits (seconds)
- `--allocation-time-limit`: Allocation phase time limit (default: 30)
- `--scheduling-time-limit`: Scheduling phase time limit (default: 300)
- `--integrated-time-limit`: Integrated mode time limit (default: 330)

### Scheduling Parameters
- `--target-soc`: Target SOC percentage (default: 95)
- `--site-capacity`: Site capacity in kW (default: 200)

### Other Options
- `--sample-scenarios`: Run predefined sample scenarios
- `--no-database`: Do not load data from database (requires custom data)
- `--export FILENAME`: Export results to JSON file

## Output Metrics

### Common Metrics
- **Status**: Optimization status (optimal, feasible, infeasible)
- **Objective Value**: Combined weighted objective
- **Solve Time**: Hexaly solver time
- **Total Time**: Including data preparation

### Allocation Metrics (allocation_only, integrated)
- **Routes Allocated/Total**: Number and percentage of routes covered
- **Allocation Score**: Total sequence score
- **Sequences Selected**: Number of vehicle-route sequences chosen
- **Vehicles Used**: Number of vehicles with routes

### Scheduling Metrics (scheduling_only, integrated)
- **Vehicles Scheduled**: Number of vehicles with charge schedules
- **Total Energy**: Total kWh scheduled
- **Total Cost**: Total electricity cost (£)
- **Avg Cost/kWh**: Average charging cost
- **Vehicles w/ Routes**: Vehicles with route energy requirements
- **Total Charge Slots**: Sum of charging time slots across all vehicles

## Sample Scenarios

The `--sample-scenarios` flag runs 5 predefined tests:

1. **Allocation Only** - Route assignment without charging
2. **Scheduling Only** - Charging without allocation
3. **Integrated (Balanced)** - α=1.0, β=1.0
4. **Integrated (Favor Allocation)** - α=2.0, β=0.5, delayed start
5. **Integrated (Favor Scheduling)** - α=0.5, β=2.0, delayed start

## Export Results

Export test results to JSON for further analysis:

```bash
python tests/test_unified_optimizer.py \
  --sample-scenarios \
  --export results.json
```

The JSON file contains:
- All test scenarios and configurations
- Detailed metrics for each run
- Success/failure status
- Timing information

## Programmatic Usage

```python
from tests.test_unified_optimizer import UnifiedOptimizerTestFramework
from datetime import datetime

# Create framework
framework = UnifiedOptimizerTestFramework()

# Run single test
result = framework.run_test_scenario(
    site_id=10,
    start_time=datetime(2026, 2, 16, 4, 30, 0),
    mode='integrated',
    window_hours=18,
    allocation_weight=1.5,
    scheduling_weight=1.0,
    use_database=True
)

print(f"Objective: {result['objective_value']:.2f}")
print(f"Routes: {result['allocation']['routes_allocated']}/{result['allocation']['routes_total']}")
print(f"Energy: {result['scheduling']['total_energy_kwh']:.2f} kWh")

# Run multiple scenarios
scenarios = [
    {'site_id': 10, 'start_time': datetime(2026, 2, 16, 4, 30, 0), 'mode': 'allocation_only'},
    {'site_id': 10, 'start_time': datetime(2026, 2, 16, 4, 30, 0), 'mode': 'scheduling_only'},
]

results = framework.run_multiple_scenarios(scenarios)

# Export
framework.export_results('my_results.json')
framework.close()
```

## Comparison with Allocation Test Framework

| Feature | test_framework.py | test_unified_optimizer.py |
|---------|------------------|---------------------------|
| **Purpose** | Only allocation controller | Unified optimizer (all modes) |
| **Modes** | Single (allocation) | 3 modes (alloc, sched, integrated) |
| **Objective** | Fixed (maximize routes + score) | Weighted sum (configurable α, β) |
| **Scheduling** | Separate phase via controller | Integrated or standalone |
| **Weights** | Not configurable | Full control over tradeoffs |
| **Use Case** | Test allocation logic | Test unified optimization model |

## Tips

1. **Start with sample scenarios** to understand output format
2. **Use allocation_only mode** to verify route assignment logic
3. **Use scheduling_only mode** to test charging optimization independently
4. **Try different weights** in integrated mode to explore tradeoffs
5. **Export results** for comparing different configurations
6. **Increase time limits** for larger problem instances

## Troubleshooting

**"Insufficient data for any optimization mode"**
- Ensure database connection or provide custom_sequences/custom_vehicles

**"Hexaly not active - using greedy fallback"**
- Check HEXALY_CLOUD_KEY and HEXALY_CLOUD_SECRET environment variables

**Very low allocation percentage**
- Try increasing allocation_weight or route_count_weight
- Check if routes/vehicles are feasible (energy, timing constraints)

**High charging costs**
- Reduce scheduling_weight to prioritize cost minimization
- Check site_capacity_kw (may be too restrictive)
- Review TRIAD penalties (may be forcing expensive charging)

## Related Files

- `/src/optimizer/unified_optimizer.py` - Unified optimizer implementation
- `/src/config.py` - Configuration constants
- `/docs/optimization_models.md` - Mathematical model documentation
- `/tests/test_framework.py` - Original allocation test framework
