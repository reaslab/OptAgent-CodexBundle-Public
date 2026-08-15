---
name: opt-writing
description: Use when OptAgent must produce a final LaTeX paper or contest-style solution document after optimization reporting, especially under cases/{case_id}/paper with bilingual cn/en templates.
---

# OptAgent Writing

## Core Rule

Write a polished, standalone LaTeX paper directly from structured OptAgent evidence and place the LaTeX project under `cases/{case_id}/paper/`. The paper is the only final narrative report.

The paper is a problem-solving narrative, not a template-filling exercise. Start from the real optimization context, motivate the model from that context, then present the model, algorithm, and numerical evidence with claims no stronger than the verified artifacts support.

## Scope

The reporter prompt and `opt-reporting` skill own evidence selection, public
artifact policy, claim strength, README generation, build, and audit. This
skill adds only paper initialization and writing conventions. Use the accepted
case evidence they provide; do not invent a second report or repeat their
workflow.

## Initialize

Choose the language before calling the tool:
- Use `cn` when the user request, problem statement, or final answer language is Chinese.
- Use `en` when the user request and problem statement are English.
- If mixed, prefer the user's latest explicit language.

Run:

```bash
python .agents/skills/opt-writing/initialize.py --lang cn|en --output cases/{case_id}/paper
```

`.agents/skills/opt-writing/initialize.py` is the runtime path for the built-in skill in the agent workspace.

If `cases/{case_id}/paper/` already contains user or agent writing, do not overwrite it. Use `--force` only when the coordinator explicitly intends to reset the paper directory.

## Required Drafting Sequence

Fill these files:
- `sections/abstract.tex`
- `sections/introduction.tex`
- `sections/model.tex`
- `sections/algorithm.tex`
- `sections/experiments.tex`
- `sections/conclusion.tex`

Draft in this order unless the coordinator gives a narrower editing task:
1. Use accepted evidence to identify the model, solver status, numerical results, and unresolved caveats.
2. Fill the introduction from the real background toward the optimization formulation; do not collapse the background into a thin objective-and-constraints summary.
3. Fill the model section with assumptions, symbols, objective, constraints, and any transformation from the original model to the solved model.
4. Fill the algorithm section using method-level prose and pseudocode, not source-code excerpts.
5. Fill experiments from actual solver and verification evidence. Optional reliability, sensitivity, comparison, and other subsections are included only when corresponding evidence exists.
6. Fill the conclusion section with the modeling route, main results, verification scope, limitations, and practical recommendation.

## Evidence And Claim Discipline

- Copy numerical values and symbols faithfully from accepted artifacts.
- Preserve claim strength: do not upgrade feasible to optimal or partial evidence to full validation; state gaps and material limitations plainly.
- Ground every major conclusion in a result, check, table, solver status, or derivation. Label material synthetic/imputed data, semantic decisions, relaxations, heuristics, feasible incumbents, and partial verification.
- Put additional tables under `tables/` and figures under `figures/`.

## Independent Report Discipline

- Write a standalone paper: never name workspace files, paths, user requests, agent activity, or `.optagent` in the paper body.
- Describe implementation with paper-style prose, pseudocode, and parameters rather than copied source code or file references.

## Prose And Structure Discipline

- Motivate the model from the real decision setting before equations, then write continuous academic prose rather than note-like lists.
- Use emphasis, bullets, numbering, and theorem environments only when they improve clarity; do not leak Markdown into LaTeX.
- Each `\section` needs a transition paragraph before any `\subsection`, explaining its question, evidence base, and relation to the preceding argument.

## Preloaded LaTeX Support

Reuse the packages and notation macros already declared in `main.tex` (including
math, table, figure, algorithm, and theorem support). Inspect them before
adding a package; add one only when the construct is genuinely required and
available to the compiler.

## Build Handoff

Do not compile or open the paper directly. The reporter owns the single final
build through `build_paper.py` after all source edits, then updates README and
runs the public audit. Keep the LaTeX project complete and report only writing
facts to the reporter; the deterministic build record is the source of truth
for PDF status and diagnostics.

## Completion Check

Before returning, verify:
- `main.tex` exists.
- All six section files exist and are meaningfully populated.
- The paper has no workspace/agent trace or `\section` immediately followed by `\subsection`.
- The project is ready for the reporter's deterministic build handoff.
