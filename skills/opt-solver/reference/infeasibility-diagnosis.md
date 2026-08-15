# Infeasibility Diagnosis Guide

## Step-by-Step Diagnostic Protocol

### Step 1: Confirm Infeasibility

1. Re-solve with relaxed tolerances (`FeasibilityTol=1e-4`)
2. If now feasible: numerical issue, not true infeasibility
3. If still infeasible: proceed to IIS

### Step 2: Compute IIS (Irreducible Infeasible Subsystem)

**Gurobi:**
```python
model.computeIIS()
model.write("iis.ilp")
for c in model.getConstrs():
    if c.IISConstr:
        print(f"IIS constraint: {c.ConstrName}")
for v in model.getVars():
    if v.IISLB or v.IISUB:
        print(f"IIS bound: {v.VarName}")
```

**PuLP/CBC:** No native IIS. Use elastic relaxation instead.

**HiGHS:** Not yet natively supported. Use elastic relaxation.

### Step 3: Elastic Relaxation

Add slack/surplus variables to all constraints with penalty costs:

```python
# For each constraint g_i(x) <= b_i, add:
# g_i(x) <= b_i + s_i, with s_i >= 0, penalty M*s_i in objective
for c in constraints:
    slack = model.addVar(name=f"slack_{c.name}")
    model.addConstr(original_expr <= rhs + slack)
    obj += penalty * slack
```

Solve the elastic model. Non-zero slack variables reveal which constraints are violated and by how much.

### Step 4: Incremental Constraint Addition

1. Start with variable bounds only — solve (should be feasible)
2. Add constraint groups one at a time, solving after each
3. When infeasibility first appears, the last-added group conflicts with previous ones
4. Narrow within that group to find the specific conflicting constraint(s)

### Step 5: Root Cause Analysis

| IIS Finding | Likely Cause | Fix |
|---|---|---|
| Supply constraints + demand constraints | Total supply < total demand | Add dummy supply or reduce demand |
| Variable bounds + linking constraints | Bounds too tight for linking | Relax bounds or fix linking |
| Capacity + assignment constraints | Not enough capacity for all tasks | Add capacity or allow partial assignment |
| Time window + precedence | Schedule is impossible | Relax time windows or add resources |
| Two equality constraints on same variable | Conflicting fixed values | Check data for errors |

### Step 6: Common Infeasibility Patterns

1. **Unbalanced supply/demand:** Total supply < total demand. Fix: add dummy source or allow unmet demand.
2. **Over-constrained assignment:** More tasks than agents with capacity 1. Fix: allow multi-assignment or add agents.
3. **Conflicting bounds:** Lower bound > upper bound on some variable. Fix: check data.
4. **Circular precedence:** A before B before C before A. Fix: check precedence data for cycles.
5. **Empty feasible region:** Constraint intersection is empty. Fix: use elastic relaxation to find the tightest conflict.
