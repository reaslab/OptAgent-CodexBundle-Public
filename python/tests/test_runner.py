from __future__ import annotations

import hashlib
import math
import os
import uuid
from pathlib import Path

import pytest

from optagent_core.contracts import ContractError, canonical_json_bytes, load_tool_schemas, ref_from_bytes, strict_json_loads, validate_schema
from optagent_core.data import load_data_bundle
from optagent_core import api as api_module
from optagent_core import runner as runner_module
from optagent_core.runner import (
    _check_bridge_identity,
    _check_instance_identity,
    _inspect,
    _launcher_code,
    _page_results_by_instance,
    _prepare,
    _reconcile_run,
    _run_summary,
    _validate_normalized_bound_and_gap,
    _validate_schema3_backend_observation,
    _validate_ref_in_run,
)
from optagent_core.store import CaseStore


def test_safe_parts_reserves_only_the_machine_state_root():
    assert runner_module._safe_parts("models/.reaslab/model.py", pointer="/model") == (
        "models",
        ".reaslab",
        "model.py",
    )
    with pytest.raises(ContractError, match="reserved workspace area"):
        runner_module._safe_parts(".reaslab/optagent/model.py", pointer="/model")


def _schema3_observation_fixture():
    options = {
        "execution_budget_seconds": 300,
        "time_limit_seconds": None,
        "relative_gap": 0.0,
        "absolute_gap": 0.0,
        "threads": None,
        "random_seed": None,
        "warm_start": False,
        "feasibility_tolerance": 1e-8,
        "integrality_tolerance": 1e-8,
        "write_log": False,
        "export_model": False,
    }
    core = {
        "relative_gap": options["relative_gap"],
        "absolute_gap": options["absolute_gap"],
        "feasibility_tolerance": options["feasibility_tolerance"],
        "integrality_tolerance": options["integrality_tolerance"],
    }
    requested = {
        "native": {"time_limit_seconds": None, "threads": None, "random_seed": None, "warm_start": False, "parameters": {}},
        "core": core,
    }
    effective = {"time_limit_seconds": None, "threads": None, "random_seed": None, "warm_start": None, "parameters": {}}
    record = {
        "termination": "time_limit",
        "raw_status": "native-time-limit",
        "solver_invoked": True,
        "solver_returned": True,
        "raw_solution_value_valid": True,
        "best_feasible_objective": 3.0,
        "best_objective_bound": 1.5,
        "bound_evidence": {
            "source": "test-native-dual-bound",
            "scope": "global",
            "objective_sense": "min",
            "value_direction": "lower",
            "interface": "test-interface",
            "objective_transform": {"kind": "identity", "formula": "native bound is in original units"},
        },
        "termination_condition": "time_limit",
        "solve_wall_seconds": 2.5,
        "error": None,
        "requested_options": requested,
        "effective_options": effective,
        "option_set_statuses": {
            "time_limit_seconds": "not_requested",
            "threads": "not_requested",
            "random_seed": "not_requested",
            "warm_start": "not_requested",
            "relative_gap": "core_criterion_only",
            "absolute_gap": "core_criterion_only",
            "feasibility_tolerance": "core_checker_only",
            "integrality_tolerance": "core_checker_only",
        },
        "core_gap_criterion": {
            "relative_gap": 0.0,
            "absolute_gap": 0.0,
            "formula": "gap <= max(absolute_gap, relative_gap * abs(checked_objective))",
            "source": "frozen_core_check",
        },
    }
    result = {
        "termination": "time_limit",
        "raw_status": "native-time-limit",
        "solver_invoked": True,
        "has_solver_incumbent": True,
        "objective": {"value": 3.0},
        "bound": {"value": 1.5, "direction": "lower", "scope": "global", "evidence": {"path": "observation"}},
        "layer": None,
        "numerical": {
            "requested_options": requested,
            "effective_options": effective,
            "check_tolerances": {"feasibility": 1e-8, "integrality": 1e-8},
            "optimality": {"status": "not_closed"},
            "core_gap_target": {"relative_gap": 0.0, "absolute_gap": 0.0},
        },
    }
    manifest = {
        "solve_options": options,
        "candidate": {"objective_plan": {"mode": "scalar", "sense": "min"}},
    }
    return record, result, manifest, {"status": "pass", "tolerances": {"feasibility": 1e-8, "integrality": 1e-8}}


def test_schema3_backend_observation_is_bound_to_frozen_core_and_native_options():
    record, result, manifest, check = _schema3_observation_fixture()
    requested, effective, core = _validate_schema3_backend_observation(
        observation=record, result=result, manifest=manifest, check_record=check
    )
    assert requested == record["requested_options"]
    assert effective == record["effective_options"]
    assert core == {
        "relative_gap": manifest["solve_options"]["relative_gap"],
        "absolute_gap": manifest["solve_options"]["absolute_gap"],
        "feasibility_tolerance": manifest["solve_options"]["feasibility_tolerance"],
        "integrality_tolerance": manifest["solve_options"]["integrality_tolerance"],
    }


def test_schema3_runner_does_not_accept_a_bound_without_existing_native_evidence():
    record, result, manifest, check = _schema3_observation_fixture()
    record["bound_evidence"] = None

    with pytest.raises(ContractError, match="lacks matching native bound evidence"):
        _validate_schema3_backend_observation(
            observation=record,
            result=result,
            manifest=manifest,
            check_record=check,
        )


@pytest.mark.parametrize(
    "backend_id,native_gap_names",
    [
        ("highs", ("mip_rel_gap", "mip_abs_gap")),
        ("cp_sat", ("relative_gap_limit", "absolute_gap_limit")),
    ],
)
def test_native_gap_adapter_observation_is_accepted_as_core_target(backend_id, native_gap_names):
    pyo = pytest.importorskip("pyomo.environ")
    if backend_id == "highs":
        pytest.importorskip("highspy")
    else:
        pytest.importorskip("ortools.sat.python.cp_model")

    from optagent_core.solver_protocol import backend_profile_for, run_solver_once

    solve_options = {
        "time_limit_seconds": 10.0,
        "relative_gap": 0.05,
        "absolute_gap": 0.125,
        "threads": 1,
        "random_seed": 7,
        "feasibility_tolerance": 1e-8,
        "integrality_tolerance": 1e-8,
    }
    model = pyo.ConcreteModel()
    model.x = pyo.Var(domain=pyo.Integers, bounds=(0, 4))
    model.minimum = pyo.Constraint(expr=model.x >= 1)
    model.objective = pyo.Objective(expr=model.x, sense=pyo.minimize)
    observation = run_solver_once(
        model,
        backend_id=backend_id,
        objective_sense="min",
        **solve_options,
        backend_profile=backend_profile_for(backend_id),
    )
    record = dict(observation.record)

    assert observation.solver_invoked is True
    assert observation.solver_returned is True
    assert record["option_set_statuses"]["relative_gap"] == "core_criterion_only"
    assert record["option_set_statuses"]["absolute_gap"] == "core_criterion_only"
    for name, value in zip(
        native_gap_names,
        (solve_options["relative_gap"], solve_options["absolute_gap"]),
        strict=True,
    ):
        assert record["requested_options"]["native"]["parameters"][name] == value
        if backend_id == "highs":
            assert record["effective_options"]["parameters"][name] == value
        else:
            assert record["option_set_statuses"]["time_limit_seconds"] == "submitted_no_readback"
            assert record["option_set_statuses"]["threads"] == "submitted_no_readback"
            assert record["option_set_statuses"]["random_seed"] == "submitted_no_readback"
            assert record["effective_options"]["parameters"] == {}

    _, result, manifest, check = _schema3_observation_fixture()
    manifest["solve_options"].update(solve_options)
    has_incumbent = record["raw_solution_value_valid"] is True
    result.update(
        {
            "termination": record["termination"],
            "raw_status": record["raw_status"],
            "solver_invoked": record["solver_invoked"],
            "has_solver_incumbent": has_incumbent,
            "objective": {"value": record["best_feasible_objective"]} if has_incumbent else None,
            "bound": None,
        }
    )
    result["numerical"].update(
        {
            "requested_options": record["requested_options"],
            "effective_options": record["effective_options"],
            "core_gap_target": {
                "relative_gap": solve_options["relative_gap"],
                "absolute_gap": solve_options["absolute_gap"],
            },
        }
    )

    _validate_schema3_backend_observation(
        observation=record,
        result=result,
        manifest=manifest,
        check_record=check,
    )


def test_schema3_result_core_gap_target_accepts_equal_numbers_with_different_json_types():
    record, result, manifest, check = _schema3_observation_fixture()
    result["numerical"]["core_gap_target"] = {"relative_gap": 0, "absolute_gap": 0}

    _validate_schema3_backend_observation(
        observation=record, result=result, manifest=manifest, check_record=check
    )


def test_schema3_result_core_gap_target_rejects_distinct_large_integer_and_float():
    record, result, manifest, check = _schema3_observation_fixture()
    expected = 2**53 + 1
    manifest["solve_options"]["relative_gap"] = expected
    record["requested_options"]["core"]["relative_gap"] = expected
    record["core_gap_criterion"]["relative_gap"] = expected
    result["numerical"]["requested_options"]["core"]["relative_gap"] = expected
    result["numerical"]["core_gap_target"] = {
        "relative_gap": float(expected),
        "absolute_gap": 0.0,
    }

    with pytest.raises(ContractError) as error_info:
        _validate_schema3_backend_observation(
            observation=record, result=result, manifest=manifest, check_record=check
        )

    assert error_info.value.code == "artifact_changed"


def test_schema3_result_core_gap_target_rejects_different_numeric_value():
    record, result, manifest, check = _schema3_observation_fixture()
    result["numerical"]["core_gap_target"]["relative_gap"] = 0.25

    with pytest.raises(ContractError) as error_info:
        _validate_schema3_backend_observation(
            observation=record, result=result, manifest=manifest, check_record=check
        )

    assert error_info.value.code == "artifact_changed"


@pytest.mark.parametrize(
    "target",
    [
        None,
        {"relative_gap": 0.0},
        {"relative_gap": 0.0, "absolute_gap": 0.0, "extra": 0.0},
        {"relative_gap": True, "absolute_gap": 0.0},
        {"relative_gap": float("nan"), "absolute_gap": 0.0},
        {"relative_gap": float("inf"), "absolute_gap": 0.0},
        {"relative_gap": -0.1, "absolute_gap": 0.0},
    ],
)
def test_schema3_result_core_gap_target_rejects_invalid_shape_or_values(target):
    record, result, manifest, check = _schema3_observation_fixture()
    result["numerical"]["core_gap_target"] = target

    with pytest.raises(ContractError) as error_info:
        _validate_schema3_backend_observation(
            observation=record, result=result, manifest=manifest, check_record=check
        )

    assert error_info.value.code == "output_invalid"


def test_schema3_backend_observation_rejects_frozen_native_option_drift():
    record, result, manifest, check = _schema3_observation_fixture()
    manifest["solve_options"]["time_limit_seconds"] = 31
    with pytest.raises(ContractError, match="native requests differ"):
        _validate_schema3_backend_observation(
            observation=record, result=result, manifest=manifest, check_record=check
        )


def test_solve_options_warm_start_defaults_off_and_requires_a_real_boolean():
    options = {
        "execution_budget_seconds": 300,
        "time_limit_seconds": None,
        "relative_gap": 0.0,
        "absolute_gap": 0.0,
        "threads": None,
        "random_seed": None,
        "feasibility_tolerance": 1e-8,
        "integrality_tolerance": 1e-8,
        "write_log": False,
        "export_model": False,
    }

    checked = runner_module._checked_solve_options(options)
    assert checked["warm_start"] is False
    assert runner_module._checked_solve_options({**options, "warm_start": True})["warm_start"] is True

    for invalid in (0, 1, None, "true"):
        with pytest.raises(ContractError) as caught:
            runner_module._checked_solve_options({**options, "warm_start": invalid})
        assert caught.value.code == "invalid_argument"


def test_run_prepare_schema_allows_saved_budget_and_gap_defaults():
    case_id = str(uuid.uuid4())
    candidate_id = str(uuid.uuid4())
    arguments = {
        "op": "prepare",
        "case_id": case_id,
        "expected_state_revision": 0,
        "candidate_id": candidate_id,
        "solve_options": {
            "time_limit_seconds": None,
            "threads": None,
            "random_seed": None,
            "feasibility_tolerance": 1e-8,
            "integrality_tolerance": 1e-8,
            "write_log": False,
            "export_model": False,
        },
    }

    validate_schema(arguments, load_tool_schemas()["optagent.run"]["input_schema"])


def test_dynamic_context_accepts_only_a_nonempty_solver_default(tmp_path: Path):
    trusted = {
        "stamp": {"session_id": "session", "role": "root", "turn_id": "turn", "call_id": "call"},
        "workspace_absolute": str(tmp_path),
        "cancellation_observed": False,
        "captures": [],
        "default_solver_backend": "highs",
    }

    assert api_module._dynamic_context(trusted)["default_solver_backend"] == "highs"
    with pytest.raises(ContractError) as caught:
        api_module._dynamic_context({**trusted, "default_solver_backend": ""})
    assert caught.value.code == "bridge_protocol_error"


@pytest.mark.parametrize("missing", ["execution_budget_seconds", "relative_gap", "absolute_gap"])
def test_solve_options_reports_missing_per_run_values(missing: str):
    options = {
        "execution_budget_seconds": 300,
        "time_limit_seconds": None,
        "relative_gap": 0.0,
        "absolute_gap": 0.0,
        "threads": None,
        "random_seed": None,
        "feasibility_tolerance": 1e-8,
        "integrality_tolerance": 1e-8,
        "write_log": False,
        "export_model": False,
    }
    options.pop(missing)

    with pytest.raises(ContractError) as caught:
        runner_module._checked_solve_options(options)

    assert caught.value.code == "invalid_argument"
    assert caught.value.pointer == f"/solve_options/{missing}"
    assert "required for this Run" in caught.value.message


def test_prepare_freezes_explicit_warm_start_through_prepared_record_and_manifest(tmp_path: Path, monkeypatch):
    store, args, context, _ = _prepare_harness(monkeypatch, tmp_path)
    args["solve_options"]["warm_start"] = True

    result = _prepare(store, args, context)

    input_paths = [path for path in store.writes if path.endswith("/input/manifest.json")]
    assert len(input_paths) == 1
    manifest = strict_json_loads(store.writes[input_paths[0]])
    prepared_paths = [path for path in store.writes if path.endswith("/prepared.json")]
    assert len(prepared_paths) == 1
    prepared = strict_json_loads(store.writes[prepared_paths[0]])
    assert manifest["solve_options"]["warm_start"] is True
    assert prepared["solve_options"]["warm_start"] is True
    assert result["kind"] == "complete"


def test_prepare_rejects_candidate_backend_mismatch_without_mutating_case(tmp_path: Path, monkeypatch):
    store, args, context, candidate = _prepare_harness(monkeypatch, tmp_path)
    context["default_solver_backend"] = "scip"

    with pytest.raises(ContractError) as caught:
        _prepare(store, args, context)

    assert caught.value.code == "invalid_argument"
    assert caught.value.pointer == "/solver_override_reason"
    assert caught.value.details == {
        "field": "solver_override_reason",
        "configured_default": "scip",
        "candidate_backend": "highs",
    }
    assert store.writes == {}
    assert store.state["prepared_ids"] == []
    assert candidate["backend_id"] == "highs"


def test_prepare_solver_override_keeps_frozen_candidate_and_reports_effective_options(tmp_path: Path, monkeypatch):
    store, args, context, candidate = _prepare_harness(monkeypatch, tmp_path)
    context["default_solver_backend"] = "scip"
    args["solver_override_reason"] = "The existing Candidate was explicitly validated with HiGHS."

    result = _prepare(store, args, context)

    assert result["kind"] == "complete"
    reported = result["reply"]["result"]
    assert reported["solver_selection"] == {
        "backend_id": "highs",
        "override_reason": "The existing Candidate was explicitly validated with HiGHS.",
    }
    assert reported["solve_options"] == args["solve_options"] | {"warm_start": False}
    manifest_path = next(path for path in store.writes if path.endswith("/input/manifest.json"))
    manifest = strict_json_loads(store.writes[manifest_path])
    assert manifest["candidate"]["backend_id"] == "highs"
    prepared_path = next(path for path in store.writes if path.endswith("/prepared.json"))
    prepared = strict_json_loads(store.writes[prepared_path])
    assert "solver_override_reason" not in manifest
    assert "solver_override_reason" not in prepared
    assert candidate["backend_id"] == "highs"


def test_prepare_matching_solver_preference_needs_no_override(tmp_path: Path, monkeypatch):
    store, args, context, _candidate = _prepare_harness(monkeypatch, tmp_path)
    context["default_solver_backend"] = "highs"

    result = _prepare(store, args, context)

    assert result["kind"] == "complete"
    assert result["reply"]["result"]["solver_selection"] == {
        "backend_id": "highs",
        "override_reason": None,
    }
    assert result["reply"]["result"]["solve_options"] == args["solve_options"] | {"warm_start": False}


def test_schema3_backend_observation_rejects_warm_start_drift_and_validates_submission_status():
    record, result, manifest, check = _schema3_observation_fixture()
    manifest["solve_options"]["warm_start"] = True

    with pytest.raises(ContractError, match="warm-start request differs"):
        _validate_schema3_backend_observation(
            observation=record, result=result, manifest=manifest, check_record=check
        )

    record["requested_options"]["native"]["warm_start"] = True
    record["effective_options"]["warm_start"] = None
    record["option_set_statuses"]["warm_start"] = "submitted_no_readback"
    result["numerical"]["requested_options"]["native"]["warm_start"] = True
    result["numerical"]["effective_options"]["warm_start"] = None
    requested, _, _ = _validate_schema3_backend_observation(
        observation=record, result=result, manifest=manifest, check_record=check
    )
    assert requested["native"]["warm_start"] is True


def test_schema3_backend_observation_rejects_warm_start_effectiveness_claim():
    record, result, manifest, check = _schema3_observation_fixture()
    record["effective_options"]["warm_start"] = True

    with pytest.raises(ContractError, match="cannot claim an effective readback"):
        _validate_schema3_backend_observation(
            observation=record, result=result, manifest=manifest, check_record=check
        )


def test_schema3_backend_observation_rejects_native_limit_when_frozen_unset():
    record, result, manifest, check = _schema3_observation_fixture()
    record["requested_options"]["native"]["time_limit_seconds"] = 30
    result["numerical"]["requested_options"]["native"]["time_limit_seconds"] = 30
    record["option_set_statuses"]["time_limit_seconds"] = "submitted_no_readback"
    with pytest.raises(ContractError, match="native requests differ"):
        _validate_schema3_backend_observation(
            observation=record, result=result, manifest=manifest, check_record=check
        )


def test_schema3_backend_observation_accepts_matching_frozen_native_limit():
    record, result, manifest, check = _schema3_observation_fixture()
    record["requested_options"]["native"]["time_limit_seconds"] = 31
    result["numerical"]["requested_options"]["native"]["time_limit_seconds"] = 31
    record["option_set_statuses"]["time_limit_seconds"] = "submitted_no_readback"
    manifest["solve_options"]["time_limit_seconds"] = 31
    requested, _, _ = _validate_schema3_backend_observation(
        observation=record, result=result, manifest=manifest, check_record=check
    )
    assert requested["native"]["time_limit_seconds"] == 31


def test_schema3_backend_observation_rejects_submitted_native_option_without_solver_invocation():
    record, result, manifest, check = _schema3_observation_fixture()
    manifest["solve_options"]["time_limit_seconds"] = 31
    record["requested_options"]["native"]["time_limit_seconds"] = 31
    result["numerical"]["requested_options"]["native"]["time_limit_seconds"] = 31
    record["solver_invoked"] = False
    record["solver_returned"] = False
    record["raw_solution_value_valid"] = None
    record["best_feasible_objective"] = None
    record["best_objective_bound"] = None
    record["bound_evidence"] = None
    record["option_set_statuses"]["time_limit_seconds"] = "submitted_no_readback"
    result["solver_invoked"] = False
    result["has_solver_incumbent"] = False
    result["objective"] = None
    result["bound"] = None

    with pytest.raises(ContractError, match="marked submitted without a solver invocation"):
        _validate_schema3_backend_observation(
            observation=record, result=result, manifest=manifest, check_record=check
        )


def test_schema3_backend_observation_rejects_boolean_as_integer_native_readback():
    record, result, manifest, check = _schema3_observation_fixture()
    manifest["solve_options"]["threads"] = 1
    record["requested_options"]["native"]["threads"] = 1
    record["option_set_statuses"]["threads"] = "accepted_readback"
    record["effective_options"]["threads"] = True
    with pytest.raises(ContractError, match="thread count readback"):
        _validate_schema3_backend_observation(
            observation=record, result=result, manifest=manifest, check_record=check
        )


def test_schema3_backend_observation_rejects_boolean_alias_for_core_gap_number():
    record, result, manifest, check = _schema3_observation_fixture()
    manifest["solve_options"]["relative_gap"] = 1.0
    record["requested_options"]["core"]["relative_gap"] = True
    record["core_gap_criterion"]["relative_gap"] = True
    with pytest.raises(ContractError, match="Core/checker option value is invalid"):
        _validate_schema3_backend_observation(
            observation=record, result=result, manifest=manifest, check_record=check
        )


def test_schema3_result_numeric_projection_cannot_alias_boolean_for_integer_option():
    record, result, manifest, check = _schema3_observation_fixture()
    manifest["solve_options"]["relative_gap"] = 1.0
    record["requested_options"]["core"]["relative_gap"] = 1.0
    record["core_gap_criterion"]["relative_gap"] = 1.0
    numeric_options = {
        "native": dict(record["requested_options"]["native"]),
        "core": dict(record["requested_options"]["core"]),
    }
    numeric_options["core"]["relative_gap"] = True
    result["numerical"]["requested_options"] = numeric_options
    with pytest.raises(ContractError, match="Result options differ"):
        _validate_schema3_backend_observation(
            observation=record, result=result, manifest=manifest, check_record=check
        )


def test_schema3_deterministic_result_does_not_claim_requested_native_settings_were_applied():
    record, result, manifest, check = _schema3_observation_fixture()
    manifest["solve_options"].update({"time_limit_seconds": 10.0, "threads": 2, "random_seed": 7})
    record.update({
        "termination": "optimal",
        "raw_status": "deterministic_model_check",
        "solver_invoked": False,
        "solver_returned": False,
        "raw_solution_value_valid": None,
        "best_feasible_objective": None,
        "best_objective_bound": None,
        "bound_evidence": None,
        "termination_condition": "optimal",
        "solve_wall_seconds": 0.0,
    })
    record["requested_options"]["native"].update({"time_limit_seconds": 10.0, "threads": 2, "random_seed": 7})
    record["effective_options"].update({"time_limit_seconds": None, "threads": None, "random_seed": None})
    record["option_set_statuses"].update({
        "time_limit_seconds": "not_applied_solver_not_invoked",
        "threads": "not_applied_solver_not_invoked",
        "random_seed": "not_applied_solver_not_invoked",
    })
    result.update({
        "termination": "optimal",
        "raw_status": "deterministic_model_check",
        "solver_invoked": False,
        "has_solver_incumbent": False,
        "objective": None,
        "bound": None,
    })
    _validate_schema3_backend_observation(
        observation=record, result=result, manifest=manifest, check_record=check
    )


def test_run_result_pagination_keeps_all_layers_of_a_limited_instance() -> None:
    refs = [
        {"path": ".reaslab/optagent/cases/c/runs/r/output/instances/000000/layer-000/result.json"},
        {"path": ".reaslab/optagent/cases/c/runs/r/output/instances/000000/layer-001/result.json"},
        {"path": ".reaslab/optagent/cases/c/runs/r/output/instances/000001/layer-single/result.json"},
    ]

    first = _page_results_by_instance(refs, after_instance=0, limit=1)
    assert first["result_refs"] == refs[:2]
    assert first["instance_total"] == 2
    assert first["result_total"] == 3
    assert first["truncated"] is True
    assert first["next_after"] == 1

    second = _page_results_by_instance(refs, after_instance=first["next_after"], limit=1)
    assert second["result_refs"] == refs[2:]
    assert second["truncated"] is False
    assert second["next_after"] is None

    with pytest.raises(ContractError):
        _page_results_by_instance([{"path": "no-instance-index/result.json"}], after_instance=0, limit=1)


def test_run_inspect_keeps_result_instance_pages_and_verified_batch_projection(monkeypatch):
    case_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    prepared_id = str(uuid.uuid4())
    refs = [
        {"path": f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/instances/000000/layer-000/result.json"},
        {"path": f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/instances/000000/layer-001/result.json"},
        {"path": f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/instances/000002/layer-single/result.json"},
        {"path": f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/instances/000004/layer-000/result.json"},
        {"path": f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/instances/000004/layer-001/result.json"},
    ]
    batch_ref = {
        "path": f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/batch-summary.json",
        "sha256": "a" * 64,
        "byte_len": 2,
        "media_type": "application/json",
    }
    seal_ref = {
        "path": f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/seal.json",
        "sha256": "b" * 64,
        "byte_len": 2,
        "media_type": "application/json",
    }
    checker_diagnostic = {
        "kind": "business_checker_diagnostic",
        "ref": refs[0],
        "code": "output_invalid",
        "message": "business checker returned an invalid BusinessCheck",
        "pointer": "/candidate/business_checker/evidence",
    }
    batch = {
        "schema_version": 1,
        "planned_instance_count": 5,
        "started_instance_count": 4,
        "instances_with_results": 3,
        "instances_with_all_layers_attempted": 3,
        "partial_instance_count": 1,
        "remaining_instance_count": 1,
        "stop_reason": "lexicographic_layer_unavailable",
    }
    state = {"run_ids": [run_id], "state_revision": 9}
    run = {
        "run_id": run_id,
        "prepared_id": prepared_id,
        "dispatch_nonce": str(uuid.uuid4()),
        "dispatch_state": "response_received",
        "results": refs,
        "worker": {"status": "complete", "seal": seal_ref, "solver_invoked": True},
    }
    run_raw = canonical_json_bytes(run)
    run_digest = hashlib.sha256(run_raw).hexdigest()
    run_ref = ref_from_bytes(
        f".reaslab/optagent/cases/{case_id}/runs/{run_id}/versions/{run_digest}.json",
        run_raw,
        "application/json",
    )

    class Store:
        def read_case(self, requested_case_id):
            assert requested_case_id == case_id
            return state

    monkeypatch.setattr("optagent_core.runner._locate_run", lambda store, c, r: (state, run, run_ref))
    monkeypatch.setattr("optagent_core.runner._load_prepared", lambda *args: ({}, {}, {}))
    monkeypatch.setattr(
        "optagent_core.runner._reconcile_run",
        lambda *args: (
            {
                **run,
                "_verified_batch_summary": {"ref": batch_ref, "summary": batch},
                "_verified_business_checker_diagnostics": [checker_diagnostic],
            },
            [batch_ref],
        ),
    )

    def publish(_store, _case_id, _run_id, update, *, expected_nonce=None):
        current = dict(run)
        update(current)
        return current, run_ref

    monkeypatch.setattr("optagent_core.runner._publish_run_update", publish)
    monkeypatch.setattr("optagent_core.runner._run_summary", lambda current, ref: {"run_id": current["run_id"]})

    first = _inspect(Store(), {"case_id": case_id, "run_id": run_id, "limit": 2}, {})["reply"]["result"]
    assert first["result_refs"] == refs[:3]
    assert first["total"] == 3
    assert first["result_total"] == 5
    assert first["truncated"] is True
    assert first["next_after"] == 3
    assert first["batch_summary"] == {"ref": batch_ref, "summary": batch}
    assert first["new_artifacts"] == [batch_ref]
    assert first["diagnostics"] == [checker_diagnostic]

    monkeypatch.setattr("optagent_core.runner._reconcile_run", lambda *args: (
        {
            **run,
            "_verified_batch_summary": {"ref": batch_ref, "summary": batch},
            "_verified_business_checker_diagnostics": [checker_diagnostic],
        }, []
    ))
    second = _inspect(
        Store(),
        {"case_id": case_id, "run_id": run_id, "after_instance": first["next_after"], "limit": 2},
        {},
    )["reply"]["result"]
    assert second["result_refs"] == refs[3:]
    assert second["total"] == 3
    assert second["result_total"] == 5
    assert second["truncated"] is False
    assert second["next_after"] is None
    assert second["new_artifacts"] == []
    assert second["batch_summary"] == {"ref": batch_ref, "summary": batch}
    assert second["diagnostics"] == []
    monkeypatch.setattr("optagent_core.runner._reconcile_run", lambda *args: ({**run, "_verified_batch_summary": None}, []))
    without_summary = _inspect(Store(), {"case_id": case_id, "run_id": run_id, "limit": 2}, {})["reply"]["result"]
    assert without_summary["batch_summary"] is None
    assert without_summary["total"] == 3


def test_business_checker_diagnostic_ignores_malformed_result_warnings():
    result_ref = {
        "path": ".reaslab/optagent/cases/c/runs/r/output/instances/000000/layer-single/result.json",
        "sha256": "c" * 64,
        "byte_len": 2,
        "media_type": "application/json",
    }
    diagnostic = {
        "code": "output_invalid",
        "message": "business checker returned an invalid BusinessCheck",
        "pointer": "/candidate/business_checker/evidence",
    }
    warning = "business_checker_diagnostic:" + canonical_json_bytes(diagnostic).decode("utf-8")

    assert runner_module._business_checker_diagnostic([warning], result_ref) == {
        "kind": "business_checker_diagnostic",
        "ref": result_ref,
        **diagnostic,
    }
    assert runner_module._business_checker_diagnostic(["business_checker_diagnostic:not json"], result_ref) is None
    assert runner_module._business_checker_diagnostic(
        ["business_checker_diagnostic:" + '{"code": "output_invalid", "message": "x", "pointer": null}'],
        result_ref,
    ) is None


def test_run_summary_hides_the_platform_status_placeholder():
    run = {
        "run_id": "run-id",
        "dispatch_state": "response_received",
        "worker": {"status": "incomplete"},
        "platform": {"id_binding": "absent", "execution_id": None, "job_status": "unknown"},
        "cancel": {"requested": False},
        "results": [],
    }
    summary = _run_summary(run, {"path": "run.json"})
    assert summary == {
        "id": "run-id",
        "record_ref": {"path": "run.json"},
        "dispatch_state": "response_received",
        "worker_status": "incomplete",
        "execution_id_available": False,
        "cancel_requested": False,
        "result_count": 0,
    }


def test_inspect_invalid_error_reports_current_run_and_commit_effect(monkeypatch):
    import copy

    case_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    run = {
        "run_id": run_id,
        "prepared_id": str(uuid.uuid4()),
        "dispatch_nonce": str(uuid.uuid4()),
        "dispatch_state": "response_received",
        "worker": {"status": "incomplete", "seal": None, "solver_invoked": True},
        "platform": {"id_binding": "absent", "execution_id": None, "job_status": "unknown"},
        "cancel": {"requested": False},
        "results": [],
    }
    holder = {
        "run": run,
        "ref": {"path": f".reaslab/optagent/cases/{case_id}/runs/{run_id}/versions/old.json"},
    }

    class Store:
        pass

    monkeypatch.setattr(
        runner_module,
        "_locate_run",
        lambda _store, _case_id, _run_id: ({"state_revision": 4}, holder["run"], holder["ref"]),
    )
    monkeypatch.setattr(runner_module, "_load_prepared", lambda *_args: ({}, {}, {}))
    monkeypatch.setattr(
        runner_module,
        "_reconcile_run",
        lambda *_args: (_ for _ in ()).throw(
            ContractError("output_invalid", "seal validation failed", pointer="/seal", details={"path": "seal.json"})
        ),
    )

    def publish(_store, _case_id, _run_id, update, *, expected_nonce=None):
        assert expected_nonce == holder["run"]["dispatch_nonce"]
        previous = holder["run"]
        updated = copy.deepcopy(previous)
        update(updated)
        if updated != previous:
            holder["run"] = updated
            holder["ref"] = {"path": f".reaslab/optagent/cases/{case_id}/runs/{run_id}/versions/invalid.json"}
        return holder["run"], holder["ref"]

    monkeypatch.setattr(runner_module, "_publish_run_update", publish)

    with pytest.raises(ContractError) as first:
        _inspect(Store(), {"case_id": case_id, "run_id": run_id}, {})
    assert first.value.code == "output_invalid"
    assert first.value.message == "seal validation failed"
    assert first.value.pointer == "/seal"
    assert first.value.details["path"] == "seal.json"
    assert first.value.details["run"]["worker_status"] == "invalid"
    assert first.value.details["run"]["record_ref"] == holder["ref"]
    assert "platform_job_status" not in first.value.details["run"]
    assert first.value.effect == "local_committed"

    with pytest.raises(ContractError) as second:
        _inspect(Store(), {"case_id": case_id, "run_id": run_id}, {})
    assert second.value.details["run"]["record_ref"] == holder["ref"]
    assert second.value.effect == "none"


def _prepare_harness(monkeypatch, tmp_path: Path, *, parameter_object_id=None):
    case_id = str(uuid.uuid4())
    candidate_id = str(uuid.uuid4())
    session_id = "session-no-data"
    stamp = {"session_id": session_id, "role": "root", "turn_id": "turn-no-data", "call_id": "call-no-data"}
    state = {
        "case_id": case_id,
        "owner_session_id": session_id,
        "state_revision": 4,
        "source_revision": 2,
        "candidate_revision": 3,
        "promotion_generation": 1,
        "candidate_id": candidate_id,
        "source_ids": [],
        "pending_inputs": [],
        "prepared_ids": [],
    }
    code_ref = ref_from_bytes(
        f".reaslab/optagent/candidate-code/{candidate_id}/build.py",
        b"def build_model(bundle):\n    return None\n\ndef check_business(bundle, solution):\n    return []\n",
        "text/x-python; charset=utf-8",
    )
    candidate = {
        "candidate_id": candidate_id,
        "source_revision": state["source_revision"],
        "candidate_revision": state["candidate_revision"],
        "source_ids": [],
        "authoring": "pyomo",
        "backend_id": "highs",
        "input_closure": "declared",
        "closure_note": "test boundary",
        "binding_digest": "a" * 64,
        "data_id": None,
        "instance_parameter_object_id": parameter_object_id,
        "model": {"path": "models/build.py", "bytes": code_ref},
        "local_modules": [],
        "business_checker": None,
        "objective_plan": {"mode": "feasibility", "preference_note": "test boundary"},
    }

    class PrepareStore:
        def __init__(self):
            self.workspace = tmp_path
            self.state = dict(state)
            self.state["prepared_ids"] = []
            self.writes = {}
            self.reads = {code_ref["path"]: code_ref}
            self.workspace_files = {}

        def read_case(self, actual_case_id):
            assert actual_case_id == case_id
            result = dict(self.state)
            result["prepared_ids"] = list(self.state["prepared_ids"])
            return result

        def read_record(self, path):
            if path not in self.writes:
                raise ContractError("not_found", "path does not exist")
            return strict_json_loads(self.writes[path])

        def read_workspace_file(self, relative_path):
            raw = self.workspace_files.get(relative_path)
            if raw is None:
                raise ContractError("not_found", "workspace file does not exist")
            return bytes(raw), ref_from_bytes(relative_path, bytes(raw), "text/plain; charset=utf-8")

        def write_immutable(self, path, raw, media_type):
            existing = self.writes.get(path)
            if existing is not None and existing != bytes(raw):
                raise ContractError("artifact_changed", "immutable path already contains different bytes")
            self.writes[path] = bytes(raw)
            return ref_from_bytes(path, bytes(raw), media_type)

        def read_ref(self, ref):
            path = ref["path"]
            raw = self.writes.get(path)
            if raw is None:
                known = self.reads.get(path)
                if known is None or known["sha256"] != ref["sha256"]:
                    raise ContractError("not_found", "test Ref is unavailable")
                raw = code_ref_bytes if path == code_ref["path"] else known_bytes[path]
            if len(raw) != ref["byte_len"] or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
                raise ContractError("artifact_changed", "test Ref bytes differ")
            return raw

        def add_source_ref(self, ref, raw):
            self.reads[ref["path"]] = ref
            known_bytes[ref["path"]] = bytes(raw)

        def write_record(self, path, value):
            raw = canonical_json_bytes(value)
            self.writes[path] = raw
            return ref_from_bytes(path, raw, "application/json")

        def cas_case(self, actual_case_id, expected_revision, update):
            assert actual_case_id == case_id
            assert self.state["state_revision"] == expected_revision
            updated = dict(self.state)
            updated["prepared_ids"] = list(self.state["prepared_ids"])
            update(updated)
            updated["state_revision"] += 1
            self.state = updated
            return dict(updated)

    store = PrepareStore()
    code_ref_bytes = b"def build_model(bundle):\n    return None\n\ndef check_business(bundle, solution):\n    return []\n"
    known_bytes = {code_ref["path"]: code_ref_bytes}
    # These tests exercise null-data freezing; session-route snapshot semantics
    # are covered separately. Keep the final publication fence in-contract
    # without making this narrow in-memory store imitate stable OS snapshots.
    class PreparedRouteFence:
        def validate_current(self, current, actual_stamp, *, require_no_pending):
            assert current["case_id"] == case_id
            assert current["owner_session_id"] == session_id
            assert actual_stamp == stamp
            if require_no_pending:
                assert current["pending_inputs"] == []

    def commit_with_prepared_route_fence(actual_store, actual_state, actual_stamp, callback, **kwargs):
        assert actual_store is store
        assert actual_state["case_id"] == case_id
        assert actual_stamp == stamp
        return callback(PreparedRouteFence())

    monkeypatch.setattr(api_module, "_with_session_route_commit", commit_with_prepared_route_fence)
    runtime_digest = "f" * 64
    runtime_manifest_ref = ref_from_bytes(f".reaslab/optagent/runtime/{runtime_digest}/manifest.json", b"{}", "application/json")
    options = {
        "execution_budget_seconds": 120,
        "time_limit_seconds": 60,
        "relative_gap": 0.0,
        "absolute_gap": 0.0,
        "threads": 1,
        "random_seed": 0,
        "feasibility_tolerance": 1e-7,
        "integrality_tolerance": 1e-7,
        "write_log": False,
        "export_model": False,
    }
    args = {
        "case_id": case_id,
        "candidate_id": candidate_id,
        "expected_state_revision": state["state_revision"],
        "solve_options": options,
    }
    context = {"stamp": stamp, "cancellation_observed": False}

    monkeypatch.setattr(api_module, "_full_state_summary", lambda actual_store, actual_state: {"state_revision": actual_state["state_revision"]})
    monkeypatch.setattr(api_module, "_object_summary", lambda object_id, record_ref, status, source_revision, candidate_revision: {"id": object_id, "record_ref": dict(record_ref), "status": status})
    monkeypatch.setattr(api_module, "_require_owner", lambda actual_state, actual_stamp: None)
    monkeypatch.setattr(api_module, "_load_barrier", lambda actual_store, path, actual_session_id: {
        "session_id": actual_session_id,
        "arrival_epoch": "00000000-0000-4000-8000-000000000000",
        "latest_observed_seq": 0,
    })
    monkeypatch.setattr(
        runner_module,
        "_check_pending_and_current_inputs",
        lambda actual_store, actual_state, *, verify_source_bytes=True, verify_candidate_data=True: {
            "math_source_ids": [],
            "records": {},
        },
    )
    monkeypatch.setattr(runner_module, "_json_ref", lambda actual_store, path, label: (candidate, code_ref))
    monkeypatch.setattr(runner_module, "_freeze_source_bytes", lambda *args, **kwargs: ([], {}, {}))
    def freeze_code(actual_store, prefix, spec, pointer):
        source_ref = spec["bytes"]
        raw = actual_store.read_ref(source_ref)
        frozen_ref = actual_store.write_immutable(
            f"{prefix}/source/{spec['path']}", raw, source_ref["media_type"]
        )
        return {"original_path": spec["path"], "bytes": frozen_ref}

    monkeypatch.setattr(runner_module, "_freeze_code", freeze_code)
    monkeypatch.setattr(runner_module, "_publish_runtime_bundle", lambda actual_store: (runtime_digest, runtime_manifest_ref, ()))
    return store, args, context, candidate


def test_direct_prepare_freezes_declared_root_requirements_bytes(tmp_path: Path, monkeypatch):
    store, args, context, candidate = _prepare_harness(monkeypatch, tmp_path)
    candidate["authoring"] = "direct_python"
    candidate["backend_id"] = "direct_python"
    args["solve_options"].update({
        "time_limit_seconds": None,
        "threads": None,
        "random_seed": None,
    })
    declared = b"# -r comment-is-not-an-include\nexample-package==1.2.3\n"
    store.workspace_files["requirements.txt"] = declared

    result = _prepare(store, args, context)

    assert result["kind"] == "complete"
    manifest_path = next(path for path in store.writes if path.endswith("/input/manifest.json"))
    manifest = strict_json_loads(store.writes[manifest_path])
    requirements = manifest["candidate"]["requirements_file"]
    assert requirements["path"] == "requirements.txt"
    assert store.writes[requirements["bytes"]["path"]] == declared


@pytest.mark.parametrize("include", ["-r extra.txt", "--requirement\textra.txt", "-c limits.txt", "--constraint=limits.txt"])
def test_prepare_rejects_unfrozen_requirements_includes(tmp_path: Path, monkeypatch, include: str):
    store, args, context, _candidate = _prepare_harness(monkeypatch, tmp_path)
    store.workspace_files["requirements.txt"] = f"package==1\n{include}\n".encode("utf-8")

    with pytest.raises(ContractError) as caught:
        _prepare(store, args, context)

    assert caught.value.code == "capability_uncovered"
    assert caught.value.details["operation"] == "optagent.run_prepare"
    assert caught.value.details["path"] == "requirements.txt"
    assert caught.value.details["line"] == 2


def test_no_data_reconcile_identity_uses_the_shared_empty_instance_binding():
    class UnreadableReader:
        def read_ref(self, ref):
            pytest.fail("an empty DataBundle must not read a data Ref")

    candidate_binding = "e" * 64
    bundle = load_data_bundle(UnreadableReader(), None, candidate_binding_digest=candidate_binding)
    context = bundle.for_instance(None, ()).instance_context

    assert bundle.data_id is None
    assert bundle.digest == hashlib.sha256(b"null").hexdigest()
    assert bundle.objects == {}
    assert bundle.consumed_inputs == ()
    assert bundle.candidate_binding_digest == candidate_binding
    assert context is not None and context.instance_key == ()
    assert _check_instance_identity(bundle, {"data": None}, []) == context.instance_binding_digest

    with pytest.raises(ContractError) as wrong_key:
        _check_instance_identity(bundle, {"data": None}, ["one"])
    assert wrong_key.value.code == "output_invalid"
    with pytest.raises(ContractError) as wrong_parameter:
        load_data_bundle(UnreadableReader(), None, instance_parameter_object_id="instances", candidate_binding_digest=candidate_binding)
    assert wrong_parameter.value.code == "invalid_argument"
    with pytest.raises(ContractError) as nonempty_instance:
        bundle.for_instance(None, ("one",))
    assert nonempty_instance.value.code == "invalid_argument"


def test_prepare_freezes_null_data_without_publishing_a_fake_data_manifest(tmp_path: Path, monkeypatch):
    store, args, context, candidate = _prepare_harness(monkeypatch, tmp_path)

    result = _prepare(store, args, context)

    input_paths = [path for path in store.writes if path.endswith("/input/manifest.json")]
    assert len(input_paths) == 1
    prepared_manifest = strict_json_loads(store.writes[input_paths[0]])
    assert prepared_manifest["data"] is None
    assert prepared_manifest["binding_digest"] == candidate["binding_digest"]
    assert prepared_manifest["solve_options"]["warm_start"] is False
    assert not any("/data/" in path for path in store.writes)
    assert not any(path.endswith("null.json") or path.endswith("trace.json") for path in store.writes)
    assert result["kind"] == "complete"


def test_prepare_reuses_one_python_file_for_model_and_checker_roles(tmp_path: Path, monkeypatch):
    store, args, context, candidate = _prepare_harness(monkeypatch, tmp_path)
    candidate["business_checker"] = {"path": candidate["model"]["path"], "bytes": candidate["model"]["bytes"]}

    result = _prepare(store, args, context)

    assert result["kind"] == "complete"
    manifest_path = next(path for path in store.writes if path.endswith("/input/manifest.json"))
    frozen = strict_json_loads(store.writes[manifest_path])["candidate"]
    assert frozen["model"]["original_path"] == frozen["business_checker"]["original_path"]
    assert frozen["model"]["bytes"] == frozen["business_checker"]["bytes"]


def test_prepare_still_rejects_same_role_path_with_conflicting_bytes(tmp_path: Path, monkeypatch):
    store, args, context, candidate = _prepare_harness(monkeypatch, tmp_path)
    different = b"def check_business(bundle, solution):\n    return ['different']\n"
    conflict_ref = ref_from_bytes(
        f".reaslab/optagent/candidate-code/{candidate['candidate_id']}/checker.py",
        different,
        "text/x-python; charset=utf-8",
    )
    store.add_source_ref(conflict_ref, different)
    candidate["business_checker"] = {"path": candidate["model"]["path"], "bytes": conflict_ref}

    with pytest.raises(ContractError) as error:
        _prepare(store, args, context)

    assert error.value.code == "artifact_changed"
    assert "unique" not in error.value.message


def test_prepare_rejects_instance_parameter_object_when_data_is_null(tmp_path: Path, monkeypatch):
    store, args, context, _ = _prepare_harness(monkeypatch, tmp_path, parameter_object_id="instances")

    with pytest.raises(ContractError) as error:
        _prepare(store, args, context)

    assert error.value.code == "invalid_argument"
    assert not any(path.endswith("/input/manifest.json") for path in store.writes)


def test_runner_rejects_direction_conflicting_bound_and_keeps_null_bound_candidate():
    objective_plan = {
        "mode": "scalar",
        "objective_component": "cost",
        "sense": "min",
        "unit": "USD",
        "preference_note": "minimize cost",
    }
    check = {"status": "pass", "objective_values": [{"component": "cost", "sense": "min", "value": 5.0}]}
    contradictory = {
        "layer": None,
        "has_solver_incumbent": True,
        "bound": {"value": 6.0, "direction": "lower", "scope": "global", "evidence": {}},
        "gap": {"absolute": 1.0, "relative": 0.2, "definition": "raw difference"},
    }

    with pytest.raises(ContractError, match="conflicts with its checked feasible objective"):
        _validate_normalized_bound_and_gap(contradictory, check, objective_plan)

    # The worker's corrected normalization keeps the checked feasible point,
    # but omits the contradictory bound and its derived gap.
    checked_only = {
        "layer": None,
        "has_solver_incumbent": True,
        "bound": None,
        "gap": {"absolute": None, "relative": None, "definition": "strict direction"},
    }
    _validate_normalized_bound_and_gap(checked_only, check, objective_plan)


def test_runner_checks_gap_against_shared_bound_normalizer():
    objective_plan = {
        "mode": "scalar",
        "objective_component": "cost",
        "sense": "min",
        "unit": "USD",
        "preference_note": "minimize cost",
    }
    check = {"status": "pass", "objective_values": [{"component": "cost", "sense": "min", "value": 5.0}]}
    result = {
        "layer": None,
        "termination": "time_limit",
        "has_solver_incumbent": True,
        "bound": {"value": 4.0, "direction": "lower", "scope": "global", "evidence": {}},
        "gap": {"absolute": 0.0, "relative": 0.0, "definition": "strict direction"},
    }

    with pytest.raises(ContractError, match="gap does not match"):
        _validate_normalized_bound_and_gap(result, check, objective_plan)

    result["gap"] = {"absolute": 1.0, "relative": 0.2, "definition": "strict direction"}
    _validate_normalized_bound_and_gap(result, check, objective_plan)


@pytest.mark.parametrize(
    ("sense", "raw_bound", "checked_objective"),
    [
        ("min", math.nextafter(5.0, math.inf), 5.0),
        ("max", math.nextafter(5.0, -math.inf), 5.0),
        ("min", math.nextafter(math.nextafter(5.0, math.inf), math.inf), 5.0),
    ],
)
def test_runner_keeps_any_conflicting_raw_bound_out_of_result_bound(sense, raw_bound, checked_objective):
    objective_plan = {"mode": "scalar", "sense": sense}
    check = {
        "status": "pass",
        "objective_values": [{"component": "cost", "sense": sense, "value": checked_objective}],
    }
    omitted = {
        "layer": None,
        "termination": "time_limit",
        "has_solver_incumbent": True,
        "bound": None,
        "gap": {"absolute": None, "relative": None, "definition": "strict raw direction"},
        "warnings": [],
    }
    _validate_normalized_bound_and_gap(omitted, check, objective_plan, raw_bound_value=raw_bound)

    forged = dict(omitted)
    forged["bound"] = {
        "value": checked_objective,
        "direction": "lower" if sense == "min" else "upper",
        "scope": "global",
        "evidence": {},
    }
    forged["gap"] = {"absolute": 0.0, "relative": 0.0, "definition": "clamped"}
    with pytest.raises(ContractError, match="strict raw native bound"):
        _validate_normalized_bound_and_gap(forged, check, objective_plan, raw_bound_value=raw_bound)


def test_runner_keeps_raw_bound_only_without_a_checked_point_and_preserves_strict_legal_bound():
    raw_bound = math.nextafter(5.0, math.inf)
    objective_plan = {"mode": "scalar", "sense": "min"}
    raw_only_result = {
        "layer": None,
        "termination": "time_limit",
        "has_solver_incumbent": False,
        "bound": {"value": raw_bound, "direction": "lower", "scope": "global", "evidence": {}},
        "gap": {"absolute": None, "relative": None, "definition": "bound only"},
        "warnings": [],
    }
    _validate_normalized_bound_and_gap(
        raw_only_result,
        {"status": "uncovered", "objective_values": []},
        objective_plan,
        raw_bound_value=raw_bound,
    )

    legal_raw = 4.0
    legal = {
        "layer": None,
        "termination": "time_limit",
        "has_solver_incumbent": True,
        "bound": {"value": legal_raw, "direction": "lower", "scope": "global", "evidence": {}},
        "gap": {"absolute": 1.0, "relative": 0.2, "definition": "strict direction"},
        "warnings": [],
    }
    check = {"status": "pass", "objective_values": [{"component": "cost", "sense": "min", "value": 5.0}]}
    _validate_normalized_bound_and_gap(legal, check, objective_plan, raw_bound_value=legal_raw)


def test_cancel_receipt_identity_uses_the_stop_attempt_digest(monkeypatch):
    run_stamp = {
        "session_id": "session-start",
        "role": "root",
        "turn_id": "turn-start",
        "call_id": "call-start",
    }
    cancel_stamp = {
        "session_id": "session-cancel",
        "role": "root",
        "turn_id": "turn-cancel",
        "call_id": "call-cancel",
    }
    stop_digest = "b" * 64
    run = {
        "created_by": run_stamp,
        "prepared_id": str(uuid.uuid4()),
        "dispatch_nonce": str(uuid.uuid4()),
        "http_request_digest": "a" * 64,
        "cancel": {
            "stop_attempt": {"stamp": cancel_stamp, "request_digest": stop_digest},
        },
    }
    state = {"state_revision": 5}
    monkeypatch.setattr("optagent_core.runner._locate_run", lambda store, case_id, run_id: (state, run, {}))
    prepared = {"prepared_id": run["prepared_id"]}
    monkeypatch.setattr("optagent_core.runner._load_prepared", lambda store, case_id, prepared_id: (prepared, {}, {}))
    phase = {
        "kind": "cancel_receipt",
        "case_id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "dispatch_nonce": run["dispatch_nonce"],
        "request_digest": stop_digest,
    }

    _, actual_state, actual_run, actual_prepared = _check_bridge_identity(
        object(), phase, {"stamp": cancel_stamp}, kind="cancel_receipt"
    )
    assert actual_state is state
    assert actual_run is run
    assert actual_prepared is prepared

    phase["request_digest"] = "a" * 64
    with pytest.raises(ContractError) as error:
        _check_bridge_identity(object(), phase, {"stamp": cancel_stamp}, kind="cancel_receipt")
    assert error.value.code == "bridge_protocol_error"


def test_non_json_run_artifacts_are_hash_checked_incrementally(tmp_path: Path):
    store = CaseStore(tmp_path, create=True)
    case_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    output_root = f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output"
    relative = f"{output_root}/layer/violations.jsonl"
    path = store.resolve_path(relative)
    path.parent.mkdir(parents=True)
    raw = (b"{\"kind\":\"constraint\",\"violation\":1.25}\n" * 50_000)
    path.write_bytes(raw)
    ref = ref_from_bytes(relative, raw, "application/x-ndjson")

    checked, streamed_bytes = _validate_ref_in_run(store, ref, output_root, pointer="/test/ref")
    assert checked == ref
    assert streamed_bytes == b""

    path.write_bytes(raw + b"tampered")
    with pytest.raises(ContractError) as error:
        _validate_ref_in_run(store, ref, output_root, pointer="/test/ref")
    assert error.value.code == "artifact_changed"


def test_fixed_launcher_rejects_invocation_replaced_after_lstat_before_open(tmp_path: Path, monkeypatch):
    target = tmp_path / "handoff" / "invocation.json"
    target.parent.mkdir(parents=True)
    original = b"original invocation bytes"
    target.write_bytes(original)
    code = _launcher_code(
        str(tmp_path),
        "handoff/invocation.json",
        hashlib.sha256(original).hexdigest(),
    )
    real_open = os.open
    replaced = False

    def replacing_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if not replaced and Path(path) == target:
            replacement = target.with_name("replacement.tmp")
            replacement.write_bytes(b"replacement invocation bytes")
            replacement.replace(target)
            replaced = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replacing_open)
    with pytest.raises(RuntimeError, match="opened object differs|path changed"):
        exec(code, {})
    assert replaced is True


def test_incomplete_worker_reconciliation_keeps_solver_invoked_without_a_result(monkeypatch):
    run = {
        "run_id": str(uuid.uuid4()),
        "prepared_id": str(uuid.uuid4()),
        "dispatch_nonce": str(uuid.uuid4()),
        "worker": {"status": "not_observed", "seal": None, "solver_invoked": None},
        "results": [],
    }
    seal_ref = {
        "path": ".reaslab/optagent/cases/c/runs/r/output/seal.json",
        "sha256": "a" * 64,
        "byte_len": 1,
        "media_type": "application/json",
    }
    monkeypatch.setattr(
        "optagent_core.runner._read_worker_seal",
        lambda store, case_id, current, prepared, manifest: (
            {"worker_completed": False, "batch_complete": False, "solver_invoked_observed": True, "output_refs": []},
            seal_ref,
            [],
        ),
    )

    reconciled, artifacts = _reconcile_run(object(), "case", run, {}, {})

    assert reconciled["worker"] == {"status": "incomplete", "seal": seal_ref, "solver_invoked": True}
    assert reconciled["results"] == []
    assert artifacts == [seal_ref]


def test_completed_worker_without_legacy_batch_summary_keeps_coverage_unknown(monkeypatch):
    run = {
        "run_id": str(uuid.uuid4()),
        "prepared_id": str(uuid.uuid4()),
        "dispatch_nonce": str(uuid.uuid4()),
        "transport": {"response": {"status": "accepted"}},
        "worker": {"status": "not_observed", "seal": None, "solver_invoked": None},
        "results": [],
    }
    seal_ref = {
        "path": ".reaslab/optagent/cases/c/runs/r/output/seal.json",
        "sha256": "a" * 64,
        "byte_len": 1,
        "media_type": "application/json",
    }
    monkeypatch.setattr(
        "optagent_core.runner._read_worker_seal",
        lambda store, case_id, current, prepared, manifest: (
            {
                "worker_completed": True,
                "batch_complete": None,
                "batch_summary": None,
                "solver_invoked_observed": False,
                "output_refs": [],
            },
            seal_ref,
            [],
        ),
    )

    reconciled, _ = _reconcile_run(object(), "case", run, {}, {})

    assert reconciled["worker"]["status"] == "complete"
    assert reconciled["_verified_batch_summary"] is None


def test_unresolved_local_run_blocks_blind_retry_and_allows_explicit_new_preparation(monkeypatch):
    from optagent_core import runner as runner_module

    case_id = str(uuid.uuid4())
    old_run_id = str(uuid.uuid4())
    old_prepared_id = str(uuid.uuid4())
    new_prepared_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    stamp = {
        "session_id": session_id,
        "role": "root",
        "turn_id": str(uuid.uuid4()),
        "call_id": str(uuid.uuid4()),
    }
    state = {
        "state_revision": 7,
        "owner_session_id": session_id,
        "source_revision": 1,
        "candidate_revision": 1,
        "candidate_id": str(uuid.uuid4()),
        "promotion_generation": 2,
        "prepared_ids": [old_prepared_id, new_prepared_id],
        "run_ids": [old_run_id],
    }
    old_run = {
        "run_id": old_run_id,
        "prepared_id": old_prepared_id,
        "dispatch_state": "unknown",
        "local_process": {"status": "cancelled"},
        "worker": {"status": "not_observed"},
        "platform": {"id_binding": "absent", "execution_id": None},
        "cancel": {"requested": True, "stop_ack": {"kind": "local_process_exit"}},
        "results": [],
    }
    run_ref = {
        "path": "run-record.json",
        "sha256": "a" * 64,
        "byte_len": 1,
        "media_type": "application/json",
    }

    class Store:
        workspace = Path(".")

        def read_case(self, requested_case_id):
            assert requested_case_id == case_id
            return state

    class PreparedReadReached(Exception):
        pass

    monkeypatch.setattr(runner_module, "_validate_owner_and_barrier", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner_module, "_latest_run_head", lambda *_args: (old_run_id, old_run, run_ref))
    args = {
        "case_id": case_id,
        "prepared_id": new_prepared_id,
        "expected_state_revision": state["state_revision"],
    }
    context = {"stamp": stamp, "cancellation_observed": False, "local_mode": True}

    with pytest.raises(ContractError) as caught:
        runner_module._begin_start(Store(), args, context)
    assert caught.value.code == "execution_unknown"
    assert old_prepared_id != new_prepared_id

    monkeypatch.setattr(
        runner_module,
        "_load_prepared",
        lambda *_args: (_ for _ in ()).throw(PreparedReadReached()),
    )
    continued = {
        **args,
        "supersedes_run_id": old_run_id,
        "attempt_reason": "The previous process tree was confirmed stopped; explicitly retry with a fresh preparation.",
    }
    with pytest.raises(PreparedReadReached):
        runner_module._begin_start(Store(), continued, context)


def test_run_summary_marks_local_timeout_confirmed_only_with_receipt_confirmation():
    run = {
        "run_id": str(uuid.uuid4()),
        "dispatch_state": "unknown",
        "worker": {"status": "not_observed"},
        "platform": {"id_binding": "absent", "execution_id": None},
        "cancel": {"requested": False, "stop_ack": None},
        "local_process": {"status": "timed_out", "stop_confirmed": True},
        "results": [],
    }
    summary = _run_summary(run, {"path": "run-record.json"})
    assert summary["dispatch_state"] == "unknown"
    assert summary["local_stop_state"] == "confirmed"
    assert summary["worker_status"] == "not_observed"


@pytest.mark.parametrize("status", ["timed_out", "cancelled", "interrupted"])
@pytest.mark.parametrize("stop_confirmed", [None, False])
def test_run_summary_does_not_infer_local_stop_confirmation_from_status(status, stop_confirmed):
    run = {
        "run_id": str(uuid.uuid4()),
        "dispatch_state": "unknown",
        "worker": {"status": "not_observed"},
        "platform": {"id_binding": "absent", "execution_id": None},
        "cancel": {"requested": False, "stop_ack": None},
        "local_process": {"status": status, "stop_confirmed": stop_confirmed},
        "results": [],
    }
    summary = _run_summary(run, {"path": "run-record.json"})
    assert summary["local_stop_state"] == "requested_not_confirmed"


@pytest.mark.parametrize(
    ("stop_reason", "timed_out", "error_code"),
    [
        ("timeout", True, "timeout"),
        ("cancel_requested", False, "cancelled"),
        ("keyboard_interrupt", False, "interrupted"),
    ],
)
def test_local_receipt_rejects_unconfirmed_stop_with_confirmed_error(
    stop_reason, timed_out, error_code, monkeypatch
):
    from optagent_core import runner as runner_module

    digest = "a" * 64
    receipt = {
        "schema_version": 2,
        "request_digest": digest,
        "observation": "spawned",
        "pid": 1234,
        "returncode": 1,
        "timed_out": timed_out,
        "stop_reason": stop_reason,
        "stop_confirmed": False,
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:00:01Z",
        "stdout_base64": "",
        "stderr_base64": "",
        "error_code": error_code,
    }

    class Store:
        def write_immutable(self, *_args, **_kwargs):
            raise AssertionError("invalid receipt must be rejected before persistence")

    monkeypatch.setattr(
        runner_module,
        "_check_bridge_identity",
        lambda *_args, **_kwargs: ("case", {}, {}, {}),
    )
    phase = {"receipt": receipt, "request_digest": digest}
    with pytest.raises(ContractError, match="unconfirmed local process stop"):
        runner_module._start_local_process_receipt(
            Store(), "case", {}, {}, phase, {"local_mode": True}
        )

