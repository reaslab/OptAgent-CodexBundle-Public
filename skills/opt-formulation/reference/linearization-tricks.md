# MIP Linearization Techniques

## 1. Big-M Method (Indicator → Linear)

**When:** Force $x = 0$ when binary $y = 0$; or activate/deactivate a constraint.

**Pattern:** $x \leq M \cdot y$ where $M$ = tightest valid upper bound on $x$.

**Computing M:**
- Solve the LP relaxation without the constraint to find max possible $x$.
- Or derive from data: e.g., if $x$ = flow on arc with capacity $u$, then $M = u$.
- **Rule:** Use the smallest M that is still valid. Never use $M = 10^6$ by default.

**Variants:**
- Activate constraint when $y=1$: $\sum a_i x_i \leq b + M(1 - y)$
- Activate constraint when $y=0$: $\sum a_i x_i \leq b + M \cdot y$

---

## 2. Indicator Constraints (Solver-Native)

**When:** Solver supports them (Gurobi, CPLEX). Preferred over Big-M.

**Pattern:** `model.addGenConstrIndicator(y, 1, expr, GRB.LESS_EQUAL, rhs)`

**Advantage:** No Big-M needed. Tighter formulation. Better branching.

---

## 3. Logical AND: $z = x_1 \wedge x_2$ (all binary)

$$z \leq x_1$$
$$z \leq x_2$$
$$z \geq x_1 + x_2 - 1$$

**General (n variables):** $z \leq x_i \ \forall i$; $z \geq \sum_i x_i - (n-1)$.

---

## 4. Logical OR: $z = x_1 \vee x_2$ (all binary)

$$z \geq x_1$$
$$z \geq x_2$$
$$z \leq x_1 + x_2$$

**General (n variables):** $z \geq x_i \ \forall i$; $z \leq \sum_i x_i$.

---

## 5. Logical XOR: $z = x_1 \oplus x_2$ (all binary)

$$z \leq x_1 + x_2$$
$$z \geq x_1 - x_2$$
$$z \geq x_2 - x_1$$
$$z \leq 2 - x_1 - x_2$$

---

## 6. Logical Implication: $x_1 = 1 \Rightarrow x_2 = 1$

$$x_1 \leq x_2$$

**Conditional:** If $x_1 = 1$ then constraint $g(x) \leq 0$ must hold:
$$g(x) \leq M(1 - x_1)$$

---

## 7. At Most One (Mutex): at most one of $x_1, \ldots, x_n$ is 1

$$\sum_{i=1}^{n} x_i \leq 1$$

---

## 8. Exactly K of N

$$\sum_{i=1}^{n} x_i = K$$

---

## 9. If-Then-Else: if $y=1$ then $x = a$, else $x = b$

$$x = a \cdot y + b \cdot (1 - y) = b + (a - b) \cdot y$$

No auxiliary variables needed.

---

## 10. Absolute Value: $z = |x|$ (continuous $x$)

**Variables:** $z \geq 0$

**Constraints:**
$$z \geq x$$
$$z \geq -x$$

Works when minimizing $z$ (or $z$ has positive cost in objective). Otherwise need:

$$x = x^+ - x^-$$
$$z = x^+ + x^-$$
$$x^+, x^- \geq 0$$

---

## 11. Min: $z = \min(x_1, x_2)$

**With binary (only works if maximizing $z$ or no free objective on $z$):**
$$z \leq x_1$$
$$z \leq x_2$$

**General (with auxiliary binary $y$):**
$$z \leq x_1, \quad z \leq x_2$$
$$z \geq x_1 - M(1 - y), \quad z \geq x_2 - M \cdot y$$
$$y \in \{0,1\}$$

---

## 12. Max: $z = \max(x_1, x_2)$

**When minimizing $z$:**
$$z \geq x_1, \quad z \geq x_2$$

**General (with auxiliary binary $y$):**
$$z \geq x_1, \quad z \geq x_2$$
$$z \leq x_1 + M(1 - y), \quad z \leq x_2 + M \cdot y$$
$$y \in \{0,1\}$$

---

## 13. Product of Two Binaries: $z = x_1 \cdot x_2$

$$z \leq x_1$$
$$z \leq x_2$$
$$z \geq x_1 + x_2 - 1$$
$$z \in \{0,1\}$$

(Same as logical AND.)

---

## 14. Product of Binary and Continuous: $z = y \cdot x$ ($y$ binary, $0 \leq x \leq U$)

$$z \leq U \cdot y$$
$$z \leq x$$
$$z \geq x - U(1 - y)$$
$$z \geq 0$$

When $y = 0$: $z = 0$. When $y = 1$: $z = x$.

---

## 15. McCormick Envelopes: $z = x_1 \cdot x_2$ (both continuous, bounded)

**Given:** $L_1 \leq x_1 \leq U_1$, $L_2 \leq x_2 \leq U_2$

**Relaxation:**
$$z \geq L_1 x_2 + L_2 x_1 - L_1 L_2$$
$$z \geq U_1 x_2 + U_2 x_1 - U_1 U_2$$
$$z \leq U_1 x_2 + L_2 x_1 - U_1 L_2$$
$$z \leq L_1 x_2 + U_2 x_1 - L_1 U_2$$

**Note:** This is a relaxation, not exact. Tighten with spatial branching or piecewise linear.

---

## 16. Fixed Charge: cost $f$ if $x > 0$, plus variable cost $c \cdot x$

**Variables:** $y \in \{0,1\}$ — fixed charge indicator; $x \geq 0$

**Objective terms:** $f \cdot y + c \cdot x$

**Linking:** $x \leq U \cdot y$ (where $U$ = max possible $x$)

**Optional tightening:** $x \geq L \cdot y$ (minimum production if active)

---

## 17. Piecewise Linear Function: $z = f(x)$

**Using SOS2 (preferred):**
Breakpoints $a_0 < a_1 < \ldots < a_k$, values $f(a_0), \ldots, f(a_k)$.

$$x = \sum_{i=0}^{k} \lambda_i a_i, \quad z = \sum_{i=0}^{k} \lambda_i f(a_i)$$
$$\sum_{i=0}^{k} \lambda_i = 1, \quad \lambda_i \geq 0$$
$\lambda$ is SOS2 (at most 2 adjacent non-zero).

**Using binary segments:**
$$x = \sum_{i=1}^{k} x_i, \quad x_i \leq (a_i - a_{i-1}) y_i$$
$$y_i \geq y_{i+1} \quad \text{(fill from left)}$$

---

## 18. Conditional Constraint: if $\sum a_i x_i > b$ then $\sum c_i x_i \leq d$

Introduce binary $y$:
$$\sum a_i x_i \leq b + M_1 \cdot y$$
$$\sum c_i x_i \leq d + M_2 (1 - y)$$

When $y=0$: first constraint active (expression $\leq b$), second relaxed.
When $y=1$: first relaxed, second active.

**Caution:** This models "at least one constraint holds" (disjunction), not strict implication. For strict implication, additionally enforce $y = 1$ when the condition holds.

---

## 19. Cardinality Constraint: at most $K$ variables non-zero

**Variables:** $y_i \in \{0,1\}$ for each $x_i$

$$x_i \leq U_i \cdot y_i \quad \forall i$$
$$\sum_i y_i \leq K$$

---

## 20. SOS1 Constraint: at most one variable in set is non-zero

$$\text{SOS1}(x_1, x_2, \ldots, x_n)$$

Solver-native. Equivalent to introducing binaries: $x_i \leq U_i y_i$, $\sum y_i \leq 1$.

---

## 21. SOS2 Constraint: at most two adjacent variables non-zero

$$\text{SOS2}(\lambda_0, \lambda_1, \ldots, \lambda_k)$$

Primary use: piecewise linear functions (see #17).

---

## 22. Semi-Continuous Variable: $x = 0$ or $L \leq x \leq U$

**With binary $y$:**
$$L \cdot y \leq x \leq U \cdot y$$

Solver-native in Gurobi/CPLEX: `var.vtype = GRB.SEMICONT`.

---

## 23. Disjunctive Constraints (Either-Or)

Either $a^T x \leq b_1$ OR $c^T x \leq b_2$:

$$a^T x \leq b_1 + M_1(1 - y)$$
$$c^T x \leq b_2 + M_2 \cdot y$$
$$y \in \{0,1\}$$

---

## Big-M Computation Checklist

1. For each Big-M constraint, identify the expression being bounded.
2. Compute the maximum (or minimum) value of that expression from data bounds.
3. Use that value as M — no tighter, no looser.
4. If variable bounds are not explicit, solve a quick LP to find them.
5. Document each M value and its derivation.
6. After solving, check: if all Big-M constraints have slack > 0 when active, M may be too large.
7. Target: M should be tight enough that the LP relaxation is as strong as possible.
