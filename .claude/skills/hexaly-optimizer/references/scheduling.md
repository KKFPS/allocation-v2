# Hexaly Optimizer: Scheduling Reference

Hexaly natively supports rich scheduling constructs via interval variables and cumulative functions. This reference covers common scheduling patterns in Python.

---

## 1. Basic Job Shop / Flow Shop

```python
import hexaly.optimizer

n_jobs = 5
n_machines = 3
# proc_time[j][m] = processing time of job j on machine m
# order[j] = list of machine indices for job j

with hexaly.optimizer.HexalyOptimizer() as optimizer:
    m = optimizer.model
    horizon = sum(sum(row) for row in proc_time)

    # Create one interval per job-machine operation
    ops = [[m.interval_var(0, horizon) for _ in range(n_machines)]
           for _ in range(n_jobs)]

    for j in range(n_jobs):
        for k, machine in enumerate(order[j]):
            ops[j][k].duration_min = ops[j][k].duration_max = proc_time[j][machine]
            # Precedence within job
            if k > 0:
                m.constraint(m.end(ops[j][k-1]) <= m.start(ops[j][k]))

    # No overlap on same machine (disjunctive)
    for machine in range(n_machines):
        ops_on_machine = [ops[j][order[j].index(machine)]
                          for j in range(n_jobs) if machine in order[j]]
        m.constraint(m.no_overlap(ops_on_machine))

    makespan = m.max([m.end(ops[j][-1]) for j in range(n_jobs)])
    m.minimize(makespan)
    m.close()

    optimizer.param.time_limit = 30
    optimizer.solve()

    for j in range(n_jobs):
        for k in range(n_machines):
            iv = optimizer.solution.get_interval_value(ops[j][k])
            print(f"Job {j}, Op {k}: [{iv.start}, {iv.end})")
```

---

## 2. Resource-Constrained Project Scheduling (RCPSP)

```python
# Tasks with resource demands and precedences

n_tasks = 10
n_resources = 3
capacities = [4, 3, 5]  # per resource

# durations[t], demands[t][r], predecessors[t]

with hexaly.optimizer.HexalyOptimizer() as optimizer:
    m = optimizer.model
    horizon = 100

    tasks = [m.interval_var(0, horizon) for _ in range(n_tasks)]
    for t in range(n_tasks):
        tasks[t].duration_min = tasks[t].duration_max = durations[t]

    # Precedence constraints
    for t in range(n_tasks):
        for pred in predecessors[t]:
            m.constraint(m.end(tasks[pred]) <= m.start(tasks[t]))

    # Cumulative resource constraints
    for r in range(n_resources):
        usage = m.sum([m.pulse(tasks[t], demands[t][r])
                       for t in range(n_tasks)])
        m.constraint(usage <= capacities[r])

    # Minimize makespan
    m.minimize(m.max([m.end(t) for t in tasks]))
    m.close()

    optimizer.param.time_limit = 30
    optimizer.solve()
```

---

## 3. Optional Intervals (Flexible Scheduling)

```python
# Some tasks may not need to be scheduled (optional)

task = m.interval_var(0, horizon)
task.is_optional = True   # can be absent

# If present, must start after time 10
m.constraint(m.iif(m.if_present(task), m.start(task) >= 10, 1))

# Penalty if not present
penalty = m.iif(m.not_(m.if_present(task)), 100, 0)
m.minimize(penalty)
```

---

## 4. Time Windows (CVRPTW Style with Interval Variables)

For vehicle routing with time windows, combine list variables with interval variables:

```python
n_customers = 20
n_vehicles = 4

with hexaly.optimizer.HexalyOptimizer() as optimizer:
    m = optimizer.model

    routes = [m.list(n_customers) for _ in range(n_vehicles)]
    m.constraint(m.partition(routes))

    # Arrival time at each position as interval variable
    arrivals = [[m.interval_var(earliest[i], latest[i])
                 for i in range(n_customers)]
                for _ in range(n_vehicles)]

    # Service time
    for v in range(n_vehicles):
        for c in range(n_customers):
            arrivals[v][c].duration_min = arrivals[v][c].duration_max = service_time[c]

    # Travel time constraint: arrival[v][pos+1] >= arrival[v][pos] + travel
    # ... model with lambda or explicit sequence constraints

    m.minimize(total_distance)
    m.close()
    optimizer.param.time_limit = 60
    optimizer.solve()
```

---

## 5. Employee Scheduling (Shift Assignment)

```python
# Assign employees to shifts; min coverage per shift

n_employees = 10
n_shifts = 5
# shift_demand[s] = min employees needed on shift s

with hexaly.optimizer.HexalyOptimizer() as optimizer:
    m = optimizer.model

    # x[e][s] = 1 if employee e works shift s
    x = [[m.bool() for s in range(n_shifts)] for e in range(n_employees)]

    # Coverage per shift
    for s in range(n_shifts):
        covered = m.sum([x[e][s] for e in range(n_employees)])
        m.constraint(covered >= shift_demand[s])

    # Max shifts per employee
    for e in range(n_employees):
        m.constraint(m.sum([x[e][s] for s in range(n_shifts)]) <= max_shifts[e])

    # Minimize total cost
    cost = m.sum([cost_matrix[e][s] * x[e][s]
                  for e in range(n_employees)
                  for s in range(n_shifts)])
    m.minimize(cost)

    m.close()
    optimizer.param.time_limit = 10
    optimizer.solve()
```

---

## 6. Scheduling Modeling Tips

- **Horizon**: set `max_end` of interval_var to a reasonable upper bound (sum of all durations works but may be loose). Tighter horizons → faster solving.
- **Fixed duration**: set `duration_min == duration_max`.
- **No-overlap groups**: `m.no_overlap(list)` is more efficient than pairwise `m.no_overlap(a, b)` for large machine groups.
- **Cumulative resources**: use `m.pulse(iv, h)` for constant usage during interval; use `m.step_at_start` / `m.step_at_end` for state-change resources.
- **Makespan objective**: `m.max([m.end(t) for t in tasks])` — very common pattern.
- **Lexicographic multi-objective**: declare cost first, then makespan, to minimize cost while breaking ties on makespan (or vice versa).
- **Warm start**: if you have a feasible schedule, inject it via `sol.set_interval_value(iv, HxInterval(s, e))` before `solve()`.