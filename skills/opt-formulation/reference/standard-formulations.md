# Standard Mathematical Formulations

## 1. Set Covering Problem (SCP)

**Sets:** $I$ = elements to cover, $J$ = available sets

**Parameters:** $c_j$ = cost of set $j$; $a_{ij} = 1$ if set $j$ covers element $i$

**Variables:** $x_j \in \{0,1\}$ — select set $j$

**Formulation:**
$$\min \sum_{j \in J} c_j x_j$$
$$\text{s.t.} \quad \sum_{j \in J} a_{ij} x_j \geq 1 \quad \forall i \in I$$
$$x_j \in \{0,1\} \quad \forall j \in J$$

---

## 2. Set Partitioning Problem (SPP)

Same as SCP except equality constraints:
$$\sum_{j \in J} a_{ij} x_j = 1 \quad \forall i \in I$$

**Domain:** Binary. Not TU in general, but often near-TU in airline crew scheduling.

---

## 3. Bin Packing Problem (BPP)

**Sets:** $I$ = items, $B = \{1, \ldots, |I|\}$ = candidate bins

**Parameters:** $w_i$ = item weight, $C$ = bin capacity

**Variables:** $x_{ib} \in \{0,1\}$ — item $i$ in bin $b$; $y_b \in \{0,1\}$ — bin $b$ used

**Formulation:**
$$\min \sum_{b \in B} y_b$$
$$\text{s.t.} \quad \sum_{b \in B} x_{ib} = 1 \quad \forall i \in I$$
$$\sum_{i \in I} w_i x_{ib} \leq C \cdot y_b \quad \forall b \in B$$
$$x_{ib} \in \{0,1\},\ y_b \in \{0,1\}$$

**Symmetry breaking:** $y_1 \geq y_2 \geq \cdots \geq y_{|B|}$

---

## 4. 0-1 Knapsack Problem

**Sets:** $I$ = items

**Parameters:** $v_i$ = value, $w_i$ = weight, $W$ = capacity

**Variables:** $x_i \in \{0,1\}$

**Formulation:**
$$\max \sum_{i \in I} v_i x_i$$
$$\text{s.t.} \quad \sum_{i \in I} w_i x_i \leq W$$
$$x_i \in \{0,1\} \quad \forall i \in I$$

**Bounded Knapsack:** $x_i \in \{0, 1, \ldots, u_i\}$ (integer, not binary).

---

## 5. Linear Assignment Problem (LAP)

**Sets:** $I$ = agents, $J$ = tasks, $|I| = |J|$

**Parameters:** $c_{ij}$ = cost

**Variables:** $x_{ij} \geq 0$ (continuous — TU guarantees integrality)

**Formulation:**
$$\min \sum_{i \in I} \sum_{j \in J} c_{ij} x_{ij}$$
$$\text{s.t.} \quad \sum_{j \in J} x_{ij} = 1 \quad \forall i \in I$$
$$\sum_{i \in I} x_{ij} = 1 \quad \forall j \in J$$
$$x_{ij} \geq 0$$

**Domain justification:** Constraint matrix is TU (bipartite graph incidence matrix).

---

## 6. Generalized Assignment Problem (GAP)

**Sets:** $I$ = agents, $J$ = tasks

**Parameters:** $c_{ij}$ = cost, $r_{ij}$ = resource consumption, $b_i$ = agent capacity

**Variables:** $x_{ij} \in \{0,1\}$

**Formulation:**
$$\min \sum_{i,j} c_{ij} x_{ij}$$
$$\text{s.t.} \quad \sum_{i \in I} x_{ij} = 1 \quad \forall j \in J$$
$$\sum_{j \in J} r_{ij} x_{ij} \leq b_i \quad \forall i \in I$$
$$x_{ij} \in \{0,1\}$$

**Domain justification:** Capacity constraints with varying $r_{ij}$ break TU. Binary required.

---

## 7. Uncapacitated Facility Location (UFLP)

**Sets:** $I$ = customers, $J$ = candidate facilities

**Parameters:** $f_j$ = fixed cost, $c_{ij}$ = unit service cost, $d_i$ = demand

**Variables:** $y_j \in \{0,1\}$ open; $x_{ij} \geq 0$ fraction served

**Formulation:**
$$\min \sum_{j \in J} f_j y_j + \sum_{i \in I} \sum_{j \in J} c_{ij} d_i x_{ij}$$
$$\text{s.t.} \quad \sum_{j \in J} x_{ij} = 1 \quad \forall i \in I$$
$$x_{ij} \leq y_j \quad \forall i \in I, j \in J$$
$$y_j \in \{0,1\},\ x_{ij} \geq 0$$

**Capacitated (CFLP):** Add $\sum_{i} d_i x_{ij} \leq q_j y_j \quad \forall j$.

---

## 8. Traveling Salesman Problem (TSP) — DFJ Formulation

**Sets:** $V = \{0,1,\ldots,n\}$ cities, $E$ = edges

**Parameters:** $c_{ij}$ = distance

**Variables:** $x_{ij} \in \{0,1\}$

**Formulation:**
$$\min \sum_{(i,j) \in E} c_{ij} x_{ij}$$
$$\text{s.t.} \quad \sum_{j:(i,j)\in E} x_{ij} = 2 \quad \forall i \in V$$
$$\sum_{i \in S, j \in S} x_{ij} \leq |S| - 1 \quad \forall S \subset V, 2 \leq |S| \leq |V|-1$$
$$x_{ij} \in \{0,1\}$$

**Note:** Subtour constraints added via lazy callback. Exponential number but manageable.

### TSP — MTZ Formulation (Compact)

Additional variables: $u_i \geq 0$ — position of city $i$ in tour

$$u_i - u_j + n \cdot x_{ij} \leq n - 1 \quad \forall i,j \in V\setminus\{0\}, i \neq j$$
$$1 \leq u_i \leq n-1 \quad \forall i \in V\setminus\{0\}$$

**Warning:** Much weaker LP relaxation than DFJ.

---

## 9. Capacitated VRP (CVRP)

**Sets:** $V = \{0\} \cup C$ (depot + customers), $K$ = vehicles

**Parameters:** $c_{ij}$, $d_i$ = demand, $Q$ = vehicle capacity

**Variables:** $x_{ijk} \in \{0,1\}$ — vehicle $k$ uses arc $(i,j)$

**Formulation:**
$$\min \sum_{k \in K} \sum_{(i,j)} c_{ij} x_{ijk}$$
$$\text{s.t.} \quad \sum_{k \in K} \sum_{j} x_{ijk} = 1 \quad \forall i \in C$$
$$\sum_{j} x_{0jk} = 1 \quad \forall k \in K \quad \text{(leave depot)}$$
$$\sum_{i} x_{i0k} = 1 \quad \forall k \in K \quad \text{(return depot)}$$
$$\sum_{j} x_{ijk} - \sum_{j} x_{jik} = 0 \quad \forall i \in C, k \in K \quad \text{(flow)}$$
$$\sum_{i \in C} d_i \sum_{j} x_{ijk} \leq Q \quad \forall k \in K$$
+ subtour elimination

---

## 10. Job-Shop Scheduling (JSSP)

**Sets:** $J$ = jobs, $M$ = machines; $O_j$ = ordered operations for job $j$

**Parameters:** $p_{jm}$ = processing time of job $j$ on machine $m$

**Variables:** $s_{jm} \geq 0$ — start time; $z_{j_1 j_2 m} \in \{0,1\}$ — $j_1$ before $j_2$ on machine $m$; $C_{\max} \geq 0$

**Formulation:**
$$\min C_{\max}$$
$$\text{s.t.} \quad s_{j,m_{k+1}} \geq s_{j,m_k} + p_{j,m_k} \quad \forall j, \text{consecutive ops } (m_k, m_{k+1})$$
$$s_{j_1,m} \geq s_{j_2,m} + p_{j_2,m} - M(1 - z_{j_1 j_2 m}) \quad \forall j_1 \neq j_2, m$$
$$s_{j_2,m} \geq s_{j_1,m} + p_{j_1,m} - M \cdot z_{j_1 j_2 m} \quad \forall j_1 \neq j_2, m$$
$$C_{\max} \geq s_{j,m_{\text{last}}} + p_{j,m_{\text{last}}} \quad \forall j$$

**Big-M:** $M = \sum_{j \in J} \max_m p_{jm} \cdot |M|$ or tighter per-machine bound.

---

## 11. Flow-Shop Scheduling (FSSP — permutation)

**Sets:** $J$ = jobs, $M = \{1,\ldots,m\}$ machines

**Parameters:** $p_{jm}$

**Variables:** $x_{jk} \in \{0,1\}$ — job $j$ in position $k$; $C_{km} \geq 0$ — completion of position $k$ on machine $m$

**Formulation:**
$$\min C_{|J|,m}$$
$$\text{s.t.} \quad \sum_{k} x_{jk} = 1 \quad \forall j \quad \text{(each job one position)}$$
$$\sum_{j} x_{jk} = 1 \quad \forall k \quad \text{(each position one job)}$$
$$C_{k,m'} \geq C_{k,m'-1} + \sum_j p_{jm'} x_{jk} \quad \forall k, m' > 1$$
$$C_{k,m'} \geq C_{k-1,m'} + \sum_j p_{jm'} x_{jk} \quad \forall k > 1, m'$$

---

## 12. Max Flow

**Sets:** $V$ = nodes, $A$ = arcs; source $s$, sink $t$

**Parameters:** $u_{ij}$ = arc capacity

**Variables:** $f_{ij} \geq 0$ (continuous — TU)

**Formulation:**
$$\max \sum_{j:(s,j) \in A} f_{sj}$$
$$\text{s.t.} \quad \sum_{j:(i,j)\in A} f_{ij} - \sum_{j:(j,i)\in A} f_{ji} = 0 \quad \forall i \in V \setminus \{s,t\}$$
$$0 \leq f_{ij} \leq u_{ij} \quad \forall (i,j) \in A$$

---

## 13. Min-Cost Flow

**Sets:** $V$ = nodes, $A$ = arcs

**Parameters:** $c_{ij}$ = unit cost, $u_{ij}$ = capacity, $b_i$ = supply (+) / demand (-)

**Variables:** $f_{ij} \geq 0$ (continuous — TU)

**Formulation:**
$$\min \sum_{(i,j) \in A} c_{ij} f_{ij}$$
$$\text{s.t.} \quad \sum_{j:(i,j)\in A} f_{ij} - \sum_{j:(j,i)\in A} f_{ji} = b_i \quad \forall i \in V$$
$$0 \leq f_{ij} \leq u_{ij} \quad \forall (i,j) \in A$$

**Requirement:** $\sum_{i \in V} b_i = 0$ (balance).

---

## 14. Transportation Problem

**Sets:** $I$ = supply nodes, $J$ = demand nodes

**Parameters:** $c_{ij}$ = unit cost, $s_i$ = supply, $d_j$ = demand

**Variables:** $x_{ij} \geq 0$ (continuous — TU)

**Formulation:**
$$\min \sum_{i \in I} \sum_{j \in J} c_{ij} x_{ij}$$
$$\text{s.t.} \quad \sum_{j \in J} x_{ij} \leq s_i \quad \forall i \in I$$
$$\sum_{i \in I} x_{ij} \geq d_j \quad \forall j \in J$$
$$x_{ij} \geq 0$$

**Balance:** If $\sum s_i > \sum d_j$, supply constraints have slack. If equal, use = constraints.

---

## 15. Blending / Diet Problem

**Sets:** $I$ = ingredients, $J$ = nutrients

**Parameters:** $c_i$ = cost/unit, $a_{ji}$ = amount of nutrient $j$ per unit of ingredient $i$, $\ell_j$ / $u_j$ = nutrient bounds

**Variables:** $x_i \geq 0$

**Formulation:**
$$\min \sum_{i \in I} c_i x_i$$
$$\text{s.t.} \quad \ell_j \leq \sum_{i \in I} a_{ji} x_i \leq u_j \quad \forall j \in J$$
$$x_i \geq 0 \quad \forall i \in I$$

Optional: $\sum_i x_i = 1$ (proportion-based blending).

---

## 16. Shortest Path (as LP)

**Sets:** $V$ = nodes, $A$ = arcs; source $s$, destination $t$

**Parameters:** $c_{ij}$ = arc cost

**Variables:** $f_{ij} \geq 0$ (continuous — TU)

**Formulation:**
$$\min \sum_{(i,j) \in A} c_{ij} f_{ij}$$
$$\text{s.t.} \quad \sum_j f_{sj} - \sum_j f_{js} = 1$$
$$\sum_j f_{tj} - \sum_j f_{jt} = -1$$
$$\sum_j f_{ij} - \sum_j f_{ji} = 0 \quad \forall i \neq s,t$$
$$f_{ij} \geq 0$$

---

## 17. Portfolio Optimization (Markowitz Mean-Variance)

**Sets:** $I$ = assets

**Parameters:** $\mu_i$ = expected return, $\Sigma$ = covariance matrix, $r_{\min}$ = target return

**Variables:** $x_i \geq 0$ — portfolio weight

**Formulation:**
$$\min \mathbf{x}^T \Sigma \mathbf{x}$$
$$\text{s.t.} \quad \sum_{i \in I} x_i = 1$$
$$\sum_{i \in I} \mu_i x_i \geq r_{\min}$$
$$x_i \geq 0 \quad \forall i \in I$$

**Type:** Convex QP. With cardinality constraints ($\sum z_i \leq K$, $x_i \leq z_i$, $z_i \in \{0,1\}$) becomes MIQP.
