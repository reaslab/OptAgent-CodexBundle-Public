from __future__ import annotations

import hashlib
import json
import posixpath
import shutil
import subprocess
import sys
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote

import pytest

from optagent_core.contracts import ContractError, canonical_json_bytes, ref_from_bytes, strict_json_loads
from family_fixtures import current_portable_review_fixture, publish_run_update, read_run
from optagent_core.data import PortableReader, load_data_bundle, read_prepared_source_records
from optagent_core.delivery import (
    _add_package_file,
    _candidate_package_path,
    _delivery_review_projection,
    _layout_equivalent,
    _package_review_closure,
    _package_runtime_bundle,
    _render_report,
    _result_has_bound,
)
from optagent_core.sources import data_source_records

_NO_BUSINESS_SOLUTION = object()


def test_package_paths_reserve_only_the_machine_state_root():
    assert _candidate_package_path("models/.reaslab/model.py") == "source/models/.reaslab/model.py"
    with pytest.raises(ContractError, match="source path is not safe"):
        _candidate_package_path(".reaslab/optagent/model.py")

    files = {}
    objects = []
    _add_package_file(files, "source/models/.reaslab/model.py", b"model", objects)
    assert files["source/models/.reaslab/model.py"] == b"model"


def test_package_api_refuses_dotenv_suffix_source_without_writing_zip(core):
    from test_reuse_proof import _proof_review_to_delivery

    case_id, _, _ = core["create_case"](
        raw=b"Prove the scoped statement using these source premises.",
        task_kind="proof",
    )
    secret = b"SYNTHETIC-ENV-SECRET=do-not-export"
    (core["workspace"] / ".env.production").write_bytes(secret)
    state = core["store"]().read_case(case_id)
    captured = core["call"](
        "optagent.data",
        {
            "op": "capture",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "path": ".env.production",
            "purpose": "reference",
            "locations": [],
        },
    )
    assert captured["ok"], captured

    case_id, _, _, _, delivery_id = _proof_review_to_delivery(core, case_id=case_id)
    state = core["store"]().read_case(case_id)
    packed = core["call"](
        "optagent.deliver",
        {
            "op": "package",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "delivery_id": delivery_id,
            "target_path": "exports/should-not-exist.zip",
            "include_raw_sources": True,
        },
    )
    assert not packed["ok"]
    assert packed["error"]["code"] == "path_denied"
    assert not (core["workspace"] / "exports" / "should-not-exist.zip").exists()


def test_review_report_package_and_reproduction_keep_issue_without_patch_attachments():
    case_id = "00000000-0000-4000-8000-000000000001"
    review_id = "00000000-0000-4000-8000-000000000002"
    issue_id = "00000000-0000-4000-8000-000000000003"
    stamp = {"session_id": "session-a", "role": "root", "turn_id": "turn-a", "call_id": "call-a"}
    blobs: dict[str, bytes] = {}

    def record(path: str, raw: bytes, media_type: str) -> dict[str, object]:
        blobs[path] = raw
        return ref_from_bytes(path, raw, media_type)

    evidence_ref = record("reports/original-evidence.txt", b"Original evidence", "text/plain")
    provenance_raw = canonical_json_bytes({
        "schema_version": 1,
        "case_id": case_id,
        "source_binding": {"source_revision": 1, "math_source_ids": []},
        "provenance_source_ids": [],
        "sources": [],
        "route": {
            "session_id": "session-a", "unknown": False, "reason": None,
            "member": None, "guards": [], "handoffs": [],
        },
    })
    provenance_ref = record(
        f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/source-provenance.json",
        provenance_raw, "application/json",
    )
    note_ref = record(
        f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/note.txt",
        b"Scope note", "text/plain; charset=utf-8",
    )
    answer_ref = record("reports/approved-answer.md", b"Approved answer for the selected Review.\n", "text/markdown")
    issue = {
        "issue_id": issue_id,
        "claim": "The candidate omits the stated condition.",
        "locations": [{"source_id": "00000000-0000-4000-8000-000000000004", "locator": "source-level"}],
        "candidate_locations": ["model.py:12"],
        "evidence_refs": [evidence_ref],
        "proposal_stamp": {**stamp, "role": "opt-verifier"},
        "owner_dispositions": [{
            "diagnosis": "confirmed", "reason": "The proposal is accepted for follow-up.",
            "patch_status": "accepted",
            "stamp": stamp, "created_at": "2026-10-10T00:00:00Z",
        }],
    }
    issue_raw = canonical_json_bytes(issue)
    issue_ref = record(
        f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/issues/{issue_id}/versions/{ref_from_bytes('unused', issue_raw, 'application/json')['sha256']}.json",
        issue_raw, "application/json",
    )
    review = {
        "schema_version": 4,
        "created_at": "2026-10-10T00:00:00Z",
        "created_by": stamp,
        "review_revision": 1,
        "case_id": case_id,
        "review_id": review_id,
        "status": "closed",
        "binding": {"source_revision": 1, "candidate_revision": 1},
        "raw_source_ids": [],
        "source_provenance_ref": provenance_ref,
        "source_provenance_digest": hashlib.sha256(provenance_raw).hexdigest(),
        "binding_evidence_refs": None,
        "prior_review_ref": None,
        "note": {"body": note_ref},
        "candidate_opened": False,
        "answer_ref": answer_ref,
        "outcomes": [{
            "decision": "ready", "feedback": [],
            "remaining": ["Owner follow-up remains open."], "stamp": stamp,
            "created_at": "2026-10-10T00:00:00Z",
        }],
        "conclusion": {
            "claims": [{
                "kind": "formulation_fidelity", "scope": "The captured requirement.",
                "support": "conditional", "conditions": ["The source review does not certify implementation."],
                "evidence_refs": [evidence_ref],
            }],
            "remaining": ["Owner follow-up remains open."],
            "stamp": stamp,
        },
        "issues": [issue_ref],
    }
    review_raw = canonical_json_bytes(review)
    review_ref = record(
        f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/versions/{ref_from_bytes('unused', review_raw, 'application/json')['sha256']}.json",
        review_raw, "application/json",
    )

    class MemoryStore:
        def read_family_record(self, requested_case, family, requested_review, *, legacy_path=None, include_raw=False):
            assert (requested_case, family, requested_review) == (case_id, "reviews", review_id)
            return (review, review_ref, review_raw) if include_raw else (review, review_ref)

        def read_ref(self, ref):
            raw = blobs[ref["path"]]
            assert ref_from_bytes(ref["path"], raw, ref["media_type"]) == dict(ref)
            return raw

    store = MemoryStore()
    state = {"case_id": case_id, "source_ids": []}
    report_reviews = _delivery_review_projection(store, state, {"review_ids": [review_id]})
    assert report_reviews[0]["remaining"] == ["Owner follow-up remains open."]
    assert report_reviews[0]["conclusion_claims"][0]["conditions"] == ["The source review does not certify implementation."]
    assert report_reviews[0]["answer_ref"] == answer_ref
    assert report_reviews[0]["outcome"] == review["outcomes"][-1]
    report = _render_report(
        {"title": "Proof review", "task_kind": "proof"},
        {"kind": "proof", "claims": []},
        ".md",
        review_projection=report_reviews,
    ).decode("utf-8")
    assert "The candidate omits the stated condition." in report
    assert "The proposal is accepted for follow-up." in report
    assert "Proposed patch" not in report and "Owner patch" not in report

    files: dict[str, bytes] = {}
    objects: list[dict[str, object]] = []
    ref_map: dict[tuple[str, str, int, str], dict[str, object]] = {}
    packaged, closure_refs = _package_review_closure(store, state, review_id, files, objects, ref_map)
    mapped_paths = {item["ref"]["path"] for item in ref_map.values()}
    packaged_issue = packaged["issues"][0]
    assert packaged_issue["claim"] == issue["claim"]
    assert "patch_ref" not in packaged_issue
    assert "patch_ref" not in packaged_issue["owner_dispositions"][0]
    assert packaged_issue["owner_dispositions"][0]["reason"] == issue["owner_dispositions"][0]["reason"]
    assert "evidence/patches/" not in "\n".join(files)
    assert {ref["path"] for ref in closure_refs} == set(blobs)
    assert issue_ref["path"] in mapped_paths and review_ref["path"] in mapped_paths
    assert evidence_ref["path"] in mapped_paths
    assert all(files[item["package_path"]] == blobs[item["ref"]["path"]] for item in ref_map.values())
    bootstrap = Path(__file__).resolve().parents[1] / "optagent_reproduce.py"
    probe = (
        "import importlib.util, json, sys\n"
        "spec = importlib.util.spec_from_file_location('isolated_reproducer', sys.argv[1])\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "issue = json.load(sys.stdin)\n"
        "assert set(issue) == module._ISSUE_FIELDS\n"
    )
    reproduction = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(bootstrap)],
        input=json.dumps({key: value for key, value in packaged_issue.items() if key != "issue_ref"}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert reproduction.returncode == 0, reproduction.stderr


def test_verified_sealed_math_result_survives_control_generation_change(monkeypatch):
    import optagent_core.delivery as delivery_module
    import optagent_core.runner as runner_module

    case_id = "00000000-0000-4000-8000-000000000011"
    run_id = "00000000-0000-4000-8000-000000000012"
    prepared_id = "00000000-0000-4000-8000-000000000013"
    candidate_id = "00000000-0000-4000-8000-000000000014"
    seal_ref = ref_from_bytes(f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/seal.json", b"{}", "application/json")
    result_raw = canonical_json_bytes({"status": "feasible observation"})
    result_ref = ref_from_bytes(f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/one/result.json", result_raw, "application/json")
    run_payload = ref_from_bytes("unused.json", b"run", "application/json")
    run_ref = {
        **run_payload,
        "path": f".reaslab/optagent/cases/{case_id}/runs/{run_id}/versions/{run_payload['sha256']}.json",
    }
    prepared_ref = ref_from_bytes(f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/prepared.json", b"prepared", "application/json")
    state = {
        "case_id": case_id, "run_ids": [run_id], "source_revision": 7,
        "candidate_revision": 3, "candidate_id": candidate_id, "promotion_generation": 10,
    }
    run = {
        "run_id": run_id, "prepared_id": prepared_id, "dispatch_state": "response_received",
        "worker": {"status": "complete", "seal": seal_ref}, "results": [result_ref],
    }
    prepared = {
        "prepared_id": prepared_id, "source_revision": 7, "candidate_revision": 3,
        "candidate_id": candidate_id, "promotion_generation": 8, "source_ids": ["math-source"],
    }
    manifest = {"binding_digest": "a" * 64}
    candidate = {
        "candidate_id": candidate_id, "candidate_revision": 3,
        "source_ids": ["math-source"], "binding_digest": "a" * 64,
    }

    class MemoryStore:
        def read_record(self, path):
            assert path == f".reaslab/optagent/cases/{case_id}/candidates/{candidate_id}.json"
            return candidate

        def read_ref(self, ref):
            assert ref == result_ref
            return result_raw

    monkeypatch.setattr(delivery_module, "_load_run", lambda *_args: (run, run_ref))
    monkeypatch.setattr(runner_module, "_load_prepared", lambda *_args: (prepared, prepared_ref, manifest))
    monkeypatch.setattr(
        runner_module,
        "_read_worker_seal",
        lambda *_args: ({"output_refs": [result_ref]}, seal_ref, [result_ref]),
    )
    monkeypatch.setattr(delivery_module, "source_projection", lambda *_args: {"math_source_ids": ["math-source"]})

    verified = delivery_module._verified_run(MemoryStore(), state, run_id, {}, require_current=True)
    assert prepared["promotion_generation"] != state["promotion_generation"]
    assert verified["current"] is True
    result_identity = (result_ref["path"], result_ref["sha256"], result_ref["byte_len"], result_ref["media_type"])
    assert verified["results"][result_identity] == {"status": "feasible observation"}

    candidate["binding_digest"] = "b" * 64
    with pytest.raises(ContractError, match="current Candidate"):
        delivery_module._verified_run(MemoryStore(), state, run_id, {}, require_current=True)


def test_report_and_zip_writes_report_local_committed_when_final_case_cas_fails(core):
    from optagent_core.delivery import build_report, package_delivery
    from test_reuse_proof import _proof_review_to_delivery

    case_id, _, _, _, delivery_id = _proof_review_to_delivery(core)
    store = core["store"]()
    state = store.read_case(case_id)
    selected_ref = state["current_records"]["presentation"]

    def reject_publication(_current):
        raise ContractError("stale_revision", "simulated final case compare-and-swap conflict")

    with pytest.raises(ContractError) as report_error:
        build_report(
            store,
            case_id=case_id,
            expected_state_revision=state["state_revision"],
            delivery_id=delivery_id,
            target_path="reports/committed-before-race.md",
            stamp=core["context"]()["stamp"],
            validate_current=reject_publication,
        )
    assert report_error.value.effect == "local_committed"
    assert (core["workspace"] / "reports" / "committed-before-race.md").is_file()
    assert store.read_case(case_id)["current_records"]["presentation"] == selected_ref

    with pytest.raises(ContractError) as package_error:
        package_delivery(
            store,
            case_id=case_id,
            expected_state_revision=state["state_revision"],
            delivery_id=delivery_id,
            target_path="exports/committed-before-race.zip",
            include_raw_sources=False,
            stamp=core["context"]()["stamp"],
            validate_current=reject_publication,
        )
    assert package_error.value.effect == "local_committed"
    assert (core["workspace"] / "exports" / "committed-before-race.zip").is_file()
    assert store.read_case(case_id)["current_records"]["presentation"] == selected_ref


def test_select_rechecks_route_at_final_case_publication(core):
    from optagent_core.delivery import select_delivery
    from test_reuse_proof import _proof_review_to_delivery

    case_id, review_id, proof_id, claim, _ = _proof_review_to_delivery(core)
    store = core["store"]()
    state = store.read_case(case_id)
    before = state["current_records"]["presentation"]
    checks = 0

    def route_changes_before_publish(_current):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise ContractError("capture_unavailable", "route changed before final selection publication")

    with pytest.raises(ContractError) as error:
        select_delivery(
            store,
            case_id=case_id,
            expected_state_revision=state["state_revision"],
            kind="proof",
            candidate_id=None,
            review_ids=[review_id],
            proof_ids=[proof_id],
            run_ids=[],
            stamp=core["context"]()["stamp"],
            validate_current=route_changes_before_publish,
        )
    assert checks == 2
    assert error.value.code == "capture_unavailable"
    assert store.read_case(case_id)["current_records"]["presentation"] == before


def test_cancelled_delivery_mutations_stop_before_any_operation(core):
    from optagent_core.api import _delivery_operation

    class ReadOnlyStore:
        def read_case(self, _case_id):
            raise AssertionError("a cancelled delivery must stop before reading Case or route state")

    context = {
        "stamp": {"session_id": "session-a", "role": "root", "turn_id": "turn-a", "call_id": "call-a"},
        "cancellation_observed": True,
    }
    for op in ("select", "build", "package"):
        with pytest.raises(ContractError) as error:
            _delivery_operation(ReadOnlyStore(), {"op": op, "case_id": "case-a"}, context)
        assert error.value.code == "execution_unknown"


@pytest.mark.parametrize("op", ["select", "build", "package"])
@pytest.mark.parametrize(
    ("role", "expected_code"),
    [("root", "execution_unknown"), ("opt-verifier", "permission_denied")],
)
def test_dispatch_cancelled_delivery_mutations_stop_before_store_creation(
    core, monkeypatch, op, role, expected_code
):
    import optagent_core.api as api

    case_id = str(uuid.uuid4())
    delivery_id = str(uuid.uuid4())
    if op == "select":
        arguments = {
            "op": op,
            "case_id": case_id,
            "expected_state_revision": 0,
            "kind": "proof",
            "candidate_id": None,
            "review_id": str(uuid.uuid4()),
            "proof_ids": [],
            "run_ids": [],
        }
    else:
        arguments = {
            "op": op,
            "case_id": case_id,
            "expected_state_revision": 0,
            "delivery_id": delivery_id,
            "target_path": (
                "reports/cancelled-dispatch.json"
                if op == "build"
                else "exports/cancelled-dispatch.zip"
            ),
        }
        if op == "package":
            arguments["include_raw_sources"] = False

    def unexpected_store(*_args, **_kwargs):
        raise AssertionError("cancelled public delivery dispatch must stop before Store creation")

    monkeypatch.setattr(api, "_store", unexpected_store)
    before = set(core["workspace"].iterdir())
    response = api.dispatch_dynamic(
        "optagent.deliver",
        arguments,
        core["context"](role=role, cancellation_observed=True),
    )

    assert response["kind"] == "complete"
    assert response["reply"]["ok"] is False
    assert response["reply"]["error"]["code"] == expected_code
    assert response["reply"]["state_revision"] is None
    assert set(core["workspace"].iterdir()) == before


def test_overflow_and_boolean_values_cannot_support_global_bound(core):
    case_id, _, _ = core["create_case"]()
    store = core["store"]()

    for name, raw in (
        ("overflow.json", b'{"bound":{"scope":"global","value":1e999}}'),
        ("boolean.json", b'{"bound":{"scope":"global","value":true}}'),
        ("nan.json", b'{"bound":{"scope":"global","value":NaN}}'),
    ):
        path = core["workspace"] / name
        path.write_bytes(raw)
        ref = ref_from_bytes(name, raw, "application/json")
        if name == "overflow.json":
            with pytest.raises(ValueError):
                strict_json_loads(raw)
        assert not _result_has_bound(store, ref)

    finite_raw = b'{"bound":{"scope":"global","value":12.5}}'
    (core["workspace"] / "finite.json").write_bytes(finite_raw)
    finite_ref = ref_from_bytes("finite.json", finite_raw, "application/json")
    assert _result_has_bound(store, finite_ref)


def test_delivery_summaries_count_open_nonblocking_questions(core):
    from test_reuse_proof import _proof_review_to_delivery

    case_id, review_id, proof_id, claim, _ = _proof_review_to_delivery(core)
    store = core["store"]()
    question_capture = core["capture"](b"A clarification that does not block delivery.", seq=2)
    state = store.read_case(case_id)
    question = core["call"](
        "optagent.case",
        {
            "op": "question",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "text": "Confirm one missing assumption.",
            "missing_fact": "The exact capacity for B.",
            "blocks": [],
            "independent_work": ["Continue reviewing the recorded proof."],
        },
        captures=[question_capture],
    )
    assert question["ok"], question
    state = store.read_case(case_id)
    assert len(state["question_ids"]) == 1

    selected = core["call"](
        "optagent.deliver",
        {
            "op": "select",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "kind": "proof",
            "candidate_id": None,
            "review_id": review_id,
            "proof_ids": [proof_id],
            "run_ids": [],
        },
    )
    assert selected["ok"], selected
    assert selected["result"]["editable_case"]["status"] == "not_generated"
    assert selected["result"]["editable_case"]["reason"] == "no_selected_run"

    def assert_summary_matches_get(reply):
        assert reply["ok"], reply
        summary = reply["result"]["state"]
        fetched = core["call"]("optagent.case", {"op": "get", "case_id": case_id})
        assert fetched["ok"], fetched
        assert summary["pending_question_count"] == 1
        assert summary["current_validity"] == "valid"
        assert summary["state_revision"] == fetched["result"]["state"]["state_revision"]
        assert fetched["result"]["state"]["pending_question_count"] == 1

    assert_summary_matches_get(selected)
    state = store.read_case(case_id)
    built = core["call"](
        "optagent.deliver",
        {
            "op": "build",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "delivery_id": selected["result"]["delivery"]["id"],
            "target_path": "reports/nonblocking.md",
        },
    )
    assert_summary_matches_get(built)

    state = store.read_case(case_id)
    packed = core["call"](
        "optagent.deliver",
        {
            "op": "package",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "delivery_id": selected["result"]["delivery"]["id"],
            "target_path": "exports/nonblocking.zip",
            "include_raw_sources": False,
        },
    )
    assert packed["ok"], packed
    assert_summary_matches_get(packed)
    archive_bytes = store.read_ref(packed["result"]["package_ref"])
    with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
        names = set(archive.namelist())
        package_manifest = strict_json_loads(archive.read("manifest.json"))
    assert "reproduce/run.py" not in names
    assert package_manifest["reproductions"] == []
    assert package_manifest["runtime_bundles"] == []
    _, review_ref = store.read_family_record(case_id, "reviews", review_id)
    review, _ = store.read_family_record(case_id, "reviews", review_id)
    mapped_refs = {
        (row["ref"]["path"], row["ref"]["sha256"], row["ref"]["byte_len"], row["ref"]["media_type"])
        for row in package_manifest["ref_map"]
    }
    for ref in (review_ref, review["note"]["body"], claim["evidence_refs"][0]):
        assert (ref["path"], ref["sha256"], ref["byte_len"], ref["media_type"]) in mapped_refs


def test_select_is_blocked_by_hidden_route_gap_through_real_dynamic_dispatch(core):
    from test_reuse_proof import _proof_review_to_delivery
    from test_session_routing import _install_route

    case_id, review_id, proof_id, claim, _ = _proof_review_to_delivery(core)
    state = core["store"]().read_case(case_id)
    _install_route(core, case_id, covered_seq=10, latest_seq=11)
    state_ref = core["store"]().ref_for_path(f".reaslab/optagent/cases/{case_id}/state.json")
    before = core["store"]().read_ref(state_ref)
    attempted = core["call"](
        "optagent.deliver",
        {
            "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
            "kind": "proof", "candidate_id": None,
            "review_id": review_id, "proof_ids": [proof_id], "run_ids": [],
        },
    )
    assert not attempted["ok"]
    assert attempted["error"]["code"] == "capture_unavailable"
    assert core["store"]().read_ref(state_ref) == before


def test_candidate_and_active_raw_formulation_only_delivery_is_real_and_has_no_solver_facts(core):
    case_id, _, _ = core["create_case"](raw=b"Build a model that represents the captured requirement.")
    store = core["store"]()
    (core["workspace"] / "model.py").write_text("def build_model(data):\n    return None\n", encoding="utf-8")
    state = store.read_case(case_id)
    registered = core["call"](
        "optagent.case",
        {
            "op": "revise", "kind": "candidate", "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "model_path": "model.py", "local_module_paths": [], "data_id": None,
            "backend_id": "highs",
            "objective_plan": {"mode": "feasibility", "preference_note": "formulation-only review"},
            "business_checker_path": None, "input_closure": "declared", "closure_note": "No data files.",
        },
    )
    assert registered["ok"], registered
    candidate_id = registered["result"]["candidate"]["id"]
    state = store.read_case(case_id)
    source_record = store.read_record(f".reaslab/optagent/cases/{case_id}/sources/{state['source_ids'][0]}.json")
    source_ref = source_record["bytes"]
    candidate_ref = store.ref_for_path(f".reaslab/optagent/cases/{case_id}/candidates/{candidate_id}.json")
    begun = core["call"](
        "optagent.review",
        {
            "op": "begin", "case_id": case_id, "expected_state_revision": state["state_revision"],
            "candidate_id": candidate_id, "proof_ids": [], "run_ids": [],
        },
        role="opt-verifier",
    )
    assert begun["ok"], begun
    review_id = begun["result"]["review"]["id"]
    assert begun["result"]["review"]["review_revision"] == 1
    noted = core["call"](
        "optagent.review",
        {
            "op": "note", "case_id": case_id, "review_id": review_id,
            "expected_review_revision": begun["result"]["review"]["review_revision"], "body": "The captured requirement defines the whole review scope.",
            "source_locations": [{"source_id": state["source_ids"][0], "locator": "source-level"}],
        },
        role="opt-verifier",
    )
    assert noted["ok"], noted
    assert noted["result"]["review"]["review_revision"] == 2
    claim = {
        "kind": "formulation_fidelity", "scope": "The current candidate represents the active source.",
        "conditions": [], "evidence_refs": [candidate_ref, source_ref], "support": "supported",
    }
    closed = _core11_close(core, case_id, review_id, [claim])
    assert closed["ok"], closed

    state = store.read_case(case_id)
    selected = core["call"](
        "optagent.deliver",
        {
            "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
            "kind": "mixed", "candidate_id": candidate_id,
            "review_id": review_id, "proof_ids": [], "run_ids": [],
        },
    )
    assert selected["ok"], selected
    delivery_id = selected["result"]["delivery"]["id"]
    reviewed, _ = store.read_family_record(case_id, "reviews", review_id)
    selected_record = store.read_record(
        f".reaslab/optagent/cases/{case_id}/deliveries/{delivery_id}/delivery.json"
    )
    assert selected_record["claims"] == reviewed["conclusion"]["claims"]
    assert selected_record["claims"][0]["evidence_refs"] == [candidate_ref, source_ref]
    assert selected_record["reports"] == [reviewed["answer_ref"]]
    state = store.read_case(case_id)
    report = core["call"](
        "optagent.deliver",
        {
            "op": "build", "case_id": case_id, "expected_state_revision": state["state_revision"],
            "delivery_id": delivery_id, "target_path": "reports/formulation-only.json",
        },
    )
    assert report["ok"], report
    summary = strict_json_loads(store.read_ref(report["result"]["summary_ref"]))
    assert summary["result_evidence"] == []
    assert not any(key in summary for key in ("solver_status", "objective", "bound", "gap"))
    state = store.read_case(case_id)
    package = core["call"](
        "optagent.deliver",
        {
            "op": "package", "case_id": case_id, "expected_state_revision": state["state_revision"],
            "delivery_id": delivery_id, "target_path": "exports/formulation-only.zip", "include_raw_sources": True,
        },
    )
    assert package["ok"], package
    with zipfile.ZipFile(BytesIO(store.read_ref(package["result"]["package_ref"]))) as archive:
        names = set(archive.namelist())
        manifest = strict_json_loads(archive.read("manifest.json"))
    assert "source/model.py" in names
    assert manifest["entry"] == "README.md"
    assert manifest["reproductions"] == []
    assert manifest["runtime_bundles"] == []


def test_unknown_run_is_reported_as_runtime_fact_without_review_or_delivery(core):
    from optagent_core.api import dispatch_dynamic

    case_id, _, _ = core["create_case"](raw=b"A candidate needs review before numerical execution.")
    store = core["store"]()
    (core["workspace"] / "unknown-builder.py").write_text(
        "def build_model(bundle):\n    raise RuntimeError('diagnostic fixture must not import candidate code')\n",
        encoding="utf-8",
    )
    state = store.read_case(case_id)
    candidate = core["call"]("optagent.case", {
        "op": "revise", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "candidate", "model_path": "unknown-builder.py",
        "local_module_paths": [], "data_id": None, "backend_id": "highs",
        "objective_plan": {"mode": "feasibility", "preference_note": "weak diagnostic test"},
        "business_checker_path": None, "input_closure": "declared", "closure_note": "No execution evidence.",
    })
    assert candidate["ok"], candidate
    candidate_id = candidate["result"]["candidate"]["id"]
    state = store.read_case(case_id)
    prepared = core["call"]("optagent.run", {
        "op": "prepare", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "candidate_id": candidate_id,
        "solve_options": {
            "execution_budget_seconds": 60, "time_limit_seconds": 30, "relative_gap": 0.0,
            "absolute_gap": 0.0, "threads": 1, "random_seed": 0,
            "feasibility_tolerance": 1e-7, "integrality_tolerance": 1e-7,
            "write_log": False, "export_model": False,
        },
    })
    assert prepared["ok"], prepared
    prepared_id = prepared["result"]["prepared"]["id"]
    state = store.read_case(case_id)
    begun_dispatch = dispatch_dynamic("optagent.run", {
        "op": "start", "case_id": case_id, "prepared_id": prepared_id,
        "expected_state_revision": state["state_revision"],
    }, core["context"]())
    assert begun_dispatch.get("kind") == "start_plan"
    run_id = begun_dispatch["plan"]["run_id"]
    inspected = core["call"]("optagent.run", {
        "op": "inspect", "case_id": case_id, "run_id": run_id,
        "after_instance": 0, "limit": 20,
    }, role="opt-verifier")
    assert inspected["ok"], inspected
    assert inspected["result"]["run"]["dispatch_state"] == "unknown"
    assert inspected["result"]["run"]["result_count"] == 0
    from family_fixtures import publish_run_update, read_run

    response_ref = store.write_immutable(
        f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/transport-response.json",
        b'{"dispatch":"unknown","fixture":"synthetic transport observation"}',
        "application/json",
    )
    status_ref = store.write_immutable(
        f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/platform-status.json",
        b'{"status":"unknown","fixture":"synthetic platform observation"}',
        "application/json",
    )
    run, _run_ref = read_run(store, case_id, run_id)

    def add_transport_observations(current_run):
        current_run["transport"]["response"] = response_ref
        current_run["platform"]["status_evidence"] = status_ref

    run, _updated_run_ref = publish_run_update(store, case_id, run_id, add_transport_observations)

    current_run, _ = read_run(store, case_id, run_id)
    assert current_run["dispatch_state"] == "unknown"
    assert current_run["results"] == []
    assert current_run["transport"]["response"] == response_ref
    assert current_run["platform"]["status_evidence"] == status_ref
    assert store.read_ref(response_ref).startswith(b'{"dispatch":"unknown"')
    state = store.read_case(case_id)
    assert state["selected_delivery_id"] is None
    assert state["review_ids"] == []


def _core11_solve_options() -> dict:
    return {
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


def _core11_register_candidate(
    core,
    case_id: str,
    model_path: str = "core11-builder.py",
    *,
    data_id: str | None = None,
    instance_parameter_object_id: str | None = None,
) -> str:
    store = core["store"]()
    (core["workspace"] / model_path).write_text(
        "def build_model(bundle):\n    raise RuntimeError('Core11 structural test must not import candidate code')\n",
        encoding="utf-8",
    )
    state = store.read_case(case_id)
    request = {
        "op": "revise", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "candidate", "model_path": model_path,
        "local_module_paths": [], "data_id": data_id, "backend_id": "highs",
        "objective_plan": {"mode": "feasibility", "preference_note": "Core11 diagnostic fixture"},
        "business_checker_path": None, "input_closure": "declared", "closure_note": "Structural fixture only.",
    }
    if instance_parameter_object_id is not None:
        request["instance_parameter_object_id"] = instance_parameter_object_id
    registered = core["call"]("optagent.case", request)
    assert registered["ok"], registered
    return registered["result"]["candidate"]["id"]


def _core11_start_run(core, case_id: str, candidate_id: str, *, inspect_unknown: bool = True, cancel_before_dispatch: bool = False):
    from optagent_core.api import dispatch_dynamic
    from optagent_core.runner import dispatch_run_bridge

    store = core["store"]()
    state = store.read_case(case_id)
    prepared = core["call"]("optagent.run", {
        "op": "prepare", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "candidate_id": candidate_id, "solve_options": _core11_solve_options(),
    })
    assert prepared["ok"], prepared
    prepared_id = prepared["result"]["prepared"]["id"]
    state = store.read_case(case_id)
    start_context = core["context"]()
    start_args = {
        "op": "start", "case_id": case_id, "prepared_id": prepared_id,
        "expected_state_revision": state["state_revision"],
    }
    if state.get("run_ids"):
        latest_run_id = state["run_ids"][-1]
        latest_run, _ = store.read_family_record(case_id, "runs", latest_run_id, state=state)
        if latest_run.get("dispatch_state") in {"requested", "unknown"}:
            start_args["supersedes_run_id"] = latest_run_id
            start_args["attempt_reason"] = "The owner explicitly continues after the unresolved latest attempt."
    started = dispatch_dynamic("optagent.run", start_args, start_context)
    assert started.get("kind") == "start_plan", started
    run_id = started["plan"]["run_id"]
    if cancel_before_dispatch:
        plan = started["plan"]
        bridge = dispatch_run_bridge(store, {
            "kind": "start_recheck", "case_id": case_id, "run_id": run_id,
            "prepared_id": prepared_id, "dispatch_nonce": plan["dispatch_nonce"],
            "request_digest": plan["request_digest"],
        }, {**start_context, "cancellation_observed": True})
        assert bridge.get("kind") == "complete", bridge
        assert bridge["reply"]["error"]["code"] == "stale_revision", bridge
        assert read_run(store, case_id, run_id)[0]["dispatch_state"] == "without_dispatch"
    elif inspect_unknown:
        inspected = core["call"]("optagent.run", {
            "op": "inspect", "case_id": case_id, "run_id": run_id,
            "after_instance": 0, "limit": 20,
        }, role="opt-verifier")
        assert inspected["ok"], inspected
        assert inspected["result"]["run"]["dispatch_state"] == "unknown"
        assert inspected["result"]["run"]["result_count"] == 0
    return run_id, prepared_id


def test_python_execute_request_keeps_body_budget_and_adds_http_grace():
    from optagent_core import runner as runner_module

    exact_body, exact_http_timeout, exact_digest = runner_module._python_execute_request("pass", 10)
    assert strict_json_loads(exact_body) == {"code": "pass", "timeout_seconds": 10}
    assert exact_http_timeout == 40
    assert exact_digest == hashlib.sha256(
        f"POST\n/api/internal/python/execute\n{exact_body}\n40".encode("utf-8")
    ).hexdigest()


def test_python_execute_deadline_is_shared_and_bound_into_dispatch_digest(core):
    from optagent_core.api import dispatch_dynamic
    from optagent_core import runner as runner_module

    case_id, _, _ = core["create_case"](raw=b"Exercise the immutable Python execute deadline binding.")
    candidate_id = _core11_register_candidate(core, case_id)
    state = core["store"]().read_case(case_id)
    options = _core11_solve_options()
    options["execution_budget_seconds"] = 60
    prepared_response = core["call"]("optagent.run", {
        "op": "prepare", "case_id": case_id,
        "expected_state_revision": state["state_revision"],
        "candidate_id": candidate_id, "solve_options": options,
    })
    assert prepared_response["ok"], prepared_response
    prepared_id = prepared_response["result"]["prepared"]["id"]

    state = core["store"]().read_case(case_id)
    started = dispatch_dynamic("optagent.run", {
        "op": "start", "case_id": case_id, "prepared_id": prepared_id,
        "expected_state_revision": state["state_revision"],
    }, core["context"]())
    assert started.get("kind") == "start_plan", started

    store = core["store"]()
    run_id = started["plan"]["run_id"]
    run, _ = read_run(store, case_id, run_id)
    from optagent_core.runner import _dispatch_plan, _load_prepared

    prepared, _, _manifest = _load_prepared(store, case_id, prepared_id)
    request = started["plan"]["request"]
    assert strict_json_loads(request["body_json"])["timeout_seconds"] == 60
    assert request["http_timeout_seconds"] == 90
    expected_digest = hashlib.sha256(
        f"POST\n/api/internal/python/execute\n{request['body_json']}\n90".encode("utf-8")
    ).hexdigest()
    assert started["plan"]["request_digest"] == run["http_request_digest"] == expected_digest

    rebuilt = _dispatch_plan(store, case_id, run, prepared)
    assert rebuilt["request"] == request
    assert rebuilt["request_digest"] == expected_digest

    changed_deadline_run = dict(run)
    changed_deadline_run["http_request_digest"] = hashlib.sha256(
        f"POST\n/api/internal/python/execute\n{request['body_json']}\n91".encode("utf-8")
    ).hexdigest()
    with pytest.raises(ContractError, match="reconstructed dispatch plan digest"):
        _dispatch_plan(store, case_id, changed_deadline_run, prepared)

    changed_digest_run = dict(run)
    changed_digest_run["http_request_digest"] = "0" * 64
    with pytest.raises(ContractError, match="reconstructed dispatch plan digest"):
        _dispatch_plan(store, case_id, changed_digest_run, prepared)


def test_new_start_after_unresolved_latest_requires_exact_supersession_and_recheck_head(core, monkeypatch):
    from optagent_core.api import dispatch_dynamic
    from optagent_core import runner as runner_module
    from optagent_core.runner import dispatch_run_bridge

    case_id, _, _ = core["create_case"](raw=b"A new attempt must explicitly continue an unresolved latest run.")
    candidate_id = _core11_register_candidate(core, case_id)
    store = core["store"]()

    def prepare_again() -> str:
        state = store.read_case(case_id)
        prepared = core["call"]("optagent.run", {
            "op": "prepare", "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "candidate_id": candidate_id, "solve_options": _core11_solve_options(),
        })
        assert prepared["ok"], prepared
        return prepared["result"]["prepared"]["id"]

    def start(prepared_id: str, **extra):
        state = store.read_case(case_id)
        return dispatch_dynamic("optagent.run", {
            "op": "start", "case_id": case_id, "prepared_id": prepared_id,
            "expected_state_revision": state["state_revision"], **extra,
        }, core["context"]())

    def original_start_context(run_id: str) -> dict:
        run, _ = read_run(store, case_id, run_id)
        context = core["context"]()
        context["stamp"] = dict(run["created_by"])
        return context

    first_prepared = prepare_again()
    first = start(first_prepared)
    assert first.get("kind") == "start_plan", first
    first_run_id = first["plan"]["run_id"]
    case_status = core["call"](
        "optagent.case",
        {"op": "get", "case_id": case_id},
        role="opt-verifier",
    )
    assert case_status["ok"], case_status
    assert any(
        "latest Run " in action
        and " is unresolved" in action
        and first_run_id in action
        for action in case_status["result"]["next_actions"]
    )

    second_prepared = prepare_again()
    missing_decision = start(second_prepared)
    assert missing_decision.get("kind") == "complete", missing_decision
    assert missing_decision["reply"]["ok"] is False
    assert missing_decision["reply"]["error"]["code"] == "execution_unknown"
    latest_run, latest_ref = read_run(store, case_id, first_run_id)
    latest_summary = missing_decision["reply"]["error"]["details"]["run"]
    assert latest_summary["id"] == first_run_id
    assert latest_summary["record_ref"] == latest_ref
    assert latest_summary["dispatch_state"] == latest_run["dispatch_state"] == "requested"
    assert "platform_job_status" not in latest_summary
    assert store.read_case(case_id)["run_ids"] == [first_run_id]

    wrong_supersession = start(
        second_prepared,
        supersedes_run_id=str(uuid.uuid4()),
        attempt_reason="This ID is deliberately stale.",
    )
    assert wrong_supersession["reply"]["error"]["details"]["run"] == latest_summary
    malformed_supersession = start(second_prepared, supersedes_run_id="not-a-run-id")
    assert malformed_supersession["reply"]["ok"] is False
    assert malformed_supersession["reply"]["error"]["effect"] == "none"
    assert "run" not in malformed_supersession["reply"]["error"]["details"]
    assert store.read_case(case_id)["run_ids"] == [first_run_id]
    missing_reason = start(second_prepared, supersedes_run_id=first_run_id)
    assert missing_reason["reply"]["error"]["details"]["run"] == latest_summary

    second = start(
        second_prepared,
        supersedes_run_id=first_run_id,
        attempt_reason="Owner explicitly continues after the unresolved latest attempt.",
    )
    assert second.get("kind") == "start_plan", second
    second_run_id = second["plan"]["run_id"]
    second_plan = second["plan"]

    first_plan = first["plan"]
    stale_first = dispatch_run_bridge(store, {
        "kind": "start_recheck", "case_id": case_id,
        "run_id": first_run_id, "prepared_id": first_prepared,
        "dispatch_nonce": first_plan["dispatch_nonce"],
        "request_digest": first_plan["request_digest"],
    }, original_start_context(first_run_id))
    assert stale_first["kind"] == "complete", stale_first
    assert stale_first["reply"]["error"]["code"] == "stale_revision"
    assert read_run(store, case_id, first_run_id)[0]["dispatch_state"] == "without_dispatch"

    original_plan = runner_module._dispatch_plan
    third_attempt = {}

    def register_a_newer_head_before_final_start_cas(store_arg, case_arg, run_arg, prepared_arg):
        plan = original_plan(store_arg, case_arg, run_arg, prepared_arg)
        if run_arg["run_id"] == second_run_id:
            third_prepared = prepare_again()
            third_attempt["prepared_id"] = third_prepared
            third_attempt["start"] = start(
                third_prepared,
                supersedes_run_id=second_run_id,
                attempt_reason="Owner explicitly continues after the latest requested attempt.",
            )
        return plan

    monkeypatch.setattr(runner_module, "_dispatch_plan", register_a_newer_head_before_final_start_cas)
    stale_at_final_fence = dispatch_run_bridge(store, {
        "kind": "start_recheck", "case_id": case_id,
        "run_id": second_run_id, "prepared_id": second_prepared,
        "dispatch_nonce": second_plan["dispatch_nonce"],
        "request_digest": second_plan["request_digest"],
    }, original_start_context(second_run_id))
    assert stale_at_final_fence["kind"] == "complete", stale_at_final_fence
    assert stale_at_final_fence["reply"]["error"]["code"] == "stale_revision"
    assert read_run(store, case_id, second_run_id)[0]["dispatch_state"] == "without_dispatch"
    third = third_attempt["start"]
    assert third.get("kind") == "start_plan", third
    third_run_id = third["plan"]["run_id"]

    latest_ready = dispatch_run_bridge(store, {
        "kind": "start_recheck", "case_id": case_id,
        "run_id": third_run_id, "prepared_id": third_attempt["prepared_id"],
        "dispatch_nonce": third["plan"]["dispatch_nonce"],
        "request_digest": third["plan"]["request_digest"],
    }, original_start_context(third_run_id))
    assert latest_ready["kind"] == "start_ready", latest_ready


def _core11_add_observations(core, case_id: str, run_id: str, *, include_status: bool = True):
    store = core["store"]()
    root = f".reaslab/optagent/cases/{case_id}/runs/{run_id}"
    response_ref = store.write_immutable(
        f"{root}/output/transport-response.json",
        b'{"dispatch":"unknown","fixture":"synthetic transport observation"}',
        "application/json",
    )
    status_ref = None
    if include_status:
        status_ref = store.write_immutable(
            f"{root}/output/platform-status.json",
            b'{"status":"unknown","fixture":"synthetic platform observation"}',
            "application/json",
        )
    def add_observations(run):
        run["transport"]["response"] = response_ref
        if status_ref is not None:
            run["platform"]["status_evidence"] = status_ref

    _run, run_ref = publish_run_update(store, case_id, run_id, add_observations)
    return run_ref, response_ref, status_ref


def _core11_begin_note(core, case_id: str, candidate_id: str, run_ids: list[str]) -> str:
    store = core["store"]()
    state = store.read_case(case_id)
    begun = core["call"]("optagent.review", {
        "op": "begin", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "candidate_id": candidate_id, "proof_ids": [], "run_ids": run_ids,
    }, role="opt-verifier")
    assert begun["ok"], begun
    review_id = begun["result"]["review"]["id"]
    state = store.read_case(case_id)
    noted = core["call"]("optagent.review", {
        "op": "note", "case_id": case_id, "review_id": review_id,
        "expected_review_revision": 1,
        "body": "The immutable source and exact run observations define this review scope.",
        "source_locations": [{"source_id": state["source_ids"][0], "locator": "source-level"}],
    }, role="opt-verifier")
    assert noted["ok"], noted
    return review_id


def _core11_close(
    core,
    case_id: str,
    review_id: str,
    claims: list[dict],
    *,
    report_path: str | None = "reports/approved-answer.md",
):
    public_claims = []
    for claim in claims:
        public_claim = dict(claim)
        refs = public_claim.pop("evidence_refs", None)
        if refs is not None:
            public_claim["evidence_paths"] = [ref["path"] for ref in refs]
        public_claims.append(public_claim)
    if report_path is not None:
        path = core["workspace"].joinpath(*report_path.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("Final report bytes reviewed for this claim.\n", encoding="utf-8")
    return core["call"]("optagent.review", {
        "op": "submit", "case_id": case_id, "review_id": review_id,
        "expected_review_revision": 2, "record_kind": "outcome", "decision": "ready",
        "claims": public_claims, "feedback": [], "remaining": [], "report_path": report_path,
    }, role="opt-verifier")


def _public_close_claim(claim: dict) -> dict:
    public_claim = {key: value for key, value in claim.items() if key != "evidence_refs"}
    public_claim["evidence_paths"] = [ref["path"] for ref in claim.get("evidence_refs", [])]
    return public_claim


def test_solver_requirements_include_shared_pyomo_and_selected_bindings():
    from optagent_core.delivery import _observed_default_interface_requirements, _solver_requirements_bytes

    requirements = _solver_requirements_bytes([
        {
            "id": "cvxpy_copt",
            "package_versions": {"pyomo": "6.9.4", "cvxpy": "1.7.3", "coptpy": "8.0.2"},
        },
        {
            "id": "ortools_clp",
            "package_versions": {"pyomo": "6.9.4", "ortools": "9.14.6206"},
        },
        {
            "id": "xpress",
            "package_versions": {"pyomo": "6.9.4", "xpress": "9.5.5"},
        },
    ]).decode("utf-8")

    assert "pyomo==6.9.4" in requirements
    assert "cvxpy==1.7.3" in requirements
    assert "coptpy==8.0.2" in requirements
    assert "ortools==9.14.6206" in requirements
    assert "xpress==9.5.5" in requirements

    unrecorded = _solver_requirements_bytes([{"id": "highs", "package_versions": {"pyomo": "6.9.4"}}]).decode("utf-8")
    assert "pyomo==6.9.4" in unrecorded
    assert "highspy  # version not recorded for the selected solver interface" in unrecorded
    assert "highspy==" not in unrecorded
    assert _observed_default_interface_requirements([{"id": "highs", "package_versions": {"pyomo": "6.9.4"}}]) == [
        {"distribution": "highspy", "observed_version": None},
        {"distribution": "pyomo", "observed_version": "6.9.4"},
    ]


def test_cp_sat_ortools_dependency_preserves_frozen_declarations_and_observes_versions():
    from optagent_core.delivery import _solver_requirements_bytes

    observed_versions = ("9.12.9999", "9.14.6206")
    frozen_declarations = (b"ORTools\n", b"ortools>=9.0,<10\n", b"ortools==9.11.0\n")

    for version in observed_versions:
        profile = {"id": "cp_sat", "package_versions": {"pyomo": "6.9.4", "ortools": version}}
        generated = _solver_requirements_bytes([profile]).decode("utf-8")
        generated_requirements = [
            line.strip()
            for line in generated.splitlines()
            if not line.lstrip().startswith("#") and line.split(";", 1)[0].strip().casefold().startswith("ortools")
        ]

        assert generated_requirements == ["ortools"]
        assert f"# Observed runtime version: ortools=={version} (informational only; it does not determine the package requirement)." in generated
        assert "internal build" not in generated
        assert "public PyPI pin" not in generated
        assert "protobuf==5.29.5" in generated

        for frozen in frozen_declarations:
            declared = _solver_requirements_bytes(
                [profile],
                declared_requirements=[frozen],
            ).decode("utf-8")
            declared_ortools_requirements = [
                line.strip()
                for line in declared.splitlines()
                if not line.lstrip().startswith("#") and line.split(";", 1)[0].strip().casefold().startswith("ortools")
            ]

            assert declared.startswith(frozen.decode("utf-8"))
            assert declared_ortools_requirements == [frozen.decode("utf-8").strip()]
            assert f"# Observed runtime version: ortools=={version} (informational only; it does not determine the package requirement)." in declared

    declared_without_observation = _solver_requirements_bytes(
        [{"id": "cp_sat", "package_versions": {"pyomo": "6.9.4"}}],
        declared_requirements=[b"ortools>=9.0,<10\n"],
    ).decode("utf-8")
    assert declared_without_observation.startswith("ortools>=9.0,<10\n")
    assert "ortools  # version not recorded for the selected solver interface" not in declared_without_observation

    other_backend = _solver_requirements_bytes([{
        "id": "ortools_clp",
        "package_versions": {"pyomo": "6.9.4", "ortools": "9.14.6206"},
    }]).decode("utf-8")
    assert "ortools==9.14.6206" in other_backend


def test_direct_requirements_preserve_declarations_and_export_observed_versions_without_conflicts():
    from optagent_core.delivery import _solver_requirements_bytes

    frozen = (
        b"numpy\n"
        b"PyYAML==6.0\n"
        b"platform_pkg; python_version < '3'\n"
        b"git+https://example.invalid/repo.git#egg=legacy-dist\n"
    )
    rendered = _solver_requirements_bytes(
        [{
            "id": "direct_python",
            "package_versions": {"numpy": "2.3.2", "pyyaml": "6.0", "legacy-dist": "1.4", "platform_pkg": "3.2"},
        }],
        declared_requirements=[frozen],
    )

    assert rendered.startswith(frozen)
    assert b"numpy==2.3.2" in rendered
    assert b"PyYAML==6.0\n" in rendered
    assert b"PyYAML==6.0\nPyYAML==" not in rendered
    assert b"Observed runtime version: pyyaml==6.0" in rendered
    assert b"Observed runtime version: legacy-dist==1.4" in rendered
    assert b"Observed runtime version: platform_pkg==3.2" in rendered
    assert b"platform_pkg==3.2\n" not in rendered


@pytest.mark.parametrize(
    "business_objective_delta,checker_returns_uncovered",
    [(0.0, False), (1e-4, False), (0.0, True)],
)
def test_direct_e2e(core, business_objective_delta, checker_returns_uncovered, tmp_path):
    from optagent_core.api import dispatch_dynamic
    from optagent_core.worker import run_invocation

    case_id, _, _ = core["create_case"](raw=b"Minimize cost subject to quantity being at least three.")
    store = core["store"]()
    frozen_requirements = b"# frozen at run_prepare\nnumpy\n"
    (core["workspace"] / "requirements.txt").write_bytes(frozen_requirements)
    state = store.read_case(case_id)
    model_path = f"{state['artifact_dir']}/code/optimization_model.py"
    checker_path = f"{state['artifact_dir']}/code/solution_check.py"
    model_source = (
        "import numpy as np\n"
        "def solve(bundle):\n"
        "    return {\n"
        "        'variables': [{'component': 'quantity', 'key': [], 'domain': 'NonNegativeReals', 'value': int(np.asarray([3], dtype=np.int64)[0])}],\n"
        "        'objective_values': [{'component': 'cost', 'sense': 'min', 'unit': 'USD', 'value': float(np.float64(6))}],\n"
        "    }\n"
        "def extract_solution(bundle, witness):\n"
        "    quantity = next(row['value'] for row in witness['variables'] if row['component'] == 'quantity')\n"
        "    return {'quantity': quantity}\n"
    )
    if checker_returns_uncovered:
        checker_source = (
            "def check_solution(bundle, solution):\n"
            "    return {\n"
            "        'status': 'uncovered',\n"
            "        'checked_obligations': [],\n"
            "        'objective_values': [],\n"
            "        'violations': [],\n"
            "        'uncovered': ['source objective is not covered'],\n"
            "    }\n"
        )
    else:
        checker_source = (
            "import numpy as np\n"
            "def check_solution(bundle, solution):\n"
            "    quantity = solution['quantity']\n"
            f"    objective = float(np.float64(2 * quantity + {business_objective_delta!r}))\n"
            "    valid = quantity >= 3\n"
            "    return {\n"
            "        'status': 'pass' if valid else 'fail',\n"
            "        'checked_obligations': [],\n"
            "        'objective_values': [{'component': 'cost', 'sense': 'min', 'unit': 'USD', 'value': objective}],\n"
            "        'violations': [] if valid else ['quantity or objective mismatch'],\n"
            "        'uncovered': [],\n"
            "    }\n"
        )
    for path, source in ((model_path, model_source), (checker_path, checker_source)):
        target = core["workspace"].joinpath(*path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")

    state = store.read_case(case_id)
    registered = core["call"]("optagent.case", {
        "op": "revise", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "candidate", "model_path": model_path, "local_module_paths": [], "data_id": None,
        "backend_id": "direct_python",
        "objective_plan": {
            "mode": "scalar", "objective_component": "cost", "sense": "min", "unit": "USD",
            "preference_note": "direct scalar point fixture",
        },
        "business_checker_path": checker_path, "input_closure": "declared", "closure_note": "Frozen direct-run delivery fixture.",
    })
    assert registered["ok"], registered
    candidate_id = registered["result"]["candidate"]["id"]

    state = store.read_case(case_id)
    prepared_reply = core["call"]("optagent.run", {
        "op": "prepare", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "candidate_id": candidate_id,
        "solve_options": {
            "execution_budget_seconds": 60, "time_limit_seconds": None,
            "relative_gap": 0.0, "absolute_gap": 0.0, "threads": None, "random_seed": None,
            "feasibility_tolerance": 1e-7, "integrality_tolerance": 1e-7,
            "write_log": False, "export_model": False, "warm_start": False,
        },
    })
    assert prepared_reply["ok"], prepared_reply
    prepared_id = prepared_reply["result"]["prepared"]["id"]
    prepared = store.read_record(f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/prepared.json")
    manifest = strict_json_loads(store.read_ref(prepared["input_manifest"]))
    requirements_ref = manifest["candidate"]["requirements_file"]["bytes"]
    assert store.read_ref(requirements_ref) == frozen_requirements

    state = store.read_case(case_id)
    start_context = core["context"]()
    started = dispatch_dynamic("optagent.run", {
        "op": "start", "case_id": case_id, "prepared_id": prepared_id,
        "expected_state_revision": state["state_revision"],
    }, start_context)
    assert started.get("kind") == "start_plan", started
    run_id = started["plan"]["run_id"]
    run, _ = read_run(store, case_id, run_id)
    from optagent_core.runner import dispatch_run_bridge

    ready = dispatch_run_bridge(store, {
        "kind": "start_recheck", "case_id": case_id, "run_id": run_id,
        "prepared_id": prepared_id, "dispatch_nonce": started["plan"]["dispatch_nonce"],
        "request_digest": started["plan"]["request_digest"],
    }, start_context)
    assert ready.get("kind") == "start_ready", ready
    worker_result = run_invocation(
        str(store.workspace), run["worker_invocation"]["path"], run["worker_invocation"]["sha256"]
    )
    assert worker_result["result_count"] == 1
    assert worker_result["solver_invoked"] is False
    inspected = core["call"]("optagent.run", {
        "op": "inspect", "case_id": case_id, "run_id": run_id,
        "after_instance": 0, "limit": 20,
    }, role="opt-verifier")
    assert inspected["ok"], inspected
    assert inspected["result"]["run"]["worker_status"] == "complete"
    result_refs = inspected["result"]["result_refs"]
    assert len(result_refs) == 1
    result = strict_json_loads(store.read_ref(result_refs[0]))
    witness = strict_json_loads(store.read_ref(result["witness"]))
    business = strict_json_loads(store.read_ref(result["check"]))
    assert result["objective"] == witness["objective_values"][0]
    if checker_returns_uncovered:
        assert inspected["result"]["run"]["worker_status"] == "complete"
        assert business["status"] == "uncovered" and business["objective_values"] == []
        assert result["objective"] == {"component": "cost", "sense": "min", "unit": "USD", "value": 6.0}
        assert result["has_checked_candidate"] is False
        assert result["termination"] == "unknown"
        return
    if business_objective_delta:
        from optagent_core.delivery import _result_supports_claim

        assert result["check"]["path"].endswith("/business-check.json")
        assert business["status"] == "fail"
        assert business["objective_values"][0]["value"] == pytest.approx(6.0001)
        assert "business_checker_objective_mismatch" in business["violations"]
        assert result["has_checked_candidate"] is False
        assert result["termination"] == "unknown"
        assert _result_supports_claim(
            store, store.read_case(case_id), result, result_refs[0], "feasibility", {}, run_id,
            claim_support="supported", claim_conditions=[],
        ) is False
        return

    assert business["status"] == "pass"
    assert result["has_checked_candidate"] is True
    assert result["solver_invoked"] is False and result["bound"] is None

    review_id = _core11_begin_note(core, case_id, candidate_id, [run_id])
    report_path = "reports/approved-answer.md"
    report_file = core["workspace"].joinpath(*report_path.split("/"))
    report_file.parent.mkdir(parents=True, exist_ok=True)
    old_report = b"Draft report that needs correction.\n"
    final_report = b"Final report reviewed for this claim.\n"
    report_file.write_bytes(old_report)
    state = store.read_case(case_id)
    candidate_record = store.read_record(f".reaslab/optagent/cases/{case_id}/candidates/{candidate_id}.json")
    issue_reply = core["call"]("optagent.review", {
        "op": "submit", "case_id": case_id, "review_id": review_id,
        "expected_review_revision": 2, "record_kind": "issue",
        "claim": "The draft report needs correction before approval.",
        "locations": [{"source_id": state["source_ids"][0], "locator": "source-level"}],
        "candidate_locations": [model_path],
        "evidence_paths": [report_path, model_path],
    }, role="opt-verifier")
    assert issue_reply["ok"], issue_reply
    issue_snapshot = strict_json_loads(store.read_ref(issue_reply["result"]["issue_ref"]))
    old_report_ref, candidate_model_ref = issue_snapshot["evidence_refs"]
    assert old_report_ref["path"].startswith(
        f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/issue-evidence/"
    )
    assert old_report_ref["path"].endswith(f"/{report_path}")
    assert store.read_ref(old_report_ref) == old_report
    assert candidate_model_ref["path"].startswith(
        f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/issue-evidence/"
    )
    assert candidate_model_ref["path"].endswith(f"/{model_path}")
    assert store.read_ref(candidate_model_ref) == model_source.encode("utf-8")
    assert candidate_model_ref["sha256"] == candidate_record["model"]["sha256"]

    needs_changes = core["call"]("optagent.review", {
        "op": "submit", "case_id": case_id, "review_id": review_id,
        "expected_review_revision": 3, "record_kind": "outcome", "decision": "needs_changes",
        "claims": [], "feedback": ["Correct the draft report."], "remaining": [], "report_path": None,
    }, role="opt-verifier")
    assert needs_changes["ok"], needs_changes
    report_file.write_bytes(final_report)
    closed = core["call"]("optagent.review", {
        "op": "submit", "case_id": case_id, "review_id": review_id,
        "expected_review_revision": 4, "record_kind": "outcome", "decision": "ready",
        "claims": [{
            "kind": "feasibility", "scope": "The direct witness passes the recorded business checker.",
            "conditions": [], "evidence_paths": [result_refs[0]["path"]], "support": "supported",
        }],
        "feedback": [], "remaining": [], "report_path": report_path,
    }, role="opt-verifier")
    assert closed["ok"], closed
    approved_answer_ref = closed["result"]["outcome"]["answer_ref"]
    state = store.read_case(case_id)
    selected = core["call"]("optagent.deliver", {
        "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "run", "candidate_id": candidate_id,
        "review_id": review_id, "proof_ids": [], "run_ids": [run_id],
    })
    assert selected["ok"], selected
    editable = selected["result"]["editable_case"]
    assert editable["status"] == "generated" and editable["solution_status"] == "complete"
    case_root = core["workspace"].joinpath(*editable["artifact_dir"].split("/"))
    exported_requirements = (case_root / "requirements.txt").read_bytes()
    assert exported_requirements.startswith(frozen_requirements)
    readme = (case_root / "README.md").read_text(encoding="utf-8")
    assert "direct_python adapter 不添加接口包" in readme
    assert "LP 格式" not in readme
    assert "不调用 `solve`" in readme and "不生成 ModelCheck" in readme
    selected_solution = json.loads((case_root / "results" / "selected" / "solution.json").read_text(encoding="utf-8"))
    assert selected_solution["results"][0]["model_check"] is None
    assert selected_solution["results"][0]["business_check"]["status"] == "pass"

    state = store.read_case(case_id)
    packed = core["call"]("optagent.deliver", {
        "op": "package", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "delivery_id": selected["result"]["delivery"]["id"],
        "target_path": "exports/direct-e2e.zip", "include_raw_sources": True,
    })
    assert packed["ok"], packed
    package_root = tmp_path / "direct-e2e-package"
    package_root.mkdir()
    with zipfile.ZipFile(BytesIO(store.read_ref(packed["result"]["package_ref"]))) as archive:
        package_manifest = strict_json_loads(archive.read("manifest.json"))
        package_readme = archive.read("README.md").decode("utf-8")
        reproduction_requirements = archive.read("reproduce/requirements.txt").decode("utf-8")
        frozen_package_requirements = archive.read(
            f"reproduce/prepared/{prepared_id}/requirements.txt"
        )
        package_names = set(archive.namelist())
        selected_review = next(row for row in package_manifest["reviews"] if row["review_id"] == review_id)
        assert selected_review["answer_ref"] == approved_answer_ref
        answer_mapping = next(row for row in package_manifest["ref_map"] if row["ref"] == approved_answer_ref)
        answer_link_line = next(
            line for line in package_readme.splitlines() if line.startswith("- [Approved deliverable](")
        )
        encoded_answer_link_path = answer_link_line.split("](", 1)[1].rsplit(")", 1)[0]
        answer_link_path = unquote(encoded_answer_link_path)
        assert answer_link_path == answer_mapping["package_path"]
        assert answer_link_path.startswith("evidence/approved-answers/")
        assert archive.read(answer_link_path) == store.read_ref(approved_answer_ref)
        archive.extractall(package_root)
    assert "reproduce/run.py" in package_names
    assert package_manifest["reproductions"] == [{
        "prepared_manifest_ref": prepared["input_manifest"], "evidence_run_ids": [run_id],
    }]
    with zipfile.ZipFile(BytesIO(store.read_ref(packed["result"]["package_ref"]))) as archive:
        assert archive.read(f"evidence/attachments/{old_report_ref['sha256']}.md") == old_report
        assert archive.read(f"evidence/attachments/{candidate_model_ref['sha256']}.py") == model_source.encode("utf-8")
        assert archive.read(f"evidence/approved-answers/{approved_answer_ref['sha256']}.md") == final_report
    assert frozen_package_requirements == frozen_requirements
    import importlib.metadata
    assert f"numpy=={importlib.metadata.version('numpy')}" in reproduction_requirements

    reproduction_output = tmp_path / "direct-e2e-reproduction"
    reproduction = subprocess.run(
        [sys.executable, "-I", str(package_root / "reproduce" / "run.py"), "--output-dir", str(reproduction_output)],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )
    assert reproduction.returncode == 0, reproduction.stderr + reproduction.stdout
    reproduction_summary = json.loads(reproduction.stdout)
    assert reproduction_summary["kind"] == "local-reproductions"
    assert len(reproduction_summary["entries"]) == 1
    reproduction_report = reproduction_summary["entries"][0]["report"]["report"]
    assert reproduction_report["kind"] == "local-reproduction"
    assert reproduction_report["solver_invoked"] is False
    reproduced_results = list(reproduction_output.rglob("result.json"))
    assert len(reproduced_results) == 1
    reproduced_result = json.loads(reproduced_results[0].read_text(encoding="utf-8"))
    assert reproduced_result["objective"] == {
        "component": "cost", "sense": "min", "unit": "USD", "value": 6.0,
    }
    reproduced_business_checks = list(reproduction_output.rglob("business-check.json"))
    assert len(reproduced_business_checks) == 1
    reproduced_business = json.loads(reproduced_business_checks[0].read_text(encoding="utf-8"))
    assert reproduced_business["status"] == "pass"
    assert reproduced_business["solution"] == {"quantity": 3}

    report_file.write_bytes(b"Report changed after approval.\n")
    state = store.read_case(case_id)
    stale_package = core["call"]("optagent.deliver", {
        "op": "package", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "delivery_id": selected["result"]["delivery"]["id"],
        "target_path": "exports/direct-e2e-edited.zip", "include_raw_sources": True,
    })
    assert not stale_package["ok"] and stale_package["error"]["code"] == "artifact_changed", stale_package
    assert not (core["workspace"] / "exports" / "direct-e2e-edited.zip").exists()

    solved = subprocess.run(
        [sys.executable, "-I", str(case_root / "run.py"), "solve"],
        cwd=case_root, capture_output=True, text=True, timeout=60,
    )
    assert solved.returncode == 0, solved.stderr + solved.stdout
    local_summary = json.loads(solved.stdout)
    assert local_summary["operation"] == "solve" and local_summary["status"] == "completed"
    (case_root / "code" / "optimization_model.py").write_text(
        "def solve(bundle):\n    raise AssertionError('check must not call solve')\n", encoding="utf-8"
    )
    checked = subprocess.run(
        [sys.executable, "-I", str(case_root / "run.py"), "check"],
        cwd=case_root, capture_output=True, text=True, timeout=60,
    )
    assert checked.returncode == 0, checked.stderr + checked.stdout
    check_summary = json.loads(checked.stdout)
    assert check_summary["operation"] == "check" and check_summary["checks_passed"] is True
    check_report = json.loads((case_root / "results" / "check" / ".optagent" / "check.json").read_text(encoding="utf-8"))
    assert check_report["scope"].startswith("saved direct_python point")
    assert check_report["results"][0]["current_business_check"]["solution"] == {"quantity": 3}


def test_diagnostic_selection_requires_the_reviewed_run_anchor(core):
    case_id, _, _ = core["create_case"](raw=b"Only the actual dispatch observations are reviewed.")
    store = core["store"]()
    candidate_id = _core11_register_candidate(core, case_id)
    run_id, _ = _core11_start_run(core, case_id, candidate_id)
    run_ref, response_ref, status_ref = _core11_add_observations(core, case_id, run_id)
    review_id = _core11_begin_note(core, case_id, candidate_id, [run_id])
    full_claim = {
        "kind": "diagnostic", "scope": "The run and both linked observations remain unknown.",
        "conditions": [], "evidence_refs": [run_ref, response_ref, status_ref], "support": "supported",
    }
    candidate_ref = core["store"]().ref_for_path(
        f".reaslab/optagent/cases/{case_id}/candidates/{candidate_id}.json"
    )
    wrong_route = {**full_claim, "evidence_refs": [candidate_ref]}
    rejected_wrong_route = _core11_close(core, case_id, review_id, [wrong_route])
    assert not rejected_wrong_route["ok"]
    assert rejected_wrong_route["error"]["details"]["expected_anchor_types"] == [
        "linked_run_observation",
        "matching_formal_proof",
    ]
    closed = _core11_close(core, case_id, review_id, [full_claim])
    assert closed["ok"], closed

    state = core["store"]().read_case(case_id)
    omitted_run = core["call"]("optagent.deliver", {
        "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "run", "candidate_id": candidate_id,
        "review_id": review_id, "proof_ids": [], "run_ids": [],
    })
    assert not omitted_run["ok"] and omitted_run["error"]["code"] == "evidence_insufficient"

    state = core["store"]().read_case(case_id)
    selected = core["call"]("optagent.deliver", {
        "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "run", "candidate_id": candidate_id,
        "review_id": review_id, "proof_ids": [], "run_ids": [run_id],
    })
    assert selected["ok"], selected
    editable = selected["result"]["editable_case"]
    assert editable["status"] == "generated"
    assert editable["solution_status"] == "no_machine_point"
    artifact_dir = store.read_case(case_id)["artifact_dir"]
    solution = json.loads(
        (core["workspace"].joinpath(*artifact_dir.split("/")) / "results" / "selected" / "solution.json")
        .read_text(encoding="utf-8")
    )
    assert solution["status"] == "no_machine_point"
    assert solution["results"] == []
    delivery = core["store"]().read_record(
        f".reaslab/optagent/cases/{case_id}/deliveries/{selected['result']['delivery']['id']}/delivery.json"
    )
    review, _ = core["store"]().read_family_record(case_id, "reviews", review_id)
    assert delivery["claims"] == review["conclusion"]["claims"] == [full_claim]
    assert delivery["reports"] == [review["answer_ref"]]
    assert review["answer_ref"] in delivery["evidence_refs"]
    assert all(ref in delivery["evidence_refs"] for ref in full_claim["evidence_refs"])


def test_old_unknown_run_is_readable_but_cannot_be_selected_under_new_candidate(core):
    case_id, _, _ = core["create_case"](raw=b"The current candidate changes after an old unknown run.")
    candidate_a = _core11_register_candidate(core, case_id, "candidate-a.py")
    run_id, _ = _core11_start_run(core, case_id, candidate_a)
    run_ref, response_ref, _ = _core11_add_observations(core, case_id, run_id, include_status=False)
    candidate_b = _core11_register_candidate(core, case_id, "candidate-b.py")
    current = core["call"]("optagent.run", {
        "op": "inspect", "case_id": case_id, "run_id": run_id, "after_instance": 0, "limit": 20,
    }, role="opt-verifier")
    assert current["ok"] and current["result"]["run"]["dispatch_state"] == "unknown"
    _run, run_ref = read_run(core["store"](), case_id, run_id)
    review_id = _core11_begin_note(core, case_id, candidate_b, [run_id])
    historical = {
        "kind": "diagnostic", "scope": "The old run has no checked Result.",
        "conditions": ["Historical only; its old Prepared binding is not current."],
        "evidence_refs": [run_ref, response_ref], "support": "unresolved",
    }
    closed = _core11_close(core, case_id, review_id, [historical])
    assert closed["ok"], closed

    state = core["store"]().read_case(case_id)
    attempted = core["call"]("optagent.deliver", {
        "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "run", "candidate_id": candidate_b,
        "review_id": review_id, "proof_ids": [], "run_ids": [run_id],
    })
    assert not attempted["ok"] and attempted["error"]["code"] == "evidence_insufficient"


@pytest.mark.parametrize("origin", ["product_tool", "external_acp"])
def test_cancel_diagnostic_is_one_generation_only_and_new_run_blocks_old_delivery(core, origin):
    case_id, _, _ = core["create_case"](raw=b"A requested cancellation has no confirmed remote stop.")
    candidate_id = _core11_register_candidate(core, case_id)
    run_id, _ = _core11_start_run(core, case_id, candidate_id)
    store = core["store"]()
    if origin == "product_tool":
        state = store.read_case(case_id)
        cancelled = core["call"]("optagent.run", {
            "op": "cancel", "case_id": case_id, "run_id": run_id,
            "expected_state_revision": state["state_revision"],
            "reason": "Record the user's cancellation request.",
        })
        assert cancelled["ok"], cancelled
        assert any(item["code"] == "remote_cancel_unavailable" for item in cancelled["limitations"])
    else:
        event_id = str(uuid.uuid4())
        cancelled = core["control"]({
            "event_id": event_id,
            "kind": "external_cancel",
            "cancel": {
                "origin": "external_acp", "session_id": core["session_id"],
                "event_id": event_id, "turn_id": None, "call_id": None,
            },
            "bindings": [{"case_id": case_id, "run_id": run_id, "pending_start": False}],
        })
        assert cancelled["ok"], cancelled
    cancelled_run, run_ref = read_run(store, case_id, run_id)
    assert cancelled_run["cancel"]["requested"] is True
    assert cancelled_run["cancel"]["evidence"]["origin"] == origin

    review_id = _core11_begin_note(core, case_id, candidate_id, [run_id])
    claim = {
        "kind": "diagnostic",
        "scope": "Cancellation was requested; remote stop is not confirmed.",
        "conditions": ["No terminal or numerical outcome is asserted."],
        "evidence_refs": [run_ref], "support": "supported",
    }
    closed = _core11_close(core, case_id, review_id, [claim])
    assert closed["ok"], closed
    state = store.read_case(case_id)
    selected = core["call"]("optagent.deliver", {
        "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "run", "candidate_id": candidate_id,
        "review_id": review_id, "proof_ids": [], "run_ids": [run_id],
    })
    assert selected["ok"], selected
    delivery_id = selected["result"]["delivery"]["id"]

    state = store.read_case(case_id)
    built_before = core["call"]("optagent.deliver", {
        "op": "build", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "delivery_id": delivery_id, "target_path": f"reports/cancel-{origin}-before-new-run.md",
    })
    assert built_before["ok"], built_before
    state = store.read_case(case_id)
    package_before = core["call"]("optagent.deliver", {
        "op": "package", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "delivery_id": delivery_id, "target_path": f"exports/cancel-{origin}-before-new-run.zip",
        "include_raw_sources": True,
    })
    assert package_before["ok"], package_before
    with zipfile.ZipFile(BytesIO(store.read_ref(package_before["result"]["package_ref"]))) as archive:
        names = set(archive.namelist())
        manifest = strict_json_loads(archive.read("manifest.json"))
        packaged_run = strict_json_loads(archive.read(f"evidence/runs/{run_id}/record.json"))
    assert "reproduce/run.py" in names
    assert manifest["reproductions"][0]["evidence_run_ids"] == [run_id]
    assert manifest["claims"][0]["scope"] == claim["scope"]
    assert packaged_run["cancel"]["requested"] is True
    assert packaged_run["cancel"]["stop_attempt"] is None

    # A new run append at the same mathematical generation invalidates the old
    # cancellation-only observation; build and package must repeat the check.
    run2_id, _ = _core11_start_run(core, case_id, candidate_id, inspect_unknown=False)
    assert run2_id != run_id
    state = store.read_case(case_id)
    stale_build = core["call"]("optagent.deliver", {
        "op": "build", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "delivery_id": delivery_id, "target_path": f"reports/cancel-{origin}-after-new-run.md",
    })
    assert not stale_build["ok"] and stale_build["error"]["code"] == "evidence_insufficient"
    assert not (core["workspace"] / f"reports/cancel-{origin}-after-new-run.md").exists()
    state = store.read_case(case_id)
    stale_package = core["call"]("optagent.deliver", {
        "op": "package", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "delivery_id": delivery_id, "target_path": f"exports/cancel-{origin}-after-new-run.zip",
        "include_raw_sources": True,
    })
    assert not stale_package["ok"] and stale_package["error"]["code"] == "evidence_insufficient"
    assert not (core["workspace"] / f"exports/cancel-{origin}-after-new-run.zip").exists()


def test_without_dispatch_observation_can_be_selected_without_claiming_execution(core):
    case_id, _, _ = core["create_case"](raw=b"A canceled start did not reach the execution platform.")
    candidate_id = _core11_register_candidate(core, case_id)
    run_id, _ = _core11_start_run(core, case_id, candidate_id, inspect_unknown=False, cancel_before_dispatch=True)
    store = core["store"]()
    run, run_ref = read_run(store, case_id, run_id)
    assert run["dispatch_state"] == "without_dispatch"
    assert run["transport"]["error_code"] == "stale_revision"
    review_id = _core11_begin_note(core, case_id, candidate_id, [run_id])
    claim = {
        "kind": "diagnostic", "scope": "The start attempt was not dispatched.",
        "conditions": ["No execution or numerical result is asserted."],
        "evidence_refs": [run_ref], "support": "supported",
    }
    closed = _core11_close(core, case_id, review_id, [claim])
    assert closed["ok"], closed
    state = store.read_case(case_id)
    selected = core["call"]("optagent.deliver", {
        "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "run", "candidate_id": candidate_id,
        "review_id": review_id, "proof_ids": [], "run_ids": [run_id],
    })
    assert selected["ok"], selected
    editable = selected["result"]["editable_case"]
    assert editable["status"] == "generated"
    assert editable["solution_status"] == "no_machine_point"
    artifact_dir = store.read_case(case_id)["artifact_dir"]
    solution = json.loads(
        (core["workspace"].joinpath(*artifact_dir.split("/")) / "results" / "selected" / "solution.json")
        .read_text(encoding="utf-8")
    )
    assert solution["status"] == "no_machine_point"
    assert solution["results"] == []


def test_mixed_claims_on_one_run_preserve_numeric_editable_projection(core, tmp_path):
    business_solution = {"routes": [{"from": "A", "to": "B"}], "decision": "ship", "cost": 10.0}
    case_id, candidate_id, result_run_id, result_refs, store = _synthetic_reviewable_batch(
        core,
        model_inside_case_code=True,
        business_solution=business_solution,
        native_json_settings={"offset": 0.0, "solution": business_solution},
        model_source=(
            "from pyomo.environ import ConcreteModel, Objective, minimize\n"
            "from ....models.helper import VALUE\n"
            "def build_model(bundle):\n"
            "    offset = bundle.json('settings')['offset']\n"
            "    model = ConcreteModel()\n"
            "    model.cost = Objective(expr=VALUE + offset, sense=minimize)\n"
            "    return model\n"
            "\n"
            "def extract_solution(bundle, witness):\n"
            "    solution = dict(bundle.json('settings')['solution'])\n"
            "    solution['cost'] = witness['objective_values'][0]['value']\n"
            "    return solution\n"
        ),
        module_sources={"models/helper.py": "VALUE = 10.0\n"},
    )
    _, response_ref, _ = _core11_add_observations(
        core, case_id, result_run_id, include_status=False
    )
    review_id = _core11_begin_note(core, case_id, candidate_id, [result_run_id])
    numerical = {
        "kind": "feasibility", "scope": "The checked Result for site A is feasible.",
        "conditions": [], "evidence_refs": [result_refs[0]], "support": "supported",
    }
    observation = {
        "kind": "diagnostic", "scope": "The selected run has an unknown transport observation.",
        "conditions": ["The transport observation does not establish a numerical outcome."],
        "evidence_refs": [response_ref], "support": "supported",
    }
    closed = _core11_close(
        core, case_id, review_id, [numerical, observation], report_path="reports/final-review.md"
    )
    assert closed["ok"], closed
    state = store.read_case(case_id)
    selected = core["call"]("optagent.deliver", {
        "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "run", "candidate_id": candidate_id,
        "review_id": review_id, "proof_ids": [], "run_ids": [result_run_id],
    })
    assert selected["ok"], selected
    editable_case = selected["result"]["editable_case"]
    assert editable_case["status"] == "generated"
    assert selected["result"]["message"].startswith("Local editable case generated at ")
    assert editable_case["solution_status"] == "complete"
    artifact_dir = store.read_case(case_id)["artifact_dir"]
    assert editable_case["solution_path"] == f"{artifact_dir}/results/selected/solution.json"
    assert editable_case["inputs_path"] == f"{artifact_dir}/data/inputs.json"
    assert editable_case["inputs_path"] in selected["result"]["message"]
    export_manifest = store.read_record(f".reaslab/optagent/cases/{case_id}/editable-export.json")
    assert export_manifest["inputs_path"] == editable_case["inputs_path"]
    assert editable_case["run_config_path"] == f"{artifact_dir}/.optagent/run.json"
    case_root = core["workspace"].joinpath(*artifact_dir.split("/"))
    assert (case_root / "code" / "model.py").is_file()
    assert not (case_root / "code" / "code" / "model.py").exists()
    assert not (case_root / "model.py").exists()
    assert (case_root / "code" / "model.py").read_text(encoding="utf-8") == (
        "from pyomo.environ import ConcreteModel, Objective, minimize\n"
        "from ....models.helper import VALUE\n"
        "def build_model(bundle):\n"
        "    offset = bundle.json('settings')['offset']\n"
        "    model = ConcreteModel()\n"
        "    model.cost = Objective(expr=VALUE + offset, sense=minimize)\n"
        "    return model\n"
        "\n"
        "def extract_solution(bundle, witness):\n"
        "    solution = dict(bundle.json('settings')['solution'])\n"
        "    solution['cost'] = witness['objective_values'][0]['value']\n"
        "    return solution\n"
    )
    assert (case_root / "code" / "external" / "models" / "helper.py").is_file()
    assert (case_root / "run.py").is_file()
    assert (case_root / "requirements.txt").is_file()
    runtime_zip = case_root / ".optagent" / "runtime.zip"
    assert runtime_zip.is_file()
    assert not (case_root / "support").exists()
    with zipfile.ZipFile(runtime_zip) as runtime:
        assert set(runtime.namelist()) == {
            "optagent_core/__init__.py",
            "optagent_core/contracts.py",
            "optagent_core/data.py",
            "optagent_core/models.py",
            "optagent_core/results.py",
            "optagent_core/solver_protocol.py",
            "optagent_core/worker.py",
            "optagent_core/case_execution.py",
        }
    run_config_raw = (case_root / ".optagent" / "run.json").read_bytes()
    inputs_raw = (case_root / "data" / "inputs.json").read_bytes()
    assert run_config_raw.endswith(b"\n") and b"\n  \"" in run_config_raw
    assert inputs_raw.endswith(b"\n") and b"\n  \"" in inputs_raw
    run_config = json.loads(run_config_raw.decode("utf-8"))
    inputs = json.loads(inputs_raw.decode("utf-8"))
    solution = json.loads((case_root / "results" / "selected" / "solution.json").read_text(encoding="utf-8"))
    assert run_config["model_path"] == "code/model.py"
    assert run_config["code_import_paths"] == {
        "code/model.py": f"{artifact_dir}/code/model.py",
        "code/external/models/helper.py": "models/helper.py",
        "code/external/business_checker.py": "business_checker.py",
    }
    assert run_config["local_modules"] == ["code/external/models/helper.py"]
    assert run_config["inputs_path"] == "data/inputs.json"
    assert run_config["instance_parameter_object_id"] == "sites"
    assert inputs["objects"][0]["object_id"] == "sites"
    assert [item["object_id"] for item in inputs["objects"]] == ["sites", "settings"]
    assert inputs["objects"][1]["kind"] == "json"
    assert inputs["objects"][1]["values"] == [{"offset": 0.0, "solution": business_solution}]
    assert solution["schema_version"] == 2
    assert solution["kind"] == "selected-run-solution"
    result_value = json.loads(store.read_ref(result_refs[0]))
    solution_row = solution["results"][0]
    assert set(solution_row) == {"result", "witness", "model_check", "business_check"}
    assert solution_row["result"] == result_value
    assert solution_row["witness"] == json.loads(store.read_ref(result_value["witness"]))
    assert solution_row["model_check"] == json.loads(store.read_ref(result_value["check"]))
    run_record, _ = read_run(store, case_id, result_run_id)
    seal = json.loads(store.read_ref(run_record["worker"]["seal"]))
    business_path = result_refs[0]["path"].rsplit("/", 1)[0] + "/business-check.json"
    business_ref = next(item for item in seal["output_refs"] if item["path"] == business_path)
    assert solution_row["business_check"] == json.loads(store.read_ref(business_ref))
    assert solution_row["business_check"]["solution"] == business_solution
    readme = (case_root / "README.md").read_text(encoding="utf-8")
    assert "修改来源不会自动更新此文件" in readme
    assert "`data/inputs.json` 是求解与检查实际使用的数据" in readme
    selected_delivery_id = selected["result"]["delivery"]["id"]
    selected_delivery = store.read_record(
        f".reaslab/optagent/cases/{case_id}/deliveries/{selected_delivery_id}/delivery.json"
    )
    selected_report_path = selected_delivery["reports"][0]["path"]
    assert selected_report_path == "reports/final-review.md"
    case_relative_report_path = posixpath.relpath(selected_report_path, artifact_dir)
    assert f"案例目录相对路径：`{case_relative_report_path}`。" in readme
    assert "根目录的 `report.md`（如有）是本次初始交付的数学报告" not in readme
    assert "不重验历史分层锁定或全局最优性" in readme
    assert set(case_root.rglob("model.py")) == {case_root / "code" / "model.py"}
    assert f"模型代码位于 `code/model.py`" in readme
    assert "results/local/summary.md" in readme
    assert "results/check/summary.md" in readme
    assert "保留隐藏目录 `.optagent/`" in readme

    entry_run = subprocess.run(
        [sys.executable, str(case_root / "run.py"), "check"],
        cwd=case_root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert entry_run.returncode == 0, entry_run.stderr + entry_run.stdout
    entry_summary = json.loads(entry_run.stdout)
    assert entry_summary["operation"] == "check"
    assert entry_summary["status"] == "completed"
    assert entry_summary["checks_passed"] is True
    check_report = json.loads(
        (case_root / "results" / "check" / ".optagent" / "check.json").read_text(encoding="utf-8")
    )
    assert check_report["status"] == "completed"
    assert check_report["checks_passed"] is True
    assert check_report["results"][0]["current_business_check"]["solution"] == business_solution

    prepared = store.read_record(
        f".reaslab/optagent/cases/{case_id}/prepared/{run_record['prepared_id']}/prepared.json"
    )
    prepared_manifest = json.loads(store.read_ref(prepared["input_manifest"]))
    owner_model = prepared_manifest["candidate"]["model"]
    owner_model_path = owner_model["original_path"]
    owner_model_ref = dict(owner_model["bytes"])
    assert owner_model_path == f"{artifact_dir}/code/model.py"
    owner_model_raw = store.read_ref(owner_model_ref)

    def assert_owner_model_unchanged():
        current_ref = store.ref_for_path(owner_model_path)
        assert current_ref["sha256"] == owner_model_ref["sha256"]
        assert current_ref["byte_len"] == owner_model_ref["byte_len"]
        assert current_ref["media_type"] == owner_model_ref["media_type"]
        assert store.read_ref(current_ref) == owner_model_raw

    assert_owner_model_unchanged()
    state = store.read_case(case_id)
    repeated_selection = core["call"]("optagent.deliver", {
        "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "run", "candidate_id": candidate_id,
        "review_id": review_id, "proof_ids": [], "run_ids": [result_run_id],
    })
    assert repeated_selection["ok"], repeated_selection
    assert repeated_selection["result"]["editable_case"]["status"] == "generated"
    assert (case_root / "code" / "model.py").read_text(encoding="utf-8").startswith(
        "from pyomo.environ import ConcreteModel"
    )
    assert not (case_root / "code" / "code").exists()
    repeated_config = json.loads((case_root / ".optagent" / "run.json").read_text(encoding="utf-8"))
    assert repeated_config["model_path"] == "code/model.py"
    assert_owner_model_unchanged()
    selected = repeated_selection

    state = store.read_case(case_id)
    packed = core["call"]("optagent.deliver", {
        "op": "package", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "delivery_id": selected["result"]["delivery"]["id"],
        "target_path": "exports/mixed-result-and-observation.zip", "include_raw_sources": True,
    })
    assert packed["ok"], packed
    package_raw = store.read_ref(packed["result"]["package_ref"])
    with zipfile.ZipFile(BytesIO(package_raw)) as archive:
        names = set(archive.namelist())
        manifest = strict_json_loads(archive.read("manifest.json"))
        requirements = archive.read("reproduce/requirements.txt").decode("utf-8")
        selected_run_record = strict_json_loads(
            archive.read(f"evidence/runs/{result_run_id}/record.json")
        )
        package_root = tmp_path / "selected-json-package"
        package_root.mkdir()
        archive.extractall(package_root)
    assert selected_run_record["run_id"] == result_run_id
    assert f"evidence/runs/{result_run_id}/prepared-input-manifest.json" in names
    assert len(manifest["reproductions"]) == 1
    assert manifest["reproductions"][0]["evidence_run_ids"] == [result_run_id]
    assert "reproduce/run.py" in names
    assert "pyomo==6.9.4" in requirements
    assert "highspy==1.10.0" in requirements

    try:
        import pyomo.environ  # noqa: F401
        import highspy  # noqa: F401
    except ImportError:
        return
    reproduction_output = tmp_path / "selected-json-reproduction-output"
    reproduction = subprocess.run(
        [sys.executable, "-I", str(package_root / "reproduce" / "run.py"), "--output-dir", str(reproduction_output)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert reproduction.returncode == 0, reproduction.stderr + reproduction.stdout
    reproduction_summary = json.loads(reproduction.stdout)
    assert reproduction_summary["kind"] == "local-reproductions"
    assert len(reproduction_summary["entries"]) == 1
    standalone_report = reproduction_summary["entries"][0]["report"]["report"]
    assert standalone_report["kind"] == "local-reproduction"
    assert standalone_report["data_bundle"]["data_id"] is not None
    frozen_business_checks = list(reproduction_output.rglob("business-check.json"))
    assert len(frozen_business_checks) == 3
    frozen_business_check_values = [
        json.loads(path.read_text(encoding="utf-8")) for path in frozen_business_checks
    ]
    assert all(item["status"] == "pass" for item in frozen_business_check_values)
    assert all(item["solution"] == business_solution for item in frozen_business_check_values)


def test_candidate_reactivation_restores_sealed_numeric_run_for_delivery(core, tmp_path):
    business_solution = {"site": "A", "cost": 10.0}
    case_id, candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(
        core,
        model_inside_case_code=True,
        model_source=(
            "from pyomo.environ import ConcreteModel, Objective, minimize\n"
            "def build_model(bundle):\n"
            "    model = ConcreteModel()\n"
            "    model.cost = Objective(expr=10.0 + bundle.json('settings')['offset'], sense=minimize)\n"
            "    return model\n"
            "\n"
            "def extract_solution(bundle, witness):\n"
            "    return dict(bundle.json('settings')['solution'])\n"
        ),
        business_solution=business_solution,
        native_json_settings={"offset": 0.0, "solution": business_solution},
        result_specs_override=[(["A"], 10.0, "optimal", 0.0)],
        complete_batch=True,
    )
    original = store.read_record(f".reaslab/optagent/cases/{case_id}/candidates/{candidate_id}.json")
    original_model_path = original["model"]["path"]
    original_model_bytes, _ = store.read_workspace_file(original_model_path)
    alternative_path = "candidate-revision-alternative.py"
    (core["workspace"] / alternative_path).write_bytes(original_model_bytes + b"\n# alternate binding\n")

    def register_model(model_path):
        state = store.read_case(case_id)
        return core["call"]("optagent.case", {
            "op": "revise", "case_id": case_id, "expected_state_revision": state["state_revision"],
            "kind": "candidate", "model_path": model_path,
            "local_module_paths": [item["path"] for item in original["local_modules"]],
            "data_id": original["data_id"],
            "instance_parameter_object_id": original["instance_parameter_object_id"],
            "backend_id": original["backend_id"], "objective_plan": original["objective_plan"],
            "business_checker_path": original["business_checker"]["path"],
            "input_closure": original["input_closure"], "closure_note": original["closure_note"],
        })

    changed = register_model(alternative_path)
    assert changed["ok"], changed
    assert changed["result"]["candidate"]["id"] != candidate_id
    restored = register_model(original_model_path)
    assert restored["ok"], restored
    assert restored["result"]["candidate"]["id"] == candidate_id
    assert restored["result"]["candidate"]["candidate_revision"] == original["candidate_revision"]
    state = store.read_case(case_id)
    assert state["candidate_id"] == candidate_id
    assert state["candidate_revision"] == original["candidate_revision"]

    review_id = _core11_begin_note(core, case_id, candidate_id, [run_id])
    closed = _core11_close(core, case_id, review_id, [{
        "kind": "feasibility",
        "scope": "The sealed checked Result provides a feasible point for this instance.",
        "conditions": [], "evidence_refs": [result_refs[0]], "support": "supported",
    }])
    assert closed["ok"], closed
    state = store.read_case(case_id)
    selected = core["call"]("optagent.deliver", {
        "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "run", "candidate_id": candidate_id,
        "review_id": review_id, "proof_ids": [], "run_ids": [run_id],
    })
    assert selected["ok"], selected
    editable = selected["result"]["editable_case"]
    assert editable["status"] == "generated"
    assert editable["solution_status"] == "complete"

    artifact_dir = store.read_case(case_id)["artifact_dir"]
    case_root = core["workspace"].joinpath(*artifact_dir.split("/"))
    solution = json.loads((case_root / "results" / "selected" / "solution.json").read_text(encoding="utf-8"))
    assert solution["results"][0]["business_check"]["solution"] == business_solution

    state = store.read_case(case_id)
    package = core["call"]("optagent.deliver", {
        "op": "package", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "delivery_id": selected["result"]["delivery"]["id"],
        "target_path": "exports/reactivated-numeric-run.zip", "include_raw_sources": True,
    })
    assert package["ok"], package
    with zipfile.ZipFile(BytesIO(store.read_ref(package["result"]["package_ref"]))) as archive:
        names = set(archive.namelist())
        assert "reproduce/run.py" in names
        assert f"evidence/runs/{run_id}/record.json" in names

    checked = subprocess.run(
        [sys.executable, str(case_root / "run.py"), "check"],
        cwd=case_root, capture_output=True, text=True, timeout=60,
    )
    assert checked.returncode == 0, checked.stderr + checked.stdout
    assert json.loads(checked.stdout)["checks_passed"] is True
    check_report = json.loads((case_root / "results" / "check" / ".optagent" / "check.json").read_text(encoding="utf-8"))
    assert check_report["checks_passed"] is True
    assert check_report["results"][0]["current_business_check"]["solution"] == business_solution


def test_diagnostic_run_without_result_exports_current_prepared_input(core):
    case_id, _, _ = core["create_case"](raw=b"A selected diagnostic run has no numerical Result.")
    store = core["store"]()
    input_dir = core["workspace"] / "inputs"
    input_dir.mkdir(exist_ok=True)
    (input_dir / "sites.csv").write_bytes(b"site,cost\nA,10.5\nB,5\n")
    state = store.read_case(case_id)
    captured = core["call"]("optagent.data", {
        "op": "capture", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "path": "inputs/sites.csv", "purpose": "data", "locations": ["rows A and B"],
    })
    assert captured["ok"], captured
    state = store.read_case(case_id)
    normalized = core["call"]("optagent.data", {
        "op": "normalize", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "source_ids": [captured["result"]["source"]["id"]],
        "plan": _csv_data_plan(captured["result"]["source"]["id"]),
    })
    assert normalized["ok"], normalized
    data_id = normalized["result"]["data_manifest"]["data_id"]
    candidate_id = _core11_register_candidate(
        core, case_id, data_id=data_id, instance_parameter_object_id="sites"
    )
    run_id, prepared_id = _core11_start_run(core, case_id, candidate_id)
    run_ref, _, _ = _core11_add_observations(core, case_id, run_id)
    review_id = _core11_begin_note(core, case_id, candidate_id, [run_id])
    closed = _core11_close(core, case_id, review_id, [{
        "kind": "diagnostic",
        "scope": "The selected Run is unresolved and has no numerical Result.",
        "conditions": ["No completion, feasible point, or solver status is asserted."],
        "evidence_refs": [run_ref],
        "support": "supported",
    }])
    assert closed["ok"], closed

    state = store.read_case(case_id)
    selected = core["call"]("optagent.deliver", {
        "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "run", "candidate_id": candidate_id,
        "review_id": review_id, "proof_ids": [], "run_ids": [run_id],
    })
    assert selected["ok"], selected
    editable = selected["result"]["editable_case"]
    assert editable["status"] == "generated"
    assert editable["solution_status"] == "no_machine_point"
    artifact_dir = store.read_case(case_id)["artifact_dir"]
    case_root = core["workspace"].joinpath(*artifact_dir.split("/"))
    solution = json.loads((case_root / "results" / "selected" / "solution.json").read_text(encoding="utf-8"))
    inputs = json.loads((case_root / "data" / "inputs.json").read_text(encoding="utf-8"))
    assert solution["status"] == "no_machine_point"
    assert solution["results"] == []
    prepared = store.read_record(
        f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/prepared.json"
    )
    prepared_manifest_ref = prepared["input_manifest"]
    prepared_manifest = strict_json_loads(store.read_ref(prepared_manifest_ref))
    data_manifest = strict_json_loads(store.read_ref(prepared_manifest["data"]["manifest"]))
    frozen_objects = []
    for item in data_manifest["objects"]:
        frozen_objects.append({
            "object_id": item["object_id"],
            "kind": item["kind"],
            "key_fields": item["key_fields"],
            "columns": item["columns"],
            "values": strict_json_loads(store.read_ref(item["values"])),
        })
    assert inputs == {
        "schema_version": 1,
        "instance_parameter_object_id": "sites",
        "objects": frozen_objects,
    }
    run_record, _ = store.read_family_record(case_id, "runs", run_id)
    assert run_record["dispatch_state"] == "unknown"
    assert run_record["results"] == []
    readme = (case_root / "README.md").read_text(encoding="utf-8")
    assert "本次交付未选择可在此展示的数值结果" in readme
    assert "不是 Proof witness" in readme
    assert "所选 Run 未同时记录可观察的托管 Python 实现和版本" in readme
    requirements = (case_root / "requirements.txt").read_text(encoding="utf-8")
    assert "highspy  # version not recorded for the selected solver interface" in requirements

    state = store.read_case(case_id)
    repeated = core["call"]("optagent.deliver", {
        "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "run", "candidate_id": candidate_id,
        "review_id": review_id, "proof_ids": [], "run_ids": [run_id],
    })
    assert repeated["ok"], repeated
    assert repeated["result"]["editable_case"]["status"] == "generated"

    state = store.read_case(case_id)
    packed = core["call"]("optagent.deliver", {
        "op": "package", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "delivery_id": repeated["result"]["delivery"]["id"],
        "target_path": "exports/diagnostic-without-result.zip", "include_raw_sources": True,
    })
    assert packed["ok"], packed
    with zipfile.ZipFile(BytesIO(store.read_ref(packed["result"]["package_ref"]))) as archive:
        names = set(archive.namelist())
        manifest = strict_json_loads(archive.read("manifest.json"))
        reproduction = manifest["reproductions"]
        reproduction_requirements = archive.read("reproduce/requirements.txt").decode("utf-8")
        portable_data_manifest = archive.read(
            f"reproduce/prepared/{prepared_id}/data/manifest.json"
        )
        portable_data_objects = {
            item["object_id"]: archive.read(
                f"reproduce/prepared/{prepared_id}/data/objects/"
                f"{hashlib.sha256(item['object_id'].encode('utf-8')).hexdigest()}.json"
            )
            for item in data_manifest["objects"]
        }
    assert "reproduce/run.py" in names
    assert len(reproduction) == 1
    assert reproduction[0]["evidence_run_ids"] == [run_id]
    assert reproduction[0]["prepared_manifest_ref"] == prepared_manifest_ref
    assert portable_data_manifest == store.read_ref(prepared_manifest["data"]["manifest"])
    assert portable_data_objects == {
        item["object_id"]: store.read_ref(item["values"])
        for item in data_manifest["objects"]
    }
    assert "pyomo  # version not recorded for the selected solver interface" in reproduction_requirements


def test_multiple_diagnostic_runs_keep_zip_inputs_but_not_one_editable_case(core):
    case_id, _, _ = core["create_case"](raw=b"Selected diagnostic runs pin separate Prepared inputs.")
    candidate_id = _core11_register_candidate(core, case_id)
    first_run_id, first_prepared_id = _core11_start_run(core, case_id, candidate_id)
    first_ref, _, _ = _core11_add_observations(core, case_id, first_run_id)
    second_run_id, second_prepared_id = _core11_start_run(core, case_id, candidate_id)
    second_ref, _, _ = _core11_add_observations(core, case_id, second_run_id)
    assert first_prepared_id != second_prepared_id
    review_id = _core11_begin_note(core, case_id, candidate_id, [first_run_id, second_run_id])
    closed = _core11_close(core, case_id, review_id, [
        {
            "kind": "diagnostic", "scope": "The first selected Run is unresolved.",
            "conditions": ["No numerical Result is asserted."], "evidence_refs": [first_ref], "support": "supported",
        },
        {
            "kind": "diagnostic", "scope": "The second selected Run is unresolved.",
            "conditions": ["No numerical Result is asserted."], "evidence_refs": [second_ref], "support": "supported",
        },
    ])
    assert closed["ok"], closed

    store = core["store"]()
    state = store.read_case(case_id)
    selected = core["call"]("optagent.deliver", {
        "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "run", "candidate_id": candidate_id,
        "review_id": review_id, "proof_ids": [], "run_ids": [first_run_id, second_run_id],
    })
    assert selected["ok"], selected
    assert selected["result"]["editable_case"]["status"] == "not_generated"
    assert selected["result"]["editable_case"]["reason"] == "multiple_prepared_inputs_selected"

    state = store.read_case(case_id)
    packed = core["call"]("optagent.deliver", {
        "op": "package", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "delivery_id": selected["result"]["delivery"]["id"],
        "target_path": "exports/two-diagnostic-inputs.zip", "include_raw_sources": True,
    })
    assert packed["ok"], packed
    with zipfile.ZipFile(BytesIO(store.read_ref(packed["result"]["package_ref"]))) as archive:
        names = set(archive.namelist())
        manifest = strict_json_loads(archive.read("manifest.json"))
    assert "reproduce/run.py" in names
    assert {tuple(row["evidence_run_ids"]) for row in manifest["reproductions"]} == {
        (first_run_id,), (second_run_id,),
    }


def test_editable_projection_retires_only_confirmed_core_files_and_preserves_owner_requirements(core):
    from optagent_core.delivery import (
        _editable_case_projection_plan,
        _editable_export_manifest_path,
        _preflight_editable_case_projection,
        _publish_editable_case_projection,
    )

    case_id, candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(
        core,
        model_inside_artifact_dir=True,
    )
    review_id = _core11_begin_note(core, case_id, candidate_id, [run_id])
    feasibility = {
        "kind": "feasibility",
        "scope": "The checked fixture Result is feasible.",
        "conditions": [],
        "evidence_refs": [result_refs[0]],
        "support": "supported",
    }
    closed = _core11_close(core, case_id, review_id, [feasibility], report_path="reports/final-review.md")
    assert closed["ok"], closed
    state = store.read_case(case_id)
    selected = core["call"]("optagent.deliver", {
        "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "run", "candidate_id": candidate_id,
        "review_id": review_id, "proof_ids": [], "run_ids": [run_id],
    })
    assert selected["ok"], selected
    delivery_id = selected["result"]["delivery"]["id"]
    artifact_dir = store.read_case(case_id)["artifact_dir"]
    case_root = core["workspace"].joinpath(*artifact_dir.split("/"))
    owner_hidden_note = case_root / ".optagent" / "owner-note.md"
    owner_hidden_note.parent.mkdir(parents=True, exist_ok=True)
    owner_hidden_note.write_text("Owner note: keep this file.\n", encoding="utf-8")
    delivery = store.read_record(
        f".reaslab/optagent/cases/{case_id}/deliveries/{delivery_id}/delivery.json"
    )
    manifest_path = _editable_export_manifest_path(case_id)
    manifest = store.read_record(manifest_path)
    owner_report = case_root / "report.md"
    owner_report.write_text("# Owner report\nKeep these authored bytes.\n", encoding="utf-8")

    # Simulate paths left in the old accumulated export record: one Core output
    # and the Owner's original model path from the prior projection contract.
    old_solution_path = f"{artifact_dir}/solution.json"
    old_solution_raw = b'{"legacy_selected_solution":true}\n'
    (case_root / "solution.json").write_bytes(old_solution_raw)
    manifest["files"][old_solution_path] = store.ref_for_path(old_solution_path)
    owner_model_path = f"{artifact_dir}/model.py"
    owner_model_bytes = (case_root / "model.py").read_bytes()
    manifest["files"][owner_model_path] = store.ref_for_path(owner_model_path)
    store.write_record(manifest_path, manifest, replace=True)

    # A prior exact Core-owned requirements file may be regenerated.
    requirements_path = f"{artifact_dir}/requirements.txt"
    plan = _editable_case_projection_plan(store, store.read_case(case_id), delivery)
    assert plan["status"] == "planned"
    replacement_requirements = b"# updated observed interface list\n"
    plan["files"][requirements_path] = replacement_requirements
    preflight = _preflight_editable_case_projection(store, case_id=case_id, plan=plan)
    assert preflight["status"] == "planned"
    state = store.read_case(case_id)
    published = _publish_editable_case_projection(
        store,
        case_id=case_id,
        owner_session_id=state["owner_session_id"],
        expected_selected_delivery_id=delivery_id,
        plan=preflight,
    )
    assert published["status"] == "generated", published
    assert (case_root / "requirements.txt").read_bytes() == replacement_requirements
    assert owner_hidden_note.read_text(encoding="utf-8") == "Owner note: keep this file.\n"
    assert not (case_root / "solution.json").exists()
    assert (case_root / "model.py").read_bytes() == owner_model_bytes
    assert owner_report.read_text(encoding="utf-8") == "# Owner report\nKeep these authored bytes.\n"
    assert f"{artifact_dir}/report.md" not in store.read_record(manifest_path)["files"]
    readme = (case_root / "README.md").read_text(encoding="utf-8")
    selected_report_path = delivery["reports"][0]["path"]
    assert selected_report_path == "reports/final-review.md"
    case_relative_report_path = posixpath.relpath(selected_report_path, artifact_dir)
    assert f"案例目录相对路径：`{case_relative_report_path}`。" in readme
    assert old_solution_path in published["retired_paths"]
    assert owner_model_path in published["relinquished_paths"]

    readme_path = f"{artifact_dir}/README.md"
    owner_readme = b"# Owner instructions\nKeep these authored bytes.\n"
    (case_root / "README.md").write_bytes(owner_readme)
    plan = _editable_case_projection_plan(store, store.read_case(case_id), delivery)
    preflight = _preflight_editable_case_projection(store, case_id=case_id, plan=plan)
    assert preflight["status"] == "planned", preflight
    assert preflight["readme_preserved"] is True
    state = store.read_case(case_id)
    published = _publish_editable_case_projection(
        store,
        case_id=case_id,
        owner_session_id=state["owner_session_id"],
        expected_selected_delivery_id=delivery_id,
        plan=preflight,
    )
    assert published["status"] == "generated", published
    assert published["readme_preserved"] is True
    assert (case_root / "README.md").read_bytes() == owner_readme
    assert readme_path in published["relinquished_paths"]
    assert readme_path not in store.read_record(manifest_path)["files"]

    # Even bytes identical to the suggested default do not establish Core
    # ownership when the export manifest has no confirmed README Ref.
    suggested_readme = plan["files"][readme_path]
    (case_root / "README.md").write_bytes(suggested_readme)
    plan = _editable_case_projection_plan(store, store.read_case(case_id), delivery)
    preflight = _preflight_editable_case_projection(store, case_id=case_id, plan=plan)
    assert preflight["status"] == "planned", preflight
    assert preflight["readme_preserved"] is True
    state = store.read_case(case_id)
    published = _publish_editable_case_projection(
        store,
        case_id=case_id,
        owner_session_id=state["owner_session_id"],
        expected_selected_delivery_id=delivery_id,
        plan=preflight,
    )
    assert published["status"] == "generated", published
    assert published["readme_preserved"] is True
    assert (case_root / "README.md").read_bytes() == suggested_readme
    assert readme_path not in store.read_record(manifest_path)["files"]

    # An unowned file is preserved even when its bytes equal the generated
    # interface suggestion; the export manifest must not claim it.
    plan = _editable_case_projection_plan(store, store.read_case(case_id), delivery)
    suggested_requirements = plan["files"][requirements_path]
    (case_root / "requirements.txt").write_bytes(suggested_requirements)
    manifest = store.read_record(manifest_path)
    manifest["files"].pop(requirements_path)
    store.write_record(manifest_path, manifest, replace=True)
    preflight = _preflight_editable_case_projection(store, case_id=case_id, plan=plan)
    assert preflight["status"] == "planned", preflight
    state = store.read_case(case_id)
    published = _publish_editable_case_projection(
        store,
        case_id=case_id,
        owner_session_id=state["owner_session_id"],
        expected_selected_delivery_id=delivery_id,
        plan=preflight,
    )
    assert published["status"] == "generated", published
    assert published["requirements_preserved"] is True
    assert (case_root / "requirements.txt").read_bytes() == suggested_requirements
    assert requirements_path not in store.read_record(manifest_path)["files"]

    # A later Owner edit remains byte-for-byte and is relinquished as well.
    owner_requirements = b"# Owner dependencies\ncustom-package>=1\n"
    (case_root / "requirements.txt").write_bytes(owner_requirements)
    plan = _editable_case_projection_plan(store, store.read_case(case_id), delivery)
    preflight = _preflight_editable_case_projection(store, case_id=case_id, plan=plan)
    assert preflight["status"] == "planned", preflight
    state = store.read_case(case_id)
    published = _publish_editable_case_projection(
        store,
        case_id=case_id,
        owner_session_id=state["owner_session_id"],
        expected_selected_delivery_id=delivery_id,
        plan=preflight,
    )
    assert published["status"] == "generated", published
    assert published["requirements_preserved"] is True
    assert (case_root / "requirements.txt").read_bytes() == owner_requirements
    assert requirements_path not in store.read_record(manifest_path)["files"]

    # An exact old requirements Ref is not enough to prove Core ownership if
    # the previous Delivery/Prepared source chain cannot be resolved.
    manifest = store.read_record(manifest_path)
    manifest["delivery_id"] = "00000000-0000-4000-8000-000000000000"
    manifest["files"][requirements_path] = store.ref_for_path(requirements_path)
    store.write_record(manifest_path, manifest, replace=True)
    plan = _editable_case_projection_plan(store, store.read_case(case_id), delivery)
    plan["files"][requirements_path] = b"# Updated default interface versions\npyomo==6.9.7\n"
    preflight = _preflight_editable_case_projection(store, case_id=case_id, plan=plan)
    assert preflight["status"] == "planned", preflight
    assert preflight["requirements_preserved"] is True
    state = store.read_case(case_id)
    published = _publish_editable_case_projection(
        store,
        case_id=case_id,
        owner_session_id=state["owner_session_id"],
        expected_selected_delivery_id=delivery_id,
        plan=preflight,
    )
    assert published["status"] == "generated", published
    assert published["requirements_preserved"] is True
    assert (case_root / "requirements.txt").read_bytes() == owner_requirements
    assert requirements_path in published["relinquished_paths"]
    assert requirements_path not in store.read_record(manifest_path)["files"]

    # Without a resolvable prior Delivery/Run/Prepared chain, even a standard
    # legacy Core path may be a real user Source and must remain untouched.
    ambiguous_source_path = f"{artifact_dir}/inputs.json"
    ambiguous_source_bytes = b"Owner-owned bytes at a legacy standard path.\n"
    (case_root / "inputs.json").write_bytes(ambiguous_source_bytes)
    manifest = store.read_record(manifest_path)
    manifest["delivery_id"] = "00000000-0000-4000-8000-000000000000"
    manifest["files"][ambiguous_source_path] = store.ref_for_path(ambiguous_source_path)
    store.write_record(manifest_path, manifest, replace=True)
    plan = _editable_case_projection_plan(store, store.read_case(case_id), delivery)
    preflight = _preflight_editable_case_projection(store, case_id=case_id, plan=plan)
    assert preflight["status"] == "planned", preflight
    state = store.read_case(case_id)
    published = _publish_editable_case_projection(
        store,
        case_id=case_id,
        owner_session_id=state["owner_session_id"],
        expected_selected_delivery_id=delivery_id,
        plan=preflight,
    )
    assert published["status"] == "generated", published
    assert (case_root / "inputs.json").read_bytes() == ambiguous_source_bytes
    assert ambiguous_source_path not in published["retired_paths"]
    assert ambiguous_source_path in published["relinquished_paths"]
    assert ambiguous_source_path not in store.read_record(manifest_path)["files"]


@pytest.mark.parametrize("protection", ["source", "source_missing", "approved_report"])
def test_editable_projection_preserves_user_or_approved_readme_and_required_files(core, protection):
    from optagent_core.delivery import _editable_export_manifest_path

    owner_bytes = b"# Owner README\nThese bytes remain authoritative.\n"
    fixture_options = (
        {"requirement_source_path": "README.md", "requirement_source_bytes": owner_bytes}
        if protection in {"source", "source_missing"}
        else {}
    )
    case_id, candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(core, **fixture_options)
    artifact_dir = store.read_case(case_id)["artifact_dir"]
    readme_path = f"{artifact_dir}/README.md"
    review_id = _core11_begin_note(core, case_id, candidate_id, [run_id])
    report_path = readme_path if protection == "approved_report" else "reports/final-review.md"
    closed = _core11_close(
        core,
        case_id,
        review_id,
        [{
            "kind": "feasibility",
            "scope": "The checked fixture Result is feasible.",
            "conditions": [],
            "evidence_refs": [result_refs[0]],
            "support": "supported",
        }],
        report_path=report_path,
    )
    assert closed["ok"], closed
    if protection == "approved_report":
        owner_bytes = store.read_workspace_file(readme_path)[0]
    elif protection == "source_missing":
        case_root = core["workspace"].joinpath(*artifact_dir.split("/"))
        (case_root / "README.md").unlink()

    state = store.read_case(case_id)
    selected = core["call"]("optagent.deliver", {
        "op": "select",
        "case_id": case_id,
        "expected_state_revision": state["state_revision"],
        "kind": "run",
        "candidate_id": candidate_id,
        "review_id": review_id,
        "proof_ids": [],
        "run_ids": [run_id],
    })
    if protection == "source_missing":
        assert selected["ok"] is False, selected
        assert selected["error"]["code"] == "not_found"
        assert not (core["workspace"].joinpath(*artifact_dir.split("/")) / "README.md").exists()
        return
    assert selected["ok"], selected
    editable = selected["result"]["editable_case"]
    assert editable["status"] == "generated", editable
    assert editable["readme_preserved"] is True
    if protection == "approved_report":
        message = selected["result"]["message"]
        assert "The existing README was preserved" in message
        assert "`python run.py solve`" in message and "`python run.py check`" in message
        assert editable["inputs_path"] in message
        assert "update its" not in message

    case_root = core["workspace"].joinpath(*artifact_dir.split("/"))
    assert (case_root / "README.md").read_bytes() == owner_bytes
    manifest = store.read_record(_editable_export_manifest_path(case_id))
    assert readme_path not in manifest["files"]
    assert {
        "run.py",
        "requirements.txt",
        "code/external/model.py",
        "data/inputs.json",
        ".optagent/run.json",
        ".optagent/runtime.zip",
        "results/selected/solution.json",
    }.issubset(set(editable["files"]))


@pytest.mark.parametrize(
    "relative",
    [
        "run.py",
        "code/external/model.py",
        ".optagent/run.json",
        ".optagent/runtime.zip",
    ],
)
def test_editable_projection_still_rejects_required_file_conflicts_without_overwrite(core, relative):
    case_id, candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(core)
    review_id = _core11_begin_note(core, case_id, candidate_id, [run_id])
    closed = _core11_close(core, case_id, review_id, [{
        "kind": "feasibility",
        "scope": "The checked fixture Result is feasible.",
        "conditions": [],
        "evidence_refs": [result_refs[0]],
        "support": "supported",
    }])
    assert closed["ok"], closed

    state = store.read_case(case_id)
    artifact_dir = state["artifact_dir"]
    case_root = core["workspace"].joinpath(*artifact_dir.split("/"))
    target = case_root.joinpath(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    owner_bytes = b"Keep the conflicting runtime file unchanged.\n"
    target.write_bytes(owner_bytes)
    target_path = f"{artifact_dir}/{relative}"

    selected = core["call"]("optagent.deliver", {
        "op": "select",
        "case_id": case_id,
        "expected_state_revision": state["state_revision"],
        "kind": "run",
        "candidate_id": candidate_id,
        "review_id": review_id,
        "proof_ids": [],
        "run_ids": [run_id],
    })
    assert selected["ok"], selected
    editable = selected["result"]["editable_case"]
    assert editable["status"] == "not_generated"
    assert editable["reason"] == "existing_case_file_conflict"
    assert editable["details"]["path"] == target_path
    assert target.read_bytes() == owner_bytes


def test_editable_inputs_avoid_current_source_path(core):
    source_bytes = b"Owner source bytes must remain separate from normalized run inputs.\n"
    case_id, candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(
        core,
        requirement_source_path="data/inputs.json",
        requirement_source_bytes=source_bytes,
    )
    artifact_dir = store.read_case(case_id)["artifact_dir"]
    source_path = f"{artifact_dir}/data/inputs.json"
    source_file = core["workspace"].joinpath(*source_path.split("/"))

    selected = _select_fixture_run_with_feasibility_claim(
        core, case_id, candidate_id, run_id, result_refs
    )
    editable = selected["result"]["editable_case"]
    assert editable["status"] == "generated", editable
    assert editable["inputs_path"] == f"{artifact_dir}/data/run-inputs.json"
    assert source_file.read_bytes() == source_bytes
    run_config = json.loads(
        (core["workspace"].joinpath(*artifact_dir.split("/")) / ".optagent" / "run.json").read_text(encoding="utf-8")
    )
    assert run_config["inputs_path"] == "data/run-inputs.json"
    manifest = store.read_record(f".reaslab/optagent/cases/{case_id}/editable-export.json")
    assert manifest["inputs_path"] == editable["inputs_path"]


def test_editable_inputs_allocator_avoids_a_missing_source_path(core):
    from optagent_core.delivery import _select_editable_inputs_path

    case_id, _, _ = core["create_case"]()
    store = core["store"]()
    artifact_dir = store.read_case(case_id)["artifact_dir"]
    source_path = f"{artifact_dir}/missing-source-probe/data/inputs.json"
    selected_path = _select_editable_inputs_path(
        store,
        f"{artifact_dir}/missing-source-probe",
        {source_path},
    )
    assert selected_path == f"{artifact_dir}/missing-source-probe/data/run-inputs.json"


def test_editable_inputs_skip_multiple_unowned_paths(core):
    case_id, candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(core)
    artifact_dir = store.read_case(case_id)["artifact_dir"]
    case_root = core["workspace"].joinpath(*artifact_dir.split("/"))
    data_dir = case_root / "data"
    data_dir.mkdir()
    sentinels = {
        "inputs.json": b"Keep the original normalized-input-name file.\n",
        "run-inputs.json": b"Keep fallback one.\n",
        "run-inputs-2.json": b"Keep fallback two.\n",
    }
    for name, content in sentinels.items():
        (data_dir / name).write_bytes(content)

    selected = _select_fixture_run_with_feasibility_claim(
        core, case_id, candidate_id, run_id, result_refs
    )
    editable = selected["result"]["editable_case"]
    assert editable["status"] == "generated", editable
    assert editable["inputs_path"] == f"{artifact_dir}/data/run-inputs-3.json"
    for name, content in sentinels.items():
        assert (data_dir / name).read_bytes() == content
        assert f"{artifact_dir}/data/{name}" not in editable["files"]


def test_editable_inputs_skip_an_unowned_file_even_when_bytes_match(core, monkeypatch):
    from optagent_core import delivery as delivery_module

    case_id, candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(core)
    artifact_dir = store.read_case(case_id)["artifact_dir"]
    case_root = core["workspace"].joinpath(*artifact_dir.split("/"))
    real_plan = delivery_module._editable_case_projection_plan
    injected: dict[str, bytes] = {}

    def plan_after_same_bytes_occupy_default(store_value, state, delivery):
        plan = real_plan(store_value, state, delivery)
        default_path = f"{artifact_dir}/data/inputs.json"
        if not injected and plan.get("status") == "planned" and plan.get("inputs_path") == default_path:
            identical = plan["files"][default_path]
            target = case_root / "data" / "inputs.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(identical)
            injected["bytes"] = identical
            return real_plan(store_value, state, delivery)
        return plan

    monkeypatch.setattr(delivery_module, "_editable_case_projection_plan", plan_after_same_bytes_occupy_default)
    selected = _select_fixture_run_with_feasibility_claim(
        core, case_id, candidate_id, run_id, result_refs
    )
    editable = selected["result"]["editable_case"]
    assert editable["status"] == "generated", editable
    assert editable["inputs_path"] == f"{artifact_dir}/data/run-inputs.json"
    assert (case_root / "data" / "inputs.json").read_bytes() == injected["bytes"]
    manifest = store.read_record(f".reaslab/optagent/cases/{case_id}/editable-export.json")
    assert f"{artifact_dir}/data/inputs.json" not in manifest["files"]
    assert f"{artifact_dir}/data/run-inputs.json" in manifest["files"]


def test_editable_input_path_occupied_after_plan_stays_a_conflict(core, monkeypatch):
    from optagent_core import delivery as delivery_module

    case_id, candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(core)
    artifact_dir = store.read_case(case_id)["artifact_dir"]
    case_root = core["workspace"].joinpath(*artifact_dir.split("/"))
    real_publish = delivery_module._publish_editable_case_projection
    occupied = b"Created after input-path planning; must not be overwritten.\n"

    def occupy_then_publish(store_value, **kwargs):
        plan = kwargs["plan"]
        relative = plan["inputs_path"]
        target = core["workspace"].joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(occupied)
        return real_publish(store_value, **kwargs)

    monkeypatch.setattr(delivery_module, "_publish_editable_case_projection", occupy_then_publish)
    selected = _select_fixture_run_with_feasibility_claim(
        core, case_id, candidate_id, run_id, result_refs
    )
    editable = selected["result"]["editable_case"]
    assert editable["status"] == "not_generated"
    assert editable["reason"] == "existing_case_file_conflict"
    assert editable["details"]["path"] == f"{artifact_dir}/data/inputs.json"
    assert (case_root / "data" / "inputs.json").read_bytes() == occupied
    assert "Mathematical delivery remains selected" in selected["result"]["message"]


@pytest.mark.parametrize("relative", ["data/inputs.json", ".optagent/run.json"])
def test_editable_input_and_config_edits_keep_the_exported_path_and_conflict(core, relative):
    case_id, candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(core)
    review_id = _core11_begin_note(core, case_id, candidate_id, [run_id])
    closed = _core11_close(core, case_id, review_id, [{
        "kind": "feasibility",
        "scope": "The selected checked result is feasible for its recorded instance.",
        "conditions": [],
        "evidence_refs": [result_refs[0]],
        "support": "supported",
    }])
    assert closed["ok"], closed

    def select_again():
        state = store.read_case(case_id)
        return core["call"]("optagent.deliver", {
            "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
            "kind": "run", "candidate_id": candidate_id,
            "review_id": review_id, "proof_ids": [], "run_ids": [run_id],
        })

    first = select_again()
    assert first["ok"], first
    input_path = first["result"]["editable_case"]["inputs_path"]
    artifact_dir = store.read_case(case_id)["artifact_dir"]
    case_root = core["workspace"].joinpath(*artifact_dir.split("/"))
    edited_path = case_root.joinpath(*relative.split("/"))
    edited_bytes = b"Owner-edited bytes remain protected.\n"
    edited_path.write_bytes(edited_bytes)

    second = select_again()
    assert second["ok"], second
    editable = second["result"]["editable_case"]
    assert editable["status"] == "not_generated"
    assert editable["reason"] == "existing_case_file_conflict"
    assert editable["details"]["path"] == f"{artifact_dir}/{relative}"
    assert store.read_record(f".reaslab/optagent/cases/{case_id}/editable-export.json")["inputs_path"] == input_path
    assert edited_path.read_bytes() == edited_bytes


def test_editable_export_republication_restores_recorded_input_path_if_default_is_empty(core):
    case_id, candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(core)
    artifact_dir = store.read_case(case_id)["artifact_dir"]
    case_root = core["workspace"].joinpath(*artifact_dir.split("/"))
    (case_root / "data").mkdir()
    default_input_path = case_root / "data" / "inputs.json"
    default_input_bytes = b"Existing unowned file at the default input path.\n"
    default_input_path.write_bytes(default_input_bytes)
    review_id = _core11_begin_note(core, case_id, candidate_id, [run_id])
    closed = _core11_close(core, case_id, review_id, [{
        "kind": "feasibility",
        "scope": "The selected checked result is feasible for its recorded instance.",
        "conditions": [],
        "evidence_refs": [result_refs[0]],
        "support": "supported",
    }])
    assert closed["ok"], closed

    def select_again():
        state = store.read_case(case_id)
        return core["call"]("optagent.deliver", {
            "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
            "kind": "run", "candidate_id": candidate_id,
            "review_id": review_id, "proof_ids": [], "run_ids": [run_id],
        })

    first = select_again()
    assert first["ok"], first
    editable = first["result"]["editable_case"]
    assert editable["status"] == "generated", editable
    assert default_input_path.read_bytes() == default_input_bytes
    recorded_path = editable["inputs_path"]
    assert recorded_path.endswith("/data/run-inputs.json")
    manifest_path = f".reaslab/optagent/cases/{case_id}/editable-export.json"
    assert store.read_record(manifest_path)["inputs_path"] == recorded_path

    default_input_path.unlink()
    (case_root / "data" / "run-inputs.json").unlink()
    (case_root / ".optagent" / "run.json").unlink()
    second = select_again()
    assert second["ok"], second
    second_editable = second["result"]["editable_case"]
    assert second_editable["status"] == "generated", second_editable
    assert second_editable["inputs_path"] == recorded_path
    assert not default_input_path.exists()
    assert (case_root / "data" / "run-inputs.json").is_file()
    assert (case_root / ".optagent" / "run.json").is_file()
    assert store.read_record(manifest_path)["inputs_path"] == recorded_path


def test_editable_export_accepts_legacy_input_path_and_rejects_cross_case_metadata(core):
    from optagent_core.delivery import _editable_case_projection_plan, _editable_export_manifest_path

    case_id, candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(core)
    selected = _select_fixture_run_with_feasibility_claim(
        core, case_id, candidate_id, run_id, result_refs
    )
    artifact_dir = store.read_case(case_id)["artifact_dir"]
    manifest_path = _editable_export_manifest_path(case_id)
    manifest = store.read_record(manifest_path)
    assert manifest["inputs_path"] == f"{artifact_dir}/data/inputs.json"
    manifest.pop("inputs_path")
    store.write_record(manifest_path, manifest, replace=True)
    delivery = store.read_record(
        f".reaslab/optagent/cases/{case_id}/deliveries/{selected['result']['delivery']['id']}/delivery.json"
    )
    legacy_plan = _editable_case_projection_plan(store, store.read_case(case_id), delivery)
    assert legacy_plan["status"] == "planned", legacy_plan
    assert legacy_plan["inputs_path"] == f"{artifact_dir}/data/inputs.json"

    manifest["inputs_path"] = "cases/another-case/data/inputs.json"
    store.write_record(manifest_path, manifest, replace=True)
    invalid_plan = _editable_case_projection_plan(store, store.read_case(case_id), delivery)
    assert invalid_plan["status"] == "not_generated"
    assert invalid_plan["reason"] == "projection_failed"
    assert invalid_plan["details"]["error_code"] == "artifact_changed"


def test_editable_requirements_never_overwrites_a_current_or_previous_source(core):
    from optagent_core.delivery import (
        _editable_case_projection_plan,
        _editable_export_manifest_path,
        _editable_previous_source_paths,
        _preflight_editable_case_projection,
        _publish_editable_case_projection,
    )

    source_bytes = b"# User environment constraint\nmanual-package>=1\n"
    case_id, candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(
        core,
        requirement_source_path="requirements.txt",
        requirement_source_bytes=source_bytes,
    )
    review_id = _core11_begin_note(core, case_id, candidate_id, [run_id])
    feasibility = {
        "kind": "feasibility",
        "scope": "The checked fixture Result is feasible.",
        "conditions": [],
        "evidence_refs": [result_refs[0]],
        "support": "supported",
    }
    closed = _core11_close(core, case_id, review_id, [feasibility], report_path="reports/final-review.md")
    assert closed["ok"], closed

    state = store.read_case(case_id)
    first = core["call"]("optagent.deliver", {
        "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "run", "candidate_id": candidate_id,
        "review_id": review_id, "proof_ids": [], "run_ids": [run_id],
    })
    assert first["ok"], first
    assert first["result"]["editable_case"]["status"] == "generated"
    artifact_dir = store.read_case(case_id)["artifact_dir"]
    case_root = core["workspace"].joinpath(*artifact_dir.split("/"))
    requirements_path = f"{artifact_dir}/requirements.txt"
    assert (case_root / "requirements.txt").read_bytes() == source_bytes

    # Older projections could claim a Source path as generated output. Model
    # that prior record while retaining the exact bytes of the real source.
    manifest_path = _editable_export_manifest_path(case_id)
    manifest = store.read_record(manifest_path)
    assert manifest["delivery_id"] == first["result"]["delivery"]["id"]
    manifest["files"][requirements_path] = store.ref_for_path(requirements_path)
    store.write_record(manifest_path, manifest, replace=True)
    previous_protected, _ = _editable_previous_source_paths(
        store, case_id, artifact_dir, manifest
    )
    assert requirements_path in previous_protected

    delivery = store.read_record(
        f".reaslab/optagent/cases/{case_id}/deliveries/{first['result']['delivery']['id']}/delivery.json"
    )
    plan = _editable_case_projection_plan(store, store.read_case(case_id), delivery)
    assert plan["status"] == "planned"
    assert requirements_path in plan["protected_source_paths"]
    plan["files"][requirements_path] = b"# Refreshed observed interface packages\npyomo==6.9.7\n"
    current_preflight = _preflight_editable_case_projection(store, case_id=case_id, plan=plan)
    assert current_preflight["status"] == "planned", current_preflight
    assert current_preflight["requirements_preserved"] is True
    previous_only_plan = dict(plan)
    previous_only_plan["protected_source_paths"] = [
        path for path in plan["protected_source_paths"] if path != requirements_path
    ]
    preflight = _preflight_editable_case_projection(
        store, case_id=case_id, plan=previous_only_plan
    )
    assert preflight["status"] == "planned", preflight
    assert preflight["requirements_preserved"] is True
    state = store.read_case(case_id)
    published = _publish_editable_case_projection(
        store,
        case_id=case_id,
        owner_session_id=state["owner_session_id"],
        expected_selected_delivery_id=first["result"]["delivery"]["id"],
        plan=preflight,
    )
    assert published["status"] == "generated", published
    assert (case_root / "requirements.txt").read_bytes() == source_bytes
    assert published["requirements_preserved"] is True
    assert requirements_path in published["relinquished_paths"]
    manifest = store.read_record(manifest_path)
    assert requirements_path not in manifest["files"]


def test_shared_visible_builder_edit_marks_each_case_stale_and_blocks_next_boundary(core):
    model_path = core["workspace"] / "shared_builder.py"
    model_path.write_text("def build_model(data):\n    return None\n", encoding="utf-8")
    cases = []
    for seq, source in ((1, b"First case objective."), (2, b"Second case objective.")):
        case_id, _, _ = core["create_case"](raw=source, seq=seq)
        state = core["store"]().read_case(case_id)
        registered = core["call"](
            "optagent.case",
            {
                "op": "revise",
                "case_id": case_id,
                "expected_state_revision": state["state_revision"],
                "kind": "candidate",
                "model_path": "shared_builder.py",
                "local_module_paths": [],
                "data_id": None,
                "backend_id": "highs",
                "objective_plan": {"mode": "feasibility", "preference_note": "visible builder probe"},
                "business_checker_path": None,
                "input_closure": "declared",
                "closure_note": "The visible builder is the complete model source.",
            },
        )
        assert registered["ok"], registered
        cases.append((case_id, registered["result"]["candidate"]["id"]))

    pinned_state_refs = {
        case_id: core["store"]().ref_for_path(f".reaslab/optagent/cases/{case_id}/state.json")
        for case_id, _ in cases
    }
    model_path.write_text(
        "def build_model(data):\n    # changed through the shared visible builder\n    return None\n",
        encoding="utf-8",
    )

    fetched = {
        case_id: core["call"]("optagent.case", {"op": "get", "case_id": case_id})
        for case_id, _ in cases
    }
    assert all(reply["ok"] and reply["result"]["current_validity"] == "stale" for reply in fetched.values())
    listed = core["call"]("optagent.case", {"op": "list"})
    assert listed["ok"], listed
    listed_by_id = {item["case_id"]: item for item in listed["result"]["cases"]}
    assert all(listed_by_id[case_id]["current_validity"] == "stale" for case_id, _ in cases)

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
    for case_id, candidate_id in cases:
        state = core["store"]().read_case(case_id)
        prepared = core["call"](
            "optagent.run",
            {
                "op": "prepare",
                "case_id": case_id,
                "expected_state_revision": state["state_revision"],
                "candidate_id": candidate_id,
                "solve_options": solve_options,
            },
        )
        assert not prepared["ok"] and prepared["error"]["code"] == "candidate_dirty"
        selected = core["call"](
            "optagent.deliver",
            {
                "op": "select",
                "case_id": case_id,
                "expected_state_revision": state["state_revision"],
                "kind": "proof",
                "candidate_id": candidate_id,
                "review_id": str(uuid.uuid4()),
                "proof_ids": [],
                "run_ids": [],
            },
        )
        assert not selected["ok"] and selected["error"]["code"] == "candidate_dirty"
        assert core["store"]().ref_for_path(f".reaslab/optagent/cases/{case_id}/state.json") == pinned_state_refs[case_id]


def test_report_label_edit_needs_review_but_whitespace_only_edit_is_consistent(core):
    from test_reuse_proof import _proof_review_to_delivery

    case_id, _, _, _, delivery_id = _proof_review_to_delivery(core)
    state = core["store"]().read_case(case_id)
    built = core["call"](
        "optagent.deliver",
        {
            "op": "build",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "delivery_id": delivery_id,
            "target_path": "reports/audited.md",
        },
    )
    assert built["ok"], built
    report_path = core["workspace"] / "reports" / "audited.md"
    original = report_path.read_text(encoding="utf-8")
    store = core["store"]()
    delivery = store.read_record(f".reaslab/optagent/cases/{case_id}/deliveries/{delivery_id}/delivery.json")
    presentation_ref = store.read_case(case_id)["current_records"]["presentation"]
    presentation = strict_json_loads(store.read_ref(presentation_ref))
    assert presentation["reports"] == delivery["reports"]
    assert built["result"]["report_ref"] not in presentation["reports"]
    unchanged = core["call"](
        "optagent.deliver",
        {"op": "audit", "case_id": case_id, "delivery_id": delivery_id, "report_path": "reports/audited.md"},
        role="opt-verifier",
    )
    assert unchanged["ok"] and unchanged["result"]["status"] == "consistent"
    assert unchanged["result"]["comparison_basis"] == "selected_evidence_rendering"
    assert unchanged["result"]["content_differences"] == []
    assert unchanged["result"]["scientific_changes"] == []

    report_path.write_text(" ".join(original.split()) + "\n", encoding="utf-8")
    state_ref = core["store"]().ref_for_path(f".reaslab/optagent/cases/{case_id}/state.json")
    formatted = core["call"](
        "optagent.deliver",
        {"op": "audit", "case_id": case_id, "delivery_id": delivery_id, "report_path": "reports/audited.md"},
        role="opt-verifier",
    )
    assert formatted["ok"] and formatted["result"]["status"] == "consistent"
    assert formatted["result"]["comparison_basis"] == "selected_evidence_rendering"
    assert formatted["result"]["content_differences"] == []
    assert formatted["result"]["scientific_changes"] == []

    report_path.write_text(original.replace("## Reviewed claims", "## Reviewed findings"), encoding="utf-8")
    edited = core["call"](
        "optagent.deliver",
        {"op": "audit", "case_id": case_id, "delivery_id": delivery_id, "report_path": "reports/audited.md"},
        role="opt-verifier",
    )
    assert edited["ok"] and edited["result"]["status"] == "needs_review"
    assert edited["result"]["comparison_basis"] == "selected_evidence_rendering"
    assert edited["result"]["content_differences"] == [
        "Report content differs from the selected evidence comparison basis."
    ]
    assert edited["result"]["scientific_changes"] == []
    assert core["store"]().ref_for_path(f".reaslab/optagent/cases/{case_id}/state.json") == state_ref


def test_unregistered_owner_report_mismatch_is_not_reported_as_scientific(core):
    from test_reuse_proof import _proof_review_to_delivery

    case_id, _, _, _, delivery_id = _proof_review_to_delivery(core)
    report_path = core["workspace"] / "owner-explanation.md"
    report_path.write_text(
        "# User-facing mathematical explanation\n\nThis is a separately authored explanation.\n",
        encoding="utf-8",
    )
    state_ref = core["store"]().ref_for_path(f".reaslab/optagent/cases/{case_id}/state.json")
    audited = core["call"](
        "optagent.deliver",
        {
            "op": "audit",
            "case_id": case_id,
            "delivery_id": delivery_id,
            "report_path": "owner-explanation.md",
        },
        role="opt-verifier",
    )
    assert audited["ok"] and audited["result"]["status"] == "needs_review"
    assert audited["result"]["comparison_basis"] == "selected_evidence_rendering"
    assert audited["result"]["content_differences"]
    assert audited["result"]["scientific_changes"] == []
    assert core["store"]().ref_for_path(f".reaslab/optagent/cases/{case_id}/state.json") == state_ref


def test_json_report_content_match_distinguishes_batch_integer_from_boolean():
    from optagent_core.delivery import _layout_equivalent

    expected = canonical_json_bytes({
        "result_evidence": [{"batch": {"planned_instance_count": 1}}],
    })
    same_value = canonical_json_bytes({
        "result_evidence": [{"batch": {"planned_instance_count": 1}}],
    })
    changed_type = canonical_json_bytes({
        "result_evidence": [{"batch": {"planned_instance_count": True}}],
    })
    assert _layout_equivalent(same_value, expected, ".json")
    assert not _layout_equivalent(changed_type, expected, ".json")


def test_new_visible_source_does_not_make_historical_delivery_current(core):
    from test_reuse_proof import _proof_review_to_delivery

    case_id, _, _, _, delivery_id = _proof_review_to_delivery(core)
    state = core["store"]().read_case(case_id)
    built = core["call"](
        "optagent.deliver",
        {
            "op": "build",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "delivery_id": delivery_id,
            "target_path": "reports/historical.md",
        },
    )
    assert built["ok"], built
    delivery = core["store"]().read_record(
        f".reaslab/optagent/cases/{case_id}/deliveries/{delivery_id}/delivery.json"
    )

    (core["workspace"] / "new_requirement.md").write_text(
        "The current case includes a newly captured requirement.",
        encoding="utf-8",
    )
    state = core["store"]().read_case(case_id)
    added = core["call"](
        "optagent.data",
        {
            "op": "capture",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "path": "new_requirement.md",
            "purpose": "requirement",
            "locations": ["paragraph 1"],
        },
    )
    assert added["ok"], added
    current = core["store"]().read_case(case_id)
    assert current["source_revision"] > delivery["source_revision"]

    fetched = core["call"]("optagent.case", {"op": "get", "case_id": case_id})
    assert fetched["ok"] and fetched["result"]["current_validity"] == "stale"
    audited = core["call"](
        "optagent.deliver",
        {
            "op": "audit",
            "case_id": case_id,
            "delivery_id": delivery_id,
            "report_path": "reports/historical.md",
        },
        role="opt-verifier",
    )
    assert not audited["ok"] and audited["error"]["code"] == "evidence_insufficient"
    assert core["store"]().read_case(case_id)["selected_delivery_id"] == delivery_id


def test_standalone_bootstrap_requires_isolated_interpreter_before_reading_package(tmp_path):
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "optagent_reproduce.py"
    output = tmp_path / "fresh-output"
    completed = subprocess.run(
        [sys.executable, str(script), "--output-dir", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "no package files were read" in completed.stderr
    assert not output.exists()


def test_isolated_bootstrap_rejects_refmap_traversal_before_import(tmp_path):
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "optagent_reproduce.py"
    archive_path = tmp_path / "traversal-package.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("reproduce/run.py", script.read_bytes())
    case_id = "00000000-0000-4000-8000-000000000001"
    prepared_id = "00000000-0000-4000-8000-000000000002"
    manifest = {
        "schema_version": 1,
        "case_id": case_id,
        "delivery_id": "00000000-0000-4000-8000-000000000003",
        "selection_ref": {
            "path": f".reaslab/optagent/cases/{case_id}/deliveries/00000000-0000-4000-8000-000000000003/delivery.json",
            "sha256": "0" * 64, "byte_len": 0, "media_type": "application/json",
        },
        "authority_records": {"reviews": {}, "runs": {}},
        "consumed_refs": [{
            "path": f".reaslab/optagent/cases/{case_id}/deliveries/00000000-0000-4000-8000-000000000003/delivery.json",
            "sha256": "0" * 64, "byte_len": 0, "media_type": "application/json",
        }],
        "objects": [],
        "claims": [],
        "reviews": [],
        "claim_attachments": [],
        "data_bundles": [],
        "runtime_bundles": [],
        "reproductions": [],
        "ref_map": [
            {
                "ref": {
                    "path": f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/input/manifest.json",
                    "sha256": "0" * 64,
                    "byte_len": 0,
                    "media_type": "application/json",
                },
                "package_path": "../outside.json",
            }
        ],
        "entry": "README.md",
    }
    with zipfile.ZipFile(archive_path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
    package = tmp_path / "moved-package"
    package.mkdir()
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(package)
    output = tmp_path / "fresh-output"
    completed = subprocess.run(
        [sys.executable, "-I", str(package / "reproduce" / "run.py"), "--output-dir", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "escapes the extraction root" in completed.stderr
    assert not output.exists()


def test_bootstrap_run_output_refs_require_exact_manifest_authorization(tmp_path):
    script = Path(__file__).resolve().parents[1] / "optagent_reproduce.py"
    probe = tmp_path / "authorization_probe.py"
    probe.write_text(
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('optagent_reproduce_contract_test', sys.argv[1])\n"
        "bootstrap = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(bootstrap)\n"
        "case_id = '00000000-0000-4000-8000-000000000001'\n"
        "run_id = '00000000-0000-4000-8000-000000000002'\n"
        "def validate(suffix, package_path, authorized_path):\n"
        "    ref = {'path': f'.reaslab/optagent/cases/{case_id}/runs/{run_id}/output/{suffix}', 'sha256': 'a' * 64, 'byte_len': 1, 'media_type': 'application/json'}\n"
        "    locations = {} if authorized_path is None else {bootstrap._ref_key(ref): authorized_path}\n"
        "    return bootstrap._validate_map_location(case_id, ref, package_path, {}, selected_run_ids={run_id}, expected_locations=locations)\n"
        "def rejects(*args):\n"
        "    try:\n"
        "        validate(*args)\n"
        "    except ValueError as exc:\n"
        "        assert 'authorized evidence path' in str(exc)\n"
        "        return\n"
        "    raise AssertionError('unauthorized or remapped output Ref was accepted')\n"
        "attachment = 'evidence/attachments/' + 'a' * 64 + '.json'\n"
        "validate('transport-response.json', attachment, attachment)\n"
        "rejects('transport-response.json', 'evidence/runs/' + run_id + '/outputs/transport-response.json', None)\n"
        "rejects('result.json', attachment, f'evidence/runs/{run_id}/outputs/result.json')\n"
        "rejects('transport-response.json', 'evidence/attachments/other.json', attachment)\n"
        "print('four authorization cases passed')\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, "-I", str(probe), str(script)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert completed.stdout.strip() == "four authorization cases passed"


def test_bootstrap_json_depth_matches_core_container_limit_in_a_package_zip(tmp_path):
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "optagent_reproduce.py"

    def nested(count: int):
        value = "leaf"
        for index in range(count):
            value = {"child": value} if index % 2 == 0 else [value]
        return value

    def run_package(total_containers: int) -> subprocess.CompletedProcess[str]:
        archive_path = tmp_path / f"depth-{total_containers}.zip"
        # The root manifest object counts as one container.
        manifest_raw = json.dumps(
            {
                "schema_version": 1,
                "case_id": "00000000-0000-4000-8000-000000000001",
                "delivery_id": "00000000-0000-4000-8000-000000000002",
                "selection_ref": {},
                "authority_records": {"reviews": {}, "runs": {}},
                "consumed_refs": [],
                "objects": [], "claims": [], "reviews": [], "claim_attachments": [],
                "data_bundles": [], "runtime_bundles": [], "reproductions": [], "ref_map": [],
                "entry": "README.md",
                "depth_probe": nested(total_containers - 1),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("reproduce/run.py", script.read_bytes())
            archive.writestr("manifest.json", manifest_raw)
        package = tmp_path / f"package-{total_containers}"
        package.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(package)
        return subprocess.run(
            [sys.executable, "-I", str(package / "reproduce" / "run.py"), "--output-dir", str(tmp_path / f"out-{total_containers}")],
            capture_output=True,
            text=True,
            check=False,
        )

    at_limit = run_package(64)
    assert at_limit.returncode == 2
    assert "package manifest fields are invalid" in at_limit.stderr
    assert "nesting limit" not in at_limit.stderr
    assert not (tmp_path / "out-64").exists()

    over_limit = run_package(65)
    assert over_limit.returncode == 2
    assert "package JSON exceeds the fixed nesting limit" in over_limit.stderr
    assert not (tmp_path / "out-65").exists()


def test_runtime_bundle_package_preserves_the_fixed_seven_helper_import_tree(core):
    from optagent_core.runner import _publish_runtime_bundle

    store = core["store"]()
    digest, manifest_ref, _ = _publish_runtime_bundle(store)
    files = {}
    objects = []
    ref_map = {}
    runtime = {
        "digest": digest,
        "manifest": manifest_ref,
        "worker_entry": "python/optagent_core/worker.py",
    }
    packaged = _package_runtime_bundle(store, files, objects, ref_map, runtime, prepared_schema_version=3)
    root = f"reproduce/runtime/{digest}"
    assert packaged["standalone_supported"] is True
    assert packaged["helper_paths"] == [
        "python/optagent_core/__init__.py",
        "python/optagent_core/contracts.py",
        "python/optagent_core/data.py",
        "python/optagent_core/models.py",
        "python/optagent_core/results.py",
        "python/optagent_core/solver_protocol.py",
        "python/optagent_core/worker.py",
    ]
    assert files[f"{root}/COMPLETE"] == digest.encode("ascii")
    assert files[f"{root}/manifest.json"] == store.read_ref(manifest_ref)
    assert set(files) == {f"{root}/COMPLETE", f"{root}/manifest.json", *(f"{root}/{name}" for name in packaged["helper_paths"])}
    assert len(ref_map) == 8


def _csv_data_plan(source_id: str) -> dict:
    return {
        "encoding": "utf-8",
        "blocks": [{
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
        }],
        "conversions": [],
    }


def _short_test_workspace(tmp_path: Path) -> Path:
    root = tmp_path.parents[1]
    workspace = root / f"w{uuid.uuid4().hex[:6]}"
    workspace.mkdir()
    return workspace


def _core_at(workspace: Path):
    import base64
    import hashlib

    from optagent_core.api import dispatch_dynamic
    from optagent_core.store import CaseStore

    session_id = str(uuid.uuid4())
    stamp = {"session_id": session_id, "role": "root", "turn_id": str(uuid.uuid4()), "call_id": str(uuid.uuid4())}

    def context(*, role="root", captures=None):
        trusted_stamp = {**stamp, "role": role, "turn_id": str(uuid.uuid4()), "call_id": str(uuid.uuid4())}
        return {
            "stamp": trusted_stamp,
            "workspace_absolute": str(workspace),
            "cancellation_observed": False,
            "captures": list(captures or []),
        }

    def capture(raw: bytes, *, seq: int, channel="prompt", admission=None):
        if admission is None:
            admission = "received_prompt" if channel == "prompt" else "pending_steer"
        return {
            "capture_id": str(uuid.uuid4()), "arrival_epoch": str(uuid.uuid4()), "arrival_seq": seq,
            "session_id": session_id, "channel": channel, "admission": admission,
            "utf8_bytes_base64": base64.b64encode(raw).decode("ascii"), "sha256": hashlib.sha256(raw).hexdigest(),
        }

    def call(tool, args, *, role="root", captures=None):
        response = dispatch_dynamic(tool, args, context(role=role, captures=captures))
        assert response.get("kind") == "complete"
        return response["reply"]

    def create_case(*, raw=b"Minimize total cost.", seq=1, task_kind="optimize"):
        transfer = capture(raw, seq=seq)
        reply = call("optagent.case", {
            "op": "create", "capture_id": transfer["capture_id"], "title": "Core9 package fixture",
            "task_kind": task_kind, "world_relation": "single", "authorization_note": "Synthetic local test",
        }, captures=[transfer])
        assert reply["ok"], reply
        return reply["case_id"], transfer, reply

    return {
        "workspace": workspace, "session_id": session_id, "context": context, "capture": capture,
        "call": call, "create_case": create_case, "store": lambda: CaseStore(workspace, create=True),
    }


def test_candidate_data_package_preserves_manifest_trace_value_refs_and_bytes(tmp_path):
    workspace = _short_test_workspace(tmp_path)
    core = _core_at(workspace)
    try:
        _test_candidate_data_package(core)
    finally:
        shutil.rmtree(workspace)


def _select_candidate_data_delivery(core, case_id):
    """Create a real review and public selection before exercising package bytes."""
    store = core["store"]()
    state = store.read_case(case_id)
    candidate_id = state["candidate_id"]
    source_id = state["source_ids"][0]
    source_record = store.read_record(f".reaslab/optagent/cases/{case_id}/sources/{source_id}.json")
    claim = {
        "kind": "formulation_fidelity",
        "scope": "The candidate uses the current captured source and normalized data bundle.",
        "conditions": [],
        "evidence_refs": [
            store.ref_for_path(f".reaslab/optagent/cases/{case_id}/candidates/{candidate_id}.json"),
            source_record["bytes"],
        ],
        "support": "supported",
    }
    begun = core["call"]("optagent.review", {
        "op": "begin", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "candidate_id": candidate_id, "proof_ids": [], "run_ids": [],
    }, role="opt-verifier")
    assert begun["ok"], begun
    review_id = begun["result"]["review"]["id"]
    noted = core["call"]("optagent.review", {
        "op": "note", "case_id": case_id, "review_id": review_id,
        "expected_review_revision": 1,
        "body": "The captured source is the complete fixed mathematical scope.",
        "source_locations": [{"source_id": source_id, "locator": "source-level"}],
    }, role="opt-verifier")
    assert noted["ok"], noted
    closed = _core11_close(core, case_id, review_id, [claim])
    assert closed["ok"], closed
    state = store.read_case(case_id)
    selected = core["call"]("optagent.deliver", {
        "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "mixed", "candidate_id": candidate_id,
        "review_id": review_id, "proof_ids": [], "run_ids": [],
    })
    assert selected["ok"], selected
    return selected["result"]["delivery"]["id"]


def _test_candidate_data_package(core):
    case_id, _, _ = core["create_case"]()
    store = core["store"]()
    input_dir = core["workspace"] / "inputs"
    input_dir.mkdir()
    source_path = input_dir / "sites.csv"
    source_path.write_bytes(b"site,cost\nA,10.5\n")
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
        "source_ids": [source_id], "plan": _csv_data_plan(source_id),
    })
    assert normalized["ok"], normalized
    data_id = normalized["result"]["data_manifest"]["data_id"]

    model_path = core["workspace"] / "model.py"
    model_path.write_text("def build_model(data):\n    return None\n", encoding="utf-8")
    state = store.read_case(case_id)
    candidate = core["call"]("optagent.case", {
        "op": "revise", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "candidate", "model_path": "model.py", "local_module_paths": [],
        "data_id": data_id, "backend_id": "highs",
        "objective_plan": {"mode": "feasibility", "preference_note": "portable identity test"},
        "business_checker_path": None, "input_closure": "declared", "closure_note": "Fixed normalized input.",
    })
    assert candidate["ok"], candidate
    state = store.read_case(case_id)

    # Selection publishes the current presentation Ref. Packaging remains the
    # public operation under test and does not need a preceding report build.
    delivery_id = _select_candidate_data_delivery(core, case_id)
    state = store.read_case(case_id)
    packed = core["call"]("optagent.deliver", {
        "op": "package", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "delivery_id": delivery_id, "target_path": "exports/candidate-data.zip", "include_raw_sources": False,
    })
    assert packed["ok"], packed
    archive_raw = store.read_ref(packed["result"]["package_ref"])
    extracted = core["workspace"] / "moved-candidate-data"
    extracted.mkdir()
    with zipfile.ZipFile(BytesIO(archive_raw)) as archive:
        archive.extractall(extracted)
        manifest = strict_json_loads(archive.read("manifest.json"))
        portable_manifest = archive.read(f"data/{data_id}/manifest.json")

    original_manifest_ref = store.ref_for_path(f".reaslab/optagent/cases/{case_id}/data/{data_id}/manifest.json")
    original_manifest = store.read_ref(original_manifest_ref)
    assert portable_manifest == original_manifest
    assert manifest["data_bundles"] == [{
        "data_id": data_id,
        "digest": strict_json_loads(original_manifest)["digest"],
        "manifest": original_manifest_ref,
        "loader_version": "optagent-data-v1",
    }]
    ref_map = manifest["ref_map"]
    reader = PortableReader(extracted, ref_map)
    candidate_record = store.read_record(
        f".reaslab/optagent/cases/{case_id}/candidates/{candidate['result']['candidate']['id']}.json"
    )
    original_data_manifest = strict_json_loads(original_manifest)
    loaded = load_data_bundle(
        reader,
        original_manifest_ref,
        candidate_binding_digest=candidate_record["binding_digest"],
        source_records=data_source_records(
            store,
            case_id=case_id,
            source_ids=original_data_manifest["source_ids"],
            state=store.read_case(case_id),
        ),
    )
    assert loaded.digest == original_data_manifest["digest"]
    assert loaded.data_id == data_id
    trace_ref = original_data_manifest["trace"]
    assert reader.read_ref(trace_ref) == store.read_ref(trace_ref)
    for item in original_data_manifest["objects"]:
        value_ref = item["values"]
        assert reader.read_ref(value_ref) == store.read_ref(value_ref)
        matching = [row for row in ref_map if row["ref"] == value_ref]
        assert len(matching) == 1
        assert matching[0]["package_path"].startswith(f"data/{data_id}/objects/")

    # Run the fixed stdlib bootstrap against this real package. It should
    # validate the data RefMap first, then honestly report that this evidence
    # package has no native reproduction entry.
    bootstrap = Path(__file__).resolve().parents[1] / "optagent_reproduce.py"
    reproduce_dir = extracted / "reproduce"
    reproduce_dir.mkdir()
    bootstrap_copy = reproduce_dir / "run.py"
    bootstrap_copy.write_bytes(bootstrap.read_bytes())
    completed = subprocess.run(
        [sys.executable, "-I", str(bootstrap_copy), "--output-dir", str(core["workspace"] / "unused-output")],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 2
    assert "no supported standalone native reproduction entry" in completed.stderr
    assert not (core["workspace"] / "unused-output").exists()


def test_prepared_no_data_bundle_keeps_null_manifest_and_passes_bootstrap_validation(tmp_path):
    workspace = _short_test_workspace(tmp_path)
    core = _core_at(workspace)
    try:
        _test_prepared_no_data_bundle(core, tmp_path)
    finally:
        shutil.rmtree(workspace)


def _test_prepared_no_data_bundle(core, tmp_path):
    from optagent_core.delivery import _add_package_file, _package_prepared_closure, _verify_portable_reproductions, _zip_bytes
    from test_worker import _publish_legacy_runtime_bundle

    case_id, _, _ = core["create_case"](raw=b"NoData package fixture.")
    store = core["store"]()
    state = store.read_case(case_id)
    runtime_digest, runtime_ref, _ = _publish_legacy_runtime_bundle(store)
    runtime = {"digest": runtime_digest, "manifest": runtime_ref, "worker_entry": "python/optagent_core/worker.py"}
    prepared_id = str(uuid.uuid4())
    candidate_id = str(uuid.uuid4())
    code_raw = b"def build_model(data):\n    return None\n"
    code_ref = store.write_immutable(
        f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/input/source/model.py",
        code_raw,
        "text/x-python; charset=utf-8",
    )
    prepared = {
        "schema_version": 2, "prepared_id": prepared_id, "case_id": case_id,
        "source_revision": state["source_revision"], "candidate_revision": state["candidate_revision"],
        "candidate_id": candidate_id, "promotion_generation": state["promotion_generation"],
        "binding_digest": "d" * 64, "sources": [],
        "candidate": {
            "authoring": "pyomo", "model": {"original_path": "model.py", "bytes": code_ref},
            "local_modules": [], "business_checker": None, "backend_id": "highs",
            "objective_plan": {"mode": "feasibility", "preference_note": "NoData"},
            "input_closure": "declared", "closure_note": "No external data is used.",
        },
        "data": None, "runtime_bundle": runtime,
        "solve_options": {"instance_keys": []},
    }
    prepared_raw = canonical_json_bytes(prepared)
    input_ref = store.write_immutable(
        f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/input/manifest.json",
        prepared_raw,
        "application/json",
    )
    files: dict[str, bytes] = {}
    objects: list[dict] = []
    ref_map: dict[tuple[str, str, int, str], dict] = {}
    runtime_info = _package_prepared_closure(store, input_ref, case_id, prepared_id, files, objects, ref_map)["runtime"]
    assert files[f"reproduce/prepared/{prepared_id}/input/manifest.json"] == prepared_raw
    assert input_ref == ref_map[(input_ref["path"], input_ref["sha256"], input_ref["byte_len"], input_ref["media_type"])]["ref"]
    assert not any("/data/" in row["ref"]["path"] for row in ref_map.values())
    reproduction = {"prepared_manifest_ref": input_ref, "evidence_run_ids": [str(uuid.uuid4())]}
    ref_map_items = sorted(ref_map.values(), key=lambda row: (row["ref"]["path"], row["ref"]["sha256"], row["ref"]["byte_len"], row["ref"]["media_type"]))
    short_workspace = tmp_path.parents[1] / f"w{uuid.uuid4().hex[:6]}"
    short_workspace.mkdir()
    (short_workspace / ".reaslab/optagent").mkdir(parents=True)
    try:
        _verify_portable_reproductions(short_workspace, files, ref_map_items, [reproduction])
    finally:
        shutil.rmtree(short_workspace)

    # Add a synthetic sealed run around the real frozen closure. The bootstrap
    # is launched with an invalid child index, so it validates the whole ZIP
    # without importing the model worker or invoking a solver.
    run_id = reproduction["evidence_run_ids"][0]
    output_ref = store.write_immutable(
        f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/batch-summary.json",
        b'{"planned_instance_count":0}',
        "application/json",
    )
    seal = {"output_refs": [output_ref]}
    seal_ref = store.write_immutable(
        f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/seal.json",
        canonical_json_bytes(seal),
        "application/json",
    )
    from optagent_core.delivery import _register_portable_ref

    _register_portable_ref(store, output_ref, f"evidence/runs/{run_id}/outputs/batch-summary.json", files, objects, ref_map)
    _register_portable_ref(store, seal_ref, f"evidence/runs/{run_id}/seal.json", files, objects, ref_map)
    run_raw = canonical_json_bytes({"run_id": run_id, "prepared_id": prepared_id, "results": [], "worker": {"seal": seal_ref}})
    run_ref = ref_from_bytes(f".reaslab/optagent/cases/{case_id}/runs/{run_id}/record.json", run_raw, "application/json")
    _register_portable_ref(
        store, run_ref, f"evidence/runs/{run_id}/record.json", files, objects, ref_map, raw_bytes=run_raw
    )
    delivery_id = str(uuid.uuid4())
    fixture = current_portable_review_fixture(
        store,
        case_id=case_id,
        review_id=str(uuid.uuid4()),
        delivery_id=delivery_id,
        kind="run",
        claim={
            "kind": "diagnostic",
            "scope": "This synthetic Run record carries no solver conclusion.",
            "support": "unresolved",
            "conditions": [],
            "evidence_refs": [],
        },
        evidence_refs=(run_ref,),
        run_authorities={run_id: run_ref},
    )
    delivery_record = fixture["selection"]
    selection_ref = fixture["selection_ref"]
    review_raw = next(
        raw
        for _ref, package_path, raw in fixture["portable_files"]
        if package_path == f"evidence/reviews/{fixture['selection']['review_ids'][0]}.json"
    )
    review = json.loads(review_raw)
    assert review["note"]["source_locations"] == []
    assert review["candidate_opened"] is True
    assert review["binding"]["run_ids"] == [run_id]
    assert review["binding_evidence_refs"]["runs"] == {run_id: run_ref}
    assert review["note"]["body"] in fixture["consumed_refs"]
    for fixture_ref, package_path, raw in fixture["portable_files"]:
        _register_portable_ref(
            store, fixture_ref, package_path, files, objects, ref_map, raw_bytes=raw
        )

    bootstrap = Path(__file__).resolve().parents[1] / "optagent_reproduce.py"
    _add_package_file(files, "reproduce/run.py", bootstrap.read_bytes(), objects)
    package_manifest = {
        "schema_version": 1, "case_id": case_id, "delivery_id": delivery_id,
        "selection_ref": selection_ref,
        "authority_records": delivery_record["authority_records"],
        "consumed_refs": fixture["consumed_refs"],
        "objects": objects, "claims": delivery_record["claims"], "reviews": [fixture["projection"]], "claim_attachments": [], "data_bundles": [],
        "runtime_bundles": [runtime_info], "reproductions": [reproduction],
        "ref_map": sorted(ref_map.values(), key=lambda row: (row["ref"]["path"], row["ref"]["sha256"], row["ref"]["byte_len"], row["ref"]["media_type"])),
        "entry": "reproduce/run.py",
    }
    files["manifest.json"] = canonical_json_bytes(package_manifest)
    archive_path = core["workspace"] / "no-data-package.zip"
    archive_path.write_bytes(_zip_bytes(files))
    extracted = core["workspace"] / "no-data-extracted"
    extracted.mkdir()
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(extracted)
    completed = subprocess.run(
        [sys.executable, "-I", str(extracted / "reproduce" / "run.py"), "--_child-entry", "99", "--output-dir", str(core["workspace"] / "unused-output")],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 2
    assert "child reproduction index is invalid" in completed.stderr
    assert "PreparedInputManifest" not in completed.stderr
    assert not (core["workspace"] / "unused-output").exists()


def test_real_package_zip_maps_every_selected_run_seal_output_and_uses_product_bootstrap(tmp_path):
    workspace = _short_test_workspace(tmp_path)
    core = _core_at(workspace)
    try:
        _test_real_package_zip(core)
    finally:
        shutil.rmtree(workspace)


def _test_real_package_zip(core):
    case_id, candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(core)
    review_id = _core11_begin_note(core, case_id, candidate_id, [run_id])
    claim = {
        "kind": "feasibility", "scope": "The checked site A witness is feasible.",
        "conditions": [], "evidence_refs": [result_refs[0]], "support": "supported",
    }
    closed = _core11_close(core, case_id, review_id, [claim], report_path="reports/final-review.md")
    assert closed["ok"], closed
    state = store.read_case(case_id)
    selected = core["call"]("optagent.deliver", {
        "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "run", "candidate_id": candidate_id,
        "review_id": review_id, "proof_ids": [], "run_ids": [run_id],
    })
    assert selected["ok"], selected
    delivery_id = selected["result"]["delivery"]["id"]
    state = store.read_case(case_id)
    assert not (core["workspace"] / "python" / "optagent_reproduce.py").exists()
    run, _run_ref = read_run(store, case_id, run_id)
    prepared = store.read_record(f".reaslab/optagent/cases/{case_id}/prepared/{run['prepared_id']}/prepared.json")
    input_ref = prepared["input_manifest"]
    seal = strict_json_loads(store.read_ref(run["worker"]["seal"]))
    output_refs = seal["output_refs"]
    first = core["call"]("optagent.deliver", {
        "op": "package", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "delivery_id": delivery_id, "target_path": "exports/no-workspace-bootstrap.zip", "include_raw_sources": True,
    })
    assert first["ok"], first
    expected_bootstrap = Path(__file__).resolve().parents[1] / "optagent_reproduce.py"
    first_bytes = store.read_ref(first["result"]["package_ref"])
    with zipfile.ZipFile(BytesIO(first_bytes)) as archive:
        assert archive.read("reproduce/run.py") == expected_bootstrap.read_bytes()
        manifest = strict_json_loads(archive.read("manifest.json"))
        assert manifest["reproductions"][0]["prepared_manifest_ref"] == input_ref
        assert {row["ref"]["path"] for row in manifest["ref_map"]}.issuperset(ref["path"] for ref in output_refs)
        moved = core["workspace"] / "moved-run-package"
        moved.mkdir()
        archive.extractall(moved)
    reader = PortableReader(moved, manifest["ref_map"])
    for output_ref in output_refs:
        assert reader.read_ref(output_ref) == store.read_ref(output_ref)
        mapping = next(row for row in manifest["ref_map"] if row["ref"] == output_ref)
        relative_output = output_ref["path"].split(f"/runs/{run_id}/output/", 1)[1]
        assert mapping["package_path"] == f"evidence/runs/{run_id}/outputs/{relative_output}"

    omitted = output_refs[0]
    incomplete_map = [row for row in manifest["ref_map"] if row["ref"] != omitted]
    incomplete_reader = PortableReader(moved, incomplete_map)
    with pytest.raises(ContractError) as missing:
        incomplete_reader.read_ref(omitted)
    assert missing.value.code == "capture_unavailable"

    omitted_mapping = next(row for row in manifest["ref_map"] if row["ref"] == omitted)
    altered_path = moved.joinpath(*omitted_mapping["package_path"].split("/"))
    original_bytes = altered_path.read_bytes()
    altered_path.write_bytes(b"altered sealed output")
    with pytest.raises(ContractError) as altered:
        reader.read_ref(omitted)
    assert altered.value.code == "artifact_changed"
    altered_path.write_bytes(original_bytes)

    # A conflicting workspace basename must not alter the product helper bytes.
    evil_path = core["workspace"] / "python" / "optagent_reproduce.py"
    evil_path.parent.mkdir(parents=True)
    evil_path.write_bytes(b"# workspace supplied bootstrap must not enter the ZIP\n")
    state = store.read_case(case_id)
    second = core["call"]("optagent.deliver", {
        "op": "package", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "delivery_id": delivery_id, "target_path": "exports/evil-workspace-bootstrap.zip", "include_raw_sources": True,
    })
    assert second["ok"], second
    with zipfile.ZipFile(BytesIO(store.read_ref(second["result"]["package_ref"]))) as archive:
        assert archive.read("reproduce/run.py") == expected_bootstrap.read_bytes()
        assert archive.read("reproduce/run.py") != evil_path.read_bytes()


def _schema3_fixture_backend_observation(
    solve_options: dict,
    *,
    termination: str,
    objective_sense: str | None,
    objective_value: float | None,
    bound_value: float | None,
) -> dict:
    requested_options = {
        "native": {
            "time_limit_seconds": solve_options["time_limit_seconds"],
            "threads": solve_options["threads"],
            "random_seed": solve_options["random_seed"],
            "warm_start": solve_options.get("warm_start", False),
            "parameters": {},
        },
        "core": {
            "relative_gap": solve_options["relative_gap"],
            "absolute_gap": solve_options["absolute_gap"],
            "feasibility_tolerance": solve_options["feasibility_tolerance"],
            "integrality_tolerance": solve_options["integrality_tolerance"],
        },
    }
    effective_options = {
        "time_limit_seconds": None, "threads": None, "random_seed": None, "warm_start": None, "parameters": {},
    }
    option_set_statuses = {
        "time_limit_seconds": "submitted_no_readback",
        "threads": "submitted_no_readback",
        "random_seed": "submitted_no_readback",
        "warm_start": "submitted_no_readback" if solve_options.get("warm_start", False) else "not_requested",
        "relative_gap": "core_criterion_only",
        "absolute_gap": "core_criterion_only",
        "feasibility_tolerance": "core_checker_only",
        "integrality_tolerance": "core_checker_only",
    }
    bound_evidence = None
    if bound_value is not None:
        direction = "lower" if objective_sense == "min" else "upper"
        bound_evidence = {
            "source": "synthetic_structural_fixture",
            "scope": "global",
            "objective_sense": objective_sense,
            "value_direction": direction,
            "interface": "test fixture",
            "objective_transform": {
                "kind": "identity",
                "formula": "fixture bound already uses original objective units",
            },
            "raw_value": bound_value,
        }
    return {
        "solver_invoked": True,
        "solver_returned": True,
        "termination": termination,
        "raw_status": "SYNTHETIC_FIXTURE",
        "raw_solution_value_valid": True,
        "best_feasible_objective": objective_value,
        "best_objective_bound": bound_value,
        "bound_evidence": bound_evidence,
        "termination_condition": termination,
        "solve_wall_seconds": 0.001,
        "error": None,
        "requested_options": requested_options,
        "effective_options": effective_options,
        "option_set_statuses": option_set_statuses,
        "core_gap_criterion": {
            "relative_gap": solve_options["relative_gap"],
            "absolute_gap": solve_options["absolute_gap"],
            "formula": "gap <= max(absolute_gap, relative_gap * abs(checked_objective))",
            "source": "frozen_core_check",
        },
    }


def _synthetic_reviewable_batch(
    core,
    *,
    model_inside_artifact_dir: bool = False,
    model_inside_case_code: bool = False,
    model_case_relative_path: str | None = None,
    business_checker_path_override: str | None = None,
    business_checker_uses_model_path: bool = False,
    requirement_source_path: str | None = None,
    requirement_source_bytes: bytes | None = None,
    model_source: str | None = None,
    business_checker_source: str | None = None,
    module_sources: dict[str, str] | None = None,
    large_json_outputs: bool = False,
    nonclosed_second_result: bool = False,
    native_objective_delta: float | None = None,
    business_objective_delta: float | None = None,
    business_objective_values_override: list[dict] | None = None,
    objective_spec_override: dict | None = None,
    result_specs_override: list[tuple[list[str], float, str, float]] | None = None,
    complete_batch: bool = False,
    business_status_override: str | None = None,
    business_solution=_NO_BUSINESS_SOLUTION,
    native_json_settings: dict | None = None,
    monkeypatch=None,
    inspect: bool = True,
):
    """Persist validator-shaped artifacts; this fixture never invokes a solver."""
    from optagent_core.api import dispatch_dynamic
    from optagent_core.data import load_data_bundle
    from optagent_core.results import _result_numerical_summary, checked_global_bound_gap
    from optagent_core.runner import _check_instance_identity, verify_runtime_bundle

    case_id, _, _ = core["create_case"](raw=b"Minimize cost for each named site.")
    store = core["store"]()
    initial_state = store.read_case(case_id)
    if requirement_source_path is not None:
        assert requirement_source_bytes is not None
        source_path = f"{initial_state['artifact_dir']}/{requirement_source_path}"
        source_file = core["workspace"].joinpath(*source_path.split("/"))
        source_file.parent.mkdir(parents=True, exist_ok=True)
        source_file.write_bytes(requirement_source_bytes)
        captured = core["call"]("optagent.data", {
            "op": "capture", "case_id": case_id, "expected_state_revision": initial_state["state_revision"],
            "path": source_path, "purpose": "requirement", "locations": ["user environment constraint"],
        })
        assert captured["ok"], captured
    if model_case_relative_path is not None:
        model_path = f"{initial_state['artifact_dir']}/{model_case_relative_path}"
    elif model_inside_case_code:
        model_path = f"{initial_state['artifact_dir']}/code/model.py"
    elif model_inside_artifact_dir:
        model_path = f"{initial_state['artifact_dir']}/model.py"
    else:
        model_path = "model.py"
    (core["workspace"] / "inputs").mkdir()
    sites_csv = b"site,cost\nA,10\nB,5\nC,1\n" if not complete_batch else b"site,cost\nA,10\n"
    (core["workspace"] / "inputs" / "sites.csv").write_bytes(sites_csv)
    state = store.read_case(case_id)
    captured = core["call"]("optagent.data", {
        "op": "capture", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "path": "inputs/sites.csv", "purpose": "data", "locations": ["rows A through C"],
    })
    assert captured["ok"], captured
    source_ids = [captured["result"]["source"]["id"]]
    plan = _csv_data_plan(source_ids[0])
    if native_json_settings is not None:
        settings_path = core["workspace"] / "inputs" / "settings.json"
        settings_path.write_text(json.dumps(native_json_settings), encoding="utf-8")
        state = store.read_case(case_id)
        settings_capture = core["call"]("optagent.data", {
            "op": "capture", "case_id": case_id, "expected_state_revision": state["state_revision"],
            "path": "inputs/settings.json", "purpose": "data", "locations": ["native JSON settings"],
        })
        assert settings_capture["ok"], settings_capture
        settings_source_id = settings_capture["result"]["source"]["id"]
        source_ids.append(settings_source_id)
        plan["blocks"].append({
            "object_id": "settings",
            "source_id": settings_source_id,
            "format": "json",
            "layout": {"pointer": ""},
            "kind": "json",
            "key_fields": [],
            "columns": [],
            "duplicate_policy": "error",
            "missing_policy": "error",
        })
    state = store.read_case(case_id)
    normalized = core["call"]("optagent.data", {
        "op": "normalize", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "source_ids": source_ids,
        "plan": plan,
    })
    assert normalized["ok"], normalized

    model_file = core["workspace"].joinpath(*model_path.split("/"))
    model_file.parent.mkdir(parents=True, exist_ok=True)
    model_file.write_text(
        model_source
        if model_source is not None
        else "def build_model(bundle):\n    raise RuntimeError('the structural fixture never imports this candidate')\n",
        encoding="utf-8",
    )
    module_paths: list[str] = []
    for module_path, module_source in (module_sources or {}).items():
        normalized_module_path = module_path.replace("\\", "/")
        module_file = core["workspace"].joinpath(*normalized_module_path.split("/"))
        module_file.parent.mkdir(parents=True, exist_ok=True)
        module_file.write_text(module_source, encoding="utf-8")
        module_paths.append(normalized_module_path)
    business_checker_path = None
    if (
        business_checker_source is not None
        or business_checker_path_override is not None
        or business_objective_delta is not None
        or business_solution is not _NO_BUSINESS_SOLUTION
        or native_json_settings is not None
    ):
        if business_checker_source is not None:
            checker_source = business_checker_source
        elif native_json_settings is not None:
            checker_source = (
                "def check_solution(bundle, solution):\n"
                "    settings = bundle.json('settings')\n"
                "    cost = solution.get('cost') if isinstance(solution, dict) else None\n"
                "    comparable = isinstance(cost, (int, float)) and not isinstance(cost, bool)\n"
                "    matches_fixture_source = 'solution' not in settings or solution == settings['solution']\n"
                "    status = 'fail' if not matches_fixture_source else ('pass' if comparable else 'uncovered')\n"
                "    return {\n"
                "        'status': status,\n"
                "        'checked_obligations': [],\n"
                "        'objective_values': [{'component': 'cost', 'sense': 'min', 'unit': 'USD', 'value': cost}] if comparable else [],\n"
                "        'violations': [] if matches_fixture_source else ['solution_differs_from_fixture_source'],\n"
                "        'uncovered': [] if comparable or not matches_fixture_source else ['solution_objective_not_in_business_schema'],\n"
                "    }\n"
            )
        elif business_solution is _NO_BUSINESS_SOLUTION:
            checker_source = (
                "def check_solution(bundle, solution):\n"
                "    current_key = bundle.instance_context.instance_key if bundle.instance_context else ()\n"
                "    table = bundle.table('sites')\n"
                "    site = solution.get('site') if isinstance(solution, dict) else None\n"
                "    row = table.get((site,))\n"
                "    valid = len(current_key) == 1 and site == current_key[0] and row is not None and solution.get('cost') == row['cost']\n"
                "    return {\n"
                "        'status': 'pass' if valid else 'fail',\n"
                "        'checked_obligations': [],\n"
                "        'objective_values': [{'component': 'cost', 'sense': 'min', 'unit': 'USD', 'value': row['cost']}] if valid else [],\n"
                "        'violations': [] if valid else ['site_cost_or_instance_mismatch'],\n"
                "        'uncovered': [],\n"
                "    }\n"
            )
        else:
            checker_source = (
                f"STRUCTURED_SOLUTION = {business_solution!r}\n"
                "def check_solution(bundle, solution):\n"
                "    cost = solution.get('cost') if isinstance(solution, dict) else None\n"
                "    comparable = isinstance(cost, (int, float)) and not isinstance(cost, bool)\n"
                "    matches_source_solution = solution == STRUCTURED_SOLUTION\n"
                "    valid = matches_source_solution and comparable\n"
                "    return {\n"
                "        'status': 'pass' if valid else ('fail' if not matches_source_solution else 'uncovered'),\n"
                "        'checked_obligations': [],\n"
                "        'objective_values': [{'component': 'cost', 'sense': 'min', 'unit': 'USD', 'value': cost}] if valid else [],\n"
                "        'violations': [] if matches_source_solution else ['solution_differs_from_fixture_source'],\n"
                "        'uncovered': [] if comparable or not matches_source_solution else ['solution_objective_not_in_business_schema'],\n"
                "    }\n"
            )
        business_checker_path = (
            model_path
            if business_checker_uses_model_path
            else business_checker_path_override or "business_checker.py"
        )
        if business_checker_path != model_path:
            checker_file = core["workspace"].joinpath(*business_checker_path.split("/"))
            checker_file.parent.mkdir(parents=True, exist_ok=True)
            checker_file.write_text(checker_source, encoding="utf-8")
    state = store.read_case(case_id)
    objective_spec = objective_spec_override or {
        "component": "cost",
        "sense": "min",
        "unit": "USD",
    }
    candidate = core["call"]("optagent.case", {
        "op": "revise", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "candidate", "model_path": model_path, "local_module_paths": module_paths,
        "data_id": normalized["result"]["data_manifest"]["data_id"],
        "instance_parameter_object_id": "sites", "backend_id": "highs",
        "objective_plan": {
            "mode": "scalar", "objective_component": objective_spec["component"],
            "sense": objective_spec["sense"], "unit": objective_spec["unit"],
            "preference_note": "structural evidence contract fixture",
        },
        "business_checker_path": business_checker_path, "input_closure": "declared", "closure_note": "Fixture only.",
    })
    assert candidate["ok"], candidate
    candidate_id = candidate["result"]["candidate"]["id"]
    state = store.read_case(case_id)
    solve_options = {
        "execution_budget_seconds": 60, "time_limit_seconds": 30, "relative_gap": 0.0,
        "absolute_gap": 0.0, "threads": 1, "random_seed": 0,
        "feasibility_tolerance": 1e-7, "integrality_tolerance": 1e-7,
        "write_log": False, "export_model": False,
    }
    if native_objective_delta is not None:
        solve_options["absolute_gap"] = 1e-8
    prepared_reply = core["call"]("optagent.run", {
        "op": "prepare", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "candidate_id": candidate_id, "solve_options": solve_options,
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
    run, _run_ref = read_run(store, case_id, run_id)
    prepared = store.read_record(f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/prepared.json")
    manifest = strict_json_loads(store.read_ref(prepared["input_manifest"]))
    data = manifest["data"]
    bundle = load_data_bundle(
        store,
        data["manifest"],
        candidate_binding_digest=manifest["binding_digest"],
        source_records=read_prepared_source_records(store, manifest["sources"]),
    )
    checker_spec = manifest["candidate"]["business_checker"]
    output_root = f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output"
    output_refs = []
    large_json_paths = {
        "instances/000000/layer-single/model-check.json",
        "instances/000000/layer-single/business-check.json",
        "instances/000000/layer-single/result.json",
        "batch-summary.json",
    }

    def put_json(relative_path, value):
        raw = canonical_json_bytes(value)
        if large_json_outputs and relative_path in large_json_paths:
            raw = b" " * (4 * 1024 * 1024 + 7) + raw
        ref = store.write_immutable(
            f"{output_root}/{relative_path}", raw, "application/json"
        )
        output_refs.append(ref)
        return ref

    result_specs = result_specs_override or [(["A"], 10.0, "time_limit", 0.0), (["B"], 5.0, "optimal", 0.0)]
    if nonclosed_second_result:
        result_specs[1] = (["B"], 5.0, "time_limit", 1.0)
    result_refs = []
    for index, (key, objective_value, termination, gap_delta) in enumerate(result_specs):
        binding_digest = _check_instance_identity(bundle, manifest, key)
        stem = f"instances/{index:06d}/layer-single"
        objective = {
            **objective_spec,
            "value": objective_value,
        }
        check_record = {
            "input_digest": prepared["input_digest"], "instance_key": key,
            "instance_binding_digest": binding_digest, "status": "pass",
            "variable_coverage": {"required": 0, "present": 0, "completed": 0, "missing_count": 0},
            "component_coverage": [], "violations": None, "objective_values": [objective],
            "tolerances": {"feasibility": 1e-7, "integrality": 1e-7}, "uncovered": [],
        }
        check_ref = put_json(f"{stem}/model-check.json", check_record)
        witness = {
            "input_digest": prepared["input_digest"], "instance_key": key,
            "instance_binding_digest": binding_digest, "variables": [], "objective_values": [objective],
        }
        witness_ref = put_json(f"{stem}/witness.json", witness)
        if checker_spec is None:
            business_record = {
                "input_digest": prepared["input_digest"], "instance_key": key,
                "instance_binding_digest": binding_digest, "status": "uncovered",
                "checked_obligations": [],
                "objective_values": business_objective_values_override if business_objective_values_override is not None else [objective],
                "violations": [],
                "uncovered": ["business_checker_not_provided"],
            }
        else:
            if business_solution is not _NO_BUSINESS_SOLUTION:
                saved_solution = business_solution
            elif native_json_settings is not None and "solution" in native_json_settings:
                saved_solution = native_json_settings["solution"]
            else:
                saved_solution = {"site": key[0], "cost": objective_value}
            checker_objective = objective_value + (business_objective_delta or 0.0)
            status = business_status_override or ("fail" if business_objective_delta is not None else "pass")
            business_record = {
                "input_digest": prepared["input_digest"],
                "instance_key": key,
                "instance_binding_digest": binding_digest,
                "status": status,
                "checked_obligations": [],
                "objective_values": (
                    business_objective_values_override
                    if business_objective_values_override is not None
                    else [{**objective, "value": checker_objective}]
                ),
                "violations": ["business_checker_objective_mismatch"] if business_objective_delta is not None else [],
                "uncovered": [],
                "solution": saved_solution,
            }
        put_json(f"{stem}/business-check.json", business_record)
        raw_primals_ref = store.write_immutable(
            f"{output_root}/{stem}/raw-primals.jsonl", b"", "application/x-ndjson"
        )
        output_refs.append(raw_primals_ref)
        native_objective = objective_value + (
            native_objective_delta if native_objective_delta is not None and index == 1 else 0.0
        )
        bound_value = native_objective - gap_delta
        has_global_bound = termination == "optimal" or gap_delta > 0
        backend_observation = _schema3_fixture_backend_observation(
            solve_options,
            termination=termination,
            objective_sense="min",
            objective_value=native_objective,
            bound_value=bound_value if has_global_bound else None,
        )
        observation = {
            "observation": backend_observation,
            "raw_primals": raw_primals_ref,
        }
        observation_ref = put_json(f"{stem}/backend-observation.json", observation)
        bound = None
        gap = {"absolute": None, "relative": None, "definition": "no global bound"}
        if has_global_bound:
            strict_gap = checked_global_bound_gap(bound_value, "min", objective_value)
            if strict_gap is not None:
                bound = {
                    "value": bound_value, "direction": "lower", "scope": "global", "evidence": observation_ref,
                }
                absolute, relative = strict_gap
                gap = {"absolute": absolute, "relative": relative, "definition": "absolute and relative to checked objective"}
        numerical = _result_numerical_summary(
            check_record=check_record,
            objective_value=objective_value,
            objective_sense="min",
            bound=bound,
            absolute_gap=gap["absolute"],
            termination=termination,
            has_incumbent=True,
            requested_options=backend_observation["requested_options"],
            effective_options=backend_observation["effective_options"],
            core_gap_target={
                "relative_gap": solve_options["relative_gap"],
                "absolute_gap": solve_options["absolute_gap"],
            },
            native_observation=backend_observation,
        )
        if native_objective_delta is not None and index == 1:
            from optagent_core.results import CheckedModel, HighsObservation, ObjectiveSpec, result_record

            checked = CheckedModel(
                record=check_record,
                witness_variables=(),
                objective_values=(objective,),
                candidate_complete=True,
                raw_vector_complete=True,
            )
            native = HighsObservation(
                record=backend_observation,
                backend={
                    "id": "highs", "version": "structural-fixture", "adapter_version": "structural-fixture",
                    "python_version": "test-only", "package_versions": {"pyomo": "6.9.4", "highspy": "1.10.0"},
                },
                termination=termination,
                raw_status=backend_observation["raw_status"],
                solver_invoked=True,
                solver_returned=True,
                primals={},
                solver_incumbent_objective=native_objective,
                bound_value=bound_value if has_global_bound else None,
                nonfinite_fields={},
                solve_wall_seconds=0.001,
                error=None,
            )
            worker_result = result_record(
                native,
                checked,
                ObjectiveSpec("cost", "min", "USD", None, None),
                instance_key=key,
                instance_binding_digest=binding_digest,
                source_revision=prepared["source_revision"],
                candidate_revision=prepared["candidate_revision"],
                input_digest=prepared["input_digest"],
                layer=None,
                bound_evidence_ref=observation_ref,
                core_gap_target={
                    "relative_gap": solve_options["relative_gap"],
                    "absolute_gap": solve_options["absolute_gap"],
                },
            )
            result_payload = {
                **worker_result,
                "witness": witness_ref,
                "check": check_ref,
                "lexicographic": None,
            }
        else:
            result_payload = {
                "instance_key": key, "instance_binding_digest": binding_digest, "layer": None,
                "source_revision": prepared["source_revision"], "candidate_revision": prepared["candidate_revision"],
                "input_digest": prepared["input_digest"],
                "backend": {
                    "id": "highs", "version": "structural-fixture", "adapter_version": "structural-fixture",
                    "python_version": "test-only", "package_versions": {"pyomo": "6.9.4", "highspy": "1.10.0"},
                },
                "raw_status": "SYNTHETIC_FIXTURE", "termination": termination, "solver_invoked": True,
                "has_solver_incumbent": True, "has_checked_candidate": True, "witness": witness_ref,
                "objective": objective, "bound": bound, "gap": gap, "check": check_ref,
                "numerical": numerical,
                "lexicographic": None,
                "coverage": [], "warnings": ["synthetic_validator_fixture; no solver was run"],
            }
        result_ref = put_json(f"{stem}/result.json", result_payload)
        result_refs.append(result_ref)

    if complete_batch:
        completed_count = len(result_specs)
        put_json("batch-summary.json", {
            "schema_version": 1, "planned_instance_count": completed_count, "started_instance_count": completed_count,
            "instances_with_results": completed_count, "instances_with_all_layers_attempted": completed_count,
            "partial_instance_count": 0, "remaining_instance_count": 0, "stop_reason": None,
        })
    else:
        put_json("batch-summary.json", {
            "schema_version": 1, "planned_instance_count": 3, "started_instance_count": 2,
            "instances_with_results": 2, "instances_with_all_layers_attempted": 2,
            "partial_instance_count": 0, "remaining_instance_count": 1, "stop_reason": "execution_budget_reserved",
        })

    required_inputs = [prepared["input_manifest"], manifest["runtime_bundle"]["manifest"]]
    required_inputs.extend(ref for source in manifest["sources"] for ref in (source["record"], source["bytes"]))
    candidate_input = manifest["candidate"]
    required_inputs.extend([candidate_input["model"]["bytes"], *(item["bytes"] for item in candidate_input["local_modules"])])
    if candidate_input["business_checker"] is not None:
        required_inputs.append(candidate_input["business_checker"]["bytes"])
    data_manifest = strict_json_loads(store.read_ref(data["manifest"]))
    required_inputs.extend([data["manifest"], data_manifest["trace"], *(item["values"] for item in data_manifest["objects"])])
    _, runtime_refs = verify_runtime_bundle(
        store.workspace, manifest["runtime_bundle"]["digest"], manifest["runtime_bundle"]["manifest"]
    )
    required_inputs.extend(runtime_refs)
    required_inputs.append(run["worker_invocation"])
    inputs_by_path = {item["path"]: dict(item) for item in required_inputs}
    seal = {
        "schema_version": 2, "run_id": run_id, "prepared_id": prepared_id,
        "dispatch_nonce": run["dispatch_nonce"], "input_digest": prepared["input_digest"],
        "output_refs": sorted(output_refs, key=lambda item: item["path"]), "solver_returned": True,
        "worker_completed": complete_batch, "consumed_inputs": [inputs_by_path[path] for path in sorted(inputs_by_path)],
        "started_at": "2026-01-01T00:00:00Z", "completed_at": "2026-01-01T00:00:01Z",
    }
    store.write_immutable(f"{output_root}/seal.json", canonical_json_bytes(seal), "application/json")
    if inspect:
        inspected = core["call"]("optagent.run", {
            "op": "inspect", "case_id": case_id, "run_id": run_id, "after_instance": 0, "limit": 20,
        }, role="opt-verifier")
        assert inspected["ok"], inspected
        assert inspected["result"]["run"]["worker_status"] == ("complete" if complete_batch else "incomplete")
    return case_id, candidate_id, run_id, result_refs, store


def _select_fixture_run_with_feasibility_claim(core, case_id, candidate_id, run_id, result_refs):
    review_id = _core11_begin_note(core, case_id, candidate_id, [run_id])
    closed = _core11_close(core, case_id, review_id, [{
        "kind": "feasibility",
        "scope": "The selected checked result is feasible for its recorded instance.",
        "conditions": [],
        "evidence_refs": [result_refs[0]],
        "support": "supported",
    }])
    assert closed["ok"], closed
    state = core["store"]().read_case(case_id)
    selected = core["call"]("optagent.deliver", {
        "op": "select",
        "case_id": case_id,
        "expected_state_revision": state["state_revision"],
        "kind": "run",
        "candidate_id": candidate_id,
        "review_id": review_id,
        "proof_ids": [],
        "run_ids": [run_id],
    })
    assert selected["ok"], selected
    return selected


def test_proof_result_refs_are_verified_and_derive_run_dependencies(core):
    from optagent_core.delivery import _validate_proof_dependencies

    case_id, candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(
        core,
    )
    other_case_id, _, _ = core["create_case"](seq=2, raw=b"A separate case for a forged dependency.")
    other_case_path = f".reaslab/optagent/cases/{other_case_id}/forged/result.json"
    other_case_ref = store.write_immutable(other_case_path, b"{}", "application/json")
    unregistered_run_id = str(uuid.uuid4())
    unregistered_ref = store.write_immutable(
        f".reaslab/optagent/cases/{case_id}/runs/{unregistered_run_id}/output/instances/000000/layer-single/result.json",
        b"{}",
        "application/json",
    )
    unsealed_ref = store.write_immutable(
        f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/instances/999999/layer-single/result.json",
        b"{}",
        "application/json",
    )

    review_id = _core11_begin_note(core, case_id, candidate_id, [run_id])
    (core["workspace"] / "derived-proof.md").write_text(
        "The scoped mathematical conclusion is established by the submitted proof.", encoding="utf-8"
    )

    def submit_proof(evidence_refs, *, role=None, candidate=candidate_id):
        state = store.read_case(case_id)
        arguments = {
            "op": "submit",
            "case_id": case_id,
            "review_id": review_id,
            "expected_state_revision": state["state_revision"],
            "expected_review_revision": 2,
            "record_kind": "proof",
            "kind": "analytic",
            "body_path": "derived-proof.md",
            "premises": ["the original source defines this scope"],
            "evidence_refs": evidence_refs,
            "claim_kinds": ["optimality"],
            "candidate_id": candidate,
        }
        if role is None:
            return core["call"]("optagent.review", arguments)
        return core["call"]("optagent.review", arguments, role=role)

    denied_role = submit_proof([], role="opt-verifier")
    assert not denied_role["ok"] and denied_role["error"]["code"] == "permission_denied"

    forged_ref = {**result_refs[0], "sha256": "0" * 64}
    forged = submit_proof([forged_ref])
    assert not forged["ok"] and forged["error"]["code"] == "artifact_changed"

    cross_case = submit_proof([other_case_ref])
    assert not cross_case["ok"] and cross_case["error"]["code"] == "permission_denied"

    unregistered = submit_proof([unregistered_ref])
    assert not unregistered["ok"] and unregistered["error"]["code"] == "evidence_insufficient"

    outside_seal = submit_proof([unsealed_ref])
    assert not outside_seal["ok"] and outside_seal["error"]["code"] == "evidence_insufficient"
    assert store.read_case(case_id)["proof_ids"] == []

    valid = submit_proof([result_refs[0], result_refs[0]])
    assert valid["ok"], valid
    proof_id = valid["result"]["proof"]["id"]
    proof_ref = valid["result"]["proof"]["record_ref"]
    proof_record = store.read_record(
        f".reaslab/optagent/cases/{case_id}/proofs/{proof_id}.json"
    )
    assert proof_record["evidence_refs"] == [result_refs[0], result_refs[0]]
    assert proof_record["old_run_ids"] == [run_id]

    current_review_ready = _core11_close(core, case_id, review_id, [{
        "kind": "optimality",
        "scope": "The submitted analytic proof establishes the stated optimum.",
        "conditions": [],
        "evidence_refs": [proof_ref],
        "support": "supported",
    }])
    assert current_review_ready["ok"], current_review_ready
    assert _validate_proof_dependencies(store, store.read_case(case_id), proof_record, {})

    legacy_empty_allowlist = {**proof_record, "old_run_ids": []}
    with pytest.raises(ContractError, match="registered Proof dependency"):
        _validate_proof_dependencies(store, store.read_case(case_id), legacy_empty_allowlist, {})
    assert legacy_empty_allowlist["old_run_ids"] == []

    historical_candidate_id = _core11_register_candidate(
        core, case_id, model_path="core11-historical-proof-builder.py"
    )
    historical_review_id = _core11_begin_note(
        core, case_id, historical_candidate_id, [run_id]
    )
    (core["workspace"] / "historical-proof.md").write_text(
        "This current proof explicitly depends on a sealed Result from the earlier Candidate.",
        encoding="utf-8",
    )
    state = store.read_case(case_id)
    historical = core["call"]("optagent.review", {
        "op": "submit", "case_id": case_id, "review_id": historical_review_id,
        "expected_state_revision": state["state_revision"], "expected_review_revision": 2,
        "record_kind": "proof", "kind": "analytic", "body_path": "historical-proof.md",
        "premises": ["The old Run remains a valid historical dependency."],
        "evidence_refs": [result_refs[0]], "claim_kinds": ["optimality"],
        "candidate_id": historical_candidate_id,
    })
    assert historical["ok"], historical
    historical_proof_id = historical["result"]["proof"]["id"]
    historical_proof_ref = historical["result"]["proof"]["record_ref"]
    historical_record = store.read_record(
        f".reaslab/optagent/cases/{case_id}/proofs/{historical_proof_id}.json"
    )
    assert historical_record["candidate_id"] == historical_candidate_id
    assert historical_record["old_run_ids"] == [run_id]
    historical_ready = _core11_close(core, case_id, historical_review_id, [{
        "kind": "optimality",
        "scope": "The current proof supports the stated conclusion using its exact historical Result dependency.",
        "conditions": [],
        "evidence_refs": [historical_proof_ref],
        "support": "supported",
    }])
    assert historical_ready["ok"], historical_ready


def test_editable_export_reuses_one_file_for_model_module_and_checker_roles(core, tmp_path):
    model_source = (
        "from pyomo.environ import ConcreteModel, Constraint, Objective, Var, minimize\n"
        "MODEL_FACTOR = 2\n"
        "def build_model(bundle):\n"
        "    model = ConcreteModel()\n"
        "    minimum = bundle.json('settings')['minimum']\n"
        "    model.x = Var(bounds=(minimum, 10))\n"
        "    model.cost = Objective(expr=MODEL_FACTOR * model.x, sense=minimize)\n"
        "    return model\n"
        "def extract_solution(bundle, witness):\n"
        "    return {\n"
        "        'model_factor': MODEL_FACTOR,\n"
        "        'x': next(row['value'] for row in witness['variables'] if row['component'] == 'x'),\n"
        "    }\n"
        "def check_solution(bundle, solution):\n"
        "    minimum = bundle.json('settings')['minimum']\n"
        "    return {\n"
        "        'status': 'pass' if solution['model_factor'] == MODEL_FACTOR and solution['x'] >= minimum else 'fail',\n"
        "        'checked_obligations': [],\n"
        "        'objective_values': [{'component': 'cost', 'sense': 'min', 'unit': 'USD', 'value': MODEL_FACTOR * solution['x']}],\n"
        "        'violations': [],\n"
        "        'uncovered': [],\n"
        "    }\n"
    )
    case_id, candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(
        core,
        model_source=model_source,
        module_sources={"model.py": model_source},
        business_checker_uses_model_path=True,
        native_json_settings={"minimum": 3.0},
    )

    selected = _select_fixture_run_with_feasibility_claim(
        core, case_id, candidate_id, run_id, result_refs
    )
    editable = selected["result"]["editable_case"]
    assert editable["status"] == "generated", editable
    artifact_dir = store.read_case(case_id)["artifact_dir"]
    case_root = core["workspace"].joinpath(*artifact_dir.split("/"))
    run_config = json.loads((case_root / ".optagent" / "run.json").read_text(encoding="utf-8"))
    assert run_config["model_path"] == run_config["business_checker_path"]
    assert run_config["local_modules"] == [run_config["model_path"]]
    assert run_config["code_import_paths"] == {run_config["model_path"]: "model.py"}
    assert (case_root / run_config["model_path"]).read_text(encoding="utf-8") == model_source

    solved = subprocess.run(
        [sys.executable, str(case_root / "run.py"), "solve"],
        cwd=case_root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert solved.returncode == 0, solved.stderr + solved.stdout
    assert json.loads(solved.stdout)["status"] == "completed"
    checked = subprocess.run(
        [sys.executable, str(case_root / "run.py"), "check"],
        cwd=case_root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert checked.returncode == 0, checked.stderr + checked.stdout
    assert json.loads(checked.stdout)["checks_passed"] is True
    check_record = json.loads(
        (case_root / "results" / "check" / ".optagent" / "check.json").read_text(encoding="utf-8")
    )
    assert check_record["results"][0]["current_business_check"]["solution"] == {
        "model_factor": 2,
        "x": 3.0,
    }


def test_editable_export_still_rejects_distinct_paths_with_a_colliding_target(core):
    shared_source = (
        "from pyomo.environ import ConcreteModel, Objective, Var, minimize\n"
        "def build_model(bundle):\n"
        "    model = ConcreteModel()\n"
        "    model.x = Var(bounds=(0, 1))\n"
        "    model.cost = Objective(expr=model.x, sense=minimize)\n"
        "    return model\n"
        "def extract_solution(bundle, witness):\n"
        "    return {'x': next(row['value'] for row in witness['variables'] if row['component'] == 'x')}\n"
        "def check_solution(bundle, solution):\n"
        "    return {\n"
        "        'status': 'pass',\n"
        "        'checked_obligations': [],\n"
        "        'objective_values': [{'component': 'cost', 'sense': 'min', 'unit': 'USD', 'value': solution['x']}],\n"
        "        'violations': [],\n"
        "        'uncovered': [],\n"
        "    }\n"
    )
    case_id, candidate_id, run_id, result_refs, _store = _synthetic_reviewable_batch(
        core,
        model_case_relative_path="code/external/business_checker.py",
        model_source=shared_source,
        business_checker_source=shared_source,
    )

    selected = _select_fixture_run_with_feasibility_claim(
        core, case_id, candidate_id, run_id, result_refs
    )

    editable = selected["result"]["editable_case"]
    assert editable["status"] == "not_generated"
    assert editable["reason"] == "projection_failed"
    assert editable["details"]["error_code"] == "package_collision"
    assert editable["details"]["message"] == "selected code paths map to the same visible case file"


def test_worker_business_mismatch_is_sealed_consumed_and_rejected_for_positive_claim(core, monkeypatch):
    from optagent_core.delivery import _result_supports_claim

    case_id, _candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(
        core, business_objective_delta=1e-4, monkeypatch=monkeypatch,
    )
    run, _ = read_run(store, case_id, run_id)
    assert run["worker"]["status"] == "incomplete"
    assert run["results"] == result_refs
    business_path = result_refs[0]["path"].rsplit("/", 1)[0] + "/business-check.json"
    business_ref = store.ref_for_path(business_path)
    business_raw = store.read_ref(business_ref)
    business = strict_json_loads(business_raw, max_bytes=len(business_raw))
    assert business["status"] == "fail"
    assert business["objective_values"][0]["value"] == 10.0001
    assert "business_checker_objective_mismatch" in business["violations"]

    result_raw = store.read_ref(result_refs[0])
    result = strict_json_loads(result_raw, max_bytes=len(result_raw))
    assert _result_supports_claim(
        store, store.read_case(case_id), result, result_refs[0], "feasibility", {}, run_id,
        claim_support="supported", claim_conditions=[],
    ) is False


def test_not_closed_result_still_supports_feasibility_and_bound_but_not_optimality(core):
    from optagent_core.delivery import _result_supports_claim

    case_id, _candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(
        core,
        nonclosed_second_result=True,
    )
    result_raw = store.read_ref(result_refs[1])
    result = strict_json_loads(result_raw, max_bytes=len(result_raw))
    assert result["termination"] == "time_limit"
    assert result["numerical"]["optimality"]["status"] == "not_closed"
    assert result["gap"]["absolute"] == 1.0

    def supports(kind, support="supported", conditions=()):
        return _result_supports_claim(
            store,
            store.read_case(case_id),
            result,
            result_refs[1],
            kind,
            {},
            run_id,
            claim_support=support,
            claim_conditions=conditions,
        )

    assert supports("feasibility") is True
    assert supports("bound") is True
    assert supports(
        "optimality",
        support="conditional",
        conditions=["The checked global gap meets the effective tolerance."],
    ) is False


@pytest.mark.parametrize("status,should_reject", [("pass", True), ("fail", False), ("uncovered", False)])
def test_runner_rejects_passing_objective_mismatch_but_preserves_nonpassing_s(core, monkeypatch, status, should_reject):
    case_id, _candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(
        core,
        business_objective_delta=1e-4,
        business_status_override=status,
        monkeypatch=monkeypatch,
        inspect=False,
    )
    inspected = core["call"](
        "optagent.run",
        {"op": "inspect", "case_id": case_id, "run_id": run_id, "after_instance": 0, "limit": 20},
        role="opt-verifier",
    )
    if should_reject:
        assert not inspected["ok"]
        assert inspected["error"]["code"] == "artifact_changed"
        assert inspected["error"]["details"]["run"]["id"] == run_id
        assert inspected["error"]["details"]["run"]["worker_status"] == "invalid"
        assert inspected["error"]["effect"] == "local_committed"
    else:
        assert inspected["ok"], inspected
        assert inspected["result"]["run"]["worker_status"] == "incomplete"
        business = json.loads(
            store.read_ref(store.ref_for_path(result_refs[0]["path"].rsplit("/", 1)[0] + "/business-check.json"))
        )
        assert business["status"] == status
        assert business["solution"] == {"site": "A", "cost": 10.0}
        assert business["objective_values"][0]["value"] == pytest.approx(10.0001)
        result = strict_json_loads(store.read_ref(result_refs[0]))
        assert result["objective"]["value"] == 10.0


@pytest.mark.parametrize(
    "status,objective_values",
    [
        ("pass", []),
        ("pass", [{"component": "cost", "sense": "min", "unit": "bad-unit", "value": 10.0}]),
        ("fail", [{"component": "cost", "sense": "min", "unit": "USD", "value": True}]),
        ("uncovered", [{"component": "other", "sense": "min", "unit": "USD", "value": 10.0}]),
        (
            "uncovered",
            [
                {"component": "cost", "sense": "min", "unit": "USD", "value": 10.0},
                {"component": "cost", "sense": "min", "unit": "USD", "value": 10.0},
            ],
        ),
    ],
)
def test_inspect_rejects_missing_or_malformed_objective_rows(core, status, objective_values):
    case_id, _candidate_id, run_id, _result_refs, _store = _synthetic_reviewable_batch(
        core,
        business_objective_delta=0.0,
        business_status_override=status,
        business_objective_values_override=objective_values,
        inspect=False,
    )
    inspected = core["call"](
        "optagent.run",
        {"op": "inspect", "case_id": case_id, "run_id": run_id, "after_instance": 0, "limit": 20},
        role="opt-verifier",
    )

    assert not inspected["ok"]
    assert inspected["error"]["code"] == "output_invalid"
    assert inspected["error"]["effect"] == "local_committed"
    assert inspected["error"]["details"]["run"]["worker_status"] == "invalid"


def test_archived_e001_uncovered_empty_objectives_survive_native_run_inspect(core):
    # Minimal safe-field snapshot from the archived E-block Run 51907 worker
    # outputs. The replay rebuilds a consistent CaseStore identity around
    # these fields; it does not claim to inspect the old archive in place.
    objective = {
        "component": "total_expected_cost",
        "sense": "min",
        "unit": "cost",
        "value": 180449.3479888,
    }
    archived = {
        "business_check": {"status": "uncovered", "objective_values": []},
        "model_check": {"status": "pass", "objective_values": [objective]},
        "result": {
            "solver_invoked": True,
            "termination": "time_limit",
            "objective": objective,
        },
    }
    objective = archived["result"]["objective"]
    assert archived["business_check"] == {"status": "uncovered", "objective_values": []}
    assert archived["model_check"]["status"] == "pass"
    assert archived["result"]["solver_invoked"] is True
    assert archived["model_check"]["objective_values"] == [objective]

    case_id, _candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(
        core,
        result_specs_override=[(["A"], objective["value"], "time_limit", 0.0)],
        objective_spec_override={
            "component": objective["component"],
            "sense": objective["sense"],
            "unit": objective["unit"],
        },
        business_status_override=archived["business_check"]["status"],
        business_objective_values_override=archived["business_check"]["objective_values"],
        complete_batch=True,
        inspect=False,
    )
    inspected = core["call"](
        "optagent.run",
        {"op": "inspect", "case_id": case_id, "run_id": run_id, "after_instance": 0, "limit": 20},
        role="opt-verifier",
    )

    assert inspected["ok"], json.dumps(inspected, ensure_ascii=False, sort_keys=True)
    run = inspected["result"]["run"]
    assert run["worker_status"] == "complete"
    assert run["result_count"] == 1
    result_ref = inspected["result"]["result_refs"][0]
    result = strict_json_loads(store.read_ref(result_ref))
    business_path = result_ref["path"].rsplit("/", 1)[0] + "/business-check.json"
    model_path = result_ref["path"].rsplit("/", 1)[0] + "/model-check.json"
    business = strict_json_loads(store.read_ref(store.ref_for_path(business_path)))
    model = strict_json_loads(store.read_ref(store.ref_for_path(model_path)))
    assert business["status"] == "uncovered" and business["objective_values"] == []
    assert model["status"] == "pass" and model["objective_values"] == [objective]
    assert result["objective"] == objective


def test_reviewed_result_selection_and_report_project_the_selected_partial_batch_result(core):
    """Structural WorkerSeal fixture exercises real gates; no solver is invoked."""
    case_id, candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(
        core, model_inside_artifact_dir=True
    )
    state = store.read_case(case_id)

    begun = core["call"]("optagent.review", {
        "op": "begin", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "candidate_id": candidate_id, "proof_ids": [], "run_ids": [run_id],
    }, role="opt-verifier")
    assert begun["ok"], begun
    review_id = begun["result"]["review"]["id"]
    state = store.read_case(case_id)
    source_id = state["source_ids"][0]
    note = core["call"]("optagent.review", {
        "op": "note", "case_id": case_id, "review_id": review_id, "expected_review_revision": 1,
        "body": "The frozen source and candidate define the reviewed instance claims.",
        "source_locations": [{"source_id": source_id, "locator": "source-level"}],
    }, role="opt-verifier")
    assert note["ok"], note

    feasibility = {
        "kind": "feasibility", "scope": "The checked candidate for site A is feasible.",
        "conditions": [], "evidence_refs": [result_refs[0]], "support": "supported",
    }
    optimality = {
        "kind": "optimality", "scope": "The checked objective for site B meets its bound.",
        "conditions": ["The checked objective equals the independently verified global bound."],
        "evidence_refs": [result_refs[1]], "support": "conditional",
    }
    closed = _core11_close(
        core,
        case_id,
        review_id,
        [feasibility, optimality],
        report_path="reports/final-review.md",
    )
    assert closed["ok"], closed

    # A later Proof is registered after this review note. Its support cannot
    # hide a time-limited Result in the complete numerical evidence set.
    state = store.read_case(case_id)
    second_begun = core["call"]("optagent.review", {
        "op": "begin", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "candidate_id": candidate_id, "proof_ids": [], "run_ids": [run_id],
    }, role="opt-verifier")
    assert second_begun["ok"], second_begun
    second_review_id = second_begun["result"]["review"]["id"]
    state = store.read_case(case_id)
    second_note = core["call"]("optagent.review", {
        "op": "note", "case_id": case_id, "review_id": second_review_id,
        "expected_review_revision": 1, "body": "Review all linked numerical observations before closing.",
        "source_locations": [{"source_id": state["source_ids"][0], "locator": "source-level"}],
    }, role="opt-verifier")
    assert second_note["ok"], second_note
    (core["workspace"] / "analytic-result.md").write_text(
        "The scoped objective conclusion is recorded for the verifier to assess.", encoding="utf-8"
    )
    state = store.read_case(case_id)
    proof = core["call"]("optagent.review", {
        "op": "submit", "case_id": case_id, "review_id": second_review_id,
        "expected_state_revision": state["state_revision"], "expected_review_revision": 2,
        "record_kind": "proof", "kind": "analytic", "body_path": "analytic-result.md",
        "premises": ["The frozen candidate objective is the claim's target."],
        "evidence_refs": [], "claim_kinds": ["optimality"],
        "candidate_id": candidate_id,
    })
    assert proof["ok"], proof
    proof_ref = proof["result"]["proof"]["record_ref"]
    unsupported_joint = {
        "kind": "optimality", "scope": "Both selected Results prove optimality.",
        "conditions": ["The proof establishes the global optimum for the scoped instance."],
        "evidence_refs": [proof_ref, *result_refs], "support": "conditional",
    }
    rejected = _core11_close(core, case_id, second_review_id, [unsupported_joint])
    assert not rejected["ok"] and rejected["error"]["code"] == "evidence_insufficient"
    assert "independently support optimality" in rejected["error"]["message"]
    assert "a matching Proof cannot cover an unsupported Result" in rejected["error"]["message"]
    assert rejected["error"]["pointer"] == "/claims/0/evidence_refs"
    assert rejected["error"]["details"] == {
        "claim_index": 0,
        "required_support": "checked_result_supports_optimality",
    }
    assert rejected["error"]["effect"] == "none"
    assert store.read_family_record(case_id, "reviews", second_review_id)[0]["status"] == "open"

    missing_conditional = {
        "kind": "optimality", "scope": "The matching Proof and time-limited Result are reviewed without conditions.",
        "conditions": [], "evidence_refs": [proof_ref, result_refs[0]], "support": "supported",
    }
    rejected_missing_conditional = _core11_close(core, case_id, second_review_id, [missing_conditional])
    assert not rejected_missing_conditional["ok"]
    assert rejected_missing_conditional["error"]["code"] == "evidence_insufficient"
    assert rejected_missing_conditional["error"]["pointer"] == "/claims/0/support"
    assert rejected_missing_conditional["error"]["details"] == {
        "claim_index": 0,
        "required_support": "conditional_with_nonempty_conditions",
    }
    assert "conditional support" in rejected_missing_conditional["error"]["message"]
    assert "nonempty conditions" in rejected_missing_conditional["error"]["message"]
    assert "unsupported Result" not in rejected_missing_conditional["error"]["message"]

    later_optimality = {**optimality, "evidence_refs": [proof_ref, result_refs[1]]}
    closed_later = _core11_close(
        core,
        case_id,
        second_review_id,
        [later_optimality],
        report_path="reports/final-review.md",
    )
    assert closed_later["ok"], closed_later
    state = store.read_case(case_id)
    selected = core["call"]("optagent.deliver", {
        "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "run", "candidate_id": candidate_id,
        "review_id": review_id, "proof_ids": [], "run_ids": [run_id],
    })
    assert selected["ok"], selected
    artifact_dir = store.read_case(case_id)["artifact_dir"]
    case_root = core["workspace"].joinpath(*artifact_dir.split("/"))

    state = store.read_case(case_id)
    built = core["call"]("optagent.deliver", {
        "op": "build", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "delivery_id": selected["result"]["delivery"]["id"], "target_path": "reports/selected-result.json",
    })
    assert built["ok"], built
    summary = strict_json_loads(store.read_ref(built["result"]["summary_ref"]))
    projected = summary["result_evidence"]
    assert len(projected) == 2
    projected_by_instance = {tuple(item["instance_key"]): item for item in projected}
    assert set(projected_by_instance) == {("A",), ("B",)}
    projected_a = projected_by_instance[("A",)]
    projected_b = projected_by_instance[("B",)]
    assert projected_a["claim_scope"] == feasibility["scope"]
    assert projected_a["result_ref"] == result_refs[0]
    assert projected_a["objective"]["value"] == 10.0
    assert projected_a["backend"]["id"] == "highs"
    assert projected_a["model_check"]["status"] == "pass"
    assert projected_a["business_check"]["status"] == "uncovered"
    assert projected_a["batch"]["planned_instance_count"] == 3
    assert projected_a["batch"]["remaining_instance_count"] == 1
    assert projected_a["termination"] == "time_limit"
    assert projected_b["claim_scope"] == optimality["scope"]
    assert projected_b["result_ref"] == result_refs[1]
    assert projected_b["objective"]["value"] == 5.0
    assert projected_b["model_check"]["status"] == "pass"
    assert projected_b["batch"]["planned_instance_count"] == 3
    assert projected_b["batch"]["remaining_instance_count"] == 1

    state = store.read_case(case_id)
    markdown = core["call"]("optagent.deliver", {
        "op": "build", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "delivery_id": selected["result"]["delivery"]["id"], "target_path": "reports/selected-result.md",
    })
    assert markdown["ok"], markdown
    body = (core["workspace"] / "reports" / "selected-result.md").read_text(encoding="utf-8")
    assert "Objective sense: min" in body
    assert "Model variable coverage: {\"completed\":0,\"missing_count\":0,\"present\":0,\"required\":0}" in body
    assert "Model component coverage: []" in body
    assert "Model uncovered items: []" in body
    assert "Business checked obligations: []" in body
    assert 'Business uncovered items: ["business_checker_not_provided"]' in body

    from optagent_core.delivery import _render_report

    null_projection = dict(projected_a)
    null_projection["objective"] = {"value": 0, "unit": None, "sense": None}
    null_projection["model_check"] = {
        "status": None, "variable_coverage": None, "component_coverage": None, "uncovered": None,
    }
    null_projection["business_check"] = {
        "status": None, "checked_obligations": None, "uncovered": None,
    }
    null_projection["batch"] = {
        "planned_instance_count": 1,
        "started_instance_count": 1,
        "instances_with_results": 1,
        "instances_with_all_layers_attempted": 0,
        "partial_instance_count": 1,
        "remaining_instance_count": 0,
        "stop_reason": "lexicographic_layer_unavailable",
    }
    null_projection["batch_summary_ref"] = {"path": ".reaslab/optagent/cases/example/runs/example/output/batch-summary.json"}
    delivery_record = store.read_record(
        f".reaslab/optagent/cases/{case_id}/deliveries/{selected['result']['delivery']['id']}/delivery.json"
    )
    null_body = _render_report(state, delivery_record, ".md", [null_projection]).decode("utf-8")
    assert "Objective sense: not provided" in null_body
    assert "Model variable coverage: not provided" in null_body
    assert "Business checked obligations: not provided" in null_body
    assert "0 had all target layers attempted (attempted does not imply successful or optimal); 1 partial; 0 remaining;" in null_body
    assert "stop reason=lexicographic_layer_unavailable" in null_body
    assert "Batch summary evidence: .reaslab/optagent/cases/example/runs/example/output/batch-summary.json" in null_body

    state = store.read_case(case_id)
    repeated_selection = core["call"]("optagent.deliver", {
        "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "run", "candidate_id": candidate_id,
        "review_id": review_id, "proof_ids": [], "run_ids": [run_id],
    })
    assert repeated_selection["ok"], repeated_selection
    assert repeated_selection["result"]["editable_case"]["status"] == "generated", repeated_selection["result"]["editable_case"]
    repeated_delivery = store.read_record(
        f".reaslab/optagent/cases/{case_id}/deliveries/{repeated_selection['result']['delivery']['id']}/delivery.json"
    )
    from optagent_core.delivery import _editable_case_projection_plan, _preflight_editable_case_projection

    edited_plan = _editable_case_projection_plan(store, store.read_case(case_id), repeated_delivery)
    model_path = case_root / "code" / "model.py"
    model_path.write_text(
        "def build_model(bundle):\n    raise ValueError('edited-visible-model')\n",
        encoding="utf-8",
    )
    user_bytes = model_path.read_bytes()
    from optagent_core.case_execution import _capture_case, solve_case

    local_snapshot = _capture_case(case_root)
    assert local_snapshot["model_spec"]["original_path"] == f"{artifact_dir}/model.py"
    assert local_snapshot["reader"].read_ref(local_snapshot["model_spec"]["bytes"]) == user_bytes
    local_run = solve_case(case_root)
    assert local_run["status"] == "failed"
    assert local_run["error"]["child_error"]["error_type"] == "ValueError"
    preflight = _preflight_editable_case_projection(store, case_id=case_id, plan=edited_plan)
    assert preflight["status"] == "not_generated"
    assert preflight["reason"] == "existing_case_file_conflict"
    assert model_path.read_bytes() == user_bytes


def test_editable_projection_failure_preserves_safe_contract_diagnostics(monkeypatch):
    from optagent_core import delivery as delivery_module

    failure = ContractError(
        "environment_unavailable",
        "fixed editable-case support file python/optagent_core/case_execution.py is unavailable",
        pointer="/support/case_execution.py",
        details={"resource": "case_execution.py"},
        effect="local_committed",
    )

    def fail_projection(*_args):
        raise failure

    monkeypatch.setattr(delivery_module, "_delivery_result_projection", fail_projection)
    plan = delivery_module._editable_case_projection_plan(
        None,
        {"case_id": "case", "artifact_dir": "cases/case"},
        {"delivery_id": "delivery"},
    )

    assert plan["status"] == "not_generated"
    assert plan["reason"] == "projection_failed"
    assert plan["details"] == {
        "error_code": "environment_unavailable",
        "message": "fixed editable-case support file python/optagent_core/case_execution.py is unavailable",
        "pointer": "/support/case_execution.py",
        "details": {"resource": "case_execution.py"},
    }
    assert "effect" not in plan["details"]


def test_fixed_product_file_error_names_only_its_product_relative_path(tmp_path):
    from optagent_core.delivery import _read_fixed_product_file

    with pytest.raises(ContractError) as error_info:
        _read_fixed_product_file(
            tmp_path / "private-workspace" / "missing.py",
            product_relative_path="python/optagent_core/case_execution.py",
        )

    assert error_info.value.code == "environment_unavailable"
    assert "python/optagent_core/case_execution.py" in error_info.value.message
    assert str(tmp_path) not in error_info.value.message


def test_review_close_reports_optimality_support_and_missing_result_anchor(core):
    case_id, candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(core)
    review_id = _core11_begin_note(core, case_id, candidate_id, [run_id])

    unsupported_optimality = {
        "kind": "optimality",
        "scope": "The checked objective meets its global bound.",
        "conditions": [],
        "evidence_refs": [result_refs[0]],
        "support": "supported",
    }
    rejected_optimality = _core11_close(core, case_id, review_id, [unsupported_optimality])
    assert not rejected_optimality["ok"]
    assert rejected_optimality["error"] == {
        "code": "evidence_insufficient",
        "message": "each reviewed numerical Result must independently support its claim kind",
        "pointer": "/claims/0/support",
        "details": {
            "claim_index": 0,
            "required_support": "conditional_with_nonempty_conditions",
        },
        "effect": "none",
    }

    result_raw = store.read_ref(result_refs[0])
    result = strict_json_loads(result_raw, max_bytes=len(result_raw))
    missing_result = {
        "kind": "feasibility",
        "scope": "The checked witness is feasible.",
        "conditions": [],
        "evidence_refs": [result["check"]],
        "support": "supported",
    }
    rejected_missing_result = _core11_close(core, case_id, review_id, [missing_result])
    assert not rejected_missing_result["ok"]
    assert rejected_missing_result["error"] == {
        "code": "evidence_insufficient",
        "message": "claim evidence does not contain a primary anchor allowed for this claim kind",
        "pointer": "/claims/0/evidence_refs",
        "details": {
            "claim_index": 0,
            "expected_anchor_types": [
                "checked_result_with_passed_checks",
                "matching_formal_proof",
            ],
        },
        "effect": "none",
    }
    unsupported_infeasibility = {
        "kind": "infeasibility",
        "scope": "The current Result proves infeasibility.",
        "conditions": [],
        "evidence_refs": [result_refs[0]],
        "support": "supported",
    }
    rejected_infeasibility = _core11_close(core, case_id, review_id, [unsupported_infeasibility])
    assert not rejected_infeasibility["ok"]
    assert rejected_infeasibility["error"]["details"]["expected_anchor_types"] == [
        "matching_formal_proof"
    ]
    current_review, _ = store.read_family_record(case_id, "reviews", review_id)
    assert current_review["status"] == "open"


def test_numeric_review_binds_one_nonstandard_answer_ref_and_select_rechecks_it(core, monkeypatch):
    from optagent_core.store import CaseStore

    case_id, candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(core)
    review_id = _core11_begin_note(core, case_id, candidate_id, [run_id])
    report_path = "audit/final-review.v3"
    report_file = core["workspace"].joinpath(*report_path.split("/"))
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text("Final reviewed report.\n", encoding="utf-8")
    report_ref = store.ref_for_path(report_path)
    feasibility = {
        "kind": "feasibility",
        "scope": "The checked witness for the current instance is feasible.",
        "conditions": [],
        "evidence_refs": [result_refs[0], report_ref],
        "support": "supported",
    }

    before, before_ref = store.read_family_record(case_id, "reviews", review_id)
    missing_report = _core11_close(core, case_id, review_id, [
        {**feasibility, "evidence_refs": [result_refs[0]]}
    ], report_path=None)
    assert not missing_report["ok"]
    assert missing_report["error"]["code"] == "evidence_insufficient"
    assert missing_report["error"]["pointer"] == "/report_path"
    assert missing_report["error"]["details"] == {"required": True}
    assert missing_report["error"]["effect"] == "none"
    after, after_ref = store.read_family_record(case_id, "reviews", review_id)
    assert after["status"] == "open"
    assert before_ref == after_ref
    assert before["review_revision"] == after["review_revision"] == 2

    read_paths: list[str] = []
    read_user_file = CaseStore.read_user_file

    def count_report_reads(instance, relative_path):
        if relative_path.replace("\\", "/") == report_path:
            read_paths.append(relative_path)
        return read_user_file(instance, relative_path)

    monkeypatch.setattr(CaseStore, "read_user_file", count_report_reads)
    closed = _core11_close(
        core,
        case_id,
        review_id,
        [feasibility],
        report_path=report_path,
    )
    assert closed["ok"], closed
    assert read_paths == [report_path]
    reviewed, _ = store.read_family_record(case_id, "reviews", review_id)
    reviewed_claim = reviewed["conclusion"]["claims"][0]
    assert reviewed["answer_ref"] == report_ref
    assert all(ref["path"] != report_path for ref in reviewed_claim["evidence_refs"])

    report_file.write_text("Changed after close.\n", encoding="utf-8")
    state = store.read_case(case_id)
    selection_claim = {**feasibility, "evidence_refs": [result_refs[0]]}
    selected = core["call"]("optagent.deliver", {
        "op": "select",
        "case_id": case_id,
        "expected_state_revision": state["state_revision"],
        "kind": "run",
        "candidate_id": candidate_id,
        "review_id": review_id,
        "proof_ids": [],
        "run_ids": [run_id],
    })
    assert not selected["ok"]
    assert selected["error"]["code"] == "artifact_changed"


def test_report_and_zip_writes_preserve_local_committed_after_downstream_oserror(core, monkeypatch):
    import errno

    from optagent_core.delivery import build_report, package_delivery
    from test_reuse_proof import _proof_review_to_delivery

    case_id, _, _, _, delivery_id = _proof_review_to_delivery(core)
    store = core["store"]()
    state = store.read_case(case_id)
    stamp = core["context"]()["stamp"]
    real_write = store.write_content_addressed

    def fail_summary(_prefix, _raw, _media_type):
        raise OSError(errno.EIO, "injected summary storage failure")

    monkeypatch.setattr(store, "write_content_addressed", fail_summary)
    report_path = "reports/summary-storage-failure.md"
    with pytest.raises(ContractError) as report_error:
        build_report(
            store,
            case_id=case_id,
            expected_state_revision=state["state_revision"],
            delivery_id=delivery_id,
            target_path=report_path,
            stamp=stamp,
        )
    report_local = report_error.value.details["local_artifact"]
    assert report_error.value.code == "environment_unavailable"
    assert report_error.value.effect == "local_committed"
    assert report_local["kind"] == "report"
    assert report_local["report_path"] == report_path
    assert report_local["report_ref"]["path"] == report_path
    assert (core["workspace"] / report_path).is_file()
    assert "summary_ref" not in report_local

    monkeypatch.setattr(store, "write_content_addressed", real_write)
    package_path = "exports/manifest-storage-failure.zip"

    def fail_manifest(prefix, raw, media_type):
        if prefix.endswith("/package-manifests"):
            raise OSError(errno.EIO, "injected manifest storage failure")
        return real_write(prefix, raw, media_type)

    monkeypatch.setattr(store, "write_content_addressed", fail_manifest)
    with pytest.raises(ContractError) as package_error:
        package_delivery(
            store,
            case_id=case_id,
            expected_state_revision=state["state_revision"],
            delivery_id=delivery_id,
            target_path=package_path,
            include_raw_sources=False,
            stamp=stamp,
        )
    package_local = package_error.value.details["local_artifact"]
    assert package_error.value.code == "environment_unavailable"
    assert package_error.value.effect == "local_committed"
    assert package_local["kind"] == "package"
    assert package_local["package_path"] == package_path
    assert package_local["package_ref"]["path"] == package_path
    assert (core["workspace"] / package_path).is_file()
    assert "manifest_ref" not in package_local


@pytest.mark.parametrize("termination", ["time_limit", "iteration_limit", "feasible"])
def test_optimality_claim_consumes_numerical_closure_for_supported_nonoptimal_status(monkeypatch, termination):
    import optagent_core.delivery as delivery_module
    from optagent_core.results import CheckedModel, HighsObservation, ObjectiveSpec, _result_numerical_summary, result_record
    from optagent_core.runner import _validate_normalized_bound_and_gap

    case_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    output_root = f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/instances/000000"
    result_ref = ref_from_bytes(f"{output_root}/layer-single/result.json", b"result", "application/json")
    check_ref = ref_from_bytes(f"{output_root}/layer-single/model-check.json", b"check", "application/json")
    business_ref = ref_from_bytes(f"{output_root}/layer-single/business-check.json", b"business", "application/json")
    observation_ref = ref_from_bytes(f"{output_root}/layer-single/backend-observation.json", b"observation", "application/json")
    checked_objective = 100.0 - 2e-9
    check = {
        "status": "pass",
        "tolerances": {"primal": 1e-7},
        "objective_values": [{"component": "cost", "sense": "min", "unit": "unit", "value": checked_objective}],
    }
    business = {"status": "uncovered"}
    options = {
        "mip_rel_gap": 0.0,
        "mip_abs_gap": 1.0,
        "primal_feasibility_tolerance": 1e-7,
        "mip_feasibility_tolerance": 1e-7,
    }
    native_observation = {
        "termination": termination,
        "raw_solution_value_valid": True,
        "best_feasible_objective": 100.0,
        "best_objective_bound": 100.0,
        "bound_evidence": {
            "source": "synthetic structural fixture",
            "scope": "global",
            "objective_sense": "min",
            "value_direction": "lower",
            "interface": "test fixture",
            "objective_transform": {"kind": "identity", "formula": "original objective units"},
            "raw_value": 100.0,
        },
        "requested_options": options,
        "effective_options": options,
    }
    observation = {"observation": native_observation}
    checked = CheckedModel(
        record=check,
        witness_variables=(),
        objective_values=tuple(check["objective_values"]),
        candidate_complete=True,
        raw_vector_complete=True,
    )
    native = HighsObservation(
        record=native_observation,
        backend={"id": "highs", "version": "synthetic fixture"},
        termination=termination,
        raw_status="native-" + termination,
        solver_invoked=True,
        solver_returned=True,
        primals={},
        solver_incumbent_objective=100.0,
        bound_value=100.0,
        nonfinite_fields={},
        solve_wall_seconds=0.001,
        error=None,
    )
    result = result_record(
        native,
        checked,
        ObjectiveSpec("cost", "min", "unit", None, None),
        instance_key=["A"],
        instance_binding_digest="b" * 64,
        source_revision=1,
        candidate_revision=1,
        input_digest="a" * 64,
        layer=None,
        bound_evidence_ref=observation_ref,
    )
    result.update({"witness": {"path": "witness.json"}, "check": check_ref, "lexicographic": None})
    assert result["bound"] is None
    assert result["gap"]["absolute"] is None

    _validate_normalized_bound_and_gap(
        result,
        check,
        {"mode": "scalar", "sense": "min"},
        raw_bound_value=native_observation["best_objective_bound"],
    )
    runner_recomputed = _result_numerical_summary(
        check_record=check,
        objective_value=result["objective"]["value"],
        objective_sense="min",
        bound=result["bound"],
        absolute_gap=result["gap"]["absolute"],
        termination=result["termination"],
        has_incumbent=result["has_solver_incumbent"],
        requested_options=native_observation["requested_options"],
        effective_options=native_observation["effective_options"],
        native_observation=native_observation,
    )
    assert result["numerical"] == runner_recomputed
    verified = {
        "run": {"results": [result_ref]},
        "manifest": {"candidate": {"objective_plan": {"mode": "scalar", "sense": "min"}}},
        "seal": {"output_refs": [check_ref, business_ref, observation_ref]},
    }
    read = {
        check_ref["path"]: check,
        business_ref["path"]: business,
        observation_ref["path"]: observation,
    }
    monkeypatch.setattr(delivery_module, "_verified_run", lambda *args, **kwargs: verified)
    monkeypatch.setattr(delivery_module, "_read_verified_output_json", lambda _store, _verified, ref: read[ref["path"]])

    assert delivery_module._result_supports_claim(
        object(), {"case_id": case_id}, result, result_ref, "optimality", {}, run_id,
        claim_support="conditional", claim_conditions=["numerically closed within the declared tolerance after a passed feasibility check"],
    ) is True
    assert result["numerical"]["optimality"]["qualification"] == "objective_recomputation_residual"
    assert result["numerical"]["optimality"]["closure_residual"] == pytest.approx(2e-9)
    report = delivery_module._render_report(
        {"title": "Residual fixture", "task_kind": "optimize"},
        {"kind": "run", "claims": []},
        ".md",
        [{
            "claim_kind": "optimality",
            "instance_key": ["A"],
            "layer": None,
            "objective": {"value": checked_objective, "unit": "USD", "sense": "min"},
            "backend": {"id": "highs", "version": "fixture"},
            "raw_status": "native-time-limit",
            "termination": termination,
            "model_check": check,
            "business_check": business,
            "bound": None,
            "gap": result["gap"],
            "numerical": result["numerical"],
            "lexicographic": None,
            "result_ref": {"path": result_ref["path"]},
        }],
    ).decode("utf-8")
    assert "Numerical optimality closure: closed (min; objective_recomputation_residual)" in report
    assert "native_reported_bound=100.0" in report
    assert (
        f"objective_recomputation_residual={result['numerical']['optimality']['objective_recomputation_residual']}"
        in report
    )
    assert "Numerically closed under the declared tolerance; this is not an exact optimality proof." in report
    assert delivery_module._result_supports_claim(
        object(), {"case_id": case_id}, result, result_ref, "bound", {}, run_id,
        claim_support="conditional", claim_conditions=["declared tolerance"],
    ) is False

    valid_numerical = result["numerical"]
    result["numerical"] = {
        **valid_numerical,
        "optimality": {**valid_numerical["optimality"], "closure_residual": 0.0},
    }
    assert delivery_module._result_supports_claim(
        object(), {"case_id": case_id}, result, result_ref, "optimality", {}, run_id,
        claim_support="conditional", claim_conditions=["declared tolerance"],
    ) is False
    result["numerical"] = valid_numerical

    old_native_bound = native_observation["best_objective_bound"]
    native_observation["best_objective_bound"] = old_native_bound + 1.0
    assert delivery_module._result_supports_claim(
        object(), {"case_id": case_id}, result, result_ref, "optimality", {}, run_id,
        claim_support="conditional", claim_conditions=["declared tolerance"],
    ) is False
    native_observation["best_objective_bound"] = old_native_bound

    original_refs = verified["seal"]["output_refs"]
    verified["seal"]["output_refs"] = [ref for ref in original_refs if ref["path"] != observation_ref["path"]]
    assert delivery_module._result_supports_claim(
        object(), {"case_id": case_id}, result, result_ref, "optimality", {}, run_id,
        claim_support="conditional", claim_conditions=["declared tolerance"],
    ) is False
    verified["seal"]["output_refs"] = original_refs

    result["termination"] = "error"
    native_observation["termination"] = "error"
    result["numerical"] = _result_numerical_summary(
        check_record=check,
        objective_value=checked_objective,
        objective_sense="min",
        bound=result["bound"],
        absolute_gap=None,
        termination="error",
        has_incumbent=True,
        requested_options=options,
        effective_options=options,
        native_observation=native_observation,
    )
    assert delivery_module._result_supports_claim(
        object(), {"case_id": case_id}, result, result_ref, "optimality", {}, run_id,
        claim_support="conditional", claim_conditions=["a numeric bound is not enough"],
    ) is False


def test_residual_closure_flows_from_worker_result_through_runner_and_delivery(core):
    from optagent_core.delivery import _render_report, _result_supports_claim

    case_id, _candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(
        core,
        native_objective_delta=2e-9,
    )
    result_ref = result_refs[1]
    result_raw = store.read_ref(result_ref)
    result = strict_json_loads(result_raw, max_bytes=len(result_raw))

    assert result["bound"] is None
    assert result["gap"]["absolute"] is None
    assert result["numerical"]["optimality"]["status"] == "closed"
    assert result["numerical"]["optimality"]["qualification"] == "objective_recomputation_residual"
    assert result["numerical"]["optimality"]["native_reported_bound"] == 5.000000002
    assert result["numerical"]["optimality"]["closure_residual"] == pytest.approx(2e-9)

    state = store.read_case(case_id)
    assert _result_supports_claim(
        store,
        state,
        result,
        result_ref,
        "optimality",
        {},
        run_id,
        claim_support="conditional",
        claim_conditions=["Numerically closed under the frozen tolerance after a passing ModelCheck."],
    ) is True
    assert _result_supports_claim(
        store,
        state,
        result,
        result_ref,
        "bound",
        {},
        run_id,
        claim_support="conditional",
        claim_conditions=["declared tolerance"],
    ) is False

    output_dir = result_ref["path"].rsplit("/", 1)[0]
    check = strict_json_loads(store.read_ref(store.ref_for_path(f"{output_dir}/model-check.json")))
    business = strict_json_loads(store.read_ref(store.ref_for_path(f"{output_dir}/business-check.json")))
    report = _render_report(
        {"title": "Residual integration fixture", "task_kind": "optimize"},
        {"kind": "run", "claims": []},
        ".md",
        [{
            "claim_kind": "optimality",
            "instance_key": result["instance_key"],
            "layer": result["layer"],
            "objective": result["objective"],
            "backend": result["backend"],
            "raw_status": result["raw_status"],
            "termination": result["termination"],
            "model_check": check,
            "business_check": business,
            "bound": result["bound"],
            "gap": result["gap"],
            "numerical": result["numerical"],
            "lexicographic": result.get("lexicographic"),
            "result_ref": result_ref,
        }],
    ).decode("utf-8")
    assert "global_bound=None" in report
    assert "native_reported_bound=5.000000002" in report
    assert "Numerically closed under the declared tolerance; this is not an exact optimality proof." in report


def test_large_sealed_result_checks_and_batch_project_through_real_delivery_chain(core):
    limit = 4 * 1024 * 1024
    case_id, candidate_id, run_id, result_refs, store = _synthetic_reviewable_batch(
        core,
        large_json_outputs=True,
    )
    result_raw = store.read_ref(result_refs[0])
    result = strict_json_loads(result_raw, max_bytes=len(result_raw))
    check_ref = result["check"]
    business_ref = {
        "path": result_refs[0]["path"].rsplit("/", 1)[0] + "/business-check.json",
        "media_type": "application/json",
    }
    check_raw = store.read_ref(check_ref)
    business_path = business_ref["path"]
    business_ref = store.ref_for_path(business_path)
    business_raw = store.read_ref(business_ref)
    batch_ref = store.ref_for_path(f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/batch-summary.json")
    batch_raw = store.read_ref(batch_ref)
    assert all(len(raw) > limit for raw in (result_raw, check_raw, business_raw, batch_raw))

    review_id = _core11_begin_note(core, case_id, candidate_id, [run_id])
    feasibility = {
        "kind": "feasibility",
        "scope": "The checked candidate for site A remains feasible.",
        "conditions": [],
        "evidence_refs": [result_refs[0]],
        "support": "supported",
    }
    closed = _core11_close(core, case_id, review_id, [feasibility], report_path="reports/final-review.md")
    assert closed["ok"], closed

    state = store.read_case(case_id)
    selected = core["call"]("optagent.deliver", {
        "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "run", "candidate_id": candidate_id,
        "review_id": review_id, "proof_ids": [], "run_ids": [run_id],
    })
    assert selected["ok"], selected
    state = store.read_case(case_id)
    built = core["call"]("optagent.deliver", {
        "op": "build", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "delivery_id": selected["result"]["delivery"]["id"],
        "target_path": "reports/large-business-result.json",
    })
    assert built["ok"], built
    summary = strict_json_loads(store.read_ref(built["result"]["summary_ref"]))
    projected = summary["result_evidence"]
    assert len(projected) == 1
    assert projected[0]["result_ref"] == result_refs[0]
    assert projected[0]["model_check"]["status"] == "pass"
    assert projected[0]["business_check"]["status"] == "uncovered"
    assert projected[0]["batch_summary_ref"] == batch_ref
    assert projected[0]["batch"]["remaining_instance_count"] == 1

    state = store.read_case(case_id)
    packed = core["call"]("optagent.deliver", {
        "op": "package", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "delivery_id": selected["result"]["delivery"]["id"],
        "target_path": "exports/large-business-result.zip", "include_raw_sources": True,
    })
    assert packed["ok"], packed
    with zipfile.ZipFile(BytesIO(store.read_ref(packed["result"]["package_ref"]))) as archive:
        names = set(archive.namelist())
        packaged_result = archive.read(f"evidence/runs/{run_id}/outputs/instances/000000/layer-single/result.json")
        packaged_batch = archive.read(f"evidence/runs/{run_id}/outputs/batch-summary.json")
    assert packaged_result == result_raw
    assert packaged_batch == batch_raw
    assert "reproduce/run.py" in names
