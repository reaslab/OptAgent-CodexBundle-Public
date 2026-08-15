# Problem-to-Model Audit Checklist

Use this checklist to verify that the mathematical formulation (opt_spec.json) faithfully represents the problem statement.

## Variable Completeness

- [ ] Every decision described in the problem has a corresponding variable
- [ ] No extra variables exist that don't map to a problem decision
- [ ] Variable domains (binary/integer/continuous) match the decision semantics
- [ ] Variables with TU structure use continuous domain (assignment, transportation, network flow)
- [ ] All index sets are correctly defined (right dimensions, no missing/extra indices)
- [ ] Variable bounds reflect physical/logical limits from the problem

## Constraint Coverage

- [ ] Every requirement in the problem statement maps to at least one constraint
- [ ] No problem requirement is left unmodeled
- [ ] Constraint direction is correct (<= for upper limits, >= for lower limits, = for exact)
- [ ] Range constraints use both upper and lower bounds where specified
- [ ] Non-negativity constraints are present for all non-negative quantities

## Phantom Constraint Detection

- [ ] No constraint exists that doesn't trace back to a problem requirement
- [ ] No constraint is added "for safety" without justification
- [ ] Redundant constraints are documented as tightening (not requirements)
- [ ] Symmetry-breaking constraints are labeled as such

## Variable Domain Semantics

- [ ] Binary variables represent true yes/no decisions
- [ ] Integer variables represent genuinely indivisible quantities
- [ ] Continuous variables for TU problems are justified by TU property
- [ ] Semi-continuous variables are used where min-production constraints exist
- [ ] No continuous variable is used for a discrete decision (without TU justification)

## Objective Alignment

- [ ] Objective direction (min/max) matches the problem goal
- [ ] All cost/profit/penalty terms from the problem appear in the objective
- [ ] No extra terms in objective that aren't in the problem
- [ ] Coefficient values match the problem data
- [ ] Multi-objective problems use correct weighting or lexicographic approach
- [ ] Units are consistent across all objective terms

## Index Set Correctness

- [ ] Set cardinalities match the problem data
- [ ] Constraints are generated over the correct index sets
- [ ] No off-by-one in index ranges
- [ ] Subset relationships are correctly modeled (e.g., "for all i in subset S" not "for all i")

## Unit Consistency

- [ ] All terms in each constraint have the same units
- [ ] All terms in the objective have the same units
- [ ] Parameter values match their stated units
- [ ] Conversion factors are applied where needed

## Modeling Decision Documentation

- [ ] Every assumption not explicit in the problem is documented
- [ ] Ambiguities are flagged with chosen resolution and justification
- [ ] Linearization choices are documented (Big-M values, auxiliary variables)
- [ ] Relaxation choices (if any) are documented with impact assessment
