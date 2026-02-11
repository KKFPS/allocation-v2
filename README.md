# Vehicle-Route Allocation System

Python implementation of the 18-hour rolling window allocation optimizer for electric vehicle fleet management.

## Features

- 18-hour rolling window optimization
- Modular constraint framework
- Hexaly Cloud optimization solver with greedy fallback
- PostgreSQL database integration
- MAF-based configuration management
- Testing framework for simulation

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

3. Run allocation:
```bash
python main.py --site-id 10
```

## Testing

Run the test framework:
```bash
python tests/test_framework.py --site-id 10 --start-time "2026-02-11 04:30:00" --window-hours 18
```

## Project Structure

- `src/` - Main application code
  - `database/` - Database connection and queries
  - `models/` - Data models (Vehicle, Route, Allocation)
  - `maf/` - MAF parameter parsing
  - `constraints/` - Modular constraint implementations
  - `optimizer/` - Hexaly solver integration
  - `controllers/` - Allocation orchestration
- `tests/` - Testing framework and test cases

## Architecture

See [ALLOCATION_SYSTEM_SPECIFICATION.md](ALLOCATION_SYSTEM_SPECIFICATION.md) for detailed technical specification.
# allocation-v2
