# LP Relaxation Gap Analysis Guide

## Gap Definition

$$\text{LP Gap} = \frac{|z_{IP}^* - z_{LP}^*|}{|z_{IP}^*|} \times 100\%$$

For minimization: $z_{LP}^* \leq z_{IP}^*$ (LP relaxation provides a lower bound).

## Progressive Gap Strategy

### Phase 1: Initial Assessment (target: gap < 50%)

1. Solve LP relaxation
2. If gap > 50%, the formulation is fundamentally weak
3. Actions:
   - Check for excessively large Big-M values; tighten to data-derived bounds
   - Verify correct formulation class (e.g., DFJ vs MTZ for TSP)
   - Consider reformulation (e.g., network flow instead of generic MIP)

### Phase 2: Tightening (target: gap < 10%)

1. Add valid inequalities / cutting planes
2. Actions:
   - Cover inequalities for set covering/packing
   - Flow cover cuts for fixed-charge problems
   - Gomory cuts (solver-generated, increase `Cuts` parameter)
   - Clique cuts for conflict graphs
   - Lift-and-project cuts

### Phase 3: Fine-tuning (target: gap < 1%)

1. Formulation polishing
2. Actions:
   - Disaggregate constraints (per-customer linking vs aggregate)
   - Add symmetry-breaking constraints
   - Use extended formulations (more variables, tighter polytope)
   - Variable fixing from reduced costs

## Formulation Tightening Techniques

| Technique | When to Use | Expected Impact |
|---|---|---|
| Tighten Big-M | Big-M >> necessary value | Major (can cut gap 50%+) |
| Disaggregate linking | $\sum x_{ij} \leq M y_j$ → $x_{ij} \leq y_j$ | Significant |
| Add cover inequalities | Set covering/packing | Moderate |
| Symmetry breaking | Identical resources (bins, vehicles) | Moderate (reduces tree) |
| Clique constraints | Mutual exclusion structure | Moderate |
| Extended formulation | Weak natural formulation | Major (but more variables) |
| Variable bound tightening | Loose variable bounds | Moderate |

## Diagnostic Questions

1. **Which variables are fractional in LP?** These indicate where integrality is hardest to enforce.
2. **Which constraints are binding?** Binding constraints define the LP optimum; tightening these matters most.
3. **Are Big-M constraints active?** If slack on Big-M constraints is near M, the value is too large.
4. **Is there symmetry?** Multiple equivalent solutions at LP level indicate symmetry.
