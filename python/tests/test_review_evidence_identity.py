from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Mapping

import pytest

from optagent_core.contracts import ContractError, canonical_json_bytes
from optagent_core.review import _resolve_evidence_paths, _review_evidence_index


class MemoryStore:
    """Small immutable byte store for exercising the real Review resolver."""

    def __init__(self, blobs: Mapping[str, bytes]) -> None:
        self.blobs = dict(blobs)

    def read_ref(self, ref: Mapping[str, Any]) -> bytes:
        raw = self.blobs.get(ref["path"])
        if raw is None:
            raise ContractError("not_found", "fixture Ref bytes are unavailable")
        if len(raw) != ref["byte_len"] or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            raise ContractError("artifact_changed", "fixture Ref does not match its pinned bytes")
        return raw

    def read_user_file(self, path: str) -> tuple[bytes, dict[str, Any]]:
        raise ContractError("not_found", f"fixture has no user file: {path}")


def _ref(path: str, raw: bytes, media_type: str) -> dict[str, Any]:
    return {
        "path": path,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_len": len(raw),
        "media_type": media_type,
    }


def _review_with_unbound_proof(different_field: str | None = None) -> tuple:
    case_id = str(uuid.uuid4())
    review_id = str(uuid.uuid4())
    proof_id = str(uuid.uuid4())
    candidate_id = str(uuid.uuid4())
    source_bytes = b"Review evidence identity fixture."
    source_digest = hashlib.sha256(source_bytes).hexdigest()
    source_ref = _ref(
        f".reaslab/optagent/source-bytes/{source_digest}",
        source_bytes,
        "application/octet-stream",
    )
    proof_evidence_ref = {**source_ref, "media_type": "text/markdown; charset=utf-8"}
    if different_field == "sha256":
        proof_evidence_ref["sha256"] = "0" * 64 if source_digest != "0" * 64 else "1" * 64
    elif different_field == "byte_len":
        proof_evidence_ref["byte_len"] += 1

    blobs = {source_ref["path"]: source_bytes}
    provenance_path = f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/source-provenance.json"
    provenance_raw = canonical_json_bytes({"sources": [{"raw": {"ref": source_ref}}]})
    provenance_ref = _ref(provenance_path, provenance_raw, "application/json")
    blobs[provenance_path] = provenance_raw

    note_path = f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/note.txt"
    note_raw = b"Raw source reviewed."
    note_ref = _ref(note_path, note_raw, "text/plain; charset=utf-8")
    blobs[note_path] = note_raw
    stamp = {
        "session_id": str(uuid.uuid4()),
        "role": "opt-verifier",
        "turn_id": str(uuid.uuid4()),
        "call_id": str(uuid.uuid4()),
    }

    candidate_path = f".reaslab/optagent/cases/{case_id}/candidates/{candidate_id}.json"
    candidate_raw = canonical_json_bytes({"model": None, "local_modules": [], "business_checker": None})
    candidate_ref = _ref(candidate_path, candidate_raw, "application/json")
    blobs[candidate_path] = candidate_raw

    body_path = f".reaslab/optagent/cases/{case_id}/proofs/{proof_id}/body.md"
    body_raw = b"Proof body for evidence-index regression."
    body_ref = _ref(body_path, body_raw, "text/markdown; charset=utf-8")
    blobs[body_path] = body_raw
    proof = {
        "proof_id": proof_id,
        "source_revision": 4,
        "candidate_id": candidate_id,
        "body": body_ref,
        "evidence_refs": [proof_evidence_ref],
        "created_at": "2026-10-21T00:00:02Z",
        "status": "proposed",
    }
    proof_path = f".reaslab/optagent/cases/{case_id}/proofs/{proof_id}.json"
    proof_raw = canonical_json_bytes(proof)
    proof_ref = _ref(proof_path, proof_raw, "application/json")
    blobs[proof_path] = proof_raw

    review = {
        "case_id": case_id,
        "review_id": review_id,
        "source_provenance_ref": provenance_ref,
        "note": {"body": note_ref, "stamp": stamp},
        "binding": {
            "source_revision": 4,
            "candidate_id": candidate_id,
            "proof_ids": [],
            "run_ids": [],
        },
        "binding_evidence_refs": {"candidate": candidate_ref, "proofs": {}, "runs": {}},
    }
    state = {
        "case_id": case_id,
        "source_revision": 4,
        "proof_ids": [proof_id],
        "decisions": [
            {"kind": "proof_proposal", "refs": [proof_ref], "created_at": "2026-10-21T00:00:02Z"}
        ],
    }
    return MemoryStore(blobs), state, review, source_ref, proof_ref, body_ref


def test_review_evidence_index_deduplicates_same_pinned_bytes_with_different_mime():
    # The Review's current source root says octet-stream; an eligible
    # post-note Proof carries the same path/hash/length with only MIME changed.
    store, state, review, source_ref, proof_ref, body_ref = _review_with_unbound_proof()
    index = _review_evidence_index(store, state, review)

    assert len(index[source_ref["path"]]) == 1
    assert next(iter(index[source_ref["path"]].values())) == source_ref
    assert _resolve_evidence_paths(
        store, state, review, [source_ref["path"]], pointer="/claims/0/evidence_paths"
    ) == [source_ref]
    # Preserve the existing dynamic Proof path for a Review with no begin-time
    # Proof IDs: callers can explicitly cite the proposal or its body.
    assert _resolve_evidence_paths(
        store, state, review, [proof_ref["path"]], pointer="/claims/0/evidence_paths"
    ) == [proof_ref]
    assert _resolve_evidence_paths(
        store, state, review, [body_ref["path"]], pointer="/claims/0/evidence_paths"
    ) == [body_ref]


@pytest.mark.parametrize("different_field", ["sha256", "byte_len"])
def test_review_evidence_index_still_rejects_distinct_content_at_same_path(different_field):
    store, state, review, source_ref, _, _ = _review_with_unbound_proof(different_field)

    with pytest.raises(ContractError) as exc_info:
        _resolve_evidence_paths(
            store,
            state,
            review,
            [source_ref["path"]],
            pointer="/claims/0/evidence_paths",
        )
    assert exc_info.value.code == "invalid_reference"
    assert exc_info.value.details == {"path": source_ref["path"], "identities": 2}
