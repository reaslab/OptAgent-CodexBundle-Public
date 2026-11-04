from __future__ import annotations

import zipfile
from io import BytesIO


def _public_close_claim(claim: dict) -> dict:
    public = {key: value for key, value in claim.items() if key != "evidence_refs"}
    public["evidence_paths"] = [ref["path"] for ref in claim.get("evidence_refs", [])]
    return public


def _proof_review_to_delivery(
    core,
    *,
    case_id: str | None = None,
    evidence_claim: str = "A source-scoped analytic claim",
    task_kind: str = "proof",
    omit_proof_anchor: bool = False,
    same_scope_distinct_proofs: bool = False,
    select: bool = True,
):
    if case_id is None:
        case_id, _, _ = core["create_case"](raw=b"For every integer n, n(n+1) is even.", task_kind=task_kind)
    state = core["store"]().read_case(case_id)
    assert state["task_kind"] == task_kind
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
    review_id = begun["result"]["review"]["id"]
    assert begun["result"]["raw_source_refs"]
    assert begun["result"]["candidate_refs"] == []
    state = core["store"]().read_case(case_id)

    early_proof = core["call"](
        "optagent.review",
        {
            "op": "submit",
            "case_id": case_id,
            "review_id": review_id,
            "expected_state_revision": state["state_revision"],
            "expected_review_revision": 1,
            "record_kind": "proof",
            "kind": "analytic",
            "body_path": "proof.md",
            "premises": ["n is an integer"],
            "evidence_refs": [],
            "claim_kinds": ["formulation_fidelity"],
            "candidate_id": None,
        },
    )
    assert not early_proof["ok"]
    assert early_proof["error"]["code"] == "review_order"
    assert core["store"]().read_case(case_id)["proof_ids"] == []

    source_id = state["source_ids"][0]
    note = core["call"](
        "optagent.review",
        {
            "op": "note",
            "case_id": case_id,
            "review_id": review_id,
            "expected_review_revision": 1,
            "body": "The captured statement is the complete scope of the proposed parity proof.",
            "source_locations": [{"source_id": source_id, "locator": "source-level"}],
        },
        role="opt-verifier",
    )
    assert note["ok"], note

    (core["workspace"] / "proof.md").write_text(
        "Let n be even, so n(n+1) is even; if n is odd then n+1 is even, so the product is even.",
        encoding="utf-8",
    )
    state = core["store"]().read_case(case_id)
    proof_reply = core["call"](
        "optagent.review",
        {
            "op": "submit",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "review_id": review_id,
            "expected_review_revision": 2,
            "record_kind": "proof",
            "kind": "analytic",
            "body_path": "proof.md",
            "premises": ["n is an integer"],
            "evidence_refs": [],
            "claim_kinds": ["formulation_fidelity"],
            "candidate_id": None,
        },
    )
    assert proof_reply["ok"], proof_reply
    proof_id = proof_reply["result"]["proof"]["id"]
    proof_record_ref = proof_reply["result"]["proof"]["record_ref"]
    proof_ids = [proof_id]
    proof_record_refs = [proof_record_ref]
    if same_scope_distinct_proofs:
        (core["workspace"] / "proof-second.md").write_text(
            "If n is even, n(n+1) is even; if n is odd, n+1 is even.", encoding="utf-8"
        )
        state = core["store"]().read_case(case_id)
        second_proof = core["call"](
            "optagent.review",
            {
                "op": "submit",
                "case_id": case_id,
                "expected_state_revision": state["state_revision"],
                "review_id": review_id,
                "expected_review_revision": 2,
                "record_kind": "proof",
                "kind": "analytic",
                "body_path": "proof-second.md",
                "premises": ["n is an integer"],
                "evidence_refs": [],
                "claim_kinds": ["formulation_fidelity"],
                "candidate_id": None,
            },
        )
        assert second_proof["ok"], second_proof
        proof_ids.append(second_proof["result"]["proof"]["id"])
        proof_record_refs.append(second_proof["result"]["proof"]["record_ref"])

    claim = {
        "kind": "formulation_fidelity",
        "scope": evidence_claim,
        "conditions": [],
        "evidence_refs": [] if omit_proof_anchor else [proof_record_ref],
        "support": "supported",
    }
    claims = [claim]
    if same_scope_distinct_proofs:
        claims.append({**claim, "evidence_refs": [proof_record_refs[1]]})
    closed = core["call"](
        "optagent.review",
        {
            "op": "submit",
            "case_id": case_id,
            "review_id": review_id,
            "expected_review_revision": 2,
            "record_kind": "outcome",
            "decision": "ready",
            "claims": [_public_close_claim(value) for value in claims],
            "feedback": [],
            "remaining": [],
            "report_path": "proof.md",
        },
        role="opt-verifier",
    )
    if omit_proof_anchor:
        assert not closed["ok"]
        assert closed["error"]["code"] == "evidence_insufficient"
        assert core["store"]().read_family_record(case_id, "reviews", review_id)[0]["status"] == "open"
        return case_id, review_id, proof_id, claim, None
    assert closed["ok"], closed

    if not select:
        return case_id, review_id, proof_id, claim, None

    state = core["store"]().read_case(case_id)
    selected = core["call"](
        "optagent.deliver",
        {
            "op": "select",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "kind": "proof",
            "candidate_id": None,
            "review_id": review_id,
            "proof_ids": proof_ids,
            "run_ids": [],
        },
    )
    assert selected["ok"], selected
    return case_id, review_id, proof_id, claim, selected["result"]["delivery"]["id"]


def test_delivery_selection_keeps_same_scope_claims_with_distinct_proof_anchors(core):
    case_id, review_id, _proof_id, _claim, delivery_id = _proof_review_to_delivery(
        core, same_scope_distinct_proofs=True
    )
    store = core["store"]()
    review, _ = store.read_family_record(case_id, "reviews", review_id)
    claims = review["conclusion"]["claims"]
    assert len(claims) == 2
    assert {key: claims[0][key] for key in ("kind", "scope", "support", "conditions")} == {
        key: claims[1][key] for key in ("kind", "scope", "support", "conditions")
    }
    assert claims[0]["evidence_refs"] != claims[1]["evidence_refs"]
    delivery = store.read_record(
        f".reaslab/optagent/cases/{case_id}/deliveries/{delivery_id}/delivery.json"
    )
    assert delivery["claims"] == claims


def test_review_begin_rejects_unsupported_prior_schema_before_reading_its_note(core):
    case_id, prior_review_id, _proof_id, _claim, _ = _proof_review_to_delivery(core, select=False)
    store = core["store"]()
    prior, prior_ref = store.read_family_record(case_id, "reviews", prior_review_id)
    unsupported = {**prior, "schema_version": 3}
    unsupported_ref = store.write_family_version(case_id, "reviews", prior_review_id, unsupported)
    store.cas_case_family(
        case_id,
        "reviews",
        prior_review_id,
        prior_ref,
        lambda state: state["current_records"]["reviews"].__setitem__(prior_review_id, unsupported_ref),
    )

    state = store.read_case(case_id)
    begun = core["call"](
        "optagent.review",
        {
            "op": "begin",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "candidate_id": None,
            "proof_ids": [],
            "run_ids": [],
            "prior_review_id": prior_review_id,
        },
        role="opt-verifier",
    )
    assert not begun["ok"]
    assert begun["error"]["code"] == "review_binding_mismatch"
    assert begun["error"]["message"] == "prior review schema is unsupported"


def test_candidate_free_proof_review_delivery_and_package(core):
    case_id, review_id, proof_id, claim, delivery_id = _proof_review_to_delivery(core)
    state = core["store"]().read_case(case_id)
    assert state["candidate_id"] is None
    assert state["run_ids"] == []
    assert state["prepared_ids"] == []

    built = core["call"](
        "optagent.deliver",
        {
            "op": "build",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "delivery_id": delivery_id,
            "target_path": "reports/proof.md",
        },
    )
    assert built["ok"], built
    state = core["store"]().read_case(case_id)
    packed = core["call"](
        "optagent.deliver",
        {
            "op": "package",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "delivery_id": delivery_id,
            "target_path": "exports/proof.zip",
            "include_raw_sources": True,
        },
    )
    assert packed["ok"], packed
    archive_bytes = core["store"]().read_ref(packed["result"]["package_ref"])
    with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
        names = set(archive.namelist())
        assert "README.md" in names
        assert "manifest.json" in names
        assert f"evidence/proofs/{proof_id}.json" in names
        assert f"evidence/reviews/{review_id}.json" in names
        manifest = __import__("json").loads(archive.read("manifest.json"))
        assert manifest["entry"] is None
        assert manifest["runtime_bundles"] == []
        packaged_claim = dict(manifest["claims"][0])
        original_claim = dict(claim)
        portable_refs = packaged_claim.pop("evidence_refs")
        original_claim.pop("evidence_refs")
        assert packaged_claim == original_claim
        assert len(portable_refs) == 1
        assert portable_refs[0]["path"].startswith("evidence/claim-0/")
        assert portable_refs[0]["sha256"] == claim["evidence_refs"][0]["sha256"]
        assert all(not name.startswith("worker_bundle/") for name in names)


def test_public_prior_review_reuse_pins_and_packages_original_note_closure(core):
    case_id, prior_review_id, proof_id, claim, _ = _proof_review_to_delivery(core)
    store = core["store"]()
    prior, prior_ref = store.read_family_record(case_id, "reviews", prior_review_id)
    prior_note_ref = prior["note"]["body"]

    state = store.read_case(case_id)
    begun = core["call"](
        "optagent.review",
        {
            "op": "begin",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "candidate_id": None,
            "proof_ids": [],
            "run_ids": [],
            "prior_review_id": prior_review_id,
        },
        role="opt-verifier",
    )
    assert begun["ok"], begun
    review_id = begun["result"]["review"]["id"]
    reused, reused_ref = store.read_family_record(case_id, "reviews", review_id)
    assert reused["prior_review_ref"] == prior_ref
    assert reused["note"] == prior["note"]
    assert reused_ref == begun["result"]["review"]["record_ref"]

    reused_claim = _public_close_claim(claim)
    reused_claim["evidence_paths"].append(prior_note_ref["path"])
    closed = core["call"](
        "optagent.review",
        {
            "op": "submit",
            "case_id": case_id,
            "review_id": review_id,
            "expected_review_revision": 1,
            "record_kind": "outcome",
            "decision": "ready",
            "claims": [reused_claim],
            "feedback": [],
            "remaining": [],
            "report_path": "proof.md",
        },
        role="opt-verifier",
    )
    assert closed["ok"], closed

    state = store.read_case(case_id)
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

    state = store.read_case(case_id)
    packaged = core["call"](
        "optagent.deliver",
        {
            "op": "package",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "delivery_id": selected["result"]["delivery"]["id"],
            "target_path": "exports/prior-review-reuse.zip",
            "include_raw_sources": True,
        },
    )
    assert packaged["ok"], packaged
    with zipfile.ZipFile(BytesIO(store.read_ref(packaged["result"]["package_ref"]))) as archive:
        assert f"evidence/reviews/{prior_review_id}/note.txt" in archive.namelist()
        assert f"evidence/reviews/{review_id}/note.txt" not in archive.namelist()
        import json

        manifest = json.loads(archive.read("manifest.json"))
        note_map = [item for item in manifest["ref_map"] if item["ref"] == prior_note_ref]
        assert len(note_map) == 1
        assert note_map[0]["package_path"] == f"evidence/reviews/{prior_review_id}/note.txt"
        assert archive.read(note_map[0]["package_path"]) == store.read_ref(prior_note_ref)


def test_empty_strong_claim_is_rejected_then_source_only_optimize_and_feasibility_close(core):
    case_id, review_id, proof_id, _, delivery_id = _proof_review_to_delivery(core, omit_proof_anchor=True)
    assert delivery_id is None
    store = core["store"]()
    state = store.read_case(case_id)
    proof_ref = store.ref_for_path(f".reaslab/optagent/cases/{case_id}/proofs/{proof_id}.json")
    claim = {
        "kind": "formulation_fidelity",
        "scope": "A source-scoped analytic claim",
        "conditions": [],
        "evidence_refs": [proof_ref],
        "support": "supported",
    }
    proof = store.read_record(f".reaslab/optagent/cases/{case_id}/proofs/{proof_id}.json")
    body_only_claim = {**claim, "evidence_refs": [proof["body"]]}
    body_only = core["call"](
        "optagent.review",
        {
            "op": "submit", "case_id": case_id, "review_id": review_id,
            "expected_review_revision": 2, "record_kind": "outcome", "decision": "ready",
            "claims": [_public_close_claim(body_only_claim)], "feedback": [], "remaining": [], "report_path": "proof.md",
        },
        role="opt-verifier",
    )
    assert not body_only["ok"] and body_only["error"]["code"] == "evidence_insufficient"
    closed = core["call"](
        "optagent.review",
        {
            "op": "submit", "case_id": case_id, "review_id": review_id,
            "expected_review_revision": 2, "record_kind": "outcome", "decision": "ready",
            "claims": [_public_close_claim(claim)], "feedback": [], "remaining": [], "report_path": "proof.md",
        },
        role="opt-verifier",
    )
    assert closed["ok"], closed
    state = store.read_case(case_id)
    selected = core["call"](
        "optagent.deliver",
        {
            "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
            "kind": "proof", "candidate_id": None, "review_id": review_id,
            "proof_ids": [proof_id], "run_ids": [],
        },
    )
    assert selected["ok"], selected

    for task_kind in ("optimize", "feasibility"):
        case_id, review_id, proof_id, claim, delivery_id = _proof_review_to_delivery(core, task_kind=task_kind)
        assert delivery_id is not None
        state = core["store"]().read_case(case_id)
        assert state["task_kind"] == task_kind
        assert state["candidate_id"] is None and state["prepared_ids"] == [] and state["run_ids"] == []
        built = core["call"](
            "optagent.deliver",
            {
                "op": "build", "case_id": case_id, "expected_state_revision": state["state_revision"],
                "delivery_id": delivery_id, "target_path": f"reports/{task_kind}-source-only.md",
            },
        )
        assert built["ok"], built
        state = core["store"]().read_case(case_id)
        packaged = core["call"](
            "optagent.deliver",
            {
                "op": "package", "case_id": case_id, "expected_state_revision": state["state_revision"],
                "delivery_id": delivery_id, "target_path": f"exports/{task_kind}-source-only.zip",
                "include_raw_sources": True,
            },
        )
        assert packaged["ok"], packaged


def test_selection_cannot_swap_a_reviewed_primary_proof_for_another_proof(core):
    case_id, first_review_id, first_proof_id, claim, _ = _proof_review_to_delivery(core)
    store = core["store"]()
    first_proof_ref = store.ref_for_path(f".reaslab/optagent/cases/{case_id}/proofs/{first_proof_id}.json")

    state = store.read_case(case_id)
    begun = core["call"](
        "optagent.review",
        {
            "op": "begin", "case_id": case_id, "expected_state_revision": state["state_revision"],
            "candidate_id": None, "proof_ids": [first_proof_id], "run_ids": [],
        },
        role="opt-verifier",
    )
    assert begun["ok"], begun
    review_id = begun["result"]["review"]["id"]
    state = store.read_case(case_id)
    note = core["call"](
        "optagent.review",
        {
            "op": "note", "case_id": case_id, "review_id": review_id,
            "expected_review_revision": 1,
            "body": "This separate review retains the first Proof as its primary evidence.",
            "source_locations": [{"source_id": state["source_ids"][0], "locator": "source-level"}],
        },
        role="opt-verifier",
    )
    assert note["ok"], note

    (core["workspace"] / "alternate-proof.md").write_text(
        "A second proof body with the same reviewed conclusion.", encoding="utf-8"
    )
    state = store.read_case(case_id)
    alternate = core["call"](
        "optagent.review",
        {
            "op": "submit", "case_id": case_id, "review_id": review_id,
            "expected_state_revision": state["state_revision"], "expected_review_revision": 2,
            "record_kind": "proof", "kind": "analytic", "body_path": "alternate-proof.md",
            "premises": ["n is an integer"], "evidence_refs": [],
            "claim_kinds": ["formulation_fidelity"], "candidate_id": None,
        },
    )
    assert alternate["ok"], alternate
    alternate_proof_id = alternate["result"]["proof"]["id"]
    alternate_proof_ref = alternate["result"]["proof"]["record_ref"]

    first_claim = {**claim, "evidence_refs": [first_proof_ref]}
    closed = core["call"](
        "optagent.review",
        {
            "op": "submit", "case_id": case_id, "review_id": review_id,
            "expected_review_revision": 2, "record_kind": "outcome", "decision": "ready",
            "claims": [_public_close_claim(first_claim)], "feedback": [], "remaining": [], "report_path": "proof.md",
        },
        role="opt-verifier",
    )
    assert closed["ok"], closed

    state = store.read_case(case_id)
    substituted_claim = {**claim, "evidence_refs": [alternate_proof_ref]}
    selected = core["call"](
        "optagent.deliver",
        {
            "op": "select", "case_id": case_id, "expected_state_revision": state["state_revision"],
            "kind": "proof", "candidate_id": None,
            "review_id": review_id, "proof_ids": [alternate_proof_id], "run_ids": [],
        },
    )
    assert not selected["ok"]
    assert selected["error"]["code"] == "evidence_insufficient"
