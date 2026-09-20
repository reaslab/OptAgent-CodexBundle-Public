"""Public solver entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import re
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
CBC_RUNTIME_LOG = RUNTIME_LOG_DIR / f"{ROOT.name}_{os.getpid()}_cbc_runtime.log"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-inputs", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--time-limit", type=int, default=300)
    return parser.parse_args()


def ensure_dirs() -> None:
    RUN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_LOG_DIR.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


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
    """Load the files described by DATA_DIR/manifest.json."""
    manifest = json.loads((DATA_DIR / "manifest.json").read_text(encoding="utf-8"))
    return {"manifest": manifest}


def parse_float(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text.replace(",", "").rstrip("%"))
    except ValueError:
        return None


def parse_cbc_log(log_text: str) -> dict:
    lower = log_text.lower()
    parsed = {
        "time_limit": "result - stopped on time limit" in lower
        or "exiting on maximum time" in lower
        or "stopped on time - objective value" in lower,
        "optimal": "result - optimal solution found" in lower
        or "optimal - objective value" in lower
        or "optimal objective" in lower,
        "infeasible": "result - problem proven infeasible" in lower,
        "unbounded": "result - linear relaxation unbounded" in lower,
        "no_incumbent": "no feasible solution found" in lower
        or "no integer solution" in lower,
        "objective_value": None,
        "best_bound": None,
        "mip_gap": None,
    }
    objective = re.search(r"Objective value:\s*([-+0-9.eE,]+)", log_text)
    stopped = re.search(r"Stopped on time - objective value\s+([-+0-9.eE,]+)", log_text, re.I)
    bound = re.search(r"(?:Lower|Upper) bound:\s*([-+0-9.eE,]+)", log_text, re.I)
    gap = re.search(r"Gap:\s*([-+0-9.eE,%]+)", log_text, re.I)
    parsed["objective_value"] = parse_float((objective or stopped).group(1)) if (objective or stopped) else None
    parsed["best_bound"] = parse_float(bound.group(1)) if bound else None
    if gap:
        raw = gap.group(1)
        value = parse_float(raw)
        parsed["mip_gap"] = value / 100.0 if value is not None and "%" in raw else value
    return parsed


def solve(data: dict, time_limit: int) -> dict:
    import pulp

    print("Phase: building_model")
    problem = pulp.LpProblem("optimization_model", pulp.LpMinimize)

    # Build variables, objective, and constraints from public data here.
    # Example:
    # x = pulp.LpVariable.dicts("x", range(n), cat="Binary")
    # problem += pulp.lpSum(cost[i] * x[i] for i in range(n))
    # problem += pulp.lpSum(x[i] for i in range(n)) <= capacity

    problem.writeLP(str(MODEL_LP_PATH))
    problem.writeMPS(str(MODEL_MPS_PATH))

    print("Phase: optimizing")
    start = time.time()
    solver = pulp.PULP_CBC_CMD(timeLimit=time_limit, msg=False, logPath=str(CBC_RUNTIME_LOG))
    problem.solve(solver)
    elapsed = time.time() - start

    print("Phase: serializing_solution")
    cbc_status = parse_cbc_log(CBC_RUNTIME_LOG.read_text(encoding="utf-8", errors="replace"))
    raw_status = pulp.LpStatus.get(problem.status, str(problem.status))
    objective = cbc_status["objective_value"]
    if objective is None:
        objective = pulp.value(problem.objective)

    if cbc_status["optimal"]:
        status, termination = "optimal", "optimal"
        raw_status = "Optimal"
    elif cbc_status["time_limit"]:
        has_incumbent = objective is not None and not cbc_status["no_incumbent"]
        status = "feasible" if has_incumbent else "timeout"
        termination = "time_limit"
        raw_status = f"PuLP: {raw_status}; CBC: Stopped on time limit"
    elif cbc_status["infeasible"]:
        status, termination = "infeasible", "infeasible"
    elif cbc_status["unbounded"]:
        status, termination = "unbounded", "unbounded"
    else:
        status, termination = "error", "unverified_status"

    return {
        "backend": "pulp",
        "status": status,
        "raw_status": raw_status,
        "termination_condition": termination,
        "objective_value": float(objective) if objective is not None else None,
        "best_bound": cbc_status["best_bound"],
        "solve_time_sec": elapsed,
        "mip_gap": cbc_status["mip_gap"],
        "has_incumbent": status in {"optimal", "feasible"},
        "solution_count": 1 if status in {"optimal", "feasible"} else 0,
        "solver_available": True,
        "variables": {var.name: var.varValue for var in problem.variables()} if status in {"optimal", "feasible"} else {},
        "metrics": {},
        "notes": ["CBC time limit with incumbent is feasible, not proven optimal."] if termination == "time_limit" and status == "feasible" else [],
    }


def run_formal_solve(time_limit: int) -> dict:
    """Run one formal solve and persist the solution summary."""
    ensure_dirs()
    missing = check_inputs()
    if missing:
        result = failure_result("pulp", RuntimeError(f"missing required inputs: {', '.join(missing)}"))
    else:
        try:
            print(f"Solve budget: backend=pulp, time_limit_sec={time_limit}")
            print("Phase: loading_data")
            result = solve(load_public_data(), time_limit)
        except Exception as error:
            result = failure_result("pulp", error)
        finally:
            CBC_RUNTIME_LOG.unlink(missing_ok=True)
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
