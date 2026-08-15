#!/usr/bin/env python3
"""Advisory redundancy / precision audit over an OptAgent opt_spec.json.

Detects (advisory only, never blocks correctness or triggers a send-back):
  - P2: model-class precision. If the problem family is total-unimodular / LP /
        network / assignment / transportation (per the family-knowledge mapping)
        but the spec declares MILP / integer / binary variables, integerity is
        redundant. Also flags a quadratic-over-discrete model mislabeled MILP.
  - P4a: unused decision variables (declared but never referenced).
  - P4b: duplicate constraint forms.
  - P4c: duplicated constraint rows over a redundant index dimension (sparse
        index-slice column-duplication detection).

Output is a JSON object written to --output. Findings are advisory; the verifier
must NOT treat them as correctness failures or send-backs.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# Family -> expected model class (advisory family-knowledge mapping).
# TU = LP-relaxable (no integer variables needed); integer = genuinely discrete.
# ---------------------------------------------------------------------------
# Family knowledge base (C).
# Each family maps to the SET of valid model-classes (a family can be LP in one
# structural variant and MILP in another). `possible_classes` is the complete
# set; `primary_class` is the default; `class_conditions` gives the structural
# condition that selects which class applies.
# ---------------------------------------------------------------------------
FAMILY_KB: dict[str, dict[str, Any]] = {
    # ---- LP / total-unimodular (polynomial, LP relaxation integral) ----
    "transportation": {"possible_classes": ["LP"], "primary": "LP", "keywords": ["transport", "supply", "demand", "plant", "warehouse", "distribution", "shipping"]},
    "assignment": {"possible_classes": ["LP"], "primary": "LP", "keywords": ["assignment", "assign", "worker", "task", "job", "agent", "technician"]},
    "generalized_assignment": {"possible_classes": ["MILP"], "primary": "MILP", "keywords": ["generalized assignment", "gap", "agent capacity", "multi-knapsack assignment"]},
    "min_cost_flow": {"possible_classes": ["LP"], "primary": "LP", "keywords": ["min cost flow", "minimum cost flow", "network flow", "flow", "arc", "node"]},
    "max_flow": {"possible_classes": ["LP"], "primary": "LP", "keywords": ["max flow", "maximum flow", "source", "sink", "throughput"]},
    "network_flow": {"possible_classes": ["LP"], "primary": "LP", "keywords": ["flow", "network", "arc", "node", "conservation", "capacity on arc"]},
    "bipartite_matching": {"possible_classes": ["LP"], "primary": "LP", "keywords": ["bipartite matching", "bipartite", "pairing"]},
    "matching": {"possible_classes": ["MILP"], "primary": "MILP", "keywords": ["matching", "pairing", "assignment of pairs"]},
    "b_matching": {"possible_classes": ["LP", "MILP"], "primary": "MILP", "keywords": ["b matching", "degree constrained", "capacitated matching"]},
    "shortest_path": {"possible_classes": ["LP"], "primary": "LP", "keywords": ["shortest path", "shortest route", "min path", "path cost"]},
    "transshipment": {"possible_classes": ["LP"], "primary": "LP", "keywords": ["transshipment", "transit", "relay", "intermediate node"]},
    "circulation": {"possible_classes": ["LP"], "primary": "LP", "keywords": ["circulation", "conservation", "closed flow"]},
    "interval_scheduling": {"possible_classes": ["LP"], "primary": "LP", "keywords": ["interval scheduling", "interval", "time window selection", "consecutive ones"]},
    "single_item_uncapacitated_lot_sizing": {"possible_classes": ["LP", "MILP"], "primary": "MILP", "keywords": ["uncapacitated lot sizing", "wagner whitin", "single item", "no capacity"]},
    "production_mix": {"possible_classes": ["LP"], "primary": "LP", "keywords": ["production mix", "product mix", "resource allocation", "profit maximization"]},
    "blending": {"possible_classes": ["LP"], "primary": "LP", "keywords": ["blend", "blending", "mix", "composition", "grade", "feed"]},
    "diet": {"possible_classes": ["LP"], "primary": "LP", "keywords": ["diet", "nutrition", "meal", "food", "vitamin", "requirement"]},
    "linear_programming": {"possible_classes": ["LP"], "primary": "LP", "keywords": ["linear programming", "linear objective", "continuous variables"]},
    # ---- continuous convex (QP / conic / SDP) ----
    "portfolio": {"possible_classes": ["QP", "MIQP"], "primary": "QP", "keywords": ["portfolio", "markowitz", "investment", "asset", "return", "variance", "risk", "cardinality"]},
    "convex_quadratic": {"possible_classes": ["QP"], "primary": "QP", "keywords": ["convex quadratic", "quadratic programming", "quadratic objective", "least squares constraint"]},
    "conic": {"possible_classes": ["CONIC"], "primary": "CONIC", "keywords": ["conic", "socp", "second order cone", "norm constraint", "quadratic cone"]},
    "semidefinite": {"possible_classes": ["SDP"], "primary": "SDP", "keywords": ["semidefinite", "sdp", "psd", "positive semidefinite", "matrix inequality", "lmi"]},
    "least_squares": {"possible_classes": ["QP"], "primary": "QP", "keywords": ["least squares", "squared error", "regression", "l2", "residual"]},
    "curve_fitting": {"possible_classes": ["LP", "QP"], "primary": "QP", "keywords": ["curve fitting", "regression", "fit", "approximation", "chebyshev", "l1", "least squares"]},
    # ---- integer (0-1 / general-integer) ----
    "set_cover": {"possible_classes": ["MILP"], "primary": "MILP", "keywords": ["set cover", "covering", "cover", "select subsets", "coverage"]},
    "set_packing": {"possible_classes": ["MILP"], "primary": "MILP", "keywords": ["set packing", "pack", "disjoint", "conflict"]},
    "set_partitioning": {"possible_classes": ["MILP"], "primary": "MILP", "keywords": ["set partitioning", "partition", "exact cover", "crew pairing"]},
    "knapsack": {"possible_classes": ["MILP"], "primary": "MILP", "keywords": ["knapsack", "capacity", "item", "weight", "value", "bounded", "unbounded"]},
    "bin_packing": {"possible_classes": ["MILP"], "primary": "MILP", "keywords": ["bin packing", "bin", "container", "minimize bins"]},
    "facility_location": {"possible_classes": ["MILP"], "primary": "MILP", "keywords": ["facility location", "location", "open facility", "site", "uflp", "cflp", "warehouse location", "hub"]},
    "p_median": {"possible_classes": ["MILP"], "primary": "MILP", "keywords": ["p median", "p-median", "median", "facility", "locate p"]},
    "p_center": {"possible_classes": ["MILP"], "primary": "MILP", "keywords": ["p center", "p-center", "center", "max distance", "radius", "emergency"]},
    "tsp": {"possible_classes": ["MILP"], "primary": "MILP", "keywords": ["tsp", "traveling salesman", "tour", "visit each city", "subtour"]},
    "vehicle_routing": {"possible_classes": ["MILP"], "primary": "MILP", "keywords": ["vehicle routing", "vrp", "cvrp", "vrptw", "fleet", "route", "delivery", "drone"]},
    "scheduling": {"possible_classes": ["MILP"], "primary": "MILP", "keywords": ["scheduling", "schedule", "job shop", "jobshop", "flow shop", "flowshop", "single machine", "parallel machine", "makespan", "sequencing"]},
    "crew_scheduling": {"possible_classes": ["MILP"], "primary": "MILP", "keywords": ["crew scheduling", "crew pairing", "rostering", "roster", "staff", "shift", "attendance"]},
    "graph_coloring": {"possible_classes": ["MILP"], "primary": "MILP", "keywords": ["graph coloring", "coloring", "chromatic", "color", "adjacent", "conflict"]},
    "graph_partitioning": {"possible_classes": ["MILP"], "primary": "MILP", "keywords": ["graph partitioning", "partition", "min cut", "bisection", "balancing"]},
    "max_cut": {"possible_classes": ["MILP", "MIQP"], "primary": "MILP", "keywords": ["max cut", "maximum cut", "cut", "partition", "signed graph"]},
    "steiner_tree": {"possible_classes": ["MILP"], "primary": "MILP", "keywords": ["steiner tree", "steiner", "connect terminals", "minimum cost tree"]},
    "network_design": {"possible_classes": ["LP", "MILP"], "primary": "MILP", "keywords": ["network design", "fixed charge", "open arc", "capacity expansion", "telecom network", "install capacity"]},
    "lot_sizing": {"possible_classes": ["LP", "MILP"], "primary": "MILP", "keywords": ["lot sizing", "lot size", "setup", "inventory", "production plan", "batching", "capacity"]},
    "cutting_stock": {"possible_classes": ["MILP"], "primary": "MILP", "keywords": ["cutting stock", "cut stock", "cutting", "rolls", "patterns", "trim loss"]},
    "production_planning": {"possible_classes": ["LP", "MILP"], "primary": "MILP", "keywords": ["production planning", "production plan", "setup", "batch", "aggregate planning", "inventory", "capacity"]},
    "quadratic_assignment": {"possible_classes": ["MIQP"], "primary": "MIQP", "keywords": ["quadratic assignment", "qap", "location", "flow", "distance", "facility layout"]},
    "emergency_disaster_logistics": {"possible_classes": ["LP", "MILP", "CONIC"], "primary": "MILP", "keywords": ["emergency", "disaster", "relief", "evacuation", "shelter", "humanitarian", "medical supply"]},
    "telecommunication_network_design": {"possible_classes": ["LP", "MILP", "CONIC"], "primary": "MILP", "keywords": ["telecom", "telecommunication", "network design", "capacity", "routing", "hub", "survivability"]},
    # ---- nonlinear / MINLP ----
    "minlp": {"possible_classes": ["MINLP"], "primary": "MINLP", "keywords": ["minlp", "nonlinear integer", "mixed integer nonlinear", "nonconvex"]},
    "fixed_charge_nonlinear": {"possible_classes": ["MILP", "MINLP"], "primary": "MINLP", "keywords": ["fixed charge", "concave cost", "setup cost", "economies of scale", "nonlinear cost"]},
    "pooling": {"possible_classes": ["MINLP"], "primary": "MINLP", "keywords": ["pooling", "nonlinear blending", "bilinear", "quality", "grade", "streams", "refinery"]},
    "gas_energy_nonlinear": {"possible_classes": ["NLP", "MILP", "MINLP"], "primary": "MINLP", "keywords": ["gas", "energy", "power", "compressor", "weymouth", "pipeline", "nonlinear flow"]},
    # ---- stochastic / robust ----
    "two_stage_stochastic": {"possible_classes": ["LP", "MILP"], "primary": "MILP", "keywords": ["stochastic", "two stage", "recourse", "scenario", "here and now", "expected value"]},
    "robust_optimization": {"possible_classes": ["LP", "MILP", "CONIC"], "primary": "LP", "keywords": ["robust", "uncertainty", "worst case", "budget of uncertainty", "ellipsoidal", "norm bounded", "interval uncertainty"]},
    "chance_constrained": {"possible_classes": ["MILP", "CONIC", "NLP"], "primary": "MILP", "keywords": ["chance constraint", "probabilistic constraint", "reliability", "value at risk", "probability of violation"]},
    "scenario_based": {"possible_classes": ["LP", "MILP"], "primary": "MILP", "keywords": ["scenario", "scenario based", "sampling", "deterministic equivalent", "expected value", "sample average"]},
}

# Backward-compatible alias: family_key -> primary class (for single-class use)
FAMILY_CLASS: dict[str, str] = {k: v["primary"] for k, v in FAMILY_KB.items()}

# keyword hints to map an unknown family string to a known FAMILY_KB key
FAMILY_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("transport", "shipment", "warehouse", "plant", "store", "distribution"), "transportation"),
    (("generalized assignment", "gap", "agent capacity"), "generalized_assignment"),
    (("assignment", "assign", "worker", "task", "technician"), "assignment"),
    (("min cost flow", "minimum cost flow"), "min_cost_flow"),
    (("max flow", "maximum flow"), "max_flow"),
    (("flow", "network", "arc", "node", "circulation", "transshipment", "shortest path"), "network_flow"),
    (("bipartite", "matching"), "bipartite_matching"),
    (("matching",), "bipartite_matching"),
    (("interval scheduling", "interval"), "interval_scheduling"),
    (("uncapacitated lot sizing", "wagner whitin", "single item lot"), "single_item_uncapacitated_lot_sizing"),
    (("diet", "nutrition", "meal"), "diet"),
    (("blend", "mix", "blending"), "blending"),
    (("portfolio", "markowitz", "investment", "asset", "return", "cardinality"), "portfolio"),
    (("curve fit", "regression", "fit", "approximation", "chebyshev", "least squares"), "curve_fitting"),
    (("least squares", "squared error", "l2"), "least_squares"),
    (("conic", "socp", "second order cone", "norm constraint"), "conic"),
    (("semidefinite", "sdp", "psd", "matrix inequality", "lmi"), "semidefinite"),
    (("cover", "covering", "element"), "set_cover"),
    (("packing", "pack"), "set_packing"),
    (("partition",), "set_partitioning"),
    (("knapsack", "capacity", "item", "weight"), "knapsack"),
    (("bin packing",), "bin_packing"),
    (("cutting stock", "cut stock", "cutting"), "cutting_stock"),
    (("facility", "location", "site", "open", "uflp", "cflp", "hub"), "facility_location"),
    (("p median", "p-median", "locate p"), "p_median"),
    (("p center", "p-center", "radius", "emergency center"), "p_center"),
    (("routing", "vehicle", "fleet", "drone", "cvrp", "vrptw", "delivery"), "vehicle_routing"),
    (("tsp", "traveling salesman", "tour", "visit each city"), "tsp"),
    (("schedule", "job", "machine", "roster", "shift", "flowshop", "jobshop", "makespan", "sequencing"), "scheduling"),
    (("crew", "roster", "staff", "attendance", "pairing"), "crew_scheduling"),
    (("coloring", "chromatic"), "graph_coloring"),
    (("graph partit", "bisection", "balancing"), "graph_partitioning"),
    (("max cut", "maximum cut"), "max_cut"),
    (("steiner",), "steiner_tree"),
    (("network design", "fixed charge", "open arc", "capacity expansion", "telecom"), "network_design"),
    (("lot sizing", "lot size", "setup", "inventory", "batching"), "lot_sizing"),
    (("production plan", "production planning", "batch"), "production_planning"),
    (("quadratic assignment", "qap", "facility layout"), "quadratic_assignment"),
    (("emergency", "disaster", "relief", "evacuation", "shelter", "humanitarian"), "emergency_disaster_logistics"),
    (("telecom", "survivability"), "telecommunication_network_design"),
    (("minlp", "nonlinear integer", "mixed integer nonlinear", "nonconvex"), "minlp"),
    (("pooling", "bilinear", "refinery"), "pooling"),
    (("gas", "energy", "compressor", "weymouth", "pipeline"), "gas_energy_nonlinear"),
    (("stochastic", "two stage", "recourse", "scenario", "here and now"), "two_stage_stochastic"),
    (("robust", "uncertainty", "worst case", "budget of uncertainty", "interval"), "robust_optimization"),
    (("chance constraint", "probabilistic constraint", "reliability", "value at risk"), "chance_constrained"),
    (("scenario", "sample average", "deterministic equivalent"), "scenario_based"),
]


def _resolve_family(spec: dict) -> str | None:
    """Resolve the family key from spec['family'] (canonical) or, if absent,
    infer from problem_class + variable structure."""
    family_raw = str(spec.get("family", "") or "").lower()
    fl = family_raw.strip()
    if fl in FAMILY_CLASS:
        return fl
    # keyword prefix matching on the provided family string
    for kws, key in FAMILY_KEYWORDS:
        if fl and any(k in fl for k in kws):
            return key
    # fallback: infer from problem_class + variable structure
    problem_class = str(spec.get("problem_class", "")).upper()
    var_types = _var_types(spec)
    has_integer = any(t in ("binary", "integer", "bool") for t in var_types)
    quad = _objective_is_quadratic(spec)
    if quad and has_integer:
        return "qap"  # quadratic-over-discrete pattern
    if quad and not has_integer:
        return "portfolio"  # continuous quadratic pattern
    # index structure: how many index sets do the decision variables use?
    max_idx = 0
    for v in _all_variables(spec):
        idx = v.get("index_sets", []) or []
        if isinstance(idx, list):
            max_idx = max(max_idx, len(idx))
    if not has_integer:
        if max_idx <= 1:
            return "diet"  # single-index continuous (resource/mix-like)
        return "network_flow"  # multi-index continuous flow-like (LP-relaxable)
    # discrete model: binary variables with 2+ index sets often routing/assignment-like
    if max_idx >= 2:
        return "vehicle_routing"  # conservative: binary over multi-index => routing-like
    return "knapsack"  # single-discrete-index => selection/knapsack-like


def _resolve(spec: dict, set_name: str) -> list[str]:
    sets = spec.get("sets", {})
    # sets may be a dict {name: def} OR a list of {name:..., ...} defs
    if isinstance(sets, list):
        s = {}
        for item in sets:
            if isinstance(item, dict) and item.get("name") == set_name:
                s = item
                break
    elif isinstance(sets, dict):
        s = sets.get(set_name, {})
    else:
        s = {}
    if isinstance(s, dict):
        if "elements" in s:
            return [str(e) for e in s["elements"]]
        if s.get("type") == "range":
            start = int(s.get("start", 0))
            end = int(s.get("end", 0))
            return [str(i) for i in range(start, end + 1)]
        if "size" in s:
            return [str(i) for i in range(int(s["size"]))]
    return []


def _variable_count(spec: dict) -> int:
    n = 0
    for _ in spec.get("decision_variables", []) or []:
        n += 1
    for g in spec.get("variable_groups", []) or []:
        idx = g.get("index_sets", []) if isinstance(g, dict) else []
        if not idx:
            n += 1
            continue
        prod = 1
        for s in idx:
            prod *= max(1, len(_resolve(spec, s)))
        n += prod
    return n


def _all_variables(spec: dict) -> list[dict]:
    """Return all variable declarations (decision_variables + variable_groups)."""
    out = list(spec.get("decision_variables", []) or [])
    for g in spec.get("variable_groups", []) or []:
        out.append(g)
    return [v for v in out if isinstance(v, dict)]


def _var_types(spec: dict) -> list[str]:
    return [str(v.get("type", "")).lower() for v in _all_variables(spec)]


def _referenced_names(spec: dict) -> set[str]:
    ref: set[str] = set()
    # Regex to extract variable tokens from expression strings: x[i,j], x_{i}_{j}, x_ij
    _VAR_TOKEN = re.compile(r"\b([A-Za-z_]\w*)\s*[\[_]\s*[^\]]*\]|\b([A-Za-z_]\w*)")
    def from_expr(expr: str):
        if not expr:
            return
        for m in _VAR_TOKEN.finditer(expr):
            tok = m.group(1) or m.group(2)
            if tok and tok not in ("sum", "for", "all", "in", "and", "or", "not", "max", "min"):
                ref.add(tok.strip())

    def walk_terms(terms):
        for t in terms or []:
            if not isinstance(t, dict):
                continue
            v = t.get("variable") or t.get("variable_template") or t.get("var")
            if v:
                ref.add(str(v))
            else:
                # maybe lhs/rhs sub-structure
                for k in ("lhs", "rhs"):
                    sub = t.get(k)
                    if isinstance(sub, dict):
                        for s in sub.get("sum", []) or []:
                            if isinstance(s, dict) and (s.get("var") or s.get("variable")):
                                ref.add(str(s.get("var") or s.get("variable")))
                        for tk, tv in sub.items():
                            if isinstance(tv, str):
                                from_expr(tv)

    for c in spec.get("constraints", []) or []:
        if not isinstance(c, dict):
            continue
        chk = c.get("checker", {})
        if isinstance(chk, dict):
            walk_terms(chk.get("terms", []))
            for k in ("lhs", "rhs"):
                sub = chk.get(k)
                if isinstance(sub, dict):
                    for s in sub.get("sum", []) or []:
                        if isinstance(s, dict) and (s.get("var") or s.get("variable")):
                            ref.add(str(s.get("var") or s.get("variable")))
        if c.get("expression"):
            from_expr(c["expression"])

    for ic in spec.get("indexed_constraints", []) or []:
        if not isinstance(ic, dict):
            continue
        ctemp = ic.get("checker_template", {})
        if isinstance(ctemp, dict):
            walk_terms(ctemp.get("terms_template", []))
            for k in ("lhs", "rhs"):
                sub = ctemp.get(k)
                if isinstance(sub, dict):
                    for s in sub.get("sum", []) or []:
                        if isinstance(s, dict) and (s.get("var") or s.get("variable")):
                            ref.add(str(s.get("var") or s.get("variable")))
        if ic.get("expression"):
            from_expr(ic["expression"])

    obj = spec.get("objective", {}) or {}
    for t in (obj.get("linear_terms", []) or []) + (obj.get("indexed_terms", []) or []) \
            + (obj.get("term_templates", []) or []):
        if not isinstance(t, dict):
            continue
        v = t.get("variable") or t.get("variable_template") or t.get("var")
        if v:
            ref.add(str(v))
        if t.get("expression"):
            from_expr(t["expression"])
    if obj.get("expression"):
        from_expr(obj["expression"])
    return ref


def _objective_is_quadratic(spec: dict) -> bool:
    obj = spec.get("objective", {}) or {}
    return bool(obj.get("quadratic_terms") or obj.get("quadratic") or obj.get("bilinear_terms"))


def audit(spec: dict) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    problem_class = str(spec.get("problem_class", "")).upper()
    family_raw = str(spec.get("family", "") or "").lower()
    family_key = _resolve_family(spec)
    var_types = _var_types(spec)
    has_integer = any(t in ("binary", "integer", "bool") for t in var_types)
    quad = _objective_is_quadratic(spec)

    # ---- P2: model-class precision (multi-class family knowledge) ----
    kb_entry = FAMILY_KB.get(family_key) if family_key else None
    possible_classes = set(kb_entry["possible_classes"]) if kb_entry else None
    # Normalize declared class for matching (QP~MIQP~QIP are 'quadratic' family;
    # MILP covers MILP/QAP-linearized; LP covers LP/NETWORK).
    norm = problem_class
    if norm in ("QIP",):
        norm = "MIQP"
    if norm in ("NETWORK", "MIN_COST_FLOW", "MAX_FLOW"):
        norm = "LP"
    if norm in ("QAP",):
        norm = "MIQP"
    declared_in_family = possible_classes is None or norm in possible_classes

    # select the structural branch for multi-class families
    expected_branch = kb_entry["primary"] if kb_entry else None
    if kb_entry:
        possible = kb_entry["possible_classes"]
        if has_integer and "MILP" in possible and "LP" in possible:
            expected_branch = "MILP"  # discrete branch of LP/MILP family
        if quad and "MIQP" in possible:
            expected_branch = "MIQP"  # quadratic branch
        elif quad and "QP" in possible:
            expected_branch = "QP"

    if quad and has_integer and problem_class in ("MILP", "LP", "QP"):
        findings.append({
            "type": "P2_quadratic_over_discrete_misclassified",
            "severity": "hint",
            "certainty": "possible",
            "message": (
                f"Objective is quadratic AND variables are discrete, but problem_class={problem_class}. "
                f"This may be a QIP/MIQP problem; verify the class and that the quadratic objective is not "
                f"unnecessarily linearized into redundant index-boolean products."
            ),
            "expected_class": "MIQP",
        })
    if not declared_in_family and family_key:
        findings.append({
            "type": "P2_model_class_precision",
            "severity": "hint",
            "certainty": "possible",
            "message": (
                f"Family '{family_raw}' admits model class(es) {sorted(possible_classes or [])} "
                f"(primary {expected_branch}), but problem_class={problem_class}. "
                f"The declared class is not in the family's expected set; verify the structural form "
                f"before changing anything."
            ),
            "expected_class": expected_branch,
            "possible_classes": sorted(possible_classes or []),
        })

    # ---- P4a: unused variables ----
    referenced = _referenced_names(spec)
    declared_names: set[str] = set()
    for v in _all_variables(spec):
        if v.get("name"):
            declared_names.add(v["name"])
    # A variable is "used" if it appears literally, OR (for indexed groups) any
    # referenced template/name shares its base name (e.g. group 'x' used via
    # template 'x_{i}_{j}' or term 'x[i][j]').
    def is_used(name: str) -> bool:
        if name in referenced:
            return True
        for ref in referenced:
            if ref.startswith(name + "_") or ref.startswith(name + "["):
                return True
        return False

    unused = sorted(n for n in declared_names if not is_used(n))
    if unused:
        findings.append({
            "type": "P4_unused_variables",
            "severity": "hint",
            "certainty": "deterministic",
            "message": f"Decision variables declared but never referenced in the objective or any constraint: {unused}. These are unused by construction.",
            "variables": unused,
        })

    # ---- P4b: duplicate constraint forms ----
    forms: list[str] = []
    for c in spec.get("constraints", []) or []:
        if isinstance(c, dict):
            forms.append(str(c.get("form", "")).strip())
    seen: set[str] = set()
    dups: list[str] = []
    for f in forms:
        if f and f in seen:
            dups.append(f)
        seen.add(f)
    if dups:
        findings.append({
            "type": "P4_duplicate_constraints",
            "severity": "hint",
            "certainty": "possible",
            "message": f"Duplicate constraint forms detected ({len(dups)}): {list(set(dups))[:5]}. Verify these are not distinct entities that each require their own constraint.",
            "forms": list(set(dups))[:10],
        })

    # ---- P4c: index-slice column-duplication (sparse) ----
    index_slice_groups = _index_slice_duplication(spec)
    if index_slice_groups:
        findings.append({
            "type": "P4_redundant_index_dimension",
            "severity": "hint",
            "certainty": "possible",
            "message": (
                f"Variable families share an identical constraint-incidence signature across an index slice "
                f"({len(index_slice_groups)} base families). This MAY indicate a redundant index dimension "
                f"(e.g. arc x vehicle when the vehicle dimension is determined by conservation), but it may also "
                f"be a genuine indexed family over distinct entities. Verify the index is an independent decision "
                f"dimension before any change."
            ),
            "bases": index_slice_groups[:10],
        })

    return {
        "audited": True,
        "problem_class": problem_class,
        "family": family_raw,
        "family_key": family_key,
        "expected_class_by_family": expected_branch,
        "possible_classes_by_family": sorted(possible_classes) if possible_classes else None,
        "has_integer_variables": has_integer,
        "objective_is_quadratic": quad,
        "estimated_variable_count": _variable_count(spec),
        "findings": findings,
        "note": "Advisory only. These findings do NOT change validation.status and do NOT justify a send-back.",
    }


def _index_slice_duplication(spec: dict) -> list[str]:
    """Sparse index-slice column-duplication: group variables by their sorted
    (constraint-row, coefficient) incidence signature (O(nonzeros) via single pass
    over constraints). Flag base families whose members differ only by a trailing
    numeric index. Returns the base names. Handles real opt_spec structures
    (checker.terms, checker_template terms_template, lhs.sum[].var, expression)."""
    def var_terms(struct: dict) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        # terms / terms_template
        for lst in ("terms", "terms_template"):
            for t in struct.get(lst, []) or []:
                if not isinstance(t, dict):
                    continue
                vn = t.get("variable", "") or t.get("variable_template", "")
                raw_cf = t.get("coefficient", 0)
                try:
                    cf = float(raw_cf or 0)
                except (TypeError, ValueError):
                    continue  # non-numeric coefficient (e.g. parameter ref) -> skip
                if vn and abs(cf) > 1e-12:
                    out.append((vn, cf))
        # lhs.sum[].var / rhs
        for side in ("lhs", "rhs"):
            sub = struct.get(side)
            if isinstance(sub, dict):
                for s in sub.get("sum", []) or []:
                    if isinstance(s, dict) and (s.get("var") or s.get("variable")):
                        out.append((str(s.get("var") or s.get("variable")), 1.0))
        return out

    sig: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for c in spec.get("constraints", []) or []:
        if not isinstance(c, dict):
            continue
        chk = c.get("checker", {})
        if isinstance(chk, dict):
            for vn, cf in var_terms(chk):
                sig[vn].append((c.get("name", ""), cf))
    for ic in spec.get("indexed_constraints", []) or []:
        if not isinstance(ic, dict):
            continue
        ctemp = ic.get("checker_template", {})
        if isinstance(ctemp, dict):
            for vn, cf in var_terms(ctemp):
                sig[vn].append((ic.get("name", ""), cf))
    # group by signature
    groups: dict[tuple, list[str]] = defaultdict(list)
    for vn, s in sig.items():
        groups[tuple(sorted(s))].append(vn)
    bases: list[str] = []
    for s, names in groups.items():
        if len(names) <= 1:
            continue
        base_map: dict[str, list[str]] = defaultdict(list)
        for nm in names:
            b = re.sub(r"_\d+$", "", nm)
            base_map[b].append(nm)
        for b, sub in base_map.items():
            if len(sub) > 1:
                bases.append(b)
    return bases


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--spec", required=True, help="path to opt_spec.json")
    ap.add_argument("--output", required=True, help="path to write the audit JSON")
    args = ap.parse_args()

    spec_path = Path(args.spec)
    out_path = Path(args.output)
    # Robustness: never crash the verifier's tool call. On any failure, emit a
    # structured error result so the verifier can record unsupported/not_applicable.
    try:
        if not spec_path.exists():
            result = {"audited": False, "error": f"spec not found: {spec_path}", "findings": []}
        else:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            if not isinstance(spec, dict):
                raise ValueError("opt_spec.json root must be an object")
            result = audit(spec)
    except Exception as exc:  # noqa: BLE001 - robustness for the verifier
        import traceback
        result = {"audited": False, "error": f"audit failed: {exc}", "traceback": traceback.format_exc(), "findings": []}

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{result.get('error', '')}; output write failed: {exc}"
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
