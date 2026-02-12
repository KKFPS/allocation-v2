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

```bash
# Allocation only
python main.py --site-id 10

# Scheduling only
python scheduler_main.py --site-id 10

# Integrated workflow (allocation + scheduling)
python integrated_main.py --site-id 10
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
