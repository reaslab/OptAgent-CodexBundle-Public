from __future__ import annotations

from pathlib import Path

from optagent_core.contracts import ContractError, strict_json_loads
from optagent_core.data import load_data_bundle, read_prepared_source_records
from optagent_core.runner import _check_instance_identity
from optagent_core.store import CaseStore


_SOLVE_OPTIONS = {
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


def _csv_data_plan(source_id: str) -> dict:
    return {
        "encoding": "utf-8",
        "blocks": [
            {
                "object_id": "sites",
                "source_id": source_id,
                "format": "csv",
                "layout": {"delimiter": ",", "header": "first_row", "skip_lines": 0},
                "kind": "table",
                "key_fields": ["site_id", "site"],
                "columns": [
                    {"name": "site_id", "position": None, "input_name": "site_id", "type": "integer", "unit": None, "nullable": False},
                    {"name": "site", "position": None, "input_name": "site", "type": "string", "unit": None, "nullable": False},
                    {"name": "cost", "position": None, "input_name": "cost", "type": "number", "unit": "USD", "nullable": False},
                ],
                "duplicate_policy": "error",
                "missing_policy": "error",
            }
        ],
        "conversions": [],
    }


def _make_candidate(core, *, rows: str | None, parameter_object_id: str | None):
    case_id, _, _ = core["create_case"](raw=b"Minimize cost for each named site.")
    workspace: Path = core["workspace"]
    (workspace / "model.py").write_text(
        "def build_model(bundle):\n    return None\n",
        encoding="utf-8",
    )

    data_id = None
    if rows is not None:
        (workspace / "inputs").mkdir()
        (workspace / "inputs" / "sites.csv").write_text(rows, encoding="utf-8")
        state = core["store"]().read_case(case_id)
        captured = core["call"]("optagent.data", {
            "op": "capture",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "path": "inputs/sites.csv",
            "purpose": "data",
            "locations": ["the explicitly listed normalized test rows"],
        })
        assert captured["ok"], captured
        state = core["store"]().read_case(case_id)
        normalized = core["call"]("optagent.data", {
            "op": "normalize",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "source_ids": [captured["result"]["source"]["id"]],
            "plan": _csv_data_plan(captured["result"]["source"]["id"]),
        })
        assert normalized["ok"], normalized
        data_id = normalized["result"]["data_manifest"]["data_id"]

    state = core["store"]().read_case(case_id)
    candidate_arguments = {
        "op": "revise",
        "case_id": case_id,
        "expected_state_revision": state["state_revision"],
        "kind": "candidate",
        "model_path": "model.py",
        "local_module_paths": [],
        "data_id": data_id,
        "backend_id": "highs",
        "objective_plan": {
            "mode": "scalar",
            "objective_component": "cost",
            "sense": "min",
            "unit": "USD",
            "preference_note": "native adapter is not invoked by prepare",
        },
        "business_checker_path": None,
        "input_closure": "declared",
        "closure_note": "The fixture exercises public preparation only.",
    }
    if parameter_object_id is not None:
        candidate_arguments["instance_parameter_object_id"] = parameter_object_id
    candidate = core["call"]("optagent.case", candidate_arguments)
    return case_id, data_id, candidate


def _public_prepare(core, case_id: str, candidate_id: str) -> dict:
    state = core["store"]().read_case(case_id)
    return core["call"]("optagent.run", {
        "op": "prepare",
        "case_id": case_id,
        "expected_state_revision": state["state_revision"],
        "candidate_id": candidate_id,
        "solve_options": dict(_SOLVE_OPTIONS),
    })


def test_public_prepare_freezes_all_multi_instance_rows_with_typed_keys(core):
    rows = "site_id,site,cost\n1,1,10\n2,west,5\n3,east,1\n"
    case_id, data_id, candidate = _make_candidate(core, rows=rows, parameter_object_id="sites")
    assert candidate["ok"], candidate
    candidate_id = candidate["result"]["candidate"]["id"]

    prepared_reply = _public_prepare(core, case_id, candidate_id)
    assert prepared_reply["ok"], prepared_reply
    prepared_summary = prepared_reply["result"]["prepared"]
    prepared_id = prepared_summary["id"]
    record_ref = prepared_summary["record_ref"]
    assert record_ref["path"] == f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/prepared.json"

    store = core["store"]()
    prepared_record = store.read_record(record_ref["path"])
    assert prepared_record["prepared_id"] == prepared_id
    assert prepared_record["status"] == "frozen"
    input_ref = prepared_record["input_manifest"]
    input_manifest = strict_json_loads(store.read_ref(input_ref))
    data_info = input_manifest["data"]
    assert data_info["data_id"] == data_id
    assert data_info["instance_parameter_object_id"] == "sites"
    assert input_ref["sha256"] == prepared_record["input_digest"]

    candidate_record = store.read_record(f".reaslab/optagent/cases/{case_id}/candidates/{candidate_id}.json")
    frozen_data_manifest = strict_json_loads(store.read_ref(data_info["manifest"]))
    bundle = load_data_bundle(
        store,
        data_info["manifest"],
        candidate_binding_digest=input_manifest["binding_digest"],
        source_records=read_prepared_source_records(store, input_manifest["sources"]),
    )
    assert bundle.data_id == data_id
    assert bundle.digest == frozen_data_manifest["digest"]
    assert bundle.candidate_binding_digest == candidate_record["binding_digest"]
    assert bundle.instance_context is None  # prepare freezes the whole bundle, not one row
    assert tuple(bundle.table("sites")) == ((1, "1"), (2, "west"), (3, "east"))

    # The same authoritative per-row binder used during run checks receives
    # each real normalized typed key and returns the immutable row binding.
    object_value = bundle.object("sites")
    assert object_value.key_fields == ("site_id", "site")
    assert object_value.keys == ((1, "1"), (2, "west"), (3, "east"))
    assert type(object_value.keys[0][0]) is int
    assert type(object_value.keys[0][1]) is str
    for key in object_value.keys:
        bound = bundle.for_instance("sites", key)
        assert bound.instance_context is not None
        assert bound.instance_context.instance_key == key
        assert bound.instance_context.parameter_object_id == "sites"
        expected_binding = _check_instance_identity(bundle, input_manifest, list(key))
        assert expected_binding == bound.instance_context.instance_binding_digest


def test_public_prepare_keeps_nonempty_data_for_single_instance_candidate(core):
    rows = "site_id,site,cost\n1,only,10\n"
    case_id, data_id, candidate = _make_candidate(core, rows=rows, parameter_object_id=None)
    assert candidate["ok"], candidate

    prepared_reply = _public_prepare(core, case_id, candidate["result"]["candidate"]["id"])
    assert prepared_reply["ok"], prepared_reply
    prepared_id = prepared_reply["result"]["prepared"]["id"]
    store = core["store"]()
    prepared = store.read_record(f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/prepared.json")
    manifest = strict_json_loads(store.read_ref(prepared["input_manifest"]))
    data_info = manifest["data"]
    assert data_info["data_id"] == data_id
    assert data_info["instance_parameter_object_id"] is None
    bundle = load_data_bundle(
        store,
        data_info["manifest"],
        candidate_binding_digest=manifest["binding_digest"],
        source_records=read_prepared_source_records(store, manifest["sources"]),
    )
    assert bundle.data_id == data_id
    assert len(bundle.rows("sites")) == 1
    assert _check_instance_identity(bundle, manifest, [])


def test_public_prepare_rejects_unknown_parameter_table_without_fallback(core):
    rows = "site_id,site,cost\n1,only,10\n"
    case_id, _, candidate = _make_candidate(core, rows=rows, parameter_object_id="missing-sites")
    assert candidate["ok"], candidate

    prepared_reply = _public_prepare(core, case_id, candidate["result"]["candidate"]["id"])
    assert prepared_reply["ok"] is False
    assert prepared_reply["error"]["code"] == "not_found"
    assert core["store"]().read_case(case_id)["prepared_ids"] == []


def test_public_prepare_rejects_missing_normalized_object_ref_without_empty_fallback(core, monkeypatch):
    rows = "site_id,site,cost\n1,only,10\n"
    case_id, data_id, candidate = _make_candidate(core, rows=rows, parameter_object_id="sites")
    assert candidate["ok"], candidate
    store = core["store"]()
    original_manifest_path = f".reaslab/optagent/cases/{case_id}/data/{data_id}/manifest.json"
    original_manifest_ref = store.ref_for_path(original_manifest_path)
    original_manifest = strict_json_loads(store.read_ref(original_manifest_ref))
    missing_path = original_manifest["objects"][0]["values"]["path"]
    original_read_ref = CaseStore.read_ref

    def reject_removed_object_bytes(self, ref):
        if ref["path"] == missing_path:
            raise ContractError("capture_unavailable", "the normalized object bytes are unavailable")
        return original_read_ref(self, ref)

    monkeypatch.setattr(CaseStore, "read_ref", reject_removed_object_bytes)
    prepared_reply = _public_prepare(core, case_id, candidate["result"]["candidate"]["id"])
    assert prepared_reply["ok"] is False
    assert prepared_reply["error"]["code"] == "capture_unavailable"
    assert core["store"]().read_case(case_id)["prepared_ids"] == []


def test_public_candidate_rejects_parameter_object_without_data_bundle(core):
    case_id, _, _ = core["create_case"](raw=b"Minimize cost.")
    (core["workspace"] / "model.py").write_text(
        "def build_model(bundle):\n    return None\n",
        encoding="utf-8",
    )
    state = core["store"]().read_case(case_id)
    candidate = core["call"]("optagent.case", {
        "op": "revise",
        "case_id": case_id,
        "expected_state_revision": state["state_revision"],
        "kind": "candidate",
        "model_path": "model.py",
        "local_module_paths": [],
        "data_id": None,
        "instance_parameter_object_id": "sites",
        "backend_id": "highs",
        "objective_plan": {
            "mode": "scalar",
            "objective_component": "cost",
            "sense": "min",
            "unit": "USD",
            "preference_note": "negative structural test",
        },
        "business_checker_path": None,
        "input_closure": "declared",
        "closure_note": "NoData cannot be bound to a parameter table.",
    })
    assert candidate["ok"] is False
    assert candidate["error"]["code"] == "invalid_argument"
    assert "requires a data bundle" in candidate["error"]["message"]
