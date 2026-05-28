# Vehicle-Route Allocation & Charge Scheduling System

Python implementation of the 18-hour rolling window allocation optimizer and charge scheduler for electric vehicle fleet management.

## Features

### Allocation System
- 18-hour rolling window optimization
- Modular constraint framework
- Hexaly Cloud optimization solver with greedy fallback
- PostgreSQL database integration
- MAF-based configuration management
- Testing framework for simulation

### Charge Scheduler (NEW)
- Configurable planning windows (4-24 hours)
- Multi-route energy requirements
- Route-aware vehicle availability
- Fleet efficiency averaging
- Dynamic window adaptation
- Dual operation modes (independent & post-allocation)

## Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Configure environment variables:
```bash
cp .env.example .env
# Edit .env with your database and Hexaly Cloud credentials
# Required: HEXALY_CLOUD_KEY and HEXALY_CLOUD_SECRET
```

3. Run systems:

Run API using uvicorn src.api.unified_api:app --reload --host 0.0.0.0 --port 8000
```bash
http://localhost:8000/optimize/unified

{
	"site_id": 513,
	"mode": ["charge_scheduling", "charger_allocation"],
	"allocation_score_weight": 2.0,
	"test_start_time": "2026-03-21 00:00:00",
	"scheduling_cost_weight": 0.5,
	"window_hours": 24,
	"persist_to_database": true,
	"microlise_enabled": false,
	"microlise_simulate": false
}

```

## Unified Controller (NEW)

The unified controller provides a single optimization model that can run:
- **Allocation only**: Maximize routes allocated with sequence scoring
- **Scheduling only**: Minimize charging cost for pre-allocated routes
- **Integrated**: Weighted sum of allocation score and (negative) charging cost

### Key Benefits
- Single optimization model with consistent data loading
- All workflows from allocation and scheduler controllers are covered
- Supports weighted objective tuning (α for allocation, β for scheduling)
- Simplified testing and validation
- Unified database persistence

### Usage

### Testing

```bash
# Run sample test scenarios
python tests/test_unified_optimizer.py --sample-scenarios

# Run specific test
python tests/test_unified_optimizer.py --site-id 10 --mode integrated \
    --start-time "2026-02-16 04:30:00" --persist-to-database

# Test with custom weights
python tests/test_unified_optimizer.py --site-id 10 --mode integrated \
    --allocation-weight 2.0 --scheduling-weight 0.5
```

## Quick Start

### Allocation
```bash
python main.py --site-id 10 --trigger-type initial
```

### Standalone Scheduler
```bash
# Independent mode (uses t_route_plan.vehicle_id)
python scheduler_main.py --site-id 10

# Post-allocation mode (uses t_route_allocated)
python scheduler_main.py --site-id 10 --route-source allocated
```

### Integrated Workflow
```bash
# Run both allocation and scheduling
python integrated_main.py --site-id 10

# Custom configuration
python integrated_main.py --site-id 10 \
  --trigger cancellation \
  --planning-window 12
```

## Documentation

- **[Scheduler Quick Start](SCHEDULER_QUICKSTART.md)** - Get started with charge scheduling
- **[Scheduler README](SCHEDULER_README.md)** - Complete scheduler documentation
- **[Scheduler Specification](SCHEDULER_SPEC.md)** - Detailed technical specification
- **[Allocation Quickstart](QUICKSTART.md)** - Allocation system guide

## Testing

Run the test framework:
```bash
# Allocation tests
python tests/test_framework.py --site-id 10 --start-time "2026-02-11 04:30:00" --window-hours 18

# Scheduler tests (TODO)
pytest tests/test_scheduler.py
```

## Project Structure

- `src/` - Main application code
  - `database/` - Database connection and queries
  - `models/` - Data models (Vehicle, Route, Allocation, Scheduler)
  - `maf/` - MAF parameter parsing
  - `constraints/` - Modular constraint implementations
  - `optimizer/` - Hexaly solver integration
  - `controllers/` - Allocation and scheduling orchestration
- `tests/` - Testing framework and test cases
- `main.py` - Allocation entry point
- `scheduler_main.py` - Scheduler entry point
- `integrated_main.py` - Integrated workflow entry point

## Architecture

See [ALLOCATION_SYSTEM_SPECIFICATION.md](ALLOCATION_SYSTEM_SPECIFICATION.md) for detailed technical specification.
# allocation-v2
# optimizer-v2
