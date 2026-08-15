# Numerical Issues Guide

## Coefficient Range Best Practices

**Target range:** All non-zero coefficients in $[10^{-4},\ 10^{4}]$.

**Matrix range ratio:** $\frac{\max |a_{ij}|}{\min |a_{ij}|} < 10^{8}$. Smaller is better.

| Range Ratio | Risk Level | Action |
|---|---|---|
| < 1e6 | Low | No action needed |
| 1e6 - 1e8 | Medium | Consider scaling |
| 1e8 - 1e10 | High | Scale model; set NumericFocus |
| > 1e10 | Critical | Must scale; likely incorrect results otherwise |

## Scaling Techniques

### 1. Unit Conversion (Preferred)
Change units to bring coefficients closer together:
- Dollars → thousands of dollars (divide by 1000)
- Grams → kilograms (divide by 1000)
- Seconds → hours (divide by 3600)

### 2. Row/Column Scaling
- Divide each constraint by its largest coefficient
- Divide each variable column by a scaling factor
- Most solvers do this automatically, but manual scaling helps with extreme ranges

### 3. Reformulation
- Replace $x$ with $x' = x / S$ where $S$ is a scaling factor
- Update all coefficients and bounds accordingly
- Document the transformation for solution interpretation

## Solver Parameters for Numerical Focus

### Gurobi
| Parameter | Values | Effect |
|---|---|---|
| `NumericFocus` | 0 (default), 1, 2, 3 | Higher = more careful numerics, slower |
| `FeasibilityTol` | 1e-6 (default) | Primal feasibility tolerance |
| `OptimalityTol` | 1e-6 (default) | Dual feasibility tolerance |
| `IntFeasTol` | 1e-5 (default) | Integrality tolerance |
| `BarConvTol` | 1e-8 (default) | Barrier convergence tolerance |

### HiGHS
| Parameter | Effect |
|---|---|
| `primal_feasibility_tolerance` | Default 1e-7 |
| `dual_feasibility_tolerance` | Default 1e-7 |
| `ipm_optimality_tolerance` | Default 1e-8 |

### CBC
| Parameter | Effect |
|---|---|
| `primalTolerance` | Default 1e-7 |
| `dualTolerance` | Default 1e-7 |

## Common Symptoms and Fixes

| Symptom | Likely Cause | Fix |
|---|---|---|
| Solver reports optimal but constraints violated | Coefficient range too wide | Scale model; increase NumericFocus |
| Different solvers give different answers | Numerical sensitivity | Scale; compare LP relaxations |
| Objective value oscillates during solve | Ill-conditioned basis matrix | Scale; try barrier method instead of simplex |
| Infeasible with loose tolerances, feasible with tight | Near-infeasible model | Check if model is truly feasible; small data changes |
| Very small reduced costs (1e-10) | Degeneracy | Usually harmless; increase tolerance if problematic |
| Warning: "unscaled primal infeasibility" | Post-scaling violation | Increase NumericFocus; manually scale worst constraints |
| Binary variable takes value 0.9999 | IntFeasTol issue | Reduce IntFeasTol or round in post-processing |

## Big-M and Numerical Issues

Big-M constraints are the most common source of numerical trouble:

1. **Problem:** Large M creates huge coefficient range within a single constraint
2. **Detection:** Check if any Big-M exceeds 1e4. If objective coefficients are O(1), M=1e6 creates ratio 1e6.
3. **Fix:** Compute M from data bounds, not arbitrary large numbers
4. **Alternative:** Use indicator constraints (Gurobi/CPLEX) which avoid Big-M entirely

## Pre-Solve Numerical Audit

Before solving, check:
1. Print coefficient statistics: `model.printStats()` (Gurobi)
2. Identify the largest and smallest non-zero coefficients
3. Identify constraints with the widest coefficient range
4. Check RHS values are in reasonable range
5. Check objective coefficients are in reasonable range
