Here's the phased implementation spec:

---

# Phased Implementation Spec

## Shared Foundations (All Phases)

### Node Pool
```
indices [0, nbRoutes)                          → route nodes
indices [nbRoutes, nbRoutes + nbChargers*nbTimesteps)  → charge nodes (Phase 2+)
```

### Core Sequence Structure
```python
vehicleSequence[v] <- list(nbNodes)
constraint disjoint(vehicleSequence)
```

### Shared Data Arrays
```python
isCharge     = m.array([0]*nbRoutes + [1]*nbChargers*nbTimesteps)
nodeReward   = m.array(routePrizes + chargePrizes)
```

---

## Phase 1: Route Allocation Only

### Nodes
Only route nodes exist. `nbNodes = nbRoutes`.

### Sequence
```python
vehicleSequence[v] <- list(nbRoutes)
constraint disjoint(vehicleSequence)
```

### Forbidden / Mandatory
```python
for n in forbiddenNodesPerVehicle[v]:
    constraint !contains(vehicleSequence[v], n)

for n in mandatoryNodesPerVehicle[v]:
    constraint contains(vehicleSequence[v], n)
```

### Transition Feasibility
Forbidden arcs encoded as large values in `distanceMatrix`. Infeasible route-to-route transitions (e.g. timing clash, wrong vehicle type) get `bigValue`:

```python
routeDelay[v] <- sum(1...c, i => distanceMatrix[sequence[i-1]][sequence[i]])
constraint routeDelay[v] < bigValuePenalty
```

### SOC — Decreasing Only, Hard Floor at 0
No charge nodes, SOC only decreases. Recursive array pattern from the model:

```python
SOCAfterNode[v] <- array(0...count(sequence), (n, prev) =>
    (n == 0 ? batteryStartSOC[v] : prev)
    - electricityConsumption[v][sequence[n]]
)

outOfBattery[v] <- sum(0...c, n => max(0, -SOCAfterNode[v][n]))
constraint outOfBattery[v] == 0
```

### Objective
```python
maximize sum(v, sum(vehicleSequence[v], j => nodeReward[j]))
```

---

## Phase 2: Route Allocation + Homogeneous Charge Scheduling

### Additions over Phase 1
- Charge nodes added to pool: `nbNodes = nbRoutes + nbChargers * nbTimesteps`
- All charge nodes treated identically — fixed global charging power `P_fixed`
- Electricity cost included in objective via `nodeTripPrize` (negative values for charge nodes)
- Site capacity constraint active

### Node Pool
```python
isCharge  = m.array([0]*nbRoutes + [1]*(nbChargers*nbTimesteps))
nodeReward = m.array(routePrizes + [electricityCostPerSlot]*nbChargers*nbTimesteps)
```

### Transition Feasibility
`distanceMatrix` extended to cover charge nodes. All charger-to-charger transitions across different timesteps within same charger are allowed; cross-charger same-timestep transitions get `bigValue` (same arc-based approach as model comment):

```python
# distanceMatrix[c0t0][c1t0] = bigValue   (same timestep, different charger)
# distanceMatrix[c0t0][c0t1] = 0          (consecutive timestep, same charger)
routeDelay[v] <- sum(1...c, i => distanceMatrix[sequence[i-1]][sequence[i]])
constraint routeDelay[v] < bigValuePenalty
```

### SOC — Bidirectional
```python
SOCAfterNode[v] <- array(0...count(sequence), (n, prev) =>
    isCharge[sequence[n]] > 0
    ? min((n==0 ? batteryStartSOC[v] : prev) + P_fixed, batteryMaxSOC[v])
    : max((n==0 ? batteryStartSOC[v] : prev) - electricityConsumption[v][sequence[n]], 0)
)

outOfBattery[v] <- sum(0...c, n => max(0, -SOCAfterNode[v][n]))
constraint outOfBattery[v] == 0
```

### Site Capacity Constraint
Since all chargers share the same power `P_fixed`, capacity is just a count of active charge nodes per timestep:

```python
constraint and[t in 0...nbTimesteps](
    sum(v, sum(c in 0...nbChargers,
        contains(vehicleSequence[v], nbRoutes + c*nbTimesteps + t)
    )) * P_fixed < capacityPowerSite[t]
)
```

### Objective
```python
maximize sum(v, sum(vehicleSequence[v], j => nodeReward[j]))
# nodeReward for charge nodes is negative (electricity cost)
# net objective = allocation reward - charging cost
```

---

## Phase 3: Complete Model

### Additions over Phase 2
- Per-charger max power: `chargerType[c]` defines upper bound
- `chargingPowerUsed[c][t]` as decision variable `m.int(0, chargerType[c])`
- Charger switching constraints via `distanceMatrix` forbidden arcs (already partially in Phase 2)
- Site capacity uses actual power variables, not fixed power count

### Power Decision Variables
```python
chargingPowerUsed[c][t] <- int(0, chargerType[c])
```

Power for a charge node `nbRoutes + c*nbTimesteps + t` is `chargingPowerUsed[c][t]`.

### SOC — Variable Power
```python
SOCAfterNode[v] <- array(0...count(sequence), (n, prev) =>
    isCharge[sequence[n]] > 0
    ? min((n==0 ? batteryStartSOC[v] : prev)
          + chargingPowerUsed[chargerOf(sequence[n])][timestepOf(sequence[n])] * 0.5,
          batteryMaxSOC[v])
    : max((n==0 ? batteryStartSOC[v] : prev)
          - electricityConsumption[v][sequence[n]], 0)
)
```

### Site Capacity — Variable Power
```python
constraint and[t in 0...nbTimesteps](
    sum(v, sum(c in 0...nbChargers,
        contains(vehicleSequence[v], nbRoutes + c*nbTimesteps + t)
        * chargingPowerUsed[c][t]
    )) < capacityPowerSite[t]
)
```

### Charger Switching — Forbidden Arcs
A vehicle cannot jump between two different chargers at the same timestep. Already enforced via `distanceMatrix` `bigValue` arcs. Additionally, between two route nodes, charger reassignment is free — enforced by allowing all `distanceMatrix[route][charger]` transitions.

```python
# Forbidden: same timestep, different charger
distanceMatrix[nbRoutes + c0*nbTimesteps + t][nbRoutes + c1*nbTimesteps + t] = bigValue  # c0 != c1

# Allowed: consecutive timestep, same charger
distanceMatrix[nbRoutes + c*nbTimesteps + t][nbRoutes + c*nbTimesteps + (t+1)] = 0

# Charger change allowed after route node — no restriction on distanceMatrix[route][any_charger]
```

### Objective
```python
maximize sum(v, sum(vehicleSequence[v], j => nodeReward[j]))
# nodeReward[charge node c,t] = -electricityPrice[t] * chargingPowerUsed[c][t] * 0.5
```

---

## Phase Progression Summary

| Feature | Phase 1 | Phase 2 | Phase 3 |
|---|---|---|---|
| Route allocation | ✅ | ✅ | ✅ |
| `disjoint(vehicleSequence)` | ✅ | ✅ | ✅ |
| Forbidden/mandatory nodes | ✅ | ✅ | ✅ |
| SOC tracking (decrease only) | ✅ | ✅ | ✅ |
| Charge nodes in sequence | ❌ | ✅ | ✅ |
| SOC increase on charge | ❌ | ✅ (fixed P) | ✅ (variable P) |
| Electricity cost in objective | ❌ | ✅ | ✅ |
| Site capacity constraint | ❌ | ✅ (count-based) | ✅ (power-based) |
| Per-charger power variable | ❌ | ❌ | ✅ |
| Charger switching constraints | ❌ | ❌ | ✅ |