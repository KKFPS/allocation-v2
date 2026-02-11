# Allocation System Implementation - Quick Start

## What Was Built

A complete Python implementation of the 18-hour rolling window vehicle-route allocation optimizer with:

✅ **Database Integration** (psycopg2)
- Connection management with context managers
- SQL query repository
- Error handling and transaction management

✅ **Data Models**
- Vehicle (with state management)
- Route (with temporal logic)
- Allocation results

✅ **MAF Parameter Parsing**
- String-to-type conversion
- Constraint configuration extraction
- Site-specific parameter management

✅ **Modular Constraint Framework**
- Base constraint architecture
- 5 constraint implementations:
  - Energy Feasibility (hard)
  - Turnaround Time Strict (hard)
  - Turnaround Time Preferred (soft)
  - Shift Hours (hard)
  - Route Overlap Prevention (mandatory)
- Constraint manager for evaluation

✅ **Hexaly Optimizer**
- Cost matrix builder
- Set covering problem solver
- Greedy fallback solver
- Sequence generation and validation

✅ **Allocation Controller**
- Complete workflow orchestration
- MAF configuration loading
- Window management
- Result persistence

✅ **Testing Framework**
- Parameterized test scenarios
- Custom configuration support
- Result export and analysis

## Quick Start

1. **Install dependencies:**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

2. **Configure database:**
```bash
cp .env.example .env
# Edit .env with your PostgreSQL credentials
```

3. **Run allocation:**
```bash
python main.py --site-id 10
```

4. **Run tests:**
```bash
python tests/test_framework.py --site-id 10 --start-time "2026-02-11 04:30:00" --window-hours 18
```

## File Structure (29 files created)

```
allocation-v2/
├── src/
│   ├── config.py                           # Configuration
│   ├── database/
│   │   ├── connection.py                   # DB connection
│   │   └── queries.py                      # SQL queries
│   ├── models/
│   │   ├── vehicle.py                      # Vehicle model
│   │   ├── route.py                        # Route model
│   │   └── allocation.py                   # Allocation models
│   ├── maf/
│   │   └── parameter_parser.py             # MAF parsing
│   ├── constraints/
│   │   ├── base.py                         # Base constraint
│   │   ├── energy_feasibility.py           # Energy constraint
│   │   ├── turnaround_time.py             # Turnaround constraints
│   │   ├── shift_hours.py                  # Shift hour constraint
│   │   ├── route_overlap.py               # Overlap constraint
│   │   └── constraint_manager.py           # Manager
│   ├── optimizer/
│   │   ├── cost_matrix.py                  # Cost matrix
│   │   └── hexaly_solver.py               # Hexaly integration
│   ├── controllers/
│   │   └── allocation_controller.py        # Main controller
│   └── utils/
│       └── logging_config.py               # Logging
├── tests/
│   ├── test_framework.py                   # Test framework
│   └── sample_test.py                      # Sample tests
├── main.py                                 # Entry point
├── requirements.txt                        # Dependencies
├── .env.example                           # Env template
├── .gitignore                             # Git ignore
├── README.md                              # README
└── DEVELOPMENT.md                         # Dev guide
```

## Key Features

### 1. Database Integration
- Environment-based configuration
- Context managers for safe transactions
- Connection pooling support
- Comprehensive query repository

### 2. Modular Architecture
- Clean separation of concerns
- Dependency injection
- Easy to extend and test
- Configuration-driven behavior

### 3. Hexaly Optimization
- Set covering problem formulation
- Constraint-based filtering
- Sequence generation
- Fallback solver for robustness

### 4. Testing Framework
- Flexible scenario definition
- Time-based testing
- Custom configuration override
- Result export for analysis

### 5. MAF Integration
- String-based parameter storage
- Intelligent type parsing
- Site-specific configuration
- Constraint enable/disable

## Testing Examples

### Single Test
```bash
python tests/test_framework.py \
  --site-id 10 \
  --start-time "2026-02-11 04:30:00" \
  --window-hours 18 \
  --trigger-type initial
```

### Multiple Scenarios
```bash
python tests/test_framework.py --sample-scenarios --export results.json
```

### Custom Tests
```python
from datetime import datetime
from tests.test_framework import AllocationTestFramework

framework = AllocationTestFramework()
result = framework.run_test_scenario(
    site_id=10,
    start_time=datetime(2026, 2, 11, 4, 30),
    window_hours=18,
    trigger_type='initial',
    custom_config={
        'parameters': {
            'constraint_turnaround_time_strict_minimum_minutes': '30'
        }
    }
)
```

## Environment Variables

Required in `.env`:
```
psgrsql_db_user=your_user
psgrsql_db_pswd=your_password
psgrsql_db_name=your_db
psgrsql_db_host=localhost
psgrsql_db_port=5432
APPLICATION_NAME=vehicle_allocation_system
LOG_LEVEL=INFO
```

## Next Steps

1. ✅ Set up PostgreSQL database with schema
2. ✅ Configure `.env` with credentials
3. ✅ Populate test data (vehicles, routes)
4. ✅ Run test framework to validate
5. ✅ Configure MAF parameters per site
6. ✅ Deploy to production environment

## Dependencies

- `hexaly==14.0.20250814` - Optimization solver
- `psycopg2-binary==2.9.9` - PostgreSQL adapter
- `python-dotenv==1.0.0` - Environment management
- `pandas==2.1.4` - Data manipulation
- `numpy==1.26.2` - Numerical operations
- `openpyxl==3.1.2` - Excel support
- `requests==2.31.0` - HTTP client

## Support

See detailed documentation in:
- `DEVELOPMENT.md` - Development guide
- `README.md` - Project overview
- `ALLOCATION_SYSTEM_SPECIFICATION.md` - Full specification
