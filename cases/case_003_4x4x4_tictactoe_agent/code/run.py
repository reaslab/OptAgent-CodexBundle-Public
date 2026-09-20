"""Public MILP solver entrypoint for 4x4x4 cooperative coloring."""
from __future__ import annotations
import argparse, json, os, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR, RUN_OUTPUT_DIR, OUTPUT_DIR = ROOT / "data", ROOT / "run_outputs", ROOT.parent / "outputs"
MODEL_LP_PATH, MODEL_MPS_PATH = ROOT / "model.lp", ROOT / "model.mps"
RUNTIME_LOG_DIR = Path(tempfile.gettempdir()) / "case-runtime-logs"
GUROBI_LOG_PATH = RUNTIME_LOG_DIR / f"{ROOT.name}_{os.getpid()}_gurobi_runtime.log"
CBC_LOG_PATH = RUNTIME_LOG_DIR / f"{ROOT.name}_{os.getpid()}_cbc_runtime.log"

def parse_args():
    p = argparse.ArgumentParser(); p.add_argument("--check-inputs", action="store_true"); p.add_argument("--dry-run", action="store_true"); p.add_argument("--time-limit", type=int, default=300); return p.parse_args()
def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
def ensure_dirs():
    RUN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True); OUTPUT_DIR.mkdir(parents=True, exist_ok=True); RUNTIME_LOG_DIR.mkdir(parents=True, exist_ok=True)
def failure_result(backend, error):
    detail = f"{type(error).__name__}: {error}"; unavailable = any(t in detail.lower() for t in ("no module named", "license", "expired", "gurobierror"))
    return {"backend": backend, "status": "solver_unavailable" if unavailable else "error", "raw_status": detail, "termination_condition": "license_error" if unavailable else "error", "objective_value": None, "best_bound": None, "solve_time_sec": None, "mip_gap": None, "has_incumbent": False, "solution_count": 0, "solver_available": not unavailable, "variables": {}, "metrics": {"solvers_tried": [backend]}, "notes": [detail]}
def check_inputs():
    return [str(p) for p in (DATA_DIR / "manifest.json", DATA_DIR / "line_rules.json") if not p.exists()]
def load_public_data():
    manifest = json.loads((DATA_DIR / "manifest.json").read_text(encoding="utf-8")); entry = manifest.get("data_objects", {}).get("line_rules")
    if not isinstance(entry, dict): raise ValueError("manifest missing data_objects.line_rules")
    rel = Path(str(entry.get("source_path", "line_rules.json")))
    if rel.is_absolute() or ".." in rel.parts: raise ValueError("manifest data path escapes public snapshot")
    rules = json.loads((DATA_DIR / rel).read_text(encoding="utf-8")); lines = rules.get("lines")
    if not isinstance(lines, list) or len(lines) != 76: raise ValueError("line_rules must contain exactly 76 lines")
    return {"manifest": manifest, "lines": [list(map(int, x["cells"])) for x in sorted(lines, key=lambda x: int(x["line_id"]))]}
def result(status, raw, term, obj, bound, elapsed, gap, vars_out, backend, tried, notes=None):
    inc = status in {"optimal", "feasible"}; return {"backend": backend, "status": status, "raw_status": raw, "termination_condition": term, "objective_value": obj, "best_bound": bound, "solve_time_sec": elapsed, "mip_gap": gap, "has_incumbent": inc, "solution_count": 1 if inc else 0, "solver_available": True, "variables": vars_out if inc else {}, "metrics": {"solvers_tried": tried}, "notes": notes or []}

def write_public_outputs(data, out):
    """Write a compact, solver-independent board view for a returned incumbent."""
    if not out.get("has_incumbent"):
        return
    vals = out.get("variables", {})
    x_cells = [int(round(vals.get(f"x_{c}", 0.0))) for c in range(64)]
    layers = []
    for i in range(4):
        layers.append([["X" if x_cells[16*i + 4*j + k] else "O" for k in range(4)] for j in range(4)])
    complete_x = [l for l in range(76) if int(round(vals.get(f"line_x_{l}", 0.0)))]
    complete_o = [l for l in range(76) if int(round(vals.get(f"line_o_{l}", 0.0)))]
    write_json(OUTPUT_DIR / "board_solution.json", {
        "case_id": "case_003_4x4x4_tictactoe_agent",
        "board_shape": [4, 4, 4],
        "cell_id_formula": "16*x + 4*y + z",
        "layer_order": "x=0,1,2,3; rows are y=0..3; columns are z=0..3",
        "layers": layers,
        "x_count": sum(x_cells),
        "o_count": 64 - sum(x_cells),
        "complete_x_lines": len(complete_x),
        "complete_o_lines": len(complete_o),
        "complete_lines_total": len(complete_x) + len(complete_o),
        "line_count_checked": len(data["lines"]),
    })
    write_json(OUTPUT_DIR / "line_counts.json", {
        "case_id": "case_003_4x4x4_tictactoe_agent",
        "complete_x_line_ids": complete_x,
        "complete_o_line_ids": complete_o,
        "complete_lines_total": len(complete_x) + len(complete_o),
        "line_count_checked": len(data["lines"]),
    })
def solve_gurobi(data, time_limit):
    import gurobipy as gp
    from gurobipy import GRB
    env = gp.Env(empty=True); env.setParam("OutputFlag", 0); env.setParam("LogFile", str(GUROBI_LOG_PATH)); env.start(); model = gp.Model("4x4x4_standard_lines", env=env)
    try:
        model.Params.TimeLimit = float(time_limit); x = model.addVars(64, vtype=GRB.BINARY, name="x"); lx = model.addVars(76, vtype=GRB.BINARY, name="line_x"); lo = model.addVars(76, vtype=GRB.BINARY, name="line_o"); model.addConstr(gp.quicksum(x[c] for c in range(64)) == 32, name="exactly_32_x")
        for l, cells in enumerate(data["lines"]):
            for c in cells: model.addConstr(lx[l] <= x[c]); model.addConstr(lo[l] + x[c] <= 1)
            model.addConstr(lx[l] - gp.quicksum(x[c] for c in cells) >= -3); model.addConstr(lo[l] + gp.quicksum(x[c] for c in cells) >= 1); model.addConstr(lx[l] + lo[l] <= 1)
        model.setObjective(gp.quicksum(lx[l] + lo[l] for l in range(76)), GRB.MINIMIZE); model.update(); model.write(str(MODEL_LP_PATH)); model.write(str(MODEL_MPS_PATH)); start = time.time(); model.optimize(); elapsed = time.time() - start; has = model.SolCount > 0
        if model.Status == GRB.OPTIMAL: status, term = "optimal", "optimal"
        elif model.Status == GRB.TIME_LIMIT and has: status, term = "feasible", "time_limit"
        elif model.Status == GRB.TIME_LIMIT: status, term = "timeout", "time_limit"
        elif model.Status == GRB.INFEASIBLE: status, term = "infeasible", "infeasible"
        else: status, term = "error", "error"
        vals = {v.VarName: float(v.X) for v in model.getVars()} if has else {}; return result(status, str(model.Status), term, float(model.ObjVal) if has else None, float(model.ObjBound) if has else None, elapsed, float(model.MIPGap) if has else None, vals, "gurobipy", ["gurobipy"])
    finally: model.dispose(); env.dispose()
def solve_pulp(data, time_limit):
    import pulp
    prob = pulp.LpProblem("4x4x4_standard_lines", pulp.LpMinimize); x = pulp.LpVariable.dicts("x", range(64), cat="Binary"); lx = pulp.LpVariable.dicts("line_x", range(76), cat="Binary"); lo = pulp.LpVariable.dicts("line_o", range(76), cat="Binary"); prob += pulp.lpSum(lx[l] + lo[l] for l in range(76)); prob += pulp.lpSum(x[c] for c in range(64)) == 32, "exactly_32_x"
    for l, cells in enumerate(data["lines"]):
        for c in cells: prob += lx[l] <= x[c], f"line_x_upper_{l}_{c}"; prob += lo[l] + x[c] <= 1, f"line_o_upper_{l}_{c}"
        prob += lx[l] - pulp.lpSum(x[c] for c in cells) >= -3, f"line_x_lower_{l}"; prob += lo[l] + pulp.lpSum(x[c] for c in cells) >= 1, f"line_o_lower_{l}"; prob += lx[l] + lo[l] <= 1, f"line_indicator_exclusive_{l}"
    prob.writeLP(str(MODEL_LP_PATH)); prob.writeMPS(str(MODEL_MPS_PATH)); start = time.time(); prob.solve(pulp.PULP_CBC_CMD(timeLimit=time_limit, msg=False, logPath=str(CBC_LOG_PATH))); elapsed = time.time() - start; log = CBC_LOG_PATH.read_text(encoding="utf-8", errors="replace") if CBC_LOG_PATH.exists() else ""; low = log.lower(); obj = pulp.value(prob.objective); optimal = "result - optimal solution found" in low or "optimal - objective value" in low; timed = "stopped on time" in low or "exiting on maximum time" in low; infeas = "problem proven infeasible" in low
    if optimal or prob.status == pulp.LpStatusOptimal: status, term, raw = "optimal", "optimal", "Optimal"
    elif timed and obj is not None: status, term, raw = "feasible", "time_limit", "CBC: Stopped on time limit"
    elif timed: status, term, raw = "timeout", "time_limit", "CBC: Stopped on time limit"
    elif infeas or prob.status == pulp.LpStatusInfeasible: status, term, raw = "infeasible", "infeasible", "Infeasible"
    else: status, term, raw = "error", "unverified_status", pulp.LpStatus.get(prob.status, str(prob.status))
    vals = {v.name: float(v.varValue or 0.0) for v in prob.variables()}; out = result(status, raw, term, float(obj) if obj is not None else None, None, elapsed, 0.0 if status == "optimal" else None, vals, "pulp", ["gurobipy", "pulp"], ["Gurobi unavailable; fell back to PuLP/CBC."]); out["metrics"]["native_solver"] = "CBC_MIXED_INTEGER_PROGRAMMING"; return out
def run_formal_solve(time_limit):
    ensure_dirs(); missing = check_inputs()
    if missing: out = failure_result("gurobipy", RuntimeError("missing required inputs: " + ", ".join(missing)))
    else:
        data = load_public_data()
        try: print("Phase: trying_gurobipy"); out = solve_gurobi(data, time_limit)
        except Exception as err:
            first = failure_result("gurobipy", err)
            try: print("Phase: trying_pulp"); out = solve_pulp(data, time_limit); out["notes"].append(first["raw_status"])
            except Exception as err2: out = failure_result("pulp", err2); out["metrics"]["solvers_tried"] = ["gurobipy", "pulp"]
        finally: GUROBI_LOG_PATH.unlink(missing_ok=True); CBC_LOG_PATH.unlink(missing_ok=True)
    write_public_outputs(data if not missing else {"lines": []}, out)
    write_json(RUN_OUTPUT_DIR / "solution_summary.json", {k: v for k, v in out.items() if k != "variables"}); return out
def main():
    args = parse_args(); ensure_dirs()
    if args.check_inputs: missing = check_inputs(); print(json.dumps({"ok": not missing, "missing": missing}, indent=2)); return 0 if not missing else 1
    if args.dry_run:
        try: load_public_data()
        except Exception as err: print(f"Dry run failed: {type(err).__name__}: {err}"); return 1
        write_json(RUN_OUTPUT_DIR / "dry_run_summary.json", {"ok": True, "message": "Data loaded; optimization skipped."}); print("Phase: completed"); return 0
    out = run_formal_solve(args.time_limit); print(f"Result: status={out.get('status')}, termination_condition={out.get('termination_condition')}, objective_value={out.get('objective_value')}, solve_time_sec={out.get('solve_time_sec')}"); print("Phase: completed"); return 0 if out["status"] in {"optimal", "feasible", "infeasible", "unbounded", "timeout"} else 1
if __name__ == "__main__": raise SystemExit(main())
