# Reasonableness Checks for Optimization Solutions

## Conservation / Balance Checks

| Check | How | Red Flag |
|---|---|---|
| Flow conservation | For each node: inflow - outflow = supply/demand | Imbalance > 0 at non-source/sink nodes |
| Material balance | Total input = total output + waste/loss | Input != output (no loss modeled) |
| Demand satisfaction | Sum of allocations to customer >= demand | Any customer under-served |
| Supply utilization | Sum of shipments from source <= capacity | Any source over-capacity |
| Budget | Total cost <= budget parameter | Solution exceeds stated budget |
| Assignment completeness | Each agent/task assigned exactly once | Unassigned agents or tasks |

## Shadow Price (Dual Value) Checks

| Constraint Type | Expected Sign | Red Flag |
|---|---|---|
| Resource <= capacity (min cost) | >= 0 | Negative shadow price |
| Demand >= requirement (min cost) | >= 0 | Negative shadow price |
| Resource <= capacity (max profit) | >= 0 | Negative shadow price |
| Equality constraint | Any sign | Extremely large magnitude |
| Non-binding constraint | = 0 | Non-zero shadow price |

**Magnitude check:** Shadow price should be economically meaningful. A shadow price of $1M per unit of a $10 resource signals a modeling issue.

## LP Relaxation Gap

| Gap | Interpretation |
|---|---|
| 0% | LP relaxation is tight; integer solution = LP solution (TU or lucky) |
| < 1% | Excellent formulation |
| 1-5% | Good formulation |
| 5-20% | Acceptable; consider tightening (cuts, reformulation) |
| > 20% | Weak formulation; likely slow solve. Investigate reformulation. |
| > 50% | Very weak; Big-M too loose or wrong formulation class |

**Compute:** Gap = (IP_obj - LP_obj) / |IP_obj| * 100%

## Bound Tightness

- **Variable at bound:** If many variables are at their upper/lower bounds, the bounds may be too tight (over-constrained) or the problem may be degenerate.
- **Slack analysis:** Constraints with zero slack are binding. If critical constraints are not binding, check if they are correctly formulated.
- **Reduced costs:** Non-basic variables with zero reduced cost indicate degeneracy or alternative optima.

## Physical / Domain-Specific Limits

| Domain | Check |
|---|---|
| Transportation | Vehicle loads <= capacity; routes are connected; distances are non-negative |
| Scheduling | No time overlaps on same machine; makespan >= longest job; start >= 0 |
| Production | Production <= machine capacity * available time; inventory >= 0 |
| Network | Flows >= 0; flows <= arc capacities; conservation at nodes |
| Portfolio | Weights sum to 1; no short selling (if prohibited); variance > 0 |
| Blending | Proportions in [0,1]; sum of proportions = 1; nutrient bounds met |

## Solution Comparison Checks

- **Trivial solution:** Is the solution all-zeros or a single variable? May indicate missing constraints.
- **LP vs IP:** If LP relaxation = IP solution, verify TU property or check for modeling error that makes integrality constraints redundant.
- **Known bounds:** Compare against published lower/upper bounds for benchmark instances.
- **Sanity bounds:** Objective should be between (trivial lower bound, trivial upper bound). E.g., min cost >= 0, max profit <= sum of all revenues.

## Sensitivity Analysis Interpretation

| Finding | Implication |
|---|---|
| Objective coefficient range is wide | Solution robust to cost estimation errors |
| Objective coefficient range is narrow | Small data change alters optimal solution |
| RHS range is wide | Constraint not critical near optimum |
| RHS range is narrow | Constraint is critical; solution sensitive to this parameter |
| Multiple optima (reduced cost = 0) | Alternative solutions exist; report to user |
