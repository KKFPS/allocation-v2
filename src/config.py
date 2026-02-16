"""Configuration management for allocation system."""
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Database Configuration
DB_CONFIG = {
    'user': os.getenv('psgrsql_db_user'),
    'password': os.getenv('psgrsql_db_pswd'),
    'database': os.getenv('psgrsql_db_name'),
    'host': os.getenv('psgrsql_db_host'),
    'port': os.getenv('psgrsql_db_port', '5432')
}

# Hexaly Cloud Configuration
# Import here to avoid circular dependency with logging
import hexaly.optimizer

cloud_key = os.getenv("HEXALY_CLOUD_KEY")
cloud_secret = os.getenv("HEXALY_CLOUD_SECRET")

IS_HEXALY_ACTIVE = False

if not cloud_key or not cloud_secret:
    print("WARNING: HEXALY_CLOUD_KEY or HEXALY_CLOUD_SECRET not set - Using greedy solver fallback")
else:
    IS_HEXALY_ACTIVE = True
    print("INFO: Activating HEXALY solver")
    if os.getenv("HEXALY_LOCAL_AVAILABLE") == "true":
        print("INFO: HEXALY_LOCAL_AVAILABLE is true - Using local solver")
    else:
        license_text = f"""
CLOUD_KEY = {cloud_key}
CLOUD_SECRET = {cloud_secret}
"""
        hexaly.optimizer.HxVersion.set_license_content(license_text)

# Application Configuration
APPLICATION_NAME = os.getenv('WEBSITE_SITE_NAME', 'vehicle_allocation_system')
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')

# Default System Parameters
DEFAULT_ALLOCATION_WINDOW_HOURS = 18
DEFAULT_MAX_ROUTES_PER_VEHICLE = 5
DEFAULT_RESERVE_VEHICLE_COUNT = 2
DEFAULT_TURNAROUND_TIME_MINUTES = 45

# Default Scheduler Parameters
DEFAULT_PLANNING_WINDOW_HOURS = 24.0
DEFAULT_ROUTE_ENERGY_SAFETY_FACTOR = 1.15
DEFAULT_MIN_DEPARTURE_BUFFER_MINUTES = 60
DEFAULT_BACK_TO_BACK_THRESHOLD_MINUTES = 90
DEFAULT_TARGET_SOC_PERCENT = 75.0
# Minimum SOC (%) that vehicles must reach when charging (configurable in code)
DEFAULT_MIN_SOC_PERCENT = 75.0
DEFAULT_BATTERY_FACTOR = 1.0
DEFAULT_POWER_FACTOR = 0.85
DEFAULT_SITE_USAGE_FACTOR = 0.90
DEFAULT_TRIAD_PENALTY_FACTOR = 100.0
DEFAULT_SYNTHETIC_TIME_PRICE_FACTOR = 0.01
DEFAULT_SCHEDULER_TIME_LIMIT_SECONDS = 300
DEFAULT_FLEET_EFFICIENCY_KWH_MILE = 0.35
MINIMUM_PLANNING_WINDOW_HOURS = 4.0
SCHEDULE_EXPIRATION_HOURS = 2.0

# Default Constraint Penalties
DEFAULT_PENALTIES = {
    'energy_feasibility': -20,
    'turnaround_time_strict': -22,
    'turnaround_time_preferred': -2,
    'shift_hours_strict': -20,
    'minimum_soonness': -20,
    'route_overlap': -20,
    'charger_preference': 3,
    'swap_minimization': 0.5,
    'energy_optimization': 0.5
}

DEFAULT_CONSTRAINT_ENABLED = {
    'charger_preference': False,
    'energy_feasibility': True,
    'turnaround_time_strict': True,
    'turnaround_time_preferred': True,
    'shift_hours_strict': True,
    'minimum_soonness': True,
    'route_overlap': True,
    'swap_minimization': True,
    'energy_optimization': True,
}

# Unified Optimizer Configuration
# Enables combined allocation + scheduling in single model
USE_UNIFIED_OPTIMIZER = False  # Set True to use unified optimizer

# Unified optimizer weights (weighted sum objective)
# Objective: α * allocation_score - β * scheduling_cost
UNIFIED_ALLOCATION_WEIGHT = 1.0      # α: weight for allocation term
UNIFIED_SCHEDULING_WEIGHT = 1.0      # β: weight for scheduling cost term
UNIFIED_ROUTE_COUNT_WEIGHT = 1e2     # Priority weight for route coverage
UNIFIED_SOC_SHORTFALL_PENALTY = 0.2  # Penalty per kWh shortfall from target

# Unified optimizer time limits (seconds)
UNIFIED_ALLOCATION_TIME_LIMIT = 30
UNIFIED_SCHEDULING_TIME_LIMIT = 300
UNIFIED_INTEGRATED_TIME_LIMIT = 330

