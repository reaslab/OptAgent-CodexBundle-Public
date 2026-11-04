from __future__ import annotations

import copy
from typing import Any, Callable

from optagent_core.contracts import ContractError, canonical_json_bytes, sha256_bytes
from optagent_core.store import CaseStore


def current_portable_review_fixture(
    writer: Any,
    *,
    case_id: str,
    review_id: str,
    delivery_id: str,
    kind: str,
    claim: dict[str, Any],
    evidence_refs: tuple[dict[str, Any], ...] = (),
    run_authorities: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build synthetic consumer metadata, not a mathematical approval proof."""
    owner_stamp = {"session_id": "synthetic-owner", "role": "root"}
    verifier_stamp = {"session_id": "synthetic-verifier", "role": "opt-verifier"}
    run_authorities = dict(run_authorities or {})
    provenance = {
        "schema_version": 1,
        "case_id": case_id,
        "source_binding": {"source_revision": 0, "math_source_ids": []},
        "provenance_source_ids": [],
        "sources": [],
        "route": {
            "session_id": owner_stamp["session_id"],
            "unknown": False,
            "reason": None,
            "member": None,
            "guards": [],
            "handoffs": [],
        },
    }
    provenance_raw = canonical_json_bytes(provenance)
    provenance_ref = writer.write_immutable(
        f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/source-provenance.json",
        provenance_raw,
        "application/json",
    )
    answer_raw = b"Synthetic approved answer for portable closure validation.\n"
    answer_ref = writer.write_immutable(
        f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/approved-answer.md",
        answer_raw,
        "text/markdown",
    )
    note_raw = b"Synthetic source note for portable closure validation.\n"
    note_ref = writer.write_immutable(
        f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/note.txt",
        note_raw,
        "text/plain; charset=utf-8",
    )
    note = {
        "body": note_ref,
        "stamp": verifier_stamp,
        "source_locations": [],
    }
    outcome = {
        "decision": "ready",
        "feedback": [],
        "remaining": [],
        "stamp": verifier_stamp,
        "created_at": "2026-10-13T00:00:00Z",
    }
    selected_claim = {
        "kind": claim["kind"],
        "scope": claim["scope"],
        "support": claim["support"],
        "conditions": list(claim["conditions"]),
        "evidence_refs": [dict(ref) for ref in claim["evidence_refs"]],
    }
    review = {
        "schema_version": 4,
        "created_at": "2026-10-13T00:00:00Z",
        "created_by": owner_stamp,
        "review_id": review_id,
        "review_revision": 3,
        "case_id": case_id,
        "status": "closed",
        "binding": {
            "source_revision": 0,
            "candidate_revision": 0,
            "math_source_ids": [],
            "candidate_id": None,
            "proof_ids": [],
            "run_ids": list(run_authorities),
        },
        "raw_source_ids": [],
        "source_provenance_ref": provenance_ref,
        "source_provenance_digest": sha256_bytes(provenance_raw),
        "binding_evidence_refs": {
            "candidate": None,
            "proofs": {},
            "runs": run_authorities,
        },
        "prior_review_ref": None,
        "note": note,
        "candidate_opened": True,
        "answer_ref": answer_ref,
        "outcomes": [outcome],
        "issues": [],
        "conclusion": {"claims": [selected_claim], "remaining": [], "stamp": verifier_stamp},
    }
    review_raw = canonical_json_bytes(review)
    review_ref = writer.write_immutable(
        f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/versions/{sha256_bytes(review_raw)}.json",
        review_raw,
        "application/json",
    )

    selected_evidence = [dict(ref) for ref in evidence_refs]
    selected_evidence.append(answer_ref)
    deduped_evidence: list[dict[str, Any]] = []
    seen_evidence: set[tuple[str, str, int, str]] = set()
    for ref in selected_evidence:
        key = (ref["path"], ref["sha256"], ref["byte_len"], ref["media_type"])
        if key not in seen_evidence:
            seen_evidence.add(key)
            deduped_evidence.append(ref)
    authorities = {"reviews": {review_id: review_ref}, "runs": run_authorities}
    selection = {
        "schema_version": 2,
        "created_at": "2026-10-13T00:00:00Z",
        "created_by": owner_stamp,
        "delivery_id": delivery_id,
        "kind": kind,
        "source_revision": 0,
        "math_source_ids": [],
        "candidate_id": None,
        "promotion_generation": 0,
        "claims": [selected_claim],
        "evidence_refs": deduped_evidence,
        "review_ids": [review_id],
        "proof_ids": [],
        "run_ids": list(run_authorities),
        "authority_records": authorities,
        "summary": None,
        "reports": [answer_ref],
        "package": None,
    }
    selection_raw = canonical_json_bytes(selection)
    selection_ref = writer.write_immutable(
        f".reaslab/optagent/cases/{case_id}/deliveries/{delivery_id}/delivery.json",
        selection_raw,
        "application/json",
    )
    projection = {
        "review_id": review_id,
        "review_ref": review_ref,
        "answer_ref": answer_ref,
        "outcome": outcome,
        "prior_review_ref": None,
        "status": "closed",
        "conclusion_claims": [selected_claim],
        "remaining": [],
        "issues": [],
    }
    closure_refs = [review_ref, provenance_ref, note_ref, answer_ref]
    consumed: list[dict[str, Any]] = []
    seen_consumed: set[tuple[str, str, int, str]] = set()
    for ref in [selection_ref, *deduped_evidence, *authorities["reviews"].values(), *run_authorities.values(), *closure_refs]:
        key = (ref["path"], ref["sha256"], ref["byte_len"], ref["media_type"])
        if key not in seen_consumed:
            seen_consumed.add(key)
            consumed.append(ref)
    portable_files = [
        (review_ref, f"evidence/reviews/{review_id}.json", review_raw),
        (provenance_ref, f"evidence/reviews/{review_id}/source-provenance.json", provenance_raw),
        (note_ref, f"evidence/reviews/{review_id}/note.txt", note_raw),
        (answer_ref, f"evidence/approved-answers/{answer_ref['sha256']}.md", answer_raw),
        (selection_ref, "evidence/selection.json", selection_raw),
    ]
    return {
        "selection": selection,
        "selection_ref": selection_ref,
        "projection": projection,
        "consumed_refs": consumed,
        "portable_files": portable_files,
    }


def read_run(store: CaseStore, case_id: str, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    state = store.read_case(case_id)
    run, ref = store.read_family_record(
        case_id,
        "runs",
        run_id,
        state=state,
    )
    return run, ref


def publish_run_update(
    store: CaseStore,
    case_id: str,
    run_id: str,
    update: Callable[[dict[str, Any]], None],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Update a fixture Run through the public family Ref publisher, never a mutable mirror."""
    run, prior_ref = read_run(store, case_id, run_id)
    changed = copy.deepcopy(run)
    update(changed)
    next_ref = store.write_family_version(case_id, "runs", run_id, changed)
    with store.run_lock(run_id):
        def publish(current: dict[str, Any]) -> None:
            records = current.get("current_records") or {}
            if (records.get("runs") or {}).get(run_id) != prior_ref:
                raise ContractError("stale_revision", "fixture Run Ref changed before publication")
            records = current.get("current_records")
            if records is None:
                records = {"questions": {}, "reviews": {}, "runs": {}, "presentation": None}
                current["current_records"] = records
            records["runs"][run_id] = dict(next_ref)

        store.cas_case_family(
            case_id,
            "runs",
            run_id,
            prior_ref,
            publish,
        )
    return changed, next_ref
