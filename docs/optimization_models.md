# Optimization Models: Allocation & Scheduling

Concise reference for the Hexaly-based **allocation** and **charge scheduling** optimization models.

---

## 1. Allocation Model (`hexaly_solver.py`)

**Purpose:** Assign routes to vehicles by selecting at most one **sequence** per vehicle so that as many routes as possible are covered, then maximize total score.

### Decision Variables

| Variable | Type | Meaning |
|----------|------|--------|
| `sequence_vars[i]` | Binary | 1 if sequence \(i\) is selected, 0 otherwise |
| `route_covered[r]` | Binary | 1 if route \(r\) is covered by at least one selected sequence |

### Objective

**Maximize:**  
\( \displaystyle \underbrace{W \cdot \sum_r \text{route\_covered}_r}_{\text{route count (priority)}} + \sum_i \text{sequence\_vars}_i \cdot \text{cost}_i \)  

- \(W = 10^2\) so that one extra allocated route dominates any realistic score difference.
- First priority: number of routes allocated; second: total sequence score (cost).

### Constraints

| ID | Description |
|----|-------------|
| **One sequence per vehicle** | For each vehicle, the sum of `sequence_vars` over all sequences using that vehicle is \(\leq 1\). |
| **Each route at most once** | For each route, the sum of `sequence_vars` over sequences that cover that route is \(\leq 1\). |
| **Route covered definition** | For each route \(r\): \(\text{route\_covered}_r \leq \text{coverage\_sum}\) and \(\text{coverage\_sum} \leq \text{num\_covering} \cdot \text{route\_covered}_r\). So `route_covered[r] = 1` iff at least one selected sequence covers \(r\). |

### Inputs / Fallback

- **Inputs:** `sequences` (vehicle_id, route_sequence, cost), `route_ids`, `sequence_costs`.
- **Fallback:** Greedy: sort sequences by cost (best first), select without reusing a vehicle or covering a route twice.

---

## 2. Charge Scheduling Model (`charge_optimizer.py`)

**Purpose:** Decide how much to charge each vehicle in each 30‑minute slot so that route energy is met at departure (hard), site capacity and other physical limits are respected, and total electricity cost (and target SOC shortfall) is minimized.

### Decision Variables

| Variable | Type | Bounds | Meaning |
|----------|------|--------|---------|
| `charge_power[t][v]` | Float | \([0, \text{ac\_charge\_rate}_v]\) | Charging power (kW) for vehicle \(v\) in slot \(t\). |
| `cumulative_energy[t][v]` | Float | \([0, \text{headroom}_v]\) | Cumulative energy (kWh) delivered to \(v\) by end of slot \(t\). |
| `shortfall_v` | Float | \([0, \text{max\_shortfall}_v]\) | Shortfall (kWh) from target SOC for vehicle \(v\) (soft). |

Slot length = 0.5 h ⇒ energy in slot = `charge_power[t][v] * 0.5` (kWh).

### Objective

**Minimize:**  
\( \displaystyle \sum_{t,v} \underbrace{\big( \text{price}_t + \text{synthetic\_price}_t + \text{triad}_t \big) \cdot \text{energy}_{t,v}}_{\text{cost}} + \lambda \sum_v \text{shortfall}_v \)

- **price_t:** Electricity price in slot \(t\).
- **synthetic_price_t:** Time preference (earlier slots cheaper): `synthetic_time_price_factor * (n_slots - t) / n_slots`.
- **triad_t:** TRIAD penalty multiplier in TRIAD slots, 0 otherwise.
- **shortfall_v:** \(\geq \max(0, \text{target\_soc}_v - \text{initial\_soc}_v - \text{cumulative\_energy}[T-1][v])\). Penalty \(\lambda = \text{target\_soc\_shortfall\_penalty}\).

So: minimize cost first, then get as close as possible to target SOC (soft).

### Hard Constraints

| ID | Description |
|----|-------------|
| **Cumulative energy** | \(t=0\): \(\text{cumulative\_energy}[0][v] = \text{charge\_power}[0][v] \cdot 0.5\). For \(t \geq 1\): \(\text{cumulative\_energy}[t][v] = \text{cumulative\_energy}[t-1][v] + \text{charge\_power}[t][v] \cdot 0.5\). |
| **Route energy (EQ-ROUTE)** | For each route checkpoint (departure) at slot index \(k\): \(\text{cumulative\_energy}[k-1][v] \geq \text{required\_for\_route}\) where \(\text{required\_for\_route} = \max(0, \text{cumulative\_energy\_kwh} - \text{current\_soc\_kwh})\). |
| **Site capacity (EQ-02)** | For each \(t\): \(\sum_v \text{charge\_power}[t][v] \leq \max(0, \text{site\_capacity\_kw} - \text{forecast}_t)\). |
| **Max SOC (EQ-04)** | For each \(v,t\): \(\text{cumulative\_energy}[t][v] \leq \text{battery\_capacity}_v - \text{current\_soc}_v\). |
| **Charge rate (EQ-07/08)** | For each \(v,t\): \(\text{charge\_power}[t][v] \leq \text{ac\_charge\_rate}_v\). |
| **Availability (EQ-09)** | If vehicle \(v\) is not available in slot \(t\): \(\text{charge\_power}[t][v] = 0\). |

### Soft Constraint

- **Target SOC:** No hard constraint; shortfall from target SOC is penalized in the objective via `shortfall_v`.

### Inputs / Fallback

- **Inputs:** vehicles, vehicle_states, energy_requirements (route checkpoints), availability_matrices, time_slots, forecast_data, price_data (with TRIAD flags), site_capacity_kw, target_soc_percent, triad_penalty_factor, synthetic_time_price_factor, target_soc_shortfall_penalty.
- **Fallback:** Greedy: per vehicle, sort available slots by effective price (price + TRIAD penalty), fill energy need in cheapest slots first, respecting rate and availability.

---

## Summary

| Aspect | Allocation (`hexaly_solver`) | Scheduling (`charge_optimizer`) |
|--------|------------------------------|---------------------------------|
| **Goal** | Maximize routes covered, then score | Minimize charging cost + target SOC shortfall |
| **Variables** | Binary: sequence selection, route covered | Float: power per slot, cumulative energy, shortfall |
| **Key hard constraints** | One sequence per vehicle; each route at most once | Route energy at departure, site capacity, SOC cap, rate, availability |
| **Soft** | — | Target SOC (shortfall penalty) |
| **Solver** | Hexaly (or greedy) | Hexaly (or greedy) |
