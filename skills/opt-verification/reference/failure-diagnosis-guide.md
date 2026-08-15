# Failure Diagnosis Guide

## Evidence-to-Attribution Mapping

| Evidence | Likely Attribution | Confidence | Action |
|---|---|---|---|
| Solver returns infeasible | modeling_mismatch OR data_error | Medium | Run IIS analysis; check constraint conflicts |
| Solver returns unbounded | missing_constraint OR wrong_objective_direction | High | Check objective sign; look for missing bounds |
| Optimal but objective = 0 | missing_objective_terms OR all_vars_zero | High | Verify objective function has all terms |
| Optimal but trivial solution (all zeros) | missing_constraints OR phantom_feasibility | High | Check if constraints enforce activity |
| Solution violates problem requirement | missing_constraint | High | Map each requirement to a constraint |
| Variable takes unexpected value | wrong_domain OR wrong_coefficient | Medium | Check variable type and constraint coefficients |
| Very large/small objective value | coefficient_error OR unit_mismatch | Medium | Check parameter magnitudes and units |
| Solution changes dramatically with small data change | numerical_instability OR weak_formulation | Medium | Check coefficient ranges; add scaling |
| LP relaxation gap > 50% | weak_formulation | High | Tighten Big-M; add valid inequalities |
| Solver very slow (no progress) | symmetry OR weak_relaxation | Medium | Add symmetry-breaking; tighten formulation |

## Extended Evidence Patterns

| Evidence | Attribution | Confidence | Detail |
|---|---|---|---|
| All binary vars = 0 | missing_forcing_constraint | High | No constraint forces any variable to 1; likely missing demand/coverage constraint |
| Fractional binaries in IP solution | wrong_variable_type | High | Variable declared continuous instead of binary |
| Objective = sum of all possible costs | all_vars_active | High | Missing capacity or budget constraint |
| Negative flow/quantity | missing_nonnegativity | High | Variable lower bound not set to 0 |
| Solution uses only 1 of N resources | missing_balance OR overconstrained | Medium | Check if assignments are forced or constraints too tight |
| Makespan = 0 | missing_linking_to_Cmax | High | C_max not linked to completion times |
| Vehicle visits customer but doesn't return to depot | missing_depot_return | High | Incomplete route constraints |
| Two jobs overlap on same machine | missing_disjunctive | High | Disjunctive constraint missing or wrong Big-M |
| Shadow price on capacity = 0 but capacity is full | degeneracy | Low | Multiple bases; not necessarily an error |
| Solver finds optimal in < 1 second for "hard" problem | model_too_easy | Medium | May be missing constraints that make problem hard |

## Modeling Mismatch Patterns

| Pattern | Description | Detection Method |
|---|---|---|
| Missing linking constraint | Variable can be non-zero when it should be forced to zero | Check: can x_{ij} > 0 when y_j = 0? |
| Wrong constraint sense | <= instead of >=, or vice versa | Compare each constraint to problem statement |
| Double counting | Same cost/requirement counted in multiple constraints | Trace each term to its problem-statement source |
| Index mismatch | Constraint iterates over wrong set | Count constraints vs expected count |
| Phantom variable | Variable in model has no problem-statement meaning | Map every variable to a decision |
| Objective-constraint confusion | Soft objective treated as hard constraint or vice versa | Check if "minimize X" is modeled as "X <= 0" |
| Boundary condition error | Edge cases (empty sets, single elements) not handled | Test with minimal instance |
| Aggregation error | Constraint aggregates over wrong index | Sum over i instead of j, or vice versa |
| Off-by-one in indexing | Constraint range starts/ends wrong | Compare constraint count to expected |
| Missing Big-M activation | Big-M constraint always active or never active | Check y values vs constraint slack |
| Coefficient sign error | Positive instead of negative (or vice versa) | Compare each coefficient to problem statement |

## Confidence Level Guidelines

| Confidence | Meaning | When to Apply |
|---|---|---|
| **High** | Evidence strongly indicates this cause; fix with confidence | Clear structural issue (missing constraint, wrong sign) |
| **Medium** | Evidence is suggestive but not conclusive; verify before fixing | Could be multiple causes; need more evidence |
| **Low** | Possible cause among several; investigate further | Symptom is ambiguous; multiple explanations |

## Diagnostic Protocol

1. **Classify the failure:** infeasible, unbounded, wrong answer, slow convergence
2. **Collect evidence:** solver status, objective value, solution values, LP relaxation
3. **Match evidence** to the table above
4. **Identify top 2-3 hypotheses** ranked by confidence
5. **Test each hypothesis:**
   - For missing constraint: add it and re-solve
   - For wrong coefficient: fix and compare objective
   - For data error: validate input data independently
6. **Verify fix** doesn't introduce new issues
