"""Public solver entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RUN_OUTPUT_DIR = ROOT / "run_outputs"
OUTPUT_DIR = ROOT.parent / "outputs"
MODEL_LP_PATH = ROOT / "model.lp"
MODEL_MPS_PATH = ROOT / "model.mps"
RUNTIME_LOG_DIR = Path(tempfile.gettempdir()) / "case-runtime-logs"
GUROBI_LOG_PATH = RUNTIME_LOG_DIR / f"{ROOT.name}_{os.getpid()}_gurobi_runtime.log"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-inputs", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--time-limit", type=int, default=300)
    return parser.parse_args()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def ensure_dirs() -> None:
    RUN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_LOG_DIR.mkdir(parents=True, exist_ok=True)


def failure_result(backend: str, error: Exception) -> dict:
    detail = f"{type(error).__name__}: {error}"
    unavailable = "no module named" in detail.lower() or "license" in detail.lower()
    return {
        "backend": backend,
        "status": "solver_unavailable" if unavailable else "error",
        "raw_status": detail,
        "termination_condition": "import_error" if "no module named" in detail.lower() else "license_error" if "license" in detail.lower() else "error",
        "objective_value": None,
        "best_bound": None,
        "solve_time_sec": None,
        "mip_gap": None,
        "has_incumbent": False,
        "solution_count": 0,
        "solver_available": not unavailable,
        "variables": {},
        "metrics": {},
        "notes": [detail],
    }


def check_inputs() -> list[str]:
    required = [DATA_DIR / "manifest.json"]
    return [str(path) for path in required if not path.exists()]


def load_public_data() -> dict:
    manifest = json.loads((DATA_DIR / "manifest.json").read_text(encoding="utf-8"))
    return {"manifest": manifest}


def solve(data: dict, time_limit: int) -> dict:
    import gurobipy as gp
    from gurobipy import GRB

    RUNTIME_LOG_DIR.mkdir(parents=True, exist_ok=True)
    print("Phase: building_model")
    env = gp.Env(empty=True)
    model = None
    try:
        # Configure before starting Gurobi so no default workspace gurobi.log is created.
        env.setParam("OutputFlag", 0)
        env.setParam("LogFile", str(GUROBI_LOG_PATH))
        env.start()
        model = gp.Model("optimization_model", env=env)
        model.Params.TimeLimit = float(time_limit)

        # Build variables, objective, and constraints from public data here.
        # Example:
        # x = model.addVars(range(n), vtype=GRB.BINARY, name="x")
        # model.setObjective(gp.quicksum(cost[i] * x[i] for i in range(n)), GRB.MINIMIZE)
        # model.addConstr(gp.quicksum(x[i] for i in range(n)) <= capacity)

        model.update()
        model.write(str(MODEL_LP_PATH))
        model.write(str(MODEL_MPS_PATH))

        print("Phase: optimizing")
        start = time.time()
        model.optimize()
        elapsed = time.time() - start

        print("Phase: serializing_solution")
        has_incumbent = model.SolCount > 0
        if model.Status == GRB.OPTIMAL:
            status, termination = "optimal", "optimal"
        elif model.Status == GRB.TIME_LIMIT and has_incumbent:
            status, termination = "feasible", "time_limit"
        elif model.Status == GRB.TIME_LIMIT:
            status, termination = "timeout", "time_limit"
        elif model.Status == GRB.INFEASIBLE:
            status, termination = "infeasible", "infeasible"
        elif model.Status == GRB.UNBOUNDED:
            status, termination = "unbounded", "unbounded"
        else:
            status, termination = "error", "error"

        variables = {}
        if has_incumbent:
            variables = {var.VarName: var.X for var in model.getVars()}

        return {
            "backend": "gurobipy",
            "status": status,
            "raw_status": str(model.Status),
            "termination_condition": termination,
            "objective_value": float(model.ObjVal) if has_incumbent else None,
            "best_bound": float(model.ObjBound) if has_incumbent or model.Status == GRB.TIME_LIMIT else None,
            "solve_time_sec": elapsed,
            "mip_gap": float(model.MIPGap) if has_incumbent else None,
            "has_incumbent": has_incumbent,
            "solution_count": int(model.SolCount),
            "solver_available": True,
            "variables": variables,
            "metrics": {},
            "notes": [],
        }
    finally:
        if model is not None:
            model.dispose()
        env.dispose()


def run_formal_solve(time_limit: int) -> dict:
    """Run one formal solve and persist the solution summary."""
    ensure_dirs()
    missing = check_inputs()
    if missing:
        result = failure_result("gurobipy", RuntimeError(f"missing required inputs: {', '.join(missing)}"))
    else:
        try:
            print(f"Solve budget: backend=gurobipy, time_limit_sec={time_limit}")
            print("Phase: loading_data")
            result = solve(load_public_data(), time_limit)
        except Exception as error:
            result = failure_result("gurobipy", error)
        finally:
            GUROBI_LOG_PATH.unlink(missing_ok=True)
    write_json(RUN_OUTPUT_DIR / "solution_summary.json", {k: v for k, v in result.items() if k != "variables"})
    return result


def main() -> int:
    args = parse_args()
    ensure_dirs()
    if args.check_inputs:
        missing = check_inputs()
        print(json.dumps({"ok": not missing, "missing": missing}, indent=2))
        return 0 if not missing else 1
    if args.dry_run:
        missing = check_inputs()
        if missing:
            print(json.dumps({"status": "error", "missing": missing}, indent=2))
            return 1
        try:
            print("Phase: loading_data")
            load_public_data()
        except Exception as error:
            print(f"Dry run failed: {type(error).__name__}: {error}")
            return 1
        write_json(RUN_OUTPUT_DIR / "dry_run_summary.json", {"ok": True, "message": "Data loaded; optimization skipped."})
        print("Phase: completed")
        return 0

    result = run_formal_solve(args.time_limit)
    print(
        "Result: status={status}, termination_condition={termination}, "
        "objective_value={objective}, solve_time_sec={elapsed}".format(
            status=result.get("status"),
            termination=result.get("termination_condition"),
            objective=result.get("objective_value"),
            elapsed=result.get("solve_time_sec"),
        )
    )
    print("Phase: completed")
    return 0 if result["status"] in {"optimal", "feasible", "infeasible", "unbounded", "timeout"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
