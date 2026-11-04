from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from optagent_core.contracts import ContractError
from optagent_core.solver_protocol import (
    DIRECT_PYTHON_BACKEND_ID,
    SUPPORTED_BACKEND_IDS,
    _NATIVE_FEATURES,
    _BACKEND_SPECS,
    _cvxpy_native_options,
    _normalize_termination,
    _pyomo_native_options,
    _pyomo_global_bound,
    _require_linear_model,
    _require_polynomial_model,
    _safe_error,
    run_solver_once,
    direct_python_profile,
    validate_direct_python_options,
    validate_solver_options,
)


def test_direct_python_profile_has_no_native_package_claims_or_solver_controls():
    profile = direct_python_profile()
    assert profile["id"] == DIRECT_PYTHON_BACKEND_ID
    assert profile["interface"] == "solve(bundle)"
    assert profile["package_versions"] == {}
    for option, value in (
        ("time_limit_seconds", 1.0),
        ("threads", 1),
        ("random_seed", 0),
        ("warm_start", True),
        ("write_log", True),
        ("export_model", True),
    ):
        with pytest.raises(ContractError) as caught:
            validate_direct_python_options({option: value})
        assert caught.value.code == "unsupported_option"
        assert caught.value.pointer == f"/solve_options/{option}"


def test_direct_python_profile_observes_only_standard_unconditional_frozen_dependencies(monkeypatch):
    from optagent_core import solver_protocol

    observed = []

    def version(distribution):
        observed.append(distribution)
        return {"numpy": "2.3.2", "pyyaml": "6.0", "legacy-dist": "1.4"}[distribution]

    monkeypatch.setattr(solver_protocol.importlib_metadata, "version", version)
    profile = direct_python_profile(
        b"""# declared direct dependencies
numpy
PyYAML[fast]>=6; python_version < '3'
git+https://example.invalid/repo.git#egg=legacy_dist
https://example.invalid/wheels/phantom.whl
--index-url https://example.invalid/simple
"""
    )

    assert observed == ["numpy", "pyyaml", "legacy-dist"]
    assert profile["package_versions"] == {"legacy-dist": "1.4", "numpy": "2.3.2", "pyyaml": "6.0"}


def test_safe_error_preserves_license_diagnostics_while_redacting_secrets_and_paths():
    safe = _safe_error(RuntimeError(
        "Commercial license capacity limit exceeded for 12 workers at /opt/vendor/license.dat; token=synthetic-secret"
    ))
    assert "Commercial license capacity limit exceeded for 12 workers" in safe
    assert "[path redacted]" in safe
    assert "credential=[redacted]" in safe
    assert "synthetic-secret" not in safe
    assert "/opt/vendor/license.dat" not in safe

    ordinary = _safe_error(RuntimeError("solver stalled at /var/log/solver.err; token=ordinary-secret"))
    assert ordinary == "RuntimeError: solver stalled at [path redacted] credential=[redacted]"


@pytest.mark.parametrize(
    ("reason", "marker"),
    [
        ("license size limit exceeded", "license_key"),
        ("licence expired", "LICENCE-KEY"),
        ("license size limit exceeded", "licensekey"),
        ("licence expired", "licence key"),
    ],
)
def test_safe_error_redacts_license_key_markers_and_preserves_reasons(reason, marker):
    safe = _safe_error(RuntimeError(f"{reason}; {marker}=ABCD-1234"))

    assert reason in safe
    assert "credential=[redacted]" in safe
    assert "ABCD-1234" not in safe


def _run_selected_backend(model, backend_id, objective_sense):
    return run_solver_once(
        model,
        backend_id=backend_id,
        objective_sense=objective_sense,
        time_limit_seconds=5.0,
        relative_gap=0.0,
        absolute_gap=0.0,
        threads=1 if backend_id == "cp_sat" else None,
        random_seed=0 if backend_id == "cp_sat" else None,
        feasibility_tolerance=1e-6,
        integrality_tolerance=1e-6,
        backend_profile={"id": backend_id},
    )


def test_solver_registry_exposes_the_frozen_nineteen_backend_ids():
    assert SUPPORTED_BACKEND_IDS == frozenset(
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
    for backend_id in ("cplex", "scip", "xpress"):
        assert any("quadratic-polynomial QP/QCP" in family for family in _BACKEND_SPECS[backend_id]["families"])
    for backend_id in ("gurobi", "mosek"):
        families = " ".join(_BACKEND_SPECS[backend_id]["families"])
        assert "quadratic-polynomial QP/QCP path supported by current Pyomo plugin/writer" in families
        assert "license/runtime acceptance not yet verified" in families


@pytest.mark.parametrize(
    "sense,lower_bound,upper_bound,expected,direction",
    [
        ("min", 0.0, 665.83162431, 0.0, "lower"),
        ("max", -665.83162431, 0.0, 0.0, "upper"),
    ],
)
def test_cbc_milp_problem_bound_uses_objective_sense_and_preserves_zero(
    sense, lower_bound, upper_bound, expected, direction
):
    results = SimpleNamespace(
        problem=SimpleNamespace(lower_bound=lower_bound, upper_bound=upper_bound)
    )

    bound, evidence, raw = _pyomo_global_bound(
        results,
        sense,
        integer_model=True,
        backend_id="cbc",
    )

    assert bound == expected
    assert evidence["scope"] == "global"
    assert evidence["interface"] == "cbc"
    assert evidence["objective_sense"] == sense
    assert evidence["value_direction"] == direction
    assert raw["raw_value"] == expected


@pytest.mark.parametrize("raw_value", [None, float("inf"), float("-inf"), float("nan")])
def test_cbc_milp_problem_bound_requires_a_finite_observed_field(raw_value):
    results = SimpleNamespace(
        problem=SimpleNamespace(lower_bound=raw_value, upper_bound=665.83162431)
    )

    bound, evidence, raw = _pyomo_global_bound(
        results,
        "min",
        integer_model=True,
        backend_id="cbc",
    )

    assert bound is None
    assert evidence is None
    assert raw["raw_value"] is None


@pytest.mark.parametrize(
    "objective_sense,integer_domain,fixed_integer,unused_binary,native_mip_node_count,primal_extraction_fails,termination_condition,expected_termination,raw_status,incumbent,reported_bound,expected_bound",
    [
        ("max", False, False, False, -1, False, "maxIterations", "iteration_limit", "kIterationLimit", 0.0, 0.0, None),
        ("min", False, False, False, -1, False, "maxIterations", "iteration_limit", "kIterationLimit", 0.0, 0.0, None),
        ("max", False, False, False, -1, False, "optimal", "optimal", "kOptimal", 2.0, 2.0, 2.0),
        ("min", False, False, False, -1, False, "optimal", "optimal", "kOptimal", -2.0, -2.0, -2.0),
        ("max", True, False, False, 0, False, "maxTimeLimit", "time_limit", "kTimeLimit", 0.0, 2.0, 2.0),
        ("min", True, False, False, 0, False, "maxTimeLimit", "time_limit", "kTimeLimit", 0.0, -2.0, -2.0),
        ("max", True, False, False, 0, True, "maxTimeLimit", "time_limit", "kTimeLimit", 0.0, 2.0, 2.0),
        ("max", True, True, False, -1, False, "maxIterations", "iteration_limit", "kIterationLimit", 0.0, 0.0, None),
        ("min", True, True, False, -1, False, "maxIterations", "iteration_limit", "kIterationLimit", 0.0, 0.0, None),
        ("max", False, False, True, -1, False, "maxIterations", "iteration_limit", "kIterationLimit", 0.0, 0.0, None),
        ("max", True, False, False, None, False, "maxIterations", "iteration_limit", "kIterationLimit", 0.0, 2.0, None),
    ],
)
def test_highs_qualifies_nonoptimal_bounds_from_native_model_classification(
    monkeypatch,
    objective_sense,
    integer_domain,
    fixed_integer,
    unused_binary,
    native_mip_node_count,
    primal_extraction_fails,
    termination_condition,
    expected_termination,
    raw_status,
    incumbent,
    reported_bound,
    expected_bound,
):
    pyo = pytest.importorskip("pyomo.environ")

    class NativeStatus:
        def __init__(self, name):
            self.name = name

    ok_status = NativeStatus("kOk")
    highs_status = SimpleNamespace(kOk=ok_status)
    monkeypatch.setitem(sys.modules, "highspy", SimpleNamespace(HighsStatus=highs_status))

    class FakeNativeHighs:
        def __init__(self, raw_status):
            self.options = {}
            self.raw_status = raw_status

        def setOptionValue(self, name, value):
            self.options[name] = value
            return ok_status

        def getOptionValue(self, name):
            return ok_status, self.options[name]

        def getModelStatus(self):
            return NativeStatus(self.raw_status)

        def getSolution(self):
            return SimpleNamespace(value_valid=True)

        def getInfo(self):
            return SimpleNamespace(mip_node_count=native_mip_node_count)

    class FakeHighs:
        def __init__(self):
            self.config = SimpleNamespace(load_solution=True, time_limit=None, warmstart=False)
            self.highs_options = {}
            self._solver_model = None

        def set_instance(self, actual_model):
            self.model = actual_model
            self._solver_model = FakeNativeHighs(raw_status)

        def solve(self, actual_model):
            assert actual_model is self.model
            return SimpleNamespace(
                termination_condition=termination_condition,
                best_feasible_objective=incumbent,
                best_objective_bound=reported_bound,
            )

        def get_primals(self):
            if primal_extraction_fails:
                raise RuntimeError("controlled primal extraction failure")
            from pyomo.common.collections import ComponentMap

            return ComponentMap(
                (variable, 0.0)
                for variable in self.model.component_data_objects(pyo.Var, active=True, descend_into=True)
                if not variable.fixed
            )

    appsi_solvers = importlib.import_module("pyomo.contrib.appsi.solvers")
    monkeypatch.setattr(appsi_solvers, "Highs", FakeHighs)

    model = pyo.ConcreteModel()
    model.x = pyo.Var(domain=pyo.Binary if integer_domain else pyo.NonNegativeReals, bounds=(0, 1))
    model.y = pyo.Var(domain=pyo.NonNegativeReals, bounds=(0, 1))
    model.feasible = pyo.Constraint(expr=model.x + model.y >= 0)
    objective = model.x + model.y
    if objective_sense == "min":
        objective = -objective
    model.objective = pyo.Objective(
        expr=objective,
        sense=pyo.minimize if objective_sense == "min" else pyo.maximize,
    )
    if fixed_integer:
        model.x.fix(0)
    if unused_binary:
        model.unused_binary = pyo.Var(domain=pyo.Binary)

    observed = run_solver_once(
        model,
        backend_id="highs",
        objective_sense=objective_sense,
        time_limit_seconds=5.0,
        relative_gap=0.0,
        absolute_gap=0.0,
        threads=None,
        random_seed=None,
        feasibility_tolerance=1e-6,
        integrality_tolerance=1e-6,
        backend_profile={"id": "highs"},
    )

    assert observed.solver_invoked is True
    assert observed.solver_returned is True
    assert observed.termination == expected_termination
    assert observed.raw_status == raw_status
    assert observed.record["raw_solution_value_valid"] is True
    assert observed.solver_incumbent_objective == incumbent
    assert observed.bound_value == expected_bound
    assert observed.record["best_objective_bound"] == expected_bound
    if expected_bound is None:
        assert observed.record["bound_evidence"] is None
        assert observed.record["raw_bound_observation"] == {
            "source": "Pyomo APPSI HiGHS results.best_objective_bound",
            "raw_value": reported_bound,
            "accepted": False,
            "reason": (
                "nonoptimal_continuous_lp_bound_not_qualified"
                if native_mip_node_count == -1
                else "native_mip_node_count_unavailable_for_nonoptimal_bound"
            ),
        }
    else:
        assert observed.record["bound_evidence"]["scope"] == "global"
        assert observed.record["bound_evidence"]["value_direction"] == (
            "lower" if objective_sense == "min" else "upper"
        )
        assert observed.record["raw_bound_observation"]["raw_value"] == reported_bound
        if primal_extraction_fails:
            assert observed.primals == {}
            assert observed.error == "RuntimeError: controlled primal extraction failure"


@pytest.mark.parametrize(
    "sense,integer_model",
    [("min", False), ("max", False), (None, True)],
)
def test_cbc_problem_field_is_not_global_bound_without_integer_model_and_sense(sense, integer_model):
    results = SimpleNamespace(
        problem=SimpleNamespace(lower_bound=0.0, upper_bound=665.83162431)
    )

    assert _pyomo_global_bound(
        results,
        sense,
        integer_model=integer_model,
        backend_id="cbc",
    ) == (None, None, None)


def test_cbc_conflicting_bound_is_rejected_without_changing_termination_or_extracted_point(monkeypatch):
    pyo = pytest.importorskip("pyomo.environ")
    model = pyo.ConcreteModel()
    model.x = pyo.Var(domain=pyo.Binary)
    model.cost = pyo.Objective(expr=model.x, sense=pyo.minimize)

    class FakeSolver:
        def __init__(self):
            self.options = {}

        def solve(self, actual_model, **kwargs):
            assert actual_model is model
            assert kwargs == {"load_solutions": False}
            return SimpleNamespace(
                solution=[object()],
                solver=SimpleNamespace(status="aborted", termination_condition="maxTimeLimit"),
                problem=SimpleNamespace(lower_bound=1.0, upper_bound=0.0),
            )

    fake_solver = FakeSolver()
    monkeypatch.setattr(pyo, "SolverFactory", lambda _name: fake_solver)
    monkeypatch.setattr(
        model.solutions,
        "load_from",
        lambda _results: model.x.set_value(0.0, skip_validation=True),
    )

    observed = run_solver_once(
        model,
        backend_id="cbc",
        objective_sense="min",
        time_limit_seconds=5.0,
        relative_gap=0.0,
        absolute_gap=0.0,
        threads=None,
        random_seed=None,
        feasibility_tolerance=1e-6,
        integrality_tolerance=1e-6,
        backend_profile={"id": "cbc"},
    )

    assert observed.solver_returned is True
    assert observed.termination == "time_limit"
    assert observed.primals[model.x] == 0.0
    assert observed.record["raw_solution_value_valid"] is True
    assert observed.solver_incumbent_objective == 0.0
    assert observed.bound_value is None
    assert observed.record["bound_evidence"] is None
    assert observed.record["raw_bound_observation"]["accepted"] is False
    assert observed.record["raw_bound_observation"]["reason"] == "bound_conflicts_with_solver_incumbent"


def test_native_pyomo_archives_unicode_output_log_through_ascii_temporary_path(monkeypatch, tmp_path):
    pyo = pytest.importorskip("pyomo.environ")
    model = pyo.ConcreteModel()
    model.x = pyo.Var(domain=pyo.Binary)
    model.cost = pyo.Objective(expr=model.x, sense=pyo.minimize)
    output_log = tmp_path / "模型输入" / "solver.log"
    output_log.parent.mkdir()
    passed_log_paths = []

    class FakeSolver:
        def __init__(self):
            self.options = {}

        def solve(self, actual_model, **kwargs):
            assert actual_model is model
            assert kwargs["load_solutions"] is False
            log_path = kwargs["logfile"]
            assert log_path.isascii()
            assert log_path != str(output_log)
            passed_log_paths.append(log_path)
            Path(log_path).write_text("native solver output\n", encoding="utf-8")
            return SimpleNamespace(
                solution=[object()],
                solver=SimpleNamespace(status="ok", termination_condition="optimal"),
                problem=SimpleNamespace(lower_bound=0.0, upper_bound=0.0),
            )

    monkeypatch.setattr(pyo, "SolverFactory", lambda _name: FakeSolver())
    monkeypatch.setattr(
        model.solutions,
        "load_from",
        lambda _results: model.x.set_value(0.0, skip_validation=True),
    )

    observed = run_solver_once(
        model,
        backend_id="cbc",
        objective_sense="min",
        time_limit_seconds=5.0,
        relative_gap=0.0,
        absolute_gap=0.0,
        threads=None,
        random_seed=None,
        feasibility_tolerance=1e-6,
        integrality_tolerance=1e-6,
        backend_profile={"id": "cbc"},
        log_path=str(output_log),
    )

    assert observed.termination == "optimal"
    assert observed.error is None
    assert output_log.read_text(encoding="utf-8") == "native solver output\n"
    assert len(passed_log_paths) == 1
    assert not Path(passed_log_paths[0]).parent.exists()


@pytest.mark.parametrize("solver_raises", [False, True])
def test_native_log_archive_failure_is_reported_and_preserves_solver_outcome(
    monkeypatch, tmp_path, solver_raises
):
    pyo = pytest.importorskip("pyomo.environ")
    from optagent_core import solver_protocol

    model = pyo.ConcreteModel()
    model.x = pyo.Var(domain=pyo.Binary)
    model.cost = pyo.Objective(expr=model.x, sense=pyo.minimize)
    output_log = tmp_path / "模型输入" / "solver.log"
    output_log.parent.mkdir()

    class FakeSolver:
        def __init__(self):
            self.options = {}

        def solve(self, _actual_model, **kwargs):
            Path(kwargs["logfile"]).write_text("solver output\n", encoding="utf-8")
            if solver_raises:
                raise RuntimeError("controlled native solver failure")
            return SimpleNamespace(
                solution=[object()],
                solver=SimpleNamespace(status="ok", termination_condition="optimal"),
                problem=SimpleNamespace(lower_bound=0.0, upper_bound=0.0),
            )

    def fail_copy(_source, _target):
        raise OSError("controlled logfile copy failure")

    monkeypatch.setattr(pyo, "SolverFactory", lambda _name: FakeSolver())
    monkeypatch.setattr(solver_protocol.shutil, "copyfile", fail_copy)
    monkeypatch.setattr(
        model.solutions,
        "load_from",
        lambda _results: model.x.set_value(0.0, skip_validation=True),
    )

    observed = run_solver_once(
        model,
        backend_id="cbc",
        objective_sense="min",
        time_limit_seconds=5.0,
        relative_gap=0.0,
        absolute_gap=0.0,
        threads=None,
        random_seed=None,
        feasibility_tolerance=1e-6,
        integrality_tolerance=1e-6,
        backend_profile={"id": "cbc"},
        log_path=str(output_log),
    )

    assert observed.error is not None
    assert "Native solver log archive failed" in observed.error
    if solver_raises:
        assert "controlled native solver failure" in observed.error
        assert observed.solver_returned is False
        assert observed.termination == "error"
    else:
        assert observed.solver_returned is True
        assert observed.termination == "optimal"


def _option_request(**updates):
    options = {
        "time_limit_seconds": None,
        "relative_gap": 1e-6,
        "absolute_gap": 0.0,
        "threads": None,
        "random_seed": None,
        "feasibility_tolerance": 1e-6,
        "integrality_tolerance": 1e-6,
    }
    options.update(updates)
    return options


def test_native_control_matrix_is_adapter_specific_and_rejects_unmapped_requests():
    assert _NATIVE_FEATURES == {
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

    for backend_id, request in (
        ("ipopt", {"time_limit_seconds": 5.0}),
        ("cvxpy_scs", {"threads": 2}),
        ("cvxpy_copt", {"random_seed": 3}),
        ("ortools_clp", {"threads": 2}),
        ("cvxpy_pdlp", {"random_seed": 3}),
    ):
        with pytest.raises(ContractError) as caught:
            validate_solver_options(backend_id, _option_request(**request))
        assert caught.value.code == "unsupported_option"

    with pytest.raises(ContractError) as caught:
        validate_solver_options("cbc", _option_request(random_seed=0))
    assert caught.value.code == "unsupported_option"


def test_warm_start_is_opt_in_strictly_boolean_and_rejected_without_an_adapter_path():
    # Omission is the default-off contract. Validation itself must not infer a
    # start merely because a model happens to contain initialized variables.
    validate_solver_options("cbc", _option_request())
    validate_solver_options("cbc", _option_request(warm_start=False))

    for invalid in (0, 1, None, "true"):
        with pytest.raises(ContractError) as caught:
            validate_solver_options("cbc", _option_request(warm_start=invalid))
        assert caught.value.code == "invalid_argument"

    for backend_id in ("cvxpy_scs", "ortools_clp", "cp_optimizer", "cp_sat"):
        with pytest.raises(ContractError) as caught:
            validate_solver_options(backend_id, _option_request(warm_start=True))
        assert caught.value.code == "unsupported_option"


@pytest.mark.parametrize("backend_id", ["cbc", "highs"])
@pytest.mark.parametrize("initial_values", ["full", "partial", "invalid"])
def test_pyomo_native_adapters_submit_warm_start_without_trusting_the_start_as_result(
    backend_id, initial_values, tmp_path, capfd, monkeypatch
):
    pyo = pytest.importorskip("pyomo.environ")
    if backend_id == "highs":
        pytest.importorskip("highspy")
        appsi_solvers = importlib.import_module("pyomo.contrib.appsi.solvers")
        real_highs = appsi_solvers.Highs

        class StreamingHighs(real_highs):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.config.stream_solver = True

        monkeypatch.setattr(appsi_solvers, "Highs", StreamingHighs)
    else:
        solver = pyo.SolverFactory("cbc")
        if not solver.available(exception_flag=False) or solver.warm_start_capable() is not True:
            pytest.skip("installed Pyomo CBC plugin/native does not expose the required warm-start path")

    # Keep this instance large enough for both native plugins to expose a
    # meaningful MIP-start decision. The optimum is 48 including the
    # objective constant; the feasible full start is intentionally suboptimal.
    weights = (2, 3, 4, 5, 7, 8)
    profits = (5, 6, 9, 10, 13, 14)
    capacity = 12
    objective_constant = 23
    model = pyo.ConcreteModel()
    model.I = pyo.RangeSet(0, len(weights) - 1)
    model.x = pyo.Var(model.I, domain=pyo.Binary)
    model.capacity = pyo.Constraint(
        expr=sum(weights[i] * model.x[i] for i in model.I) <= capacity
    )
    model.obj = pyo.Objective(
        expr=objective_constant + sum(profits[i] * model.x[i] for i in model.I),
        sense=pyo.maximize,
    )
    if initial_values == "full":
        start = (1, 1, 1, 0, 0, 0)
    elif initial_values == "partial":
        start = (1, 1, None, None, None, None)
    else:
        # Nonzero and infeasible: weight 14 exceeds the capacity 12. This
        # also ensures the CBC plugin writes a non-empty MIP-start file.
        start = (1, 1, 1, 1, 0, 0)
    for i, value in enumerate(start):
        model.x[i].set_value(value, skip_validation=True)

    log_path = str(tmp_path / f"{backend_id}-{initial_values}.log") if backend_id == "cbc" else None
    observed = run_solver_once(
        model,
        backend_id=backend_id,
        objective_sense="max",
        time_limit_seconds=10.0,
        relative_gap=0.0,
        absolute_gap=0.0,
        threads=None,
        random_seed=None,
        feasibility_tolerance=1e-6,
        integrality_tolerance=1e-6,
        backend_profile={"id": backend_id},
        log_path=log_path,
        warm_start=True,
    )

    record = observed.record
    assert observed.solver_invoked is True
    assert record["requested_options"]["native"]["warm_start"] is True
    assert record["effective_options"]["warm_start"] is None
    assert record["option_set_statuses"]["warm_start"] == "submitted_no_readback"
    # The submission record is not an acceptance claim. Only the current
    # solver-returned point is exposed as primals and objective evidence.
    assert observed.solver_returned is True
    assert observed.termination == "optimal"
    assert record["raw_solution_value_valid"] is True
    assert {variable.name for variable in observed.primals} == {
        model.x[i].name for i in model.I
    }
    solution = [observed.primals[model.x[i]] for i in model.I]
    assert all(value in (0, 1) for value in solution)
    assert sum(weights[i] * solution[i] for i in model.I) <= capacity
    assert sum(profits[i] * solution[i] for i in model.I) + objective_constant == 48
    assert observed.solver_incumbent_objective == pytest.approx(48)
    if backend_id == "cbc":
        native_log = (tmp_path / f"{backend_id}-{initial_values}.log").read_text(encoding="utf-8")
    else:
        captured = capfd.readouterr()
        native_log = captured.out + captured.err
    native_log = native_log.casefold()
    if backend_id == "cbc":
        assert " -mipstart " in native_log
        if initial_values == "invalid":
            assert "mipstart values could not be used to build a solution" in native_log
        else:
            assert "mipstart values read for" in native_log
            assert "mipstart provided solution with cost" in native_log
    elif initial_values == "invalid":
        assert "user-supplied values of discrete variables cannot yield feasible solution" in native_log
    else:
        assert "mip start solution is feasible" in native_log


@pytest.mark.parametrize("backend_id", ["cbc", "highs"])
def test_pyomo_warm_start_with_no_integer_solution_does_not_return_initialized_values(backend_id):
    pyo = pytest.importorskip("pyomo.environ")
    if backend_id == "highs":
        pytest.importorskip("highspy")
    else:
        solver = pyo.SolverFactory("cbc")
        if not solver.available(exception_flag=False) or solver.warm_start_capable() is not True:
            pytest.skip("installed Pyomo CBC plugin/native does not expose the required warm-start path")

    model = pyo.ConcreteModel()
    model.x = pyo.Var(domain=pyo.Binary, initialize=0)
    model.impossible = pyo.Constraint(expr=model.x >= 2)
    model.cost = pyo.Objective(expr=model.x, sense=pyo.minimize)
    observed = run_solver_once(
        model,
        backend_id=backend_id,
        objective_sense="min",
        time_limit_seconds=10.0,
        relative_gap=0.0,
        absolute_gap=0.0,
        threads=None,
        random_seed=None,
        feasibility_tolerance=1e-6,
        integrality_tolerance=1e-6,
        backend_profile={"id": backend_id},
        warm_start=True,
    )

    assert observed.solver_invoked is True
    assert observed.solver_returned is True
    assert observed.termination == "infeasible"
    assert observed.record["raw_solution_value_valid"] is False
    assert dict(observed.primals) == {}
    assert observed.solver_incumbent_objective is None
    assert model.x.value == 0
    assert observed.record["requested_options"]["native"]["warm_start"] is True
    assert observed.record["option_set_statuses"]["warm_start"] == "submitted_no_readback"


@pytest.mark.parametrize(
    "backend_id,expected",
    [
        (
            "gurobi",
            {
                "TimeLimit": 12.5,
                "Threads": 3,
                "Seed": 7,
                "MIPGap": 1e-6,
                "MIPGapAbs": 0.0,
            },
        ),
        (
            "cplex",
            {
                "timelimit": 12.5,
                "threads": 3,
                "randomseed": 7,
                "mip_tolerances_mipgap": 1e-6,
                "mip_tolerances_absmipgap": 0.0,
            },
        ),
        (
            "scip",
            {
                "limits/time": 12.5,
                "parallel/maxnthreads": 3,
                "randomization/randomseedshift": 7,
                "limits/gap": 1e-6,
                "limits/absgap": 0.0,
            },
        ),
        (
            "cbc",
            {
                "seconds": 12.5,
                "threads": 3,
                "randomSeed": 7,
                "randomCbcSeed": 7,
                "ratioGap": 1e-6,
                "allowableGap": 0.0,
            },
        ),
        (
            "mosek",
            {
                "dparam.optimizer_max_time": 12.5,
                "iparam.num_threads": 3,
                "iparam.mio_seed": 7,
                "dparam.mio_tol_rel_gap": 1e-6,
                "dparam.mio_tol_abs_gap": 0.0,
            },
        ),
    ],
)
def test_pyomo_native_option_maps_use_plugin_parameter_names(backend_id, expected):
    options, parameters = _pyomo_native_options(
        backend_id,
        time_limit_seconds=12.5,
        relative_gap=1e-6,
        absolute_gap=0.0,
        threads=3,
        random_seed=7,
    )
    assert options == expected
    assert parameters == expected


def test_pyomo_integer_seconds_and_elapsed_cp_optimizer_mapping_are_explicit():
    glpk, glpk_record = _pyomo_native_options(
        "glpk",
        time_limit_seconds=1.1,
        relative_gap=1e-6,
        absolute_gap=0.0,
        threads=None,
        random_seed=None,
    )
    assert glpk == {"tmlim": 2, "mipgap": 1e-6}
    assert glpk_record == glpk

    xpress, xpress_record = _pyomo_native_options(
        "xpress",
        time_limit_seconds=1.1,
        relative_gap=1e-6,
        absolute_gap=0.0,
        threads=None,
        random_seed=None,
    )
    assert xpress["maxtime"] == -2
    assert xpress["miprelstop"] == 1e-6
    assert xpress["mipabsstop"] == 0.0
    assert xpress_record == xpress

    cp_optimizer, cp_optimizer_record = _pyomo_native_options(
        "cp_optimizer",
        time_limit_seconds=2.25,
        relative_gap=1e-6,
        absolute_gap=0.0,
        threads=2,
        random_seed=9,
    )
    assert cp_optimizer == {
        "TimeLimit": 2.25,
        "TimeMode": "ElapsedTime",
        "Workers": 2,
        "RandomSeed": 9,
        "RelativeOptimalityTolerance": 1e-6,
    }
    assert cp_optimizer_record == cp_optimizer


def test_cvxpy_options_are_nested_or_named_for_the_selected_wrapper():
    copt, copt_record = _cvxpy_native_options(
        "cvxpy_copt",
        time_limit_seconds=4.5,
        relative_gap=1e-6,
        absolute_gap=0.0,
        threads=2,
        random_seed=None,
    )
    assert copt == {"TimeLimit": 4.5, "Threads": 2, "RelGap": 1e-6, "AbsGap": 0.0}
    assert copt_record == copt

    clarabel, clarabel_record = _cvxpy_native_options(
        "cvxpy_clarabel",
        time_limit_seconds=4.5,
        relative_gap=1e-6,
        absolute_gap=0.0,
        threads=2,
        random_seed=None,
    )
    assert clarabel == {
        "time_limit": 4.5,
        "max_threads": 2,
        "tol_gap_rel": 1e-6,
        "tol_gap_abs": 0.0,
    }
    assert clarabel_record == clarabel

    cvxopt, cvxopt_record = _cvxpy_native_options(
        "cvxpy_cvxopt",
        time_limit_seconds=None,
        relative_gap=1e-6,
        absolute_gap=0.0,
        threads=None,
        random_seed=None,
    )
    assert cvxopt == {"reltol": 1e-6}
    assert cvxopt_record == cvxopt

    scs, scs_record = _cvxpy_native_options(
        "cvxpy_scs",
        time_limit_seconds=4.5,
        relative_gap=1e-6,
        absolute_gap=0.0,
        threads=None,
        random_seed=None,
    )
    assert scs == {"time_limit_secs": 4.5}
    assert scs_record == scs

    osqp, osqp_record = _cvxpy_native_options(
        "cvxpy_osqp",
        time_limit_seconds=4.5,
        relative_gap=1e-6,
        absolute_gap=0.0,
        threads=None,
        random_seed=None,
    )
    assert osqp == {"time_limit": 4.5}
    assert osqp_record == osqp

    glop, glop_record = _cvxpy_native_options(
        "cvxpy_glop",
        time_limit_seconds=1.0001,
        relative_gap=1e-6,
        absolute_gap=0.0,
        threads=3,
        random_seed=11,
    )
    assert int(glop["time_limit_sec"] * 1000.0) == 1001
    assert glop["parameters_proto"].num_omp_threads == 3
    assert glop["parameters_proto"].random_seed == 11
    assert glop_record == {
        "time_limit_sec": glop["time_limit_sec"],
        "parameters_proto.num_omp_threads": 3,
        "parameters_proto.random_seed": 11,
    }

    pdlp, pdlp_record = _cvxpy_native_options(
        "cvxpy_pdlp",
        time_limit_seconds=1.25,
        relative_gap=1e-6,
        absolute_gap=0.0,
        threads=4,
        random_seed=None,
    )
    proto = pdlp["parameters_proto"]
    assert pdlp["time_limit_sec"] == 1.25
    assert proto.num_threads == 4
    assert proto.termination_criteria.simple_optimality_criteria.eps_optimal_relative == 1e-6
    assert proto.termination_criteria.simple_optimality_criteria.eps_optimal_absolute == 0.0
    assert pdlp_record["parameters_proto.num_threads"] == 4


@pytest.mark.parametrize(
    "relative_gap,absolute_gap,expected",
    [
        (0.0, 0.0, {"reltol": 0.0}),
        (1e-7, 0.0, {"reltol": 1e-7}),
        (0.0, 1e-7, {"reltol": 0.0, "abstol": 1e-7}),
        (1e-7, 1e-7, {"reltol": 1e-7, "abstol": 1e-7}),
    ],
)
def test_cvxopt_maps_only_positive_absolute_gap_to_native_abstol(relative_gap, absolute_gap, expected):
    options, parameters = _cvxpy_native_options(
        "cvxpy_cvxopt",
        time_limit_seconds=None,
        relative_gap=relative_gap,
        absolute_gap=absolute_gap,
        threads=None,
        random_seed=None,
    )
    assert options == expected
    assert parameters == expected


@pytest.mark.parametrize(
    "relative_gap,absolute_gap",
    [(0.0, 0.0), (1e-7, 0.0), (0.0, 1e-7), (1e-7, 1e-7)],
)
def test_cvxopt_gap_combinations_keep_core_target_and_return_checked_qp(relative_gap, absolute_gap):
    pyo = pytest.importorskip("pyomo.environ")
    pytest.importorskip("cvxpy")
    pytest.importorskip("cvxopt")

    model = pyo.ConcreteModel()
    model.x = pyo.Var(domain=pyo.NonNegativeReals)
    model.y = pyo.Var(domain=pyo.NonNegativeReals)
    model.feasible = pyo.Constraint(expr=model.x + model.y >= 1)
    model.cost = pyo.Objective(expr=(model.x - 2) ** 2 + (model.y - 1) ** 2 + 7)

    observed = run_solver_once(
        model,
        backend_id="cvxpy_cvxopt",
        objective_sense="min",
        time_limit_seconds=None,
        relative_gap=relative_gap,
        absolute_gap=absolute_gap,
        threads=None,
        random_seed=None,
        feasibility_tolerance=1e-6,
        integrality_tolerance=1e-6,
        backend_profile={"id": "cvxpy_cvxopt"},
    )

    assert observed.solver_invoked is True
    assert observed.solver_returned is True
    assert observed.termination == "optimal"
    assert observed.record["raw_solution_value_valid"] is True
    assert {variable.name for variable in observed.primals} == {"x", "y"}
    x_value = observed.primals[model.x]
    y_value = observed.primals[model.y]
    assert x_value >= -1e-6
    assert y_value >= -1e-6
    assert x_value + y_value >= 1.0 - 1e-6
    returned_objective = (x_value - 2.0) ** 2 + (y_value - 1.0) ** 2 + 7.0
    assert returned_objective == pytest.approx(7.0, abs=1e-6)
    assert observed.solver_incumbent_objective == pytest.approx(returned_objective, abs=1e-6)
    assert observed.record["requested_options"]["core"] == {
        "relative_gap": relative_gap,
        "absolute_gap": absolute_gap,
        "feasibility_tolerance": 1e-6,
        "integrality_tolerance": 1e-6,
    }
    assert observed.record["core_gap_criterion"]["relative_gap"] == relative_gap
    assert observed.record["core_gap_criterion"]["absolute_gap"] == absolute_gap
    native_parameters = observed.record["requested_options"]["native"]["parameters"]
    assert native_parameters["reltol"] == relative_gap
    if absolute_gap > 0:
        assert native_parameters["abstol"] == absolute_gap
    else:
        assert "abstol" not in native_parameters
    assert observed.record["effective_options"]["parameters"] == {}


@pytest.mark.parametrize(
    "backend_id,status,nonlinear,expected",
    [
        ("ipopt", "optimal", True, "local_optimal"),
        ("ipopt", "infeasible", True, "unknown"),
        ("ipopt", "unbounded", True, "unknown"),
        ("xpress", "unbounded", True, "unknown"),
        ("xpress", "infeasibleOrUnbounded", True, "unknown"),
        ("xpress", "unbounded", False, "unbounded"),
    ],
)
def test_local_or_ambiguous_nonlinear_termination_is_not_promoted_to_global(backend_id, status, nonlinear, expected):
    assert _normalize_termination(
        status,
        backend_id=backend_id,
        has_primal=False,
        nonlinear_model=nonlinear,
    ) == expected


def test_quadratic_polynomial_preflight_accepts_qp_and_qcp_terms():
    pyo = pytest.importorskip("pyomo.environ")

    model = pyo.ConcreteModel()
    model.x = pyo.Var(bounds=(0, 4))
    model.y = pyo.Var(bounds=(0, 4))
    model.qcp = pyo.Constraint(expr=model.x**2 + model.y**2 <= 8)
    model.cost = pyo.Objective(expr=model.x**2 + model.x * model.y + model.y**2 - 7)

    variables, constraints, objectives, degree = _require_polynomial_model(model, maximum_degree=2)

    assert len(variables) == 2
    assert len(constraints) == 1
    assert len(objectives) == 1
    assert degree == 2


def test_linear_adapter_gate_still_rejects_quadratic_terms_and_degree_two_gate_rejects_cubic_terms():
    pyo = pytest.importorskip("pyomo.environ")

    quadratic = pyo.ConcreteModel()
    quadratic.x = pyo.Var(bounds=(0, 4))
    quadratic.cost = pyo.Objective(expr=quadratic.x**2)
    with pytest.raises(ContractError) as caught:
        _require_linear_model(quadratic)
    assert caught.value.code == "capability_uncovered"

    cubic = pyo.ConcreteModel()
    cubic.x = pyo.Var(bounds=(0, 4))
    cubic.cost = pyo.Objective(expr=cubic.x**3)
    with pytest.raises(ContractError) as caught:
        _require_polynomial_model(cubic, maximum_degree=2)
    assert caught.value.code == "capability_uncovered"


def test_ortools_clp_sparse_rows_preserve_senses_constants_objective_offset_and_maximize():
    pyo = pytest.importorskip("pyomo.environ")
    pytest.importorskip("ortools.linear_solver.pywraplp")

    model = pyo.ConcreteModel()
    model.x = pyo.Var(bounds=(0, 10))
    model.y = pyo.Var(bounds=(0, 10))
    model.lower = pyo.Constraint(expr=model.x + 1 >= 3)
    model.upper = pyo.Constraint(expr=model.x + 2 <= 6)
    model.range = pyo.Constraint(expr=(3, model.x + 1, 5))
    model.equal = pyo.Constraint(expr=model.y + 2 == 5)
    model.objective = pyo.Objective(expr=2 * model.x + model.y + 7, sense=pyo.maximize)

    observed = _run_selected_backend(model, "ortools_clp", "max")

    assert observed.solver_invoked is True
    assert observed.solver_returned is True
    assert observed.termination == "optimal"
    assert observed.raw_status == "OPTIMAL"
    assert observed.primals[model.x] == pytest.approx(4.0)
    assert observed.primals[model.y] == pytest.approx(3.0)
    assert observed.solver_incumbent_objective == pytest.approx(18.0)


@pytest.mark.parametrize(
    "lower,upper,expected_termination",
    [
        (0, 0, "optimal"),
        (1, None, "infeasible"),
    ],
)
def test_ortools_clp_keeps_zero_coefficient_rows_including_constant_contradictions(lower, upper, expected_termination):
    pyo = pytest.importorskip("pyomo.environ")
    pytest.importorskip("ortools.linear_solver.pywraplp")

    model = pyo.ConcreteModel()
    model.x = pyo.Var(bounds=(0, 1))
    model.empty_row = pyo.Constraint(expr=(lower, model.x - model.x, upper))
    model.objective = pyo.Objective(expr=model.x)

    observed = _run_selected_backend(model, "ortools_clp", "min")

    assert observed.solver_invoked is True
    assert observed.solver_returned is True
    assert observed.termination == expected_termination
    if expected_termination == "optimal":
        assert observed.primals[model.x] == pytest.approx(0.0)
        assert observed.solver_incumbent_objective == pytest.approx(0.0)
    else:
        assert observed.primals == {}


def test_ortools_clp_preserves_fixed_variable_bounds_and_objective_offset():
    pyo = pytest.importorskip("pyomo.environ")
    pytest.importorskip("ortools.linear_solver.pywraplp")

    model = pyo.ConcreteModel()
    model.x = pyo.Var(bounds=(0, 10))
    model.x.fix(3)
    model.row = pyo.Constraint(expr=model.x + 1 == 4)
    model.objective = pyo.Objective(expr=model.x + 5, sense=pyo.minimize)

    observed = _run_selected_backend(model, "ortools_clp", "min")

    assert observed.solver_returned is True
    assert observed.termination == "optimal"
    assert observed.primals[model.x] == pytest.approx(3.0)
    assert observed.solver_incumbent_objective == pytest.approx(8.0)


def test_cp_sat_uses_official_status_name_and_preserves_status_code_for_optimal_mip():
    pyo = pytest.importorskip("pyomo.environ")
    model_api = pytest.importorskip("ortools.sat.python.cp_model")

    model = pyo.ConcreteModel()
    model.x = pyo.Var(domain=pyo.Binary)
    model.y = pyo.Var(domain=pyo.Binary)
    model.cover = pyo.Constraint(expr=model.x + model.y >= 1)
    model.objective = pyo.Objective(expr=3 * model.x + 2 * model.y)

    observed = _run_selected_backend(model, "cp_sat", "min")

    assert observed.solver_invoked is True
    assert observed.solver_returned is True
    assert observed.raw_status == "OPTIMAL"
    assert observed.record["raw_status_code"] == int(model_api.OPTIMAL)
    assert observed.record["raw_status_name"] == "OPTIMAL"
    assert observed.termination == "optimal"
    assert observed.record["requested_options"]["native"]["parameters"]["max_time_in_seconds"] == 5.0
    assert observed.record["requested_options"]["native"]["parameters"]["num_search_workers"] == 1
    assert observed.record["requested_options"]["native"]["parameters"]["random_seed"] == 0
    assert observed.record["requested_options"]["native"]["warm_start"] is False
    assert observed.record["option_set_statuses"]["warm_start"] == "not_requested"
    assert observed.record["effective_options"]["warm_start"] is None
    assert observed.record["effective_options"]["time_limit_seconds"] is None
    assert observed.record["effective_options"]["threads"] is None
    assert observed.record["effective_options"]["random_seed"] is None
    assert observed.record["effective_options"]["parameters"] == {}
    assert observed.record["option_set_statuses"]["time_limit_seconds"] == "submitted_no_readback"
    assert observed.record["option_set_statuses"]["threads"] == "submitted_no_readback"
    assert observed.record["option_set_statuses"]["random_seed"] == "submitted_no_readback"
    assert observed.primals[model.x] == 0
    assert observed.primals[model.y] == 1
    assert observed.solver_incumbent_objective == pytest.approx(2.0)


def test_cp_sat_feasibility_model_without_objective_returns_a_checked_primal():
    pyo = pytest.importorskip("pyomo.environ")
    model_api = pytest.importorskip("ortools.sat.python.cp_model")

    model = pyo.ConcreteModel()
    model.x = pyo.Var(domain=pyo.Integers, bounds=(0, 2))
    model.feasible = pyo.Constraint(expr=model.x >= 1)

    observed = _run_selected_backend(model, "cp_sat", None)

    assert observed.solver_returned is True
    assert observed.record["raw_status_code"] == int(model_api.OPTIMAL)
    assert observed.record["raw_status_name"] == "OPTIMAL"
    assert observed.record["raw_solution_value_valid"] is True
    assert 1 <= observed.primals[model.x] <= 2
    assert observed.solver_incumbent_objective is None


def test_cp_sat_infeasible_status_does_not_create_a_primal():
    pyo = pytest.importorskip("pyomo.environ")
    model_api = pytest.importorskip("ortools.sat.python.cp_model")

    model = pyo.ConcreteModel()
    model.x = pyo.Var(domain=pyo.Binary)
    model.impossible = pyo.Constraint(expr=model.x >= 2)
    model.objective = pyo.Objective(expr=model.x)

    observed = _run_selected_backend(model, "cp_sat", "min")

    assert observed.solver_returned is True
    assert observed.record["raw_status_code"] == int(model_api.INFEASIBLE)
    assert observed.record["raw_status_name"] == "INFEASIBLE"
    assert observed.termination == "infeasible"
    assert observed.record["raw_solution_value_valid"] is False
    assert observed.primals == {}
    assert observed.solver_incumbent_objective is None


def test_cp_sat_unknown_status_does_not_create_a_primal(monkeypatch):
    pyo = pytest.importorskip("pyomo.environ")
    model_api = pytest.importorskip("ortools.sat.python.cp_model")
    real_solver = model_api.CpSolver
    status_calls = []

    class UnknownStatusSolver:
        def __init__(self):
            self.delegate = real_solver()

        def __getattr__(self, name):
            return getattr(self.delegate, name)

        def Solve(self, cp_model):
            return model_api.UNKNOWN

        def StatusName(self, status=None):
            status_calls.append(status)
            return self.delegate.StatusName(status)

        def Value(self, _native_var):
            raise AssertionError("UNKNOWN status must not attempt primal extraction")

    monkeypatch.setattr(model_api, "CpSolver", UnknownStatusSolver)
    model = pyo.ConcreteModel()
    model.x = pyo.Var(domain=pyo.Binary)
    model.objective = pyo.Objective(expr=model.x)

    observed = _run_selected_backend(model, "cp_sat", "min")

    assert status_calls == [model_api.UNKNOWN]
    assert observed.solver_returned is True
    assert observed.record["raw_status_code"] == int(model_api.UNKNOWN)
    assert observed.record["raw_status_name"] == "UNKNOWN"
    assert observed.termination == "unknown"
    assert observed.record["raw_solution_value_valid"] is False
    assert observed.primals == {}


def test_cp_sat_solve_exception_does_not_decode_initial_unknown_as_native_status(monkeypatch):
    pyo = pytest.importorskip("pyomo.environ")
    model_api = pytest.importorskip("ortools.sat.python.cp_model")
    real_solver = model_api.CpSolver
    status_calls = []

    class RaisingSolver:
        def __init__(self):
            self.delegate = real_solver()

        def __getattr__(self, name):
            return getattr(self.delegate, name)

        def Solve(self, _cp_model):
            raise RuntimeError("controlled solve failure")

        def StatusName(self, status=None):
            status_calls.append(status)
            raise AssertionError("StatusName must not receive an initial placeholder")

    monkeypatch.setattr(model_api, "CpSolver", RaisingSolver)
    model = pyo.ConcreteModel()
    model.x = pyo.Var(domain=pyo.Binary)
    model.objective = pyo.Objective(expr=model.x)

    observed = _run_selected_backend(model, "cp_sat", "min")

    assert status_calls == []
    assert observed.solver_invoked is True
    assert observed.solver_returned is False
    assert observed.record["raw_status_code"] is None
    assert observed.record["raw_status_name"] == "UNKNOWN"
    assert observed.termination == "error"
    assert observed.record["raw_solution_value_valid"] is None
    assert observed.primals == {}
    assert observed.error.startswith("RuntimeError:")
