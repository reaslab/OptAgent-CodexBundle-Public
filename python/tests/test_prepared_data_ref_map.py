from __future__ import annotations

import hashlib
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
from io import BytesIO
from pathlib import Path, PurePosixPath

import pytest

from optagent_core.contracts import ContractError, canonical_json_bytes, ref_from_bytes, strict_json_loads
from optagent_core.data import PortableReader, load_data_bundle, read_prepared_source_records
from optagent_core.results import _require_supported_platform
from test_delivery import _core_at, _public_close_claim, _schema3_fixture_backend_observation


_SOURCE = Path(__file__).resolve().parents[2]
_OWNER_MARKER = ".prepared_mapping_fix_owner"
_WORKSPACE_PREFIX = "optagent-prepared-ref-map-"


def _new_workspace() -> tuple[Path, str]:
    owner = uuid.uuid4().hex
    workspace = Path(tempfile.mkdtemp(prefix=_WORKSPACE_PREFIX))
    (workspace / _OWNER_MARKER).write_text(owner, encoding="ascii")
    return workspace, owner


def _remove_owned_workspace(workspace: Path, owner: str) -> None:
    temp_root = Path(tempfile.gettempdir()).resolve()
    resolved = workspace.resolve()
    if resolved.parent != temp_root or not resolved.name.startswith(_WORKSPACE_PREFIX):
        raise RuntimeError("refusing to remove a path outside this test's temporary workspace")
    marker = resolved / _OWNER_MARKER
    if not marker.is_file() or marker.read_text(encoding="ascii") != owner:
        raise RuntimeError("refusing to remove a workspace without this test's ownership marker")
    info = workspace.lstat()
    reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if workspace.is_symlink() or stat.S_ISLNK(info.st_mode) or reparse:
        raise RuntimeError("refusing to remove a reparse workspace")
    shutil.rmtree(resolved)


def _data_plan(source_id: str, object_id: str) -> dict:
    return {
        "encoding": "utf-8",
        "blocks": [{
            "object_id": object_id,
            "source_id": source_id,
            "format": "csv",
            "layout": {"delimiter": ",", "header": "first_row", "skip_lines": 0},
            "kind": "table",
            "key_fields": ["site"],
            "columns": [
                {"name": "site", "position": None, "input_name": "site", "type": "string", "unit": None, "nullable": False},
                {"name": "cost", "position": None, "input_name": "cost", "type": "number", "unit": "USD", "nullable": False},
            ],
            "duplicate_policy": "error",
            "missing_policy": "error",
        }],
        "conversions": [],
    }


def _ref_map_row(package_manifest: dict, ref: dict) -> dict:
    matches = [row for row in package_manifest["ref_map"] if row["ref"] == ref]
    assert len(matches) == 1
    return matches[0]


def _ref_map_row_by_path(package_manifest: dict, path: str) -> dict:
    matches = [row for row in package_manifest["ref_map"] if row["ref"]["path"] == path]
    assert len(matches) == 1
    return matches[0]


def _extract_package(archive_raw: bytes, destination: Path) -> dict:
    destination.mkdir()
    with zipfile.ZipFile(BytesIO(archive_raw)) as archive:
        for member in archive.infolist():
            relative = PurePosixPath(member.filename)
            assert not relative.is_absolute()
            assert all(part not in {"", ".", ".."} for part in relative.parts)
            path = destination.joinpath(*relative.parts)
            if member.is_dir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(member))
        packaged_bootstrap = (destination / "reproduce" / "run.py").read_bytes()
        source_bootstrap = (_SOURCE / "python" / "optagent_reproduce.py").read_bytes()
        assert packaged_bootstrap == source_bootstrap, (
            "delivery ZIP reproduce/run.py must be the unchanged Source bootstrap; "
            f"package_sha256={hashlib.sha256(packaged_bootstrap).hexdigest()} "
            f"source_sha256={hashlib.sha256(source_bootstrap).hexdigest()}"
        )
        return strict_json_loads(archive.read("manifest.json"))


def _make_public_package(
    workspace: Path, object_id: str, *, requirements_bytes: bytes | None = None
) -> dict:
    from optagent_core.api import dispatch_dynamic
    from optagent_core.data import load_data_bundle
    from optagent_core.runner import _check_instance_identity, verify_runtime_bundle

    if requirements_bytes is not None:
        (workspace / "requirements.txt").write_bytes(requirements_bytes)
    core = _core_at(workspace)
    case_id, _, _ = core["create_case"](raw=b"Prepare and package one normalized object.")
    store = core["store"]()
    (workspace / "inputs").mkdir()
    (workspace / "inputs" / "sites.csv").write_bytes(b"site,cost\nA,10.5\n")

    state = store.read_case(case_id)
    captured = core["call"]("optagent.data", {
        "op": "capture", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "path": "inputs/sites.csv", "purpose": "data", "locations": ["row A"],
    })
    assert captured["ok"], captured
    source_id = captured["result"]["source"]["id"]
    state = store.read_case(case_id)
    normalized = core["call"]("optagent.data", {
        "op": "normalize", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "source_ids": [source_id], "plan": _data_plan(source_id, object_id),
    })
    assert normalized["ok"], normalized
    data_id = normalized["result"]["data_manifest"]["data_id"]
    inspected_data = core["call"]("optagent.data", {
        "op": "inspect", "case_id": case_id, "data_id": data_id, "sample_rows": 1,
    }, role="opt-verifier")
    assert inspected_data["ok"], inspected_data

    (workspace / "model.py").write_text(
        "def build_model(bundle):\n    raise RuntimeError('prepared mapping fixture reached candidate build')\n",
        encoding="utf-8",
    )
    state = store.read_case(case_id)
    candidate = core["call"]("optagent.case", {
        "op": "revise", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "candidate", "model_path": "model.py", "local_module_paths": [],
        "data_id": data_id, "instance_parameter_object_id": object_id, "backend_id": "highs",
        "objective_plan": {"mode": "feasibility", "preference_note": "Prepared RefMap regression fixture"},
        "business_checker_path": None, "input_closure": "declared", "closure_note": "Structural fixture only.",
    })
    assert candidate["ok"], candidate
    candidate_id = candidate["result"]["candidate"]["id"]
    state = store.read_case(case_id)
    prepared_reply = core["call"]("optagent.run", {
        "op": "prepare", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "candidate_id": candidate_id,
        "solve_options": {
            "execution_budget_seconds": 60, "time_limit_seconds": 30, "relative_gap": 0.0,
            "absolute_gap": 0.0, "threads": 1, "random_seed": 0,
            "feasibility_tolerance": 1e-7, "integrality_tolerance": 1e-7,
            "write_log": False, "export_model": False,
        },
    })
    assert prepared_reply["ok"], prepared_reply
    prepared_id = prepared_reply["result"]["prepared"]["id"]

    state = store.read_case(case_id)
    started = dispatch_dynamic("optagent.run", {
        "op": "start", "case_id": case_id, "prepared_id": prepared_id,
        "expected_state_revision": state["state_revision"],
    }, core["context"]())
    assert started.get("kind") == "start_plan", started
    run_id = started["plan"]["run_id"]
    from family_fixtures import read_run

    run_record, _run_ref = read_run(store, case_id, run_id)
    prepared_record = store.read_record(f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/prepared.json")
    input_manifest_raw = store.read_ref(prepared_record["input_manifest"])
    input_manifest = strict_json_loads(input_manifest_raw)
    frozen_data = input_manifest["data"]
    bundle = load_data_bundle(
        store,
        frozen_data["manifest"],
        candidate_binding_digest=input_manifest["binding_digest"],
        source_records=read_prepared_source_records(store, input_manifest["sources"]),
    )
    instance_key = ["A"]
    instance_binding = _check_instance_identity(bundle, input_manifest, instance_key)
    output_root = f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output"
    output_refs: list[dict] = []

    def put_output(relative_path: str, value: dict) -> dict:
        ref = store.write_immutable(
            f"{output_root}/{relative_path}", canonical_json_bytes(value), "application/json"
        )
        output_refs.append(ref)
        return ref

    stem = "instances/000000/layer-single"
    model_check_value = {
        "input_digest": prepared_record["input_digest"], "instance_key": instance_key,
        "instance_binding_digest": instance_binding, "status": "pass",
        "variable_coverage": {"required": 0, "present": 0, "completed": 0, "missing_count": 0},
        "component_coverage": [], "violations": None, "objective_values": [],
        "tolerances": {"feasibility": 1e-7, "integrality": 1e-7}, "uncovered": [],
    }
    model_check = put_output(f"{stem}/model-check.json", model_check_value)
    put_output(f"{stem}/business-check.json", {
        "input_digest": prepared_record["input_digest"], "instance_key": instance_key,
        "instance_binding_digest": instance_binding, "status": "uncovered",
        "checked_obligations": [], "objective_values": [], "violations": [],
        "uncovered": ["business_checker_not_provided"],
    })
    witness = put_output(f"{stem}/witness.json", {
        "input_digest": prepared_record["input_digest"], "instance_key": instance_key,
        "instance_binding_digest": instance_binding, "variables": [], "objective_values": [],
    })
    raw_primals = store.write_immutable(
        f"{output_root}/{stem}/raw-primals.jsonl", b"", "application/x-ndjson"
    )
    output_refs.append(raw_primals)
    backend_observation = _schema3_fixture_backend_observation(
        input_manifest["solve_options"],
        termination="optimal",
        objective_sense=None,
        objective_value=None,
        bound_value=None,
    )
    put_output(f"{stem}/backend-observation.json", {
        "observation": backend_observation,
        "raw_primals": raw_primals,
    })
    from optagent_core.results import _result_numerical_summary

    numerical = _result_numerical_summary(
        check_record=model_check_value,
        objective_value=None,
        objective_sense=None,
        bound=None,
        absolute_gap=None,
        termination="optimal",
        has_incumbent=True,
        requested_options=backend_observation["requested_options"],
        effective_options=backend_observation["effective_options"],
        core_gap_target={
            "relative_gap": input_manifest["solve_options"]["relative_gap"],
            "absolute_gap": input_manifest["solve_options"]["absolute_gap"],
        },
        native_observation=backend_observation,
    )
    result = put_output(f"{stem}/result.json", {
        "instance_key": instance_key, "instance_binding_digest": instance_binding, "layer": None,
        "source_revision": prepared_record["source_revision"],
        "candidate_revision": prepared_record["candidate_revision"],
        "input_digest": prepared_record["input_digest"],
        "backend": {
            "id": "highs", "version": "structural-fixture", "adapter_version": "structural-fixture",
            "python_version": "test-only", "package_versions": {},
        },
        "raw_status": "SYNTHETIC_FIXTURE", "termination": "optimal", "solver_invoked": True,
        "has_solver_incumbent": True, "has_checked_candidate": True, "witness": witness,
        "objective": None, "bound": None,
        "gap": {"absolute": None, "relative": None, "definition": "no objective or global bound"},
        "numerical": numerical, "lexicographic": None, "check": model_check, "coverage": [],
        "warnings": ["synthetic structural fixture; no solver was run"],
    })
    put_output("batch-summary.json", {
        "schema_version": 1, "planned_instance_count": 1, "started_instance_count": 1,
        "instances_with_results": 1, "instances_with_all_layers_attempted": 1,
        "partial_instance_count": 0, "remaining_instance_count": 0, "stop_reason": None,
    })

    data_manifest = strict_json_loads(store.read_ref(frozen_data["manifest"]))
    required_inputs = [prepared_record["input_manifest"], input_manifest["runtime_bundle"]["manifest"]]
    required_inputs.extend(ref for source in input_manifest["sources"] for ref in (source["record"], source["bytes"]))
    candidate_input = input_manifest["candidate"]
    required_inputs.extend([candidate_input["model"]["bytes"], *(item["bytes"] for item in candidate_input["local_modules"])])
    if candidate_input["business_checker"] is not None:
        required_inputs.append(candidate_input["business_checker"]["bytes"])
    if candidate_input.get("requirements_file") is not None:
        required_inputs.append(candidate_input["requirements_file"]["bytes"])
    required_inputs.extend([frozen_data["manifest"], data_manifest["trace"], *(item["values"] for item in data_manifest["objects"])])
    _, runtime_refs = verify_runtime_bundle(
        store.workspace, input_manifest["runtime_bundle"]["digest"], input_manifest["runtime_bundle"]["manifest"]
    )
    required_inputs.extend(runtime_refs)
    required_inputs.append(run_record["worker_invocation"])
    inputs_by_path = {ref["path"]: dict(ref) for ref in required_inputs}
    seal = {
        "schema_version": 2, "run_id": run_id, "prepared_id": prepared_id,
        "dispatch_nonce": run_record["dispatch_nonce"], "input_digest": prepared_record["input_digest"],
        "output_refs": sorted(output_refs, key=lambda ref: ref["path"]),
        "solver_returned": True, "worker_completed": False,
        "consumed_inputs": [inputs_by_path[path] for path in sorted(inputs_by_path)],
        "started_at": "2026-01-01T00:00:00Z", "completed_at": "2026-01-01T00:00:01Z",
    }
    store.write_immutable(f"{output_root}/seal.json", canonical_json_bytes(seal), "application/json")
    inspected = core["call"]("optagent.run", {
        "op": "inspect", "case_id": case_id, "run_id": run_id, "after_instance": 0, "limit": 20,
    }, role="opt-verifier")
    assert inspected["ok"], inspected
    assert inspected["result"]["run"]["result_count"] == 1

    state = store.read_case(case_id)
    begun = core["call"]("optagent.review", {
        "op": "begin", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "candidate_id": candidate_id, "proof_ids": [], "run_ids": [run_id],
    }, role="opt-verifier")
    assert begun["ok"], begun
    review_id = begun["result"]["review"]["id"]
    state = store.read_case(case_id)
    noted = core["call"]("optagent.review", {
        "op": "note", "case_id": case_id, "review_id": review_id, "expected_review_revision": 1,
        "body": "Synthetic structural evidence exercises the portable package reader only.",
        "source_locations": [{"source_id": source_id, "locator": "CSV row A"}],
    }, role="opt-verifier")
    assert noted["ok"], noted
    claim = {
        "kind": "feasibility", "scope": "The structural fixture reaches the Prepared package reader.",
        "conditions": [], "evidence_refs": [result], "support": "supported",
    }
    report_path = "reports/prepared-data-review.md"
    report_file = core["workspace"].joinpath(*report_path.split("/"))
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text("The checked Result is summarized here.\n", encoding="utf-8")
    closed = core["call"]("optagent.review", {
        "op": "submit", "case_id": case_id, "review_id": review_id,
        "expected_review_revision": 2, "record_kind": "outcome", "decision": "ready",
        "claims": [_public_close_claim(claim)], "feedback": [], "remaining": [], "report_path": report_path,
    }, role="opt-verifier")
    assert closed["ok"], closed
    state = store.read_case(case_id)
    selected = core["call"]("optagent.deliver", {
        "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "run", "candidate_id": candidate_id,
        "review_id": review_id, "proof_ids": [], "run_ids": [run_id],
    })
    assert selected["ok"], selected
    state = store.read_case(case_id)
    packaged = core["call"]("optagent.deliver", {
        "op": "package", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "delivery_id": selected["result"]["delivery"]["id"],
        "target_path": "exports/prepared-ref-map.zip", "include_raw_sources": True,
    })
    assert packaged["ok"], packaged

    package_root = workspace / "portable"
    package_manifest = _extract_package(store.read_ref(packaged["result"]["package_ref"]), package_root)
    return {
        "workspace": workspace,
        "case_id": case_id,
        "prepared_id": prepared_id,
        "package_root": package_root,
        "package_manifest": package_manifest,
        "prepared_record": prepared_record,
        "input_manifest": input_manifest,
        "object_id": object_id,
    }


@pytest.fixture(scope="module")
def payload_package():
    workspace, owner = _new_workspace()
    try:
        yield _make_public_package(workspace, "payload")
    finally:
        _remove_owned_workspace(workspace, owner)


@pytest.fixture(scope="module")
def long_id_package():
    workspace, owner = _new_workspace()
    try:
        yield _make_public_package(workspace, "a" * 64)
    finally:
        _remove_owned_workspace(workspace, owner)


@pytest.fixture(scope="module")
def requirements_package():
    workspace, owner = _new_workspace()
    try:
        yield _make_public_package(
            workspace,
            "requirements",
            requirements_bytes=b"# frozen root requirements closure fixture\n",
        )
    finally:
        _remove_owned_workspace(workspace, owner)


def _run_bootstrap(package_root: Path, *, index: int = 0, output_name: str = "unused-output") -> subprocess.CompletedProcess:
    script = package_root / "reproduce" / "run.py"
    output = package_root.parent / output_name
    return subprocess.run(
        [sys.executable, "-I", str(script), "--_child-entry", str(index), "--output-dir", str(output)],
        capture_output=True, text=True, check=False, timeout=60,
    )


@pytest.mark.parametrize("object_id", ["payload", "a" * 64, "\u4e2d\u6587 id", "a/b", "abc.json"], ids=["ordinary", "64-hex", "unicode-space", "safe-multicomponent", "json-suffix"])
def test_real_prepared_package_maps_object_ids_to_hashed_files_and_reaches_platform_gate(object_id):
    workspace, owner = _new_workspace()
    try:
        built = _make_public_package(workspace, object_id)
        package_manifest = built["package_manifest"]
        provenance_source_rows = [
            item for item in package_manifest["ref_map"]
            if item["ref"]["path"].startswith(".reaslab/optagent/source-bytes/")
        ]
        assert provenance_source_rows
        assert all(
            item["package_path"] == f"evidence/attachments/{item['ref']['sha256']}.bin"
            for item in provenance_source_rows
        )
        reader = PortableReader(built["package_root"], package_manifest["ref_map"])
        reproduction = package_manifest["reproductions"][0]
        prepared_raw = reader.read_ref(reproduction["prepared_manifest_ref"])
        prepared = strict_json_loads(prepared_raw)
        frozen_data = prepared["data"]
        data_manifest_raw = reader.read_ref(frozen_data["manifest"])
        data_manifest = strict_json_loads(data_manifest_raw)
        values_ref = data_manifest["objects"][0]["values"]
        values = reader.read_ref(values_ref)
        trace_ref = data_manifest["trace"]
        trace = strict_json_loads(reader.read_ref(trace_ref))

        assert data_manifest["objects"][0]["object_id"] == object_id
        assert values_ref["path"].endswith(f"/input/data/objects/{object_id}.json")
        token = hashlib.sha256(object_id.encode("utf-8")).hexdigest()
        expected_package_path = f"reproduce/prepared/{built['prepared_id']}/data/objects/{token}.json"
        assert _ref_map_row(package_manifest, values_ref)["package_path"] == expected_package_path
        assert len(values) == values_ref["byte_len"] and hashlib.sha256(values).hexdigest() == values_ref["sha256"]
        expected_trace_sources = [
            source["bytes"] for source in prepared["sources"]
            if source["source_id"] in data_manifest["source_ids"]
        ]
        assert trace["schema_version"] == 1
        assert trace["plan"] == data_manifest["import_plan"]
        assert trace["source_refs"] == expected_trace_sources
        assert isinstance(trace["steps"], list)
        loaded = load_data_bundle(
            reader,
            frozen_data["manifest"],
            candidate_binding_digest=prepared["binding_digest"],
            source_records=read_prepared_source_records(reader, prepared["sources"]),
        )
        assert [dict(row) for row in loaded.object(object_id).rows] == [{"site": "A", "cost": 10.5}]
        assert loaded.data_id == frozen_data["data_id"]
        assert {ref["path"] for ref in loaded.consumed_inputs} == {
            frozen_data["manifest"]["path"], trace_ref["path"], values_ref["path"],
            *[ref["path"] for ref in expected_trace_sources],
        }
        if object_id == "a" * 64:
            assert token != object_id

        try:
            supported_platform = _require_supported_platform()
        except ContractError as exc:
            assert exc.code == "capability_uncovered"
            supported_profile = False
        else:
            assert sys.version_info[:3] == (3, 12, 11)
            assert supported_platform["os"] == "Linux"
            assert supported_platform["architecture"].lower() in {"x86_64", "amd64"}
            assert supported_platform["python_implementation"] == "CPython"
            supported_profile = True
        output = built["package_root"].parent / "platform-gate-output"
        completed = _run_bootstrap(built["package_root"], output_name="platform-gate-output")
        assert completed.returncode == 2
        assert completed.stdout == ""
        if supported_profile:
            assert "prepared mapping fixture reached candidate build" in completed.stderr
            assert output.is_dir() and not any(output.iterdir())
        else:
            assert "worker platform and interpreter are outside the one verified managed profile" in completed.stderr
            assert not output.exists()
    finally:
        _remove_owned_workspace(workspace, owner)


def test_real_prepared_package_maps_optional_root_requirements(requirements_package):
    built = requirements_package
    package_manifest = built["package_manifest"]
    reader = PortableReader(built["package_root"], package_manifest["ref_map"])
    reproduction = package_manifest["reproductions"][0]
    prepared = strict_json_loads(reader.read_ref(reproduction["prepared_manifest_ref"]))
    requirements = prepared["candidate"]["requirements_file"]
    requirements_ref = requirements["bytes"]
    expected_ref_path = (
        f".reaslab/optagent/cases/{built['case_id']}/prepared/{built['prepared_id']}/input/requirements.txt"
    )
    expected_package_path = f"reproduce/prepared/{built['prepared_id']}/requirements.txt"

    assert requirements == {"path": "requirements.txt", "bytes": requirements_ref}
    assert requirements_ref["path"] == expected_ref_path
    assert _ref_map_row(package_manifest, requirements_ref)["package_path"] == expected_package_path
    assert reader.read_ref(requirements_ref) == b"# frozen root requirements closure fixture\n"

    try:
        _require_supported_platform()
    except ContractError:
        supported_platform = False
    else:
        supported_platform = True
    completed = _run_bootstrap(built["package_root"], output_name="requirements-platform-gate-output")
    assert completed.returncode == 2
    if supported_platform:
        assert "prepared mapping fixture reached candidate build" in completed.stderr
        output = built["package_root"].parent / "requirements-platform-gate-output"
        assert output.is_dir() and not any(output.iterdir())
    else:
        assert "worker platform and interpreter are outside the one verified managed profile" in completed.stderr
        assert not (built["package_root"].parent / "requirements-platform-gate-output").exists()


def test_bootstrap_rejects_unknown_candidate_field_when_requirements_is_present(requirements_package):
    source_root = requirements_package["package_root"]

    def mutate(root: Path) -> None:
        def edit(prepared: dict, _data_manifest: dict) -> None:
            prepared["candidate"]["unexpected_field"] = None

        _rewrite_prepared_and_data_manifests(root, edit)

    _run_rejected_mutation(
        source_root,
        f"v{uuid.uuid4().hex[:6]}",
        mutate,
        "prepared candidate profile is unsupported",
    )


def test_bootstrap_rejects_noncanonical_requirements_package_path(requirements_package):
    source_root = requirements_package["package_root"]
    prepared_id = requirements_package["prepared_id"]

    def mutate(root: Path) -> None:
        package_manifest_path = root / "manifest.json"
        package_manifest = strict_json_loads(package_manifest_path.read_bytes())
        reproduction = package_manifest["reproductions"][0]
        prepared = strict_json_loads(_ref_map_file(root, package_manifest, reproduction["prepared_manifest_ref"]))
        requirements_ref = prepared["candidate"]["requirements_file"]["bytes"]
        row = _ref_map_row(package_manifest, requirements_ref)
        old_relative = PurePosixPath(row["package_path"])
        wrong_relative = PurePosixPath(f"reproduce/prepared/{prepared_id}/input/requirements.txt")
        old_path = root.joinpath(*old_relative.parts)
        wrong_path = root.joinpath(*wrong_relative.parts)
        wrong_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.replace(wrong_path)
        row["package_path"] = wrong_relative.as_posix()
        package_manifest_path.write_bytes(canonical_json_bytes(package_manifest))

    _run_rejected_mutation(
        source_root,
        f"v{uuid.uuid4().hex[:6]}",
        mutate,
        "frozen input Ref is mapped outside its fixed package path",
    )


def _replace_package_ref(package_manifest: dict, old_ref: dict, new_ref: dict) -> None:
    row = _ref_map_row(package_manifest, old_ref)
    row["ref"] = new_ref


def _rewrite_prepared_and_data_manifests(package_root: Path, mutate) -> dict:
    package_manifest_path = package_root / "manifest.json"
    package_manifest = strict_json_loads(package_manifest_path.read_bytes())
    reproduction = package_manifest["reproductions"][0]
    old_prepared_ref = reproduction["prepared_manifest_ref"]
    prepared_row = _ref_map_row(package_manifest, old_prepared_ref)
    prepared_path = package_root.joinpath(*PurePosixPath(prepared_row["package_path"]).parts)
    prepared = strict_json_loads(prepared_path.read_bytes())

    old_data_ref = prepared["data"]["manifest"]
    data_row = _ref_map_row(package_manifest, old_data_ref)
    data_path = package_root.joinpath(*PurePosixPath(data_row["package_path"]).parts)
    data_manifest = strict_json_loads(data_path.read_bytes())
    mutate(prepared, data_manifest)

    data_raw = canonical_json_bytes(data_manifest)
    new_data_ref = ref_from_bytes(old_data_ref["path"], data_raw, old_data_ref["media_type"])
    data_path.write_bytes(data_raw)
    _replace_package_ref(package_manifest, old_data_ref, new_data_ref)
    prepared["data"]["manifest"] = new_data_ref

    prepared_raw = canonical_json_bytes(prepared)
    new_prepared_ref = ref_from_bytes(old_prepared_ref["path"], prepared_raw, old_prepared_ref["media_type"])
    prepared_path.write_bytes(prepared_raw)
    _replace_package_ref(package_manifest, old_prepared_ref, new_prepared_ref)
    reproduction["prepared_manifest_ref"] = new_prepared_ref
    package_manifest_path.write_bytes(canonical_json_bytes(package_manifest))
    return package_manifest


def _copy_package_root(source: Path, name: str) -> tuple[Path, str]:
    destination = source.parent / name
    assert not destination.exists()
    shutil.copytree(source, destination)
    owner = uuid.uuid4().hex
    (destination / _OWNER_MARKER).write_text(owner, encoding="ascii")
    return destination, owner


def _run_rejected_mutation(source_root: Path, name: str, mutate, expected_error: str) -> None:
    variant, owner = _copy_package_root(source_root, name)
    try:
        mutate(variant)
        completed = _run_bootstrap(variant, output_name=f"{name}-output")
        assert completed.returncode == 2
        assert expected_error in completed.stderr
        assert not (variant.parent / f"{name}-output").exists()
    finally:
        resolved_source = source_root.parent.resolve()
        resolved_variant = variant.resolve()
        marker = resolved_variant / _OWNER_MARKER
        if (
            resolved_variant.parent != resolved_source or not resolved_variant.name.startswith("v")
            or not marker.is_file() or marker.read_text(encoding="ascii") != owner
            or variant.is_symlink()
        ):
            raise RuntimeError("refusing to clean an invalid package mutation workspace")
        shutil.rmtree(resolved_variant)


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("wrong-object-id", "frozen data values Ref does not match its object identity"),
        ("duplicate-id", "frozen data object identity is invalid"),
        ("empty-id", "frozen data object identity is invalid"),
        ("non-string-id", "frozen data object identity is invalid"),
        ("missing-id", "frozen data object identity is invalid"),
        ("missing-values", "reference fields are invalid"),
        ("objects-not-list", "frozen DataManifest objects are invalid"),
        ("wrong-data-id", "frozen Prepared DataBundle identity does not match its DataManifest"),
        ("wrong-manifest-prepared", "frozen Prepared DataManifest Ref is invalid"),
        ("wrong-trace-prepared", "frozen Prepared DataBundle trace Ref is invalid"),
        ("wrong-values-prepared", "frozen data values Ref does not match its object identity"),
        ("traversal-values", "reference path is invalid"),
    ],
)
def test_bootstrap_rejects_prepared_manifest_identity_and_closure_mismatches(payload_package, mutation, expected_error):
    source_root = payload_package["package_root"]

    def mutate(root: Path) -> None:
        def edit(prepared: dict, data_manifest: dict) -> None:
            if mutation == "wrong-object-id":
                data_manifest["objects"][0]["object_id"] = "renamed"
            elif mutation == "duplicate-id":
                duplicate = dict(data_manifest["objects"][0])
                data_manifest["objects"].append(duplicate)
            elif mutation == "empty-id":
                data_manifest["objects"][0]["object_id"] = ""
            elif mutation == "non-string-id":
                data_manifest["objects"][0]["object_id"] = 7
            elif mutation == "missing-id":
                del data_manifest["objects"][0]["object_id"]
            elif mutation == "missing-values":
                del data_manifest["objects"][0]["values"]
            elif mutation == "objects-not-list":
                data_manifest["objects"] = {}
            elif mutation == "wrong-data-id":
                data_manifest["data_id"] = "different-data-id"
            elif mutation == "wrong-trace-prepared":
                data_manifest["trace"]["path"] = data_manifest["trace"]["path"].replace(
                    f"/prepared/{payload_package['prepared_id']}/", f"/prepared/{uuid.uuid4()}/"
                )
            elif mutation == "wrong-values-prepared":
                value_ref = data_manifest["objects"][0]["values"]
                value_ref["path"] = value_ref["path"].replace(
                    f"/prepared/{payload_package['prepared_id']}/", f"/prepared/{uuid.uuid4()}/"
                )
            elif mutation == "traversal-values":
                value_ref = data_manifest["objects"][0]["values"]
                value_ref["path"] = value_ref["path"].replace("objects/payload.json", "objects/../escape.json")

        if mutation == "wrong-manifest-prepared":
            package_manifest_path = root / "manifest.json"
            package_manifest = strict_json_loads(package_manifest_path.read_bytes())
            prepared_ref = package_manifest["reproductions"][0]["prepared_manifest_ref"]
            prepared_row = _ref_map_row(package_manifest, prepared_ref)
            prepared_path = root.joinpath(*PurePosixPath(prepared_row["package_path"]).parts)
            prepared = strict_json_loads(prepared_path.read_bytes())
            prepared["data"]["manifest"]["path"] = prepared["data"]["manifest"]["path"].replace(
                f"/prepared/{payload_package['prepared_id']}/", f"/prepared/{uuid.uuid4()}/"
            )
            prepared_raw = canonical_json_bytes(prepared)
            new_prepared_ref = ref_from_bytes(prepared_ref["path"], prepared_raw, prepared_ref["media_type"])
            prepared_path.write_bytes(prepared_raw)
            _ref_map_row(package_manifest, prepared_ref)["ref"] = new_prepared_ref
            package_manifest["reproductions"][0]["prepared_manifest_ref"] = new_prepared_ref
            package_manifest_path.write_bytes(canonical_json_bytes(package_manifest))
        else:
            _rewrite_prepared_and_data_manifests(root, edit)

    _run_rejected_mutation(source_root, f"v{uuid.uuid4().hex[:6]}", mutate, expected_error)


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("unselected-run", "reproduction evidence Run is not a selected Run"),
        ("different-prepared-id", "PreparedInputManifest does not match its selected Run"),
    ],
)
def test_bootstrap_binds_reproduction_prepared_input_to_selected_run(
    payload_package, mutation, expected_error
):
    source_root = payload_package["package_root"]

    def mutate(root: Path) -> None:
        package_manifest_path = root / "manifest.json"
        package_manifest = strict_json_loads(package_manifest_path.read_bytes())
        reproduction = package_manifest["reproductions"][0]
        if mutation == "unselected-run":
            reproduction["evidence_run_ids"] = [str(uuid.uuid4())]
            package_manifest_path.write_bytes(canonical_json_bytes(package_manifest))
            return

        run_id = reproduction["evidence_run_ids"][0]
        old_run_ref = package_manifest["authority_records"]["runs"][run_id]
        run_row = _ref_map_row(package_manifest, old_run_ref)
        run_path = root.joinpath(*PurePosixPath(run_row["package_path"]).parts)
        run_record = strict_json_loads(run_path.read_bytes())
        run_record["prepared_id"] = str(uuid.uuid4())
        run_raw = canonical_json_bytes(run_record)
        run_version_prefix = f".reaslab/optagent/cases/{package_manifest['case_id']}/runs/{run_id}/versions/"
        new_run_path = (
            run_version_prefix + hashlib.sha256(run_raw).hexdigest() + ".json"
            if old_run_ref["path"].startswith(run_version_prefix)
            else old_run_ref["path"]
        )
        new_run_ref = ref_from_bytes(new_run_path, run_raw, old_run_ref["media_type"])
        run_row["ref"] = new_run_ref
        run_path.write_bytes(run_raw)

        selection_ref = package_manifest["selection_ref"]
        selection_row = _ref_map_row(package_manifest, selection_ref)
        selection_path = root.joinpath(*PurePosixPath(selection_row["package_path"]).parts)
        selection = strict_json_loads(selection_path.read_bytes())
        selection["authority_records"]["runs"][run_id] = new_run_ref
        selection["evidence_refs"] = [
            new_run_ref if item == old_run_ref else item
            for item in selection["evidence_refs"]
        ]
        selection_raw = canonical_json_bytes(selection)
        new_selection_ref = ref_from_bytes(
            selection_ref["path"], selection_raw, selection_ref["media_type"]
        )
        selection_row["ref"] = new_selection_ref
        selection_path.write_bytes(selection_raw)

        package_manifest["authority_records"]["runs"][run_id] = new_run_ref
        package_manifest["selection_ref"] = new_selection_ref
        package_manifest["consumed_refs"] = [
            new_selection_ref if item == selection_ref
            else new_run_ref if item == old_run_ref
            else item
            for item in package_manifest["consumed_refs"]
        ]
        package_manifest["ref_map"].sort(
            key=lambda item: (
                item["ref"]["path"], item["ref"]["sha256"],
                item["ref"]["byte_len"], item["ref"]["media_type"],
            )
        )
        for package_path, raw in ((run_row["package_path"], run_raw), (selection_row["package_path"], selection_raw)):
            object_rows = [item for item in package_manifest["objects"] if item["path"] == package_path]
            assert len(object_rows) == 1
            object_rows[0]["sha256"] = hashlib.sha256(raw).hexdigest()
            object_rows[0]["byte_len"] = len(raw)
        package_manifest_path.write_bytes(canonical_json_bytes(package_manifest))

    _run_rejected_mutation(source_root, f"v{uuid.uuid4().hex[:6]}", mutate, expected_error)


def test_bootstrap_rejects_wrong_hash_length_media_package_path_and_unreferenced_extra_ref(long_id_package):
    built = long_id_package
    source_root = built["package_root"]

    def wrong_package_shape(root: Path) -> None:
        package_manifest_path = root / "manifest.json"
        package_manifest = strict_json_loads(package_manifest_path.read_bytes())
        prepared_ref = package_manifest["reproductions"][0]["prepared_manifest_ref"]
        prepared = strict_json_loads(_ref_map_file(root, package_manifest, prepared_ref))
        data_manifest = strict_json_loads(_ref_map_file(root, package_manifest, prepared["data"]["manifest"]))
        values_ref = data_manifest["objects"][0]["values"]
        row = _ref_map_row(package_manifest, values_ref)
        previous_path = row["package_path"]
        malformed_path = f"reproduce/prepared/{built['prepared_id']}/data/objects/not-a-hash.json"
        previous_file = root.joinpath(*PurePosixPath(previous_path).parts)
        malformed_file = root.joinpath(*PurePosixPath(malformed_path).parts)
        malformed_file.parent.mkdir(parents=True, exist_ok=True)
        previous_file.replace(malformed_file)
        row["package_path"] = malformed_path
        for object_row in package_manifest["objects"]:
            if object_row["path"] == previous_path:
                object_row["path"] = malformed_path
        package_manifest_path.write_bytes(canonical_json_bytes(package_manifest))

    _run_rejected_mutation(
        source_root, f"v{uuid.uuid4().hex[:6]}", wrong_package_shape,
        "frozen data object package path is invalid",
    )

    def traversal_package_path(root: Path) -> None:
        package_manifest_path = root / "manifest.json"
        package_manifest = strict_json_loads(package_manifest_path.read_bytes())
        prepared_ref = package_manifest["reproductions"][0]["prepared_manifest_ref"]
        prepared = strict_json_loads(_ref_map_file(root, package_manifest, prepared_ref))
        data_manifest = strict_json_loads(_ref_map_file(root, package_manifest, prepared["data"]["manifest"]))
        values_ref = data_manifest["objects"][0]["values"]
        _ref_map_row(package_manifest, values_ref)["package_path"] = "../escape.json"
        package_manifest_path.write_bytes(canonical_json_bytes(package_manifest))

    _run_rejected_mutation(
        source_root, f"v{uuid.uuid4().hex[:6]}", traversal_package_path,
        "package path escapes the extraction root",
    )

    def wrong_package_path(root: Path) -> None:
        package_manifest_path = root / "manifest.json"
        package_manifest = strict_json_loads(package_manifest_path.read_bytes())
        prepared_ref = package_manifest["reproductions"][0]["prepared_manifest_ref"]
        prepared_row = _ref_map_row(package_manifest, prepared_ref)
        prepared_path = root.joinpath(*PurePosixPath(prepared_row["package_path"]).parts)
        prepared = strict_json_loads(prepared_path.read_bytes())
        data_manifest = strict_json_loads(_ref_map_file(root, package_manifest, prepared["data"]["manifest"]))
        values_ref = data_manifest["objects"][0]["values"]
        row = _ref_map_row(package_manifest, values_ref)
        old_path = root.joinpath(*PurePosixPath(row["package_path"]).parts)
        wrong_path = f"reproduce/prepared/{built['prepared_id']}/data/objects/" + "a" * 64 + ".json"
        assert wrong_path != row["package_path"]
        new_file = root.joinpath(*PurePosixPath(wrong_path).parts)
        new_file.parent.mkdir(parents=True, exist_ok=True)
        new_file.write_bytes(old_path.read_bytes())
        row["package_path"] = wrong_path
        package_manifest_path.write_bytes(canonical_json_bytes(package_manifest))

    _run_rejected_mutation(
        source_root, f"v{uuid.uuid4().hex[:6]}", wrong_package_path,
        "frozen data values Ref is not mapped to its fixed object path",
    )

    def wrong_hash(root: Path) -> None:
        def edit(prepared: dict, data_manifest: dict) -> None:
            data_manifest["objects"][0]["values"]["sha256"] = "0" * 64

        manifest = _rewrite_prepared_and_data_manifests(root, edit)
        prepared = strict_json_loads(_ref_map_file(root, manifest, manifest["reproductions"][0]["prepared_manifest_ref"]))
        data_manifest = strict_json_loads(_ref_map_file(root, manifest, prepared["data"]["manifest"]))
        wrong_ref = data_manifest["objects"][0]["values"]
        _ref_map_row_by_path(manifest, wrong_ref["path"])["ref"] = wrong_ref
        (root / "manifest.json").write_bytes(canonical_json_bytes(manifest))

    _run_rejected_mutation(
        source_root, f"v{uuid.uuid4().hex[:6]}", wrong_hash,
        "packaged bytes do not match their original Ref",
    )

    def wrong_length(root: Path) -> None:
        def edit(prepared: dict, data_manifest: dict) -> None:
            data_manifest["objects"][0]["values"]["byte_len"] += 1

        manifest = _rewrite_prepared_and_data_manifests(root, edit)
        prepared = strict_json_loads(_ref_map_file(root, manifest, manifest["reproductions"][0]["prepared_manifest_ref"]))
        data_manifest = strict_json_loads(_ref_map_file(root, manifest, prepared["data"]["manifest"]))
        wrong_ref = data_manifest["objects"][0]["values"]
        _ref_map_row_by_path(manifest, wrong_ref["path"])["ref"] = wrong_ref
        (root / "manifest.json").write_bytes(canonical_json_bytes(manifest))

    _run_rejected_mutation(
        source_root, f"v{uuid.uuid4().hex[:6]}", wrong_length,
        "packaged bytes do not match their original Ref",
    )

    def wrong_media(root: Path) -> None:
        def edit(prepared: dict, data_manifest: dict) -> None:
            data_manifest["objects"][0]["values"]["media_type"] = "application/octet-stream"

        manifest = _rewrite_prepared_and_data_manifests(root, edit)
        prepared = strict_json_loads(_ref_map_file(root, manifest, manifest["reproductions"][0]["prepared_manifest_ref"]))
        data_manifest = strict_json_loads(_ref_map_file(root, manifest, prepared["data"]["manifest"]))
        wrong_ref = data_manifest["objects"][0]["values"]
        _ref_map_row_by_path(manifest, wrong_ref["path"])["ref"] = wrong_ref
        (root / "manifest.json").write_bytes(canonical_json_bytes(manifest))

    _run_rejected_mutation(
        source_root, f"v{uuid.uuid4().hex[:6]}", wrong_media,
        "frozen data object media type is invalid",
    )

    def missing_values_map(root: Path) -> None:
        package_manifest_path = root / "manifest.json"
        package_manifest = strict_json_loads(package_manifest_path.read_bytes())
        prepared_ref = package_manifest["reproductions"][0]["prepared_manifest_ref"]
        prepared = strict_json_loads(_ref_map_file(root, package_manifest, prepared_ref))
        data_manifest = strict_json_loads(_ref_map_file(root, package_manifest, prepared["data"]["manifest"]))
        values_ref = data_manifest["objects"][0]["values"]
        package_manifest["ref_map"].remove(_ref_map_row(package_manifest, values_ref))
        package_manifest_path.write_bytes(canonical_json_bytes(package_manifest))

    _run_rejected_mutation(
        source_root, f"v{uuid.uuid4().hex[:6]}", missing_values_map,
        "frozen data values Ref is not mapped to its fixed object path",
    )

    def add_extra_ref(root: Path) -> None:
        package_manifest_path = root / "manifest.json"
        package_manifest = strict_json_loads(package_manifest_path.read_bytes())
        prepared_ref = package_manifest["reproductions"][0]["prepared_manifest_ref"]
        prepared = strict_json_loads(_ref_map_file(root, package_manifest, prepared_ref))
        prefix = prepared["data"]["manifest"]["path"].rsplit("/manifest.json", 1)[0]
        extra_id = "unreferenced"
        raw = b"[]"
        extra_ref = ref_from_bytes(f"{prefix}/objects/{extra_id}.json", raw, "application/json")
        extra_package_path = f"reproduce/prepared/{built['prepared_id']}/data/objects/{hashlib.sha256(extra_id.encode('utf-8')).hexdigest()}.json"
        extra_file = root.joinpath(*PurePosixPath(extra_package_path).parts)
        extra_file.parent.mkdir(parents=True, exist_ok=True)
        extra_file.write_bytes(raw)
        package_manifest["ref_map"].append({"ref": extra_ref, "package_path": extra_package_path})
        package_manifest_path.write_bytes(canonical_json_bytes(package_manifest))

    _run_rejected_mutation(
        source_root, f"v{uuid.uuid4().hex[:6]}", add_extra_ref,
        "package contains an unreferenced Prepared DataBundle Ref",
    )


def _ref_map_file(package_root: Path, package_manifest: dict, ref: dict) -> bytes:
    row = _ref_map_row(package_manifest, ref)
    return package_root.joinpath(*PurePosixPath(row["package_path"]).parts).read_bytes()
