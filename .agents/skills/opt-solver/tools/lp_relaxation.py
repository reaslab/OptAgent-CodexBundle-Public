#!/usr/bin/env python3
"""
lp_relaxation.py — Computes LP relaxation bound for MIP models.

Usage:
  python3 lp_relaxation.py --model cases/case_001_base/code/run.py --solution .optagent/cases/case_001_base/solver/solution.json
  Inside OptAgent, run via: python .agents/skills/opt-solver/tools/lp_relaxation.py

Reads the solver model, creates a modified version with relaxed integrality,
solves it, and computes the integrality gap.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Compute LP relaxation bound")
    parser.add_argument("--model", required=True, help="Path to public solver run.py")
    parser.add_argument("--solution", required=True, help="Path to solver/solution.json")
    args = parser.parse_args()

    report = {"computed": False, "reason": ""}

    # Load solution to check if MIP
    if not os.path.exists(args.solution):
        report["reason"] = "solution.json not found"
        print(json.dumps(report, indent=2))
        return 1

    with open(args.solution, "r", encoding="utf-8-sig") as f:
        solution = json.load(f)

    # Only compute for MIP problems (has binary or integer variables)
    mip_obj = solution.get("objective_value")
    if mip_obj is None:
        report["reason"] = "No objective value in solution"
        print(json.dumps(report, indent=2))
        return 0

    # Read model code
    if not os.path.exists(args.model):
        report["reason"] = "solver code not found"
        print(json.dumps(report, indent=2))
        return 1

    with open(args.model, "r", encoding="utf-8-sig") as f:
        code = f.read()

    # Check if it's actually a MIP (has binary/integer variable declarations)
    has_integrality = bool(re.search(
        r'cat\s*=\s*["\'](?:Binary|Integer)["\']|'
        r'vtype\s*=\s*GRB\.(?:BINARY|INTEGER)|'
        r'boolean\s*=\s*True|integer\s*=\s*True|'
        r'NewBoolVar|NewIntVar|'
        r'within\s*=\s*(?:Binary|Integers)',
        code
    ))

    if not has_integrality:
        report["reason"] = "No integrality constraints detected — already an LP"
        report["lp_relaxation_value"] = mip_obj
        report["integrality_gap"] = 0.0
        report["integrality_binding"] = False
        print(json.dumps(report, indent=2))
        return 0

    # Create relaxed version of model
    relaxed_code = code

    # PuLP: cat='Binary' → cat='Continuous', cat='Integer' → cat='Continuous'
    relaxed_code = re.sub(r"cat\s*=\s*['\"]Binary['\"]", "cat='Continuous'", relaxed_code)
    relaxed_code = re.sub(r"cat\s*=\s*['\"]Integer['\"]", "cat='Continuous'", relaxed_code)

    # Gurobi: vtype=GRB.BINARY → vtype=GRB.CONTINUOUS
    relaxed_code = re.sub(r"vtype\s*=\s*GRB\.BINARY", "vtype=GRB.CONTINUOUS", relaxed_code)
    relaxed_code = re.sub(r"vtype\s*=\s*GRB\.INTEGER", "vtype=GRB.CONTINUOUS", relaxed_code)

    # CVXPY: boolean=True → remove
    relaxed_code = re.sub(r",?\s*boolean\s*=\s*True", "", relaxed_code)
    relaxed_code = re.sub(r",?\s*integer\s*=\s*True", "", relaxed_code)

    solver_dir = os.path.dirname(args.solution) or "."
    temp_dir = os.path.join(solver_dir, "temp")
    os.makedirs(temp_dir, exist_ok=True)
    public_code_dir = os.path.dirname(os.path.abspath(args.model))
    temp_run_outputs = os.path.join(temp_dir, "lp_relaxation_run_outputs")
    temp_public_outputs = os.path.join(temp_dir, "lp_relaxation_outputs")
    os.makedirs(temp_run_outputs, exist_ok=True)
    os.makedirs(temp_public_outputs, exist_ok=True)
    temp_solution = os.path.join(temp_run_outputs, "solution_summary.json")
    temp_log = os.path.join(temp_dir, "lp_relaxation_log.txt")
    temp_result = os.path.join(temp_public_outputs, "result.csv")
    temp_model_lp = os.path.join(temp_dir, "lp_relaxation_model.lp")
    temp_model_mps = os.path.join(temp_dir, "lp_relaxation_model.mps")
    temp_solution_code = temp_solution.replace("\\", "/")
    temp_log_code = temp_log.replace("\\", "/")
    temp_result_code = temp_result.replace("\\", "/")

    # Keep probe artifacts internal while preserving run.py's relative input loading.
    relaxed_code = re.sub(
        r"ROOT\s*=\s*Path\(__file__\)\.resolve\(\)\.parent",
        f"ROOT = Path({json.dumps(public_code_dir)}).resolve()",
        relaxed_code,
    )
    relaxed_code = re.sub(
        r'RUN_OUTPUT_DIR\s*=\s*ROOT\s*/\s*["\']run_outputs["\']',
        f"RUN_OUTPUT_DIR = Path({json.dumps(temp_run_outputs)})",
        relaxed_code,
    )
    relaxed_code = re.sub(
        r'OUTPUT_DIR\s*=\s*ROOT\.parent\s*/\s*["\']outputs["\']',
        f"OUTPUT_DIR = Path({json.dumps(temp_public_outputs)})",
        relaxed_code,
    )
    relaxed_code = re.sub(
        r'MODEL_LP_PATH\s*=\s*ROOT\s*/\s*["\']model\.lp["\']',
        f"MODEL_LP_PATH = Path({json.dumps(temp_model_lp)})",
        relaxed_code,
    )
    relaxed_code = re.sub(
        r'MODEL_MPS_PATH\s*=\s*ROOT\s*/\s*["\']model\.mps["\']',
        f"MODEL_MPS_PATH = Path({json.dumps(temp_model_mps)})",
        relaxed_code,
    )

    # Modify output paths to keep probe artifacts inside the internal solver temp dir.
    output_replacements = {
        args.solution: temp_solution_code,
        args.solution.replace("\\", "/"): temp_solution_code,
        os.path.join(solver_dir, "solution.json"): temp_solution_code,
        os.path.join(solver_dir, "solver_log.txt"): temp_log_code,
        "solver/solution.json": temp_solution_code,
        "solver/solver_log.txt": temp_log_code,
        "solver/result.csv": temp_result_code,
    }
    for old, new in output_replacements.items():
        relaxed_code = relaxed_code.replace(old, new)

    # Write and run relaxed model
    relaxed_path = os.path.join(temp_dir, "lp_relaxation_model.py")
    with open(relaxed_path, "w", encoding="utf-8") as f:
        f.write(relaxed_code)

    try:
        result = subprocess.run(
            [sys.executable, relaxed_path],
            capture_output=True, text=True, timeout=120
        )

        # Read relaxed solution
        if os.path.exists(temp_solution):
            with open(temp_solution, "r", encoding="utf-8") as f:
                lp_solution = json.load(f)

            lp_obj = lp_solution.get("objective_value")
            if lp_obj is not None:
                gap = abs(mip_obj - lp_obj) / max(abs(mip_obj), 1.0)
                binding = gap > 1e-9

                # Count fractional variables in LP solution
                lp_vars = lp_solution.get("variables", {})
                fractional = sum(
                    1 for v in lp_vars.values()
                    if isinstance(v, (int, float)) and abs(v) > 1e-6 and abs(v - 1.0) > 1e-6
                    and abs(v - round(v)) > 1e-6
                )

                report.update({
                    "computed": True,
                    "lp_relaxation_value": lp_obj,
                    "mip_value": mip_obj,
                    "integrality_gap": gap,
                    "integrality_binding": binding,
                    "fractional_variables_count": fractional,
                })

                if not binding:
                    report["note"] = "LP relaxation equals MIP — integrality constraints are non-binding"

                # Append to original solution.json
                solution["lp_relaxation_value"] = lp_obj
                solution["integrality_gap"] = gap
                with open(args.solution, "w", encoding="utf-8") as f:
                    json.dump(solution, f, indent=2, ensure_ascii=False)
            else:
                report["reason"] = "LP relaxation solve produced no objective"
        else:
            report["reason"] = f"LP relaxation output not found. stderr: {result.stderr[:500]}"

    except subprocess.TimeoutExpired:
        report["reason"] = "LP relaxation solve timed out (120s)"
    except Exception as e:
        report["reason"] = f"Error running LP relaxation: {e}"
    finally:
        # Cleanup temp files
        for f in [relaxed_path, temp_solution, temp_log, temp_result, temp_model_lp, temp_model_mps]:
            if os.path.exists(f):
                os.remove(f)

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
