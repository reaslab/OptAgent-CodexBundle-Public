"""Public solver entrypoint."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RUN_OUTPUT_DIR = ROOT / "run_outputs"
OUTPUT_DIR = ROOT.parent / "outputs"
MODEL_LP_PATH = ROOT / "model.lp"
MODEL_MPS_PATH = ROOT / "model.mps"
ORTOOLS_MIP_SOLVER_CANDIDATES = (
    "CBC_MIXED_INTEGER_PROGRAMMING",
    "CBC",
    "SAT_INTEGER_PROGRAMMING",
    "SAT",
)
ORTOOLS_LP_SOLVER_CANDIDATES = (
    "GLOP_LINEAR_PROGRAMMING",
    "GLOP",
    "CLP_LINEAR_PROGRAMMING",
    "CLP",
)


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


def create_linear_solver(pywraplp, *, prefer_mip: bool = True):
    """Create an OR-Tools native backend that works without SCIP."""
    candidates = (
        ORTOOLS_MIP_SOLVER_CANDIDATES + ORTOOLS_LP_SOLVER_CANDIDATES
        if prefer_mip
        else ORTOOLS_LP_SOLVER_CANDIDATES + ORTOOLS_MIP_SOLVER_CANDIDATES
    )
    attempted = []
    for name in candidates:
        attempted.append(name)
        solver = pywraplp.Solver.CreateSolver(name)
        if solver is not None:
            return solver, name, attempted
    return None, None, attempted


def solve(data: dict, time_limit: int) -> dict:
    from ortools.linear_solver import pywraplp

    print("Phase: building_model")
    solver, native_solver, attempted_native_solvers = create_linear_solver(pywraplp, prefer_mip=True)
    if solver is None:
        return {
            "backend": "ortools-linear",
            "native_solver": None,
            "status": "solver_unavailable",
            "raw_status": "solver_creation_failed",
            "termination_condition": "error",
            "objective_value": None,
            "best_bound": None,
            "solve_time_sec": None,
            "mip_gap": None,
            "has_incumbent": False,
            "solution_count": 0,
            "solver_available": False,
            "variables": {},
            "metrics": {
                "attempted_native_solvers": attempted_native_solvers,
            },
            "notes": ["OR-Tools could not create any supported native linear solver."],
        }

    solver.SetTimeLimit(int(time_limit * 1000))

    # Build variables, objective, and constraints from public data here.
    # Example:
    # x = [solver.BoolVar(f"x[{i}]") for i in range(n)]
    # solver.Minimize(solver.Sum(cost[i] * x[i] for i in range(n)))
    # solver.Add(solver.Sum(x[i] for i in range(n)) <= capacity)

    MODEL_LP_PATH.write_text(solver.ExportModelAsLpFormat(False), encoding="utf-8")
    MODEL_MPS_PATH.write_text(solver.ExportModelAsMpsFormat(False, False), encoding="utf-8")

    print("Phase: optimizing")
    start = time.time()
    result_status = solver.Solve()
    elapsed = time.time() - start

    print("Phase: serializing_solution")
    status_map = {
        pywraplp.Solver.OPTIMAL: ("optimal", "optimal"),
        pywraplp.Solver.FEASIBLE: ("feasible", "feasible"),
        pywraplp.Solver.INFEASIBLE: ("infeasible", "infeasible"),
        pywraplp.Solver.UNBOUNDED: ("unbounded", "unbounded"),
        pywraplp.Solver.NOT_SOLVED: ("error", "not_solved"),
        pywraplp.Solver.ABNORMAL: ("error", "error"),
    }
    status, termination = status_map.get(result_status, ("error", "error"))
    has_incumbent = result_status in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE)
    variables = {var.name(): var.solution_value() for var in solver.variables()} if has_incumbent else {}

    return {
        "backend": "ortools-linear",
        "native_solver": native_solver,
        "status": status,
        "raw_status": str(result_status),
        "termination_condition": termination,
        "objective_value": solver.Objective().Value() if has_incumbent else None,
        "best_bound": solver.Objective().BestBound() if has_incumbent and hasattr(solver.Objective(), "BestBound") else None,
        "solve_time_sec": elapsed,
        "mip_gap": None,
        "has_incumbent": has_incumbent,
        "solution_count": 1 if has_incumbent else 0,
        "solver_available": True,
        "variables": variables,
        "metrics": {
            "attempted_native_solvers": attempted_native_solvers,
        },
        "notes": [],
    }


def run_formal_solve(time_limit: int) -> dict:
    """Run one formal solve and persist the solution summary."""
    ensure_dirs()
    missing = check_inputs()
    if missing:
        result = failure_result("ortools-linear", RuntimeError(f"missing required inputs: {', '.join(missing)}"))
    else:
        try:
            print(f"Solve budget: backend=ortools-linear, time_limit_sec={time_limit}")
            print("Phase: loading_data")
            result = solve(load_public_data(), time_limit)
        except Exception as error:
            result = failure_result("ortools-linear", error)
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
