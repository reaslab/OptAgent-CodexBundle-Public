#!/usr/bin/env python3
"""Deterministic opt_spec.json finalization: fill `family` if missing.

The formulator LLM may omit `family` despite the spec_validator requiring it.
This step runs after the formulator writes opt_spec.json and guarantees the
`family` field is present (inferred from problem_class + variable structure
via the same logic as redundancy_audit._resolve_family). It is idempotent:
if `family` is already set, it is left unchanged.

Usage:
  python3 spec_finalize.py --spec .optagent/cases/{case_id}/formulation/opt_spec.json
  Inside OptAgent, run via: python .agents/tools/spec_finalize.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Family knowledge (mirrors redundancy_audit, kept minimal here to avoid import
# coupling; redundancy_audit.py is the canonical source).
TU_FAMILIES = {"transportation", "assignment", "min_cost_flow", "max_flow",
               "network_flow", "bipartite_matching", "shortest_path",
               "transshipment", "circulation", "diet", "blending", "production_mix"}
INTEGER_FAMILIES = {"set_cover", "set_packing", "set_partitioning", "knapsack",
                    "bin_packing", "facility_location", "routing", "tsp",
                    "scheduling", "graph_coloring", "lot_sizing",
                    "production_planning", "cutting_stock", "qap"}


def infer_family(spec: dict) -> str | None:
    """Infer a canonical family name from problem_class + variable structure."""
    pc = str(spec.get("problem_class", "")).upper()
    vars_ = []
    for g in spec.get("variable_groups", []) or []:
        if isinstance(g, dict):
            vars_.append(g)
    for v in spec.get("decision_variables", []) or []:
        if isinstance(v, dict):
            vars_.append(v)
    has_integer = any(str(v.get("type", "")).lower() in ("binary", "integer", "bool")
                      for v in vars_)
    max_idx = 0
    for v in vars_:
        idx = v.get("index_sets", []) or []
        if isinstance(idx, list):
            max_idx = max(max_idx, len(idx))
    # quadratic objective => portfolio/qap
    obj = spec.get("objective", {}) or {}
    quad = bool(obj.get("quadratic_terms") or obj.get("quadratic") or obj.get("bilinear_terms"))
    if quad and has_integer:
        return "qap"
    if quad and not has_integer:
        return "portfolio"
    if not has_integer:
        if max_idx <= 1:
            return "diet"
        return "network_flow"
    if max_idx >= 2:
        return "vehicle_routing"
    return "knapsack"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--spec", required=True)
    args = ap.parse_args()

    spec_path = Path(args.spec)
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"read/parse failed: {exc}"}, ensure_ascii=False))
        return 1

    had_family = bool(spec.get("family"))
    if not had_family:
        fam = infer_family(spec)
        if fam:
            spec["family"] = fam
            spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "family": spec.get("family"),
                      "filled": (not had_family and bool(spec.get("family")))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
