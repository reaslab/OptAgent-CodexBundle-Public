from __future__ import annotations

from optagent_core.review import _make_issue, _validate_issue_snapshot
from optagent_core.contracts import strict_json_loads


def test_issue_and_owner_disposition_snapshot_needs_no_patch_attachment():
    source_id = "00000000-0000-4000-8000-000000000001"
    verifier_stamp = {
        "session_id": "verifier-session", "role": "opt-verifier",
        "turn_id": "verifier-turn", "call_id": "verifier-call",
    }
    owner_stamp = {
        "session_id": "owner-session", "role": "root",
        "turn_id": "owner-turn", "call_id": "owner-call",
    }
    issue = _make_issue(
        object(),
        {"raw_source_ids": [source_id]},
        {
            "claim": "The candidate omits the stated source condition.",
            "locations": [{"source_id": source_id, "locator": "source-level"}],
            "candidate_locations": ["model.py:12"],
            "evidence_refs": [],
        },
        verifier_stamp,
    )
    issue["owner_dispositions"].append({
        "diagnosis": "confirmed",
        "reason": "The cited condition is absent from the current Candidate.",
        "patch_status": "unchecked",
        "stamp": owner_stamp,
        "created_at": "2026-10-24T00:00:00Z",
    })

    _validate_issue_snapshot(issue)
    assert set(issue) == {
        "issue_id", "claim", "locations", "candidate_locations", "evidence_refs",
        "proposal_stamp", "owner_dispositions",
    }
    assert set(issue["owner_dispositions"][0]) == {
        "diagnosis", "reason", "patch_status", "stamp", "created_at",
    }


def test_raw_first_review_keeps_owner_disposition_separate(core, monkeypatch):
    case_id, _, _ = core["create_case"](seq=1)
    other_case, _, _ = core["create_case"](seq=2)
    (core["workspace"] / "model.py").write_text(
        "# CANDIDATE_GOLD_SENTINEL\ndef build_model(data):\n    return None\n",
        encoding="utf-8",
    )
    state = core["store"]().read_case(case_id)
    store = core["store"]()
    source_id = state["source_ids"][0]
    source_path = f".reaslab/optagent/cases/{case_id}/sources/{source_id}.json"
    source_record = store.read_record(source_path)
    source_record["locations"] = ["OWNER_LOCATION_SENTINEL"]
    source_record["owner_note"] = "OWNER_NOTE_SENTINEL"
    store.write_record(source_path, source_record, replace=True)
    candidate = core["call"](
        "optagent.case",
        {
            "op": "revise",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "kind": "candidate",
            "model_path": "model.py",
            "local_module_paths": [],
            "data_id": None,
            "backend_id": "highs",
            "objective_plan": {"mode": "feasibility", "preference_note": "test only"},
            "business_checker_path": None,
            "input_closure": "declared",
            "closure_note": "All inputs are declared.",
        },
    )
    assert candidate["ok"], candidate
    candidate_id = candidate["result"]["candidate"]["id"]
    state = core["store"]().read_case(case_id)

    begun = core["call"](
        "optagent.review",
        {
            "op": "begin",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "candidate_id": candidate_id,
            "proof_ids": [],
            "run_ids": [],
        },
        role="opt-verifier",
    )
    assert begun["ok"], begun
    review_id = begun["result"]["review"]["id"]
    raw_refs = begun["result"]["raw_source_refs"]
    provenance_ref = next(ref for ref in raw_refs if ref["path"].endswith("/source-provenance.json"))
    provenance = store.read_ref(provenance_ref)
    assert b"OWNER_LOCATION_SENTINEL" not in provenance
    assert b"OWNER_NOTE_SENTINEL" not in provenance
    assert b"CANDIDATE_GOLD_SENTINEL" not in provenance
    assert b"CANDIDATE_GOLD_SENTINEL" not in b"".join(store.read_ref(ref) for ref in raw_refs)
    projected = __import__("json").loads(provenance)
    assert projected["source_binding"] == {
        "source_revision": state["source_revision"],
        "math_source_ids": [source_id],
    }
    assert projected["sources"][0]["source_id"] == source_id
    assert projected["sources"][0]["raw"]["ref"] == source_record["bytes"]
    assert "locations" not in projected["sources"][0]
    review_record, review_ref = store.read_family_record(case_id, "reviews", review_id)
    assert review_record["source_provenance_ref"] == provenance_ref
    assert store.read_case(case_id)["current_records"]["reviews"][review_id] == review_ref
    issue_args = {
        "op": "submit",
        "case_id": case_id,
        "review_id": review_id,
        "expected_review_revision": 1,
        "record_kind": "issue",
        "claim": "The candidate omits a source requirement.",
        "locations": [{"source_id": source_id, "locator": "source-level"}],
        "candidate_locations": ["model.py"],
        "evidence_paths": [],
    }
    premature = core["call"]("optagent.review", issue_args, role="opt-verifier")
    assert not premature["ok"]
    assert premature["error"]["code"] == "review_order"

    note = core["call"](
        "optagent.review",
        {
            "op": "note",
            "case_id": case_id,
            "review_id": review_id,
            "expected_review_revision": 1,
            "body": "The captured source states the requirement that the candidate omits.",
            "source_locations": [{"source_id": source_id, "locator": "source-level"}],
        },
        role="opt-verifier",
    )
    assert note["ok"], note
    issue_args["expected_review_revision"] = 2
    issue_reply = core["call"]("optagent.review", issue_args, role="opt-verifier")
    assert issue_reply["ok"], issue_reply
    issue_id = issue_reply["result"]["issue_id"]
    store = core["store"]()
    issue_snapshot = strict_json_loads(store.read_ref(issue_reply["result"]["issue_ref"]))
    assert set(issue_snapshot) == {
        "issue_id", "claim", "locations", "candidate_locations", "evidence_refs",
        "proposal_stamp", "owner_dispositions",
    }
    review_before_disposition, review_ref_before_disposition = store.read_family_record(
        case_id, "reviews", review_id
    )

    # An Owner may record a historical disposition after a new Candidate is
    # registered. This appends an opinion to the exact old Review/Issue without
    # making that Review current again or advancing math/business revisions.
    (core["workspace"] / "model.py").write_text(
        "# CANDIDATE_REVISED_SENTINEL\ndef build_model(data):\n    return None\n",
        encoding="utf-8",
    )
    state = store.read_case(case_id)
    revised_candidate = core["call"](
        "optagent.case",
        {
            "op": "revise",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "kind": "candidate",
            "model_path": "model.py",
            "local_module_paths": [],
            "data_id": None,
            "backend_id": "highs",
            "objective_plan": {"mode": "feasibility", "preference_note": "test only"},
            "business_checker_path": None,
            "input_closure": "declared",
            "closure_note": "All inputs are declared.",
        },
    )
    assert revised_candidate["ok"], revised_candidate
    before_disposition_state = store.read_case(case_id)
    assert before_disposition_state["candidate_revision"] > review_before_disposition["binding"]["candidate_revision"]

    disposition_args = {
        "op": "submit",
        "case_id": case_id,
        "review_id": review_id,
        "expected_review_revision": 3,
        "record_kind": "disposition",
        "issue_id": issue_id,
        "diagnosis": "confirmed",
        "reason": "The owner confirms the cited source omission.",
        "patch_status": "unchecked",
    }
    cancelled = core["call"](
        "optagent.review", disposition_args, cancellation_observed=True
    )
    assert not cancelled["ok"]
    assert cancelled["error"]["code"] == "execution_unknown"
    assert store.read_case(case_id)["current_records"]["reviews"][review_id] == review_ref_before_disposition

    import optagent_core.api as api_module

    from test_session_routing import _install_route_members, _queue_capture_arrival

    _install_route_members(core, {case_id: 1, other_case: 2}, latest_seq=2)
    original_prepare = api_module._prepare_session_route_fence
    prepare_calls = 0
    queued_event = None

    def queue_unrelated_case_after_snapshot(current_store, current_state, **kwargs):
        nonlocal prepare_calls, queued_event
        fence = original_prepare(current_store, current_state, **kwargs)
        if current_state["case_id"] == case_id:
            prepare_calls += 1
            if prepare_calls == 1:
                queued_event = _queue_capture_arrival(core, other_case, seq=3)
        return fence

    monkeypatch.setattr(api_module, "_prepare_session_route_fence", queue_unrelated_case_after_snapshot)
    disposition = core["call"]("optagent.review", disposition_args)
    assert disposition["ok"], disposition
    assert queued_event is not None and prepare_calls == 2
    review_after_disposition, review_ref_after_disposition = store.read_family_record(
        case_id, "reviews", review_id
    )
    assert review_ref_after_disposition != review_ref_before_disposition
    assert review_after_disposition["binding"] == review_before_disposition["binding"]
    assert store.read_case(case_id)["candidate_revision"] == before_disposition_state["candidate_revision"]
    assert store.read_case(case_id)["state_revision"] == before_disposition_state["state_revision"]
    final_issue = strict_json_loads(core["store"]().read_ref(review_after_disposition["issues"][0]))
    prior_issue = strict_json_loads(store.read_ref(review_before_disposition["issues"][0]))
    assert "patch_ref" not in final_issue
    assert set(final_issue["owner_dispositions"][0]) == {
        "diagnosis", "reason", "patch_status", "stamp", "created_at",
    }
    assert final_issue["proposal_stamp"]["role"] == "opt-verifier"
    assert final_issue["owner_dispositions"][0]["stamp"]["role"] == "root"
    assert final_issue["claim"] == prior_issue["claim"]
