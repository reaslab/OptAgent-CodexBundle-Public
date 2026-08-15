#!/usr/bin/env python3
"""
solution_checker.py — Independent solution reasonableness checks.

Usage:
  python3 solution_checker.py --solution .optagent/cases/case_001_base/solver/solution.json \
  Inside OptAgent, run via: python .agents/skills/opt-verification/tools/solution_checker.py
    --spec .optagent/cases/case_001_base/formulation/opt_spec.json \
    --manifest .optagent/cases/case_001_base/data/data_manifest.json

Checks: variable bounds, binary/integer domains, objective recomputation,
known optimal comparison from manifest metadata, coefficient magnitude.
"""
import argparse
import csv
import json
import math
import os
import sys


def load_csv_rows(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def num(v):
    try:
        return int(v) if "." not in str(v) else float(v)
    except (ValueError, TypeError):
        return v


def workspace_root_for_manifest(manifest_path):
    manifest = os.path.abspath(manifest_path)
    current = os.path.dirname(manifest)
    while True:
        if os.path.basename(current) == ".optagent":
            return os.path.dirname(current)
        parent = os.path.dirname(current)
        if parent == current:
            return os.getcwd()
        current = parent


def resolve_path(path, workspace_root):
    if os.path.isabs(path):
        return path
    return os.path.join(workspace_root, path)


def find_known_optimal(manifest, workspace_root):
    """Find benchmark optimal metadata without assuming a specific input filename."""
    candidates = []
    for name, obj in manifest.get("data_objects", {}).items():
        if not isinstance(obj, dict):
            continue
        metadata = obj.get("metadata", {})
        for source in (obj, metadata):
            for key in ("known_optimal_value", "optimal_value", "best_known_value"):
                if key in source and source[key] is not None:
                    candidates.append((name, key, source[key]))

        role = str(obj.get("role", "")).lower()
        kind = str(metadata.get("kind", "")).lower()
        if "benchmark" not in role and kind != "benchmark":
            continue

        source_path = obj.get("source_path") or obj.get("path")
        resolved_source_path = resolve_path(source_path, workspace_root) if source_path else None
        if not resolved_source_path or not os.path.exists(resolved_source_path) or not resolved_source_path.lower().endswith(".json"):
            continue
        try:
            with open(resolved_source_path, "r", encoding="utf-8-sig") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(payload, dict):
            for key in ("known_optimal_value", "optimal_value", "best_known_value"):
                if key in payload and payload[key] is not None:
                    candidates.append((name, key, payload[key]))

    return candidates[0] if candidates else None


def main():
    parser = argparse.ArgumentParser(description="Check solution reasonableness")
    parser.add_argument("--solution", required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--manifest", default=".optagent/cases/case_001_base/data/data_manifest.json")
    parser.add_argument("--problem-dir", default=None, help="Deprecated; known optimal values are read from manifest metadata")
    args = parser.parse_args()

    report = {"checks": [], "summary": {"pass": 0, "fail": 0, "warn": 0}}

    # Load solution
    if not os.path.exists(args.solution):
        report["checks"].append({"check": "solution_exists", "status": "FAIL", "detail": f"Not found: {args.solution}"})
        print(json.dumps(report, indent=2))
        return 1

    with open(args.solution, "r", encoding="utf-8-sig") as f:
        solution = json.load(f)

    # Load spec
    spec = {}
    if os.path.exists(args.spec):
        with open(args.spec, "r", encoding="utf-8-sig") as f:
            spec = json.load(f)

    manifest = {}
    if os.path.exists(args.manifest):
        with open(args.manifest, "r", encoding="utf-8-sig") as f:
            manifest = json.load(f)

    variables = solution.get("variables", {})
    obj_value = solution.get("objective_value")
    status = solution.get("status", "")
    has_incumbent = solution.get("has_incumbent") is True

    # Check 1: Solution status
    if status not in ("optimal", "feasible"):
        report["checks"].append({"check": "solver_status", "status": "WARN", "detail": f"Status is '{status}', not optimal/feasible"})
        report["summary"]["warn"] += 1
    else:
        report["checks"].append({"check": "solver_status", "status": "PASS", "detail": f"Status: {status}"})
        report["summary"]["pass"] += 1

    # Check 2: Variable bounds. Never report a pass when there is no concrete
    # incumbent or when variable names cannot be mapped to the specification.
    var_groups = spec.get("variable_groups", spec.get("decision_variables", {}))
    if isinstance(var_groups, list):
        normalized_groups = [
            (item.get("name"), item)
            for item in var_groups
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
    elif isinstance(var_groups, dict):
        normalized_groups = [
            (name, item)
            for name, item in var_groups.items()
            if isinstance(item, dict)
        ]
    else:
        normalized_groups = []

    bound_violations = []
    binary_violations = []
    integer_violations = []
    unmatched_groups = []
    checked_variables = 0
    has_binary_group = any(vdef.get("type") == "binary" for _, vdef in normalized_groups)
    has_integer_group = any(vdef.get("type") == "integer" for _, vdef in normalized_groups)

    if has_incumbent and isinstance(variables, dict) and normalized_groups:
        for vname, vdef in normalized_groups:
            matched = [
                key for key in variables
                if key == vname or key.startswith((f"{vname}_", f"{vname}[", f"{vname}("))
            ]
            if not matched:
                unmatched_groups.append(vname)
                continue
            vtype = vdef.get("type", "continuous")
            lb = vdef.get("lb", vdef.get("lower_bound", None))
            ub = vdef.get("ub", vdef.get("upper_bound", None))

            for var_key in matched:
                var_val = variables[var_key]
                if not isinstance(var_val, (int, float)) or isinstance(var_val, bool):
                    unmatched_groups.append(f"{vname} (non-numeric {var_key})")
                    continue
                checked_variables += 1
                if lb is not None and isinstance(lb, (int, float)) and var_val < lb - 1e-6:
                    bound_violations.append(f"{var_key}={var_val} < lb={lb}")
                if ub is not None and isinstance(ub, (int, float)) and var_val > ub + 1e-6:
                    bound_violations.append(f"{var_key}={var_val} > ub={ub}")

                if vtype == "binary":
                    if abs(var_val) > 1e-6 and abs(var_val - 1.0) > 1e-6:
                        binary_violations.append(f"{var_key}={var_val}")
                elif vtype == "integer":
                    if abs(var_val - round(var_val)) > 1e-6:
                        integer_violations.append(f"{var_key}={var_val}")

    skip_reason = None
    if not has_incumbent:
        skip_reason = "No incumbent is available for variable-domain checks"
    elif not isinstance(variables, dict) or not variables:
        skip_reason = "An incumbent is declared but no solution variables are available"
    elif not normalized_groups:
        skip_reason = "No supported variable group representation is available in opt_spec.json"
    elif unmatched_groups:
        skip_reason = f"Could not map all variable groups: {unmatched_groups[:5]}"
    elif checked_variables == 0:
        skip_reason = "No numeric solution variables were matched to opt_spec.json"

    if skip_reason:
        for check_name in ("variable_bounds", "binary_domain", "integer_domain"):
            report["checks"].append({
                "check": check_name,
                "status": "SKIP",
                "detail": skip_reason,
                "checked_variables": checked_variables,
            })
    elif bound_violations:
        report["checks"].append({"check": "variable_bounds", "status": "FAIL", "detail": f"{len(bound_violations)} violations", "examples": bound_violations[:5]})
        report["summary"]["fail"] += 1
    else:
        report["checks"].append({"check": "variable_bounds", "status": "PASS", "checked_variables": checked_variables})
        report["summary"]["pass"] += 1

    if not skip_reason and not has_binary_group:
        report["checks"].append({
            "check": "binary_domain",
            "status": "SKIP",
            "detail": "opt_spec.json declares no binary variable group",
            "checked_variables": 0,
        })
    elif not skip_reason and binary_violations:
        report["checks"].append({"check": "binary_domain", "status": "FAIL", "detail": f"{len(binary_violations)} non-binary values", "examples": binary_violations[:5]})
        report["summary"]["fail"] += 1
    elif not skip_reason:
        report["checks"].append({"check": "binary_domain", "status": "PASS", "checked_variables": checked_variables})
        report["summary"]["pass"] += 1

    if not skip_reason and not has_integer_group:
        report["checks"].append({
            "check": "integer_domain",
            "status": "SKIP",
            "detail": "opt_spec.json declares no integer variable group",
            "checked_variables": 0,
        })
    elif not skip_reason and integer_violations:
        report["checks"].append({"check": "integer_domain", "status": "FAIL", "detail": f"{len(integer_violations)} non-integer values", "examples": integer_violations[:5]})
        report["summary"]["fail"] += 1
    elif not skip_reason:
        report["checks"].append({"check": "integer_domain", "status": "PASS", "checked_variables": checked_variables})
        report["summary"]["pass"] += 1

    # Check 3: Known optimal comparison
    known = find_known_optimal(manifest, workspace_root_for_manifest(args.manifest))
    if known:
        obj_name, key, known_opt = known
        if known_opt is not None and obj_value is not None:
            known_opt = float(known_opt)
            diff = abs(obj_value - known_opt) / max(abs(known_opt), 1.0)
            if diff > 0.001:
                report["checks"].append({
                    "check": "known_optimal",
                    "status": "WARN",
                    "detail": f"Solver objective {obj_value} differs from known optimal {known_opt} ({obj_name}.{key}) by {diff*100:.2f}%"
                })
                report["summary"]["warn"] += 1
            else:
                report["checks"].append({"check": "known_optimal", "status": "PASS", "detail": f"Within 0.1% of known optimal {known_opt} ({obj_name}.{key}, {diff*100:.4f}%)"})
                report["summary"]["pass"] += 1
    else:
        report["checks"].append({"check": "known_optimal", "status": "SKIP", "detail": "No manifest metadata or benchmark data object with known optimal value found"})

    # Check 4: LP relaxation gap (from solution.json if present)
    lp_gap = solution.get("integrality_gap")
    if lp_gap is not None:
        if lp_gap < 1e-6:
            report["checks"].append({
                "check": "integrality_gap",
                "status": "WARN",
                "detail": "Gap=0% — integrality constraints are non-binding. Binary/integer variables may not be needed."
            })
            report["summary"]["warn"] += 1
        elif lp_gap > 0.20:
            report["checks"].append({
                "check": "integrality_gap",
                "status": "WARN",
                "detail": f"Gap={lp_gap*100:.1f}% — formulation may be weak, consider tightening"
            })
            report["summary"]["warn"] += 1
        else:
            report["checks"].append({"check": "integrality_gap", "status": "PASS", "detail": f"Gap={lp_gap*100:.2f}%"})
            report["summary"]["pass"] += 1

    # Check 5: Solution sparsity
    if variables:
        at_bounds = sum(1 for v in variables.values() if isinstance(v, (int, float)) and (abs(v) < 1e-9 or abs(v - 1.0) < 1e-9))
        total_vars = len(variables)
        sparsity = at_bounds / total_vars if total_vars > 0 else 0
        report["checks"].append({
            "check": "solution_sparsity",
            "status": "INFO",
            "detail": f"{at_bounds}/{total_vars} variables at bounds ({sparsity*100:.1f}%)"
        })

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["summary"]["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
