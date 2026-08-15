---
name: opt-reporting
description: Build the final OptAgent paper and public case index directly from structured formulation, solve, verification, and decision evidence.
---

# OptAgent Reporting

## Public Contract

`state.json.report_contract` is the authoritative final narrative format.
The default `latex` contract uses the paper under `cases/{case_id}/paper/`.
An explicit user-requested `markdown` contract uses only
`cases/{case_id}/report/final.md`; do not create placeholder LaTeX merely to
satisfy the default audit. `cases/{case_id}/README.md` is a concise entry point
and artifact index in either mode.
`cases/{case_id}/outputs/` contains only actual results, tables, figures, exports, and attachments.

Never create narrative reports such as formulation.md, summary.md, technical_report.md, comparison_report.md, sensitivity_report.md, benchmark_report.md, or pareto_report.md under outputs/.

## Evidence

Begin with `python .agents/tools/case_evidence.py claims --workspace-root . --case-id {case_id}`. For an optimization case, also read its formulation stage view. These provide a bounded, source-linked index of status, objective, coverage, active disclosures, and public artifacts without persisting a report manifest.

The index is not evidence authority. Read `Problem.md` and `opt_spec.json` for equations and model semantics, and reopen the cited solution, execution, diagnosis, decision, code, result, or referenced-case source whenever it is truncated, marked `open_source_required`, non-optimal/partial/failed, material to disclosure, or needed for exact values. `objective_recheck.json`, `constraint_check.csv`, and `feasibility_report.json` are conditional evidence: use them when present, but do not invent them for an inapplicable check.

`solution.json.variables` is an object keyed by variable name. Prefer bounded evidence views; if sampling it in Python, use `list(variables.items())[:N]` rather than slicing the object.

Do not require all optimization artifacts for an analysis case. Report missing expected evidence truthfully.

## Decision Log

Follow `supersedes` links to determine active decisions. Historical entries remain evidence but are not current policy.
Every active entry with `impact.user_visible=true` must appear in README and the relevant section of the selected narrative.
Always disclose generated or imputed data, semantic assumptions, hard-constraint relaxation, approximation, feasibility/optimality limits, and unresolved risks.

Keep exact values in CSV/JSON evidence, but use sensible significant digits for measured runtimes, gaps, and diagnostics in the selected narrative. Do not expose transient agent command failures or wrapper syntax details as scientific limitations when an equivalent canonical check subsequently succeeds; retain those only in internal diagnostics unless they materially weaken reproducibility or the result.

Public files must never name internal launchers, wrappers, agent infrastructure, hidden workspace paths, environment variables, or internal execution routing. Python may be named when it is a genuine public dependency. Describe successful public-entrypoint checks by outcome only. If execution is completely impossible, disclose the user-relevant reproducibility impact without infrastructure/configuration names.

## Claim Strength

- Proven: established by solver status/bounds and consistent checks.
- Verified: independently checked by verification artifacts.
- Observed: directly present but not independently established.
- Assumed: an explicit interpretation or data assumption.
- Approximate: produced by a heuristic, relaxation, or approximation.
- Unresolved: missing or conflicting evidence.

Never upgrade feasible to optimal, environment failure to infeasible, synthetic data to observed data, or partial verification to full validation.

## Workflow

1. Build one claim inventory from the read-only index, then inspect only the source evidence needed for the selected report contract and public audit.
   For `latex`, bootstrap may create an empty `paper/` directory; treat it as initialized only if `paper/main.tex` exists. For `markdown`, create and populate `report/final.md` directly and do not initialize LaTeX.
2. Under `latex`, build or update the standalone paper using opt-writing and replace every initialized section with evidence-grounded content. Under `markdown`, write one meaningfully populated `report/final.md` with the same claim-strength and disclosure rules.
3. Put analysis matrices or sweep results in CSV/JSON under outputs/ and their interpretation in the selected narrative.
   For comparisons, verify all arithmetic and distinguish absolute counts from normalized density, and one-run runtime observations from evidence of general problem difficulty. Undefined metrics remain NA rather than zero.
4. Remove only the three known legacy generated reports: formulation.md, summary.md, and technical_report.md.
5. Under `latex`, run `python .agents/tools/build_paper.py --case-dir cases/{case_id} --reporting-dir .optagent/cases/{case_id}/reporting` and read its JSON status. Skip this build under `markdown`.
6. Run update_case_readme.py after the final narrative/build step so the index reflects the selected contract and actual outputs.
7. Audit public files after the last public edit with `python .agents/tools/audit_public_case.py --case-dir cases/{case_id} --report-contract {latex|markdown} --json`, using the exact value from state. Fix every listed error until JSON says `passed=true`; JSON mode's zero process exit is not a pass.

Do not delete unknown Markdown files. They may be user-owned.

## Completion

Required in all modes:
- cases/{case_id}/README.md
- the selected narrative artifact
- a passing audit whose `report_contract` matches state

For `latex`, the selected artifact is `paper/main.tex` plus six meaningfully
populated sections; the internal paper build log is required and PDF remains
best effort. For `markdown`, the selected artifact is a meaningfully populated
`report/final.md`; no LaTeX source, build log, or PDF is required.
