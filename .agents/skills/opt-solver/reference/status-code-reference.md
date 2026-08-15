# Solver Status Code Reference

## Gurobi

| Code | Name | Meaning | Action |
|---|---|---|---|
| 1 | LOADED | Model loaded, not solved | Call optimize() |
| 2 | OPTIMAL | Optimal solution found | Extract solution |
| 3 | INFEASIBLE | Model is infeasible | Compute IIS: `model.computeIIS()` |
| 4 | INF_OR_UNBD | Infeasible or unbounded | Set `DualReductions=0`, re-solve to distinguish |
| 5 | UNBOUNDED | Model is unbounded | Add missing constraints or variable bounds |
| 6 | CUTOFF | Objective worse than cutoff | Relax Cutoff parameter |
| 7 | ITERATION_LIMIT | Iteration limit reached | Increase IterationLimit |
| 8 | NODE_LIMIT | Node limit reached | Increase NodeLimit; check gap |
| 9 | TIME_LIMIT | Time limit reached | Check if incumbent exists; extend TimeLimit |
| 10 | SOLUTION_LIMIT | Solution limit reached | Extract best solution; continue if needed |
| 11 | INTERRUPTED | User interrupt | Resume or extract current best |
| 12 | NUMERIC | Numerical difficulties | Set NumericFocus=1-3; scale model |
| 13 | SUBOPTIMAL | Solution available but suboptimal | Check tolerances; may be acceptable |
| 15 | IN_PROGRESS | Optimization in progress | Wait |
| 16 | USER_OBJ_LIMIT | Objective reached user limit | Extract solution |

## CBC / PuLP

| Status | PuLP Constant | Meaning | Action |
|---|---|---|---|
| 0 | `LpStatusNotSolved` | Not solved | Call solve() |
| 1 | `LpStatusOptimal` | Optimal | Extract solution |
| -1 | `LpStatusInfeasible` | Infeasible | Check constraints; relax bounds |
| -2 | `LpStatusUnbounded` | Unbounded | Add constraints/bounds |
| -3 | `LpStatusUndefined` | Error or other | Check model; increase time limit |

CBC log caveat: for PuLP/CBC, always inspect the native solver log in addition to `pulp.LpStatus`. If CBC reports `Result - Stopped on time limit` with an incumbent/objective, normalize to `status="feasible"` and `termination_condition="time_limit"`, and make `raw_status` mention the CBC time-limit evidence instead of a bare `Optimal`; if no incumbent exists, normalize to `status="timeout"`. Only report `status="optimal"` when the CBC log contains an explicit optimality marker.

## HiGHS

| Code | Name | Meaning | Action |
|---|---|---|---|
| 7 | OPTIMAL | Optimal solution found | Extract solution |
| 8 | INFEASIBLE | Infeasible | Use `getInfoValue("primal_infeasibility")` |
| 9 | UNBOUNDED_OR_INFEASIBLE | Cannot distinguish | Add bounds; re-solve |
| 10 | UNBOUNDED | Unbounded | Add constraints |
| 11 | OBJECTIVE_BOUND | Objective reached limit | Extract solution |
| 12 | OBJECTIVE_TARGET | Target reached | Extract solution |
| 13 | TIME_LIMIT | Time limit | Check for incumbent |
| 14 | ITERATION_LIMIT | Iteration limit | Increase limit |
| 15 | SOLUTION_LIMIT | Solution limit | Extract best |

## SCIP

| Code | Name | Meaning | Action |
|---|---|---|---|
| OPTIMAL | Optimal | Proven optimal | Extract solution |
| INFEASIBLE | Infeasible | No feasible solution | Analyze conflict constraints |
| UNBOUNDED | Unbounded | Unbounded | Add bounds |
| INFORUNBD | Infeasible or unbounded | Cannot distinguish | Add bounds; re-solve |
| TIMELIMIT | Time limit | Time expired | Check incumbent; extend |
| MEMLIMIT | Memory limit | Out of memory | Reduce model; increase memory |
| GAPLIMIT | Gap limit | MIP gap reached | Extract solution (gap-optimal) |
| SOLLIMIT | Solution limit | Enough solutions found | Extract best |

## OR-Tools CP-SAT

| Code | Name | Meaning | Action |
|---|---|---|---|
| 0 | UNKNOWN | Not solved or no status | Check model validity |
| 1 | MODEL_INVALID | Model is invalid | Fix model definition errors |
| 2 | FEASIBLE | Feasible but not proven optimal | Extract solution; extend time for proof |
| 3 | INFEASIBLE | Infeasible | Remove/relax constraints |
| 4 | OPTIMAL | Optimal | Extract solution |

## CVXPY

| Status | Meaning | Action |
|---|---|---|
| `optimal` | Optimal solution found | Extract values |
| `infeasible` | Problem infeasible | Check constraints |
| `unbounded` | Problem unbounded | Add constraints/bounds |
| `infeasible_inaccurate` | Likely infeasible (numerical) | Improve scaling; try different solver |
| `unbounded_inaccurate` | Likely unbounded (numerical) | Add bounds; try different solver |
| `optimal_inaccurate` | Near-optimal (numerical issues) | Accept with caution; improve scaling |
| `solver_error` | Solver failed | Try different solver backend |
