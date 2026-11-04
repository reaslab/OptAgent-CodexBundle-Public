from __future__ import annotations

import contextlib
import hashlib
import copy

import pytest

from optagent_core.contracts import ContractError, canonical_json_bytes, ref_from_bytes
from optagent_core.data import PortableReader, _validate_plan, load_data_bundle
from optagent_core.sources import data_source_records
from optagent_core.store import CaseStore


def _csv_plan(source_id: str) -> dict:
    return {
        "encoding": "utf-8",
        "blocks": [
            {
                "object_id": "sites",
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
            }
        ],
        "conversions": [],
    }


def test_import_plan_type_errors_are_contract_failures_not_python_type_errors() -> None:
    mutations = [
        lambda plan: plan["blocks"][0].update(source_id=[]),
        lambda plan: plan["blocks"][0].update(key_fields=None),
        lambda plan: plan["blocks"][0]["columns"][0].update(position=True, input_name=None),
        lambda plan: plan["blocks"][0]["columns"][0].update(position=None, input_name=[]),
        lambda plan: plan["blocks"][0]["layout"].update(header=[]),
        lambda plan: plan["blocks"][0].update(duplicate_policy={}),
    ]
    for mutate in mutations:
        plan = copy.deepcopy(_csv_plan("source-1"))
        mutate(plan)
        with pytest.raises(ContractError):
            _validate_plan(plan, {"source-1"})


def test_none_loader_returns_immutable_empty_bundle_before_reader_access():
    class ForbiddenWorkspace:
        @property
        def read_ref(self):
            raise AssertionError("None data must return before _store or Reader access")

    binding = "a" * 64
    bundle = load_data_bundle(ForbiddenWorkspace(), None, candidate_binding_digest=binding)
    assert bundle.data_id is None
    assert bundle.digest == hashlib.sha256(canonical_json_bytes(None)).hexdigest()
    assert bundle.digest == "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b"
    assert bundle.loader_version == "optagent-data-v1"
    assert bundle.objects == {}
    with pytest.raises(TypeError):
        bundle.objects["unexpected"] = object()
    assert bundle.consumed_inputs == ()
    assert bundle.candidate_binding_digest == binding
    assert bundle.instance_context is None

    instance = bundle.for_instance(None, ())
    assert instance.data_id is None
    assert instance.candidate_binding_digest == binding
    assert instance.instance_context is not None
    assert instance.instance_context.instance_key == ()
    assert instance.instance_context.parameter_object_id is None
    assert instance.instance_context.parameters == {}
    assert instance.instance_context.row_digest == hashlib.sha256(canonical_json_bytes({})).hexdigest()
    assert instance.instance_context.instance_binding_digest != bundle.digest


@pytest.mark.parametrize("bad_binding", [None, "", "not-a-sha256"])
def test_none_loader_requires_real_candidate_binding_without_reader_access(bad_binding):
    class ForbiddenWorkspace:
        @property
        def read_ref(self):
            raise AssertionError("None data must return before _store or Reader access")

    with pytest.raises(ContractError) as caught:
        load_data_bundle(ForbiddenWorkspace(), None, candidate_binding_digest=bad_binding)
    assert caught.value.code == "invalid_argument"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"instance_parameter_object_id": "parameter-table"},
        {"instance_key": ["A"]},
    ],
)
def test_none_loader_rejects_parameterized_instances(kwargs):
    with pytest.raises(ContractError) as caught:
        load_data_bundle(object(), None, candidate_binding_digest="b" * 64, **kwargs)
    assert caught.value.code == "invalid_argument"


def test_invalid_non_none_manifest_never_falls_back_to_empty_bundle():
    class NoReadReader:
        def read_ref(self, ref):
            raise AssertionError("invalid inline manifest must fail before any reference is read")

    with pytest.raises(ContractError) as caught:
        load_data_bundle(NoReadReader(), {}, candidate_binding_digest="c" * 64)
    assert caught.value.code == "output_invalid"


def test_explicit_csv_normalizes_once_and_shared_loader_preserves_typed_keys(core):
    case_id, _, _ = core["create_case"]()
    inputs_dir = core["workspace"] / "inputs"
    inputs_dir.mkdir()
    (inputs_dir / "sites.csv").write_bytes(b"site,cost\nA,10.5\nB,22\n")
    state = core["store"]().read_case(case_id)
    captured = core["call"](
        "optagent.data",
        {
            "op": "capture",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "path": "inputs/sites.csv",
            "purpose": "data",
            "locations": ["rows 1-2"],
        },
    )
    assert captured["ok"], captured
    source_id = captured["result"]["source"]["id"]
    state = core["store"]().read_case(case_id)
    normalized = core["call"](
        "optagent.data",
        {
            "op": "normalize",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "source_ids": [source_id],
            "plan": _csv_plan(source_id),
        },
    )
    assert normalized["ok"], normalized
    manifest_ref = normalized["result"]["data_manifest"]["manifest_ref"]
    store = core["store"]()
    state = store.read_case(case_id)
    source_records = data_source_records(store, case_id=case_id, source_ids=[source_id], state=state)
    bundle = load_data_bundle(store, manifest_ref, source_records=source_records)
    assert tuple(bundle.table("sites")) == (("A",), ("B",))
    assert bundle.table("sites")[("A",)]["cost"] == 10.5
    assert bundle.table("sites")[("B",)]["cost"] == 22.0
    assert bundle.consumed_inputs


def test_data_capture_reads_large_workspace_bytes_before_session_lock(core, monkeypatch):
    case_id, _, _ = core["create_case"]()
    inputs_dir = core["workspace"] / "inputs"
    inputs_dir.mkdir()
    raw = b"x" * (2 * 1024 * 1024)
    (inputs_dir / "large.bin").write_bytes(raw)
    state = core["store"]().read_case(case_id)

    real_session_lock = CaseStore.session_lock
    real_read_workspace_file = CaseStore.read_workspace_file
    real_read_ref = CaseStore.read_ref
    lock_depth = 0
    workspace_reads_under_session_lock = []
    ref_reads_under_session_lock = []

    @contextlib.contextmanager
    def tracked_session_lock(store, digest):
        nonlocal lock_depth
        with real_session_lock(store, digest):
            lock_depth += 1
            try:
                yield
            finally:
                lock_depth -= 1

    def tracked_workspace_read(store, path):
        if lock_depth:
            workspace_reads_under_session_lock.append(path)
        return real_read_workspace_file(store, path)

    def tracked_ref_read(store, ref):
        if lock_depth:
            ref_reads_under_session_lock.append(ref["path"])
        return real_read_ref(store, ref)

    monkeypatch.setattr(CaseStore, "session_lock", tracked_session_lock)
    monkeypatch.setattr(CaseStore, "read_workspace_file", tracked_workspace_read)
    monkeypatch.setattr(CaseStore, "read_ref", tracked_ref_read)
    reply = core["call"]("optagent.data", {
        "op": "capture",
        "case_id": case_id,
        "expected_state_revision": state["state_revision"],
        "path": "inputs/large.bin",
        "purpose": "data",
        "locations": ["the complete captured file"],
    })

    assert reply["ok"], reply
    assert workspace_reads_under_session_lock == []
    assert ref_reads_under_session_lock == []


def test_repeated_data_capture_reuses_only_the_same_path_bytes_and_locations(core):
    case_id, _, _ = core["create_case"]()
    inputs_dir = core["workspace"] / "inputs"
    inputs_dir.mkdir()
    raw = b"site,cost\nA,10.5\n"
    (inputs_dir / "sites.csv").write_bytes(raw)
    (inputs_dir / "copy.csv").write_bytes(raw)

    def capture(path, locations):
        state = core["store"]().read_case(case_id)
        return core["call"]("optagent.data", {
            "op": "capture",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "path": path,
            "purpose": "data",
            "locations": locations,
        })

    first = capture("inputs/sites.csv", ["rows 1-2"])
    assert first["ok"], first
    initial_id = first["result"]["source"]["id"]
    initial_state = core["store"]().read_case(case_id)
    second = capture("inputs/sites.csv", ["rows 1-2"])
    assert second["ok"], second
    assert second["result"]["source"]["id"] == initial_id
    assert second["state_revision"] == initial_state["state_revision"]
    assert core["store"]().read_case(case_id)["source_revision"] == initial_state["source_revision"]

    other_path = capture("inputs/copy.csv", ["rows 1-2"])
    assert other_path["ok"], other_path
    assert other_path["result"]["source"]["id"] != initial_id
    new_locations = capture("inputs/sites.csv", ["supplier table rows 1-2"])
    assert new_locations["ok"], new_locations
    assert new_locations["result"]["source"]["id"] != initial_id


def test_json_number_overflow_is_rejected_at_ingestion_boundary(core):
    case_id, _, _ = core["create_case"]()
    inputs_dir = core["workspace"] / "inputs"
    inputs_dir.mkdir()
    (inputs_dir / "overflow.json").write_bytes(b'{"cost":1e999}')
    state = core["store"]().read_case(case_id)
    captured = core["call"](
        "optagent.data",
        {
            "op": "capture",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "path": "inputs/overflow.json",
            "purpose": "data",
            "locations": [],
        },
    )
    assert captured["ok"], captured
    source_id = captured["result"]["source"]["id"]
    state = core["store"]().read_case(case_id)
    plan = _csv_plan(source_id)
    plan["blocks"][0]["format"] = "json"
    plan["blocks"][0]["layout"] = {"pointer": ""}
    plan["blocks"][0]["columns"] = [
        {"name": "cost", "position": None, "input_name": "cost", "type": "number", "unit": "USD", "nullable": False}
    ]
    plan["blocks"][0]["key_fields"] = []
    plan["blocks"][0]["kind"] = "scalar"
    response = core["call"](
        "optagent.data",
        {
            "op": "normalize",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "source_ids": [source_id],
            "plan": plan,
        },
    )
    assert not response["ok"]
    assert response["error"]["code"] == "invalid_argument"


def test_portable_reader_only_reads_a_verified_original_ref_mapping(tmp_path):
    package = tmp_path / "moved-package"
    artifact = package / "evidence" / "frozen-data.json"
    artifact.parent.mkdir(parents=True)
    raw = b'{"value":7}'
    artifact.write_bytes(raw)
    original_ref = ref_from_bytes(
        ".reaslab/optagent/cases/00000000-0000-4000-8000-000000000001/prepared/values.json",
        raw,
        "application/json",
    )
    reader = PortableReader(
        package,
        [{"ref": original_ref, "package_path": "evidence/frozen-data.json"}],
    )
    assert reader.read_ref(original_ref) == raw

    unknown = ref_from_bytes(".reaslab/optagent/unmapped.json", raw, "application/json")
    with pytest.raises(ContractError) as caught:
        reader.read_ref(unknown)
    assert caught.value.code == "capture_unavailable"


def test_portable_reader_rejects_traversal_and_changed_bytes(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    raw = b"frozen"
    ref = ref_from_bytes(".reaslab/optagent/frozen.bin", raw, "application/octet-stream")
    with pytest.raises(ContractError) as traversal:
        PortableReader(package, [{"ref": ref, "package_path": "../outside.bin"}])
    assert traversal.value.code == "path_denied"

    artifact = package / "frozen.bin"
    artifact.write_bytes(raw)
    reader = PortableReader(package, [{"ref": ref, "package_path": "frozen.bin"}])
    artifact.write_bytes(b"changed")
    with pytest.raises(ContractError) as changed:
        reader.read_ref(ref)
    assert changed.value.code == "artifact_changed"


def test_changed_raw_data_is_not_silently_renormalized_or_used_by_prepare(core):
    case_id, _, _ = core["create_case"]()
    inputs_dir = core["workspace"] / "inputs"
    inputs_dir.mkdir()
    raw_path = inputs_dir / "sites.csv"
    raw_path.write_bytes(b"site,cost\nA,10\n")
    state = core["store"]().read_case(case_id)
    captured = core["call"](
        "optagent.data",
        {
            "op": "capture",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "path": "inputs/sites.csv",
            "purpose": "data",
            "locations": ["row A"],
        },
    )
    assert captured["ok"], captured
    source_id = captured["result"]["source"]["id"]
    state = core["store"]().read_case(case_id)
    normalized = core["call"](
        "optagent.data",
        {
            "op": "normalize",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "source_ids": [source_id],
            "plan": _csv_plan(source_id),
        },
    )
    assert normalized["ok"], normalized
    data_id = normalized["result"]["data_manifest"]["data_id"]

    (core["workspace"] / "model.py").write_text(
        "def build_model(data):\n    return None\n",
        encoding="utf-8",
    )
    state = core["store"]().read_case(case_id)
    candidate = core["call"](
        "optagent.case",
        {
            "op": "revise",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "kind": "candidate",
            "model_path": "model.py",
            "local_module_paths": [],
            "data_id": data_id,
            "backend_id": "highs",
            "objective_plan": {"mode": "feasibility", "preference_note": "raw-source freshness probe"},
            "business_checker_path": None,
            "input_closure": "declared",
            "closure_note": "The frozen normalized input is the only model data.",
        },
    )
    assert candidate["ok"], candidate
    state_before = core["store"]().read_ref(core["store"]().ref_for_path(f".reaslab/optagent/cases/{case_id}/state.json"))
    state_revision = core["store"]().read_case(case_id)["state_revision"]
    raw_path.write_bytes(b"site,cost\nA,99\n")

    fetched = core["call"]("optagent.case", {"op": "get", "case_id": case_id})
    assert fetched["ok"] and fetched["result"]["current_validity"] == "stale"
    prepare = core["call"](
        "optagent.run",
        {
            "op": "prepare",
            "case_id": case_id,
            "expected_state_revision": state_revision,
            "candidate_id": candidate["result"]["candidate"]["id"],
            "solve_options": {
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
            },
        },
    )
    assert not prepare["ok"] and prepare["error"]["code"] == "artifact_changed"
    assert core["store"]().read_case(case_id)["state_revision"] == state_revision
    assert core["store"]().read_ref(core["store"]().ref_for_path(f".reaslab/optagent/cases/{case_id}/state.json")) == state_before
