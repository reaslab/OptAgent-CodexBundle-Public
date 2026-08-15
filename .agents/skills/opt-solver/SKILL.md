---
name: opt-solver
description: Backend implementation guidance for OptAgent's public solver entrypoint and managed formal-solve contract.
---

# Optimization Solver Skill

## Scope

The Solver Engineer role prompt is the authoritative contract for paths,
deliverables, public/internal boundaries, status normalization, and completion.
This skill adds backend implementation guidance; do not create a second solver
workflow or bypass the managed helper.

## Backend Selection

| Problem Class | Preferred | Fallbacks |
| --- | --- | --- |
| LP / MILP | gurobipy | pulp, ortools-linear, pyomo-cbc |
| CP-SAT / scheduling / logical | ortools-cp-sat | no direct fallback unless semantics match |
| Convex QP/SOCP/SDP | cvxpy | gurobipy when semantics are supported |
| Nonlinear | pyomo-ipopt | cvxpy only for convex subset |

`state.json.preferred_solver` and `fallback_solvers` are the case-specific
attempt order. This table is default guidance only when the Coordinator has
not supplied a compatible chain. Never change model semantics to use a
different backend.

Automatic scaffolded code generation is limited to `gurobipy`, `pulp`,
`ortools-linear`, `ortools-cp-sat`, `cvxpy`, `pyomo-cbc`, and `pyomo-ipopt`.
Do not treat a static registry entry or an installed package as proof that an
unscaffolded backend may be selected automatically. Follow the role prompt's
runtime probe and use its result as availability evidence.

For OR-Tools Linear, never assume SCIP is linked. Use an actually creatable
native solver: for MILP try `CBC_MIXED_INTEGER_PROGRAMMING`, `CBC`,
`SAT_INTEGER_PROGRAMMING`, then `SAT`; for LP try `GLOP_LINEAR_PROGRAMMING`,
`GLOP`, `CLP_LINEAR_PROGRAMMING`, `CLP`, then CBC if needed. Record the
selected native solver and attempts in the managed result metrics.

## Scaffold and Data Hooks

Create a missing entrypoint with
`python .agents/tools/solver_scaffold.py --backend {backend} --case-dir cases/{case_id}`.
Patch its data-loading, model-building, solution-extraction, and result-table
hooks instead of recreating boilerplate. Preserve
`run_formal_solve(time_limit: int) -> dict` and its call from `main`.

Resolve `data_ref.object` through the manifest's top-level `data_objects`
mapping before creating the public snapshot. Never infer a filename from an
object name or use the legacy `objects` field. The public entrypoint reads only
its copied `code/data/` snapshot; it never accesses `.optagent` at runtime.

## Backend-Specific Correctness

- Gurobi: initialize `gp.Env(empty=True)`, set `OutputFlag=0` and a temporary
  `LogFile`, call `env.start()`, then create `gp.Model(..., env=env)`. Map
  license/import failures to `solver_unavailable`, never to infeasibility.
- PuLP/CBC: pass a temporary `logPath` to `PULP_CBC_CMD` and parse its native
  log. A time-limited incumbent is `feasible/time_limit`, not `optimal`; only
  explicit CBC optimality evidence permits `optimal`.
- Keep normal native progress output off: CP-SAT uses
  `log_search_progress=False`, OR-Tools Linear does not call
  `solver.EnableOutput()`, CVXPY uses `verbose=False`, and Pyomo uses
  `tee=False`. Temporary parser logs stay outside the public case directory
  and are deleted after evidence is captured.

Export `model.lp` or `model.mps` only when the selected backend can produce a
faithful representation. An unavailable or failed export is a note, never a
reason to fabricate another formulation.

## Managed Formal Solve

Run one formal attempt with:

```bash
python .agents/tools/run_managed_solver.py --workspace-root . --case-id {case_id}
```

The helper calls `run_formal_solve` once, writes internal solution, execution,
and log records atomically, keeps stdout compact, and appends a structured
entry to `state.json.solver_attempts`. Failed attempts also append a bounded
entry to `state.json.error_log`; later successful attempts do not erase that
history. Do not write those records, hashes, or state telemetry by hand.

Presolve evidence (advisory, when available): after the formal solve, if the
native backend log reports presolve reduction (e.g. Gurobi "Presolve removed
N rows, M columns", CBC "Presolve ... rows, ... columns", CP-SAT cut/presolve
counts), record `presolve_before_rows`/`presolve_before_cols` and
`presolve_after_rows`/`presolve_after_cols` in `execution_record.json`
(alongside the normalized status). This is optional, advisory evidence for the
verifier's redundancy audit; never fabricate numbers from a log that lacks them.
The raw native log (including presolve lines) is already captured in
`solver_log.txt` for inspection.

During an active formal call, absent internal records are normal. Wait for the
call rather than launching another one. A compact helper response with
`status="busy"` is the only normal same-case re-entry signal: wait and inspect
existing records, never clear them or start a parallel solve. The local lock
does not prove that an interrupted remote execution has stopped.

For an incumbent, return every instantiated scalar variable, including zeros
and auxiliary/slack variables. For a non-incumbent outcome, return
`has_incumbent=false` and `variables={}`. Never confuse an environment,
license, import, or missing-binary failure with model infeasibility.

After a verification rework request, patch the existing public code or data,
then run the managed command once and inspect its refreshed records. Re-run
the scaffolded `--dry-run` as the normal preflight; it already checks inputs.
Use `--check-inputs` only to diagnose a missing, unsupported, or failed dry
run. Do not repeat a long solve merely to test portability.
