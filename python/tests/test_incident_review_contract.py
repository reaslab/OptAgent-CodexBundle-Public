from __future__ import annotations

import pytest

from optagent_core.contracts import strict_json_loads


def _register_candidate(core, case_id: str, *, model_path: str = "candidate.py") -> tuple[str, dict]:
    (core["workspace"] / model_path).write_text(
        "def build_model(bundle):\n    return None\n",
        encoding="utf-8",
    )
    state = core["store"]().read_case(case_id)
    registered = core["call"](
        "optagent.case",
        {
            "op": "revise",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "kind": "candidate",
            "model_path": model_path,
            "local_module_paths": [],
            "data_id": None,
            "backend_id": "highs",
            "objective_plan": {"mode": "feasibility", "preference_note": "review contract test"},
            "business_checker_path": None,
            "input_closure": "declared",
            "closure_note": "The test candidate declares all inputs.",
        },
    )
    assert registered["ok"], registered
    candidate_id = registered["result"]["candidate"]["id"]
    candidate_ref = core["store"]().ref_for_path(
        f".reaslab/optagent/cases/{case_id}/candidates/{candidate_id}.json"
    )
    return candidate_id, candidate_ref


def _begin_review(core, case_id: str, candidate_id: str | None) -> tuple[str, dict]:
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
    return begun["result"]["review"]["id"], begun["result"]


def _note(core, case_id: str, review_id: str, *, body: str, source_locations: list[dict]) -> dict:
    noted = core["call"](
        "optagent.review",
        {
            "op": "note",
            "case_id": case_id,
            "review_id": review_id,
            "expected_review_revision": 1,
            "body": body,
            "source_locations": source_locations,
        },
        role="opt-verifier",
    )
    assert noted["ok"], noted
    return noted["result"]


def _formulation_claim(evidence_paths: list[str]) -> dict:
    return {
        "kind": "formulation_fidelity",
        "scope": "The candidate follows the reviewed source requirement.",
        "conditions": [],
        "evidence_paths": evidence_paths,
        "support": "supported",
    }


def test_needs_changes_keeps_review_open_and_ready_binds_the_final_deliverable(core):
    case_id, _, _ = core["create_case"](raw=b"The complete formulation must preserve every stated condition.")
    candidate_id, _ = _register_candidate(core, case_id)
    review_id, _ = _begin_review(core, case_id, candidate_id)
    state = core["store"]().read_case(case_id)
    source_ref = core["store"]().read_record(
        f".reaslab/optagent/cases/{case_id}/sources/{state['source_ids'][0]}.json"
    )["bytes"]
    _note(
        core,
        case_id,
        review_id,
        body="The source defines the full formulation scope.",
        source_locations=[{"source_id": state["source_ids"][0], "locator": "source-level"}],
    )

    answer_path = "reports/reviewed-answer.md"
    answer_file = core["workspace"] / answer_path
    answer_file.parent.mkdir(parents=True, exist_ok=True)
    answer_file.write_text("Draft answer: a condition still needs to be stated.\n", encoding="utf-8")
    needs_changes = core["call"](
        "optagent.review",
        {
            "op": "submit",
            "case_id": case_id,
            "review_id": review_id,
            "expected_review_revision": 2,
            "record_kind": "outcome",
            "decision": "needs_changes",
            "claims": [],
            "feedback": ["State the omitted source condition in the answer."],
            "remaining": [],
            "report_path": None,
        },
        role="opt-verifier",
    )
    assert needs_changes["ok"], needs_changes
    assert needs_changes["result"]["outcome"]["review_id"] == review_id
    assert needs_changes["result"]["outcome"]["review_revision"] == 3
    assert needs_changes["result"]["outcome"]["status"] == "open"
    assert needs_changes["result"]["outcome"]["next_action"] == "revise_answer_and_resubmit_same_review"
    open_review, before_ready_ref = core["store"]().read_family_record(case_id, "reviews", review_id)
    assert open_review["status"] == "open"
    assert open_review["answer_ref"] is None
    assert open_review["outcomes"][-1]["decision"] == "needs_changes"

    final_bytes = b"Final answer: every source condition is represented in the approved scope.\n"
    answer_file.write_bytes(final_bytes)
    ready_with_feedback = core["call"](
        "optagent.review",
        {
            "op": "submit",
            "case_id": case_id,
            "review_id": review_id,
            "expected_review_revision": 3,
            "record_kind": "outcome",
            "decision": "ready",
            "claims": [_formulation_claim([source_ref["path"]])],
            "feedback": ["This is still a change request."],
            "remaining": [],
            "report_path": answer_path,
        },
        role="opt-verifier",
    )
    assert not ready_with_feedback["ok"]
    assert ready_with_feedback["error"]["code"] == "invalid_argument"
    unchanged, unchanged_ref = core["store"]().read_family_record(case_id, "reviews", review_id)
    assert unchanged["status"] == "open"
    assert unchanged["review_revision"] == 3
    assert unchanged_ref == before_ready_ref
    assert unchanged["outcomes"] == open_review["outcomes"]

    ready = core["call"](
        "optagent.review",
        {
            "op": "submit",
            "case_id": case_id,
            "review_id": review_id,
            "expected_review_revision": 3,
            "record_kind": "outcome",
            "decision": "ready",
            "claims": [_formulation_claim([source_ref["path"]])],
            "feedback": [],
            "remaining": [],
            "report_path": answer_path,
        },
        role="opt-verifier",
    )
    assert ready["ok"], ready
    review, _ = core["store"]().read_family_record(case_id, "reviews", review_id)
    assert review["status"] == "closed"
    assert review["answer_ref"]["sha256"] != before_ready_ref["sha256"]
    assert core["store"]().read_ref(review["answer_ref"]) == final_bytes
    assert "answer_ref" not in review["conclusion"]["claims"][0]

    state = core["store"]().read_case(case_id)
    selected = core["call"](
        "optagent.deliver",
        {
            "op": "select",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "kind": "mixed",
            "candidate_id": candidate_id,
            "review_id": review_id,
            "proof_ids": [],
            "run_ids": [],
        },
    )
    assert selected["ok"], selected
    delivery_id = selected["result"]["delivery"]["id"]
    delivery = core["store"]().read_record(
        f".reaslab/optagent/cases/{case_id}/deliveries/{delivery_id}/delivery.json"
    )
    assert delivery["claims"] == review["conclusion"]["claims"]
    assert delivery["claims"][0]["conditions"] == review["conclusion"]["claims"][0]["conditions"]
    assert delivery["reports"] == [review["answer_ref"]]

    state = core["store"]().read_case(case_id)
    built = core["call"](
        "optagent.deliver",
        {
            "op": "build",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "delivery_id": delivery_id,
            "target_path": "reports/evidence-summary.md",
        },
    )
    assert built["ok"], built

    answer_file.write_bytes(b"Changed after approval.\n")
    case = core["call"]("optagent.case", {"op": "get", "case_id": case_id})
    assert case["ok"], case
    assert case["result"]["current_validity"] == "stale"
    audited = core["call"](
        "optagent.deliver",
        {
            "op": "audit",
            "case_id": case_id,
            "delivery_id": delivery_id,
            "report_path": "reports/evidence-summary.md",
        },
        role="opt-verifier",
    )
    assert not audited["ok"] and audited["error"]["code"] == "artifact_changed"


def _close(
    core,
    case_id: str,
    review_id: str,
    claims: list[dict],
    *,
    expected_review_revision: int = 2,
) -> dict:
    report_path = "reports/approved-answer.md"
    report_file = core["workspace"] / "reports" / "approved-answer.md"
    report_file.parent.mkdir(parents=True, exist_ok=True)
    if not report_file.exists():
        report_file.write_text("Final reviewed answer bytes.\n", encoding="utf-8")
    return core["call"](
        "optagent.review",
        {
            "op": "submit",
            "case_id": case_id,
            "review_id": review_id,
            "expected_review_revision": expected_review_revision,
            "record_kind": "outcome",
            "decision": "ready",
            "claims": claims,
            "feedback": [],
            "remaining": [],
            "report_path": report_path,
        },
        role="opt-verifier",
    )


def test_long_note_persists_source_locations_and_opens_candidate_ref_after_raw_note(core):
    case_id, _, _ = core["create_case"](raw=b"The reviewed requirement and its exact location.")
    candidate_id, _ = _register_candidate(core, case_id)
    review_id, begun = _begin_review(core, case_id, candidate_id)
    assert begun["candidate_refs"] == []

    state = core["store"]().read_case(case_id)
    locations = [
        {"source_id": state["source_ids"][0], "locator": "lines 2-4"},
        {"source_id": state["source_ids"][0], "locator": "table row 7"},
    ]
    body = "Independent raw-source observation. " + ("detail " * 220)
    assert len(body) > 1200
    noted = _note(core, case_id, review_id, body=body, source_locations=locations)

    store = core["store"]()
    review, _ = store.read_family_record(case_id, "reviews", review_id)
    assert review["note"]["source_locations"] == locations
    assert store.read_ref(noted["note_ref"]).decode("utf-8") == body
    candidate_refs = noted["candidate_refs"]
    assert candidate_refs[0]["path"] == f".reaslab/optagent/cases/{case_id}/candidates/{candidate_id}.json"
    candidate = strict_json_loads(store.read_ref(candidate_refs[0]))
    assert candidate["candidate_id"] == candidate_id
    assert candidate_refs[1]["path"] == candidate["model"]["path"]


def test_returned_note_ref_is_issue_evidence_but_not_a_numerical_primary_anchor(core):
    case_id, _, _ = core["create_case"](raw=b"A source fact recorded by the verifier.")
    review_id, _ = _begin_review(core, case_id, None)
    state = core["store"]().read_case(case_id)
    noted = _note(
        core,
        case_id,
        review_id,
        body="The exact source bytes support this observation.",
        source_locations=[{"source_id": state["source_ids"][0], "locator": "source-level"}],
    )
    note_path = noted["note_ref"]["path"]

    issue = core["call"](
        "optagent.review",
        {
            "op": "submit",
            "case_id": case_id,
            "review_id": review_id,
            "expected_review_revision": 2,
            "record_kind": "issue",
            "claim": "The source contains a material unresolved condition.",
            "locations": [{"source_id": state["source_ids"][0], "locator": "source-level"}],
            "candidate_locations": [],
            "evidence_paths": [note_path],
        },
        role="opt-verifier",
    )
    assert issue["ok"], issue

    numerical = {
        "kind": "feasibility",
        "scope": "The model has a feasible solution.",
        "conditions": [],
        "evidence_paths": [note_path],
        "support": "supported",
    }
    rejected = _close(
        core,
        case_id,
        review_id,
        [numerical],
        expected_review_revision=issue["result"]["review"]["review_revision"],
    )
    assert not rejected["ok"]
    assert rejected["error"]["code"] == "evidence_insufficient"
    assert rejected["error"]["details"]["expected_anchor_types"] == [
        "checked_result_with_passed_checks",
        "matching_formal_proof",
    ]

    inline_issue = {
        "claim": "This close must not create another issue.",
        "locations": [],
        "candidate_locations": [],
        "evidence_paths": [],
    }
    invalid_close = core["call"](
        "optagent.review",
        {
            "op": "submit",
            "case_id": case_id,
            "review_id": review_id,
            "expected_review_revision": 3,
            "record_kind": "outcome",
            "decision": "ready",
            "claims": [],
            "feedback": [],
            "remaining": [],
            "report_path": "reports/approved-answer.md",
            "issues": [inline_issue],
        },
        role="opt-verifier",
    )
    assert not invalid_close["ok"]
    assert invalid_close["error"]["code"] == "invalid_argument"
    assert invalid_close["error"]["pointer"] == "/issues"

    closed = core["call"](
        "optagent.review",
        {
            "op": "submit",
            "case_id": case_id,
            "review_id": review_id,
            "expected_review_revision": 3,
            "record_kind": "outcome",
            "decision": "ready",
            "claims": [],
            "feedback": [],
            "remaining": [],
            "report_path": "reports/approved-answer.md",
        },
        role="opt-verifier",
    )
    assert closed["ok"], closed
    record, _ = core["store"]().read_family_record(case_id, "reviews", review_id)
    assert len(record["issues"]) == 1
    assert record["issues"][0]["path"] == issue["result"]["issue_ref"]["path"]


def test_close_persists_current_candidate_anchor_but_keeps_source_bytes_required(core):
    case_id, _, _ = core["create_case"](raw=b"A source-scoped formulation requirement.")
    candidate_id, candidate_ref = _register_candidate(core, case_id)
    review_id, _ = _begin_review(core, case_id, candidate_id)
    state = core["store"]().read_case(case_id)
    source_ref = core["store"]().read_record(
        f".reaslab/optagent/cases/{case_id}/sources/{state['source_ids'][0]}.json"
    )["bytes"]
    _note(
        core,
        case_id,
        review_id,
        body="The original source describes the formulation requirement.",
        source_locations=[{"source_id": state["source_ids"][0], "locator": "source-level"}],
    )

    closed = _close(core, case_id, review_id, [_formulation_claim([source_ref["path"]])])
    assert closed["ok"], closed
    store = core["store"]()
    review, _ = store.read_family_record(case_id, "reviews", review_id)
    canonical = review["conclusion"]["claims"][0]
    assert canonical["evidence_refs"] == [candidate_ref, source_ref]
    assert "evidence_paths" not in canonical

    selected = core["call"](
        "optagent.deliver",
        {
            "op": "select",
            "case_id": case_id,
            "expected_state_revision": store.read_case(case_id)["state_revision"],
            "kind": "mixed",
            "candidate_id": review["binding"]["candidate_id"],
            "review_id": review["review_id"],
            "proof_ids": [],
            "run_ids": [],
        },
    )
    assert selected["ok"], selected

    # Candidate identity is supplied by the bound review, while original raw
    # Source bytes remain an independent required anchor.
    case_without_source, _, _ = core["create_case"](seq=2, raw=b"A second formulation requirement.")
    second_candidate_id, second_candidate_ref = _register_candidate(core, case_without_source)
    second_review_id, _ = _begin_review(core, case_without_source, second_candidate_id)
    second_state = core["store"]().read_case(case_without_source)
    _note(
        core,
        case_without_source,
        second_review_id,
        body="The second source is reviewed independently.",
        source_locations=[{"source_id": second_state["source_ids"][0], "locator": "source-level"}],
    )
    missing_raw = _close(
        core,
        case_without_source,
        second_review_id,
        [_formulation_claim([second_candidate_ref["path"]])],
    )
    assert not missing_raw["ok"]
    assert missing_raw["error"]["code"] == "evidence_insufficient"
    still_open, _ = core["store"]().read_family_record(case_without_source, "reviews", second_review_id)
    assert still_open["status"] == "open"


def test_close_rejects_explicit_stale_conflicting_and_cross_case_candidate_refs(core):
    case_id, _, _ = core["create_case"](raw=b"The current candidate must match this case.")
    old_candidate_id, old_candidate_ref = _register_candidate(core, case_id, model_path="old.py")
    current_candidate_id, current_candidate_ref = _register_candidate(core, case_id, model_path="current.py")
    review_id, _ = _begin_review(core, case_id, current_candidate_id)
    state = core["store"]().read_case(case_id)
    source_ref = core["store"]().read_record(
        f".reaslab/optagent/cases/{case_id}/sources/{state['source_ids'][0]}.json"
    )["bytes"]
    _note(
        core,
        case_id,
        review_id,
        body="The source is reviewed against the current candidate.",
        source_locations=[{"source_id": state["source_ids"][0], "locator": "source-level"}],
    )

    other_case, _, _ = core["create_case"](seq=2, raw=b"A different case has a distinct Candidate.")
    other_candidate_id, other_candidate_ref = _register_candidate(core, other_case, model_path="other.py")
    assert old_candidate_id != current_candidate_id
    assert other_candidate_id != current_candidate_id

    claims_with_invalid_candidate = [
        _formulation_claim([old_candidate_ref["path"], source_ref["path"]]),
        _formulation_claim([current_candidate_ref["path"], old_candidate_ref["path"], source_ref["path"]]),
        _formulation_claim([other_candidate_ref["path"], source_ref["path"]]),
    ]
    expected_error_codes = ("invalid_reference", "invalid_reference", "permission_denied")
    for claim, expected_error_code in zip(claims_with_invalid_candidate, expected_error_codes, strict=True):
        rejected = _close(core, case_id, review_id, [claim])
        assert not rejected["ok"]
        assert rejected["error"]["code"] == expected_error_code
        still_open, _ = core["store"]().read_family_record(case_id, "reviews", review_id)
        assert still_open["status"] == "open"


def test_diagnostic_and_formal_proof_claims_do_not_gain_candidate_anchor(core):
    case_id, _, _ = core["create_case"](raw=b"A source-scoped analytic statement.")
    candidate_id, _ = _register_candidate(core, case_id)
    review_id, _ = _begin_review(core, case_id, candidate_id)
    state = core["store"]().read_case(case_id)
    _note(
        core,
        case_id,
        review_id,
        body="The source is noted before candidate review.",
        source_locations=[{"source_id": state["source_ids"][0], "locator": "source-level"}],
    )

    (core["workspace"] / "proof.md").write_text(
        "For each integer n, one of n and n+1 is even, so n(n+1) is even.",
        encoding="utf-8",
    )
    state = core["store"]().read_case(case_id)
    proof_reply = core["call"](
        "optagent.review",
        {
            "op": "submit",
            "case_id": case_id,
            "review_id": review_id,
            "expected_state_revision": state["state_revision"],
            "expected_review_revision": 2,
            "record_kind": "proof",
            "kind": "analytic",
            "body_path": "proof.md",
            "premises": ["n is an integer"],
            "evidence_refs": [],
            "claim_kinds": ["formulation_fidelity"],
            "candidate_id": candidate_id,
        },
    )
    assert proof_reply["ok"], proof_reply
    proof_ref = proof_reply["result"]["proof"]["record_ref"]
    proof_claim = _formulation_claim([proof_ref["path"]])
    closed = _close(core, case_id, review_id, [proof_claim])
    assert closed["ok"], closed
    reviewed, _ = core["store"]().read_family_record(case_id, "reviews", review_id)
    assert reviewed["conclusion"]["claims"][0]["evidence_refs"] == [proof_ref]

    diagnostic_case, _, _ = core["create_case"](seq=2, raw=b"A separate diagnostic scope.")
    diagnostic_candidate, _ = _register_candidate(core, diagnostic_case, model_path="diagnostic.py")
    diagnostic_review_id, _ = _begin_review(core, diagnostic_case, diagnostic_candidate)
    diagnostic_state = core["store"]().read_case(diagnostic_case)
    _note(
        core,
        diagnostic_case,
        diagnostic_review_id,
        body="The diagnostic review remains unresolved.",
        source_locations=[{"source_id": diagnostic_state["source_ids"][0], "locator": "source-level"}],
    )
    diagnostic_claim = {
        "kind": "diagnostic",
        "scope": "No linked execution observation was available.",
        "conditions": [],
        "evidence_paths": [],
        "support": "unresolved",
    }
    diagnostic_closed = _close(core, diagnostic_case, diagnostic_review_id, [diagnostic_claim])
    assert diagnostic_closed["ok"], diagnostic_closed
    diagnostic_record, _ = core["store"]().read_family_record(
        diagnostic_case, "reviews", diagnostic_review_id
    )
    assert diagnostic_record["conclusion"]["claims"][0]["evidence_refs"] == []


@pytest.mark.parametrize(
    "claim_kind",
    [
        "formulation_fidelity",
        "feasibility",
        "optimality",
        "local_optimality",
        "infeasibility",
        "unboundedness",
        "bound",
        "tradeoff",
        "diagnostic",
    ],
)
@pytest.mark.parametrize("support", ["unresolved", "refuted"])
def test_review_close_records_negative_claim_status_for_each_claim_kind(core, claim_kind, support):
    case_id, _, _ = core["create_case"](raw=b"A source with a reviewable mathematical claim.")
    candidate_id, _ = _register_candidate(core, case_id)
    review_id, _ = _begin_review(core, case_id, candidate_id)
    state = core["store"]().read_case(case_id)
    _note(
        core,
        case_id,
        review_id,
        body="This claim remains unresolved or has a counterexample in its stated scope.",
        source_locations=[{"source_id": state["source_ids"][0], "locator": "source-level"}],
    )

    claim = {
        "kind": claim_kind,
        "scope": "The claim's stated scope only.",
        "conditions": [],
        "evidence_paths": [],
        "support": support,
    }
    closed = _close(core, case_id, review_id, [claim])
    assert closed["ok"], closed

    record, _ = core["store"]().read_family_record(case_id, "reviews", review_id)
    assert record["status"] == "closed"
    assert record["conclusion"]["claims"][0]["kind"] == claim_kind
    assert record["conclusion"]["claims"][0]["support"] == support
    assert record["conclusion"]["claims"][0]["evidence_refs"] == []


def test_negative_formulation_review_cannot_be_selected_as_a_delivery_claim(core):
    case_id, _, _ = core["create_case"](raw=b"A source whose formulation interpretation is disputed.")
    candidate_id, _ = _register_candidate(core, case_id)
    review_id, _ = _begin_review(core, case_id, candidate_id)
    state = core["store"]().read_case(case_id)
    _note(
        core,
        case_id,
        review_id,
        body="The source and candidate have a material semantic conflict.",
        source_locations=[{"source_id": state["source_ids"][0], "locator": "source-level"}],
    )

    (core["workspace"] / "proof.md").write_text(
        "For every integer n, n(n+1) is even.",
        encoding="utf-8",
    )
    state = core["store"]().read_case(case_id)
    proof = core["call"](
        "optagent.review",
        {
            "op": "submit",
            "case_id": case_id,
            "review_id": review_id,
            "expected_state_revision": state["state_revision"],
            "expected_review_revision": 2,
            "record_kind": "proof",
            "kind": "analytic",
            "body_path": "proof.md",
            "premises": ["n is an integer"],
            "evidence_refs": [],
            "claim_kinds": ["formulation_fidelity"],
            "candidate_id": candidate_id,
        },
    )
    assert proof["ok"], proof

    negative_claim = {
        "kind": "formulation_fidelity",
        "scope": "The candidate matches the original task semantics.",
        "conditions": [],
        "evidence_paths": [],
        "support": "refuted",
    }
    closed = _close(core, case_id, review_id, [negative_claim])
    assert closed["ok"], closed

    store = core["store"]()
    record, _ = store.read_family_record(case_id, "reviews", review_id)
    selected = core["call"](
        "optagent.deliver",
        {
            "op": "select",
            "case_id": case_id,
            "expected_state_revision": store.read_case(case_id)["state_revision"],
            "kind": "proof",
            "candidate_id": candidate_id,
            "review_id": review_id,
            "proof_ids": [proof["result"]["proof"]["id"]],
            "run_ids": [],
        },
    )
    assert not selected["ok"]
    assert selected["error"]["code"] == "evidence_insufficient"
    assert "cannot be presented as supported" in selected["error"]["message"]
