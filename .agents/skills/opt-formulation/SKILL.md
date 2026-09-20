---
name: opt-formulation
description: Structured guide for converting natural language optimization problems into solver-ready formulation packages (opt_spec.json, data_manifest.json).
---

# Optimization Formulation Skill

## Workflow

### Step 1: Data Manifest (MUST be written FIRST)

Write `.optagent/cases/{case_id}/data/data_manifest.json` before anything else.

Fast path for large cases:
- If the coordinator prompt already gives the model class, entities, data files, and core constraints, use that context directly.
- Inspect headers, small samples, and cheap row counts first; avoid repeated full-table scans.
- Treat observed CSV headers and JSON shapes as authoritative. Do not run follow-up checks against guessed field names or container types.
- Prefer `data_ref` to raw source tables. Put all solver-needed source and derived data objects under `.optagent/cases/{case_id}/data/objects/`; the solver will later create the public runtime snapshot under `cases/{case_id}/code/data/`.
- Keep the two core deliverables written early so a timeout can still leave recoverable artifacts.
- Build a compact first draft from actual case semantics. The examples under `.agents/schemas/` are references, not fields to copy wholesale; include optional branches only when they describe the case.
- For files over 10,000 rows, default to one streaming pass per file for validation stats. Additional targeted passes are fine when correctness requires them, but combine checks into one script where practical and avoid exploratory full-table loops.
- If a large sparse relation is already present as a source table, register it as a `sparse_relation` object and reference it directly instead of generating duplicate derived JSON.

1. Read all data files referenced in the problem (CSV, Excel, JSON, etc.).
2. For each data source, record:
   - `role`: set | parameter_table | dense_parameter | sparse_relation | scalar | mapping | raw_reference
   - `source_type`: csv | excel | json | etc.
   - `source_path`: workspace-relative path under `.optagent/cases/{case_id}/data/objects/` whenever materialized
     (the solver later creates the public snapshot under `cases/{case_id}/code/data/`; that public manifest
     must use public-relative paths under `code/data/`, never `.optagent/...` paths)
   - `metadata.kind`: user_input | derived | benchmark | manual
   - `metadata.authenticity`: observed | derived | imputed | synthetic
   - `metadata.materialized`: true unless a large external file could not be copied
   - `metadata.source_basis`: required logical sources or domain basis for imputed/synthetic data
   - `metadata.generation`: method, randomness_used, and seed when randomness is used
   - `key_fields`: primary key columns
   - `value_fields`: mapping of logical names to actual column names with types/units
3. Record cross-file `relationships` (foreign key linkages).
4. Record `assumptions` about data quality and `ambiguities` about data interpretation.

### Step 2: Problem Classification

Classify into: LP, MILP, QP, NLP, CP-SAT, routing, scheduling, robust, stochastic, or hybrid.

Decision guide:
- All continuous variables + linear objective/constraints → LP
- Any integer/binary variables + linear → MILP
- Quadratic objective with linear constraints → QP
- Nonlinear objective or constraints → NLP
- Scheduling, logical, combinatorial → CP-SAT
- Capacitated routing → routing (often MILP)
- Uncertain parameters → robust/stochastic

### Step 2.5: Pattern Matching

After the data manifest exists, run the local bounded OptGraph retriever:

```powershell
python .agents/skills/opt-formulation/tools/optgraph_retrieve.py --problem-file cases/{case_id}/Problem.md --output .optagent/cases/{case_id}/knowledge/retrieval_bundle.json
```

If `retrieval.status` is `sufficient` or `ambiguous`, use only applicable nodes
as a starting pattern and error checklist. Verify every retrieved assumption
against `Problem.md` and observed data. The retriever is local-first: when the
local graph is `insufficient`, it may query the allow-listed official OR-Tools
source index, cache compact source-backed case nodes beside the bundle, and
return `sufficient_online`. These nodes always carry `code_url`, retrieval
time, and `online_source_verified_not_reproduced`; they are leads, not verified
objective values. If both local and online retrieval are insufficient, consult
`reference/problem-taxonomy.md` and the family section of
`reference/standard-formulations.md` instead.

Blueprint fast path:

- When the retrieval bundle is `sufficient` and the selected formulation
  payload contains `blueprint`, use its `data_contract`, variable list,
  objective, constraint templates, and validation checks as the first draft.
- Bind the blueprint to the observed data manifest and expand only the
  indexed constraints required by the current instance. Do not reconstruct a
  standard formulation from the full reference Markdown unless the blueprint
  is missing, contradictory, or the problem introduces an unsupported rule.
- Preserve the blueprint's `instantiation_rule` and record any deviation in
  `modeling_decisions`. A blueprint is a reusable starting structure; it does
  not override Problem.md or observed data.

For every retrieved choice actually used, include stable node IDs in the
corresponding `modeling_decisions[].knowledge_refs`. Never cite an unused node.
For variable type decisions, follow `reference/variable-domain-guide.md`.

### Step 3: Formulation Construction

Build `.optagent/cases/{case_id}/formulation/opt_spec.json` (must remain <50KB — use data_ref for large parameters):

When writing JSON artifacts through a Python heredoc, use Python literals (`True`, `False`, `None`) inside dictionaries. The JSON examples in this skill intentionally use JSON literals (`true`, `false`, `null`); do not paste those tokens into executable Python. Writing valid JSON directly is also acceptable.

1. **Sets**: Small sets (≤100 elements) use inline `elements`. Large sets use `type: "range"` with `start`/`end`.
2. **Parameters**: Scalars and compact solver-critical collections may use inline `value`. Large or duplicated source data normally use `data_ref` pointing to a manifest object, e.g. `"data_ref": {"object": "demand", "key_fields": ["sku"], "value_field": "qty"}`. Include `shape` and optional `nnz` metadata for data references.
3. **Decision Variables**: Define with type (continuous/integer/binary), bounds, indexing.
4. **Objective**: Define a readable expression and the applicable scalar or indexed machine-checkable terms.
5. **Constraints**: Define name, description, expression, and a `checker` whenever the constraint is representable by the checker contract.
6. **Indexed Constraints**: Use `checker_template` for representable indexed constraints; do not invent a partial checker for unsupported semantics.

For a repeated family with more than 16 structurally equivalent constraints, use one `indexed_constraints` entry with its index set and checker template. Keep per-index coefficients and data in data references. This is a serialization rule only: preserve every equation and leave genuinely heterogeneous constraints explicit.

### Step 4: Machine-Checkable Fields

For linear/mostly-linear models, populate every applicable machine-checkable field:

```
objective.constant         -> constant term when present
objective.linear_terms     -> scalar-variable terms when present
objective.indexed_terms    -> indexed terms when present
constraints[].checker      -> each representable linear constraint
indexed_constraints[].checker_template -> each representable indexed linear constraint
```

Do not omit a representable linear term or constraint merely to reduce file
size. Conversely, do not create empty duplicate arrays or fake checker fields
when a representation is inapplicable; retain the readable expression and
document a material limitation or modeling choice instead. These fields enable
automatic verification by the Verifier agent.

### Step 5: Quality Checklist

Before finishing, verify:

- [ ] Every decision variable has type and bounds (lb/ub)
- [ ] Every constraint has a unique name and description
- [ ] Objective has a readable expression plus every applicable scalar/indexed term representation
- [ ] Machine-checkable checker fields are present for every representable linear constraint
- [ ] No placeholder strings in coefficient values (must be concrete numbers or data references)
- [ ] Units documented where relevant
- [ ] Small sets have concrete elements; large sets use range notation
- [ ] Inline collections above 100 entries were reviewed; use data_ref when it avoids material duplication or improves provenance
- [ ] All data_ref objects exist in `.optagent/cases/{case_id}/data/data_manifest.json`
- [ ] Ambiguities are genuinely blocking (not routine assumptions)
- [ ] data_manifest.json is consistent with opt_spec.json data_ref object references
- [ ] opt_spec.json is under 50KB
- [ ] retrieval_bundle.json exists and every adopted graph assumption was checked against case evidence
- [ ] used graph patterns and errors have stable `knowledge_refs` in the relevant modeling decision

### Step 5.5: Modeling Decision Documentation

Record every non-trivial modeling choice in opt_spec.json → `modeling_decisions`:
```json
"modeling_decisions": [{"aspect": "variable_type", "choice": "binary",
  "justification": "...", "alternatives_considered": ["continuous [0,1]"], "ambiguous": false}]
```
If variable domain is ambiguous (problem admits both binary and continuous), set `ambiguous: true`.
See `reference/variable-domain-guide.md` for decision framework.
Check `reference/common-pitfalls.md` for problem-type-specific risks.

### Step 5.6: Structural Validation

For iterative draft diagnostics, run:
```bash
if ! python .agents/skills/opt-formulation/tools/spec_validator.py --spec .optagent/cases/{case_id}/formulation/opt_spec.json --manifest .optagent/cases/{case_id}/data/data_manifest.json; then
  echo "validator diagnostics require repair"
fi
```
Fix every error found before completing, then run the validator normally and require a successful `valid: true` result.

Run the validator once after the compact draft as well when the core manifest
and spec already exist. Treat its size and inline-data warnings as prompts to
review representation, never as permission to alter equations, domains, hard
constraints, or data meaning automatically.

### Step 6: Structured Handoff

Do not write a formulation Markdown report or any file under `cases/{case_id}/outputs/`.
The final paper is produced later from data_manifest.json, opt_spec.json, solver evidence, verification evidence, and decision_log.json.

When approval_status is `auto_approve`:
- Resolve ambiguity autonomously using explicit statements, supplied data, domain convention, least-assumptive semantics, then reproducibility.
- Mark generated, imputed, and derived data distinctly in the manifest. Record method, source basis, and a deterministic random seed when randomness is used.
- Preserve hard constraints and never hide relaxation or synthetic-data use.
- Surface material choices to the Coordinator for append-only decision logging.

## Rework Checklist

When re-dispatched after verification failure:

1. Read `.optagent/cases/{case_id}/verification/diagnosis.json` FIRST
2. Identify which constraints/variables were flagged
3. Check if the issue is:
   - Wrong constraint direction (≤ vs ≥)
   - Missing variable or bound
   - Incorrect coefficient
   - Wrong data reference
4. Fix the specific issue in `.optagent/cases/{case_id}/formulation/opt_spec.json`
5. Update data_manifest.json if data interpretation changed
6. Surface any changed material decision so the Coordinator can append a superseding decision-log entry
