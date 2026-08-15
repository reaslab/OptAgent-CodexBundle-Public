# Common Modeling Pitfalls

## General Pitfalls

| # | Pitfall | Symptom | Fix |
|---|---|---|---|
| G1 | Wrong objective direction | Unbounded or trivial solution | Verify: min cost / max profit |
| G2 | Wrong constraint direction | Infeasible or over-permissive | >= for lower bounds, <= for upper bounds |
| G3 | Missing non-negativity | Negative quantities in solution | Add x >= 0 explicitly |
| G4 | Missing variable upper bounds | Unbounded solution | Add natural upper bounds |
| G5 | Big-M too large | Weak LP relaxation, slow solve, numerical issues | Compute tightest M from data |
| G6 | Big-M too small | Cuts off feasible solutions | Validate: M >= max possible value of expression |
| G7 | Coefficient magnitude disparity | Numerical instability | Scale data so coefficients in [1e-4, 1e+4] |
| G8 | Symmetry in model | Exponential branching | Add symmetry-breaking constraints |
| G9 | Confusing index sets | Wrong constraint dimensions | Verify cardinality: |constraints| should match specification |
| G10 | Implicit assumption not documented | Incorrect model if assumption wrong | Document every assumption in opt_spec |
| G11 | Summing over wrong index | Constraint means something different | Verify: "for all i" in spec maps to outer loop in code |
| G12 | Missing linking constraint | Open facility / active route not enforced | Always add x_{ij} <= y_j or equivalent |
| G13 | Equality instead of inequality | Over-constrains the model | Use = only when exact balance is required |
| G14 | Forgetting to handle zero-demand cases | Division by zero or trivial infeasibility | Guard with if-statements or exclude zero-demand elements |
| G15 | Duplicate constraints | Wasted memory, no correctness impact | Deduplicate index sets |
| G16 | Soft constraint modeled as hard | Infeasible when real problem has penalties | Use slack variable with penalty in objective |
| G17 | Multi-objective without proper handling | Arbitrary tradeoff | Use weighted sum, lexicographic, or epsilon-constraint |
| G18 | Circular dependency in indices | Infeasible or incorrect precedence | Check DAG structure of precedence data |

## Per-Problem-Type Pitfalls

### Set Covering / Partitioning
- Covering uses >= 1, partitioning uses = 1. Mixing them up changes the problem entirely.
- Partitioning is more constrained; check feasibility first.
- Weighted set covering: forgetting to multiply by cost (using count instead).
- Not verifying that every element can be covered by at least one set.

### Bin Packing
- Bin symmetry: bins are identical, so permuting assignments gives equivalent solutions. Add ordering constraints (y_1 >= y_2 >= ...).
- Forgetting to link item assignment to bin opening: x_{ib} <= y_b.
- Not pre-checking: if any item weight > bin capacity, problem is infeasible.
- Using too many candidate bins (|B| = |I| is sufficient upper bound).

### Knapsack
- Bounded knapsack: need integer variables, not just binary.
- Multiple knapsack: forgetting each item can go in at most one knapsack.
- Maximization: forgetting this is max (not min). Solver default may differ.
- Negative weights or values in data: sanitize inputs.

### Assignment
- Unbalanced case (|I| != |J|): must add dummy agents or tasks.
- Using binary variables when TU gives integer LP solution for free.
- Rectangular assignment: must decide if partial assignment allowed.
- Costs of -infinity or +infinity to forbid assignments: use explicit exclusion constraints instead.

### Facility Location
- Omitting linking constraint x_{ij} <= y_j allows serving from closed facilities.
- Using sum_i x_{ij} <= M * y_j with M too loose instead of per-customer linking.
- Forgetting that CFLP capacity applies to total demand, not count of customers.
- Not including fixed cost in objective: only variable cost modeled.

### TSP
- Using MTZ subtour elimination: gives very weak LP relaxation.
- Forgetting subtour elimination entirely: solution will have disconnected cycles.
- Directed vs undirected: degree constraints differ (=2 undirected, =1 in/out directed).
- Asymmetric distances: must use directed formulation.
- Self-loops: ensure x_{ii} is excluded or fixed to 0.

### VRP
- Not enforcing depot return for each vehicle.
- Ignoring time windows when they exist (yields infeasible real-world routes).
- Capacity constraint on wrong index: must sum demands on each vehicle route.
- Not limiting number of vehicles: all vehicles may be used even when fewer suffice.
- Empty vehicle routes: add constraint to prevent trivially empty routes if fixed fleet.

### Job-Shop Scheduling
- Big-M in disjunctive constraints: use M = sum of processing times on that machine, not a generic large number.
- Forgetting precedence constraints within each job's operation sequence.
- Makespan variable not linked to all final operations.
- Zero processing times: disjunctive constraints become degenerate.

### Flow-Shop Scheduling
- Assuming permutation schedule is always optimal (only guaranteed for 2 machines).
- Missing no-wait or blocking constraints if present in the real problem.
- Not handling machine idle time correctly in completion time calculations.

### Network Flow
- Unbalanced supply/demand without slack/surplus node: infeasible.
- Anti-parallel arcs in undirected networks: must model both directions.
- Using integer variables unnecessarily: TU guarantees integrality.
- Negative cost cycles in min-cost flow: check if solver handles them or add constraints.
- Forgetting arc capacity lower bounds (some arcs may have minimum flow requirements).

### Transportation
- Total supply < total demand without dummy: infeasible.
- Mixing units between supply (tons) and demand (kg).
- Transshipment nodes: using transportation model when transshipment is needed (use min-cost flow instead).
- Not allowing partial shipments when problem permits them.

### Blending / Diet
- Mixing absolute amounts with proportional constraints.
- Missing normalization constraint (sum x_i = 1) when working with fractions.
- Nutrient coefficients in wrong units.
- Upper and lower nutrient bounds both active with inconsistent data: infeasible.
- Ingredient availability limits omitted.

### Portfolio
- Covariance matrix not positive semi-definite: solver rejects or gives wrong answer.
- Short selling: missing x_i >= 0 allows negative weights.
- Cardinality constraints need binary indicators; pure LP won't work.
- Using historical returns as expected returns without adjustment.
- Risk-free asset not modeled when it should be.

## Pitfall Severity Guide

| Severity | Consequence | Examples |
|---|---|---|
| **Critical** | Wrong answer (silent) | Wrong objective direction, missing constraint, wrong domain |
| **Major** | Infeasible or unbounded | Missing non-negativity, unbalanced supply/demand |
| **Performance** | Correct but slow | Big-M too large, symmetry, unnecessary integer vars |
| **Minor** | Suboptimal modeling | Missing tightening constraints, redundant variables |
