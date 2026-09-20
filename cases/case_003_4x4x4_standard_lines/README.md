# case_003_4x4x4_standard_lines

## Overview

This directory contains the public artifacts for this optimization case.

## Status

- Case type: `optimization`
- Solver status: `optimal` (PuLP/CBC; Gurobi unavailable because its license had expired)
- Objective value: `0` complete lines
- Verification: `passed` with full case-specific coverage
- Board balance: `32 X / 32 O`; lines checked: `76`

## Structure

- `Problem.md`: problem statement when present.
- `code/`: executable solver, public data snapshot, model exports, and run summaries when present.
- `outputs/`: result tables, figures, exported data, and case-specific attachments.
- `paper/`: final paper source and `main.pdf` when compilation succeeds.

## Main Artifacts

- `outputs/board_solution.json`
- `outputs/line_counts.json`
- `paper/main.pdf`
- `paper/main.tex`
- `paper/sections/abstract.tex`
- `paper/sections/algorithm.tex`
- `paper/sections/audit.tex`
- `paper/sections/conclusion.tex`
- `paper/sections/experiments.tex`
- `paper/sections/introduction.tex`
- `paper/sections/method.tex`
- `paper/sections/model.tex`
- `paper/sections/results.tex`

## Interpretation

The objective counts every monochromatic length-four standard line, regardless of symbol. The reported feasible coloring has zero such lines. Because the objective is a sum of nonnegative binary indicators, zero is a global lower bound and the feasible solution proves global optimality. Alternating play is represented exactly by the final 32/32 balance; any such coloring can be ordered into alternating moves.

The generic bundle precheck has advisory unsupported checks for indexed expressions, so the report relies on the case-specific feasibility, objective-recheck, geometry, and constraint evidence. No move-by-move sequence is claimed; only the final coloring is optimized.
