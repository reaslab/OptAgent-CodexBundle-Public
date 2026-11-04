from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping

import pytest

from optagent_core.contracts import (
    ContractError,
    canonical_json_bytes,
    ref_from_bytes,
    sha256_bytes,
    strict_json_loads,
)
from optagent_core.data import PortableReader, load_data_bundle, normalize_data
from optagent_core.delivery import (
    _package_data_bundle,
    _package_prepared_closure,
    _read_verified_output_json,
)
from optagent_core.sources import data_source_records


_DEFAULT_JSON_LIMIT = 4 * 1024 * 1024


class _RefReader:
    def __init__(self, payloads: Mapping[tuple[str, str, int, str], bytes], workspace: Path | None = None) -> None:
        self.payloads = dict(payloads)
        self.workspace = workspace
        self.source_records: dict[str, dict[str, Any]] = {}
        self.source_record_refs: dict[str, dict[str, Any]] = {}

    def read_ref(self, ref: Mapping[str, Any]) -> bytes:
        key = (ref["path"], ref["sha256"], ref["byte_len"], ref["media_type"])
        raw = self.payloads.get(key)
        if raw is None:
            raise ContractError("capture_unavailable", "test Ref is not mapped")
        if len(raw) != ref["byte_len"] or sha256_bytes(raw) != ref["sha256"]:
            raise ContractError("artifact_changed", "test Ref bytes changed")
        return raw

    def add_ref(self, ref: Mapping[str, Any], raw: bytes) -> None:
        self.payloads[(ref["path"], ref["sha256"], ref["byte_len"], ref["media_type"])] = raw

    def write_immutable(self, path: str, raw: bytes, media_type: str) -> dict[str, Any]:
        ref = ref_from_bytes(path, raw, media_type)
        self.add_ref(ref, raw)
        return ref

    def ref_for_path(self, path: str) -> dict[str, Any]:
        for ref_path, digest, byte_len, media_type in reversed(tuple(self.payloads)):
            if ref_path == path:
                return {"path": ref_path, "sha256": digest, "byte_len": byte_len, "media_type": media_type}
        raise ContractError("not_found", "test path has no exact Ref")

    def read_workspace_file(self, relative_path: str):
        if self.workspace is None:
            raise AssertionError("test reader has no runtime workspace")
        raw = (self.workspace / Path(relative_path)).read_bytes()
        media_type = "text/x-python; charset=utf-8" if relative_path.endswith(".py") else "application/json"
        return raw, ref_from_bytes(relative_path, raw, media_type)


@pytest.fixture
def short_core_root(tmp_path):
    from test_delivery import _core_at

    root = tmp_path.parents[1] / f"b{uuid.uuid4().hex[:7]}"
    root.mkdir()
    workspace = root / "w"
    workspace.mkdir()
    try:
        yield _core_at(workspace), root
    finally:
        shutil.rmtree(root)


def _bundle_reader(values_raw: bytes, *, large_manifest: bool = False, large_trace: bool = False):
    case_id = "00000000-0000-4000-8000-000000000001"
    data_id = "00000000-0000-4000-8000-000000000002"
    reader = _RefReader({})
    source_id = "00000000-0000-4000-8000-000000000005"
    source_raw = b"id,value\nA,7\n"
    source_bytes_ref = ref_from_bytes(
        f".reaslab/optagent/source-bytes/{sha256_bytes(source_raw)}", source_raw, "application/octet-stream"
    )
    source_record = {
        "source_id": source_id,
        "purpose": "data",
        "kind": "file",
        "bytes": source_bytes_ref,
        "original_sha256": source_bytes_ref["sha256"],
        "original_size": len(source_raw),
    }
    reader.source_records = {source_id: source_record}
    reader.add_ref(source_bytes_ref, source_raw)
    source_record_raw = canonical_json_bytes(source_record)
    source_record_ref = ref_from_bytes(
        f".reaslab/optagent/cases/{case_id}/sources/{source_id}.json", source_record_raw, "application/json"
    )
    reader.source_record_refs = {source_id: source_record_ref}
    reader.add_ref(source_record_ref, source_record_raw)
    plan = {
        "encoding": "utf-8",
        "blocks": [{
            "object_id": "items",
            "source_id": source_id,
            "format": "csv",
            "layout": {"delimiter": ",", "header": "first_row", "skip_lines": 0},
            "kind": "table",
            "key_fields": ["id"],
            "columns": [
                {"name": "id", "position": None, "input_name": "id", "type": "string", "unit": None, "nullable": False},
                {"name": "value", "position": None, "input_name": "value", "type": "integer", "unit": None, "nullable": False},
            ],
            "duplicate_policy": "error",
            "missing_policy": "error",
        }],
        "conversions": [],
    }
    produced = normalize_data(
        reader,
        data_id=data_id,
        source_records=reader.source_records,
        source_bytes={source_id: source_raw},
        plan=plan,
        target_prefix=f".reaslab/optagent/cases/{case_id}/data/{data_id}",
    )
    manifest = produced["manifest"]
    produced_value_ref = manifest["objects"][0]["values"]
    value_ref = ref_from_bytes(produced_value_ref["path"], values_raw, "application/json")
    reader.add_ref(value_ref, values_raw)
    manifest["objects"][0]["values"] = value_ref
    trace_ref = manifest["trace"]
    trace_raw = reader.read_ref(trace_ref)
    if large_trace:
        trace_raw = b" " * (_DEFAULT_JSON_LIMIT + 3) + trace_raw
        trace_ref = ref_from_bytes(trace_ref["path"], trace_raw, "application/json")
        reader.add_ref(trace_ref, trace_raw)
        manifest["trace"] = trace_ref
    base = {key: value for key, value in manifest.items() if key != "digest"}
    manifest["digest"] = sha256_bytes(canonical_json_bytes(base))
    manifest_raw = canonical_json_bytes(manifest)
    if large_manifest:
        manifest_raw = b" " * (_DEFAULT_JSON_LIMIT + 5) + manifest_raw
    manifest_ref = ref_from_bytes(
        f".reaslab/optagent/cases/{case_id}/data/{data_id}/manifest.json",
        manifest_raw,
        "application/json",
    )
    reader.add_ref(manifest_ref, manifest_raw)
    return (
        reader,
        manifest_ref,
        value_ref,
        trace_ref,
        manifest,
    )


def test_loader_uses_exact_large_ref_lengths_without_changing_default_control_cap():
    values_raw = b" " * (_DEFAULT_JSON_LIMIT + 11) + b'[{"id":"A","value":7}]'
    reader, manifest_ref, value_ref, trace_ref, manifest = _bundle_reader(
        values_raw,
        large_manifest=True,
        large_trace=True,
    )
    manifest_raw = reader.read_ref(manifest_ref)
    trace_raw = reader.read_ref(trace_ref)

    for raw in (manifest_raw, values_raw, trace_raw):
        with pytest.raises(ValueError, match="too large"):
            strict_json_loads(raw)

    bundle = load_data_bundle(
        reader,
        manifest_ref,
        candidate_binding_digest="a" * 64,
        source_records=reader.source_records,
    )
    assert bundle.data_id == manifest["data_id"]
    assert bundle.digest == manifest["digest"]
    assert bundle.candidate_binding_digest == "a" * 64
    assert bundle.table("items") == {("A",): {"id": "A", "value": 7}}
    assert len(values_raw) > _DEFAULT_JSON_LIMIT
    assert len(manifest_raw) > _DEFAULT_JSON_LIMIT
    assert len(trace_raw) > _DEFAULT_JSON_LIMIT

    files: dict[str, bytes] = {}
    objects: list[dict[str, Any]] = []
    ref_map: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    packaged = _package_data_bundle(
        reader,
        "00000000-0000-4000-8000-000000000001",
        manifest["data_id"],
        files,
        objects,
        ref_map,
        reader.source_records,
        reader.source_record_refs,
    )
    assert packaged["manifest"] == manifest_ref
    assert files[f"data/{manifest['data_id']}/manifest.json"] == manifest_raw
    assert files[f"data/{manifest['data_id']}/trace.json"] == trace_raw
    portable_reader = _RefReader(
        {
            (item["ref"]["path"], item["ref"]["sha256"], item["ref"]["byte_len"], item["ref"]["media_type"]): files[item["package_path"]]
            for item in ref_map.values()
        }
    )
    portable_reader.source_records = reader.source_records
    source_record_ref = reader.source_record_refs[next(iter(reader.source_records))]
    assert any(item["ref"] == source_record_ref for item in ref_map.values())
    portable = load_data_bundle(
        portable_reader,
        manifest_ref,
        source_records=portable_reader.source_records,
    )
    assert portable.table("items") == bundle.table("items")

    tampered = _RefReader(
        {
            (value_ref["path"], value_ref["sha256"], value_ref["byte_len"], value_ref["media_type"]): values_raw + b" "
        }
    )
    with pytest.raises(ContractError) as caught:
        tampered.read_ref(value_ref)
    assert caught.value.code == "artifact_changed"


@pytest.mark.parametrize(
    ("raw", "description"),
    [
        (b'[{"id":"A","id":"B","value":7}]', "duplicate object key"),
        (b'[{"id":"A","value":1e999}]', "non-finite number"),
        (b"[" + b"[" * 64 + b"0" + b"]" * 64 + b"]", "65-level tree"),
    ],
)
def test_large_ref_capacity_does_not_skip_json_integrity_checks(raw: bytes, description: str):
    del description
    reader, manifest_ref, _, _, _ = _bundle_reader(b" " * (_DEFAULT_JSON_LIMIT + 1) + raw)
    with pytest.raises(ContractError) as caught:
        load_data_bundle(reader, manifest_ref, source_records=reader.source_records)
    assert caught.value.code == "output_invalid"


def test_bulk_values_still_reject_a_noncanonical_primary_key_type():
    raw = b'[{"id":1.25,"value":7}]'
    reader, manifest_ref, _, _, _ = _bundle_reader(raw)
    manifest_ref = dict(manifest_ref)
    manifest_raw = reader.read_ref(manifest_ref)
    manifest = strict_json_loads(manifest_raw)
    manifest["objects"][0]["columns"][0]["type"] = "number"
    manifest["import_plan"]["blocks"][0]["columns"][0]["type"] = "number"
    trace = strict_json_loads(reader.read_ref(manifest["trace"]))
    trace["plan"] = manifest["import_plan"]
    trace_raw = canonical_json_bytes(trace)
    trace_ref = ref_from_bytes(manifest["trace"]["path"], trace_raw, "application/json")
    reader.payloads.pop((
        manifest["trace"]["path"], manifest["trace"]["sha256"],
        manifest["trace"]["byte_len"], manifest["trace"]["media_type"],
    ))
    reader.add_ref(trace_ref, trace_raw)
    manifest["trace"] = trace_ref
    base = {key: value for key, value in manifest.items() if key != "digest"}
    manifest["digest"] = sha256_bytes(canonical_json_bytes(base))
    manifest_raw = canonical_json_bytes(manifest)
    replacement_ref = ref_from_bytes(manifest_ref["path"], manifest_raw, "application/json")
    reader.payloads.pop((manifest_ref["path"], manifest_ref["sha256"], manifest_ref["byte_len"], manifest_ref["media_type"]))
    reader.payloads[(replacement_ref["path"], replacement_ref["sha256"], replacement_ref["byte_len"], replacement_ref["media_type"])] = manifest_raw

    with pytest.raises(ContractError, match="primary key") as caught:
        load_data_bundle(reader, replacement_ref, source_records=reader.source_records)
    assert caught.value.code == "output_invalid"


def test_delivery_large_output_parse_requires_exact_ref_in_verified_seal():
    case_id = "00000000-0000-4000-8000-000000000003"
    run_id = "00000000-0000-4000-8000-000000000004"
    raw = b" " * (_DEFAULT_JSON_LIMIT + 2) + b'{"status":"pass","coverage":[]}'
    ref = ref_from_bytes(
        f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/instance-0/check.json",
        raw,
        "application/json",
    )
    reader = _RefReader({(ref["path"], ref["sha256"], ref["byte_len"], ref["media_type"]): raw})
    verified = {"seal": {"output_refs": [ref]}}

    assert _read_verified_output_json(reader, verified, ref) == {"status": "pass", "coverage": []}
    unrelated = {**ref, "path": ref["path"].replace("check.json", "unsealed.json")}
    with pytest.raises(ContractError) as caught:
        _read_verified_output_json(reader, verified, unrelated)
    assert caught.value.code == "artifact_changed"


def test_prepared_closure_bulk_data_refs_keep_original_identity(short_core_root):
    _, short_root = short_core_root
    values_raw = b" " * (_DEFAULT_JSON_LIMIT + 17) + b'[{"id":"A","value":7}]'
    reader, data_ref, _, _, data_manifest = _bundle_reader(
        values_raw,
        large_manifest=True,
        large_trace=True,
    )
    runtime_workspace = short_root / "runtime-workspace"
    runtime_workspace.mkdir()
    reader.workspace = runtime_workspace

    case_id = "00000000-0000-4000-8000-000000000001"
    prepared_id = "00000000-0000-4000-8000-000000000005"
    source_id = next(iter(reader.source_records))
    original_source = reader.source_records[source_id]
    original_source_bytes = reader.read_ref(original_source["bytes"])
    frozen_source_prefix = f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/input/sources/{source_id}"
    frozen_source_bytes = ref_from_bytes(
        f"{frozen_source_prefix}/bytes.bin",
        original_source_bytes,
        "application/octet-stream",
    )
    reader.add_ref(frozen_source_bytes, original_source_bytes)
    frozen_source_record = {**original_source, "bytes": frozen_source_bytes}
    frozen_source_record_raw = canonical_json_bytes(frozen_source_record)
    frozen_source_record_ref = ref_from_bytes(
        f"{frozen_source_prefix}/record.json",
        frozen_source_record_raw,
        "application/json",
    )
    reader.add_ref(frozen_source_record_ref, frozen_source_record_raw)

    trace_raw = reader.read_ref(data_manifest["trace"])
    trace_value = strict_json_loads(trace_raw, max_bytes=len(trace_raw))
    trace_value["source_refs"] = [frozen_source_bytes]
    frozen_trace_raw = b" " * (_DEFAULT_JSON_LIMIT + 3) + canonical_json_bytes(trace_value)
    trace_ref = ref_from_bytes(data_manifest["trace"]["path"], frozen_trace_raw, "application/json")
    reader.add_ref(trace_ref, frozen_trace_raw)
    data_manifest["trace"] = trace_ref
    data_material = {key: value for key, value in data_manifest.items() if key != "digest"}
    data_manifest["digest"] = sha256_bytes(canonical_json_bytes(data_material))
    manifest_raw = b" " * (_DEFAULT_JSON_LIMIT + 5) + canonical_json_bytes(data_manifest)
    data_ref = ref_from_bytes(data_ref["path"], manifest_raw, "application/json")
    reader.add_ref(data_ref, manifest_raw)

    code_path = f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/input/source/model.py"
    code_raw = b"def build_model(data):\n    return None\n"
    code_ref = ref_from_bytes(code_path, code_raw, "text/x-python; charset=utf-8")
    reader.add_ref(code_ref, code_raw)
    helper_paths = (
        "python/optagent_core/__init__.py",
        "python/optagent_core/contracts.py",
        "python/optagent_core/data.py",
        "python/optagent_core/models.py",
        "python/optagent_core/results.py",
        "python/optagent_core/worker.py",
    )
    helper_files = []
    helper_bytes = {}
    for relative in helper_paths:
        raw = b"def run_local_reproduction():\n    return {}\n" if relative.endswith("worker.py") else b"# fixed test helper\n"
        helper_bytes[relative] = raw
        helper_files.append({"path": relative, "sha256": sha256_bytes(raw), "byte_len": len(raw)})
    identity = {"schema_version": 1, "files": helper_files}
    runtime_digest = sha256_bytes(json.dumps(identity, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    runtime_prefix = f".reaslab/optagent/runtime/{runtime_digest}/"
    for relative, raw in helper_bytes.items():
        destination = runtime_workspace / runtime_prefix / Path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        helper_ref = ref_from_bytes(runtime_prefix + relative, raw, "text/x-python; charset=utf-8")
        reader.add_ref(helper_ref, raw)
    complete_path = runtime_workspace / runtime_prefix / "COMPLETE"
    complete_path.parent.mkdir(parents=True, exist_ok=True)
    complete_path.write_bytes(runtime_digest.encode("ascii"))
    runtime_manifest = {"schema_version": 1, "digest": runtime_digest, "files": helper_files}
    runtime_manifest_raw = canonical_json_bytes(runtime_manifest)
    runtime_manifest_ref = ref_from_bytes(runtime_prefix + "manifest.json", runtime_manifest_raw, "application/json")
    reader.add_ref(runtime_manifest_ref, runtime_manifest_raw)

    prepared = {
        "schema_version": 2,
        "prepared_id": prepared_id,
        "case_id": case_id,
        "source_revision": 1,
        "candidate_revision": 1,
        "candidate_id": "00000000-0000-4000-8000-000000000006",
        "promotion_generation": 0,
        "binding_digest": "b" * 64,
        "sources": [{
            "source_id": source_id,
            "record": frozen_source_record_ref,
            "bytes": frozen_source_bytes,
        }],
        "candidate": {
            "authoring": "pyomo",
            "model": {"original_path": "model.py", "bytes": code_ref},
            "local_modules": [],
            "business_checker": None,
            "backend_id": "highs",
            "objective_plan": {"mode": "feasibility"},
            "input_closure": "declared",
            "closure_note": "Fixture only.",
        },
        "data": {"data_id": data_manifest["data_id"], "manifest": data_ref, "instance_parameter_object_id": None},
        "runtime_bundle": {
            "digest": runtime_digest,
            "manifest": runtime_manifest_ref,
            "worker_entry": "python/optagent_core/worker.py",
        },
        "solve_options": {"instance_keys": []},
    }
    input_path = f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/input/manifest.json"
    input_raw = canonical_json_bytes(prepared)
    input_ref = ref_from_bytes(input_path, input_raw, "application/json")
    reader.add_ref(input_ref, input_raw)

    files: dict[str, bytes] = {}
    objects: list[dict[str, Any]] = []
    ref_map: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    packaged = _package_prepared_closure(reader, input_ref, case_id, prepared_id, files, objects, ref_map)
    assert packaged["manifest"]["data"] == prepared["data"]
    assert packaged["runtime"]["digest"] == runtime_digest
    assert files[f"reproduce/prepared/{prepared_id}/data/manifest.json"] == reader.read_ref(data_ref)
    assert files[f"reproduce/prepared/{prepared_id}/data/trace.json"] == reader.read_ref(data_manifest["trace"])
    assert len(files[f"reproduce/prepared/{prepared_id}/data/manifest.json"]) > _DEFAULT_JSON_LIMIT
    assert len(files[f"reproduce/prepared/{prepared_id}/data/trace.json"]) > _DEFAULT_JSON_LIMIT


def test_public_json_source_normalize_inspect_and_candidate_zip_keep_large_bytes(short_core_root):
    core, short_root = short_core_root
    case_id, _, _ = core["create_case"](raw=b"Normalize this explicitly declared JSON value.")
    input_dir = core["workspace"] / "inputs"
    input_dir.mkdir(exist_ok=True)
    payload = "x" * (_DEFAULT_JSON_LIMIT + 64)
    source_raw = canonical_json_bytes({"payload": payload})
    (input_dir / "large.json").write_bytes(source_raw)

    state = core["store"]().read_case(case_id)
    captured = core["call"](
        "optagent.data",
        {
            "op": "capture",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "path": "inputs/large.json",
            "purpose": "data",
            "locations": [],
        },
    )
    assert captured["ok"], captured
    source_id = captured["result"]["source"]["id"]
    plan = {
        "encoding": "utf-8",
        "blocks": [
            {
                "object_id": "payload",
                "source_id": source_id,
                "format": "json",
                "layout": {"pointer": "/payload"},
                "kind": "scalar",
                "key_fields": [],
                "columns": [
                    {
                        "name": "payload",
                        "position": None,
                        "input_name": "payload",
                        "type": "string",
                        "unit": None,
                        "nullable": False,
                    }
                ],
                "duplicate_policy": "error",
                "missing_policy": "error",
            }
        ],
        "conversions": [],
    }
    state = core["store"]().read_case(case_id)
    normalized = core["call"](
        "optagent.data",
        {
            "op": "normalize",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "source_ids": [source_id],
            "plan": plan,
        },
    )
    assert normalized["ok"], normalized
    data_id = normalized["result"]["data_manifest"]["data_id"]
    manifest_ref = normalized["result"]["data_manifest"]["manifest_ref"]
    store = core["store"]()
    bundle = load_data_bundle(
        store,
        manifest_ref,
        source_records=data_source_records(
            store,
            case_id=case_id,
            source_ids=[source_id],
            state=store.read_case(case_id),
        ),
    )
    assert len(bundle.object("payload").rows[0].encode("utf-8")) > _DEFAULT_JSON_LIMIT
    assert bundle.object("payload").rows[0] == payload

    inspected = core["call"](
        "optagent.data",
        {"op": "inspect", "case_id": case_id, "data_id": data_id, "sample_rows": 0},
    )
    assert inspected["ok"], inspected
    assert inspected["result"]["samples"][0]["total"] == 1
    assert inspected["result"]["samples"][0]["rows"] == []

    (core["workspace"] / "model.py").write_text("def build_model(data):\n    return None\n", encoding="utf-8")
    state = core["store"]().read_case(case_id)
    candidate = core["call"](
        "optagent.case",
        {
            "op": "revise",
            "kind": "candidate",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "model_path": "model.py",
            "local_module_paths": [],
            "data_id": data_id,
            "backend_id": "highs",
            "objective_plan": {"mode": "feasibility", "preference_note": "byte-admission fixture"},
            "business_checker_path": None,
            "input_closure": "declared",
            "closure_note": "The captured JSON payload is the fixed input.",
        },
    )
    assert candidate["ok"], candidate

    from test_delivery import _select_candidate_data_delivery

    store = core["store"]()
    delivery_id = _select_candidate_data_delivery(core, case_id)
    state = store.read_case(case_id)
    packed = core["call"](
        "optagent.deliver",
        {
            "op": "package",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "delivery_id": delivery_id,
            "target_path": "exports/large-json-data.zip",
            "include_raw_sources": False,
        },
    )
    assert packed["ok"], packed
    archive_root = short_root / "package"
    archive_root.mkdir()
    with zipfile.ZipFile(BytesIO(store.read_ref(packed["result"]["package_ref"]))) as archive:
        package_manifest = strict_json_loads(archive.read("manifest.json"))
        archive.extractall(archive_root)
    object_id = "payload"
    values_ref = strict_json_loads(store.read_ref(manifest_ref))["objects"][0]["values"]
    token = hashlib.sha256(object_id.encode("utf-8")).hexdigest()
    assert (archive_root / "data" / data_id / "objects" / f"{token}.json").read_bytes() == store.read_ref(values_ref)
    portable = PortableReader(archive_root, package_manifest["ref_map"])
    portable_data = package_manifest["data_bundles"][0]
    portable_manifest = strict_json_loads(portable.read_ref(portable_data["manifest"]))
    portable_source_records = data_source_records(
        store,
        case_id=case_id,
        source_ids=portable_manifest["source_ids"],
        state=store.read_case(case_id),
    )
    for source_id in portable_manifest["source_ids"]:
        source_record_path = f".reaslab/optagent/cases/{case_id}/sources/{source_id}.json"
        source_record_ref = store.ref_for_path(source_record_path)
        source_bytes_ref = portable_source_records[source_id]["bytes"]
        mapped_paths = {
            (item["ref"]["path"], item["ref"]["sha256"], item["ref"]["byte_len"], item["ref"]["media_type"]): item["package_path"]
            for item in package_manifest["ref_map"]
        }
        assert mapped_paths[(
            source_record_ref["path"], source_record_ref["sha256"],
            source_record_ref["byte_len"], source_record_ref["media_type"],
        )] == f"data/{data_id}/sources/{source_id}/record.json"
        assert mapped_paths[(
            source_bytes_ref["path"], source_bytes_ref["sha256"],
            source_bytes_ref["byte_len"], source_bytes_ref["media_type"],
        )] == f"data/{data_id}/sources/{source_id}/bytes.bin"
        assert portable.read_ref(source_record_ref) == store.read_ref(source_record_ref)
        assert portable.read_ref(source_bytes_ref) == store.read_ref(source_bytes_ref)
    portable_bundle = load_data_bundle(
        portable,
        manifest_ref,
        candidate_binding_digest=store.read_record(
            f".reaslab/optagent/cases/{case_id}/candidates/{state['candidate_id']}.json"
        )["binding_digest"],
        source_records=portable_source_records,
    )
    assert portable_bundle.object("payload").rows[0] == payload

    bootstrap = Path(__file__).resolve().parents[1] / "optagent_reproduce.py"
    reproduce_dir = archive_root / "reproduce"
    reproduce_dir.mkdir()
    bootstrap_copy = reproduce_dir / "run.py"
    bootstrap_copy.write_bytes(bootstrap.read_bytes())
    child = subprocess.run(
        [sys.executable, "-I", str(bootstrap_copy), "--output-dir", str(short_root / "unused")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 2
    assert "no supported standalone native reproduction entry" in child.stderr
    assert "exceeds the fixed size limit" not in child.stderr
    assert not (short_root / "unused").exists()
