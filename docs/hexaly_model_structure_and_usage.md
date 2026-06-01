# Hexaly Model Structure and Usage

Specification for the unified list-based vehicle routing model with optional charge scheduling (Phase 2) and variable charger power (Phase 3).

---

## Shared Foundations

### Node pool
```
indices [0, nbRoutes)                                    → route nodes
indices [nbRoutes, nbRoutes + nbChargers * nbTimesteps)   → charge nodes (when scheduling enabled)
```

### Core sequence structure
```python
vehicleSequence[v] <- list(nbNodes)
constraint disjoint(vehicleSequence)
```

### Shared data arrays (integrated model)
```python
isCharge   = m.array([0]*nbRoutes + [1]*(nbChargers*nbTimesteps))
nodeReward = m.array(routePrizes + chargePrizes)
```

---

## Route allocation

### Nodes
Only route nodes. `nbNodes = nbRoutes`.

### Forbidden / mandatory
```python
for n in forbiddenNodesPerVehicle[v]:
    constraint !contains(vehicleSequence[v], n)

for n in mandatoryNodesPerVehicle[v]:
    constraint contains(vehicleSequence[v], n)
```

### No overlapping routes on the same vehicle
Pairwise constraints (in addition to consecutive arc costs) forbid two routes on one vehicle when neither can precede the other with required turnaround (includes time overlap). This also applies when charge nodes appear between routes in the sequence.

```python
for (r1, r2) in incompatibleRoutePairs:
    constraint !(contains(vehicleSequence[v], r1) && contains(vehicleSequence[v], r2))
```

### Transition feasibility
Route-to-route infeasible arcs use `BIG_VALUE` in `distanceMatrix`:

```python
routeDelay[v] <- sum(1...count, i => distanceMatrix[sequence[i-1]][sequence[i]])
constraint routeDelay[v] < bigValuePenalty
```

Use `range(1, count(seq))` in Hexaly so `sequence[i-1]` is never read at `i=0`.

### SOC (routes only)
SOC decreases along routes; hard floor at 0:

```python
SOCAfterNode[v] <- array(0...count(sequence), (n, prev) =>
    prev - electricityConsumption[v][sequence[n]], start=batteryStartSOC[v])

outOfBattery[v] <- sum(0...count, n => max(0, -SOCAfterNode[v][n]))
constraint outOfBattery[v] == 0
```

### Objective
```python
maximize route_count_weight * count(route nodes in seq)
       + sum(v, sum(vehicleSequence[v], j => nodeReward[j]))
```

Implemented in `RouteAllocationOptimizer` (`src/optimizer/allocation_optimizer.py`).

**API:** `mode=["allocation"]` only.

---

## Integrated charge scheduling (Phase 2)

Homogeneous charging: fixed `P_fixed` kW per active charge node.

**API:** `charge_scheduling` without `charger_allocation`.

### Node pool
`nbNodes = nbRoutes + nbChargers * nbTimesteps`

Each charger has exactly **48** charge nodes: 30-minute slots covering a 24-hour horizon (`nbTimesteps = 48`).

### Transition feasibility (extended matrix)
- Route–route: turnaround / overlap rules (unchanged).
- Route → charge: `0` (free reassignment after route).
- Charge(c,t) → charge(c,t+1): `0`.
- Charge(c0,t) → charge(c1,t), c0≠c1: `BIG_VALUE`.
- Charge → route: gap from slot end to route start; `BIG_VALUE` if infeasible.

### SOC — bidirectional (fixed power)
```python
SOCAfterNode[v] <- array(0...count(sequence), (n, prev) =>
    isCharge[sequence[n]] > 0
    ? min(prev + P_fixed * slot_hours, batteryMaxSOC[v])
    : max(prev - electricityConsumption[v][sequence[n]], 0),
    start=batteryStartSOC[v])
```

### Max routes per vehicle
`max_routes_per_vehicle` applies to **each** vehicle sequence (route nodes only), not as a fleet-wide route total.

### Site capacity
Per timestep `t`: `count(active charge nodes) * P_fixed < capacityPowerSite[t]`.

### Objective
Route-count bonus on route nodes, plus `sum(nodeReward)` (charge nodes have negative static prizes = electricity cost), minus SOC shortfall soft penalty (see integrated section in code).

Implemented in `UnifiedOptimizer` when `enable_variable_charger_power=False`.

---

## Variable charger power (Phase 3)

**API:** include `charger_allocation` with `charge_scheduling`  
e.g. `mode=["allocation","charge_scheduling","charger_allocation"]`.

### Power decision variables
```python
chargingPowerUsed[c][t] <- int(0, chargerMaxPowerKw[c])
```

Linked to node visits:
```python
constraint chargingPowerUsed[c][t] <= chargerMaxPowerKw[c] * sum_v contains(vehicleSequence[v], chargeNode(c,t))
```

### SOC — variable power
```python
SOCAfterNode[v] <- array(..., (n, prev) =>
    isCharge[sequence[n]] > 0
    ? min(prev + chargingPowerUsed[c][t] * slot_hours, batteryMaxSOC[v])
    : max(prev - electricityConsumption[v][sequence[n]], 0))
```
(`c`,`t` decoded from `sequence[n]` via `node_to_charger` / `node_to_timestep` arrays.)

### Site capacity — variable power
```python
constraint sum(c, chargingPowerUsed[c][t]) < capacityPowerSite[t]   # per timestep t
```

### Objective — charging cost
Charge node static prizes are zero; cost is explicit:
```python
maximize ... - sum(c,t, electricityPrice[t] * chargingPowerUsed[c][t] * slot_hours)
```

Phase 2 behaviour is unchanged when `charger_allocation` is omitted.

---

## API modes

| `mode` | Solver | Charging model |
|--------|--------|----------------|
| `["allocation"]` | `RouteAllocationOptimizer` | — |
| `["charge_scheduling"]` | `UnifiedOptimizer` | Phase 2 homogeneous |
| `["allocation","charge_scheduling"]` | `UnifiedOptimizer` | Phase 2 homogeneous |
| `["…","charger_allocation"]` | `UnifiedOptimizer` | Phase 3 variable power (requires `charge_scheduling`) |

Persistence: `t_route_allocated`, `t_charge_schedule` (`charge_power`, `assigned_charger_power_kw`).
