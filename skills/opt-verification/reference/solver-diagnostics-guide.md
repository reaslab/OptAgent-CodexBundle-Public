# Solver Diagnostics Guide

## LP Relaxation Gap Interpretation

The LP relaxation gap measures formulation quality:

$$\text{Gap} = \frac{|z_{IP} - z_{LP}|}{|z_{IP}|} \times 100\%$$

| Gap Range | Quality | Action |
|---|---|---|
| 0% | Perfect (TU or naturally integer) | No action needed |
| < 1% | Excellent | Standard solve |
| 1-5% | Good | Monitor solve time |
| 5-15% | Acceptable | Consider adding cuts or tightening |
| 15-30% | Weak | Reformulate: tighten Big-M, add valid inequalities |
| > 30% | Very weak | Major reformulation needed; consider alternative model |

**Tightening techniques:** See gap-analysis-guide.md in opt-solver.

## MIP Gap During Solving

The MIP gap reported during branch-and-bound:

$$\text{MIPGap} = \frac{|z_{\text{incumbent}} - z_{\text{best bound}}|}{|z_{\text{incumbent}}|} \times 100\%$$

| Behavior | Diagnosis |
|---|---|
| Gap decreases steadily | Normal solving progress |
| Gap stuck at large value | Weak formulation or hard instance; try cuts, heuristics |
| Gap drops to 0 quickly | Easy instance or strong formulation |
| No incumbent found | Feasibility problem; check for infeasibility or increase heuristic effort |
| Bound improves but no new incumbent | Branching is pruning; solution may be near-optimal |

**Parameters to adjust:**
- `MIPGap` / `MIPGapAbs`: acceptable termination gap
- `Heuristics`: increase heuristic frequency to find incumbents faster
- `Cuts`: increase aggressiveness of cutting planes

## Solver Status Codes (Summary)

See `opt-solver/reference/status-code-reference.md` for full tables.

| Status | Meaning | Action |
|---|---|---|
| Optimal | Proven optimal within gap tolerance | Report solution |
| Infeasible | No feasible solution exists | Run IIS; check constraints |
| Unbounded | Objective can improve without limit | Check missing constraints, variable bounds |
| InfOrUnbd | Cannot distinguish | Add variable bounds; re-solve |
| Time Limit (with solution) | Best solution found but not proven optimal | Report with gap; extend time or tighten |
| Time Limit (no solution) | No feasible solution found in time | Increase time; check feasibility |
| Numerical | Numerical issues encountered | Scale model; use NumericFocus |

## Sensitivity Analysis Interpretation

### Reduced Costs (for non-basic variables)

| Reduced Cost | Meaning |
|---|---|
| > 0 (minimization) | Variable would increase objective if forced into basis |
| = 0 | Variable is at breakpoint; alternative optima may exist |
| Large magnitude | Variable is far from being attractive to enter basis |

### Shadow Prices (Dual Values)

| Sign (minimization) | Constraint Type | Meaning |
|---|---|---|
| Positive | <= constraint | Relaxing RHS by 1 unit decreases objective by this amount |
| Negative | >= constraint | Relaxing RHS by 1 unit decreases objective by this amount |
| Zero | Any | Constraint is non-binding (has slack) |

### Ranges

- **Objective coefficient range:** Range over which the current basis remains optimal.
- **RHS range:** Range over which the current basis (shadow prices) remain valid.
- Narrow ranges indicate sensitivity; report to user for data validation focus.

## Common Diagnostic Workflows

### Slow Solve
1. Check LP relaxation gap (weak formulation?)
2. Check for symmetry (identical bins, vehicles, machines)
3. Try solver emphasis: `MIPFocus=1` (feasibility) or `MIPFocus=2` (optimality)
4. Add cuts: `Cuts=2` (aggressive)
5. Reduce Big-M values

### Infeasible Model
1. Compute IIS (`model.computeIIS()` in Gurobi)
2. Review IIS constraints against problem statement
3. Try elastic relaxation to find which constraints conflict
4. Check data for errors (supply < demand, etc.)

### Numerical Warnings
1. Check coefficient range: `model.printStats()` in Gurobi
2. Scale data to [1e-4, 1e+4]
3. Set `NumericFocus=1` (or 2, 3)
4. Avoid very small and very large coefficients in same constraint
