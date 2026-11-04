from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import zipfile
from types import SimpleNamespace

import pytest

from optagent_core import case_execution, worker
from optagent_core.data import load_data_bundle
from optagent_core.delivery import _EDITABLE_CASE_RUN_PY, _EDITABLE_SUPPORT_FILES
from optagent_core.results import ObjectiveSpec


_DIGEST = "a" * 64
_BINDING = "b" * 64
_COST = {"component": "cost", "sense": "min", "unit": "unit", "value": 6.0}


def _business_payload(objective_values=()):
    return {
        "status": "pass",
        "checked_obligations": [],
        "objective_values": [dict(row) for row in objective_values],
        "violations": [],
        "uncovered": [],
    }


def test_native_worker_extracts_then_checks_one_business_solution(tmp_path, monkeypatch):
    bundle = load_data_bundle(object(), None, candidate_binding_digest=_BINDING)
    model = object()
    extracted = {"plan": {"quantity": 3}}
    checked_solutions = []

    def frozen_call(_reader, _spec, _modules, *, function_name, args, **_kwargs):
        if function_name == "extract_solution":
            assert args[0] is bundle
            return extracted, ()
        if function_name == "check_solution":
            checked_solutions.append(args[1])
            assert args[0] is bundle
            return _business_payload([_COST]), ()
        pytest.fail(f"unexpected frozen call: {function_name}")

    monkeypatch.setattr(worker, "call_frozen_function", frozen_call)
    variable = {"component": "quantity", "key": [], "domain": "Reals", "value": 3.0, "origin": "solver_primal"}
    checked = SimpleNamespace(
        record={
            "input_digest": _DIGEST,
            "instance_key": [],
            "instance_binding_digest": _BINDING,
            "status": "pass",
            "variable_coverage": [],
            "component_coverage": [],
            "violations": None,
            "objective_values": [_COST],
            "tolerances": {"feasibility": 1e-7, "integrality": 1e-7},
            "uncovered": [],
        },
        witness_variables=(variable,),
        objective_values=(_COST,),
        candidate_complete=True,
    )
    monkeypatch.setattr(
        worker,
        "_write_violation_stream",
        lambda *_args, **_kwargs: (
            checked,
            {"path": "instances/000000/layer-single/violations.jsonl", "sha256": "e" * 64, "byte_len": 1, "media_type": "application/x-ndjson"},
        ),
    )
    output = worker._OutputTarget.create_local(tmp_path / "native-output")
    raw_ref = {
        "path": "instances/000000/layer-single/raw-primals.jsonl",
        "sha256": "c" * 64,
        "byte_len": 1,
        "media_type": "application/x-ndjson",
    }
    result, witness, complete, refs = worker._write_layer(
        reader=object(),
        output=output,
        stem="instances/000000/layer-single",
        model=model,
        model_spec={"original_path": "code/model.py"},
        instance_bundle=bundle,
        prepared={
            "input_digest": _DIGEST,
            "source_revision": "source-revision",
            "candidate_revision": "candidate-revision",
            "schema_version": 3,
            "solve_options": {
                "feasibility_tolerance": 1e-7,
                "integrality_tolerance": 1e-7,
                "relative_gap": 0.0,
                "absolute_gap": 0.0,
                "time_limit_seconds": None,
                "threads": 1,
                "random_seed": 0,
                "warm_start": False,
                "execution_budget_seconds": 60,
            },
        },
        objective=ObjectiveSpec("cost", "min", "unit", None, None),
        layer=None,
        observation=None,
        primals={},
        deterministic_termination="optimal",
        solver_invoked=True,
        warnings=(),
        checker_spec={"original_path": "code/checker.py"},
        module_specs=(),
        runtime_refs=(),
        backend_profile={"id": "highs"},
        backend_observation_ref=None,
        raw_primals_ref=raw_ref,
        model_consumed=(),
    )

    business = json.loads(output.path("instances/000000/layer-single/business-check.json").read_text())
    assert complete is True
    assert witness is not None
    assert result["has_solver_incumbent"] is False
    assert result["has_checked_candidate"] is True
    assert business["status"] == "pass"
    assert business["solution"] == {"plan": {"quantity": 3}}
    assert checked_solutions == [business["solution"]]
    assert refs["business_check"]["path"].endswith("business-check.json")


def test_direct_worker_extracts_then_checks_one_business_solution(tmp_path, monkeypatch):
    bundle = load_data_bundle(object(), None, candidate_binding_digest=_BINDING)
    extracted = {"plan": {"quantity": 3}}
    checked_solutions = []

    def frozen_call(_reader, _spec, _modules, *, function_name, args, **_kwargs):
        if function_name == "solve":
            return {
                "variables": [{"component": "quantity", "key": [], "domain": "NonNegativeReals", "value": 3.0}],
                "objective_values": [_COST],
            }, ()
        if function_name == "extract_solution":
            return extracted, ()
        if function_name == "check_solution":
            checked_solutions.append(args[1])
            return _business_payload([_COST]), ()
        pytest.fail(f"unexpected frozen call: {function_name}")

    monkeypatch.setattr(worker, "call_frozen_function", frozen_call)
    output = worker._OutputTarget.create_local(tmp_path / "direct-output")
    plan = {
        "mode": "scalar",
        "objective_component": "cost",
        "sense": "min",
        "unit": "unit",
        "preference_note": "direct business solution test",
    }
    manifest = {
        "candidate": {
            "model": {"original_path": "code/model.py"},
            "local_modules": [],
            "business_checker": {"original_path": "code/checker.py"},
            "objective_plan": plan,
        },
        "data": {"instance_parameter_object_id": None},
        "solve_options": {"execution_budget_seconds": 60},
        "source_revision": "source-revision",
        "candidate_revision": "candidate-revision",
    }

    records, _, _, _, _ = worker._run_direct_instances(
        object(),
        output,
        manifest,
        input_digest=_DIGEST,
        data_bundle=bundle,
        base_consumed_inputs=(),
        execution_started=0.0,
        backend_profile={"id": "direct_python"},
        enforce_execution_budget=False,
    )

    business = json.loads(output.path("instances/000000/layer-single/business-check.json").read_text())
    assert business["status"] == "pass"
    assert business["solution"] == {"plan": {"quantity": 3}}
    assert checked_solutions == [business["solution"]]
    assert records[0]["model_check"] is None
    assert records[0]["model_consumed"] == []


def test_exported_run_py_check_uses_saved_edited_s_without_witness_or_solve(tmp_path):
    (tmp_path / "code").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / ".optagent").mkdir()
    model_source = (
        "from pyomo.environ import ConcreteModel, Objective, Var, minimize\n"
        "def build_model(bundle):\n"
        "    raise RuntimeError('check must not rebuild a model without a saved Witness')\n"
    )
    checker_source = (
        "def check_solution(bundle, solution):\n"
        "    settings = bundle.json('settings')\n"
        "    quantity = solution['quantity']\n"
        "    failed = quantity < settings['minimum']\n"
        "    return {\n"
        "        'status': 'fail' if failed else 'pass',\n"
        "        'checked_obligations': [],\n"
        "        'objective_values': [{'component': 'cost', 'sense': 'min', 'unit': 'unit', 'value': quantity * settings['factor']}],\n"
        "        'violations': ['below_minimum'] if failed else [],\n"
        "        'uncovered': [],\n"
        "    }\n"
    )
    (tmp_path / "code" / "model.py").write_text(model_source, encoding="utf-8")
    (tmp_path / "code" / "checker.py").write_text(checker_source, encoding="utf-8")
    run = {
        "schema_version": 1,
        "model_path": "code/model.py",
        "local_modules": [],
        "business_checker_path": "code/checker.py",
        "inputs_path": "data/inputs.json",
        "instance_parameter_object_id": None,
        "backend_id": "highs",
        "objective_plan": {
            "mode": "scalar",
            "objective_component": "cost",
            "sense": "min",
            "unit": "unit",
            "preference_note": "saved solution only",
        },
        "solve_options": {
            "execution_budget_seconds": 60,
            "time_limit_seconds": None,
            "relative_gap": 0.0,
            "absolute_gap": 0.0,
            "threads": None,
            "random_seed": None,
            "feasibility_tolerance": 1e-6,
            "integrality_tolerance": 1e-6,
            "write_log": False,
            "export_model": False,
        },
    }
    inputs = {
        "schema_version": 1,
        "instance_parameter_object_id": None,
        "objects": [{
            "object_id": "settings",
            "kind": "json",
            "key_fields": [],
            "columns": [],
            "values": [{"minimum": 4, "factor": 2}],
        }],
    }
    (tmp_path / ".optagent" / "run.json").write_text(json.dumps(run), encoding="utf-8")
    (tmp_path / "data" / "inputs.json").write_text(json.dumps(inputs), encoding="utf-8")

    package_root = Path(case_execution.__file__).resolve().parent
    with zipfile.ZipFile(tmp_path / ".optagent" / "runtime.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in _EDITABLE_SUPPORT_FILES:
            archive.write(package_root / name, f"optagent_core/{name}")
    (tmp_path / "run.py").write_bytes(_EDITABLE_CASE_RUN_PY)

    edited_solution = {"quantity": 1}
    old_objective = {"component": "cost", "sense": "min", "unit": "unit", "value": 0.0}
    selected = {
        "schema_version": 2,
        "kind": "selected-run-solution",
        "status": "completed",
        "results": [{
            "result": {
                "instance_key": [],
                "input_digest": "c" * 64,
                "instance_binding_digest": "d" * 64,
                "layer": None,
                "backend": {"id": "highs"},
                "objective": old_objective,
                "bound": {"value": 0.0, "sense": "min"},
            },
            "witness": None,
            "model_check": None,
            "business_check": {"solution": edited_solution},
        }],
    }
    selected_path = tmp_path / "results" / "selected" / "solution.json"
    selected_path.parent.mkdir(parents=True)
    selected_path.write_text(json.dumps(selected), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-I", str(tmp_path / "run.py"), "check", "--solution", "results/selected/solution.json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert completed.returncode == 1, completed.stderr or completed.stdout
    report = json.loads((tmp_path / "results" / "check" / ".optagent" / "check.json").read_text(encoding="utf-8"))
    row = report["results"][0]
    assert row["saved_witness"] is None
    assert row["current_business_check"]["status"] == "fail"
    assert row["current_business_check"]["solution"] == edited_solution
    assert row["current_business_check"]["objective_values"][0]["value"] == 2
    assert row["current_business_check"]["input_digest"] != "c" * 64
    assert row["current_business_check"]["instance_binding_digest"] != "d" * 64
    assert row["saved_result_objective_matches_current_business"] is False
    assert row["scope"]["global_optimality"] == "not_checked"
    assert row["checks_passed"] is False
