from __future__ import annotations

import errno
import hashlib
import json
import os
import subprocess
import sys
import time
import types
import uuid
from pathlib import Path

import pytest

import optagent_core.contracts as contracts
from optagent_core.contracts import ContractError, canonical_json_bytes, ref_from_bytes, sha256_bytes, strict_json_loads, validate_ref_shape, windows_long_path
from optagent_core.data import load_data_bundle, normalize_data
from optagent_core.runner import _launcher_code, _publish_runtime_bundle, _runtime_identity_bytes
from optagent_core.store import CaseStore
from optagent_core import worker as worker_module
from optagent_core.worker import FrozenReader, _OUTPUT_REFS, _OutputTarget, _fail_seal, _json_output, _jsonl_chunks, _run_instances, _write_stream_output, run_invocation, run_local_reproduction, verify_runtime_bundle


class _PortableMemoryReader:
    def __init__(self):
        self.values: dict[tuple[str, str, int, str], bytes] = {}
        self.reads: list[dict[str, object]] = []

    @staticmethod
    def key(ref):
        validated = validate_ref_shape(dict(ref))
        return (validated["path"], validated["sha256"], validated["byte_len"], validated["media_type"])

    def add(self, ref, raw):
        self.values[self.key(ref)] = raw

    def read_ref(self, ref):
        validated = validate_ref_shape(dict(ref))
        self.reads.append(validated)
        try:
            return self.values[self.key(validated)]
        except KeyError as exc:
            raise ContractError("not_found", "portable test reader has no exact Ref mapping") from exc


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH local output regression")
def test_local_output_target_writes_below_long_root_and_keeps_logical_refs(tmp_path):
    output_length = 261
    component_length = output_length - len(str(tmp_path)) - 1
    assert 0 < component_length <= 255
    output_root = tmp_path / ("x" * component_length)
    assert len(str(output_root)) == output_length

    output = _OutputTarget.create_local(output_root)
    raw = canonical_json_bytes({"ok": True})
    ref = output.write("nested/result.json", raw, "application/json")

    assert ref["path"] == "nested/result.json"
    assert ref["sha256"] == sha256_bytes(raw)
    assert ref["byte_len"] == len(raw)
    assert output.path("nested/result.json").read_bytes() == raw
    assert output.hash_file("nested/result.json") == (len(raw), sha256_bytes(raw))


def test_uncovered_model_check_publishes_one_sample_per_component_and_reason(tmp_path):
    pyo = pytest.importorskip("pyomo.environ")
    from optagent_core.results import ObjectiveSpec

    model = pyo.ConcreteModel()
    model.I = pyo.RangeSet(0, 7)
    model.x = pyo.Var(model.I, initialize=7.0)
    model.required = pyo.Constraint(model.I, rule=lambda current, index: current.x[index] >= 0.0)
    model.cost = pyo.Objective(expr=sum(model.x[index] for index in model.I))
    output = _OutputTarget.create_local(tmp_path / "outputs")

    checked, violations_ref = worker_module._write_violation_stream(
        output,
        "layer/violations.jsonl",
        model,
        {},
        ObjectiveSpec("cost", "min", "time_unit", model.cost.expr, None),
        {
            "input_digest": "a" * 64,
            "instance_key": [],
            "instance_binding_digest": "b" * 64,
        },
        {"feasibility_tolerance": 1e-7, "integrality_tolerance": 1e-7},
    )

    assert checked.record["status"] == "uncovered"
    assert checked.record["variable_coverage"] == {"required": 8, "present": 0, "completed": 0, "missing_count": 8}
    assert checked.record["component_coverage"] == [{
        "component": "required",
        "required": 8,
        "checked": 0,
        "failures": 0,
        "uncovered": 8,
        "max_violation": None,
        "max_violation_key": None,
    }]
    assert violations_ref is not None
    assert output.refs == {"layer/violations.jsonl": violations_ref}
    stream_path = tmp_path / "outputs" / "layer" / "violations.jsonl"
    rows = [strict_json_loads(line) for line in stream_path.read_bytes().splitlines()]
    assert len(rows) == 2
    assert rows[0]["kind"] == "variable"
    assert rows[0]["component"] == "x"
    assert rows[0]["key"] == [0]
    assert rows[0]["error"] == "missing_or_nonfinite_value"
    assert rows[1]["kind"] == "constraint"
    assert rows[1]["component"] == "required"
    assert rows[1]["key"] == [0]
    assert rows[1]["error"] == "row_value_unavailable"
    assert all(variable.value == 7.0 for variable in model.x.values())


def test_zero_variable_pass_removes_empty_violation_jsonl(tmp_path):
    pyo = pytest.importorskip("pyomo.environ")
    from optagent_core.results import ObjectiveSpec

    model = pyo.ConcreteModel()
    model.cost = pyo.Objective(expr=5.0)
    output = _OutputTarget.create_local(tmp_path / "outputs")

    checked, violations_ref = worker_module._write_violation_stream(
        output,
        "layer/violations.jsonl",
        model,
        {},
        ObjectiveSpec("cost", "min", "time_unit", model.cost.expr, None),
        {
            "input_digest": "a" * 64,
            "instance_key": [],
            "instance_binding_digest": "b" * 64,
        },
        {"feasibility_tolerance": 1e-7, "integrality_tolerance": 1e-7},
    )

    assert checked.record["status"] == "pass"
    assert violations_ref is None
    assert output.refs == {}
    assert not (tmp_path / "outputs" / "layer" / "violations.jsonl").exists()


def _publish_legacy_runtime_bundle(store: CaseStore):
    legacy_files = (
        "python/optagent_core/__init__.py",
        "python/optagent_core/contracts.py",
        "python/optagent_core/data.py",
        "python/optagent_core/models.py",
        "python/optagent_core/results.py",
        "python/optagent_core/worker.py",
    )
    source_root = Path(__file__).resolve().parents[1]
    entries = []
    copied = []
    for relative in legacy_files:
        raw = (source_root / relative.removeprefix("python/")).read_bytes()
        entries.append({"path": relative, "sha256": sha256_bytes(raw), "byte_len": len(raw)})
        copied.append((relative, raw))
    digest = sha256_bytes(_runtime_identity_bytes(entries))
    root = f".reaslab/optagent/runtime/{digest}"
    refs = []
    for entry, (relative, raw) in zip(entries, copied, strict=True):
        ref = store.write_immutable(f"{root}/{relative}", raw, "text/x-python; charset=utf-8")
        assert ref["sha256"] == entry["sha256"] and ref["byte_len"] == entry["byte_len"]
        refs.append(ref)
    manifest_ref = store.write_immutable(
        f"{root}/manifest.json",
        canonical_json_bytes({"schema_version": 1, "digest": digest, "files": entries}),
        "application/json",
    )
    store.write_immutable(f"{root}/COMPLETE", digest.encode("ascii"), "text/plain; charset=utf-8")
    verify_runtime_bundle(store.workspace, digest, manifest_ref, prepared_schema_version=2)
    return digest, manifest_ref, tuple(refs)

def test_business_checker_returns_objective_for_the_extracted_solution(monkeypatch):
    checker_ref = ref_from_bytes("candidate/business_checker.py", b"check_solution", "text/x-python; charset=utf-8")
    solution = {"flow": 10.0}
    expected = [{"component": "cost", "sense": "min", "unit": "USD", "value": 10.0}]
    returned = {
        "status": "pass",
        "checked_obligations": [],
        "objective_values": [{"component": "cost", "sense": "min", "unit": "USD", "value": 10.00000005}],
        "violations": [],
        "uncovered": [],
    }

    def call_checker(_reader, _spec, _modules, *, function_name, args, pointer):
        assert function_name == "check_solution"
        assert pointer == "/candidate/business_checker"
        assert args[1] is solution
        return returned, (checker_ref,)

    monkeypatch.setattr(worker_module, "call_frozen_function", call_checker)
    business, consumed, diagnostic, saved = worker_module._business_check_solution(
        object(), {"path": "business_checker.py", "bytes": checker_ref}, (), object(), solution,
        input_digest="a" * 64,
        instance_key=["north", 2],
        instance_binding_digest="b" * 64,
        expected_objective_values=expected,
    )
    assert business["status"] == "pass"
    assert business["objective_values"][0]["value"] == 10.00000005
    assert saved is solution and business["solution"] is solution
    assert consumed == (checker_ref,)
    assert diagnostic is None


def test_business_checker_objective_mismatch_fails_but_keeps_s(monkeypatch):
    solution = {"flow": 10.0}
    returned = {
        "status": "pass",
        "checked_obligations": [],
        "objective_values": [{"component": "cost", "sense": "min", "unit": "USD", "value": 10.000001}],
        "violations": [],
        "uncovered": [],
    }
    monkeypatch.setattr(worker_module, "call_frozen_function", lambda *_args, **_kwargs: (returned, ()))
    business, _, diagnostic, saved = worker_module._business_check_solution(
        object(), {"path": "business_checker.py"}, (), object(), solution,
        input_digest="c" * 64,
        instance_key=[7],
        instance_binding_digest="d" * 64,
        expected_objective_values=[{"component": "cost", "sense": "min", "unit": "USD", "value": 10.0}],
    )
    assert business["status"] == "fail"
    assert business["objective_values"][0]["value"] == 10.000001
    assert "business_checker_objective_mismatch" in business["violations"]
    assert saved is solution and business["solution"] is solution
    assert diagnostic is None


@pytest.mark.parametrize("forged_key", [[True], [1.0]])
def test_business_checker_cannot_supply_instance_identity(forged_key, monkeypatch):
    solution = {"flow": 1.0}
    returned = {
        "input_digest": "e" * 64,
        "instance_key": forged_key,
        "instance_binding_digest": "f" * 64,
        "status": "pass",
        "checked_obligations": [],
        "objective_values": [],
        "violations": [],
        "uncovered": [],
    }
    monkeypatch.setattr(worker_module, "call_frozen_function", lambda *_args, **_kwargs: (returned, ()))
    business, _, diagnostic, saved = worker_module._business_check_solution(
        object(), {"path": "business_checker.py"}, (), object(), solution,
        input_digest="1" * 64,
        instance_key=[1],
        instance_binding_digest="2" * 64,
        expected_objective_values=None,
    )
    assert business["status"] == "uncovered"
    assert business["input_digest"] == "1" * 64
    assert business["instance_key"] == [1]
    assert business["instance_binding_digest"] == "2" * 64
    assert business["solution"] is solution and saved is solution
    assert diagnostic["code"] == "output_invalid"


def test_business_checker_contract_error_is_generic_and_preserves_s(monkeypatch):
    solution = {"flow": 1.0}
    error = ContractError(
        "input_closure_unknown",
        "PRIVATE candidate runtime detail",
        pointer="/private-path",
    )
    monkeypatch.setattr(worker_module, "call_frozen_function", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
    business, consumed, diagnostic, saved = worker_module._business_check_solution(
        object(), {"path": "business_checker.py"}, (), object(), solution,
        input_digest="1" * 64,
        instance_key=[],
        instance_binding_digest="2" * 64,
        expected_objective_values=None,
    )
    assert business["status"] == "uncovered"
    assert business["uncovered"] == ["business_checker_error"]
    assert business["solution"] is solution and saved is solution
    assert consumed == () and diagnostic is None


@pytest.mark.parametrize("point", [None, {"variables": [], "objective_values": []}])
def test_direct_python_run_distinguishes_no_point_from_a_valid_empty_point(tmp_path: Path, monkeypatch, point):
    from optagent_core.solver_protocol import direct_python_profile

    input_digest = "a" * 64
    bundle = load_data_bundle(_PortableMemoryReader(), None, candidate_binding_digest="b" * 64)
    plan = {"mode": "feasibility", "preference_note": "empty witness fixture"}
    manifest = {
        "schema_version": 3,
        "input_digest": input_digest,
        "source_revision": 1,
        "candidate_revision": 2,
        "candidate_id": "candidate-direct",
        "data": None,
        "candidate": {
            "authoring": "direct_python",
            "model": {"original_path": "code/optimization_model.py", "bytes": {}},
            "local_modules": [],
            "business_checker": {"original_path": "code/solution_check.py", "bytes": {}},
            "backend_id": "direct_python",
            "objective_plan": plan,
        },
        "solve_options": {"execution_budget_seconds": 60},
    }
    def call_frozen(_reader, _spec, _modules, *, function_name, args, **_kwargs):
        if function_name == "solve":
            return point, ()
        if function_name == "extract_solution":
            assert args[1]["variables"] == []
            return {"empty_plan": []}, ()
        if function_name == "check_solution":
            assert args[1] == {"empty_plan": []}
            return {
                "status": "pass",
                "checked_obligations": [],
                "objective_values": [],
                "violations": [],
                "uncovered": [],
            }, ()
        pytest.fail(f"unexpected frozen call: {function_name}")

    monkeypatch.setattr(worker_module, "call_frozen_function", call_frozen)
    output = _OutputTarget.create_local(tmp_path / "direct-empty")
    records, refs, _consumed, solver_invoked, solver_returned = _run_instances(
        _PortableMemoryReader(),
        output,
        manifest,
        (),
        input_digest=input_digest,
        data_bundle=bundle,
        base_consumed_inputs=(),
        execution_started=time.monotonic(),
        backend_profile=direct_python_profile(),
    )

    result = strict_json_loads(output.path(records[0]["result"]["path"]).read_bytes())
    assert solver_invoked is False
    assert solver_returned is False
    assert records[0]["model_check"] is None
    assert records[0]["backend_observation"] is None
    assert not any(ref["path"].endswith(("/model-check.json", "/backend-observation.json", "/raw-primals.jsonl")) for ref in refs)
    assert (result["witness"] is None) == (point is None)
    assert result["check"]["path"].endswith("/business-check.json")
    assert result["solver_invoked"] is False
    assert result["bound"] is None
    assert result["has_checked_candidate"] is (point is not None)
    if point is not None:
        witness = strict_json_loads(output.path(records[0]["witness"]["path"]).read_bytes())
        assert witness["variables"] == []
        assert result["termination"] == "feasible"
        business = strict_json_loads(output.path(result["check"]["path"]).read_bytes())
        assert business["solution"] == {"empty_plan": []}
    else:
        assert result["termination"] == "unknown"


def test_direct_python_dispatch_bypasses_native_interface_availability_gate(monkeypatch):
    expected = object()
    observed = {}

    def direct_instances(*_args, **kwargs):
        observed.update(kwargs)
        return expected

    monkeypatch.setattr(worker_module, "_run_direct_instances", direct_instances)
    result = _run_instances(
        object(),
        object(),
        {"candidate": {"authoring": "direct_python"}},
        (),
        input_digest="a" * 64,
        data_bundle=None,
        base_consumed_inputs=(),
        execution_started=time.monotonic(),
        backend_profile={"id": "direct_python", "interface_available": False},
    )

    assert result is expected
    assert observed["backend_profile"]["interface_available"] is False


def _local_prepared_fixture(
    tmp_path: Path,
    *,
    include_data: bool = True,
    include_source: bool = False,
    native_json: bool = False,
    invalid_business_solution: bool = False,
    extra_business_evidence: bool = False,
    checker_contract_error: bool = False,
):
    store = CaseStore(tmp_path, create=True)
    reader = _PortableMemoryReader()
    runtime_digest, runtime_manifest_ref, helper_refs = _publish_legacy_runtime_bundle(store)
    for ref in (*helper_refs, runtime_manifest_ref):
        reader.add(ref, store.read_ref(ref))

    case_id = str(uuid.uuid4())
    prepared_id = str(uuid.uuid4())
    candidate_id = str(uuid.uuid4())
    binding_digest = "b" * 64
    input_root = f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/input"
    if native_json:
        extractor_expression = b"float('nan')" if invalid_business_solution else b"{'x': value, 'metadata': settings['metadata']}"
        model_raw = (
            b"from pyomo.environ import ConcreteModel, Constraint, Objective, Var, minimize\n"
            b"def build_model(bundle):\n"
            b"    settings = bundle.json('settings')\n"
            b"    settings['metadata']['flags'].append('model-only-mutation')\n"
            b"    model = ConcreteModel()\n"
            b"    model.x = Var(bounds=(0, 10))\n"
            b"    model.target = Constraint(expr=model.x == settings['target'])\n"
            b"    model.cost = Objective(expr=model.x, sense=minimize)\n"
            b"    return model\n"
            b"def extract_solution(bundle, witness):\n"
            b"    settings = bundle.json('settings')\n"
            b"    value = next(row['value'] for row in witness['variables'] if row['component'] == 'x')\n"
            b"    return " + extractor_expression + b"\n"
        )
    else:
        model_raw = b"def build_model(bundle):\n    raise AssertionError('candidate must not run in this boundary test')\n"
    model_ref = store.write_immutable(f"{input_root}/candidate/models/build.py", model_raw, "text/x-python; charset=utf-8")
    reader.add(model_ref, model_raw)
    checker_spec = None
    objective_plan = {"mode": "feasibility", "preference_note": "fixture"}
    if native_json:
        extra_evidence = (
            b"        'evidence': {'private': 'checker-private-value'},\n"
            if extra_business_evidence
            else b""
        )
        if checker_contract_error:
            checker_raw = (
                b"from optagent_core.contracts import ContractError\n"
                b"def check_solution(bundle, solution):\n"
                b"    raise ContractError('output_invalid', 'PRIVATE-CHECKER-CONTRACT-MARKER', pointer='/private-marker')\n"
            )
        else:
            checker_raw = (
                b"def check_solution(bundle, solution):\n"
                b"    settings = bundle.json('settings')\n"
                b"    value = solution['x']\n"
                b"    return {\n"
                b"        'status': 'pass' if value == settings['target'] else 'fail',\n"
                b"        'checked_obligations': [],\n"
                b"        'objective_values': [{'component': 'cost', 'sense': 'min', 'unit': 'unit', 'value': value}],\n"
                b"        'violations': [],\n"
                b"        'uncovered': [],\n"
            ) + extra_evidence + b"    }\n"
        checker_ref = store.write_immutable(
            f"{input_root}/candidate/business_checker.py", checker_raw, "text/x-python; charset=utf-8"
        )
        reader.add(checker_ref, checker_raw)
        checker_spec = {"original_path": "business_checker.py", "bytes": checker_ref}
        objective_plan = {
            "mode": "scalar",
            "objective_component": "cost",
            "sense": "min",
            "unit": "unit",
            "preference_note": "frozen native JSON fixture",
        }

    sources = []
    if include_source:
        source_id = str(uuid.uuid4())
        source_bytes = b"Source facts used by this NoData reproduction boundary test."
        source_bytes_ref = store.write_immutable(
            f".reaslab/optagent/cases/{case_id}/sources/{source_id}.txt",
            source_bytes,
            "application/octet-stream",
        )
        source_record_raw = canonical_json_bytes({"source_id": source_id, "bytes": source_bytes_ref})
        source_record_ref = store.write_immutable(
            f".reaslab/optagent/cases/{case_id}/sources/{source_id}.json",
            source_record_raw,
            "application/json",
        )
        reader.add(source_bytes_ref, source_bytes)
        reader.add(source_record_ref, source_record_raw)
        sources.append({"source_id": source_id, "record": source_record_ref, "bytes": source_bytes_ref})

    if include_data:
        source_id = str(uuid.uuid4())
        data_source_raw = (
            b'{"settings":{"target":2,"metadata":{"flags":[],"label":null}}}'
            if native_json
            else b"instance\none\n"
        )
        source_bytes_ref = store.write_immutable(
            f".reaslab/optagent/source-bytes/{sha256_bytes(data_source_raw)}",
            data_source_raw,
            "application/octet-stream",
        )
        data_source = {
            "source_id": source_id,
            "purpose": "data",
            "kind": "file",
            "bytes": source_bytes_ref,
            "original_sha256": source_bytes_ref["sha256"],
            "original_size": len(data_source_raw),
            "origin": {"workspace_path": None},
        }
        source_record_raw = canonical_json_bytes(data_source)
        source_record_ref = store.write_immutable(
            f".reaslab/optagent/cases/{case_id}/sources/{source_id}.json",
            source_record_raw,
            "application/json",
        )
        reader.add(source_bytes_ref, data_source_raw)
        reader.add(source_record_ref, source_record_raw)
        sources.append({"source_id": source_id, "record": source_record_ref, "bytes": source_bytes_ref})
        data_id = str(uuid.uuid4())
        block = ({
            "object_id": "settings",
            "source_id": source_id,
            "format": "json",
            "layout": {"pointer": "/settings"},
            "kind": "json",
            "key_fields": [],
            "columns": [],
            "duplicate_policy": "error",
            "missing_policy": "error",
        } if native_json else {
            "object_id": "instances",
            "source_id": source_id,
            "format": "csv",
            "layout": {"delimiter": ",", "header": "first_row", "skip_lines": 0},
            "kind": "table",
            "key_fields": ["instance"],
            "columns": [{
                "name": "instance", "position": None, "input_name": "instance",
                "type": "string", "unit": None, "nullable": False,
            }],
            "duplicate_policy": "error",
            "missing_policy": "error",
        })
        produced = normalize_data(
            store,
            data_id=data_id,
            source_records={source_id: data_source},
            source_bytes={source_id: data_source_raw},
            plan={
                "encoding": "utf-8",
                "blocks": [block],
                "conversions": [],
            },
            target_prefix=f".reaslab/optagent/cases/{case_id}/data/{data_id}",
        )
        data_manifest = produced["manifest"]
        data_ref = produced["manifest_ref"]
        reader.add(data_ref, store.read_ref(data_ref))
        reader.add(data_manifest["trace"], store.read_ref(data_manifest["trace"]))
        for spec in data_manifest["objects"]:
            reader.add(spec["values"], store.read_ref(spec["values"]))
        data_info = {
            "data_id": data_id,
            "manifest": data_ref,
            "instance_parameter_object_id": None if native_json else "instances",
        }
    else:
        data_info = None

    input_manifest = {
        "schema_version": 2,
        "prepared_id": prepared_id,
        "case_id": case_id,
        "source_revision": 3,
        "candidate_revision": 7,
        "candidate_id": candidate_id,
        "promotion_generation": 2,
        "binding_digest": binding_digest,
        "sources": sources,
        "candidate": {
            "authoring": "pyomo",
            "model": {"original_path": "models/build.py", "bytes": model_ref},
            "local_modules": [],
            "business_checker": checker_spec,
            "backend_id": "highs",
            "objective_plan": objective_plan,
            "input_closure": "declared",
            "closure_note": "fixture",
        },
        "data": data_info,
        "runtime_bundle": {"digest": runtime_digest, "manifest": runtime_manifest_ref, "worker_entry": "python/optagent_core/worker.py"},
        "solve_options": {
            "execution_budget_seconds": 120,
            "time_limit_seconds": 60,
            "relative_gap": None,
            "absolute_gap": None,
            "threads": 1,
            "random_seed": 0,
            "feasibility_tolerance": 1e-7,
            "integrality_tolerance": 1e-7,
            "write_log": False,
            "export_model": False,
        },
    }
    input_raw = canonical_json_bytes(input_manifest)
    input_ref = store.write_immutable(f"{input_root}/manifest.json", input_raw, "application/json")
    reader.add(input_ref, input_raw)
    return reader, input_ref, input_manifest, input_raw


def _replace_prepared_manifest(reader: _PortableMemoryReader, manifest_ref, manifest):
    raw = canonical_json_bytes(manifest)
    changed_ref = ref_from_bytes(manifest_ref["path"], raw, manifest_ref["media_type"])
    reader.add(changed_ref, raw)
    return changed_ref


def test_runtime_bundle_is_published_once_and_rejects_changed_helper_bytes(tmp_path: Path):
    store = CaseStore(tmp_path, create=True)
    digest, manifest_ref, helper_refs = _publish_runtime_bundle(store)

    assert len(helper_refs) == 7
    manifest, verified = verify_runtime_bundle(tmp_path, digest, manifest_ref)
    assert manifest["digest"] == digest
    assert tuple(verified) == helper_refs

    worker = store.resolve_path(f".reaslab/optagent/runtime/{digest}/python/optagent_core/worker.py", must_exist=True)
    worker.write_bytes(worker.read_bytes() + b"\n# altered after the frozen bundle was published\n")
    with pytest.raises(ContractError) as error:
        verify_runtime_bundle(tmp_path, digest, manifest_ref)
    assert error.value.code == "artifact_changed"


def test_runtime_bundle_reader_checks_manifest_and_all_helpers_by_exact_ref(tmp_path: Path):
    reader, _, manifest, _ = _local_prepared_fixture(tmp_path)
    runtime = manifest["runtime_bundle"]
    _, helper_refs = verify_runtime_bundle(
        reader,
        runtime["digest"],
        runtime["manifest"],
        prepared_schema_version=manifest["schema_version"],
    )

    assert len(helper_refs) == 6
    assert all(ref in reader.reads for ref in helper_refs)
    helper_key = reader.key(helper_refs[-1])
    reader.values[helper_key] += b"# changed helper bytes\n"
    with pytest.raises(ContractError) as changed:
        verify_runtime_bundle(
            reader,
            runtime["digest"],
            runtime["manifest"],
            prepared_schema_version=manifest["schema_version"],
        )
    assert changed.value.code == "artifact_changed"
    changed = dict(helper_refs[-1])
    changed["media_type"] = "application/octet-stream"
    with pytest.raises(ContractError) as error:
        reader.read_ref(changed)
    assert error.value.code == "not_found"


def test_local_reproduction_uses_one_reader_and_emits_local_only_summary(tmp_path: Path, monkeypatch):
    reader, input_ref, manifest, _ = _local_prepared_fixture(tmp_path)
    observed = {}
    fake_profile = {"id": "highs", "version": "test-only", "os": "Linux", "architecture": "x86_64"}

    def fake_run_instances(actual_reader, output, prepared_manifest, runtime_refs, **kwargs):
        assert worker_module.os.environ.get("OPTAGENT_TEST_RUNTIME_SENTINEL") == "preserved"
        observed["reader"] = actual_reader
        observed["prepared_manifest"] = prepared_manifest
        observed["data_bundle"] = kwargs["data_bundle"]
        assert actual_reader is reader
        assert prepared_manifest["prepared_id"] == manifest["prepared_id"]
        context = kwargs["data_bundle"].for_instance("instances", ("one",)).instance_context
        assert context is not None
        result_ref = output.write_json("instances/000000/layer-single/result.json", {"instance_key": list(context.instance_key)})
        primal_ref = output.write_stream(
            "instances/000000/layer-single/raw-primals.jsonl",
            [b'{"component":"x","key":[],"value":1.0}\n'],
            "application/x-ndjson",
        )
        batch_ref = output.write_json("batch-summary.json", {"planned_instance_count": 1})
        consumed = [*kwargs["base_consumed_inputs"], *kwargs["data_bundle"].consumed_inputs]
        return ([{
            "instance_key": list(context.instance_key),
            "row_digest": context.row_digest,
            "instance_binding_digest": context.instance_binding_digest,
            "layer": None,
            "result": result_ref,
        }], [result_ref, primal_ref, batch_ref], consumed, False, False)

    monkeypatch.setattr(worker_module, "_backend_versions", lambda: dict(fake_profile))
    monkeypatch.setattr(worker_module, "_run_instances", fake_run_instances)
    monkeypatch.setenv("OPTAGENT_TEST_RUNTIME_SENTINEL", "preserved")
    output_dir = tmp_path / "fresh-local-output"
    result = run_local_reproduction(reader, input_ref, output_dir)

    assert observed["reader"] is reader
    assert observed["data_bundle"].data_id == manifest["data"]["data_id"]
    report = result["report"]
    assert report["kind"] == "local-reproduction"
    assert report["input_digest"] == input_ref["sha256"]
    assert report["backend_profile"] == fake_profile
    assert report["data_bundle"]["data_id"] == manifest["data"]["data_id"]
    assert report["data_bundle"]["digest"] == observed["data_bundle"].digest
    assert report["solve_options"] == manifest["solve_options"]
    assert report["instances"][0]["row_digest"]
    assert "run_id" not in report and "dispatch_nonce" not in report and "stamp" not in report
    assert "seal" not in result
    assert strict_json_loads((output_dir / "local-reproduction.json").read_bytes()) == report
    assert strict_json_loads((output_dir / "instances/000000/layer-single/result.json").read_bytes()) == {"instance_key": ["one"]}
    assert (output_dir / "instances/000000/layer-single/raw-primals.jsonl").read_bytes() == b'{"component":"x","key":[],"value":1.0}\n'
    assert all(ref["path"].startswith(".reaslab/optagent/") for ref in report["consumed_inputs"])
    assert not any(ref["path"].startswith(".reaslab/optagent/cases/") and "/runs/" in ref["path"] for ref in report["consumed_inputs"])


def test_frozen_reproduction_solves_native_json_and_preserves_business_solution(tmp_path: Path, monkeypatch):
    pytest.importorskip("pyomo.environ")
    pytest.importorskip("highspy")
    reader, input_ref, manifest, _ = _local_prepared_fixture(tmp_path, native_json=True)
    fake_profile = {"id": "highs", "version": "test-only", "os": "Linux", "architecture": "x86_64"}
    monkeypatch.setattr(worker_module, "_backend_versions", lambda: dict(fake_profile))

    output_dir = tmp_path / "fresh-json-output"
    report = run_local_reproduction(reader, input_ref, output_dir)["report"]

    assert report["data_bundle"]["data_id"] == manifest["data"]["data_id"]
    assert report["solver_invoked"] is True
    assert report["solver_returned"] is True
    assert report["instances"][0]["instance_key"] == []
    output_records = {ref["path"].rsplit("/", 1)[-1]: ref for ref in report["output_refs"]}

    def read_output(name: str) -> dict:
        ref = output_records[name]
        return strict_json_loads((output_dir / ref["path"]).read_bytes())

    witness = read_output("witness.json")
    model_check = read_output("model-check.json")
    business_check = read_output("business-check.json")
    assert witness["variables"][0]["value"] == pytest.approx(2)
    assert model_check["status"] == "pass"
    assert business_check["status"] == "pass"
    assert business_check["solution"] == {
        "x": pytest.approx(2),
        "metadata": {"flags": [], "label": None},
    }


def test_frozen_reproduction_keeps_solver_artifacts_when_business_solution_is_invalid(tmp_path: Path, monkeypatch):
    pytest.importorskip("pyomo.environ")
    pytest.importorskip("highspy")
    reader, input_ref, _manifest, _ = _local_prepared_fixture(
        tmp_path,
        native_json=True,
        invalid_business_solution=True,
    )
    fake_profile = {"id": "highs", "version": "test-only", "os": "Linux", "architecture": "x86_64"}
    monkeypatch.setattr(worker_module, "_backend_versions", lambda: dict(fake_profile))

    output_dir = tmp_path / "fresh-invalid-json-solution-output"
    report = run_local_reproduction(reader, input_ref, output_dir)["report"]

    assert report["solver_invoked"] is True
    assert report["solver_returned"] is True
    output_records = {ref["path"].rsplit("/", 1)[-1]: ref for ref in report["output_refs"]}
    assert {"result.json", "witness.json", "model-check.json", "business-check.json", "backend-observation.json"} <= set(output_records)

    def read_output(name: str) -> dict:
        ref = output_records[name]
        return strict_json_loads((output_dir / ref["path"]).read_bytes())

    result = read_output("result.json")
    witness = read_output("witness.json")
    model_check = read_output("model-check.json")
    business_check = read_output("business-check.json")
    backend_observation = read_output("backend-observation.json")
    assert result["solver_invoked"] is True
    assert result["witness"] == output_records["witness.json"]
    assert result["check"] == output_records["model-check.json"]
    assert witness["variables"][0]["value"] == pytest.approx(2)
    assert model_check["status"] == "pass"
    assert business_check["status"] == "uncovered"
    assert business_check["uncovered"] == ["business_solution_extraction_error"]
    assert "solution" not in business_check
    assert backend_observation["observation"]["solver_invoked"] is True
    assert backend_observation["observation"]["solver_returned"] is True


def test_frozen_reproduction_reports_extra_business_field_and_keeps_solver_artifacts(tmp_path: Path, monkeypatch):
    pytest.importorskip("pyomo.environ")
    pytest.importorskip("highspy")
    reader, input_ref, _manifest, _ = _local_prepared_fixture(
        tmp_path,
        native_json=True,
        extra_business_evidence=True,
    )
    fake_profile = {"id": "highs", "version": "test-only", "os": "Linux", "architecture": "x86_64"}
    monkeypatch.setattr(worker_module, "_backend_versions", lambda: dict(fake_profile))

    report = run_local_reproduction(reader, input_ref, tmp_path / "fresh-extra-business-field-output")["report"]

    assert report["solver_invoked"] is True
    assert report["solver_returned"] is True
    output_records = {ref["path"].rsplit("/", 1)[-1]: ref for ref in report["output_refs"]}

    def read_output(name: str) -> dict:
        ref = output_records[name]
        return strict_json_loads((tmp_path / "fresh-extra-business-field-output" / ref["path"]).read_bytes())

    result = read_output("result.json")
    witness = read_output("witness.json")
    model_check = read_output("model-check.json")
    business_check = read_output("business-check.json")
    backend_observation = read_output("backend-observation.json")
    assert witness["variables"][0]["value"] == pytest.approx(2)
    assert model_check["status"] == "pass"
    assert business_check["status"] == "uncovered"
    assert business_check["uncovered"] == ["business_checker_error"]
    assert result["witness"] == output_records["witness.json"]
    assert result["check"] == output_records["model-check.json"]
    assert backend_observation["observation"]["solver_returned"] is True
    assert "business_checker_unavailable" in result["warnings"]
    diagnostic = next(
        warning.removeprefix("business_checker_diagnostic:")
        for warning in result["warnings"]
        if warning.startswith("business_checker_diagnostic:")
    )
    assert json.loads(diagnostic) == {
        "code": "output_invalid",
        "message": "business checker returned an invalid BusinessCheck",
        "pointer": "/candidate/business_checker/evidence",
    }
    assert "checker-private-value" not in diagnostic


def test_unknown_business_checker_exception_keeps_the_generic_uncovered_warning(monkeypatch):
    from types import SimpleNamespace
    from optagent_core.results import CheckedModel, ObjectiveSpec

    checked = CheckedModel(
        record={"status": "pass", "uncovered": [], "tolerances": {}},
        witness_variables=(),
        objective_values=(),
        candidate_complete=True,
        raw_vector_complete=True,
    )
    monkeypatch.setattr(worker_module, "_write_violation_stream", lambda *_args, **_kwargs: (checked, None))

    extracted = {"feasible": True}

    def raise_unknown(_reader, _spec, _modules, *, function_name, **_kwargs):
        if function_name == "extract_solution":
            return extracted, ()
        if function_name == "check_solution":
            raise RuntimeError("private runtime detail")
        pytest.fail(f"unexpected frozen call: {function_name}")

    monkeypatch.setattr(worker_module, "call_frozen_function", raise_unknown)

    written = {}

    def write_json(_output, name, value):
        written[name] = value
        return {"path": name}

    monkeypatch.setattr(
        worker_module,
        "_json_output_to",
        write_json,
    )
    result, _witness, _complete, refs = worker_module._write_layer(
        reader=object(),
        output=object(),
        stem="instances/000000/layer-single",
        model=object(),
        model_spec={"path": "model.py"},
        instance_bundle=SimpleNamespace(instance_context=None, candidate_binding_digest="b" * 64),
        prepared={
            "input_digest": "a" * 64,
            "source_revision": 1,
            "candidate_revision": 1,
            "schema_version": 2,
            "solve_options": {},
        },
        objective=ObjectiveSpec(None, None, None, None, None),
        layer=None,
        observation=None,
        primals={},
        deterministic_termination="feasible",
        solver_invoked=False,
        warnings=(),
        checker_spec={"path": "business_checker.py"},
        module_specs=(),
        runtime_refs=(),
        backend_profile={"id": "test"},
        backend_observation_ref=None,
        raw_primals_ref={"path": "raw-primals"},
        model_consumed=(),
    )

    business_check = refs["business_check"]
    assert business_check == {"path": "instances/000000/layer-single/business-check.json"}
    assert written["instances/000000/layer-single/business-check.json"]["uncovered"] == ["business_checker_error"]
    assert written["instances/000000/layer-single/business-check.json"]["solution"] == extracted
    assert "business_checker_unavailable" in result["warnings"]
    diagnostic = next(
        warning.removeprefix("business_checker_diagnostic:")
        for warning in result["warnings"]
        if warning.startswith("business_checker_diagnostic:")
    )
    assert json.loads(diagnostic) == {"code": "business_checker_error", "error_type": "RuntimeError"}
    assert "private runtime detail" not in json.dumps(result)


def test_write_layer_keeps_direction_conflict_strict_and_persists_native_closure(monkeypatch):
    from types import SimpleNamespace
    from optagent_core.results import CheckedModel, HighsObservation, ObjectiveSpec

    checked_objective = 100.0 - 2e-9
    objective = {"component": "cost", "sense": "min", "unit": "unit", "value": checked_objective}
    checked = CheckedModel(
        record={
            "status": "pass",
            "tolerances": {"primal": 1e-7},
            "objective_values": [objective],
        },
        witness_variables=(),
        objective_values=(objective,),
        candidate_complete=True,
        raw_vector_complete=True,
    )
    native_record = {
        "termination": "time_limit",
        "raw_solution_value_valid": True,
        "best_feasible_objective": 100.0,
        "best_objective_bound": 100.0,
        "bound_evidence": {
            "source": "synthetic worker fixture",
            "scope": "global",
            "objective_sense": "min",
            "value_direction": "lower",
            "interface": "worker test fixture",
            "objective_transform": {"kind": "identity", "formula": "original objective units"},
            "raw_value": 100.0,
        },
        "requested_options": {},
        "effective_options": {},
    }
    observation = HighsObservation(
        record=native_record,
        backend={"id": "highs", "version": "synthetic fixture"},
        termination="time_limit",
        raw_status="native-time-limit",
        solver_invoked=True,
        solver_returned=True,
        primals={},
        solver_incumbent_objective=100.0,
        bound_value=100.0,
        nonfinite_fields={},
        solve_wall_seconds=0.001,
        error=None,
    )
    monkeypatch.setattr(worker_module, "_write_violation_stream", lambda *_args, **_kwargs: (checked, None))
    extracted = {"flow": 100.0}

    def call_frozen(_reader, _spec, _modules, *, function_name, **_kwargs):
        if function_name == "extract_solution":
            return extracted, ()
        if function_name == "check_solution":
            return {
                "status": "pass",
                "checked_obligations": [],
                "objective_values": [objective],
                "violations": [],
                "uncovered": [],
            }, ()
        pytest.fail(f"unexpected frozen call: {function_name}")

    monkeypatch.setattr(worker_module, "call_frozen_function", call_frozen)
    written = {}

    def write_json(_output, name, value):
        written[name] = value
        return {"path": name}

    monkeypatch.setattr(worker_module, "_json_output_to", write_json)
    instance = SimpleNamespace(instance_key=("A",), instance_binding_digest="b" * 64)
    result, _witness, complete, _refs = worker_module._write_layer(
        reader=object(),
        output=object(),
        stem="instances/000000/layer-single",
        model=object(),
        model_spec={"path": "model.py"},
        instance_bundle=SimpleNamespace(instance_context=instance, candidate_binding_digest="b" * 64),
        prepared={
            "input_digest": "a" * 64,
            "source_revision": 1,
            "candidate_revision": 1,
            "schema_version": 3,
            "solve_options": {"relative_gap": 0.0, "absolute_gap": 1e-8},
        },
        objective=ObjectiveSpec("cost", "min", "unit", None, None),
        layer=None,
        observation=observation,
        primals={},
        deterministic_termination=None,
        solver_invoked=True,
        warnings=(),
        checker_spec={"path": "business_checker.py"},
        module_specs=(),
        runtime_refs=(),
        backend_profile={"id": "highs", "version": "synthetic fixture"},
        backend_observation_ref={"path": "instances/000000/layer-single/backend-observation.json"},
        raw_primals_ref={"path": "instances/000000/layer-single/raw-primals.jsonl"},
        model_consumed=(),
    )

    assert complete is True
    assert result["bound"] is None
    assert result["gap"]["absolute"] is None
    assert result["numerical"]["optimality"]["status"] == "closed"
    assert result["numerical"]["optimality"]["qualification"] == "objective_recomputation_residual"
    assert result["numerical"]["optimality"]["native_reported_bound"] == 100.0
    assert result["numerical"]["optimality"]["closure_residual"] == pytest.approx(2e-9)
    assert written["instances/000000/layer-single/result.json"] == result


def test_candidate_raised_contract_error_does_not_leak_candidate_details(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace
    from optagent_core.results import CheckedModel, ObjectiveSpec

    reader, _input_ref, manifest, _ = _local_prepared_fixture(
        tmp_path,
        native_json=True,
        checker_contract_error=True,
    )
    checked = CheckedModel(
        record={"status": "pass", "uncovered": [], "tolerances": {}},
        witness_variables=(),
        objective_values=(),
        candidate_complete=True,
        raw_vector_complete=True,
    )
    monkeypatch.setattr(worker_module, "_write_violation_stream", lambda *_args, **_kwargs: (checked, None))
    preserved_solution = {"x": 3.0, "metadata": {"flags": [], "label": None}}
    monkeypatch.setattr(
        worker_module,
        "_extract_business_solution",
        lambda *_args, **_kwargs: (preserved_solution, ()),
    )

    written = {}

    def write_json(_output, name, value):
        written[name] = value
        return {"path": name}

    monkeypatch.setattr(worker_module, "_json_output_to", write_json)
    result, _witness, _complete, refs = worker_module._write_layer(
        reader=reader,
        output=object(),
        stem="instances/000000/layer-single",
        model=object(),
        model_spec=manifest["candidate"]["model"],
        instance_bundle=SimpleNamespace(
            instance_context=None,
            candidate_binding_digest=manifest["binding_digest"],
        ),
        prepared={
            "input_digest": "a" * 64,
            "source_revision": 1,
            "candidate_revision": 1,
            "schema_version": 2,
            "solve_options": {},
        },
        objective=ObjectiveSpec(None, None, None, None, None),
        layer=None,
        observation=None,
        primals={},
        deterministic_termination="feasible",
        solver_invoked=False,
        warnings=(),
        checker_spec=manifest["candidate"]["business_checker"],
        module_specs=(),
        runtime_refs=(),
        backend_profile={"id": "test"},
        backend_observation_ref=None,
        raw_primals_ref={"path": "raw-primals"},
        model_consumed=(),
    )

    assert result["warnings"] == []
    assert not any(warning.startswith("business_checker_diagnostic:") for warning in result["warnings"])
    assert written["instances/000000/layer-single/business-check.json"]["uncovered"] == ["business_checker_error"]
    assert written["instances/000000/layer-single/business-check.json"]["solution"] == {"x": 3.0, "metadata": {"flags": [], "label": None}}
    assert "PRIVATE-CHECKER-CONTRACT-MARKER" not in json.dumps(result)
    assert "PRIVATE-CHECKER-CONTRACT-MARKER" not in json.dumps(written)
    assert refs["business_check"] == {"path": "instances/000000/layer-single/business-check.json"}


def test_managed_invocation_preserves_solver_runtime_environment(tmp_path: Path, monkeypatch):
    invocation = {
        "case_id": "case-id",
        "prepared_id": "prepared-id",
        "run_id": "run-id",
        "dispatch_nonce": "nonce",
        "input_digest": "a" * 64,
        "input_manifest": {"path": "manifest-ref"},
        "output_path": ".reaslab/optagent/cases/case-id/runs/run-id/output",
    }
    manifest = {"schema_version": 2, "runtime_bundle": {"digest": "b" * 64, "manifest": {"path": "runtime-ref"}}}
    invocation_ref = {"path": "invocation-ref"}
    monkeypatch.setattr(worker_module, "_load_invocation", lambda *_args: (invocation, manifest, invocation_ref))
    monkeypatch.setattr(worker_module, "FrozenReader", lambda *_args: object())
    monkeypatch.setattr(worker_module._OutputTarget, "platform", lambda *_args: object())
    monkeypatch.setattr(worker_module, "verify_runtime_bundle", lambda *_args, **_kwargs: ({}, ()))
    monkeypatch.setattr(worker_module, "_collect_worker_inputs", lambda *_args, **_kwargs: (None, ()))
    monkeypatch.setattr(worker_module, "_backend_versions", lambda: {"id": "highs", "version": "test"})
    monkeypatch.setattr(worker_module, "_json_output", lambda *_args: {"path": "seal-ref"})
    monkeypatch.setenv("OPTAGENT_TEST_RUNTIME_SENTINEL", "preserved")

    def observe_environment(*_args, **_kwargs):
        assert worker_module.os.environ.get("OPTAGENT_TEST_RUNTIME_SENTINEL") == "preserved"
        return [], [], (), False, False

    monkeypatch.setattr(worker_module, "_run_instances", observe_environment)
    result = run_invocation(str(tmp_path), "invocation.json", "c" * 64)

    assert result["seal"] == {"path": "seal-ref"}


def test_local_reproduction_uses_empty_bundle_and_keeps_real_input_closure(tmp_path: Path, monkeypatch):
    reader, input_ref, manifest, _ = _local_prepared_fixture(tmp_path, include_data=False, include_source=True)
    observed = {}
    loader_calls = []
    original_loader = worker_module.load_data_bundle
    fake_profile = {"id": "highs", "version": "test-only", "os": "Linux", "architecture": "x86_64"}

    def loader_spy(actual_reader, data_ref_or_manifest, **kwargs):
        loader_calls.append((actual_reader, data_ref_or_manifest, kwargs))
        return original_loader(actual_reader, data_ref_or_manifest, **kwargs)

    def fake_run_instances(actual_reader, output, prepared_manifest, runtime_refs, **kwargs):
        bundle = kwargs["data_bundle"]
        context = bundle.for_instance(None, ()).instance_context
        assert context is not None
        observed["reader"] = actual_reader
        observed["context"] = context
        result_ref = output.write_json(
            "instances/000000/layer-single/result.json",
            {"instance_key": list(context.instance_key), "instance_binding_digest": context.instance_binding_digest},
        )
        batch_ref = output.write_json("batch-summary.json", {"planned_instance_count": 1})
        consumed = [*kwargs["base_consumed_inputs"], *bundle.consumed_inputs]
        return ([{
            "instance_key": list(context.instance_key),
            "row_digest": context.row_digest,
            "instance_binding_digest": context.instance_binding_digest,
            "layer": None,
            "result": result_ref,
        }], [result_ref, batch_ref], consumed, False, False)

    monkeypatch.setattr(worker_module, "load_data_bundle", loader_spy)
    monkeypatch.setattr(worker_module, "_backend_versions", lambda: dict(fake_profile))
    monkeypatch.setattr(worker_module, "_run_instances", fake_run_instances)
    result = run_local_reproduction(reader, input_ref, tmp_path / "fresh-no-data-output")

    report = result["report"]
    assert loader_calls == [(reader, None, {"candidate_binding_digest": manifest["binding_digest"]})]
    assert observed["reader"] is reader
    assert observed["context"].instance_key == ()
    assert observed["context"].row_digest == sha256_bytes(canonical_json_bytes({}))
    assert report["data_bundle"] == {
        "data_id": None,
        "digest": sha256_bytes(canonical_json_bytes(None)),
        "loader_version": "optagent-data-v1",
        "instance_parameter_object_id": None,
    }
    assert report["instances"][0]["instance_key"] == []
    assert report["instances"][0]["instance_binding_digest"] == observed["context"].instance_binding_digest
    assert all("/data/" not in ref["path"] for ref in reader.reads)
    consumed_paths = {ref["path"] for ref in report["consumed_inputs"]}
    assert manifest["candidate"]["model"]["bytes"]["path"] in consumed_paths
    assert manifest["sources"][0]["record"]["path"] in consumed_paths
    assert manifest["sources"][0]["bytes"]["path"] in consumed_paths
    assert manifest["runtime_bundle"]["manifest"]["path"] in consumed_paths
    assert not any(path.startswith(f".reaslab/optagent/cases/{manifest['case_id']}/data/") for path in consumed_paths)
    assert strict_json_loads((tmp_path / "fresh-no-data-output" / "local-reproduction.json").read_bytes()) == report


@pytest.mark.parametrize("interface_available", [True, None])
def test_run_instances_no_data_uses_one_empty_key_and_same_instance_context(tmp_path: Path, monkeypatch, interface_available):
    class FakeVar:
        pass

    class FakeModel:
        def component_data_objects(self, component_type, **kwargs):
            assert component_type is FakeVar
            assert kwargs == {"active": True, "descend_into": True}
            return iter(())

    fake_environ = types.ModuleType("pyomo.environ")
    fake_environ.Var = FakeVar
    fake_pyomo = types.ModuleType("pyomo")
    fake_pyomo.__path__ = []
    fake_pyomo.environ = fake_environ
    monkeypatch.setitem(sys.modules, "pyomo", fake_pyomo)
    monkeypatch.setitem(sys.modules, "pyomo.environ", fake_environ)

    model = FakeModel()
    binding_digest = "c" * 64
    bundle = load_data_bundle(_PortableMemoryReader(), None, candidate_binding_digest=binding_digest)
    manifest = {
        "input_digest": "d" * 64,
        "source_revision": 2,
        "candidate_revision": 3,
        "data": None,
        "candidate": {
            "model": {"bytes": ref_from_bytes(".reaslab/optagent/model.py", b"model", "text/x-python; charset=utf-8")},
            "local_modules": [],
            "business_checker": None,
            "backend_id": "highs",
            "objective_plan": {"mode": "feasibility", "preference_note": "empty data boundary"},
        },
        "solve_options": {
            "execution_budget_seconds": 120,
            "time_limit_seconds": 60,
            "relative_gap": None,
            "absolute_gap": None,
            "threads": 1,
            "random_seed": 0,
            "feasibility_tolerance": 1e-7,
            "integrality_tolerance": 1e-7,
            "write_log": False,
            "export_model": False,
        },
    }
    seen = {"write_layer_calls": 0}

    def build_model(actual_reader, instance_bundle, model_spec, modules, *, backend_id, require_extract_solution):
        seen["built_bundle"] = instance_bundle
        seen["build_backend_id"] = backend_id
        seen["require_extract_solution"] = require_extract_solution
        assert backend_id == "highs"
        assert require_extract_solution is False
        assert instance_bundle.data_id is None
        assert instance_bundle.instance_context.instance_key == ()
        return model, []

    def write_layer(**kwargs):
        seen["write_layer_calls"] += 1
        context = kwargs["instance_bundle"].instance_context
        seen["written_key"] = context.instance_key
        assert kwargs["observation"] is None
        assert kwargs["solver_invoked"] is False
        assert kwargs["backend_observation_ref"] is None
        observation_path = kwargs["output"].path(kwargs["stem"] + "/backend-observation.json")
        assert not observation_path.exists()
        result_ref = kwargs["output"].write_json(
            kwargs["stem"] + "/result.json",
            {
                "instance_key": [],
                "raw_status": "deterministic_model_check",
                "termination": "infeasible",
                "has_checked_candidate": False,
                "has_solver_incumbent": False,
                "best_objective_bound": None,
                "relative_gap": None,
            },
        )
        return (
            {
                "raw_status": "deterministic_model_check",
                "termination": "infeasible",
                "has_checked_candidate": False,
                "has_solver_incumbent": False,
                "best_objective_bound": None,
                "relative_gap": None,
                "objective": None,
            },
            None,
            False,
            {"result": result_ref, "model_consumed": [], "checker_consumed": [], "ancillary": []},
        )

    monkeypatch.setattr(worker_module, "build_native_model", build_model)
    monkeypatch.setattr(worker_module, "empty_domain", lambda variable: (False, None))
    monkeypatch.setattr(worker_module, "objective_for_layer", lambda actual_model, plan, layer: types.SimpleNamespace(component=None))
    monkeypatch.setattr(worker_module, "_requires_solver", lambda actual_model: False)
    monkeypatch.setattr(worker_module, "_write_layer", write_layer)
    output = _OutputTarget.create_local(tmp_path / "one-empty-instance")
    records, refs, consumed, solver_invoked, solver_returned = _run_instances(
        _PortableMemoryReader(),
        output,
        manifest,
        (),
        input_digest=manifest["input_digest"],
        data_bundle=bundle,
        base_consumed_inputs=(),
        execution_started=time.monotonic(),
        backend_profile={"id": "highs", "version": "test-only", "interface_available": interface_available},
    )

    context = bundle.for_instance(None, ()).instance_context
    assert seen["built_bundle"].instance_context.instance_binding_digest == context.instance_binding_digest
    assert seen["build_backend_id"] == "highs"
    assert seen["require_extract_solution"] is False
    assert seen["written_key"] == ()
    assert len(records) == 1 and records[0]["instance_key"] == []
    assert records[0]["instance_binding_digest"] == context.instance_binding_digest
    assert any(ref["path"].endswith("batch-summary.json") for ref in refs)
    assert seen["write_layer_calls"] == 1
    backend_ref = records[0]["backend_observation"]
    assert backend_ref is not None
    assert sum(ref["path"] == backend_ref["path"] for ref in refs) == 1
    backend = strict_json_loads(output.path("instances/000000/layer-single/backend-observation.json").read_bytes())
    assert backend["observation"]["raw_status"] == "deterministic_model_check"
    assert backend["observation"]["termination"] == "infeasible"
    assert backend["observation"]["solver_invoked"] is False
    assert backend["observation"]["solver_returned"] is False
    assert backend["observation"]["best_feasible_objective"] is None
    assert backend["observation"]["best_objective_bound"] is None
    assert backend["observation"]["bound_evidence"] is None
    assert backend["observation"]["termination_condition"] == "infeasible"
    assert backend["backend"] is None
    assert backend["nonfinite_fields"] == {}
    assert not consumed
    assert solver_invoked is False and solver_returned is False


@pytest.mark.parametrize(
    ("backend_id", "engine", "interface", "interface_key"),
    [
        ("highs", "HiGHS", "Pyomo APPSI", "appsi_highs"),
        ("cp_sat", "CP-SAT", "OR-Tools native CpModel", "CpModel"),
    ],
)
def test_native_run_stops_before_candidate_execution_when_interface_is_unavailable(
    tmp_path: Path, monkeypatch, backend_id, engine, interface, interface_key
):
    backend_profile = {
        "id": backend_id,
        "engine": engine,
        "interface": interface,
        "interface_key": interface_key,
        "interface_available": False,
        "interface_diagnostic": "ModuleNotFoundError",
        "python_implementation": "CPython",
    }
    monkeypatch.setattr(
        worker_module,
        "build_native_model",
        lambda *_args, **_kwargs: pytest.fail("candidate model execution must not begin"),
    )
    output = _OutputTarget.create_local(tmp_path / "unavailable-interface")

    with pytest.raises(ContractError) as raised:
        _run_instances(
            object(),
            output,
            {"candidate": {"authoring": "pyomo", "backend_id": backend_id}},
            (),
            input_digest="f" * 64,
            data_bundle=None,
            base_consumed_inputs=(),
            execution_started=time.monotonic(),
            backend_profile=backend_profile,
        )

    assert raised.value.code == "environment_unavailable"
    assert raised.value.details == {"backend_profile": backend_profile}


def test_run_instances_passes_frozen_warm_start_to_solver(tmp_path: Path, monkeypatch):
    class FakeVar:
        pass

    class FakeModel:
        def component_data_objects(self, component_type, **kwargs):
            assert component_type is FakeVar
            assert kwargs == {"active": True, "descend_into": True}
            return iter(())

    fake_environ = types.ModuleType("pyomo.environ")
    fake_environ.Var = FakeVar
    fake_pyomo = types.ModuleType("pyomo")
    fake_pyomo.__path__ = []
    fake_pyomo.environ = fake_environ
    monkeypatch.setitem(sys.modules, "pyomo", fake_pyomo)
    monkeypatch.setitem(sys.modules, "pyomo.environ", fake_environ)

    model = FakeModel()
    manifest = {
        "schema_version": 3,
        "input_digest": "d" * 64,
        "source_revision": 2,
        "candidate_revision": 3,
        "data": None,
        "candidate": {
            "model": {"bytes": ref_from_bytes(".reaslab/optagent/model.py", b"model", "text/x-python; charset=utf-8")},
            "local_modules": [],
            "business_checker": None,
            "backend_id": "cbc",
            "objective_plan": {"mode": "scalar", "sense": "min", "objective_component": "cost", "unit": "unit", "preference_note": "test"},
        },
        "solve_options": {
            "execution_budget_seconds": 120,
            "time_limit_seconds": None,
            "relative_gap": 0.0,
            "absolute_gap": 0.0,
            "threads": None,
            "random_seed": None,
            "warm_start": True,
            "feasibility_tolerance": 1e-6,
            "integrality_tolerance": 1e-6,
            "write_log": False,
            "export_model": False,
        },
    }
    bundle = load_data_bundle(
        _PortableMemoryReader(), None, candidate_binding_digest="c" * 64
    )
    seen: dict[str, object] = {}

    def build_native_model(_reader, _bundle, _model_spec, _modules, *, backend_id, require_extract_solution):
        seen["build_backend_id"] = backend_id
        seen["require_extract_solution"] = require_extract_solution
        assert backend_id == "cbc"
        assert require_extract_solution is False
        return model, []

    monkeypatch.setattr(worker_module, "build_native_model", build_native_model)
    monkeypatch.setattr(worker_module, "empty_domain", lambda _variable: (False, None))
    monkeypatch.setattr(
        worker_module,
        "objective_for_layer",
        lambda *_args: types.SimpleNamespace(component=object(), sense="min"),
    )
    monkeypatch.setattr(worker_module, "_requires_solver", lambda _model: True)

    def fake_run_solver_once(_model, **kwargs):
        seen["solver_options"] = dict(kwargs)
        return types.SimpleNamespace(
            record={"solver_returned": True},
            backend={"id": "cbc"},
            nonfinite_fields={},
            primals={},
            solver_invoked=True,
            solver_returned=True,
        )

    from optagent_core import solver_protocol

    monkeypatch.setattr(solver_protocol, "run_solver_once", fake_run_solver_once)

    def fake_write_layer(**kwargs):
        seen["observation"] = kwargs["observation"]
        result_ref = kwargs["output"].write_json(
            kwargs["stem"] + "/result.json", {"termination": "optimal"}
        )
        return (
            {"termination": "optimal"},
            None,
            True,
            {"result": result_ref, "model_consumed": [], "checker_consumed": [], "ancillary": []},
        )

    monkeypatch.setattr(worker_module, "_write_layer", fake_write_layer)
    output = _OutputTarget.create_local(tmp_path / "warm-start-worker")
    records, _refs, _consumed, solver_invoked, solver_returned = _run_instances(
        _PortableMemoryReader(),
        output,
        manifest,
        (),
        input_digest=manifest["input_digest"],
        data_bundle=bundle,
        base_consumed_inputs=(),
        execution_started=time.monotonic(),
        backend_profile={"id": "cbc", "version": "test-only"},
        enforce_execution_budget=False,
    )

    assert seen["solver_options"]["warm_start"] is True
    assert seen["build_backend_id"] == "cbc"
    assert seen["require_extract_solution"] is False
    assert seen["observation"].record["solver_returned"] is True
    assert len(records) == 1
    assert solver_invoked is True and solver_returned is True


def test_prepared_manifest_missing_data_key_is_not_treated_as_null(tmp_path: Path, monkeypatch):
    reader, input_ref, manifest, _ = _local_prepared_fixture(tmp_path, include_data=False)
    del manifest["data"]
    malformed_ref = _replace_prepared_manifest(reader, input_ref, manifest)
    monkeypatch.setattr(worker_module, "_backend_versions", lambda: pytest.fail("malformed input must fail before profile checks"))

    with pytest.raises(ContractError) as error:
        run_local_reproduction(reader, malformed_ref, tmp_path / "missing-data-key-output")

    assert error.value.code == "output_invalid"
    assert not (tmp_path / "missing-data-key-output").exists()


def test_prepared_real_data_ref_failure_does_not_fallback_to_empty_bundle(tmp_path: Path, monkeypatch):
    reader, input_ref, manifest, _ = _local_prepared_fixture(tmp_path)
    bad_ref = ref_from_bytes(
        f".reaslab/optagent/cases/{manifest['case_id']}/data/manifest.json",
        b"{}",
        "application/json",
    )
    manifest["data"]["manifest"] = bad_ref
    malformed_ref = _replace_prepared_manifest(reader, input_ref, manifest)
    monkeypatch.setattr(worker_module, "_backend_versions", lambda: pytest.fail("bad data Ref must fail before profile checks"))

    with pytest.raises(ContractError) as error:
        run_local_reproduction(reader, malformed_ref, tmp_path / "bad-data-ref-output")

    assert error.value.code == "not_found"
    assert not (tmp_path / "bad-data-ref-output").exists()


def test_local_reproduction_refuses_existing_output_before_reading_inputs(tmp_path: Path):
    reader, input_ref, _, _ = _local_prepared_fixture(tmp_path)
    existing = tmp_path / "already-here"
    existing.mkdir()
    sentinel = existing / "keep.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    reads_before = len(reader.reads)

    with pytest.raises(ContractError) as error:
        run_local_reproduction(reader, input_ref, existing)

    assert error.value.code == "package_collision"
    assert len(reader.reads) == reads_before
    assert sentinel.read_text(encoding="utf-8") == "unchanged"


def test_local_reproduction_does_not_fallback_when_portable_ref_is_missing(tmp_path: Path, monkeypatch):
    reader, input_ref, manifest, _ = _local_prepared_fixture(tmp_path)
    model_ref = manifest["candidate"]["model"]["bytes"]
    reader.values.pop(reader.key(model_ref))
    monkeypatch.setattr(worker_module, "_backend_versions", lambda: pytest.fail("profile check must follow closure verification"))
    output_dir = tmp_path / "missing-ref-output"

    with pytest.raises(ContractError) as error:
        run_local_reproduction(reader, input_ref, output_dir)

    assert error.value.code == "not_found"
    assert not output_dir.exists()


def test_local_profile_gate_runs_before_any_numeric_package_import(tmp_path: Path, monkeypatch):
    import builtins

    from optagent_core import results

    reader, input_ref, _, _ = _local_prepared_fixture(tmp_path)
    imported_numeric: list[str] = []
    original_import = builtins.__import__

    def gated_platform():
        raise ContractError("capability_uncovered", "synthetic unsupported profile")

    def guarded_import(name, *args, **kwargs):
        if name.split(".", 1)[0] in {"numpy", "pyomo", "highspy"}:
            imported_numeric.append(name)
            raise AssertionError(f"numeric import occurred before platform gate: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(results, "_require_supported_platform", gated_platform)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    output_dir = tmp_path / "unsupported-profile-output"

    with pytest.raises(ContractError) as error:
        run_local_reproduction(reader, input_ref, output_dir)

    assert error.value.code == "capability_uncovered"
    assert imported_numeric == []
    assert not output_dir.exists()


def test_worker_local_entry_source_has_no_core_store_or_dispatch_imports():
    import ast

    source = Path(worker_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1 and node.module:
                imported.add(node.module)
            elif node.level == 0 and node.module:
                imported.add(node.module.split(".", 1)[0])
        elif isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)

    assert imported & {"api", "delivery", "review", "runner", "sources", "store"} == set()
    assert {"contracts", "data", "models", "results"} <= imported


def test_frozen_reader_rejects_path_replaced_after_lstat_before_open(tmp_path: Path, monkeypatch):
    CaseStore(tmp_path, create=True)
    case_id = str(uuid.uuid4())
    prepared_id = str(uuid.uuid4())
    relative = f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/input/model.py"
    target = tmp_path.joinpath(*relative.split("/"))
    target.parent.mkdir(parents=True)
    original = b"original frozen model bytes"
    target.write_bytes(original)
    ref = ref_from_bytes(relative, original, "text/x-python; charset=utf-8")
    reader = FrozenReader(tmp_path, case_id, prepared_id)
    real_open = __import__("os").open
    replaced = False

    def replacing_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if not replaced and Path(path) == target:
            replacement = target.with_name("replacement.tmp")
            replacement.write_bytes(b"replacement bytes")
            replacement.replace(target)
            replaced = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("optagent_core.worker.os.open", replacing_open)
    with pytest.raises(ContractError) as error:
        reader.read_ref(ref)
    assert replaced is True
    assert error.value.code == "source_changed_during_read"


def test_fixed_worker_launcher_accepts_null_data_until_profile_gate(core):
    from optagent_core.api import dispatch_dynamic
    from optagent_core.runner import dispatch_run_bridge

    workspace = core["workspace"]
    store = core["store"]()
    case_id, _, _ = core["create_case"]()
    model_path = workspace / "model.py"
    model_path.write_text("def build_model(data):\n    return None\n", encoding="utf-8")
    state = store.read_case(case_id)
    candidate_reply = core["call"]("optagent.case", {
        "op": "revise",
        "kind": "candidate",
        "case_id": case_id,
        "expected_state_revision": state["state_revision"],
        "model_path": "model.py",
        "local_module_paths": [],
        "data_id": None,
        "backend_id": "highs",
        "objective_plan": {"mode": "feasibility", "preference_note": "profile boundary test"},
        "business_checker_path": None,
        "input_closure": "declared",
        "closure_note": "A valid no-data Pyomo candidate for the fixed worker boundary.",
    })
    assert candidate_reply["ok"], candidate_reply
    candidate_id = candidate_reply["result"]["candidate"]["id"]
    state = store.read_case(case_id)
    options = {
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
    prepared_reply = core["call"]("optagent.run", {
        "op": "prepare",
        "case_id": case_id,
        "expected_state_revision": state["state_revision"],
        "candidate_id": candidate_id,
        "solve_options": options,
    })
    assert prepared_reply["ok"], prepared_reply
    prepared_id = prepared_reply["result"]["prepared"]["id"]
    state = store.read_case(case_id)
    execution_context = core["context"]()
    started = dispatch_dynamic("optagent.run", {
        "op": "start",
        "case_id": case_id,
        "expected_state_revision": state["state_revision"],
        "prepared_id": prepared_id,
    }, execution_context)
    assert started["kind"] == "start_plan", started
    start_plan = started["plan"]
    run_id = start_plan["run_id"]
    rechecked = dispatch_run_bridge(store, {
        "kind": "start_recheck",
        "case_id": case_id,
        "run_id": run_id,
        "prepared_id": prepared_id,
        "dispatch_nonce": start_plan["dispatch_nonce"],
        "request_digest": start_plan["request_digest"],
    }, execution_context)
    assert rechecked["kind"] == "start_ready", rechecked
    invocation_ref = rechecked["plan"]["worker_invocation"]
    code = _launcher_code(str(workspace), invocation_ref["path"], invocation_ref["sha256"])
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code],
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0
    seal = strict_json_loads(store.resolve_path(f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/seal.json", must_exist=True).read_bytes())
    assert seal["worker_completed"] is False
    failures = [ref for ref in seal["output_refs"] if ref["path"].endswith("/worker-failure.json")]
    assert len(failures) == 1
    failure = strict_json_loads(store.read_ref(failures[0]))
    assert failure["error_code"] in {"capability_uncovered", "environment_unavailable"}, result.stderr


def test_large_primal_jsonl_is_streamed_and_content_addressed(tmp_path: Path):
    store = CaseStore(tmp_path, create=True)
    case_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    output_path = f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output"
    count = 0

    def rows():
        nonlocal count
        for index in range(12_000):
            count += 1
            yield {"component": "x", "key": [index], "value": float(index)}

    ref = _write_stream_output(
        tmp_path,
        output_path,
        "instances/000000/layer-single/raw-primals.jsonl",
        _jsonl_chunks(rows()),
        "application/x-ndjson",
    )
    expected_raw = b"".join(canonical_json_bytes({"component": "x", "key": [i], "value": float(i)}) + b"\n" for i in range(12_000))
    assert count == 12_000
    assert ref["byte_len"] == len(expected_raw)
    assert ref["sha256"] == hashlib.sha256(expected_raw).hexdigest()
    assert store.resolve_path(ref["path"], must_exist=True).read_bytes() == expected_raw

    same = _write_stream_output(
        tmp_path,
        output_path,
        "instances/000000/layer-single/raw-primals.jsonl",
        _jsonl_chunks(rows()),
        "application/x-ndjson",
    )
    assert same == ref
    assert count == 24_000

    with pytest.raises(ContractError) as error:
        _write_stream_output(
            tmp_path,
            output_path,
            "instances/000000/layer-single/raw-primals.jsonl",
            _jsonl_chunks([{"component": "x", "key": [], "value": -1.0}]),
            "application/x-ndjson",
        )
    assert error.value.code == "artifact_changed"


def test_failure_seal_keeps_a_real_backend_return_without_a_completed_result(tmp_path: Path, monkeypatch):
    CaseStore(tmp_path, create=True)
    case_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    prepared_id = str(uuid.uuid4())
    output_path = f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output"
    input_ref = ref_from_bytes(
        f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/input/manifest.json",
        b"{}",
        "application/json",
    )
    monkeypatch.setitem(_OUTPUT_REFS, output_path, {})
    _json_output(
        tmp_path,
        output_path,
        "instances/000000/layer-single/backend-observation.json",
        {"observation": {"solver_returned": True, "raw_status": "kTimeLimit"}},
    )

    _fail_seal(
        tmp_path,
        {
            "case_id": case_id,
            "run_id": run_id,
            "prepared_id": prepared_id,
            "dispatch_nonce": str(uuid.uuid4()),
            "input_digest": "a" * 64,
            "output_path": output_path,
        },
        "a" * 64,
        "2026-10-09T00:00:00Z",
        ContractError("capability_uncovered", "check failed after solver observation"),
        [input_ref],
    )

    seal_path = tmp_path / output_path / "seal.json"
    seal = strict_json_loads(seal_path.read_bytes())
    assert seal["worker_completed"] is False
    assert seal["solver_returned"] is True
    assert [ref["path"].rsplit("/", 1)[-1] for ref in seal["output_refs"]] == [
        "backend-observation.json",
        "worker-failure.json",
    ]


def test_raw_output_and_failure_seal_publish_through_linux_link_fallback(tmp_path: Path, monkeypatch):
    CaseStore(tmp_path, create=True)
    case_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    prepared_id = str(uuid.uuid4())
    output_path = f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output"
    output_dir = tmp_path / output_path
    raw = b'{"component":"x","key":[],"value":1.0}\n'
    link_destinations = []

    class NativeCall:
        argtypes = None
        restype = None

        def __call__(self, *_arguments):
            contracts.ctypes.set_errno(errno.EINVAL)
            return -1

    class FakeLibc:
        renameat2 = NativeCall()

    monkeypatch.setattr(contracts.sys, "platform", "linux")
    monkeypatch.setattr(contracts.ctypes, "CDLL", lambda _name, *, use_errno: FakeLibc())
    real_link = contracts.os.link

    def observe_real_link(source, destination, *, follow_symlinks=True):
        link_destinations.append(Path(os.fsdecode(destination)).name)
        return real_link(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(contracts.os, "link", observe_real_link)
    monkeypatch.setitem(_OUTPUT_REFS, output_path, {})

    raw_ref = _write_stream_output(
        tmp_path,
        output_path,
        "raw-primals.jsonl",
        [raw],
        "application/x-ndjson",
    )
    input_ref = ref_from_bytes(
        f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/input/manifest.json",
        b"{}",
        "application/json",
    )
    _fail_seal(
        tmp_path,
        {
            "run_id": run_id,
            "prepared_id": prepared_id,
            "dispatch_nonce": str(uuid.uuid4()),
            "output_path": output_path,
        },
        "a" * 64,
        "2026-10-29T00:00:00Z",
        ContractError("capability_uncovered", "synthetic failure after output"),
        [input_ref],
    )

    raw_path = output_dir / "raw-primals.jsonl"
    failure_path = output_dir / "worker-failure.json"
    seal_path = output_dir / "seal.json"
    failure = strict_json_loads(failure_path.read_bytes())
    seal = strict_json_loads(seal_path.read_bytes())
    assert raw_path.read_bytes() == raw
    assert raw_ref["path"].endswith("/raw-primals.jsonl")
    assert failure["error_code"] == "capability_uncovered"
    assert seal["worker_completed"] is False
    assert {ref["path"] for ref in seal["output_refs"]} == {
        raw_ref["path"],
        f"{output_path}/worker-failure.json",
    }
    assert link_destinations == ["raw-primals.jsonl", "worker-failure.json", "seal.json"]
    assert list(output_dir.rglob("*.tmp")) == []

