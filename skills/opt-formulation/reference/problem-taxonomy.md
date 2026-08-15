# Optimization Problem Taxonomy

## Model Class Selection (disjoint boundaries)

Choose the simplest correct class from the OBJECTIVE and CONSTRAINT algebraic form (linear vs quadratic/conic/nonlinear; continuous vs discrete), NOT from the problem name:

| Class | Boundary | Example families |
|---|---|---|
| **LP** | all linear + all continuous + total-unimodular/network/assignment (LP relaxation integral) | transportation, assignment, min-cost flow, max flow, shortest path, transshipment, circulation, bipartite matching, blending, diet, production mix |
| **Continuous convex (QP / conic / SDP)** | objective/constraints quadratic/conic/semidefinite + convex + continuous (no integer vars) | portfolio (Markowitz), convex QP, SOCP, SDP |
| **MILP** | linear objective & constraints + genuine discrete (0-1 / general-integer) decision | set cover/packing/partitioning, knapsack, bin packing, facility location, routing (TSP/VRP), scheduling, lot-sizing, graph coloring, cutting stock |
| **QIP / MIQP** | quadratic objective + discrete variables | quadratic assignment (QAP), cardinality-constrained portfolio |
| **MINLP / CP-SAT / NLP** | discrete+nonconvex-nonlinear, pure nonconvex nonlinear, or CP structure | — |

Key rules:
- A **quadratic objective over discrete variables ⇒ QIP/MIQP**, never MILP (the QAP-class error). Do not linearize a quadratic objective into redundant index-boolean products.
- A **TU / network / assignment / transportation** family needs NO integer variables (LP relaxation is integral).
- **Variable-minimality (index-as-decision-dimension):** index a variable only over independent decision dimensions. Do NOT index a flow/load variable over an extra dimension (e.g. arc × vehicle) when conservation/identical-vehicles determines that dimension. One decision role ⇒ one variable family with the minimal index set.
- **Integer vs binary:** capacity/quantity decisions (how many units/modules) are general-integer or continuous, NOT binary; only take/not/open/select decisions are binary.

## Family → Expected Class (complete knowledge base)

A family can admit MULTIPLE valid classes depending on its structural variant. Use the structural condition to pick the class; do not declare a class outside the family's set.

| Family | Possible classes | Structural condition |
|---|---|---|
| transportation | LP | always (bipartite supply–demand, TU) |
| assignment | LP | linear assignment (TU) |
| generalized assignment (GAP) | MILP | agent capacity rows (not TU) |
| min-cost flow / max flow / network flow / transshipment / circulation / shortest path | LP | node-arc incidence (TU) |
| bipartite matching | LP | bipartite incidence (TU) |
| matching (general) | MILP | non-bipartite, blossom/odd-set needed |
| b-matching | LP / MILP | bipartite → LP; general → MILP |
| interval scheduling | LP | consecutive-ones (TU) |
| single-item uncapacitated lot-sizing | LP / MILP | no setup → LP (TDI); fixed-charge setup → MILP |
| production mix / blending / diet / linear programming | LP | continuous linear |
| portfolio (Markowitz) | QP / MIQP | no cardinality → QP; cardinality/round-lot → MIQP |
| convex quadratic / least squares | QP | convex continuous quadratic |
| conic (SOCP) | CONIC | second-order cone |
| semidefinite | SDP | PSD / LMI |
| curve fitting | LP / QP | Chebyshev/L1 → LP; least-squares → QP |
| set cover / set packing / set partitioning | MILP | binary coverage/packing/partition |
| knapsack | MILP | 0-1 or bounded/unbounded integer |
| bin packing / cutting stock | MILP | integer bins / patterns |
| facility location (UFLP/CFLP) / p-median / p-center | MILP | binary open / locate p |
| TSP / vehicle routing (CVRP/VRPTW) | MILP | binary arc/route |
| scheduling (job-shop/flow-shop/single/parallel) / crew scheduling | MILP | binary sequencing / roster |
| graph coloring / graph partitioning / Steiner tree | MILP | binary color / partition / edge |
| max cut | MILP / MIQP | linearized → MILP; native 0-1 QP → MIQP |
| network design | LP / MILP | no fixed charge, single-commodity → LP (TU); fixed-charge / multi-commodity → MILP |
| lot sizing (general) | LP / MILP | single-item uncapacitated no-setup → LP; capacitated/multi-level → MILP |
| production planning | LP / MILP | no setup → LP; setup/fixed-charge → MILP |
| quadratic assignment (QAP) | MIQP | native 0-1 QP (linearization gives MILP) |
| emergency / disaster logistics | LP / MILP / CONIC | pure flow → LP; fixed-charge/integer → MILP; robust min-max → CONIC |
| telecom network design | LP / MILP / CONIC | uncapacitated single-commodity → LP; fixed-charge/multi-commodity → MILP; robust → CONIC |
| MINLP | MINLP | discrete + nonlinear |
| fixed-charge nonlinear / pooling | MILP / MINLP | linearized → MILP; nonlinear retained → MINLP |
| gas/energy nonlinear | NLP / MILP / MINLP | continuous → NLP; piecewise-linear → MILP; nonlinear+discrete → MINLP |
| two-stage stochastic / scenario-based | LP / MILP | per-stage integrality |
| robust optimization | LP / MILP / CONIC | polyhedral → LP; ellipsoidal → CONIC; + integer → MILP |
| chance-constrained | MILP / CONIC / NLP | reformulation-dependent |

## Hierarchy

```
Optimization
├── Combinatorial / Discrete
│   ├── Covering & Partitioning
│   │   ├── Set Covering
│   │   └── Set Partitioning
│   ├── Packing & Knapsack
│   │   ├── Bin Packing
│   │   ├── 0-1 Knapsack
│   │   └── Bounded/Unbounded Knapsack
│   ├── Assignment
│   │   ├── Linear Assignment (bipartite)
│   │   └── Generalized Assignment (GAP)
│   ├── Routing
│   │   ├── TSP
│   │   └── VRP (CVRP, VRPTW, …)
│   ├── Scheduling
│   │   ├── Job-Shop (JSSP)
│   │   └── Flow-Shop (FSSP)
│   └── Facility Location
│       ├── Uncapacitated (UFLP)
│       └── Capacitated (CFLP)
├── Network Flow
│   ├── Max Flow
│   ├── Min-Cost Flow
│   ├── Shortest Path
│   └── Transportation / Transshipment
├── Continuous / LP-based
│   ├── Blending / Diet
│   └── Portfolio Optimization
└── Mixed (LP + integer)
    └── Fixed-charge, lot-sizing, etc.
```

---

## Problem Details

### 1. Set Covering

| Field | Detail |
|---|---|
| **Sets** | I = items to cover, J = candidate sets |
| **Parameters** | c_j = cost of set j; a_{ij} = 1 if set j covers item i |
| **Variables** | x_j in {0,1} — select set j |
| **Objective** | min sum_j c_j x_j |
| **Constraints** | sum_j a_{ij} x_j >= 1 for all i (each item covered) |
| **Properties** | NP-hard. Greedy gives O(ln n) approx. LP relaxation often fractional. |
| **Pitfalls** | Confusing covering (>=1) with partitioning (=1). Forgetting binary domain. |

### 2. Set Partitioning

| Field | Detail |
|---|---|
| **Sets/Params/Vars** | Same as set covering |
| **Objective** | min sum_j c_j x_j |
| **Constraints** | sum_j a_{ij} x_j = 1 for all i (exactly one set covers each item) |
| **Properties** | NP-hard. Constraint matrix often near-TU in crew scheduling. |
| **Pitfalls** | More likely infeasible than covering. Ensure feasibility before solving. |

### 3. Bin Packing

| Field | Detail |
|---|---|
| **Sets** | I = items, B = candidate bins |
| **Parameters** | w_i = item weight, C = bin capacity |
| **Variables** | x_{ib} in {0,1} assign item i to bin b; y_b in {0,1} open bin b |
| **Objective** | min sum_b y_b |
| **Constraints** | sum_b x_{ib} = 1 (each item assigned); sum_i w_i x_{ib} <= C y_b (capacity) |
| **Properties** | NP-hard. High symmetry among bins — use symmetry-breaking (order bins). |
| **Pitfalls** | Symmetry causes weak LP bounds. Add y_1 >= y_2 >= ... or branch-and-price. |

### 4. 0-1 Knapsack

| Field | Detail |
|---|---|
| **Sets** | I = items |
| **Parameters** | v_i = value, w_i = weight, W = capacity |
| **Variables** | x_i in {0,1} — take item i |
| **Objective** | max sum_i v_i x_i |
| **Constraints** | sum_i w_i x_i <= W |
| **Properties** | NP-hard but pseudo-polynomial DP. LP relaxation tight with one fractional item. |
| **Pitfalls** | Forgetting that bounded/unbounded variants need integer (not binary) vars. |

### 5. Assignment Problem

| Field | Detail |
|---|---|
| **Sets** | I = agents, J = tasks (|I|=|J| for balanced) |
| **Parameters** | c_{ij} = cost of assigning agent i to task j |
| **Variables** | x_{ij} in {0,1} |
| **Objective** | min sum_{ij} c_{ij} x_{ij} |
| **Constraints** | sum_j x_{ij} = 1 (each agent); sum_i x_{ij} = 1 (each task) |
| **Properties** | **TU** — LP relaxation always integer. Solvable in O(n^3) (Hungarian). |
| **Pitfalls** | Unbalanced case needs dummy agents/tasks. GAP (capacitated) is NOT TU. |

### 6. Facility Location (UFLP/CFLP)

| Field | Detail |
|---|---|
| **Sets** | I = customers, J = candidate facilities |
| **Parameters** | f_j = fixed cost, c_{ij} = service cost, d_i = demand, q_j = capacity (CFLP) |
| **Variables** | y_j in {0,1} open facility; x_{ij} >= 0 fraction of demand i served by j |
| **Objective** | min sum_j f_j y_j + sum_{ij} c_{ij} d_i x_{ij} |
| **Constraints** | sum_j x_{ij} = 1; x_{ij} <= y_j; sum_i d_i x_{ij} <= q_j y_j (CFLP) |
| **Properties** | NP-hard. Strong LP relaxation with x_{ij} <= y_j. |
| **Pitfalls** | Omitting linking constraint x_{ij} <= y_j. Units on demand vs capacity. |

### 7. TSP

| Field | Detail |
|---|---|
| **Sets** | V = cities, E = edges |
| **Parameters** | c_{ij} = travel cost |
| **Variables** | x_{ij} in {0,1} — traverse edge (i,j) |
| **Objective** | min sum_{ij} c_{ij} x_{ij} |
| **Constraints** | sum_j x_{ij} = 2 (degree); subtour elimination (SEC or MTZ) |
| **Properties** | NP-hard. SEC exponential but added via lazy callbacks. MTZ compact but weak LP. |
| **Pitfalls** | MTZ gives very weak LP relaxation. Prefer SEC with lazy cuts. |

### 8. VRP (Capacitated)

| Field | Detail |
|---|---|
| **Sets** | V = depot + customers, K = vehicles |
| **Parameters** | c_{ij}, d_i = demand, Q = vehicle capacity |
| **Variables** | x_{ijk} in {0,1} — vehicle k traverses (i,j) |
| **Objective** | min sum_{ijk} c_{ij} x_{ijk} |
| **Constraints** | Each customer visited exactly once; vehicle starts/ends at depot; capacity per vehicle; subtour elimination |
| **Properties** | NP-hard. Column generation / branch-and-price for large instances. |
| **Pitfalls** | Forgetting depot return. Weak formulations without capacity cuts. |

### 9. Job-Shop Scheduling (JSSP)

| Field | Detail |
|---|---|
| **Sets** | J = jobs, M = machines, O = operations |
| **Parameters** | p_{jm} = processing time of job j on machine m; routing order per job |
| **Variables** | s_{jm} >= 0 start time; y_{j1,j2,m} in {0,1} ordering on machine m; C_max >= 0 |
| **Objective** | min C_max (makespan) |
| **Constraints** | Precedence within job; disjunctive on machines (Big-M or indicator); C_max >= s_{jm} + p_{jm} |
| **Properties** | NP-hard even for 2 machines. Disjunctive graph representation. |
| **Pitfalls** | Big-M too large weakens LP. Use tight M = sum of all processing times on that machine. |

### 10. Flow-Shop Scheduling (FSSP)

| Field | Detail |
|---|---|
| **Sets** | J = jobs, M = machines (fixed order m=1..M) |
| **Parameters** | p_{jm} = processing time |
| **Variables** | s_{jm} >= 0 start time; sequence variables |
| **Objective** | min C_max |
| **Constraints** | s_{j,m+1} >= s_{jm} + p_{jm} (job order); machine processes one job at a time |
| **Properties** | Permutation flow-shop: same order on all machines. 2-machine solved by Johnson's rule. |
| **Pitfalls** | Assuming permutation schedule is optimal (not always for m>=3). |

### 11. Max Flow

| Field | Detail |
|---|---|
| **Sets** | V = nodes, E = arcs; s = source, t = sink |
| **Parameters** | u_{ij} = arc capacity |
| **Variables** | f_{ij} >= 0 flow on arc (i,j) |
| **Objective** | max sum_j f_{sj} |
| **Constraints** | Flow conservation; 0 <= f_{ij} <= u_{ij} |
| **Properties** | **TU** — LP gives integer when capacities integer. Poly-time algorithms exist. |
| **Pitfalls** | Forgetting anti-parallel arcs in directed formulation. |

### 12. Min-Cost Flow

| Field | Detail |
|---|---|
| **Sets** | V = nodes, E = arcs |
| **Parameters** | c_{ij} = unit cost, u_{ij} = capacity, b_i = supply (>0) / demand (<0) |
| **Variables** | f_{ij} >= 0 |
| **Objective** | min sum_{ij} c_{ij} f_{ij} |
| **Constraints** | Flow conservation: sum_j f_{ij} - sum_j f_{ji} = b_i; 0 <= f_{ij} <= u_{ij} |
| **Properties** | **TU**. Subsumes assignment, transportation, shortest path. |
| **Pitfalls** | Supply/demand must balance (sum b_i = 0). Add slack node if unbalanced. |

### 13. Transportation Problem

| Field | Detail |
|---|---|
| **Sets** | I = supply nodes, J = demand nodes |
| **Parameters** | c_{ij} = unit transport cost, s_i = supply, d_j = demand |
| **Variables** | x_{ij} >= 0 |
| **Objective** | min sum_{ij} c_{ij} x_{ij} |
| **Constraints** | sum_j x_{ij} <= s_i; sum_i x_{ij} >= d_j |
| **Properties** | **TU**. LP always integer if supplies/demands integer. Special simplex. |
| **Pitfalls** | Must balance: total supply >= total demand (or add dummy). |

### 14. Blending / Diet Problem

| Field | Detail |
|---|---|
| **Sets** | I = ingredients, J = nutrients |
| **Parameters** | c_i = cost, a_{ji} = nutrient j per unit of ingredient i, lo_j/hi_j = bounds |
| **Variables** | x_i >= 0 amount of ingredient i |
| **Objective** | min sum_i c_i x_i |
| **Constraints** | lo_j <= sum_i a_{ji} x_i <= hi_j for all j; optional: sum x_i = 1 (proportions) |
| **Properties** | Pure LP. Always feasible if bounds are consistent. |
| **Pitfalls** | Mixing absolute and proportional constraints. Unit mismatches. |

### 15. Portfolio Optimization (Markowitz)

| Field | Detail |
|---|---|
| **Sets** | I = assets |
| **Parameters** | mu_i = expected return, Sigma = covariance matrix, r = target return |
| **Variables** | x_i >= 0 portfolio weight |
| **Objective** | min x^T Sigma x (variance) |
| **Constraints** | sum_i x_i = 1; sum_i mu_i x_i >= r; optional cardinality/sector limits |
| **Properties** | QP (convex). With cardinality constraints becomes MIQP. |
| **Pitfalls** | Sigma must be positive semi-definite. Cardinality constraints make it NP-hard. |
