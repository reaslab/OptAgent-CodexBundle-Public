---
name: opt-writing
description: Write evidence-grounded optimization reports in Markdown or the supplied LaTeX style, including the mathematical model, computational method, results, and limitations.
---

# OptAgent Writing

## Core Rule

Write a clear, standalone optimization report from the current structured evidence. Use the user-requested format. State the decision problem and scope at the level supported by the problem statement, then explain the model, method, and numerical evidence. Make claims no stronger than the bound artifacts support.

## Scope

This skill owns the shared body-writing conventions for Markdown and LaTeX. `opt-report` owns evidence selection, output location, format choice, README and delivery arrangement. `opt-review` owns independent review, final-artifact reading, approval and selection rules. Follow those skills instead of duplicating their workflow here.

The brief `model.md` is an earlier user-facing explanation governed by `opt-workflow`, not a complete report. A later report must reuse its mathematical content and remain self-contained; do not replace the report's full model section with a link to the short note.

## Format and Structure

- Follow the current request. If no report format is specified, default to Markdown. For LaTeX source or PDF, use the supplied template and compile with local tools.
- The common structure follows the supplied LaTeX sections: optional abstract or opening summary, problem statement and scope, optimization model, algorithm and implementation, numerical experiments, and conclusion. In either format, the first substantive section after the title and any abstract states the decision problem and its scope; an abstract does not replace that section. Markdown may omit the abstract or keywords and combine short sections while preserving the same substantive content; keep it as one complete `.md` file.
- In LaTeX, complete the six supplied section files. Follow the language specified by the current request; if omitted, use the language best supported by the problem and user conversation. An existing paper directory is user work: edit it incrementally and do not reset it just to apply a template.
- Use `$...$` and `$$...$$` for inline and display mathematics in Markdown. State symbols in math notation, not as unexplained code field names.
- In LaTeX, inspect `main.tex` before adding packages. Reuse the declared math, table, figure, and algorithm support and notation macros.

## Copy a LaTeX Template

Choose the matching `template_cn/` or `template_en/` under the currently installed `opt-writing` skill. Use the actual Case `artifact_dir`; do not infer a case directory from its ID or assume a historical runtime path. For a new `paper/` directory, set quoted `paper_dir` and `template_dir` paths and create the directory and copy the full template contents using the current host shell, including hidden files such as .latexmkrc; do not use a wildcard that may omit them. If `paper/` already exists, leave its files in place and edit it incrementally instead of copying over them. Create `figures/` or `tables/` only when the report needs them. Template copying and LaTeX compilation are local workspace operations.

## Required Drafting Sequence

For LaTeX, fill these files:

- `sections/abstract.tex`
- `sections/introduction.tex`
- `sections/model.tex`
- `sections/algorithm.tex`
- `sections/experiments.tex`
- `sections/conclusion.tex`

For either Markdown or LaTeX, draft in this order unless the assigned edit is narrower:

1. Use the current sources, model, and available computation or proof records to identify the actual model, solver status, numerical results, and unresolved caveats. Lead with the result and its conditions in the abstract or opening summary.
2. State the problem setting and scope using the information the user provided. For an abstract problem, it is fine to proceed directly to the decision variables, objective, and constraints.
3. Write the model in a clear order: material assumptions; notation with symbols, meanings, units, and sources when available; decision variables and domains; objective; and necessary constraints grouped by their modeling role. Give each constraint a formula and explain its meaning in the problem at the level the prompt supports, then summarize model size when useful. End the model section with a complete optimization model subsection that gathers the objective, all necessary constraints with their index sets or quantifiers, and every variable domain. Previously defined sets and parameters may be referenced. Explain correctness in prose rather than proposition or proof environments.
4. Describe algorithms in method-level prose or pseudocode, not source-code excerpts.
5. Report experiments from actual solver and verification evidence. Give solver status, objective and units, optimality gap and its convention, runtime, and the maximum independently recomputed violation when available. Add sensitivity, comparison, or reliability sections only when their evidence exists.
6. Conclude with the modeling route, main results, verification scope, limitations, and practical recommendation.

## Evidence and Claim Discipline

- Copy numerical values and symbols faithfully from their actual supporting artifacts. Keep each result, bound, gap, or status tied to its actual evidence and scope; leave evidence bindings in system records rather than naming internal Result/Seal records in report prose. Do not treat a selected or ready status as mathematical proof.
- For the same original instance, objective direction, and units, preserve a better concrete feasible point already independently verified in the original domain. A backend or Candidate change does not make that point disappear; label each point and bind its value, bound, gap, and status to its own evidence.
- State the sets, indices, units, variable domains, objective, and all necessary constraint families so a reader can reconstruct the modeled problem. Explain material assumptions and their source. Do not present an implementation shortcut as a source requirement.
- When using elimination, aggregation, decomposition, or another transformation, explain how it recovers original-domain decisions and under what conditions. State equivalence, optimal-value preservation, relaxation bounds, and global claims only for the object, quantifier, and assumptions actually established.
- Define each derived business metric, its unit, calculation, and coverage. Recompute it from the current input and saved solution; do not mix scopes or derive a route, count, or rate from a different point.
- Distinguish observation from causal explanation. Attribute an outcome to a method only when a derivation or controlled comparison supports that link. Label synthetic or imputed data, semantic choices, relaxations, heuristics, feasible incumbents, and partial verification.
- Preserve claim strength: do not upgrade feasible to optimal or partial evidence to full validation. State important uncertainty and limitations near the affected conclusion.

## Prose and Structure

- Genre: an applied optimization technical report for business and technical decision-makers, not a mathematics-journal paper. By default, use no theorem, lemma, proposition, proof, or definition environments. Present modeling assumptions and soundness in explanatory prose, with solving evidence and independently checked results. Only use formal property-analysis sections when the user explicitly requests them; include a plain-language reading, place proofs in an appendix using `\appendix`, and add `\usepackage{amsthm}` plus the required `\newtheorem` declarations to `main.tex` for that report.
- Explain how the formulation matches the stated problem and scope. Use concise prose, lists, or equations where each fits; do not add a business narrative that the problem does not support. Do not use Markdown syntax in LaTeX source.
- Use tables and figures only when they help explain the model or decisions. Add numerical tables under `tables/` and figures under `figures/`; give each a clear caption and preserve the measured scope and units.
- Keep workspace paths, user-request traces, agent activity, and `.optagent` details out of the report body. Put reproducibility commands, dependencies, and use instructions in the Case README.

## LaTeX Build and Final Artifact

The Owner owns the writing and build handoff; there is no separate Reporter or Writer role. From the Case's `paper/` directory, run `latexmk -pdf main.tex`. The supplied template creates `main.pdf` in that directory.

For a LaTeX/PDF report, the compiled PDF is the single review and delivery main artifact. The `.tex` files, sections, tables, figures, and required dependencies are supporting editable sources. Provide the actual PDF path to `opt-review`; do not use `main.tex` as the answer when its content depends on other files. A successful command or an existing old PDF is not sufficient: confirm that the current build produced a readable PDF at the reported path. The final PDF must remain unchanged until the Verifier has read it and `ready` has bound its bytes.

After a successful build and readable-PDF check, before the Verifier's final read, run `latexmk -c -bibtex- main.tex` to remove regenerable auxiliary files while preserving `.bbl` and other actual dependencies. Retain build logs when compilation fails.

Follow `opt-review` for full-text PDF extraction before `ready`. If the PDF cannot be read or the extracted text is materially incomplete, do not claim the final artifact was fully reviewed. Page rendering and visual inspection must use an available renderer and be reported only when an image-capable reviewer actually inspected the rendered pages.

## Editing and Completion

When editing existing `.tex` files, use `apply_patch` or another safe literal edit. After each write, reread the file to confirm it is non-empty and contains the intended change. Do not use inline Python string writes for LaTeX commands, where backslash escaping can corrupt the document.

Before handoff, verify the selected format:

- **LaTeX/PDF:** `main.tex` and all six section files exist; sections are meaningfully populated; no workspace or agent trace appears in the report; and the current locally compiled PDF exists, is readable, and corresponds to the submitted sources.
- **Markdown:** one complete `.md` report exists; equations use the required delimiters; tables, symbols, units, conclusions, and source-linked claims are readable and internally consistent.

Send `opt-review` the exact final main-artifact path and a concise notice that it is ready to read. Do not change that artifact while the Verifier is reading or after `ready`.
