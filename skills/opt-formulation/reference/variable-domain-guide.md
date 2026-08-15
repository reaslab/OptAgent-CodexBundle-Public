# Variable Domain Selection Guide

## Decision Tree

```
Is it a yes/no decision?
├── YES → Binary (x in {0,1})
│   Examples: open facility, select item, assign pair
│
└── NO → Is the quantity naturally integer?
    ├── YES → Does the problem have TU structure?
    │   ├── YES → Use continuous (LP gives integer automatically)
    │   │   TU problems: assignment, transportation, network flow,
    │   │   shortest path, bipartite matching
    │   └── NO → Integer (x in Z+)
    │       Examples: number of trucks, employees, batches
    │
    └── NO → Continuous (x >= 0 or x in R)
        Examples: flow rate, weight, proportion, time
```

## TU (Total Unimodularity) Rules

A constraint matrix A is TU if every square submatrix has determinant in {-1, 0, 1}.

**Sufficient conditions (Ghouila-Houri):** Each column has at most two non-zero entries which are +1 and -1, and rows can be partitioned into two sets such that same-set entries in a column have opposite signs, different-set entries have same signs.

**Automatically TU problem classes:**

| Problem | Why TU | Implication |
|---|---|---|
| Assignment | Node-arc incidence of bipartite graph | Declare x_{ij} continuous |
| Transportation | Same structure as assignment | Declare x_{ij} continuous |
| Min-cost flow | Node-arc incidence of directed graph | Declare f_{ij} continuous |
| Max flow | Special case of min-cost flow | Declare f_{ij} continuous |
| Shortest path | Special case of min-cost flow | Declare f_{ij} continuous |

**NOT TU (must use integer/binary):**

- Set covering/partitioning (general 0-1 matrix)
- Knapsack, bin packing
- GAP (generalized assignment)
- Facility location (linking constraints break TU)
- TSP, VRP, scheduling

## Ambiguity Detection Rules

Flag as **ambiguity** (ask user) when:
1. Problem says "number of X" but X could be fractional in practice (e.g., "number of hours" — integer or continuous?)
2. Proportions that might need to be exact fractions vs approximate
3. "At most K types" — cardinality constraint needs binary indicators; confirm K

Flag as **assumption** (document and proceed) when:
4. Standard convention applies (e.g., "assign workers" implies binary)
5. TU structure makes domain choice irrelevant
6. Physical indivisibility is obvious (e.g., vehicles, people)

## Common Domain Mistakes

| Mistake | Consequence | Fix |
|---|---|---|
| Binary for TU assignment | Slower solve, no benefit | Use continuous |
| Continuous for item counts | Fractional trucks/people | Use integer |
| Integer for proportions | Unnecessary restriction | Use continuous |
| Forgetting lower bound 0 | Negative flows/quantities | Add x >= 0 |
| Missing upper bound | Unbounded variables | Add natural upper bounds |

## Quick Reference Table

| Decision Type | Domain | Example |
|---|---|---|
| Select / open / close | Binary | Open warehouse y_j |
| Assign pair | Binary | Assign worker to task x_{ij} |
| Route edge | Binary | Traverse arc x_{ij} |
| Sequence order | Binary | Job i before j: y_{ij} |
| Count items | Integer | Number of trucks k |
| Lot size | Integer | Production batch q |
| Flow / rate | Continuous | Flow on arc f_{ij} |
| Proportion / fraction | Continuous | Blend ratio x_i |
| Time / duration | Continuous | Start time s_j |
| Amount / quantity | Continuous | Kg of ingredient x_i |
