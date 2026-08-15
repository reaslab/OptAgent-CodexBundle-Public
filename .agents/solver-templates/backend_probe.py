"""Runtime solver capability probe for solver cases.

This script is intentionally self-contained and safe to run before a formal
solve. It checks imports, selected native binaries, license-sensitive smoke
solves, and OR-Tools native backend linkage. Write the JSON output to
`.optagent/cases/{case_id}/solver/backend_registry.json`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable


COPT_DEP_LIB = "/opt/copt/lib/python/deps"


def add_copt_runtime_path() -> None:
    """Patch the current process for COPT packages that keep deps separately."""
    if not Path(COPT_DEP_LIB).exists():
        return
    current = os.environ.get("LD_LIBRARY_PATH", "")
    parts = [part for part in current.split(":") if part]
    if COPT_DEP_LIB not in parts:
        os.environ["LD_LIBRARY_PATH"] = ":".join(parts + [COPT_DEP_LIB])


def import_available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def safe_call(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    saved_stdout = os.dup(1)
    saved_stderr = os.dup(2)
    result: dict[str, Any] | None = None
    error: Exception | None = None
    trace_tail: list[str] = []

    with tempfile.TemporaryFile() as stdout_capture, tempfile.TemporaryFile() as stderr_capture:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(stdout_capture.fileno(), 1)
            os.dup2(stderr_capture.fileno(), 2)
            try:
                result = fn()
            except Exception as exc:
                error = exc
                trace_tail = traceback.format_exc().splitlines()[-6:]
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(saved_stdout, 1)
            os.dup2(saved_stderr, 2)
            os.close(saved_stdout)
            os.close(saved_stderr)

        stdout_capture.seek(0)
        stderr_capture.seek(0)
        captured_stdout = stdout_capture.read().decode("utf-8", errors="replace").strip()
        captured_stderr = stderr_capture.read().decode("utf-8", errors="replace").strip()

    if error is not None:
        record: dict[str, Any] = {
            "can_solve": False,
            "error_type": type(error).__name__,
            "error": str(error),
            "trace_tail": trace_tail,
        }
    else:
        record = result or {}

    if captured_stdout:
        record["captured_stdout_tail"] = tail_text(captured_stdout)
    if captured_stderr:
        record["captured_stderr_tail"] = tail_text(captured_stderr)
    return record


def tail_text(text: str, max_lines: int = 8, max_chars: int = 4000) -> str:
    tail = "\n".join(text.splitlines()[-max_lines:])
    if len(tail) > max_chars:
        return tail[-max_chars:]
    return tail


def status_code(value: Any) -> int | str:
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def test_pulp() -> dict[str, Any]:
    import pulp

    problem = pulp.LpProblem("capability_probe", pulp.LpMinimize)
    x = pulp.LpVariable("x", lowBound=0)
    problem += x
    problem += x >= 1
    status_code = problem.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=10))
    status = pulp.LpStatus.get(status_code, str(status_code))
    return {
        "can_solve": status == "Optimal",
        "status": status,
        "objective": pulp.value(problem.objective),
        "available_solvers": pulp.listSolvers(onlyAvailable=True),
    }


def test_ortools_linear() -> dict[str, Any]:
    from ortools.linear_solver import pywraplp

    native: dict[str, dict[str, Any]] = {}
    for name in [
        "SCIP",
        "CBC_MIXED_INTEGER_PROGRAMMING",
        "CBC",
        "GLOP_LINEAR_PROGRAMMING",
        "GLOP",
        "CLP_LINEAR_PROGRAMMING",
        "CLP",
        "SAT_INTEGER_PROGRAMMING",
        "SAT",
    ]:
        solver = pywraplp.Solver.CreateSolver(name)
        if solver is None:
            native[name] = {"created": False, "can_solve": False}
            continue
        x = solver.NumVar(0, solver.infinity(), "x")
        solver.Minimize(x)
        solver.Add(x >= 1)
        solver.SetTimeLimit(10_000)
        status = solver.Solve()
        native[name] = {
            "created": True,
            "can_solve": status == pywraplp.Solver.OPTIMAL,
            "status_code": status,
            "optimal_code": pywraplp.Solver.OPTIMAL,
            "objective": solver.Objective().Value() if status == pywraplp.Solver.OPTIMAL else None,
        }
    return {
        "can_solve": any(value["can_solve"] for value in native.values()),
        "native_solvers": native,
    }


def test_ortools_cpsat() -> dict[str, Any]:
    from ortools.sat.python import cp_model

    model = cp_model.CpModel()
    x = model.NewIntVar(0, 10, "x")
    model.Add(x >= 1)
    model.Minimize(x)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10
    status = solver.Solve(model)
    return {
        "can_solve": status in (cp_model.OPTIMAL, cp_model.FEASIBLE),
        "status_code": status_code(status),
        "optimal_code": status_code(cp_model.OPTIMAL),
        "objective": solver.ObjectiveValue() if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None,
    }


def test_gurobipy() -> dict[str, Any]:
    import gurobipy as gp

    env = gp.Env(empty=True)
    model = None
    try:
        # Suppress the default workspace log before license initialization.
        env.setParam("OutputFlag", 0)
        env.setParam("LogFile", "")
        env.start()
        model = gp.Model("capability_probe", env=env)
        x = model.addVar(lb=0, name="x")
        model.setObjective(x, gp.GRB.MINIMIZE)
        model.addConstr(x >= 1)
        model.optimize()
        return {
            "can_solve": model.Status == gp.GRB.OPTIMAL,
            "status": model.Status,
            "objective": model.ObjVal if model.SolCount else None,
            "version": gp.gurobi.version(),
        }
    finally:
        if model is not None:
            model.dispose()
        env.dispose()


def test_coptpy() -> dict[str, Any]:
    add_copt_runtime_path()
    import coptpy as cp

    env = cp.Envr()
    model = env.createModel("capability_probe")
    x = model.addVar(lb=0, name="x")
    model.setObjective(x, cp.COPT.MINIMIZE)
    model.addConstr(x >= 1)
    model.setParam(cp.COPT.Param.Logging, 0)
    model.solve()
    return {
        "can_solve": model.status == cp.COPT.OPTIMAL,
        "status": model.status,
        "objective": getattr(model, "objval", None),
        "ld_library_path_includes_deps": COPT_DEP_LIB in os.environ.get("LD_LIBRARY_PATH", "").split(":"),
    }


def test_docplex() -> dict[str, Any]:
    from docplex.mp.model import Model

    model = Model(name="capability_probe")
    x = model.continuous_var(lb=0, name="x")
    model.minimize(x)
    model.add_constraint(x >= 1)
    solution = model.solve(log_output=False)
    return {
        "can_solve": solution is not None,
        "status": str(model.solve_details.status),
        "objective": solution.objective_value if solution else None,
    }


def test_mosek_fusion() -> dict[str, Any]:
    import mosek
    import mosek.fusion as mf

    with mf.Model("capability_probe") as model:
        x = model.variable("x", 1, mf.Domain.greaterThan(0.0))
        model.constraint(x, mf.Domain.greaterThan(1.0))
        model.objective(mf.ObjectiveSense.Minimize, x.index(0))
        model.solve()
        return {
            "can_solve": True,
            "objective": model.primalObjValue(),
            "version": mosek.Env.getversion(),
        }


def package_record(module: str, smoke: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "import_name": module,
        "import_available": import_available(module),
        "can_solve": False,
    }
    if record["import_available"] and smoke is not None:
        record.update(safe_call(smoke))
    return record


def main() -> int:
    add_copt_runtime_path()
    result: dict[str, Any] = {
        "schema_version": 1,
        "python": {
            "executable": sys.executable,
            "version": sys.version,
        },
        "environment": {
            "grb_license_file_set": bool(os.environ.get("GRB_LICENSE_FILE")),
            "mosek_license_file_set": bool(os.environ.get("MOSEKLM_LICENSE_FILE")),
            "copt_license_dir_set": bool(os.environ.get("COPT_LICENSE_DIR")),
        },
        "binaries": {
            name: shutil.which(name) is not None
            for name in [
                "gurobi_cl",
                "cbc",
                "glpsol",
                "highs",
                "ipopt",
                "scip",
                "cplex",
                "xpress",
                "mosek",
                "ampl",
                "gams",
                "julia",
                "matlab",
                "Rscript",
            ]
        },
        "backends": {},
    }

    backends = result["backends"]
    backends["pulp"] = package_record("pulp", test_pulp)
    backends["ortools-linear"] = package_record("ortools.linear_solver.pywraplp", test_ortools_linear)
    backends["ortools-cp-sat"] = package_record("ortools.sat.python.cp_model", test_ortools_cpsat)
    backends["gurobipy"] = package_record("gurobipy", test_gurobipy)
    backends["coptpy"] = package_record("coptpy", test_coptpy)
    backends["docplex"] = package_record("docplex.mp.model", test_docplex)
    backends["mosek-fusion"] = package_record("mosek.fusion", test_mosek_fusion)
    backends["cvxpy"] = package_record("cvxpy")
    backends["pyomo"] = package_record("pyomo.environ")
    backends["pyscipopt"] = package_record("pyscipopt")
    backends["xpress"] = package_record("xpress")
    backends["highspy"] = package_record("highspy")

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
