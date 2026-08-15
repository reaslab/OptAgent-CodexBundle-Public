---
name: result-validation
description: Use when solver outputs need explicit validation against feasibility, optimality, sensitivity, and consistency checks before being treated as reliable.
---

## Use when

Use this skill when a solver result appears promising and now needs structured checks before it is treated as reliable and reported to the user.

Typical signals:

- A solver reports "optimal" but special cases or constraint boundary behavior have not been checked.
- The solution may depend strongly on solver settings, time limits, or numerical tolerances.
- Multiple solve attempts appear consistent, but overclaiming risk remains.
- The next step is to decide whether the solution is trustworthy enough for the final report.

## Inputs

- The solution (`.optagent/cases/{case_id}/solver/solution.json`) and solver logs.
- The formulation (`.optagent/cases/{case_id}/formulation/opt_spec.json`) and case data manifest.
- The verification results (`.optagent/cases/{case_id}/verification/diagnosis.json`, `constraint_check.csv`).
- Any known anomalies, failed runs, or solver warnings.

## Outputs

- A validation record covering feasibility, optimality, sensitivity, and consistency checks.
- An explicit statement of which claims remain supported, weakened, contradicted, or unresolved.
- A completed `Solution Integrity Audit` checklist.

## Hard rules

- Check feasibility, optimality gap, constraint satisfaction, and consistency before endorsing a result.
- Search for counterexamples and contradictory evidence instead of only confirming the preferred interpretation.
- Complete the `Solution Integrity Audit` before handing work to the reporter.
- If validation exposes infeasibility, assumption drift, or unresolved contradictions, weaken the claim explicitly.

## Validation process

1. State the exact claim being validated (e.g., "The solution with objective 42.5 is globally optimal for the stated MILP").
2. Check **feasibility**: all variable domains satisfied, all constraints within tolerance, no bound violations.
3. Check **optimality**: MIP gap is acceptable, objective matches independent recomputation, no obvious improving direction.
4. Check **sensitivity**: does the solution change materially when time limit is doubled? When a constraint is relaxed by 1%? When a cost coefficient is perturbed?
5. Check **consistency**: does the solution make business sense? Are binding constraints expected to bind? Do variable values have reasonable magnitudes?
6. Search for **counterexamples**: can you find a feasible solution with a better objective by manual construction or heuristic?
7. Complete the audit checklist and state the solution reliability level.

## Solution Integrity Audit

Complete every item explicitly:

- `Overclaiming`: Is the solution reported as "optimal" when only "feasible" is proven (e.g., solver timed out with incumbent)?
- `Assumption drift`: Did the formulation assumptions drift from the original problem statement during solving?
- `Definition slippage`: Did any constraint, variable, or objective meaning change between formulation and implementation?
- `Tolerance dependence`: Does the result change if constraint tolerances or solver precision settings are tightened?
- `Missing checks`: Have all constraints been verified, or were some skipped due to complexity?

## Solution reliability levels

- `Tentative`: some evidence supports the solution, but validation is incomplete or mixed.
- `Verified within tested regime`: solution survives the main validation checks, with clear caveats about untested cases.
- `Strongly verified`: validation findings are positive across feasibility, optimality, sensitivity, and consistency.

## Handoff guidance

- Move back to solver (opt-solver) when validation shows implementation bugs, data loading errors, or missing constraints.
- Move back to formulator (opt-formulator) when failures point to model misspecification, hidden assumptions, or incorrect constraint structures.
- Move forward to reporter (opt-reporter) when the validation record is complete and the solution reliability level is established.
