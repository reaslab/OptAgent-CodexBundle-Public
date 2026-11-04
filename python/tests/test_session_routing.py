from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import uuid

import optagent_core.api as api
import pytest
from optagent_core.api import dispatch_control_event, dispatch_dynamic
from optagent_core.contracts import ContractError, canonical_json_bytes


def _route_paths(session_id: str) -> tuple[str, str]:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return (
        f".reaslab/optagent/host/session_bindings/{digest}.json",
        f".reaslab/optagent/input-fences/{digest}.json",
    )


def _install_route(
    core,
    case_id: str,
    *,
    covered_seq: int,
    latest_seq: int,
    gap: bool = True,
    missing_start: int | None = None,
    missing_end: int | None = None,
    observed_by: dict | None = None,
):
    store = core["store"]()
    state = store.read_case(case_id)
    session_id = state["owner_session_id"]
    index_path, barrier_path = _route_paths(session_id)
    gap_ref = None
    if gap:
        observed = observed_by or core["context"]()["stamp"]
        audit = {
            "schema_version": 1,
            "case_id": case_id,
            "session_id": session_id,
            "covered_seq": covered_seq,
            "missing_seq_ranges": [{
                "start": missing_start if missing_start is not None else covered_seq + 1,
                "end": missing_end if missing_end is not None else missing_start if missing_start is not None else covered_seq + 1,
            }],
            "registered_by": state["created_by"],
            "observed_by": observed,
        }
        raw = canonical_json_bytes(audit)
        digest = hashlib.sha256(raw).hexdigest()
        gap_path = f".reaslab/optagent/host/route_audits/{digest}.json"
        gap_ref = store.write_immutable(gap_path, raw, "application/json")
    index = {
        "schema_version": 1,
        "session_id": session_id,
        "workspace_absolute": str(core["workspace"]),
        "index_revision": 1,
        "members": [{
            "case_id": case_id,
            "registered_by": state["created_by"],
            "input_mode": "working",
            "covered_seq": covered_seq,
            "gap_ref": gap_ref,
        }],
        "pending_starts": [],
    }
    store.write_record(index_path, index, replace=True)
    barrier = {
        "session_id": session_id,
        "arrival_epoch": core["arrival_epoch"],
        "latest_observed_seq": latest_seq,
    }
    store.write_record(barrier_path, barrier, replace=True)
    return gap_ref


def _capture_arrived(core, case_id: str, transfer: dict):
    return core["control"]({
        "event_id": str(uuid.uuid4()),
        "kind": "capture_arrived",
        "capture": transfer,
        "bindings": [{"case_id": case_id, "run_id": None, "pending_start": False}],
    })


def _install_route_members(core, covered_by_case: dict[str, int], *, latest_seq: int) -> None:
    store = core["store"]()
    first_state = store.read_case(next(iter(covered_by_case)))
    session_id = first_state["owner_session_id"]
    index_path, barrier_path = _route_paths(session_id)
    members = []
    for case_id, covered_seq in covered_by_case.items():
        state = store.read_case(case_id)
        assert state["owner_session_id"] == session_id
        members.append({
            "case_id": case_id,
            "registered_by": state["created_by"],
            "input_mode": "working",
            "covered_seq": covered_seq,
            "gap_ref": None,
        })
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    with store.session_lock(digest):
        store.write_record(index_path, {
            "schema_version": 1,
            "session_id": session_id,
            "workspace_absolute": str(core["workspace"]),
            "index_revision": 1,
            "members": members,
            "pending_starts": [],
        }, replace=True)
        store.write_record(barrier_path, {
            "session_id": session_id,
            "arrival_epoch": core["arrival_epoch"],
            "latest_observed_seq": latest_seq,
        }, replace=True)


def _queue_capture_arrival(core, case_id: str, *, seq: int) -> dict:
    store = core["store"]()
    transfer = core["capture"](b"queued but not yet materialized", seq=seq)
    event = {
        "event_id": str(uuid.uuid4()),
        "kind": "capture_arrived",
        "capture": transfer,
        "bindings": [{"case_id": case_id, "run_id": None, "pending_start": False}],
    }
    session_id = core["session_id"]
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    with store.session_lock(digest):
        queue_path = f".reaslab/optagent/host/pending_control/{digest}.json"
        try:
            queue = store.read_record(queue_path)
        except ContractError as exc:
            if exc.code != "not_found":
                raise
            queue = {
                "schema_version": 1,
                "session_id": session_id,
                "workspace_absolute": str(core["workspace"]),
                "events": [],
            }
        queue["events"].append(event)
        store.write_record(queue_path, queue, replace=True)
    return event


def _queue_after_route_snapshot(monkeypatch, core, case_id: str, *, arrival_case_id: str | None = None, seq: int = 2):
    original = api._prepare_session_route_fence
    observed = {"calls": 0, "event": None}

    def prepare_then_queue(store, state, **kwargs):
        fence = original(store, state, **kwargs)
        if state["case_id"] == case_id:
            observed["calls"] += 1
            if observed["event"] is None:
                observed["event"] = _queue_capture_arrival(
                    core, arrival_case_id or case_id, seq=seq
                )
        return fence

    monkeypatch.setattr(api, "_prepare_session_route_fence", prepare_then_queue)
    return observed


def test_capture_final_gate_rejects_new_unmaterialized_case_arrival(core, monkeypatch):
    case_id, _, _ = core["create_case"](seq=1)
    _install_route_members(core, {case_id: 1}, latest_seq=1)
    state_before = core["store"]().read_case(case_id)
    inputs_dir = core["workspace"] / "inputs"
    inputs_dir.mkdir()
    (inputs_dir / "arrival.csv").write_bytes(b"x,value\na,1\n")

    original_prepare = api._prepare_session_route_fence
    injected = False
    queued_event = None

    def prepare_then_queue(store, state, **kwargs):
        nonlocal injected, queued_event
        fence = original_prepare(store, state, **kwargs)
        if state["case_id"] == case_id and not injected:
            injected = True
            queued_event = _queue_capture_arrival(core, case_id, seq=2)
        return fence

    monkeypatch.setattr(api, "_prepare_session_route_fence", prepare_then_queue)
    reply = core["call"]("optagent.data", {
        "op": "capture",
        "case_id": case_id,
        "expected_state_revision": state_before["state_revision"],
        "path": "inputs/arrival.csv",
        "purpose": "data",
        "locations": ["rows 1-1"],
    })

    assert injected and queued_event is not None
    assert not reply["ok"] and reply["error"]["code"] == "capture_unavailable"
    state_after = core["store"]().read_case(case_id)
    assert state_after["state_revision"] == state_before["state_revision"]
    assert state_after["source_ids"] == state_before["source_ids"]
    assert state_after["pending_inputs"] == state_before["pending_inputs"]
    queued = core["store"]().read_record(
        f".reaslab/optagent/host/pending_control/{hashlib.sha256(core['session_id'].encode('utf-8')).hexdigest()}.json"
    )
    assert queued["events"] == [queued_event]


def test_capture_final_gate_refreshes_once_for_an_unrelated_case_arrival(core, monkeypatch):
    target_case, _, _ = core["create_case"](seq=1)
    other_case, _, _ = core["create_case"](seq=2)
    _install_route_members(core, {target_case: 1, other_case: 2}, latest_seq=2)
    target_before = core["store"]().read_case(target_case)
    other_before = core["store"]().read_case(other_case)
    inputs_dir = core["workspace"] / "inputs"
    inputs_dir.mkdir()
    (inputs_dir / "target.csv").write_bytes(b"x,value\na,1\n")

    original_prepare = api._prepare_session_route_fence
    prepare_calls = 0
    queued_event = None

    def prepare_then_queue_other(store, state, **kwargs):
        nonlocal prepare_calls, queued_event
        fence = original_prepare(store, state, **kwargs)
        if state["case_id"] == target_case:
            prepare_calls += 1
            if prepare_calls == 1:
                queued_event = _queue_capture_arrival(core, other_case, seq=3)
        return fence

    monkeypatch.setattr(api, "_prepare_session_route_fence", prepare_then_queue_other)
    reply = core["call"]("optagent.data", {
        "op": "capture",
        "case_id": target_case,
        "expected_state_revision": target_before["state_revision"],
        "path": "inputs/target.csv",
        "purpose": "data",
        "locations": ["rows 1-1"],
    })

    assert reply["ok"], reply
    assert queued_event is not None and prepare_calls == 2
    target_after = core["store"]().read_case(target_case)
    other_after = core["store"]().read_case(other_case)
    assert target_after["state_revision"] == target_before["state_revision"] + 1
    assert len(target_after["source_ids"]) == len(target_before["source_ids"]) + 1
    assert other_after == other_before
    queue_path = f".reaslab/optagent/host/pending_control/{hashlib.sha256(core['session_id'].encode('utf-8')).hexdigest()}.json"
    assert core["store"]().read_record(queue_path)["events"] == [queued_event]


def test_capture_final_gate_ignores_review_family_ref_only_change(core, monkeypatch):
    case_id, _, _ = core["create_case"](task_kind="proof", seq=1)
    state = core["store"]().read_case(case_id)
    begun = core["call"]("optagent.review", {
        "op": "begin",
        "case_id": case_id,
        "expected_state_revision": state["state_revision"],
        "candidate_id": None,
        "proof_ids": [],
        "run_ids": [],
    }, role="opt-verifier")
    assert begun["ok"], begun
    review_id = begun["result"]["review"]["id"]
    state_before = core["store"]().read_case(case_id)
    old_review_ref = state_before["current_records"]["reviews"][review_id]
    _install_route_members(core, {case_id: 1}, latest_seq=1)
    inputs_dir = core["workspace"] / "inputs"
    inputs_dir.mkdir()
    (inputs_dir / "proof-data.csv").write_bytes(b"x,value\na,1\n")

    original_prepare = api._prepare_session_route_fence
    changed = False

    def prepare_after_review_note(store, state, **kwargs):
        nonlocal changed
        fence = original_prepare(store, state, **kwargs)
        if state["case_id"] == case_id and not changed:
            changed = True
            note = core["call"]("optagent.review", {
                "op": "note",
                "case_id": case_id,
                "review_id": review_id,
                "expected_review_revision": 1,
                "body": "The source-level scope remains explicit.",
                "source_locations": [{"source_id": state_before["source_ids"][0], "locator": "source-level"}],
            }, role="opt-verifier")
            assert note["ok"], note
            current = store.read_case(case_id)
            assert current["state_revision"] == state_before["state_revision"]
            assert current["current_records"]["reviews"][review_id] != old_review_ref
        return fence

    monkeypatch.setattr(api, "_prepare_session_route_fence", prepare_after_review_note)
    reply = core["call"]("optagent.data", {
        "op": "capture",
        "case_id": case_id,
        "expected_state_revision": state_before["state_revision"],
        "path": "inputs/proof-data.csv",
        "purpose": "data",
        "locations": ["rows 1-1"],
    })

    assert changed and reply["ok"], reply
    state_after = core["store"]().read_case(case_id)
    assert state_after["state_revision"] == state_before["state_revision"] + 1
    assert state_after["current_records"]["reviews"][review_id] != old_review_ref
    review, current_review_ref = core["store"]().read_family_record(case_id, "reviews", review_id, state=state_after)
    assert current_review_ref == state_after["current_records"]["reviews"][review_id]
    assert review["review_revision"] == 2 and review["note"] is not None


def test_capture_arrival_positive_materialization_witness_rechecks_after_source_adoption(core, monkeypatch):
    from optagent_core.store import CaseStore

    case_id, _, _ = core["create_case"](raw=b"Original requirement.", seq=1)
    _install_route(core, case_id, covered_seq=1, latest_seq=2)
    transfer = core["capture"](b"Complete replacement requirement.", seq=2)
    first_arrival = _capture_arrived(core, case_id, transfer)
    assert first_arrival["ok"], first_arrival
    original_session_lock = CaseStore.session_lock
    injected = False
    adoption_reply = None

    @contextlib.contextmanager
    def adopt_after_negative_preflight(store, digest):
        nonlocal injected, adoption_reply
        if not injected:
            injected = True
            adoption_reply = _source_revise(
                core,
                case_id,
                [transfer["capture_id"]],
                source_ids=[],
                captures=[transfer],
            )
            assert adoption_reply["ok"], adoption_reply
        with original_session_lock(store, digest):
            yield

    monkeypatch.setattr(CaseStore, "session_lock", adopt_after_negative_preflight)
    replay = _capture_arrived(core, case_id, transfer)

    assert injected and adoption_reply is not None
    assert replay["ok"] and replay["affected_case_ids"] == [case_id]
    state = core["store"]().read_case(case_id)
    assert state["pending_inputs"] == []
    assert len(state["source_ids"]) == 1
    adopted_source = core["store"]().read_record(
        f".reaslab/optagent/cases/{case_id}/sources/{state['source_ids'][-1]}.json"
    )
    assert adopted_source["origin"]["capture_id"] == transfer["capture_id"]
    assert adopted_source["origin"]["arrival_seq"] == transfer["arrival_seq"]


def test_capture_arrival_negative_materialization_witness_survives_review_ref_race(core, monkeypatch):
    from optagent_core.store import CaseStore

    case_id, _, _ = core["create_case"](task_kind="proof", seq=1)
    state_before = core["store"]().read_case(case_id)
    begun = core["call"]("optagent.review", {
        "op": "begin",
        "case_id": case_id,
        "expected_state_revision": state_before["state_revision"],
        "candidate_id": None,
        "proof_ids": [],
        "run_ids": [],
    }, role="opt-verifier")
    assert begun["ok"], begun
    review_id = begun["result"]["review"]["id"]
    state_before = core["store"]().read_case(case_id)
    old_review_ref = state_before["current_records"]["reviews"][review_id]
    _install_route(core, case_id, covered_seq=1, latest_seq=1, gap=False)
    transfer = core["capture"](b"A new, not yet sourced capture.", seq=2)
    original_session_lock = CaseStore.session_lock
    injected = False
    note_reply = None

    @contextlib.contextmanager
    def write_review_note_after_negative_preflight(store, digest):
        nonlocal injected, note_reply
        if not injected:
            injected = True
            note_reply = core["call"]("optagent.review", {
                "op": "note",
                "case_id": case_id,
                "review_id": review_id,
                "expected_review_revision": 1,
                "body": "This changes only the Review family Ref.",
                "source_locations": [{"source_id": state_before["source_ids"][0], "locator": "source-level"}],
            }, role="opt-verifier")
            assert note_reply["ok"], note_reply
        with original_session_lock(store, digest):
            yield

    monkeypatch.setattr(CaseStore, "session_lock", write_review_note_after_negative_preflight)
    arrival = _capture_arrived(core, case_id, transfer)

    assert injected and note_reply is not None
    assert arrival["ok"] and arrival["affected_case_ids"] == [case_id]
    state_after = core["store"]().read_case(case_id)
    assert state_after["state_revision"] == state_before["state_revision"] + 1
    assert state_after["current_records"]["reviews"][review_id] != old_review_ref
    assert [item["capture_id"] for item in state_after["pending_inputs"]] == [transfer["capture_id"]]


def test_candidate_final_gate_rejects_queued_case_arrival(core, monkeypatch):
    case_id, _, _ = core["create_case"](seq=1)
    _install_route_members(core, {case_id: 1}, latest_seq=1)
    (core["workspace"] / "model.py").write_text(
        "def build_model(bundle):\n    return None\n", encoding="utf-8"
    )
    before = core["store"]().read_case(case_id)
    arguments = {
        "op": "revise",
        "case_id": case_id,
        "expected_state_revision": before["state_revision"],
        "kind": "candidate",
        "model_path": "model.py",
        "local_module_paths": [],
        "data_id": None,
        "backend_id": "highs",
        "objective_plan": {"mode": "feasibility", "preference_note": "route final gate"},
        "business_checker_path": None,
        "input_closure": "declared",
        "closure_note": "A valid fixture candidate.",
    }
    observed = _queue_after_route_snapshot(monkeypatch, core, case_id, seq=2)

    reply = core["call"]("optagent.case", arguments)

    assert not reply["ok"] and reply["error"]["code"] == "capture_unavailable"
    assert observed["calls"] == 2 and observed["event"] is not None
    after = core["store"]().read_case(case_id)
    assert after["state_revision"] == before["state_revision"]
    assert after["candidate_id"] is None and after["candidate_revision"] == before["candidate_revision"]


def test_candidate_noop_final_gate_rejects_queued_case_arrival(core, monkeypatch):
    case_id, _, _ = core["create_case"](seq=1)
    _install_route_members(core, {case_id: 1}, latest_seq=1)
    (core["workspace"] / "model.py").write_text(
        "def build_model(bundle):\n    return None\n", encoding="utf-8"
    )
    before = core["store"]().read_case(case_id)
    arguments = {
        "op": "revise",
        "case_id": case_id,
        "expected_state_revision": before["state_revision"],
        "kind": "candidate",
        "model_path": "model.py",
        "local_module_paths": [],
        "data_id": None,
        "backend_id": "highs",
        "objective_plan": {"mode": "feasibility", "preference_note": "route no-op gate"},
        "business_checker_path": None,
        "input_closure": "declared",
        "closure_note": "A valid fixture candidate.",
    }
    registered = core["call"]("optagent.case", arguments)
    assert registered["ok"], registered
    before_noop = core["store"]().read_case(case_id)
    arguments["expected_state_revision"] = before_noop["state_revision"]
    observed = _queue_after_route_snapshot(monkeypatch, core, case_id, seq=2)

    reply = core["call"]("optagent.case", arguments)

    assert not reply["ok"] and reply["error"]["code"] == "capture_unavailable"
    assert observed["calls"] == 2 and observed["event"] is not None
    after = core["store"]().read_case(case_id)
    assert after["state_revision"] == before_noop["state_revision"]
    assert after["candidate_id"] == before_noop["candidate_id"]
    assert after["candidate_revision"] == before_noop["candidate_revision"]


def test_data_capture_reuse_final_gate_rejects_queued_case_arrival(core, monkeypatch):
    case_id, _, _ = core["create_case"](seq=1)
    inputs = core["workspace"] / "inputs"
    inputs.mkdir()
    (inputs / "reuse.csv").write_bytes(b"site,cost\nA,10\n")
    store = core["store"]()
    before_capture = store.read_case(case_id)
    capture_args = {
        "op": "capture",
        "case_id": case_id,
        "expected_state_revision": before_capture["state_revision"],
        "path": "inputs/reuse.csv",
        "purpose": "data",
        "locations": ["row A"],
    }
    captured = core["call"]("optagent.data", capture_args)
    assert captured["ok"], captured
    _install_route_members(core, {case_id: 1}, latest_seq=1)
    before_reuse = store.read_case(case_id)
    capture_args["expected_state_revision"] = before_reuse["state_revision"]
    observed = _queue_after_route_snapshot(monkeypatch, core, case_id, seq=2)

    reply = core["call"]("optagent.data", capture_args)

    assert not reply["ok"] and reply["error"]["code"] == "capture_unavailable"
    assert observed["calls"] == 2 and observed["event"] is not None
    after = store.read_case(case_id)
    assert after["state_revision"] == before_reuse["state_revision"]
    assert after["source_ids"] == before_reuse["source_ids"]


def test_data_normalize_final_gate_rejects_queued_case_arrival(core, monkeypatch):
    from test_runner_multi_instance_freeze import _csv_data_plan

    case_id, _, _ = core["create_case"](seq=1)
    inputs = core["workspace"] / "inputs"
    inputs.mkdir()
    (inputs / "normalize.csv").write_bytes(b"site_id,site,cost\n1,only,10\n")
    store = core["store"]()
    state = store.read_case(case_id)
    captured = core["call"]("optagent.data", {
        "op": "capture", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "path": "inputs/normalize.csv", "purpose": "data", "locations": ["row 1"],
    })
    assert captured["ok"], captured
    source_id = captured["result"]["source"]["id"]
    _install_route_members(core, {case_id: 1}, latest_seq=1)
    before = store.read_case(case_id)
    observed = _queue_after_route_snapshot(monkeypatch, core, case_id, seq=2)

    reply = core["call"]("optagent.data", {
        "op": "normalize", "case_id": case_id,
        "expected_state_revision": before["state_revision"],
        "source_ids": [source_id], "plan": _csv_data_plan(source_id),
    })

    assert not reply["ok"] and reply["error"]["code"] == "capture_unavailable"
    assert observed["calls"] == 2 and observed["event"] is not None
    after = store.read_case(case_id)
    assert after["state_revision"] == before["state_revision"]
    assert after["data_ids"] == before["data_ids"]


def test_root_proof_final_gate_rejects_queued_case_arrival(core, monkeypatch):
    case_id, _, _ = core["create_case"](
        raw=b"For every integer n, n(n+1) is even.", seq=1, task_kind="proof"
    )
    _install_route_members(core, {case_id: 1}, latest_seq=1)
    state = core["store"]().read_case(case_id)
    begun = core["call"]("optagent.review", {
        "op": "begin", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "candidate_id": None, "proof_ids": [], "run_ids": [],
    }, role="opt-verifier")
    assert begun["ok"], begun
    review_id = begun["result"]["review"]["id"]
    source_id = state["source_ids"][0]
    noted = core["call"]("optagent.review", {
        "op": "note", "case_id": case_id, "review_id": review_id,
        "expected_review_revision": 1,
        "body": "The captured statement is the complete proof scope.",
        "source_locations": [{"source_id": source_id, "locator": "source-level"}],
    }, role="opt-verifier")
    assert noted["ok"], noted
    proof_body = core["workspace"] / "proof.md"
    proof_body.write_text(
        "If n is even the product is even; if n is odd then n+1 is even, so the product is even.",
        encoding="utf-8",
    )
    before = core["store"]().read_case(case_id)
    _, review_ref_before = core["store"]().read_family_record(case_id, "reviews", review_id, state=before)
    observed = _queue_after_route_snapshot(monkeypatch, core, case_id, seq=2)

    reply = core["call"]("optagent.review", {
        "op": "submit", "case_id": case_id,
        "expected_state_revision": before["state_revision"],
        "review_id": review_id, "expected_review_revision": 2,
        "record_kind": "proof", "kind": "analytic", "body_path": "proof.md",
        "premises": ["n is an integer"], "evidence_refs": [],
        "claim_kinds": ["formulation_fidelity"], "candidate_id": None,
    })

    assert not reply["ok"] and reply["error"]["code"] == "capture_unavailable"
    assert observed["calls"] == 2 and observed["event"] is not None
    after = core["store"]().read_case(case_id)
    _, review_ref_after = core["store"]().read_family_record(case_id, "reviews", review_id, state=after)
    assert after["state_revision"] == before["state_revision"]
    assert after["proof_ids"] == before["proof_ids"]
    assert review_ref_after == review_ref_before


def test_delivery_select_final_gate_rejects_queued_case_arrival(core, monkeypatch):
    from test_reuse_proof import _proof_review_to_delivery

    case_id, review_id, proof_id, claim, _ = _proof_review_to_delivery(core, select=False)
    _install_route_members(core, {case_id: 1}, latest_seq=1)
    before = core["store"]().read_case(case_id)
    observed = _queue_after_route_snapshot(monkeypatch, core, case_id, seq=2)

    reply = core["call"]("optagent.deliver", {
        "op": "select", "case_id": case_id,
        "expected_state_revision": before["state_revision"], "kind": "proof",
        "candidate_id": None, "review_id": review_id,
        "proof_ids": [proof_id], "run_ids": [],
    })

    assert not reply["ok"] and reply["error"]["code"] == "capture_unavailable"
    assert observed["calls"] == 2 and observed["event"] is not None
    after = core["store"]().read_case(case_id)
    assert after["state_revision"] == before["state_revision"]
    assert after["selected_delivery_id"] is None
    assert after["current_records"]["presentation"] is None


@pytest.mark.parametrize("operation", ["build", "package"])
def test_presentation_final_gate_rejects_queued_case_arrival(core, monkeypatch, operation):
    from test_reuse_proof import _proof_review_to_delivery

    case_id, _review_id, _proof_id, _claim, delivery_id = _proof_review_to_delivery(core)
    _install_route_members(core, {case_id: 1}, latest_seq=1)
    before = core["store"]().read_case(case_id)
    prior_presentation = before["current_records"]["presentation"]
    observed = _queue_after_route_snapshot(monkeypatch, core, case_id, seq=2)
    target = f"reports/route-race.{ 'md' if operation == 'build' else 'zip' }"
    arguments = {
        "op": operation, "case_id": case_id,
        "expected_state_revision": before["state_revision"],
        "delivery_id": delivery_id, "target_path": target,
    }
    if operation == "package":
        arguments["include_raw_sources"] = False

    reply = core["call"]("optagent.deliver", arguments)

    assert not reply["ok"] and reply["error"]["code"] == "capture_unavailable"
    assert reply["error"]["effect"] == "local_committed"
    assert observed["calls"] == 2 and observed["event"] is not None
    after = core["store"]().read_case(case_id)
    assert after["current_records"]["presentation"] == prior_presentation
    assert after["state_revision"] == before["state_revision"]
    assert (core["workspace"] / target).is_file()


def _source_revise(core, case_id: str, capture_ids: list[str], *, source_ids: list[str] | None = None, expected_revision: int | None = None, captures: list[dict] | None = None):
    state = core["store"]().read_case(case_id)
    if expected_revision is None:
        expected_revision = state["state_revision"]
    if source_ids is None:
        source_ids = list(state["source_ids"])
    return core["call"](
        "optagent.case",
        {
            "op": "revise",
            "kind": "sources",
            "case_id": case_id,
            "expected_state_revision": expected_revision,
            "source_ids": list(source_ids),
            "capture_ids": capture_ids,
            "facts": [],
            "reason": "The captured text is the complete current requirement source set.",
        },
        captures=captures,
    )


def test_readonly_get_list_and_audit_keep_unknown_gap_visible_without_writes(core):
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
            "target_path": "reports/old.md",
        },
    )
    assert built["ok"], built
    _install_route(core, case_id, covered_seq=10, latest_seq=11)
    store = core["store"]()
    state_path = f".reaslab/optagent/cases/{case_id}/state.json"
    index_path, _ = _route_paths(core["session_id"])
    state_before = store.read_ref(store.ref_for_path(state_path))
    index_before = store.read_ref(store.ref_for_path(index_path))

    fetched = core["call"]("optagent.case", {"op": "get", "case_id": case_id})
    listed = core["call"]("optagent.case", {"op": "list"})
    audited = core["call"](
        "optagent.deliver",
        {"op": "audit", "case_id": case_id, "delivery_id": delivery_id, "report_path": "reports/old.md"},
        role="opt-verifier",
    )

    assert fetched["ok"] and fetched["result"]["current_validity"] == "unclassified"
    assert listed["ok"]
    listed_case = next(item for item in listed["result"]["cases"] if item["case_id"] == case_id)
    assert listed_case["current_validity"] == "unclassified"
    assert audited["ok"] and audited["result"]["status"] == "consistent"
    for reply in (fetched, listed, audited):
        assert any(item["code"] == "capture_unavailable" and "新鲜度未知" in item["message"] for item in reply["warnings"])
    assert store.read_ref(store.ref_for_path(state_path)) == state_before
    assert store.read_ref(store.ref_for_path(index_path)) == index_before


def test_source_recovery_ack_uses_real_complete_capture_and_keeps_unselected_math_input(core):
    case_id, _, _ = core["create_case"](raw=b"Old requirement set.", seq=10)
    gap_ref = _install_route(core, case_id, covered_seq=10, latest_seq=11)
    index_path, _ = _route_paths(core["session_id"])
    index_digest = core["store"]().ref_for_path(index_path)["sha256"]

    progress = core["capture"](b"No mathematical change; this is progress only.", seq=12)
    assert _capture_arrived(core, case_id, progress)["ok"]
    state = core["store"]().read_case(case_id)
    no_math = core["call"](
        "optagent.case",
        {
            "op": "revise",
            "kind": "note_input",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "capture_id": progress["capture_id"],
            "disposition": "no_math_change",
            "reason": "Progress only.",
        },
        captures=[progress],
    )
    assert no_math["ok"], no_math
    assert no_math["result"]["state"]["current_validity"] == "unclassified"

    replacement = core["capture"](b"Complete replacement: minimize cost subject to capacities A and B.", seq=13)
    unselected_math = core["capture"](b"Change the capacity of B to 17.", seq=14)
    assert _capture_arrived(core, case_id, replacement)["ok"]
    assert _capture_arrived(core, case_id, unselected_math)["ok"]
    adopted = _source_revise(core, case_id, [replacement["capture_id"]], source_ids=[], captures=[replacement])
    assert adopted["ok"], adopted
    changed = core["store"]().read_case(case_id)
    assert changed["source_revision"] == 2
    assert changed["promotion_generation"] == 1
    assert [item["arrival_seq"] for item in changed["pending_inputs"]] == [14]
    source_change = changed["decisions"][-1]
    assert source_change["kind"] == "source_change"
    assert gap_ref in source_change["refs"]
    adoption_ref = next(ref for ref in source_change["refs"] if "/attachments/source_adoption/" in ref["path"])
    assert adoption_ref["path"].endswith(adoption_ref["sha256"] + ".json")
    adoption = json.loads(core["store"]().read_ref(adoption_ref))
    assert adoption["gap_ref"] == gap_ref
    assert adoption["capture_id"] == replacement["capture_id"]
    assert adoption["arrival_seq"] == 13
    assert adoption["stamp"] == source_change["stamp"]
    assert all(ref in source_change["refs"] for ref in adoption["source_refs"])
    assert core["store"]().ref_for_path(index_path)["sha256"] == index_digest

    later_progress = core["capture"](b"Progress update only.", seq=15)
    assert _capture_arrived(core, case_id, later_progress)["ok"]
    # Selecting seq15 as an adopted source cannot erase an earlier, unselected
    # mathematical capture at seq14. Only explicit capture_ids are classified.
    adopted_later = _source_revise(core, case_id, [later_progress["capture_id"]], captures=[later_progress])
    assert adopted_later["ok"], adopted_later
    latest_state = core["store"]().read_case(case_id)
    assert [item["arrival_seq"] for item in latest_state["pending_inputs"]] == [14]
    assert latest_state["source_revision"] == 3
    assert core["store"]().ref_for_path(index_path)["sha256"] == index_digest
    summary = core["call"]("optagent.case", {"op": "get", "case_id": case_id})
    assert summary["ok"] and summary["result"]["current_validity"] == "unclassified"
    blocked = core["call"](
        "optagent.deliver",
        {
            "op": "select",
            "case_id": case_id,
            "expected_state_revision": latest_state["state_revision"],
            "kind": "proof",
            "candidate_id": None,
            "review_id": str(uuid.uuid4()),
            "proof_ids": [],
            "run_ids": [],
        },
    )
    assert not blocked["ok"]
    assert blocked["error"]["code"] in {"input_unclassified", "capture_unavailable"}


def test_source_recovery_rejects_invalid_gap_ref_and_stale_cas(core):
    case_id, _, _ = core["create_case"](raw=b"Old source.", seq=10)
    actual_gap = _install_route(core, case_id, covered_seq=10, latest_seq=11)
    store = core["store"]()
    index_path, _ = _route_paths(core["session_id"])
    index = store.read_record(index_path)
    index["members"][0]["gap_ref"]["sha256"] = "0" * 64
    index["members"][0]["gap_ref"]["path"] = f".reaslab/optagent/host/route_audits/{'0' * 64}.json"
    store.write_record(index_path, index, replace=True)
    candidate = core["capture"](b"A complete accepted replacement.", seq=12)
    assert _capture_arrived(core, case_id, candidate)["ok"]
    before = store.read_ref(store.ref_for_path(f".reaslab/optagent/cases/{case_id}/state.json"))
    invalid = _source_revise(core, case_id, [candidate["capture_id"]], captures=[candidate])
    assert not invalid["ok"] and invalid["error"]["code"] == "capture_unavailable"
    assert store.read_ref(store.ref_for_path(f".reaslab/optagent/cases/{case_id}/state.json")) == before

    # Restore the actual trusted member, then prove stale CAS cannot create an
    # adoption Decision even though the offered input is otherwise eligible.
    _install_route(core, case_id, covered_seq=10, latest_seq=12)
    current = store.read_case(case_id)
    stale = _source_revise(
        core,
        case_id,
        [candidate["capture_id"]],
        expected_revision=current["state_revision"] - 1,
        captures=[candidate],
    )
    assert not stale["ok"] and stale["error"]["code"] == "stale_revision"
    unchanged = store.read_case(case_id)
    assert unchanged["owner_session_id"] == core["session_id"]
    assert not any(
        decision["kind"] == "source_change" and any("source_adoption" in ref["path"] for ref in decision["refs"])
        for decision in unchanged["decisions"]
    )
    assert actual_gap["path"].endswith(actual_gap["sha256"] + ".json")


def test_source_recovery_does_not_drop_older_raw_arrival_materialized_after_commit(core):
    case_id, _, _ = core["create_case"](raw=b"Old source.", seq=10)
    _install_route(core, case_id, covered_seq=10, latest_seq=11)

    progress = core["capture"](b"Progress only.", seq=12)
    assert _capture_arrived(core, case_id, progress)["ok"]
    state = core["store"]().read_case(case_id)
    classified = core["call"](
        "optagent.case",
        {"op": "revise", "kind": "note_input", "case_id": case_id,
         "expected_state_revision": state["state_revision"], "capture_id": progress["capture_id"],
         "disposition": "no_math_change", "reason": "Progress only."},
        captures=[progress],
    )
    assert classified["ok"], classified

    replacement = core["capture"](b"Complete replacement sources at seq13.", seq=13)
    missing_math = core["capture"](b"A mathematical change at seq14.", seq=14)
    later_progress = core["capture"](b"Progress update at seq15.", seq=15)
    assert _capture_arrived(core, case_id, replacement)["ok"]
    # The host has seen seq15 but has not yet materialized the known seq14
    # transfer to this dormant/partially routed case.
    assert _capture_arrived(core, case_id, later_progress)["ok"]
    adopted = _source_revise(
        core,
        case_id,
        [replacement["capture_id"], later_progress["capture_id"]],
        source_ids=[],
        captures=[replacement, later_progress],
    )
    assert adopted["ok"], adopted
    after_adoption = core["store"]().read_case(case_id)
    assert after_adoption["pending_inputs"] == []
    assert after_adoption["decisions"][-1]["kind"] == "source_change"
    ack_ref = next(ref for ref in after_adoption["decisions"][-1]["refs"] if "/attachments/source_adoption/" in ref["path"])
    assert json.loads(core["store"]().read_ref(ack_ref))["arrival_seq"] == 13

    state_path = f".reaslab/optagent/cases/{case_id}/state.json"
    state_digest = core["store"]().ref_for_path(state_path)["sha256"]
    replayed_adoption = _capture_arrived(core, case_id, replacement)
    assert replayed_adoption["ok"], replayed_adoption
    assert replayed_adoption["affected_case_ids"] == [case_id]
    assert core["store"]().ref_for_path(state_path)["sha256"] == state_digest
    _index_path, barrier_path = _route_paths(core["session_id"])
    barrier = core["store"]().read_record(barrier_path)
    assert barrier["latest_observed_seq"] == 15
    assert set(barrier) == {"session_id", "arrival_epoch", "latest_observed_seq"}

    late_materialized = _capture_arrived(core, case_id, missing_math)
    assert late_materialized["ok"]
    state = core["store"]().read_case(case_id)
    assert [item["arrival_seq"] for item in state["pending_inputs"]] == [14]
    assert state["pending_inputs"][0]["capture_id"] == missing_math["capture_id"]
    _index_path, barrier_path = _route_paths(core["session_id"])
    assert set(core["store"]().read_record(barrier_path)) == {"session_id", "arrival_epoch", "latest_observed_seq"}
    current = core["call"]("optagent.case", {"op": "get", "case_id": case_id})
    assert current["ok"] and current["result"]["current_validity"] == "unclassified"
    assert not any(item["code"] == "capture_unavailable" for item in current["warnings"])


def test_source_recovery_re_adopts_committed_complete_capture_with_original_identity(core):
    case_id, _, _ = core["create_case"](raw=b"Old source.", seq=10)
    gap_ref = _install_route(
        core,
        case_id,
        covered_seq=13,
        latest_seq=16,
        missing_start=14,
        missing_end=14,
    )
    earlier = core["capture"](b"Earlier complete source segment.", seq=13)
    final = core["capture"](b"The complete current source snapshot at seq16.", seq=16)
    assert _capture_arrived(core, case_id, earlier)["ok"]
    assert _capture_arrived(core, case_id, final)["ok"]

    first = _source_revise(
        core,
        case_id,
        [earlier["capture_id"], final["capture_id"]],
        source_ids=[],
        captures=[earlier, final],
    )
    assert first["ok"], first
    first_state = core["store"]().read_case(case_id)
    first_decision = first_state["decisions"][-1]
    first_ack_ref = next(ref for ref in first_decision["refs"] if "/attachments/source_adoption/" in ref["path"])
    assert json.loads(core["store"]().read_ref(first_ack_ref))["arrival_seq"] == 13
    assert first_state["pending_inputs"] == []
    assert first["result"]["state"]["current_validity"] == "unclassified"

    # Leaving the already-adopted source in source_ids alone is not new C.
    no_new_adoption = _source_revise(core, case_id, [], source_ids=[first_state["source_ids"][-1]])
    assert not no_new_adoption["ok"] and no_new_adoption["error"]["code"] == "capture_unavailable"
    assert core["store"]().read_case(case_id)["state_revision"] == first_state["state_revision"]

    # The original transfer bytes are no longer in trusted context or pending_inputs.
    # Re-adopting by the exact old capture_id reads only the committed Source refs.
    second = _source_revise(core, case_id, [final["capture_id"]], source_ids=[])
    assert second["ok"], second
    final_state = core["store"]().read_case(case_id)
    assert final_state["pending_inputs"] == []
    assert final_state["source_revision"] == first_state["source_revision"] + 1
    second_decision = final_state["decisions"][-1]
    second_ack_ref = next(ref for ref in second_decision["refs"] if "/attachments/source_adoption/" in ref["path"])
    second_ack = json.loads(core["store"]().read_ref(second_ack_ref))
    assert second_ack["gap_ref"] == gap_ref
    assert second_ack["capture_id"] == final["capture_id"]
    assert second_ack["arrival_seq"] == 16
    source_record = core["store"]().read_record(
        f".reaslab/optagent/cases/{case_id}/sources/{final_state['source_ids'][0]}.json"
    )
    origin = source_record["origin"]
    assert origin["capture_id"] == final["capture_id"]
    assert origin["arrival_epoch"] == final["arrival_epoch"]
    assert origin["arrival_seq"] == final["arrival_seq"]
    assert origin["transport_admission"] == final["admission"]
    assert source_record["original_sha256"] == final["sha256"]
    assert source_record["created_by"] == second_decision["stamp"]

    stable_state_digest = core["store"]().ref_for_path(f".reaslab/optagent/cases/{case_id}/state.json")["sha256"]
    replayed_old_adoption = _capture_arrived(core, case_id, earlier)
    assert replayed_old_adoption["ok"], replayed_old_adoption
    assert replayed_old_adoption["affected_case_ids"] == [case_id]
    assert core["store"]().read_case(case_id)["pending_inputs"] == []
    assert core["store"]().ref_for_path(f".reaslab/optagent/cases/{case_id}/state.json")["sha256"] == stable_state_digest
    assert core["call"]("optagent.case", {"op": "get", "case_id": case_id})["result"]["current_validity"] == "none"


def test_empty_and_unconfirmed_captures_cannot_recover_a_route_gap(core):
    case_id, _, _ = core["create_case"](raw=b"Old source.", seq=10)
    _install_route(core, case_id, covered_seq=10, latest_seq=11)
    unconfirmed = core["capture"](
        b"Maybe use a different requirement.", seq=12, channel="steer", admission="unconfirmed_steer"
    )
    empty = core["capture"](b"", seq=13, admission="received_prompt")
    assert _capture_arrived(core, case_id, unconfirmed)["ok"]
    assert _capture_arrived(core, case_id, empty)["ok"]
    before = core["store"]().read_case(case_id)
    for transfer in (unconfirmed, empty):
        result = _source_revise(core, case_id, [transfer["capture_id"]], source_ids=[], captures=[transfer])
        assert not result["ok"] and result["error"]["code"] == "capture_unavailable"
        assert core["store"]().read_case(case_id)["state_revision"] == before["state_revision"]
    state = core["store"]().read_case(case_id)
    assert len(state["pending_inputs"]) == 2
    assert not any(
        decision["kind"] == "source_change" and any("source_adoption" in ref["path"] for ref in decision["refs"])
        for decision in state["decisions"]
    )


def test_handoff_carries_verified_guard_and_readonly_target_recovers_it_without_member(core):
    case_id, _, _ = core["create_case"](raw=b"Old source.", seq=10)
    target_session = str(uuid.uuid4())
    target_stamp = {
        "session_id": target_session,
        "role": "root",
        "turn_id": str(uuid.uuid4()),
        "call_id": str(uuid.uuid4()),
    }
    gap_ref = _install_route(core, case_id, covered_seq=10, latest_seq=11, observed_by=target_stamp)
    old_index_path, _ = _route_paths(core["session_id"])
    old_index_digest = core["store"]().ref_for_path(old_index_path)["sha256"]
    raw = b"I am taking over this case."
    transfer = {
        "capture_id": str(uuid.uuid4()),
        "arrival_epoch": str(uuid.uuid4()),
        "arrival_seq": 1,
        "session_id": target_session,
        "channel": "prompt",
        "admission": "received_prompt",
        "utf8_bytes_base64": base64.b64encode(raw).decode("ascii"),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    target_context = {
        "stamp": target_stamp,
        "workspace_absolute": str(core["workspace"]),
        "cancellation_observed": False,
        "captures": [transfer],
    }
    state = core["store"]().read_case(case_id)
    handoff = dispatch_dynamic(
        "optagent.case",
        {
            "op": "revise",
            "kind": "handoff",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "capture_id": transfer["capture_id"],
            "reason": "Transfer ownership to the receiving session.",
        },
        target_context,
    )["reply"]
    assert handoff["ok"], handoff
    changed = core["store"]().read_case(case_id)
    assert changed["owner_session_id"] == target_session
    assert changed["pending_inputs"][-1]["capture_id"] == transfer["capture_id"]
    assert changed["pending_inputs"][-1]["arrival_seq"] == 1
    handoff_decision = changed["decisions"][-1]
    assert handoff_decision["kind"] == "handoff"
    assert gap_ref in handoff_decision["refs"]
    assert core["store"]().ref_for_path(old_index_path)["sha256"] == old_index_digest
    target_index_path, _ = _route_paths(target_session)
    assert not (core["workspace"] / target_index_path).exists()

    before = core["store"]().read_ref(core["store"]().ref_for_path(f".reaslab/optagent/cases/{case_id}/state.json"))
    target_read = dispatch_dynamic(
        "optagent.case",
        {"op": "get", "case_id": case_id},
        {**target_context, "captures": []},
    )["reply"]
    assert target_read["ok"]
    assert target_read["result"]["current_validity"] == "unclassified"
    assert any(item["code"] == "capture_unavailable" for item in target_read["warnings"])
    assert core["store"]().read_ref(core["store"]().ref_for_path(f".reaslab/optagent/cases/{case_id}/state.json")) == before


def test_handoff_stale_cas_keeps_old_owner_and_guard(core):
    case_id, _, _ = core["create_case"](raw=b"Old source.", seq=10)
    gap_ref = _install_route(core, case_id, covered_seq=10, latest_seq=11)
    target_session = str(uuid.uuid4())
    stamp = {"session_id": target_session, "role": "root", "turn_id": str(uuid.uuid4()), "call_id": str(uuid.uuid4())}
    raw = b"Take over."
    transfer = {
        "capture_id": str(uuid.uuid4()), "arrival_epoch": str(uuid.uuid4()), "arrival_seq": 1,
        "session_id": target_session, "channel": "prompt", "admission": "received_prompt",
        "utf8_bytes_base64": base64.b64encode(raw).decode("ascii"), "sha256": hashlib.sha256(raw).hexdigest(),
    }
    state = core["store"]().read_case(case_id)
    result = dispatch_dynamic(
        "optagent.case",
        {"op": "revise", "kind": "handoff", "case_id": case_id,
         "expected_state_revision": state["state_revision"] - 1,
         "capture_id": transfer["capture_id"], "reason": "Take over."},
        {"stamp": stamp, "workspace_absolute": str(core["workspace"]), "cancellation_observed": False, "captures": [transfer]},
    )["reply"]
    assert not result["ok"] and result["error"]["code"] == "stale_revision"
    unchanged = core["store"]().read_case(case_id)
    assert unchanged["owner_session_id"] == core["session_id"]
    assert not any(decision["kind"] == "handoff" for decision in unchanged["decisions"])
    assert gap_ref["path"].endswith(gap_ref["sha256"] + ".json")


def test_handoff_checks_both_queues_outside_locks_and_refreshes_unrelated_arrival(core, monkeypatch):
    from optagent_core.store import CaseStore

    case_id, _, _ = core["create_case"](raw=b"Old owner source.", seq=10)
    other_case, _, _ = core["create_case"](raw=b"Another registered case.", seq=11)
    _install_route_members(core, {case_id: 10, other_case: 11}, latest_seq=11)
    target_session = str(uuid.uuid4())
    target_stamp = {
        "session_id": target_session,
        "role": "root",
        "turn_id": str(uuid.uuid4()),
        "call_id": str(uuid.uuid4()),
    }
    raw = b"Transfer the case to the receiving session."
    transfer = {
        "capture_id": str(uuid.uuid4()),
        "arrival_epoch": str(uuid.uuid4()),
        "arrival_seq": 1,
        "session_id": target_session,
        "channel": "prompt",
        "admission": "received_prompt",
        "utf8_bytes_base64": base64.b64encode(raw).decode("ascii"),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    unrelated_transfer = {
        "capture_id": str(uuid.uuid4()),
        "arrival_epoch": str(uuid.uuid4()),
        "arrival_seq": 1,
        "session_id": target_session,
        "channel": "prompt",
        "admission": "received_prompt",
        "utf8_bytes_base64": base64.b64encode(b"Unrelated receiving-session input.").decode("ascii"),
        "sha256": hashlib.sha256(b"Unrelated receiving-session input.").hexdigest(),
    }
    receiver_digest = hashlib.sha256(target_session.encode("utf-8")).hexdigest()
    receiver_queue_path = f".reaslab/optagent/host/pending_control/{receiver_digest}.json"
    store = core["store"]()
    store.write_record(receiver_queue_path, {
        "schema_version": 1,
        "session_id": target_session,
        "workspace_absolute": str(core["workspace"]),
        "events": [{
            "event_id": str(uuid.uuid4()),
            "kind": "capture_arrived",
            "capture": unrelated_transfer,
            "bindings": [{"case_id": other_case, "run_id": None, "pending_start": False}],
        }],
    }, replace=True)

    session_lock_depth = 0
    original_session_lock = CaseStore.session_lock

    @contextlib.contextmanager
    def tracked_session_lock(current_store, digest):
        nonlocal session_lock_depth
        with original_session_lock(current_store, digest):
            session_lock_depth += 1
            try:
                yield
            finally:
                session_lock_depth -= 1

    monkeypatch.setattr(CaseStore, "session_lock", tracked_session_lock)
    original_pending_status = api._pending_control_status
    pending_calls = []

    def tracked_pending_status(current_store, session_id, target_case_id, **kwargs):
        pending_calls.append((session_id, target_case_id, session_lock_depth))
        assert session_lock_depth == 0
        return original_pending_status(current_store, session_id, target_case_id, **kwargs)

    monkeypatch.setattr(api, "_pending_control_status", tracked_pending_status)
    original_prepare = api._prepare_session_route_fence
    prepare_calls = 0
    unrelated_owner_event = None

    def prepare_then_queue_unrelated(store_arg, state, **kwargs):
        nonlocal prepare_calls, unrelated_owner_event
        fence = original_prepare(store_arg, state, **kwargs)
        if state["case_id"] == case_id:
            prepare_calls += 1
            if prepare_calls == 1:
                unrelated_owner_event = _queue_capture_arrival(core, other_case, seq=12)
        return fence

    monkeypatch.setattr(api, "_prepare_session_route_fence", prepare_then_queue_unrelated)
    state = store.read_case(case_id)
    reply = dispatch_dynamic(
        "optagent.case",
        {
            "op": "revise",
            "kind": "handoff",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "capture_id": transfer["capture_id"],
            "reason": "Transfer ownership while preserving unrelated session inputs.",
        },
        {
            "stamp": target_stamp,
            "workspace_absolute": str(core["workspace"]),
            "cancellation_observed": False,
            "captures": [transfer],
        },
    )["reply"]

    assert reply["ok"], reply
    assert prepare_calls == 2
    assert unrelated_owner_event is not None
    assert {session_id for session_id, case, depth in pending_calls if case == case_id} == {
        core["session_id"], target_session,
    }
    assert all(depth == 0 for _, case, depth in pending_calls if case == case_id)
    assert sum(session_id == target_session and case == case_id for session_id, case, _ in pending_calls) >= 2


def test_committed_handoff_without_target_member_is_readonly_unknown_even_without_old_gap(core):
    case_id, _, _ = core["create_case"](raw=b"Original requirement.", seq=10)
    _install_route(core, case_id, covered_seq=10, latest_seq=10, gap=False)
    target_session = str(uuid.uuid4())
    target_epoch = str(uuid.uuid4())
    target_stamp = {"session_id": target_session, "role": "root", "turn_id": str(uuid.uuid4()), "call_id": str(uuid.uuid4())}
    raw = b"Take over this case."
    transfer = {
        "capture_id": str(uuid.uuid4()),
        "arrival_epoch": target_epoch,
        "arrival_seq": 1,
        "session_id": target_session,
        "channel": "prompt",
        "admission": "received_prompt",
        "utf8_bytes_base64": base64.b64encode(raw).decode("ascii"),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    state = core["store"]().read_case(case_id)
    handoff = dispatch_dynamic(
        "optagent.case",
        {"op": "revise", "kind": "handoff", "case_id": case_id,
         "expected_state_revision": state["state_revision"], "capture_id": transfer["capture_id"],
         "reason": "Transfer ownership."},
        {"stamp": target_stamp, "workspace_absolute": str(core["workspace"]),
         "cancellation_observed": False, "captures": [transfer]},
    )["reply"]
    assert handoff["ok"], handoff
    target_index, _ = _route_paths(target_session)
    assert not (core["workspace"] / target_index).exists()

    before = core["store"]().read_ref(core["store"]().ref_for_path(f".reaslab/optagent/cases/{case_id}/state.json"))
    fetched = dispatch_dynamic(
        "optagent.case",
        {"op": "get", "case_id": case_id},
        {"stamp": target_stamp, "workspace_absolute": str(core["workspace"]),
         "cancellation_observed": False, "captures": []},
    )["reply"]
    assert fetched["ok"] and fetched["result"]["current_validity"] == "unclassified"
    assert any(item["code"] == "capture_unavailable" for item in fetched["warnings"])
    assert core["store"]().read_ref(core["store"]().ref_for_path(f".reaslab/optagent/cases/{case_id}/state.json")) == before


def test_handoff_and_current_owner_guards_are_both_required_and_adopted_together(core):
    from optagent_core.api import _route_freshness, _source_adoption_ack
    from test_reuse_proof import _proof_review_to_delivery

    case_id, _review_id, _proof_id, _claim, delivery_id = _proof_review_to_delivery(core)
    state = core["store"]().read_case(case_id)
    built = core["call"](
        "optagent.deliver",
        {"op": "build", "case_id": case_id, "expected_state_revision": state["state_revision"],
         "delivery_id": delivery_id, "target_path": "reports/handoff.md"},
    )
    assert built["ok"], built

    target_session = str(uuid.uuid4())
    target_epoch = str(uuid.uuid4())
    target_stamp = {"session_id": target_session, "role": "root", "turn_id": str(uuid.uuid4()), "call_id": str(uuid.uuid4())}
    old_gap = _install_route(core, case_id, covered_seq=10, latest_seq=11, observed_by=target_stamp)
    old_path, _ = _route_paths(core["session_id"])
    old_index = core["store"]().read_record(old_path)
    old_index["members"][0]["covered_seq"] = 10
    # Replace the helper's default 11 range with the exact old-session gap.
    old_audit = {
        "schema_version": 1,
        "case_id": case_id,
        "session_id": core["session_id"],
        "covered_seq": 10,
        "missing_seq_ranges": [{"start": 11, "end": 11}],
        "registered_by": core["store"]().read_case(case_id)["created_by"],
        "observed_by": target_stamp,
    }
    old_audit_raw = canonical_json_bytes(old_audit)
    old_digest = hashlib.sha256(old_audit_raw).hexdigest()
    old_gap = core["store"]().write_immutable(
        f".reaslab/optagent/host/route_audits/{old_digest}.json", old_audit_raw, "application/json"
    )
    old_index["members"][0]["gap_ref"] = old_gap
    core["store"]().write_record(old_path, old_index, replace=True)

    def target_transfer(text: bytes, seq: int) -> dict:
        return {
            "capture_id": str(uuid.uuid4()), "arrival_epoch": target_epoch, "arrival_seq": seq,
            "session_id": target_session, "channel": "prompt", "admission": "received_prompt",
            "utf8_bytes_base64": base64.b64encode(text).decode("ascii"),
            "sha256": hashlib.sha256(text).hexdigest(),
        }

    takeover = target_transfer(b"Take over this proof case.", 6)
    target_context = {
        "stamp": target_stamp,
        "workspace_absolute": str(core["workspace"]),
        "cancellation_observed": False,
        "captures": [takeover],
    }
    state = core["store"]().read_case(case_id)
    handed = dispatch_dynamic(
        "optagent.case",
        {"op": "revise", "kind": "handoff", "case_id": case_id,
         "expected_state_revision": state["state_revision"], "capture_id": takeover["capture_id"],
         "reason": "Transfer this proof case to the receiving owner."},
        target_context,
    )["reply"]
    assert handed["ok"], handed

    target_index_path, target_barrier_path = _route_paths(target_session)
    target_index = {
        "schema_version": 1,
        "session_id": target_session,
        "workspace_absolute": str(core["workspace"]),
        "index_revision": 1,
        "members": [{
            "case_id": case_id,
            "registered_by": target_stamp,
            "input_mode": "working",
            "covered_seq": 6,
            "gap_ref": None,
        }],
        "pending_starts": [],
    }
    core["store"]().write_record(target_index_path, target_index)
    core["store"]().write_record(target_barrier_path, {
        "session_id": target_session,
        "arrival_epoch": takeover["arrival_epoch"],
        "latest_observed_seq": 6,
    })

    def target_call(tool: str, args: dict, *, role: str = "root", captures: list[dict] | None = None) -> dict:
        stamp = {**target_stamp, "role": role, "turn_id": str(uuid.uuid4()), "call_id": str(uuid.uuid4())}
        return dispatch_dynamic(
            tool,
            args,
            {"stamp": stamp, "workspace_absolute": str(core["workspace"]),
             "cancellation_observed": False, "captures": list(captures or [])},
        )["reply"]

    state = core["store"]().read_case(case_id)
    classified = target_call(
        "optagent.case",
        {"op": "revise", "kind": "note_input", "case_id": case_id,
         "expected_state_revision": state["state_revision"], "capture_id": takeover["capture_id"],
         "disposition": "no_math_change", "reason": "Ownership transfer only."},
        captures=[takeover],
    )
    assert classified["ok"], classified

    first_snapshot = target_transfer(b"For every integer n, n(n+1) is even.", 8)
    first_arrival = dispatch_control_event(
        {"event_id": str(uuid.uuid4()), "kind": "capture_arrived", "capture": first_snapshot,
         "bindings": [{"case_id": case_id, "run_id": None, "pending_start": False}]},
        {"session_id": target_session, "workspace_absolute": str(core["workspace"])},
    )
    assert first_arrival["ok"], first_arrival
    first_adoption = _source_revise(
        {**core, "call": target_call},
        case_id,
        [first_snapshot["capture_id"]],
        source_ids=[],
        captures=[first_snapshot],
    )
    assert first_adoption["ok"], first_adoption
    first_state = core["store"]().read_case(case_id)
    first_refs = first_state["decisions"][-1]["refs"]
    first_ack_refs = [ref for ref in first_refs if "/attachments/source_adoption/" in ref["path"]]
    assert len(first_ack_refs) == 1
    assert json.loads(core["store"]().read_ref(first_ack_refs[0]))["gap_ref"] == old_gap

    # The old guard is now resolved, so B can register a fresh proof delivery
    # against the adopted source before a distinct B-owned gap is observed.
    current_state = core["store"]().read_case(case_id)
    current_review = target_call(
        "optagent.review",
        {"op": "begin", "case_id": case_id, "expected_state_revision": current_state["state_revision"],
         "candidate_id": None, "proof_ids": [], "run_ids": []},
        role="opt-verifier",
    )
    assert current_review["ok"], current_review
    current_review_id = current_review["result"]["review"]["id"]
    source_id = current_state["source_ids"][0]
    noted = target_call(
        "optagent.review",
        {"op": "note", "case_id": case_id, "review_id": current_review_id,
         "expected_review_revision": 1,
         "body": "The current source states that n(n+1) is even for every integer n.",
         "source_locations": [{"source_id": source_id, "locator": "source-level"}]},
        role="opt-verifier",
    )
    assert noted["ok"], noted
    (core["workspace"] / "proof-current.md").write_text(
        "Let n be even, so n(n+1) is even. If n is odd, n+1 is even, so the product is even.",
        encoding="utf-8",
    )
    current_state = core["store"]().read_case(case_id)
    current_proof = target_call(
        "optagent.review",
        {"op": "submit", "case_id": case_id, "expected_state_revision": current_state["state_revision"],
         "review_id": current_review_id, "expected_review_revision": 2, "record_kind": "proof",
         "kind": "analytic", "body_path": "proof-current.md", "premises": ["n is an integer"],
         "evidence_refs": [], "claim_kinds": ["formulation_fidelity"],
         "candidate_id": None},
    )
    assert current_proof["ok"], current_proof
    current_proof_id = current_proof["result"]["proof"]["id"]
    current_proof_ref = current_proof["result"]["proof"]["record_ref"]
    current_public_claim = {
        "kind": "formulation_fidelity",
        "scope": "The parity claim in the current source.",
        "conditions": [],
        "evidence_paths": [current_proof_ref["path"]],
        "support": "supported",
    }
    answer_path = "proof-current.md"
    current_closed = target_call(
        "optagent.review",
        {"op": "submit", "case_id": case_id, "review_id": current_review_id,
         "expected_review_revision": 2, "record_kind": "outcome", "decision": "ready",
         "claims": [current_public_claim],
         "feedback": [], "remaining": [], "report_path": answer_path},
        role="opt-verifier",
    )
    assert current_closed["ok"], current_closed
    current_state = core["store"]().read_case(case_id)
    current_claim = {"kind": "formulation_fidelity", "scope": "The parity claim in the current source.",
                     "conditions": [], "evidence_refs": [current_proof_ref], "support": "supported"}
    current_selection = target_call(
        "optagent.deliver",
        {"op": "select", "case_id": case_id, "expected_state_revision": current_state["state_revision"],
         "kind": "proof", "candidate_id": None,
         "review_id": current_review_id, "proof_ids": [current_proof_id], "run_ids": []},
    )
    assert current_selection["ok"], current_selection
    current_delivery_id = current_selection["result"]["delivery"]["id"]
    current_state = core["store"]().read_case(case_id)
    current_built = target_call(
        "optagent.deliver",
        {"op": "build", "case_id": case_id, "expected_state_revision": current_state["state_revision"],
         "delivery_id": current_delivery_id, "target_path": "reports/handoff_current.md",
        },
    )
    assert current_built["ok"], current_built

    # The old A guard is now acknowledged. A newly discovered B gap remains
    # independent and keeps read-only case, list, and audit views unknown.
    observer = {"session_id": target_session, "role": "root", "turn_id": str(uuid.uuid4()), "call_id": str(uuid.uuid4())}
    target_gap_audit = {
        "schema_version": 1,
        "case_id": case_id,
        "session_id": target_session,
        "covered_seq": 6,
        "missing_seq_ranges": [{"start": 7, "end": 7}],
        "registered_by": target_stamp,
        "observed_by": observer,
    }
    target_gap_raw = canonical_json_bytes(target_gap_audit)
    target_gap_digest = hashlib.sha256(target_gap_raw).hexdigest()
    target_gap = core["store"]().write_immutable(
        f".reaslab/optagent/host/route_audits/{target_gap_digest}.json", target_gap_raw, "application/json"
    )
    target_index["members"][0]["gap_ref"] = target_gap
    target_index["index_revision"] = 2
    core["store"]().write_record(target_index_path, target_index, replace=True)
    assessment = _route_freshness(core["store"](), core["store"]().read_case(case_id))
    assert assessment["unknown"]
    assert len(assessment["guards"]) == 2
    assert sum(guard["ack"] is not None for guard in assessment["guards"]) == 1
    assert any(guard["gap_ref"] == target_gap and guard["ack"] is None for guard in assessment["guards"])

    state_path = f".reaslab/optagent/cases/{case_id}/state.json"
    state_before = core["store"]().read_ref(core["store"]().ref_for_path(state_path))
    index_before = core["store"]().read_ref(core["store"]().ref_for_path(target_index_path))
    fetched = target_call("optagent.case", {"op": "get", "case_id": case_id})
    listed = target_call("optagent.case", {"op": "list"})
    audited = target_call(
        "optagent.deliver",
        {"op": "audit", "case_id": case_id, "delivery_id": current_delivery_id,
         "report_path": "reports/handoff_current.md"},
        role="opt-verifier",
    )
    assert fetched["ok"] and fetched["result"]["current_validity"] == "unclassified"
    listed_case = next(row for row in listed["result"]["cases"] if row["case_id"] == case_id)
    assert listed_case["current_validity"] == "unclassified"
    assert audited["ok"] and audited["result"]["status"] == "consistent"
    for reply in (fetched, listed, audited):
        assert any(warning["code"] == "capture_unavailable" for warning in reply["warnings"])
    assert core["store"]().read_ref(core["store"]().ref_for_path(state_path)) == state_before
    assert core["store"]().read_ref(core["store"]().ref_for_path(target_index_path)) == index_before

    # The old A guard remains resolved by its original committed ack. B's full
    # snapshot writes one new adoption Ref for the unresolved B guard; a later
    # unselected capture still becomes a pending fence.
    second_snapshot = target_transfer(b"Complete replacement snapshot from owner B, revision two.", 9)
    second_arrival = dispatch_control_event(
        {"event_id": str(uuid.uuid4()), "kind": "capture_arrived", "capture": second_snapshot,
         "bindings": [{"case_id": case_id, "run_id": None, "pending_start": False}]},
        {"session_id": target_session, "workspace_absolute": str(core["workspace"])},
    )
    assert second_arrival["ok"], second_arrival
    second_adoption = _source_revise(
        {**core, "call": target_call},
        case_id,
        [second_snapshot["capture_id"]],
        source_ids=[],
        captures=[second_snapshot],
    )
    assert second_adoption["ok"], second_adoption
    second_state = core["store"]().read_case(case_id)
    second_ack_refs = [ref for ref in second_state["decisions"][-1]["refs"] if "/attachments/source_adoption/" in ref["path"]]
    assert len(second_ack_refs) == 1
    assert {json.loads(core["store"]().read_ref(ref))["gap_ref"]["path"] for ref in second_ack_refs} == {
        target_gap["path"]
    }
    assert not _route_freshness(core["store"](), second_state)["unknown"]

    # A later normal source commit may replace the active Source set, and a
    # later owner may differ; neither invalidates B's committed proof that the
    # exact old A guard was covered. The original Source refs remain checkable
    # through that Decision even after they leave the active set.
    historical_state = {**second_state, "owner_session_id": str(uuid.uuid4())}
    assert _source_adoption_ack(core["store"](), historical_state, old_gap) is not None

    later = target_transfer(b"A later change not selected by the preceding sources operation.", 10)
    later_arrival = dispatch_control_event(
        {"event_id": str(uuid.uuid4()), "kind": "capture_arrived", "capture": later,
         "bindings": [{"case_id": case_id, "run_id": None, "pending_start": False}]},
        {"session_id": target_session, "workspace_absolute": str(core["workspace"])},
    )
    assert later_arrival["ok"], later_arrival
    final_state = core["store"]().read_case(case_id)
    assert [item["capture_id"] for item in final_state["pending_inputs"]] == [later["capture_id"]]
    assert not _route_freshness(core["store"](), final_state)["unknown"]


def test_one_sources_cas_records_each_unresolved_inherited_and_current_guard(core):
    from optagent_core.api import _route_freshness

    case_id, _, _ = core["create_case"](raw=b"Original owner source.", seq=10)
    inherited_gap = _install_route(core, case_id, covered_seq=10, latest_seq=11)
    target_session = str(uuid.uuid4())
    target_epoch = str(uuid.uuid4())
    target_stamp = {
        "session_id": target_session,
        "role": "root",
        "turn_id": str(uuid.uuid4()),
        "call_id": str(uuid.uuid4()),
    }

    def transfer(raw: bytes, seq: int) -> dict:
        return {
            "capture_id": str(uuid.uuid4()),
            "arrival_epoch": target_epoch,
            "arrival_seq": seq,
            "session_id": target_session,
            "channel": "prompt",
            "admission": "received_prompt",
            "utf8_bytes_base64": base64.b64encode(raw).decode("ascii"),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    takeover = transfer(b"Take over this case.", 6)
    target_context = {
        "stamp": target_stamp,
        "workspace_absolute": str(core["workspace"]),
        "cancellation_observed": False,
        "captures": [takeover],
    }
    state = core["store"]().read_case(case_id)
    handoff = dispatch_dynamic(
        "optagent.case",
        {"op": "revise", "kind": "handoff", "case_id": case_id,
         "expected_state_revision": state["state_revision"], "capture_id": takeover["capture_id"],
         "reason": "Transfer ownership while preserving the unresolved input guard."},
        target_context,
    )["reply"]
    assert handoff["ok"], handoff
    after_handoff = core["store"]().read_case(case_id)
    handoff_refs = after_handoff["decisions"][-1]["refs"]
    assert inherited_gap in handoff_refs

    target_index_path, target_barrier_path = _route_paths(target_session)
    current_gap_audit = {
        "schema_version": 1,
        "case_id": case_id,
        "session_id": target_session,
        "covered_seq": 7,
        "missing_seq_ranges": [{"start": 8, "end": 8}],
        "registered_by": target_stamp,
        "observed_by": target_stamp,
    }
    gap_raw = canonical_json_bytes(current_gap_audit)
    gap_digest = hashlib.sha256(gap_raw).hexdigest()
    current_gap = core["store"]().write_immutable(
        f".reaslab/optagent/host/route_audits/{gap_digest}.json", gap_raw, "application/json"
    )
    core["store"]().write_record(target_index_path, {
        "schema_version": 1,
        "session_id": target_session,
        "workspace_absolute": str(core["workspace"]),
        "index_revision": 1,
        "members": [{
            "case_id": case_id,
            "registered_by": target_stamp,
            "input_mode": "working",
            "covered_seq": 7,
            "gap_ref": current_gap,
        }],
        "pending_starts": [],
    })
    core["store"]().write_record(target_barrier_path, {
        "session_id": target_session,
        "arrival_epoch": target_epoch,
        "latest_observed_seq": 8,
    })

    def target_call(tool: str, args: dict, *, captures: list[dict] | None = None) -> dict:
        stamp = {**target_stamp, "turn_id": str(uuid.uuid4()), "call_id": str(uuid.uuid4())}
        return dispatch_dynamic(
            tool,
            args,
            {"stamp": stamp, "workspace_absolute": str(core["workspace"]),
             "cancellation_observed": False, "captures": list(captures or [])},
        )["reply"]

    # Resolve the actual handoff request without claiming that it is math.
    state = core["store"]().read_case(case_id)
    classified = target_call(
        "optagent.case",
        {"op": "revise", "kind": "note_input", "case_id": case_id,
         "expected_state_revision": state["state_revision"], "capture_id": takeover["capture_id"],
         "disposition": "no_math_change", "reason": "The handoff request only changes ownership."},
        captures=[takeover],
    )
    assert classified["ok"], classified

    first = transfer(b"Owner B's complete source snapshot at seq7.", 7)
    arrived = dispatch_control_event(
        {"event_id": str(uuid.uuid4()), "kind": "capture_arrived", "capture": first,
         "bindings": [{"case_id": case_id, "run_id": None, "pending_start": False}]},
        {"session_id": target_session, "workspace_absolute": str(core["workspace"])},
    )
    assert arrived["ok"] and arrived["affected_case_ids"] == [case_id]
    state = core["store"]().read_case(case_id)
    first_result = target_call(
        "optagent.case",
        {"op": "revise", "kind": "sources", "case_id": case_id,
         "expected_state_revision": state["state_revision"], "source_ids": [], "facts": [],
         "capture_ids": [first["capture_id"]],
         "reason": "Replace the active source set from the current owner's accepted snapshot."},
        captures=[first],
    )
    assert first_result["ok"], first_result
    first_state = core["store"]().read_case(case_id)
    first_adoptions = [ref for ref in first_state["decisions"][-1]["refs"] if "/attachments/source_adoption/" in ref["path"]]
    assert len(first_adoptions) == 2
    assert {json.loads(core["store"]().read_ref(ref))["gap_ref"]["path"] for ref in first_adoptions} == {
        inherited_gap["path"], current_gap["path"]
    }
    assessment = _route_freshness(core["store"](), first_state)
    assert assessment["unknown"]
    assert sum(guard["ack"] is not None for guard in assessment["guards"]) == 2
    assert any(guard["gap_ref"] == current_gap and guard["unresolved"] for guard in assessment["guards"])
    assert any(guard["gap_ref"] == inherited_gap and not guard["unresolved"] for guard in assessment["guards"])

    # The seq8 snapshot resolves only B's still-open guard. A's historical ack
    # remains in its original committed Decision and is not rewritten.
    second = transfer(b"Owner B's complete source snapshot at seq8.", 8)
    arrived = dispatch_control_event(
        {"event_id": str(uuid.uuid4()), "kind": "capture_arrived", "capture": second,
         "bindings": [{"case_id": case_id, "run_id": None, "pending_start": False}]},
        {"session_id": target_session, "workspace_absolute": str(core["workspace"])},
    )
    assert arrived["ok"] and arrived["affected_case_ids"] == [case_id]
    state = core["store"]().read_case(case_id)
    second_result = target_call(
        "optagent.case",
        {"op": "revise", "kind": "sources", "case_id": case_id,
         "expected_state_revision": state["state_revision"], "source_ids": [], "facts": [],
         "capture_ids": [second["capture_id"]],
         "reason": "Complete the receiving owner's remaining source coverage."},
        captures=[second],
    )
    assert second_result["ok"], second_result
    second_state = core["store"]().read_case(case_id)
    second_adoptions = [ref for ref in second_state["decisions"][-1]["refs"] if "/attachments/source_adoption/" in ref["path"]]
    assert len(second_adoptions) == 1
    assert json.loads(core["store"]().read_ref(second_adoptions[0]))["gap_ref"] == current_gap
    assert not _route_freshness(core["store"](), second_state)["unknown"]

    later = transfer(b"A later unselected change remains fenced.", 9)
    arrived = dispatch_control_event(
        {"event_id": str(uuid.uuid4()), "kind": "capture_arrived", "capture": later,
         "bindings": [{"case_id": case_id, "run_id": None, "pending_start": False}]},
        {"session_id": target_session, "workspace_absolute": str(core["workspace"])},
    )
    assert arrived["ok"] and arrived["affected_case_ids"] == [case_id]
    final_state = core["store"]().read_case(case_id)
    assert [item["capture_id"] for item in final_state["pending_inputs"]] == [later["capture_id"]]
    assert final_state["pending_inputs"][0]["disposition"] == "unclassified"
    # The route gaps are resolved. This later capture remains independently
    # fenced as an unclassified Case input; it does not make route freshness
    # unknown until it is queued or represented by a route gap.
    assert not _route_freshness(core["store"](), final_state)["unknown"]


def test_present_owner_index_missing_known_case_is_readonly_unknown(core):
    case_id, _, _ = core["create_case"](raw=b"Original requirement.", seq=10)
    _install_route(core, case_id, covered_seq=10, latest_seq=10, gap=False)
    store = core["store"]()
    index_path, _ = _route_paths(core["session_id"])
    index = store.read_record(index_path)
    index["members"] = []
    store.write_record(index_path, index, replace=True)
    state_path = f".reaslab/optagent/cases/{case_id}/state.json"
    state_before = store.read_ref(store.ref_for_path(state_path))
    index_before = store.read_ref(store.ref_for_path(index_path))

    fetched = core["call"]("optagent.case", {"op": "get", "case_id": case_id})
    assert fetched["ok"]
    assert fetched["result"]["current_validity"] == "unclassified"
    assert any(item["code"] == "capture_unavailable" for item in fetched["warnings"])
    assert store.read_ref(store.ref_for_path(state_path)) == state_before
    assert store.read_ref(store.ref_for_path(index_path)) == index_before


def test_new_owner_can_adopt_real_sources_without_comparing_old_session_sequence(core):
    case_id, _, _ = core["create_case"](raw=b"Old source.", seq=10)
    old_gap = _install_route(core, case_id, covered_seq=10, latest_seq=11)
    target_session = str(uuid.uuid4())
    target_epoch = str(uuid.uuid4())
    target_stamp = {"session_id": target_session, "role": "root", "turn_id": str(uuid.uuid4()), "call_id": str(uuid.uuid4())}

    def transfer(raw: bytes, seq: int) -> dict:
        return {
            "capture_id": str(uuid.uuid4()),
            "arrival_epoch": target_epoch,
            "arrival_seq": seq,
            "session_id": target_session,
            "channel": "prompt",
            "admission": "received_prompt",
            "utf8_bytes_base64": base64.b64encode(raw).decode("ascii"),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    handoff_input = transfer(b"Take over the existing case.", 1)
    target_context = {
        "stamp": target_stamp,
        "workspace_absolute": str(core["workspace"]),
        "cancellation_observed": False,
        "captures": [handoff_input],
    }
    state = core["store"]().read_case(case_id)
    handoff = dispatch_dynamic(
        "optagent.case",
        {"op": "revise", "kind": "handoff", "case_id": case_id,
         "expected_state_revision": state["state_revision"], "capture_id": handoff_input["capture_id"],
         "reason": "Transfer the case to the receiving owner."},
        target_context,
    )["reply"]
    assert handoff["ok"], handoff

    state = core["store"]().read_case(case_id)
    classify_handoff = dispatch_dynamic(
        "optagent.case",
        {"op": "revise", "kind": "note_input", "case_id": case_id,
         "expected_state_revision": state["state_revision"], "capture_id": handoff_input["capture_id"],
         "disposition": "no_math_change", "reason": "The handoff request itself changes no mathematical source."},
        target_context,
    )["reply"]
    assert classify_handoff["ok"], classify_handoff

    store = core["store"]()
    index_path, barrier_path = _route_paths(target_session)
    store.write_record(index_path, {
        "schema_version": 1,
        "session_id": target_session,
        "workspace_absolute": str(core["workspace"]),
        "index_revision": 1,
        "members": [{
            "case_id": case_id,
            "registered_by": target_stamp,
            "input_mode": "working",
            "covered_seq": 1,
            "gap_ref": old_gap,
        }],
        "pending_starts": [],
    })
    store.write_record(barrier_path, {
        "session_id": target_session,
        "arrival_epoch": handoff_input["arrival_epoch"],
        "latest_observed_seq": 1,
    })
    replacement = transfer(b"Complete new-owner source set.", 2)
    arrived = dispatch_control_event(
        {"event_id": str(uuid.uuid4()), "kind": "capture_arrived", "capture": replacement,
         "bindings": [{"case_id": case_id, "run_id": None, "pending_start": False}]},
        {"session_id": target_session, "workspace_absolute": str(core["workspace"])},
    )
    assert arrived["ok"] and arrived["affected_case_ids"] == [case_id]
    state = store.read_case(case_id)
    adopted = dispatch_dynamic(
        "optagent.case",
        {"op": "revise", "kind": "sources", "case_id": case_id,
         "expected_state_revision": state["state_revision"], "source_ids": [], "facts": [],
         "capture_ids": [replacement["capture_id"]],
         "reason": "Use the complete current requirements from this receiving session."},
        {**target_context, "captures": [replacement]},
    )["reply"]
    assert adopted["ok"], adopted
    updated = store.read_case(case_id)
    assert updated["owner_session_id"] == target_session
    assert updated["pending_inputs"] == []
    adoption_ref = next(ref for ref in updated["decisions"][-1]["refs"] if "/attachments/source_adoption/" in ref["path"])
    adoption = json.loads(store.read_ref(adoption_ref))
    adopted_record_ref = next(
        ref for ref in adoption["source_refs"]
        if ref["path"].startswith(f".reaslab/optagent/cases/{case_id}/sources/")
        and ref["path"].endswith(".json")
    )
    adopted_source_id = adopted_record_ref["path"].rsplit("/", 1)[-1][:-5]
    assert adoption["session_id"] == target_session
    assert adoption["arrival_seq"] == 2
    assert adoption["gap_ref"] == old_gap
    old_session_read = core["call"]("optagent.case", {"op": "get", "case_id": case_id})
    assert old_session_read["ok"] and old_session_read["result"]["current_validity"] == "none"
    target_read = dispatch_dynamic(
        "optagent.case", {"op": "get", "case_id": case_id}, {**target_context, "captures": []}
    )["reply"]
    assert target_read["ok"]
    assert target_read["result"]["current_validity"] == "none"
    assert not any(item["code"] == "capture_unavailable" for item in target_read["warnings"])

    # A later source replacement must not invalidate the exact historical
    # Source Ref named by the already verified route-adoption acknowledgement.
    replacement_after_adoption = transfer(b"Updated current requirements.", 3)
    arrived = dispatch_control_event(
        {"event_id": str(uuid.uuid4()), "kind": "capture_arrived", "capture": replacement_after_adoption,
         "bindings": [{"case_id": case_id, "run_id": None, "pending_start": False}]},
        {"session_id": target_session, "workspace_absolute": str(core["workspace"])},
    )
    assert arrived["ok"] and arrived["affected_case_ids"] == [case_id]
    state = store.read_case(case_id)
    replaced = dispatch_dynamic(
        "optagent.case",
        {"op": "revise", "kind": "sources", "case_id": case_id,
         "expected_state_revision": state["state_revision"], "source_ids": [], "facts": [],
         "capture_ids": [replacement_after_adoption["capture_id"]],
         "reason": "Replace the active requirement set with the latest complete statement."},
        {**target_context, "captures": [replacement_after_adoption]},
    )["reply"]
    assert replaced["ok"], replaced
    current = store.read_case(case_id)
    assert adopted_source_id not in current["source_ids"]

    verifier_stamp = {**target_stamp, "role": "opt-verifier"}
    begun_after_replace = dispatch_dynamic(
        "optagent.review",
        {"op": "begin", "case_id": case_id, "expected_state_revision": current["state_revision"],
         "candidate_id": None, "proof_ids": [], "run_ids": []},
        {**target_context, "stamp": verifier_stamp, "captures": []},
    )["reply"]
    assert begun_after_replace["ok"], begun_after_replace
    provenance_ref = next(
        ref for ref in begun_after_replace["result"]["raw_source_refs"]
        if ref["path"].endswith("/source-provenance.json")
    )
    provenance = json.loads(store.read_ref(provenance_ref))
    active_ids = {item["source_id"] for item in provenance["sources"]}
    historical_adoptions = [
        item for guard in provenance["route"]["guards"]
        for item in guard.get("adoption", {}).get("sources", [])
    ]
    assert active_ids == set(current["source_ids"])
    matching_historical = [item for item in historical_adoptions if item["source_id"] == adopted_source_id]
    assert len(matching_historical) == 1
    historical_source = matching_historical[0]
    assert historical_source["raw"]["ref"] in begun_after_replace["result"]["raw_source_refs"]
    assert store.read_ref(historical_source["raw"]["ref"]) == b"Complete new-owner source set."


def test_stale_source_revise_returns_current_fence_before_creating_source_or_adoption(core):
    case_id, _, _ = core["create_case"](raw=b"Old requirement set.", seq=10)
    _install_route(core, case_id, covered_seq=10, latest_seq=11)
    before_state = core["store"]().read_case(case_id)
    original_source_ids = list(before_state["source_ids"])

    transfer = core["capture"](b"Complete replacement requirement.", seq=11)
    arrived = _capture_arrived(core, case_id, transfer)
    assert arrived["ok"], arrived
    after_arrival = core["store"]().read_case(case_id)
    assert after_arrival["state_revision"] == before_state["state_revision"] + 1
    source_dir = core["workspace"] / ".reaslab/optagent" / "cases" / case_id / "sources"
    source_files_before = sorted(path.name for path in source_dir.glob("*.json"))
    adoption_dir = core["workspace"] / ".reaslab/optagent" / "cases" / case_id / "attachments" / "source_adoption"

    stale = _source_revise(
        core,
        case_id,
        [transfer["capture_id"]],
        expected_revision=before_state["state_revision"],
        captures=[transfer],
    )
    assert not stale["ok"]
    assert stale["error"]["code"] == "stale_revision"
    assert stale["state_revision"] == after_arrival["state_revision"]
    details = stale["error"]["details"]
    assert details["actual"] == after_arrival["state_revision"]
    assert details["current_state"]["state_revision"] == after_arrival["state_revision"]
    assert details["pending_captures"] == [
        {
            "capture_id": transfer["capture_id"],
            "arrival_epoch": transfer["arrival_epoch"],
            "arrival_seq": transfer["arrival_seq"],
            "channel": transfer["channel"],
            "admission": transfer["admission"],
            "raw_ref": after_arrival["pending_inputs"][0]["raw_ref"],
        }
    ]
    assert "preview" not in str(details)
    assert "note" not in details["pending_captures"][0]
    assert core["store"]().read_case(case_id)["source_ids"] == original_source_ids
    assert sorted(path.name for path in source_dir.glob("*.json")) == source_files_before
    assert not adoption_dir.exists()

    retried = _source_revise(core, case_id, [transfer["capture_id"]], captures=[transfer])
    assert retried["ok"], retried
    assert retried["result"]["state"]["state_revision"] == after_arrival["state_revision"] + 1


def test_source_cas_race_returns_latest_safe_state_and_pending_capture(core, monkeypatch):
    from optagent_core.store import CaseStore

    case_id, _, _ = core["create_case"](raw=b"Original source set.", seq=1)
    _install_route(core, case_id, covered_seq=1, latest_seq=1)
    transfer = core["capture"](b"Complete replacement source set.", seq=2)
    arrived = _capture_arrived(core, case_id, transfer)
    assert arrived["ok"], arrived
    current = core["store"]().read_case(case_id)
    original_cas = CaseStore.cas_case
    injected = False

    def race_once(store, target_case_id, expected_revision, update):
        nonlocal injected
        if target_case_id == case_id and not injected:
            injected = True
            original_cas(
                store,
                target_case_id,
                expected_revision,
                lambda state: state.update({"title": "Concurrent title update"}),
            )
        return original_cas(store, target_case_id, expected_revision, update)

    monkeypatch.setattr(CaseStore, "cas_case", race_once)
    stale = _source_revise(
        core,
        case_id,
        [transfer["capture_id"]],
        expected_revision=current["state_revision"],
        captures=[transfer],
    )
    assert injected, stale
    assert not stale["ok"]
    assert stale["error"]["code"] == "stale_revision"
    details = stale["error"]["details"]
    after_race = core["store"]().read_case(case_id)
    assert stale["state_revision"] == after_race["state_revision"]
    assert details["actual"] == after_race["state_revision"]
    assert details["current_state"]["title"] == "Concurrent title update"
    assert details["current_state"]["state_revision"] == after_race["state_revision"]
    assert details["pending_captures"] == [
        {
            "capture_id": transfer["capture_id"],
            "arrival_epoch": transfer["arrival_epoch"],
            "arrival_seq": transfer["arrival_seq"],
            "channel": transfer["channel"],
            "admission": transfer["admission"],
            "raw_ref": after_race["pending_inputs"][0]["raw_ref"],
        }
    ]
    retried = _source_revise(core, case_id, [transfer["capture_id"]], captures=[transfer])
    assert retried["ok"], retried


def test_source_cas_stale_reply_does_not_disclose_new_owner_pending_capture(core, monkeypatch):
    from optagent_core.store import CaseStore

    case_id, _, _ = core["create_case"](raw=b"Original source set.", seq=1)
    _install_route(core, case_id, covered_seq=1, latest_seq=2)
    old_transfer = core["capture"](b"Old owner recovery source.", seq=2)
    arrived = _capture_arrived(core, case_id, old_transfer)
    assert arrived["ok"], arrived
    adopted = _source_revise(core, case_id, [old_transfer["capture_id"]], captures=[old_transfer])
    assert adopted["ok"], adopted
    assert core["store"]().read_case(case_id)["pending_inputs"] == []
    before = core["store"]().read_case(case_id)

    new_session = str(uuid.uuid4())
    new_raw = b"New owner private pending input."
    new_transfer = {
        "capture_id": str(uuid.uuid4()),
        "arrival_epoch": str(uuid.uuid4()),
        "arrival_seq": 1,
        "session_id": new_session,
        "channel": "prompt",
        "admission": "received_prompt",
        "utf8_bytes_base64": base64.b64encode(new_raw).decode("ascii"),
        "sha256": hashlib.sha256(new_raw).hexdigest(),
    }
    target_context = {
        "stamp": {
            "session_id": new_session,
            "role": "root",
            "turn_id": str(uuid.uuid4()),
            "call_id": str(uuid.uuid4()),
        },
        "workspace_absolute": str(core["workspace"]),
        "cancellation_observed": False,
        "captures": [new_transfer],
    }
    handoff_args = {
        "op": "revise",
        "kind": "handoff",
        "case_id": case_id,
        "expected_state_revision": before["state_revision"],
        "capture_id": new_transfer["capture_id"],
        "reason": "Transfer ownership to the receiving session.",
    }
    original_session_lock = CaseStore.session_lock
    injected = False
    handoff_reply = None

    @contextlib.contextmanager
    def race_handoff_before_session_lock(store, digest):
        nonlocal injected, handoff_reply
        if not injected:
            injected = True
            handoff_reply = dispatch_dynamic("optagent.case", handoff_args, target_context)["reply"]
            assert handoff_reply["ok"], handoff_reply
        with original_session_lock(store, digest):
            yield

    monkeypatch.setattr(CaseStore, "session_lock", race_handoff_before_session_lock)
    stale = _source_revise(
        core,
        case_id,
        [old_transfer["capture_id"]],
        expected_revision=before["state_revision"],
        captures=[old_transfer],
    )
    assert injected and handoff_reply and handoff_reply["ok"]
    assert not stale["ok"] and stale["error"]["code"] == "stale_revision"
    current = core["store"]().read_case(case_id)
    assert current["owner_session_id"] == new_session
    new_fence = next(item for item in current["pending_inputs"] if item["capture_id"] == new_transfer["capture_id"])

    details = stale["error"]["details"]
    assert details == {
        "expected": before["state_revision"],
        "actual": current["state_revision"],
    }
    stale_json = json.dumps(stale, sort_keys=True)
    assert new_transfer["capture_id"] not in stale_json
    assert new_fence["raw_ref"]["path"] not in stale_json

    old_owner_inputs = core["call"](
        "optagent.case",
        {"op": "inputs", "case_id": case_id, "after_arrival_seq": 0, "limit": 20},
        captures=[],
    )
    assert not old_owner_inputs["ok"]
    assert old_owner_inputs["error"]["code"] == "permission_denied"


def test_review_begin_gives_verifier_route_scope_and_true_source_refs_before_note(core):
    case_id, _, _ = core["create_case"](
        raw=b"The original requirement set does not include the lost sequence.",
        seq=10,
        task_kind="proof",
    )
    gap_ref = _install_route(core, case_id, covered_seq=10, latest_seq=12, missing_start=11, missing_end=12)
    state = core["store"]().read_case(case_id)
    begun = core["call"](
        "optagent.review",
        {
            "op": "begin",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "candidate_id": None,
            "proof_ids": [],
            "run_ids": [],
        },
        role="opt-verifier",
    )
    assert begun["ok"], begun
    refs = begun["result"]["raw_source_refs"]
    provenance_ref = next(ref for ref in refs if ref["path"].endswith("/source-provenance.json"))
    projection = json.loads(core["store"]().read_ref(provenance_ref))
    gap_audit = json.loads(core["store"]().read_ref(gap_ref))
    assert projection["route"]["unknown"] is True
    assert projection["route"]["guards"] == [
        {
            "gap_ref": gap_ref,
            "scope": {
                "session_id": core["session_id"],
                "covered_seq": 10,
                "missing_seq_ranges": [{"start": 11, "end": 12}],
                "registered_by": state["created_by"],
                "observed_by": gap_audit["observed_by"],
            },
            "unresolved": True,
        }
    ]
    source_id = state["source_ids"][0]
    source_projection = projection["sources"][0]
    assert source_projection["source_id"] == source_id
    raw_ref = source_projection["raw"]["ref"]
    assert raw_ref in refs
    assert core["store"]().read_ref(raw_ref) == b"The original requirement set does not include the lost sequence."
