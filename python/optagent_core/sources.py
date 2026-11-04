"""Source bytes, ACP capture transfer, and initial case records."""

from __future__ import annotations

import datetime as dt
import keyword
import unicodedata
import uuid
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

from .contracts import (
    ContractError,
    canonical_json_bytes,
    decode_base64,
    require_hash,
    require_id,
    require_int,
    require_object,
    require_string,
    sha256_bytes,
    ref_from_bytes,
    strict_json_loads,
    validate_ref_shape,
)
from .store import CaseStore


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def case_artifact_dir(title: str) -> str:
    """Return the base visible directory for a new Case title."""
    normalized = unicodedata.normalize("NFKC", title).casefold()
    normalized = unicodedata.normalize("NFKC", normalized)
    parts: list[str] = []
    for char in normalized:
        if char.isidentifier() or ("A" + char).isidentifier():
            parts.append(char)
        elif parts and parts[-1] != "_":
            parts.append("_")

    slug = "".join(parts).strip("_")
    if not slug:
        slug = "case"
    slug = slug[:32].rstrip("_") or "case"
    if not slug[0].isidentifier():
        slug = f"case_{slug}"[:32].rstrip("_")
    if keyword.iskeyword(slug):
        slug = f"{slug}_case"
    if slug.casefold() in {"con", "prn", "aux", "nul"} or (
        len(slug) == 4
        and slug[:3].casefold() in {"com", "lpt"}
        and slug[3] in "123456789"
    ):
        slug = f"case_{slug}"
    # A final check keeps generated package directory names import-safe even
    # when truncation or Unicode normalization changed the leading character.
    if not slug.isidentifier() or keyword.iskeyword(slug):
        slug = "case"
    return f"cases/{slug}"


def case_artifact_dir_for_state(state: Mapping[str, Any]) -> str:
    """Read the stored suggestion, falling back without mutating legacy state."""
    value = state.get("artifact_dir")
    if isinstance(value, str) and value:
        return value
    return f"cases/case_{str(state['case_id']).replace('-', '')}"


def validate_capture_transfer(value: Any, *, expected_session_id: str | None = None) -> tuple[dict[str, Any], bytes]:
    capture = require_object(value, pointer="/capture")
    fields = {
        "capture_id",
        "arrival_epoch",
        "arrival_seq",
        "session_id",
        "channel",
        "admission",
        "utf8_bytes_base64",
        "sha256",
    }
    if set(capture) != fields:
        raise ContractError("invalid_argument", "capture transfer has unknown or missing fields", pointer="/capture")
    result = {
        "capture_id": require_id(capture["capture_id"], pointer="/capture/capture_id"),
        "arrival_epoch": require_id(capture["arrival_epoch"], pointer="/capture/arrival_epoch"),
        "arrival_seq": require_int(capture["arrival_seq"], pointer="/capture/arrival_seq"),
        "session_id": require_string(capture["session_id"], pointer="/capture/session_id"),
        "channel": require_string(capture["channel"], pointer="/capture/channel"),
        "admission": require_string(capture["admission"], pointer="/capture/admission"),
        "utf8_bytes_base64": capture["utf8_bytes_base64"],
        "sha256": require_hash(capture["sha256"], pointer="/capture/sha256"),
    }
    if result["channel"] not in {"prompt", "steer"}:
        raise ContractError("invalid_argument", "capture channel is invalid", pointer="/capture/channel")
    valid_admissions = {
        "prompt": {"received_prompt"},
        "steer": {"pending_steer", "accepted_steer", "unconfirmed_steer"},
    }
    if result["admission"] not in valid_admissions[result["channel"]]:
        raise ContractError("invalid_argument", "capture admission does not match channel", pointer="/capture/admission")
    if expected_session_id is not None and result["session_id"] != expected_session_id:
        raise ContractError("permission_denied", "capture belongs to another session", pointer="/capture/session_id")
    raw = decode_base64(result["utf8_bytes_base64"], pointer="/capture/utf8_bytes_base64")
    if sha256_bytes(raw) != result["sha256"]:
        raise ContractError("invalid_argument", "capture hash does not match its bytes", pointer="/capture/sha256")
    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ContractError("invalid_argument", "captured prompt is not valid UTF-8", pointer="/capture/utf8_bytes_base64") from exc
    return result, raw


def capture_summary(transfer: Mapping[str, Any]) -> dict[str, Any]:
    capture, raw = validate_capture_transfer(transfer)
    text = raw.decode("utf-8")
    preview = text[:512]
    return {
        "capture_id": capture["capture_id"],
        "arrival_epoch": capture["arrival_epoch"],
        "arrival_seq": capture["arrival_seq"],
        "channel": capture["channel"],
        "admission": capture["admission"],
        "sha256": capture["sha256"],
        "preview": preview,
        "total_bytes": len(raw),
        "truncated": len(text) > len(preview),
    }


def source_from_capture(
    store: CaseStore,
    *,
    case_id: str,
    purpose: str,
    capture_transfer: Mapping[str, Any],
    stamp: Mapping[str, str],
    locations: list[str] | None = None,
    conversation_context: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    capture, raw = validate_capture_transfer(capture_transfer, expected_session_id=stamp["session_id"])
    if not raw:
        raise ContractError("invalid_argument", "empty capture cannot be adopted as a source")
    if capture["admission"] == "pending_steer":
        raise ContractError("capture_pending", "pending steer cannot be adopted yet")
    if capture["admission"] == "unconfirmed_steer":
        raise ContractError("capture_unconfirmed", "unconfirmed steer cannot be adopted as a source")
    source_id = new_id()
    bytes_ref = store.capture_source_bytes(raw, digest=capture["sha256"])
    origin = {
        "workspace_path": None,
        "session_id": capture["session_id"],
        "capture_id": capture["capture_id"],
        "channel": capture["channel"],
        "arrival_epoch": capture["arrival_epoch"],
        "arrival_seq": capture["arrival_seq"],
        "transport_admission": capture["admission"],
    }
    source = _make_source(
        source_id,
        purpose,
        "external_prompt" if capture["channel"] == "prompt" else "external_steer",
        bytes_ref,
        origin,
        locations or [],
        stamp,
        conversation_context_ref=(
            store.write_content_addressed(
                f".reaslab/optagent/cases/{case_id}/source-context",
                canonical_json_bytes(dict(conversation_context)),
                "application/json",
            )
            if conversation_context is not None
            else None
        ),
    )
    record_ref = store.write_record(f".reaslab/optagent/cases/{case_id}/sources/{source_id}.json", source)
    return source, record_ref


def capture_file_source(
    store: CaseStore,
    *,
    case_id: str,
    path: str,
    purpose: str,
    locations: list[str],
    stamp: Mapping[str, str],
    stable_read: tuple[bytes, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Capture one stable read of a workspace file into the shared raw-byte pool."""
    raw, source_bytes_ref = stable_read or store.read_user_file(path)
    content_ref = store.capture_source_bytes(raw, digest=source_bytes_ref["sha256"])
    if source_bytes_ref["sha256"] != content_ref["sha256"] or source_bytes_ref["byte_len"] != content_ref["byte_len"]:
        raise ContractError("source_changed_during_read", "source bytes changed during capture")
    source_id = new_id()
    normalized_path = str(PurePosixPath(path.replace("\\", "/")))
    origin = {
        "workspace_path": normalized_path,
        "session_id": None,
        "capture_id": None,
        "channel": None,
        "arrival_epoch": None,
        "arrival_seq": None,
        "transport_admission": None,
    }
    source = _make_source(source_id, purpose, "file", content_ref, origin, locations, stamp)
    record_ref = store.write_record(f".reaslab/optagent/cases/{case_id}/sources/{source_id}.json", source)
    return source, record_ref


def reusable_file_source(
    store: CaseStore,
    state: Mapping[str, Any],
    *,
    path: str,
    purpose: str,
    locations: list[str],
    bytes_ref: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Reuse only an identical active file Source with the same declared identity."""
    if purpose != "data":
        return None
    normalized_path = str(PurePosixPath(path.replace("\\", "/")))
    identity = validate_ref_shape(bytes_ref, pointer="/source/bytes")
    view = source_projection(store, state)
    for source_id in view["source_ids"]:
        source = view["records"][source_id]
        if (
            source.get("purpose") == purpose
            and source.get("kind") == "file"
            and source.get("origin", {}).get("workspace_path") == normalized_path
            and source.get("locations") == locations
            and source.get("bytes", {}).get("sha256") == identity["sha256"]
            and source.get("bytes", {}).get("byte_len") == identity["byte_len"]
        ):
            return source, view["record_refs"][source_id]
    return None


def _make_source(
    source_id: str,
    purpose: str,
    kind: str,
    bytes_ref: Mapping[str, Any],
    origin: Mapping[str, Any],
    locations: list[str],
    stamp: Mapping[str, str],
    conversation_context_ref: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if purpose not in {"requirement", "data", "reference"}:
        raise ContractError("invalid_argument", "source purpose is invalid")
    byte_len = bytes_ref["byte_len"]
    digest = bytes_ref["sha256"]
    source = {
        "schema_version": 2,
        "created_at": utc_now(),
        "created_by": dict(stamp),
        "source_id": source_id,
        "purpose": purpose,
        "kind": kind,
        "bytes": dict(bytes_ref),
        "origin": {key: value for key, value in origin.items() if key != "stamp"},
        "captured_at": utc_now(),
        "original_size": byte_len,
        "original_sha256": digest,
        "read_atomicity": "single_read_checked",
        "locations": list(locations),
    }
    if conversation_context_ref is not None:
        source["conversation_context"] = dict(conversation_context_ref)
    return source


def source_projection(store: CaseStore, state: Mapping[str, Any]) -> dict[str, Any]:
    """Read registered Source records once and retain Case order.

    Requirement and data Sources form the mathematical binding. Reference
    Sources remain in complete provenance while additions to them alone do
    not invalidate that binding.
    """
    case_id = require_id(state.get("case_id"), pointer="/case_id")
    source_ids = state.get("source_ids")
    if not isinstance(source_ids, list) or any(not isinstance(item, str) for item in source_ids) or len(set(source_ids)) != len(source_ids):
        raise ContractError("output_invalid", "Case Source order is invalid")
    records: dict[str, dict[str, Any]] = {}
    refs: dict[str, dict[str, Any]] = {}
    mathematical_ids: list[str] = []
    for source_id_value in source_ids:
        source_id = require_id(source_id_value, pointer="/source_ids")
        relative = f".reaslab/optagent/cases/{case_id}/sources/{source_id}.json"
        raw = store._read_stable_path(store.resolve_path(relative, must_exist=True))
        record_ref = ref_from_bytes(relative, raw, "application/json")
        try:
            record = strict_json_loads(raw, max_bytes=len(raw))
        except (ValueError, UnicodeError) as exc:
            raise ContractError("output_invalid", "Source record is invalid JSON") from exc
        if not isinstance(record, dict) or record.get("source_id") != source_id:
            raise ContractError("artifact_changed", "Source record identity does not match the Case")
        purpose = record.get("purpose")
        if purpose not in {"requirement", "data", "reference"}:
            raise ContractError("output_invalid", "Source purpose is invalid")
        validate_ref_shape(record.get("bytes"), pointer=f"/sources/{source_id}/bytes")
        records[source_id] = record
        refs[source_id] = record_ref
        if purpose in {"requirement", "data"}:
            mathematical_ids.append(source_id)
    return {
        "source_ids": list(source_ids),
        "math_source_ids": mathematical_ids,
        "records": records,
        "record_refs": refs,
    }


def data_source_records(
    store: CaseStore,
    *,
    case_id: str,
    source_ids: Sequence[str],
    state: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve only manifest-declared data Source IDs from this Case.

    Active records reuse the Case projection when supplied. A historical
    DataManifest may name a Source no longer active on the Case; that record
    is read only from its exact ID path, with no directory scan or promotion
    back into the current Source set.
    """
    checked_case_id = require_id(case_id, pointer="/case_id")
    if not isinstance(source_ids, Sequence) or isinstance(source_ids, (str, bytes)):
        raise ContractError("output_invalid", "DataManifest Source IDs must be an ordered array")
    checked_ids = [require_id(item, pointer="/source_ids") for item in source_ids]
    if len(set(checked_ids)) != len(checked_ids):
        raise ContractError("output_invalid", "DataManifest Source IDs repeat")
    active: Mapping[str, Mapping[str, Any]] = {}
    if state is not None:
        if state.get("case_id") != checked_case_id:
            raise ContractError("artifact_changed", "Data source lookup Case identity changed")
        active = source_projection(store, state)["records"]
    records: dict[str, dict[str, Any]] = {}
    for source_id in checked_ids:
        record = active.get(source_id)
        if record is None:
            record = store.read_record(f".reaslab/optagent/cases/{checked_case_id}/sources/{source_id}.json")
        if (
            not isinstance(record, Mapping)
            or record.get("source_id") != source_id
            or record.get("purpose") != "data"
        ):
            raise ContractError("artifact_changed", "DataManifest Source ID does not resolve to a data Source record")
        validate_ref_shape(record.get("bytes"), pointer=f"/sources/{source_id}/bytes")
        records[source_id] = dict(record)
    return records


def initial_case_state(
    *,
    case_id: str,
    title: str,
    task_kind: str,
    world_relation: str,
    authorization_note: str,
    owner_session_id: str,
    source_id: str,
    stamp: Mapping[str, str],
) -> dict[str, Any]:
    if task_kind not in {"optimize", "feasibility", "proof", "compare"}:
        raise ContractError("invalid_argument", "task_kind is invalid")
    if world_relation not in {"single", "joint", "exclusive_scenarios", "shared_first_stage", "independent_batch"}:
        raise ContractError("invalid_argument", "world_relation is invalid")
    return {
        "schema_version": 2,
        "created_at": utc_now(),
        "created_by": dict(stamp),
        "case_id": case_id,
        "title": title,
        "artifact_dir": case_artifact_dir(title),
        "owner_session_id": owner_session_id,
        "state_revision": 1,
        "source_revision": 1,
        "candidate_revision": 0,
        "promotion_generation": 0,
        "task_kind": task_kind,
        "world_relation": world_relation,
        "authorization_note": authorization_note,
        "source_ids": [source_id],
        "facts": [],
        "candidate_id": None,
        "data_ids": [],
        "question_ids": [],
        "review_ids": [],
        "proof_ids": [],
        "prepared_ids": [],
        "run_ids": [],
        "selected_delivery_id": None,
        "current_records": {"questions": {}, "reviews": {}, "runs": {}, "presentation": None},
        "pending_inputs": [],
        "decisions": [],
    }


def create_case_from_capture(
    store: CaseStore,
    *,
    title: str,
    task_kind: str,
    world_relation: str,
    authorization_note: str,
    capture_transfer: Mapping[str, Any],
    stamp: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    capture, raw = validate_capture_transfer(capture_transfer, expected_session_id=stamp["session_id"])
    if not raw.strip():
        raise ContractError("invalid_argument", "empty capture cannot create a business case")
    if capture["channel"] == "steer" and capture["admission"] != "accepted_steer":
        raise ContractError("capture_unconfirmed", "only accepted steer can create a case")
    case_id = new_id()
    source, source_ref = source_from_capture(
        store,
        case_id=case_id,
        purpose="requirement",
        capture_transfer=capture,
        stamp=stamp,
    )
    state = initial_case_state(
        case_id=case_id,
        title=title,
        task_kind=task_kind,
        world_relation=world_relation,
        authorization_note=authorization_note,
        owner_session_id=stamp["session_id"],
        source_id=source["source_id"],
        stamp=stamp,
    )
    state = store.create_case_state(state, existing_source_id=source["source_id"])
    return state, {"source": source, "record_ref": source_ref}
