# 4x4x4 cooperative 3D tic-tac-toe (standalone Agent run)

This case models the completed 4x4x4 board directly. The optimization assigns
32 X symbols and 32 O symbols and minimizes the number of monochromatic winning
lines. It is a cooperative final-state formulation, not an adversarial
move-by-move game model.

## Status

- Case type: `optimization` / MILP
- Solver status: `optimal`
- Objective value: `0` complete monochromatic lines
- Backend: PuLP with CBC; Gurobi was attempted but was unavailable in the run environment
- Verified: all 64 cells assigned, 32 X and 32 O, all 76 line indicators exact, 0 constraint violations

## Model convention

The counted line set has exactly 76 standard length-4 lines: 48 axis lines,
24 planar diagonals (both diagonals in each coordinate plane), and 4 body
diagonals. Cell ids use `16*x + 4*y + z`. Alternating play is represented by
the final count constraint only: any 32/32 coloring can be ordered into 32
alternating X/O turns, so no additional sequencing or adversarial payoff is
imposed.

## Structure

- `Problem.md`: authoritative problem statement.
- `code/`: reproducible public solver, input snapshot, and LP/MPS exports.
- `outputs/`: compact board and complete-line counts.
- `report/final.md`: formulation, result, assumptions, and verification evidence.

The public entrypoint is `code/run.py`; it reads only `code/data/` and supports
`--dry-run`, `--check-inputs`, and `--time-limit`.
