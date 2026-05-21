# Hexaly Optimizer: Operators Reference

## All HxModel Decision Variable Constructors

| Method | Returns | Notes |
|---|---|---|
| `m.bool()` | Boolean HxExpression | Domain {0,1} |
| `m.int(lb, ub)` | Integer HxExpression | Domain [lb, ub] |
| `m.float(lb, ub)` | Float HxExpression | Domain [lb, ub] |
| `m.list(n)` | List HxExpression | Ordered sequence, subset of {0..n-1} |
| `m.set(n)` | Set HxExpression | Unordered subset of {0..n-1} |
| `m.interval_var(min_start, max_end)` | Interval HxExpression | Scheduling task |

---

## Arithmetic Operators

| Operator / Method | Description |
|---|---|
| `a + b`, `a - b`, `a * b`, `a / b` | Standard arithmetic |
| `a ** b` or `m.pow(a, b)` | Power |
| `m.abs(a)` | Absolute value |
| `m.sqrt(a)` | Square root |
| `m.log(a)` | Natural logarithm |
| `m.exp(a)` | Exponential |
| `m.floor(a)` | Floor |
| `m.ceil(a)` | Ceiling |
| `m.round(a)` | Round to nearest integer |
| `m.mod(a, b)` | Modulo |
| `m.min(a, b)` or `m.min([exprs])` | Minimum |
| `m.max(a, b)` or `m.max([exprs])` | Maximum |
| `m.sum([exprs])` | Sum of a list of expressions |

---

## Logical & Comparison Operators

| Operator / Method | Description |
|---|---|
| `a <= b`, `a >= b`, `a < b`, `a > b` | Comparisons (return HxExpression) |
| `a == b`, `a != b` | Equality/inequality |
| `m.and_(a, b)` | Logical AND |
| `m.or_(a, b)` | Logical OR |
| `m.not_(a)` | Logical NOT |
| `m.iif(cond, a, b)` | Ternary: if cond then a else b |

---

## Array Operators

| Method | Description |
|---|---|
| `m.array(data)` | Create constant array from Python list/numpy array |
| `m.at(arr, i)` | 1D array access: arr[i] |
| `m.at(arr, i, j)` | 2D array access: arr[i][j] |
| `m.at(arr, i, j, k)` | 3D array access |
| `m.step_array(X, Y)` | Piecewise step function array |

**Note:** `data` can be a flat list, list-of-lists, or numpy ndarray. Hexaly flattens and tracks shape internally.

---

## Collection (List / Set) Operators

### Applicable to both list and set variables:

| Method | Description |
|---|---|
| `m.count(coll)` | Number of elements |
| `m.contains(coll, i)` | Boolean: element i is in collection |
| `m.sum(coll, lambda)` | Sum of lambda(i) for i in coll |
| `m.max(coll, lambda)` | Max of lambda(i) for i in coll |
| `m.min(coll, lambda)` | Min of lambda(i) for i in coll |
| `m.and_(coll, lambda)` | AND of lambda(i) for i in coll |
| `m.or_(coll, lambda)` | OR of lambda(i) for i in coll |
| `m.partition(list_of_colls)` | All elements covered exactly once |
| `m.cover(list_of_colls)` | All elements covered at least once |
| `m.disjoint(list_of_colls)` | Collections are pairwise disjoint |

### List-only operators:

| Method | Description |
|---|---|
| `lst[i]` | Element at position i (model-time constant i only) |
| `m.index_of(lst, elem)` | Position of element (or -1) |
| `m.first(lst)` | First element of list |
| `m.last(lst)` | Last element of list |
| `m.prev(lst, i, default)` | Element before i in list |
| `m.next(lst, i, default)` | Element after i in list |
| `m.distinct(lst)` | Constraint: all elements distinct |

---

## Lambda / Functional Expressions

```python
# Create a lambda (m.lambda_function and m.lambd are identical)
f = m.lambda_function(lambda i: m.at(costs, i))
f = m.lambd(lambda i: m.at(costs, i))

# Multi-argument lambda
g = m.lambd(lambda i, j: m.at(matrix, i, j))

# Use in sum/max/min/count
total = m.sum(m.range(0, n), f)
total = m.sum(my_list_var, f)
total = m.sum(my_set_var, f)

# Range object
r = m.range(0, n)    # integers 0, 1, ..., n-1
```

**Lambda gotcha:** The lambda receives an *index* or *element identifier* (an HxExpression representing the loop variable), not a Python integer. Use `m.at(arr, i)` — do not try to use Python subscript with `i` directly.

---

## Interval Variable Operators

| Method | Description |
|---|---|
| `m.start(iv)` | Start time |
| `m.end(iv)` | End time |
| `m.length(iv)` | Duration (end - start) |
| `m.if_present(iv)` | Bool: interval is scheduled (optional intervals) |
| `m.overlap_length(iv1, iv2)` | Overlap between two intervals |
| `m.no_overlap(iv1, iv2)` | Non-overlap constraint |
| `m.no_overlap(list_of_iv)` | All intervals non-overlapping |
| `m.pulse(iv, height)` | Cumulative resource: add `height` during iv |
| `m.step_at(t, height)` | Cumulative: step function at time t |
| `m.step_at_start(iv, height)` | Add height at start of iv |
| `m.step_at_end(iv, height)` | Add height at end of iv |

### Interval Variable Attributes (set before m.close()):

```python
iv = m.interval_var(0, horizon)
iv.duration_min = 5          # minimum duration
iv.duration_max = 10         # maximum duration (fixed if ==)
iv.intensity_function = ...  # optional: intensity array
iv.is_optional = True        # interval may not be scheduled
```

---

## Model-Level Methods

```python
m.constraint(expr)          # add a constraint (expr == 1 or True)
m.minimize(expr)            # add minimization objective
m.maximize(expr)            # add maximization objective
m.close()                   # freeze model, required before solve

m.nb_expressions            # number of expressions in model
m.nb_objectives             # number of objectives
m.nb_constraints            # number of constraints
```

---

## HxParam Attributes

```python
p = optimizer.param
p.time_limit        # int, seconds (default: no limit)
p.iteration_limit   # int (default: no limit)
p.nb_threads        # int (default: auto)
p.seed              # int (default: 0)
p.verbosity         # 0=silent, 1=normal, 2=detailed (default: 1)
p.log_file          # str: path to write log
p.obj_threshold     # stop when objective ≤ threshold
```

---

## HxSolution Methods

```python
sol = optimizer.solution

sol.status           # HxSolutionStatus enum
sol.get_value(expr)                    # scalar (int/float/bool)
sol.set_value(expr, value)             # warm-start
sol.get_collection_value(coll_expr)    # HxCollection (iterable)
sol.set_collection_value(coll_expr, iterable)
sol.get_interval_value(iv_expr)        # HxInterval
sol.set_interval_value(iv_expr, HxInterval(start, end))

# Check status
from hexaly.optimizer import HxSolutionStatus
if sol.status == HxSolutionStatus.OPTIMAL: ...
if sol.status == HxSolutionStatus.FEASIBLE: ...
```