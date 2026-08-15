# Model-to-Code Audit Checklist

Use this checklist to verify that the public solver entrypoint correctly implements the mathematical formulation in opt_spec.json.

## Variable Count Matching

- [ ] Number of variable groups in code matches opt_spec
- [ ] Index sets for each variable group match opt_spec dimensions
- [ ] Total number of variables created matches expected count from data
- [ ] Variable naming in code is traceable to opt_spec variable names

## Variable Type Matching

- [ ] Binary variables declared as BINARY (not INTEGER with bounds 0-1)
- [ ] Integer variables declared as INTEGER
- [ ] Continuous variables declared as CONTINUOUS
- [ ] Variable bounds (lb, ub) match opt_spec
- [ ] No accidental type mismatch (e.g., continuous instead of binary)

## Data Loading Correctness

- [ ] All parameters from opt_spec are loaded from the data source
- [ ] Parameter dimensions match opt_spec (scalar, vector, matrix)
- [ ] Default values for missing data are documented and reasonable
- [ ] Index mapping between data and model is correct (0-based vs 1-based)
- [ ] Data types are appropriate (int vs float)

## Constraint Count Matching

- [ ] Number of constraint groups in code matches opt_spec
- [ ] Each constraint group iterates over the correct index set
- [ ] Total constraint count matches expected: |index_set| per group
- [ ] Constraint sense (<=, >=, ==) matches opt_spec

## Constraint Implementation

- [ ] Each constraint's LHS terms match opt_spec formula
- [ ] Each constraint's RHS matches opt_spec
- [ ] Summation indices are correct (summing over right set)
- [ ] Coefficients match opt_spec parameters
- [ ] No missing terms in any constraint
- [ ] No extra terms not in opt_spec

## Objective Term Completeness

- [ ] Objective sense (minimize/maximize) matches opt_spec
- [ ] All objective terms from opt_spec appear in code
- [ ] No extra terms in code objective
- [ ] Coefficients on each term match opt_spec
- [ ] Summation indices in objective are correct

## Solver Configuration

- [ ] Solver selection is appropriate for problem type (LP/MIP/QP)
- [ ] Time limit is set
- [ ] MIP gap tolerance is set if applicable
- [ ] Solution extraction handles solver status correctly
