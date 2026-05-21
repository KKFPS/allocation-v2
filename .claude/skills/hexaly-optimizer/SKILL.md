---
name: hexaly-optimizer
description: >
  Expert guide for building and solving optimization models using the Hexaly Optimizer Python API
  (hexaly==14.0.20250814). Use this skill whenever the user is writing, debugging, or designing any
  Hexaly Optimizer Python code — including routing, scheduling, assignment, packing, blending,
  production, or any combinatorial/continuous optimization problem. Triggers on mentions of:
  HexalyOptimizer, HxModel, list variables, interval variables, set variables, lambda expressions,
  array expressions, m.list(), m.set(), m.interval_var(), m.sum(), m.lambda_function(), routing
  optimization, job scheduling in Hexaly, VRP in Python, TSP in Hexaly, or any use of
  hexaly.optimizer. Always use this skill when helping with Hexaly modeling even if the user only
  asks for a "quick example" or says they're "just getting started."
compatibility:
  python: ">=3.8"
  package: "hexaly==14.0.20250814"
  install: "pip install hexaly==14.0.20250814"
---
 
# Hexaly Optimizer Python Skill
 
## Quick Reference: Core Workflow
 
```python
import hexaly.optimizer
 
with hexaly.optimizer.HexalyOptimizer() as optimizer:
    m = optimizer.model          # HxModel — build your model here
 
    # --- declare variables and expressions ---
    x = m.bool()                 # Boolean decision
    y = m.int(0, 100)            # Integer in [0, 100]
    r = m.float(0.0, 1.0)        # Float in [0.0, 1.0]
 
    # --- constraints and objectives ---
    m.constraint(x + y <= 50)
    m.minimize(y - 10 * x)
 
    m.close()                    # MUST call before solve()
 
    optimizer.param.time_limit = 5   # seconds
    optimizer.solve()
 
    sol = optimizer.solution
    print(sol.status)            # OPTIMAL, FEASIBLE, INFEASIBLE, INCONSISTENT
    print(x.value, y.value)
```
 
**Key rule:** Always call `m.close()` before `optimizer.solve()`. Access solution values via `expr.value` or `sol.get_value(expr)`.
 
---
 
## Decision Variable Types
 
| Method | Type | Domain |
|---|---|---|
| `m.bool()` | Boolean | {0, 1} |
| `m.int(lb, ub)` | Integer | [lb, ub] |
| `m.float(lb, ub)` | Float | [lb, ub] |
| `m.list(n)` | List (ordered sequence) | permutation of subset of {0..n-1} |
| `m.set(n)` | Set (unordered selection) | subset of {0..n-1} |
| `m.interval_var(min_start, max_end)` | Interval | [start, end] with duration |
 
---
 
## 1. List Variables (Routing & Sequencing)
 
A **list variable** represents an ordered sequence — ideal for vehicle routes, job sequences, TSP.
 
```python
n_customers = 10
 
with hexaly.optimizer.HexalyOptimizer() as optimizer:
    m = optimizer.model
 
    # One list variable per vehicle: ordered sequence of customer indices
    routes = [m.list(n_customers) for _ in range(n_vehicles)]
 
    # Each customer visited exactly once across all routes
    m.constraint(m.partition(routes))   # covers all, no overlap
 
    # Distance array: dist[i][j] = cost from i to j
    dist_array = m.array(dist_matrix)   # wrap numpy/list matrix
 
    for route in routes:
        # Lambda over list: sum of travel costs
        route_cost = m.sum(route, m.lambda_function(
            lambda i: m.at(dist_array, route[i - 1], route[i])
        ))
        # m.count(route) gives number of customers on this route
 
    m.minimize(total_cost)
    m.close()
    optimizer.param.time_limit = 30
    optimizer.solve()
 
    for k, route in enumerate(routes):
        sol_route = optimizer.solution.get_collection_value(route)
        print(f"Vehicle {k}: {list(sol_route)}")
```
 
**List-specific operators:**
 
| Expression | Meaning |
|---|---|
| `m.count(lst)` | Number of elements |
| `m.contains(lst, i)` | Boolean: i in list |
| `m.index_of(lst, i)` | Position of element i |
| `m.partition(lists)` | All lists partition {0..n-1} |
| `m.cover(lists)` | All lists cover {0..n-1} (allows overlap) |
| `lst[i]` | Element at position i (0-indexed) |
 
---
 
## 2. Array Expressions (Data Lookup)
 
Arrays allow efficient multidimensional data access inside model expressions.
 
```python
# 1D array from Python list or numpy array
costs = m.array([10, 20, 15, 30])       # m.at(costs, i) → costs[i]
 
# 2D array (distance matrix)
dist = m.array([[0,5,9],[5,0,3],[9,3,0]])
# Access: m.at(dist, i, j)
 
# Using numpy
import numpy as np
matrix = np.array([[1,2],[3,4]], dtype=float)
arr = m.array(matrix)
# m.at(arr, row_expr, col_expr)
```
 
**Important:** Always use `m.at(array_expr, idx...)` to index arrays inside model expressions. Python `[]` indexing only works at model-build time with constants.
 
---
 
## 3. Interval Variables (Scheduling)
 
Interval variables model tasks with start time, end time, and duration.
 
```python
horizon = 100  # scheduling horizon
 
with hexaly.optimizer.HexalyOptimizer() as optimizer:
    m = optimizer.model
 
    # Optional interval: task may or may not be scheduled
    task = m.interval_var(0, horizon)
    task.duration_min = 5
    task.duration_max = 10
 
    # Fixed-duration task
    job = m.interval_var(0, horizon)
    job.duration_min = job.duration_max = 8
 
    # Precedence: job ends before task starts
    m.constraint(m.end(job) <= m.start(task))
 
    # No overlap between two tasks (disjunctive)
    m.constraint(m.no_overlap(job, task))
 
    # Resource capacity: cumulative resource ≤ capacity
    # demand[i] is resource usage of task i
    usage = m.sum([m.pulse(tasks[i], demands[i]) for i in range(n)])
    m.constraint(usage <= capacity)
 
    m.minimize(m.max([m.end(t) for t in all_tasks]))  # makespan
    m.close()
    optimizer.solve()
 
    sol = optimizer.solution
    print(m.start(job).value, m.end(job).value)
    # For optional intervals: check if_present(task).value
```
 
**Scheduling operators:**
 
| Expression | Meaning |
|---|---|
| `m.start(iv)` | Start time of interval |
| `m.end(iv)` | End time of interval |
| `m.length(iv)` | Duration of interval |
| `m.if_present(iv)` | Bool: is interval scheduled |
| `m.no_overlap(iv1, iv2)` | Non-overlap constraint |
| `m.overlap_length(iv1, iv2)` | Overlap amount |
| `m.pulse(iv, height)` | Cumulative resource usage |
 
---
 
## 4. Set Variables (Selection Problems)
 
Set variables represent unordered subsets — for selection, grouping, and assignment.
 
```python
# Set variable over {0..n-1}
selected = m.set(n_items)
 
# Operators on sets
size = m.count(selected)
contains_item = m.contains(selected, item_idx)
 
# Partition: sets cover all items with no overlap
m.constraint(m.partition([set1, set2, set3]))
 
# Cover: sets cover all items (overlap allowed)
m.constraint(m.cover([set1, set2]))
 
# Lambda sum over a set
total_weight = m.sum(selected, m.lambda_function(
    lambda i: m.at(weights_array, i)
))
m.constraint(total_weight <= capacity)
```
 
---
 
## 5. Lambda / Functional Expressions
 
Lambda functions enable compact, vectorized constraints and objectives.
 
```python
# Lambda over a range
total = m.sum(m.range(0, n), m.lambda_function(
    lambda i: m.at(costs, i) * m.bool()   # example
))
 
# Lambda over a list variable
route = m.list(n)
travel = m.sum(route, m.lambda_function(
    lambda i: m.at(dist, route[i - 1], route[i])
    # Note: route[i-1] gives previous element; wraps around at index 0
))
 
# Filter (conditional sum)
penalty = m.sum(
    m.range(0, n),
    m.lambda_function(lambda i: m.iif(condition[i], penalty_cost, 0))
)
 
# m.lambda_function shorthand: m.lambd
f = m.lambd(lambda i: m.at(arr, i) * 2)
```
 
**Available functional operators:**
 
| Method | Description |
|---|---|
| `m.sum(iterable, lambda)` | Sum of lambda applied to iterable |
| `m.max(iterable, lambda)` | Maximum |
| `m.min(iterable, lambda)` | Minimum |
| `m.and_(iterable, lambda)` | Logical AND |
| `m.or_(iterable, lambda)` | Logical OR |
| `m.count(iterable, lambda)` | Count where lambda is true |
| `m.lambda_function(fn)` / `m.lambd(fn)` | Create a lambda expression |
 
---
 
## 6. Constraints & Objectives
 
```python
# Constraints
m.constraint(expr)              # expr must be true / == 1
 
# Objectives (can declare multiple — lexicographic order)
m.minimize(expr)
m.maximize(expr)
 
# Conditional (ternary)
val = m.iif(condition, true_val, false_val)
 
# Arithmetic
m.abs(x), m.sqrt(x), m.pow(x, 2), m.log(x), m.exp(x)
m.mod(x, y), m.floor(x), m.ceil(x), m.round(x)
 
# Logical
m.and_(a, b), m.or_(a, b), m.not_(a)
 
# Comparison operators work directly: x <= y, x == y, x != y
```
 
---
 
## 7. Multi-Objective & Phases
 
```python
# Multiple objectives: optimized in declaration order (lexicographic)
m.minimize(primary_cost)
m.minimize(secondary_cost)
m.close()
 
# Phases: control time per objective
phase1 = optimizer.create_phase()
phase1.time_limit = 10          # 10s for primary objective
phase2 = optimizer.create_phase()
phase2.time_limit = 20          # 20s for secondary
 
optimizer.solve()
```
 
---
 
## 8. Parameters & Solution Retrieval
 
```python
# Time and iteration limits
optimizer.param.time_limit = 30          # seconds
optimizer.param.iteration_limit = 100000
optimizer.param.nb_threads = 4
optimizer.param.seed = 42               # reproducibility
optimizer.param.verbosity = 1           # 0=silent, 1=normal, 2=detailed
 
# Solution status
sol = optimizer.solution
# sol.status: OPTIMAL, FEASIBLE, INFEASIBLE, INCONSISTENT
 
# Scalar value
val = my_expr.value                     # shortcut
val = sol.get_value(my_expr)            # equivalent
 
# Collection value (list/set variable)
route_vals = sol.get_collection_value(route_var)
items = list(route_vals)                # Python list of ints
 
# Interval value
iv_start = sol.get_interval_value(task).start
iv_end   = sol.get_interval_value(task).end
```
 
---
 
## 9. Setting an Initial Solution
 
```python
# Warm-start with a known feasible solution
m.close()
sol = optimizer.solution
 
# Set scalar decision values
sol.set_value(x, 1)
sol.set_value(y, 42)
 
# Set list variable values
sol.set_collection_value(route, [3, 1, 4, 2])
 
# Set interval variable
sol.set_interval_value(task, HxInterval(start=5, end=13))
 
optimizer.solve()   # solver starts from this solution
```
 
---
 
## 10. Common Patterns by Problem Type
 
### TSP / VRP
```python
route = m.list(n)                          # single tour
m.constraint(m.count(route) == n)          # must visit all
dist_arr = m.array(distance_matrix)
travel = m.sum(route, m.lambd(
    lambda i: m.at(dist_arr, route[i-1], route[i])
))
m.minimize(travel)
```
 
### Bin Packing / Knapsack
```python
# items selected into knapsack
selected = m.set(n_items)
weight = m.sum(selected, m.lambd(lambda i: m.at(weights, i)))
value  = m.sum(selected, m.lambd(lambda i: m.at(values, i)))
m.constraint(weight <= capacity)
m.maximize(value)
```
 
### Job Scheduling (Makespan)
```python
jobs = [m.interval_var(0, horizon) for _ in range(n)]
for j in jobs:
    j.duration_min = j.duration_max = processing_times[j_idx]
# Precedence
m.constraint(m.end(jobs[i]) <= m.start(jobs[j]))
# Minimize makespan
m.minimize(m.max([m.end(j) for j in jobs]))
```
 
### Assignment
```python
# x[i][j] = 1 if worker i assigned to job j
x = [[m.bool() for j in range(n_jobs)] for i in range(n_workers)]
for j in range(n_jobs):
    m.constraint(m.sum([x[i][j] for i in range(n_workers)]) == 1)
for i in range(n_workers):
    m.constraint(m.sum([x[i][j] for j in range(n_jobs)]) <= 1)
total_cost = m.sum([cost[i][j] * x[i][j] for i in range(n_workers) for j in range(n_jobs)])
m.minimize(total_cost)
```
 
---
 
## 11. Piecewise & Nonlinear Expressions
 
```python
# Piecewise linear function via step array
# StepArray: for x in [X[k], X[k+1]), value is Y[k]
X = m.array([0, 10, 20, 30])
Y = m.array([5, 15, 10, 25])
pw = m.step_array(X, Y)
result = m.at(pw, continuous_var)   # piecewise lookup
 
# Nonlinear: use standard math operators
expr = m.sqrt(x**2 + y**2)
expr = m.log(demand) * unit_cost
```
 
---
 
## 12. Callbacks (Solution Events)
 
```python
def on_new_solution(optimizer):
    sol = optimizer.solution
    print(f"New solution: obj={my_obj.value:.4f}")
 
optimizer.add_callback(
    hexaly.optimizer.HxCallbackType.SOLUTION_FOUND,
    on_new_solution
)
optimizer.solve()
```
 
---
 
## 13. Debugging Tips
 
1. **INFEASIBLE immediately?** Add constraints one-by-one. Comment all out and add back.
2. **Check `m.count(list_var)` constraints** — did you forget to constrain list size?
3. **Array index out of bounds?** Hexaly silently returns 0 — check data dimensions.
4. **`m.partition` vs `m.cover`**: partition = no overlap; cover = may overlap.
5. **Lambda argument is an index**, not an element value. Use `m.at(arr, i)` to get element.
6. **Multi-objective**: objectives optimized in strict declaration order; add a phase per objective if you want to control time allocation.
7. **`m.close()` required** before `solve()` — forgetting it raises an error.
8. **Reproducibility**: set `optimizer.param.seed = 0` for deterministic results.
---
 
## References
 
- Full Python API: https://www.hexaly.com/docs/last/pythonapi/optimizer/index.html
- Modeling features: https://www.hexaly.com/docs/last/modelingfeatures/index.html
- Routing guide: https://www.hexaly.com/docs/last/modelingfeatures/routing.html
- Code templates: https://www.hexaly.com/templates
For detailed operator tables and advanced topics, see `references/operators.md` and `references/scheduling.md` in this skill folder.
 
