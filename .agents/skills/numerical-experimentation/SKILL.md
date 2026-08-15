---
name: numerical-experimentation
description: Use when optimization work needs reproducible solver experiments, parameter sweeps, algorithm comparisons, and retained failure records rather than a single solve attempt.
---

## Use when

Use this skill when the next stage is to run solver experiments, parameter sweeps, backend comparisons, or sensitivity analyses that should produce evidence robust enough for later reporting and validation.

Typical signals:

- A claim about solution quality depends on behavior across time limits, solver backends, or parameter settings.
- One solver run is not enough to justify the conclusion (e.g., comparing Gurobi vs. PuLP vs. OR-Tools).
- The work needs tables or metrics comparing multiple runs that will feed a later report.
- Failures, infeasibilities, or numerical warnings might be informative and should not be discarded.
- Solver parameter tuning is needed (presolve, cut strategies, branch priorities, MIP gap tolerance).

## Inputs

- The optimization model (`.optagent/cases/{case_id}/formulation/opt_spec.json`) and case data manifest.
- The hypothesis or question the experiments are meant to inform.
- Candidate solver backends, parameter ranges, time limits, and target metrics.
- Any known issues from prior solve attempts (infeasibility, timeout, numerical instability).

## Outputs

- A reproducible experiment record with solver settings, time limits, and run identifiers.
- A body of evidence covering representative runs, sweeps, comparisons, and failures.
- Tables and summary metrics prepared for later reporting and validation.
- A retained anomaly log covering failed runs, infeasible cases, and unexpected solver behavior.

## Hard rules

- Record the exact solver backend, version, parameters, time limit, and thread count for every reported run.
- Do not present one cherry-picked successful solve as the main evidence when richer comparison is needed.
- Retain anomalies: infeasible runs, timeout runs, numerical warnings, and unexpected statuses for later validation.
- Distinguish exploratory runs (trying different backends) from confirmatory runs (verifying a specific result).
- Keep outputs structured so tables and metrics can be reused by later reporting and validation steps.

## Experiment record requirements

- `Objective`: what question this run or batch addresses (e.g., "Does OR-Tools match Gurobi's solution within 1%?").
- `Solver configuration`: backend, version, solver parameters, time limit, thread count.
- `Model version`: which opt_spec.json and data manifest were used; any modifications from baseline.
- `Run outcomes`: optimal, feasible, infeasible, timeout, error, solver_unavailable.
- `Metrics`: objective value, MIP gap, solve time, variable count, constraint count.
- `Artifacts`: solution.json files, solver logs, public result tables, comparison tables.

## Evidence guidance

- Prefer parameter sweeps (e.g., time limit: 60s, 120s, 300s, 600s) over single fixed-parameter runs.
- Report both headline metrics (objective, gap, time) and failure diagnostics (infeasibility causes, timeout behavior).
- When comparing backends, use identical model and data to ensure fair comparison.
- If some runs fail or produce different solutions, record them alongside successful runs and note possible causes.
- Track solution stability: do different solver settings produce materially different solutions?

## Reporting handoff guidance

- Prepare comparison tables that make solver/parameter differences easy to inspect.
- Prepare summary metrics that aggregate across runs when aggregation is justified.
- Label which artifacts are suitable for the final report and which exist for debugging.
- Ensure every reported number traces back to a specific solver run with recorded parameters.

## Exit criteria

- Another reviewer can reproduce the reported evidence from the recorded solver settings and model version.
- The evidence set includes successes, failures, and anomalies relevant to the optimization claim.
- The produced artifacts are organized well enough to feed result-validation without rerunning experiments.

## Integration with analysis cases

When the Coordinator runs multi-backend comparisons or parameter sweeps as separate optimization cases, the experiment evidence from this skill feeds into an analysis case. The Coordinator creates the analysis case (type="analysis") and dispatches the reporter, which writes actual comparison/sweep tables under outputs/ and the final narrative under paper/.
