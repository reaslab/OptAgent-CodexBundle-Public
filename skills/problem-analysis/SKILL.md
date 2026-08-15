---
name: problem-analysis
description: Use when an optimization problem is still being clarified and the agent must separate stated facts from assumptions before formulation.
---

## Use when

Use this skill when the optimization problem statement is open-ended, underspecified, easy to over-interpret, or likely to tempt the agent into inventing structure too early.

Typical signals:

- The task mixes explicit givens with implied context.
- The objective function is not yet precise enough to formulate safely.
- Multiple interpretations of constraints or goals are plausible.
- The agent is about to introduce hidden assumptions, variable definitions, or constraint structures not stated by the user.
- Business rules are ambiguous (overtime policy, penalty structures, resource availability).

## Inputs

- The original problem statement or optimization request.
- Any user-provided data files, constraints, objectives, and domain context.
- Any uncertainty about scope, missing data, or ambiguous terminology.

## Outputs

- A short problem restatement in plain language.
- A separated list of knowns, unknowns, and unresolved ambiguities.
- A clear distinction between user-provided facts and agent-added assumptions.
- Success criteria describing what would count as a satisfactory solution.
- A short set of candidate modeling directions, each labeled with why it may fit.

## Hard rules

- Separate given facts from agent-added assumptions explicitly.
- Do not silently invent variables, constraints, objectives, or data structures.
- Mark every assumption as an assumption, not a fact.
- If a key detail is missing (cost data, capacity limits, demand values), say it is missing instead of filling it in without notice.
- Do not jump to formulation or solver code before the problem frame is clear enough.
- If multiple interpretations are plausible (minimize cost vs. maximize profit, hard vs. soft constraints), surface them instead of pretending one is certain.

In `auto_approve` mode, surfacing alternatives does not mean stopping for user input. Rank them, choose the least-assumptive reproducible interpretation using explicit statements and supplied data first, and mark the selected interpretation as an agent assumption for decision logging. Missing data may be derived, imputed, or generated only with explicit authenticity, method, source basis, and deterministic seed metadata.

## Process

1. Restate the optimization problem in simpler words without changing its meaning.
2. Extract explicit givens into a `Given facts` section (data files, stated constraints, objective direction).
3. List `Unknowns and ambiguities` that still block confident formulation.
4. Add an `Assumptions introduced by the agent` section. If none are needed yet, say `None`.
5. Define `Success criteria` so the formulator has a clear target (optimality gap tolerance, constraint priorities, solution format).
6. Produce a short ranked set of `Candidate modeling directions` (LP vs. MILP vs. CP-SAT, single vs. multi-objective, etc.).

## Red flags

- Treating common optimization conventions as if the user already stated them (e.g., assuming minimize when not specified).
- Converting a word problem into a mathematical program before naming the interpretation.
- Smuggling in penalty terms, slack variables, or relaxations without explicit acknowledgment.
- Acting as if missing cost/demand data is obvious when it materially changes the solution.
- Assuming constraint priority (hard vs. soft) without user confirmation.

## Handoff guidance

- Move to formulation (opt-formulator) only after the facts, assumptions, and success criteria are explicit enough to support a defensible model.
- In manual mode, if the problem is still too ambiguous, surface it to the user for clarification.
- In auto_approve mode, select and document a defensible interpretation or a truthful reportable limitation; never leave the workflow waiting for clarification.
