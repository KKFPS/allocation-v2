# Constraint Manager, Optimizer, and Integration Parameters

This document describes all parameters used by the constraint manager, parameters that feed into the optimizer, related environment variables, and MAF (Module Application Framework) parameters. It covers `unified_controller`, `constraint_manager`, `CostMatrixBuilder`, `UnifiedOptimizer`, and the Microlise integration.

---

## 1. Environment variables

### Application and database

| Variable | Used in | Description | Default / notes |
|----------|---------|-------------|-----------------|
| `WEBSITE_SITE_NAME` | `config.py` | Application name used when calling MAF `sp_get_module_params`. | `vehicle_allocation_system` |
| `LOG_LEVEL` | `config.py` | Logging level. | `INFO` |
| `psgrsql_db_user` | `config.py` | PostgreSQL user. | Required for DB. |
| `psgrsql_db_pswd` | `config.py` | PostgreSQL password. | Required for DB. |
| `psgrsql_db_name` | `config.py` | PostgreSQL database name. | Required for DB. |
| `psgrsql_db_host` | `config.py` | PostgreSQL host. | Required for DB. |
| `psgrsql_db_port` | `config.py` | PostgreSQL port. | `5432` |

### Hexaly solver

| Variable | Used in | Description | Default / notes |
|----------|---------|-------------|-----------------|
| `HEXALY_CLOUD_KEY` | `config.py` | Hexaly cloud license key. | If unset, greedy solver fallback. |
| `HEXALY_CLOUD_SECRET` | `config.py` | Hexaly cloud secret. | If unset, greedy solver fallback. |
| `HEXALY_LOCAL_AVAILABLE` | `config.py` | Set to `"true"` to use local solver. | Optional. |

### Microlise integration

| Variable | Used in | Description | Default / notes |
|----------|---------|-------------|-----------------|
| `JLP_Microlise_TokenClientId` | `config.py`, `microlise.py` | OAuth2 client ID for Microlise token. | Empty string disables. |
| `JLP_Microlise_TokenClient_Secret` | `config.py`, `microlise.py` | OAuth2 client secret. | Empty string disables. |
| `JLP_Microlise_Token_URL` | `config.py`, `microlise.py` | Microlise token endpoint URL. | Empty string disables. |
| `JLP_Microlise_JourneysWebAPI_URL` | `config.py`, `microlise.py` | Microlise Journeys API base URL. | Empty string disables. |
| `storage_account_conn_string` | `config.py`, `microlise.py` | Azure Storage connection string for reports. | Empty string disables. |
| `allocation_blob_container` | `config.py`, `microlise.py` | Blob container name for allocation reports. | Empty string disables. |
| `allocation_blob_dir` | `config.py`, `microlise.py` | Blob directory prefix for reports. | Empty string disables. |
| `simulate_response` | `config.py` | Set to `"True"` to skip real Microlise API calls. | `"True"` (synthetic 201). |
| `send_report` | `config.py` | Set to `"True"` to generate and upload Excel allocation report. | `"False"` |

---

## 2. MAF (Module Application Framework) parameters

MAF parameters are loaded via `sp_get_module_params(APPLICATION_NAME)` and parsed by `parse_maf_response()`. The result is a dict keyed by **site_id**; each value has:

- **`parameters`**: flat dict of `parameter_name` → parsed value (used for site-level and constraint params).
- **`enabled_vehicles`**: list of vehicle IDs that are enabled for allocation at that site.

Controllers must use the config for the current site, e.g. `site_config = site_configs.get(site_id, {})` or equivalent, so that `site_config['parameters']` and `site_config['enabled_vehicles']` are the per-site values.

### 2.1 Site-level MAF parameters (used by controller / optimizer)

These are read with `get_site_parameter(site_config, param_key, default)` from `site_config['parameters']`:

| MAF parameter name | Used in | Description | Code default |
|--------------------|---------|-------------|--------------|
| `allocation_window_hours` | `unified_controller`, `allocation_controller` | Planning window length (hours). | `DEFAULT_ALLOCATION_WINDOW_HOURS` (18) |
| `max_routes_per_vehicle_in_window` | `unified_controller`, `allocation_controller` | Max routes per vehicle in the allocation window. Passed to `CostMatrixBuilder`. | `DEFAULT_MAX_ROUTES_PER_VEHICLE` (5) |

### 2.2 Constraint MAF parameters (prefix `constraint_<name>_`)

Constraint configs are built by `get_all_constraint_configs(site_id, site_config)`, which for each constraint:

- Reads **`constraint_<constraint_name>_enabled`** (boolean).
- Reads all **`constraint_<constraint_name>_<param>`** into `params`.
- Uses **`constraint_<constraint_name>_penalty`** or `DEFAULT_PENALTIES[constraint_name]` for penalty.

Constraint names in MAF: `energy_feasibility`, `turnaround_time_strict`, `turnaround_time_preferred`, `shift_hours_strict`, `minimum_soonness`, `route_overlap`, `charger_preference`, `swap_minimization`, `energy_optimization`. Only the ones implemented in `ConstraintManager` are used (see below).

---

## 3. Constraint manager and constraint parameters

`ConstraintManager` is initialized with `constraint_configs` from `get_all_constraint_configs()`. Each constraint config has:

- **`enabled`**: bool
- **`params`**: dict of constraint-specific params
- **`penalty`**: number (default from `DEFAULT_PENALTIES` in `config.py`)

Default penalties and default-enabled state are in `config.py`: `DEFAULT_PENALTIES`, `DEFAULT_CONSTRAINT_ENABLED`.

### 3.1 energy_feasibility

- **Class**: `EnergyFeasibilityConstraint`
- **Hard constraint**: Yes
- **MAF enabled key**: `constraint_energy_feasibility_enabled`
- **MAF params prefix**: `constraint_energy_feasibility_`

| Param | Type | Description | Default in code |
|-------|------|-------------|-----------------|
| `safety_margin_kwh` | float | Extra kWh required above route energy. | `5.0` |
| `allow_dc_charging` | bool | Whether to allow DC charging between routes. | `True` |

**Extra kwargs from CostMatrixBuilder**: `vehicle_charger_map`, `all_routes`, `all_vehicles`. Optional: `site_chargers` (list of charger dicts with `charger_id`, `max_power`) for charge power cap; if not passed, charger cap is not applied.

### 3.2 turnaround_time_strict

- **Class**: `TurnaroundTimeStrictConstraint`
- **Hard constraint**: Yes
- **MAF enabled key**: `constraint_turnaround_time_strict_enabled`
- **MAF params prefix**: `constraint_turnaround_time_strict_`

| Param | Type | Description | Default in code |
|-------|------|-------------|-----------------|
| `minimum_minutes` | int | Minimum minutes between end of one route and start of next. | `45` |

### 3.3 turnaround_time_preferred

- **Class**: `TurnaroundTimePreferredConstraint`
- **Hard constraint**: No (soft)
- **MAF enabled key**: `constraint_turnaround_time_preferred_enabled`
- **MAF params prefix**: `constraint_turnaround_time_preferred_`

| Param | Type | Description | Default in code |
|-------|------|-------------|-----------------|
| `standard_minutes` | int | Preferred minimum turnaround (minutes); below this applies `penalty_standard`. | `75` |
| `optimal_minutes` | int | Optimal minimum turnaround (minutes); below this applies `penalty_optimal`. | `90` |
| `penalty_standard` | float | Penalty when below `standard_minutes`. | `-2` |
| `penalty_optimal` | float | Penalty when below `optimal_minutes` but ≥ `standard_minutes`. | `-1` |

### 3.4 shift_hours_strict

- **Class**: `ShiftHoursStrictConstraint`
- **Hard constraint**: Yes
- **MAF enabled key**: `constraint_shift_hours_strict_enabled`
- **MAF params prefix**: `constraint_shift_hours_strict_`

| Param | Type | Description | Default in code |
|-------|------|-------------|-----------------|
| `max_hours` | float | Maximum allowed shift length (hours). | `16` |
| `calculation_method` | str | `'first_to_last'` = first route start to last route end; `'cumulative'` = sum of route durations. | `'first_to_last'` |
| `pre_shift_buffer_hours` | float | Buffer added before first route. | `0.5` |
| `post_shift_buffer_hours` | float | Buffer added after last route. | `0.5` |

### 3.5 route_overlap

- **Class**: `RouteOverlapConstraint`
- **Hard constraint**: Yes (mandatory; always enabled in `ConstraintManager`)
- **MAF enabled key**: `constraint_route_overlap_enabled` (ignored; forced enabled)
- **MAF params prefix**: `constraint_route_overlap_`

| Param / kwargs | Type | Description | Default in code |
|----------------|------|-------------|-----------------|
| `turnaround_minutes` | int (kwargs) | Minimum minutes between routes for overlap check (passed to `Route.overlaps_with()`). | `0` (not passed by CostMatrixBuilder) |

### 3.6 charger_preference

- **Class**: `ChargerPreferenceConstraint`
- **Hard constraint**: No (soft)
- **MAF enabled key**: `constraint_charger_preference_enabled`
- **MAF params prefix**: `constraint_charger_preference_`

| Param | Type | Description | Default in code |
|-------|------|-------------|-----------------|
| `map` | str | Charger priority map. **New format**: `[87,86]:3,[85,83]:0,[DISC]:2` (list of charger IDs per priority). **Legacy**: JSON object `{"87":"3","86":"1","DISC":"-3"}` (charger_id → cost). | `'{}'` |
| `time_window_start` | int | Start hour (0–23) to apply charger preference. | `0` |
| `time_window_end` | int | End hour (0–23) to apply charger preference. | `24` |
| `apply_to_position` | str | Which routes to score: `first`, `all`, or `longest`. | `'first'` |

**kwargs from CostMatrixBuilder**: `vehicle_charger_map` (vehicle_id → charger_id or None), `all_routes`, `all_vehicles`.

---

## 4. Parameters feeding into the optimizer

### 4.1 CostMatrixBuilder (allocation cost matrix)

| Parameter | Source | Description |
|-----------|--------|-------------|
| `vehicles` | Controller | List of `Vehicle` for the site (after MAF enabled_vehicles filter). |
| `routes` | Controller | Routes in allocation window. |
| `constraint_manager` | Controller | `ConstraintManager(constraint_configs)` from `get_all_constraint_configs(site_id, site_config)`. |
| `max_routes_per_vehicle` | MAF `max_routes_per_vehicle_in_window` or `DEFAULT_MAX_ROUTES_PER_VEHICLE` (5) | Max routes per vehicle in window. |
| `vehicle_charger_map` | DB `get_vehicle_chargers_in_window()` | vehicle_id → charger_id (or None) for charger preference and energy feasibility. |

**Context passed to `constraint_manager.evaluate_sequence()`**: `vehicle_charger_map`, `all_routes`, `all_vehicles`. Optional (not set by CostMatrixBuilder): `site_chargers`, `turnaround_minutes`.

### 4.2 UnifiedOptimizationConfig (unified optimizer)

Used by `UnifiedOptimizer` and built in `unified_controller._build_optimization_config()` (and optionally overridden via API).

| Parameter | Type | Description | Default |
|-----------|------|-------------|---------|
| `mode` | OptimizationMode | `ALLOCATION_ONLY`, `SCHEDULING_ONLY`, or `INTEGRATED`. | `INTEGRATED` |
| `allocation_time_limit` | int | Allocation phase time limit (seconds). | 30 |
| `scheduling_time_limit` | int | Scheduling phase time limit (seconds). | 300 |
| `integrated_time_limit` | int | Integrated mode time limit (seconds). | 330 |
| `route_count_weight` | float | Weight for route coverage in objective. | 1e2 |
| `allocation_score_weight` | float | Weight for allocation score term (α). | 1.0 |
| `scheduling_cost_weight` | float | Weight for charging cost term (β). | 1.0 |
| `target_soc_shortfall_penalty` | float | Penalty per kWh shortfall from target SOC. | 0.2 |
| `triad_penalty_factor` | float | Triad penalty factor (API compatibility). | 100.0 |
| `synthetic_time_price_factor` | float | Synthetic time price factor. | 0.01 |
| `target_soc_percent` | float | Target SOC (%) for charging. | 75.0 |
| `site_capacity_kw` | float | Site capacity (kW); from DB `GET_SITE_ASC` (t_site.ASC) when not overridden. | 0.0 |
| `enable_charger_allocation` | bool | Enable charger allocation constraints in scheduling. | True |

Config defaults in code come from `UnifiedOptimizationConfig` in `src/optimizer/unified_optimizer.py`. `config.py` also defines `UNIFIED_*` globals (e.g. `UNIFIED_ALLOCATION_TIME_LIMIT`, `UNIFIED_SCHEDULING_TIME_LIMIT`, `UNIFIED_INTEGRATED_TIME_LIMIT`, `UNIFIED_ALLOCATION_WEIGHT`, etc.) that can be used to align with this config.

### 4.3 Unified controller → optimizer inputs

- **Allocation**: `sequences`, `route_ids`, `sequence_costs` (from CostMatrixBuilder), plus optimizer config.
- **Scheduling**: `schedule_id`, `vehicles`, `vehicle_states`, `energy_requirements`, `availability_matrices`, `time_slots`, `forecast_data`, `price_data`, `site_chargers`.
- **Fleet efficiency**: From DB `GET_FLEET_EFFICIENCY(site_id)`; used for energy requirement calculation and defaults to `0.35` kWh/mile if missing.

Planning window is derived from `allocation_window_hours` (MAF or default 18), then clamped by forecast and price data horizons.

---

## 5. Microlise integration parameters

### 5.1 MicroLiseParams (runtime, not env)

Used when calling `MicroLiseClient.dispatch_allocations()`:

| Attribute | Type | Description | Default |
|-----------|------|-------------|---------|
| `simulate_response` | bool | If True, skip real API calls and return synthetic 201. | True |
| `send_report` | bool | Generate and upload Excel allocation report. | False |
| `trigger_type` | str | e.g. `'initial'`, `'reallocation'`. | `"initial"` |
| `initial_report` | bool | Include initial allocation sheet. | True |
| `compliance_report` | bool | Include vehicle-match compliance sheet. | False |
| `unallocated_report` | bool | Include unallocated routes sheet. | False |
| `start_hour_allocation` | int | Upper bound (exclusive) hour for initial-allocation window (compliance baseline). | 6 |
| `end_hour_allocation` | int | Lower bound (inclusive) hour for that window. | 4 |

### 5.2 MicroLiseClient constructor

All of these fall back to the environment variables listed in section 1 if not passed: `client_id`, `client_secret`, `token_url`, `journeys_api_url`, `storage_conn_string`, `blob_container`, `blob_dir`. `connection_type` is `'test'` or `'prod'` (for alert labelling).

---

## 6. Config.py defaults (summary)

- **DEFAULT_ALLOCATION_WINDOW_HOURS**: 18  
- **DEFAULT_MAX_ROUTES_PER_VEHICLE**: 5  
- **DEFAULT_RESERVE_VEHICLE_COUNT**: 2  
- **DEFAULT_TURNAROUND_TIME_MINUTES**: 45  
- **DEFAULT_PENALTIES**: see `config.py` (e.g. energy_feasibility -20, turnaround_time_strict -22, turnaround_time_preferred -2, shift_hours_strict -20, route_overlap -20, charger_preference 3).  
- **DEFAULT_CONSTRAINT_ENABLED**: see `config.py` (e.g. charger_preference False by default; others True).  
- Scheduler defaults: **DEFAULT_PLANNING_WINDOW_HOURS**, **DEFAULT_ROUTE_ENERGY_SAFETY_FACTOR**, **DEFAULT_MIN_DEPARTURE_BUFFER_MINUTES**, **DEFAULT_BATTERY_FACTOR**, **DEFAULT_POWER_FACTOR**, **DEFAULT_SITE_USAGE_FACTOR**, **DEFAULT_TARGET_SOC_PERCENT**, **DEFAULT_MIN_SOC_PERCENT**, **DEFAULT_FLEET_EFFICIENCY_KWH_MILE**, etc.

---

## 7. File reference

| Component | File(s) |
|-----------|---------|
| Constraint manager and constraints | `src/constraints/constraint_manager.py`, `src/constraints/base.py`, `src/constraints/*.py` |
| MAF parsing | `src/maf/parameter_parser.py` |
| Config and env defaults | `src/config.py` |
| Unified controller and planning window | `src/controllers/unified_controller.py` |
| Cost matrix and allocation inputs | `src/optimizer/cost_matrix.py` |
| Unified optimizer config | `src/optimizer/unified_optimizer.py` |
| Microlise integration | `src/integrations/microlise.py` |
