---
name: opt-verification
description: Deterministic precheck and focused follow-up guidance for independent OptAgent verification.
---

# Optimization Verification Skill

## Scope

The Verifier role prompt is the authoritative contract for source artifacts,
approval behavior, diagnosis fields, mathematical coverage, attribution, and
completion. This skill supplies the bounded evidence commands and only the
follow-up techniques that are not already guaranteed by the role prompt.

## Bounded Evidence First

Start with the solver-stage view:

```bash
python .agents/tools/case_evidence.py stage \
  --workspace-root . --case-id {case_id} --stage solver
```

It is a routing view, not a substitute for the authoritative sources. Reopen a
source whenever the view is truncated, a check fails or is partial, or a
conclusion needs exact equations, variables, data, code behavior, logs, or
attribution evidence.

Before writing a case-specific verifier, run the deterministic bundle:

```bash
python .agents/tools/verification_bundle.py \
  --spec .optagent/cases/{case_id}/formulation/opt_spec.json \
  --manifest .optagent/cases/{case_id}/data/data_manifest.json \
  --solution .optagent/cases/{case_id}/solver/solution.json \
  --execution-record .optagent/cases/{case_id}/solver/execution_record.json \
  --public-code cases/{case_id}/code/run.py \
  --solver-log .optagent/cases/{case_id}/solver/solver_log.txt \
  --output .optagent/cases/{case_id}/verification/precheck_bundle.json
```

Add `--public-data-manifest cases/{case_id}/code/data/manifest.json` when it
exists. The bundle is an evidence index with source hashes:

- `pass` applies only to its declared coverage.
- `fail` requires diagnosis or rework.
- `unsupported`, `not_applicable`, and `environment_limited` are never passes.

Read raw helper output and the cited source before attributing a finding. The
bundle does not replace Problem-to-Model review, indexed/nonlinear mathematics,
evidence conflict resolution, or diagnosis ownership.

## Focused Follow-Ups

Do not rerun generic helpers after a successful bundle unless a concrete raw
finding needs investigation. For model/code follow-up:

```bash
python .agents/skills/opt-verification/tools/model_auditor.py \
  --spec .optagent/cases/{case_id}/formulation/opt_spec.json \
  --code cases/{case_id}/code/run.py \
  --manifest .optagent/cases/{case_id}/data/data_manifest.json
```

For a solution-focused follow-up:

```bash
python .agents/skills/opt-verification/tools/solution_checker.py \
  --solution .optagent/cases/{case_id}/solver/solution.json \
  --spec .optagent/cases/{case_id}/formulation/opt_spec.json \
  --manifest .optagent/cases/{case_id}/data/data_manifest.json
```

For the redundancy / precision audit (Step 11, advisory only):

```bash
python .agents/tools/redundancy_audit.py \
  --spec .optagent/cases/{case_id}/formulation/opt_spec.json \
  --output .optagent/cases/{case_id}/verification/redundancy_audit.json
```

Read the produced `redundancy_audit.json` and record any P2 (model-class precision)
or P4 (unused variables / duplicate constraints / redundant index dimension)
findings in `diagnosis.json.notes` and `diagnosis.json.evidence` as advisory hints.
This audit is advisory-only: it never changes `validation.status` or `next_action`,
and it must never bias the correctness verdict. Report only what the tool concretely
finds; absence of a finding is not evidence of redundancy, and a structurally similar
row over distinct entities is not redundant.

These helpers are advisory and bounded. A fully matched scalar linear check may
support the corresponding diagnosis evidence. Indexed, nonlinear, symbolic,
or data-dependent objectives and constraints require a correct case-specific
method or an explicit partial/unsupported coverage record. Never turn an
unsupported check into a pass.

When writing `verification/verify.py`, keep it a short source-hash runner plus
only genuinely case-specific mathematics. Resolve the workspace from explicit
case paths or an ancestor marker, not a fixed `Path.parents[n]` depth. Run it
once before completing verification.
