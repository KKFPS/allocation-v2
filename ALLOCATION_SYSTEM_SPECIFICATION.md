# Vehicle-Route Allocation System - Technical Specification

**Document Version:** 2.0  
**Last Updated:** February 11, 2026  
**System Owner:** Flexible Power Systems  
**Target Audience:** Technical Stakeholders, Database Administrators, Integration Engineers

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [System Boundaries](#system-boundaries)
3. [Business Purpose & Workflow](#business-purpose--workflow)
4. [Database Schema](#database-schema)
5. [Business Logic & Constraints](#business-logic--constraints)
6. [Integration Points](#integration-points)
7. [Operational Scenarios](#operational-scenarios)
8. [Key Business Metrics](#key-business-metrics)
9. [Assumptions & Dependencies](#assumptions--dependencies)
10. [Future Enhancements](#future-enhancements)

---

## Executive Summary

The Vehicle-Route Allocation System is an automated optimization platform for electric vehicle (EV) fleet management using an **18-hour rolling window allocation model**. It assigns delivery vehicles to planned routes while respecting battery constraints, driver regulations, charging infrastructure capacity, and operational time windows. The system features **modular, configurable constraints** managed through Module Asset Framework (MAF), enabling site-specific business rule customization without code changes.

**Key Capabilities:**
- 18-hour rolling window optimization with non-overlapping route sequencing
- Modular constraint framework (enable/disable/override per site via MAF)
- Real-time vehicle state monitoring and dynamic re-allocation
- Energy feasibility validation with charging potential calculation
- Configurable regulatory compliance for driver working hours
- Integration with external TMS (Microlise API)
- Automated reporting and alerting

**Major Changes in v2.0:**
- Replaced shift-based allocation with continuous 18-hour rolling window
- Implemented MAF-based modular constraint configuration
- Added temporal overlap prevention for route sequences
- Enabled per-site constraint customization via string-based MAF parameters

---

## System Boundaries

### In Scope

**Core Allocation Functions:**
- Automated vehicle-to-route assignment using 18-hour lookahead window
- Multi-route sequencing per vehicle with temporal overlap prevention
- Energy constraint validation based on battery capacity and charging availability
- Configurable time-based constraints (turnaround time, shift hours, soonness rules)
- Dynamic re-allocation triggered by operational changes
- Optimization using cost matrices with weighted modular constraints

**Data Management:**
- Route plan ingestion from `t_route_plan` (populated by external route-fetch service)
- Vehicle state synchronization via `t_vsm` (Vehicle State Management)
- Allocation persistence in `t_route_allocated` (live) and `t_route_allocated_history` (archive)
- Configuration management via Module Asset Framework (MAF) parameters in `{String: String}` format

**Integrations:**
- Microlise API for vehicle allocation push
- Azure Blob Storage for report delivery
- PostgreSQL database for operational data persistence

**Reporting:**
- Daily morning allocation report
- Previous-day compliance analysis
- Unallocated route error reporting

### Out of Scope

**Excluded Functions:**
- Route planning and optimization (handled by Microlise)
- Vehicle telematics data collection (handled by Microlise/VSM feed)
- Charging station control or scheduling
- Driver rostering and assignment
- Manual allocation UI (system is automation-focused)
- Route execution tracking beyond allocation
- Financial cost modeling or billing

**Not Managed By This System:**
- Creation of delivery routes or stop sequences
- Customer order management
- Fleet maintenance scheduling
- Driver break planning
- Traffic or weather impact analysis
- Vehicle procurement decisions

### System Interfaces

**Inbound Data:**
- Route plans from `t_route_plan` (updated by external Microlise route-fetch service)
- Vehicle state from `t_vsm` (updated by external VSM feed)
- Configuration parameters from MAF stored procedure
- Manual trigger events via `t_allocation_monitor`

**Outbound Data:**
- Vehicle allocations to Microlise API
- Allocation results to `t_route_allocated`
- Reports to Azure Blob Storage
- Alerts to `t_alert` table
- Error logs to `t_error_log`

---

## Business Purpose & Workflow

### System Purpose

The allocation system solves the continuous optimization problem of assigning a limited fleet of electric delivery vehicles to a rolling 18-hour window of delivery routes. The challenge is unique to EV operations due to:

- **Energy Constraints:** Unlike diesel vehicles, EVs have limited range requiring careful energy budget management
- **Charging Time:** Multi-hour charging periods create dependencies between sequential route assignments
- **Battery Degradation:** Optimal charging practices require avoiding complete discharge
- **Regulatory Compliance:** Driver shift hour limits require careful route sequence planning
- **Dynamic Changes:** Route cancellations, traffic delays, and early returns require real-time re-optimization
- **Temporal Sequencing:** Vehicles must complete routes without overlapping time windows

### 18-Hour Rolling Window Model

**Key Concept:** Instead of dividing routes into discrete shifts, the system allocates all routes within an 18-hour lookahead window in a single optimization run.

**Time Window Calculation:**
```
allocation_start_time = current_datetime
allocation_end_time = current_datetime + 18 hours

eligible_routes = routes WHERE plan_start_date_time BETWEEN 
                  allocation_start_time AND allocation_end_time
```

**Benefits:**
- **Continuous optimization** across operational period without artificial shift boundaries
- **Better vehicle utilization** through intelligent multi-route assignments
- **Temporal constraint awareness** preventing overlapping route assignments
- **Flexible working patterns** not constrained by fixed shift definitions
- **Dynamic adaptability** to changing operational conditions

**Non-Overlapping Route Constraint:**

For each vehicle, the system ensures assigned routes do not overlap in time:

```
Route A and Route B can be assigned to same vehicle IF:
  (Route_A_end + turnaround_time) ≤ Route_B_start
  OR
  (Route_B_end + turnaround_time) ≤ Route_A_start
```

**Route Sequencing:**
- System automatically sequences routes per vehicle by start time
- Vehicle availability cascades: Vehicle free after route N completes + turnaround
- Energy availability cascades: Battery after route N - energy_used_N + charging_time

### Main Workflow

```
┌─────────────────┐
│ Trigger         │ ← Route changes, time-based, manual override
│ Detection       │
└────────┬────────┘
         │
         ↓
┌─────────────────┐
│ Controller      │ ← Initialize parameters, validate prerequisites
│                 │   Load vehicles, routes, site configuration from MAF
└────────┬────────┘
         │
         ↓
┌─────────────────┐
│ Window Manager  │ ← Define 18-hour window, filter eligible routes
│                 │   Build temporal route graph
└────────┬────────┘
         │
         ↓
┌─────────────────┐
│ Allocation      │ ← Optimize entire window with modular constraints
│ Optimizer       │   Build cost matrix, solve assignment problem
└────────┬────────┘
         │
         ↓
┌─────────────────┐
│ Cleanup         │ ← Validate results, check temporal conflicts
│                 │   Verify allocation quality score
└────────┬────────┘
         │
         ↓
┌─────────────────┐
│ Microlise       │ ← Push allocations to external TMS
│ Integration     │   Handle HTTP responses, retry logic
└────────┬────────┘
         │
         ↓
┌─────────────────┐
│ Reporting       │ ← Generate Excel reports, upload to blob storage
│                 │   Update allocation history
└─────────────────┘
```

### Trigger Types

| Trigger Type | Description | Typical Timing | Re-allocation Scope |
|-------------|-------------|----------------|---------------------|
| `initial` | New routes detected in `t_route_plan` | 04:00-06:00 AM | All routes in 18-hour window |
| `cancellation` | Route status changed to 'X' (cancelled) | Real-time | Remaining routes in window |
| `arrival` | Vehicle returned significantly early/late | Real-time | Subsequent routes |
| `estimated_arrival` | VSM updated vehicle ETA | Real-time | Subsequent routes |
| `different_allocation` | Manual override or correction needed | Ad-hoc | Configurable scope |

### Process Flow Details

**Phase 1: Initialization (Controller)**
1. Create allocation record in `t_allocation_monitor` with status 'N' (New)
2. Fetch MAF parameters for site via `sp_get_module_params(application_name)`
3. Parse modular constraint configurations from MAF string parameters
4. Load vehicle fleet specifications from `t_vehicle`
5. Read vehicle current state from `t_vsm`
6. Load route plans for 18-hour window from `t_route_plan`
7. Validate prerequisites:
   - Sufficient vehicles available (not VOR, MAF-enabled)
   - Routes are energy-feasible given fleet capabilities
   - Minimum stop count requirements met
8. Update allocation status to 'P' (In Progress)

**Phase 2: Window Definition & Route Filtering**
```
1. Calculate 18-hour window:
   window_start = now()
   window_end = now() + 18 hours

2. Filter eligible routes:
   WHERE plan_start_date_time >= window_start
   AND plan_start_date_time <= window_end
   AND route_status = 'N'
   AND to_allocate = true

3. Sort routes by plan_start_date_time ASC

4. Create temporal route graph:
   - Nodes = Routes
   - Edges = "Can be sequenced" (Route_i_end + turnaround < Route_j_start)
```

**Phase 3: Vehicle Availability Calculation**
```
For each vehicle:
  1. Get current state from t_vsm:
     - If on-route: available_time = return_eta
     - If at depot: available_time = now
     - If charging: available_time = now (can interrupt)
  
  2. Get current energy:
     - available_energy = max(estimated_soc, return_soc) * battery / 100
  
  3. Get already-allocated routes in 18h window:
     - committed_routes = routes WHERE vehicle_id_allocated = this_vehicle
                                  AND plan_start_date_time IN window
     - Update available_time and available_energy after each committed route
```

**Phase 4: Route Compatibility & Sequencing**

Build M×N×K compatibility tensor where:
- M = number of routes in window
- N = number of vehicles
- K = maximum routes per vehicle in window (typically 4-6)

```
For each (vehicle, route_sequence) combination:
  route_sequence = [route_1, route_2, ..., route_k] (k ≤ K)

  Validate:
  1. Temporal ordering: route_1.start < route_2.start < ... < route_k.start
  2. No temporal overlaps: route_i.end + turnaround ≤ route_{i+1}.start
  3. Cascading energy: energy after route_i sufficient for route_{i+1}
  4. Total working time: route_k.end - route_1.start ≤ configured limits
  5. Each route non-overlapping with all committed routes

  Cost = Σ(modular_constraint_penalties) for entire sequence
```

**Phase 5: Cost Matrix Construction with Modular Constraints**

For each (vehicle, route_sequence):
1. Load constraint configurations from MAF
2. Apply enabled constraints with site-specific parameters:
   - Energy feasibility (if enabled)
   - Turnaround time strict/preferred (if enabled)
   - Shift hours strict/balance (if enabled)
   - Minimum soonness (if enabled)
   - Route overlap prevention (always enabled)
   - Charger preference (if enabled)
   - Swap minimization (if enabled)
   - Energy optimization (if enabled)
3. Sum penalties/bonuses to get total cost
4. Populate cost matrix

**Phase 6: Optimization**

```
Objective: Maximize Σ(route_vehicle_sequence_score)

Subject to:
  1. Each route assigned to exactly one vehicle
  2. Routes assigned to same vehicle must be non-overlapping
  3. Vehicle capacity limits (max K routes per vehicle in window)

Solver: Hexaly (if licensed)
```

**Phase 7: Persistence & Integration**
1. Write allocations to `t_route_allocated` (replaces old data for site)
2. Append to `t_route_allocated_history` for audit trail
3. Call Microlise API for each route-vehicle pair
4. Record HTTP responses in `http_response` field
5. Generate alerts for API failures

**Phase 8: Reporting**
1. Generate morning allocation report (route → vehicle mapping)
2. Generate compliance report (previous day: allocated vs actual)
3. Generate unallocated routes report (routes with fetch errors)
4. Upload Excel file to Azure Blob Storage
5. Update allocation status to 'A' (Allocated) or 'F' (Failed)

---

## Database Schema

### Entity Relationship Overview

```
t_client
   │
   └──→ t_site ←──────┐
           │          │
           ├──→ t_vehicle ──→ t_vehicle_telematics
           │      │
           │      └──→ t_vehicle_charge ──→ t_charger
           │      │
           │      └──→ t_vsm
           │
           └──→ t_route_plan ←────┐
                   │               │
                   └──→ t_route_allocated ──→ t_allocation_monitor
                           │
                           └──→ t_route_allocated_history
```

### Core Tables

#### **t_allocation_monitor**

**Purpose:** Tracks each allocation run as a workflow instance. Creates audit trail of all allocation attempts including trigger reason, status progression, and quality score.

| Column | Type | Description |
|--------|------|-------------|
| `allocation_id` | INTEGER (PK) | Unique identifier for allocation run, auto-increment |
| `site_id` | INTEGER (FK) | Site where allocation occurs |
| `status` | VARCHAR(1) | 'N'=New, 'P'=In Progress, 'A'=Allocated, 'F'=Failed, 'S'=Success |
| `trigger_type` | VARCHAR(50) | 'initial', 'cancellation', 'arrival', 'estimated_arrival', 'different_allocation' |
| `run_datetime` | TIMESTAMP | When allocation process started (UTC) |
| `score` | NUMERIC | Overall allocation quality (-∞ to +∞, <-4 = rejected) |
| `allocation_window_start` | TIMESTAMP | Start of 18-hour allocation window |
| `allocation_window_end` | TIMESTAMP | End of 18-hour allocation window |
| `routes_in_window` | INTEGER | Total routes within time window |
| `routes_allocated` | INTEGER | Routes successfully assigned vehicles |
| `routes_overlapping_count` | INTEGER | Number of routes rejected due to temporal conflicts |

**Business Rules:**
- One allocation_id per allocation attempt
- Status workflow: N → P → A/F, then optionally → S
- Score calculated as sum of all route-vehicle assignment costs
- Negative scores indicate constraint violations
- Window fields track the temporal scope of allocation

---

#### **t_route_plan**

**Purpose:** Contains planned delivery routes imported from Microlise TMS. This is the source of truth for what routes need to be executed. Updated by external route-fetch service (not part of allocation system).

| Column | Type | Description |
|--------|------|-------------|
| `route_id` | VARCHAR(50) (PK) | Unique route identifier (Microlise GUID) |
| `site_id` | INTEGER (FK) | Operating site for this route |
| `vehicle_id` | VARCHAR(20) | Microlise vehicle ID (if manually pre-assigned) |
| `route_status` | VARCHAR(2) | 'N'=New, 'A'=Active, 'C'=Complete, 'X'=Cancelled, 'E'=Error, 'AO'=Allocation Override, 'U'=Unfeasible |
| `route_alias` | VARCHAR(50) | Human-readable route name (e.g., "706-1", "706-2") |
| `plan_start_date_time` | TIMESTAMP | Planned departure time |
| `actual_start_date_time` | TIMESTAMP | Actual departure time (populated during execution) |
| `plan_end_date_time` | TIMESTAMP | Planned return time |
| `actual_end_date_time` | TIMESTAMP | Actual return time |
| `plan_mileage` | NUMERIC | Total route distance in miles |
| `n_orders` | INTEGER | Number of delivery stops on route |

**Business Rules:**
- Routes with status 'N' within 18-hour window are candidates for allocation
- Routes with < `min_stops` orders marked 'U' (unfeasible)
- `vehicle_id` may be null, 0, -1, or 'X' indicating no pre-assignment
- Routes fetched from Microlise daily, typically appear 04:00-05:00 AM

---

#### **t_route_allocated**

**Purpose:** Live allocation table showing current allocation results within 18-hour windows. This is the operational table used by downstream systems.

| Column | Type | Description |
|--------|------|-------------|
| `allocation_id` | INTEGER (FK) | Reference to allocation run that created this assignment |
| `route_id` | VARCHAR(50) (PK, FK) | Route being allocated |
| `site_id` | INTEGER (FK) | Operating site |
| `vehicle_id_allocated` | INTEGER | System-assigned vehicle (FPS internal ID) |
| `vehicle_id_actual` | VARCHAR(20) | Actually used vehicle (Microlise ID, from execution) |
| `status` | VARCHAR(2) | Current route status (mirrors `t_route_plan.route_status`) |
| `estimated_arrival` | TIMESTAMP | Expected return time (calculated during allocation) |
| `estimated_arrival_soc` | NUMERIC | Expected battery % at return |
| `http_response` | INTEGER | Microlise API response (-1=not sent, 0=pending, 200/201=success, 4xx/5xx=error) |
| `vehicle_id` | INTEGER | Redundant field (legacy, mirrors `vehicle_id_allocated`) |

**Business Rules:**
- Table updated on each allocation run for routes in window
- `vehicle_id_allocated` = 0 or -1 indicates no vehicle assigned (unfeasible route)
- `vehicle_id_actual` populated after route execution starts
- `http_response` tracks Microlise API integration success

---

#### **t_route_allocated_history**

**Purpose:** Immutable historical archive of all allocations. Used for compliance reporting, analysis of allocation changes over time, and audit trail.

| Column | Type | Description |
|--------|------|-------------|
| *(same as t_route_allocated)* | | Plus timestamp of insertion |

**Business Rules:**
- Append-only table (no updates or deletes)
- Captures every allocation attempt including failed ones
- Used to compare allocated vs actual vehicle assignments

---

#### **t_vehicle**

**Purpose:** Fleet vehicle master data including specifications, operational status, and charging capabilities.

| Column | Type | Description |
|--------|------|-------------|
| `vehicle_id` | INTEGER (PK) | Internal FPS vehicle identifier |
| `site_id` | INTEGER (FK) | Home site for this vehicle |
| `active` | BOOLEAN | Is vehicle operational (true) or decommissioned (false) |
| `VOR` | BOOLEAN | Vehicle Off Road flag (maintenance, damage) |
| `charge_power_ac` | NUMERIC | Maximum AC charging rate (kW) |
| `charge_power_dc` | NUMERIC | Maximum DC charging rate (kW) |
| `battery_capacity` | NUMERIC | Total battery size (kWh) |
| `efficiency_kwh_mile` | NUMERIC | Energy consumption rate (kWh per mile) |

**Business Rules:**
- Only vehicles with `active=true AND VOR=false` eligible for allocation
- Additionally filtered by MAF `enabled` parameter
- Charging power limited by minimum of vehicle capability and charger capability

---

#### **t_vsm** (Vehicle State Management)

**Purpose:** Real-time vehicle telemetry data providing current location, battery level, and operational status. Updated by external VSM feed every 5-15 minutes.

| Column | Type | Description |
|--------|------|-------------|
| `vehicle_id` | INTEGER (FK) | Vehicle being tracked |
| `date_time` | TIMESTAMP | Timestamp of this state reading (UTC) |
| `status` | VARCHAR(50) | 'On-Route', 'No meter', 'Charged', 'Charging', 'Unknown', etc. |
| `route_id` | VARCHAR(50) | Current route if status='On-Route' |
| `estimated_soc` | NUMERIC | Current state of charge (0-100%) |
| `return_eta` | TIMESTAMP | Expected return time (if on-route) |
| `return_soc` | NUMERIC | Expected SOC at return (%) |

**Business Rules:**
- Latest record per vehicle determines current state
- Used to calculate vehicle availability in 18-hour window
- `return_eta` determines when vehicle available for next route in sequence

---

#### **t_vehicle_charge**

**Purpose:** Tracks which vehicle is connected to which charger. Used for preferred charger prioritization logic.

| Column | Type | Description |
|--------|------|-------------|
| `vehicle_id` | INTEGER (FK) | Vehicle connected to charger |
| `charger_id` | INTEGER (FK) | Charger being used |
| `start_date_time` | TIMESTAMP | When vehicle connected to this charger |

**Business Rules:**
- Most recent record per vehicle indicates current/last-known charger
- Null `charger_id` treated as "DISC" (disconnected) in charger preference logic
- Used when charger preference constraint enabled via MAF

---

#### **t_vehicle_telematics**

**Purpose:** Maps internal FPS vehicle IDs to external system identifiers (Microlise telematic labels).

| Column | Type | Description |
|--------|------|-------------|
| `vehicle_id` | INTEGER (FK) | Internal FPS vehicle ID |
| `telematic_id` | INTEGER | System identifier (2 = Microlise) |
| `telematic_label` | VARCHAR(50) | External system's vehicle ID (e.g., "706.25003T25") |

**Business Rules:**
- `telematic_id = 2` indicates Microlise system
- Used to translate `vehicle_id_allocated` → `telematic_label` for API calls

---

#### **t_charger**

**Purpose:** Defines available charging infrastructure at each site.

| Column | Type | Description |
|--------|------|-------------|
| `charger_id` | INTEGER (PK) | Unique charger identifier |
| `site_id` | INTEGER (FK) | Site where charger located |
| `max_power` | NUMERIC | Maximum charging rate (kW) |
| `dc_flag` | BOOLEAN | true=DC fast charger, false/null=AC charger |

**Business Rules:**
- Site-level AC charger power = `max(max_power WHERE dc_flag=false/null)`
- Site-level DC charger power = `max(max_power WHERE dc_flag=true)`
- Used in energy feasibility calculations for route sequences

---

#### **t_alert**

**Purpose:** System-generated alerts for operational issues requiring human intervention.

| Column | Type | Description |
|--------|------|-------------|
| `alert_id` | INTEGER (PK) | Unique alert identifier |
| `site_id` | INTEGER (FK) | Site where alert originated |
| `alert_message_id` | INTEGER | Alert type (9=Microlise API failure, 14=Negative allocation score) |
| `dev_app_id` | TEXT | Additional context (error details, allocation_id, etc.) |
| `alert_date_time` | TIMESTAMP | When alert was generated |

---

#### **t_error_log**

**Purpose:** Application error tracking for debugging and monitoring.

| Column | Type | Description |
|--------|------|-------------|
| `error_datetime` | TIMESTAMP | When error occurred |
| `module_no` | VARCHAR(100) | Module identifier |
| `error_message` | TEXT | Error details and stack trace |

---

### Stored Procedures

#### **sp_get_module_params**

**Purpose:** Centralized configuration retrieval via Module Asset Framework (MAF). Returns hierarchical JSON with client → site → vehicle parameters. **All parameters are in `{String: String}` format.**

**Signature:**
```sql
sp_get_module_params(application_name VARCHAR) RETURNS JSON
```

**Returns:** Nested JSON structure with string-only parameter values:
```json
{
  "clients": [
    {
      "client_id": 1,
      "sites": [
        {
          "site_id": 10,
          "parameters": {
            "allocation_window_hours": "18",
            "max_routes_per_vehicle_in_window": "5",
            "reserve_vehicle_count": "2",
            "constraint_energy_feasibility_enabled": "true",
            "constraint_energy_feasibility_safety_margin_kwh": "5.0",
            "constraint_turnaround_time_strict_enabled": "true",
            "constraint_turnaround_time_strict_minimum_minutes": "45",
            "constraint_charger_preference_map": "{\"87\":\"3\",\"86\":\"3\",\"DISC\":\"-3\"}"
          },
          "vehicles": [
            {
              "vehicle_id": 101,
              "enabled": "true"
            }
          ]
        }
      ]
    }
  ]
}
```

**Business Rules:**
- Called once per allocation run during controller initialization
- All parameter values are strings; application parses to appropriate types
- Missing parameters fall back to system defaults
- Constraint parameters follow `constraint_{name}_{parameter}` naming convention

---

## Business Logic & Constraints

### Modular Constraint Framework

All constraints are configured through MAF parameters with consistent naming:

**Pattern:** `constraint_{constraint_name}_{parameter_name}`

**Standard Parameters:**
- `constraint_{name}_enabled` (BOOLEAN string): "true" or "false"
- `constraint_{name}_penalty` (NUMERIC string): Penalty value (negative = penalty, positive = bonus)
- Additional constraint-specific parameters

### MAF Parameter Naming Convention

All MAF parameters are stored and retrieved as `{String: String}` key-value pairs. The application is responsible for parsing string values to appropriate types.

**Type Inference Rules:**

1. **Boolean Detection:**
   - Parameter name ends with `_enabled` or `_flag`
   - OR value is "true"/"false"/"yes"/"no"/"1"/"0" (case insensitive)

2. **Numeric Detection:**
   - Parameter name ends with `_minutes`, `_hours`, `_seconds`, `_kwh`, `_penalty`, `_weight`, `_bonus`, `_threshold`, `_count`, `_margin`
   - Parse as integer if no decimal point, float otherwise

3. **JSON Array/Object Detection:**
   - Value starts with `[` → parse as JSON array
   - Value starts with `{` → parse as JSON object

4. **Time Format Detection:**
   - Parameter name ends with `_period` AND value contains `:` → parse as HH:MM:SS

5. **Default:** Return as string

---

### Core MAF Parameters for 18-Hour Window Model

| Parameter Key | Example Value | Parsed Type | Description |
|--------------|---------------|-------------|-------------|
| `allocation_window_hours` | "18" | INTEGER | Rolling window duration for allocation |
| `max_routes_per_vehicle_in_window` | "5" | INTEGER | Maximum sequential routes per vehicle |
| `route_sequence_buffer_minutes` | "15" | INTEGER | Additional buffer between sequential routes |
| `reserve_vehicle_count` | "2" | INTEGER | Minimum vehicles to keep unallocated |
| `enable_dynamic_reallocation` | "true" | BOOLEAN | Allow mid-day re-optimization |
| `reallocation_trigger_variance_minutes` | "30" | INTEGER | Min variance to trigger re-allocation |

---

### Modular Constraint Definitions

#### **1. Energy Feasibility Constraint**

**Purpose:** Ensure vehicle has sufficient battery energy to complete route sequence.

**MAF Parameters:**

| Parameter | Example | Type | Description |
|-----------|---------|------|-------------|
| `constraint_energy_feasibility_enabled` | "true" | BOOLEAN | Enable this constraint |
| `constraint_energy_feasibility_safety_margin_kwh` | "5.0" | NUMERIC | Energy buffer to prevent deep discharge |
| `constraint_energy_feasibility_allow_dc_charging` | "true" | BOOLEAN | Consider DC charging in calculations |
| `constraint_energy_feasibility_penalty` | "-20" | NUMERIC | Penalty for insufficient energy |

**Business Rule:**
```
For each route in vehicle sequence:
  required_energy = route_miles × vehicle_efficiency_kwh_per_mile
  available_energy = current_soc + charging_time × charger_power
  
  IF available_energy < (required_energy + safety_margin):
    penalty = constraint_energy_feasibility_penalty
```

**Configuration Examples:**

Standard site:
```json
{
  "constraint_energy_feasibility_enabled": "true",
  "constraint_energy_feasibility_safety_margin_kwh": "5.0",
  "constraint_energy_feasibility_allow_dc_charging": "true",
  "constraint_energy_feasibility_penalty": "-20"
}
```

Conservative rural site (larger margin):
```json
{
  "constraint_energy_feasibility_enabled": "true",
  "constraint_energy_feasibility_safety_margin_kwh": "8.0",
  "constraint_energy_feasibility_allow_dc_charging": "true",
  "constraint_energy_feasibility_penalty": "-20"
}
```

Test environment (disabled for testing):
```json
{
  "constraint_energy_feasibility_enabled": "false"
}
```

---

#### **2. Turnaround Time Constraint (Strict)**

**Purpose:** Enforce minimum time gap between sequential routes for operational procedures.

**MAF Parameters:**

| Parameter | Example | Type | Description |
|-----------|---------|------|-------------|
| `constraint_turnaround_time_strict_enabled` | "true" | BOOLEAN | Enable strict constraint |
| `constraint_turnaround_time_strict_minimum_minutes` | "45" | INTEGER | Absolute minimum turnaround |
| `constraint_turnaround_time_strict_penalty` | "-22" | NUMERIC | Penalty for violation |

**Business Rule:**
```
For sequential routes in vehicle sequence:
  turnaround = next_route.start - previous_route.end
  
  IF constraint_turnaround_time_strict_enabled:
    IF turnaround < constraint_turnaround_time_strict_minimum_minutes:
      penalty = constraint_turnaround_time_strict_penalty
```

**Configuration Examples:**

Urban depot (tight operations):
```json
{
  "constraint_turnaround_time_strict_enabled": "true",
  "constraint_turnaround_time_strict_minimum_minutes": "30",
  "constraint_turnaround_time_strict_penalty": "-22"
}
```

Rural depot (extended turnaround):
```json
{
  "constraint_turnaround_time_strict_enabled": "true",
  "constraint_turnaround_time_strict_minimum_minutes": "60",
  "constraint_turnaround_time_strict_penalty": "-22"
}
```

Automated depot (minimal turnaround):
```json
{
  "constraint_turnaround_time_strict_enabled": "true",
  "constraint_turnaround_time_strict_minimum_minutes": "15",
  "constraint_turnaround_time_strict_penalty": "-22"
}
```

---

#### **3. Turnaround Time Constraint (Preferred)**

**Purpose:** Soft constraint encouraging comfortable turnaround times beyond strict minimum.

**MAF Parameters:**

| Parameter | Example | Type | Description |
|-----------|---------|------|-------------|
| `constraint_turnaround_time_preferred_enabled` | "true" | BOOLEAN | Enable preferred constraint |
| `constraint_turnaround_time_preferred_standard_minutes` | "75" | INTEGER | Standard comfortable turnaround |
| `constraint_turnaround_time_preferred_optimal_minutes` | "90" | INTEGER | Optimal turnaround time |
| `constraint_turnaround_time_preferred_penalty_standard` | "-2" | NUMERIC | Penalty below standard |
| `constraint_turnaround_time_preferred_penalty_optimal` | "-1" | NUMERIC | Penalty below optimal |

**Business Rule:**
```
IF constraint_turnaround_time_preferred_enabled:
  IF turnaround < standard_minutes:
    penalty = penalty_standard
  ELIF turnaround < optimal_minutes:
    penalty = penalty_optimal
  ELSE:
    penalty = 0
```

**Configuration Examples:**

Standard site:
```json
{
  "constraint_turnaround_time_preferred_enabled": "true",
  "constraint_turnaround_time_preferred_standard_minutes": "75",
  "constraint_turnaround_time_preferred_optimal_minutes": "90",
  "constraint_turnaround_time_preferred_penalty_standard": "-2",
  "constraint_turnaround_time_preferred_penalty_optimal": "-1"
}
```

Disabled for test site:
```json
{
  "constraint_turnaround_time_preferred_enabled": "false"
}
```

---

#### **4. Shift Hours Constraint (Strict)**

**Purpose:** Enforce regulatory working hour limits for driver safety and compliance.

**MAF Parameters:**

| Parameter | Example | Type | Description |
|-----------|---------|------|-------------|
| `constraint_shift_hours_strict_enabled` | "false" | BOOLEAN | Enable shift hour limits |
| `constraint_shift_hours_strict_max_hours` | "7.5" | NUMERIC | Maximum working hours |
| `constraint_shift_hours_strict_calculation_method` | "first_to_last" | STRING | "first_to_last" or "cumulative" |
| `constraint_shift_hours_strict_pre_shift_buffer_hours` | "0.5" | NUMERIC | Pre-shift loading buffer |
| `constraint_shift_hours_strict_post_shift_buffer_hours` | "0.5" | NUMERIC | Post-shift unloading buffer |
| `constraint_shift_hours_strict_penalty` | "-20" | NUMERIC | Penalty for violation |

**Business Rule:**
```
IF constraint_shift_hours_strict_enabled:
  IF calculation_method == "first_to_last":
    total_hours = (last_route.end - first_route.start) / 3600
  ELIF calculation_method == "cumulative":
    total_hours = Σ(route.duration) for all routes
  
  IF total_hours > max_hours:
    penalty = constraint_shift_hours_strict_penalty
```

**Configuration Examples:**

Standard site with driver regulations:
```json
{
  "constraint_shift_hours_strict_enabled": "true",
  "constraint_shift_hours_strict_max_hours": "7.5",
  "constraint_shift_hours_strict_calculation_method": "first_to_last",
  "constraint_shift_hours_strict_pre_shift_buffer_hours": "0.5",
  "constraint_shift_hours_strict_post_shift_buffer_hours": "0.5",
  "constraint_shift_hours_strict_penalty": "-20"
}
```

24/7 automated site (disabled):
```json
{
  "constraint_shift_hours_strict_enabled": "false"
}
```

Extended hours site:
```json
{
  "constraint_shift_hours_strict_enabled": "true",
  "constraint_shift_hours_strict_max_hours": "9.0",
  "constraint_shift_hours_strict_calculation_method": "first_to_last",
  "constraint_shift_hours_strict_penalty": "-20"
}
```

---

#### **5. Minimum Soonness Constraint**

**Purpose:** Prevent last-minute allocations that are operationally impossible.

**MAF Parameters:**

| Parameter | Example | Type | Description |
|-----------|---------|------|-------------|
| `constraint_minimum_soonness_enabled` | "true" | BOOLEAN | Enable minimum notice period |
| `constraint_minimum_soonness_hours` | "0.75" | NUMERIC | Minimum hours notice |
| `constraint_minimum_soonness_penalty` | "-20" | NUMERIC | Penalty for too-soon allocation |

**Business Rule:**
```
IF constraint_minimum_soonness_enabled:
  time_to_departure = route.start - now()
  
  IF time_to_departure < (constraint_minimum_soonness_hours × 3600):
    penalty = constraint_minimum_soonness_penalty
```

---

#### **6. Route Overlap Prevention (Mandatory)**

**Purpose:** Ensure no vehicle is assigned to routes with overlapping time windows.

**MAF Parameters:**

| Parameter | Example | Type | Description |
|-----------|---------|------|-------------|
| `constraint_route_overlap_enabled` | "true" | BOOLEAN | Always true (mandatory constraint) |
| `constraint_route_overlap_penalty` | "-20" | NUMERIC | Penalty for temporal overlap |

**Business Rule:**
```
For vehicle with route sequence [R1, R2, ..., Rk]:
  Sort by start time
  
  For i in 1 to k-1:
    route_i_end = R[i].plan_end_date_time + turnaround_time
    route_i_next_start = R[i+1].plan_start_date_time
    
    IF route_i_end > route_i_next_start:
      penalty = constraint_route_overlap_penalty  # REJECT
```

**Note:** This constraint cannot be disabled as it would violate physical impossibility (vehicle in two places at once).

---

#### **7. Charger Preference Constraint**

**Purpose:** Prioritize vehicles on preferred chargers for high-priority routes.

**MAF Parameters:**

| Parameter | Example | Type | Description |
|-----------|---------|------|-------------|
| `constraint_charger_preference_enabled` | "false" | BOOLEAN | Enable charger-based prioritization |
| `constraint_charger_preference_map` | '{"87":"3","86":"3","85":"0","DISC":"-3"}' | JSON OBJECT | Charger ID → priority mapping |
| `constraint_charger_preference_time_window_start` | "4" | INTEGER | Apply from this hour |
| `constraint_charger_preference_time_window_end` | "7" | INTEGER | Apply until this hour |
| `constraint_charger_preference_apply_to_position` | "first" | STRING | "first", "all", or "longest" route |

**Business Rule:**
```
IF constraint_charger_preference_enabled:
  route_hour = route.plan_start_date_time.hour
  
  IF time_window_start <= route_hour < time_window_end:
    IF route_position_matches_apply_to_position:
      charger_id = get_vehicle_charger(vehicle_id)
      priority = charger_preference_map[charger_id] or 0
      bonus = priority  # Positive = good, negative = bad
```

**Configuration Examples:**

Enabled with premium chargers:
```json
{
  "constraint_charger_preference_enabled": "true",
  "constraint_charger_preference_map": "{\"87\":\"3\",\"86\":\"3\",\"85\":\"0\",\"DISC\":\"-3\"}",
  "constraint_charger_preference_time_window_start": "4",
  "constraint_charger_preference_time_window_end": "7",
  "constraint_charger_preference_apply_to_position": "first"
}
```

Disabled (uniform chargers):
```json
{
  "constraint_charger_preference_enabled": "false"
}
```

---

#### **8. Swap Minimization Constraint**

**Purpose:** Prefer keeping same vehicle assignments from previous allocation to minimize disruption.

**MAF Parameters:**

| Parameter | Example | Type | Description |
|-----------|---------|------|-------------|
| `constraint_swap_minimization_enabled` | "true" | BOOLEAN | Prefer maintaining assignments |
| `constraint_swap_minimization_bonus_weight` | "0.5" | NUMERIC | Bonus for same assignment |
| `constraint_swap_minimization_lookback_hours` | "24" | INTEGER | Hours to look back |

**Business Rule:**
```
IF constraint_swap_minimization_enabled:
  previous_allocation = get_allocation(
    site_id, 
    route_id, 
    time >= (now() - lookback_hours)
  )
  
  IF previous_allocation.vehicle_id == current_vehicle_id:
    bonus = constraint_swap_minimization_bonus_weight
```

**Configuration Examples:**

Enabled for dynamic re-allocation:
```json
{
  "constraint_swap_minimization_enabled": "true",
  "constraint_swap_minimization_bonus_weight": "0.5",
  "constraint_swap_minimization_lookback_hours": "24"
}
```

Disabled for initial allocation:
```json
{
  "constraint_swap_minimization_enabled": "false"
}
```

---

#### **9. Energy Optimization Constraint**

**Purpose:** Prefer comfortable energy margins beyond minimum feasibility.

**MAF Parameters:**

| Parameter | Example | Type | Description |
|-----------|---------|------|-------------|
| `constraint_energy_optimization_enabled` | "true" | BOOLEAN | Enable margin scoring |
| `constraint_energy_optimization_margin_thresholds` | "[0.1, 0.2, 0.3]" | JSON ARRAY | Margin % thresholds |
| `constraint_energy_optimization_scores` | "[0.1, 0.3, 0.5]" | JSON ARRAY | Scores for each threshold |

**Business Rule:**
```
IF constraint_energy_optimization_enabled:
  energy_margin_pct = (available - required) / battery_capacity
  
  For i from highest to lowest threshold:
    IF energy_margin_pct >= thresholds[i]:
      bonus = scores[i]
      break
```

---

### Parameter Parsing Implementation

```python
def parse_maf_parameter(param_key: str, param_value: str):
    """
    Parse MAF string parameter to appropriate type.
    
    Args:
        param_key: Parameter name (e.g., "constraint_turnaround_time_strict_enabled")
        param_value: String value (e.g., "true", "45", "[1, 2, 3]")
    
    Returns:
        Parsed value in appropriate type
    """
    
    # Handle None/empty
    if param_value in ['NONE', 'None', 'none', 'NO_VALUE', '', None]:
        return None
    
    # Boolean detection
    if param_key.endswith('_enabled') or param_key.endswith('_flag') or \
       param_value.lower() in ['true', 'false', 'yes', 'no']:
        return param_value.lower() in ['true', 'yes', '1']
    
    # JSON array
    if param_value.strip().startswith('['):
        try:
            return json.loads(param_value)
        except json.JSONDecodeError:
            logger.error(f"Failed to parse JSON array for {param_key}: {param_value}")
            return None
    
    # JSON object
    if param_value.strip().startswith('{'):
        try:
            return json.loads(param_value)
        except json.JSONDecodeError:
            logger.error(f"Failed to parse JSON object for {param_key}: {param_value}")
            return None
    
    # Numeric detection
    numeric_suffixes = ['_minutes', '_hours', '_seconds', '_kwh', '_penalty', 
                        '_weight', '_bonus', '_threshold', '_count', '_margin']
    if any(param_key.endswith(suffix) for suffix in numeric_suffixes):
        try:
            if '.' not in param_value:
                return int(param_value)
            else:
                return float(param_value)
        except ValueError:
            logger.error(f"Failed to parse numeric for {param_key}: {param_value}")
            return None
    
    # Time format
    if ':' in param_value and param_key.endswith('_period'):
        try:
            return datetime.strptime(param_value, '%H:%M:%S').time()
        except ValueError:
            logger.error(f"Failed to parse time for {param_key}: {param_value}")
            return None
    
    # Default: string
    return param_value


def get_constraint_config(site_id: int, constraint_name: str, maf_params: dict):
    """
    Retrieve constraint configuration from MAF parameters.
    
    Returns:
        dict with 'enabled' (bool), 'params' (dict), 'penalty' (numeric)
    """
    
    enabled_key = f"constraint_{constraint_name}_enabled"
    enabled = parse_maf_parameter(enabled_key, maf_params.get(enabled_key, "true"))
    
    if not enabled:
        return {'enabled': False, 'params': {}, 'penalty': 0}
    
    # Extract all parameters for this constraint
    constraint_params = {}
    prefix = f"constraint_{constraint_name}_"
    
    for key, value in maf_params.items():
        if key.startswith(prefix) and key != enabled_key:
            param_name = key[len(prefix):]
            constraint_params[param_name] = parse_maf_parameter(key, value)
    
    penalty = constraint_params.get('penalty', DEFAULT_PENALTIES.get(constraint_name, -20))
    
    return {
        'enabled': True,
        'params': constraint_params,
        'penalty': penalty
    }
```

---

## Integration Points

### Microlise API Integration

**Purpose:** Microlise is the external Transport Management System managing routes and vehicle telematics.

**Authentication:** OAuth 2.0 Client Credentials Grant

**Token Request:**
```http
POST {JLP_Microlise_Token_URL}
Content-Type: application/x-www-form-urlencoded
Authorization: Basic {base64(client_id:client_secret)}

grant_type=client_credentials&scope=journeyallocatevehicle
```

**Vehicle Allocation Endpoint:**
```http
POST {JLP_Microlise_JourneysWebAPI_URL}{route_id}/vehicles/
Content-Type: application/json
Authorization: Bearer {access_token}

{
  "VehicleName": "706.25003T25",
  "scope": "journeyallocatevehicle"
}
```

**Success Responses:**
- 200: Vehicle already allocated (no change)
- 201: New allocation created

**Error Responses:**
- 400: Bad request
- 401: Unauthorized
- 403: Forbidden
- 404: Resource not found

---

### Azure Blob Storage Integration

**Purpose:** Store daily allocation reports.

**Reports Generated:**
1. Morning Allocation Report (if `initial_report = "true"`)
2. Compliance Report (if `compliance_report = "true"`)
3. Unallocated Routes Report (if `unallocated_report = "true"`)

**File Format:** Excel (.xlsx) with multiple sheets

---

### Module Asset Framework (MAF) Integration

**Purpose:** Centralized configuration management with all parameters as `{String: String}`.

**Access:** `sp_get_module_params(application_name)`

**Configuration Hierarchy:**
1. System defaults (hard-coded)
2. MAF parameters (site-level string values)

---

## Operational Scenarios

### Scenario 1: Initial 18-Hour Allocation

**Context:** Daily allocation at 04:30 AM for next 18 hours (04:30 AM → 10:30 PM)

**Process:**
1. Define window: 04:30 → 22:30
2. Filter 42 routes in window
3. Calculate vehicle availability and sequences
4. Load MAF constraint configurations
5. Build cost matrix with modular constraints
6. Solve optimization (8 seconds)
7. Validate temporal sequences
8. Push to Microlise
9. Generate reports

**Outcome:** All routes allocated, 2 reserve vehicles, average 3.2 routes per vehicle

---

### Scenario 2: Mid-Day Re-Allocation

**Context:** Route cancelled at 11:45 AM

**Process:**
1. New window: 11:45 AM → 05:45 AM (next day)
2. Identify 18 reallocable routes
3. Check swap minimization constraint (enabled with high priority)
4. Re-optimize minimizing disruption
5. Update allocation table
6. No Microlise calls needed

**Outcome:** Minimal disruption, 0 swaps, 2-second re-optimization

---

### Scenario 3: Site-Specific Constraint Override

**Context:** Rural site needs extended turnaround times

**MAF Configuration:**
```json
{
  "site_id": 25,
  "constraint_turnaround_time_strict_enabled": "true",
  "constraint_turnaround_time_strict_minimum_minutes": "60",
  "constraint_shift_hours_strict_enabled": "true",
  "constraint_shift_hours_strict_max_hours": "9.0"
}
```

**Outcome:** Site 25 operates with 60-min turnaround vs 45-min standard, purely configuration-driven

---

## Key Business Metrics

### 18-Hour Window Utilization

```
utilization = (allocated_hours / available_vehicle_hours) × 100%
```

**Targets:**
- Peak: 70-85%
- Off-Peak: 50-65%

---

### Route Sequence Length

```
avg_sequence_length = total_routes / active_vehicles
```

**Typical:** 2-3 routes per vehicle (balanced utilization and buffer)

---

### Temporal Conflict Rate

```
conflict_rate = (temporal_conflicts / total_combinations) × 100%
```

**Target:** <15%

---

## Assumptions & Dependencies

### Assumptions

**18-Hour Window Model:**
1. Routes beyond 18-hour horizon can be ignored
2. Route durations predictable within ±15 minutes
3. At least 2 reserve vehicles available
4. Dynamic re-allocation triggered within 5 minutes

**MAF Parameters:**
1. All parameters stored as `{String: String}`
2. Application responsible for type parsing
3. Invalid values fall back to defaults
4. Changes take effect on next allocation run

**Modular Constraints:**
1. Constraint configuration changes non-retroactive
2. Disabling mandatory constraints requires admin override
3. Missing constraint parameters use system defaults

---

### Dependencies

**External Systems:**
1. Microlise TMS (route plans, allocation API, actuals)
2. VSM Feed (vehicle telemetry)
3. MAF (configuration parameters)
4. Azure Blob Storage (reports)

**Database Tables:**
- Master: `t_client`, `t_site`, `t_vehicle`, `t_charger`
- Operational: `t_route_plan`, `t_vsm`, `t_vehicle_charge`
- Allocation: `t_allocation_monitor`, `t_route_allocated`

**Infrastructure:**
- PostgreSQL 12+
- Python 3.8+
- Hexaly Solver (optional)

---

## Future Enhancements

### Near-Term (3-6 Months)

**1. Rolling Window Continuous Re-Planning**
- Every 30-60 minutes, re-optimize remaining routes in window
- Incorporate latest VSM data

**2. Constraint Learning Engine**
- Track violations and suggest configuration adjustments
- Auto-tune constraint parameters

**3. Multi-Vehicle Route Splitting**
- Automatically split long routes between vehicles
- Coordinate handoffs

---

### Mid-Term (6-12 Months)

**4. Multi-Site Optimization**
- Optimize across site clusters
- Enable vehicle sharing between sites

**5. Driver Rostering Integration**
- Match driver availability to vehicle assignments
- Consider driver skills and preferences

**6. Real-Time Allocation Dashboard**
- Live view of allocations
- Manual override interface
- Constraint score breakdown visualization

---

### Long-Term (12-24 Months)

**7. ML-Based Route Duration Prediction**
- Predict actual durations from historical data
- Account for weather, traffic, time of day

**8. Configuration Validation Service**
- Validate MAF parameters before deployment
- Type checking, range checking, conflict detection

**9. Configuration Versioning**
- Track changes over time
- A/B testing of constraint parameters
- Rollback capability

**10. Dynamic Constraint Tuning**
- ML recommendations for optimal settings
- Identify over/under-constrained scenarios

**11. Per-Vehicle Constraint Overrides**
- Extend MAF to support vehicle-level configuration
- Custom constraints for specialized vehicles

---

**End of Specification**

---

**Document Control:**
- **Version:** 2.0
- **Date:** February 11, 2026
- **Author:** System Analysis Team
- **Changes from v1.0:**
  - Replaced shift-based allocation with 18-hour rolling window
  - Implemented MAF-based modular constraint framework
  - All parameters in `{String: String}` format
  - Added temporal overlap prevention
  - Site-specific constraint configuration
- **Next Review:** August 2026
