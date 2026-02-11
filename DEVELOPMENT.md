# Development and Usage Guide

## Installation

1. **Create virtual environment:**
```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

2. **Install dependencies:**
```bash
pip install -r requirements.txt
```

3. **Configure environment:**
```bash
cp .env.example .env
# Edit .env with your PostgreSQL and Hexaly Cloud credentials
# Required environment variables:
# - psgrsql_db_user, psgrsql_db_pswd, psgrsql_db_name, psgrsql_db_host, psgrsql_db_port
# - HEXALY_CLOUD_KEY, HEXALY_CLOUD_SECRET
# Optional:
# - HEXALY_LOCAL_AVAILABLE=true (to use local solver instead of cloud)
```

## Hexaly Cloud Configuration

The system uses Hexaly Cloud for optimization. You need to obtain Hexaly Cloud credentials:

1. Sign up for Hexaly Cloud at https://www.hexaly.com/
2. Get your cloud API key and secret
3. Set `HEXALY_CLOUD_KEY` and `HEXALY_CLOUD_SECRET` in your .env file

If Hexaly Cloud credentials are not provided, the system will automatically fall back to a greedy heuristic solver.

For local development with a Hexaly license, set `HEXALY_LOCAL_AVAILABLE=true`.

## Database Setup

Ensure your PostgreSQL database has the required tables:
- `t_allocation_monitor`
- `t_route_plan`
- `t_route_allocated`
- `t_route_allocated_history`
- `t_vehicle`
- `t_vsm`
- `t_vehicle_charge`
- `t_vehicle_telematics`
- `t_charger`
- `t_alert`
- `t_error_log`

The stored procedure `sp_get_module_params` should be available for MAF configuration.

## Running Allocation

### Basic Usage

```bash
# Run allocation for site 10
python main.py --site-id 10

# With specific trigger type
python main.py --site-id 10 --trigger-type cancellation

# With custom start time
python main.py --site-id 10 --start-time "2026-02-11 04:30:00"
```

### Testing Framework

```bash
# Run test framework with specific parameters
python tests/test_framework.py --site-id 10 --start-time "2026-02-11 04:30:00" --window-hours 18

# Run sample scenarios
python tests/test_framework.py --sample-scenarios

# Export results
python tests/test_framework.py --sample-scenarios --export test_results.json

# Run sample tests
python tests/sample_test.py
```

## Project Structure

```
allocation-v2/
├── src/
│   ├── config.py                    # Configuration management
│   ├── database/                    # Database layer
│   │   ├── connection.py           # Database connection
│   │   └── queries.py              # SQL queries
│   ├── models/                     # Data models
│   │   ├── vehicle.py             # Vehicle model
│   │   ├── route.py               # Route model
│   │   └── allocation.py          # Allocation models
│   ├── maf/                       # MAF parameter parsing
│   │   └── parameter_parser.py
│   ├── constraints/               # Modular constraints
│   │   ├── base.py               # Base constraint class
│   │   ├── energy_feasibility.py
│   │   ├── turnaround_time.py
│   │   ├── shift_hours.py
│   │   ├── route_overlap.py
│   │   └── constraint_manager.py
│   ├── optimizer/                # Optimization engine
│   │   ├── cost_matrix.py       # Cost matrix builder
│   │   └── hexaly_solver.py     # Hexaly integration
│   ├── controllers/              # Orchestration
│   │   └── allocation_controller.py
│   └── utils/
│       └── logging_config.py
├── tests/
│   ├── test_framework.py         # Testing framework
│   └── sample_test.py            # Sample tests
├── main.py                       # Main entry point
├── requirements.txt
├── .env.example
└── README.md
```

## Key Components

### 1. Allocation Controller
Orchestrates the entire allocation process:
- Initializes allocation run
- Loads MAF configuration
- Defines 18-hour window
- Loads vehicles and routes
- Runs optimization
- Persists results

### 2. Constraint Framework
Modular constraints that can be enabled/disabled per site via MAF:
- Energy Feasibility
- Turnaround Time (Strict & Preferred)
- Shift Hours
- Route Overlap Prevention
- Charger Preference
- Swap Minimization
- Energy Optimization

### 3. Hexaly Optimizer
Uses Hexaly solver to find optimal vehicle-route assignments:
- Generates feasible route sequences
- Builds cost matrix
- Solves set covering problem
- Maximizes total score

### 4. MAF Parameter Parser
Parses string-based MAF parameters to appropriate types:
- Boolean detection
- Numeric detection
- JSON array/object parsing
- Time format parsing

## Configuration

### Environment Variables

```bash
# Database
psgrsql_db_user=your_username
psgrsql_db_pswd=your_password
psgrsql_db_name=allocation_db
psgrsql_db_host=localhost
psgrsql_db_port=5432

# Application
APPLICATION_NAME=vehicle_allocation_system
LOG_LEVEL=INFO
```

### MAF Parameters

Example MAF configuration (stored as {String: String}):

```json
{
  "allocation_window_hours": "18",
  "max_routes_per_vehicle_in_window": "5",
  "constraint_energy_feasibility_enabled": "true",
  "constraint_energy_feasibility_safety_margin_kwh": "5.0",
  "constraint_turnaround_time_strict_enabled": "true",
  "constraint_turnaround_time_strict_minimum_minutes": "45"
}
```

## Testing

The testing framework allows you to:
1. Run allocation for specific timeframes
2. Test different trigger types
3. Apply custom configurations
4. Compare multiple scenarios
5. Export results for analysis

### Example Test Scenario

```python
from datetime import datetime
from tests.test_framework import AllocationTestFramework

framework = AllocationTestFramework()

result = framework.run_test_scenario(
    site_id=10,
    start_time=datetime(2026, 2, 11, 4, 30, 0),
    window_hours=18,
    trigger_type='initial'
)

print(f"Score: {result['total_score']}")
print(f"Allocated: {result['routes_allocated']}/{result['routes_in_window']}")
```

## Troubleshooting

### Database Connection Issues
- Verify credentials in `.env`
- Check PostgreSQL is running
- Ensure database user has appropriate permissions

### Hexaly License Issues
- Verify Hexaly is installed: `pip show hexaly`
- Check license file location
- Consider using greedy fallback for testing

### Low Allocation Scores
- Review constraint configurations
- Check vehicle availability and energy levels
- Verify route data quality
- Review logs for constraint violations

## Performance

Typical performance metrics:
- **Small sites (10-20 routes):** 2-5 seconds
- **Medium sites (30-50 routes):** 5-15 seconds
- **Large sites (60+ routes):** 15-30 seconds

Optimization time scales with:
- Number of vehicles
- Number of routes
- Maximum routes per vehicle
- Number of enabled constraints

## Logging

Logs are written to:
- Console (stdout)
- File: `allocation_system.log`

Log levels:
- `DEBUG`: Detailed constraint evaluations
- `INFO`: Progress updates and results
- `WARNING`: Non-critical issues
- `ERROR`: Failures and exceptions

## Next Steps

1. **Database Schema:** Ensure all required tables exist
2. **MAF Configuration:** Set up site-specific parameters
3. **Test Data:** Populate test routes and vehicles
4. **Run Tests:** Verify system works with test framework
5. **Production:** Deploy and schedule allocation runs
