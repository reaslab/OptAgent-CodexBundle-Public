#!/usr/bin/env python3
"""
model_auditor.py — Compares opt_spec.json structure against the public solver implementation.

Usage:
  python3 model_auditor.py --spec .optagent/cases/case_001_base/formulation/opt_spec.json --code cases/case_001_base/code/run.py --manifest .optagent/cases/case_001_base/data/data_manifest.json
  Inside OptAgent, run via: python .agents/skills/opt-verification/tools/model_auditor.py

Checks variable count, constraint count, variable types, data loading consistency.
"""
import argparse
import json
import os
import re
import sys


def count_expected_variables(spec):
    """Count expected variables from opt_spec.json variable groups and index sets."""
    sets = spec.get("sets", {})
    var_groups = spec.get("variable_groups", spec.get("decision_variables", {}))
    total = 0
    details = {}

    # Normalize list format to dict: [{"name": "x", ...}, ...] → {"x": {...}, ...}
    if isinstance(var_groups, list):
        var_groups = {v.get("name", f"var_{i}"): v for i, v in enumerate(var_groups) if isinstance(v, dict)}

    if isinstance(var_groups, dict):
        for vname, vdef in var_groups.items():
            if not isinstance(vdef, dict):
                continue
            index_sets = vdef.get("index_sets", vdef.get("indices", []))
            if not index_sets:
                count = 1
            else:
                count = 1
                for idx in index_sets:
                    sdef = sets.get(idx, {})
                    if "elements" in sdef:
                        count *= len(sdef["elements"])
                    elif sdef.get("type") == "range":
                        count *= (sdef.get("end", 0) - sdef.get("start", 0) + 1)
                    elif sdef.get("type") == "count":
                        count *= sdef.get("size", 0)
                    elif "source" in sdef:
                        count = None
                        break
                if count is None:
                    details[vname] = "unknown (set size from external source)"
                    continue
            total += count
            details[vname] = count
    return total, details


def count_expected_constraints(spec):
    """Count expected constraints from opt_spec.json."""
    sets = spec.get("sets", {})
    constraints = spec.get("constraints", [])
    indexed = spec.get("indexed_constraints", [])

    # Normalize dict format to list: {"con_name": {...}, ...} → [{"name": "con_name", ...}, ...]
    if isinstance(constraints, dict):
        constraints = [{"name": k, **v} for k, v in constraints.items()]
    if isinstance(indexed, dict):
        indexed = [{"name": k, **v} for k, v in indexed.items()]

    scalar_count = len(constraints)
    indexed_count = 0

    for ic in indexed:
        if not isinstance(ic, dict):
            continue
        index_sets = ic.get("index_sets", [])
        count = 1
        for idx in index_sets:
            sdef = sets.get(idx, {})
            if "elements" in sdef:
                count *= len(sdef["elements"])
            elif sdef.get("type") == "range":
                count *= (sdef.get("end", 0) - sdef.get("start", 0) + 1)
            elif sdef.get("type") == "count":
                count *= sdef.get("size", 0)
            else:
                count = None
                break
        if count is not None:
            indexed_count += count

    return scalar_count + indexed_count, {"scalar": scalar_count, "indexed": indexed_count}


def analyze_code(code_path):
    """Parse solver code to count variables and constraints via regex patterns."""
    if not os.path.exists(code_path):
        return None

    with open(code_path, "r", encoding="utf-8-sig") as f:
        code = f.read()

    # Variable creation patterns (common solvers)
    var_patterns = [
        r'LpVariable\s*\(',          # PuLP
        r'model\.addVar\s*\(',       # Gurobi
        r'model\.addVars\s*\(',      # Gurobi batch
        r'cp\.Variable\s*\(',        # CVXPY
        r'model\.NewBoolVar\s*\(',   # OR-Tools CP-SAT
        r'model\.NewIntVar\s*\(',    # OR-Tools CP-SAT
        r'Var\s*\(\s*within\s*=',    # Pyomo
    ]

    # Constraint patterns
    con_patterns = [
        r'model\.addConstr\s*\(',     # Gurobi single
        r'model\.addConstrs\s*\(',    # Gurobi batch
        r'prob\s*\+=',                # PuLP (adding constraints)
        r'model\.Add\s*\(',           # OR-Tools CP-SAT
        r'model\.addLConstr',         # Gurobi legacy
        r'subject_to',               # CVXPY
    ]

    # Detect variable type declarations
    type_indicators = {
        "binary": len(re.findall(r'cat\s*=\s*["\']Binary["\']|vtype\s*=\s*GRB\.BINARY|boolean\s*=\s*True|NewBoolVar', code)),
        "integer": len(re.findall(r'cat\s*=\s*["\']Integer["\']|vtype\s*=\s*GRB\.INTEGER|integer\s*=\s*True|NewIntVar', code)),
        "continuous": len(re.findall(r'cat\s*=\s*["\']Continuous["\']|vtype\s*=\s*GRB\.CONTINUOUS|nonneg\s*=\s*True', code)),
    }

    # Check data loading
    data_loads = {
        "csv_reads": len(re.findall(r'read_csv|csv\.reader|DictReader|csv\.DictReader', code)),
        "json_reads": len(re.findall(r'json\.load|json\.loads', code)),
        "manifest_ref": "data_manifest" in code,
        "opt_spec_ref": "opt_spec" in code,
        "internal_path_ref": ".optagent" in code,
        "pathlib_root_ref": "Path(__file__).resolve().parent" in code,
    }

    return {
        "variable_creation_calls": sum(len(re.findall(p, code)) for p in var_patterns),
        "constraint_creation_calls": sum(len(re.findall(p, code)) for p in con_patterns),
        "type_indicators": type_indicators,
        "data_loading": data_loads,
        "code_lines": len(code.splitlines()),
    }


def main():
    parser = argparse.ArgumentParser(description="Audit solver code against opt_spec.json")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--code", required=True)
    parser.add_argument("--manifest", default=".optagent/cases/case_001_base/data/data_manifest.json")
    args = parser.parse_args()

    report = {"checks": [], "summary": {"pass": 0, "fail": 0, "warn": 0}}

    # Load spec
    if not os.path.exists(args.spec):
        report["checks"].append({"check": "spec_exists", "status": "FAIL", "detail": f"Not found: {args.spec}"})
        report["summary"]["fail"] += 1
        print(json.dumps(report, indent=2))
        return 1

    with open(args.spec, "r", encoding="utf-8-sig") as f:
        spec = json.load(f)

    # Count expected variables
    exp_vars, var_details = count_expected_variables(spec)
    exp_cons, con_details = count_expected_constraints(spec)

    report["expected"] = {
        "variables": exp_vars,
        "variable_details": var_details,
        "constraints": exp_cons,
        "constraint_details": con_details,
    }

    # Analyze code
    code_analysis = analyze_code(args.code)
    if code_analysis is None:
        report["checks"].append({"check": "code_exists", "status": "FAIL", "detail": f"Not found: {args.code}"})
        report["summary"]["fail"] += 1
        print(json.dumps(report, indent=2))
        return 1

    report["code_analysis"] = code_analysis

    # Public run.py should be generated from opt_spec/data_manifest, but must not
    # read internal .optagent artifacts at runtime.
    if code_analysis["data_loading"]["opt_spec_ref"] or code_analysis["data_loading"]["internal_path_ref"]:
        report["checks"].append({"check": "no_internal_runtime_dependency", "status": "FAIL", "detail": "public solver code references opt_spec/data_manifest or .optagent at runtime"})
        report["summary"]["fail"] += 1
    else:
        report["checks"].append({"check": "no_internal_runtime_dependency", "status": "PASS"})
        report["summary"]["pass"] += 1

    if code_analysis["data_loading"]["pathlib_root_ref"]:
        report["checks"].append({"check": "portable_root", "status": "PASS"})
        report["summary"]["pass"] += 1
    else:
        report["checks"].append({"check": "portable_root", "status": "WARN", "detail": "Path(__file__).resolve().parent not detected"})
        report["summary"]["warn"] += 1

    if not code_analysis["data_loading"]["csv_reads"] and not code_analysis["data_loading"]["json_reads"]:
        report["checks"].append({"check": "loads_data", "status": "WARN", "detail": "No CSV or JSON data loading detected"})
        report["summary"]["warn"] += 1
    else:
        report["checks"].append({"check": "loads_data", "status": "PASS"})
        report["summary"]["pass"] += 1

    # Check variable types match spec
    var_groups = spec.get("variable_groups", spec.get("decision_variables", {}))
    if isinstance(var_groups, list):
        var_iter = (v for v in var_groups if isinstance(v, dict))
    elif isinstance(var_groups, dict):
        var_iter = (v for v in var_groups.values() if isinstance(v, dict))
    else:
        var_iter = iter(())
    var_defs = list(var_iter)
    spec_has_binary = any(
        v.get("type") == "binary"
        for v in var_defs
    )
    spec_has_integer = any(
        v.get("type") == "integer"
        for v in var_defs
    )

    if spec_has_binary and code_analysis["type_indicators"]["binary"] == 0:
        report["checks"].append({"check": "binary_vars_in_code", "status": "WARN", "detail": "Spec declares binary variables but none found in code"})
        report["summary"]["warn"] += 1
    if spec_has_integer and code_analysis["type_indicators"]["integer"] == 0:
        report["checks"].append({"check": "integer_vars_in_code", "status": "WARN", "detail": "Spec declares integer variables but none found in code"})
        report["summary"]["warn"] += 1

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
