# Final Report: 4x4x4 Cooperative 3D Tic-Tac-Toe

## Result

The final-state MILP solved to a global optimum with objective value **0**:
there are no complete monochromatic lines for either X or O. The returned
board contains 32 X cells and 32 O cells. The public result is in
`outputs/board_solution.json`; the line-level result is in
`outputs/line_counts.json`.

The preferred `gurobipy` backend was attempted first but was unavailable
(`ModuleNotFoundError: No module named 'gurobipy'`). The run therefore used the
configured PuLP/CBC fallback and reported `Optimal` in approximately 1.283
seconds, with zero reported MIP gap. The execution record lists both attempted
backends.

## Exact formulation

Let `C = {0,...,63}` be the cells, `L = {0,...,75}` the counted lines, and
`P = {0,1,2,3}` the four positions in a line. Cell ids are
`c(x,y,z) = 16*x + 4*y + z`, for `x,y,z in {0,1,2,3}`. The data snapshot
`code/data/line_rules.json` supplies the four cell ids `cells[l,p]` for every
line.

Binary variables:

- `x_c = 1` iff cell `c` is X; `x_c = 0` means O.
- `line_x_l = 1` iff line `l` is all X.
- `line_o_l = 1` iff line `l` is all O.

Objective:

```text
minimize  sum(l in L) (line_x_l + line_o_l)
```

Balance and line-indicator constraints:

```text
sum(c in C) x_c = 32
line_x_l <= x_cells[l,p]                         for l in L, p in P
line_x_l >= sum(p in P) x_cells[l,p] - 3         for l in L
line_o_l + x_cells[l,p] <= 1                     for l in L, p in P
line_o_l + sum(p in P) x_cells[l,p] >= 1         for l in L
line_x_l + line_o_l <= 1                         for l in L
```

All variables are binary. The first constraint gives exactly 32 O cells by
binary complement. The two-sided inequalities force each line indicator to be
one exactly when all four cells carry its symbol; the final inequality records
the mutual exclusivity of X and O completion.

## 76-line convention

The model counts each standard length-4 line once:

| line family | count |
|---|---:|
| axis-aligned X, Y, Z lines | 48 |
| planar diagonals in the 12 coordinate planes | 24 |
| body/space diagonals | 4 |
| **total** | **76** |

Verification matched every expected direction count (16 for each axis family,
4 for each of the six planar diagonal direction families, and 4 space
diagonals), and confirmed that all line cell ids are valid.

## Reachability assumption

The model deliberately optimizes the completed board rather than the full
move sequence. Under the stated cooperative rules, any assignment with 32 X
and 32 O can be made reachable by taking the X cells in any order on the 32 X
turns and the O cells in any order on the 32 O turns. Thus alternating play
adds the cardinality equation `sum x = 32`, but no intermediate-state,
strategy, or adversarial payoff constraints are required for this final-state
question. If the intended game instead requires optimal responses at every
move, a separate sequential game model would be needed.

## Verification evidence

The verification diagnosis reports `status: passed` and full coverage:

- 216 expected binary variables were present (64 cell variables plus 152 line indicators).
- All 64 cells were assigned; counts were X = 32 and O = 32.
- All 76 line indicators were exact; complete X lines = 0 and complete O lines = 0.
- The objective was independently recomputed as 0 and matched the reported 0.0.
- 989 expanded constraint rows were evaluated with 0 violations.
- Input checks and the dry run passed; recorded entrypoint and data hashes matched, with portable paths.

The independent objective and constraint checks and the solver execution record
are retained with the case's internal run records. No external known-optimal
value was assumed; optimality is based on the solver's `Optimal` termination for
this MILP.
