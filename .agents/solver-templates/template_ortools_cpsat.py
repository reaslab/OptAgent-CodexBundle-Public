"""Public solver entrypoint. CP-SAT has no LP/MPS equivalent export; record that as a note instead of failing."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RUN_OUTPUT_DIR = ROOT / "run_outputs"
OUTPUT_DIR = ROOT.parent / "outputs"


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
    from ortools.sat.python import cp_model

    print("Phase: building_model")
    model = cp_model.CpModel()

    # Build variables, objective, and constraints from public data here.
    # Example:
    # x = [model.NewBoolVar(f"x[{i}]") for i in range(n)]
    # model.Minimize(sum(cost[i] * x[i] for i in range(n)))
    # model.Add(sum(x[i] for i in range(n)) <= capacity)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit)
    solver.parameters.log_search_progress = False

    print("Phase: optimizing")
    start = time.time()
    raw = solver.Solve(model)
    elapsed = time.time() - start

    print("Phase: serializing_solution")
    status_map = {
        cp_model.OPTIMAL: ("optimal", "optimal"),
        cp_model.FEASIBLE: ("feasible", "feasible"),
        cp_model.INFEASIBLE: ("infeasible", "infeasible"),
        cp_model.MODEL_INVALID: ("error", "model_invalid"),
        cp_model.UNKNOWN: ("timeout", "time_limit"),
    }
    status, termination = status_map.get(raw, ("error", "error"))

    return {
        "backend": "ortools-cp-sat",
        "status": status,
        "raw_status": solver.StatusName(raw),
        "termination_condition": termination,
        "objective_value": solver.ObjectiveValue() if status in {"optimal", "feasible"} else None,
        "best_bound": solver.BestObjectiveBound() if status in {"optimal", "feasible", "timeout"} else None,
        "solve_time_sec": elapsed,
        "mip_gap": None,
        "has_incumbent": status in {"optimal", "feasible"},
        "solution_count": 1 if status in {"optimal", "feasible"} else 0,
        "solver_available": True,
        "variables": {},
        "metrics": {},
        "notes": ["CP-SAT does not provide an LP/MPS export."],
    }


def run_formal_solve(time_limit: int) -> dict:
    """Run one formal solve and persist the solution summary."""
    ensure_dirs()
    missing = check_inputs()
    if missing:
        result = failure_result("ortools-cp-sat", RuntimeError(f"missing required inputs: {', '.join(missing)}"))
    else:
        try:
            print(f"Solve budget: backend=ortools-cp-sat, time_limit_sec={time_limit}")
            print("Phase: loading_data")
            result = solve(load_public_data(), time_limit)
        except Exception as error:
            result = failure_result("ortools-cp-sat", error)
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
    return 0 if result["status"] in {"optimal", "feasible", "infeasible", "timeout"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
