"""Selected-backend adapters for frozen Pyomo models.

The module deliberately has no numerical-library imports at module import
time.  It translates one already-built Pyomo model through one selected solver
interface and returns the raw primal observation plus only a bound whose
source and direction are known for that interface.
"""

from __future__ import annotations

import importlib
import math
import os
import platform
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from typing import Any, Mapping

from .contracts import ContractError


SUPPORTED_BACKEND_IDS = frozenset(
    {
        "highs",
        "cplex",
        "scip",
        "glpk",
        "cbc",
        "xpress",
        "gurobi",
        "mosek",
        "ipopt",
        "cp_optimizer",
        "cvxpy_copt",
        "cvxpy_clarabel",
        "cvxpy_cvxopt",
        "cvxpy_scs",
        "cvxpy_osqp",
        "cvxpy_glop",
        "cvxpy_pdlp",
        "ortools_clp",
        "cp_sat",
    }
)

DIRECT_PYTHON_BACKEND_ID = "direct_python"


@dataclass(frozen=True)
class SolverObservation:
    """One solver invocation and its unmodified primal observation."""

    record: Mapping[str, Any]
    backend: Mapping[str, Any]
    termination: str
    raw_status: str
    solver_invoked: bool
    solver_returned: bool
    primals: Mapping[Any, Any]
    solver_incumbent_objective: float | None
    bound_value: float | None
    nonfinite_fields: Mapping[str, str]
    solve_wall_seconds: float
    error: str | None
    native_response_solution: tuple[int, ...] | None = None
    native_model_proto: bytes | None = None
    native_solution_hint_count: int | None = None


_BACKEND_SPECS: dict[str, dict[str, Any]] = {
    "highs": {"engine": "HiGHS", "interface": "Pyomo APPSI", "factory": "appsi_highs", "families": ["LP/MIP; nonlinear expressions unsupported by this adapter"]},
    "cplex": {"engine": "CPLEX", "interface": "Pyomo SolverFactory", "factory": "cplex", "families": ["LP/MIP and representative quadratic-polynomial QP/QCP families; current Pyomo factory probe"]},
    "scip": {"engine": "SCIP", "interface": "Pyomo SolverFactory", "factory": "scip", "families": ["LP/MIP and representative quadratic-polynomial QP/QCP families; current Pyomo factory probe"]},
    "glpk": {"engine": "GLPK", "interface": "Pyomo SolverFactory", "factory": "glpk", "families": ["linear-expression LP/MIP"]},
    "cbc": {"engine": "CBC", "interface": "Pyomo SolverFactory", "factory": "cbc", "families": ["linear-expression LP/MIP"]},
    "xpress": {"engine": "Xpress", "interface": "Pyomo SolverFactory", "factory": "xpress", "families": ["LP/MIP and representative quadratic-polynomial QP/QCP families; current Pyomo factory probe"]},
    "gurobi": {"engine": "Gurobi", "interface": "Pyomo SolverFactory", "factory": "gurobi", "families": ["LP/MIP and quadratic-polynomial QP/QCP path supported by current Pyomo plugin/writer; license/runtime acceptance not yet verified"]},
    "mosek": {"engine": "MOSEK", "interface": "Pyomo SolverFactory", "factory": "mosek", "families": ["LP/MIP and quadratic-polynomial QP/QCP path supported by current Pyomo plugin/writer; license/runtime acceptance not yet verified"]},
    "ipopt": {"engine": "IPOPT", "interface": "Pyomo SolverFactory", "factory": "ipopt", "families": ["continuous local NLP; integer models unsupported"]},
    "cp_optimizer": {"engine": "CP Optimizer", "interface": "Pyomo CP Optimizer", "factory": "cp_optimizer", "families": ["ordinary Var and linear Constraints with finite integer domains"]},
    "cvxpy_copt": {"engine": "COPT", "interface": "CVXPY standard-form bridge", "factory": "COPT", "families": ["LP/MIP/convex QP/QCP"]},
    "cvxpy_clarabel": {"engine": "CLARABEL", "interface": "CVXPY standard-form bridge", "factory": "CLARABEL", "families": ["continuous DCP QP/QCP"]},
    "cvxpy_cvxopt": {"engine": "CVXOPT", "interface": "CVXPY standard-form bridge", "factory": "CVXOPT", "families": ["continuous DCP QP/QCP"]},
    "cvxpy_scs": {"engine": "SCS", "interface": "CVXPY standard-form bridge", "factory": "SCS", "families": ["continuous DCP QP/QCP"]},
    "cvxpy_osqp": {"engine": "OSQP", "interface": "CVXPY standard-form bridge", "factory": "OSQP", "families": ["continuous convex QP with affine constraints"]},
    "cvxpy_glop": {"engine": "GLOP", "interface": "CVXPY standard-form bridge", "factory": "GLOP", "families": ["LP"]},
    "cvxpy_pdlp": {"engine": "PDLP", "interface": "CVXPY standard-form bridge", "factory": "PDLP", "families": ["LP"]},
    "ortools_clp": {"engine": "CLP", "interface": "OR-Tools MPSolver", "factory": "CLP", "families": ["LP"]},
    "cp_sat": {"engine": "CP-SAT", "interface": "OR-Tools native CpModel", "factory": "CpModel", "families": ["native CpModel with finite integer domains and OR-Tools-validated integer constraints; exact integer scalar objective or feasibility plan (no lexicographic plan); Core ModelCheck does not independently recheck CP global constraints"]},
}

_DISTRIBUTIONS = {
    "highs": ("pyomo", "highspy"),
    "cplex": ("pyomo", "cplex"),
    "scip": ("pyomo", "pyscipopt"),
    "glpk": ("pyomo",),
    "cbc": ("pyomo",),
    "xpress": ("pyomo", "xpress"),
    "gurobi": ("pyomo", "gurobipy"),
    "mosek": ("pyomo", "mosek"),
    "ipopt": ("pyomo",),
    "cp_optimizer": ("pyomo", "docplex"),
    "cvxpy_copt": ("pyomo", "cvxpy", "coptpy"),
    "cvxpy_clarabel": ("pyomo", "cvxpy", "clarabel"),
    "cvxpy_cvxopt": ("pyomo", "cvxpy", "cvxopt"),
    "cvxpy_scs": ("pyomo", "cvxpy", "scs"),
    "cvxpy_osqp": ("pyomo", "cvxpy", "osqp"),
    "cvxpy_glop": ("pyomo", "cvxpy", "ortools"),
    "cvxpy_pdlp": ("pyomo", "cvxpy", "ortools"),
    "ortools_clp": ("pyomo", "ortools"),
    "cp_sat": ("pyomo", "ortools"),
}

# Native controls are enabled only where the selected adapter has a stable,
# backend-specific setting.  Core gap targets and witness tolerances remain
# independent of whether a backend also receives a native gap setting.
_NATIVE_FEATURES: dict[str, frozenset[str]] = {
    "highs": frozenset({"time_limit_seconds", "threads", "random_seed", "relative_gap", "absolute_gap"}),
    "cplex": frozenset({"time_limit_seconds", "threads", "random_seed", "relative_gap", "absolute_gap"}),
    "scip": frozenset({"time_limit_seconds", "threads", "random_seed", "relative_gap", "absolute_gap"}),
    "glpk": frozenset({"time_limit_seconds", "relative_gap"}),
    "cbc": frozenset({"time_limit_seconds", "threads", "random_seed", "relative_gap", "absolute_gap"}),
    "xpress": frozenset({"time_limit_seconds", "threads", "random_seed", "relative_gap", "absolute_gap"}),
    "gurobi": frozenset({"time_limit_seconds", "threads", "random_seed", "relative_gap", "absolute_gap"}),
    "mosek": frozenset({"time_limit_seconds", "threads", "random_seed", "relative_gap", "absolute_gap"}),
    "ipopt": frozenset(),
    "cp_optimizer": frozenset({"time_limit_seconds", "threads", "random_seed", "relative_gap", "absolute_gap"}),
    "cvxpy_copt": frozenset({"time_limit_seconds", "threads", "relative_gap", "absolute_gap"}),
    "cvxpy_clarabel": frozenset({"time_limit_seconds", "threads", "relative_gap", "absolute_gap"}),
    "cvxpy_cvxopt": frozenset({"relative_gap", "absolute_gap"}),
    "cvxpy_scs": frozenset({"time_limit_seconds"}),
    "cvxpy_osqp": frozenset({"time_limit_seconds"}),
    "cvxpy_glop": frozenset({"time_limit_seconds", "threads", "random_seed"}),
    "cvxpy_pdlp": frozenset({"time_limit_seconds", "threads", "relative_gap", "absolute_gap"}),
    "ortools_clp": frozenset({"time_limit_seconds"}),
    "cp_sat": frozenset({"time_limit_seconds", "threads", "random_seed", "relative_gap", "absolute_gap"}),
}

_CVXPY_SOLVERS = {
    "cvxpy_copt": "COPT",
    "cvxpy_clarabel": "CLARABEL",
    "cvxpy_cvxopt": "CVXOPT",
    "cvxpy_scs": "SCS",
    "cvxpy_osqp": "OSQP",
    "cvxpy_glop": "GLOP",
    "cvxpy_pdlp": "PDLP",
}

_STATUS_MAP = {
    "optimal": "optimal",
    "locallyoptimal": "local_optimal",
    "localoptimal": "local_optimal",
    "feasible": "feasible",
    "infeasible": "infeasible",
    "unbounded": "unbounded",
    "infeasibleorunbounded": "infeasible_or_unbounded",
    "maxtimelimit": "time_limit",
    "timelimit": "time_limit",
    "maxiterations": "iteration_limit",
    "maxevaluations": "iteration_limit",
    "iterationlimit": "iteration_limit",
    "userlimit": "unknown",
    "error": "error",
    "solvererror": "error",
    "abnormal": "error",
    "unknown": "unknown",
}


def _require_backend(backend_id: str) -> dict[str, Any]:
    if not isinstance(backend_id, str) or backend_id not in SUPPORTED_BACKEND_IDS:
        raise ContractError(
            "capability_uncovered",
            "the selected solver backend is not registered",
            pointer="/candidate/backend_id",
            details={"backend_id": backend_id if isinstance(backend_id, str) else None},
        )
    return _BACKEND_SPECS[backend_id]


def validate_direct_python_options(solve_options: Mapping[str, Any]) -> None:
    """Reject native-only controls when a candidate executes its own algorithm."""
    unsupported = {
        "time_limit_seconds": solve_options.get("time_limit_seconds") is not None,
        "threads": solve_options.get("threads") is not None,
        "random_seed": solve_options.get("random_seed") is not None,
        "warm_start": solve_options.get("warm_start", False) is True,
        "write_log": solve_options.get("write_log", False) is True,
        "export_model": solve_options.get("export_model", False) is True,
    }
    for name, requested in unsupported.items():
        if requested:
            raise ContractError(
                "unsupported_option",
                f"solve_options.{name} is only supported by a compatible native solver adapter",
                pointer=f"/solve_options/{name}",
                details={"option": name, "backend_id": DIRECT_PYTHON_BACKEND_ID},
            )


def _direct_python_requirement_entries(requirements_bytes: bytes) -> tuple[tuple[str, str | None, bool], ...]:
    """Return declared distribution names and their PEP 508 markers.

    This intentionally reads only the frozen root requirements file. It is not
    a resolver and does not inspect installed distributions to discover names.
    """
    if not isinstance(requirements_bytes, bytes):
        raise ContractError("output_invalid", "frozen root requirements must be bytes", pointer="/requirements.txt")
    try:
        text = requirements_bytes.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ContractError("output_invalid", "frozen root requirements must be UTF-8", pointer="/requirements.txt") from exc

    entries: list[tuple[str, str | None, bool]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        egg = re.search(r"#egg=([A-Za-z0-9][A-Za-z0-9._-]*)", stripped)
        if stripped.startswith(("-e ", "--editable ")):
            if egg is not None:
                name = egg.group(1)
                has_constraint = True
            else:
                continue
        elif stripped.startswith(("-", "--")):
            # pip index/find-links and other options do not name a distribution.
            continue
        else:
            base = stripped.split(";", 1)[0]
            match = re.match(r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)(?=$|\s|\[|[<>=!~@;])", base)
            if match is None:
                if egg is None:
                    continue
                name = egg.group(1)
                has_constraint = True
            else:
                name = match.group(1)
                rest = base[match.end():].lstrip()
                if rest.startswith("["):
                    extra_end = rest.find("]")
                    if extra_end < 0:
                        continue
                    rest = rest[extra_end + 1:].lstrip()
                has_constraint = rest.startswith(("@", "===", "==", "~=", "!=", "<=", ">=", "<", ">"))
        marker = None
        if ";" in stripped:
            marker = stripped.split(";", 1)[1].split("#", 1)[0].strip() or None
        canonical_name = re.sub(r"[-_.]+", "-", name).lower()
        entries.append((canonical_name, marker, has_constraint))
    return tuple(entries)


def direct_python_profile(requirements_bytes: bytes = b"") -> dict[str, Any]:
    package_versions: dict[str, str] = {}
    for distribution, _, _ in _direct_python_requirement_entries(requirements_bytes):
        try:
            version = importlib_metadata.version(distribution)
        except importlib_metadata.PackageNotFoundError:
            continue
        package_versions[distribution] = str(version)
    return {
        "id": DIRECT_PYTHON_BACKEND_ID,
        "engine": "Python direct algorithm",
        "interface": "solve(bundle)",
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "package_versions": dict(sorted(package_versions.items())),
    }


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _nonnegative_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def _raise_option(name: str, message: str) -> None:
    raise ContractError(
        "unsupported_option",
        message,
        pointer=f"/solve_options/{name}",
        details={"option": name},
    )


def validate_solver_options(backend_id: str, solve_options: Mapping[str, Any]) -> None:
    """Validate the frozen options and reject unsupported explicit controls.

    `relative_gap` and `absolute_gap` are Core acceptance criteria.  Their
    absence from `_NATIVE_FEATURES` never blocks a backend.
    """
    _require_backend(backend_id)
    if not isinstance(solve_options, Mapping):
        raise ContractError("invalid_argument", "solve options must be an object", pointer="/solve_options")
    warm_start = solve_options.get("warm_start", False)
    if type(warm_start) is not bool:
        raise ContractError("invalid_argument", "warm_start must be a boolean", pointer="/solve_options/warm_start")

    required = (
        "time_limit_seconds",
        "relative_gap",
        "absolute_gap",
        "threads",
        "random_seed",
        "feasibility_tolerance",
        "integrality_tolerance",
    )
    missing = [name for name in required if name not in solve_options]
    if missing:
        raise ContractError(
            "invalid_argument",
            "solve options are missing required fields",
            pointer="/solve_options",
            details={"missing": missing},
        )

    time_limit = solve_options["time_limit_seconds"]
    if time_limit is not None and (_finite_number(time_limit) is None or float(time_limit) <= 0):
        raise ContractError("invalid_argument", "native time limit must be null or finite and positive", pointer="/solve_options/time_limit_seconds")
    for name in ("relative_gap", "absolute_gap"):
        value = solve_options[name]
        finite = _finite_number(value)
        if finite is None or finite < 0:
            raise ContractError("invalid_argument", "Core gap target must be finite and nonnegative", pointer=f"/solve_options/{name}")
    if solve_options["threads"] is not None and not _positive_int(solve_options["threads"]):
        raise ContractError("invalid_argument", "threads must be null or a positive integer", pointer="/solve_options/threads")
    if solve_options["random_seed"] is not None and not _nonnegative_int(solve_options["random_seed"]):
        raise ContractError("invalid_argument", "random seed must be null or a nonnegative integer", pointer="/solve_options/random_seed")
    for name in ("feasibility_tolerance", "integrality_tolerance"):
        finite = _finite_number(solve_options[name])
        if finite is None or finite < 1e-10:
            raise ContractError("invalid_argument", "checker tolerance must be finite and at least 1e-10", pointer=f"/solve_options/{name}")

    supported = _NATIVE_FEATURES.get(backend_id, frozenset())
    explicit = {
        "time_limit_seconds": time_limit is not None,
        "threads": solve_options["threads"] is not None,
        "random_seed": solve_options["random_seed"] is not None,
    }
    for name, requested in explicit.items():
        if requested and name not in supported:
            _raise_option(name, f"{backend_id} has no verified native {name} mapping")
    if warm_start and (
        backend_id in _CVXPY_SOLVERS
        or backend_id in {"ortools_clp", "cp_optimizer", "cp_sat"}
    ):
        _raise_option("warm_start", f"{backend_id} adapter has no warm-start submission path")
    if backend_id == "cbc" and solve_options["random_seed"] == 0:
        _raise_option("random_seed", "CBC seed zero selects time-based seeding and cannot honor a fixed seed request")


def _ceil_native_milliseconds(seconds: float) -> int:
    """Round a positive time cap up to the millisecond unit used by wrappers."""
    scaled = float(seconds) * 1000.0
    if not math.isfinite(scaled):
        raise ContractError(
            "invalid_argument",
            "native time limit is too large for millisecond precision",
            pointer="/solve_options/time_limit_seconds",
        )
    return max(1, math.ceil(scaled))


def _seconds_float_for_native_milliseconds(milliseconds: int) -> float:
    """Avoid a float-rounding truncation in wrappers that cast ms to int."""
    seconds = milliseconds / 1000.0
    while int(seconds * 1000.0) < milliseconds:
        seconds = math.nextafter(seconds, math.inf)
    return seconds


def _ceil_native_seconds(seconds: float) -> int:
    """Round a positive time cap up to a whole second for integer controls."""
    return max(1, math.ceil(float(seconds)))


def _pyomo_native_options(
    backend_id: str,
    *,
    time_limit_seconds: float | None,
    relative_gap: float,
    absolute_gap: float,
    threads: int | None,
    random_seed: int | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the selected Pyomo plugin options and their scalar record."""
    options: dict[str, Any] = {}
    parameters: dict[str, Any] = {}

    def set_option(name: str, value: Any) -> None:
        options[name] = value
        parameters[name] = value

    if backend_id == "cp_optimizer":
        if time_limit_seconds is not None:
            set_option("TimeLimit", float(time_limit_seconds))
            set_option("TimeMode", "ElapsedTime")
        if threads is not None:
            set_option("Workers", threads)
        if random_seed is not None:
            set_option("RandomSeed", random_seed)
        set_option("RelativeOptimalityTolerance", float(relative_gap))
        # DOcplex requires OptimalityTolerance > 0.  A Core absolute target of
        # zero remains solely a Core criterion and is not rewritten.
        if absolute_gap > 0:
            set_option("OptimalityTolerance", float(absolute_gap))
        return options, parameters

    if backend_id == "cplex":
        if time_limit_seconds is not None:
            set_option("timelimit", float(time_limit_seconds))
        if threads is not None:
            set_option("threads", threads)
        if random_seed is not None:
            set_option("randomseed", random_seed)
        set_option("mip_tolerances_mipgap", float(relative_gap))
        set_option("mip_tolerances_absmipgap", float(absolute_gap))
    elif backend_id == "scip":
        if time_limit_seconds is not None:
            set_option("limits/time", float(time_limit_seconds))
        if threads is not None:
            set_option("parallel/maxnthreads", threads)
        if random_seed is not None:
            set_option("randomization/randomseedshift", random_seed)
        set_option("limits/gap", float(relative_gap))
        set_option("limits/absgap", float(absolute_gap))
    elif backend_id == "glpk":
        if time_limit_seconds is not None:
            set_option("tmlim", _ceil_native_seconds(time_limit_seconds))
        set_option("mipgap", float(relative_gap))
    elif backend_id == "cbc":
        if time_limit_seconds is not None:
            set_option("seconds", float(time_limit_seconds))
        if threads is not None:
            set_option("threads", threads)
        if random_seed is not None:
            set_option("randomSeed", random_seed)
            set_option("randomCbcSeed", random_seed)
        set_option("ratioGap", float(relative_gap))
        set_option("allowableGap", float(absolute_gap))
    elif backend_id == "xpress":
        if time_limit_seconds is not None:
            # A negative MAXTIME is Xpress's hard elapsed-time cap, including
            # the case where no incumbent has yet been found.
            set_option("maxtime", -_ceil_native_seconds(time_limit_seconds))
        if threads is not None:
            set_option("threads", threads)
        if random_seed is not None:
            set_option("randomseed", random_seed)
        set_option("miprelstop", float(relative_gap))
        set_option("mipabsstop", float(absolute_gap))
    elif backend_id == "gurobi":
        if time_limit_seconds is not None:
            set_option("TimeLimit", float(time_limit_seconds))
        if threads is not None:
            set_option("Threads", threads)
        if random_seed is not None:
            set_option("Seed", random_seed)
        set_option("MIPGap", float(relative_gap))
        set_option("MIPGapAbs", float(absolute_gap))
    elif backend_id == "mosek":
        if time_limit_seconds is not None:
            set_option("dparam.optimizer_max_time", float(time_limit_seconds))
        if threads is not None:
            set_option("iparam.num_threads", threads)
        if random_seed is not None:
            set_option("iparam.mio_seed", random_seed)
        set_option("dparam.mio_tol_rel_gap", float(relative_gap))
        set_option("dparam.mio_tol_abs_gap", float(absolute_gap))
    elif backend_id == "ipopt":
        pass
    else:
        raise ContractError("capability_uncovered", f"no Pyomo native option map is registered for {backend_id}")
    return options, parameters


def _cvxpy_native_options(
    backend_id: str,
    *,
    time_limit_seconds: float | None,
    relative_gap: float,
    absolute_gap: float,
    threads: int | None,
    random_seed: int | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build only the option shape accepted by the selected CVXPY wrapper."""
    options: dict[str, Any] = {}
    parameters: dict[str, Any] = {}

    def set_option(name: str, value: Any, *, record_name: str | None = None) -> None:
        options[name] = value
        parameters[record_name or name] = value

    if backend_id == "cvxpy_copt":
        if time_limit_seconds is not None:
            set_option("TimeLimit", float(time_limit_seconds))
        if threads is not None:
            set_option("Threads", threads)
        set_option("RelGap", float(relative_gap))
        set_option("AbsGap", float(absolute_gap))
    elif backend_id == "cvxpy_clarabel":
        if time_limit_seconds is not None:
            set_option("time_limit", float(time_limit_seconds))
        if threads is not None:
            set_option("max_threads", threads)
        set_option("tol_gap_rel", float(relative_gap))
        set_option("tol_gap_abs", float(absolute_gap))
    elif backend_id == "cvxpy_cvxopt":
        set_option("reltol", float(relative_gap))
        # Core permits zero; omitting zero leaves CVXOPT's positive default available.
        if absolute_gap > 0:
            set_option("abstol", float(absolute_gap))
    elif backend_id == "cvxpy_scs":
        if time_limit_seconds is not None:
            set_option("time_limit_secs", float(time_limit_seconds))
    elif backend_id == "cvxpy_osqp":
        if time_limit_seconds is not None:
            set_option("time_limit", float(time_limit_seconds))
    elif backend_id == "cvxpy_glop":
        from ortools.glop import parameters_pb2

        if time_limit_seconds is not None:
            milliseconds = _ceil_native_milliseconds(time_limit_seconds)
            set_option("time_limit_sec", _seconds_float_for_native_milliseconds(milliseconds))
        proto = parameters_pb2.GlopParameters()
        has_proto_option = False
        if threads is not None:
            proto.num_omp_threads = threads
            parameters["parameters_proto.num_omp_threads"] = threads
            has_proto_option = True
        if random_seed is not None:
            proto.random_seed = random_seed
            parameters["parameters_proto.random_seed"] = random_seed
            has_proto_option = True
        if has_proto_option:
            options["parameters_proto"] = proto
    elif backend_id == "cvxpy_pdlp":
        from ortools.pdlp import solvers_pb2

        if time_limit_seconds is not None:
            set_option("time_limit_sec", float(time_limit_seconds))
        proto = solvers_pb2.PrimalDualHybridGradientParams()
        has_proto_option = False
        if threads is not None:
            proto.num_threads = threads
            parameters["parameters_proto.num_threads"] = threads
            has_proto_option = True
        proto.termination_criteria.simple_optimality_criteria.eps_optimal_relative = float(relative_gap)
        proto.termination_criteria.simple_optimality_criteria.eps_optimal_absolute = float(absolute_gap)
        parameters["parameters_proto.termination_criteria.simple_optimality_criteria.eps_optimal_relative"] = float(relative_gap)
        parameters["parameters_proto.termination_criteria.simple_optimality_criteria.eps_optimal_absolute"] = float(absolute_gap)
        has_proto_option = True
        if has_proto_option:
            options["parameters_proto"] = proto
    else:
        raise ContractError("capability_uncovered", f"no CVXPY native option map is registered for {backend_id}")
    return options, parameters


def _distribution_version(name: str) -> str | None:
    try:
        return importlib_metadata.version(name)
    except importlib_metadata.PackageNotFoundError:
        return None
    except Exception:
        return None


def _selected_interface_available(backend_id: str) -> tuple[bool | None, str | None]:
    """Check only the chosen adapter; never enumerate or solve a probe model."""
    spec = _require_backend(backend_id)
    try:
        if backend_id in _CVXPY_SOLVERS:
            defines = importlib.import_module("cvxpy.reductions.solvers.defines")
            solver_name = _CVXPY_SOLVERS[backend_id]
            interface = getattr(defines, "SOLVER_MAP_QP", {}).get(solver_name)
            if interface is None:
                interface = getattr(defines, "SOLVER_MAP_CONIC", {}).get(solver_name)
            if interface is None:
                return False, "selected CVXPY solver is not registered"
            installed_check = getattr(interface, "is_installed", None)
            if callable(installed_check):
                available = bool(installed_check())
                return available, None if available else "selected CVXPY solver is not installed"
            # Only import the dependency for the selected solver.  Do not call
            # installed_solvers(), which probes every registered solver.
            dependencies = _DISTRIBUTIONS[backend_id][1:]
            if dependencies:
                importlib.import_module(dependencies[0])
                return True, None
            return None, "selected CVXPY solver has no targeted availability check"
        if backend_id in {"ortools_clp", "cp_sat"}:
            if backend_id == "cp_sat":
                importlib.import_module("ortools.sat.python.cp_model")
                return True, None
            pywraplp = importlib.import_module("ortools.linear_solver.pywraplp")
            solver = pywraplp.Solver.CreateSolver("CLP")
            return (solver is not None), None if solver is not None else "selected MPSolver CLP interface is unavailable"

        pyomo = importlib.import_module("pyomo.environ")
        solver = pyomo.SolverFactory(spec["factory"])
        available = solver.available(exception_flag=False)
        return bool(available), None if available else "selected Pyomo interface is unavailable"
    except Exception as exc:
        return False, type(exc).__name__


def backend_profile(backend_id: str) -> dict[str, Any]:
    """Describe only the selected interface and this worker's observed setup."""
    spec = _require_backend(backend_id)
    versions = {name: version for name in _DISTRIBUTIONS[backend_id] if (version := _distribution_version(name)) is not None}
    available, diagnostic = _selected_interface_available(backend_id)
    return {
        "id": backend_id,
        "engine": spec["engine"],
        "interface": spec["interface"],
        "interface_key": spec["factory"],
        "model_families": list(spec["families"]),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "os": platform.system(),
        "architecture": platform.machine(),
        "package_versions": versions,
        "interface_available": available,
        "interface_diagnostic": diagnostic,
        "license_status": "unknown_until_selected_solve",
        "license_scope": "unknown",
    }


# Keep the spelling used by worker/runner call sites as an explicit alias.
backend_profile_for = backend_profile


def _enum_name(value: Any) -> str:
    name = getattr(value, "name", None)
    return name if isinstance(name, str) else str(value).rsplit(".", 1)[-1]


def _status_name(value: Any) -> str:
    text = _enum_name(value)
    return text.casefold().replace("_", "").replace(" ", "")


def _normalize_termination(
    value: Any,
    *,
    backend_id: str,
    has_primal: bool,
    nonlinear_model: bool = False,
) -> str:
    raw = _status_name(value)
    mapped = _STATUS_MAP.get(raw, "unknown")
    if backend_id == "ipopt" and mapped == "optimal":
        return "local_optimal"
    if backend_id == "ipopt" and mapped in {"infeasible", "unbounded", "infeasible_or_unbounded"}:
        return "unknown"
    if backend_id == "xpress" and nonlinear_model and mapped in {"unbounded", "infeasible_or_unbounded"}:
        return "unknown"
    if mapped == "unknown" and has_primal:
        return "feasible"
    return mapped


def _safe_error(exc: BaseException) -> str:
    message = str(exc).replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    # Native libraries sometimes echo environment paths or license locations.
    message = re.sub(r"(?:[A-Za-z]:\\|/)[^\s,'\"]+", "[path redacted]", message)
    message = re.sub(r"\b(?:token|password|secret|api[_ -]?key|licen[cs]e[_ -]?key)\s*[:=]\s*[^\s,;]+", "credential=[redacted]", message, flags=re.IGNORECASE)
    message = message[:500]
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _result_number(value: Any) -> float | None:
    finite = _finite_number(value)
    if finite is not None:
        return finite
    if getattr(value, "shape", None) == ():
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if math.isfinite(parsed) else None
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except (ValueError, OverflowError):
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def _param_record(
    *,
    time_limit_seconds: float | None,
    relative_gap: float,
    absolute_gap: float,
    threads: int | None,
    random_seed: int | None,
    feasibility_tolerance: float,
    integrality_tolerance: float,
    warm_start: bool = False,
    native_parameters: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str], dict[str, Any]]:
    requested = {
        "native": {
            "time_limit_seconds": time_limit_seconds,
            "threads": threads,
            "random_seed": random_seed,
            "warm_start": warm_start,
            "parameters": dict(native_parameters or {}),
        },
        "core": {
            "relative_gap": relative_gap,
            "absolute_gap": absolute_gap,
            "feasibility_tolerance": feasibility_tolerance,
            "integrality_tolerance": integrality_tolerance,
        },
    }
    effective = {
        "time_limit_seconds": None,
        "threads": None,
        "random_seed": None,
        "warm_start": None,
        "parameters": {},
    }
    statuses = {
        "time_limit_seconds": "not_requested" if time_limit_seconds is None else "submitted_no_readback",
        "threads": "not_requested" if threads is None else "submitted_no_readback",
        "random_seed": "not_requested" if random_seed is None else "submitted_no_readback",
        "warm_start": "submitted_no_readback" if warm_start else "not_requested",
        # These statuses describe the top-level Core/checker fields. An
        # adapter may also pass the same gap target through a backend-native
        # parameter, which is recorded separately in native.parameters.
        "relative_gap": "core_criterion_only",
        "absolute_gap": "core_criterion_only",
        "feasibility_tolerance": "core_checker_only",
        "integrality_tolerance": "core_checker_only",
    }
    criterion = {
        "relative_gap": relative_gap,
        "absolute_gap": absolute_gap,
        "formula": "gap <= max(absolute_gap, relative_gap * abs(checked_objective))",
        "source": "frozen_core_check",
    }
    return requested, effective, statuses, criterion


def _mark_unapplied_native_controls(
    statuses: dict[str, str],
    *,
    solver_invoked: bool,
    time_limit_seconds: float | None,
    threads: int | None,
    random_seed: int | None,
    warm_start: bool = False,
) -> None:
    if solver_invoked:
        return
    for name, value in (
        ("time_limit_seconds", time_limit_seconds),
        ("threads", threads),
        ("random_seed", random_seed),
    ):
        if value is not None:
            statuses[name] = "not_applied_solver_not_invoked"
    if warm_start:
        statuses["warm_start"] = "not_applied_solver_not_invoked"


def _pyomo_variables(model: Any) -> list[Any]:
    from pyomo.environ import Var

    return list(model.component_data_objects(Var, active=True, descend_into=True))


def _reject_unverified_components(model: Any, *, interface: str) -> None:
    unsupported_types = {"BooleanVar", "IntervalVar", "LogicalConstraint", "SOSConstraint", "Disjunction", "Disjunct"}
    for component in model.component_objects(active=True, descend_into=True):
        ctype_name = getattr(getattr(component, "ctype", None), "__name__", "")
        class_name = type(component).__name__
        if ctype_name in unsupported_types or class_name in unsupported_types:
            raise ContractError(
                "capability_uncovered",
                f"{interface} adapter does not cover {ctype_name or class_name} semantics",
            )


def _pyomo_has_integer_variables(variables: list[Any]) -> bool:
    for variable in variables:
        if variable.fixed:
            continue
        try:
            if variable.is_integer() or variable.is_binary():
                return True
        except Exception:
            domain = str(variable.domain).casefold()
            if "integer" in domain or "binary" in domain:
                return True
    return False


def _component_value(value: Any) -> float | None:
    if value is None:
        return None
    try:
        from pyomo.environ import value as pyomo_value

        return _finite_number(pyomo_value(value, exception=False))
    except Exception:
        return None


def _pyomo_bound_value(expression: Any, *, side: str) -> float | None:
    """Evaluate a bound and treat only its expected infinity as absent."""
    if expression is None:
        return None
    try:
        from pyomo.environ import value as pyomo_value

        observed = pyomo_value(expression, exception=False)
    except Exception as exc:
        raise ContractError("capability_uncovered", "solver bridge requires numeric bounds") from exc
    if isinstance(observed, bool) or not isinstance(observed, (int, float)):
        raise ContractError("capability_uncovered", "solver bridge requires numeric bounds")
    numeric = float(observed)
    if math.isinf(numeric):
        if side == "lower" and numeric < 0 or side == "upper" and numeric > 0:
            return None
        raise ContractError("capability_uncovered", "solver bound has infinity in the unsupported direction")
    if not math.isfinite(numeric):
        raise ContractError("capability_uncovered", "solver bridge does not accept NaN bounds")
    return numeric


def _solution_count(results: Any) -> int:
    try:
        return len(results.solution)
    except Exception:
        return 0


def _load_native_solution(model: Any, results: Any, variables: list[Any]) -> tuple[Mapping[Any, Any], bool | None, float | None]:
    """Explicitly load this result after clearing nonfixed prior values."""
    from pyomo.common.collections import ComponentMap

    if _solution_count(results) <= 0:
        return ComponentMap(), False, None
    old_values = ComponentMap()
    for variable in variables:
        old_values[variable] = variable.value
        if not variable.fixed:
            variable.set_value(None, skip_validation=True)
    try:
        model.solutions.load_from(results)
        primals = ComponentMap()
        complete = True
        for variable in variables:
            if variable.fixed:
                continue
            value = variable.value
            if value is None:
                complete = False
                continue
            primals[variable] = value
        return primals, bool(complete and len(primals) > 0), None
    finally:
        for variable, value in old_values.items():
            variable.set_value(value, skip_validation=True)


def _solver_result_status(results: Any) -> tuple[str, str]:
    solver_info = getattr(results, "solver", None)
    raw = _enum_name(getattr(solver_info, "termination_condition", "unknown"))
    status = _enum_name(getattr(solver_info, "status", "unknown"))
    if raw == "unknown" and status.casefold() in {"error", "aborted"}:
        raw = status
    return raw, status


def _pyomo_incumbent(results: Any, objective_sense: str | None, *, integer_model: bool, backend_id: str) -> float | None:
    if backend_id == "highs":
        return _result_number(getattr(results, "best_feasible_objective", None))
    solver_info = getattr(results, "solver", None)
    if backend_id == "scip":
        return _result_number(getattr(solver_info, "primal_bound", None))
    problem = getattr(results, "problem", None)
    field = "upper_bound" if objective_sense == "min" else "lower_bound" if objective_sense == "max" else None
    candidate = _result_number(getattr(problem, field, None)) if field else None
    if candidate is not None:
        return candidate
    try:
        solution = results.solution(0)
        objective = solution.objective
        if isinstance(objective, Mapping):
            if len(objective) == 1:
                return _result_number(next(iter(objective.values())))
        return _result_number(objective)
    except Exception:
        return None


def _pyomo_global_bound(results: Any, objective_sense: str | None, *, integer_model: bool, backend_id: str) -> tuple[float | None, dict[str, Any] | None, dict[str, Any] | None]:
    if objective_sense not in {"min", "max"}:
        return None, None, None
    raw: Any = None
    source: str | None = None
    if backend_id == "highs":
        raw = getattr(results, "best_objective_bound", None)
        source = "Pyomo APPSI HiGHS results.best_objective_bound"
    elif backend_id == "scip" and integer_model:
        # The SCIP plugin also sorts primal/dual values into lower/upper fields;
        # preserve the source dual bound directly instead of trusting that sort.
        raw = getattr(getattr(results, "solver", None), "dual_bound", None)
        source = "Pyomo SCIPAMPL results.solver.dual_bound"
    elif backend_id in {"cbc", "cplex", "gurobi", "xpress", "cp_optimizer"} and integer_model:
        problem = getattr(results, "problem", None)
        direction = "lower_bound" if objective_sense == "min" else "upper_bound"
        raw = getattr(problem, direction, None)
        source = f"Pyomo {backend_id} native MIP bound mapped to problem.{direction}"
    finite = _result_number(raw)
    if source is None:
        return None, None, None
    raw_record = {"source": source, "raw_value": finite, "raw_repr": repr(raw)}
    if finite is None:
        return None, None, raw_record
    evidence = {
        "source": source,
        "scope": "global",
        "objective_sense": objective_sense,
        "value_direction": "lower" if objective_sense == "min" else "upper",
        "interface": backend_id,
        "objective_transform": {
            "kind": "identity",
            "formula": "native bound is already in original Pyomo objective units",
        },
        "raw_value": finite,
    }
    return finite, evidence, raw_record


def _bound_direction_ok(bound: float | None, incumbent: float | None, sense: str | None) -> bool:
    if bound is None or sense not in {"min", "max"}:
        return False
    if incumbent is None:
        return True
    return bound <= incumbent if sense == "min" else bound >= incumbent


def _set_log_file(path: str | None, line: str | None = None) -> None:
    if path is None:
        return
    with open(path, "a", encoding="utf-8") as stream:
        if line:
            stream.write(line.rstrip("\n") + "\n")


def _ascii_native_log_target() -> tuple[Any, str]:
    """Create an ASCII-only temporary logfile target for native Pyomo adapters."""
    directory = tempfile.gettempdir()
    if not directory.isascii():
        directory = (
            os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Temp")
            if os.name == "nt"
            else "/tmp"
        )
    if not directory.isascii():
        raise OSError("no ASCII temporary directory is available for the native solver log")
    temporary_directory = tempfile.TemporaryDirectory(prefix="optagent-", dir=directory)
    temporary_log_path = os.path.join(temporary_directory.name, "solver.log")
    if not temporary_log_path.isascii():
        temporary_directory.cleanup()
        raise OSError("temporary native solver log path is not ASCII")
    return temporary_directory, temporary_log_path


def _run_highs(
    model: Any,
    *,
    backend: Mapping[str, Any],
    objective_sense: str | None,
    time_limit_seconds: float | None,
    relative_gap: float,
    absolute_gap: float,
    threads: int | None,
    random_seed: int | None,
    feasibility_tolerance: float,
    integrality_tolerance: float,
    warm_start: bool,
    log_path: str | None,
) -> SolverObservation:
    from pyomo.common.collections import ComponentMap
    from pyomo.contrib.appsi.solvers import Highs

    _reject_unverified_components(model, interface="Pyomo APPSI HiGHS")
    _require_linear_model(model)
    solver = Highs()
    solver.config.load_solution = False
    if warm_start:
        try:
            solver.config.warmstart = True
        except Exception as exc:
            _raise_option("warm_start", "APPSI HiGHS configuration does not support warm starts")
    if time_limit_seconds is not None:
        solver.config.time_limit = float(time_limit_seconds)
    native = {
        name: value
        for name, value in (
            ("time_limit", time_limit_seconds),
            ("threads", threads),
            ("random_seed", random_seed),
            ("mip_rel_gap", relative_gap),
            ("mip_abs_gap", absolute_gap),
        )
        if value is not None
    }
    if log_path is not None:
        native["log_file"] = log_path
        native["log_to_console"] = False
        solver.highs_options["log_file"] = log_path
        solver.highs_options["log_to_console"] = False
    native_parameters: dict[str, Any] = {key: value for key, value in native.items() if key not in {"log_file", "log_to_console"}}
    started = time.perf_counter()
    result: Any = None
    error: str | None = None
    solver_invoked = False
    solver_returned = False
    raw_status = "unknown"
    raw_valid: bool | None = None
    native_mip_node_count: int | None = None
    primals: Mapping[Any, Any] = ComponentMap()
    nonfinite: dict[str, str] = {}
    effective_native: dict[str, Any] = {}
    set_statuses: dict[str, str] = {}
    try:
        solver.set_instance(model)
        raw_model = getattr(solver, "_solver_model", None)
        if raw_model is None:
            raise ContractError("capability_uncovered", "APPSI HiGHS did not expose its active native model")
        from highspy import HighsStatus

        for name, requested in native_parameters.items():
            status = raw_model.setOptionValue(name, requested)
            set_statuses[name] = _enum_name(status)
            if status != HighsStatus.kOk:
                raise ContractError("unsupported_option", "HiGHS rejected a requested native option", details={"option": name})
            get_status, readback = raw_model.getOptionValue(name)
            if get_status != HighsStatus.kOk or _finite_number(readback) is None or float(readback) != float(requested):
                raise ContractError("unsupported_option", "HiGHS did not retain a requested native option", details={"option": name})
            effective_native[name] = readback
        solver_invoked = True
        result = solver.solve(model)
        solver_returned = True
        raw_model = getattr(solver, "_solver_model", None)
        if raw_model is not None:
            raw_status = _enum_name(raw_model.getModelStatus())
            solution = raw_model.getSolution()
            raw_valid = bool(solution.value_valid)
            try:
                mip_node_count = _finite_number(getattr(raw_model.getInfo(), "mip_node_count", None))
                if mip_node_count is not None and mip_node_count.is_integer():
                    native_mip_node_count = int(mip_node_count)
            except Exception:
                pass
            if raw_valid:
                primals = solver.get_primals()
            for name in native_parameters:
                try:
                    get_status, readback = raw_model.getOptionValue(name)
                    if get_status == HighsStatus.kOk and _finite_number(readback) is not None:
                        effective_native[name] = readback
                except Exception:
                    pass
    except ContractError:
        raise
    except Exception as exc:
        error = _safe_error(exc)
        # APPSI may set a native status before raising while retaining a point.
        raw_model = getattr(solver, "_solver_model", None)
        if raw_model is not None:
            try:
                raw_status = _enum_name(raw_model.getModelStatus())
                solution = raw_model.getSolution()
                raw_valid = bool(solution.value_valid)
                if raw_valid:
                    primals = solver.get_primals()
            except Exception:
                pass
    elapsed = time.perf_counter() - started
    termination_condition = _enum_name(getattr(result, "termination_condition", "error" if error else "unknown"))
    termination = _normalize_termination(termination_condition, backend_id="highs", has_primal=raw_valid is True)
    if error and result is None and raw_status.casefold() in {"ksolveerror", "kpostsolveerror"}:
        termination = "numeric_error"
    incumbent_raw = getattr(result, "best_feasible_objective", None) if result is not None else None
    bound_raw = getattr(result, "best_objective_bound", None) if result is not None else None
    incumbent = _finite_number(incumbent_raw)
    bound = _finite_number(bound_raw)
    if incumbent_raw is not None and incumbent is None:
        nonfinite["best_feasible_objective"] = repr(incumbent_raw)
    if bound_raw is not None and bound is None:
        nonfinite["best_objective_bound"] = repr(bound_raw)
    bound_evidence = None
    raw_bound = None
    if result is not None and objective_sense in {"min", "max"} and bound is not None:
        source = "Pyomo APPSI HiGHS results.best_objective_bound"
        raw_bound = {
            "source": source,
            "raw_value": bound,
        }
        if termination != "optimal" and native_mip_node_count == -1:
            raw_bound["accepted"] = False
            raw_bound["reason"] = "nonoptimal_continuous_lp_bound_not_qualified"
            bound = None
        elif termination != "optimal" and (native_mip_node_count is None or native_mip_node_count < 0):
            raw_bound["accepted"] = False
            raw_bound["reason"] = "native_mip_node_count_unavailable_for_nonoptimal_bound"
            bound = None
        elif _bound_direction_ok(bound, incumbent, objective_sense):
            bound_evidence = {
                "source": source,
                "scope": "global",
                "objective_sense": objective_sense,
                "value_direction": "lower" if objective_sense == "min" else "upper",
                "interface": "appsi_highs",
                "objective_transform": {"kind": "identity", "formula": "native bound is already in original Pyomo objective units"},
                "raw_value": bound,
            }
        else:
            raw_bound["accepted"] = False
            raw_bound["reason"] = "bound_conflicts_with_solver_incumbent"
            bound = None
    elif bound_raw is not None:
        raw_bound = {"source": "Pyomo APPSI HiGHS results.best_objective_bound", "raw_repr": repr(bound_raw), "accepted": False, "reason": "nonfinite_bound"}
        bound = None
    requested, effective, statuses, criterion = _param_record(
        time_limit_seconds=time_limit_seconds,
        relative_gap=relative_gap,
        absolute_gap=absolute_gap,
        threads=threads,
        random_seed=random_seed,
        feasibility_tolerance=feasibility_tolerance,
        integrality_tolerance=integrality_tolerance,
        warm_start=warm_start,
        native_parameters=native_parameters,
    )
    effective["time_limit_seconds"] = effective_native.get("time_limit")
    effective["threads"] = effective_native.get("threads")
    effective["random_seed"] = effective_native.get("random_seed")
    effective["parameters"] = effective_native
    for name, value in (("time_limit_seconds", time_limit_seconds), ("threads", threads), ("random_seed", random_seed)):
        if value is not None:
            parameter = {"time_limit_seconds": "time_limit", "threads": "threads", "random_seed": "random_seed"}[name]
            if parameter in effective_native:
                statuses[name] = "accepted_readback"
            elif solver_invoked:
                statuses[name] = "submitted_no_readback"
            else:
                statuses[name] = "not_applied_solver_not_invoked"
    if warm_start and not solver_invoked:
        statuses["warm_start"] = "not_applied_solver_not_invoked"
    elapsed = time.perf_counter() - started
    record = {
        "termination": termination,
        "raw_status": raw_status,
        "solver_invoked": solver_invoked,
        "solver_returned": solver_returned,
        "raw_solution_value_valid": raw_valid,
        "best_feasible_objective": incumbent,
        "best_objective_bound": bound,
        "bound_evidence": bound_evidence,
        "raw_bound_observation": raw_bound,
        "termination_condition": termination_condition,
        "solve_wall_seconds": elapsed,
        "error": error,
        "requested_options": requested,
        "effective_options": effective,
        "option_set_statuses": statuses,
        "core_gap_criterion": criterion,
    }
    profile = dict(backend)
    profile["license_status"] = "unavailable" if error and "license" in error.casefold() else "accepted_for_this_solve" if solver_returned else "unknown"
    profile["license_scope"] = "unknown"
    if log_path is not None and not _safe_file_nonempty(log_path):
        _set_log_file(log_path, "HiGHS log capture unavailable")
    return SolverObservation(record, profile, termination, raw_status, solver_invoked, solver_returned, primals, incumbent, bound, nonfinite, elapsed, error)


def _safe_file_nonempty(path: str) -> bool:
    try:
        import os

        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _run_native_pyomo(
    model: Any,
    *,
    backend_id: str,
    backend: Mapping[str, Any],
    objective_sense: str | None,
    time_limit_seconds: float | None,
    relative_gap: float,
    absolute_gap: float,
    threads: int | None,
    random_seed: int | None,
    feasibility_tolerance: float,
    integrality_tolerance: float,
    warm_start: bool,
    log_path: str | None,
) -> SolverObservation:
    from pyomo.environ import SolverFactory
    from pyomo.common.collections import ComponentMap

    spec = _require_backend(backend_id)
    variables = _pyomo_variables(model)
    integer_model = _pyomo_has_integer_variables(variables)
    if backend_id == "ipopt" and integer_model:
        raise ContractError("capability_uncovered", "IPOPT only supports continuous models in this adapter")
    if backend_id == "cp_optimizer":
        return _run_cp_optimizer(
            model,
            backend=backend,
            objective_sense=objective_sense,
            time_limit_seconds=time_limit_seconds,
            relative_gap=relative_gap,
            absolute_gap=absolute_gap,
            threads=threads,
            random_seed=random_seed,
            feasibility_tolerance=feasibility_tolerance,
            integrality_tolerance=integrality_tolerance,
            log_path=log_path,
        )
    _reject_unverified_components(model, interface=f"Pyomo {backend_id}")
    model_degree = 0
    if backend_id in {"cplex", "scip", "xpress", "gurobi", "mosek"}:
        model_degree = _require_polynomial_model(model, maximum_degree=2)[3]
    elif backend_id != "ipopt":
        _require_linear_model(model)
    solver = SolverFactory(spec["factory"])
    native_options, native_parameters = _pyomo_native_options(
        backend_id,
        time_limit_seconds=time_limit_seconds,
        relative_gap=relative_gap,
        absolute_gap=absolute_gap,
        threads=threads,
        random_seed=random_seed,
    )
    if backend_id == "highs":
        # The explicit APPSI path has its own set/readback protocol.
        raise ContractError("capability_uncovered", "HiGHS must use the APPSI adapter")
    if warm_start:
        try:
            warm_start_capable = solver.warm_start_capable()
        except Exception:
            warm_start_capable = False
        if warm_start_capable is not True:
            _raise_option("warm_start", f"{backend_id} Pyomo plugin does not report warm-start support")
    started = time.perf_counter()
    result: Any = None
    solver_invoked = False
    solver_returned = False
    error: str | None = None
    log_archive_issues: list[str] = []
    temporary_log_directory: Any | None = None
    solver_log_path = None
    if log_path is not None:
        try:
            temporary_log_directory, solver_log_path = _ascii_native_log_target()
        except Exception as exc:
            log_archive_issues.append(
                "Native solver log archive failed before invocation: " + _safe_error(exc)
            )
    primals: Mapping[Any, Any] = ComponentMap()
    raw_valid: bool | None = None
    raw_status = "unknown"
    solver_status = "unknown"
    try:
        try:
            solver.options.update(native_options)
            solver_invoked = True
            kwargs: dict[str, Any] = {"load_solutions": False}
            if warm_start:
                kwargs["warmstart"] = True
            if solver_log_path is not None:
                kwargs["logfile"] = solver_log_path
            result = solver.solve(model, **kwargs)
            solver_returned = True
            raw_status, solver_status = _solver_result_status(result)
            primals, raw_valid, _ = _load_native_solution(model, result, variables)
        except Exception as exc:
            error = _safe_error(exc)
            if result is not None:
                raw_status, solver_status = _solver_result_status(result)
    finally:
        if temporary_log_directory is not None:
            try:
                shutil.copyfile(solver_log_path, log_path)
            except Exception as exc:
                log_archive_issues.append("Native solver log archive failed: " + _safe_error(exc))
            try:
                temporary_log_directory.cleanup()
            except Exception as exc:
                log_archive_issues.append("Native solver log scratch cleanup failed: " + _safe_error(exc))
    if log_archive_issues:
        log_archive_error = "; ".join(log_archive_issues)
        error = f"{error}; {log_archive_error}" if error else log_archive_error
    elapsed = time.perf_counter() - started
    termination = _normalize_termination(
        raw_status,
        backend_id=backend_id,
        has_primal=raw_valid is True,
        nonlinear_model=model_degree > 1,
    )
    if error and not solver_returned:
        termination = "numeric_error" if solver_status.casefold() == "error" else "error"
    elif solver_status.casefold() == "error" and termination == "unknown":
        termination = "numeric_error"
    incumbent = _pyomo_incumbent(result, objective_sense, integer_model=integer_model, backend_id=backend_id) if result is not None else None
    bound, bound_evidence, raw_bound = _pyomo_global_bound(result, objective_sense, integer_model=integer_model, backend_id=backend_id) if result is not None else (None, None, None)
    if model_degree > 1 and bound is not None:
        raw_bound = {
            **(raw_bound or {}),
            "accepted": False,
            "reason": "quadratic_model_global_bound_scope_not_verified",
        }
        bound = None
        bound_evidence = None
    if bound is not None and not _bound_direction_ok(bound, incumbent, objective_sense):
        if raw_bound is not None:
            raw_bound = {**raw_bound, "accepted": False, "reason": "bound_conflicts_with_solver_incumbent"}
        bound = None
        bound_evidence = None
    nonfinite: dict[str, str] = {}
    if result is not None:
        for name, value in (("solver_incumbent_objective", incumbent), ("bound_value", bound)):
            if value is None:
                continue
            if not math.isfinite(value):
                nonfinite[name] = repr(value)
    requested, effective, statuses, criterion = _param_record(
        time_limit_seconds=time_limit_seconds,
        relative_gap=relative_gap,
        absolute_gap=absolute_gap,
        threads=threads,
        random_seed=random_seed,
        feasibility_tolerance=feasibility_tolerance,
        integrality_tolerance=integrality_tolerance,
        warm_start=warm_start,
        native_parameters=native_parameters,
    )
    _mark_unapplied_native_controls(
        statuses,
        solver_invoked=solver_invoked,
        time_limit_seconds=time_limit_seconds,
        threads=threads,
        random_seed=random_seed,
        warm_start=warm_start,
    )
    record = {
        "termination": termination,
        "raw_status": raw_status,
        "solver_status": solver_status,
        "solver_invoked": solver_invoked,
        "solver_returned": solver_returned,
        "raw_solution_value_valid": raw_valid,
        "best_feasible_objective": incumbent,
        "best_objective_bound": bound,
        "bound_evidence": bound_evidence,
        "raw_bound_observation": raw_bound,
        "termination_condition": raw_status,
        "solve_wall_seconds": elapsed,
        "error": error,
        "requested_options": requested,
        "effective_options": effective,
        "option_set_statuses": statuses,
        "core_gap_criterion": criterion,
    }
    profile = dict(backend)
    profile["license_status"] = "unavailable" if error and "license" in error.casefold() else "accepted_for_this_solve" if solver_returned else "unknown"
    profile["license_scope"] = "unknown" if backend_id in {"xpress", "mosek", "gurobi", "cplex"} else "not_reported"
    return SolverObservation(record, profile, termination, raw_status, solver_invoked, solver_returned, primals, incumbent, bound, nonfinite, elapsed, error)


def _run_cp_optimizer(
    model: Any,
    *,
    backend: Mapping[str, Any],
    objective_sense: str | None,
    time_limit_seconds: float | None,
    relative_gap: float,
    absolute_gap: float,
    threads: int | None,
    random_seed: int | None,
    feasibility_tolerance: float,
    integrality_tolerance: float,
    log_path: str | None,
) -> SolverObservation:
    import copy
    from pyomo.common.collections import ComponentMap
    from pyomo.core.base.componentuid import ComponentUID
    from pyomo.core.expr.visitor import polynomial_degree
    from pyomo.environ import Constraint, Objective, Var, value

    for component in model.component_objects(active=True, descend_into=True):
        ctype_name = getattr(getattr(component, "ctype", None), "__name__", "")
        class_name = type(component).__name__
        if "BooleanVar" in {ctype_name, class_name} or "IntervalVar" in {ctype_name, class_name}:
            raise ContractError("capability_uncovered", "CP Optimizer adapter does not cover BooleanVar or IntervalVar semantics")
        if ctype_name in {"LogicalConstraint", "SOSConstraint"} or class_name in {"Disjunction", "Disjunct"}:
            raise ContractError("capability_uncovered", "CP Optimizer adapter supports ordinary linear Constraints only")
    variables = list(model.component_data_objects(Var, active=True, descend_into=True))
    for constraint in model.component_data_objects(Constraint, active=True, descend_into=True):
        for expr in (constraint.body, constraint.lower, constraint.upper):
            if expr is not None and polynomial_degree(expr) not in (0, 1):
                raise ContractError("capability_uncovered", "CP Optimizer adapter supports linear constraints only")
    objectives = list(model.component_data_objects(Objective, active=True, descend_into=True))
    if len(objectives) > 1 or any(polynomial_degree(objective.expr) not in (0, 1) for objective in objectives):
        raise ContractError("capability_uncovered", "CP Optimizer adapter supports one linear objective")
    for variable in variables:
        try:
            integer = variable.is_integer() or variable.is_binary()
        except Exception:
            integer = "integer" in str(variable.domain).casefold() or "binary" in str(variable.domain).casefold()
        if integer:
            lower = _pyomo_bound_value(variable.lb, side="lower")
            upper = _pyomo_bound_value(variable.ub, side="upper")
            if lower is None or upper is None or not lower.is_integer() or not upper.is_integer():
                raise ContractError("capability_uncovered", "CP Optimizer requires finite integer domains for integer variables")

    clone = copy.deepcopy(model)
    clone_variables = list(clone.component_data_objects(Var, active=True, descend_into=True))
    originals: dict[str, Any] = {str(ComponentUID(variable)): variable for variable in variables}
    clone_by_uid: dict[str, Any] = {str(ComponentUID(variable)): variable for variable in clone_variables}
    for uid, original in originals.items():
        if not original.fixed:
            clone_by_uid[uid].set_value(None, skip_validation=True)

    solver = importlib.import_module("pyomo.environ").SolverFactory("cp_optimizer")
    native_options, native_parameters = _pyomo_native_options(
        "cp_optimizer",
        time_limit_seconds=time_limit_seconds,
        relative_gap=relative_gap,
        absolute_gap=absolute_gap,
        threads=threads,
        random_seed=random_seed,
    )
    result: Any = None
    error: str | None = None
    solver_invoked = False
    solver_returned = False
    raw_status = "unknown"
    solver_status = "unknown"
    started = time.perf_counter()
    try:
        solver.options.update(native_options)
        solver_invoked = True
        # This adapter writes only the isolated clone, as required by the
        # installed CP Optimizer plugin's in-place solution contract.
        result = solver.solve(clone)
        solver_returned = True
        raw_status, solver_status = _solver_result_status(result)
    except Exception as exc:
        error = _safe_error(exc)
        if result is not None:
            raw_status, solver_status = _solver_result_status(result)
    elapsed = time.perf_counter() - started
    primals = ComponentMap()
    nonfinite: dict[str, str] = {}
    completeness = True
    any_value = False
    for uid, original in originals.items():
        if original.fixed:
            continue
        cloned = clone_by_uid[uid]
        if cloned.value is None:
            completeness = False
            continue
        primals[original] = cloned.value
        any_value = True
        if _finite_number(cloned.value) is None:
            nonfinite[f"primal:{uid}"] = repr(cloned.value)
    raw_valid = bool(completeness and any_value) if variables else solver_returned
    termination = _normalize_termination(raw_status, backend_id="cp_optimizer", has_primal=raw_valid is True)
    if error and not solver_returned:
        termination = "error"
    if result is not None:
        incumbent = _pyomo_incumbent(result, objective_sense, integer_model=True, backend_id="cp_optimizer")
        bound, bound_evidence, raw_bound = _pyomo_global_bound(result, objective_sense, integer_model=True, backend_id="cp_optimizer")
    else:
        incumbent = bound = None
        bound_evidence = raw_bound = None
    if bound is not None and not _bound_direction_ok(bound, incumbent, objective_sense):
        raw_bound = {**(raw_bound or {}), "accepted": False, "reason": "bound_conflicts_with_solver_incumbent"}
        bound = None
        bound_evidence = None
    requested, effective, statuses, criterion = _param_record(
        time_limit_seconds=time_limit_seconds,
        relative_gap=relative_gap,
        absolute_gap=absolute_gap,
        threads=threads,
        random_seed=random_seed,
        feasibility_tolerance=feasibility_tolerance,
        integrality_tolerance=integrality_tolerance,
        native_parameters=native_parameters,
    )
    _mark_unapplied_native_controls(
        statuses,
        solver_invoked=solver_invoked,
        time_limit_seconds=time_limit_seconds,
        threads=threads,
        random_seed=random_seed,
    )
    record = {
        "termination": termination,
        "raw_status": raw_status,
        "solver_status": solver_status,
        "solver_invoked": solver_invoked,
        "solver_returned": solver_returned,
        "raw_solution_value_valid": raw_valid,
        "native_result_mode": "inplace_clone",
        "best_feasible_objective": incumbent,
        "best_objective_bound": bound,
        "bound_evidence": bound_evidence,
        "raw_bound_observation": raw_bound,
        "termination_condition": raw_status,
        "solve_wall_seconds": elapsed,
        "error": error,
        "requested_options": requested,
        "effective_options": effective,
        "option_set_statuses": statuses,
        "core_gap_criterion": criterion,
    }
    profile = dict(backend)
    profile["license_status"] = "unavailable" if error and "license" in error.casefold() else "accepted_for_this_solve" if solver_returned else "unknown"
    if log_path is not None:
        _set_log_file(log_path, "CP Optimizer native log capture was not exposed by the verified plugin")
    return SolverObservation(record, profile, termination, raw_status, solver_invoked, solver_returned, primals, incumbent, bound, nonfinite, elapsed, error)


def _require_polynomial_model(
    model: Any,
    *,
    maximum_degree: int,
) -> tuple[list[Any], list[Any], list[Any], int]:
    from pyomo.core.expr.visitor import polynomial_degree
    from pyomo.environ import Constraint, Objective, Var

    variables = list(model.component_data_objects(Var, active=True, descend_into=True))
    constraints = list(model.component_data_objects(Constraint, active=True, descend_into=True))
    objectives = list(model.component_data_objects(Objective, active=True, descend_into=True))
    if len(objectives) > 1:
        raise ContractError("capability_uncovered", "selected backend adapter requires at most one active objective")
    expressions = [constraint.body for constraint in constraints]
    expressions.extend(bound for constraint in constraints for bound in (constraint.lower, constraint.upper) if bound is not None)
    expressions.extend(objective.expr for objective in objectives)
    model_degree = 0
    for expr in expressions:
        degree = polynomial_degree(expr)
        if degree is None or degree > maximum_degree:
            label = "quadratic-polynomial" if maximum_degree == 2 else "linear"
            raise ContractError("capability_uncovered", f"selected backend adapter supports at most {label} Pyomo expressions")
        model_degree = max(model_degree, degree)
    return variables, constraints, objectives, model_degree


def _require_linear_model(model: Any) -> tuple[list[Any], list[Any], list[Any]]:
    variables, constraints, objectives, _ = _require_polynomial_model(model, maximum_degree=1)
    return variables, constraints, objectives


def _finite_integer(value: Any, label: str) -> int:
    finite = _component_value(value)
    if finite is None or not finite.is_integer():
        raise ContractError("capability_uncovered", f"CP-SAT requires an exact integer {label}")
    result = int(finite)
    if abs(result) > (1 << 53) - 1:
        raise ContractError("capability_uncovered", f"CP-SAT {label} exceeds exact floating witness precision")
    return result


def _cp_sat_safe_term(coefficients: list[int], lower: list[int], upper: list[int], label: str) -> None:
    limit = (1 << 63) - 1
    activity = 0
    for coefficient, lo, hi in zip(coefficients, lower, upper, strict=True):
        term = abs(coefficient) * max(abs(lo), abs(hi))
        activity += term
        if term > limit or activity > limit:
            raise ContractError("capability_uncovered", f"CP-SAT {label} can overflow signed int64")
    if activity > (1 << 53) - 1:
        raise ContractError("capability_uncovered", f"CP-SAT {label} exceeds exact floating witness precision")


def _solve_cp_sat_model(
    native_model: Any,
    *,
    backend: Mapping[str, Any],
    objective_sense: str | None,
    has_objective: bool,
    time_limit_seconds: float | None,
    relative_gap: float,
    absolute_gap: float,
    threads: int | None,
    random_seed: int | None,
    feasibility_tolerance: float,
    integrality_tolerance: float,
    log_path: str | None,
    pyomo_variables: Any = None,
    pyomo_native_variables: Any = None,
    native_response_mode: bool = False,
    native_model_proto: bytes | None = None,
    native_solution_hint_count: int | None = None,
) -> SolverObservation:
    """Share CP-SAT parameter, logging, status, bound and timeout observation."""
    model_api = importlib.import_module("ortools.sat.python.cp_model")
    solver = model_api.CpSolver()
    parameters: dict[str, Any] = {}
    if time_limit_seconds is not None:
        solver.parameters.max_time_in_seconds = float(time_limit_seconds)
        parameters["max_time_in_seconds"] = float(time_limit_seconds)
    if threads is not None:
        solver.parameters.num_search_workers = threads
        parameters["num_search_workers"] = threads
    if random_seed is not None:
        solver.parameters.random_seed = random_seed
        parameters["random_seed"] = random_seed
    solver.parameters.relative_gap_limit = relative_gap
    solver.parameters.absolute_gap_limit = absolute_gap
    parameters["relative_gap_limit"] = relative_gap
    parameters["absolute_gap_limit"] = absolute_gap

    log_file = None
    if log_path is not None:
        solver.parameters.log_search_progress = True
        log_file = open(log_path, "a", encoding="utf-8")
        if hasattr(solver, "log_callback"):
            solver.log_callback = log_file.write
    started = time.perf_counter()
    invoked = False
    returned = False
    status_value: Any = None
    status_code: int | None = None
    status_name = "UNKNOWN"
    response: Any | None = None
    response_solution: tuple[int, ...] | None = None
    if native_response_mode:
        primals: Any = {}
    else:
        from pyomo.common.collections import ComponentMap

        primals = ComponentMap()
    incumbent: float | None = None
    bound: float | None = None
    bound_evidence: dict[str, Any] | None = None
    raw_bound: dict[str, Any] | None = None
    raw_valid: bool | None = None
    nonfinite: dict[str, str] = {}
    error: str | None = None

    def capture_point() -> None:
        nonlocal response, response_solution, primals, raw_valid, incumbent, bound, bound_evidence, raw_bound
        if native_response_mode:
            response = solver.response_proto
            response_solution = tuple(int(value) for value in response.solution)
            raw_valid = len(response_solution) == len(native_model.proto.variables)
            if has_objective:
                raw_objective = getattr(response, "objective_value", None)
                incumbent = _finite_number(raw_objective)
                if raw_objective is not None and incumbent is None:
                    nonfinite["objective_value"] = repr(raw_objective)
                raw_bound_value = _finite_number(getattr(response, "best_objective_bound", None))
                if getattr(response, "best_objective_bound", None) is not None and raw_bound_value is None:
                    nonfinite["best_objective_bound"] = repr(response.best_objective_bound)
            else:
                raw_bound_value = None
        else:
            if pyomo_variables is None or pyomo_native_variables is None:
                raise ContractError("artifact_changed", "Pyomo CP-SAT response projection is missing its variable map")
            for variable, native_variable in pyomo_native_variables.items():
                primals[variable] = solver.Value(native_variable)
            raw_valid = len(primals) == len(pyomo_variables)
            raw_objective = solver.ObjectiveValue() if has_objective else None
            incumbent = _finite_number(raw_objective) if has_objective else None
            raw_bound_value = _finite_number(solver.BestObjectiveBound()) if has_objective else None

        if has_objective and raw_bound_value is not None:
            raw_bound = {"source": "OR-Tools CP-SAT CpSolver.BestObjectiveBound", "raw_value": raw_bound_value}
            if _bound_direction_ok(raw_bound_value, incumbent, objective_sense):
                bound = raw_bound_value
                bound_evidence = {
                    "source": "OR-Tools CP-SAT CpSolver.BestObjectiveBound",
                    "scope": "global",
                    "objective_sense": objective_sense,
                    "value_direction": "lower" if objective_sense == "min" else "upper",
                    "interface": "native_cp_model",
                    "objective_transform": {"kind": "identity", "formula": "CpModel objective includes the original constant and sense"},
                    "raw_value": raw_bound_value,
                }
            else:
                raw_bound["accepted"] = False
                raw_bound["reason"] = "bound_conflicts_with_solver_incumbent"

    try:
        invoked = True
        status_value = solver.Solve(native_model)
        returned = True
        if isinstance(status_value, int) and not isinstance(status_value, bool):
            status_code = int(status_value)
        observed_name = solver.StatusName(status_value)
        if not isinstance(observed_name, str):
            raise TypeError("OR-Tools CpSolver.StatusName did not return a string")
        status_name = observed_name
        if status_name in {"OPTIMAL", "FEASIBLE"}:
            capture_point()
        else:
            raw_valid = False
    except Exception as exc:
        error = _safe_error(exc)
        if native_response_mode and response is None:
            try:
                response = solver.response_proto
            except Exception:
                response = None
        if status_name in {"OPTIMAL", "FEASIBLE"}:
            try:
                capture_point()
            except Exception:
                pass
    finally:
        if log_file is not None:
            log_file.close()

    elapsed = time.perf_counter() - started
    termination = {
        "OPTIMAL": "optimal",
        "FEASIBLE": "feasible",
        "INFEASIBLE": "infeasible",
        "MODEL_INVALID": "error",
        "UNKNOWN": "unknown",
    }.get(status_name, "unknown")
    if error and invoked and not returned:
        termination = "error"
    requested, effective, statuses, criterion = _param_record(
        time_limit_seconds=time_limit_seconds,
        relative_gap=relative_gap,
        absolute_gap=absolute_gap,
        threads=threads,
        random_seed=random_seed,
        feasibility_tolerance=feasibility_tolerance,
        integrality_tolerance=integrality_tolerance,
        native_parameters=parameters,
    )
    effective["parameters"] = {}
    for name, value in (("time_limit_seconds", time_limit_seconds), ("threads", threads), ("random_seed", random_seed)):
        if value is not None:
            statuses[name] = "submitted_no_readback" if invoked else "not_applied_solver_not_invoked"
    record = {
        "termination": termination,
        "raw_status": status_name,
        "raw_status_code": status_code,
        "raw_status_name": status_name,
        "solver_invoked": invoked,
        "solver_returned": returned,
        "raw_solution_value_valid": raw_valid,
        "best_feasible_objective": incumbent,
        "best_objective_bound": bound,
        "bound_evidence": bound_evidence,
        "raw_bound_observation": raw_bound,
        "termination_condition": status_name,
        "solve_wall_seconds": elapsed,
        "error": error,
        "requested_options": requested,
        "effective_options": effective,
        "option_set_statuses": statuses,
        "core_gap_criterion": criterion,
    }
    if native_response_mode:
        record["native_model_input"] = {"solution_hint_variable_count": native_solution_hint_count or 0}
    profile = dict(backend)
    profile["license_status"] = "accepted_for_this_solve" if returned else "unknown"
    return SolverObservation(
        record,
        profile,
        termination,
        status_name,
        invoked,
        returned,
        primals,
        incumbent,
        bound,
        nonfinite,
        elapsed,
        error,
        response_solution if native_response_mode else None,
        native_model_proto if native_response_mode else None,
        native_solution_hint_count if native_response_mode else None,
    )


def _run_cp_sat(
    model: Any,
    *,
    backend: Mapping[str, Any],
    objective_sense: str | None,
    time_limit_seconds: float | None,
    relative_gap: float,
    absolute_gap: float,
    threads: int | None,
    random_seed: int | None,
    feasibility_tolerance: float,
    integrality_tolerance: float,
    log_path: str | None,
) -> SolverObservation:
    from pyomo.common.collections import ComponentMap
    from pyomo.environ import Constraint, Objective, Var
    from pyomo.repn.standard_repn import generate_standard_repn

    _reject_unverified_components(model, interface="CP-SAT")
    variables, constraints, objectives = _require_linear_model(model)
    for variable in variables:
        try:
            integer = variable.is_integer() or variable.is_binary()
        except Exception:
            integer = "integer" in str(variable.domain).casefold() or "binary" in str(variable.domain).casefold()
        if not integer:
            raise ContractError("capability_uncovered", "CP-SAT accepts integer-domain Pyomo variables only")

    model_api = importlib.import_module("ortools.sat.python.cp_model")
    cp_model = model_api.CpModel()
    cp_vars: Any = ComponentMap()
    cp_index: Any = ComponentMap()
    var_lowers: list[int] = []
    var_uppers: list[int] = []
    for index, variable in enumerate(variables):
        if variable.fixed:
            fixed_value = _finite_integer(variable.value, "fixed variable value")
            lower = upper = fixed_value
        else:
            lower_value = _pyomo_bound_value(variable.lb, side="lower")
            upper_value = _pyomo_bound_value(variable.ub, side="upper")
            if lower_value is None or upper_value is None:
                raise ContractError("capability_uncovered", "CP-SAT requires finite bounds on every integer variable")
            lower = _finite_integer(lower_value, "variable lower bound")
            upper = _finite_integer(upper_value, "variable upper bound")
        if lower > upper:
            raise ContractError("capability_uncovered", "CP-SAT received an empty integer domain")
        var_lowers.append(lower)
        var_uppers.append(upper)
        if variable.is_binary():
            native_var = cp_model.NewBoolVar(f"v{index}")
            if lower > 0:
                cp_model.Add(native_var >= lower)
            if upper < 1:
                cp_model.Add(native_var <= upper)
        else:
            native_var = cp_model.NewIntVar(lower, upper, f"v{index}")
        if variable.fixed:
            fixed_value = _finite_integer(variable.value, "fixed variable value")
            cp_model.Add(native_var == fixed_value)
        cp_vars[variable] = native_var
        cp_index[variable] = index

    def linear(expression: Any) -> tuple[list[tuple[int, Any]], int]:
        repn = generate_standard_repn(expression, compute_values=True, quadratic=True)
        if repn.nonlinear_expr is not None or repn.quadratic_vars:
            raise ContractError("capability_uncovered", "CP-SAT accepts linear expressions only")
        constant = _finite_integer(repn.constant or 0, "linear constant")
        terms: list[tuple[int, Any]] = []
        coefs: list[int] = []
        for variable, coefficient in zip(repn.linear_vars, repn.linear_coefs, strict=True):
            coef = _finite_integer(coefficient, "linear coefficient")
            if variable not in cp_vars:
                raise ContractError("capability_uncovered", "CP-SAT expression references an unsupported variable")
            terms.append((coef, cp_vars[variable]))
            coefs.append(coef)
        _cp_sat_safe_term(coefs, [var_lowers[cp_index[v]] for v in repn.linear_vars], [var_uppers[cp_index[v]] for v in repn.linear_vars], "linear expression")
        linear_activity = abs(constant) + sum(abs(coef) * max(abs(var_lowers[cp_index[var]]), abs(var_uppers[cp_index[var]])) for var, coef in zip(repn.linear_vars, coefs, strict=True))
        if linear_activity > (1 << 63) - 1:
            raise ContractError("capability_uncovered", "CP-SAT linear expression can overflow signed int64")
        if linear_activity > (1 << 53) - 1:
            raise ContractError("capability_uncovered", "CP-SAT linear expression can lose exact floating witness precision")
        return terms, constant

    for constraint in constraints:
        body_terms, body_constant = linear(constraint.body)
        body = sum((coef * var for coef, var in body_terms), body_constant)
        lower_value = _pyomo_bound_value(constraint.lower, side="lower")
        upper_value = _pyomo_bound_value(constraint.upper, side="upper")
        lower = _finite_integer(lower_value, "constraint lower bound") if lower_value is not None else None
        upper = _finite_integer(upper_value, "constraint upper bound") if upper_value is not None else None
        if lower is None and upper is None:
            raise ContractError("capability_uncovered", "CP-SAT constraint has no finite side")
        if lower is not None and upper is not None and lower == upper:
            cp_model.Add(body == lower)
        else:
            if lower is not None:
                cp_model.Add(body >= lower)
            if upper is not None:
                cp_model.Add(body <= upper)

    objective_constant = 0
    objective_terms: list[tuple[int, Any]] = []
    objective_coefficients: list[int] = []
    if objectives:
        repn = generate_standard_repn(objectives[0].expr, compute_values=True, quadratic=True)
        if repn.nonlinear_expr is not None or repn.quadratic_vars:
            raise ContractError("capability_uncovered", "CP-SAT accepts a linear objective only")
        objective_constant = _finite_integer(repn.constant or 0, "objective constant")
        for variable, coefficient in zip(repn.linear_vars, repn.linear_coefs, strict=True):
            coef = _finite_integer(coefficient, "objective coefficient")
            if variable not in cp_vars:
                raise ContractError("capability_uncovered", "CP-SAT objective references an unsupported variable")
            objective_terms.append((coef, cp_vars[variable]))
            objective_coefficients.append(coef)
        _cp_sat_safe_term(objective_coefficients, [var_lowers[cp_index[v]] for v in repn.linear_vars], [var_uppers[cp_index[v]] for v in repn.linear_vars], "objective")
        if abs(objective_constant) > (1 << 53) - 1:
            raise ContractError("capability_uncovered", "CP-SAT objective constant exceeds exact floating witness precision")
        objective_activity = abs(objective_constant) + sum(abs(coef) * max(abs(var_lowers[cp_index[var]]), abs(var_uppers[cp_index[var]])) for var, coef in zip(repn.linear_vars, objective_coefficients, strict=True))
        if objective_activity > (1 << 63) - 1 or objective_activity > (1 << 53) - 1:
            raise ContractError("capability_uncovered", "CP-SAT objective can overflow or lose exact floating witness precision")
    objective_expr = sum((coef * var for coef, var in objective_terms), objective_constant)
    if objective_sense == "max":
        cp_model.Maximize(objective_expr)
    else:
        cp_model.Minimize(objective_expr)
    validation = cp_model.Validate()
    if validation:
        raise ContractError("capability_uncovered", "CP-SAT rejected the compiled integer model", details={"validation": str(validation)[:300]})

    return _solve_cp_sat_model(
        cp_model,
        backend=backend,
        objective_sense=objective_sense,
        has_objective=bool(objectives),
        time_limit_seconds=time_limit_seconds,
        relative_gap=relative_gap,
        absolute_gap=absolute_gap,
        threads=threads,
        random_seed=random_seed,
        feasibility_tolerance=feasibility_tolerance,
        integrality_tolerance=integrality_tolerance,
        log_path=log_path,
        pyomo_variables=variables,
        pyomo_native_variables=cp_vars,
    )


def _run_cp_sat_native(
    native_build: Any,
    *,
    backend: Mapping[str, Any],
    objective_sense: str | None,
    time_limit_seconds: float | None,
    relative_gap: float,
    absolute_gap: float,
    threads: int | None,
    random_seed: int | None,
    feasibility_tolerance: float,
    integrality_tolerance: float,
    log_path: str | None,
) -> SolverObservation:
    """Solve the exact CpModel returned by the Core-validated builder envelope."""
    from .models import NativeCpModelBuild

    if not isinstance(native_build, NativeCpModelBuild):
        raise ContractError("invalid_argument", "native CP-SAT execution requires the Core-validated model envelope")
    model = native_build.model
    proto = model.proto
    try:
        has_objective = bool(proto.HasField("objective"))
        has_float_objective = bool(proto.HasField("floating_point_objective"))
    except Exception as exc:
        raise ContractError("capability_uncovered", "OR-Tools CpModel objective fields are not inspectable") from exc
    if has_float_objective:
        raise ContractError("capability_uncovered", "native cp_sat currently supports integer objectives only")
    if has_objective:
        scale = float(proto.objective.scaling_factor)
        scale = 1.0 if scale == 0.0 else scale
        actual_sense = "max" if scale == -1.0 else "min" if scale == 1.0 else None
        if actual_sense is None:
            raise ContractError("capability_uncovered", "native cp_sat objective uses an unsupported scaling factor")
        if objective_sense != actual_sense:
            raise ContractError("invalid_argument", "native CpModel objective sense differs from objective_plan")
    elif objective_sense is not None:
        raise ContractError("invalid_argument", "native CpModel has no integer objective for the scalar objective_plan")

    validation = model.Validate()
    if validation:
        raise ContractError("capability_uncovered", "OR-Tools rejected the native CpModel", details={"validation": str(validation)[:300]})
    from .results import validate_native_cp_sat_model_contract

    validate_native_cp_sat_model_contract(
        proto,
        objective_sense,
        mapped_variable_indexes=tuple(index for _, _, index in native_build.variable_bindings),
    )
    try:
        model_proto_bytes = proto.SerializeToString(deterministic=True)
    except TypeError:
        model_proto_bytes = proto.SerializeToString()
    hint_count = len(proto.solution_hint.vars)
    return _solve_cp_sat_model(
        model,
        backend=backend,
        objective_sense=objective_sense,
        has_objective=has_objective,
        time_limit_seconds=time_limit_seconds,
        relative_gap=relative_gap,
        absolute_gap=absolute_gap,
        threads=threads,
        random_seed=random_seed,
        feasibility_tolerance=feasibility_tolerance,
        integrality_tolerance=integrality_tolerance,
        log_path=log_path,
        native_response_mode=True,
        native_model_proto=model_proto_bytes,
        native_solution_hint_count=hint_count,
    )

def _cvxpy_value(value: Any) -> float | None:
    return _finite_number(value)


def _cvxpy_copt_inverse_facts(
    data: Any,
    chain: Any,
    inverse_data: Any,
    *,
    canonical_sense_sign: float,
    objective_class: str,
) -> dict[str, Any]:
    """Inspect this solve's QP reduction and inverse data before accepting BestBnd.

    The bridge keeps the original objective constant outside CVXPY.  A COPT
    bound is therefore usable only for the observed QP path when its canonical
    objective offset and any exposed affine inverse fields are neutral.  Other
    reduction chains remain useful primal paths, but do not get a guessed bound.
    """
    reductions = list(getattr(chain, "reductions", ()) or ())
    reduction_names = [type(reduction).__name__ for reduction in reductions]
    reduction_modules = [type(reduction).__module__ for reduction in reductions]
    if isinstance(inverse_data, (list, tuple)):
        inverses = list(inverse_data)
    else:
        inverses = []

    inverse_types: list[str] = []
    affine_fields: list[dict[str, Any]] = []
    affine_fields_known = len(inverses) == len(reductions)
    affine_names = ("objective_offset", "obj_offset", "objective_scale", "obj_scale", "objective_sign", "obj_sign", "offset", "scale", "sign")
    for index, reduction in enumerate(reductions):
        if index >= len(inverses):
            break
        inverse = inverses[index]
        inverse_types.append(type(inverse).__name__)
        for field in affine_names:
            if isinstance(inverse, Mapping):
                present = field in inverse
                value = inverse.get(field)
            else:
                present = hasattr(inverse, field)
                try:
                    value = getattr(inverse, field) if present else None
                except Exception:
                    value = None
            if not present:
                continue
            numeric = _result_number(value)
            affine_fields.append(
                {
                    "reduction": type(reduction).__name__,
                    "field": field,
                    "value": numeric,
                    "raw_repr": None if numeric is not None else repr(value)[:200],
                }
            )
            if numeric is None:
                affine_fields_known = False

    canonical_offset_present = isinstance(data, Mapping) and "d" in data
    canonical_offset = _result_number(data.get("d")) if canonical_offset_present else None
    allowed_reductions = {"CvxAttr2Constr", "Qp2SymbolicQp", "QpMatrixStuffing", "COPT"}
    qp_chain = (
        reduction_names.count("QpMatrixStuffing") == 1
        and "ConeMatrixStuffing" not in reduction_names
        and reduction_names[-1:] == ["COPT"]
        and all(name in allowed_reductions for name in reduction_names)
    )
    inverse_values_neutral = all(
        item["value"] == 0.0 if "offset" in item["field"] else item["value"] == 1.0
        for item in affine_fields
    )
    neutral_transform = (
        canonical_offset_present
        and canonical_offset == 0.0
        and affine_fields_known
        and inverse_values_neutral
        and canonical_sense_sign in {-1.0, 1.0}
        and objective_class == "Minimize"
    )
    return {
        "reduction_chain": [f"{module}.{name}" for module, name in zip(reduction_modules, reduction_names, strict=True)],
        "inverse_data_types": inverse_types,
        "inverse_data_aligned": len(inverses) == len(reductions),
        "inverse_affine_fields": affine_fields,
        "canonical_objective_offset": {
            "key": "d",
            "present": canonical_offset_present,
            "value": canonical_offset,
        },
        "canonical_objective_class": objective_class,
        "canonical_objective_sense": "minimize",
        "canonical_sense_sign": canonical_sense_sign,
        "recognized_qp_chain": qp_chain,
        "neutral_inverse_verified": bool(neutral_transform),
    }


def _run_cvxpy(
    model: Any,
    *,
    backend_id: str,
    backend: Mapping[str, Any],
    objective_sense: str | None,
    time_limit_seconds: float | None,
    relative_gap: float,
    absolute_gap: float,
    threads: int | None,
    random_seed: int | None,
    feasibility_tolerance: float,
    integrality_tolerance: float,
    log_path: str | None,
) -> SolverObservation:
    import numpy as np
    import scipy.sparse as sp
    import cvxpy as cp
    from pyomo.common.collections import ComponentMap, ComponentSet
    from pyomo.core.expr.visitor import polynomial_degree
    from pyomo.environ import Constraint, Objective, Var
    from pyomo.repn.standard_repn import generate_standard_repn

    variables = list(model.component_data_objects(Var, active=True, descend_into=True))
    constraints = list(model.component_data_objects(Constraint, active=True, descend_into=True))
    objectives = list(model.component_data_objects(Objective, active=True, descend_into=True))
    if len(objectives) > 1:
        raise ContractError("capability_uncovered", "CVXPY bridge requires at most one active objective")
    _reject_unverified_components(model, interface="CVXPY")

    integer_variables = ComponentSet()
    for variable in variables:
        if variable.fixed:
            continue
        try:
            if variable.is_integer() or variable.is_binary():
                integer_variables.add(variable)
        except Exception:
            if "integer" in str(variable.domain).casefold() or "binary" in str(variable.domain).casefold():
                integer_variables.add(variable)
    has_integer = bool(integer_variables)
    solver_name = _CVXPY_SOLVERS[backend_id]
    if backend_id in {"cvxpy_glop", "cvxpy_pdlp"} and has_integer:
        raise ContractError("capability_uncovered", f"{solver_name} path is LP-only and cannot return an integer incumbent")
    if backend_id == "cvxpy_osqp" and any(polynomial_degree(row.body) == 2 for row in constraints):
        raise ContractError("capability_uncovered", "OSQP bridge supports affine constraints only")
    if has_integer and backend_id not in {"cvxpy_copt"}:
        raise ContractError("capability_uncovered", f"{solver_name} path is continuous-only in this adapter")

    cp_vars: Any = ComponentMap()
    cp_index: Any = ComponentMap()
    cvx_constraints: list[Any] = []
    for index, variable in enumerate(variables):
        is_integer = variable in integer_variables
        is_binary = False
        try:
            is_binary = bool(variable.is_binary())
        except Exception:
            is_binary = "binary" in str(variable.domain).casefold()
        cv = cp.Variable(name=f"v{index}", integer=is_integer and not is_binary, boolean=is_binary)
        cp_vars[variable] = cv
        cp_index[variable] = index
        lower = _pyomo_bound_value(variable.lb, side="lower")
        upper = _pyomo_bound_value(variable.ub, side="upper")
        if lower is not None:
            cvx_constraints.append(cv >= lower)
        if upper is not None:
            cvx_constraints.append(cv <= upper)
        if variable.fixed:
            fixed = _component_value(variable.value)
            if fixed is None:
                raise ContractError("capability_uncovered", "fixed Pyomo variable has no finite value")
            cvx_constraints.append(cv == fixed)

    def expression_representation(repn: Any) -> Any:
        if repn.nonlinear_expr is not None:
            raise ContractError("capability_uncovered", "CVXPY bridge accepts standard polynomial expressions through degree two")
        result: Any = float(repn.constant or 0.0)
        for variable, coefficient in zip(repn.linear_vars, repn.linear_coefs, strict=True):
            coef = _component_value(coefficient)
            if coef is None or variable not in cp_vars:
                raise ContractError("capability_uncovered", "CVXPY expression has an unsupported linear term")
            result = result + coef * cp_vars[variable]
        if repn.quadratic_vars:
            rows: list[int] = []
            columns: list[int] = []
            values: list[float] = []
            for (left, right), coefficient in zip(repn.quadratic_vars, repn.quadratic_coefs, strict=True):
                coef = _component_value(coefficient)
                if coef is None or left not in cp_index or right not in cp_index:
                    raise ContractError("capability_uncovered", "CVXPY expression has an unsupported quadratic term")
                i, j = cp_index[left], cp_index[right]
                if i == j:
                    rows.append(i)
                    columns.append(i)
                    values.append(coef)
                else:
                    rows.extend((i, j))
                    columns.extend((j, i))
                    values.extend((coef / 2.0, coef / 2.0))
            q = sp.coo_matrix((np.asarray(values), (np.asarray(rows), np.asarray(columns))), shape=(len(variables), len(variables))).tocsc()
            q.sum_duplicates()
            vector = cp.hstack([cp_vars[variable] for variable in variables]) if variables else cp.Constant(np.zeros(0))
            result = result + cp.quad_form(vector, q)
        return result

    for constraint in constraints:
        degrees = [polynomial_degree(expr) for expr in (constraint.body, constraint.lower, constraint.upper) if expr is not None]
        if any(degree is None or degree > 2 for degree in degrees):
            raise ContractError("capability_uncovered", "CVXPY bridge supports only standard linear and quadratic rows")
        if constraint.equality:
            repn = generate_standard_repn(constraint.body - constraint.lower, compute_values=True, quadratic=True)
            cvx_constraints.append(expression_representation(repn) == 0)
        else:
            if constraint.lower is not None:
                repn = generate_standard_repn(constraint.body - constraint.lower, compute_values=True, quadratic=True)
                cvx_constraints.append(expression_representation(repn) >= 0)
            if constraint.upper is not None:
                repn = generate_standard_repn(constraint.body - constraint.upper, compute_values=True, quadratic=True)
                cvx_constraints.append(expression_representation(repn) <= 0)

    objective_constant = 0.0
    if objectives:
        objective_repn = generate_standard_repn(objectives[0].expr, compute_values=True, quadratic=True)
        objective_constant = _component_value(objective_repn.constant or 0.0)
        if objective_constant is None:
            raise ContractError("capability_uncovered", "CVXPY objective constant is not finite")
        variable_objective = expression_representation(objective_repn) - objective_constant
    else:
        variable_objective = cp.Constant(0.0)
    sign = -1.0 if objective_sense == "max" else 1.0
    cvx_objective = cp.Minimize(sign * variable_objective)
    problem = cp.Problem(cvx_objective, cvx_constraints)
    if not problem.is_dcp():
        raise ContractError("capability_uncovered", "selected CVXPY solver rejected the original model under its DCP rules")
    if backend_id in {"cvxpy_glop", "cvxpy_pdlp"} and any(polynomial_degree(item.expr) == 2 for item in objectives):
        raise ContractError("capability_uncovered", f"{solver_name} path is LP-only")
    if backend_id == "cvxpy_osqp" and any(polynomial_degree(item.expr) == 2 for item in constraints):
        raise ContractError("capability_uncovered", "OSQP bridge supports affine constraints only")

    started = time.perf_counter()
    invoked = False
    returned = False
    raw_status = "unknown"
    raw_solution: Any = None
    error: str | None = None
    native_parameters: dict[str, Any] = {}
    native_options: dict[str, Any] = {}
    primals = ComponentMap()
    nonfinite: dict[str, str] = {}
    try:
        # CVXPY's low-level standard-form route performs exactly one solve;
        # it exposes the actual reduction chain so bound provenance is not
        # guessed from the generic Problem.value field.
        try:
            data, chain, inverse_data = problem.get_problem_data(solver_name)
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError(
                "capability_uncovered",
                "the selected CVXPY interface does not cover this model's canonical form",
                details={"diagnostic": _safe_error(exc)},
            ) from exc
        solve_via_data = getattr(chain, "solve_via_data", None)
        if not callable(solve_via_data):
            raise ContractError("capability_uncovered", "CVXPY selected reduction chain has no solve_via_data entry")
        native_options, native_parameters = _cvxpy_native_options(
            backend_id,
            time_limit_seconds=time_limit_seconds,
            relative_gap=relative_gap,
            absolute_gap=absolute_gap,
            threads=threads,
            random_seed=random_seed,
        )
        invoked = True
        raw_solution = solve_via_data(problem, data, warm_start=False, verbose=False, solver_opts=native_options)
        returned = True
        problem.unpack_results(raw_solution, chain, inverse_data)
        raw_status = str(problem.status)
    except ContractError:
        raise
    except Exception as exc:
        error = _safe_error(exc)
        raw_status = str(getattr(problem, "status", "error"))

    elapsed = time.perf_counter() - started
    status_key = raw_status.casefold()
    values_available = True
    for variable in variables:
        value = cp_vars[variable].value
        if value is None:
            values_available = False
            continue
        scalar = np.asarray(value)
        if scalar.size != 1 or not np.isfinite(scalar.astype(float)).all():
            values_available = False
            nonfinite[f"primal:{variable.name}"] = repr(value)
            continue
        primals[variable] = value.item() if getattr(value, "size", 1) == 1 else value
    raw_valid = bool(returned and variables and values_available and len(primals) == len(variables))
    termination = {
        "optimal": "optimal",
        "optimal_inaccurate": "feasible",
        "user_limit": "feasible" if raw_valid else "unknown",
        "infeasible": "infeasible",
        "infeasible_inaccurate": "infeasible",
        "unbounded": "unbounded",
        "unbounded_inaccurate": "unbounded",
        "infeasible_or_unbounded": "infeasible_or_unbounded",
    }.get(status_key, "unknown")
    if error and invoked:
        termination = "error"
    canonical_value = _cvxpy_value(problem.value)
    incumbent = None
    if canonical_value is not None and raw_valid and objectives:
        incumbent = objective_constant + sign * canonical_value
    elif returned and objectives and problem.value is not None and canonical_value is None:
        nonfinite["solver_objective_value"] = repr(problem.value)
    bound: float | None = None
    bound_evidence: dict[str, Any] | None = None
    raw_bound: dict[str, Any] | None = None
    if backend_id == "cvxpy_copt" and has_integer and returned:
        stats = getattr(problem, "solver_stats", None)
        extra = getattr(stats, "extra_stats", None) if stats is not None else None
        canonical_bound = None
        bound_repr = None
        candidate = None
        candidate_available = False
        try:
            candidate = getattr(extra, "BestBnd")
            candidate_available = True
            canonical_bound = _result_number(candidate)
            bound_repr = repr(candidate)
        except Exception:
            try:
                bound_repr = repr(getattr(extra, "BestBnd", None))
            except Exception:
                bound_repr = "unavailable"
        if candidate_available and canonical_bound is None:
            nonfinite["best_objective_bound"] = repr(candidate)
        inverse_facts = _cvxpy_copt_inverse_facts(
            data,
            chain,
            inverse_data,
            canonical_sense_sign=sign,
            objective_class=type(problem.objective).__name__,
        )
        raw_bound = {
            "source": "CVXPY COPT extra_stats.BestBnd",
            "raw_value": canonical_bound,
            "raw_repr": bound_repr,
            "inverse_facts": inverse_facts,
            "accepted": False,
        }
        if canonical_bound is not None and inverse_facts["recognized_qp_chain"] and inverse_facts["neutral_inverse_verified"]:
            candidate_bound = objective_constant + sign * canonical_bound
            if _bound_direction_ok(candidate_bound, incumbent, objective_sense):
                bound = candidate_bound
                bound_evidence = {
                    "source": "CVXPY COPT extra_stats.BestBnd",
                    "scope": "global",
                    "objective_sense": objective_sense,
                    "value_direction": "lower" if objective_sense == "min" else "upper",
                    "interface": "cvxpy_copt",
                    "objective_transform": {
                        "kind": "sign_and_constant_inverse",
                        "canonical_sense_sign": sign,
                        "external_constant": objective_constant,
                        "inverse_offset": 0.0,
                        "formula": "original_bound = external_constant + canonical_sense_sign * canonical_min_bound",
                        "inverse_facts": inverse_facts,
                    },
                    "raw_value": canonical_bound,
                }
                raw_bound["accepted"] = True
            else:
                raw_bound["reason"] = "bound_conflicts_with_solver_incumbent"
        else:
            raw_bound["reason"] = "source_or_this_solve_inverse_not_verified"
    requested, effective, statuses, criterion = _param_record(
        time_limit_seconds=time_limit_seconds,
        relative_gap=relative_gap,
        absolute_gap=absolute_gap,
        threads=threads,
        random_seed=random_seed,
        feasibility_tolerance=feasibility_tolerance,
        integrality_tolerance=integrality_tolerance,
        native_parameters=native_parameters if invoked else {},
    )
    _mark_unapplied_native_controls(
        statuses,
        solver_invoked=invoked,
        time_limit_seconds=time_limit_seconds,
        threads=threads,
        random_seed=random_seed,
    )
    record = {
        "termination": termination,
        "raw_status": raw_status,
        "solver_invoked": invoked,
        "solver_returned": returned,
        "raw_solution_value_valid": raw_valid,
        "best_feasible_objective": incumbent,
        "best_objective_bound": bound,
        "bound_evidence": bound_evidence,
        "raw_bound_observation": raw_bound,
        "termination_condition": raw_status,
        "solve_wall_seconds": elapsed,
        "error": error,
        "requested_options": requested,
        "effective_options": effective,
        "option_set_statuses": statuses,
        "core_gap_criterion": criterion,
        "objective_transform": {
            "canonical_sense_sign": sign,
            "external_constant": objective_constant,
            "canonical_objective": "minimize(sign * (original_objective - external_constant))",
        },
    }
    profile = dict(backend)
    profile["license_status"] = "unavailable" if error and "license" in error.casefold() else "accepted_for_this_solve" if returned else "unknown"
    if backend_id == "cvxpy_copt":
        profile["license_scope"] = "community_limit_unknown" if returned else "unknown"
    if log_path is not None:
        _set_log_file(log_path, f"CVXPY {solver_name} status={raw_status}; native log capture unavailable")
    return SolverObservation(record, profile, termination, raw_status, invoked, returned, primals, incumbent, bound, nonfinite, elapsed, error)


def _run_ortools_clp(
    model: Any,
    *,
    backend: Mapping[str, Any],
    objective_sense: str | None,
    time_limit_seconds: float | None,
    relative_gap: float,
    absolute_gap: float,
    feasibility_tolerance: float,
    integrality_tolerance: float,
    log_path: str | None,
) -> SolverObservation:
    from pyomo.common.collections import ComponentMap
    from pyomo.repn.standard_repn import generate_standard_repn
    from ortools.linear_solver import pywraplp

    _reject_unverified_components(model, interface="OR-Tools CLP")
    variables, constraints, objectives = _require_linear_model(model)
    if _pyomo_has_integer_variables(variables):
        raise ContractError("capability_uncovered", "OR-Tools CLP path is LP-only")
    solver = pywraplp.Solver.CreateSolver("CLP")
    if solver is None:
        raise ContractError("environment_unavailable", "OR-Tools MPSolver CLP interface is unavailable")
    native_vars: Any = ComponentMap()
    for index, variable in enumerate(variables):
        lower = _pyomo_bound_value(variable.lb, side="lower")
        upper = _pyomo_bound_value(variable.ub, side="upper")
        native = solver.NumVar(-solver.infinity() if lower is None else lower, solver.infinity() if upper is None else upper, f"v{index}")
        if variable.fixed:
            fixed = _component_value(variable.value)
            if fixed is None:
                raise ContractError("capability_uncovered", "fixed Pyomo variable has no finite value")
            native.SetBounds(fixed, fixed)
        native_vars[variable] = native

    def linear(expression: Any) -> tuple[list[tuple[Any, float]], float]:
        repn = generate_standard_repn(expression, compute_values=True, quadratic=True)
        if repn.nonlinear_expr is not None or repn.quadratic_vars:
            raise ContractError("capability_uncovered", "OR-Tools CLP supports linear expressions only")
        terms: list[tuple[Any, float]] = []
        for variable, coefficient in zip(repn.linear_vars, repn.linear_coefs, strict=True):
            coef = _component_value(coefficient)
            if coef is None or variable not in native_vars:
                raise ContractError("capability_uncovered", "CLP expression contains unsupported terms")
            terms.append((variable, coef))
        constant = _component_value(repn.constant or 0.0)
        if constant is None:
            raise ContractError("capability_uncovered", "CLP expression constant is nonfinite")
        return terms, constant

    for constraint in constraints:
        terms, constant = linear(constraint.body)
        lower = _pyomo_bound_value(constraint.lower, side="lower")
        upper = _pyomo_bound_value(constraint.upper, side="upper")
        low = -solver.infinity() if lower is None else lower - constant
        high = solver.infinity() if upper is None else upper - constant
        row = solver.RowConstraint(low, high, constraint.name)
        for variable, coefficient in terms:
            row.SetCoefficient(native_vars[variable], coefficient)
    if objectives:
        terms, constant = linear(objectives[0].expr)
        objective = solver.Objective()
        for variable, coefficient in terms:
            objective.SetCoefficient(native_vars[variable], coefficient)
        objective.SetOffset(constant)
    else:
        objective = solver.Objective()
    if objective_sense == "max":
        objective.SetMaximization()
    else:
        objective.SetMinimization()
    time_limit_milliseconds = None
    if time_limit_seconds is not None:
        time_limit_milliseconds = _ceil_native_milliseconds(time_limit_seconds)
        solver.SetTimeLimit(time_limit_milliseconds)
    if log_path is not None:
        _set_log_file(log_path, "OR-Tools CLP native log capture unavailable")
    started = time.perf_counter()
    invoked = False
    returned = False
    raw_status_value: Any = "ABNORMAL"
    error: str | None = None
    primals = ComponentMap()
    incumbent = None
    raw_valid = None
    nonfinite: dict[str, str] = {}
    raw_bound: dict[str, Any] = {
        "source": "OR-Tools MPSolver Objective.BestBound",
        "accepted": False,
        "reason": "global-bound mapping is not established for this CLP adapter",
    }
    try:
        invoked = True
        raw_status_value = solver.Solve()
        returned = True
        if raw_status_value in {pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE}:
            for variable, native in native_vars.items():
                primals[variable] = native.solution_value()
            raw_valid = len(primals) == len(variables)
            if objectives:
                incumbent = _finite_number(objective.Value())
                if incumbent is None:
                    nonfinite["best_feasible_objective"] = repr(objective.Value())
        if returned:
            try:
                observed_bound = objective.BestBound()
                finite_bound = _result_number(observed_bound)
                raw_bound["raw_value"] = finite_bound
                raw_bound["raw_repr"] = repr(observed_bound)
                if finite_bound is None:
                    nonfinite["best_objective_bound"] = repr(observed_bound)
            except Exception as exc:
                raw_bound["raw_repr"] = f"unavailable: {type(exc).__name__}"
    except Exception as exc:
        error = _safe_error(exc)
    elapsed = time.perf_counter() - started
    status_names = {}
    for name in ("OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNBOUNDED", "ABNORMAL", "NOT_SOLVED", "MODEL_INVALID"):
        value = getattr(pywraplp.Solver, name, None)
        if value is not None:
            status_names[value] = name
    raw_status = status_names.get(raw_status_value, _enum_name(raw_status_value))
    termination = {
        "OPTIMAL": "optimal",
        "FEASIBLE": "feasible",
        "INFEASIBLE": "infeasible",
        "UNBOUNDED": "unbounded",
        "ABNORMAL": "numeric_error",
        "NOT_SOLVED": "unknown",
    }.get(raw_status, "unknown")
    requested, effective, statuses, criterion = _param_record(
        time_limit_seconds=time_limit_seconds,
        relative_gap=relative_gap,
        absolute_gap=absolute_gap,
        threads=None,
        random_seed=None,
        feasibility_tolerance=feasibility_tolerance,
        integrality_tolerance=integrality_tolerance,
        native_parameters={"time_limit_milliseconds": time_limit_milliseconds} if time_limit_milliseconds is not None else {},
    )
    statuses["time_limit_seconds"] = "submitted_no_readback" if time_limit_seconds is not None else "not_requested"
    if time_limit_seconds is not None:
        effective["parameters"] = {}
    record = {
        "termination": termination,
        "raw_status": raw_status,
        "solver_invoked": invoked,
        "solver_returned": returned,
        "raw_solution_value_valid": raw_valid,
        "best_feasible_objective": incumbent,
        "best_objective_bound": None,
        "bound_evidence": None,
        "raw_bound_observation": raw_bound,
        "termination_condition": raw_status,
        "solve_wall_seconds": elapsed,
        "error": error,
        "requested_options": requested,
        "effective_options": effective,
        "option_set_statuses": statuses,
        "core_gap_criterion": criterion,
    }
    profile = dict(backend)
    profile["license_status"] = "accepted_for_this_solve" if returned else "unknown"
    return SolverObservation(record, profile, termination, raw_status, invoked, returned, primals, incumbent, None, nonfinite, elapsed, error)


def run_solver_once(
    model: Any,
    *,
    backend_id: str,
    objective_sense: str | None,
    time_limit_seconds: float | None,
    relative_gap: float,
    absolute_gap: float,
    threads: int | None,
    random_seed: int | None,
    feasibility_tolerance: float,
    integrality_tolerance: float,
    backend_profile: Mapping[str, Any],
    log_path: str | None = None,
    warm_start: bool = False,
) -> SolverObservation:
    """Run one selected adapter once and return raw evidence for Core checks."""
    options = {
        "time_limit_seconds": time_limit_seconds,
        "relative_gap": relative_gap,
        "absolute_gap": absolute_gap,
        "threads": threads,
        "random_seed": random_seed,
        "feasibility_tolerance": feasibility_tolerance,
        "integrality_tolerance": integrality_tolerance,
        "warm_start": warm_start,
    }
    validate_solver_options(backend_id, options)
    profile = dict(backend_profile or backend_profile_for(backend_id))
    if profile.get("id") != backend_id:
        raise ContractError("output_invalid", "solver profile does not match the selected backend")
    if objective_sense not in {None, "min", "max"}:
        raise ContractError("invalid_argument", "objective sense must be min, max, or null")
    if backend_id == "highs":
        return _run_highs(model, backend=profile, objective_sense=objective_sense, log_path=log_path, **{k: options[k] for k in ("time_limit_seconds", "relative_gap", "absolute_gap", "threads", "random_seed", "feasibility_tolerance", "integrality_tolerance", "warm_start")})
    if backend_id in _CVXPY_SOLVERS:
        return _run_cvxpy(model, backend_id=backend_id, backend=profile, objective_sense=objective_sense, log_path=log_path, **{k: options[k] for k in ("time_limit_seconds", "relative_gap", "absolute_gap", "threads", "random_seed", "feasibility_tolerance", "integrality_tolerance")})
    if backend_id == "ortools_clp":
        return _run_ortools_clp(model, backend=profile, objective_sense=objective_sense, log_path=log_path, **{k: options[k] for k in ("time_limit_seconds", "relative_gap", "absolute_gap", "feasibility_tolerance", "integrality_tolerance")})
    if backend_id == "cp_sat":
        from .models import is_native_cp_model_build

        if is_native_cp_model_build(model):
            return _run_cp_sat_native(
                model,
                backend=profile,
                objective_sense=objective_sense,
                log_path=log_path,
                **{k: options[k] for k in ("time_limit_seconds", "relative_gap", "absolute_gap", "threads", "random_seed", "feasibility_tolerance", "integrality_tolerance")},
            )
        return _run_cp_sat(model, backend=profile, objective_sense=objective_sense, log_path=log_path, **{k: options[k] for k in ("time_limit_seconds", "relative_gap", "absolute_gap", "threads", "random_seed", "feasibility_tolerance", "integrality_tolerance")})
    return _run_native_pyomo(model, backend_id=backend_id, backend=profile, objective_sense=objective_sense, log_path=log_path, **{k: options[k] for k in ("time_limit_seconds", "relative_gap", "absolute_gap", "threads", "random_seed", "feasibility_tolerance", "integrality_tolerance", "warm_start")})
