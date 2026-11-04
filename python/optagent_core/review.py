"""Raw-first review records with separate reviewer proposals and owner decisions."""

from __future__ import annotations

import copy
from typing import Any, Callable, Mapping, Sequence

from .contracts import ContractError, canonical_json_bytes, require_id, require_stamp, sha256_bytes, strict_json_loads, validate_ref_shape
from .delivery import (
    _ref_identity,
    _result_path_parts,
    _validate_candidate_record,
    _validate_case_evidence_ref,
    _verified_run,
    pending_question_count,
    validate_review_claims,
)
from .sources import new_id, source_projection, utc_now
from .store import CaseStore


def begin_review(
    store: CaseStore,
    *,
    case_id: str,
    expected_state_revision: int,
    candidate_id: str | None,
    proof_ids: Sequence[str],
    run_ids: Sequence[str],
    prior_review_id: str | None,
    stamp: Mapping[str, str],
    source_provenance_builder: Callable[
        [CaseStore, Mapping[str, Any], Mapping[str, Any]],
        tuple[Mapping[str, Any], Sequence[Mapping[str, Any]]],
    ],
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = store.read_case(case_id)
    _require_current_owner(state, stamp, "opt-verifier")
    if state["state_revision"] != expected_state_revision:
        raise ContractError("stale_revision", "case revision changed", details={"actual": state["state_revision"]})
    if candidate_id != state["candidate_id"]:
        raise ContractError("review_binding_mismatch", "candidate is not the current case binding")
    for proof_id in proof_ids:
        if proof_id not in state["proof_ids"]:
            raise ContractError("not_found", "proof does not belong to this case")
    for run_id in run_ids:
        if run_id not in state["run_ids"]:
            raise ContractError("not_found", "run does not belong to this case")
    prior: dict[str, Any] | None = None
    prior_review_ref: dict[str, Any] | None = None
    if prior_review_id is not None:
        prior, prior_review_ref = _load_review(store, case_id, prior_review_id)
        if not _is_published_review_ref(case_id, prior_review_id, prior_review_ref):
            raise ContractError("review_binding_mismatch", "prior review is not a pinned immutable snapshot")
        if prior.get("schema_version") != 4:
            raise ContractError("review_binding_mismatch", "prior review schema is unsupported")
    source_view = source_projection(store, state)
    binding = {
        "source_revision": state["source_revision"],
        "math_source_ids": source_view["math_source_ids"],
        "candidate_revision": state["candidate_revision"],
        "candidate_id": candidate_id,
        "proof_ids": list(proof_ids),
        "run_ids": list(run_ids),
    }
    provenance_value, provenance_raw_refs = source_provenance_builder(store, state, source_view)
    if not isinstance(provenance_value, Mapping):
        raise ContractError("environment_unavailable", "source provenance builder returned an invalid projection")
    provenance_raw = canonical_json_bytes(dict(provenance_value))
    if len(provenance_raw) > 256 * 1024:
        raise ContractError("environment_unavailable", "source provenance projection exceeds its fixed size limit")
    provenance_digest = sha256_bytes(provenance_raw)
    note = None
    candidate_opened = False
    if prior_review_id is not None:
        prior_binding = prior.get("binding")
        prior_math_source_ids = prior_binding.get("math_source_ids") if isinstance(prior_binding, Mapping) else None
        note_value = prior.get("note")
        note_ref = validate_ref_shape(note_value.get("body"), pointer="/prior_review/note/body") if isinstance(note_value, Mapping) else None
        if note_ref is not None:
            store.read_ref(note_ref)
            require_stamp(note_value.get("stamp"), pointer="/prior_review/note/stamp")
        if (
            prior.get("case_id") != case_id
            or not isinstance(prior_binding, Mapping)
            or any(
                prior_binding.get(key) != value
                for key, value in binding.items()
                if key not in {"math_source_ids", "source_revision"}
            )
            or prior_math_source_ids != binding["math_source_ids"]
            or note_ref is None
            or not note_ref["path"].startswith(f".reaslab/optagent/cases/{case_id}/reviews/")
            or not note_ref["path"].endswith("/note.txt")
            or not _prior_provenance_scope_matches(store, case_id, prior, provenance_value)
        ):
            raise ContractError("review_binding_mismatch", "prior review does not match this evidence binding")
        note = copy.deepcopy(prior["note"])
        candidate_opened = True
    review_id = new_id()
    provenance_ref = store._write_immutable_known_sha(
        f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/source-provenance.json",
        provenance_raw,
        "application/json",
        provenance_digest,
    )
    review = {
        "schema_version": 4,
        "created_at": utc_now(),
        "created_by": dict(stamp),
        "review_id": review_id,
        "review_revision": 1,
        "case_id": case_id,
        "status": "open",
        "binding": binding,
        "binding_evidence_refs": None,
        "raw_source_ids": list(state["source_ids"]),
        "source_provenance_ref": provenance_ref,
        "source_provenance_digest": provenance_digest,
        "prior_review_ref": prior_review_ref,
        "note": note,
        "candidate_opened": candidate_opened,
        "answer_ref": None,
        "outcomes": [],
        "issues": [],
        "conclusion": None,
    }
    if candidate_opened:
        binding_evidence_refs = _pin_binding_evidence_refs(store, state, binding)
        review["binding_evidence_refs"] = binding_evidence_refs
    review_ref = store.write_family_version(case_id, "reviews", review_id, review)
    raw_refs = _unique_refs(
        [*[validate_ref_shape(ref, pointer="/source_provenance/raw_refs") for ref in provenance_raw_refs], provenance_ref]
    )
    candidate_refs = (
        _candidate_refs(
            store,
            case_id,
            binding,
            (review.get("binding_evidence_refs") or {}).get("candidate"),
        )
        if candidate_opened and candidate_id
        else []
    )
    pinned = review.get("binding_evidence_refs") or {}

    def add_review(current: dict[str, Any]) -> None:
        if current["state_revision"] != expected_state_revision:
            raise ContractError("stale_revision", "case revision changed", details={"actual": current["state_revision"]})
        current["review_ids"].append(review_id)
        _publish_review_pointer(current, review_id, review_ref)

    next_state = store.cas_case(case_id, expected_state_revision, add_review)
    summary = _review_summary(review, review_ref)
    return {
        "review": summary,
        "raw_source_refs": raw_refs,
        "candidate_refs": candidate_refs,
        "binding_evidence_refs": copy.deepcopy(pinned) if pinned else None,
        "state": _state_summary(store, next_state),
    }, review


def _add_note_locked(
    store: CaseStore,
    *,
    case_id: str,
    review_id: str,
    expected_review_revision: int,
    body: str,
    source_locations: Sequence[Mapping[str, str]],
    stamp: Mapping[str, str],
) -> dict[str, Any]:
    review, review_ref = _load_review(store, case_id, review_id)
    _require_review_actor(review, stamp)
    _check_review_revision(review, expected_review_revision)
    if review["status"] != "open" or review["note"] is not None:
        raise ContractError("review_order", "review note can be recorded once while open")
    allowed = set(review["raw_source_ids"])
    for loc in source_locations:
        if loc["source_id"] not in allowed:
            raise ContractError("review_binding_mismatch", "note location points outside reviewed source")
    note_bytes = body.encode("utf-8")
    note_ref = store.write_immutable(
        f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/note.txt",
        note_bytes,
        "text/plain; charset=utf-8",
    )
    next_review = copy.deepcopy(review)
    next_review["review_revision"] += 1
    next_review["note"] = {
        "body": note_ref,
        "stamp": dict(stamp),
        "source_locations": copy.deepcopy(list(source_locations)),
    }
    next_review["candidate_opened"] = True
    state = store.read_case(case_id)
    next_review["binding_evidence_refs"] = _pin_binding_evidence_refs(
        store, state, next_review["binding"]
    )
    next_ref = _publish_review(
        store, case_id, review_id, review, review_ref, next_review, stamp, record_kind="note"
    )
    pinned = next_review["binding_evidence_refs"]
    candidate_refs = _candidate_refs(
        store, case_id, next_review["binding"], pinned.get("candidate")
    )
    return {
        "review": _review_summary(next_review, next_ref),
        "note_ref": note_ref,
        "candidate_refs": candidate_refs,
        "proof_refs": [
            _proof_ref(store, case_id, proof_id, pinned["proofs"][proof_id])
            for proof_id in next_review["binding"]["proof_ids"]
        ],
        "run_refs": [
            _run_ref(store, case_id, run_id, pinned["runs"][run_id])
            for run_id in next_review["binding"]["run_ids"]
        ],
        "binding_evidence_refs": copy.deepcopy(pinned),
    }


def _submit_review_locked(
    store: CaseStore,
    *,
    case_id: str,
    review_id: str,
    expected_review_revision: int,
    record_kind: str,
    stamp: Mapping[str, str],
    args: Mapping[str, Any],
    expected_state_revision: int | None = None,
    validate_current: Callable[[Mapping[str, Any]], None] | None = None,
    deferred_publish: list[Callable[[Callable[[Mapping[str, Any]], None] | None], dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    review, review_ref = _load_review(store, case_id, review_id)
    _check_review_revision(review, expected_review_revision)
    if record_kind == "proof":
        state = store.read_case(case_id)
        _require_current_owner(state, stamp, "root")
        if state["state_revision"] != expected_state_revision:
            raise ContractError("stale_revision", "case revision changed", details={"actual": state["state_revision"]})
        if review["status"] != "open" or review["note"] is None:
            raise ContractError("review_order", "the verifier must record the raw source note before proof submission")
        if (
            review["binding"]["source_revision"] != state["source_revision"]
            or review["binding"].get("math_source_ids") != source_projection(store, state)["math_source_ids"]
            or review["binding"]["candidate_revision"] != state["candidate_revision"]
            or review["binding"]["candidate_id"] != args["candidate_id"]
            or args["candidate_id"] != state["candidate_id"]
        ):
            raise ContractError("review_binding_mismatch", "proof does not match the open review binding")
        proof_id = new_id()
        raw, body_ref = store.read_user_file(args["body_path"])
        old_run_ids: list[str] = []
        seen_run_ids: set[str] = set()
        verified_runs: dict[str, dict[str, Any]] = {}
        for ref in args["evidence_refs"]:
            _validate_case_evidence_ref(store, state, ref)
            result_parts = _result_path_parts(case_id, ref["path"])
            if result_parts is None:
                continue
            run_id, _relative_path = result_parts
            if run_id not in state["run_ids"]:
                raise ContractError("evidence_insufficient", "Proof Result dependency is not a registered case Run")
            verified = _verified_run(store, state, run_id, verified_runs, require_current=False)
            if _ref_identity(ref) not in verified["results"]:
                raise ContractError("evidence_insufficient", "Proof Result dependency is not in its original Seal")
            if run_id not in seen_run_ids:
                old_run_ids.append(run_id)
                seen_run_ids.add(run_id)
        proof_body = store.write_immutable(
            f".reaslab/optagent/cases/{case_id}/proofs/{proof_id}/body{_suffix(args['body_path'])}",
            raw,
            body_ref["media_type"],
        )
        proof = {
            "schema_version": 2,
            "created_at": utc_now(),
            "created_by": dict(stamp),
            "proof_id": proof_id,
            "kind": args["kind"],
            "source_revision": state["source_revision"],
            "candidate_id": args["candidate_id"],
            "body": proof_body,
            "premises": list(args["premises"]),
            "evidence_refs": [dict(ref) for ref in args["evidence_refs"]],
            "old_run_ids": old_run_ids,
            "claim_kinds": list(args["claim_kinds"]),
            "status": "proposed",
        }
        proof_ref = store.write_record(f".reaslab/optagent/cases/{case_id}/proofs/{proof_id}.json", proof)

        def add_proof(current: dict[str, Any], final_validate: Callable[[Mapping[str, Any]], None] | None = None) -> None:
            if final_validate is not None:
                final_validate(current)
            if current["state_revision"] != expected_state_revision:
                raise ContractError("stale_revision", "case revision changed", details={"actual": current["state_revision"]})
            records = current.get("current_records") or {}
            if (records.get("reviews") or {}).get(review_id) != review_ref:
                raise ContractError("stale_revision", "current Review Ref changed before proof publication")
            if proof_id not in current["proof_ids"]:
                current["proof_ids"].append(proof_id)
            current.setdefault("decisions", []).append(
                {
                    "kind": "proof_proposal",
                    "note": "registered proposed proof bytes",
                    "refs": [dict(proof_ref)],
                    "stamp": dict(stamp),
                    "created_at": utc_now(),
                }
            )

        def publish_proof(final_validate: Callable[[Mapping[str, Any]], None] | None) -> dict[str, Any]:
            updated_state = store.cas_case(
                case_id,
                expected_state_revision,
                lambda current: add_proof(current, final_validate),
            )
            return {
                "state": _state_summary(store, updated_state),
                "proof": _object_summary(proof_id, proof_ref, "proposed", proof["source_revision"], None),
            }

        if deferred_publish is not None:
            deferred_publish.append(publish_proof)
            return {}
        return publish_proof(validate_current)

    reviewer = stamp.get("role") == "opt-verifier"
    owner = stamp.get("role") == "root"
    state = store.read_case(case_id)
    if reviewer:
        _require_review_actor(review, stamp)
    elif owner:
        _require_current_owner(state, stamp, "root")
    else:
        raise ContractError("permission_denied", "role cannot submit review records")
    if record_kind in {"issue", "outcome"} and not reviewer:
        raise ContractError("permission_denied", "only the verifier can submit review findings")
    if record_kind == "disposition" and not owner:
        raise ContractError("permission_denied", "only the current owner can add a disposition")
    if record_kind in {"issue", "outcome"} and review["status"] != "open":
        raise ContractError("review_order", "closed review cannot be edited by the verifier")
    if record_kind in {"issue", "outcome"} and review["note"] is None:
        raise ContractError("review_order", "raw source note must come before candidate findings")
    next_review = copy.deepcopy(review)
    if record_kind in {"issue", "outcome"}:
        pins = review.get("binding_evidence_refs")
        if not isinstance(pins, Mapping):
            pins = _pin_binding_evidence_refs(store, state, review["binding"])
            next_review["binding_evidence_refs"] = pins
        review_for_findings = copy.deepcopy(review)
        review_for_findings["binding_evidence_refs"] = copy.deepcopy(dict(pins))
    else:
        review_for_findings = review
    if record_kind == "issue":
        issue_args = dict(args)
        issue_args["evidence_refs"] = _resolve_evidence_paths(
            store,
            state,
            review_for_findings,
            args["evidence_paths"],
            pointer="/evidence_paths",
            immutable_workspace_evidence_directory=(
                f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/issue-evidence"
            ),
        )
        issue = _make_issue(store, review_for_findings, issue_args, stamp)
        _pin_resolved_proof_roots(next_review, issue["evidence_refs"])
        issue_ref = _write_issue_snapshot(store, case_id, review_id, issue)
        next_review["issues"].append(issue_ref)
        response_extra = {"issue_id": issue["issue_id"], "issue_ref": issue_ref}
    elif record_kind == "outcome":
        if (
            review["binding"].get("source_revision") != state["source_revision"]
            or review["binding"].get("math_source_ids") != source_projection(store, state)["math_source_ids"]
            or review["binding"].get("candidate_revision") != state["candidate_revision"]
            or review["binding"].get("candidate_id") != state["candidate_id"]
        ):
            raise ContractError("review_binding_mismatch", "review no longer matches the current mathematical binding")
        decision = args["decision"]
        feedback = list(args["feedback"])
        outcomes = list(next_review.get("outcomes", []))
        if decision == "needs_changes":
            if args["claims"] or not feedback:
                raise ContractError(
                    "invalid_argument",
                    "needs_changes requires empty claims and at least one actionable feedback item",
                    pointer="/feedback",
                )
            outcomes.append({
                "decision": "needs_changes",
                "feedback": feedback,
                "remaining": list(args["remaining"]),
                "stamp": dict(stamp),
                "created_at": utc_now(),
            })
            next_review["outcomes"] = outcomes
            response_extra = {
                "outcome": {
                    "decision": "needs_changes",
                    "feedback": feedback,
                    "next_action": "revise_answer_and_resubmit_same_review",
                }
            }
        elif decision == "ready":
            if feedback:
                raise ContractError(
                    "invalid_argument",
                    "ready outcome cannot contain change feedback",
                    pointer="/feedback",
                )
            report_path = args.get("report_path")
            if report_path is None:
                raise ContractError(
                    "evidence_insufficient",
                    "ready outcome must bind the final readable user artifact",
                    pointer="/report_path",
                    details={"required": True},
                )
            _, answer_ref = store.read_user_file(report_path)
            normalized_report_path = report_path.replace("\\", "/")
            claims = []
            for index, value in enumerate(args["claims"]):
                claim = dict(value)
                evidence_paths = [
                    path
                    for path in value["evidence_paths"]
                    if path.replace("\\", "/") != normalized_report_path
                ]
                claim["evidence_refs"] = _resolve_evidence_paths(
                    store,
                    state,
                    review_for_findings,
                    evidence_paths,
                    pointer=f"/claims/{index}/evidence_paths",
                )
                claim.pop("evidence_paths", None)
                claims.append(claim)
            canonical_claims = validate_review_claims(store, state, review_for_findings, claims)
            _pin_resolved_proof_roots(
                next_review,
                [ref for claim in canonical_claims for ref in claim["evidence_refs"]],
            )
            next_review["answer_ref"] = dict(answer_ref)
            next_review["status"] = "closed"
            next_review["conclusion"] = {
                "claims": canonical_claims,
                "remaining": list(args["remaining"]),
                "stamp": dict(stamp),
            }
            outcomes.append({
                "decision": "ready",
                "feedback": [],
                "remaining": list(args["remaining"]),
                "stamp": dict(stamp),
                "created_at": utc_now(),
            })
            next_review["outcomes"] = outcomes
            response_extra = {
                "outcome": {
                    "decision": "ready",
                    "answer_ref": dict(answer_ref),
                    "next_action": "return_ready_review_to_owner_for_delivery_decision",
                }
            }
        else:
            raise ContractError("invalid_argument", "review outcome decision is invalid", pointer="/decision")
    elif record_kind == "disposition":
        issue_index, issue, _ = _find_issue(store, case_id, review_id, next_review, args["issue_id"])
        disposition = {
            "diagnosis": args["diagnosis"],
            "reason": args["reason"],
            "patch_status": args["patch_status"],
            "stamp": dict(stamp),
            "created_at": utc_now(),
        }
        issue["owner_dispositions"].append(disposition)
        issue_ref = _write_issue_snapshot(store, case_id, review_id, issue)
        next_review["issues"][issue_index] = issue_ref
        response_extra = {"issue_id": issue["issue_id"], "issue_ref": issue_ref}
    else:
        raise ContractError("unknown_operation", "unknown review record kind")
    next_review["review_revision"] += 1
    if record_kind == "outcome":
        response_extra["outcome"].update({
            "review_id": review_id,
            "review_revision": next_review["review_revision"],
            "status": next_review["status"],
        })
    prepared_review_ref = (
        store.write_family_version(case_id, "reviews", review_id, next_review)
        if deferred_publish is not None
        else None
    )

    def publish_review(final_validate: Callable[[Mapping[str, Any]], None] | None) -> dict[str, Any]:
        next_ref = _publish_review(
            store,
            case_id,
            review_id,
            review,
            review_ref,
            next_review,
            stamp,
            record_kind=record_kind,
            validate_current=final_validate,
            prepared_ref=prepared_review_ref,
        )
        return {"review": _review_summary(next_review, next_ref), **response_extra}

    if deferred_publish is not None:
        deferred_publish.append(publish_review)
        return {}
    return publish_review(validate_current)


def _make_issue(
    store: CaseStore,
    review: Mapping[str, Any],
    args: Mapping[str, Any],
    stamp: Mapping[str, str],
) -> dict[str, Any]:
    valid_source_ids = set(review["raw_source_ids"])
    for loc in args["locations"]:
        if loc["source_id"] not in valid_source_ids:
            raise ContractError("review_binding_mismatch", "issue location points outside reviewed source")
    evidence_refs = [dict(ref) for ref in args["evidence_refs"]]
    for ref in evidence_refs:
        store.read_ref(ref)
    return {
        "issue_id": new_id(),
        "claim": args["claim"],
        "locations": [dict(item) for item in args["locations"]],
        "candidate_locations": list(args["candidate_locations"]),
        "evidence_refs": evidence_refs,
        "proposal_stamp": dict(stamp),
        "owner_dispositions": [],
    }


def _suffix(path: str) -> str:
    from pathlib import Path

    suffix = Path(path).suffix
    if not suffix or len(suffix) > 16 or any(not (char.isalnum() or char == ".") for char in suffix):
        return ".bin"
    return suffix


def _unique_refs(refs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int, str]] = set()
    for raw in refs:
        ref = validate_ref_shape(raw, pointer="/raw_source_refs")
        key = (ref["path"], ref["sha256"], ref["byte_len"], ref["media_type"])
        if key not in seen:
            seen.add(key)
            result.append(ref)
    return result


def _candidate_refs(
    store: CaseStore,
    case_id: str,
    binding: Mapping[str, Any],
    candidate_record_ref: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    candidate_id = binding.get("candidate_id")
    if candidate_id is None or candidate_record_ref is None:
        return []
    candidate_path = f".reaslab/optagent/cases/{case_id}/candidates/{candidate_id}.json"
    candidate_record_ref = validate_ref_shape(dict(candidate_record_ref), pointer="/binding_evidence_refs/candidate")
    if candidate_record_ref["path"] != candidate_path or candidate_record_ref["media_type"] != "application/json":
        raise ContractError("artifact_changed", "pinned Candidate Ref does not match the bound Candidate")
    candidate_bytes = store.read_ref(candidate_record_ref)
    candidate = strict_json_loads(candidate_bytes, max_bytes=len(candidate_bytes))
    if not isinstance(candidate, dict):
        raise ContractError("artifact_changed", "Candidate record is not a JSON object")
    _validate_candidate_record(
        {
            "source_revision": binding["source_revision"],
            "candidate_revision": binding["candidate_revision"],
        },
        candidate,
        candidate_id,
        binding["math_source_ids"],
    )
    refs = [candidate["model"]]
    refs.extend(candidate["local_modules"])
    if candidate.get("business_checker") is not None:
        refs.append(candidate["business_checker"])
    for ref in refs:
        store.read_ref(ref)
    return [candidate_record_ref, *[dict(ref) for ref in refs]]


def _proof_ref(
    store: CaseStore,
    case_id: str,
    proof_id: str,
    root_ref: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    path = f".reaslab/optagent/cases/{case_id}/proofs/{proof_id}.json"
    ref = validate_ref_shape(dict(root_ref), pointer="/binding_evidence_refs/proofs") if root_ref is not None else _record_ref(store, path)
    if ref["path"] != path or ref["media_type"] != "application/json":
        raise ContractError("artifact_changed", "pinned Proof Ref does not match its fixed path")
    store.read_ref(ref)
    return ref


def _run_ref(
    store: CaseStore,
    case_id: str,
    run_id: str,
    root_ref: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if root_ref is None:
        _, ref = store.read_family_record(
            case_id,
            "runs",
            run_id,
            legacy_path=f".reaslab/optagent/cases/{case_id}/runs/{run_id}/record.json",
        )
    else:
        ref = validate_ref_shape(dict(root_ref), pointer="/binding_evidence_refs/runs")
        prefix = f".reaslab/optagent/cases/{case_id}/runs/{run_id}/versions/"
        legacy = f".reaslab/optagent/cases/{case_id}/runs/{run_id}/record.json"
        if (not ref["path"].startswith(prefix) and ref["path"] != legacy) or ref["media_type"] != "application/json":
            raise ContractError("artifact_changed", "pinned Run Ref does not match its fixed path")
        store.read_ref(ref)
    return dict(ref)


def _record_ref(store: CaseStore, path: str) -> dict[str, Any]:
    store.resolve_path(path, must_exist=True)
    raw = store._read_stable_path(store.resolve_path(path, must_exist=True))
    return {"path": path, "sha256": sha256_bytes(raw), "byte_len": len(raw), "media_type": "application/json"}


def _decision_record_ref(
    store: CaseStore,
    state: Mapping[str, Any],
    *,
    decision_kind: str,
    expected_path: str,
) -> dict[str, Any]:
    matches: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    for decision in state.get("decisions", []):
        if not isinstance(decision, Mapping) or decision.get("kind") != decision_kind:
            continue
        refs = decision.get("refs")
        if not isinstance(refs, list):
            continue
        for value in refs:
            if not isinstance(value, Mapping):
                continue
            ref = validate_ref_shape(dict(value), pointer=f"/decisions/{decision_kind}/refs")
            if ref["path"] == expected_path:
                matches[(ref["path"], ref["sha256"], ref["byte_len"], ref["media_type"])] = ref
    if len(matches) > 1:
        raise ContractError("artifact_changed", "record has conflicting creation Refs")
    if matches:
        ref = next(iter(matches.values()))
    else:
        # Legacy records predate persisted creation Refs. Capture only their
        # first disclosure under this Review and pin it in that Review.
        ref = store.ref_for_path(expected_path, media_type="application/json")
    if ref["path"] != expected_path or ref["media_type"] != "application/json":
        raise ContractError("artifact_changed", "record creation Ref does not match its fixed path")
    store.read_ref(ref)
    return dict(ref)


def _read_json_ref(store: CaseStore, raw_ref: Mapping[str, Any], *, pointer: str) -> tuple[dict[str, Any], dict[str, Any]]:
    ref = validate_ref_shape(dict(raw_ref), pointer=pointer)
    if ref["media_type"] != "application/json":
        raise ContractError("artifact_changed", "review evidence record is not JSON", pointer=pointer)
    raw = store.read_ref(ref)
    try:
        value = strict_json_loads(raw, max_bytes=len(raw))
    except (ValueError, UnicodeError) as exc:
        raise ContractError("artifact_changed", "review evidence record is invalid JSON", pointer=pointer) from exc
    if not isinstance(value, dict):
        raise ContractError("artifact_changed", "review evidence record is not an object", pointer=pointer)
    return value, ref


def _pin_binding_evidence_refs(
    store: CaseStore,
    state: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    case_id = state["case_id"]
    candidate_id = binding.get("candidate_id")
    candidate_ref = None
    if candidate_id is not None:
        path = f".reaslab/optagent/cases/{case_id}/candidates/{candidate_id}.json"
        candidate_ref = _decision_record_ref(
            store, state, decision_kind="candidate_change", expected_path=path
        )
        candidate, _ = _read_json_ref(store, candidate_ref, pointer="/binding_evidence_refs/candidate")
        _validate_candidate_record(
            {"source_revision": binding["source_revision"], "candidate_revision": binding["candidate_revision"]},
            candidate,
            candidate_id,
            binding["math_source_ids"],
        )

    proof_refs: dict[str, dict[str, Any]] = {}
    for proof_id in binding.get("proof_ids", []):
        path = f".reaslab/optagent/cases/{case_id}/proofs/{proof_id}.json"
        ref = _decision_record_ref(store, state, decision_kind="proof_proposal", expected_path=path)
        proof, _ = _read_json_ref(store, ref, pointer=f"/binding_evidence_refs/proofs/{proof_id}")
        if proof.get("proof_id") != proof_id:
            raise ContractError("artifact_changed", "Proof identity does not match its fixed path")
        proof_refs[proof_id] = ref

    run_refs: dict[str, dict[str, Any]] = {}
    records = state.get("current_records")
    current_runs = records.get("runs", {}) if isinstance(records, Mapping) else {}
    for run_id in binding.get("run_ids", []):
        ref_value = current_runs.get(run_id) if isinstance(current_runs, Mapping) else None
        if ref_value is None:
            _, ref = store.read_family_record(
                case_id,
                "runs",
                run_id,
                legacy_path=f".reaslab/optagent/cases/{case_id}/runs/{run_id}/record.json",
            )
        else:
            ref = validate_ref_shape(dict(ref_value), pointer=f"/current_records/runs/{run_id}")
        run, _ = _read_json_ref(store, ref, pointer=f"/binding_evidence_refs/runs/{run_id}")
        if run.get("run_id") != run_id:
            raise ContractError("artifact_changed", "Run identity does not match its bound ID")
        run_refs[run_id] = dict(ref)
    return {"candidate": candidate_ref, "proofs": proof_refs, "runs": run_refs}


def _ref_identity(ref: Mapping[str, Any]) -> tuple[str, str, int, str]:
    return (ref["path"], ref["sha256"], ref["byte_len"], ref["media_type"])


def _evidence_content_identity(ref: Mapping[str, Any]) -> tuple[str, str, int]:
    """Identify pinned bytes for path resolution; MIME is descriptive metadata."""
    return (ref["path"], ref["sha256"], ref["byte_len"])


def _add_index_ref(index: dict[str, dict[tuple[str, str, int], dict[str, Any]]], raw_ref: Any) -> None:
    if not isinstance(raw_ref, Mapping):
        return
    ref = validate_ref_shape(dict(raw_ref), pointer="/review/evidence")
    identities = index.setdefault(ref["path"].replace("\\", "/"), {})
    identities.setdefault(_evidence_content_identity(ref), ref)


def _review_evidence_index(
    store: CaseStore,
    state: Mapping[str, Any],
    review: Mapping[str, Any],
) -> dict[str, dict[tuple[str, str, int], dict[str, Any]]]:
    """Index only explicit Ref fields reachable from this Review's pinned roots."""
    case_id = state["case_id"]
    index: dict[str, dict[tuple[str, str, int], dict[str, Any]]] = {}
    provenance_ref = validate_ref_shape(dict(review["source_provenance_ref"]), pointer="/source_provenance_ref")
    expected_provenance = f".reaslab/optagent/cases/{case_id}/reviews/{review['review_id']}/source-provenance.json"
    if provenance_ref["path"] != expected_provenance:
        raise ContractError("artifact_changed", "Review provenance Ref is outside its fixed path")
    _add_index_ref(index, provenance_ref)
    note = review.get("note")
    if note is not None:
        if not isinstance(note, Mapping):
            raise ContractError("artifact_changed", "Review note record is invalid")
        note_ref = validate_ref_shape(note.get("body"), pointer="/review/note/body")
        note_prefix = f".reaslab/optagent/cases/{case_id}/reviews/"
        note_suffix = note_ref["path"][len(note_prefix) :] if note_ref["path"].startswith(note_prefix) else ""
        note_parts = note_suffix.split("/")
        note_review_id = note_parts[0] if len(note_parts) == 2 and note_parts[1] == "note.txt" else None
        if note_review_id is None:
            raise ContractError("artifact_changed", "Review note Ref is outside its fixed note path")
        if note_review_id != review.get("review_id"):
            prior_ref_value = review.get("prior_review_ref")
            prior_review_ref = (
                validate_ref_shape(dict(prior_ref_value), pointer="/prior_review_ref")
                if isinstance(prior_ref_value, Mapping)
                else None
            )
            if prior_review_ref is None or not _is_published_review_ref(case_id, note_review_id, prior_review_ref):
                raise ContractError("artifact_changed", "reused Review note is not bound to its published prior Review")
        store.read_ref(note_ref)
        require_stamp(note.get("stamp"), pointer="/review/note/stamp")
        _add_index_ref(index, note_ref)
    provenance, _ = _read_json_ref(store, provenance_ref, pointer="/source_provenance_ref")

    def add_source_refs(source: Any) -> None:
        if not isinstance(source, Mapping):
            return
        raw = source.get("raw")
        if isinstance(raw, Mapping):
            _add_index_ref(index, raw.get("ref"))
        context = source.get("conversation_context")
        if isinstance(context, Mapping):
            _add_index_ref(index, context.get("ref"))
            _add_index_ref(index, context.get("question_ref"))

    for source in provenance.get("sources", []):
        add_source_refs(source)
    route = provenance.get("route")
    if isinstance(route, Mapping):
        for guard in route.get("guards", []):
            if isinstance(guard, Mapping):
                _add_index_ref(index, guard.get("gap_ref"))
                adoption = guard.get("adoption")
                if isinstance(adoption, Mapping):
                    for source in adoption.get("sources", []):
                        add_source_refs(source)
        for handoff in route.get("handoffs", []):
            if isinstance(handoff, Mapping):
                for ref in handoff.get("guard_refs", []):
                    _add_index_ref(index, ref)

    pins = review.get("binding_evidence_refs")
    if not isinstance(pins, Mapping):
        raise ContractError("artifact_changed", "Review evidence roots were not pinned")
    candidate_id = review.get("binding", {}).get("candidate_id")
    candidate_root = pins.get("candidate")
    if candidate_id is not None:
        candidate_ref = validate_ref_shape(dict(candidate_root), pointer="/binding_evidence_refs/candidate")
        candidate_path = f".reaslab/optagent/cases/{case_id}/candidates/{candidate_id}.json"
        if candidate_ref["path"] != candidate_path:
            raise ContractError("artifact_changed", "pinned Candidate Ref does not match the Review binding")
        _add_index_ref(index, candidate_ref)
        candidate, _ = _read_json_ref(store, candidate_ref, pointer="/binding_evidence_refs/candidate")
        for value in [candidate.get("model"), *candidate.get("local_modules", [])]:
            _add_index_ref(index, value)
        _add_index_ref(index, candidate.get("business_checker"))

    for proof_id in review.get("binding", {}).get("proof_ids", []):
        raw_ref = pins.get("proofs", {}).get(proof_id) if isinstance(pins.get("proofs"), Mapping) else None
        if raw_ref is None:
            raise ContractError("artifact_changed", "pinned Proof Ref is missing")
        proof_ref = validate_ref_shape(dict(raw_ref), pointer=f"/binding_evidence_refs/proofs/{proof_id}")
        if proof_ref["path"] != f".reaslab/optagent/cases/{case_id}/proofs/{proof_id}.json":
            raise ContractError("artifact_changed", "pinned Proof Ref does not match its Review binding")
        _add_index_ref(index, proof_ref)
        proof, _ = _read_json_ref(store, proof_ref, pointer=f"/binding_evidence_refs/proofs/{proof_id}")
        _add_index_ref(index, proof.get("body"))
        for ref in proof.get("evidence_refs", []):
            _add_index_ref(index, ref)

    # A Proof proposed after the raw note is registered by the same Case CAS.
    # It is eligible only when its persisted creation Ref and mathematical
    # binding match this Review; no directory scan or path re-hash is used.
    proof_prefix = f".reaslab/optagent/cases/{case_id}/proofs/"
    bound_proof_ids = set(review.get("binding", {}).get("proof_ids", []))
    for decision in state.get("decisions", []):
        if not isinstance(decision, Mapping) or decision.get("kind") != "proof_proposal":
            continue
        decision_refs = decision.get("refs")
        if not isinstance(decision_refs, list):
            continue
        for raw_ref in decision_refs:
            if not isinstance(raw_ref, Mapping):
                continue
            ref = validate_ref_shape(dict(raw_ref), pointer="/decisions/proof_proposal/refs")
            if not ref["path"].startswith(proof_prefix) or not ref["path"].endswith(".json"):
                continue
            proof_id = ref["path"][len(proof_prefix):-5]
            if not proof_id or "/" in proof_id or proof_id in bound_proof_ids or proof_id not in state.get("proof_ids", []):
                continue
            proof, _ = _read_json_ref(store, ref, pointer=f"/decisions/proof_proposal/{proof_id}")
            if (
                proof.get("proof_id") == proof_id
                and proof.get("source_revision") == review.get("binding", {}).get("source_revision")
                and proof.get("candidate_id") == review.get("binding", {}).get("candidate_id")
            ):
                _add_index_ref(index, ref)
                _add_index_ref(index, proof.get("body"))
                for value in proof.get("evidence_refs", []):
                    _add_index_ref(index, value)

    for run_id in review.get("binding", {}).get("run_ids", []):
        raw_ref = pins.get("runs", {}).get(run_id) if isinstance(pins.get("runs"), Mapping) else None
        if raw_ref is None:
            raise ContractError("artifact_changed", "pinned Run Ref is missing")
        run_ref = validate_ref_shape(dict(raw_ref), pointer=f"/binding_evidence_refs/runs/{run_id}")
        _add_index_ref(index, run_ref)
        run, _ = _read_json_ref(store, run_ref, pointer=f"/binding_evidence_refs/runs/{run_id}")
        if run.get("run_id") != run_id:
            raise ContractError("artifact_changed", "pinned Run record identity differs")
        for container, key in ((run.get("transport"), "response"), (run.get("platform"), "status_evidence")):
            if isinstance(container, Mapping):
                _add_index_ref(index, container.get(key))
        cancel = run.get("cancel")
        if isinstance(cancel, Mapping):
            # ``cancel.evidence`` is the embedded cancellation event, not an
            # Artifact Ref.  The Run itself is already pinned above; only the
            # explicit receipt fields below carry Ref contracts.
            _add_index_ref(index, cancel.get("stop_ack"))
            stop = cancel.get("stop_attempt")
            if isinstance(stop, Mapping):
                _add_index_ref(index, stop.get("receipt"))
        worker = run.get("worker")
        seal_ref = worker.get("seal") if isinstance(worker, Mapping) else None
        if isinstance(seal_ref, Mapping):
            _add_index_ref(index, seal_ref)
            seal, _ = _read_json_ref(store, seal_ref, pointer=f"/runs/{run_id}/worker/seal")
            output_refs = seal.get("output_refs", [])
            if isinstance(output_refs, list):
                for ref in output_refs:
                    _add_index_ref(index, ref)
        for result_ref in run.get("results", []) if isinstance(run.get("results"), list) else []:
            _add_index_ref(index, result_ref)
            result, _ = _read_json_ref(store, result_ref, pointer=f"/runs/{run_id}/results")
            for key in ("check", "witness", "business_check"):
                _add_index_ref(index, result.get(key))
    return index


def _pin_resolved_proof_roots(review: dict[str, Any], refs: Sequence[Mapping[str, Any]]) -> None:
    pins = review.get("binding_evidence_refs")
    if not isinstance(pins, dict):
        return
    prefix = f".reaslab/optagent/cases/{review.get('case_id')}/proofs/"
    proof_map = pins.setdefault("proofs", {})
    if not isinstance(proof_map, dict):
        return
    for ref in refs:
        path = ref.get("path")
        if not isinstance(path, str) or not path.startswith(prefix) or not path.endswith(".json"):
            continue
        proof_id = path[len(prefix):-5]
        if proof_id and "/" not in proof_id:
            proof_map.setdefault(proof_id, dict(ref))


def _resolve_evidence_paths(
    store: CaseStore,
    state: Mapping[str, Any],
    review: Mapping[str, Any],
    paths: Sequence[str],
    *,
    pointer: str,
    immutable_workspace_evidence_directory: str | None = None,
) -> list[dict[str, Any]]:
    index = _review_evidence_index(store, state, review)
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    internal_prefix = ".reaslab/"
    case_prefix = f".reaslab/optagent/cases/{state['case_id']}/"
    for path_index, raw_path in enumerate(paths):
        item_pointer = f"{pointer}/{path_index}"
        if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
            raise ContractError("invalid_reference", "evidence path must be a workspace-relative path", pointer=item_pointer)
        normalized = raw_path.replace("\\", "/")
        path_parts = normalized.split("/")
        if normalized.startswith("/") or len(normalized) > 1 and normalized[1] == ":" or any(part in {"", ".", ".."} for part in path_parts):
            raise ContractError("invalid_reference", "evidence path must be a normalized workspace-relative path", pointer=item_pointer)
        folded = normalized.casefold()
        if folded.startswith(".reaslab/optagent/cases/") and not folded.startswith(case_prefix.casefold()):
            raise ContractError("permission_denied", "evidence path belongs to another Case", pointer=item_pointer)
        matches = index.get(normalized)
        if matches:
            if len(matches) != 1:
                raise ContractError(
                    "invalid_reference",
                    "evidence path maps to multiple pinned identities",
                    pointer=item_pointer,
                    details={"path": normalized, "identities": len(matches)},
                )
            ref = next(iter(matches.values()))
            raw = store.read_ref(ref)
        elif folded == ".reaslab" or folded.startswith(internal_prefix):
            known_paths = sorted(index)[:64]
            raise ContractError(
                "invalid_reference",
                "internal evidence path is not reachable from this Review's bound records",
                pointer=item_pointer,
                details={"path": normalized, "known_paths": known_paths},
            )
        else:
            try:
                raw, ref = store.read_user_file(normalized)
            except ContractError as exc:
                if exc.code == "not_found":
                    raise ContractError(
                        "invalid_reference",
                        "workspace evidence path does not exist",
                        pointer=item_pointer,
                        details={"path": normalized},
                    ) from exc
                raise
        if immutable_workspace_evidence_directory is not None and not ref["path"].replace("\\", "/").casefold().startswith(internal_prefix):
            ref = store.write_content_addressed(
                immutable_workspace_evidence_directory,
                raw,
                ref["media_type"],
                suffix=f"/{normalized}",
            )
        identity = _evidence_content_identity(ref)
        if identity not in seen:
            seen.add(identity)
            result.append(dict(ref))
    return result


def _load_review(store: CaseStore, case_id: str, review_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    path = f".reaslab/optagent/cases/{case_id}/reviews/{review_id}.json"
    review, ref = store.read_family_record(case_id, "reviews", review_id, legacy_path=path)
    if review.get("case_id") != case_id or review.get("review_id") != review_id:
        raise ContractError("not_found", "review does not belong to this case")
    return review, ref


def _is_published_review_ref(case_id: str, review_id: str, raw_ref: Mapping[str, Any]) -> bool:
    try:
        ref = validate_ref_shape(dict(raw_ref), pointer="/prior_review_ref")
    except ContractError:
        return False
    prefix = f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/versions/"
    return (
        ref["path"].startswith(prefix)
        and ref["path"] == prefix + ref["sha256"] + ".json"
        and ref["media_type"] == "application/json"
    )


def _prior_provenance_scope_matches(
    store: CaseStore,
    case_id: str,
    prior: Mapping[str, Any],
    current: Mapping[str, Any],
) -> bool:
    prior_ref_value = prior.get("source_provenance_ref")
    if not isinstance(prior_ref_value, Mapping):
        return False
    try:
        ref = validate_ref_shape(dict(prior_ref_value), pointer="/prior_review/source_provenance_ref")
        expected = f".reaslab/optagent/cases/{case_id}/reviews/{prior['review_id']}/source-provenance.json"
        if ref["path"] != expected or ref["media_type"] != "application/json":
            return False
        raw = store.read_ref(ref)
        if sha256_bytes(raw) != prior.get("source_provenance_digest"):
            return False
        previous = strict_json_loads(raw, max_bytes=len(raw))
    except (ContractError, ValueError, UnicodeError, KeyError, TypeError):
        return False
    previous_scope = _reviewed_provenance_scope(previous)
    current_scope = _reviewed_provenance_scope(current)
    return previous_scope is not None and previous_scope == current_scope


def _reviewed_provenance_scope(value: Any) -> Any:
    """Compare mathematical Sources and route gaps/adoptions, not control-only handoffs."""
    if not isinstance(value, Mapping):
        return None
    binding = value.get("source_binding")
    sources = value.get("sources")
    route = value.get("route")
    if not isinstance(binding, Mapping) or not isinstance(sources, list) or not isinstance(route, Mapping):
        return None
    math_ids = binding.get("math_source_ids")
    guards = route.get("guards")
    if (
        not isinstance(math_ids, list)
        or any(not isinstance(source_id, str) for source_id in math_ids)
        or not isinstance(guards, list)
    ):
        return None
    math_id_set = set(math_ids)

    def source_scope(items: Any) -> list[Any] | None:
        if not isinstance(items, list):
            return None
        selected = []
        for item in items:
            if not isinstance(item, Mapping):
                return None
            if item.get("source_id") in math_id_set:
                selected.append(dict(item))
        return selected

    normalized_guards: list[Any] = []
    for guard in guards:
        if not isinstance(guard, Mapping) or not isinstance(guard.get("scope"), Mapping):
            return None
        normalized: dict[str, Any] = {
            "gap_ref": guard.get("gap_ref"),
            "scope": {
                key: guard["scope"].get(key)
                for key in ("session_id", "covered_seq", "missing_seq_ranges")
            },
            "unresolved": guard.get("unresolved"),
        }
        adoption = guard.get("adoption")
        if adoption is not None:
            if not isinstance(adoption, Mapping):
                return None
            adopted_sources = adoption.get("sources")
            if not isinstance(adopted_sources, list) or any(not isinstance(item, Mapping) for item in adopted_sources):
                return None
            normalized["adoption"] = {
                key: adoption.get(key)
                for key in ("fingerprint", "capture_id", "session_id", "arrival_seq")
            }
            normalized["adoption"]["sources"] = [
                dict(item) for item in adopted_sources
                if isinstance(item, Mapping) and item.get("purpose") in {"requirement", "data"}
            ]
        normalized_guards.append(normalized)
    selected_sources = source_scope(sources)
    if selected_sources is None:
        return None
    return {
        "math_source_ids": list(math_ids),
        "sources": selected_sources,
        "guards": normalized_guards,
    }


def _publish_review_pointer(state: dict[str, Any], review_id: str, ref: Mapping[str, Any]) -> None:
    records = state.get("current_records")
    if records is None:
        records = {"questions": {}, "reviews": {}, "runs": {}, "presentation": None}
        state["current_records"] = records
    records["reviews"][review_id] = dict(ref)


def _publish_review(
    store: CaseStore,
    case_id: str,
    review_id: str,
    prior: Mapping[str, Any],
    prior_ref: Mapping[str, Any],
    next_review: Mapping[str, Any],
    stamp: Mapping[str, Any],
    *,
    record_kind: str,
    validate_current: Callable[[Mapping[str, Any]], None] | None = None,
    prepared_ref: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    next_ref = (
        validate_ref_shape(dict(prepared_ref), pointer="/current_records/reviews")
        if prepared_ref is not None
        else store.write_family_version(case_id, "reviews", review_id, next_review)
    )
    legacy_path = f".reaslab/optagent/cases/{case_id}/reviews/{review_id}.json"

    def update(current: dict[str, Any]) -> None:
        if validate_current is not None:
            validate_current(current)
        if stamp.get("role") == "root" and current.get("owner_session_id") != stamp.get("session_id"):
            raise ContractError("permission_denied", "only the current Owner can publish a review disposition")
        if stamp.get("role") == "opt-verifier" and next_review.get("created_by", {}).get("session_id") != stamp.get("session_id"):
            raise ContractError("permission_denied", "review publisher differs from its verifier session")
        if record_kind != "disposition":
            binding = next_review.get("binding")
            if not isinstance(binding, Mapping) or (
                binding.get("source_revision") != current["source_revision"]
                or binding.get("candidate_revision") != current["candidate_revision"]
                or binding.get("candidate_id") != current["candidate_id"]
            ):
                raise ContractError("review_binding_mismatch", "review no longer matches the current mathematical binding")
        if prior.get("review_revision") != next_review.get("review_revision") - 1:
            raise ContractError("stale_revision", "review revision is not a single current transition")
        _publish_review_pointer(current, review_id, next_ref)

    store.cas_case_family(case_id, "reviews", review_id, prior_ref, update, legacy_path=legacy_path)
    return next_ref


def _write_issue_snapshot(
    store: CaseStore,
    case_id: str,
    review_id: str,
    issue: Mapping[str, Any],
) -> dict[str, Any]:
    raw = canonical_json_bytes(dict(issue))
    digest = sha256_bytes(raw)
    path = f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/issues/{issue['issue_id']}/versions/{digest}.json"
    return store._write_immutable_known_sha(path, raw, "application/json", digest)


def _find_issue(
    store: CaseStore,
    case_id: str,
    review_id: str,
    review: Mapping[str, Any],
    issue_id: str,
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    matches: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    prefix = f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/issues/"
    seen_ids: set[str] = set()
    for index, raw_ref in enumerate(review.get("issues", [])):
        ref = validate_ref_shape(raw_ref, pointer=f"/review/issues/{index}")
        if not ref["path"].startswith(prefix) or ref["media_type"] != "application/json":
            raise ContractError("artifact_changed", "review Issue Ref does not match its immutable path")
        suffix = ref["path"][len(prefix):]
        parts = suffix.split("/")
        if len(parts) != 3 or parts[1] != "versions" or parts[2] != f"{ref['sha256']}.json":
            raise ContractError("artifact_changed", "review Issue Ref does not match its immutable path")
        candidate_id = parts[0]
        require_id(candidate_id, pointer=f"/review/issues/{index}")
        if not candidate_id or candidate_id in seen_ids:
            raise ContractError("artifact_changed", "review Issue IDs are empty or repeated")
        seen_ids.add(candidate_id)
        if candidate_id == issue_id:
            issue, _raw, issue_ref = _read_issue_snapshot(store, case_id, review_id, ref)
            matches.append((index, issue, issue_ref))
    if len(matches) != 1:
        if matches:
            raise ContractError("artifact_changed", "review Issue identity is repeated")
        raise ContractError("not_found", "review issue does not exist")
    return matches[0]


def _read_issue_snapshot(
    store: CaseStore,
    case_id: str,
    review_id: str,
    raw_ref: Mapping[str, Any],
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    ref = validate_ref_shape(dict(raw_ref), pointer="/review/issues")
    prefix = f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/issues/"
    if not ref["path"].startswith(prefix) or ref["media_type"] != "application/json":
        raise ContractError("artifact_changed", "review Issue Ref is outside its immutable family path")
    raw = store.read_ref(ref)
    try:
        issue = strict_json_loads(raw, max_bytes=len(raw))
    except (ValueError, UnicodeError) as exc:
        raise ContractError("artifact_changed", "review Issue snapshot is invalid") from exc
    if not isinstance(issue, dict) or not isinstance(issue.get("issue_id"), str) or canonical_json_bytes(issue) != raw:
        raise ContractError("artifact_changed", "review Issue snapshot has invalid identity or is not canonical")
    require_id(issue["issue_id"], pointer="/review/issues/issue_id")
    _validate_issue_snapshot(issue)
    expected_path = f"{prefix}{issue['issue_id']}/versions/{ref['sha256']}.json"
    if ref["path"] != expected_path:
        raise ContractError("artifact_changed", "review Issue Ref does not match its immutable path")
    return issue, raw, ref


def _validate_issue_snapshot(issue: Mapping[str, Any]) -> None:
    expected_fields = {
        "issue_id", "claim", "locations", "candidate_locations", "evidence_refs",
        "proposal_stamp", "owner_dispositions",
    }
    if set(issue) != expected_fields:
        raise ContractError("artifact_changed", "review Issue snapshot has invalid fields")
    if not isinstance(issue["claim"], str) or not issue["claim"]:
        raise ContractError("artifact_changed", "review Issue claim is invalid")
    locations = issue["locations"]
    candidates = issue["candidate_locations"]
    evidence_refs = issue["evidence_refs"]
    dispositions = issue["owner_dispositions"]
    if not isinstance(locations, list) or not isinstance(candidates, list) or not isinstance(evidence_refs, list) or not isinstance(dispositions, list):
        raise ContractError("artifact_changed", "review Issue collections are invalid")
    for index, location in enumerate(locations):
        if not isinstance(location, Mapping) or set(location) != {"source_id", "locator"}:
            raise ContractError("artifact_changed", "review Issue source location is invalid", pointer=f"/review/issues/locations/{index}")
        require_id(location["source_id"], pointer=f"/review/issues/locations/{index}/source_id")
        if not isinstance(location["locator"], str) or not location["locator"]:
            raise ContractError("artifact_changed", "review Issue source locator is invalid", pointer=f"/review/issues/locations/{index}/locator")
    if any(not isinstance(item, str) or not item for item in candidates):
        raise ContractError("artifact_changed", "review Issue candidate locations are invalid")
    for index, value in enumerate(evidence_refs):
        validate_ref_shape(value, pointer=f"/review/issues/evidence_refs/{index}")
    require_stamp(issue["proposal_stamp"], pointer="/review/issues/proposal_stamp")
    for index, disposition in enumerate(dispositions):
        pointer = f"/review/issues/owner_dispositions/{index}"
        expected_disposition_fields = {"diagnosis", "reason", "patch_status", "stamp", "created_at"}
        if not isinstance(disposition, Mapping) or set(disposition) != expected_disposition_fields:
            raise ContractError("artifact_changed", "review Issue owner disposition is invalid", pointer=pointer)
        if disposition["diagnosis"] not in {"confirmed", "disproved", "unresolved"}:
            raise ContractError("artifact_changed", "review Issue diagnosis is invalid", pointer=f"{pointer}/diagnosis")
        if disposition["patch_status"] not in {"unchecked", "accepted", "rejected"}:
            raise ContractError("artifact_changed", "review Issue patch status is invalid", pointer=f"{pointer}/patch_status")
        if not isinstance(disposition["reason"], str) or not disposition["reason"]:
            raise ContractError("artifact_changed", "review Issue disposition reason is invalid", pointer=f"{pointer}/reason")
        if not isinstance(disposition["created_at"], str) or not disposition["created_at"]:
            raise ContractError("artifact_changed", "review Issue disposition timestamp is invalid", pointer=f"{pointer}/created_at")
        require_stamp(disposition["stamp"], pointer=f"{pointer}/stamp")


def _require_current_owner(state: Mapping[str, Any], stamp: Mapping[str, str], role: str) -> None:
    if stamp.get("role") != role or state.get("owner_session_id") != stamp.get("session_id"):
        raise ContractError("permission_denied", "caller is not the current case owner")


def _require_review_actor(review: Mapping[str, Any], stamp: Mapping[str, str]) -> None:
    if stamp.get("role") != "opt-verifier" or review["created_by"].get("session_id") != stamp.get("session_id"):
        raise ContractError("permission_denied", "caller is not the review's verifier session")


def _check_review_revision(review: Mapping[str, Any], expected: int) -> None:
    actual = review["review_revision"]
    if actual != expected:
        raise ContractError("stale_revision", "review revision changed", details={"expected": expected, "actual": actual})


def _review_summary(review: Mapping[str, Any], record_ref: Mapping[str, Any]) -> dict[str, Any]:
    outcomes = review.get("outcomes", [])
    latest_outcome = outcomes[-1] if isinstance(outcomes, list) and outcomes else None
    return {
        "id": review["review_id"],
        "record_ref": dict(record_ref),
        "review_revision": review["review_revision"],
        "status": review["status"],
        "outcome": copy.deepcopy(latest_outcome),
        "answer_ref": copy.deepcopy(review.get("answer_ref")),
        "source_revision": review["binding"]["source_revision"],
        "candidate_revision": review["binding"]["candidate_revision"],
    }


def _object_summary(object_id: str, ref: Mapping[str, Any], status: str, source_revision: int | None, candidate_revision: int | None) -> dict[str, Any]:
    return {
        "id": object_id,
        "record_ref": dict(ref),
        "status": status,
        "source_revision": source_revision,
        "candidate_revision": candidate_revision,
    }


def _state_summary(store: CaseStore, state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "case_id": state["case_id"],
        "title": state["title"],
        "owner_session_id": state["owner_session_id"],
        "state_revision": state["state_revision"],
        "source_revision": state["source_revision"],
        "candidate_revision": state["candidate_revision"],
        "promotion_generation": state["promotion_generation"],
        "current_validity": "none" if state["selected_delivery_id"] is None else "stale",
        "pending_question_count": pending_question_count(store, state),
        "pending_input_count": len(state["pending_inputs"]),
    }


def add_note(
    store: CaseStore,
    *,
    case_id: str,
    review_id: str,
    expected_review_revision: int,
    body: str,
    source_locations: Sequence[Mapping[str, str]],
    stamp: Mapping[str, str],
) -> dict[str, Any]:
    with store.review_lock(review_id):
        return _add_note_locked(
            store,
            case_id=case_id,
            review_id=review_id,
            expected_review_revision=expected_review_revision,
            body=body,
            source_locations=source_locations,
            stamp=stamp,
        )


def submit_review(
    store: CaseStore,
    *,
    case_id: str,
    review_id: str,
    expected_review_revision: int,
    record_kind: str,
    stamp: Mapping[str, str],
    args: Mapping[str, Any],
    expected_state_revision: int | None = None,
    validate_current: Callable[[Mapping[str, Any]], None] | None = None,
    session_commit: Callable[
        [Callable[[Callable[[Mapping[str, Any]], None] | None], dict[str, Any]]], dict[str, Any]
    ] | None = None,
) -> dict[str, Any]:
    if session_commit is not None and record_kind in {"proof", "disposition"}:
        deferred: list[Callable[[Callable[[Mapping[str, Any]], None] | None], dict[str, Any]]] = []
        with store.review_lock(review_id):
            _submit_review_locked(
                store,
                case_id=case_id,
                review_id=review_id,
                expected_review_revision=expected_review_revision,
                record_kind=record_kind,
                stamp=stamp,
                args=args,
                expected_state_revision=expected_state_revision,
                validate_current=None,
                deferred_publish=deferred,
            )
        if len(deferred) != 1:
            raise ContractError("environment_unavailable", "review submission did not prepare one publication")
        return session_commit(deferred[0])
    with store.review_lock(review_id):
        return _submit_review_locked(
            store,
            case_id=case_id,
            review_id=review_id,
            expected_review_revision=expected_review_revision,
            record_kind=record_kind,
            stamp=stamp,
            args=args,
            expected_state_revision=expected_state_revision,
            validate_current=validate_current,
        )
