from __future__ import annotations

import math
import sys
from types import ModuleType, SimpleNamespace

import pytest

from optagent_core.results import (
    CheckedModel,
    HighsObservation,
    ObjectiveSpec,
    check_pyomo_model,
    checked_lexicographic_lock,
    objective_for_layer,
    objective_values_match,
    result_record,
    run_highs_once,
    business_check_is_fully_passed,
    direct_witness_from_output,
    validate_direct_witness_record,
)
from optagent_core.contracts import ContractError
from optagent_core.worker import _requires_solver


def _native_observation_record(
    *,
    incumbent=100.0,
    bound=99.0,
    sense="min",
    termination="time_limit",
    raw_solution_value_valid=True,
    evidence=True,
):
    direction = "lower" if sense == "min" else "upper"
    return {
        "termination": termination,
        "raw_solution_value_valid": raw_solution_value_valid,
        "best_feasible_objective": incumbent,
        "best_objective_bound": bound,
        "bound_evidence": (
            {
                "source": "test native global bound",
                "scope": "global",
                "objective_sense": sense,
                "value_direction": direction,
                "interface": "test adapter",
                "objective_transform": {
                    "kind": "identity",
                    "formula": "native value uses checked objective units",
                },
                "raw_value": bound,
            }
            if evidence and bound is not None
            else None
        ),
    }


def _summary_with_native_values(
    *,
    checked_objective,
    incumbent,
    native_bound,
    sense="min",
    absolute_gap=0.0,
    relative_gap=0.0,
    termination="time_limit",
    raw_solution_value_valid=True,
    evidence_update=None,
    checked_status="pass",
    native_termination=None,
):
    from optagent_core.results import _result_numerical_summary, checked_global_bound_gap

    observation = _native_observation_record(
        incumbent=incumbent,
        bound=native_bound,
        sense=sense,
        termination=native_termination or termination,
        raw_solution_value_valid=raw_solution_value_valid,
    )
    if evidence_update is not None and isinstance(observation.get("bound_evidence"), dict):
        observation["bound_evidence"].update(evidence_update)
    strict_gap = (
        checked_global_bound_gap(native_bound, sense, checked_objective)
        if isinstance(native_bound, (int, float))
        and isinstance(checked_objective, (int, float))
        and math.isfinite(native_bound)
        and math.isfinite(checked_objective)
        else None
    )
    direction = "lower" if sense == "min" else "upper"
    strict_bound = (
        {"value": native_bound, "direction": direction, "scope": "global"}
        if strict_gap is not None
        else None
    )
    return _result_numerical_summary(
        check_record={"status": checked_status, "tolerances": {}},
        objective_value=checked_objective,
        objective_sense=sense,
        bound=strict_bound,
        absolute_gap=strict_gap[0] if strict_gap is not None else None,
        termination=termination,
        has_incumbent=True,
        requested_options={"mip_abs_gap": absolute_gap, "mip_rel_gap": relative_gap},
        effective_options={"mip_abs_gap": absolute_gap, "mip_rel_gap": relative_gap},
        core_gap_target={"absolute_gap": absolute_gap, "relative_gap": relative_gap},
        native_observation=observation,
    )


def test_direct_witness_distinguishes_no_point_from_valid_empty_point_and_binds_origin():
    identity = {
        "input_digest": "a" * 64,
        "instance_key": ["west", 7],
        "instance_binding_digest": "b" * 64,
    }
    plan = {"mode": "feasibility", "preference_note": "test"}

    assert direct_witness_from_output(None, plan, **identity) is None
    empty = direct_witness_from_output(
        {"variables": [], "objective_values": []}, plan, **identity
    )
    assert empty is not None
    assert empty["variables"] == []
    assert validate_direct_witness_record(empty, plan, **identity) == empty

    scalar_plan = {
        "mode": "scalar",
        "objective_component": "cost",
        "sense": "min",
        "unit": "USD",
        "preference_note": "test",
    }
    point = direct_witness_from_output(
        {
            "variables": [{"component": "flow", "key": [7], "domain": "NonNegativeReals", "value": 2}],
            "objective_values": [{"component": "cost", "sense": "min", "unit": "USD", "value": 6}],
        },
        scalar_plan,
        **identity,
    )
    assert point is not None
    assert point["variables"][0]["origin"] == "direct_python"
    assert point["instance_key"] == ["west", 7]
    assert validate_direct_witness_record(point, scalar_plan, **identity) == point


def test_direct_checked_status_requires_full_business_check():
    assert business_check_is_fully_passed({"status": "pass", "violations": [], "uncovered": []})
    assert not business_check_is_fully_passed({"status": "pass", "violations": [], "uncovered": ["index"]})
    assert not business_check_is_fully_passed({"status": "pass", "violations": ["constraint"], "uncovered": []})
    assert not business_check_is_fully_passed({"status": "uncovered", "violations": [], "uncovered": []})


def test_objective_values_use_tolerance_only_after_strict_row_identity_validation():
    model_value = [{"component": "cost", "sense": "min", "unit": "USD", "value": 10.0}]

    within_tolerance = [
        {"component": "cost", "sense": "min", "unit": "USD", "value": 10.00000005}
    ]
    outside_tolerance = [
        {"component": "cost", "sense": "min", "unit": "USD", "value": 10.000001}
    ]
    assert objective_values_match(within_tolerance, model_value) is True
    assert objective_values_match(outside_tolerance, model_value) is False


def test_objective_values_accept_json_serializable_builtin_subclasses():
    class Numeric(float):
        pass

    class Text(str):
        pass

    left = [{"component": Text("cost"), "sense": Text("min"), "unit": Text("USD"), "value": Numeric(3.0)}]
    right = [{"component": "cost", "sense": "min", "unit": "USD", "value": 3.0}]
    assert objective_values_match(left, right) is True


@pytest.mark.parametrize(
    "left,right",
    [
        (
            [{"component": "cost", "sense": "min", "unit": "USD", "value": 1.0}],
            [{"component": "time", "sense": "min", "unit": "USD", "value": 1.0}],
        ),
        (
            [{"component": "a", "sense": "min", "unit": "USD", "value": 1.0},
             {"component": "b", "sense": "min", "unit": "USD", "value": 2.0}],
            [{"component": "b", "sense": "min", "unit": "USD", "value": 2.0},
             {"component": "a", "sense": "min", "unit": "USD", "value": 1.0}],
        ),
        (
            [{"component": "cost", "sense": "min", "unit": "USD", "value": 1.0, "extra": 0}],
            [{"component": "cost", "sense": "min", "unit": "USD", "value": 1.0}],
        ),
        (
            [{"component": "cost", "sense": "min", "unit": "USD", "value": True}],
            [{"component": "cost", "sense": "min", "unit": "USD", "value": 1.0}],
        ),
        (
            [{"component": "cost", "sense": [], "unit": "USD", "value": 1.0}],
            [{"component": "cost", "sense": "min", "unit": "USD", "value": 1.0}],
        ),
        (
            [{"component": "cost", "sense": "min", "unit": "USD", "value": float("inf")}],
            [{"component": "cost", "sense": "min", "unit": "USD", "value": 1.0}],
        ),
        (
            [{"component": "cost", "sense": "min", "unit": "USD", "value": 10**400}],
            [{"component": "cost", "sense": "min", "unit": "USD", "value": 1.0}],
        ),
    ],
)
def test_objective_values_reject_invalid_structure_metadata_and_numbers(left, right):
    with pytest.raises(ContractError):
        objective_values_match(left, right)


def test_objective_values_reject_different_row_counts():
    row = {"component": "cost", "sense": "min", "unit": "USD", "value": 1.0}
    with pytest.raises(ContractError):
        objective_values_match([row], [])


@pytest.mark.parametrize(
    "sense,objective,bound,expected",
    [("min", 102.0, 100.0, 102.0), ("max", 98.0, 100.0, 98.0)],
)
def test_lexicographic_restriction_uses_global_bound_plus_or_minus_tolerance(sense, objective, bound, expected):
    lock = checked_lexicographic_lock(
        bound_value=bound,
        sense=sense,
        objective_value=objective,
        absolute_tolerance=2.0,
        relative_tolerance=0.0,
        stored_absolute_gap=2.0,
    )
    assert lock is not None
    assert lock["restriction_value"] == expected


@pytest.mark.parametrize(
    "bound,objective,absolute,relative,gap",
    [(100.0, 103.0, 2.0, 0.0, 3.0), (100.0, 101.0, 0.0, 0.0, 1.0), (None, 100.0, 2.0, 0.0, 0.0)],
)
def test_lexicographic_lock_stops_without_a_valid_bound_within_declared_tolerance(bound, objective, absolute, relative, gap):
    assert checked_lexicographic_lock(
        bound_value=bound,
        sense="min",
        objective_value=objective,
        absolute_tolerance=absolute,
        relative_tolerance=relative,
        stored_absolute_gap=gap,
    ) is None


def test_invalid_highs_tolerances_fail_before_loading_native_solver():
    with pytest.raises(ContractError) as caught:
        run_highs_once(
            object(), time_limit_seconds=1.0, relative_gap=None, absolute_gap=None,
            threads=1, random_seed=0, feasibility_tolerance=1e-12, integrality_tolerance=1e-7,
        )
    assert caught.value.code == "invalid_argument"
    assert caught.value.pointer == "/solve_options/feasibility_tolerance"


def _scalar_objective(model):
    return objective_for_layer(
        model,
        {
            "mode": "scalar",
            "objective_component": "cost",
            "sense": "min",
            "unit": "USD",
            "preference_note": "minimize the declared cost",
        },
        None,
    )


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("constraint_hit", True),
        ("objective_hit", True),
        ("fixed_only", False),
        ("unreferenced", False),
        ("inactive_parent_var", False),
        ("foreign_same_name", False),
        ("inactive_rows", False),
        ("mixed_fixed_and_unfixed", True),
    ],
)
def test_requires_solver_uses_only_active_unfixed_model_variables(scenario, expected):
    pyo = pytest.importorskip("pyomo.environ")
    model = pyo.ConcreteModel()

    if scenario == "constraint_hit":
        model.x = pyo.Var()
        model.row = pyo.Constraint(expr=model.x >= 0)
    elif scenario == "objective_hit":
        model.x = pyo.Var()
        model.cost = pyo.Objective(expr=model.x)
    elif scenario == "fixed_only":
        model.x = pyo.Var(initialize=1)
        model.x.fix()
        model.row = pyo.Constraint(expr=model.x >= 0)
    elif scenario == "unreferenced":
        model.x = pyo.Var()
    elif scenario == "inactive_parent_var":
        model.subblock = pyo.Block()
        model.subblock.x = pyo.Var()
        model.subblock.deactivate()
        model.row = pyo.Constraint(expr=model.subblock.x >= 0)
    elif scenario == "foreign_same_name":
        foreign = pyo.ConcreteModel()
        foreign.x = pyo.Var()
        model.x = pyo.Var()
        model.row = pyo.Constraint(expr=foreign.x >= 0)
    elif scenario == "inactive_rows":
        model.x = pyo.Var()
        model.row = pyo.Constraint(expr=model.x >= 0)
        model.cost = pyo.Objective(expr=model.x)
        model.row.deactivate()
        model.cost.deactivate()
    elif scenario == "mixed_fixed_and_unfixed":
        model.fixed_x = pyo.Var(initialize=1)
        model.fixed_x.fix()
        model.free_x = pyo.Var()
        model.row = pyo.Constraint(expr=model.fixed_x + model.free_x >= 0)
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(f"unknown scenario: {scenario}")

    assert _requires_solver(model) is expected


def test_active_rows_use_raw_primals_not_stale_var_initial_values():
    pyo = pytest.importorskip("pyomo.environ")
    from pyomo.common.collections import ComponentMap

    model = pyo.ConcreteModel()
    model.x = pyo.Var(bounds=(0, 10), initialize=9)
    model.lower = pyo.Constraint(expr=model.x >= 2)
    model.upper = pyo.Constraint(expr=model.x <= 4)
    model.cost = pyo.Objective(expr=model.x, sense=pyo.minimize)
    objective = _scalar_objective(model)
    assert _requires_solver(model)
    failures = []

    checked = check_pyomo_model(
        model,
        ComponentMap([(model.x, 5.0)]),
        objective,
        input_digest="a" * 64,
        instance_key=[],
        instance_binding_digest="b" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
        violation_sink=failures.append,
    )

    assert checked.record["status"] == "fail"
    assert checked.record["variable_coverage"] == {"required": 1, "present": 1, "completed": 0, "missing_count": 0}
    assert checked.record["component_coverage"] == [
        {"component": "lower", "required": 1, "checked": 1, "failures": 0, "uncovered": 0, "max_violation": 0.0, "max_violation_key": []},
        {"component": "upper", "required": 1, "checked": 1, "failures": 1, "uncovered": 0, "max_violation": 1.0, "max_violation_key": []},
    ]
    assert failures == [{
        "kind": "constraint", "component": "upper", "key": [], "lower": None,
        "body": 5.0, "upper": 4.0, "violation": 1.0, "error": "constraint_violation",
    }]
    assert checked.witness_variables is not None
    assert checked.candidate_complete is False
    assert model.x.value == 9


@pytest.mark.parametrize(
    "domain,raw,expected",
    [
        ("integer", 2.0 + 5e-8, 2.0),
        ("binary", 1.0 - 5e-8, 1.0),
    ],
)
def test_near_integral_solver_primals_are_checked_as_rounded_copies(domain, raw, expected):
    pyo = pytest.importorskip("pyomo.environ")
    from pyomo.common.collections import ComponentMap

    model = pyo.ConcreteModel()
    variable_domain = pyo.Integers if domain == "integer" else pyo.Binary
    model.x = pyo.Var(domain=variable_domain, bounds=(0, 5), initialize=0.25)
    model.cost = pyo.Objective(expr=model.x, sense=pyo.minimize)
    primals = ComponentMap([(model.x, raw)])

    checked = check_pyomo_model(
        model,
        primals,
        _scalar_objective(model),
        input_digest="a" * 64,
        instance_key=[],
        instance_binding_digest="b" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
    )

    assert checked.record["status"] == "pass"
    assert checked.candidate_complete is True
    assert checked.witness_variables == ({
        "component": "x",
        "key": [],
        "domain": domain,
        "value": expected,
        "origin": "solver_primal",
    },)
    assert checked.objective_values[0]["value"] == expected
    assert primals[model.x] == raw
    assert model.x.value == 0.25


def test_integer_solver_primal_outside_integrality_tolerance_is_not_rounded():
    pyo = pytest.importorskip("pyomo.environ")
    from pyomo.common.collections import ComponentMap

    model = pyo.ConcreteModel()
    model.x = pyo.Var(domain=pyo.Integers, bounds=(0, 5), initialize=3)
    model.cost = pyo.Objective(expr=model.x, sense=pyo.minimize)
    raw = 2.0 + 1.5e-7
    primals = ComponentMap([(model.x, raw)])

    checked = check_pyomo_model(
        model,
        primals,
        _scalar_objective(model),
        input_digest="a" * 64,
        instance_key=[],
        instance_binding_digest="b" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
    )

    assert checked.record["status"] == "fail"
    assert checked.record["variable_coverage"]["present"] == 1
    assert checked.candidate_complete is False
    assert checked.witness_variables is None
    assert checked.objective_values[0]["value"] == raw
    assert primals[model.x] == raw
    assert model.x.value == 3


@pytest.mark.parametrize(
    "sense,limit,expected_status,expected_violation",
    [
        ("lower", 1000.0, "pass", 0.0),
        ("upper", 999999.99, "fail", 0.01),
    ],
)
def test_rows_are_checked_once_on_the_rounded_point(
    sense, limit, expected_status, expected_violation
):
    pyo = pytest.importorskip("pyomo.environ")
    from pyomo.common.collections import ComponentMap

    model = pyo.ConcreteModel()
    model.x = pyo.Var(domain=pyo.Integers, bounds=(0, 2), initialize=0)
    if sense == "lower":
        model.row = pyo.Constraint(expr=1000.0 * model.x >= limit)
    else:
        model.row = pyo.Constraint(expr=1_000_000.0 * model.x <= limit)
    model.cost = pyo.Objective(expr=model.x, sense=pyo.minimize)
    raw = 1.0 - 5e-7
    primals = ComponentMap([(model.x, raw)])
    violations = []

    checked = check_pyomo_model(
        model,
        primals,
        _scalar_objective(model),
        input_digest="a" * 64,
        instance_key=[],
        instance_binding_digest="b" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-6,
        violation_sink=violations.append,
    )

    assert checked.record["status"] == expected_status
    assert checked.objective_values[0]["value"] == 1.0
    assert checked.witness_variables[0]["value"] == 1.0
    assert checked.witness_variables[0]["origin"] == "solver_primal"
    assert checked.candidate_complete is (expected_status == "pass")
    assert primals[model.x] == raw
    assert model.x.value == 0
    assert checked.record["component_coverage"][0]["max_violation"] == pytest.approx(
        expected_violation
    )
    if expected_status == "fail":
        assert any(
            item["kind"] == "constraint"
            and item["body"] == 1_000_000.0
            and item["error"] == "constraint_violation"
            for item in violations
        )


def test_bounds_are_checked_on_the_rounded_point_without_raw_fallback():
    pyo = pytest.importorskip("pyomo.environ")
    from pyomo.common.collections import ComponentMap

    model = pyo.ConcreteModel()
    model.x = pyo.Var(domain=pyo.Integers, bounds=(0, 0.9999995), initialize=0)
    model.cost = pyo.Objective(expr=model.x, sense=pyo.minimize)
    raw = 0.9999995
    primals = ComponentMap([(model.x, raw)])
    violations = []

    checked = check_pyomo_model(
        model,
        primals,
        _scalar_objective(model),
        input_digest="a" * 64,
        instance_key=[],
        instance_binding_digest="b" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-6,
        violation_sink=violations.append,
    )

    assert checked.record["status"] == "fail"
    assert checked.candidate_complete is False
    assert checked.objective_values[0]["value"] == 1.0
    assert primals[model.x] == raw
    assert violations == [{
        "kind": "variable",
        "component": "x",
        "key": [],
        "lower": 0.0,
        "body": 1.0,
        "upper": 0.9999995,
        "violation": None,
        "error": "upper_bound_violation",
    }]


def test_checked_objective_recomputes_gap_after_integrality_normalization():
    pyo = pytest.importorskip("pyomo.environ")
    from pyomo.common.collections import ComponentMap

    model = pyo.ConcreteModel()
    model.x = pyo.Var(domain=pyo.Integers, bounds=(0, 5), initialize=0)
    model.cost = pyo.Objective(expr=model.x, sense=pyo.minimize)
    raw = 1.0 - 5e-9
    primals = ComponentMap([(model.x, raw)])
    objective = _scalar_objective(model)

    checked = check_pyomo_model(
        model,
        primals,
        objective,
        input_digest="a" * 64,
        instance_key=[],
        instance_binding_digest="b" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
    )
    observation = HighsObservation(
        record={"raw_solution_value_valid": True},
        backend={"id": "highs", "version": "test"},
        termination="time_limit",
        raw_status="kTimeLimit",
        solver_invoked=True,
        solver_returned=True,
        primals=primals,
        solver_incumbent_objective=1.0,
        bound_value=0.8,
        nonfinite_fields={},
        solve_wall_seconds=0.1,
        error=None,
    )

    result = result_record(
        observation,
        checked,
        objective,
        instance_key=[],
        instance_binding_digest="b" * 64,
        source_revision=1,
        candidate_revision=1,
        input_digest="a" * 64,
        layer=None,
        bound_evidence_ref={
            "path": "backend-observation.json",
            "sha256": "c" * 64,
            "byte_len": 2,
            "media_type": "application/json",
        },
    )

    assert checked.objective_values[0]["value"] == 1.0
    assert result["has_solver_incumbent"] is True
    assert result["objective"]["value"] == 1.0
    assert result["bound"]["value"] == 0.8
    assert result["gap"]["absolute"] == pytest.approx(0.2)
    assert result["gap"]["relative"] == pytest.approx(0.2)
    assert primals[model.x] == raw


def test_fixed_continuous_and_deterministic_values_are_not_integrality_normalized():
    pyo = pytest.importorskip("pyomo.environ")
    from pyomo.common.collections import ComponentMap

    model = pyo.ConcreteModel()
    model.continuous = pyo.Var(domain=pyo.Reals, bounds=(0, 5), initialize=3)
    model.fixed = pyo.Var(domain=pyo.Integers, bounds=(0, 5), initialize=2)
    model.fixed.fix(2)
    model.unused = pyo.Var(domain=pyo.Integers, bounds=(0, 5))
    model.cost = pyo.Objective(expr=model.continuous + model.fixed, sense=pyo.minimize)
    continuous_raw = 1.0 + 5e-8
    primals = ComponentMap([
        (model.continuous, continuous_raw),
        (model.fixed, 4.0),
    ])

    checked = check_pyomo_model(
        model,
        primals,
        _scalar_objective(model),
        input_digest="a" * 64,
        instance_key=[],
        instance_binding_digest="b" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
    )

    rows = {item["component"]: item for item in checked.witness_variables}
    assert checked.record["status"] == "pass"
    assert rows["continuous"]["value"] == continuous_raw
    assert rows["continuous"]["origin"] == "solver_primal"
    assert rows["fixed"]["value"] == 2.0
    assert rows["fixed"]["origin"] == "fixed_model_value"
    assert rows["unused"]["value"] == 0.0
    assert rows["unused"]["origin"] == "deterministic_completion"
    assert primals[model.continuous] == continuous_raw
    assert primals[model.fixed] == 4.0


def test_actual_variable_bound_violation_still_reaches_violation_sink(monkeypatch):
    _install_pyomo_logic_stubs(monkeypatch)
    import optagent_core.results as results

    x = _StubVar("x", 9, bounds=(0, 4))
    objective_expr = _StubExpr((x,))
    model = _StubModel([x], [], [_StubObjective("cost", objective_expr)])
    monkeypatch.setattr(results, "_value", lambda expr: expr.evaluate() if isinstance(expr, _StubExpr) else expr)
    violations = []

    checked = results.check_pyomo_model(
        model,
        _IdentityMap([(x, 5.0)]),
        ObjectiveSpec("cost", "min", "USD", objective_expr, None),
        input_digest="a" * 64,
        instance_key=[],
        instance_binding_digest="b" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
        violation_sink=violations.append,
    )

    assert checked.record["status"] == "fail"
    assert violations == [{
        "kind": "variable",
        "component": "x",
        "key": [],
        "lower": 0.0,
        "body": 5.0,
        "upper": 4.0,
        "violation": None,
        "error": "upper_bound_violation",
    }]


def test_missing_active_primal_never_uses_initialized_value_as_solution():
    pyo = pytest.importorskip("pyomo.environ")
    model = pyo.ConcreteModel()
    model.x = pyo.Var(bounds=(0, 10), initialize=3)
    model.required = pyo.Constraint(expr=model.x >= 2)
    model.cost = pyo.Objective(expr=model.x, sense=pyo.minimize)

    checked = check_pyomo_model(
        model,
        {},
        _scalar_objective(model),
        input_digest="a" * 64,
        instance_key=[],
        instance_binding_digest="b" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
    )

    assert checked.record["status"] == "uncovered"
    assert checked.record["variable_coverage"]["missing_count"] == 1
    assert checked.witness_variables is None
    assert model.x.value == 3


def test_fixed_model_value_is_not_replaced_by_a_raw_primal():
    pyo = pytest.importorskip("pyomo.environ")
    from pyomo.common.collections import ComponentMap

    model = pyo.ConcreteModel()
    model.x = pyo.Var(bounds=(0, 10), initialize=3)
    model.x.fix(3)
    model.cost = pyo.Objective(expr=model.x, sense=pyo.minimize)

    checked = check_pyomo_model(
        model,
        ComponentMap([(model.x, 8.0)]),
        _scalar_objective(model),
        input_digest="a" * 64,
        instance_key=[],
        instance_binding_digest="b" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
    )

    assert checked.record["status"] == "pass"
    assert checked.witness_variables[0]["value"] == 3.0
    assert checked.witness_variables[0]["origin"] == "fixed_model_value"


def test_result_bound_is_global_only_when_a_finite_backend_bound_was_observed():
    checked = CheckedModel(
        record={"status": "pass", "uncovered": []},
        witness_variables=({"component": "x", "key": [], "value": 5.0},),
        objective_values=({"component": "cost", "sense": "min", "unit": "USD", "value": 5.0},),
        candidate_complete=True,
        raw_vector_complete=True,
    )
    objective = ObjectiveSpec("cost", "min", "USD", None, None)
    evidence = {"path": ".reaslab/optagent/cases/c/runs/r/output/layer/backend-observation.json", "sha256": "c" * 64, "byte_len": 20, "media_type": "application/json"}
    observation = HighsObservation(
        record={"raw_solution_value_valid": True},
        backend={"id": "highs", "version": "1.10.0"},
        termination="optimal",
        raw_status="kOptimal",
        solver_invoked=True,
        solver_returned=True,
        primals={},
        solver_incumbent_objective=5.0,
        bound_value=4.0,
        nonfinite_fields={},
        solve_wall_seconds=0.2,
        error=None,
    )

    result = result_record(
        observation,
        checked,
        objective,
        instance_key=[],
        instance_binding_digest="b" * 64,
        source_revision=1,
        candidate_revision=1,
        input_digest="a" * 64,
        layer=None,
        bound_evidence_ref=evidence,
    )
    assert result["bound"] == {"value": 4.0, "direction": "lower", "scope": "global", "evidence": evidence}
    assert result["gap"]["absolute"] == 1.0
    assert result["gap"]["relative"] == 0.2

    unbounded = HighsObservation(
        record={"raw_solution_value_valid": True},
        backend={"id": "highs", "version": "1.10.0"},
        termination="unbounded",
        raw_status="kUnbounded",
        solver_invoked=True,
        solver_returned=True,
        primals={},
        solver_incumbent_objective=None,
        bound_value=None,
        nonfinite_fields={},
        solve_wall_seconds=0.2,
        error=None,
    )
    unbounded_result = result_record(
        unbounded,
        checked,
        objective,
        instance_key=[],
        instance_binding_digest="b" * 64,
        source_revision=1,
        candidate_revision=1,
        input_digest="a" * 64,
        layer=None,
        bound_evidence_ref=evidence,
    )
    assert unbounded_result["bound"] is None
    assert unbounded_result["gap"]["absolute"] is None


@pytest.mark.parametrize(
    ("sense", "bound_value"),
    [
        ("min", 6.0),
        ("max", 4.0),
        ("min", math.nextafter(5.0, math.inf)),
        ("min", 5.0 + 1e-12),
    ],
)
def test_direction_conflicting_global_bound_is_uncovered_without_erasing_candidate(sense, bound_value):
    checked = CheckedModel(
        record={"status": "pass", "uncovered": []},
        witness_variables=({"component": "x", "key": [], "value": 5.0},),
        objective_values=({"component": "cost", "sense": sense, "unit": "USD", "value": 5.0},),
        candidate_complete=True,
        raw_vector_complete=True,
    )
    objective = ObjectiveSpec("cost", sense, "USD", None, None)
    evidence = {"path": "backend.json", "sha256": "c" * 64, "byte_len": 20, "media_type": "application/json"}
    observation = HighsObservation(
        record={"raw_solution_value_valid": True, "best_objective_bound": bound_value},
        backend={"id": "highs", "version": "1.10.0"},
        termination="time_limit",
        raw_status="kTimeLimit",
        solver_invoked=True,
        solver_returned=True,
        primals={},
        solver_incumbent_objective=5.0,
        bound_value=bound_value,
        nonfinite_fields={},
        solve_wall_seconds=0.2,
        error=None,
    )

    result = result_record(
        observation,
        checked,
        objective,
        instance_key=[],
        instance_binding_digest="b" * 64,
        source_revision=1,
        candidate_revision=1,
        input_digest="a" * 64,
        layer=None,
        bound_evidence_ref=evidence,
    )

    assert observation.record["best_objective_bound"] == bound_value
    assert result["objective"]["value"] == 5.0
    assert result["has_checked_candidate"] is True
    assert result["has_solver_incumbent"] is True
    assert result["bound"] is None
    assert result["gap"]["absolute"] is None
    assert result["gap"]["relative"] is None
    assert "global_bound_uncovered:direction_conflicts_with_checked_objective" in result["coverage"]
    assert "global_bound_conflicts_with_checked_feasible_objective" in result["warnings"]


@pytest.mark.parametrize(
    ("sense", "checked_objective"),
    [
        ("min", 5.0 - 2e-9),
        ("max", 5.0 + 2e-9),
    ],
)
def test_result_record_keeps_conflicting_raw_bound_strict_and_qualifies_recompute_residual(sense, checked_objective):
    checked = CheckedModel(
        record={"status": "pass", "uncovered": []},
        witness_variables=({"component": "x", "key": [], "value": checked_objective},),
        objective_values=({"component": "cost", "sense": sense, "unit": "USD", "value": checked_objective},),
        candidate_complete=True,
        raw_vector_complete=True,
    )
    evidence = {"path": "backend.json", "sha256": "d" * 64, "byte_len": 20, "media_type": "application/json"}
    native_record = _native_observation_record(incumbent=5.0, bound=5.0, sense=sense)
    observation = HighsObservation(
        record=native_record,
        backend={"id": "highs", "version": "1.10.0"},
        termination="time_limit",
        raw_status="kTimeLimit",
        solver_invoked=True,
        solver_returned=True,
        primals={},
        solver_incumbent_objective=5.0,
        bound_value=5.0,
        nonfinite_fields={},
        solve_wall_seconds=0.2,
        error=None,
    )

    result = result_record(
        observation,
        checked,
        ObjectiveSpec("cost", sense, "USD", None, None),
        instance_key=[],
        instance_binding_digest="b" * 64,
        source_revision=1,
        candidate_revision=1,
        input_digest="a" * 64,
        layer=None,
        bound_evidence_ref=evidence,
        core_gap_target={"relative_gap": 0.0, "absolute_gap": 1e-8},
    )

    closure = result["numerical"]["optimality"]
    assert result["bound"] is None
    assert result["gap"]["absolute"] is None
    assert result["gap"]["relative"] is None
    assert observation.bound_value == 5.0
    assert observation.record["best_objective_bound"] == 5.0
    assert closure["status"] == "closed"
    assert closure["qualification"] == "objective_recomputation_residual"
    assert closure["global_bound"] is None
    assert closure["native_reported_bound"] == 5.0
    assert closure["native_gap"] == 0.0
    assert closure["objective_recomputation_residual"] == pytest.approx(2e-9)
    assert closure["closure_residual"] == pytest.approx(2e-9)
    assert checked_lexicographic_lock(
        bound_value=None,
        sense=sense,
        objective_value=checked_objective,
        absolute_tolerance=1e-8,
        relative_tolerance=0.0,
        stored_absolute_gap=None,
    ) is None


@pytest.mark.parametrize(
    ("sense", "raw_bound"),
    [
        ("min", math.nextafter(math.nextafter(5.0, math.inf), math.inf)),
        ("max", math.nextafter(math.nextafter(5.0, -math.inf), -math.inf)),
        ("min", 5.0 + 1e-12),
    ],
)
def test_result_record_keeps_strict_direction_for_any_conflicting_bound(sense, raw_bound):
    checked = CheckedModel(
        record={"status": "pass", "uncovered": []},
        witness_variables=({"component": "x", "key": [], "value": 5.0},),
        objective_values=({"component": "cost", "sense": sense, "unit": "USD", "value": 5.0},),
        candidate_complete=True,
        raw_vector_complete=True,
    )
    observation = HighsObservation(
        record=_native_observation_record(incumbent=5.0, bound=raw_bound, sense=sense),
        backend={"id": "highs", "version": "1.10.0"},
        termination="time_limit",
        raw_status="kTimeLimit",
        solver_invoked=True,
        solver_returned=True,
        primals={},
        solver_incumbent_objective=5.0,
        bound_value=raw_bound,
        nonfinite_fields={},
        solve_wall_seconds=0.2,
        error=None,
    )

    result = result_record(
        observation,
        checked,
        ObjectiveSpec("cost", sense, "USD", None, None),
        instance_key=[],
        instance_binding_digest="b" * 64,
        source_revision=1,
        candidate_revision=1,
        input_digest="a" * 64,
        layer=None,
        bound_evidence_ref={"path": "backend.json", "sha256": "c" * 64, "byte_len": 20, "media_type": "application/json"},
    )

    assert result["bound"] is None
    assert result["gap"]["absolute"] is None
    assert "global_bound_uncovered:direction_conflicts_with_checked_objective" in result["coverage"]


def test_result_record_keeps_raw_bound_when_no_complete_checked_point_exists():
    raw_bound = math.nextafter(5.0, math.inf)
    checked = CheckedModel(
        record={"status": "uncovered", "uncovered": ["incomplete"]},
        witness_variables=(),
        objective_values=({"component": "cost", "sense": "min", "unit": "USD", "value": 5.0},),
        candidate_complete=False,
        raw_vector_complete=False,
    )
    observation = HighsObservation(
        record=_native_observation_record(
            incumbent=None,
            bound=raw_bound,
            raw_solution_value_valid=False,
        ),
        backend={"id": "highs", "version": "1.10.0"},
        termination="time_limit",
        raw_status="kTimeLimit",
        solver_invoked=True,
        solver_returned=True,
        primals={},
        solver_incumbent_objective=None,
        bound_value=raw_bound,
        nonfinite_fields={},
        solve_wall_seconds=0.2,
        error=None,
    )

    result = result_record(
        observation,
        checked,
        ObjectiveSpec("cost", "min", "USD", None, None),
        instance_key=[],
        instance_binding_digest="b" * 64,
        source_revision=1,
        candidate_revision=1,
        input_digest="a" * 64,
        layer=None,
        bound_evidence_ref={"path": "backend.json", "sha256": "c" * 64, "byte_len": 20, "media_type": "application/json"},
    )

    assert result["bound"]["value"] == raw_bound
    assert result["gap"]["absolute"] is None
    assert result["numerical"]["optimality"]["status"] == "unknown"


def test_result_record_does_not_normalize_without_existing_bound_evidence():
    raw_bound = math.nextafter(5.0, math.inf)
    checked = CheckedModel(
        record={"status": "pass", "uncovered": []},
        witness_variables=({"component": "x", "key": [], "value": 5.0},),
        objective_values=({"component": "cost", "sense": "min", "unit": "USD", "value": 5.0},),
        candidate_complete=True,
        raw_vector_complete=True,
    )
    observation = HighsObservation(
        record={"raw_solution_value_valid": True, "best_objective_bound": raw_bound},
        backend={"id": "highs", "version": "1.10.0"},
        termination="time_limit",
        raw_status="kTimeLimit",
        solver_invoked=True,
        solver_returned=True,
        primals={},
        solver_incumbent_objective=raw_bound,
        bound_value=raw_bound,
        nonfinite_fields={},
        solve_wall_seconds=0.2,
        error=None,
    )

    result = result_record(
        observation,
        checked,
        ObjectiveSpec("cost", "min", "USD", None, None),
        instance_key=[],
        instance_binding_digest="b" * 64,
        source_revision=1,
        candidate_revision=1,
        input_digest="a" * 64,
        layer=None,
        bound_evidence_ref=None,
    )

    assert result["bound"] is None
    assert result["gap"]["absolute"] is None


def test_bound_gap_helper_uses_strict_direction_and_preserves_legal_gap():
    from optagent_core.results import checked_global_bound_gap

    assert checked_global_bound_gap(4.0, "min", 5.0) == (1.0, 0.2)
    assert checked_global_bound_gap(6.0, "min", 5.0) is None
    assert checked_global_bound_gap(6.0, "max", 5.0) == (1.0, 0.2)
    assert checked_global_bound_gap(4.0, "max", 5.0) is None
    assert checked_global_bound_gap(5.0 + 1e-12, "min", 5.0) is None
    assert checked_global_bound_gap(-1e308, "min", 1e308) == (None, None)


def test_native_numerical_closure_zero_targets_identity_and_actual_residual_boundary():
    exact_zero = _summary_with_native_values(
        checked_objective=0.0,
        incumbent=0.0,
        native_bound=0.0,
        absolute_gap=0.0,
        relative_gap=0.0,
    )["optimality"]
    assert exact_zero["status"] == "closed"
    assert exact_zero["qualification"] == "strict_direction"
    assert exact_zero["allowed_gap"] == 0.0
    assert exact_zero["objective_recomputation_residual"] == 0.0
    assert exact_zero["closure_residual"] == 0.0

    zero_target_with_residual = _summary_with_native_values(
        checked_objective=0.0,
        incumbent=5e-9,
        native_bound=5e-9,
        absolute_gap=0.0,
        relative_gap=0.0,
    )["optimality"]
    assert zero_target_with_residual["status"] == "not_closed"
    assert zero_target_with_residual["allowed_gap"] == 0.0
    assert zero_target_with_residual["objective_recomputation_residual"] == pytest.approx(5e-9)
    assert zero_target_with_residual["closure_residual"] == pytest.approx(5e-9)

    represented_residual = abs((5.0 + 2e-9) - 5.0)
    at_target = _summary_with_native_values(
        checked_objective=5.0,
        incumbent=5.0 + 2e-9,
        native_bound=5.0 + 2e-9,
        absolute_gap=represented_residual,
        relative_gap=0.0,
    )["optimality"]
    assert at_target["status"] == "closed"
    assert at_target["closure_residual"] == pytest.approx(at_target["allowed_gap"])

    below_required_precision = _summary_with_native_values(
        checked_objective=5.0,
        incumbent=5.0 + 2e-9,
        native_bound=5.0 + 2e-9,
        absolute_gap=1e-9,
        relative_gap=0.0,
    )["optimality"]
    assert below_required_precision["status"] == "not_closed"


@pytest.mark.parametrize(
    ("damage", "expected_qualification"),
    [
        ("missing_native_incumbent", "not_qualified"),
        ("raw_solution_invalid", "not_qualified"),
        ("local_scope", "not_qualified"),
        ("wrong_sense", "not_qualified"),
        ("wrong_direction_metadata", "not_qualified"),
        ("missing_transform", "not_qualified"),
        ("raw_value_nonfinite", "not_qualified"),
        ("native_direction_conflict", "not_qualified_native_direction"),
        ("checked_point_failed", "not_qualified"),
        ("incumbent_identity_mismatch", "not_qualified_objective_identity"),
        ("native_termination_mismatch", "not_qualified"),
    ],
)
def test_native_numerical_closure_requires_all_same_point_and_bound_evidence_gates(damage, expected_qualification):
    kwargs = {
        "checked_objective": 100.0,
        "incumbent": 100.0,
        "native_bound": 100.0,
        "absolute_gap": 1e-6,
        "relative_gap": 0.0,
    }
    if damage == "missing_native_incumbent":
        kwargs["incumbent"] = None
    elif damage == "raw_solution_invalid":
        kwargs["raw_solution_value_valid"] = False
    elif damage == "local_scope":
        kwargs["evidence_update"] = {"scope": "local"}
    elif damage == "wrong_sense":
        kwargs["evidence_update"] = {"objective_sense": "max"}
    elif damage == "wrong_direction_metadata":
        kwargs["evidence_update"] = {"value_direction": "upper"}
    elif damage == "missing_transform":
        kwargs["evidence_update"] = {"objective_transform": None}
    elif damage == "raw_value_nonfinite":
        kwargs["evidence_update"] = {"raw_value": float("nan")}
    elif damage == "native_direction_conflict":
        kwargs["native_bound"] = 101.0
    elif damage == "checked_point_failed":
        kwargs["checked_status"] = "uncovered"
    elif damage == "incumbent_identity_mismatch":
        kwargs["incumbent"] = 100.001
    elif damage == "native_termination_mismatch":
        kwargs["native_termination"] = "optimal"

    optimality = _summary_with_native_values(**kwargs)["optimality"]
    assert optimality["status"] == "not_closed"
    assert optimality["qualification"] == expected_qualification


def test_transformed_native_bound_uses_original_units_without_changing_strict_result_fields():
    checked_value = 100.0 + 2e-9
    evidence = {
        "source": "CVXPY COPT extra_stats.BestBnd",
        "scope": "global",
        "objective_sense": "max",
        "value_direction": "upper",
        "interface": "cvxpy_copt",
        "objective_transform": {
            "kind": "sign_and_constant_inverse",
            "canonical_sense_sign": -1,
            "external_constant": 13.0,
            "inverse_offset": 0.0,
            "formula": "original_bound = external_constant + canonical_sense_sign * canonical_min_bound",
        },
        # COPT reports the canonical minimization bound: 13 + (-1) * (-87) = 100.
        "raw_value": -87.0,
    }
    observation = HighsObservation(
        record={
            "termination": "time_limit",
            "raw_solution_value_valid": True,
            "best_feasible_objective": 100.0,
            "best_objective_bound": 100.0,
            "bound_evidence": evidence,
            "requested_options": {},
            "effective_options": {},
        },
        backend={"id": "cvxpy_copt", "version": "test-fixture"},
        termination="time_limit",
        raw_status="time_limit",
        solver_invoked=True,
        solver_returned=True,
        primals={},
        solver_incumbent_objective=100.0,
        bound_value=100.0,
        nonfinite_fields={},
        solve_wall_seconds=0.1,
        error=None,
    )
    checked = CheckedModel(
        record={"status": "pass", "uncovered": [], "tolerances": {}},
        witness_variables=({"component": "x", "key": [], "value": 1.0},),
        objective_values=({"component": "profit", "sense": "max", "unit": "USD", "value": checked_value},),
        candidate_complete=True,
        raw_vector_complete=True,
    )

    result = result_record(
        observation,
        checked,
        ObjectiveSpec("profit", "max", "USD", None, None),
        instance_key=[],
        instance_binding_digest="b" * 64,
        source_revision=1,
        candidate_revision=1,
        input_digest="a" * 64,
        layer=None,
        bound_evidence_ref={"path": "backend-observation.json", "sha256": "c" * 64, "byte_len": 20, "media_type": "application/json"},
        core_gap_target={"absolute_gap": 2e-9, "relative_gap": 0.0},
    )

    # The raw canonical value stays in the sealed observation; the converted
    # original-unit value drives closure, while strict Result fields stay strict.
    assert observation.record["best_objective_bound"] == 100.0
    assert observation.record["bound_evidence"]["raw_value"] == -87.0
    assert result["bound"] is None
    assert result["gap"]["absolute"] is None
    assert result["gap"]["relative"] is None
    optimality = result["numerical"]["optimality"]
    assert optimality["status"] == "closed"
    assert optimality["qualification"] == "objective_recomputation_residual"
    assert optimality["native_reported_bound"] == 100.0
    assert optimality["native_gap"] == 0.0
    assert optimality["objective_recomputation_residual"] == pytest.approx(2e-9)
    assert optimality["closure_residual"] == pytest.approx(2e-9)


def test_native_numerical_closure_rejects_nonfinite_values_and_overflowed_residual_sum():
    nonfinite_native = _summary_with_native_values(
        checked_objective=5.0,
        incumbent=float("nan"),
        native_bound=5.0,
        absolute_gap=1.0,
        relative_gap=0.0,
    )["optimality"]
    assert nonfinite_native["status"] == "not_closed"
    assert nonfinite_native["native_gap"] is None
    assert nonfinite_native["closure_residual"] is None

    incumbent = 1e308
    checked_objective = math.nextafter(incumbent, math.inf)
    native_bound = incumbent - sys.float_info.max
    assert math.isclose(incumbent, checked_objective, rel_tol=1e-8, abs_tol=1e-8)
    overflowed = _summary_with_native_values(
        checked_objective=checked_objective,
        incumbent=incumbent,
        native_bound=native_bound,
        absolute_gap=sys.float_info.max,
        relative_gap=0.0,
    )["optimality"]
    assert overflowed["native_gap"] == sys.float_info.max
    assert overflowed["objective_recomputation_residual"] > 0.0
    assert overflowed["closure_residual"] is None
    assert overflowed["qualification"] == "not_qualified_nonfinite_residual"
    assert overflowed["status"] == "not_closed"


def test_numerical_closure_uses_real_gap_for_supported_stop_states_and_rejects_negative_effective_tolerances():
    from optagent_core.results import _result_numerical_summary

    def summarize(termination, absolute=2.0, relative=0.0):
        return _result_numerical_summary(
            check_record={"status": "pass", "tolerances": {"feasibility": 1e-7}},
            objective_value=100.0,
            objective_sense="min",
            bound={"value": 99.0, "direction": "lower", "scope": "global"},
            absolute_gap=1.0,
            termination=termination,
            has_incumbent=True,
            requested_options={"mip_abs_gap": None, "mip_rel_gap": None},
            effective_options={"mip_abs_gap": absolute, "mip_rel_gap": relative},
            native_observation=_native_observation_record(termination=termination),
        )

    for termination in ("optimal", "time_limit", "iteration_limit", "feasible"):
        assert summarize(termination)["optimality"]["status"] == "closed"

    for termination in ("error", "numeric_error", "unknown", "infeasible", "unbounded"):
        assert summarize(termination)["optimality"]["status"] == "not_closed"

    negative_absolute = summarize("optimal", absolute=-1.0)
    negative_relative = summarize("time_limit", relative=-0.01)
    assert negative_absolute["optimality"]["status"] == "unknown"
    assert negative_absolute["optimality"]["allowed_gap"] is None
    assert negative_absolute["effective_options"]["mip_abs_gap"] == -1.0
    assert negative_relative["optimality"]["status"] == "unknown"
    assert negative_relative["optimality"]["allowed_gap"] is None
    assert negative_relative["effective_options"]["mip_rel_gap"] == -0.01


def test_solver_invoked_tracks_the_actual_adapter_call_even_when_it_raises(monkeypatch):
    highs_ok = "kOk"

    class FakeRawHighs:
        def __init__(self):
            self.options = {}

        def setOptionValue(self, name, value):
            self.options[name] = value
            return highs_ok

        def getOptionValue(self, name):
            return highs_ok, self.options.get(name)

        def getModelStatus(self):
            return "kNotset"

        def getSolution(self):
            return SimpleNamespace(value_valid=False)

    class FakeHighs:
        def __init__(self):
            self.config = SimpleNamespace(load_solution=None, time_limit=None, mip_gap=None)
            self.highs_options = {}

        def set_instance(self, model):
            self._solver_model = FakeRawHighs()

        def solve(self, model):
            raise RuntimeError("adapter rejected the model before returning a result")

        def get_primals(self):
            raise RuntimeError("no primal values")

    modules = {}
    for name in ("pyomo", "pyomo.contrib", "pyomo.contrib.appsi", "pyomo.contrib.appsi.solvers"):
        module = ModuleType(name)
        module.__path__ = []
        modules[name] = module
    modules["pyomo.contrib.appsi.solvers"].Highs = FakeHighs
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    highspy = ModuleType("highspy")
    highspy.HighsStatus = SimpleNamespace(kOk=highs_ok)
    monkeypatch.setitem(sys.modules, "highspy", highspy)
    monkeypatch.setattr("optagent_core.results._require_supported_platform", lambda: {"os": "Linux"})
    monkeypatch.setattr("optagent_core.results._backend_versions", lambda: {"id": "highs", "version": "test"})

    observed = run_highs_once(
        object(),
        time_limit_seconds=1.0,
        relative_gap=None,
        absolute_gap=None,
        threads=1,
        random_seed=1,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
    )

    assert observed.solver_invoked is True
    assert observed.solver_returned is False
    assert observed.record["solver_invoked"] is True
    assert observed.record["raw_status"] == "kNotset"


def test_run_highs_once_rejects_unsupported_profile_before_native_import(monkeypatch):
    import builtins
    import optagent_core.results as results

    monkeypatch.setattr(results.platform, "system", lambda: "Windows")
    attempted_native_imports = []
    real_import = builtins.__import__

    def import_sentinel(name, *args, **kwargs):
        if name.split(".", 1)[0] in {"numpy", "pyomo", "highspy"}:
            attempted_native_imports.append(name)
            raise AssertionError(f"native package import preceded platform gate: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_sentinel)
    with pytest.raises(ContractError) as error:
        run_highs_once(
            object(),
            time_limit_seconds=1.0,
            relative_gap=None,
            absolute_gap=None,
            threads=1,
            random_seed=1,
            feasibility_tolerance=1e-7,
            integrality_tolerance=1e-7,
        )

    assert error.value.code == "capability_uncovered"
    assert attempted_native_imports == []


class _IdentitySet:
    def __init__(self, values=()):
        self._values = {}
        self.update(values)

    def update(self, values):
        for value in values:
            self._values[id(value)] = value

    def __contains__(self, value):
        return self._values.get(id(value)) is value

    def __iter__(self):
        return iter(self._values.values())


class _IdentityMap:
    def __init__(self, values=()):
        self._values = {}
        for key, value in values:
            self[key] = value

    def __getitem__(self, key):
        item = self._values[id(key)]
        if item[0] is not key:
            raise KeyError(key)
        return item[1]

    def __setitem__(self, key, value):
        self._values[id(key)] = (key, value)

    def __iter__(self):
        return (item[0] for item in self._values.values())

    def __len__(self):
        return len(self._values)

    def items(self):
        return ((key, value) for key, value in self._values.values())


class _StubVar:
    __hash__ = None

    def __init__(self, name, value, bounds=(0, 10)):
        self.name = name
        self.value = value
        self.lb, self.ub = bounds
        self.fixed = False
        self.domain = SimpleNamespace(bounds=lambda: (None, None))
        self._parent = SimpleNamespace(name=name)

    def parent_component(self):
        return self._parent

    def index(self):
        return None

    def is_binary(self):
        return False

    def is_integer(self):
        return False

    def is_continuous(self):
        return True

    def set_value(self, value, skip_validation=False):
        self.value = value


class _StubExpr:
    def __init__(self, variables=(), constant=None):
        self.variables = tuple(variables)
        self.constant = constant
        self.evaluations = 0

    def evaluate(self):
        self.evaluations += 1
        if self.constant is not None:
            return self.constant
        return sum(var.value for var in self.variables)


class _StubConstraint:
    def __init__(self, name, body, lower=None, upper=None):
        self._parent = SimpleNamespace(name=name)
        self.body = body
        self.lower = lower
        self.upper = upper

    def parent_component(self):
        return self._parent

    def index(self):
        return None


class _StubObjective:
    def __init__(self, name, expr):
        self._parent = SimpleNamespace(name=name)
        self.expr = expr

    def parent_component(self):
        return self._parent


class _StubBlock:
    pass


class _StubBuildAction:
    pass


class _StubBuildCheck:
    pass


class _StubParam:
    pass


class _StubSet:
    pass


class _StubRangeSet:
    pass


class _StubExpression:
    pass


class _StubSuffix:
    pass


class _StubComponent:
    def __init__(self, ctype, name, *, active=True, values=()):
        self.ctype = ctype
        self.name = name
        self.active = active
        self._values = tuple(values)

    def values(self):
        return iter(self._values)


class _StubModel:
    def __init__(self, variables=(), constraints=(), objectives=(), extra_components=()):
        self.variables = list(variables)
        self.constraints = list(constraints)
        self.objectives = list(objectives)
        self.extra_components = list(extra_components)

    def component_objects(self, active=True, descend_into=False):
        groups = (
            (_StubVar, self.variables, "variable"),
            (_StubConstraint, self.constraints, "constraint"),
            (_StubObjective, self.objectives, "objective"),
        )
        for ctype, items, name in groups:
            if items:
                yield _StubComponent(ctype, name)
        for component in self.extra_components:
            if active is None or component.active:
                yield component

    def component_data_objects(self, kind, active=True, descend_into=True):
        if kind is _StubVar:
            return iter(self.variables)
        if kind is _StubConstraint:
            return iter(self.constraints)
        if kind is _StubObjective:
            return iter(self.objectives)
        return iter(())


def _install_pyomo_logic_stubs(monkeypatch):
    pyomo = ModuleType("pyomo")
    pyomo.__path__ = []
    common = ModuleType("pyomo.common")
    common.__path__ = []
    collections = ModuleType("pyomo.common.collections")
    collections.ComponentMap = _IdentityMap
    collections.ComponentSet = _IdentitySet
    environ = ModuleType("pyomo.environ")
    environ.Constraint = _StubConstraint
    environ.Objective = _StubObjective
    environ.Var = _StubVar
    environ.Block = _StubBlock
    environ.BuildAction = _StubBuildAction
    environ.BuildCheck = _StubBuildCheck
    environ.Param = _StubParam
    environ.Set = _StubSet
    environ.RangeSet = _StubRangeSet
    environ.Expression = _StubExpression
    environ.Suffix = _StubSuffix
    core = ModuleType("pyomo.core")
    core.__path__ = []
    expr = ModuleType("pyomo.core.expr")
    expr.__path__ = []
    visitor = ModuleType("pyomo.core.expr.visitor")
    visitor.identify_variables = lambda node, include_fixed=True: getattr(node, "variables", ())
    pyomo.common = common
    common.collections = collections
    pyomo.environ = environ
    pyomo.core = core
    core.expr = expr
    expr.visitor = visitor
    for name, module in {
        "pyomo": pyomo,
        "pyomo.common": common,
        "pyomo.common.collections": collections,
        "pyomo.environ": environ,
        "pyomo.core": core,
        "pyomo.core.expr": expr,
        "pyomo.core.expr.visitor": visitor,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)


def test_checker_rejects_unknown_active_component_before_mutating_values(monkeypatch):
    _install_pyomo_logic_stubs(monkeypatch)
    import optagent_core.results as results

    class UnsupportedSOS:
        pass

    x = _StubVar("x", 7)
    expression = _StubExpr((x,))
    model = _StubModel(
        [x],
        objectives=[_StubObjective("cost", expression)],
        extra_components=[_StubComponent(UnsupportedSOS, "sos", active=True)],
    )
    with pytest.raises(ContractError) as error:
        results.check_pyomo_model(
            model,
            _IdentityMap([(x, 1.0)]),
            ObjectiveSpec("cost", "min", "USD", expression, None),
            input_digest="a" * 64,
            instance_key=[],
            instance_binding_digest="b" * 64,
            feasibility_tolerance=1e-7,
            integrality_tolerance=1e-7,
        )

    assert error.value.code == "capability_uncovered"
    assert error.value.details == {"component": "sos", "ctype": "UnsupportedSOS"}
    assert x.value == 7
    assert expression.evaluations == 0


def test_inactive_unknown_component_is_outside_active_model_scope(monkeypatch):
    _install_pyomo_logic_stubs(monkeypatch)
    from optagent_core.models import require_supported_active_components

    class UnsupportedGDP:
        pass

    model = _StubModel(extra_components=[_StubComponent(UnsupportedGDP, "inactive_gdp", active=False)])
    require_supported_active_components(model)


def test_missing_primal_never_evaluates_rows_or_objective_from_stale_values(monkeypatch):
    _install_pyomo_logic_stubs(monkeypatch)
    import optagent_core.results as results

    x = _StubVar("x", 41)
    row_expr = _StubExpr((x,))
    second_row_expr = _StubExpr((x,))
    objective_expr = _StubExpr((x,))
    first_row = _StubConstraint("required", row_expr, lower=2)
    second_row = _StubConstraint("required", second_row_expr, lower=2)
    second_row._parent = first_row.parent_component()
    model = _StubModel(
        [x],
        [first_row, second_row],
        [_StubObjective("cost", objective_expr)],
    )
    monkeypatch.setattr(results, "_value", lambda expr: expr.evaluate() if isinstance(expr, _StubExpr) else expr)
    violations = []

    checked = results.check_pyomo_model(
        model,
        _IdentityMap(),
        ObjectiveSpec("cost", "min", "USD", objective_expr, None),
        input_digest="a" * 64,
        instance_key=[],
        instance_binding_digest="b" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
        violation_sink=violations.append,
    )

    assert checked.record["status"] == "uncovered"
    assert checked.witness_variables is None
    assert checked.objective_values == ()
    assert checked.record["objective_values"] == []
    assert checked.record["uncovered"] == [
        "active_variable_missing_primal:x",
        "objective_value_unavailable:cost",
        "row_value_unavailable:required",
    ]
    assert checked.record["component_coverage"] == [{
        "component": "required",
        "required": 2,
        "checked": 0,
        "failures": 0,
        "uncovered": 2,
        "max_violation": None,
        "max_violation_key": None,
    }]
    assert violations == [
        {
            "kind": "variable",
            "component": "x",
            "key": [],
            "lower": 0.0,
            "body": None,
            "upper": 10.0,
            "violation": None,
            "error": "missing_or_nonfinite_value",
        },
        {
            "kind": "constraint",
            "component": "required",
            "key": [],
            "lower": None,
            "body": None,
            "upper": None,
            "violation": None,
            "error": "row_value_unavailable",
        },
    ]
    assert row_expr.evaluations == 0
    assert second_row_expr.evaluations == 0
    assert objective_expr.evaluations == 0
    assert x.value == 41

    observation = HighsObservation(
        record={"raw_solution_value_valid": False},
        backend={"id": "highs", "version": "test-stub"},
        termination="time_limit",
        raw_status="kTimeLimit",
        solver_invoked=True,
        solver_returned=True,
        primals={},
        solver_incumbent_objective=None,
        bound_value=7.0,
        nonfinite_fields={},
        solve_wall_seconds=0.1,
        error=None,
    )
    result = results.result_record(
        observation,
        checked,
        ObjectiveSpec("cost", "min", "USD", objective_expr, None),
        instance_key=[],
        instance_binding_digest="b" * 64,
        source_revision=1,
        candidate_revision=1,
        input_digest="a" * 64,
        layer=None,
        bound_evidence_ref={"path": "backend.json", "sha256": "c" * 64, "byte_len": 2, "media_type": "application/json"},
    )
    assert result["objective"] is None
    assert result["gap"] == {
        "absolute": None,
        "relative": None,
        "definition": result["gap"]["definition"],
    }
    assert result["has_checked_candidate"] is False
    assert result["has_solver_incumbent"] is False


def test_finite_invalid_primal_still_drives_constraint_check_not_var_value(monkeypatch):
    _install_pyomo_logic_stubs(monkeypatch)
    import optagent_core.results as results

    x = _StubVar("x", 9)
    row_expr = _StubExpr((x,))
    objective_expr = _StubExpr((x,))
    model = _StubModel(
        [x],
        [_StubConstraint("upper", row_expr, upper=4)],
        [_StubObjective("cost", objective_expr)],
    )
    monkeypatch.setattr(results, "_value", lambda expr: expr.evaluate() if isinstance(expr, _StubExpr) else expr)
    violations = []

    checked = results.check_pyomo_model(
        model,
        _IdentityMap([(x, 5.0)]),
        ObjectiveSpec("cost", "min", "USD", objective_expr, None),
        input_digest="a" * 64,
        instance_key=[],
        instance_binding_digest="b" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
        violation_sink=violations.append,
    )

    assert checked.record["status"] == "fail"
    assert checked.record["component_coverage"][0]["max_violation"] == 1.0
    assert violations == [{
        "kind": "constraint",
        "component": "upper",
        "key": [],
        "lower": None,
        "body": 5.0,
        "upper": 4.0,
        "violation": 1.0,
        "error": "constraint_violation",
    }]
    assert row_expr.evaluations == 1
    assert x.value == 9


def test_partial_primal_keeps_the_first_concrete_missing_key_per_component(monkeypatch):
    _install_pyomo_logic_stubs(monkeypatch)
    import optagent_core.results as results

    x = _StubVar("x", 41)
    y = _StubVar("x", 42)
    x._parent = SimpleNamespace(name="x")
    y._parent = x.parent_component()
    x.index = lambda: 0
    y.index = lambda: 1
    row_expr = _StubExpr((x, y))
    objective_expr = _StubExpr((x, y))
    model = _StubModel(
        [x, y],
        [_StubConstraint("required", row_expr, lower=0)],
        [_StubObjective("cost", objective_expr)],
    )
    monkeypatch.setattr(results, "_value", lambda expr: expr.evaluate() if isinstance(expr, _StubExpr) else expr)
    violations = []

    checked = results.check_pyomo_model(
        model,
        _IdentityMap([(x, 1.0)]),
        ObjectiveSpec("cost", "min", "USD", objective_expr, None),
        input_digest="a" * 64,
        instance_key=[],
        instance_binding_digest="b" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
        violation_sink=violations.append,
    )

    assert checked.record["status"] == "uncovered"
    assert checked.record["variable_coverage"] == {"required": 2, "present": 1, "completed": 0, "missing_count": 1}
    assert checked.record["uncovered"] == [
        "active_variable_missing_primal:x",
        "objective_value_unavailable:cost",
        "row_value_unavailable:required",
    ]
    assert checked.candidate_complete is False
    assert violations == [
        {
            "kind": "variable",
            "component": "x",
            "key": [1],
            "lower": 0.0,
            "body": None,
            "upper": 10.0,
            "violation": None,
            "error": "missing_or_nonfinite_value",
        },
        {
            "kind": "constraint",
            "component": "required",
            "key": [],
            "lower": None,
            "body": None,
            "upper": None,
            "violation": None,
            "error": "row_value_unavailable",
        },
    ]
    assert row_expr.evaluations == 0
    assert objective_expr.evaluations == 0
    assert x.value == 41 and y.value == 42


def test_many_missing_values_emit_one_sample_per_component_and_reason(monkeypatch):
    _install_pyomo_logic_stubs(monkeypatch)
    import optagent_core.results as results

    variables = [_StubVar("x", index) for index in range(64)]
    for index, variable in enumerate(variables):
        variable.index = lambda index=index: index
    objective_expr = _StubExpr(variables)
    rows = []
    for index in range(20):
        expression = _StubExpr(variables)
        row = _StubConstraint("required", expression, lower=0)
        row.index = lambda index=index: index
        if rows:
            row._parent = rows[0].parent_component()
        rows.append(row)
    model = _StubModel(variables, rows, [_StubObjective("cost", objective_expr)])
    monkeypatch.setattr(results, "_value", lambda expr: expr.evaluate() if isinstance(expr, _StubExpr) else expr)
    violations = []

    checked = results.check_pyomo_model(
        model,
        _IdentityMap(),
        ObjectiveSpec("cost", "min", "USD", objective_expr, None),
        input_digest="a" * 64,
        instance_key=[],
        instance_binding_digest="b" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
        violation_sink=violations.append,
    )

    assert checked.record["status"] == "uncovered"
    assert checked.record["variable_coverage"] == {
        "required": 64, "present": 0, "completed": 0, "missing_count": 64,
    }
    assert checked.record["component_coverage"] == [{
        "component": "required",
        "required": 20,
        "checked": 0,
        "failures": 0,
        "uncovered": 20,
        "max_violation": None,
        "max_violation_key": None,
    }]
    assert len(violations) == 2
    assert violations[0]["component"] == "x" and violations[0]["key"] == [0]
    assert violations[0]["error"] == "missing_or_nonfinite_value"
    assert violations[1]["component"] == "required" and violations[1]["key"] == [0]
    assert violations[1]["error"] == "row_value_unavailable"


def test_actual_constraint_violations_remain_exhaustive_within_component(monkeypatch):
    _install_pyomo_logic_stubs(monkeypatch)
    import optagent_core.results as results

    x = _StubVar("x", 9)
    first_expr = _StubExpr((x,))
    second_expr = _StubExpr((x,))
    objective_expr = _StubExpr((x,))
    first = _StubConstraint("upper", first_expr, upper=4)
    second = _StubConstraint("upper", second_expr, upper=3)
    first.index = lambda: 0
    second.index = lambda: 1
    second._parent = first.parent_component()
    model = _StubModel([x], [first, second], [_StubObjective("cost", objective_expr)])
    monkeypatch.setattr(results, "_value", lambda expr: expr.evaluate() if isinstance(expr, _StubExpr) else expr)
    violations = []

    checked = results.check_pyomo_model(
        model,
        _IdentityMap([(x, 5.0)]),
        ObjectiveSpec("cost", "min", "USD", objective_expr, None),
        input_digest="a" * 64,
        instance_key=[],
        instance_binding_digest="b" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
        violation_sink=violations.append,
    )

    assert checked.record["status"] == "fail"
    assert checked.record["component_coverage"][0]["failures"] == 2
    assert [row["key"] for row in violations] == [[0], [1]]
    assert [row["violation"] for row in violations] == [1.0, 2.0]


@pytest.mark.parametrize("nonfinite", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_primal_remains_uncovered_with_one_diagnostic_sample(monkeypatch, nonfinite):
    _install_pyomo_logic_stubs(monkeypatch)
    import optagent_core.results as results

    x = _StubVar("x", 41)
    row_expr = _StubExpr((x,))
    objective_expr = _StubExpr((x,))
    model = _StubModel(
        [x],
        [_StubConstraint("required", row_expr, lower=0)],
        [_StubObjective("cost", objective_expr)],
    )
    monkeypatch.setattr(results, "_value", lambda expr: expr.evaluate() if isinstance(expr, _StubExpr) else expr)
    violations = []

    checked = results.check_pyomo_model(
        model,
        _IdentityMap([(x, nonfinite)]),
        ObjectiveSpec("cost", "min", "USD", objective_expr, None),
        input_digest="a" * 64,
        instance_key=[],
        instance_binding_digest="b" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
        violation_sink=violations.append,
    )

    assert checked.record["status"] == "uncovered"
    assert checked.record["variable_coverage"]["missing_count"] == 1
    assert checked.record["uncovered"] == [
        "active_variable_missing_primal:x",
        "objective_value_unavailable:cost",
        "row_value_unavailable:required",
    ]
    assert violations == [
        {
            "kind": "variable",
            "component": "x",
            "key": [],
            "lower": 0.0,
            "body": None,
            "upper": 10.0,
            "violation": None,
            "error": "missing_or_nonfinite_value",
        },
        {
            "kind": "constraint",
            "component": "required",
            "key": [],
            "lower": None,
            "body": None,
            "upper": None,
            "violation": None,
            "error": "row_value_unavailable",
        },
    ]
    assert row_expr.evaluations == 0
    assert objective_expr.evaluations == 0
    assert x.value == 41


def test_fixed_variable_without_model_value_is_missing_not_completed(monkeypatch):
    _install_pyomo_logic_stubs(monkeypatch)
    import optagent_core.results as results

    x = _StubVar("x", None)
    x.fixed = True
    objective_expr = _StubExpr((x,))
    model = _StubModel([x], [], [_StubObjective("cost", objective_expr)])
    monkeypatch.setattr(results, "_value", lambda expr: expr.evaluate() if isinstance(expr, _StubExpr) else expr)
    violations = []

    checked = results.check_pyomo_model(
        model,
        _IdentityMap(),
        ObjectiveSpec("cost", "min", "USD", objective_expr, None),
        input_digest="a" * 64,
        instance_key=[],
        instance_binding_digest="b" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
        violation_sink=violations.append,
    )

    assert checked.record["status"] == "uncovered"
    assert checked.record["variable_coverage"] == {"required": 1, "present": 0, "completed": 0, "missing_count": 1}
    assert checked.objective_values == ()
    assert checked.witness_variables is None
    assert objective_expr.evaluations == 0
    assert checked.record["uncovered"] == [
        "fixed_variable_value_missing:x",
        "objective_value_unavailable:cost",
    ]
    assert violations == [{
        "kind": "variable",
        "component": "x",
        "key": [],
        "lower": 0.0,
        "body": None,
        "upper": 10.0,
        "violation": None,
        "error": "missing_or_nonfinite_value",
    }]


def test_raw_invalid_is_not_solver_incumbent_but_complete_checked_candidate_is_retained():
    checked = CheckedModel(
        record={"status": "pass", "uncovered": []},
        witness_variables=({"component": "x", "key": [], "value": 10.0},),
        objective_values=({"component": "cost", "sense": "min", "unit": "USD", "value": 10.0},),
        candidate_complete=True,
        raw_vector_complete=True,
    )
    observation = HighsObservation(
        record={"raw_solution_value_valid": False},
        backend={"id": "highs", "version": "logic-stub"},
        termination="time_limit",
        raw_status="kTimeLimit",
        solver_invoked=True,
        solver_returned=True,
        primals={},
        solver_incumbent_objective=10.0,
        bound_value=7.0,
        nonfinite_fields={},
        solve_wall_seconds=0.1,
        error=None,
    )
    result = result_record(
        observation,
        checked,
        ObjectiveSpec("cost", "min", "USD", None, None),
        instance_key=[],
        instance_binding_digest="b" * 64,
        source_revision=1,
        candidate_revision=1,
        input_digest="a" * 64,
        layer=None,
        bound_evidence_ref={"path": "backend.json", "sha256": "c" * 64, "byte_len": 2, "media_type": "application/json"},
    )
    assert result["has_solver_incumbent"] is False
    assert result["has_checked_candidate"] is True
    assert result["gap"]["absolute"] is None
    assert result["gap"]["relative"] is None


def test_zero_variable_deterministic_check_remains_a_checked_candidate(monkeypatch):
    _install_pyomo_logic_stubs(monkeypatch)
    import optagent_core.results as results

    expression = _StubExpr(constant=5.0)
    model = _StubModel([], [], [_StubObjective("cost", expression)])
    monkeypatch.setattr(results, "_value", lambda expr: expr.evaluate() if isinstance(expr, _StubExpr) else expr)
    monkeypatch.setattr(results, "_backend_versions", lambda: {
        "id": "highs", "version": "verified-profile-stub", "os": "Linux", "architecture": "x86_64"
    })
    objective = ObjectiveSpec("cost", "min", "USD", expression, None)
    checked = results.check_pyomo_model(
        model,
        _IdentityMap(),
        objective,
        input_digest="a" * 64,
        instance_key=[],
        instance_binding_digest="b" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
    )
    result = results.result_record(
        None,
        checked,
        objective,
        instance_key=[],
        instance_binding_digest="b" * 64,
        source_revision=1,
        candidate_revision=1,
        input_digest="a" * 64,
        layer=None,
    )
    assert checked.record["status"] == "pass"
    assert checked.candidate_complete is True
    assert result["has_checked_candidate"] is True
    assert result["has_solver_incumbent"] is False


def test_backend_profile_requires_linux_amd64_cpython_312_and_records_platform(monkeypatch):
    import optagent_core.results as results

    package_versions = {"pyomo": "6.9.4", "highspy": "1.10.0", "numpy": "2.3.2"}
    numpy = ModuleType("numpy")
    pyomo = ModuleType("pyomo")
    highspy = ModuleType("highspy")
    highspy.Highs = lambda: SimpleNamespace(version=lambda: "1.10.0")
    for name, module in (("numpy", numpy), ("pyomo", pyomo), ("highspy", highspy)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(results.importlib_metadata, "version", lambda name: package_versions[name])
    monkeypatch.setattr(results, "sys", SimpleNamespace(version_info=(3, 12)))
    monkeypatch.setattr(results, "platform", SimpleNamespace(
        system=lambda: "Linux",
        machine=lambda: "x86_64",
        python_implementation=lambda: "CPython",
        python_version=lambda: "3.12.11",
    ))

    backend = results._backend_versions()
    assert backend["os"] == "Linux"
    assert backend["architecture"] == "x86_64"
    assert backend["python_implementation"] == "CPython"
    assert backend["python_version"] == "3.12.11"
    assert backend["package_versions"] == {"pyomo": "6.9.4", "highspy": "1.10.0", "numpy": "2.3.2"}
    assert backend["adapter_version"] == "6.9.4"

    monkeypatch.setattr(results, "platform", SimpleNamespace(
        system=lambda: "Windows",
        machine=lambda: "ARM64",
        python_implementation=lambda: "CPython",
        python_version=lambda: "3.12.11",
    ))
    with pytest.raises(ContractError) as error:
        results._backend_versions()
    assert error.value.code == "capability_uncovered"
    assert error.value.details["os"] == "Windows"
    assert error.value.details["architecture"] == "arm64"


def test_backend_package_mismatch_returns_capability_uncovered_with_profile_details(monkeypatch):
    import optagent_core.results as results

    package_versions = {"pyomo": "6.9.4", "highspy": "1.10.0", "numpy": "2.3.1"}
    numpy = ModuleType("numpy")
    pyomo = ModuleType("pyomo")
    highspy = ModuleType("highspy")
    highspy.Highs = lambda: SimpleNamespace(version=lambda: "1.10.0")
    for name, module in (("numpy", numpy), ("pyomo", pyomo), ("highspy", highspy)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(results.importlib_metadata, "version", lambda name: package_versions[name])
    monkeypatch.setattr(results, "sys", SimpleNamespace(version_info=(3, 12)))
    monkeypatch.setattr(results, "platform", SimpleNamespace(
        system=lambda: "Linux",
        machine=lambda: "x86_64",
        python_implementation=lambda: "CPython",
        python_version=lambda: "3.12.11",
    ))

    with pytest.raises(ContractError) as error:
        results._backend_versions()

    assert error.value.code == "capability_uncovered"
    assert error.value.details["observed"] == {"pyomo": "6.9.4", "highspy": "1.10.0", "numpy": "2.3.1"}
    assert error.value.details["expected"] == {"pyomo": "6.9.4", "highspy": "1.10.0", "numpy": "2.3.2"}
    assert error.value.details["expected_platform"] == {
        "os": "Linux",
        "architecture": "x86_64/amd64",
        "python_implementation": "CPython",
        "python_version": "3.12.x",
    }
    assert error.value.details["python_version"] == "3.12.11"


def test_backend_profile_rejects_missing_distribution_metadata(monkeypatch):
    import optagent_core.results as results
    from importlib.metadata import PackageNotFoundError

    numpy = ModuleType("numpy")
    pyomo = ModuleType("pyomo")
    highspy = ModuleType("highspy")
    highspy.Highs = lambda: SimpleNamespace(version=lambda: "1.10.0")
    for name, module in (("numpy", numpy), ("pyomo", pyomo), ("highspy", highspy)):
        monkeypatch.setitem(sys.modules, name, module)

    def missing_distribution(name):
        if name == "highspy":
            raise PackageNotFoundError(name)
        return {"pyomo": "6.9.4", "numpy": "2.3.2"}[name]

    monkeypatch.setattr(results.importlib_metadata, "version", missing_distribution)
    monkeypatch.setattr(results, "sys", SimpleNamespace(version_info=(3, 12)))
    monkeypatch.setattr(results, "platform", SimpleNamespace(
        system=lambda: "Linux",
        machine=lambda: "x86_64",
        python_implementation=lambda: "CPython",
        python_version=lambda: "3.12.11",
    ))

    with pytest.raises(ContractError) as error:
        results._backend_versions()

    assert error.value.code == "environment_unavailable"
    assert error.value.details == {"distribution": "highspy"}


@pytest.mark.parametrize("termination", ["time_limit", "iteration_limit", "feasible"])
def test_numerical_closure_accepts_verified_gap_on_supported_nonoptimal_termination(termination):
    from optagent_core.results import _result_numerical_summary

    summary = _result_numerical_summary(
        check_record={"status": "pass", "tolerances": {"feasibility": 1e-7}},
        objective_value=100.0,
        objective_sense="min",
        bound={"value": 99.0, "direction": "lower", "scope": "global"},
        absolute_gap=1.0,
        termination=termination,
        has_incumbent=True,
        requested_options={"mip_rel_gap": 0.0, "mip_abs_gap": 1.0},
        effective_options={"mip_rel_gap": 0.0, "mip_abs_gap": 1.0},
        native_observation=_native_observation_record(termination=termination),
    )

    assert summary["optimality"]["status"] == "closed"
    assert summary["optimality"]["allowed_gap"] == 1.0


@pytest.mark.parametrize("termination", ["error", "numeric_error", "unknown", "infeasible", "unbounded"])
def test_numerical_closure_does_not_upgrade_unsupported_termination(termination):
    from optagent_core.results import _result_numerical_summary

    summary = _result_numerical_summary(
        check_record={"status": "pass", "tolerances": {}},
        objective_value=100.0,
        objective_sense="min",
        bound={"value": 99.0, "direction": "lower", "scope": "global"},
        absolute_gap=1.0,
        termination=termination,
        has_incumbent=True,
        requested_options={"mip_rel_gap": 0.0, "mip_abs_gap": 1.0},
        effective_options={"mip_rel_gap": 0.0, "mip_abs_gap": 1.0},
        native_observation=_native_observation_record(termination=termination),
    )

    assert summary["optimality"]["status"] == "not_closed"


@pytest.mark.parametrize(
    "effective_options",
    [
        {"mip_rel_gap": -0.01, "mip_abs_gap": 1.0},
        {"mip_rel_gap": 0.0, "mip_abs_gap": -0.01},
        {"mip_rel_gap": None, "mip_abs_gap": 1.0},
        {"mip_rel_gap": 0.0, "mip_abs_gap": None},
    ],
)
def test_numerical_closure_keeps_missing_or_negative_effective_gap_unclosed(effective_options):
    from optagent_core.results import _result_numerical_summary

    summary = _result_numerical_summary(
        check_record={"status": "pass", "tolerances": {}},
        objective_value=100.0,
        objective_sense="min",
        bound={"value": 99.0, "direction": "lower", "scope": "global"},
        absolute_gap=1.0,
        termination="time_limit",
        has_incumbent=True,
        requested_options={"mip_rel_gap": None, "mip_abs_gap": None},
        effective_options=effective_options,
        native_observation=_native_observation_record(),
    )

    assert summary["optimality"]["status"] == "unknown"
    assert summary["optimality"]["allowed_gap"] is None


def test_schema3_core_gap_target_closes_without_native_gap_readback():
    from optagent_core.results import _result_numerical_summary

    requested = {"native": {"parameters": {}}, "core": {"relative_gap": 0.0, "absolute_gap": 1.0}}
    effective = {"time_limit_seconds": None, "threads": None, "random_seed": None, "parameters": {}}
    summary = _result_numerical_summary(
        check_record={"status": "pass", "tolerances": {"feasibility": 1e-8}},
        objective_value=100.0,
        objective_sense="min",
        bound={"value": 99.0, "direction": "lower", "scope": "global"},
        absolute_gap=1.0,
        termination="time_limit",
        has_incumbent=True,
        requested_options=requested,
        effective_options=effective,
        core_gap_target={"relative_gap": 0.0, "absolute_gap": 1.0},
        native_observation=_native_observation_record(),
    )

    assert summary["optimality"]["status"] == "closed"
    assert summary["optimality"]["allowed_gap"] == 1.0
    assert summary["core_gap_target"] == {"relative_gap": 0.0, "absolute_gap": 1.0}
