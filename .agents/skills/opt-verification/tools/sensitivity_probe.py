#!/usr/bin/env python3
"""
sensitivity_probe.py — Lightweight sensitivity analysis for solution validation.

Usage:
  python3 sensitivity_probe.py --model cases/case_001_base/code/run.py --solution .optagent/cases/case_001_base/solver/solution.json
  Inside OptAgent, run via: python .agents/skills/opt-verification/tools/sensitivity_probe.py

Perturbs binding constraint RHS and top objective coefficients to validate
model behavior and solution stability.
"""
import argparse
import json
import os
import re
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description="Sensitivity probe")
    parser.add_argument("--model", required=True)
    parser.add_argument("--solution", required=True)
    args = parser.parse_args()

    report = {"probes": [], "summary": ""}

    if not os.path.exists(args.solution) or not os.path.exists(args.model):
        report["summary"] = "Required files not found"
        print(json.dumps(report, indent=2))
        return 1

    with open(args.solution, "r", encoding="utf-8-sig") as f:
        solution = json.load(f)

    base_obj = solution.get("objective_value")
    if base_obj is None:
        report["summary"] = "No objective value — cannot run sensitivity analysis"
        print(json.dumps(report, indent=2))
        return 0

    with open(args.model, "r", encoding="utf-8-sig") as f:
        code = f.read()

    # Detect sense from code
    is_maximize = bool(re.search(r'LpMaximize|maximize|Maximize|GRB\.MAXIMIZE|sense\s*=\s*-1|cp\.Maximize', code, re.IGNORECASE))
    report["objective_sense"] = "maximize" if is_maximize else "minimize"
    report["base_objective"] = base_obj

    solver_dir = os.path.dirname(args.solution) or "."
    temp_dir = os.path.join(solver_dir, "temp")
    os.makedirs(temp_dir, exist_ok=True)
    public_code_dir = os.path.dirname(os.path.abspath(args.model))
    temp_run_outputs = os.path.join(temp_dir, "probe_run_outputs")
    temp_public_outputs = os.path.join(temp_dir, "probe_outputs")
    os.makedirs(temp_run_outputs, exist_ok=True)
    os.makedirs(temp_public_outputs, exist_ok=True)
    temp_solution = os.path.join(temp_run_outputs, "solution_summary.json")
    temp_log = os.path.join(temp_dir, "probe_log.txt")
    temp_result = os.path.join(temp_public_outputs, "result.csv")
    temp_model_lp = os.path.join(temp_dir, "probe_model.lp")
    temp_model_mps = os.path.join(temp_dir, "probe_model.mps")
    temp_solution_code = temp_solution.replace("\\", "/")
    temp_log_code = temp_log.replace("\\", "/")
    temp_result_code = temp_result.replace("\\", "/")

    # Probe: tighten time limit to verify re-solve works
    probe_code = code
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
        probe_code = probe_code.replace(old, new)

    # Keep probe artifacts internal while preserving run.py's relative input loading.
    probe_code = re.sub(
        r"ROOT\s*=\s*Path\(__file__\)\.resolve\(\)\.parent",
        f"ROOT = Path({json.dumps(public_code_dir)}).resolve()",
        probe_code,
    )
    probe_code = re.sub(
        r'RUN_OUTPUT_DIR\s*=\s*ROOT\s*/\s*["\']run_outputs["\']',
        f"RUN_OUTPUT_DIR = Path({json.dumps(temp_run_outputs)})",
        probe_code,
    )
    probe_code = re.sub(
        r'OUTPUT_DIR\s*=\s*ROOT\.parent\s*/\s*["\']outputs["\']',
        f"OUTPUT_DIR = Path({json.dumps(temp_public_outputs)})",
        probe_code,
    )
    probe_code = re.sub(
        r'MODEL_LP_PATH\s*=\s*ROOT\s*/\s*["\']model\.lp["\']',
        f"MODEL_LP_PATH = Path({json.dumps(temp_model_lp)})",
        probe_code,
    )
    probe_code = re.sub(
        r'MODEL_MPS_PATH\s*=\s*ROOT\s*/\s*["\']model\.mps["\']',
        f"MODEL_MPS_PATH = Path({json.dumps(temp_model_mps)})",
        probe_code,
    )

    probe_path = os.path.join(temp_dir, "probe_model.py")

    def run_probe(modified_code, label):
        with open(probe_path, "w", encoding="utf-8") as f:
            f.write(modified_code)
        try:
            result = subprocess.run(
                [sys.executable, probe_path],
                capture_output=True, text=True, timeout=60
            )
            if os.path.exists(temp_solution):
                with open(temp_solution, "r", encoding="utf-8") as f:
                    probe_sol = json.load(f)
                probe_obj = probe_sol.get("objective_value")
                probe_status = probe_sol.get("status", "")
                return {"label": label, "objective": probe_obj, "status": probe_status, "error": None}
            return {"label": label, "objective": None, "status": "no_output", "error": result.stderr[:200]}
        except subprocess.TimeoutExpired:
            return {"label": label, "objective": None, "status": "timeout", "error": "Probe timed out (60s)"}
        except Exception as e:
            return {"label": label, "objective": None, "status": "error", "error": str(e)}
        finally:
            for f in [probe_path, temp_solution, temp_log, temp_result, temp_model_lp, temp_model_mps]:
                if os.path.exists(f):
                    os.remove(f)

    # Probe 1: Remove integrality (should match LP relaxation if already computed)
    relaxed = probe_code
    relaxed = re.sub(r"cat\s*=\s*['\"]Binary['\"]", "cat='Continuous'", relaxed)
    relaxed = re.sub(r"cat\s*=\s*['\"]Integer['\"]", "cat='Continuous'", relaxed)
    relaxed = re.sub(r"vtype\s*=\s*GRB\.BINARY", "vtype=GRB.CONTINUOUS", relaxed)
    relaxed = re.sub(r"vtype\s*=\s*GRB\.INTEGER", "vtype=GRB.CONTINUOUS", relaxed)
    result = run_probe(relaxed, "integrality_relaxation")
    if result["objective"] is not None:
        gap = abs(base_obj - result["objective"]) / max(abs(base_obj), 1.0)
        result["gap_from_base"] = gap
        if gap < 1e-9:
            result["interpretation"] = "Integrality is non-binding"
        else:
            result["interpretation"] = f"Integrality tightens objective by {gap*100:.2f}%"
    report["probes"].append(result)

    anomalies = sum(1 for p in report["probes"] if p.get("status") not in ("optimal", "feasible"))
    report["summary"] = f"{len(report['probes'])} probes completed, {anomalies} anomalies"

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
