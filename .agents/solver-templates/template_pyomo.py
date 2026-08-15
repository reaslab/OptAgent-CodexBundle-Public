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
PYOMO_LOG_PATH = RUNTIME_LOG_DIR / f"{ROOT.name}_{os.getpid()}_pyomo_runtime.log"
DEFAULT_PYOMO_SOLVER = "cbc"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-inputs", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--time-limit", type=int, default=300)
    parser.add_argument("--solver", default=DEFAULT_PYOMO_SOLVER)
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


def solve(data: dict, solver_name: str, time_limit: int) -> dict:
    import pyomo.environ as pyo

    RUNTIME_LOG_DIR.mkdir(parents=True, exist_ok=True)
    print("Phase: building_model")
    model = pyo.ConcreteModel()

    # Build sets, variables, objective, and constraints from public data here.
    # Example:
    # model.I = pyo.RangeSet(0, n - 1)
    # model.x = pyo.Var(model.I, domain=pyo.Binary)
    # model.obj = pyo.Objective(expr=sum(cost[i] * model.x[i] for i in model.I))
    # model.capacity = pyo.Constraint(expr=sum(model.x[i] for i in model.I) <= capacity)

    try:
        model.write(str(MODEL_LP_PATH), io_options={"symbolic_solver_labels": True})
        model.write(str(MODEL_MPS_PATH), io_options={"symbolic_solver_labels": True})
    except Exception:
        pass

    opt = pyo.SolverFactory(solver_name)
    if not opt.available(False):
        return {
            "backend": f"pyomo-{solver_name}",
            "status": "solver_unavailable",
            "raw_status": "solver_unavailable",
            "termination_condition": "import_error",
            "objective_value": None,
            "best_bound": None,
            "solve_time_sec": None,
            "mip_gap": None,
            "has_incumbent": False,
            "solution_count": 0,
            "solver_available": False,
            "variables": {},
            "metrics": {},
            "notes": [f"Pyomo solver {solver_name} is unavailable."],
        }
    if solver_name in {"cbc", "glpk", "highs"}:
        opt.options["seconds" if solver_name == "cbc" else "time_limit"] = time_limit
    elif solver_name == "ipopt":
        opt.options["max_cpu_time"] = time_limit

    print("Phase: optimizing")
    start = time.time()
    result = opt.solve(model, tee=False, logfile=str(PYOMO_LOG_PATH))
    elapsed = time.time() - start

    print("Phase: serializing_solution")
    term = str(result.solver.termination_condition).lower()
    if "optimal" in term:
        status, termination = "optimal", "optimal"
    elif "max" in term or "time" in term:
        status, termination = "timeout", "time_limit"
    elif "infeasible" in term:
        status, termination = "infeasible", "infeasible"
    elif "unbounded" in term:
        status, termination = "unbounded", "unbounded"
    else:
        status, termination = "error", term

    variables = {}
    if status in {"optimal", "feasible"}:
        variables = {
            component.name: pyo.value(component, exception=False)
            for component in model.component_data_objects(pyo.Var)
        }

    return {
        "backend": f"pyomo-{solver_name}",
        "status": status,
        "raw_status": term,
        "termination_condition": termination,
        "objective_value": None,
        "best_bound": None,
        "solve_time_sec": elapsed,
        "mip_gap": None,
        "has_incumbent": status in {"optimal", "feasible"},
        "solution_count": 1 if status in {"optimal", "feasible"} else 0,
        "solver_available": True,
        "variables": variables,
        "metrics": {},
        "notes": [],
    }


def run_selected_solver(solver_name: str, time_limit: int) -> dict:
    ensure_dirs()
    missing = check_inputs()
    if missing:
        result = failure_result(f"pyomo-{solver_name}", RuntimeError(f"missing required inputs: {', '.join(missing)}"))
    else:
        try:
            print(f"Solve budget: backend=pyomo-{solver_name}, time_limit_sec={time_limit}")
            print("Phase: loading_data")
            result = solve(load_public_data(), solver_name, time_limit)
        except Exception as error:
            result = failure_result(f"pyomo-{solver_name}", error)
        finally:
            PYOMO_LOG_PATH.unlink(missing_ok=True)
    write_json(RUN_OUTPUT_DIR / "solution_summary.json", {k: v for k, v in result.items() if k != "variables"})
    return result


def run_formal_solve(time_limit: int) -> dict:
    """Run the default solver backend."""
    return run_selected_solver(DEFAULT_PYOMO_SOLVER, time_limit)


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

    result = run_selected_solver(args.solver, args.time_limit)
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
