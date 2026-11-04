from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

from optagent_core.contracts import ContractError
from optagent_core.data import load_local_data_bundle


def _column(name: str, kind: str, *, unit: str | None = None, nullable: bool = False) -> dict:
    return {"name": name, "type": kind, "unit": unit, "nullable": nullable}


def test_local_data_bundle_preserves_normalized_kinds_types_and_order():
    payload = {
        "schema_version": 1,
        "instance_parameter_object_id": "instances",
        "objects": [
            {
                "object_id": "instances",
                "kind": "table",
                "key_fields": ["id"],
                "columns": [_column("id", "integer"), _column("label", "string", nullable=True)],
                "values": [{"id": 7, "label": None}, {"id": 8, "label": "second"}],
            },
            {
                "object_id": "links",
                "kind": "relation",
                "key_fields": ["source", "target"],
                "columns": [_column("source", "string"), _column("target", "integer")],
                "values": [{"source": "a", "target": 3}, {"source": "b", "target": 1}],
            },
            {
                "object_id": "scale",
                "kind": "scalar",
                "key_fields": [],
                "columns": [_column("value", "number", unit="kg")],
                "values": [2.5],
            },
            {
                "object_id": "zones",
                "kind": "set",
                "key_fields": [],
                "columns": [_column("zone", "string")],
                "values": ["west", "east"],
            },
        ],
    }

    bundle = load_local_data_bundle(payload, candidate_binding_digest="a" * 64)

    assert bundle.object("instances").values_ref is None
    assert bundle.object("instances").source_locations == ()
    assert bundle.table("instances") == {
        (7,): {"id": 7, "label": None},
        (8,): {"id": 8, "label": "second"},
    }
    assert bundle.relation("links") == (("a", 3), ("b", 1))
    assert bundle.scalar("scale") == 2.5
    assert bundle.set("zones") == ("west", "east")
    assert bundle.object("scale").columns[0]["unit"] == "kg"
    assert bundle.for_instance("instances", (7,)).instance_context.parameters["label"] is None


def test_direct_local_check_checks_saved_business_solution_without_calling_solve_or_model_builder(monkeypatch):
    from optagent_core import case_execution
    from optagent_core import worker
    from optagent_core.data import load_data_bundle
    from optagent_core.results import direct_witness_from_output

    plan = {
        "mode": "scalar",
        "objective_component": "cost",
        "sense": "min",
        "unit": "USD",
        "preference_note": "saved-point fixture",
    }
    bundle = load_data_bundle(object(), None, candidate_binding_digest="b" * 64)
    context = bundle.for_instance(None, ()).instance_context
    assert context is not None
    old_digest = "a" * 64
    current_digest = "c" * 64
    witness = direct_witness_from_output(
        {
            "variables": [{"component": "flow", "key": [1], "domain": "NonNegativeReals", "value": 3}],
            "objective_values": [{"component": "cost", "sense": "min", "unit": "USD", "value": 6}],
        },
        plan,
        input_digest=old_digest,
        instance_key=(),
        instance_binding_digest=context.instance_binding_digest,
    )
    assert witness is not None
    result = {
        "instance_key": [],
        "layer": None,
        "backend": {"id": "direct_python"},
        "input_digest": old_digest,
        "instance_binding_digest": context.instance_binding_digest,
        "objective": {"component": "cost", "sense": "min", "unit": "USD", "value": 6},
    }
    saved_business_solution = {"flow": 3.0}
    solution = {
        "kind": "local-solve",
        "status": "completed",
        "results": [{
            "result": result,
            "witness": witness,
            "model_check": None,
            "business_check": {"input_digest": old_digest, "solution": saved_business_solution},
        }],
    }
    snapshot = {
        "run_config": {
            "backend_id": "direct_python",
            "objective_plan": plan,
            "solve_options": {"feasibility_tolerance": 1e-7, "integrality_tolerance": 1e-7},
            "instance_parameter_object_id": None,
        },
        "data_bundle": bundle,
        "reader": object(),
        "checker_spec": {"original_path": "code/solution_check.py", "bytes": {}},
        "module_specs": [],
        "input_digest": current_digest,
    }

    def unexpected_solve(*_args, **_kwargs):
        pytest.fail("local direct check must reuse the saved point and must not solve or build a native model")

    def check_saved_solution(_reader, _checker_spec, _module_specs, *, function_name, args, pointer):
        assert function_name == "check_solution"
        assert pointer == "/candidate/business_checker"
        assert args[1] is saved_business_solution
        return ({
            "status": "pass",
            "checked_obligations": [],
            "objective_values": [{"component": "cost", "sense": "min", "unit": "USD", "value": 6.0}],
            "violations": [],
            "uncovered": [],
        }, ())

    monkeypatch.setattr(case_execution, "build_native_model", unexpected_solve)
    monkeypatch.setattr(worker, "call_frozen_function", check_saved_solution)

    rows = case_execution._check_rows(snapshot, solution)

    assert rows[0]["checks_passed"] is True
    assert rows[0]["current_model_check"] is None
    assert rows[0]["current_business_check"]["status"] == "pass"
    assert rows[0]["current_business_check"]["solution"] is saved_business_solution
    assert rows[0]["current_input_digest"] == current_digest
    assert rows[0]["current_business_check"]["input_digest"] == current_digest
    assert rows[0]["saved_objective_matches_current"] is True


def test_native_local_check_rechecks_saved_point_without_import_shadow_error(tmp_path):
    from optagent_core.case_execution import _capture_case, _check_rows

    _local_case(tmp_path)
    run_path = tmp_path / ".optagent" / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["backend_id"] = "cbc"
    run_path.write_text(json.dumps(run), encoding="utf-8")
    snapshot = _capture_case(tmp_path)
    bundle = snapshot["data_bundle"].for_instance(None, ())
    assert bundle.instance_context is not None
    input_digest = snapshot["input_digest"]
    binding_digest = bundle.instance_context.instance_binding_digest
    objective = {"component": "cost", "sense": "min", "unit": "unit", "value": 6.0}
    solution = {
        "schema_version": 2,
        "kind": "selected-run-solution",
        "status": "completed",
        "results": [{
            "result": {
                "instance_key": [],
                "instance_binding_digest": binding_digest,
                "input_digest": input_digest,
                "layer": None,
                "backend": {"id": "cbc"},
                "objective": objective,
            },
            "witness": {
                "input_digest": input_digest,
                "instance_key": [],
                "instance_binding_digest": binding_digest,
                "variables": [{
                    "component": "x",
                    "key": [],
                    "domain": "Reals",
                    "value": 3.0,
                    "origin": "solver_primal",
                }],
                "objective_values": [objective],
            },
            "model_check": None,
            "business_check": None,
        }],
    }

    rows = _check_rows(snapshot, solution)

    assert rows[0]["checks_passed"] is True
    assert rows[0]["current_model_check"]["status"] == "pass"
    assert rows[0]["saved_objective_matches_current"] is False
    assert rows[0]["current_objective_values"] == []
    assert rows[0]["current_model_check"]["objective_values"] == [objective]
    assert rows[0]["current_business_check"]["status"] == "uncovered"
    assert rows[0]["current_business_check"]["uncovered"] == ["business_checker_not_declared"]


def test_native_cp_sat_local_check_does_not_solve_or_claim_global_recheck(tmp_path, monkeypatch):
    pytest.importorskip("ortools.sat.python.cp_model")
    from optagent_core import case_execution
    from optagent_core.case_execution import _capture_case, _check_rows, _make_check_report

    _local_case(tmp_path)
    run_path = tmp_path / ".optagent" / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["backend_id"] = "cp_sat"
    run_path.write_text(json.dumps(run), encoding="utf-8")
    (tmp_path / "code" / "model.py").write_text(
        "from ortools.sat.python import cp_model\n"
        "def build_model(bundle):\n"
        "    model = cp_model.CpModel()\n"
        "    x = model.NewIntVar(0, 1, 'x')\n"
        "    model.Minimize(1000000000 + x)\n"
        "    return {'model': model, 'variables': {'x': {(): x}}}\n"
        "def extract_solution(bundle, witness):\n"
        "    raise AssertionError('local native check must not re-extract a solution')\n",
        encoding="utf-8",
    )
    snapshot = _capture_case(tmp_path)
    context = snapshot["data_bundle"].for_instance(None, ()).instance_context
    assert context is not None
    objective = {"component": "cost", "sense": "min", "unit": "unit", "value": 1000000000.0}
    result = {
        "instance_key": [],
        "layer": None,
        "backend": {"id": "cp_sat"},
        "input_digest": snapshot["input_digest"],
        "instance_binding_digest": context.instance_binding_digest,
        "objective": objective,
    }
    witness = {
        "input_digest": snapshot["input_digest"],
        "instance_key": [],
        "instance_binding_digest": context.instance_binding_digest,
        "variables": [{"component": "x", "key": [], "domain": "integer", "value": 0.0, "origin": "solver_primal"}],
        "objective_values": [objective],
    }
    solution = {
        "kind": "selected-run-solution",
        "status": "complete",
        "results": [{
            "result": result,
            "witness": witness,
            "model_check": {"method": "core_cp_sat_response"},
            "business_check": None,
        }],
    }

    def unexpected_solve(*_args, **_kwargs):
        pytest.fail("native local check must not run a solver")

    monkeypatch.setattr(case_execution, "_run_instances", unexpected_solve)

    rows = _check_rows(snapshot, solution)
    report = _make_check_report(snapshot, solution, started_at=0.0)

    assert rows[0]["checks_passed"] is False
    assert rows[0]["validation_status"] == "uncovered"
    assert rows[0]["scope"]["current_base_model_feasibility"] == "native_cp_sat_global_constraints_not_rechecked"
    assert rows[0]["current_model_check"] is None
    assert rows[0]["saved_result_objective_matches_witness"] is True
    assert "CP-SAT global constraints and global optimality are not rechecked" in report["scope"]


@pytest.mark.parametrize(
    ("failure_stage", "expected_scope_status", "expected_code"),
    [
        ("rebuild", "not_checked_current_model_rebuild_failed", "invalid_argument"),
        ("check", "not_checked_current_model_check_failed", "current_model_check_error"),
    ],
)
def test_current_model_failure_scope_reports_incomplete_check(
    tmp_path, monkeypatch, failure_stage, expected_scope_status, expected_code
):
    from optagent_core import case_execution
    from optagent_core.case_execution import _capture_case, _make_check_report

    _local_case(tmp_path)
    snapshot = _capture_case(tmp_path)
    context = snapshot["data_bundle"].for_instance(None, ()).instance_context
    assert context is not None
    objective = {"component": "cost", "sense": "min", "unit": "unit", "value": 6.0}
    solution = {
        "schema_version": 2,
        "kind": "selected-run-solution",
        "status": "completed",
        "results": [{
            "result": {
                "instance_key": [],
                "backend": {"id": "highs"},
                "input_digest": snapshot["input_digest"],
                "instance_binding_digest": context.instance_binding_digest,
                "layer": None,
                "objective": objective,
            },
            "witness": {
                "input_digest": snapshot["input_digest"],
                "instance_key": [],
                "instance_binding_digest": context.instance_binding_digest,
                "variables": [{
                    "component": "x",
                    "key": [],
                    "domain": "Reals",
                    "value": 3.0,
                    "origin": "solver_primal",
                }],
                "objective_values": [objective],
            },
            "model_check": None,
            "business_check": None,
        }],
    }

    def unexpected_solve(*_args, **_kwargs):
        pytest.fail("checking a saved witness must not start a solver")

    monkeypatch.setattr(case_execution, "_run_instances", unexpected_solve)
    if failure_stage == "rebuild":
        def fail_to_rebuild(*_args, **_kwargs):
            raise ContractError(
                "invalid_argument",
                "simulated current model rebuild rejection",
                pointer="/candidate/model",
            )

        monkeypatch.setattr(case_execution, "build_native_model", fail_to_rebuild)
    else:
        monkeypatch.setattr(case_execution, "build_native_model", lambda *_args, **_kwargs: (object(), []))
        monkeypatch.setattr(case_execution, "objective_for_layer", lambda *_args: None)
        monkeypatch.setattr(case_execution, "_saved_primals", lambda *_args, **_kwargs: {})

        def fail_to_check(*_args, **_kwargs):
            raise RuntimeError("simulated current model check exception")

        monkeypatch.setattr(case_execution, "check_pyomo_model", fail_to_check)

    report = _make_check_report(snapshot, solution, started_at=0.0)
    row = report["results"][0]

    assert row["saved_result_objective_matches_witness"] is True
    assert row["checks_passed"] is False
    assert row["validation_status"] == "uncovered"
    assert row["scope"]["current_base_model_feasibility"] == expected_scope_status
    assert row["current_model_check"] is None
    assert row["current_model_check_diagnostic"]["stage"] == failure_stage
    assert row["current_model_check_diagnostic"]["code"] == expected_code
    assert report["checks_passed"] is False
    assert report["validation_status"] == "uncovered"
    assert "current base-model feasibility was not checked for one or more results" in report["scope"]
    assert "global optimality not checked; no solve" in report["scope"]


@pytest.mark.parametrize(
    "values,columns,kind,key_fields",
    [
        ([{"id": 1}, {"id": 1}], [_column("id", "integer")], "table", ["id"]),
        ([{"id": True}], [_column("id", "integer")], "table", ["id"]),
        ([1.0, 2.0], [_column("value", "number")], "scalar", []),
    ],
)
def test_local_data_bundle_uses_shared_duplicate_and_typed_value_validation(values, columns, kind, key_fields):
    payload = {
        "schema_version": 1,
        "instance_parameter_object_id": None,
        "objects": [
            {
                "object_id": "value",
                "kind": kind,
                "key_fields": key_fields,
                "columns": columns,
                "values": values,
            }
        ],
    }
    with pytest.raises(ContractError):
        load_local_data_bundle(payload)


def test_local_data_bundle_reports_key_field_not_declared_as_column():
    payload = {
        "schema_version": 1,
        "instance_parameter_object_id": None,
        "objects": [
            {
                "object_id": "instances",
                "kind": "table",
                "key_fields": ["missing_id"],
                "columns": [_column("id", "integer")],
                "values": [{"id": 7}],
            }
        ],
    }

    with pytest.raises(ContractError) as exc_info:
        load_local_data_bundle(payload)

    assert exc_info.value.pointer == "/objects/0/key_fields"
    assert exc_info.value.code == "output_invalid"


def _write_runtime_zip(root: Path, *, zip_import_marker: bool = False) -> None:
    package_root = Path(__file__).resolve().parents[1] / "optagent_core"
    target = root / ".optagent" / "runtime.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(package_root.rglob("*.py")):
            relative = source.relative_to(package_root.parent).as_posix()
            raw = source.read_bytes()
            if zip_import_marker and source.name == "case_execution.py":
                marker_code = (
                    b"\nimport os as _optagent_test_os\n"
                    b"_optagent_test_marker = _optagent_test_os.environ.get('OPTAGENT_TEST_ZIP_IMPORT_MARKER')\n"
                    b"if _optagent_test_marker:\n"
                    b"    with open(_optagent_test_marker, 'w', encoding='utf-8') as _optagent_test_stream:\n"
                    b"        _optagent_test_stream.write(__file__)\n"
                )
                raw = raw.replace(b'\nif __name__ == "__main__":', marker_code + b'\nif __name__ == "__main__":')
            archive.writestr(relative, raw)


def _local_case(root: Path, *, zip_import_marker: bool = False) -> None:
    (root / "code").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / ".optagent").mkdir(parents=True, exist_ok=True)
    (root / "results" / "selected").mkdir(parents=True, exist_ok=True)
    (root / "code" / "model.py").write_text(
        "from pyomo.environ import ConcreteModel, Objective, Var, minimize\n"
        "def build_model(bundle):\n"
        "    model = ConcreteModel()\n"
        "    model.x = Var(bounds=(0, 10))\n"
        "    model.cost = Objective(expr=bundle.scalar('scale') * model.x, sense=minimize)\n"
        "    return model\n"
        "def extract_solution(bundle, witness):\n"
        "    return {'x': next(row['value'] for row in witness['variables'] if row['component'] == 'x')}\n",
        encoding="utf-8",
    )
    options = {
        "execution_budget_seconds": 120,
        "time_limit_seconds": None,
        "relative_gap": 0.0,
        "absolute_gap": 0.0,
        "threads": None,
        "random_seed": None,
        "feasibility_tolerance": 1e-6,
        "integrality_tolerance": 1e-6,
        "write_log": False,
        "export_model": False,
    }
    run = {
        "schema_version": 1,
        "model_path": "code/model.py",
        "local_modules": [],
        "business_checker_path": None,
        "inputs_path": "data/inputs.json",
        "instance_parameter_object_id": None,
        "backend_id": "highs",
        "objective_plan": {
            "mode": "scalar",
            "objective_component": "cost",
            "sense": "min",
            "unit": "unit",
            "preference_note": "test",
        },
        "solve_options": options,
    }
    inputs = {
        "schema_version": 1,
        "instance_parameter_object_id": None,
        "objects": [
            {
                "object_id": "scale",
                "kind": "scalar",
                "key_fields": [],
                "columns": [_column("value", "number", unit="unit")],
                "values": [2.0],
            }
        ],
    }
    (root / ".optagent" / "run.json").write_text(json.dumps(run), encoding="utf-8")
    (root / "data" / "inputs.json").write_text(json.dumps(inputs), encoding="utf-8")
    _write_runtime_zip(root, zip_import_marker=zip_import_marker)


def _write_solution(root: Path, value: dict, relative: str = "results/selected/solution.json") -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value), encoding="utf-8")
    return target


def test_capture_maps_physical_paths_to_logical_import_tree_and_reads_edits(tmp_path):
    from optagent_core.case_execution import _capture_case
    from optagent_core.models import build_native_model

    _local_case(tmp_path)
    assert not (tmp_path / "results" / "local").exists()
    assert not (tmp_path / "results" / "check").exists()
    model_source = (
        "from pyomo.environ import ConcreteModel, Objective, Var, minimize\n"
        "from ...models.helper import VALUE\n"
        "def build_model(bundle):\n"
        "    model = ConcreteModel()\n"
        "    model.x = Var(bounds=(0, VALUE + 1))\n"
        "    model.cost = Objective(expr=model.x, sense=minimize)\n"
        "    return model\n"
    )
    (tmp_path / "code" / "model.py").write_text(model_source, encoding="utf-8")
    helper = tmp_path / "code" / "models" / "helper.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("VALUE = 9\n", encoding="utf-8")
    run_path = tmp_path / ".optagent" / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["local_modules"] = ["code/models/helper.py"]
    run["code_import_paths"] = {
        "code/model.py": "cases/title/model.py",
        "code/models/helper.py": "models/helper.py",
    }
    run_path.write_text(json.dumps(run), encoding="utf-8")

    first = _capture_case(tmp_path)
    first_model, _ = build_native_model(
        first["reader"], first["data_bundle"], first["model_spec"], first["module_specs"]
    )
    assert first["model_spec"]["original_path"] == "cases/title/model.py"
    assert first["module_specs"][0]["original_path"] == "models/helper.py"
    assert first["model_spec"]["bytes"]["path"].endswith("/code/code/model.py")
    assert first_model.x.ub == 10
    assert not (tmp_path / "cases" / "title" / "model.py").exists()

    edited_source = model_source.replace("VALUE + 1", "VALUE + 2")
    (tmp_path / "code" / "model.py").write_text(edited_source, encoding="utf-8")
    second = _capture_case(tmp_path)
    second_model, _ = build_native_model(
        second["reader"], second["data_bundle"], second["model_spec"], second["module_specs"]
    )

    assert second["input_digest"] != first["input_digest"]
    assert second["reader"].read_ref(second["model_spec"]["bytes"]) == edited_source.encode("utf-8")
    assert second_model.x.ub == 11


@pytest.mark.parametrize("import_direction", ["builder_uses_checker", "checker_uses_model"])
def test_local_check_supports_cross_role_helper_imports(tmp_path, import_direction):
    from optagent_core.case_execution import _capture_case, check_case

    _local_case(tmp_path)
    run_path = tmp_path / ".optagent" / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["business_checker_path"] = "code/checker.py"

    if import_direction == "builder_uses_checker":
        model_source = (
            "from pyomo.environ import ConcreteModel, Objective, Var, minimize\n"
            "def build_model(bundle):\n"
            "    from .checker import FACTOR\n"
            "    model = ConcreteModel()\n"
            "    model.x = Var(bounds=(3, 10))\n"
            "    model.cost = Objective(expr=FACTOR * bundle.scalar('scale') * model.x, sense=minimize)\n"
            "    return model\n"
            "def extract_solution(bundle, witness):\n"
            "    from .checker import FACTOR\n"
            "    value = next(row['value'] for row in witness['variables'] if row['component'] == 'x')\n"
            "    return {'factor': FACTOR, 'x': value}\n"
        )
        checker_source = (
            "FACTOR = 3\n"
            "def check_solution(bundle, solution):\n"
            "    value = FACTOR * bundle.scalar('scale') * solution['x']\n"
            "    return {\n"
            "        'status': 'pass' if solution['factor'] == FACTOR else 'fail',\n"
            "        'checked_obligations': [],\n"
            "        'objective_values': [{'component': 'cost', 'sense': 'min', 'unit': 'unit', 'value': value}],\n"
            "        'violations': [],\n"
            "        'uncovered': [],\n"
            "    }\n"
        )
        run["local_modules"] = ["code/checker.py"]
        expected_factor = 3
    else:
        model_source = (
            "from pyomo.environ import ConcreteModel, Objective, Var, minimize\n"
            "MODEL_FACTOR = 4\n"
            "def build_model(bundle):\n"
            "    model = ConcreteModel()\n"
            "    model.x = Var(bounds=(3, 10))\n"
            "    model.cost = Objective(expr=MODEL_FACTOR * bundle.scalar('scale') * model.x, sense=minimize)\n"
            "    return model\n"
            "def extract_solution(bundle, witness):\n"
            "    value = next(row['value'] for row in witness['variables'] if row['component'] == 'x')\n"
            "    return {'factor': MODEL_FACTOR, 'x': value}\n"
        )
        checker_source = (
            "def check_solution(bundle, solution):\n"
            "    from .model import MODEL_FACTOR\n"
            "    value = MODEL_FACTOR * bundle.scalar('scale') * solution['x']\n"
            "    return {\n"
            "        'status': 'pass' if solution['factor'] == MODEL_FACTOR else 'fail',\n"
            "        'checked_obligations': [],\n"
            "        'objective_values': [{'component': 'cost', 'sense': 'min', 'unit': 'unit', 'value': value}],\n"
            "        'violations': [],\n"
            "        'uncovered': [],\n"
            "    }\n"
        )
        run["local_modules"] = ["code/model.py"]
        expected_factor = 4

    (tmp_path / "code" / "model.py").write_text(model_source, encoding="utf-8")
    (tmp_path / "code" / "checker.py").write_text(checker_source, encoding="utf-8")
    run_path.write_text(json.dumps(run), encoding="utf-8")

    snapshot = _capture_case(tmp_path)
    if import_direction == "builder_uses_checker":
        assert snapshot["module_specs"][0] == snapshot["checker_spec"]
    else:
        assert snapshot["module_specs"][0] == snapshot["model_spec"]
    binding_digest = snapshot["data_bundle"].candidate_binding_digest
    objective = {"component": "cost", "sense": "min", "unit": "unit", "value": expected_factor * 2 * 3}
    _write_solution(tmp_path, {
        "schema_version": 2,
        "kind": "selected-run-solution",
        "status": "completed",
        "results": [{
            "result": {
                "instance_key": [],
                "instance_binding_digest": binding_digest,
                "input_digest": snapshot["input_digest"],
                "layer": None,
                "objective": objective,
            },
            "witness": {
                "input_digest": snapshot["input_digest"],
                "instance_key": [],
                "instance_binding_digest": binding_digest,
                "variables": [{"component": "x", "key": [], "domain": "Reals", "value": 3.0, "origin": "solver_primal"}],
                "objective_values": [objective],
            },
            "model_check": None,
            "business_check": {"solution": {"factor": expected_factor, "x": 3.0}},
        }],
    })

    checked = check_case(tmp_path)

    assert checked["checks_passed"] is True, checked
    assert checked["results"][0]["current_business_check"]["solution"] == {"factor": expected_factor, "x": 3.0}


def test_local_run_freezes_optional_warm_start_and_rejects_non_boolean_values(tmp_path):
    from optagent_core.case_execution import _capture_case, _prepared_for_local

    _local_case(tmp_path)
    snapshot = _capture_case(tmp_path)
    assert snapshot["run_config"]["solve_options"]["warm_start"] is False
    assert _prepared_for_local(snapshot)["solve_options"]["warm_start"] is False

    run_path = tmp_path / ".optagent" / "run.json"
    original = json.loads(run_path.read_text(encoding="utf-8"))
    explicit_true = dict(original)
    explicit_true["solve_options"] = {**original["solve_options"], "warm_start": True}
    run_path.write_text(json.dumps(explicit_true), encoding="utf-8")
    explicit_snapshot = _capture_case(tmp_path)
    assert explicit_snapshot["run_config"]["solve_options"]["warm_start"] is True
    assert _prepared_for_local(explicit_snapshot)["solve_options"]["warm_start"] is True

    for invalid in (0, 1, None, "true"):
        run = dict(original)
        run["solve_options"] = {**original["solve_options"], "warm_start": invalid}
        run_path.write_text(json.dumps(run), encoding="utf-8")
        with pytest.raises(ContractError) as caught:
            _capture_case(tmp_path)
        assert caught.value.code == "invalid_argument"


def test_collect_solution_rows_reads_original_child_model_and_business_check_refs(tmp_path):
    from optagent_core.case_execution import _collect_solution_rows
    from optagent_core.contracts import ref_from_bytes

    output_root = tmp_path / "attempt" / "artifacts"
    output_root.mkdir(parents=True)
    values = {
        "result": {"instance_key": ["east"], "layer": 0, "raw_status": "optimal"},
        "witness": {"instance_key": ["east"], "variables": []},
        "model-check": {"status": "pass", "coverage": {"constraints": 3}},
        "business-check": {
            "status": "pass",
            "checked_obligations": [{"label": "安排", "source_locations": [], "evidence": "实际方案为东区 3 单位"}],
        },
    }
    refs = {}
    for name, value in values.items():
        relative = f"instances/000000/layer-0000/{name}.json"
        target = output_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(value).encode("utf-8")
        target.write_bytes(raw)
        refs[name.replace("-", "_")] = ref_from_bytes(relative, raw, "application/json")

    rows = _collect_solution_rows(output_root, [refs])

    assert rows == [{
        "result": values["result"],
        "witness": values["witness"],
        "model_check": values["model-check"],
        "business_check": values["business-check"],
    }]


def test_solve_summary_reuses_existing_business_evidence_and_real_log_paths(tmp_path):
    from optagent_core.case_execution import _solve_summary_markdown

    stage = tmp_path / "stage"
    log = stage / ".optagent" / "artifacts" / "instances" / "000000" / "layer-single" / "solver.log"
    log.parent.mkdir(parents=True)
    log.write_text("solver log", encoding="utf-8")
    execution = {
        "status": "partial",
        "backend_id": "highs",
        "batch_summary": {"stop_reason": "budget"},
        "result_count": 1,
        "solution_path": "solution.json",
    }
    solution = {"results": [{
            "result": {"instance_key": ["east"], "layer": None, "raw_status": "feasible"},
            "witness": {"objective_values": [{"component": "cost", "sense": "min", "unit": "kg", "value": 7.0}]},
            "model_check": {"status": "pass"},
            "business_check": {
                "status": "pass",
                "checked_obligations": [{"label": "真实业务方案", "source_locations": [], "evidence": "东区安排 7 kg"}],
            },
        }]}
    (stage / "solution.json").write_text(json.dumps(solution), encoding="utf-8")

    summary = _solve_summary_markdown(execution, stage)

    assert "本次尝试状态：`partial`" in summary
    assert "cost / min = 7.0 kg" in summary
    assert "东区安排 7 kg" in summary
    assert ".optagent/artifacts/instances/000000/layer-single/solver.log" in summary


@pytest.mark.parametrize("status", ["completed", "partial"])
def test_solve_stores_full_rows_once_and_preserves_raw_refs_and_partial_ipc(tmp_path, monkeypatch, status):
    from hashlib import sha256

    from optagent_core.case_execution import solve_case
    from optagent_core.contracts import ref_from_bytes

    _local_case(tmp_path)
    (tmp_path / "code" / "model.py").write_text(
        "from pyomo.environ import ConcreteModel, Objective, RangeSet, Var, minimize\n"
        "def build_model(bundle):\n"
        "    model = ConcreteModel()\n"
        "    model.rows = RangeSet(0, 2499)\n"
        "    model.x = Var(model.rows, bounds=(0, 10))\n"
        "    model.cost = Objective(expr=sum(model.x[i] for i in model.rows), sense=minimize)\n"
        "    return model\n",
        encoding="utf-8",
    )
    artifact_payloads = {
        "raw-primals": [
            {"component": "x", "key": [index], "value": 3.0 if index == 0 else 0.0}
            for index in range(2500)
        ],
        "witness": {
            "input_digest": "b" * 64,
            "instance_key": [],
            "instance_binding_digest": "c" * 64,
            "variables": [
                {"component": "x", "key": [index], "domain": "Reals", "value": 3.0 if index == 0 else 0.0, "origin": "solver_primal"}
                for index in range(2500)
            ],
            "objective_values": [{"component": "cost", "sense": "min", "unit": "unit", "value": 3.0}],
        },
        "model-check": {"status": "pass"},
        "business-check": {
            "status": "pass",
            "checked_obligations": [{"label": "方案", "source_locations": [], "evidence": "已安排 3 unit"}],
        },
    }
    artifact_root = tmp_path / "fake-child-artifacts"
    refs = {}
    for name, payload in artifact_payloads.items():
        extension = ".jsonl" if name == "raw-primals" else ".json"
        relative = f"instances/000000/layer-single/{name}{extension}"
        target = artifact_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = (
            b"".join(json.dumps(row, separators=(",", ":")).encode("utf-8") + b"\n" for row in payload)
            if name == "raw-primals"
            else json.dumps(payload).encode("utf-8")
        )
        target.write_bytes(raw)
        media_type = "application/x-ndjson" if name == "raw-primals" else "application/json"
        refs[name.replace("-", "_")] = ref_from_bytes(relative, raw, media_type)
    observation_payload = {"observation": {"raw_status": "feasible", "termination": "time_limit"}, "raw_primals": refs["raw_primals"]}
    observation_path = "instances/000000/layer-single/backend-observation.json"
    observation_raw = json.dumps(observation_payload).encode("utf-8")
    (artifact_root / observation_path).write_bytes(observation_raw)
    refs["backend_observation"] = ref_from_bytes(observation_path, observation_raw, "application/json")
    result_payload = {
        "instance_key": [],
        "layer": None,
        "raw_status": "feasible",
        "termination": "time_limit",
        "objective": {"component": "cost", "sense": "min", "unit": "unit", "value": 3.0},
        "bound": {
            "value": 0.0,
            "direction": "lower",
            "scope": "global",
            "evidence": refs["backend_observation"],
        },
        "witness": refs["witness"],
        "check": refs["model_check"],
    }
    result_path = "instances/000000/layer-single/result.json"
    result_raw = json.dumps(result_payload).encode("utf-8")
    (artifact_root / result_path).write_bytes(result_raw)
    refs["result"] = ref_from_bytes(result_path, result_raw, "application/json")
    refs_in_worker_order = [
        refs["result"],
        refs["model_check"],
        refs["business_check"],
        refs["witness"],
        refs["raw_primals"],
        refs["backend_observation"],
    ]
    instance_refs = {
        "result": refs["result"],
        "witness": refs["witness"],
        "model_check": refs["model_check"],
        "business_check": refs["business_check"],
    }
    child_report = {
        "schema_version": 1,
        "kind": "local-computation",
        "status": status,
        "input_digest": "b" * 64,
        "backend_id": "highs",
        "backend_profile": {"backend_id": "highs"},
        "solve_options": {},
        "execution_elapsed_seconds": 0.1,
        "instances": [instance_refs],
        "batch_summary": {"stop_reason": "budget"} if status == "partial" else None,
        "consumed_inputs": [],
        "output_refs": refs_in_worker_order,
        "solver_invoked": True,
        "solver_returned": True,
    }

    def fake_spawn(_case_dir, child_artifact_root, _run_snapshot, **_kwargs):
        child_root = Path(child_artifact_root)
        shutil.copytree(artifact_root, child_root)
        (child_root / "child-report.json").write_text(json.dumps(child_report), encoding="utf-8")
        return 0, False

    monkeypatch.setattr("optagent_core.case_execution._spawn_case_child", fake_spawn)

    result = solve_case(tmp_path)

    execution_path = tmp_path / "results" / "local" / ".optagent" / "execution.json"
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    solution = json.loads((tmp_path / "results" / "local" / "solution.json").read_text(encoding="utf-8"))
    summary = (tmp_path / "results" / "local" / "summary.md").read_text(encoding="utf-8")
    published_artifacts = tmp_path / "results" / "local" / ".optagent" / "artifacts"
    ipc_path = published_artifacts / "child-report.json"
    assert result["result_count"] == execution["result_count"] == 1
    assert result["solution_path"] == execution["solution_path"] == "solution.json"
    assert "results" not in result and "results" not in execution
    assert len(solution["results"]) == 1
    assert solution["results"][0]["result"] == result_payload
    assert len(solution["results"][0]["witness"]["variables"]) == 2500
    assert "本次保存结果数：1" in summary
    assert "cost / min = 3.0 unit" in summary
    assert "已安排 3 unit" in summary
    assert ipc_path.exists() is (status == "partial")
    assert not (tmp_path / "results" / "local" / "execution.json").exists()
    assert not (tmp_path / "results" / "local" / "artifacts").exists()
    assert not (tmp_path / "results" / "local" / "status.json").exists()
    for reference in execution["output_refs"]:
        raw = (published_artifacts / reference["path"]).read_bytes()
        assert len(raw) == reference["byte_len"]
        assert sha256(raw).hexdigest() == reference["sha256"]


def test_local_json_input_survives_solve_check_and_runtime_zip(tmp_path):
    from optagent_core.case_execution import _capture_case, check_case, solve_case

    _local_case(tmp_path)
    payload = {"factor": 2, "minimum": 3, "payload": "Q" * 5000}
    (tmp_path / "data" / "inputs.json").write_text(json.dumps({
        "schema_version": 1,
        "instance_parameter_object_id": None,
        "objects": [{
            "object_id": "settings",
            "kind": "json",
            "key_fields": [],
            "columns": [],
            "values": [payload],
        }],
    }), encoding="utf-8")
    run_path = tmp_path / ".optagent" / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["backend_id"] = "direct_python"
    run["objective_plan"] = {
        "mode": "scalar",
        "objective_component": "cost",
        "sense": "min",
        "unit": "unit",
        "preference_note": "local path mapping regression",
    }
    run["business_checker_path"] = "code/checker.py"
    run["local_modules"] = ["code/shared.py"]
    run["code_import_paths"] = {
        "code/model.py": "case-family v1.2/model.py",
        "code/checker.py": "case-family v1.2/checker.py",
        "code/shared.py": "case-family v1.2/solver_helper.py",
    }
    run_path.write_text(json.dumps(run), encoding="utf-8")
    (tmp_path / "code" / "model.py").write_text(
        "from .solver_helper import solve_point\n"
        "def solve(bundle):\n"
        "    return solve_point(bundle)\n"
        "def extract_solution(bundle, witness):\n"
        "    value = next(row['value'] for row in witness['variables'] if row['component'] == 'x')\n"
        "    settings = bundle.json('settings')\n"
        "    return {'x': value, 'payload': settings['payload']}\n",
        encoding="utf-8",
    )
    (tmp_path / "code" / "checker.py").write_text(
        "from .solver_helper import checked_value\n"
        "def check_solution(bundle, solution):\n"
        "    settings = bundle.json('settings')\n"
        "    value = solution['x']\n"
        "    expected_cost = checked_value(bundle, value)\n"
        "    return {\n"
        "        'status': 'pass' if value >= settings['minimum'] and solution['payload'] == settings['payload'] else 'fail',\n"
        "        'checked_obligations': [],\n"
        "        'objective_values': [{'component': 'cost', 'sense': 'min', 'unit': 'unit', 'value': expected_cost}],\n"
        "        'violations': [],\n"
        "        'uncovered': [],\n"
        "    }\n",
        encoding="utf-8",
    )
    (tmp_path / "code" / "shared.py").write_text(
        "def checked_value(bundle, value):\n"
        "    return bundle.json('settings')['factor'] * value\n"
        "def solve_point(bundle):\n"
        "    settings = bundle.json('settings')\n"
        "    value = settings['minimum']\n"
        "    return {\n"
        "        'variables': [{'component': 'x', 'key': [], 'domain': 'Reals', 'value': value}],\n"
        "        'objective_values': [{'component': 'cost', 'sense': 'min', 'unit': 'unit', 'value': checked_value(bundle, value)}],\n"
        "    }\n",
        encoding="utf-8",
    )
    _write_runtime_zip(tmp_path)

    captured = _capture_case(tmp_path)
    assert captured["model_spec"]["original_path"] == "case-family v1.2/model.py"
    assert captured["checker_spec"]["original_path"] == "case-family v1.2/checker.py"
    assert captured["module_specs"][0]["original_path"] == "case-family v1.2/solver_helper.py"

    execution = solve_case(tmp_path)

    assert execution["status"] == "completed", execution
    saved = json.loads((tmp_path / "results" / "local" / "solution.json").read_text(encoding="utf-8"))
    business_solution = saved["results"][0]["business_check"]["solution"]
    assert business_solution["x"] == pytest.approx(3)
    assert business_solution["payload"] == "Q" * 5000
    assert "Q" * 5000 not in (tmp_path / "results" / "local" / "summary.md").read_text(encoding="utf-8")

    checked = check_case(tmp_path)

    assert checked["checks_passed"] is True, checked
    check_record = json.loads((tmp_path / "results" / "check" / ".optagent" / "check.json").read_text(encoding="utf-8"))
    current_business_solution = check_record["results"][0]["current_business_check"]["solution"]
    assert current_business_solution == business_solution
    assert check_record["results"][0]["current_business_check"]["status"] == "pass"
    assert check_record["results"][0]["current_objective_values"][0]["value"] == pytest.approx(6)
    assert "Q" * 5000 not in (tmp_path / "results" / "check" / "summary.md").read_text(encoding="utf-8")

    from optagent_core.delivery import _EDITABLE_CASE_RUN_PY

    (tmp_path / "run.py").write_bytes(_EDITABLE_CASE_RUN_PY)
    archive_path = tmp_path.parent / "editable-case.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in tmp_path.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(tmp_path))
    unzipped_case = tmp_path.parent / "unzipped-case"
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert ".optagent/run.json" in names
        assert ".optagent/runtime.zip" in names
        archive.extractall(unzipped_case)

    edited_inputs_path = unzipped_case / "data" / "inputs.json"
    edited_inputs = json.loads(edited_inputs_path.read_text(encoding="utf-8"))
    edited_inputs["objects"][0]["values"][0]["minimum"] = 4
    edited_inputs_path.write_text(json.dumps(edited_inputs), encoding="utf-8")
    edited_shared_path = unzipped_case / "code" / "shared.py"
    edited_shared = edited_shared_path.read_text(encoding="utf-8").replace(
        "value = settings['minimum']", "value = settings['minimum'] + 1"
    )
    edited_shared_path.write_text(edited_shared, encoding="utf-8")

    zip_solve = subprocess.run(
        [sys.executable, "-I", str(unzipped_case / "run.py"), "solve"],
        cwd=unzipped_case,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert zip_solve.returncode == 0, zip_solve.stderr + zip_solve.stdout
    zip_solve_summary = json.loads(zip_solve.stdout)
    assert str(unzipped_case / "results" / "local" / "solution.json") in zip_solve_summary["result_files"]
    assert str(unzipped_case / "results" / "local" / "summary.md") in zip_solve_summary["result_files"]
    zip_solution = json.loads((unzipped_case / "results" / "local" / "solution.json").read_text(encoding="utf-8"))
    assert zip_solution["results"][0]["business_check"]["solution"]["x"] == pytest.approx(5)

    zip_check = subprocess.run(
        [sys.executable, "-I", str(unzipped_case / "run.py"), "check"],
        cwd=unzipped_case,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert zip_check.returncode == 0, zip_check.stderr + zip_check.stdout
    zip_check_summary = json.loads(zip_check.stdout)
    assert str(unzipped_case / "results" / "check" / "summary.md") in zip_check_summary["result_files"]
    assert (unzipped_case / "results" / "local" / ".optagent" / "execution.json").is_file()
    assert (unzipped_case / "results" / "check" / ".optagent" / "check.json").is_file()


def test_check_summary_links_real_logs_from_the_current_stage(tmp_path):
    from optagent_core.case_execution import _check_summary_markdown

    stage = tmp_path / "results" / "check"
    log = stage / ".optagent" / "artifacts" / "current-check.log"
    log.parent.mkdir(parents=True)
    log.write_text("current check detail", encoding="utf-8")
    report = {
        "status": "completed",
        "validation_status": "failed",
        "checks_passed": False,
        "source_solution_status": "completed",
        "provided_result_count": 1,
        "current_plan_coverage": {
            "expected_identity_count": 1,
            "matched_provided_identity_count": 1,
            "missing_count": 0,
            "missing": [],
        },
        "results": [],
    }

    summary = _check_summary_markdown(report, stage)

    assert "本次检查日志" in summary
    assert ".optagent/artifacts/current-check.log" in summary


def test_completed_failed_validation_check_folds_and_removes_child_ipc(tmp_path, monkeypatch):
    from optagent_core.case_execution import check_case

    _local_case(tmp_path)
    saved_result = {
        "instance_key": [],
        "layer": None,
        "objective": {"component": "cost", "sense": "min", "unit": "unit", "value": 3.0},
    }
    saved_witness = {
        "input_digest": "b" * 64,
        "instance_key": [],
        "instance_binding_digest": "c" * 64,
        "variables": [{"component": "x", "key": [], "domain": "Reals", "value": 3.0, "origin": "solver_primal"}],
        "objective_values": [{"component": "cost", "sense": "min", "unit": "unit", "value": 3.0}],
    }
    _write_solution(tmp_path, {
        "schema_version": 2,
        "kind": "selected-run-solution",
        "status": "completed",
        "results": [{"result": saved_result, "witness": saved_witness, "model_check": None, "business_check": None}],
    })
    child_report = {
        "schema_version": 1,
        "kind": "local-check",
        "status": "completed",
        "validation_status": "failed",
        "checks_passed": False,
        "scope": "current model and configured business obligations",
        "source_solution_status": "completed",
        "provided_result_count": 1,
        "current_plan_coverage": {
            "expected_identity_count": 1,
            "matched_provided_identity_count": 1,
            "missing_count": 0,
            "missing": [],
        },
        "results": [{
            "saved_result": saved_result,
            "saved_witness": saved_witness,
            "saved_objective_values": saved_witness["objective_values"],
            "current_objective_values": saved_witness["objective_values"],
            "validation_status": "failed",
            "checks_passed": False,
            "current_model_check": {"status": "fail"},
            "current_business_check": {"status": "fail", "checked_obligations": []},
            "current_violations": [],
        }],
    }

    def fake_spawn(_case_dir, child_artifact_root, _run_snapshot, **_kwargs):
        child_root = Path(child_artifact_root)
        child_root.mkdir(parents=True)
        (child_root / "child-check.json").write_text(json.dumps(child_report), encoding="utf-8")
        return 0, False

    monkeypatch.setattr("optagent_core.case_execution._spawn_case_child", fake_spawn)

    report = check_case(tmp_path)

    check_dir = tmp_path / "results" / "check"
    persisted = json.loads((check_dir / ".optagent" / "check.json").read_text(encoding="utf-8"))
    assert report["status"] == "completed"
    assert report["validation_status"] == "failed"
    assert persisted["results"][0]["saved_result"] == saved_result
    assert persisted["results"][0]["saved_witness"] == saved_witness
    assert not (check_dir / ".optagent" / "artifacts" / "child-check.json").exists()
    assert not (check_dir / "check.json").exists()
    assert not (check_dir / "artifacts").exists()
    assert not (check_dir / "status.json").exists()


def test_recovery_keeps_only_checks_written_in_this_attempt(tmp_path):
    from optagent_core.case_execution import _recover_solution_rows
    from optagent_core.contracts import ref_from_bytes

    output_root = tmp_path / "current" / "artifacts"
    row_dir = output_root / "instances" / "000000" / "layer-single"
    row_dir.mkdir(parents=True)
    stale_dir = tmp_path / "results" / "local" / ".optagent" / "artifacts" / "instances" / "000000" / "layer-single"
    stale_dir.mkdir(parents=True)
    (stale_dir / "business-check.json").write_text('{"status":"pass"}', encoding="utf-8")

    witness = {"instance_key": [1], "variables": [], "objective_values": []}
    witness_raw = json.dumps(witness).encode("utf-8")
    (row_dir / "witness.json").write_bytes(witness_raw)
    (row_dir / "business-check.json").write_text(json.dumps({
        "instance_key": [True],
        "input_digest": "a" * 64,
        "instance_binding_digest": "b" * 64,
        "status": "pass",
    }), encoding="utf-8")
    result = {
        "instance_key": [1],
        "layer": None,
        "input_digest": "a" * 64,
        "instance_binding_digest": "b" * 64,
        "witness": ref_from_bytes("instances/000000/layer-single/witness.json", witness_raw, "application/json"),
        "check": ref_from_bytes("instances/000000/layer-single/model-check.json", b"{}", "application/json"),
    }
    (row_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")

    rows = _recover_solution_rows(output_root)

    assert rows == [{
        "result": result,
        "witness": witness,
        "model_check": None,
        "business_check": None,
    }]


def test_case_copy_and_rename_uses_its_own_hidden_runtime_and_result_paths(tmp_path):
    from optagent_core.case_execution import check_case

    source = tmp_path / "original-case"
    copied = tmp_path / "renamed-case"
    source.mkdir()
    _local_case(source)
    (source / "code" / "model.py").write_text(
        "from pyomo.environ import ConcreteModel, Objective, Var, minimize\n"
        "from ...models.helper import VALUE\n"
        "def build_model(bundle):\n"
        "    model = ConcreteModel()\n"
        "    model.x = Var(bounds=(0, VALUE + 1))\n"
        "    model.cost = Objective(expr=model.x, sense=minimize)\n"
        "    return model\n",
        encoding="utf-8",
    )
    (source / "code" / "models").mkdir()
    (source / "code" / "models" / "helper.py").write_text("VALUE = 9\n", encoding="utf-8")
    run_path = source / ".optagent" / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["local_modules"] = ["code/models/helper.py"]
    run["code_import_paths"] = {
        "code/model.py": "cases/title/model.py",
        "code/models/helper.py": "models/helper.py",
    }
    run_path.write_text(json.dumps(run), encoding="utf-8")
    _write_solution(source, {
        "schema_version": 2,
        "kind": "selected-run-solution",
        "status": "completed",
        "results": [{
            "result": {
                "instance_key": [],
                "instance_binding_digest": "c" * 64,
                "input_digest": "b" * 64,
                "layer": None,
                "objective": {"component": "cost", "sense": "min", "unit": "unit", "value": 3.0},
            },
            "witness": {
                "input_digest": "b" * 64,
                "instance_key": [],
                "instance_binding_digest": "c" * 64,
                "variables": [{"component": "x", "key": [], "domain": "Reals", "value": 3.0, "origin": "solver_primal"}],
                "objective_values": [{"component": "cost", "sense": "min", "unit": "unit", "value": 3.0}],
            },
            "model_check": None,
            "business_check": None,
        }],
    })
    shutil.copytree(source, copied)

    report = check_case(copied)

    assert report["status"] == "completed"
    assert report["source_solution_status"] == "completed"
    assert report["validation_status"] == "passed_model_only"
    assert report["results"][0]["current_model_check"]["status"] == "pass"
    assert report["results"][0]["current_objective_values"] == []
    assert report["results"][0]["current_model_check"]["objective_values"] == [
        {"component": "cost", "sense": "min", "unit": "unit", "value": 3.0}
    ]
    assert report["results"][0]["current_business_check"]["status"] == "uncovered"
    assert report["results"][0]["current_business_check"]["uncovered"] == ["business_checker_not_declared"]
    assert (copied / "results" / "check" / ".optagent" / "check.json").is_file()
    assert (copied / "results" / "check" / "summary.md").is_file()
    assert not (source / "results" / "check").exists()


def test_child_process_imports_case_runtime_zip(tmp_path, monkeypatch):
    from optagent_core.case_execution import check_case

    _local_case(tmp_path, zip_import_marker=True)
    marker = tmp_path / "zip-import-marker.txt"
    monkeypatch.setenv("OPTAGENT_TEST_ZIP_IMPORT_MARKER", str(marker))

    report = check_case(tmp_path)

    assert report["status"] == "completed"
    assert "runtime.zip" in marker.read_text(encoding="utf-8")


def test_check_case_rechecks_saved_point_without_solver_and_recomputes_objective(tmp_path, monkeypatch):
    from optagent_core import case_execution, solver_protocol

    _local_case(tmp_path)
    inputs_path = tmp_path / "data" / "inputs.json"
    edited_inputs = json.loads(inputs_path.read_text(encoding="utf-8"))
    edited_inputs["objects"][0]["values"] = [4.0]
    inputs_path.write_text(json.dumps(edited_inputs), encoding="utf-8")
    saved_digest = "b" * 64
    saved_binding = "c" * 64
    solution = {
        "schema_version": 2,
        "kind": "selected-run-solution",
        "status": "completed",
        "results": [
            {
                "result": {
                    "instance_key": [],
                    "instance_binding_digest": saved_binding,
                    "input_digest": saved_digest,
                    "layer": None,
                    "objective": {"component": "cost", "sense": "min", "unit": "unit", "value": 3.0},
                },
                "witness": {
                    "input_digest": saved_digest,
                    "instance_key": [],
                    "instance_binding_digest": saved_binding,
                    "variables": [
                        {"component": "x", "key": [], "domain": "Reals", "value": 3.0, "origin": "solver_primal"}
                    ],
                    "objective_values": [
                        {"component": "cost", "sense": "min", "unit": "unit", "value": 3.0}
                    ],
                },
                "model_check": {"status": "pass", "uncovered": ["tampered historical record"]},
                "business_check": {
                    "status": "pass",
                    "checked_obligations": [{"label": "historical", "source_locations": [], "evidence": "tampered historical pass"}],
                },
            }
        ],
    }
    selected_path = _write_solution(tmp_path, solution)
    local_solution_path = _write_solution(tmp_path, solution, "results/local/solution.json")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("check-only must not initialize or call a solver")

    monkeypatch.setattr(solver_protocol, "backend_profile_for", forbidden)
    monkeypatch.setattr(solver_protocol, "run_solver_once", forbidden)
    monkeypatch.setattr(case_execution, "_run_instances", forbidden)

    snapshot = case_execution._capture_case(tmp_path)
    direct_rows = case_execution._check_rows(snapshot, solution)
    assert direct_rows[0]["checks_passed"] is True
    assert direct_rows[0]["saved_objective_matches_current"] is False
    assert direct_rows[0]["current_objective_values"] == []
    assert direct_rows[0]["current_model_check"]["objective_values"][0]["value"] == 12.0

    tampered_solution = json.loads(json.dumps(solution))
    tampered_solution["results"][0]["result"]["objective"]["value"] = 99.0
    tampered_rows = case_execution._check_rows(snapshot, tampered_solution)
    assert tampered_rows[0]["saved_result_objective_matches_witness"] is False
    assert tampered_rows[0]["checks_passed"] is False

    report = case_execution.check_case(tmp_path)

    assert report["kind"] == "local-check"
    assert report["status"] == "completed"
    assert report["validation_status"] == "passed_model_only"
    assert report["checks_passed"] is True
    row = report["results"][0]
    assert row["current_model_check"]["status"] == "pass"
    assert row["saved_objective_matches_current"] is False
    assert row["current_objective_values"] == []
    assert row["current_model_check"]["objective_values"] == [
        {"component": "cost", "sense": "min", "unit": "unit", "value": 12.0}
    ]
    assert row["scope"]["historical_lexicographic_locks"] == "not_checked"
    assert row["scope"]["current_business_obligations"] == "uncovered_not_configured"
    assert row["current_business_check"]["uncovered"] == ["business_checker_not_declared"]
    assert "solution" not in row["current_business_check"]
    edited_solution_without_checker = json.loads(json.dumps(solution))
    edited_solution_without_checker["results"][0]["business_check"]["solution"] = {"x": 3.0}
    edited_s_row = case_execution._check_rows(snapshot, edited_solution_without_checker)[0]
    assert edited_s_row["validation_status"] == "uncovered"
    assert edited_s_row["checks_passed"] is False
    assert edited_s_row["current_model_check"]["status"] == "pass"
    assert edited_s_row["current_business_check"]["status"] == "uncovered"
    assert edited_s_row["current_business_check"]["solution"] == {"x": 3.0}
    assert edited_s_row["current_objective_values"] == []
    assert edited_s_row["current_model_check"]["objective_values"][0]["value"] == 12.0
    assert selected_path.read_text(encoding="utf-8") == json.dumps(solution)
    check_path = tmp_path / "results" / "check" / ".optagent" / "check.json"
    assert check_path.is_file()
    check_record = json.loads(check_path.read_text(encoding="utf-8"))
    assert check_record["source_solution_path"] == "results/local/solution.json"
    assert check_record["results"][0]["saved_result"] == solution["results"][0]["result"]
    assert check_record["results"][0]["saved_witness"] == solution["results"][0]["witness"]
    local_solution_path.write_text(
        json.dumps({"schema_version": 2, "kind": "local-computation", "status": "failed", "results": []}),
        encoding="utf-8",
    )
    preserved_check = json.loads(check_path.read_text(encoding="utf-8"))
    assert preserved_check["results"][0]["saved_result"] == solution["results"][0]["result"]
    assert preserved_check["results"][0]["saved_witness"] == solution["results"][0]["witness"]
    assert not (tmp_path / "results" / "check" / ".optagent" / "artifacts" / "child-check.json").exists()
    summary_text = (tmp_path / "results" / "check" / "summary.md").read_text(encoding="utf-8")
    assert "已按本次输入和代码对可用保存见证进行逐点模型核对；实际目标核对见各点记录" in summary_text
    assert "当前重算目标：未提供" in summary_text
    assert "当前重算目标：cost / min = 12.0 unit" not in summary_text
    assert "tampered historical pass" not in summary_text
    assert not (tmp_path / "local-check-results").exists()


def test_check_case_exposes_controlled_business_checker_contract_error(tmp_path, monkeypatch):
    from optagent_core import case_execution
    from optagent_core import worker
    from optagent_core.case_execution import check_case

    _local_case(tmp_path)
    run_path = tmp_path / ".optagent" / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["business_checker_path"] = "code/checker.py"
    run_path.write_text(json.dumps(run), encoding="utf-8")
    (tmp_path / "code" / "checker.py").write_text(
        "def check_solution(bundle, solution):\n"
        "    return {'evidence': 'PRIVATE checker return value'}\n",
        encoding="utf-8",
    )
    objective = {"component": "cost", "sense": "min", "unit": "unit", "value": 6.0}
    solution = {
        "schema_version": 2,
        "kind": "selected-run-solution",
        "status": "completed",
        "results": [{
            "result": {
                "instance_key": [],
                "instance_binding_digest": "c" * 64,
                "input_digest": "b" * 64,
                "layer": None,
                "objective": objective,
            },
            "witness": {
                "input_digest": "b" * 64,
                "instance_key": [],
                "instance_binding_digest": "c" * 64,
                "variables": [{"component": "x", "key": [], "domain": "Reals", "value": 3.0, "origin": "solver_primal"}],
                "objective_values": [objective],
            },
            "model_check": None,
            "business_check": {"solution": {"x": 3.0}},
        }],
    }
    _write_solution(tmp_path, solution)

    report = check_case(tmp_path, solution_path="results/selected/solution.json")

    assert report["status"] == "completed"
    assert report["validation_status"] == "uncovered"
    row = report["results"][0]
    assert row["current_business_check"]["status"] == "uncovered"
    assert row["current_business_check"]["uncovered"] == ["business_checker_error"]
    assert row["current_business_checker_diagnostic"] == {
        "code": "output_invalid",
        "message": "business checker returned an invalid BusinessCheck",
        "pointer": "/candidate/business_checker/evidence",
    }
    check_record = json.loads((tmp_path / "results" / "check" / ".optagent" / "check.json").read_text(encoding="utf-8"))
    summary = (tmp_path / "results" / "check" / "summary.md").read_text(encoding="utf-8")
    assert check_record["results"][0]["current_business_checker_diagnostic"] == row["current_business_checker_diagnostic"]
    assert "business checker returned an invalid BusinessCheck" in summary
    assert "/candidate/business_checker" in summary
    assert "PRIVATE checker return value" not in json.dumps(check_record)
    assert "PRIVATE checker return value" not in summary

    snapshot = case_execution._capture_case(tmp_path)

    def unknown_checker_error(*_args, **_kwargs):
        raise RuntimeError("PRIVATE unknown checker exception")

    monkeypatch.setattr(worker, "call_frozen_function", unknown_checker_error)
    unknown_row = case_execution._check_rows(snapshot, solution)[0]
    assert unknown_row["current_business_check"]["uncovered"] == ["business_checker_error"]
    assert unknown_row["current_business_checker_diagnostic"] == {
        "code": "business_checker_error",
        "error_type": "RuntimeError",
    }
    assert "PRIVATE unknown checker exception" not in json.dumps(unknown_row)


def test_check_reports_current_constraint_violation_despite_historical_pass(tmp_path):
    from optagent_core.case_execution import check_case

    _local_case(tmp_path)
    (tmp_path / "code" / "model.py").write_text(
        "from pyomo.environ import ConcreteModel, Constraint, Objective, Var, minimize\n"
        "def build_model(bundle):\n"
        "    model = ConcreteModel()\n"
        "    model.x = Var(bounds=(0, 10))\n"
        "    model.maximum_x = Constraint(expr=model.x <= 2)\n"
        "    model.cost = Objective(expr=bundle.scalar('scale') * model.x, sense=minimize)\n"
        "    return model\n",
        encoding="utf-8",
    )
    selected = {
        "schema_version": 2,
        "kind": "selected-run-solution",
        "status": "completed",
        "results": [{
            "result": {
                "instance_key": [],
                "instance_binding_digest": "c" * 64,
                "input_digest": "b" * 64,
                "layer": None,
                "objective": {"component": "cost", "sense": "min", "unit": "unit", "value": 6.0},
            },
            "witness": {
                "input_digest": "b" * 64,
                "instance_key": [],
                "instance_binding_digest": "c" * 64,
                "variables": [{"component": "x", "key": [], "domain": "Reals", "value": 3.0, "origin": "solver_primal"}],
                "objective_values": [{"component": "cost", "sense": "min", "unit": "unit", "value": 6.0}],
            },
            "model_check": {"status": "pass", "violations": []},
            "business_check": {"status": "pass", "checked_obligations": []},
        }],
    }
    selected_path = _write_solution(tmp_path, selected)

    report = check_case(tmp_path)

    assert report["checks_passed"] is False
    assert report["validation_status"] == "failed"
    row = report["results"][0]
    assert row["current_model_check"]["status"] == "fail"
    assert row["current_violations"] == [{
        "kind": "constraint",
        "component": "maximum_x",
        "key": [],
        "lower": None,
        "body": 3.0,
        "upper": 2.0,
        "violation": 1.0,
        "error": "constraint_violation",
    }]
    saved_report = json.loads((tmp_path / "results" / "check" / ".optagent" / "check.json").read_text(encoding="utf-8"))
    assert saved_report["results"][0]["current_violations"] == row["current_violations"]
    summary = (tmp_path / "results" / "check" / "summary.md").read_text(encoding="utf-8")
    assert "当前模型实际违例" in summary
    assert "约束 `maximum_x`" in summary
    assert "constraint_violation" in summary
    assert "本次值 `3.0`" in summary and "上界 `2.0`" in summary and "违反量 `1.0`" in summary
    assert selected_path.read_text(encoding="utf-8") == json.dumps(selected)


@pytest.mark.parametrize(
    "objective_change",
    ["sense_and_unit", "remove_objective", "old_layer_unavailable", "old_scalar_unavailable"],
)
def test_check_case_keeps_saved_point_when_current_objective_identity_changes(tmp_path, objective_change):
    from optagent_core.case_execution import check_case

    _local_case(tmp_path)
    run_path = tmp_path / ".optagent" / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    if objective_change == "sense_and_unit":
        run["objective_plan"]["sense"] = "max"
        run["objective_plan"]["unit"] = "updated_unit"
        (tmp_path / "code" / "model.py").write_text(
            "from pyomo.environ import ConcreteModel, Objective, Var, maximize\n"
            "def build_model(bundle):\n"
            "    model = ConcreteModel()\n"
            "    model.x = Var(bounds=(0, 10))\n"
            "    model.cost = Objective(expr=2 * model.x, sense=maximize)\n"
            "    return model\n",
            encoding="utf-8",
        )
    elif objective_change == "remove_objective":
        run["objective_plan"] = {"mode": "feasibility", "preference_note": "current feasibility check"}
    elif objective_change == "old_scalar_unavailable":
        run["objective_plan"] = {
            "mode": "lexicographic",
            "layers": [
                {"objective_component": "cost", "sense": "min", "unit": "unit", "absolute_tolerance": 0.0, "relative_tolerance": 0.0},
                {"objective_component": "cost", "sense": "max", "unit": "unit", "absolute_tolerance": 0.0, "relative_tolerance": 0.0},
            ],
            "preference_note": "current lexicographic plan",
        }
    run_path.write_text(json.dumps(run), encoding="utf-8")

    saved_digest = "b" * 64
    saved_binding = "c" * 64
    solution = {
        "schema_version": 2,
        "kind": "selected-run-solution",
        "status": "completed",
        "results": [
            {
                "result": {
                    "instance_key": [],
                    "instance_binding_digest": saved_binding,
                    "input_digest": saved_digest,
                    "layer": 1 if objective_change == "old_layer_unavailable" else None,
                    "objective": None if objective_change == "old_scalar_unavailable" else {"component": "cost", "sense": "min", "unit": "unit", "value": 6.0},
                },
                "witness": {
                    "input_digest": saved_digest,
                    "instance_key": [],
                    "instance_binding_digest": saved_binding,
                    "variables": [
                        {"component": "x", "key": [], "domain": "Reals", "value": 3.0, "origin": "solver_primal"}
                    ],
                    "objective_values": [] if objective_change == "old_scalar_unavailable" else [
                        {"component": "cost", "sense": "min", "unit": "unit", "value": 6.0}
                    ],
                },
                "model_check": {"status": "pass"},
                "business_check": {"status": "pass"},
            }
        ],
    }
    _write_solution(tmp_path, solution)

    report = check_case(tmp_path)

    assert report["status"] == "completed"
    row = report["results"][0]
    assert row["saved_result_objective_matches_witness"] is True
    assert row["saved_objective_matches_current"] is False
    if objective_change in {"old_layer_unavailable", "old_scalar_unavailable"}:
        assert report["validation_status"] == "uncovered"
        assert report["checks_passed"] is False
        assert row["current_model_check"]["status"] == "pass"
        assert row["scope"]["current_layer_objective"] == "unavailable_not_comparable"
        summary = (tmp_path / "results" / "check" / "summary.md").read_text(encoding="utf-8")
        assert "已按本次输入和代码对可用保存见证进行逐点模型核对；实际目标核对见各点记录" in summary
        assert "当前重算目标：未提供" in summary
    else:
        assert report["checks_passed"] is True
    if objective_change == "sense_and_unit":
        assert row["current_objective_values"] == []
        assert row["current_model_check"]["objective_values"] == [
            {"component": "cost", "sense": "max", "unit": "updated_unit", "value": 6.0}
        ]
    else:
        assert row["current_objective_values"] == []


def test_check_defaults_to_latest_local_attempt_and_does_not_fall_back_to_root_solution(tmp_path):
    from optagent_core.case_execution import check_case

    _local_case(tmp_path)
    root_solution = {
        "schema_version": 2,
        "kind": "selected-run-solution",
        "status": "completed",
        "results": [],
    }
    latest_solution = {
        "schema_version": 2,
        "kind": "local-computation",
        "status": "failed",
        "results": [],
    }
    _write_solution(tmp_path, root_solution)
    _write_solution(tmp_path, latest_solution, "results/local/solution.json")

    report = check_case(tmp_path)

    assert report["status"] == "completed"
    assert report["validation_status"] == "uncovered"
    assert report["checks_passed"] is False
    assert report["source_solution_kind"] == "local-computation"
    assert report["source_solution_status"] == "failed"
    assert report["results"] == []


def test_check_reports_no_saved_point_when_latest_attempt_has_no_solution_file(tmp_path):
    from optagent_core.case_execution import check_case

    _local_case(tmp_path)
    _write_solution(
        tmp_path,
        {"schema_version": 2, "kind": "selected-run-solution", "status": "completed", "results": []},
    )
    (tmp_path / "results" / "local").mkdir()

    report = check_case(tmp_path)

    assert report["status"] == "completed"
    assert report["validation_status"] == "uncovered"
    assert report["checks_passed"] is False
    assert report["source_solution_status"] == "no_saved_point"
    assert report["results"] == []
    summary = (tmp_path / "results" / "check" / "summary.md").read_text(encoding="utf-8")
    assert "本次没有可用保存结果点，未执行逐点模型重算" in summary
    assert "已按本次输入和代码对可用保存见证进行逐点模型核对；实际目标核对见各点记录" not in summary


def test_latest_failed_solve_replaces_green_summary_without_falling_back_to_selected(tmp_path, monkeypatch):
    from optagent_core.case_execution import _default_solution_path, solve_case

    _local_case(tmp_path)
    selected = {"schema_version": 2, "kind": "selected-run-solution", "status": "completed", "results": []}
    selected_path = _write_solution(tmp_path, selected)
    local_dir = tmp_path / "results" / "local"
    local_dir.mkdir()
    (local_dir / "summary.md").write_text("# old green success\n", encoding="utf-8")
    _write_solution(
        tmp_path,
        {"schema_version": 2, "kind": "local-computation", "status": "completed", "results": []},
        "results/local/solution.json",
    )

    monkeypatch.setattr("optagent_core.case_execution._spawn_case_child", lambda *_args, **_kwargs: (9, False))
    result = solve_case(tmp_path)

    assert result["status"] == "failed"
    latest = json.loads((local_dir / "solution.json").read_text(encoding="utf-8"))
    summary = (local_dir / "summary.md").read_text(encoding="utf-8")
    assert latest == {"schema_version": 2, "kind": "local-computation", "status": "failed", "results": []}
    assert "本次尝试状态：`failed`" in summary
    assert "old green success" not in summary
    assert (local_dir / ".optagent" / "execution.json").is_file()
    execution = json.loads((local_dir / ".optagent" / "execution.json").read_text(encoding="utf-8"))
    assert execution["result_count"] == 0
    assert execution["solution_path"] == "solution.json"
    assert "results" not in execution
    assert not (local_dir / "status.json").exists()
    assert _default_solution_path(tmp_path) == "results/local/solution.json"
    assert selected_path.read_text(encoding="utf-8") == json.dumps(selected)


def test_check_keeps_legal_subset_pass_and_reports_missing_typed_instance_layers(tmp_path):
    from optagent_core.case_execution import _capture_case, _check_rows, _make_check_report

    _local_case(tmp_path)
    run_path = tmp_path / ".optagent" / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["instance_parameter_object_id"] = "instances"
    run["objective_plan"] = {
        "mode": "lexicographic",
        "layers": [
            {"objective_component": "cost", "sense": "min", "unit": "unit", "absolute_tolerance": 0.0, "relative_tolerance": 0.0},
            {"objective_component": "cost", "sense": "min", "unit": "unit", "absolute_tolerance": 0.0, "relative_tolerance": 0.0},
        ],
        "preference_note": "coverage contract",
    }
    run_path.write_text(json.dumps(run), encoding="utf-8")
    inputs_path = tmp_path / "data" / "inputs.json"
    inputs = json.loads(inputs_path.read_text(encoding="utf-8"))
    inputs["instance_parameter_object_id"] = "instances"
    inputs["objects"].insert(0, {
        "object_id": "instances",
        "kind": "table",
        "key_fields": ["id", "zone"],
        "columns": [_column("id", "integer"), _column("zone", "string")],
        "values": [{"id": 1, "zone": "east"}, {"id": 2, "zone": "west"}],
    })
    inputs_path.write_text(json.dumps(inputs), encoding="utf-8")
    saved_digest = "b" * 64
    saved_binding = "c" * 64
    solution = {
        "schema_version": 2,
        "kind": "selected-run-solution",
        "status": "completed",
        "results": [{
            "result": {
                "instance_key": [1, "east"],
                "instance_binding_digest": saved_binding,
                "input_digest": saved_digest,
                "layer": 0,
                "objective": {"component": "cost", "sense": "min", "unit": "unit", "value": 6.0},
            },
            "witness": {
                "input_digest": saved_digest,
                "instance_key": [1, "east"],
                "instance_binding_digest": saved_binding,
                "variables": [{"component": "x", "key": [], "domain": "Reals", "value": 3.0, "origin": "solver_primal"}],
                "objective_values": [{"component": "cost", "sense": "min", "unit": "unit", "value": 6.0}],
            },
            "model_check": {"status": "pass"},
            "business_check": {"status": "pass"},
        }],
    }
    snapshot = _capture_case(tmp_path)

    checked_rows = _check_rows(snapshot, solution)
    report = _make_check_report(snapshot, solution, started_at=0.0)

    assert checked_rows[0]["checks_passed"] is True
    assert report["checks_passed"] is True
    assert report["validation_status"] == "passed_model_only"
    assert report["provided_result_count"] == 1
    assert report["current_plan_coverage"] == {
        "expected_identity_count": 4,
        "matched_provided_identity_count": 1,
        "missing_count": 3,
        "missing": [
            {"instance_key": [1, "east"], "layer": 1},
            {"instance_key": [2, "west"], "layer": 0},
            {"instance_key": [2, "west"], "layer": 1},
        ],
    }
    summary = __import__("optagent_core.case_execution", fromlist=["_check_summary_markdown"])._check_summary_markdown(report)
    assert "提供的 1 个保存结果的检查" in summary
    assert "当前计划另 3 项未提供，未覆盖全计划" in summary
    assert "全局最优性均未检查" in summary


def test_check_cli_returns_nonzero_when_no_complete_saved_point_exists(tmp_path, capsys):
    from optagent_core.case_execution import main

    _local_case(tmp_path)

    exit_code = main(["check"], case_directory=tmp_path)

    assert exit_code != 0
    assert '"checks_passed": false' in capsys.readouterr().out


def test_solve_cli_uses_explicit_result_count_without_returning_rows(tmp_path, monkeypatch, capsys):
    from optagent_core import case_execution

    machine_result = {
        "kind": "local-computation",
        "status": "completed",
        "backend_id": "highs",
        "result_count": 1,
        "solution_path": "solution.json",
        "termination_counts": {"optimal": 1},
        "checked_point_count": 1,
        "solver_error_observed_count": 0,
        "model_check_status_counts": {"pass": 1},
        "business_check_status_counts": {"uncovered": 1},
        "numerical_optimality_status_counts": {"closed": 1},
    }

    def fake_solve(_case_directory, *, output_directory=None):
        return machine_result

    monkeypatch.setattr(case_execution, "solve_case", fake_solve)
    exit_code = case_execution.main(["solve"], case_directory=tmp_path)

    stdout = capsys.readouterr().out
    summary = json.loads(stdout)
    assert exit_code == 0
    assert "results" not in summary
    assert summary["result_count"] == 1
    assert summary["backend_id"] == "highs"
    assert summary["termination_counts"] == {"optimal": 1}
    assert summary["checked_point_count"] == 1
    assert summary["solver_error_observed_count"] == 0
    assert summary["model_check_status_counts"] == {"pass": 1}
    assert summary["business_check_status_counts"] == {"uncovered": 1}
    assert summary["numerical_optimality_status_counts"] == {"closed": 1}
    assert summary["output_directory"] == str(tmp_path / "results" / "local")
    assert summary["result_files"] == [
        str(tmp_path / "results" / "local" / ".optagent" / "execution.json"),
        str(tmp_path / "results" / "local" / "solution.json"),
        str(tmp_path / "results" / "local" / "summary.md"),
    ]


def test_solution_result_counts_aggregate_statuses_and_direct_not_applicable():
    from optagent_core.case_execution import _solution_result_counts

    rows = [
        {
            "result": {
                "termination": "optimal",
                "has_checked_candidate": True,
                "backend": {"id": "cbc"},
                "numerical": {"optimality": {"status": "closed"}},
            },
            "model_check": {"status": "pass"},
            "business_check": {"status": "uncovered"},
        },
        {
            "result": {
                "termination": "feasible",
                "has_checked_candidate": True,
                "backend": {"id": "direct_python"},
            },
            "model_check": None,
            "business_check": {"status": "pass"},
        },
        {
            "result": {
                "termination": "error",
                "has_checked_candidate": False,
                "backend": {"id": "cbc"},
            },
            "model_check": None,
            "business_check": None,
        },
    ]

    counts = _solution_result_counts(rows)

    assert counts["checked_point_count"] == 2
    assert counts["solver_error_observed_count"] == 0
    assert counts["termination_counts"] == {"optimal": 1, "feasible": 1, "error": 1}
    assert counts["model_check_status_counts"] == {"pass": 1, "not_applicable": 1, "unknown": 1}
    assert counts["business_check_status_counts"] == {"uncovered": 1, "pass": 1, "unknown": 1}
    assert counts["numerical_optimality_status_counts"] == {
        "closed": 1,
        "not_applicable": 1,
        "unknown": 1,
    }


def test_solver_error_observed_count_uses_existing_result_warning_marker():
    from optagent_core.case_execution import _solution_result_counts

    counts = _solution_result_counts([
        {
            "result": {
                "termination": "optimal",
                "warnings": ["solver_error_observed"],
                "backend": {"id": "cbc"},
            },
            "model_check": None,
            "business_check": None,
        }
    ])

    assert counts["termination_counts"] == {"optimal": 1}
    assert counts["solver_error_observed_count"] == 1


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown", "fixed_value_changed"])
def test_check_saved_point_rejects_incomplete_or_rewritten_variable_keys(mutation):
    from pyomo.environ import ConcreteModel, Var

    from optagent_core.case_execution import _saved_primals

    model = ConcreteModel()
    model.x = Var([1, 2], bounds=(0, 5))
    model.x[1].fix(2.0)
    variables = [
        {"component": "x", "key": [1], "domain": "Reals", "value": 2.0, "origin": "fixed_model_value"},
        {"component": "x", "key": [2], "domain": "Reals", "value": 3.0, "origin": "solver_primal"},
    ]
    if mutation == "missing":
        variables.pop()
    elif mutation == "duplicate":
        variables.append(dict(variables[0]))
    elif mutation == "unknown":
        variables[1]["key"] = [9]
    else:
        variables[0]["value"] = 4.0
    witness = {
        "input_digest": "d" * 64,
        "instance_key": [],
        "instance_binding_digest": "e" * 64,
        "variables": variables,
        "objective_values": [],
    }

    with pytest.raises(ContractError):
        _saved_primals(model, witness, row_index=0)


@pytest.mark.skipif(__import__("os").name != "posix", reason="Linux process-group timeout is the acceptance target")
def test_check_wall_deadline_publishes_timeout(tmp_path):
    from optagent_core.case_execution import check_case

    _local_case(tmp_path)
    model_path = tmp_path / "code" / "model.py"
    model_path.write_text(
        "import time\n"
        "from pyomo.environ import ConcreteModel, Objective, Var, minimize\n"
        "def build_model(bundle):\n"
        "    time.sleep(3)\n"
        "    model = ConcreteModel()\n"
        "    model.x = Var(bounds=(0, 10))\n"
        "    model.cost = Objective(expr=model.x, sense=minimize)\n"
        "    return model\n",
        encoding="utf-8",
    )
    run_path = tmp_path / ".optagent" / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["solve_options"]["execution_budget_seconds"] = 0.25
    run_path.write_text(json.dumps(run), encoding="utf-8")
    solution = {
        "schema_version": 2,
        "kind": "selected-run-solution",
        "status": "completed",
        "results": [
            {
                "result": {"instance_key": [], "instance_binding_digest": "c" * 64, "input_digest": "b" * 64, "layer": None},
                "witness": {
                    "input_digest": "b" * 64,
                    "instance_key": [],
                    "instance_binding_digest": "c" * 64,
                    "variables": [{"component": "x", "key": [], "domain": "Reals", "value": 1.0, "origin": "solver_primal"}],
                    "objective_values": [],
                },
                "model_check": None,
                "business_check": None,
            }
        ],
    }
    _write_solution(tmp_path, solution)

    report = check_case(tmp_path)

    assert report["status"] == "timeout"
    assert report["checks_passed"] is False
    assert report["error"]["code"] == "execution_budget_exceeded"


def test_candidate_code_revision_uses_visible_paths_without_recapturing_sources(core):
    from optagent_core.sources import source_projection

    case_id, _, _ = core["create_case"](raw=b"Use the stated requirement to prepare an optimization model.")
    workspace = core["workspace"]
    store = core["store"]()

    reference_path = workspace / "reference" / "background.md"
    reference_path.parent.mkdir(parents=True)
    reference_path.write_text("Consulted background; not a mathematical input.\n", encoding="utf-8")
    state = store.read_case(case_id)
    reference = core["call"](
        "optagent.data",
        {
            "op": "capture",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "path": "reference/background.md",
            "purpose": "reference",
            "locations": ["background context"],
        },
    )
    assert reference["ok"], reference
    reference_id = reference["result"]["source"]["id"]

    state = store.read_case(case_id)
    source_ids_before = list(state["source_ids"])
    source_revision_before = state["source_revision"]
    math_source_ids_before = source_projection(store, state)["math_source_ids"]
    assert reference_id in source_ids_before
    assert reference_id not in math_source_ids_before

    def write_code(relative_path, content):
        path = workspace / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def candidate_arguments(paths, expected_state_revision):
        return {
            "op": "revise",
            "case_id": case_id,
            "expected_state_revision": expected_state_revision,
            "kind": "candidate",
            "model_path": paths["model"],
            "local_module_paths": [paths["module"]],
            "data_id": None,
            "backend_id": "highs",
            "objective_plan": {"mode": "feasibility", "preference_note": "prepare the declared model"},
            "business_checker_path": paths["checker"],
            "input_closure": "declared",
            "closure_note": "Model, helper, and checker paths are explicitly registered.",
        }

    v1_paths = {
        "model": "code/v1/model.py",
        "module": "code/v1/helper.py",
        "checker": "code/v1/checker.py",
    }
    v1_code = {
        "model": "from .helper import VERSION\n\ndef build_model(bundle):\n    return VERSION\n\ndef extract_solution(bundle, witness):\n    return {'version': VERSION}\n",
        "module": "VERSION = 1\n",
        "checker": "from .helper import VERSION\n\ndef check_solution(bundle, solution):\n    valid = solution == {'version': VERSION}\n    return {'status': 'pass' if valid else 'fail', 'checked_obligations': [], 'objective_values': [], 'violations': [] if valid else ['version_mismatch'], 'uncovered': []}\n",
    }
    for key, relative_path in v1_paths.items():
        write_code(relative_path, v1_code[key])

    v1_candidate = core["call"]("optagent.case", candidate_arguments(v1_paths, state["state_revision"]))
    assert v1_candidate["ok"], v1_candidate
    v1_candidate_id = v1_candidate["result"]["candidate"]["id"]
    state = store.read_case(case_id)
    assert state["source_revision"] == source_revision_before
    assert state["source_ids"] == source_ids_before
    assert source_projection(store, state)["math_source_ids"] == math_source_ids_before

    solve_options = {
        "execution_budget_seconds": 60,
        "time_limit_seconds": 30,
        "relative_gap": 0.0,
        "absolute_gap": 0.0,
        "threads": 1,
        "random_seed": 0,
        "feasibility_tolerance": 1e-7,
        "integrality_tolerance": 1e-7,
        "write_log": False,
        "export_model": False,
    }

    def prepare(candidate_id):
        current = store.read_case(case_id)
        return core["call"](
            "optagent.run",
            {
                "op": "prepare",
                "case_id": case_id,
                "expected_state_revision": current["state_revision"],
                "candidate_id": candidate_id,
                "solve_options": solve_options,
            },
        )

    v1_prepared = prepare(v1_candidate_id)
    assert v1_prepared["ok"], v1_prepared
    v1_prepared_id = v1_prepared["result"]["prepared"]["id"]
    assert store.read_record(
        f".reaslab/optagent/cases/{case_id}/prepared/{v1_prepared_id}/prepared.json"
    )["status"] == "frozen"

    for key, relative_path in v1_paths.items():
        write_code(relative_path, f"# stale v1 {key} after owner edit\n")
    v2_paths = {
        "model": "code/v2/model.py",
        "module": "code/v2/helper.py",
        "checker": "code/v2/checker.py",
    }
    v2_code = {
        "model": "from .helper import VERSION\n\ndef build_model(bundle):\n    return VERSION * 2\n\ndef extract_solution(bundle, witness):\n    return {'version': VERSION * 2}\n",
        "module": "VERSION = 2\n",
        "checker": "from .helper import VERSION\n\ndef check_solution(bundle, solution):\n    valid = solution == {'version': VERSION * 2}\n    return {'status': 'pass' if valid else 'fail', 'checked_obligations': [], 'objective_values': [], 'violations': [] if valid else ['version_mismatch'], 'uncovered': []}\n",
    }
    for key, relative_path in v2_paths.items():
        write_code(relative_path, v2_code[key])

    state_before_stale_prepare = store.read_case(case_id)
    stale_prepare = prepare(v1_candidate_id)
    assert not stale_prepare["ok"], stale_prepare
    assert store.read_case(case_id)["state_revision"] == state_before_stale_prepare["state_revision"]

    v2_candidate = core["call"](
        "optagent.case",
        candidate_arguments(v2_paths, state_before_stale_prepare["state_revision"]),
    )
    assert v2_candidate["ok"], v2_candidate
    v2_candidate_id = v2_candidate["result"]["candidate"]["id"]
    state_after_v2 = store.read_case(case_id)
    assert state_after_v2["source_ids"] == source_ids_before
    assert state_after_v2["source_revision"] == source_revision_before
    assert source_projection(store, state_after_v2)["math_source_ids"] == math_source_ids_before
    assert store.read_record(f".reaslab/optagent/cases/{case_id}/sources/{reference_id}.json")["purpose"] == "reference"

    v2_prepared = prepare(v2_candidate_id)
    assert v2_prepared["ok"], v2_prepared
    v2_prepared_id = v2_prepared["result"]["prepared"]["id"]
    prepared_record = store.read_record(
        f".reaslab/optagent/cases/{case_id}/prepared/{v2_prepared_id}/prepared.json"
    )
    assert prepared_record["status"] == "frozen"
    assert prepared_record["source_revision"] == source_revision_before
    input_manifest = json.loads(store.read_ref(prepared_record["input_manifest"]))
    assert input_manifest["candidate_revision"] == state_after_v2["candidate_revision"]
    frozen_candidate = input_manifest["candidate"]
    assert store.read_ref(frozen_candidate["model"]["bytes"]) == v2_code["model"].encode("utf-8")
    assert store.read_ref(frozen_candidate["local_modules"][0]["bytes"]) == v2_code["module"].encode("utf-8")
    assert store.read_ref(frozen_candidate["business_checker"]["bytes"]) == v2_code["checker"].encode("utf-8")
