# Specification: MAF Parameters (Module Asset Framework)

**Module purpose:** MAF is the configuration layer for the vehicle allocation and charge scheduling system. Parameters are stored in the database as string pairs and loaded at runtime via `sp_get_module_params`. Controllers read site-level settings, per-vehicle enablement, and constraint-specific tuning from MAF rather than hard-coded values.

**Application module name:** `vehicle_allocation_system` (env `WEBSITE_SITE_NAME`, default `vehicle_allocation_system`)

**Load path:** `SELECT sp_get_module_params(%s)` → `parse_maf_response()` → `resolve_site_config()` → per-site config consumed by controllers and startup checks.

---

## Hierarchy

MAF configuration is organized in four layers. Only layers that appear in the stored-procedure JSON are parsed today; client-level parameter bags are not yet consumed by this codebase.

| Level | Source in MAF JSON | Parsed by | Use case |
|-------|-------------------|-----------|----------|
| **Module** | Stored procedure argument (`APPLICATION_NAME`) | `CALL_GET_MODULE_PARAMS` | Identifies which module's parameter set to load (allocation vs other FPS modules). |
| **Client** | `clients[]` with `client_id` | `parse_maf_response` (walk only) | Groups sites under a tenant; no client-scoped parameters are read yet. |
| **Site** | `sites[]` → `parameters[]`, `site_id` | `parse_maf_response`, `get_site_parameter` | Window lengths, max routes, scheduler targets, all `constraint_*` keys. |
| **Vehicle** | `sites[]` → `vehicles[]` | `parse_maf_response` | Per-vehicle `enabled` flag filters the active fleet for allocation. |

### MAF response shape (expected)

```json
{
  "clients": [
    {
      "client_id": 1,
      "sites": [
        {
          "site_id": 10,
          "parameters": [
            { "parameter_name": "allocation_window_hours", "parameter_value": "18" },
            { "parameter_name": "planning_window_hours", "parameter_value": "24" },
            { "parameter_name": "target_soc_percent", "parameter_value": "90" },
            { "parameter_name": "constraint_energy_feasibility_enabled", "parameter_value": "true" }
          ],
          "vehicles": [
            { "vehicle_id": 101, "enabled": "true" },
            { "vehicle_id": 102, "enabled": "false" }
          ]
        }
      ]
    }
  ]
}
```

After parsing, each site is stored internally as:

```python
{
  "parameters": { "allocation_window_hours": 18, ... },  # typed values
  "enabled_vehicles": [101, ...]                       # only vehicles with enabled=true
}
```

Site selection uses `resolve_site_config(site_configs, site_id)`, which matches `site_id` as int or str.

---

## Parameter type parsing

All MAF values are strings in the database. `parse_maf_parameter(param_key, param_value)` infers types:

| Rule | Example key / value | Parsed type |
|------|---------------------|-------------|
| `NONE`, `None`, `none`, `NO_VALUE`, empty | any | `None` |
| Key ends with `_enabled` or `_flag`, or value is `true`/`false`/`yes`/`no` | `constraint_*_enabled`, `true` | `bool` |
| Value starts with `[` | JSON array string | `list` |
| Value starts with `{` | JSON object string | `dict` |
| Key ends with `_minutes`, `_hours`, `_seconds`, `_kwh`, `_penalty`, `_weight`, `_bonus`, `_threshold`, `_count`, `_margin` | `minimum_minutes`, `45` | `int` or `float` |
| Key ends with `_period` and value contains `:` | time period | `datetime.time` (`%H:%M:%S`) |
| Otherwise | | `str` |

Note: `planning_window_hours` and `target_soc_percent` are parsed as numeric strings (int/float) via the default string path unless the value contains a decimal point for hours.

---

## Site-level parameters

These live in `site.parameters` with **no** `constraint_` prefix. They apply to the whole site run.

### Allocation

| Parameter | Type | Default (code) | Used by | Use case |
|-----------|------|----------------|---------|----------|
| `allocation_window_hours` | int/float | `18` | `AllocationController`; `UnifiedController` (allocation-only mode) | How far ahead from run time routes are loaded and allocated. |
| `max_routes_per_vehicle_in_window` | int | `4` | `AllocationController`, `UnifiedController`, `CostMatrixBuilder` | Maximum routes chained on one vehicle in a single sequence during cost-matrix generation and Hexaly solve. |

### Charge scheduling

| Parameter | Type | Default (code) | Used by | Use case |
|-----------|------|----------------|---------|----------|
| `planning_window_hours` | float | `24.0` | `SchedulerController`, `UnifiedController` (scheduling / integrated modes) | Charge scheduling horizon from run time. Capped by forecast and price data availability in DB. Minimum effective window: 4 hours. |
| `target_soc_percent` | float | `85.0` | `SchedulerController`, `UnifiedController` (`UnifiedOptimizationConfig`) | Soft target SOC (%) the charge optimizer tries to reach. Penalizes shortfall from this target in the objective. |

Scheduler params are resolved via `get_scheduler_params_from_site_config(site_config)`.

### Window parameter selection (unified optimization)

`UnifiedController` picks the window MAF key based on optimization mode:

| Mode | MAF key for window |
|------|-------------------|
| `allocation_only` | `allocation_window_hours` |
| `scheduling_only` | `planning_window_hours` |
| `integrated` | `planning_window_hours` |

### API / CLI overrides

| Override | Applies to | When set |
|----------|------------|----------|
| `window_hours` (API / `run_unified_optimization`) | Planning/allocation window | Replaces MAF window for that run |
| `target_soc_percent` (API request body) | Target SOC | Replaces MAF `target_soc_percent` for that run |
| `planning_window_hours` (`SchedulerController.run_scheduling`) | Scheduler window | Replaces MAF `planning_window_hours` for that run |

When API `window_hours` is omitted (`null`), MAF defaults apply.

---

## Vehicle-level parameters

| Field | Type | Default | Used by | Use case |
|-------|------|---------|---------|----------|
| `enabled` | bool | `true` if omitted | `parse_maf_response`, `_load_vehicles` | When MAF lists vehicles for a site, only those with `enabled=true` are included in `enabled_vehicles`. If the list is **empty**, all active DB vehicles are used (no MAF filter). |

**Use cases:**

- Pilot rollout: enable allocation for a subset of vehicles at a site.
- Exclude problematic assets from optimizer input without deactivating them in `t_vehicle`.
- Staged fleet expansion: add vehicles in MAF before they participate in allocation.

---

## Constraint parameters

Constraint keys follow the naming convention:

- Enable flag: `constraint_{name}_enabled`
- Sub-parameters: `constraint_{name}_{param}`
- Penalty: `constraint_{name}_penalty` (optional; falls back to `DEFAULT_PENALTIES`)

`get_constraint_config()` strips the prefix and passes `params` to the constraint class. `get_all_constraint_configs()` loads all known constraint names; `ConstraintManager` only instantiates constraints that have a Python implementation.

### Summary table

| Constraint `name` | Hard / soft | Default enabled | Default penalty | Implemented |
|-------------------|-------------|-----------------|-----------------|-------------|
| `energy_feasibility` | Hard | `true` | `-20` | Yes |
| `turnaround_time_strict` | Hard | `true` | `-22` | Yes |
| `turnaround_time_preferred` | Soft | `true` | `-2` | Yes |
| `shift_hours_strict` | Hard | `true` | `-20` | Yes |
| `minimum_soonness` | — | `true` | `-20` | **No** (parsed only) |
| `charger_preference` | Soft | **`false`** | `3` (bonus) | Yes |
| `swap_minimization` | — | `true` | `0.5` | **No** (parsed only) |
| `energy_optimization` | — | `true` | `0.5` | **No** (parsed only) |

Hard constraint violation sets `is_feasible=False` and stops evaluation for that vehicle sequence. Soft constraints add to the objective cost only.

---

### `energy_feasibility`

**Purpose:** Ensure the vehicle has enough energy (including opportunistic charging between routes and before the first route) to complete each route in the sequence, with a safety margin.

| MAF key | Param key (in code) | Type | Default | Use case |
|---------|---------------------|------|---------|----------|
| `constraint_energy_feasibility_enabled` | `enabled` | bool | `true` | Turn off energy checks for debugging or when energy data is unreliable. |
| `constraint_energy_feasibility_safety_margin_kwh` | `safety_margin_kwh` | float | `5.0` | Extra kWh buffer above route requirement. |
| `constraint_energy_feasibility_allow_dc_charging` | `allow_dc_charging` | bool | `true` | Use DC charge rate when estimating pre-route / between-route charging. |
| `constraint_energy_feasibility_penalty` | `penalty` | float | `-20` | Hard reject penalty when energy insufficient. |

---

### `turnaround_time_strict`

**Purpose:** Enforce minimum idle/charge time between consecutive routes on the same vehicle.

| MAF key | Param key | Type | Default | Use case |
|---------|-----------|------|---------|----------|
| `constraint_turnaround_time_strict_enabled` | `enabled` | bool | `true` | Disable strict turnaround for stress testing. |
| `constraint_turnaround_time_strict_minimum_minutes` | `minimum_minutes` | int | `45` | Minimum gap between route end and next route start. |
| `constraint_turnaround_time_strict_penalty` | `penalty` | float | `-22` | Hard violation penalty. |

Also used by `CostMatrixBuilder` for overlap pruning when the constraint is enabled.

---

### `turnaround_time_preferred`

**Purpose:** Soft preference for comfortable turnaround times without rejecting assignments.

| MAF key | Param key | Type | Default | Use case |
|---------|-----------|------|---------|----------|
| `constraint_turnaround_time_preferred_enabled` | `enabled` | bool | `true` | Disable soft turnaround scoring. |
| `constraint_turnaround_time_preferred_standard_minutes` | `standard_minutes` | int | `75` | Below this: apply `penalty_standard` per pair. |
| `constraint_turnaround_time_preferred_optimal_minutes` | `optimal_minutes` | int | `90` | Between standard and optimal: apply `penalty_optimal` per pair. |
| `constraint_turnaround_time_preferred_penalty_standard` | `penalty_standard` | float | `-2` | Soft penalty per short turnaround. |
| `constraint_turnaround_time_preferred_penalty_optimal` | `penalty_optimal` | float | `-1` | Smaller soft penalty for acceptable-but-not-ideal gaps. |
| `constraint_turnaround_time_preferred_penalty` | `penalty` | float | `-2` | Fallback penalty field. |

---

### `shift_hours_strict`

**Purpose:** Cap total working time for driver compliance.

| MAF key | Param key | Type | Default | Use case |
|---------|-----------|------|---------|----------|
| `constraint_shift_hours_strict_enabled` | `enabled` | bool | `true` | Disable shift cap. |
| `constraint_shift_hours_strict_max_hours` | `max_hours` | float | `16` | Maximum allowed shift duration. |
| `constraint_shift_hours_strict_calculation_method` | `calculation_method` | str | `first_to_last` | `first_to_last` or `cumulative`. |
| `constraint_shift_hours_strict_pre_shift_buffer_hours` | `pre_shift_buffer_hours` | float | `0.5` | Prep time before shift. |
| `constraint_shift_hours_strict_post_shift_buffer_hours` | `post_shift_buffer_hours` | float | `0.5` | Time after shift. |
| `constraint_shift_hours_strict_penalty` | `penalty` | float | `-20` | Hard violation penalty. |

---

### `charger_preference`

**Purpose:** Prefer vehicles on high-priority chargers for early-departing routes. **Default disabled.**

| MAF key | Param key | Type | Default | Use case |
|---------|-----------|------|---------|----------|
| `constraint_charger_preference_enabled` | `enabled` | bool | **`false`** | Enable charger-based prioritization. |
| `constraint_charger_preference_map` | `map` | str → map | `{}` | Charger ID → cost/bonus. |
| `constraint_charger_preference_time_window_start` | `time_window_start` | int (0–23) | `0` | Start hour for preference window. |
| `constraint_charger_preference_time_window_end` | `time_window_end` | int (0–23) | `24` | End hour (exclusive); supports midnight wrap. |
| `constraint_charger_preference_apply_to_position` | `apply_to_position` | str | `first` | `first`, `all`, or `longest`. |
| `constraint_charger_preference_penalty` | `penalty` | float | `3` | Base field; evaluate uses map values. |

**`map` formats:**

1. **List format:** `[87,86]:3,[85,83]:0,[DISC]:2`
2. **Legacy JSON:** `{"87":"3","86":"1","DISC":"-3"}`

`DISC` = disconnected / no charger recorded in `t_vehicle_charge`.

---

### `minimum_soonness` (not implemented)

| MAF key | Default enabled | Default penalty | Status |
|---------|-----------------|-----------------|--------|
| `constraint_minimum_soonness_enabled` | `true` | `-20` | Parsed and logged; no constraint class. |

---

### `swap_minimization` (not implemented)

| MAF key | Default enabled | Default penalty | Status |
|---------|-----------------|-----------------|--------|
| `constraint_swap_minimization_enabled` | `true` | `0.5` | Parsed only. |

---

### `energy_optimization` (not implemented)

| MAF key | Default enabled | Default penalty | Status |
|---------|-----------------|-----------------|--------|
| `constraint_energy_optimization_enabled` | `true` | `0.5` | Parsed only. |

---

## Example site configuration

```python
{
  "parameters": {
    "allocation_window_hours": 18,
    "planning_window_hours": 24,
    "target_soc_percent": 90,
    "max_routes_per_vehicle_in_window": 5,
    "constraint_turnaround_time_strict_enabled": "true",
    "constraint_turnaround_time_strict_minimum_minutes": "30",
    "constraint_energy_feasibility_enabled": "true",
    "constraint_energy_feasibility_safety_margin_kwh": "3.0"
  },
  "enabled_vehicles": []   # empty = all active DB vehicles
}
```

Equivalent MAF DB rows:

| parameter_name | parameter_value |
|----------------|-----------------|
| `allocation_window_hours` | `18` |
| `planning_window_hours` | `24` |
| `target_soc_percent` | `90` |
| `max_routes_per_vehicle_in_window` | `5` |
| `constraint_turnaround_time_strict_minimum_minutes` | `30` |
| `constraint_energy_feasibility_safety_margin_kwh` | `3.0` |

---

## Operational flows

### Allocation (`AllocationController`)

1. `sp_get_module_params(APPLICATION_NAME)`
2. `parse_maf_response` → `resolve_site_config(site_id)`
3. `get_all_constraint_configs` → `ConstraintManager`
4. `allocation_window_hours` → window start/end
5. `max_routes_per_vehicle_in_window` → `CostMatrixBuilder`
6. `enabled_vehicles` → vehicle filter in `_load_vehicles`

### Charge scheduling (`SchedulerController`)

1. Create or load `SchedulerConfig` from `t_scheduler`
2. `sp_get_module_params` → `resolve_site_config`
3. `get_scheduler_params_from_site_config` → `planning_window_hours`, `target_soc_percent` on config
4. `_calculate_planning_window` uses `config.planning_window_hours`
5. `ChargeOptimizer` receives `target_soc_percent` from config

### Unified optimization (`UnifiedController`)

1. Load MAF before monitor/window initialization
2. Window: `allocation_window_hours` (allocation-only) or `planning_window_hours` (scheduling / integrated)
3. Allocation leg: constraints + `max_routes_per_vehicle_in_window`
4. Scheduling leg: `target_soc_percent` on `UnifiedOptimizationConfig`
5. Site capacity (`site_capacity_kw`) still loaded from `t_site.ASC`, not MAF

### Integrated workflow (`IntegratedWorkflowController`)

1. Allocation via `AllocationController` (MAF allocation params)
2. Scheduling via `SchedulerController` (MAF scheduler params)
3. Optional CLI `planning_window_hours` override passed to `run_scheduling`

### Startup validation (`startup_checks`)

On app start (when DB env vars are set), logs per-site:

- `allocation_window_hours`, `max_routes_per_vehicle_in_window`, `planning_window_hours`, `target_soc_percent`
- Enabled / disabled constraint names from `constraint_*_enabled`

---

## Code defaults reference (`src/config.py`)

When MAF omits a key, these code defaults apply:

| Constant | Value | Maps to MAF key |
|----------|-------|----------------|
| `DEFAULT_ALLOCATION_WINDOW_HOURS` | `18` | `allocation_window_hours` |
| `DEFAULT_MAX_ROUTES_PER_VEHICLE` | `4` | `max_routes_per_vehicle_in_window` |
| `DEFAULT_PLANNING_WINDOW_HOURS` | `24.0` | `planning_window_hours` |
| `DEFAULT_TARGET_SOC_PERCENT` | `85.0` | `target_soc_percent` |
| `DEFAULT_TURNAROUND_TIME_MINUTES` | `45` | strict turnaround fallback / overlap pruning |
| `DEFAULT_PENALTIES` | per constraint | `constraint_*_penalty` |
| `DEFAULT_CONSTRAINT_ENABLED` | per constraint | `constraint_*_enabled` |

### Not MAF-backed (code / DB only)

| Setting | Source | Notes |
|---------|--------|-------|
| `agreed_site_capacity_kva` / `site_capacity_kw` | `t_site.ASC` | Site electrical capacity for scheduling |
| `triad_penalty_factor`, `synthetic_time_price_factor` | `SchedulerConfig` / API | Optimizer tuning |
| `route_energy_safety_factor`, `min_departure_buffer_minutes` | `SchedulerConfig` defaults | Energy / availability modelling |
| `DEFAULT_RESERVE_VEHICLE_COUNT` | `config.py` | Not wired to MAF or used in controllers |

---

## Gaps and recommendations

1. **Client-level parameters** — JSON includes `client_id` but no client-scoped params are read; all tuning is per site.
2. **Unimplemented constraints** — `minimum_soonness`, `swap_minimization`, `energy_optimization` appear in MAF schema and defaults but do not affect the solver.
3. **`reserve_vehicle_count`** — No MAF key exists yet.
4. **Additional scheduler params** — `min_soc_percent`, `triad_penalty_factor`, etc. remain code/API defaults; extend MAF if ops need DB-driven tuning.

---

## Related files

| File | Role |
|------|------|
| `src/maf/parameter_parser.py` | Parse SP response, type coercion, `resolve_site_config`, `get_scheduler_params_from_site_config` |
| `src/config.py` | Default penalties, enabled flags, window and SOC defaults |
| `src/constraints/*.py` | Constraint behavior and param consumption |
| `src/controllers/allocation_controller.py` | MAF for allocation |
| `src/controllers/scheduler_controller.py` | MAF for charge scheduling |
| `src/controllers/unified_controller.py` | MAF for unified runs |
| `src/controllers/integrated_workflow.py` | Sequential allocation + scheduling |
| `src/utils/startup_checks.py` | Startup MAF logging |
| `src/database/queries.py` | `CALL_GET_MODULE_PARAMS` |
| `src/api/unified_api.py` | HTTP overrides for `window_hours`, `target_soc_percent` |
