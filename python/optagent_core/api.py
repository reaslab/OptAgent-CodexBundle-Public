"""Fixed stdin/stdout bridge for the five OptAgent tool groups."""

from __future__ import annotations

import copy
import contextlib
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

if __package__ in {None, ""}:
    # `python -I path/to/api.py` excludes PYTHONPATH. Add only this fixed package root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    # Keep one canonical module identity for CLI and private package imports.
    from optagent_core.api import main as _main

    raise SystemExit(_main())

from optagent_core.contracts import (  # noqa: E402
    CONTRACT_VERSION,
    TOOL_NAMES,
    ContractError,
    canonical_json_bytes,
    load_tool_schemas,
    public_failure,
    public_success,
    require_hash,
    require_int,
    require_id,
    require_object,
    require_string,
    require_stamp,
    sha256_bytes,
    strict_json_loads,
    validate_schema,
    validate_ref_shape,
    workspace_path_identity,
)
from optagent_core.data import load_data_bundle, normalize_data  # noqa: E402
from optagent_core.delivery import (  # noqa: E402
    _check_pending_and_current_inputs,
    _check_delivery_binding,
    _check_delivery_authorities,
    _load_delivery,
    _load_presentation,
    audit_report,
    build_report,
    package_delivery,
    select_delivery,
    state_summary,
    object_summary,
)
from optagent_core.review import add_note, begin_review, submit_review  # noqa: E402
from optagent_core.sources import (  # noqa: E402
    capture_file_source,
    capture_summary,
    create_case_from_capture,
    data_source_records,
    new_id,
    source_from_capture,
    source_projection,
    reusable_file_source,
    utc_now,
    validate_capture_transfer,
)
from optagent_core.store import CaseStore  # noqa: E402


_MAX_TOOL_ARGUMENT_BYTES = 65_536
_MAX_TOOL_ARGUMENT_CONTAINER_DEPTH = 32
_ROUTE_INPUT_UNSET = object()


class _RouteEvidenceChanged(Exception):
    """A lock-free route witness changed before its final short-lock check."""


def _route_dependency_projection(state: Mapping[str, Any]) -> bytes:
    route_decisions = [
        decision
        for decision in state.get("decisions", [])
        if isinstance(decision, Mapping)
        and decision.get("kind") in {"handoff", "source_change", "input_classified"}
    ]
    return canonical_json_bytes({
        "case_id": state.get("case_id"),
        "owner_session_id": state.get("owner_session_id"),
        "state_revision": state.get("state_revision"),
        "source_revision": state.get("source_revision"),
        "promotion_generation": state.get("promotion_generation"),
        "source_ids": state.get("source_ids"),
        "pending_inputs": state.get("pending_inputs"),
        "route_decisions": route_decisions,
    })


def _route_predicate_projection(route: Mapping[str, Any]) -> bytes:
    member = route.get("member")
    projected_member = None if member is None else {
        key: member.get(key)
        for key in ("session_id", "case_id", "registered_by", "input_mode", "covered_seq", "gap_ref")
    }
    guards = []
    for guard in route.get("guards", []):
        ack = guard.get("ack")
        guards.append({
            "gap_ref": guard.get("gap_ref"),
            "unresolved": guard.get("unresolved"),
            "ack": None if ack is None else {
                "ref": ack.get("ref"),
                "capture_id": ack.get("capture_id"),
                "session_id": ack.get("session_id"),
                "arrival_seq": ack.get("arrival_seq"),
                "source_refs": ack.get("source_refs"),
            },
        })
    return canonical_json_bytes({
        "unknown": route.get("unknown"),
        "reason": route.get("reason"),
        "member": projected_member,
        "guards": guards,
        "control_pending": route.get("control_pending"),
        "control_error": route.get("control_error"),
    })


class _SessionRouteFence:
    def __init__(
        self,
        store: CaseStore,
        state: Mapping[str, Any],
        route: Mapping[str, Any],
        index_snapshot: Any,
        queue_snapshot: Any,
        *,
        extra_queue_snapshots: Mapping[str, Any] | None = None,
        exact_ref_snapshots: Mapping[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.case_id = state["case_id"]
        self.session_id = state["owner_session_id"]
        self.dependencies = _route_dependency_projection(state)
        self.route = dict(route)
        self.predicate = _route_predicate_projection(route)
        self.index_snapshot = index_snapshot
        self.queue_snapshot = queue_snapshot
        self.extra_queue_snapshots = dict(extra_queue_snapshots or {})
        self.exact_ref_snapshots = dict(exact_ref_snapshots or {})
        self.extra_control_status = {
            session_id: _pending_control_status(
                store,
                session_id,
                self.case_id,
                queue_value=_snapshot_json(snapshot, max_bytes=64 * 1024 * 1024),
            )
            for session_id, snapshot in self.extra_queue_snapshots.items()
        }

    def close(self) -> None:
        self.index_snapshot.close()
        self.queue_snapshot.close()
        for snapshot in self.extra_queue_snapshots.values():
            snapshot.close()
        for snapshot in self.exact_ref_snapshots.values():
            snapshot.close()

    def __enter__(self) -> "_SessionRouteFence":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def external_unchanged(self) -> bool:
        return (
            self.index_snapshot.unchanged()
            and self.queue_snapshot.unchanged()
            and all(snapshot.unchanged() for snapshot in self.extra_queue_snapshots.values())
            and all(snapshot.unchanged() for snapshot in self.exact_ref_snapshots.values())
        )

    def handoff_control_status(self, session_id: str) -> tuple[bool, str | None]:
        if session_id == self.session_id:
            if self.route.get("member") is None:
                raise ContractError(
                    "capture_unavailable",
                    "handoff owner queue status was not validated for a registered route member",
                )
            try:
                pending = self.route["control_pending"]
                error = self.route["control_error"]
            except KeyError as exc:
                raise ContractError(
                    "capture_unavailable",
                    "handoff owner queue status is missing from the current route fence",
                ) from exc
            if type(pending) is not bool or (error is not None and not isinstance(error, str)):
                raise ContractError(
                    "capture_unavailable",
                    "handoff owner queue status is invalid in the current route fence",
                )
            return pending, error
        try:
            return self.extra_control_status[session_id]
        except KeyError as exc:
            raise ContractError(
                "capture_unavailable",
                "handoff queue status was not captured by the current route fence",
            ) from exc

    def validate_current(
        self,
        current: Mapping[str, Any],
        stamp: Mapping[str, Any],
        *,
        require_no_pending: bool,
        allow_unknown_route: bool = False,
        require_owner: bool = True,
    ) -> None:
        if require_owner:
            _require_owner(current, stamp)
        if require_no_pending and current.get("pending_inputs"):
            raise ContractError("input_unclassified", "case has pending session input")
        if current["case_id"] != self.case_id or current.get("owner_session_id") != self.session_id:
            raise _RouteEvidenceChanged
        if _route_dependency_projection(current) != self.dependencies:
            raise ContractError("stale_revision", "route-relevant Case identity changed before publication")
        if not self.external_unchanged():
            raise _RouteEvidenceChanged
        if self.route.get("unknown") and not allow_unknown_route:
            raise ContractError(
                "capture_unavailable",
                "current case input freshness is unknown; classify or adopt real inputs before relying on current claims",
                details={"reason": self.route.get("reason")},
            )
        if self.route.get("member") is not None:
            barrier_path = f".reaslab/optagent/input-fences/{hashlib.sha256(self.session_id.encode('utf-8')).hexdigest()}.json"
            try:
                _load_barrier(self.store, barrier_path, self.session_id)
            except ContractError as exc:
                raise _RouteEvidenceChanged from exc


class _LocalSessionRouteFence:
    """Short Case/session CAS fence for the standalone bundle."""

    def __init__(self, store: CaseStore, state: Mapping[str, Any]) -> None:
        self.store = store
        self.case_id = state["case_id"]
        self.session_id = state["owner_session_id"]
        self.dependencies = _route_dependency_projection(state)
        self.route = {"unknown": False, "reason": None, "member": None, "control_pending": False, "control_error": None}
        self.predicate = canonical_json_bytes({"local_mode": True, "case_id": self.case_id, "session_id": self.session_id})

    def close(self) -> None:
        return None

    def external_unchanged(self) -> bool:
        return True

    def handoff_control_status(self, session_id: str) -> tuple[bool, str | None]:
        if session_id == self.session_id:
            return False, None
        raise ContractError("capture_unavailable", "local bundle has no cross-session host control queue")

    def validate_current(
        self,
        current: Mapping[str, Any],
        stamp: Mapping[str, Any],
        *,
        require_no_pending: bool,
        allow_unknown_route: bool = False,
        require_owner: bool = True,
    ) -> None:
        if require_owner:
            _require_owner(current, stamp)
        if require_no_pending and current.get("pending_inputs"):
            raise ContractError("input_unclassified", "case has pending session input")
        if current.get("case_id") != self.case_id or current.get("owner_session_id") != self.session_id:
            raise ContractError("stale_revision", "local Case owner changed before publication")
        if _route_dependency_projection(current) != self.dependencies:
            raise ContractError("stale_revision", "local Case route-relevant identity changed before publication")


def _snapshot_json(snapshot: Any, *, max_bytes: int) -> Any:
    if snapshot.missing:
        return None
    raw = snapshot.raw
    if not isinstance(raw, bytes) or len(raw) > max_bytes:
        return {"invalid_snapshot": True}
    try:
        return strict_json_loads(raw, max_bytes=max_bytes)
    except (ValueError, UnicodeError):
        return {"invalid_snapshot": True}


def _prepare_session_route_fence(
    store: CaseStore,
    state: Mapping[str, Any],
    *,
    extra_queue_sessions: Sequence[str] = (),
    exact_refs: Sequence[Mapping[str, Any]] = (),
) -> _SessionRouteFence:
    if getattr(store, "local_mode", False):
        return _LocalSessionRouteFence(store, state)  # type: ignore[return-value]
    session_id = state["owner_session_id"]
    session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    index_snapshot = store.stable_path_snapshot(
        f".reaslab/optagent/host/session_bindings/{session_digest}.json",
        max_bytes=8 * 1024 * 1024,
    )
    try:
        queue_snapshot = store.stable_path_snapshot(
            f".reaslab/optagent/host/pending_control/{session_digest}.json",
            max_bytes=64 * 1024 * 1024,
        )
    except BaseException:
        index_snapshot.close()
        raise
    try:
        route = _route_freshness(
            store,
            state,
            route_index=_snapshot_json(index_snapshot, max_bytes=8 * 1024 * 1024),
            control_queue=_snapshot_json(queue_snapshot, max_bytes=64 * 1024 * 1024),
        )
    except BaseException:
        index_snapshot.close()
        queue_snapshot.close()
        raise
    extra_snapshots: dict[str, Any] = {}
    exact_ref_snapshots: dict[str, Any] = {}
    try:
        for extra_session_id in sorted(set(extra_queue_sessions) - {session_id}):
            extra_digest = hashlib.sha256(extra_session_id.encode("utf-8")).hexdigest()
            extra_snapshots[extra_session_id] = store.stable_path_snapshot(
                f".reaslab/optagent/host/pending_control/{extra_digest}.json",
                max_bytes=64 * 1024 * 1024,
            )
        for raw_ref in exact_refs:
            ref = validate_ref_shape(dict(raw_ref), pointer="/route_fence/exact_refs")
            identity = canonical_json_bytes(ref).decode("utf-8")
            if identity in exact_ref_snapshots:
                continue
            snapshot = store.stable_path_snapshot(ref["path"], max_bytes=ref["byte_len"])
            if (
                snapshot.missing
                or not isinstance(snapshot.raw, bytes)
                or len(snapshot.raw) != ref["byte_len"]
                or hashlib.sha256(snapshot.raw).hexdigest() != ref["sha256"]
            ):
                snapshot.close()
                raise ContractError("artifact_changed", "exact Ref bytes changed before the final session gate")
            exact_ref_snapshots[identity] = snapshot
    except BaseException:
        index_snapshot.close()
        queue_snapshot.close()
        for snapshot in extra_snapshots.values():
            snapshot.close()
        for snapshot in exact_ref_snapshots.values():
            snapshot.close()
        raise
    return _SessionRouteFence(
        store,
        state,
        route,
        index_snapshot,
        queue_snapshot,
        extra_queue_snapshots=extra_snapshots,
        exact_ref_snapshots=exact_ref_snapshots,
    )


def _with_session_route_commit(
    store: CaseStore,
    state: Mapping[str, Any],
    stamp: Mapping[str, Any],
    commit: Callable[[_SessionRouteFence], Any],
    *,
    require_no_pending: bool = True,
    allow_unknown_route: bool = False,
    expected_route_predicate: bytes | None = None,
    extra_session_ids: Sequence[str] = (),
    exact_refs: Sequence[Mapping[str, Any]] = (),
) -> Any:
    """Commit once under session→family/case locks, refreshing evidence at most once."""
    case_id = state["case_id"]
    baseline_dependencies = _route_dependency_projection(state)
    fence = _prepare_session_route_fence(
        store,
        state,
        extra_queue_sessions=extra_session_ids,
        exact_refs=exact_refs,
    )
    baseline_predicate = fence.predicate
    if expected_route_predicate is not None and baseline_predicate != expected_route_predicate:
        fence.close()
        raise ContractError("capture_unavailable", "target Case route predicate changed before publication")
    try:
        for attempt in range(2):
            try:
                session_ids = set(extra_session_ids) | {stamp["session_id"], fence.session_id}
                with contextlib.ExitStack() as locks:
                    for selected_session in sorted(
                        session_ids,
                        key=lambda selected: hashlib.sha256(selected.encode("utf-8")).hexdigest(),
                    ):
                        digest = hashlib.sha256(selected_session.encode("utf-8")).hexdigest()
                        locks.enter_context(store.session_lock(digest))
                    if not fence.external_unchanged():
                        raise _RouteEvidenceChanged
                    return commit(fence)
            except _RouteEvidenceChanged:
                if attempt:
                    raise ContractError("capture_unavailable", "session route evidence changed during publication")
                fence.close()
                current = store.read_case(case_id)
                if _route_dependency_projection(current) != baseline_dependencies:
                    raise ContractError("stale_revision", "route-relevant Case identity changed before retry")
                fence = _prepare_session_route_fence(
                    store,
                    current,
                    extra_queue_sessions=extra_session_ids,
                    exact_refs=exact_refs,
                )
                if fence.predicate != baseline_predicate:
                    raise ContractError("capture_unavailable", "target Case route predicate changed before retry")
                if fence.route.get("unknown"):
                    if not allow_unknown_route:
                        raise ContractError(
                            "capture_unavailable",
                            "current case input freshness is unknown; classify or adopt real inputs before relying on current claims",
                            details={"reason": fence.route.get("reason")},
                        )
        raise ContractError("capture_unavailable", "session route evidence changed during publication")
    finally:
        fence.close()


def _with_session_route_read(
    store: CaseStore,
    state: Mapping[str, Any],
    stamp: Mapping[str, Any],
    read: Callable[[Mapping[str, Any]], Any],
    *,
    exact_refs: Sequence[Mapping[str, Any]] = (),
) -> Any:
    def commit(fence: _SessionRouteFence) -> Any:
        with store.case_lock(state["case_id"]):
            current = store.read_case(state["case_id"])
            if current.get("state_revision") != state.get("state_revision"):
                raise ContractError("stale_revision", "case revision changed before the read completed")
            fence.validate_current(current, stamp, require_no_pending=False)
            return read(current)

    return _with_session_route_commit(
        store,
        state,
        stamp,
        commit,
        require_no_pending=False,
        exact_refs=exact_refs,
    )


def main() -> int:
    """Read one request, write one JSON response, and keep all diagnostics on stderr."""
    raw = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
    try:
        if len(raw) > 8 * 1024 * 1024:
            raise ContractError("invalid_argument", "bridge request is too large")
        envelope = strict_json_loads(raw, max_bytes=8 * 1024 * 1024)
        response = dispatch_envelope(envelope)
    except ContractError as exc:
        _stderr(f"core request rejected: {exc.code}")
        response = _protocol_error_reply(exc)
    except (ValueError, UnicodeError) as exc:
        _stderr(f"core request rejected: {type(exc).__name__}")
        response = _protocol_error_reply(ContractError("bridge_protocol_error", "core request is not one strict JSON value"))
    except Exception:
        _stderr("core request failed unexpectedly")
        _stderr(f"unexpected exception type: {type(sys.exc_info()[1]).__name__}")
        response = _protocol_error_reply(ContractError("bridge_protocol_error", "core request could not be completed", effect="unknown"))
    sys.stdout.buffer.write(canonical_json_bytes(response) + b"\n")
    sys.stdout.buffer.flush()
    return 0


def dispatch_envelope(value: Any) -> dict[str, Any]:
    envelope = require_object(value, pointer="/")
    kind = envelope.get("kind")
    version = envelope.get("contract_version")
    if type(version) is not int or version != CONTRACT_VERSION:
        raise ContractError("bridge_protocol_error", "unsupported core contract version")
    if kind == "dynamic_tool":
        if set(envelope) != {"contract_version", "kind", "context", "tool", "arguments"}:
            raise ContractError("bridge_protocol_error", "dynamic tool envelope has invalid fields")
        context = _dynamic_context(envelope["context"])
        tool = envelope["tool"]
        arguments = require_object(envelope["arguments"], pointer="/arguments")
        return dispatch_dynamic(tool, arguments, context)
    if kind == "run_bridge":
        if set(envelope) != {"contract_version", "kind", "context", "phase"}:
            raise ContractError("bridge_protocol_error", "run bridge envelope has invalid fields")
        context = _dynamic_context(envelope["context"])
        phase = require_object(envelope["phase"], pointer="/phase")
        return _dispatch_run_bridge(phase, context)
    if kind == "control_event":
        if set(envelope) != {"contract_version", "kind", "context", "event"}:
            raise ContractError("bridge_protocol_error", "control event envelope has invalid fields")
        context = _control_context(envelope["context"])
        return dispatch_control_event(envelope["event"], context)
    raise ContractError("bridge_protocol_error", "unknown core entry kind")


def _dynamic_context(value: Any) -> dict[str, Any]:
    obj = require_object(value, pointer="/context")
    required_fields = {"stamp", "workspace_absolute", "cancellation_observed", "captures"}
    optional_fields = {"question_turn_context", "default_solver_backend", "local_mode", "local_imports"}
    if not required_fields.issubset(obj) or set(obj) - required_fields - optional_fields:
        raise ContractError("bridge_protocol_error", "trusted context has invalid fields")
    stamp = require_stamp(obj["stamp"])
    workspace = obj["workspace_absolute"]
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise ContractError("bridge_protocol_error", "trusted workspace path is invalid")
    if not isinstance(obj["cancellation_observed"], bool) or not isinstance(obj["captures"], list):
        raise ContractError("bridge_protocol_error", "trusted context values are invalid")
    captures = []
    for index, item in enumerate(obj["captures"]):
        transfer, _ = validate_capture_transfer(item, expected_session_id=stamp["session_id"])
        captures.append(transfer)
    raw_local_imports = obj.get("local_imports", [])
    if not isinstance(raw_local_imports, list):
        raise ContractError("bridge_protocol_error", "local import references must be an array")
    local_imports = []
    seen_local_capture_ids: set[str] = set()
    for index, value in enumerate(raw_local_imports):
        item = require_object(value, pointer=f"/context/local_imports/{index}")
        if set(item) != {"capture_id", "workspace_path", "sha256", "byte_len", "provenance", "host_capture_authenticated"}:
            raise ContractError("bridge_protocol_error", "local import reference has invalid fields")
        capture_id = require_id(item["capture_id"], pointer=f"/context/local_imports/{index}/capture_id")
        path = require_string(item["workspace_path"], pointer=f"/context/local_imports/{index}/workspace_path")
        parts = path.replace("\\", "/").split("/")
        if path.startswith("/") or any(part in {"", ".", ".."} for part in parts):
            raise ContractError("path_denied", "local import path is not workspace relative")
        digest = require_hash(item["sha256"], pointer=f"/context/local_imports/{index}/sha256")
        byte_len = require_int(item["byte_len"], pointer=f"/context/local_imports/{index}/byte_len")
        if (
            capture_id in seen_local_capture_ids
            or item["provenance"] != "workspace_file"
            or item["host_capture_authenticated"] is not False
        ):
            raise ContractError("bridge_protocol_error", "local import provenance is invalid or repeated")
        seen_local_capture_ids.add(capture_id)
        local_imports.append({
            "capture_id": capture_id,
            "workspace_path": path,
            "sha256": digest,
            "byte_len": byte_len,
            "provenance": "workspace_file",
            "host_capture_authenticated": False,
        })
    question_turn_context = _validate_question_turn_context(
        obj.get("question_turn_context"), pointer="/context/question_turn_context"
    )
    default_solver_backend = obj.get("default_solver_backend")
    if default_solver_backend is not None and (
        not isinstance(default_solver_backend, str) or not default_solver_backend
    ):
        raise ContractError("bridge_protocol_error", "trusted default solver context is invalid", pointer="/context/default_solver_backend")
    local_mode = obj.get("local_mode", False)
    if not isinstance(local_mode, bool):
        raise ContractError("bridge_protocol_error", "local mode marker is invalid", pointer="/context/local_mode")
    if local_imports and local_mode is not True:
        raise ContractError("bridge_protocol_error", "workspace file imports require explicit local mode")
    return {
        "stamp": stamp,
        "workspace_absolute": workspace,
        "cancellation_observed": obj["cancellation_observed"],
        "captures": captures,
        "local_imports": local_imports,
        "question_turn_context": question_turn_context,
        "default_solver_backend": default_solver_backend,
        "local_mode": local_mode,
    }


def _validate_question_turn_context(value: Any, *, pointer: str) -> dict[str, Any] | None:
    if value is None:
        return None
    obj = require_object(value, pointer=pointer)
    if set(obj) != {"assistant_family_omitted", "unavailable_turn_ids", "messages"}:
        raise ContractError("bridge_protocol_error", "retained Question context has invalid fields", pointer=pointer)
    if not isinstance(obj["assistant_family_omitted"], bool):
        raise ContractError("bridge_protocol_error", "retained Question omission marker is invalid", pointer=f"{pointer}/assistant_family_omitted")
    unavailable = obj["unavailable_turn_ids"]
    messages = obj["messages"]
    if not isinstance(unavailable, list) or not isinstance(messages, list):
        raise ContractError("bridge_protocol_error", "retained Question context lists are invalid", pointer=pointer)
    unavailable_turn_ids: list[str] = []
    seen_turn_ids: set[str] = set()
    for index, raw_turn_id in enumerate(unavailable):
        turn_id = require_string(raw_turn_id, pointer=f"{pointer}/unavailable_turn_ids/{index}")
        if turn_id in seen_turn_ids:
            raise ContractError("bridge_protocol_error", "retained Question context repeats an unavailable turn", pointer=f"{pointer}/unavailable_turn_ids/{index}")
        seen_turn_ids.add(turn_id)
        unavailable_turn_ids.append(turn_id)
    normalized_messages: list[dict[str, Any]] = []
    for index, raw_message in enumerate(messages):
        item_pointer = f"{pointer}/messages/{index}"
        message = require_object(raw_message, pointer=item_pointer)
        if set(message) != {"source", "phase", "text"}:
            raise ContractError("bridge_protocol_error", "retained Question message has invalid fields", pointer=item_pointer)
        source = require_object(message["source"], pointer=f"{item_pointer}/source")
        if set(source) != {"id", "revision", "complete"}:
            raise ContractError("bridge_protocol_error", "retained Question source has invalid fields", pointer=f"{item_pointer}/source")
        identity = require_object(source["id"], pointer=f"{item_pointer}/source/id")
        if set(identity) != {"message_id", "turn_id", "role"}:
            raise ContractError("bridge_protocol_error", "retained Question source identity is invalid", pointer=f"{item_pointer}/source/id")
        source_turn_id = require_string(identity["turn_id"], pointer=f"{item_pointer}/source/id/turn_id")
        if (
            identity["role"] != "assistant"
            or not isinstance(source["complete"], bool)
            or source["complete"] is not True
        ):
            raise ContractError("bridge_protocol_error", "retained Question source is not a complete assistant item", pointer=f"{item_pointer}/source")
        normalized_source = {
            "id": {
                "message_id": require_string(identity["message_id"], pointer=f"{item_pointer}/source/id/message_id"),
                "turn_id": source_turn_id,
                "role": "assistant",
            },
            "revision": require_string(source["revision"], pointer=f"{item_pointer}/source/revision"),
            "complete": True,
        }
        phase = message["phase"]
        if phase is not None and (not isinstance(phase, str) or phase not in {"commentary", "final_answer"}):
            raise ContractError("bridge_protocol_error", "retained Question phase is invalid", pointer=f"{item_pointer}/phase")
        text = message["text"]
        if not isinstance(text, str):
            raise ContractError("bridge_protocol_error", "retained Question text is invalid", pointer=f"{item_pointer}/text")
        normalized_messages.append({"source": normalized_source, "phase": phase, "text": text})
    return {
        "assistant_family_omitted": obj["assistant_family_omitted"],
        "unavailable_turn_ids": unavailable_turn_ids,
        "messages": normalized_messages,
    }


def _control_context(value: Any) -> dict[str, str]:
    obj = require_object(value, pointer="/context")
    if set(obj) != {"session_id", "workspace_absolute"}:
        raise ContractError("bridge_protocol_error", "control context has invalid fields")
    session_id = obj["session_id"]
    workspace = obj["workspace_absolute"]
    if not isinstance(session_id, str) or not session_id or not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise ContractError("bridge_protocol_error", "control context values are invalid")
    return {"session_id": session_id, "workspace_absolute": workspace}


def _store(context: Mapping[str, Any], *, create: bool = True) -> CaseStore:
    return CaseStore(context["workspace_absolute"], create=create, local_mode=context.get("local_mode") is True)


def _dispatch_run_bridge(phase: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    try:
        from optagent_core.runner import dispatch_run_bridge  # supplied by the run owner
    except ImportError as exc:
        raise ContractError("environment_unavailable", "run bridge implementation is not installed") from exc

    result = dispatch_run_bridge(_store(context), phase, context)
    return _private_reply(result)


def _private_reply(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("kind") not in {"complete", "start_plan", "start_ready", "stop_plan"}:
        raise ContractError("bridge_protocol_error", "run owner returned an invalid private reply")
    expected = {"kind", "reply"} if value["kind"] == "complete" else {"kind", "plan"}
    if set(value) != expected:
        raise ContractError("bridge_protocol_error", "run owner returned an invalid private reply shape")
    if value["kind"] == "complete" and not isinstance(value["reply"], dict):
        raise ContractError("bridge_protocol_error", "complete reply is invalid")
    if value["kind"] != "complete" and not isinstance(value["plan"], dict):
        raise ContractError("bridge_protocol_error", "private plan is invalid")
    return value


def _check_tool_argument_depth(value: Any, parent_depth: int = 0) -> None:
    if isinstance(value, dict):
        depth = parent_depth + 1
        children = value.values()
    elif isinstance(value, list):
        depth = parent_depth + 1
        children = value
    else:
        return
    if depth > _MAX_TOOL_ARGUMENT_CONTAINER_DEPTH:
        raise ContractError(
            "invalid_argument",
            "tool arguments exceed 32-container depth",
            pointer="/arguments",
        )
    for child in children:
        _check_tool_argument_depth(child, depth)


def _validate_dynamic_arguments_capacity(arguments: Any) -> None:
    if not isinstance(arguments, dict):
        raise ContractError("invalid_argument", "tool arguments must be an object", pointer="/arguments")
    _check_tool_argument_depth(arguments)
    try:
        serialized = canonical_json_bytes(arguments)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ContractError("invalid_argument", "tool arguments are not a finite JSON Value", pointer="/arguments") from exc
    if len(serialized) > _MAX_TOOL_ARGUMENT_BYTES:
        raise ContractError(
            "invalid_argument",
            "tool arguments exceed 65536 UTF-8 bytes",
            pointer="/arguments",
        )


def dispatch_dynamic(tool: Any, arguments: dict[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    safe_tool = tool if isinstance(tool, str) and tool in TOOL_NAMES else "optagent.case"
    op = None
    case_id = None
    store: CaseStore | None = None
    request_validated = False
    try:
        _validate_dynamic_arguments_capacity(arguments)
        op = arguments.get("op")
        case_id = arguments.get("case_id") if isinstance(arguments.get("case_id"), str) else None
        if tool not in TOOL_NAMES:
            raise ContractError("unknown_operation", "unknown product tool")
        entry = load_tool_schemas()[tool]
        validate_schema(arguments, entry["input_schema"])
        request_validated = True
        stamp = context["stamp"]
        if (
            tool == "optagent.deliver"
            and op in {"select", "build", "package"}
            and context.get("cancellation_observed") is True
        ):
            _require_root(stamp)
            raise ContractError(
                "execution_unknown",
                "the active tool call was cancelled before delivery mutation",
            )
        read_only = (
            tool == "optagent.case" and op in {"list", "get", "inputs"}
        ) or (tool == "optagent.data" and op == "inspect") or (tool == "optagent.run" and op == "inspect") or (tool == "optagent.deliver" and op == "audit")
        store = _store(context, create=not read_only)
        if tool == "optagent.case":
            result, actual_case_id, revision = _case_operation(store, arguments, context)
        elif tool == "optagent.data":
            result, actual_case_id, revision = _data_operation(store, arguments, context)
        elif tool == "optagent.run":
            if op == "inspect":
                _require_read_role(stamp)
            elif stamp["role"] != "root":
                raise ContractError("permission_denied", "only root can change run state")
            if op in {"prepare", "start"} and case_id is not None:
                run_state = store.read_case(case_id)
                _require_owner(run_state, stamp)
                _require_route_current(store, run_state)
            try:
                from optagent_core.runner import run_tool  # supplied by the run owner
            except ImportError as exc:
                raise ContractError("environment_unavailable", "run operations are not installed") from exc

            private = run_tool(store, arguments, context)
            if op == "inspect" and case_id is not None and private.get("kind") == "complete":
                reply = private.get("reply")
                if isinstance(reply, dict) and reply.get("ok") is True:
                    warnings = _route_warning(store, [case_id])
                    if warnings:
                        reply["warnings"] = [*reply.get("warnings", []), *warnings]
            return _private_reply(private)
        elif tool == "optagent.review":
            result, actual_case_id, revision = _review_operation(store, arguments, context)
        else:
            result, actual_case_id, revision = _delivery_operation(store, arguments, context)
        warning_case_ids: list[str] = []
        if tool == "optagent.case" and op == "get" and actual_case_id is not None:
            warning_case_ids = [actual_case_id]
        elif tool == "optagent.case" and op == "list" and isinstance(result, dict):
            warning_case_ids = [item["case_id"] for item in result.get("cases", []) if isinstance(item, dict) and isinstance(item.get("case_id"), str)]
        elif tool == "optagent.deliver" and op == "audit" and actual_case_id is not None:
            warning_case_ids = [actual_case_id]
        warnings = _route_warning(store, warning_case_ids) if warning_case_ids else []
        return {
            "kind": "complete",
            "reply": public_success(
                tool,
                op if isinstance(op, str) else "unknown",
                case_id=actual_case_id,
                state_revision=revision,
                result=result,
                warnings=warnings,
            ),
        }
    except ContractError as exc:
        current_revision = None
        if case_id and store is not None:
            try:
                current_revision = store.read_case(case_id)["state_revision"]
            except ContractError:
                pass
        return {
            "kind": "complete",
            "reply": public_failure(safe_tool, op if isinstance(op, str) else "unknown", exc, case_id=case_id, state_revision=current_revision),
        }
    except Exception as exc:
        if not request_validated:
            raise
        _stderr("core operation failed unexpectedly")
        _stderr(f"unexpected exception type: {type(exc).__name__}")
        error = ContractError(
            "bridge_protocol_error",
            "core request could not be completed",
            effect="unknown",
        )
        return {
            "kind": "complete",
            "reply": public_failure(
                safe_tool,
                op,
                error,
                case_id=case_id,
                state_revision=None,
            ),
        }


def dispatch_control_event(value: Any, context: Mapping[str, str]) -> dict[str, Any]:
    event = require_object(value, pointer="/event")
    event_id = event.get("event_id")
    try:
        event_id = require_id(event_id, pointer="/event/event_id")
        store = _store(context)
        if event.get("kind") == "capture_arrived":
            affected = _control_capture_arrived(store, event, context)
        elif event.get("kind") == "steer_admission":
            affected = _control_steer_admission(store, event, context)
        elif event.get("kind") == "external_cancel":
            affected = _control_external_cancel(store, event, context)
        else:
            raise ContractError("bridge_protocol_error", "unsupported control event")
        return {"ok": True, "event_id": event_id, "affected_case_ids": affected, "error": None}
    except ContractError as exc:
        safe_id = event_id if isinstance(event_id, str) else "00000000-0000-4000-8000-000000000000"
        return {"ok": False, "event_id": safe_id, "affected_case_ids": [], "error": {"code": exc.code, "message": exc.message}}


def _control_capture_arrived(store: CaseStore, event: Mapping[str, Any], context: Mapping[str, str]) -> list[str]:
    if set(event) != {"event_id", "kind", "capture", "bindings"}:
        raise ContractError("bridge_protocol_error", "capture event has invalid fields")
    capture, raw = validate_capture_transfer(event["capture"], expected_session_id=context["session_id"])
    bindings = _control_bindings(event["bindings"])
    if not bindings:
        return []
    if len(bindings) != 1:
        raise ContractError("bridge_protocol_error", "capture arrival must bind exactly one Case")
    raw_ref = store.capture_source_bytes(raw)
    session_digest = hashlib.sha256(context["session_id"].encode("utf-8")).hexdigest()
    barrier_path = f".reaslab/optagent/input-fences/{session_digest}.json"
    case_id = bindings[0]["case_id"]
    prepared_state: Mapping[str, Any] | None = None
    materialized = False
    for attempt in range(2):
        if prepared_state is None:
            try:
                prepared_state = store.read_case(case_id)
            except ContractError as exc:
                if exc.code == "not_found":
                    prepared_state = None
                else:
                    raise
            if prepared_state is not None and prepared_state["owner_session_id"] == context["session_id"]:
                old = next((item for item in prepared_state["pending_inputs"] if item["capture_id"] == capture["capture_id"]), None)
                materialized = old is None and _capture_was_materialized(
                    store,
                    prepared_state,
                    capture,
                    raw_ref,
                    incoming_raw=raw,
                )
        refresh = False
        result: list[str] = []
        with store.session_lock(session_digest):
            barrier = _load_barrier(store, barrier_path, context["session_id"])
            existing_seq = barrier["latest_observed_seq"]
            if existing_seq and capture["arrival_seq"] == existing_seq and capture["arrival_epoch"] != barrier["arrival_epoch"]:
                raise ContractError("bridge_protocol_error", "same session sequence has a different arrival epoch")
            if capture["arrival_seq"] > existing_seq or existing_seq == 0:
                barrier["arrival_epoch"] = capture["arrival_epoch"]
                barrier["latest_observed_seq"] = capture["arrival_seq"]
            store.write_record(barrier_path, barrier, replace=True)
            try:
                current = store.read_case(case_id)
            except ContractError as exc:
                if exc.code == "not_found":
                    return []
                raise
            if current["owner_session_id"] != context["session_id"]:
                return []
            if prepared_state is None:
                refresh = True
            elif _route_dependency_projection(current) != _route_dependency_projection(prepared_state):
                refresh = True
            else:
                old = next((item for item in current["pending_inputs"] if item["capture_id"] == capture["capture_id"]), None)
                if old is None:
                    if not materialized:
                        fence = {
                            "session_id": capture["session_id"],
                            "capture_id": capture["capture_id"],
                            "arrival_epoch": capture["arrival_epoch"],
                            "arrival_seq": capture["arrival_seq"],
                            "channel": capture["channel"],
                            "admission": capture["admission"],
                            "raw_ref": raw_ref,
                            "disposition": "unclassified",
                            "note": "",
                        }
                        store.cas_case(
                            case_id,
                            current["state_revision"],
                            lambda latest, item=fence: latest["pending_inputs"].append(dict(item)),
                        )
                    result = [case_id]
                elif not _pending_capture_matches(old, capture, raw_ref):
                    raise ContractError("bridge_protocol_error", "capture ID was reused with different bytes")
                else:
                    result = [case_id]
        if not refresh:
            return result
        if attempt:
            raise ContractError("stale_revision", "Case route dependencies changed during capture arrival replay")
        prepared_state = None
    return []


def _capture_was_materialized(
    store: CaseStore,
    state: Mapping[str, Any],
    capture: Mapping[str, Any],
    raw_ref: Mapping[str, Any],
    *,
    incoming_raw: bytes,
) -> bool:
    transfer = _persisted_source_capture(
        store,
        state,
        capture["capture_id"],
        state["owner_session_id"],
        missing_ok=True,
    )
    if transfer is not None:
        parsed, persisted = validate_capture_transfer(transfer, expected_session_id=state["owner_session_id"])
        if (
            parsed["arrival_seq"] != capture["arrival_seq"]
            or parsed["arrival_epoch"] != capture["arrival_epoch"]
            or parsed["channel"] != capture["channel"]
            or parsed["admission"] != capture["admission"]
            or parsed["sha256"] != raw_ref["sha256"]
            or len(persisted) != raw_ref["byte_len"]
            or persisted != incoming_raw
        ):
            raise ContractError("bridge_protocol_error", "capture ID was reused with different source identity")
        return True
    prefix = f"{capture['capture_id']}:"
    marker = f"sha256={raw_ref['sha256']}"
    for decision in state["decisions"]:
        if decision.get("kind") != "input_classified" or not isinstance(decision.get("note"), str):
            continue
        if not decision["note"].startswith(prefix) or marker not in decision["note"]:
            continue
        try:
            classified_by = require_stamp(decision.get("stamp"), pointer="/decision/stamp")
        except ContractError:
            continue
        if classified_by["role"] == "root" and classified_by["session_id"] == state["owner_session_id"]:
            return True
    return False


def _pending_capture_matches(
    fence: Mapping[str, Any], capture: Mapping[str, Any], raw_ref: Mapping[str, Any]
) -> bool:
    return (
        fence.get("session_id") == capture["session_id"]
        and fence.get("raw_ref") == raw_ref
        and fence.get("arrival_seq") == capture["arrival_seq"]
        and fence.get("arrival_epoch") == capture["arrival_epoch"]
        and fence.get("channel") == capture["channel"]
    )


def _control_steer_admission(store: CaseStore, event: Mapping[str, Any], context: Mapping[str, str]) -> list[str]:
    if set(event) != {"event_id", "kind", "capture_id", "arrival_seq", "admission", "bindings"}:
        raise ContractError("bridge_protocol_error", "steer admission event has invalid fields")
    capture_id = require_id(event["capture_id"], pointer="/event/capture_id")
    if not isinstance(event["arrival_seq"], int) or isinstance(event["arrival_seq"], bool) or event["arrival_seq"] < 0:
        raise ContractError("bridge_protocol_error", "steer arrival sequence is invalid")
    if event["admission"] not in {"accepted_steer", "unconfirmed_steer"}:
        raise ContractError("bridge_protocol_error", "steer admission value is invalid")
    bindings = _control_bindings(event["bindings"])
    case_id = bindings[0]["case_id"]
    session_digest = hashlib.sha256(context["session_id"].encode("utf-8")).hexdigest()
    with store.session_lock(session_digest):
        state = store.read_case(case_id)
        if state["owner_session_id"] != context["session_id"]:
            return []
        fence = next((item for item in state["pending_inputs"] if item["capture_id"] == capture_id), None)
        if fence is None or fence.get("session_id") != context["session_id"] or fence["arrival_seq"] != event["arrival_seq"] or fence["channel"] != "steer":
            raise ContractError("capture_unavailable", "pending steer capture is unavailable")
        if fence["admission"] == event["admission"]:
            return [case_id]
        if fence["admission"] != "pending_steer":
            raise ContractError("bridge_protocol_error", "capture admission cannot be changed twice")
        store.cas_case(
            case_id,
            state["state_revision"],
            lambda current: _replace_fence_admission(current, capture_id, event["admission"]),
        )
        return [case_id]


def _control_external_cancel(store: CaseStore, event: Mapping[str, Any], context: Mapping[str, str]) -> list[str]:
    if set(event) != {"event_id", "kind", "cancel", "bindings"}:
        raise ContractError("bridge_protocol_error", "external cancel event has invalid fields")
    cancel = require_object(event["cancel"], pointer="/event/cancel")
    if set(cancel) != {"origin", "session_id", "event_id", "turn_id", "call_id"}:
        raise ContractError("bridge_protocol_error", "cancel evidence has invalid fields")
    if cancel["origin"] != "external_acp" or cancel["session_id"] != context["session_id"] or cancel["event_id"] != event["event_id"] or cancel["turn_id"] is not None or cancel["call_id"] is not None:
        raise ContractError("bridge_protocol_error", "external cancel identity is invalid")
    bindings = _control_bindings(event["bindings"])
    case_id = bindings[0]["case_id"]
    session_digest = hashlib.sha256(context["session_id"].encode("utf-8")).hexdigest()
    with store.session_lock(session_digest):
        state = store.read_case(case_id)
        if state["owner_session_id"] != context["session_id"]:
            return []
        run_id = bindings[0]["run_id"]
        if run_id is not None:
            from optagent_core.runner import _load_run_record

            with store.run_lock(run_id):
                state = store.read_case(case_id)
                if state["owner_session_id"] != context["session_id"]:
                    return []
                run, old_ref = _load_run_record(store, case_id, run_id, state=state)
                if run.get("owner_session_id") != context["session_id"]:
                    return []
                prior = run.get("cancel", {}).get("evidence")
                if prior == cancel:
                    return [case_id]
                if prior is not None:
                    raise ContractError("bridge_protocol_error", "run already has different cancel evidence")
                next_run = copy.deepcopy(run)
                next_run["cancel"]["requested"] = True
                next_run["cancel"]["evidence"] = dict(cancel)
                run_ref = store.write_family_version(case_id, "runs", run_id, next_run)

                def fence_run(current: dict[str, Any]) -> None:
                    records = current.get("current_records")
                    if records is None:
                        records = {"questions": {}, "reviews": {}, "runs": {}, "presentation": None}
                        current["current_records"] = records
                    if records["runs"].get(run_id) != old_ref:
                        raise ContractError("stale_revision", "run changed during external cancellation")
                    if current["owner_session_id"] != context["session_id"]:
                        raise ContractError("permission_denied", "external cancellation is outside the current owner session")
                    records["runs"][run_id] = dict(run_ref)
                    _bump_generation(current)

                store.cas_case(case_id, state["state_revision"], fence_run)
            return [case_id]
        # Fence first. The persisted generation blocks a late run even if no verified stop ID exists.
        store.cas_case(
            case_id,
            state["state_revision"],
            lambda current: _bump_generation(current),
        )
        return [case_id]


def _control_bindings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ContractError("bridge_protocol_error", "control bindings must be an array")
    if len(value) != 1:
        raise ContractError("bridge_protocol_error", "each control event must bind exactly one Case")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        binding = require_object(item, pointer=f"/event/bindings/{index}")
        if set(binding) != {"case_id", "run_id", "pending_start"}:
            raise ContractError("bridge_protocol_error", "control binding has invalid fields")
        case_id = require_id(binding["case_id"], pointer=f"/event/bindings/{index}/case_id")
        run_id = None if binding["run_id"] is None else require_id(binding["run_id"], pointer=f"/event/bindings/{index}/run_id")
        if not isinstance(binding["pending_start"], bool) or case_id in seen:
            raise ContractError("bridge_protocol_error", "control binding is repeated or invalid")
        if run_id is not None and binding["pending_start"]:
            raise ContractError("bridge_protocol_error", "run binding cannot also be pending start")
        seen.add(case_id)
        result.append({"case_id": case_id, "run_id": run_id, "pending_start": binding["pending_start"]})
    return result


def _load_barrier(store: CaseStore, path: str, session_id: str) -> dict[str, Any]:
    try:
        barrier = store.read_record(path)
    except ContractError as exc:
        if exc.code != "not_found":
            raise
        return {
            "session_id": session_id,
            "arrival_epoch": "00000000-0000-4000-8000-000000000000",
            "latest_observed_seq": 0,
        }
    fields = {"session_id", "arrival_epoch", "latest_observed_seq"}
    if set(barrier) != fields or barrier["session_id"] != session_id:
        raise ContractError("capture_unavailable", "session input barrier is invalid")
    try:
        require_id(barrier["arrival_epoch"], pointer="/barrier/arrival_epoch")
        require_int(barrier["latest_observed_seq"], pointer="/barrier/latest_observed_seq")
    except ContractError as exc:
        raise ContractError("capture_unavailable", "session input barrier is invalid") from exc
    return barrier


def _session_route_member(
    store: CaseStore,
    session_id: str,
    case_id: str,
    *,
    index_value: Any = _ROUTE_INPUT_UNSET,
) -> tuple[dict[str, Any] | None, str | None]:
    """Read one already-registered member without scanning or repairing the host index."""
    session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    if index_value is _ROUTE_INPUT_UNSET:
        path = f".reaslab/optagent/host/session_bindings/{session_digest}.json"
        try:
            index = store.read_record(path)
        except ContractError as exc:
            if exc.code == "not_found":
                return None, None
            return None, "route_index_invalid"
    else:
        index = index_value
        if index is None:
            return None, None
    expected_fields = {"schema_version", "session_id", "workspace_absolute", "index_revision", "members", "pending_starts"}
    if (
        set(index) != expected_fields
        or index.get("schema_version") != 1
        or index.get("session_id") != session_id
        or not isinstance(index.get("workspace_absolute"), str)
        or workspace_path_identity(index["workspace_absolute"]) != workspace_path_identity(store.workspace)
        or not isinstance(index.get("members"), list)
        or not isinstance(index.get("pending_starts"), list)
    ):
        return None, "route_index_invalid"
    try:
        require_int(index["index_revision"], pointer="/route_index/index_revision")
    except ContractError:
        return None, "route_index_invalid"
    matches = [item for item in index["members"] if isinstance(item, dict) and item.get("case_id") == case_id]
    if not matches:
        return None, "route_member_missing"
    if len(matches) != 1:
        return None, "route_index_invalid"
    member = matches[0]
    if set(member) != {"case_id", "registered_by", "input_mode", "covered_seq", "gap_ref"}:
        return None, "route_index_invalid"
    try:
        require_id(member["case_id"], pointer="/route_index/members/case_id")
        registered_by = require_stamp(member["registered_by"], pointer="/route_index/members/registered_by")
        covered_seq = require_int(member["covered_seq"], pointer="/route_index/members/covered_seq")
        if registered_by["role"] != "root" or registered_by["session_id"] != session_id:
            return None, "route_index_invalid"
        if member["input_mode"] not in {"working", "foreground", "dormant"}:
            return None, "route_index_invalid"
        gap_ref = None if member["gap_ref"] is None else validate_ref_shape(member["gap_ref"], pointer="/route_index/members/gap_ref")
    except (ContractError, KeyError):
        return None, "route_index_invalid"
    return {**member, "session_id": session_id, "registered_by": registered_by, "covered_seq": covered_seq, "gap_ref": gap_ref}, None


def _read_route_audit(
    store: CaseStore,
    ref_value: Mapping[str, Any],
    *,
    case_id: str,
    member: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ref = validate_ref_shape(ref_value, pointer="/gap_ref")
    prefix = ".reaslab/optagent/host/route_audits/"
    expected_suffix = f"{ref['sha256']}.json"
    if ref["path"] != prefix + expected_suffix or ref["media_type"] != "application/json":
        raise ContractError("capture_unavailable", "route gap reference is invalid")
    raw = store.read_ref(ref)
    if len(raw) > 64 * 1024:
        raise ContractError("capture_unavailable", "route gap audit exceeds its size limit")
    try:
        audit = strict_json_loads(raw, max_bytes=64 * 1024)
    except (ValueError, UnicodeError) as exc:
        raise ContractError("capture_unavailable", "route gap audit is unreadable") from exc
    fields = {"schema_version", "case_id", "session_id", "covered_seq", "missing_seq_ranges", "registered_by", "observed_by"}
    if not isinstance(audit, dict) or set(audit) != fields or audit.get("schema_version") != 1 or audit.get("case_id") != case_id:
        raise ContractError("capture_unavailable", "route gap audit identity is invalid")
    try:
        audit_session = require_string(audit["session_id"], pointer="/gap_audit/session_id")
        covered = require_int(audit["covered_seq"], pointer="/gap_audit/covered_seq")
        registered_by = require_stamp(audit["registered_by"], pointer="/gap_audit/registered_by")
        observed_by = require_stamp(audit["observed_by"], pointer="/gap_audit/observed_by")
        ranges = audit["missing_seq_ranges"]
        if not isinstance(ranges, list) or not ranges:
            raise ContractError("capture_unavailable", "route gap audit has no missing sequence")
        normalized_ranges: list[dict[str, int]] = []
        previous_end = covered
        for index, interval in enumerate(ranges):
            if not isinstance(interval, dict) or set(interval) != {"start", "end"}:
                raise ContractError("capture_unavailable", "route gap sequence range is invalid")
            start = require_int(interval["start"], pointer=f"/gap_audit/missing_seq_ranges/{index}/start", minimum=1)
            end = require_int(interval["end"], pointer=f"/gap_audit/missing_seq_ranges/{index}/end", minimum=1)
            if start <= previous_end or end < start:
                raise ContractError("capture_unavailable", "route gap sequence ranges overlap or are unordered")
            normalized_ranges.append({"start": start, "end": end})
            previous_end = end
    except (ContractError, KeyError) as exc:
        raise ContractError("capture_unavailable", "route gap audit fields are invalid") from exc
    if (
        registered_by["role"] != "root"
        or observed_by["role"] != "root"
        or registered_by["session_id"] != audit_session
    ):
        raise ContractError("capture_unavailable", "route gap stamps do not match their session")
    if member is not None and member.get("session_id") == audit_session:
        if member.get("registered_by") != registered_by or member.get("covered_seq") != covered:
            raise ContractError("capture_unavailable", "route gap does not match its registered member")
    return {
        **audit,
        "session_id": audit_session,
        "covered_seq": covered,
        "missing_seq_ranges": normalized_ranges,
        "registered_by": registered_by,
        "observed_by": observed_by,
        "ref": ref,
    }


def _source_adoption_ack(
    store: CaseStore,
    state: Mapping[str, Any],
    gap_ref: Mapping[str, Any],
) -> dict[str, Any] | None:
    case_id = state["case_id"]
    prefix = f".reaslab/optagent/cases/{case_id}/attachments/source_adoption/"
    for decision in reversed(state.get("decisions", [])):
        if not isinstance(decision, dict) or decision.get("kind") != "source_change":
            continue
        refs = decision.get("refs")
        if not isinstance(refs, list):
            continue
        normalized_refs: list[dict[str, Any]] = []
        try:
            normalized_refs = [validate_ref_shape(item, pointer="/decision/refs") for item in refs]
        except ContractError:
            continue
        for adoption_ref in normalized_refs:
            if not adoption_ref["path"].startswith(prefix):
                continue
            if (
                adoption_ref["path"] != prefix + adoption_ref["sha256"] + ".json"
                or adoption_ref["media_type"] != "application/json"
            ):
                continue
            try:
                raw = store.read_ref(adoption_ref)
                if len(raw) > 64 * 1024:
                    continue
                ack = strict_json_loads(raw, max_bytes=64 * 1024)
            except (ContractError, ValueError, UnicodeError):
                continue
            required = {
                "schema_version", "case_id", "gap_ref", "capture_id", "session_id", "arrival_seq",
                "source_refs", "stamp", "source_revision", "promotion_generation",
            }
            if not isinstance(ack, dict) or set(ack) != required or ack.get("schema_version") != 1 or ack.get("case_id") != case_id:
                continue
            try:
                if validate_ref_shape(ack["gap_ref"], pointer="/source_adoption/gap_ref") != dict(gap_ref):
                    continue
                capture_id = require_id(ack["capture_id"], pointer="/source_adoption/capture_id")
                session_id = require_string(ack["session_id"], pointer="/source_adoption/session_id")
                arrival_seq = require_int(ack["arrival_seq"], pointer="/source_adoption/arrival_seq", minimum=1)
                source_revision = require_int(ack["source_revision"], pointer="/source_adoption/source_revision", minimum=1)
                promotion_generation = require_int(ack["promotion_generation"], pointer="/source_adoption/promotion_generation")
                stamp = require_stamp(ack["stamp"], pointer="/source_adoption/stamp")
                source_refs_value = ack["source_refs"]
                if not isinstance(source_refs_value, list) or not source_refs_value:
                    continue
                source_refs = [validate_ref_shape(item, pointer="/source_adoption/source_refs") for item in source_refs_value]
                decision_stamp = require_stamp(decision["stamp"], pointer="/decision/stamp")
            except (ContractError, KeyError):
                continue
            if (
                stamp != decision_stamp
                or stamp["role"] != "root"
                or stamp["session_id"] != session_id
                or source_revision > state["source_revision"]
                or promotion_generation > state["promotion_generation"]
                or adoption_ref not in normalized_refs
                or dict(gap_ref) not in normalized_refs
            ):
                continue
            source_prefix = f".reaslab/optagent/cases/{case_id}/sources/"
            record_refs: list[dict[str, Any]] = []
            byte_refs: list[dict[str, Any]] = []
            refs_have_canonical_paths = True
            for source_ref in source_refs:
                ref_path = source_ref["path"]
                if ref_path.startswith(source_prefix):
                    source_id_text = ref_path[len(source_prefix) :]
                    try:
                        source_id = require_id(source_id_text.removesuffix(".json"), pointer="/source_adoption/source_refs")
                    except ContractError:
                        refs_have_canonical_paths = False
                        break
                    if source_id_text != source_id + ".json" or source_ref["media_type"] != "application/json":
                        refs_have_canonical_paths = False
                        break
                    record_refs.append(source_ref)
                    continue
                bytes_prefix = ".reaslab/optagent/source-bytes/"
                if ref_path.startswith(bytes_prefix):
                    digest = ref_path[len(bytes_prefix) :]
                    try:
                        require_hash(digest, pointer="/source_adoption/source_refs")
                    except ContractError:
                        refs_have_canonical_paths = False
                        break
                    if source_ref["media_type"] != "application/octet-stream":
                        refs_have_canonical_paths = False
                        break
                    byte_refs.append(source_ref)
                    continue
                refs_have_canonical_paths = False
                break
            if not refs_have_canonical_paths or len(record_refs) + len(byte_refs) != len(source_refs) or not record_refs or len({r["path"] for r in source_refs}) != len(source_refs):
                continue
            if any(ref not in normalized_refs for ref in source_refs):
                continue
            try:
                by_path = {ref["path"]: ref for ref in source_refs}
                adopted = False
                for record_ref in record_refs:
                    source = strict_json_loads(store.read_ref(record_ref))
                    if not isinstance(source, dict) or source.get("source_id") != Path(record_ref["path"]).stem:
                        raise ContractError("capture_unavailable", "source adoption record identity is invalid")
                    bytes_ref = validate_ref_shape(source.get("bytes"), pointer="/source/bytes")
                    if bytes_ref["path"] not in by_path or by_path[bytes_ref["path"]] != bytes_ref:
                        raise ContractError("capture_unavailable", "source adoption bytes are not acknowledged")
                    raw_source = store.read_ref(bytes_ref)
                    if source.get("original_sha256") != bytes_ref["sha256"] or source.get("original_size") != len(raw_source):
                        raise ContractError("capture_unavailable", "source adoption bytes do not match the record")
                    origin = source.get("origin", {})
                    created_by = require_stamp(source.get("created_by"), pointer="/source/created_by")
                    if (
                        origin.get("capture_id") == capture_id
                        and origin.get("arrival_seq") == arrival_seq
                        and created_by == stamp
                        and created_by["session_id"] == session_id
                        and bool(raw_source)
                        and origin.get("transport_admission") in {"received_prompt", "accepted_steer"}
                    ):
                        adopted = True
                if not adopted:
                    continue
            except (ContractError, ValueError, UnicodeError):
                continue
            return {
                **ack,
                "capture_id": capture_id,
                "session_id": session_id,
                "arrival_seq": arrival_seq,
                "source_refs": source_refs,
                "stamp": stamp,
                "source_revision": source_revision,
                "promotion_generation": promotion_generation,
                "ref": adoption_ref,
            }
    return None


def _route_freshness(
    store: CaseStore,
    state: Mapping[str, Any],
    *,
    route_index: Any = _ROUTE_INPUT_UNSET,
    control_queue: Any = _ROUTE_INPUT_UNSET,
) -> dict[str, Any]:
    """Return a read-only route assessment for this known case and its current owner."""
    if getattr(store, "local_mode", False):
        pending = bool(state.get("pending_inputs"))
        return {
            "unknown": pending,
            "reason": "local_pending_input" if pending else None,
            "member": None,
            "gap_ref": None,
            "gap": None,
            "ack": None,
            "guards": [],
            "effective_covered_seq": None,
            "barrier": None,
            "control_pending": False,
            "control_error": None,
            "local_mode": True,
        }
    case_id = state["case_id"]
    owner_session_id = state["owner_session_id"]
    member, index_error = _session_route_member(
        store,
        owner_session_id,
        case_id,
        index_value=route_index,
    )
    reason = index_error
    if member is None and index_error is None:
        for decision in reversed(state.get("decisions", [])):
            if not isinstance(decision, dict) or decision.get("kind") != "handoff":
                continue
            try:
                handoff_stamp = require_stamp(decision.get("stamp"), pointer="/handoff/stamp")
            except ContractError:
                continue
            if handoff_stamp["role"] == "root" and handoff_stamp["session_id"] == owner_session_id:
                reason = "route_member_missing"
                break
    route_index_readable = index_error in {None, "route_member_missing"}
    guards: list[dict[str, Any]] = []
    guard_by_identity: dict[tuple[str, str, int, str], dict[str, Any]] = {}

    def committed_by_current_handoff(ref: Mapping[str, Any]) -> bool:
        for decision in state.get("decisions", []):
            if not isinstance(decision, dict) or decision.get("kind") != "handoff":
                continue
            try:
                handoff_stamp = require_stamp(decision.get("stamp"), pointer="/handoff/stamp")
            except ContractError:
                continue
            if handoff_stamp["role"] != "root" or handoff_stamp["session_id"] != owner_session_id:
                continue
            refs = decision.get("refs", [])
            if not isinstance(refs, list):
                continue
            for raw_ref in refs:
                try:
                    if validate_ref_shape(raw_ref, pointer="/handoff/refs") == dict(ref):
                        return True
                except ContractError:
                    continue
        return False

    if route_index_readable and member is not None and member.get("gap_ref") is not None:
        try:
            gap_ref = validate_ref_shape(member["gap_ref"], pointer="/route_index/members/gap_ref")
            gap = _read_route_audit(store, gap_ref, case_id=case_id, member=member)
            if gap["session_id"] != owner_session_id and not committed_by_current_handoff(gap_ref):
                raise ContractError("capture_unavailable", "cross-session route gap is not committed by a handoff")
            guard = {"gap_ref": gap_ref, "gap": gap, "ack": None, "unresolved": True}
            identity = (gap_ref["path"], gap_ref["sha256"], gap_ref["byte_len"], gap_ref["media_type"])
            guard_by_identity[identity] = guard
            guards.append(guard)
        except ContractError:
            reason = "route_gap_invalid"

    # A committed handoff may carry one or more older owner guards. Keep every
    # exact Ref from this known case's current-owner handoff Decision; do not
    # scan another case, another session, or an index side table.
    if route_index_readable:
        for decision in state.get("decisions", []):
            if not isinstance(decision, dict) or decision.get("kind") != "handoff":
                continue
            try:
                decision_stamp = require_stamp(decision.get("stamp"), pointer="/handoff/stamp")
            except ContractError:
                continue
            if decision_stamp["role"] != "root" or decision_stamp["session_id"] != owner_session_id:
                continue
            decision_refs = decision.get("refs", [])
            if not isinstance(decision_refs, list):
                reason = "route_gap_invalid"
                continue
            for raw_ref in decision_refs:
                try:
                    candidate = validate_ref_shape(raw_ref, pointer="/handoff/refs")
                except ContractError:
                    reason = "route_gap_invalid"
                    continue
                if not candidate["path"].startswith(".reaslab/optagent/host/route_audits/"):
                    continue
                identity = (candidate["path"], candidate["sha256"], candidate["byte_len"], candidate["media_type"])
                if identity in guard_by_identity:
                    continue
                try:
                    candidate_gap = _read_route_audit(store, candidate, case_id=case_id)
                except ContractError:
                    reason = "route_gap_invalid"
                    continue
                guard = {"gap_ref": candidate, "gap": candidate_gap, "ack": None, "unresolved": True}
                guard_by_identity[identity] = guard
                guards.append(guard)

    if route_index_readable and member is not None and member.get("gap_ref") is not None and not guards:
        reason = reason or "route_gap_invalid"

    for guard in guards:
        ack = _source_adoption_ack(
            store,
            state,
            guard["gap_ref"],
        )
        guard["ack"] = ack
        gap = guard["gap"]
        guard["unresolved"] = ack is None or (
            gap["session_id"] == ack["session_id"]
            and any(interval["end"] > ack["arrival_seq"] for interval in gap["missing_seq_ranges"])
        )

    unresolved_gap = any(guard["unresolved"] for guard in guards)
    primary = next((guard for guard in guards if guard["unresolved"]), guards[0] if guards else None)
    gap_ref = primary["gap_ref"] if primary is not None else None
    gap = primary["gap"] if primary is not None else None
    ack = primary["ack"] if primary is not None else None
    effective_covered = member["covered_seq"] if member is not None else None
    if member is not None:
        current_session_anchors = [
            guard["ack"]["arrival_seq"]
            for guard in guards
            if guard["ack"] is not None and guard["ack"]["session_id"] == owner_session_id
        ]
        if current_session_anchors:
            effective_covered = max(effective_covered or 0, *current_session_anchors)

    control_pending = False
    control_error = None
    barrier = None
    if member is not None:
        barrier_path = f".reaslab/optagent/input-fences/{hashlib.sha256(owner_session_id.encode('utf-8')).hexdigest()}.json"
        try:
            barrier = _load_barrier(store, barrier_path, owner_session_id)
        except ContractError:
            reason = reason or "route_barrier_invalid"
        control_pending, control_error = _pending_control_status(
            store,
            owner_session_id,
            case_id,
            queue_value=control_queue,
        )
        if control_error is not None:
            reason = reason or control_error
    unknown = reason is not None or unresolved_gap or control_pending
    if unresolved_gap:
        reason = "capture_unavailable"
    elif control_pending and reason is None:
        reason = "capture_unavailable"
    return {
        "unknown": unknown,
        "reason": reason,
        "member": member,
        "gap_ref": gap_ref,
        "gap": gap,
        "ack": ack,
        "guards": guards,
        "effective_covered_seq": effective_covered,
        "barrier": barrier,
        "control_pending": control_pending,
        "control_error": control_error,
    }


def _pending_control_status(
    store: CaseStore,
    session_id: str,
    case_id: str,
    *,
    queue_value: Any = _ROUTE_INPUT_UNSET,
) -> tuple[bool, str | None]:
    """Check only the bounded durable outbox and attribute bad events by Case."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    path = f".reaslab/optagent/host/pending_control/{digest}.json"
    if queue_value is _ROUTE_INPUT_UNSET:
        try:
            queue = CaseStore._json_read(store.resolve_path(path, must_exist=True), max_bytes=64 * 1024 * 1024)
        except ContractError as exc:
            if exc.code == "not_found":
                return False, None
            return True, "route_control_queue_invalid"
        except (OSError, ValueError, UnicodeError):
            return True, "route_control_queue_invalid"
    else:
        queue = queue_value
        if queue is None:
            return False, None
    if not isinstance(queue, dict):
        return True, "route_control_queue_invalid"
    expected_workspace = workspace_path_identity(store.workspace)
    if (
        set(queue) != {"schema_version", "session_id", "workspace_absolute", "events"}
        or queue.get("schema_version") != 1
        or queue.get("session_id") != session_id
        or not isinstance(queue.get("workspace_absolute"), str)
        or workspace_path_identity(queue["workspace_absolute"]) != expected_workspace
        or not isinstance(queue.get("events"), list)
        or len(queue["events"]) > 1024
    ):
        return True, "route_control_queue_invalid"
    seen_event_ids: set[str] = set()
    target_pending = False
    for event in queue["events"]:
        if not isinstance(event, dict):
            return True, "route_control_queue_invalid"
        raw_event_id = event.get("event_id")
        try:
            event_id = require_id(raw_event_id, pointer="/pending_control/events/event_id")
        except ContractError:
            return True, "route_control_queue_invalid"
        if event_id in seen_event_ids:
            return True, "route_control_queue_invalid"
        seen_event_ids.add(event_id)
        bindings = event.get("bindings")
        if not isinstance(bindings, list) or not bindings:
            return True, "route_control_queue_invalid"
        event_cases: set[str] = set()
        for binding in bindings:
            if not isinstance(binding, dict):
                return True, "route_control_queue_invalid"
            try:
                event_cases.add(require_id(binding.get("case_id"), pointer="/pending_control/events/bindings/case_id"))
            except ContractError:
                return True, "route_control_queue_invalid"
        if case_id in event_cases:
            # Any unacknowledged event fences that exact Case. Legacy or
            # malformed multi-binding rows remain attributable to their named
            # Cases and do not poison unrelated members.
            target_pending = True
    return target_pending, "route_control_pending" if target_pending else None


def _route_warning(store: CaseStore, case_ids: Sequence[str]) -> list[dict[str, str]]:
    for case_id in dict.fromkeys(case_ids):
        try:
            state = store.read_case(case_id)
        except ContractError:
            continue
        if _route_freshness(store, state)["unknown"]:
            return [{
                "code": "capture_unavailable",
                "message": "当前输入新鲜度未知；旧选择只代表历史状态，报告审计仅确认既有附件一致。",
            }]
    return []


def _require_route_current(store: CaseStore, state: Mapping[str, Any]) -> dict[str, Any]:
    route = _route_freshness(store, state)
    if route["unknown"]:
        raise ContractError("capture_unavailable", "current case input freshness is unknown; classify or adopt real inputs before relying on current claims")
    return route


def _require_session_event_gate(store: CaseStore, state: Mapping[str, Any], stamp: Mapping[str, Any]) -> None:
    """Short final-CAS fence for unmaterialized events on this exact Case."""
    _require_owner(state, stamp)
    if state.get("pending_inputs"):
        raise ContractError("input_unclassified", "case has pending session input")
    _require_route_current(store, state)
    pending, reason = _pending_control_status(store, stamp["session_id"], state["case_id"])
    if pending:
        raise ContractError(
            "capture_unavailable",
            "case has an unacknowledged or invalid session control event",
            details={"reason": reason},
        )


def _replace_fence_admission(state: dict[str, Any], capture_id: str, admission: str) -> None:
    for fence in state["pending_inputs"]:
        if fence["capture_id"] == capture_id:
            fence["admission"] = admission
            return
    raise ContractError("capture_unavailable", "pending capture disappeared")


def _bump_generation(state: dict[str, Any]) -> None:
    state["promotion_generation"] += 1


def _stderr(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def _protocol_error_reply(error: ContractError) -> dict[str, Any]:
    return {"kind": "complete", "reply": public_failure("optagent.case", "invalid", error)}


def _case_operation(store: CaseStore, args: Mapping[str, Any], context: Mapping[str, Any]) -> tuple[dict[str, Any], str | None, int | None]:
    op = args["op"]
    stamp = context["stamp"]
    if op == "list":
        entries = _list_cases(store)
        after = args.get("after_case_id")
        if after is not None:
            entries = [item for item in entries if item["case_id"] > after]
        limit = args.get("limit", 50)
        page = entries[:limit]
        next_after = page[-1]["case_id"] if len(entries) > limit and page else None
        return {"cases": page, "next_after": next_after}, None, None
    if op == "create":
        _require_root(stamp)
        transfer = _context_capture(context, args["capture_id"])
        state, source_bundle = create_case_from_capture(
            store,
            title=args["title"],
            task_kind=args["task_kind"],
            world_relation=args["world_relation"],
            authorization_note=args["authorization_note"],
            capture_transfer=transfer,
            stamp=stamp,
        )
        result = {"state": _full_state_summary(store, state), "source": _object_summary(source_bundle["source"]["source_id"], source_bundle["record_ref"], "captured", state["source_revision"], None)}
        return result, state["case_id"], state["state_revision"]
    if op == "inputs":
        _require_root(stamp)
        case_id = args.get("case_id")
        if case_id is None:
            transfers: dict[str, dict[str, Any]] = {}
            for transfer in context["captures"]:
                parsed, _ = validate_capture_transfer(transfer, expected_session_id=stamp["session_id"])
                transfers[parsed["capture_id"]] = parsed
            return _input_page(transfers.values(), args), None, None
        state = store.read_case(case_id)
        if state["owner_session_id"] != stamp["session_id"]:
            raise ContractError("permission_denied", "pending inputs belong to the current owner session")
        return _input_page(_captures_for_case(store, context, state), args), case_id, state["state_revision"]
    case_id = args["case_id"]
    state = store.read_case(case_id)
    if op == "get":
        _require_read_role(stamp)
        validity = _current_validity(store, state)
        current_ref = None
        if state["selected_delivery_id"] is not None:
            path = f".reaslab/optagent/cases/{case_id}/deliveries/{state['selected_delivery_id']}/delivery.json"
            current_ref = store.ref_for_path(path)
        candidate_ref = None
        if state["candidate_id"] is not None:
            candidate_ref = store.ref_for_path(f".reaslab/optagent/cases/{case_id}/candidates/{state['candidate_id']}.json")
        return {
            "state": _full_state_summary(store, state),
            "state_ref": store.ref_for_path(f".reaslab/optagent/cases/{case_id}/state.json"),
            "question_refs": [_read_question(store, state, qid)[1] for qid in state["question_ids"]],
            "candidate_ref": candidate_ref,
            "current_ref": current_ref,
            "current_validity": validity,
            "next_actions": _next_actions(store, state, validity),
        }, case_id, state["state_revision"]
    if op == "revise":
        _require_root(stamp)
        kind = args["kind"]
        if kind == "handoff":
            return _revise_handoff(store, state, args, context)
        _require_owner(state, stamp)
        expected = args["expected_state_revision"]
        if state["state_revision"] != expected and kind != "sources":
            raise ContractError("stale_revision", "case revision changed", details={"actual": state["state_revision"]})
        if kind == "display":
            next_state = store.cas_case(case_id, expected, lambda current: current.update({"title": args["title"]}))
            return {"state": _full_state_summary(store, next_state), "changed_source": False, "changed_candidate": False}, case_id, next_state["state_revision"]
        if kind == "note_input":
            capture = _capture_anywhere(store, args["capture_id"], context, state)
            if capture["channel"] == "steer" and capture["admission"] == "pending_steer":
                raise ContractError("capture_pending", "steer admission is still pending")
            if args["disposition"] == "superseded":
                later = _capture_anywhere(store, args["superseding_capture_id"], context, state)
                if later["arrival_seq"] <= capture["arrival_seq"] or later["admission"] not in {"received_prompt", "accepted_steer"}:
                    raise ContractError("capture_unconfirmed", "superseding input must be a later accepted capture")
            next_state = store.cas_case(
                case_id,
                expected,
                lambda current: _classify_capture(current, args["capture_id"], args["disposition"], args["reason"], stamp),
            )
            return {"state": _full_state_summary(store, next_state), "changed_source": False, "changed_candidate": False}, case_id, next_state["state_revision"]
        if kind == "withdraw_question":
            next_state = _withdraw_question(store, state, args, stamp)
            return {"state": _full_state_summary(store, next_state), "changed_source": False, "changed_candidate": False}, case_id, next_state["state_revision"]
        if kind == "sources":
            return _revise_sources(store, state, args, context)
        if kind == "candidate":
            _require_route_current(store, state)
            return _revise_candidate(store, state, args, stamp)
        raise ContractError("unknown_operation", "unknown case revision kind")
    if op == "question":
        _require_root(stamp)
        _require_owner(state, stamp)
        if state["state_revision"] != args["expected_state_revision"]:
            raise ContractError("stale_revision", "case revision changed", details={"actual": state["state_revision"]})
        return _ask_question(store, state, args, context)
    if op == "answer":
        _require_root(stamp)
        _require_owner(state, stamp)
        if state["state_revision"] != args["expected_state_revision"]:
            raise ContractError("stale_revision", "case revision changed", details={"actual": state["state_revision"]})
        return _answer_question(store, state, args, context)
    raise ContractError("unknown_operation", "unknown case operation")


def _data_operation(store: CaseStore, args: Mapping[str, Any], context: Mapping[str, Any]) -> tuple[dict[str, Any], str, int | None]:
    op = args["op"]
    stamp = context["stamp"]
    case_id = args["case_id"]
    state = store.read_case(case_id)
    if op == "inspect":
        _require_read_role(stamp)
        if args["data_id"] not in state["data_ids"]:
            raise ContractError("not_found", "data bundle does not belong to this case")
        path = f".reaslab/optagent/cases/{case_id}/data/{args['data_id']}/manifest.json"
        manifest_ref = store.ref_for_path(path)
        manifest_raw = store.read_ref(manifest_ref)
        manifest = strict_json_loads(manifest_raw, max_bytes=len(manifest_raw))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("source_ids"), list):
            raise ContractError("artifact_changed", "DataManifest source identity is invalid")
        bundle = load_data_bundle(
            store,
            manifest,
            source_records=data_source_records(
                store,
                case_id=case_id,
                source_ids=manifest["source_ids"],
            ),
        )
        samples = []
        json_budget = {"nodes": args.get("sample_rows", 10), "chars": 4096}
        for spec in manifest["objects"]:
            obj = bundle.object(spec["object_id"])
            if obj.kind == "json":
                if json_budget["nodes"] == 0 or json_budget["chars"] == 0:
                    preview, preview_truncated, included = None, True, False
                else:
                    preview, preview_length, preview_truncated, included = _bounded_json_preview(
                        bundle.json(obj.object_id),
                        node_budget=json_budget,
                        char_limit=json_budget["chars"],
                    )
                    if included:
                        json_budget["chars"] -= preview_length
                samples.append({
                    "object_id": obj.object_id,
                    "kind": "json",
                    "rows": [preview] if included else [],
                    "total": 1,
                    "truncated": preview_truncated,
                })
            elif obj.kind in {"table", "relation"}:
                rows = [dict(row) for row in obj.rows[: args.get("sample_rows", 10)]]
                samples.append({"object_id": obj.object_id, "rows": rows, "total": len(obj.rows), "truncated": len(obj.rows) > args.get("sample_rows", 10)})
            else:
                rows = list(obj.rows[: args.get("sample_rows", 10)])
                samples.append({"object_id": obj.object_id, "rows": rows, "total": len(obj.rows), "truncated": len(obj.rows) > args.get("sample_rows", 10)})
        trace = manifest["trace"]
        return {"manifest_ref": manifest_ref, "trace": trace, "samples": samples}, case_id, state["state_revision"]
    _require_root(stamp)
    _require_owner(state, stamp)
    _require_route_current(store, state)
    expected = args["expected_state_revision"]
    if state["state_revision"] != expected:
        raise ContractError("stale_revision", "case revision changed", details={"actual": state["state_revision"]})
    if op == "capture":
        raw, workspace_ref = store.read_user_file(args["path"])
        if args["purpose"] == "data":
            existing = reusable_file_source(
                store,
                state,
                path=args["path"],
                purpose=args["purpose"],
                locations=args["locations"],
                bytes_ref=workspace_ref,
            )
            if existing is not None:
                source, record_ref = existing
                existing_bytes_ref = validate_ref_shape(source.get("bytes"), pointer="/source/bytes")
                if (
                    source.get("original_sha256") != existing_bytes_ref["sha256"]
                    or source.get("original_size") != existing_bytes_ref["byte_len"]
                ):
                    raise ContractError("artifact_changed", "reused Source metadata differs from its raw-byte Ref")
                current = _with_session_route_read(
                    store,
                    state,
                    stamp,
                    lambda current: current,
                    exact_refs=[record_ref, existing_bytes_ref],
                )
                return {
                    "state": _full_state_summary(store, current),
                    "source": _object_summary(source["source_id"], record_ref, "reused", current["source_revision"], None),
                }, case_id, current["state_revision"]
        source, record_ref = capture_file_source(
            store,
            case_id=case_id,
            path=args["path"],
            purpose=args["purpose"],
            locations=args["locations"],
            stamp=stamp,
            stable_read=(raw, workspace_ref),
        )
        def commit_capture(fence: _SessionRouteFence) -> dict[str, Any]:
            def add_source(current: dict[str, Any]) -> None:
                fence.validate_current(current, stamp, require_no_pending=False)
                if current["state_revision"] != expected:
                    raise ContractError("stale_revision", "case revision changed", details={"actual": current["state_revision"]})
                if source["source_id"] not in current["source_ids"]:
                    current["source_ids"].append(source["source_id"])
                if args["purpose"] in {"requirement", "data"}:
                    current["source_revision"] += 1
                    current["promotion_generation"] += 1
                    current["decisions"].append({"kind": "source_change", "note": "captured requirement or data source", "refs": [record_ref], "stamp": dict(stamp), "created_at": utc_now()})

            return store.cas_case(case_id, expected, add_source)

        next_state = _with_session_route_commit(
            store,
            state,
            stamp,
            commit_capture,
            require_no_pending=False,
        )
        return {"state": _full_state_summary(store, next_state), "source": _object_summary(source["source_id"], record_ref, "captured", next_state["source_revision"], None)}, case_id, next_state["state_revision"]
    if op == "normalize":
        if not set(args["source_ids"]).issubset(state["source_ids"]):
            raise ContractError("not_found", "normalization source is not registered on this case")
        source_records: dict[str, dict[str, Any]] = {}
        for source_id in args["source_ids"]:
            record = store.read_record(f".reaslab/optagent/cases/{case_id}/sources/{source_id}.json")
            if record.get("purpose") != "data":
                raise ContractError("invalid_argument", "normalization requires Sources classified as data")
            source_records[source_id] = record
        data_id = new_id()
        normalized = normalize_data(
            store,
            data_id=data_id,
            source_records=source_records,
            source_bytes=None,
            plan=args["plan"],
            target_prefix=f".reaslab/optagent/cases/{case_id}/data/{data_id}",
        )
        data_manifest_ref = normalized["manifest_ref"]
        def commit_normalize(fence: _SessionRouteFence) -> dict[str, Any]:
            def add_data(current: dict[str, Any]) -> None:
                fence.validate_current(current, stamp, require_no_pending=False)
                if current["state_revision"] != expected:
                    raise ContractError("stale_revision", "case revision changed", details={"actual": current["state_revision"]})
                current["data_ids"].append(data_id)

            return store.cas_case(case_id, expected, add_data)

        next_state = _with_session_route_commit(
            store,
            state,
            stamp,
            commit_normalize,
            require_no_pending=False,
        )
        return {
            "state": _full_state_summary(store, next_state),
            "data_manifest": {"data_id": data_id, "manifest_ref": data_manifest_ref, "object_count": normalized["object_count"], "trace_ref": normalized["trace_ref"]},
        }, case_id, next_state["state_revision"]
    raise ContractError("unknown_operation", "unknown data operation")


def _json_string_char_length(value: str, *, limit: int | None = None) -> int:
    """Measure compact ensure_ascii=False JSON string output without building it."""
    total = 2
    for char in value:
        code = ord(char)
        if char in {'"', "\\", "\b", "\f", "\n", "\r", "\t"}:
            total += 2
        elif code < 0x20:
            total += 6
        else:
            total += 1
        if limit is not None and total > limit:
            return total
    return total


def _json_string_prefix(value: str, limit: int) -> tuple[str, int, bool]:
    total = 2
    end = 0
    for end, char in enumerate(value, start=1):
        code = ord(char)
        part = 2 if char in {'"', "\\", "\b", "\f", "\n", "\r", "\t"} else 6 if code < 0x20 else 1
        if total + part > limit:
            return value[: end - 1], total, True
        total += part
    return value[:end] if value else "", total, False


def _bounded_json_preview(
    value: Any,
    *,
    node_budget: dict[str, int],
    char_limit: int,
    depth: int = 0,
) -> tuple[Any, int, bool, bool]:
    """Build a valid JSON prefix under shared node, depth, and character budgets."""
    if depth > 4 or node_budget["nodes"] <= 0:
        return None, 0, True, False
    node_budget["nodes"] -= 1
    if value is None or isinstance(value, bool):
        preview, truncated = value, False
        length = len(json.dumps(preview, separators=(",", ":")))
        return (preview, length, truncated, True) if length <= char_limit else (None, 0, True, False)
    if isinstance(value, (int, float)):
        length = len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
        return (value, length, False, True) if length <= char_limit else (None, 0, True, False)
    if isinstance(value, str):
        full_length = _json_string_char_length(value, limit=char_limit)
        if full_length <= char_limit:
            return value, full_length, False, True
        prefix, length, _cut = _json_string_prefix(value, char_limit)
        if length <= char_limit:
            return prefix, length, True, True
        return None, 0, True, False
    if isinstance(value, list):
        if char_limit < 2:
            return None, 0, True, False
        preview: list[Any] = []
        used = 2
        truncated = False
        for child in value:
            separator = 1 if preview else 0
            remaining = char_limit - used - separator
            child_preview, child_length, child_truncated, included = _bounded_json_preview(
                child, node_budget=node_budget, char_limit=remaining, depth=depth + 1
            )
            if not included:
                truncated = True
                break
            preview.append(child_preview)
            used += separator + child_length
            if child_truncated:
                truncated = True
                break
        else:
            return preview, used, False, True
        return preview, used, truncated, True
    if isinstance(value, dict):
        if char_limit < 2:
            return None, 0, True, False
        preview: dict[str, Any] = {}
        used = 2
        truncated = False
        for key, child in value.items():
            separator = 1 if preview else 0
            key_length = _json_string_char_length(key, limit=char_limit - used - separator - 1)
            remaining = char_limit - used - separator - key_length - 1
            if key_length > char_limit - used - separator - 1 or remaining < 0:
                truncated = True
                break
            child_preview, child_length, child_truncated, included = _bounded_json_preview(
                child, node_budget=node_budget, char_limit=remaining, depth=depth + 1
            )
            if not included:
                truncated = True
                break
            preview[key] = child_preview
            used += separator + key_length + 1 + child_length
            if child_truncated:
                truncated = True
                break
        else:
            return preview, used, False, True
        return preview, used, truncated, True
    return None, 0, True, False


def _review_source_provenance(
    store: CaseStore,
    state: Mapping[str, Any],
    source_view: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build the fixed raw-first projection without exposing owner prose or full record refs."""
    case_id = state["case_id"]
    projected_by_id: dict[str, dict[str, Any]] = {}
    projected_record_refs: dict[str, dict[str, Any]] = {}
    raw_refs: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    raw_bytes: dict[tuple[str, str, int, str], bytes] = {}
    source_prefix = f".reaslab/optagent/cases/{case_id}/sources/"
    context_prefix = f".reaslab/optagent/cases/{case_id}/source-context/"
    question_prefix = f".reaslab/optagent/cases/{case_id}/questions/"
    question_context_scope = (
        "这是所答 Question 提问轮的 retained assistant 候选背景，不包含完整中间对话，也不保证最新、唯一或未被替代。"
    )

    def read_question_context(
        source: Mapping[str, Any],
        bytes_ref: Mapping[str, Any],
        origin_value: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        raw_ref_value = source.get("conversation_context")
        if raw_ref_value is None:
            return None
        context_ref = validate_ref_shape(raw_ref_value, pointer="/source/conversation_context")
        if (
            context_ref["path"] != f"{context_prefix}{context_ref['sha256']}.json"
            or context_ref["media_type"] != "application/json"
        ):
            raise ContractError("artifact_changed", "Source conversation context Ref is outside its fixed content-addressed path")
        context_raw = store.read_ref(context_ref)
        try:
            context_value = strict_json_loads(context_raw, max_bytes=len(context_raw))
        except (ValueError, UnicodeError) as exc:
            raise ContractError("artifact_changed", "Source conversation context is invalid JSON") from exc
        if not isinstance(context_value, Mapping) or canonical_json_bytes(dict(context_value)) != context_raw:
            raise ContractError("artifact_changed", "Source conversation context is not canonical JSON")
        if set(context_value) != {
            "kind", "capture_binding", "question_ref", "asked_by", "assistant_family_omitted", "messages"
        } or context_value.get("kind") != "question_turn_context":
            raise ContractError("artifact_changed", "Source conversation context has an invalid shape")
        capture_binding = context_value.get("capture_binding")
        if not isinstance(capture_binding, Mapping) or set(capture_binding) != {
            "session_id", "capture_id", "arrival_epoch", "arrival_seq", "channel", "admission", "sha256"
        }:
            raise ContractError("artifact_changed", "Source conversation context capture binding is invalid")
        capture_binding = {
            "session_id": require_string(capture_binding["session_id"], pointer="/source/conversation_context/capture_binding/session_id"),
            "capture_id": require_id(capture_binding["capture_id"], pointer="/source/conversation_context/capture_binding/capture_id"),
            "arrival_epoch": require_id(capture_binding["arrival_epoch"], pointer="/source/conversation_context/capture_binding/arrival_epoch"),
            "arrival_seq": require_int(capture_binding["arrival_seq"], pointer="/source/conversation_context/capture_binding/arrival_seq", minimum=1),
            "channel": capture_binding["channel"],
            "admission": capture_binding["admission"],
            "sha256": require_hash(capture_binding["sha256"], pointer="/source/conversation_context/capture_binding/sha256"),
        }
        if (
            capture_binding["channel"] != "prompt"
            or capture_binding["admission"] != "received_prompt"
            or capture_binding["session_id"] != origin_value.get("session_id")
            or capture_binding["capture_id"] != origin_value.get("capture_id")
            or capture_binding["arrival_epoch"] != origin_value.get("arrival_epoch")
            or capture_binding["arrival_seq"] != origin_value.get("arrival_seq")
            or capture_binding["channel"] != origin_value.get("channel")
            or capture_binding["admission"] != origin_value.get("transport_admission")
            or capture_binding["sha256"] != bytes_ref.get("sha256")
        ):
            raise ContractError("artifact_changed", "Source conversation context is not bound to its accepted user Source")
        asked_by = require_stamp(context_value.get("asked_by"), pointer="/source/conversation_context/asked_by")
        if asked_by["role"] != "root" or asked_by["session_id"] != capture_binding["session_id"]:
            raise ContractError("artifact_changed", "Source conversation context Question anchor is invalid")
        retained = _validate_question_turn_context(
            {
                "assistant_family_omitted": context_value.get("assistant_family_omitted"),
                "unavailable_turn_ids": [],
                "messages": context_value.get("messages"),
            },
            pointer="/source/conversation_context",
        )
        if retained is None or not retained["messages"] or any(
            item["source"]["id"]["turn_id"] != asked_by["turn_id"]
            for item in retained["messages"]
        ):
            raise ContractError("artifact_changed", "Source conversation context does not match its Question turn")
        question_ref = validate_ref_shape(context_value.get("question_ref"), pointer="/source/conversation_context/question_ref")
        question_suffix = question_ref["path"][len(question_prefix):] if question_ref["path"].startswith(question_prefix) else ""
        question_parts = question_suffix.split("/")
        if (
            len(question_parts) != 3
            or question_parts[1] != "versions"
            or question_parts[2] != f"{question_ref['sha256']}.json"
            or question_ref["media_type"] != "application/json"
        ):
            raise ContractError("artifact_changed", "Source conversation context Question Ref is outside its immutable Case path")
        question_id = require_id(question_parts[0], pointer="/source/conversation_context/question_ref/path")
        question_raw = store.read_ref(question_ref)
        try:
            question_value = strict_json_loads(question_raw, max_bytes=len(question_raw))
        except (ValueError, UnicodeError) as exc:
            raise ContractError("artifact_changed", "Source conversation context Question is invalid JSON") from exc
        if (
            not isinstance(question_value, Mapping)
            or canonical_json_bytes(dict(question_value)) != question_raw
            or question_value.get("request_id") != question_id
            or question_value.get("asked_by") != asked_by
        ):
            raise ContractError("artifact_changed", "Source conversation context does not match its exact Question Ref")
        return (
            {
                "ref": context_ref,
                "question_ref": question_ref,
                "assistant_family_omitted": retained["assistant_family_omitted"],
                "scope": question_context_scope,
            },
            [context_ref, question_ref],
        )

    def project_source(
        source_id_value: str,
        expected_record_ref: Mapping[str, Any] | None = None,
        source_record: Mapping[str, Any] | None = None,
        source_record_raw: bytes | None = None,
    ) -> dict[str, Any]:
        source_id = require_id(source_id_value, pointer="/source_id")
        record_ref = (
            validate_ref_shape(dict(expected_record_ref), pointer="/source_adoption/source_refs")
            if expected_record_ref is not None
            else source_view["record_refs"].get(source_id)
        )
        cached = projected_by_id.get(source_id)
        if cached is not None:
            if record_ref is None or projected_record_refs[source_id] != record_ref:
                raise ContractError("artifact_changed", "source provenance Ref differs from its cached exact record")
            return cached
        source = source_view["records"].get(source_id)
        active_ref = source_view["record_refs"].get(source_id)
        if expected_record_ref is not None and active_ref != record_ref:
            if source_record is None:
                raw_record = source_record_raw if source_record_raw is not None else store.read_ref(record_ref)
                try:
                    source_record = strict_json_loads(raw_record, max_bytes=len(raw_record))
                except (ValueError, UnicodeError) as exc:
                    raise ContractError("artifact_changed", "historical Source record is invalid JSON") from exc
            if not isinstance(source_record, Mapping):
                raise ContractError("artifact_changed", "historical Source record is not an object")
            source = dict(source_record)
        elif source_record is not None:
            source = dict(source_record)
        if source is None:
            raise ContractError("capture_unavailable", "provenance Source is outside the current Case projection")
        if record_ref is None:
            raise ContractError("capture_unavailable", "provenance Source has no exact committed record Ref")
        if expected_record_ref is not None:
            if record_ref["path"] != f"{source_prefix}{source_id}.json" or record_ref["media_type"] != "application/json":
                raise ContractError("artifact_changed", "adopted Source record Ref is outside its fixed Case path")
            if active_ref != record_ref:
                exact_raw = source_record_raw if source_record_raw is not None else store.read_ref(record_ref)
                if source_record is not None and canonical_json_bytes(dict(source_record)) != exact_raw:
                    raise ContractError("artifact_changed", "historical Source record bytes differ from its exact Ref")
        if source.get("source_id") != source_id:
            raise ContractError("artifact_changed", "source provenance record identity is invalid")
        bytes_ref = validate_ref_shape(source.get("bytes"), pointer="/source/bytes")
        bytes_key = (bytes_ref["path"], bytes_ref["sha256"], bytes_ref["byte_len"], bytes_ref["media_type"])
        source_raw = raw_bytes.get(bytes_key)
        if source_raw is None:
            source_raw = store.read_ref(bytes_ref)
            raw_bytes[bytes_key] = source_raw
        if source.get("original_sha256") != bytes_ref["sha256"] or source.get("original_size") != len(source_raw):
            raise ContractError("artifact_changed", "source provenance bytes do not match their record")
        created_by = require_stamp(source.get("created_by"), pointer="/source/created_by")
        origin_value = source.get("origin")
        if not isinstance(origin_value, dict):
            raise ContractError("output_invalid", "source provenance origin is invalid")
        origin = {
            key: origin_value.get(key)
            for key in ("session_id", "capture_id", "arrival_epoch", "arrival_seq", "channel", "transport_admission")
        }
        if source.get("purpose") not in {"requirement", "data", "reference"} or source.get("kind") not in {
            "external_prompt", "external_steer", "file"
        }:
            raise ContractError("output_invalid", "source provenance classification is invalid")
        for key in ("capture_id", "arrival_epoch"):
            if origin[key] is not None:
                require_id(origin[key], pointer=f"/source/origin/{key}")
        if origin["arrival_seq"] is not None:
            require_int(origin["arrival_seq"], pointer="/source/origin/arrival_seq")
        if origin["channel"] not in {None, "prompt", "steer"}:
            raise ContractError("output_invalid", "source provenance channel is invalid")
        if origin["transport_admission"] not in {None, "received_prompt", "accepted_steer"}:
            raise ContractError("output_invalid", "source provenance admission is invalid")
        projection = {
            "source_id": source_id,
            "purpose": source.get("purpose"),
            "kind": source.get("kind"),
            "created_by": created_by,
            "origin": origin,
            "record_fingerprint": {"sha256": record_ref["sha256"], "byte_len": record_ref["byte_len"]},
            "raw": {
                "sha256": bytes_ref["sha256"],
                "byte_len": bytes_ref["byte_len"],
                "ref": bytes_ref,
            },
        }
        question_context = read_question_context(source, bytes_ref, origin_value)
        if question_context is not None:
            projected_context, context_refs = question_context
            projection["conversation_context"] = projected_context
            for context_raw_ref in context_refs:
                context_key = (
                    context_raw_ref["path"],
                    context_raw_ref["sha256"],
                    context_raw_ref["byte_len"],
                    context_raw_ref["media_type"],
                )
                raw_refs[context_key] = context_raw_ref
        projected_by_id[source_id] = projection
        projected_record_refs[source_id] = record_ref
        key = (bytes_ref["path"], bytes_ref["sha256"], bytes_ref["byte_len"], bytes_ref["media_type"])
        raw_refs[key] = bytes_ref
        return projection

    active_sources = [project_source(source_id) for source_id in source_view["source_ids"]]
    route = _route_freshness(store, state)
    guards: list[dict[str, Any]] = []
    for guard in route.get("guards", []):
        gap = guard["gap"]
        projected_guard: dict[str, Any] = {
            "gap_ref": dict(guard["gap_ref"]),
            "scope": {
                "session_id": gap["session_id"],
                "covered_seq": gap["covered_seq"],
                "missing_seq_ranges": [dict(item) for item in gap["missing_seq_ranges"]],
                "registered_by": dict(gap["registered_by"]),
                "observed_by": dict(gap["observed_by"]),
            },
            "unresolved": bool(guard["unresolved"]),
        }
        ack = guard.get("ack")
        if ack is not None:
            adopted_sources: list[dict[str, Any]] = []
            for source_ref_value in ack["source_refs"]:
                source_ref = validate_ref_shape(source_ref_value, pointer="/source_adoption/source_refs")
                if not source_ref["path"].startswith(source_prefix):
                    continue
                name = source_ref["path"][len(source_prefix) :]
                source_id = require_id(name.removesuffix(".json"), pointer="/source_adoption/source_refs")
                if name != source_id + ".json" or source_ref["media_type"] != "application/json":
                    raise ContractError("capture_unavailable", "adopted source provenance path is invalid")
                raw_source = store.read_ref(source_ref)
                try:
                    source_record = strict_json_loads(raw_source, max_bytes=len(raw_source))
                except (ValueError, UnicodeError) as exc:
                    raise ContractError("artifact_changed", "adopted Source record is invalid JSON") from exc
                if not isinstance(source_record, Mapping) or source_record.get("source_id") != source_id:
                    raise ContractError("artifact_changed", "adopted Source record identity is invalid")
                adopted_sources.append(project_source(source_id, source_ref, source_record, raw_source))
            projected_guard["adoption"] = {
                "fingerprint": {"sha256": ack["ref"]["sha256"], "byte_len": ack["ref"]["byte_len"]},
                "capture_id": ack["capture_id"],
                "session_id": ack["session_id"],
                "arrival_seq": ack["arrival_seq"],
                "stamp": dict(ack["stamp"]),
                "source_revision": ack["source_revision"],
                "promotion_generation": ack["promotion_generation"],
                "sources": adopted_sources,
            }
        guards.append(projected_guard)

    validated_guard_keys = {
        (guard["gap_ref"]["path"], guard["gap_ref"]["sha256"], guard["gap_ref"]["byte_len"], guard["gap_ref"]["media_type"])
        for guard in route.get("guards", [])
    }
    handoffs: list[dict[str, Any]] = []
    for decision in state.get("decisions", []):
        if not isinstance(decision, dict) or decision.get("kind") != "handoff":
            continue
        handoff_stamp = require_stamp(decision.get("stamp"), pointer="/handoff/stamp")
        refs_value = decision.get("refs")
        if not isinstance(refs_value, list):
            raise ContractError("capture_unavailable", "handoff provenance references are invalid")
        guard_refs = []
        for ref_value in refs_value:
            ref = validate_ref_shape(ref_value, pointer="/handoff/refs")
            key = (ref["path"], ref["sha256"], ref["byte_len"], ref["media_type"])
            if ref["path"].startswith(".reaslab/optagent/host/route_audits/") and key in validated_guard_keys:
                guard_refs.append(ref)
        if guard_refs:
            handoffs.append({"stamp": handoff_stamp, "guard_refs": guard_refs})

    route_member = route.get("member")
    member_projection = None
    if route_member is not None:
        member_projection = {
            "session_id": route_member["session_id"],
            "registered_by": dict(route_member["registered_by"]),
            "input_mode": route_member["input_mode"],
            "covered_seq": route_member["covered_seq"],
            "gap_ref": dict(route_member["gap_ref"]) if route_member.get("gap_ref") is not None else None,
        }
    route_reason = route.get("reason")
    if route_reason not in {None, "route_index_invalid", "route_member_missing", "route_gap_invalid", "route_barrier_invalid", "capture_unavailable"}:
        route_reason = "capture_unavailable"
    projection = {
        "schema_version": 1,
        "case_id": case_id,
        "source_binding": {
            "source_revision": state["source_revision"],
            "math_source_ids": list(source_view["math_source_ids"]),
        },
        "provenance_source_ids": list(source_view["source_ids"]),
        "sources": active_sources,
        "route": {
            "session_id": state["owner_session_id"],
            "unknown": bool(route["unknown"]),
            "reason": route_reason,
            "member": member_projection,
            "guards": guards,
            "handoffs": handoffs,
        },
    }
    return projection, list(raw_refs.values())


def _review_operation(store: CaseStore, args: Mapping[str, Any], context: Mapping[str, Any]) -> tuple[dict[str, Any], str, int]:
    stamp = context["stamp"]
    op = args["op"]
    case_id = args["case_id"]
    if op == "begin":
        if stamp["role"] != "opt-verifier":
            raise ContractError("permission_denied", "only the verifier can begin a review")
        result, review = begin_review(
            store,
            case_id=case_id,
            expected_state_revision=args["expected_state_revision"],
            candidate_id=args["candidate_id"],
            proof_ids=args["proof_ids"],
            run_ids=args["run_ids"],
            prior_review_id=args.get("prior_review_id"),
            stamp=stamp,
            source_provenance_builder=_review_source_provenance,
        )
        return {k: v for k, v in result.items() if k != "state"}, case_id, result["state"]["state_revision"]
    if op == "note":
        if stamp["role"] != "opt-verifier":
            raise ContractError("permission_denied", "only the verifier can write a source note")
        return add_note(
            store,
            case_id=case_id,
            review_id=args["review_id"],
            expected_review_revision=args["expected_review_revision"],
            body=args["body"],
            source_locations=args["source_locations"],
            stamp=stamp,
        ), case_id, store.read_case(case_id)["state_revision"]
    if op == "submit":
        if args["record_kind"] in {"issue", "outcome"} and stamp["role"] != "opt-verifier":
            raise ContractError("permission_denied", "only the verifier can submit review findings")
        if args["record_kind"] in {"disposition", "proof"} and stamp["role"] != "root":
            raise ContractError("permission_denied", "only root can submit this review record")
        mutation_state: Mapping[str, Any] | None = None
        if args["record_kind"] in {"proof", "disposition"}:
            mutation_state = store.read_case(case_id)
            _require_owner(mutation_state, stamp)
            _require_route_current(store, mutation_state)
        validate_current: Callable[[Mapping[str, Any]], None] | None = None
        if args["record_kind"] == "disposition":
            def validate_disposition_state(current: Mapping[str, Any]) -> None:
                if context.get("cancellation_observed") is True:
                    raise ContractError("execution_unknown", "the active tool call was cancelled before recording its disposition")
                _require_owner(current, stamp)

            validate_current = validate_disposition_state
        elif args["record_kind"] == "proof":
            def validate_proof_state(current: Mapping[str, Any]) -> None:
                if context.get("cancellation_observed") is True:
                    raise ContractError("execution_unknown", "the active tool call was cancelled before recording its proof")
                _require_owner(current, stamp)

            validate_current = validate_proof_state

        def session_commit(finalize: Callable[[Callable[[Mapping[str, Any]], None] | None], Any]) -> Any:
            if mutation_state is None:
                raise ContractError("environment_unavailable", "review commit is missing its Case snapshot")

            def commit(fence: _SessionRouteFence) -> Any:
                def validate(current: Mapping[str, Any]) -> None:
                    if validate_current is not None:
                        validate_current(current)
                    fence.validate_current(current, stamp, require_no_pending=False)

                # Proof/disposition artifacts were prepared under the Review
                # lock and that lock was released before taking the session
                # gate. Reacquire in the approved session→review→Case order;
                # the publisher still CASes the exact prior Review Ref.
                with store.review_lock(args["review_id"]):
                    return finalize(validate)

            return _with_session_route_commit(
                store,
                mutation_state,
                stamp,
                commit,
                require_no_pending=False,
            )

        expected_state = args.get("expected_state_revision")
        result = submit_review(
            store,
            case_id=case_id,
            review_id=args["review_id"],
            expected_review_revision=args["expected_review_revision"],
            record_kind=args["record_kind"],
            stamp=stamp,
            args=args,
            expected_state_revision=expected_state,
            validate_current=validate_current,
            session_commit=session_commit if args["record_kind"] in {"proof", "disposition"} else None,
        )
        state = store.read_case(case_id)
        return result, case_id, state["state_revision"]
    raise ContractError("unknown_operation", "unknown review operation")


def _delivery_operation(store: CaseStore, args: Mapping[str, Any], context: Mapping[str, Any]) -> tuple[dict[str, Any], str, int | None]:
    op = args["op"]
    case_id = args["case_id"]
    stamp = context["stamp"]
    if op == "audit":
        _require_read_role(stamp)
        return audit_report(store, case_id=case_id, delivery_id=args["delivery_id"], report_path=args["report_path"]), case_id, store.read_case(case_id)["state_revision"]
    _require_root(stamp)
    if context.get("cancellation_observed") is True:
        raise ContractError("execution_unknown", "the active tool call was cancelled before delivery mutation")
    delivery_state = store.read_case(case_id)
    _require_session_event_gate(store, delivery_state, stamp)

    def validate_presentation_state(current: Mapping[str, Any]) -> None:
        if context.get("cancellation_observed") is True:
            raise ContractError("execution_unknown", "the active tool call was cancelled before delivery mutation")

    def session_commit(finalize: Callable[..., Any]) -> Any:
        def commit(fence: _SessionRouteFence) -> Any:
            return finalize(
                lambda current: fence.validate_current(
                    current,
                    stamp,
                    require_no_pending=True,
                )
            )

        return _with_session_route_commit(store, delivery_state, stamp, commit)

    if op == "select":
        result, _delivery = select_delivery(
            store,
            case_id=case_id,
            expected_state_revision=args["expected_state_revision"],
            kind=args["kind"],
            candidate_id=args["candidate_id"],
            review_ids=[args["review_id"]],
            proof_ids=args["proof_ids"],
            run_ids=args["run_ids"],
            stamp=stamp,
            validate_current=validate_presentation_state,
            session_commit=session_commit,
        )
        return result, case_id, result["state"]["state_revision"]
    if op == "build":
        result = build_report(
            store,
            case_id=case_id,
            expected_state_revision=args["expected_state_revision"],
            delivery_id=args["delivery_id"],
            target_path=args["target_path"],
            stamp=stamp,
            validate_current=validate_presentation_state,
            session_commit=session_commit,
        )
        return result, case_id, result["state"]["state_revision"]
    if op == "package":
        result = package_delivery(
            store,
            case_id=case_id,
            expected_state_revision=args["expected_state_revision"],
            delivery_id=args["delivery_id"],
            target_path=args["target_path"],
            include_raw_sources=args["include_raw_sources"],
            stamp=stamp,
            validate_current=validate_presentation_state,
            session_commit=session_commit,
        )
        return result, case_id, result["state"]["state_revision"]
    raise ContractError("unknown_operation", "unknown delivery operation")


def _revise_sources(
    store: CaseStore,
    state: Mapping[str, Any],
    args: Mapping[str, Any],
    context: Mapping[str, Any],
) -> tuple[dict[str, Any], str, int]:
    case_id = state["case_id"]
    stamp = context["stamp"]
    expected = args["expected_state_revision"]
    if state["state_revision"] != expected:
        raise ContractError(
            "stale_revision",
            "case revision changed",
            details=_source_stale_details(store, state, expected, stamp),
        )
    route = _route_freshness(store, state)
    recovering_gap = bool(route["unknown"])
    route_guards = route.get("guards", [])
    unresolved_guards = [guard for guard in route_guards if guard["unresolved"]]
    if recovering_gap and (
        not route_guards
        or route["member"] is None
        or route.get("reason") in {"route_index_invalid", "route_gap_invalid", "route_barrier_invalid"}
    ):
        raise ContractError("capture_unavailable", "a verified route gap and current registered member are required for source recovery")
    if recovering_gap and route["member"].get("session_id") != stamp["session_id"]:
        raise ContractError("capture_unavailable", "source recovery must use the current registered owner session")
    if recovering_gap and route.get("control_pending"):
        raise ContractError("capture_unavailable", "source recovery cannot proceed while a current-session control event is unacknowledged")
    if recovering_gap and not args["capture_ids"]:
        raise ContractError("capture_unavailable", "source recovery requires a real accepted capture as the new base")

    # Resolve the selected Source family once after the route and recovery
    # checks. All consumers below (retained records, math projection, and
    # original Ref map) must use this same case snapshot.
    source_view = source_projection(store, state)
    records: dict[str, dict[str, Any]] = {}
    for source_id in args["source_ids"]:
        if source_id not in state["source_ids"]:
            raise ContractError("not_found", "source is not registered on this case")
        records[source_id] = source_view["records"][source_id]
    adopted: list[tuple[dict[str, Any], bytes, dict[str, Any], bool]] = []
    fences = {item["capture_id"]: item for item in state["pending_inputs"]}
    seen_capture_ids: set[str] = set()
    for capture_id in args["capture_ids"]:
        if capture_id in seen_capture_ids:
            raise ContractError("invalid_argument", "source capture IDs must not repeat")
        seen_capture_ids.add(capture_id)
        fence = fences.get(capture_id)
        if fence is not None:
            if fence.get("session_id") != stamp["session_id"]:
                raise ContractError("capture_unavailable", "pending capture belongs to its original session and was not handed off as a new arrival")
            if fence.get("raw_ref") is None:
                raise ContractError("capture_unavailable", "source adoption requires persisted real input bytes")
            try:
                fence_raw_ref = validate_ref_shape(fence["raw_ref"], pointer="/pending_inputs/raw_ref")
                persisted_raw = store.read_ref(fence_raw_ref)
            except (ContractError, KeyError, TypeError) as exc:
                raise ContractError("capture_unavailable", "persisted input bytes are unavailable for source adoption") from exc
            try:
                transfer = _context_capture(context, capture_id)
            except ContractError as exc:
                if exc.code != "capture_unavailable":
                    raise
                transfer = _transfer_from_stored_raw(fence, persisted_raw, stamp["session_id"])
        else:
            transfer = _persisted_source_capture(store, state, capture_id, stamp["session_id"])
            persisted_raw = None
            fence_raw_ref = None
        parsed, raw = validate_capture_transfer(transfer, expected_session_id=stamp["session_id"])
        if (
            not raw
            or parsed["admission"] not in {"received_prompt", "accepted_steer"}
            or (
                fence is not None
                and (
                    parsed["arrival_seq"] != fence.get("arrival_seq")
                    or parsed["arrival_epoch"] != fence.get("arrival_epoch")
                    or parsed["channel"] != fence.get("channel")
                    or parsed["admission"] != fence.get("admission")
                    or parsed["sha256"] != fence_raw_ref["sha256"]
                    or raw != persisted_raw
                )
            )
        ):
            raise ContractError("capture_unavailable", "source capture does not match a non-empty accepted case input")
        adopted.append((parsed, raw, transfer, fence is not None))

    new_sources: list[dict[str, Any]] = []
    new_source_refs: list[dict[str, Any]] = []
    for parsed, raw, transfer, _had_fence in adopted:
        source, ref = source_from_capture(
            store,
            case_id=case_id,
            purpose="requirement",
            capture_transfer=transfer,
            stamp=stamp,
        )
        new_sources.append(source)
        new_source_refs.append(ref)
        records[source["source_id"]] = source
    final_ids = list(dict.fromkeys([*args["source_ids"], *[item["source_id"] for item in new_sources]]))
    if not final_ids:
        raise ContractError("invalid_argument", "case must retain at least one source")
    fact_ids: set[str] = set()
    next_facts: list[dict[str, Any]] = []
    for fact in args["facts"]:
        if fact["fact_id"] in fact_ids:
            raise ContractError("invalid_argument", "fact ID repeats")
        fact_ids.add(fact["fact_id"])
        if not fact["source_ids"] or not set(fact["source_ids"]).issubset(final_ids):
            raise ContractError("invalid_argument", "fact must cite selected case sources")
        next_facts.append(dict(fact))
    old_math_sources = list(source_view["math_source_ids"])
    next_math_sources = [source_id for source_id in final_ids if records[source_id]["purpose"] in {"requirement", "data"}]
    math_changed = recovering_gap or old_math_sources != next_math_sources or state["facts"] != next_facts

    source_refs: list[dict[str, Any]] = []
    source_ref_paths: set[str] = set()
    checked_source_bytes: set[tuple[str, str, int, str]] = set()
    for source_id in final_ids:
        record_ref = next((ref for source, ref in zip(new_sources, new_source_refs) if source["source_id"] == source_id), None)
        if record_ref is None:
            record_ref = source_view["record_refs"].get(source_id)
            if record_ref is None:
                raise ContractError("artifact_changed", "selected Source record has no stable Ref")
        source_record = records[source_id]
        bytes_ref = validate_ref_shape(source_record.get("bytes"), pointer="/source/bytes")
        bytes_key = (bytes_ref["path"], bytes_ref["sha256"], bytes_ref["byte_len"], bytes_ref["media_type"])
        if bytes_key not in checked_source_bytes:
            store.read_ref(bytes_ref)
            checked_source_bytes.add(bytes_key)
        for ref in (record_ref, bytes_ref):
            if ref["path"] not in source_ref_paths:
                source_ref_paths.add(ref["path"])
                source_refs.append(dict(ref))

    adoption_refs: list[dict[str, Any]] = []
    if recovering_gap:
        selected_adoption = min((transfer for transfer, _, _, _ in adopted), key=lambda item: item["arrival_seq"])
        source_revision = state["source_revision"] + 1
        promotion_generation = state["promotion_generation"] + 1
        for guard in unresolved_guards:
            adoption = {
                "schema_version": 1,
                "case_id": case_id,
                "gap_ref": dict(guard["gap_ref"]),
                "capture_id": selected_adoption["capture_id"],
                "session_id": stamp["session_id"],
                "arrival_seq": selected_adoption["arrival_seq"],
                "source_refs": source_refs,
                "stamp": dict(stamp),
                "source_revision": source_revision,
                "promotion_generation": promotion_generation,
            }
            adoption_raw = canonical_json_bytes(adoption)
            adoption_digest = hashlib.sha256(adoption_raw).hexdigest()
            adoption_path = f".reaslab/optagent/cases/{case_id}/attachments/source_adoption/{adoption_digest}.json"
            adoption_refs.append(store.write_immutable(adoption_path, adoption_raw, "application/json"))

    decision_refs: list[dict[str, Any]] = []
    seen_ref_paths: set[str] = set()
    for ref in ([guard["gap_ref"] for guard in unresolved_guards] if recovering_gap else []) + source_refs + adoption_refs:
        if ref["path"] not in seen_ref_paths:
            seen_ref_paths.add(ref["path"])
            decision_refs.append(dict(ref))

    def commit_sources(fence: _SessionRouteFence) -> dict[str, Any]:
        def apply(current: dict[str, Any]) -> None:
            fence.validate_current(
                current,
                stamp,
                require_no_pending=False,
                allow_unknown_route=recovering_gap,
            )
            if current["state_revision"] != expected:
                raise ContractError("stale_revision", "case revision changed", details={"actual": current["state_revision"]})
            current["source_ids"] = final_ids
            current["facts"] = next_facts
            for parsed, _, _, had_fence in adopted:
                if had_fence:
                    _classify_capture(current, parsed["capture_id"], "no_math_change", "adopted into a mathematical source", stamp, record_decision=False)
            if math_changed:
                current["source_revision"] += 1
                current["promotion_generation"] += 1
                current["decisions"].append({"kind": "source_change", "note": args["reason"], "refs": decision_refs, "stamp": dict(stamp), "created_at": utc_now()})

        return store.cas_case(case_id, expected, apply)

    try:
        updated = _with_session_route_commit(
            store,
            state,
            stamp,
            commit_sources,
            require_no_pending=False,
            allow_unknown_route=recovering_gap,
            expected_route_predicate=_route_predicate_projection(route),
        )
    except ContractError as exc:
        if exc.code != "stale_revision":
            raise
        current = store.read_case(case_id)
        raise ContractError(
            "stale_revision",
            "case revision changed",
            details=_source_stale_details(store, current, expected, stamp),
        ) from exc
    return {"state": _full_state_summary(store, updated), "changed_source": math_changed, "changed_candidate": False}, case_id, updated["state_revision"]


def _source_stale_details(
    store: CaseStore,
    state: Mapping[str, Any],
    expected: int,
    stamp: Mapping[str, str],
) -> dict[str, Any]:
    """Return a safe current-state view when a source CAS loses a race."""
    current = store.read_case(state["case_id"])
    if current["owner_session_id"] != stamp["session_id"]:
        return {"expected": expected, "actual": current["state_revision"]}
    pending_captures = []
    for fence in current.get("pending_inputs", []):
        raw_ref = fence.get("raw_ref")
        if raw_ref is not None:
            raw_ref = validate_ref_shape(raw_ref, pointer="/pending_inputs/raw_ref")
        pending_captures.append(
            {
                "capture_id": fence["capture_id"],
                "arrival_epoch": fence["arrival_epoch"],
                "arrival_seq": fence["arrival_seq"],
                "channel": fence["channel"],
                "admission": fence["admission"],
                "raw_ref": raw_ref,
            }
        )
    return {
        "expected": expected,
        "actual": current["state_revision"],
        "current_state": _full_state_summary(store, current),
        "pending_captures": pending_captures,
    }


def _revise_candidate(
    store: CaseStore,
    state: Mapping[str, Any],
    args: Mapping[str, Any],
    stamp: Mapping[str, str],
) -> tuple[dict[str, Any], str, int]:
    case_id = state["case_id"]
    if "authoring" in args:
        raise ContractError(
            "invalid_argument",
            "candidate authoring family is inferred from backend_id",
            pointer="/authoring",
        )
    from .solver_protocol import DIRECT_PYTHON_BACKEND_ID, SUPPORTED_BACKEND_IDS
    backend_id = args.get("backend_id")
    if backend_id != DIRECT_PYTHON_BACKEND_ID and backend_id not in SUPPORTED_BACKEND_IDS:
        raise ContractError("capability_uncovered", "the selected solver backend is not implemented", pointer="/backend_id")
    authoring = "direct_python" if backend_id == DIRECT_PYTHON_BACKEND_ID else "pyomo"
    if authoring == "direct_python" and args["objective_plan"].get("mode") == "lexicographic":
        raise ContractError(
            "capability_uncovered",
            "direct_python supports scalar and feasibility objective plans only",
            pointer="/objective_plan/mode",
        )

    def read_candidate_file(path: str, *, field: str) -> tuple[bytes, dict[str, Any]]:
        try:
            return store.read_user_file(path)
        except ContractError as exc:
            details = dict(exc.details)
            details.update({"operation": "case_revise_candidate", "field": field, "path": path})
            raise ContractError(
                exc.code,
                f"{exc.message} (operation=case_revise_candidate, field={field}, path={path})",
                pointer=f"/{field}",
                details=details,
            ) from exc

    expected = args["expected_state_revision"]
    module_paths = list(args["local_module_paths"])
    if len(set(module_paths)) != len(module_paths):
        raise ContractError("invalid_argument", "local module paths must not repeat")
    if args.get("instance_parameter_object_id") is not None and args["data_id"] is None:
        raise ContractError("invalid_argument", "instance parameter object requires a data bundle")
    if args["data_id"] is not None and args["data_id"] not in state["data_ids"]:
        raise ContractError("not_found", "candidate data bundle does not belong to this case")
    model_raw, model_ref = read_candidate_file(args["model_path"], field="model_path")
    modules: list[dict[str, Any]] = []
    for index, path in enumerate(module_paths):
        _, ref = read_candidate_file(path, field=f"local_module_paths/{index}")
        modules.append(ref)
    checker_ref = None
    if args["business_checker_path"] is not None:
        _, checker_ref = read_candidate_file(args["business_checker_path"], field="business_checker_path")
    source_ids = source_projection(store, state)["math_source_ids"]
    binding = {
        "source_ids": source_ids,
        "source_revision": state["source_revision"],
        "authoring": authoring,
        "model": model_ref,
        "local_modules": modules,
        "business_checker": checker_ref,
        "data_id": args["data_id"],
        "instance_parameter_object_id": args.get("instance_parameter_object_id"),
        "backend_id": args["backend_id"],
        "objective_plan": args["objective_plan"],
        "input_closure": args["input_closure"],
        "closure_note": args["closure_note"],
    }
    binding_digest = __import__("hashlib").sha256(canonical_json_bytes(binding)).hexdigest()
    old = None
    if state["candidate_id"]:
        old = store.read_record(f".reaslab/optagent/cases/{case_id}/candidates/{state['candidate_id']}.json")
    if old is not None and old["binding_digest"] == binding_digest:
        def confirm_candidate(fence: _SessionRouteFence) -> dict[str, Any]:
            def validate(current: dict[str, Any]) -> None:
                fence.validate_current(current, stamp, require_no_pending=False)
                if (
                    current["candidate_id"] != state["candidate_id"]
                    or current["candidate_revision"] != state["candidate_revision"]
                ):
                    raise ContractError("stale_revision", "current candidate changed before no-op registration")

            return store.cas_case(case_id, expected, validate)

        updated = _with_session_route_commit(
            store,
            state,
            stamp,
            confirm_candidate,
            require_no_pending=False,
        )
        ref = store.ref_for_path(f".reaslab/optagent/cases/{case_id}/candidates/{state['candidate_id']}.json")
        return {"state": _full_state_summary(store, updated), "changed_source": False, "changed_candidate": False, "candidate": object_summary(state["candidate_id"], ref, "registered", state["source_revision"], state["candidate_revision"])}, case_id, updated["state_revision"]

    candidate_root = f".reaslab/optagent/cases/{case_id}/candidates/"

    def candidate_from_ref(raw_ref: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
        record_ref = validate_ref_shape(raw_ref)
        path = record_ref["path"]
        if not path.startswith(candidate_root) or not path.endswith(".json"):
            return None
        candidate_id_from_path = path[len(candidate_root):-5]
        if not candidate_id_from_path or "/" in candidate_id_from_path:
            return None
        try:
            record = strict_json_loads(store.read_ref(record_ref))
        except (ValueError, UnicodeError) as exc:
            raise ContractError("output_invalid", "committed candidate record is invalid") from exc
        if (
            not isinstance(record, dict)
            or record.get("candidate_id") != candidate_id_from_path
            or type(record.get("candidate_revision")) is not int
            or record["candidate_revision"] < 1
        ):
            raise ContractError("output_invalid", "committed candidate identity is invalid")
        return record, record_ref

    committed_candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for decision in state["decisions"]:
        if decision["kind"] != "candidate_change":
            continue
        for historical_ref in decision["refs"]:
            historical = candidate_from_ref(historical_ref)
            if historical is not None:
                committed_candidates.append(historical)

    # Include the current pointer, but never enumerate candidate files: a failed
    # CAS may leave an uncommitted record in that directory.
    if state["candidate_id"]:
        current_ref = store.ref_for_path(f"{candidate_root}{state['candidate_id']}.json")
        current_record = candidate_from_ref(current_ref)
        if current_record is not None:
            committed_candidates.append(current_record)

    historical_match = next(
        ((record, record_ref) for record, record_ref in committed_candidates
         if record.get("binding_digest") == binding_digest
         and all(key in record and record[key] == value for key, value in binding.items())),
        None,
    )
    if historical_match is not None:
        historical_record, historical_ref = historical_match
        candidate_id = historical_record["candidate_id"]
        new_revision = historical_record["candidate_revision"]
        record_ref = historical_ref
        decision_note = "reactivated exact registered candidate binding"
    else:
        candidate_id = new_id()
        highest_committed_revision = max(
            (record["candidate_revision"] for record, _record_ref in committed_candidates),
            default=0,
        )
        new_revision = max(state["candidate_revision"], highest_committed_revision) + 1
        candidate = {
            "schema_version": 2,
            "created_at": utc_now(),
            "created_by": dict(stamp),
            "candidate_id": candidate_id,
            "source_revision": state["source_revision"],
            "candidate_revision": new_revision,
            "source_ids": source_ids,
            "authoring": authoring,
            "model": model_ref,
            "local_modules": modules,
            "data_id": args["data_id"],
            "instance_parameter_object_id": args.get("instance_parameter_object_id"),
            "backend_id": args["backend_id"],
            "objective_plan": args["objective_plan"],
            "business_checker": checker_ref,
            "input_closure": args["input_closure"],
            "closure_note": args["closure_note"],
            "binding_digest": binding_digest,
        }
        record_ref = store.write_record(f"{candidate_root}{candidate_id}.json", candidate)
        decision_note = "registered candidate bytes and bindings"

    def commit_candidate(fence: _SessionRouteFence) -> dict[str, Any]:
        def apply(current: dict[str, Any]) -> None:
            fence.validate_current(current, stamp, require_no_pending=False)
            if current["source_revision"] != state["source_revision"]:
                raise ContractError("stale_revision", "source changed while candidate was registered")
            current["candidate_id"] = candidate_id
            current["candidate_revision"] = new_revision
            current["promotion_generation"] += 1
            current["decisions"].append({"kind": "candidate_change", "note": decision_note, "refs": [record_ref], "stamp": dict(stamp), "created_at": utc_now()})

        return store.cas_case(case_id, expected, apply)

    updated = _with_session_route_commit(
        store,
        state,
        stamp,
        commit_candidate,
        require_no_pending=False,
    )
    return {"state": _full_state_summary(store, updated), "changed_source": False, "changed_candidate": True, "candidate": object_summary(candidate_id, record_ref, "registered", state["source_revision"], new_revision)}, case_id, updated["state_revision"]


def _revise_handoff(
    store: CaseStore,
    state: Mapping[str, Any],
    args: Mapping[str, Any],
    context: Mapping[str, Any],
) -> tuple[dict[str, Any], str, int]:
    stamp = context["stamp"]
    if state["owner_session_id"] == stamp["session_id"]:
        raise ContractError("invalid_argument", "handoff requires a different receiving session")
    route = _route_freshness(store, state)
    if route["member"] is None:
        raise ContractError("capture_unavailable", "handoff requires the prior owner's known registered route member")
    unresolved_guards = [guard for guard in route.get("guards", []) if guard["unresolved"]]
    if route["unknown"] and not unresolved_guards:
        raise ContractError("capture_unavailable", "handoff cannot proceed while prior input coverage is unknown without a verified guard")
    if route.get("reason") in {"route_index_invalid", "route_gap_invalid", "route_barrier_invalid"}:
        raise ContractError("capture_unavailable", "handoff cannot proceed with an invalid prior route record")
    transfer = _context_capture(context, args["capture_id"])
    capture, raw = validate_capture_transfer(transfer, expected_session_id=stamp["session_id"])
    if not raw.strip() or capture["admission"] not in {"received_prompt", "accepted_steer"}:
        raise ContractError("capture_unconfirmed", "handoff needs a non-empty accepted real capture")
    raw_ref = store.capture_source_bytes(raw)
    expected = args["expected_state_revision"]
    carried_gap_refs = [guard["gap_ref"] for guard in unresolved_guards]
    old_session_id = state["owner_session_id"]
    session_ids = sorted(
        {old_session_id, stamp["session_id"]},
        key=lambda session_id: hashlib.sha256(session_id.encode("utf-8")).hexdigest(),
    )

    def apply(current: dict[str, Any], fence: _SessionRouteFence) -> None:
        fence.validate_current(
            current,
            stamp,
            require_no_pending=False,
            allow_unknown_route=bool(unresolved_guards),
            require_owner=False,
        )
        if current["owner_session_id"] != old_session_id:
            raise ContractError("stale_revision", "case owner changed before handoff")
        if current["pending_inputs"]:
            raise ContractError("input_unclassified", "prior owner must dispose of pending input before handoff")
        for owner_session in session_ids:
            queue_pending, queue_error = fence.handoff_control_status(owner_session)
            if queue_pending or queue_error is not None:
                raise ContractError(
                    "capture_unavailable",
                    "handoff waits for the prior or receiving session's targeted controls to be resolved",
                    details={"reason": queue_error},
                )
        current["owner_session_id"] = stamp["session_id"]
        current["promotion_generation"] += 1
        if any(item.get("capture_id") == capture["capture_id"] for item in current["pending_inputs"]):
            raise ContractError("bridge_protocol_error", "handoff capture is already fenced on this case")
        current["pending_inputs"].append({
            "session_id": capture["session_id"],
            "capture_id": capture["capture_id"],
            "arrival_epoch": capture["arrival_epoch"],
            "arrival_seq": capture["arrival_seq"],
            "channel": capture["channel"],
            "admission": capture["admission"],
            "raw_ref": dict(raw_ref),
            "disposition": "unclassified",
            "note": "",
        })
        refs = [dict(raw_ref), *(dict(ref) for ref in carried_gap_refs)]
        current["decisions"].append({
            "kind": "handoff",
            "note": args["reason"],
            "refs": refs,
            "capture_binding": {
                "session_id": capture["session_id"],
                "capture_id": capture["capture_id"],
                "arrival_epoch": capture["arrival_epoch"],
                "arrival_seq": capture["arrival_seq"],
                "channel": capture["channel"],
                "admission": capture["admission"],
                "raw_ref": dict(raw_ref),
            },
            "stamp": dict(stamp),
            "created_at": utc_now(),
        })

    def commit_handoff(fence: _SessionRouteFence) -> dict[str, Any]:
        def apply_with_fence(current: dict[str, Any]) -> None:
            apply(current, fence)

        return store.cas_case(state["case_id"], expected, apply_with_fence)

    updated = _with_session_route_commit(
        store,
        state,
        stamp,
        commit_handoff,
        require_no_pending=False,
        allow_unknown_route=bool(unresolved_guards),
        expected_route_predicate=_route_predicate_projection(route),
        extra_session_ids=session_ids,
    )
    return {"state": _full_state_summary(store, updated), "changed_source": False, "changed_candidate": False}, state["case_id"], updated["state_revision"]


def _case_current_records(state: dict[str, Any]) -> dict[str, Any]:
    records = state.get("current_records")
    if records is None:
        records = {"questions": {}, "reviews": {}, "runs": {}, "presentation": None}
        state["current_records"] = records
    return records


def _publish_family_pointer(state: dict[str, Any], family: str, object_id: str, ref: Mapping[str, Any]) -> None:
    _case_current_records(state)[family][object_id] = dict(ref)


def _read_question(store: CaseStore, state: Mapping[str, Any], request_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    question, ref = store.read_family_record(
        state["case_id"],
        "questions",
        request_id,
        state=state,
        legacy_path=f".reaslab/optagent/cases/{state['case_id']}/questions/{request_id}.json",
    )
    if question.get("request_id") != request_id:
        raise ContractError("artifact_changed", "question identity does not match its registered ID")
    return question, ref


def _latest_handoff_capture_binding(store: CaseStore, state: Mapping[str, Any], session_id: str) -> dict[str, Any]:
    for decision in reversed(state.get("decisions", [])):
        if not isinstance(decision, Mapping) or decision.get("kind") != "handoff":
            continue
        raw_stamp = decision.get("stamp")
        if (
            not isinstance(raw_stamp, Mapping)
            or raw_stamp.get("role") != "root"
            or not isinstance(raw_stamp.get("session_id"), str)
            or not raw_stamp.get("session_id")
        ):
            raise ContractError("question_stale", "handoff history has no verifiable Owner Stamp")
        if raw_stamp["session_id"] != session_id:
            continue
        try:
            stamp = require_stamp(raw_stamp, pointer="/decision/stamp")
            binding = decision.get("capture_binding")
            if stamp["role"] != "root" or stamp["session_id"] != session_id or not isinstance(binding, dict) or set(binding) != {
                "session_id", "capture_id", "arrival_epoch", "arrival_seq", "channel", "admission", "raw_ref"
            }:
                raise ContractError("question_stale", "latest Owner handoff has no valid capture binding")
            capture_id = require_id(binding["capture_id"], pointer="/decision/capture_binding/capture_id")
            epoch = require_id(binding["arrival_epoch"], pointer="/decision/capture_binding/arrival_epoch")
            arrival_seq = require_int(binding["arrival_seq"], pointer="/decision/capture_binding/arrival_seq", minimum=1)
            if binding["session_id"] != session_id or binding["channel"] not in {"prompt", "steer"}:
                raise ContractError("question_stale", "latest Owner handoff capture identity is invalid")
            if binding["admission"] not in {"received_prompt", "accepted_steer"}:
                raise ContractError("question_stale", "latest Owner handoff capture admission is invalid")
            if (binding["channel"], binding["admission"]) not in {("prompt", "received_prompt"), ("steer", "accepted_steer")}:
                raise ContractError("question_stale", "latest Owner handoff capture channel does not match its admission")
            raw_ref = validate_ref_shape(binding["raw_ref"], pointer="/decision/capture_binding/raw_ref")
            refs = [validate_ref_shape(item) for item in decision.get("refs", [])]
            if raw_ref not in refs or refs.count(raw_ref) != 1:
                raise ContractError("question_stale", "latest Owner handoff does not cite its raw capture Ref exactly once")
            raw = store.read_ref(raw_ref)
            if not raw.strip():
                raise ContractError("question_stale", "latest Owner handoff capture is empty")
            return {
                "session_id": session_id,
                "capture_id": capture_id,
                "arrival_epoch": epoch,
                "arrival_seq": arrival_seq,
            }
        except (ContractError, TypeError, ValueError) as exc:
            if isinstance(exc, ContractError) and exc.code == "question_stale":
                raise
            raise ContractError("question_stale", "latest Owner handoff capture binding is invalid") from exc
    raise ContractError("question_stale", "no verified handoff baseline is available for this Owner")


def _ask_question(store: CaseStore, state: Mapping[str, Any], args: Mapping[str, Any], context: Mapping[str, Any]) -> tuple[dict[str, Any], str, int]:
    case_id = state["case_id"]
    stamp = context["stamp"]
    capture_rows = [
        validate_capture_transfer(item, expected_session_id=stamp["session_id"])[0]
        for item in context["captures"]
    ]
    if not capture_rows:
        raise ContractError("capture_unavailable", "question needs a real input arrival sequence")
    latest = max(capture_rows, key=lambda item: item["arrival_seq"])
    request_id = args.get("request_id")
    prior: dict[str, Any] | None = None
    superseded: dict[str, Any] | None = None
    existing_ref: dict[str, Any] | None = None
    superseded_ref: dict[str, Any] | None = None
    if request_id is not None and request_id in state["question_ids"]:
        existing, existing_ref = _read_question(store, state, request_id)
        if existing["status"] != "pending":
            raise ContractError("question_stale", "question is no longer pending")
        same_meaning = (
            existing["text"] == args["text"]
            and existing["missing_fact"] == args["missing_fact"]
            and existing["blocks"] == args["blocks"]
            and existing["independent_work"] == args["independent_work"]
        )
        if not same_meaning:
            superseded = copy.deepcopy(existing)
            superseded["status"] = "superseded"
            superseded["question_version"] += 1
            superseded_ref = existing_ref
        elif existing["source_revision"] != state["source_revision"] or existing["candidate_revision"] != state["candidate_revision"] or existing["owner_session_id"] != stamp["session_id"]:
            note = args.get("rebind_note")
            if not note:
                raise ContractError("question_stale", "changed bindings need an explicit same-meaning rebind")
            prior = copy.deepcopy(existing)
            if existing["owner_session_id"] != stamp["session_id"]:
                binding = _latest_handoff_capture_binding(store, state, stamp["session_id"])
                prior["asked_after"] = {
                    "session_id": binding["session_id"],
                    "arrival_epoch": binding["arrival_epoch"],
                    "arrival_seq": binding["arrival_seq"],
                }
            elif not isinstance(existing.get("asked_after"), dict) or not isinstance(existing["asked_after"].get("session_id"), str):
                raise ContractError("question_stale", "legacy question arrival baseline cannot be inferred; create a new question")
            prior["source_revision"] = state["source_revision"]
            prior["candidate_revision"] = state["candidate_revision"]
            prior["owner_session_id"] = stamp["session_id"]
            prior["question_version"] += 1
            prior["rebinding_notes"].append(note)
        else:
            raise ContractError("invalid_argument", "question is already pending with this meaning")
    request_id = prior["request_id"] if prior is not None else (
        new_id() if request_id is None or request_id in state["question_ids"] else request_id
    )
    question = prior or {
        "request_id": request_id,
        "question_version": 1,
        "status": "pending",
        "owner_session_id": stamp["session_id"],
        "asked_by": dict(stamp),
        "text": args["text"],
        "missing_fact": args["missing_fact"],
        "blocks": list(args["blocks"]),
        "independent_work": list(args["independent_work"]),
        "source_revision": state["source_revision"],
        "candidate_revision": state["candidate_revision"],
        "asked_after": {"session_id": latest["session_id"], "arrival_epoch": latest["arrival_epoch"], "arrival_seq": latest["arrival_seq"]},
        "answer_source_id": None,
        "answer_interpretation": None,
        "rebinding_notes": [],
    }
    question_ref = store.write_family_version(case_id, "questions", request_id, question)
    superseded_version_ref = store.write_family_version(case_id, "questions", superseded["request_id"], superseded) if superseded is not None else None

    def apply(current: dict[str, Any]) -> None:
        if prior is None:
            records = current.get("current_records") or {"questions": {}, "reviews": {}, "runs": {}, "presentation": None}
            if request_id in current["question_ids"] or request_id in records["questions"]:
                raise ContractError("question_stale", "question ID was registered concurrently")
            current["question_ids"].append(request_id)
        else:
            latest, latest_ref = _read_question(store, current, request_id)
            if latest_ref != existing_ref or latest.get("status") != "pending":
                raise ContractError("question_stale", "question changed while its rebind was being recorded")
        _publish_family_pointer(current, "questions", request_id, question_ref)
        if superseded is not None:
            latest_prior, latest_ref = _read_question(store, current, superseded["request_id"])
            if latest_prior.get("status") != "pending" or latest_ref != superseded_ref:
                raise ContractError("question_stale", "prior question changed while replacement was being recorded")
            assert superseded_version_ref is not None
            _publish_family_pointer(current, "questions", superseded["request_id"], superseded_version_ref)

    updated = store.cas_case(case_id, args["expected_state_revision"], apply)
    return {"state": _full_state_summary(store, updated), "question": question}, case_id, updated["state_revision"]


def _question_turn_context_for_answer(
    question: Mapping[str, Any],
    question_ref: Mapping[str, Any],
    parsed_capture: Mapping[str, Any],
    stamp: Mapping[str, str],
    raw_context: Any,
) -> dict[str, Any] | None:
    if (
        stamp.get("role") != "root"
        or parsed_capture.get("channel") != "prompt"
        or parsed_capture.get("admission") != "received_prompt"
    ):
        return None
    asked_by_value = question.get("asked_by")
    if not isinstance(asked_by_value, Mapping):
        return None
    try:
        asked_by = require_stamp(asked_by_value, pointer="/question/asked_by")
    except ContractError:
        # Legacy or malformed anchors do not block an otherwise valid answer;
        # they simply cannot select native assistant context.
        return None
    if (
        asked_by["role"] != "root"
        or asked_by["session_id"] != stamp.get("session_id")
        or asked_by["turn_id"] == stamp.get("turn_id")
    ):
        return None
    retained = _validate_question_turn_context(
        raw_context,
        pointer="/context/question_turn_context",
    )
    if retained is None or asked_by["turn_id"] in retained["unavailable_turn_ids"]:
        return None
    messages = [
        message
        for message in retained["messages"]
        if message["source"]["id"]["turn_id"] == asked_by["turn_id"]
    ]
    if not messages:
        return None
    return {
        "kind": "question_turn_context",
        "capture_binding": {
            "session_id": parsed_capture["session_id"],
            "capture_id": parsed_capture["capture_id"],
            "arrival_epoch": parsed_capture["arrival_epoch"],
            "arrival_seq": parsed_capture["arrival_seq"],
            "channel": parsed_capture["channel"],
            "admission": parsed_capture["admission"],
            "sha256": parsed_capture["sha256"],
        },
        "question_ref": dict(question_ref),
        "asked_by": asked_by,
        "assistant_family_omitted": retained["assistant_family_omitted"],
        "messages": messages,
    }


def _answer_question(store: CaseStore, state: Mapping[str, Any], args: Mapping[str, Any], context: Mapping[str, Any]) -> tuple[dict[str, Any], str, int]:
    case_id = state["case_id"]
    stamp = context["stamp"]
    request_id = args["request_id"]
    if request_id not in state["question_ids"]:
        raise ContractError("not_found", "question does not belong to this case")
    question, question_ref = _read_question(store, state, request_id)
    if question["status"] != "pending" or question["question_version"] != args["expected_question_version"]:
        raise ContractError("question_stale", "question version is no longer pending")
    if question["owner_session_id"] != stamp["session_id"]:
        raise ContractError("permission_denied", "question belongs to another receiving session")
    capture = _context_capture(context, args["capture_id"])
    parsed, raw = validate_capture_transfer(capture, expected_session_id=stamp["session_id"])
    if not raw.strip() or parsed["admission"] not in {"received_prompt", "accepted_steer"}:
        raise ContractError("capture_unconfirmed", "answer must cite a non-empty prompt or accepted steer")
    asked_after = question.get("asked_after")
    if (
        not isinstance(asked_after, dict)
        or asked_after.get("session_id") != parsed["session_id"]
        or isinstance(asked_after.get("arrival_seq"), bool)
        or not isinstance(asked_after.get("arrival_seq"), int)
        or parsed["arrival_seq"] <= asked_after["arrival_seq"]
    ):
        raise ContractError("question_stale", "answer capture arrived before the question")
    if not any(item["capture_id"] == parsed["capture_id"] and item.get("session_id") == parsed["session_id"] for item in state["pending_inputs"]):
        raise ContractError("capture_unavailable", "answer capture has not been materialized for this case")
    changed_binding = question["source_revision"] != state["source_revision"] or question["candidate_revision"] != state["candidate_revision"]
    if changed_binding and not args.get("rebind_note"):
        raise ContractError("question_stale", "changed binding needs an explicit same-meaning answer rebind")
    question_turn_context = _question_turn_context_for_answer(
        question,
        question_ref,
        parsed,
        stamp,
        context.get("question_turn_context"),
    )
    local_import = next(
        (item for item in context.get("local_imports", []) if item.get("capture_id") == parsed["capture_id"]),
        None,
    )
    if local_import is not None:
        stable_read = store.read_user_file(local_import["workspace_path"])
        if (
            stable_read[0] != raw
            or len(stable_read[0]) != local_import["byte_len"]
            or sha256_bytes(stable_read[0]) != local_import["sha256"]
        ):
            raise ContractError("artifact_changed", "local answer file changed after the explicit import")
        source, source_ref = capture_file_source(
            store,
            case_id=case_id,
            path=local_import["workspace_path"],
            purpose="requirement",
            locations=[],
            stamp=stamp,
            stable_read=stable_read,
        )
    else:
        source, source_ref = source_from_capture(
            store,
            case_id=case_id,
            purpose="requirement",
            capture_transfer=capture,
            stamp=stamp,
            conversation_context=question_turn_context,
        )
    fact = {
        "fact_id": new_id(),
        "text": args["interpretation"],
        "source_ids": [source["source_id"]],
        "interpretation": args["interpretation"],
    }
    next_question = copy.deepcopy(question)
    next_question["status"] = "answered"
    next_question["question_version"] += 1
    next_question["answer_source_id"] = source["source_id"]
    next_question["answer_interpretation"] = args["interpretation"]
    if changed_binding:
        next_question["rebinding_notes"].append(args["rebind_note"])
    next_question["source_revision"] = state["source_revision"] + 1
    next_question["candidate_revision"] = state["candidate_revision"]
    next_question_ref = store.write_family_version(case_id, "questions", request_id, next_question)

    def apply(current: dict[str, Any]) -> None:
        latest, latest_ref = _read_question(store, current, request_id)
        if latest_ref != question_ref or latest["question_version"] != args["expected_question_version"] or latest["status"] != "pending":
            raise ContractError("question_stale", "question changed while answer was being recorded")
        _publish_family_pointer(current, "questions", request_id, next_question_ref)
        current["source_ids"].append(source["source_id"])
        current["facts"].append(fact)
        current["source_revision"] += 1
        current["promotion_generation"] += 1
        _classify_capture(current, parsed["capture_id"], "no_math_change", "accepted as the recorded answer source", stamp, record_decision=False)
        current["decisions"].append({"kind": "source_change", "note": "accepted answer to " + request_id, "refs": [source_ref], "stamp": dict(stamp), "created_at": utc_now()})

    updated = store.cas_case(case_id, args["expected_state_revision"], apply)
    return {
        "state": _full_state_summary(store, updated),
        "question": next_question,
        "source": _object_summary(source["source_id"], source_ref, "captured", updated["source_revision"], None),
        "fact": fact,
    }, case_id, updated["state_revision"]


def _withdraw_question(store: CaseStore, state: Mapping[str, Any], args: Mapping[str, Any], stamp: Mapping[str, str]) -> dict[str, Any]:
    case_id = state["case_id"]
    request_id = args["request_id"]
    if request_id not in state["question_ids"]:
        raise ContractError("question_stale", "question does not belong to this case")
    for ref in args["evidence_refs"]:
        store.read_ref(ref)
    question, question_ref = _read_question(store, state, request_id)
    if question["status"] != "pending" or question["question_version"] != args["expected_question_version"]:
        raise ContractError("question_stale", "only the current pending question version can be withdrawn")
    changed = copy.deepcopy(question)
    changed["status"] = "withdrawn"
    changed["question_version"] += 1
    changed_ref = store.write_family_version(case_id, "questions", request_id, changed)

    def apply(current: dict[str, Any]) -> None:
        latest, latest_ref = _read_question(store, current, request_id)
        if latest_ref != question_ref or latest["status"] != "pending" or latest["question_version"] != args["expected_question_version"]:
            raise ContractError("question_stale", "question changed while it was being withdrawn")
        _publish_family_pointer(current, "questions", request_id, changed_ref)
        current["decisions"].append({"kind": "question_withdrawal", "note": args["reason"], "refs": list(args["evidence_refs"]), "stamp": dict(stamp), "created_at": utc_now()})

    return store.cas_case(case_id, args["expected_state_revision"], apply)


def _context_capture(context: Mapping[str, Any], capture_id: str) -> dict[str, Any]:
    matches = [
        item
        for item in context.get("captures", [])
        if isinstance(item, dict) and item.get("capture_id") == capture_id
    ]
    if not matches:
        raise ContractError("capture_unavailable", "capture is not present in the trusted context")
    parsed: list[dict[str, Any]] = []
    for value in matches:
        transfer, _ = validate_capture_transfer(value, expected_session_id=context["stamp"]["session_id"])
        parsed.append(transfer)
    if any(item != parsed[0] for item in parsed[1:]):
        raise ContractError("bridge_protocol_error", "capture ID has conflicting trusted transfers")
    return parsed[0]


def _capture_anywhere(
    store: CaseStore,
    capture_id: str,
    context: Mapping[str, Any],
    state: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        return _context_capture(context, capture_id)
    except ContractError as exc:
        if exc.code != "capture_unavailable":
            raise
    session_id = state["owner_session_id"]
    fence = next((item for item in state["pending_inputs"] if item["capture_id"] == capture_id), None)
    if fence is not None and fence.get("raw_ref") is not None:
        try:
            raw = store.read_ref(fence["raw_ref"])
        except ContractError as exc:
            raise ContractError("capture_unavailable", "stored capture bytes are unavailable for this case") from exc
        return _transfer_from_stored_raw(fence, raw, session_id)
    for source_id in state["source_ids"]:
        source = store.read_record(f".reaslab/optagent/cases/{state['case_id']}/sources/{source_id}.json")
        origin = source.get("origin", {})
        if origin.get("capture_id") != capture_id:
            continue
        if origin.get("session_id") != session_id:
            raise ContractError("permission_denied", "capture belongs to another owner session")
        try:
            raw = store.read_ref(source["bytes"])
        except ContractError as exc:
            raise ContractError("capture_unavailable", "stored source capture bytes are unavailable") from exc
        stored = {
            "capture_id": capture_id,
            "arrival_epoch": origin.get("arrival_epoch"),
            "arrival_seq": origin.get("arrival_seq"),
            "channel": origin.get("channel"),
            "admission": origin.get("transport_admission"),
        }
        return _transfer_from_stored_raw(stored, raw, session_id)
    raise ContractError("capture_unavailable", "capture bytes are no longer available for this case")


def _persisted_source_capture(
    store: CaseStore,
    state: Mapping[str, Any],
    capture_id: str,
    session_id: str,
    *,
    missing_ok: bool = False,
) -> dict[str, Any] | None:
    """Reconstruct only a capture already committed through this case's Source chain."""
    case_id = state["case_id"]
    prefix = f".reaslab/optagent/cases/{case_id}/sources/"
    records: dict[str, dict[str, Any] | None] = {
        prefix + source_id + ".json": None for source_id in state["source_ids"]
    }
    for decision in state.get("decisions", []):
        if not isinstance(decision, dict) or decision.get("kind") != "source_change":
            continue
        refs = decision.get("refs")
        if not isinstance(refs, list):
            continue
        for raw_ref in refs:
            try:
                ref = validate_ref_shape(raw_ref, pointer="/decision/refs")
            except ContractError:
                continue
            path = ref["path"]
            if not path.startswith(prefix) or not path.endswith(".json"):
                continue
            source_id = require_id(path[len(prefix) : -5], pointer="/decision/refs")
            if path == prefix + source_id + ".json" and ref["media_type"] == "application/json":
                records[path] = ref

    candidates: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for path, record_ref in records.items():
        if record_ref is None:
            source = store.read_record(path)
        else:
            try:
                raw_record = store.read_ref(record_ref)
                source = strict_json_loads(raw_record, max_bytes=2 * 1024 * 1024)
            except (ContractError, ValueError, UnicodeError) as exc:
                raise ContractError("capture_unavailable", "committed source record is unavailable for re-adoption") from exc
        source_id = require_id(source.get("source_id"), pointer="/source/source_id") if isinstance(source, dict) else None
        if source_id is None or path != prefix + source_id + ".json":
            raise ContractError("capture_unavailable", "committed source record identity is invalid")
        origin = source.get("origin")
        if not isinstance(origin, dict) or origin.get("capture_id") != capture_id:
            continue
        created_by = require_stamp(source.get("created_by"), pointer="/source/created_by")
        if created_by["role"] != "root" or origin.get("session_id") != session_id:
            raise ContractError("capture_unavailable", "source capture belongs to another owner session")
        bytes_ref = validate_ref_shape(source.get("bytes"), pointer="/source/bytes")
        if (
            bytes_ref["path"] != f".reaslab/optagent/source-bytes/{bytes_ref['sha256']}"
            or bytes_ref["media_type"] != "application/octet-stream"
        ):
            raise ContractError("capture_unavailable", "source capture bytes have a noncanonical reference")
        raw = store.read_ref(bytes_ref)
        if (
            not raw
            or source.get("original_sha256") != bytes_ref["sha256"]
            or source.get("original_size") != len(raw)
            or bytes_ref["byte_len"] != len(raw)
            or source.get("kind") not in {"external_prompt", "external_steer"}
            or origin.get("workspace_path") is not None
        ):
            raise ContractError("capture_unavailable", "persisted source bytes do not match the capture origin")
        metadata = {
            "session_id": origin.get("session_id"),
            "capture_id": capture_id,
            "arrival_epoch": origin.get("arrival_epoch"),
            "arrival_seq": origin.get("arrival_seq"),
            "channel": origin.get("channel"),
            "admission": origin.get("transport_admission"),
        }
        transfer = _transfer_from_stored_raw(metadata, raw, session_id)
        parsed, _ = validate_capture_transfer(transfer, expected_session_id=session_id)
        if parsed["admission"] not in {"received_prompt", "accepted_steer"}:
            raise ContractError("capture_unavailable", "unconfirmed source capture cannot be re-adopted")
        identity = (
            parsed["capture_id"],
            parsed["arrival_epoch"],
            parsed["arrival_seq"],
            parsed["channel"],
            parsed["admission"],
            parsed["sha256"],
            len(raw),
        )
        candidates.append((identity, transfer))
    if not candidates:
        if missing_ok:
            return None
        raise ContractError("capture_unavailable", "capture has no committed source record for re-adoption")
    identity = candidates[0][0]
    if any(candidate_identity != identity for candidate_identity, _ in candidates[1:]):
        raise ContractError("capture_unavailable", "committed source records conflict on capture identity")
    return candidates[0][1]


def _transfer_from_stored_raw(
    metadata: Mapping[str, Any],
    raw: bytes,
    session_id: str,
) -> dict[str, Any]:
    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ContractError("capture_unavailable", "stored capture is not valid UTF-8") from exc
    origin_session_id = metadata.get("session_id")
    if not isinstance(origin_session_id, str) or not origin_session_id:
        raise ContractError("capture_unavailable", "stored capture has no verified origin session")
    if origin_session_id != session_id:
        raise ContractError("permission_denied", "stored capture belongs to another session")
    transfer = {
        "capture_id": metadata["capture_id"],
        "arrival_epoch": metadata["arrival_epoch"],
        "arrival_seq": metadata["arrival_seq"],
        "session_id": origin_session_id,
        "channel": metadata["channel"],
        "admission": metadata["admission"],
        "utf8_bytes_base64": __import__("base64").b64encode(raw).decode("ascii"),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    parsed, _ = validate_capture_transfer(transfer, expected_session_id=session_id)
    return parsed


def _classify_capture(
    state: dict[str, Any],
    capture_id: str,
    disposition: str,
    reason: str,
    stamp: Mapping[str, str] | None = None,
    *,
    record_decision: bool = True,
) -> None:
    fence = next((item for item in state["pending_inputs"] if item["capture_id"] == capture_id), None)
    if fence is None:
        raise ContractError("capture_unavailable", "capture has no pending input fence")
    capture_hash = fence.get("raw_ref", {}).get("sha256") if isinstance(fence.get("raw_ref"), dict) else None
    if disposition == "defer":
        fence["disposition"] = "deferred"
        fence["note"] = reason[:512]
    elif disposition in {"no_math_change", "superseded"}:
        state["pending_inputs"] = [item for item in state["pending_inputs"] if item["capture_id"] != capture_id]
    else:
        raise ContractError("invalid_argument", "input disposition is invalid")
    if record_decision:
        if stamp is None:
            raise ContractError("bridge_protocol_error", "input disposition is missing its trusted stamp")
        state["decisions"].append(
            {
                "kind": "input_classified",
                "note": f"{capture_id}: {disposition}; sha256={capture_hash or 'unavailable'}; {reason[:320]}",
                "refs": [],
                "stamp": dict(stamp),
                "created_at": utc_now(),
            }
        )


def _captures_for_case(
    store: CaseStore,
    context: Mapping[str, Any],
    state: Mapping[str, Any],
) -> list[dict[str, Any]]:
    context_transfers: dict[str, tuple[dict[str, Any], bytes]] = {}
    for transfer in context["captures"]:
        parsed, raw = validate_capture_transfer(transfer, expected_session_id=state["owner_session_id"])
        context_transfers[parsed["capture_id"]] = (parsed, raw)
    transfers: dict[str, dict[str, Any]] = {}
    for fence in state["pending_inputs"]:
        if fence.get("session_id") != state["owner_session_id"]:
            continue
        if fence["capture_id"] in context_transfers:
            parsed, raw = context_transfers[fence["capture_id"]]
            if parsed["arrival_seq"] != fence["arrival_seq"] or parsed["arrival_epoch"] != fence["arrival_epoch"]:
                raise ContractError("capture_unavailable", "context capture does not match the case input fence")
            if fence.get("raw_ref") is not None and parsed["sha256"] != fence["raw_ref"]["sha256"]:
                raise ContractError("capture_unavailable", "context bytes do not match the case input fence")
            adjusted = dict(parsed)
            adjusted["admission"] = fence["admission"]
            transfers[fence["capture_id"]], _ = validate_capture_transfer(
                adjusted,
                expected_session_id=state["owner_session_id"],
            )
        elif fence.get("raw_ref") is not None:
            transfers[fence["capture_id"]] = _transfer_from_stored_raw(
                fence,
                store.read_ref(fence["raw_ref"]),
                state["owner_session_id"],
            )
    return list(transfers.values())


def _input_page(transfers: Sequence[Mapping[str, Any]], args: Mapping[str, Any]) -> dict[str, Any]:
    after_seq = args.get("after_arrival_seq", 0)
    rows = [
        (transfer["arrival_seq"], capture_summary(transfer))
        for transfer in transfers
        if transfer["arrival_seq"] > after_seq
    ]
    rows.sort(key=lambda item: (item[0], item[1]["capture_id"]))
    limit = args.get("limit", 20)
    total = len(rows)
    page = rows[:limit]
    return {
        "captures": [item[1] for item in page],
        "total": total,
        "truncated": total > limit,
        "next_after": page[-1][0] if total > limit and page else None,
    }


def _full_state_summary(store: CaseStore, state: Mapping[str, Any]) -> dict[str, Any]:
    summary = state_summary(store, state)
    summary["current_validity"] = _current_validity(store, state)
    return summary


def _current_validity(store: CaseStore, state: Mapping[str, Any]) -> str:
    if _route_freshness(store, state)["unknown"]:
        return "unclassified"
    if state["pending_inputs"]:
        return "unclassified"
    try:
        source_view = _check_pending_and_current_inputs(store, state)
    except ContractError as exc:
        if exc.code == "input_unclassified":
            return "unclassified"
        return "stale"
    delivery_id = state.get("selected_delivery_id")
    if delivery_id is None:
        return "none"
    try:
        delivery, selection_ref = _load_delivery(store, state["case_id"], delivery_id)
        if (
            delivery.get("delivery_id") != delivery_id
            or delivery.get("source_revision") != state["source_revision"]
            or delivery.get("candidate_id") != state["candidate_id"]
            or delivery.get("promotion_generation") != state["promotion_generation"]
        ):
            return "stale"
        _check_delivery_binding(store, state, delivery, math_source_ids=source_view["math_source_ids"])
        _load_presentation(store, state, delivery_id, selection_ref, delivery=delivery)
        _check_delivery_authorities(store, state, delivery)
        for ref in delivery.get("evidence_refs", []):
            store.read_ref(ref)
        return "valid"
    except ContractError as exc:
        if exc.code == "input_unclassified":
            return "unclassified"
        return "stale"


def _next_actions(store: CaseStore, state: Mapping[str, Any], validity: str) -> list[str]:
    actions: list[str] = []
    if state["pending_inputs"]:
        actions.append("Classify or defer pending input before relying on selected evidence.")
    if any(
        _read_question(store, state, request_id)[0].get("status") == "pending"
        for request_id in state["question_ids"]
    ):
        actions.append("Review pending questions against the current source and candidate bindings.")
    if state["candidate_id"] is None:
        actions.append("Register a candidate when model authoring is needed; source-first proof work can continue independently.")
    run_ids = state.get("run_ids", [])
    if run_ids:
        latest_run_id = run_ids[-1]
        latest_run, _ = store.read_family_record(
            state["case_id"],
            "runs",
            latest_run_id,
            state=state,
            legacy_path=f".reaslab/optagent/cases/{state['case_id']}/runs/{latest_run_id}/record.json",
        )
        if latest_run.get("dispatch_state") in {"requested", "unknown"}:
            local_process = latest_run.get("local_process")
            receipt_ref = local_process.get("receipt") if isinstance(local_process, dict) else None
            stop_confirmed = latest_run.get("cancel", {}).get("stop_ack") is not None
            if not stop_confirmed and isinstance(receipt_ref, dict):
                receipt = strict_json_loads(store.read_ref(receipt_ref))
                stop_confirmed = isinstance(receipt, dict) and receipt.get("stop_confirmed") is True
            if stop_confirmed:
                actions.append(
                    f"The latest Run {latest_run_id} has no complete valid result, but its local process tree is confirmed stopped. For another attempt, prepare new input and name that exact Run ID with a nonblank attempt_reason."
                )
            else:
                actions.append(
                    f"The latest Run {latest_run_id} is unresolved. If you choose another attempt, prepare new input and name that exact Run ID with a nonblank attempt_reason; this does not prove the earlier execution stopped."
                )
    if validity == "stale":
        actions.append("Recheck the changed inputs and select evidence for the current bindings.")
    elif validity == "none":
        actions.append("Select claim-scoped evidence when there is evidence ready to present.")
    return actions


def _list_cases(store: CaseStore) -> list[dict[str, Any]]:
    try:
        cases_dir = store.resolve_path(".reaslab/optagent/cases", must_exist=True)
    except ContractError as exc:
        if exc.code == "not_found":
            return []
        raise
    entries: list[dict[str, Any]] = []
    for path in cases_dir.iterdir():
        try:
            verified_path = store.resolve_path(f".reaslab/optagent/cases/{path.name}", must_exist=True)
        except ContractError as exc:
            if exc.code == "not_found":
                continue
            raise
        if not verified_path.is_dir():
            continue
        case_id = path.name
        try:
            require_id(case_id, pointer="/case_id")
            state = store.read_case(case_id)
        except ContractError as exc:
            if exc.code in {"not_found", "legacy_read_only"}:
                continue
            raise
        summary = _full_state_summary(store, state)
        entries.append({
            "case_id": summary["case_id"],
            "title": summary["title"],
            "artifact_dir": summary["artifact_dir"],
            "owner_session_id": summary["owner_session_id"],
            "state_revision": summary["state_revision"],
            "source_revision": summary["source_revision"],
            "candidate_revision": summary["candidate_revision"],
            "current_validity": summary["current_validity"],
        })
    return sorted(entries, key=lambda item: item["case_id"])


def _require_root(stamp: Mapping[str, Any]) -> None:
    if stamp.get("role") != "root":
        raise ContractError("permission_denied", "only root may perform this operation")


def _require_owner(state: Mapping[str, Any], stamp: Mapping[str, Any]) -> None:
    _require_root(stamp)
    if state.get("owner_session_id") != stamp.get("session_id"):
        raise ContractError("permission_denied", "only the current owner session may change this case")


def _require_read_role(stamp: Mapping[str, Any]) -> None:
    if stamp.get("role") not in {"root", "opt-verifier", "opt-worker"}:
        raise ContractError("permission_denied", "trusted read role is required")


def _object_summary(object_id: str, record_ref: Mapping[str, Any], status: str, source_revision: int | None, candidate_revision: int | None) -> dict[str, Any]:
    return object_summary(object_id, record_ref, status, source_revision, candidate_revision)

