"""Frozen native-run preparation and the finite run transport bridge.

Preparation only reads, copies and hashes declared bytes. Candidate Python is
not imported here; it is imported by the isolated worker after the host grants
the one persisted dispatch attempt.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .contracts import (
    ContractError,
    canonical_json_bytes,
    decode_base64,
    public_failure,
    public_success,
    require_hash,
    require_id,
    require_int,
    require_stamp,
    sha256_bytes,
    strict_json_loads,
    validate_ref_shape,
)
from .data import load_data_bundle, read_prepared_source_records
from .delivery import _check_pending_and_current_inputs
from .results import (
    NATIVE_CP_SAT_METHOD,
    NATIVE_CP_SAT_SCOPE,
    _result_numerical_summary,
    checked_global_bound_gap,
    checked_lexicographic_lock,
    objective_values_exactly_match,
    objective_values_match,
    validate_direct_witness_record,
    business_check_is_fully_passed,
    validate_native_cp_sat_model_contract,
)
from .sources import new_id, utc_now
from .store import CaseStore
from .worker import _LEGACY_RUNTIME_FILES, verify_runtime_bundle


_RUNTIME_FILES = (
    "python/optagent_core/__init__.py",
    "python/optagent_core/contracts.py",
    "python/optagent_core/data.py",
    "python/optagent_core/models.py",
    "python/optagent_core/results.py",
    "python/optagent_core/solver_protocol.py",
    "python/optagent_core/worker.py",
)


_RAW_BOUND_UNSET = object()
_BOUND_SUPPORTING_TERMINATIONS = {"optimal", "time_limit", "iteration_limit", "feasible"}
_PYTHON_EXECUTE_HTTP_TIMEOUT_GRACE_SECONDS = 30
_PREPARED_FIELDS = {
    "schema_version",
    "prepared_id",
    "case_id",
    "source_revision",
    "candidate_revision",
    "candidate_id",
    "promotion_generation",
    "binding_digest",
    "sources",
    "candidate",
    "data",
    "runtime_bundle",
    "solve_options",
}
_INVOCATION_FIELDS = {
    "schema_version",
    "stamp",
    "case_id",
    "prepared_id",
    "run_id",
    "dispatch_nonce",
    "input_manifest",
    "input_digest",
    "output_path",
}
_RECEIPT_FIELDS = {
    "http_status",
    "body_base64",
    "body_complete",
    "error_code",
    "send_observation",
}
_RESTRICTED_PATH_PARTS = {".git", ".codex", ".env", "vendor"}
_BUSINESS_CHECKER_DIAGNOSTIC_PREFIX = "business_checker_diagnostic:"


def _finite_number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _json_ref(store: CaseStore, path: str, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    raw, ref = store.read_workspace_file(path)
    try:
        value = strict_json_loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ContractError("output_invalid", f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise ContractError("output_invalid", f"{label} must be an object")
    return value, ref


def _safe_parts(value: Any, *, pointer: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ContractError("path_denied", "path must be a safe workspace-relative path", pointer=pointer)
    path = PurePosixPath(value)
    parts = tuple(path.parts)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ContractError("path_denied", "path escapes the workspace", pointer=pointer)
    if parts and parts[0].casefold() == ".reaslab":
        raise ContractError("path_denied", "path points into a reserved workspace area", pointer=pointer)
    if any(part.casefold() in _RESTRICTED_PATH_PARTS for part in parts):
        raise ContractError("path_denied", "path points into a reserved workspace area", pointer=pointer)
    return parts


def _checked_solve_options(value: Any) -> dict[str, Any]:
    fields = {
        "execution_budget_seconds",
        "time_limit_seconds",
        "relative_gap",
        "absolute_gap",
        "threads",
        "random_seed",
        "feasibility_tolerance",
        "integrality_tolerance",
        "write_log",
        "export_model",
    }
    if not isinstance(value, dict):
        raise ContractError("invalid_argument", "solve options must be an object", pointer="/solve_options")
    for name in ("execution_budget_seconds", "relative_gap", "absolute_gap"):
        if name not in value:
            raise ContractError(
                "invalid_argument",
                f"solve_options.{name} is required for this Run unless a saved setting supplies it",
                pointer=f"/solve_options/{name}",
            )
    if frozenset(value) not in {frozenset(fields), frozenset(fields | {"warm_start"})}:
        raise ContractError("invalid_argument", "solve options have invalid fields", pointer="/solve_options")
    warm_start = value.get("warm_start", False)
    if type(warm_start) is not bool:
        raise ContractError("invalid_argument", "warm_start must be a boolean", pointer="/solve_options/warm_start")
    for name in ("execution_budget_seconds", "feasibility_tolerance", "integrality_tolerance"):
        number = value[name]
        if not _finite_number(number):
            raise ContractError("invalid_argument", "solve option must be finite", pointer=f"/solve_options/{name}")
    total = float(value["execution_budget_seconds"])
    if total <= 30:
        raise ContractError("invalid_argument", "execution budget must reserve 30 seconds for finalization", pointer="/solve_options/execution_budget_seconds")
    cap = value["time_limit_seconds"]
    if cap is not None and (not _finite_number(cap) or float(cap) <= 0):
        raise ContractError("invalid_argument", "native time limit must be null or finite and positive", pointer="/solve_options/time_limit_seconds")
    for name in ("relative_gap", "absolute_gap"):
        number = value[name]
        if not _finite_number(number) or number < 0:
            raise ContractError("invalid_argument", "Core gap target must be finite and nonnegative", pointer=f"/solve_options/{name}")
    if value["threads"] is not None and (isinstance(value["threads"], bool) or not isinstance(value["threads"], int) or value["threads"] < 1):
        raise ContractError("invalid_argument", "threads must be null or a positive integer", pointer="/solve_options/threads")
    if value["random_seed"] is not None and (isinstance(value["random_seed"], bool) or not isinstance(value["random_seed"], int) or value["random_seed"] < 0):
        raise ContractError("invalid_argument", "random seed must be null or a nonnegative integer", pointer="/solve_options/random_seed")
    if float(value["feasibility_tolerance"]) < 1e-10 or float(value["integrality_tolerance"]) < 1e-10:
        raise ContractError("invalid_argument", "model-check feasibility and integrality tolerances must be at least 1e-10")
    if not isinstance(value["write_log"], bool) or not isinstance(value["export_model"], bool):
        raise ContractError("invalid_argument", "log and model export options must be boolean")
    checked = dict(value)
    checked["warm_start"] = warm_start
    return checked


def _checked_legacy_solve_options(value: Any) -> dict[str, Any]:
    """Validate the sealed schema-2 option contract without reinterpreting it."""
    fields = {
        "execution_budget_seconds", "time_limit_seconds", "relative_gap", "absolute_gap",
        "threads", "random_seed", "feasibility_tolerance", "integrality_tolerance", "write_log", "export_model",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ContractError("output_invalid", "legacy sealed solve options have invalid fields")
    for name in ("execution_budget_seconds", "time_limit_seconds", "feasibility_tolerance", "integrality_tolerance"):
        if not _finite_number(value[name]):
            raise ContractError("output_invalid", "legacy sealed solve option is not finite", pointer=f"/solve_options/{name}")
    if float(value["execution_budget_seconds"]) <= 30 or float(value["time_limit_seconds"]) <= 0 or float(value["time_limit_seconds"]) > float(value["execution_budget_seconds"]) - 30:
        raise ContractError("output_invalid", "legacy sealed solve budget is invalid")
    for name in ("relative_gap", "absolute_gap"):
        if value[name] is not None and (not _finite_number(value[name]) or value[name] < 0):
            raise ContractError("output_invalid", "legacy sealed gap option is invalid", pointer=f"/solve_options/{name}")
    if isinstance(value["threads"], bool) or not isinstance(value["threads"], int) or value["threads"] < 1:
        raise ContractError("output_invalid", "legacy sealed thread option is invalid")
    if isinstance(value["random_seed"], bool) or not isinstance(value["random_seed"], int) or value["random_seed"] < 0:
        raise ContractError("output_invalid", "legacy sealed seed option is invalid")
    if not isinstance(value["write_log"], bool) or not isinstance(value["export_model"], bool):
        raise ContractError("output_invalid", "legacy sealed artifact options are invalid")
    return dict(value)


def _store_ref_copy(store: CaseStore, source_ref: Mapping[str, Any], target: str) -> dict[str, Any]:
    ref = validate_ref_shape(dict(source_ref))
    raw = store.read_ref(ref)
    return store.write_immutable(target, raw, ref["media_type"])


def _freeze_source_bytes(
    store: CaseStore,
    *,
    case_id: str,
    prepared_id: str,
    source_ids: Sequence[str],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, dict[str, Any]]]]:
    prefix = f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/input"
    manifest_sources: list[dict[str, Any]] = []
    records: dict[str, dict[str, Any]] = {}
    byte_refs: dict[str, dict[str, dict[str, Any]]] = {}
    for source_id in source_ids:
        record_path = f".reaslab/optagent/cases/{case_id}/sources/{source_id}.json"
        record, _ = _json_ref(store, record_path, "source record")
        if record.get("source_id") != source_id:
            raise ContractError("artifact_changed", "source record identity does not match its case binding")
        original_ref = validate_ref_shape(record.get("bytes"), pointer="/source/bytes")
        source_bytes = store.read_ref(original_ref)
        frozen_bytes = store.write_immutable(
            f"{prefix}/sources/{source_id}/bytes.bin",
            source_bytes,
            original_ref["media_type"],
        )
        frozen_record = dict(record)
        frozen_record["bytes"] = frozen_bytes
        frozen_record_ref = store.write_immutable(
            f"{prefix}/sources/{source_id}/record.json",
            canonical_json_bytes(frozen_record),
            "application/json",
        )
        manifest_sources.append({"source_id": source_id, "record": frozen_record_ref, "bytes": frozen_bytes})
        records[source_id] = frozen_record
        byte_refs[source_id] = {"original": original_ref, "frozen": frozen_bytes}
    return manifest_sources, records, byte_refs


def _freeze_code(store: CaseStore, prefix: str, ref_value: Any, *, pointer: str) -> dict[str, Any]:
    ref = validate_ref_shape(ref_value, pointer=pointer)
    parts = _safe_parts(ref["path"], pointer=f"{pointer}/path")
    if PurePosixPath(ref["path"]).suffix != ".py":
        raise ContractError("capability_uncovered", "native code closure members must be Python source files", pointer=f"{pointer}/path")
    raw = store.read_ref(ref)
    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ContractError("output_invalid", "Python source must be UTF-8", pointer=pointer) from exc
    frozen = store.write_immutable(
        f"{prefix}/source/{'/'.join(parts)}",
        raw,
        ref["media_type"],
    )
    return {"original_path": "/".join(parts), "bytes": frozen}


def _freeze_data_bundle(
    store: CaseStore,
    *,
    case_id: str,
    prepared_id: str,
    candidate: Mapping[str, Any],
    data_id: str,
    source_byte_refs: Mapping[str, Mapping[str, Mapping[str, Any]]],
    frozen_source_records: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    original_path = f".reaslab/optagent/cases/{case_id}/data/{data_id}/manifest.json"
    original_ref = store.ref_for_path(original_path)
    frozen_prefix = f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/input/data"
    # Re-read through the same verified Ref used by the loader. Reading the
    # workspace path here could silently accept a replacement manifest.
    manifest_raw = store.read_ref(original_ref)
    try:
        manifest = strict_json_loads(manifest_raw, max_bytes=len(manifest_raw))
    except (ValueError, UnicodeError) as exc:
        raise ContractError("output_invalid", "data manifest is not strict JSON") from exc
    if not isinstance(manifest, dict):
        raise ContractError("output_invalid", "data manifest must be an object")
    if manifest.get("data_id") != data_id or not isinstance(manifest.get("source_ids"), list):
        raise ContractError("artifact_changed", "data manifest identity or source list is invalid")
    source_ids = manifest["source_ids"]
    if not source_ids or len(set(source_ids)) != len(source_ids):
        raise ContractError("output_invalid", "data manifest source list is empty or repeats")
    if any(source_id not in source_byte_refs for source_id in source_ids):
        raise ContractError("input_closure_unknown", "data manifest names a Source outside the frozen input")
    material = {key: value for key, value in manifest.items() if key != "digest"}
    if manifest.get("digest") != sha256_bytes(canonical_json_bytes(material)):
        raise ContractError("artifact_changed", "normalized DataManifest digest does not match its original bytes")
    if not isinstance(manifest.get("objects"), list):
        raise ContractError("output_invalid", "data manifest objects must be an array")
    frozen_manifest = dict(manifest)
    frozen_objects: list[dict[str, Any]] = []
    for item in manifest["objects"]:
        copied = dict(item)
        copied["values"] = _store_ref_copy(
            store,
            item["values"],
            f"{frozen_prefix}/objects/{item['object_id']}.json",
        )
        frozen_objects.append(copied)
    frozen_manifest["objects"] = frozen_objects

    trace_ref = validate_ref_shape(manifest["trace"], pointer="/data/trace")
    trace_raw = store.read_ref(trace_ref)
    try:
        trace = strict_json_loads(trace_raw, max_bytes=len(trace_raw))
    except (ValueError, UnicodeError) as exc:
        raise ContractError("output_invalid", "data trace is invalid") from exc
    if not isinstance(trace, dict) or not isinstance(trace.get("source_refs"), list):
        raise ContractError("output_invalid", "data trace source binding is invalid")
    if len(trace["source_refs"]) != len(source_ids):
        raise ContractError("artifact_changed", "data trace Source count differs from its manifest")
    remapped_refs: list[dict[str, Any]] = []
    for index, (source_id, old_ref_value) in enumerate(zip(source_ids, trace["source_refs"], strict=True)):
        old_ref = validate_ref_shape(old_ref_value, pointer=f"/data/trace/source_refs/{index}")
        refs = source_byte_refs[source_id]
        original_source_ref = validate_ref_shape(refs["original"], pointer=f"/sources/{source_id}/original")
        frozen_source_ref = validate_ref_shape(refs["frozen"], pointer=f"/sources/{source_id}/frozen")
        if old_ref != original_source_ref:
            raise ContractError("artifact_changed", "data trace Source Ref does not match its declared Source ID")
        remapped_refs.append(dict(frozen_source_ref))
    trace = dict(trace)
    trace["source_refs"] = remapped_refs
    frozen_trace_ref = store.write_immutable(
        f"{frozen_prefix}/trace.json",
        canonical_json_bytes(trace),
        "application/json",
    )
    frozen_manifest["trace"] = frozen_trace_ref
    data_material = {key: value for key, value in frozen_manifest.items() if key != "digest"}
    frozen_manifest["digest"] = sha256_bytes(canonical_json_bytes(data_material))
    frozen_manifest_ref = store.write_immutable(
        f"{frozen_prefix}/manifest.json",
        canonical_json_bytes(frozen_manifest),
        "application/json",
    )
    # The same Core loader validates both complete DataBundles. A prepared
    # bundle freezes every normalized row; instance selection happens later
    # through DataBundle.for_instance(parameter_id, actual_key).
    frozen_bundle = load_data_bundle(
        store,
        frozen_manifest_ref,
        candidate_binding_digest=candidate["binding_digest"],
        source_records=frozen_source_records,
    )
    if frozen_bundle.data_id != data_id:
        raise ContractError("artifact_changed", "frozen data bundle identity changed")
    parameter_object_id = candidate["instance_parameter_object_id"]
    if parameter_object_id is not None:
        frozen_bundle.table(parameter_object_id)
    return {
        "data_id": data_id,
        "manifest": frozen_manifest_ref,
        "instance_parameter_object_id": parameter_object_id,
    }


def _runtime_identity_bytes(files: Sequence[Mapping[str, Any]]) -> bytes:
    # Keep this field order stable; it is part of the runtime bundle digest.
    rows = [
        {"path": item["path"], "sha256": item["sha256"], "byte_len": item["byte_len"]}
        for item in files
    ]
    return json.dumps(
        {"schema_version": 1, "files": rows},
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _publish_runtime_bundle(store: CaseStore) -> tuple[str, dict[str, Any], tuple[dict[str, Any], ...]]:
    source_package = Path(os.path.abspath(__file__)).parent
    entries: list[dict[str, Any]] = []
    copied: list[tuple[str, bytes]] = []
    for relative in _RUNTIME_FILES:
        package_relative = relative.removeprefix("python/")
        source_path = source_package.parent.joinpath(*PurePosixPath(package_relative).parts)
        if not source_path.is_file():
            # Installed wheels place the fixed worker closure beside this module
            # rather than under a source checkout's `python/` directory.
            source_path = source_package / PurePosixPath(package_relative).name
        try:
            source_info = source_path.lstat()
            if stat.S_ISLNK(source_info.st_mode) or not stat.S_ISREG(source_info.st_mode):
                raise ContractError("path_denied", "runtime helper is not a regular source file")
            raw = source_path.read_bytes()
            after = source_path.lstat()
        except OSError as exc:
            raise ContractError("environment_unavailable", "fixed native worker source is unavailable") from exc
        if (source_info.st_size, source_info.st_mtime_ns, source_info.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ContractError("source_changed_during_read", "runtime helper changed while it was copied")
        entries.append({"path": relative, "sha256": sha256_bytes(raw), "byte_len": len(raw)})
        copied.append((relative, raw))
    if tuple(item["path"] for item in entries) != _RUNTIME_FILES:
        raise ContractError("input_closure_unknown", "native runtime allowlist ordering changed")
    digest = sha256_bytes(_runtime_identity_bytes(entries))
    root = f".reaslab/optagent/runtime/{digest}"
    helper_refs: list[dict[str, Any]] = []
    for item, (relative, raw) in zip(entries, copied, strict=True):
        helper_ref = store.write_immutable(
            f"{root}/{relative}",
            raw,
            "text/x-python; charset=utf-8",
        )
        helper_refs.append(helper_ref)
        if helper_ref["sha256"] != item["sha256"] or helper_ref["byte_len"] != item["byte_len"]:
            raise ContractError("artifact_changed", "published runtime helper reference differs from its bytes")
    manifest_value = {"schema_version": 1, "digest": digest, "files": entries}
    manifest_raw = canonical_json_bytes(manifest_value)
    manifest_ref = store.write_immutable(f"{root}/manifest.json", manifest_raw, "application/json")
    store.write_immutable(f"{root}/COMPLETE", digest.encode("ascii"), "text/plain; charset=utf-8")
    _, verified_refs = verify_runtime_bundle(store.workspace, digest, manifest_ref)
    if [dict(ref) for ref in verified_refs] != helper_refs:
        raise ContractError("artifact_changed", "published native runtime closure failed byte verification")
    return digest, manifest_ref, tuple(helper_refs)


def _prepare(
    store: CaseStore,
    args: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    from optagent_core.api import _full_state_summary, _object_summary, _require_owner, _require_route_current

    stamp = require_stamp(context["stamp"])
    case_id = require_id(args["case_id"], pointer="/case_id")
    prepared_id = new_id()
    expected_revision = args["expected_state_revision"]
    options = _checked_solve_options(args["solve_options"])
    state = store.read_case(case_id)
    _require_owner(state, stamp)
    _require_route_current(store, state)
    if state["state_revision"] != expected_revision:
        raise ContractError("stale_revision", "case revision changed", details={"actual": state["state_revision"]})
    if context.get("cancellation_observed"):
        raise ContractError("execution_unknown", "prepare was cancelled before freezing inputs")
    source_view = _check_pending_and_current_inputs(
        store, state, verify_source_bytes=False, verify_candidate_data=False
    )
    if state.get("candidate_id") != args["candidate_id"]:
        raise ContractError("candidate_dirty", "requested candidate is not the current case candidate")
    candidate_path = f".reaslab/optagent/cases/{case_id}/candidates/{args['candidate_id']}.json"
    candidate, _ = _json_ref(store, candidate_path, "candidate record")
    math_source_ids = source_view["math_source_ids"]
    if (
        candidate.get("candidate_id") != args["candidate_id"]
        or candidate.get("source_revision") != state["source_revision"]
        or candidate.get("candidate_revision") != state["candidate_revision"]
        or candidate.get("source_ids") != math_source_ids
    ):
        raise ContractError("candidate_dirty", "candidate revisions or source set are stale")
    from .solver_protocol import (
        DIRECT_PYTHON_BACKEND_ID,
        SUPPORTED_BACKEND_IDS,
        validate_direct_python_options,
        validate_solver_options,
    )
    backend_id = candidate.get("backend_id")
    if backend_id == DIRECT_PYTHON_BACKEND_ID:
        if candidate.get("authoring") != "direct_python":
            raise ContractError("artifact_changed", "direct backend and candidate entry family differ")
        if candidate["objective_plan"].get("mode") == "lexicographic":
            raise ContractError("capability_uncovered", "direct_python supports scalar and feasibility objective plans only", pointer="/candidate/objective_plan/mode")
        validate_direct_python_options(options)
    else:
        if candidate.get("authoring") != "pyomo":
            raise ContractError("capability_uncovered", "native solver runs require the Pyomo candidate entry family")
        if backend_id not in SUPPORTED_BACKEND_IDS:
            raise ContractError("capability_uncovered", "candidate names an unsupported solver backend", pointer="/candidate/backend_id")
        validate_solver_options(backend_id, options)
    configured_backend = context.get("default_solver_backend")
    override_reason = None
    if configured_backend is not None:
        if configured_backend not in SUPPORTED_BACKEND_IDS:
            raise ContractError("bridge_protocol_error", "trusted default solver is not a registered native backend")
        if backend_id != configured_backend:
            supplied_reason = args.get("solver_override_reason")
            if not isinstance(supplied_reason, str) or not supplied_reason.strip() or len(supplied_reason) > 512:
                raise ContractError(
                    "invalid_argument",
                    f"candidate backend {backend_id!r} differs from configured default {configured_backend!r}; provide solver_override_reason for this Run",
                    pointer="/solver_override_reason",
                    details={"field": "solver_override_reason", "configured_default": configured_backend, "candidate_backend": backend_id},
                )
            override_reason = supplied_reason.strip()
    if candidate.get("input_closure") != "declared":
        raise ContractError("capability_uncovered", "candidate local module closure is not declared")
    binding_digest = require_hash(candidate.get("binding_digest"), pointer="/candidate/binding_digest")

    prefix = f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/input"
    sources, source_records, source_byte_refs = _freeze_source_bytes(
        store,
        case_id=case_id,
        prepared_id=prepared_id,
        source_ids=candidate["source_ids"],
    )
    model_code = _freeze_code(store, prefix, candidate["model"], pointer="/candidate/model")
    local_modules = [
        _freeze_code(store, prefix, ref, pointer=f"/candidate/local_modules/{index}")
        for index, ref in enumerate(candidate["local_modules"])
    ]
    checker = None
    if candidate["business_checker"] is not None:
        checker = _freeze_code(store, prefix, candidate["business_checker"], pointer="/candidate/business_checker")
    requirements_file = None
    try:
        requirements_raw, requirements_source_ref = store.read_workspace_file("requirements.txt")
    except ContractError as exc:
        if exc.code != "not_found":
            details = dict(exc.details)
            details.update({"operation": "optagent.run_prepare", "field": "requirements_file", "path": "requirements.txt"})
            raise ContractError(
                exc.code,
                f"{exc.message} (operation=optagent.run_prepare, field=requirements_file, path=requirements.txt)",
                pointer="/requirements.txt",
                details=details,
            ) from exc
    else:
        try:
            requirements_raw.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ContractError(
                "output_invalid",
                "root requirements.txt must be UTF-8",
                pointer="/requirements.txt",
                details={"operation": "optagent.run_prepare", "field": "requirements_file", "path": "requirements.txt"},
            ) from exc
        for line_number, line in enumerate(requirements_raw.decode("utf-8").splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith(("-r ", "-r\t", "--requirement ", "--requirement\t", "--requirement=", "-c ", "-c\t", "--constraint ", "--constraint\t", "--constraint=")):
                raise ContractError(
                    "capability_uncovered",
                    "requirements include and constraint files are not in the frozen root-file dependency closure",
                    pointer="/requirements.txt",
                    details={
                        "operation": "optagent.run_prepare",
                        "field": "requirements_file",
                        "path": "requirements.txt",
                        "line": line_number,
                    },
                )
        frozen_requirements_ref = store.write_immutable(
            f"{prefix}/requirements.txt",
            requirements_raw,
            requirements_source_ref["media_type"],
        )
        requirements_file = {"path": "requirements.txt", "bytes": frozen_requirements_ref}
    frozen_candidate = {
        "authoring": candidate["authoring"],
        "model": model_code,
        "local_modules": local_modules,
        "business_checker": checker,
        "requirements_file": requirements_file,
        "backend_id": candidate["backend_id"],
        "objective_plan": candidate["objective_plan"],
        "input_closure": candidate["input_closure"],
        "closure_note": candidate["closure_note"],
    }
    data = None
    if candidate["data_id"] is not None:
        if candidate["data_id"] not in state["data_ids"]:
            raise ContractError("candidate_dirty", "candidate data bundle is no longer registered on the case")
        data = _freeze_data_bundle(
            store,
            case_id=case_id,
            prepared_id=prepared_id,
            candidate=candidate,
            data_id=candidate["data_id"],
            source_byte_refs=source_byte_refs,
            frozen_source_records=source_records,
        )
    elif candidate["instance_parameter_object_id"] is not None:
        raise ContractError("invalid_argument", "instance parameter object requires normalized data")

    runtime_digest, runtime_manifest_ref, _ = _publish_runtime_bundle(store)
    input_manifest = {
        "schema_version": 3,
        "prepared_id": prepared_id,
        "case_id": case_id,
        "source_revision": state["source_revision"],
        "candidate_revision": state["candidate_revision"],
        "candidate_id": candidate["candidate_id"],
        "promotion_generation": state["promotion_generation"],
        "binding_digest": binding_digest,
        "sources": sources,
        "candidate": frozen_candidate,
        "data": data,
        "runtime_bundle": {
            "digest": runtime_digest,
            "manifest": runtime_manifest_ref,
            "worker_entry": "python/optagent_core/worker.py",
        },
        "solve_options": options,
    }
    if set(input_manifest) != _PREPARED_FIELDS:
        raise ContractError("environment_unavailable", "internal PreparedInputManifest fields diverged from contract")
    input_raw = canonical_json_bytes(input_manifest)
    input_digest = sha256_bytes(input_raw)
    input_manifest_ref = store.write_immutable(f"{prefix}/manifest.json", input_raw, "application/json")
    if input_manifest_ref["sha256"] != input_digest:
        raise ContractError("artifact_changed", "frozen input manifest Ref does not match canonical bytes")

    prepared_record = {
        "schema_version": 2,
        "created_at": utc_now(),
        "created_by": stamp,
        "prepared_id": prepared_id,
        "case_id": case_id,
        "source_revision": state["source_revision"],
        "candidate_revision": state["candidate_revision"],
        "candidate_id": candidate["candidate_id"],
        "promotion_generation": state["promotion_generation"],
        "source_ids": list(candidate["source_ids"]),
        "input_manifest": input_manifest_ref,
        "input_digest": input_digest,
        "solve_options": options,
        "status": "frozen",
    }
    prepared_path = f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/prepared.json"
    prepared_ref = store.write_record(prepared_path, prepared_record)
    from optagent_core.api import _SessionRouteFence, _with_session_route_commit

    def commit_prepare(fence: _SessionRouteFence) -> dict[str, Any]:
        def publish(current: dict[str, Any]) -> None:
            fence.validate_current(current, stamp, require_no_pending=True)
            if (
                current["state_revision"] != expected_revision
                or current["source_revision"] != state["source_revision"]
                or current["candidate_revision"] != state["candidate_revision"]
                or current.get("candidate_id") != state.get("candidate_id")
                or current["promotion_generation"] != state["promotion_generation"]
            ):
                raise ContractError("stale_revision", "case changed while prepared inputs were being frozen")
            current["prepared_ids"].append(prepared_id)

        return store.cas_case(case_id, expected_revision, publish)

    updated = _with_session_route_commit(store, state, stamp, commit_prepare)
    return {
        "kind": "complete",
        "reply": public_success(
            "optagent.run",
            "prepare",
            case_id=case_id,
            state_revision=updated["state_revision"],
            result={
                "state": _full_state_summary(store, updated),
                "prepared": _object_summary(prepared_id, prepared_ref, "frozen", state["source_revision"], state["candidate_revision"]),
                "solve_options": options,
                "solver_selection": {
                    "backend_id": candidate["backend_id"],
                    "override_reason": override_reason,
                },
            },
        ),
    }


def _run_path(case_id: str, run_id: str) -> str:
    return f".reaslab/optagent/cases/{case_id}/runs/{run_id}/record.json"


def _load_run_record(
    store: CaseStore,
    case_id: str,
    run_id: str,
    *,
    state: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    run, ref = store.read_family_record(
        case_id,
        "runs",
        run_id,
        state=state,
        legacy_path=_run_path(case_id, run_id),
    )
    if run.get("run_id") != run_id or run.get("case_id", case_id) != case_id:
        raise ContractError("artifact_changed", "run identity does not match its Case Ref")
    return run, ref


def _latest_run_head(
    store: CaseStore,
    case_id: str,
    state: Mapping[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    run_ids = state.get("run_ids")
    if not isinstance(run_ids, list):
        raise ContractError("output_invalid", "Case run index is invalid")
    if not run_ids:
        return None
    run_id = require_id(run_ids[-1], pointer="/run_ids/-1")
    run, ref = _load_run_record(store, case_id, run_id, state=state)
    if run.get("dispatch_state") not in {"requested", "without_dispatch", "unknown", "response_received"}:
        raise ContractError("output_invalid", "latest Run dispatch state is invalid")
    return run_id, run, ref


def _publish_run_update(
    store: CaseStore,
    case_id: str,
    run_id: str,
    update: Any,
    *,
    expected_nonce: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge one run-owned change into the latest immutable snapshot.

    The family lock serializes writers for this run; Case CAS publishes only
    the current Ref and never advances the business revision. Callers must do
    slow worker/transport reads before entering this helper.
    """
    with store.run_lock(run_id):
        state = store.read_case(case_id)
        if run_id not in state.get("run_ids", []):
            raise ContractError("not_found", "run is not registered on this Case")
        latest, old_ref = _load_run_record(store, case_id, run_id, state=state)
        if expected_nonce is not None and latest.get("dispatch_nonce") != expected_nonce:
            raise ContractError("artifact_changed", "run dispatch nonce changed before update")
        next_run = copy.deepcopy(latest)
        update(next_run)
        if next_run == latest:
            return latest, old_ref
        new_ref = store.write_family_version(case_id, "runs", run_id, next_run)

        def publish(current: dict[str, Any]) -> None:
            records = current.get("current_records")
            if records is None:
                records = {"questions": {}, "reviews": {}, "runs": {}, "presentation": None}
                current["current_records"] = records
            records["runs"][run_id] = dict(new_ref)

        store.cas_case_family(
            case_id,
            "runs",
            run_id,
            old_ref,
            publish,
            legacy_path=_run_path(case_id, run_id),
        )
        return next_run, new_ref


def _prepared_path(case_id: str, prepared_id: str) -> str:
    return f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/prepared.json"


def _read_record_with_ref(store: CaseStore, path: str, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    return _json_ref(store, path, label)


def _stamp_equal(left: Any, right: Mapping[str, Any]) -> bool:
    try:
        return require_stamp(left, pointer="/record/created_by") == dict(right)
    except ContractError:
        return False


def _object_summary(store: CaseStore, object_id: str, ref: Mapping[str, Any], status: str, source_revision: int | None, candidate_revision: int | None) -> dict[str, Any]:
    from optagent_core.delivery import object_summary

    return object_summary(object_id, ref, status, source_revision, candidate_revision)


def _state_summary(store: CaseStore, state: Mapping[str, Any]) -> dict[str, Any]:
    from optagent_core.api import _full_state_summary

    return _full_state_summary(store, state)


def _run_summary(run: Mapping[str, Any], record_ref: Mapping[str, Any]) -> dict[str, Any]:
    local = run.get("local_process")
    summary = {
        "id": run["run_id"],
        "record_ref": dict(record_ref),
        "dispatch_state": run["dispatch_state"],
        "worker_status": run["worker"]["status"],
        "execution_id_available": run["platform"]["id_binding"] == "verified" and run["platform"]["execution_id"] is not None,
        "cancel_requested": run["cancel"]["requested"],
        "result_count": len(run["results"]),
    }
    if isinstance(local, dict):
        stop_attempted = (
            run["cancel"]["requested"]
            or local.get("status") in {"stop_unconfirmed", "cancelled", "interrupted", "timed_out"}
            or local.get("stop_confirmed") is False
        )
        summary["local_stop_state"] = (
            "confirmed" if local.get("stop_confirmed") is True
            else "requested_not_confirmed" if stop_attempted
            else "not_requested"
        )
    return summary


def _load_prepared(store: CaseStore, case_id: str, prepared_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    prepared, prepared_ref = _read_record_with_ref(store, _prepared_path(case_id, prepared_id), "prepared record")
    fields = {
        "schema_version", "created_at", "created_by", "prepared_id", "case_id", "source_revision",
        "candidate_revision", "candidate_id", "promotion_generation", "source_ids", "input_manifest",
        "input_digest", "solve_options", "status",
    }
    legacy_fields = fields | {"started_run_id"}
    if frozenset(prepared) not in {frozenset(fields), frozenset(legacy_fields)} or prepared.get("schema_version") != 2 or prepared.get("status") != "frozen":
        raise ContractError("output_invalid", "prepared record fields or frozen status are invalid")
    if prepared.get("prepared_id") != prepared_id or prepared.get("case_id") != case_id:
        raise ContractError("artifact_changed", "prepared record identity does not match its path")
    input_ref = validate_ref_shape(prepared["input_manifest"], pointer="/prepared/input_manifest")
    expected_input_path = f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/input/manifest.json"
    input_raw = store.read_ref(input_ref)
    if input_ref["path"] != expected_input_path or input_ref["sha256"] != prepared.get("input_digest"):
        raise ContractError("artifact_changed", "prepared record does not bind its frozen manifest")
    try:
        manifest = strict_json_loads(input_raw)
    except (ValueError, UnicodeError) as exc:
        raise ContractError("output_invalid", "prepared input manifest is invalid") from exc
    manifest_version = manifest.get("schema_version") if isinstance(manifest, dict) else None
    if not isinstance(manifest, dict) or set(manifest) != _PREPARED_FIELDS or manifest_version not in {2, 3}:
        raise ContractError("output_invalid", "prepared input manifest fields are invalid")
    if manifest_version == 2:
        _checked_legacy_solve_options(manifest.get("solve_options"))
        if not isinstance(manifest.get("candidate"), dict) or manifest["candidate"].get("backend_id") != "highs":
            raise ContractError("capability_uncovered", "legacy sealed runs retain their original HiGHS backend")
    else:
        _checked_solve_options(manifest.get("solve_options"))
        if prepared.get("solve_options") != manifest.get("solve_options"):
            raise ContractError("artifact_changed", "prepared record solve options differ from its frozen manifest")
    if any(manifest.get(field) != prepared.get(field) for field in (
        "prepared_id", "case_id", "source_revision", "candidate_revision", "candidate_id", "promotion_generation"
    )):
        raise ContractError("artifact_changed", "prepared input manifest identity differs from its record")
    return prepared, prepared_ref, manifest


def _validate_owner_and_barrier(store: CaseStore, case_id: str, state: Mapping[str, Any], stamp: Mapping[str, Any]) -> None:
    from optagent_core.api import _require_owner, _require_route_current

    _require_owner(state, stamp)
    _check_pending_and_current_inputs(store, state)
    _require_route_current(store, state)


def _launcher_code(workspace_absolute: str, invocation_path: str, invocation_sha256: str) -> str:
    workspace_literal = json.dumps(workspace_absolute, ensure_ascii=True)
    invocation_literal = json.dumps(invocation_path, ensure_ascii=True)
    digest_literal = json.dumps(invocation_sha256, ensure_ascii=True)
    files_literal = repr(list(_RUNTIME_FILES))
    legacy_files_literal = repr(list(_LEGACY_RUNTIME_FILES))
    return "\n".join((
        "import hashlib,json,os,stat,sys",
        f"W={workspace_literal}; P={invocation_literal}; H={digest_literal}; FILES={files_literal}; LEGACY_FILES={legacy_files_literal}",
        "def sig(s): return (int(s.st_dev),int(s.st_ino),stat.S_IFMT(s.st_mode),int(s.st_size),int(s.st_mtime_ns),int(s.st_ctime_ns))",
        "def ident(s): return sig(s)[:3]",
        "def linked(s): return stat.S_ISLNK(s.st_mode) or bool(getattr(s,'st_file_attributes',0)&0x400)",
        "def read(rel):",
        "    if not isinstance(rel,str) or not rel or rel.startswith('/') or '\\\\' in rel or any(p in ('','.','..') for p in rel.split('/')): raise RuntimeError('unsafe worker path')",
        "    cur=W; chain=[]; parts=rel.split('/')",
        "    for i,part in enumerate(parts):",
        "        cur=os.path.join(cur,part); st=os.lstat(cur); last=i==len(parts)-1",
        "        if linked(st) or (not stat.S_ISREG(st.st_mode) if last else not stat.S_ISDIR(st.st_mode)): raise RuntimeError('unsafe worker path')",
        "        chain.append((cur,st,last))",
        "    fd=os.open(cur,os.O_RDONLY|getattr(os,'O_BINARY',0)|getattr(os,'O_NOFOLLOW',0))",
        "    try:",
        "        before=os.fstat(fd); pre=chain[-1][1]",
        "        if not stat.S_ISREG(before.st_mode) or ident(before)!=ident(pre): raise RuntimeError('worker opened object differs from checked path')",
        "        with os.fdopen(fd,'rb',closefd=False) as f: data=f.read()",
        "        after=os.fstat(fd)",
        "        if sig(before)!=sig(after) or len(data)!=after.st_size: raise RuntimeError('worker file changed while reading')",
        "        for path,old,last in chain:",
        "            now=os.lstat(path)",
        "            if linked(now) or ident(now)!=ident(old) or (last and sig(now)!=sig(after)): raise RuntimeError('worker path changed while reading')",
        "        return data",
        "    finally: os.close(fd)",
        "def sha(b): return hashlib.sha256(b).hexdigest()",
        "iraw=read(P)",
        "if sha(iraw)!=H: raise RuntimeError('worker invocation hash mismatch')",
        "inv=json.loads(iraw.decode('utf-8'))",
        "if set(inv)!=set(['schema_version','stamp','case_id','prepared_id','run_id','dispatch_nonce','input_manifest','input_digest','output_path']) or inv['schema_version']!=2: raise RuntimeError('worker invocation fields invalid')",
        "iref=inv['input_manifest']; mraw=read(iref['path'])",
        "if sha(mraw)!=iref['sha256'] or len(mraw)!=iref['byte_len'] or iref['sha256']!=inv['input_digest']: raise RuntimeError('prepared input manifest mismatch')",
        "pm=json.loads(mraw.decode('utf-8')); rb=pm['runtime_bundle']; d=rb['digest']; mr=rb['manifest']; root='.reaslab/optagent/runtime/'+d",
        "expected_files=LEGACY_FILES if type(pm.get('schema_version')) is int and pm['schema_version']==2 else FILES if type(pm.get('schema_version')) is int and pm['schema_version']==3 else None",
        "if expected_files is None: raise RuntimeError('prepared schema has no fixed runtime closure')",
        "if rb['worker_entry']!='python/optagent_core/worker.py' or mr['path']!=root+'/manifest.json': raise RuntimeError('worker runtime reference invalid')",
        "m=read(root+'/manifest.json')",
        "if sha(m)!=mr['sha256'] or len(m)!=mr['byte_len']: raise RuntimeError('runtime manifest reference mismatch')",
        "rm=json.loads(m.decode('utf-8'))",
        "identity={'schema_version':1,'files':[{'path':x['path'],'sha256':x['sha256'],'byte_len':x['byte_len']} for x in rm['files']]}",
        "ib=json.dumps(identity,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')",
        "if rm.get('schema_version')!=1 or rm.get('digest')!=d or sha(ib)!=d or [x.get('path') for x in rm.get('files',[])]!=expected_files: raise RuntimeError('runtime bundle identity invalid')",
        "if read(root+'/COMPLETE')!=d.encode('ascii'): raise RuntimeError('runtime bundle is incomplete')",
        "for x in rm['files']:",
        "    b=read(root+'/'+x['path'])",
        "    if sha(b)!=x['sha256'] or len(b)!=x['byte_len']: raise RuntimeError('runtime helper bytes mismatch')",
        "sys.dont_write_bytecode=True; sys.path.insert(0,os.path.join(W,root,'python'))",
        "from optagent_core.worker import run_invocation",
        "out=run_invocation(W,P,H)",
        "sys.stdout.write(json.dumps(out,separators=(',',':'),allow_nan=False)+'\\n')",
    ))


def _python_execute_request(code: str, execution_budget_seconds: Any) -> tuple[str, int, str]:
    timeout_seconds = math.ceil(float(execution_budget_seconds))
    request_body = canonical_json_bytes({"code": code, "timeout_seconds": timeout_seconds}).decode("utf-8")
    http_timeout_seconds = timeout_seconds + _PYTHON_EXECUTE_HTTP_TIMEOUT_GRACE_SECONDS
    request_digest = sha256_bytes(
        f"POST\n/api/internal/python/execute\n{request_body}\n{http_timeout_seconds}".encode("utf-8")
    )
    return request_body, http_timeout_seconds, request_digest


def _local_process_request(
    store: CaseStore,
    invocation_ref: Mapping[str, Any],
    execution_budget_seconds: Any,
    *,
    executable: str | None = None,
) -> dict[str, Any]:
    timeout_seconds = math.ceil(float(execution_budget_seconds))
    executable = os.path.abspath(sys.executable if executable is None else executable)
    arguments = [
        "-I",
        "-m",
        "optagent_core.local_worker_entry",
        str(store.workspace),
        str(invocation_ref["path"]),
        str(invocation_ref["sha256"]),
    ]
    identity = {
        "schema_version": 1,
        "executable": executable,
        "arguments": arguments,
        "timeout_seconds": timeout_seconds,
    }
    return {**identity, "request_digest": sha256_bytes(canonical_json_bytes(identity))}


def _dispatch_plan(
    store: CaseStore,
    case_id: str,
    run: Mapping[str, Any],
    prepared: Mapping[str, Any],
) -> dict[str, Any]:
    invocation_ref = validate_ref_shape(run["worker_invocation"], pointer="/run/worker_invocation")
    input_ref = validate_ref_shape(prepared["input_manifest"], pointer="/prepared/input_manifest")
    invocation = {
        "schema_version": 2,
        "stamp": dict(run["created_by"]),
        "case_id": case_id,
        "prepared_id": run["prepared_id"],
        "run_id": run["run_id"],
        "dispatch_nonce": run["dispatch_nonce"],
        "input_manifest": input_ref,
        "input_digest": prepared["input_digest"],
        "output_path": f".reaslab/optagent/cases/{case_id}/runs/{run['run_id']}/output",
    }
    invocation_raw = store.read_ref(invocation_ref)
    if invocation_raw != canonical_json_bytes(invocation):
        raise ContractError("artifact_changed", "worker invocation bytes differ from its run binding")
    frozen_local = run.get("local_process")
    if frozen_local is not None:
        if not isinstance(frozen_local, Mapping):
            raise ContractError("artifact_changed", "persisted local process identity is invalid")
        if run.get("http_request_digest") != "0" * 64:
            raise ContractError("artifact_changed", "local Run has a non-placeholder HTTP request digest")
        frozen_executable = frozen_local.get("executable")
        if not isinstance(frozen_executable, str) or not frozen_executable:
            raise ContractError("artifact_changed", "persisted local process executable is invalid")
        local_process = _local_process_request(
            store,
            invocation_ref,
            prepared["solve_options"]["execution_budget_seconds"],
            executable=frozen_executable,
        )
        identity_fields = ("schema_version", "executable", "arguments", "timeout_seconds", "request_digest")
        if any(frozen_local.get(key) != local_process[key] for key in identity_fields):
            raise ContractError("artifact_changed", "reconstructed local process digest differs from persisted run")
        return {
            "run_id": run["run_id"],
            "prepared_id": run["prepared_id"],
            "dispatch_nonce": run["dispatch_nonce"],
            "worker_invocation": invocation_ref,
            "local_process": local_process,
            "request_digest": local_process["request_digest"],
        }
    code = _launcher_code(store.workspace.as_posix(), invocation_ref["path"], invocation_ref["sha256"])
    request_body, http_timeout_seconds, request_digest = _python_execute_request(
        code, prepared["solve_options"]["execution_budget_seconds"]
    )
    if request_digest != run["http_request_digest"]:
        raise ContractError("artifact_changed", "reconstructed dispatch plan digest differs from persisted run")
    return {
        "run_id": run["run_id"],
        "prepared_id": run["prepared_id"],
        "dispatch_nonce": run["dispatch_nonce"],
        "worker_invocation": invocation_ref,
        "request": {"body_json": request_body, "http_timeout_seconds": http_timeout_seconds},
        "request_digest": request_digest,
    }


def _begin_start(store: CaseStore, args: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    stamp = require_stamp(context["stamp"])
    case_id = require_id(args["case_id"], pointer="/case_id")
    prepared_id = require_id(args["prepared_id"], pointer="/prepared_id")
    state = store.read_case(case_id)
    _validate_owner_and_barrier(store, case_id, state, stamp)
    if state["state_revision"] != args["expected_state_revision"]:
        raise ContractError("stale_revision", "case revision changed", details={"actual": state["state_revision"]})
    if context.get("cancellation_observed"):
        raise ContractError("execution_unknown", "start was cancelled before a run was created")
    if prepared_id not in state["prepared_ids"]:
        raise ContractError("not_found", "prepared input is not registered on this case")
    observed_head = _latest_run_head(store, case_id, state)
    supersedes_id = args.get("supersedes_run_id")
    attempt_reason = args.get("attempt_reason")
    if observed_head is not None and observed_head[1]["dispatch_state"] in {"requested", "unknown"}:
        latest_id, _latest_run, old_ref = observed_head
        latest_summary = _run_summary(_latest_run, old_ref)
        if supersedes_id is None:
            raise ContractError(
                "execution_unknown",
                "the latest start attempt is unresolved; explicitly identify it before starting another run",
                details={"run_id": latest_id, "run": latest_summary},
            )
        supersedes_id = require_id(supersedes_id, pointer="/supersedes_run_id")
        if supersedes_id != latest_id:
            raise ContractError(
                "stale_revision",
                "supersedes_run_id must identify the exact latest Run",
                pointer="/supersedes_run_id",
                details={"latest_run_id": latest_id, "run": latest_summary},
            )
        if not isinstance(attempt_reason, str) or not attempt_reason.strip():
            raise ContractError(
                "invalid_argument",
                "an explicit reason is required to continue after an unresolved latest attempt",
                pointer="/attempt_reason",
                details={"run": latest_summary},
            )
        attempt_reason = attempt_reason.strip()
    else:
        if supersedes_id is not None:
            supersedes_id = require_id(supersedes_id, pointer="/supersedes_run_id")
            raise ContractError(
                "invalid_argument",
                "only the latest requested or unknown Run can be explicitly superseded",
                pointer="/supersedes_run_id",
                details={
                    "run": _run_summary(observed_head[1], observed_head[2])
                } if observed_head is not None else None,
            )
        if attempt_reason is not None:
            raise ContractError(
                "invalid_argument",
                "attempt_reason requires supersedes_run_id",
                pointer="/attempt_reason",
                details={"run": _run_summary(observed_head[1], observed_head[2])} if observed_head is not None else None,
            )
        old_ref = None
    prepared, _, manifest = _load_prepared(store, case_id, prepared_id)
    started_run_id = None
    for prior_run_id in state["run_ids"]:
        prior, _ = _load_run_record(store, case_id, prior_run_id, state=state)
        if prior.get("prepared_id") == prepared_id:
            started_run_id = prior_run_id
            break
    if started_run_id is not None:
        raise ContractError("already_started", "this prepared input already has a persisted start attempt", details={"run_id": started_run_id})
    if (
        prepared["source_revision"] != state["source_revision"]
        or prepared["candidate_revision"] != state["candidate_revision"]
        or prepared["candidate_id"] != state["candidate_id"]
        or prepared["promotion_generation"] != state["promotion_generation"]
    ):
        raise ContractError("stale_revision", "prepared input no longer matches the current case binding")
    runtime = manifest["runtime_bundle"]
    verify_runtime_bundle(
        store.workspace,
        runtime["digest"],
        runtime["manifest"],
        prepared_schema_version=manifest["schema_version"],
    )
    backend_id = manifest["candidate"].get("backend_id")
    from .solver_protocol import DIRECT_PYTHON_BACKEND_ID, validate_direct_python_options
    if manifest["candidate"].get("authoring") == "direct_python":
        if backend_id != DIRECT_PYTHON_BACKEND_ID or manifest.get("schema_version") != 3:
            raise ContractError("capability_uncovered", "frozen direct_python candidate identity is invalid")
        if manifest["candidate"]["objective_plan"].get("mode") == "lexicographic":
            raise ContractError("capability_uncovered", "direct_python does not execute lexicographic objective plans")
        validate_direct_python_options(manifest["solve_options"])
    elif manifest["candidate"].get("authoring") != "pyomo":
        raise ContractError("capability_uncovered", "frozen run has an unsupported candidate entry family")
    elif manifest.get("schema_version") == 2:
        if backend_id != "highs":
            raise ContractError("capability_uncovered", "legacy sealed runs retain their original HiGHS backend")
    else:
        from .solver_protocol import SUPPORTED_BACKEND_IDS
        if backend_id not in SUPPORTED_BACKEND_IDS:
            raise ContractError("capability_uncovered", "frozen run names an unsupported solver backend")

    run_id = new_id()
    nonce = new_id()
    invocation_path = f".reaslab/optagent/cases/{case_id}/runs/{run_id}/invocation.json"
    invocation = {
        "schema_version": 2,
        "stamp": stamp,
        "case_id": case_id,
        "prepared_id": prepared_id,
        "run_id": run_id,
        "dispatch_nonce": nonce,
        "input_manifest": dict(prepared["input_manifest"]),
        "input_digest": prepared["input_digest"],
        "output_path": f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output",
    }
    invocation_ref = store.write_immutable(invocation_path, canonical_json_bytes(invocation), "application/json")
    provisional_run = {
        "schema_version": 2,
        "created_at": utc_now(),
        "created_by": stamp,
        "run_id": run_id,
        "prepared_id": prepared_id,
        "owner_session_id": state["owner_session_id"],
        "dispatch_state": "requested",
        "dispatch_nonce": nonce,
        "worker_invocation": invocation_ref,
        "http_request_digest": "0" * 64,
        "local_process": None,
        "transport": {"http_status": None, "response": None, "error_code": None},
        "platform": {"execution_id": None, "id_binding": "absent", "job_status": "unknown", "status_evidence": None},
        "worker": {"status": "not_observed", "seal": None, "solver_invoked": None},
        "cancel": {"requested": False, "evidence": None, "stop_attempt": None, "stop_ack": None},
        "results": [],
        "supersedes_run_id": None,
    }
    # Calculate the exact immutable execute body before persisting the run's digest.
    # _dispatch_plan verifies the digest; build its stable request bytes here.
    if context.get("local_mode") is True:
        local_process = _local_process_request(store, invocation_ref, prepared["solve_options"]["execution_budget_seconds"])
        provisional_run["local_process"] = {
            **local_process,
            "receipt": None,
            "status": "not_started",
            "stop_confirmed": None,
        }
        request_digest = local_process["request_digest"]
    else:
        code = _launcher_code(store.workspace.as_posix(), invocation_ref["path"], invocation_ref["sha256"])
        request_body, http_timeout_seconds, request_digest = _python_execute_request(
            code, prepared["solve_options"]["execution_budget_seconds"]
        )
        provisional_run["http_request_digest"] = request_digest
    if supersedes_id is not None:
        if observed_head is None or supersedes_id != observed_head[0] or old_ref != observed_head[2]:
            raise ContractError("stale_revision", "latest Run changed before the new attempt was prepared")
        old = observed_head[1]
        if old.get("run_id") != supersedes_id:
            raise ContractError("artifact_changed", "superseded run identity is invalid")
        if old.get("dispatch_state") not in {"requested", "unknown"}:
            raise ContractError(
                "invalid_argument",
                "only the latest requested or unknown run can be explicitly superseded",
                pointer="/supersedes_run_id",
            )
        provisional_run["supersedes_run_id"] = supersedes_id

    run_ref = store.write_family_version(case_id, "runs", run_id, provisional_run)
    try:
        from optagent_core.api import _SessionRouteFence, _with_session_route_commit

        def commit_start(fence: _SessionRouteFence) -> dict[str, Any]:
            def add_run(current: dict[str, Any]) -> None:
                fence.validate_current(current, stamp, require_no_pending=True)
                if current["owner_session_id"] != stamp["session_id"] or current["state_revision"] != state["state_revision"]:
                    raise ContractError("stale_revision", "case owner or revision changed before start was persisted")
                current_ids = current.get("run_ids", [])
                current_head_id = current_ids[-1] if current_ids else None
                current_head_ref = None
                if current_head_id is not None:
                    current_head_ref = (current.get("current_records") or {}).get("runs", {}).get(current_head_id)
                    if current_head_ref is not None:
                        current_head_ref = validate_ref_shape(dict(current_head_ref), pointer="/current_records/runs")
                if observed_head is None:
                    if current_head_id is not None:
                        raise ContractError("stale_revision", "latest Run changed before start was persisted")
                elif current_head_id != observed_head[0] or current_head_ref != observed_head[2]:
                    raise ContractError("stale_revision", "latest Run Ref changed before start was persisted")
                if prepared_id not in current["prepared_ids"] or run_id in current_ids:
                    raise ContractError("already_started", "prepared input already has a run attempt")
                records = current.get("current_records")
                if records is None:
                    records = {"questions": {}, "reviews": {}, "runs": {}, "presentation": None}
                    current["current_records"] = records
                current["run_ids"].append(run_id)
                records["runs"][run_id] = dict(run_ref)
                if supersedes_id is not None:
                    current["decisions"].append({
                        "kind": "run_superseded",
                        "note": attempt_reason.strip(),
                        "refs": [old_ref],
                        "stamp": dict(stamp),
                        "created_at": utc_now(),
                    })

            return store.cas_case(case_id, state["state_revision"], add_run)

        _with_session_route_commit(store, state, stamp, commit_start)
    except ContractError:
        # No Case Ref or plan escaped this invocation, so this orphan version
        # cannot authorize a transport send.
        raise
    # The dispatch plan is deterministic and can be reconstructed during the
    # unique recheck, but it is returned to Rust only after all local records exist.
    return {"kind": "start_plan", "plan": {
        "run_id": run_id,
        "prepared_id": prepared_id,
        "dispatch_nonce": nonce,
        "worker_invocation": invocation_ref,
        **({"local_process": local_process, "request_digest": request_digest} if context.get("local_mode") is True else {
            "request": {"body_json": request_body, "http_timeout_seconds": http_timeout_seconds},
            "request_digest": request_digest,
        }),
    }}


def _validate_ref_in_run(store: CaseStore, ref_value: Any, output_root: str, *, pointer: str) -> tuple[dict[str, Any], bytes]:
    ref = validate_ref_shape(ref_value, pointer=pointer)
    if not ref["path"].startswith(output_root + "/"):
        raise ContractError("path_denied", "worker artifact is outside this run's own-output tree", pointer=pointer)
    if ref["media_type"] == "application/json":
        return ref, store.read_ref(ref)
    path = store.resolve_path(ref["path"], must_exist=True)
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ContractError("path_denied", "worker artifact is not a regular file", pointer=pointer)
        digest = hashlib.sha256()
        byte_len = 0
        with open(path, "rb") as stream:
            opened = os.fstat(stream.fileno())
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                byte_len += len(chunk)
        after = path.lstat()
    except OSError as exc:
        raise ContractError("not_found", "worker artifact is unavailable", pointer=pointer) from exc
    if (
        stat.S_ISLNK(after.st_mode)
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or (opened.st_dev, opened.st_ino, byte_len) != (after.st_dev, after.st_ino, after.st_size)
        or byte_len != ref["byte_len"]
        or digest.hexdigest() != ref["sha256"]
    ):
        raise ContractError("artifact_changed", "worker artifact bytes do not match their reference", pointer=pointer)
    return ref, b""


def _check_instance_identity(bundle: Any, manifest: Mapping[str, Any], key_value: Any) -> str:
    if not isinstance(key_value, list) or any(type(item) not in (str, int) for item in key_value):
        raise ContractError("output_invalid", "worker instance key must contain typed string or integer values")
    data_info = manifest["data"]
    if data_info is None:
        if key_value:
            raise ContractError("output_invalid", "single-instance result must use an empty key")
        if bundle.data_id is not None:
            raise ContractError("artifact_changed", "empty PreparedInputManifest data does not match the loaded DataBundle")
        parameter_id = None
    else:
        if not isinstance(data_info, dict) or set(data_info) != {"data_id", "manifest", "instance_parameter_object_id"}:
            raise ContractError("output_invalid", "prepared data identity has invalid fields")
        if not isinstance(data_info["data_id"], str) or not data_info["data_id"]:
            raise ContractError("output_invalid", "prepared data identity is invalid")
        if data_info["instance_parameter_object_id"] is not None and not isinstance(data_info["instance_parameter_object_id"], str):
            raise ContractError("output_invalid", "prepared instance parameter identity is invalid")
        if bundle.data_id != data_info["data_id"]:
            raise ContractError("artifact_changed", "worker DataBundle identity differs from PreparedInputManifest")
        parameter_id = data_info["instance_parameter_object_id"]
    instance = bundle.for_instance(parameter_id, key_value)
    context = instance.instance_context
    if context is None or list(context.instance_key) != key_value:
        raise ContractError("artifact_changed", "Core DataBundle did not produce the expected instance context")
    return context.instance_binding_digest


def _validate_identity_triplet(
    value: Any,
    *,
    input_digest: str,
    key: list[str | int],
    binding_digest: str,
    expected_fields: set[str],
    label: str,
    optional_fields: set[str] | None = None,
) -> dict[str, Any]:
    allowed_fields = expected_fields | (optional_fields or set())
    actual_fields = set(value) if isinstance(value, dict) else set()
    if not isinstance(value, dict) or not expected_fields.issubset(actual_fields) or not actual_fields.issubset(allowed_fields):
        raise ContractError("output_invalid", f"worker {label} has invalid fields")
    if value.get("input_digest") != input_digest or value.get("instance_key") != key or value.get("instance_binding_digest") != binding_digest:
        raise ContractError("artifact_changed", f"worker {label} is bound to a different input instance")
    return value


def _planned_objective_sense(plan: Mapping[str, Any], layer: Any) -> str | None:
    mode = plan.get("mode")
    if mode == "scalar" and layer is None:
        sense = plan.get("sense")
        return sense if sense in {"min", "max"} else None
    if mode == "lexicographic":
        layers = plan.get("layers")
        if isinstance(layer, int) and not isinstance(layer, bool) and isinstance(layers, list) and 0 <= layer < len(layers):
            descriptor = layers[layer]
            if isinstance(descriptor, dict) and descriptor.get("sense") in {"min", "max"}:
                return descriptor["sense"]
    return None


def _validate_normalized_bound_and_gap(
    result: Mapping[str, Any],
    check_record: Mapping[str, Any],
    objective_plan: Mapping[str, Any],
    *,
    raw_bound_value: Any = _RAW_BOUND_UNSET,
) -> None:
    """Independently check Result bound/gap against its frozen checked point."""
    bound = result["bound"]
    objective_sense = _planned_objective_sense(objective_plan, result["layer"])
    checked_objectives = check_record.get("objective_values")
    if not isinstance(checked_objectives, list):
        raise ContractError("output_invalid", "ModelCheck objective values are invalid")

    checked_objective: Mapping[str, Any] | None = None
    if check_record.get("status") == "pass" and len(checked_objectives) == 1:
        candidate_objective = checked_objectives[0]
        if (
            not isinstance(candidate_objective, dict)
            or candidate_objective.get("sense") != objective_sense
            or not _finite_number(candidate_objective.get("value"))
        ):
            raise ContractError("output_invalid", "checked objective is invalid for global bound validation")
        checked_objective = candidate_objective

    if bound is not None:
        expected_direction = "lower" if objective_sense == "min" else "upper" if objective_sense == "max" else None
        if expected_direction is None:
            raise ContractError("output_invalid", "Result global bound has no matching objective sense")
        if bound["direction"] != expected_direction:
            raise ContractError("artifact_changed", "Result bound direction differs from its checked objective sense")
        if checked_objective is not None:
            if checked_global_bound_gap(bound["value"], objective_sense, checked_objective["value"]) is None:
                raise ContractError("output_invalid", "Result global bound conflicts with its checked feasible objective")

    if raw_bound_value is not _RAW_BOUND_UNSET:
        expected_bound: float | None
        if not _finite_number(raw_bound_value):
            expected_bound = None
        elif checked_objective is None:
            expected_bound = (
                float(raw_bound_value)
                if result.get("termination") in _BOUND_SUPPORTING_TERMINATIONS
                else None
            )
        elif result.get("termination") not in _BOUND_SUPPORTING_TERMINATIONS:
            expected_bound = None
        else:
            expected_bound = (
                float(raw_bound_value)
                if checked_global_bound_gap(
                    raw_bound_value,
                    objective_sense or "",
                    checked_objective["value"],
                ) is not None
                else None
            )

        if bound is not None:
            if expected_bound is None or bound["value"] != expected_bound:
                raise ContractError("artifact_changed", "Result global bound differs from the strict raw native bound")

    gap = result["gap"]
    if not isinstance(gap, dict) or set(gap) != {"absolute", "relative", "definition"} or not isinstance(gap["definition"], str):
        raise ContractError("output_invalid", "Result gap fields are invalid")
    for field in ("absolute", "relative"):
        value = gap[field]
        if value is not None and (
            not _finite_number(value)
            or value < 0
        ):
            raise ContractError("output_invalid", "Result gap values must be null or finite nonnegative numbers")
    expected_gap = (None, None)
    if bound is not None and result["has_solver_incumbent"]:
        if check_record.get("status") != "pass" or len(checked_objectives) != 1:
            raise ContractError("output_invalid", "solver incumbent has no complete checked objective")
        checked_objective = checked_objectives[0]
        expected_gap = checked_global_bound_gap(
            bound["value"],
            checked_objective["sense"],
            checked_objective["value"],
        )
        if expected_gap is None:
            raise ContractError("output_invalid", "Result gap cannot use a contradictory global bound")
    if (gap["absolute"], gap["relative"]) != expected_gap:
        raise ContractError("artifact_changed", "Result gap does not match its normalized global bound and checked objective")


def _validate_schema3_backend_observation(
    *,
    observation: Mapping[str, Any],
    result: Mapping[str, Any],
    manifest: Mapping[str, Any],
    check_record: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Cross-check the new solver DTO against frozen requests and public Result fields."""
    required = {
        "termination", "raw_status", "solver_invoked", "solver_returned", "raw_solution_value_valid",
        "best_feasible_objective", "best_objective_bound", "bound_evidence", "termination_condition",
        "solve_wall_seconds", "error", "requested_options", "effective_options", "option_set_statuses",
        "core_gap_criterion",
    }
    if not required.issubset(observation):
        raise ContractError("output_invalid", "schema-3 backend observation is incomplete")
    if (
        observation.get("termination") != result.get("termination")
        or observation.get("raw_status") != result.get("raw_status")
        or observation.get("solver_invoked") is not result.get("solver_invoked")
    ):
        raise ContractError("artifact_changed", "Result solver status differs from its raw backend observation")
    for name in ("solver_invoked", "solver_returned"):
        if not isinstance(observation.get(name), bool):
            raise ContractError("output_invalid", f"backend observation {name} must be boolean")
    if observation["solver_returned"] and not observation["solver_invoked"]:
        raise ContractError("artifact_changed", "backend returned a solver result without invoking the solver")
    raw_valid = observation.get("raw_solution_value_valid")
    if raw_valid is not None and type(raw_valid) is not bool:
        raise ContractError("output_invalid", "backend raw solution validity is invalid")
    for name in ("best_feasible_objective", "best_objective_bound"):
        value = observation.get(name)
        if value is not None and not _finite_number(value):
            raise ContractError("output_invalid", f"backend {name} must be null or finite")
    if observation.get("best_feasible_objective") is not None and raw_valid is not True:
        raise ContractError("artifact_changed", "backend reported a feasible objective without a valid raw solution")
    elapsed = observation.get("solve_wall_seconds")
    if not _finite_number(elapsed) or elapsed < 0:
        raise ContractError("output_invalid", "backend solve duration must be finite and nonnegative")
    if observation.get("termination_condition") is not None and not isinstance(observation.get("termination_condition"), str):
        raise ContractError("output_invalid", "backend termination condition must be a string or null")
    if observation.get("error") is not None and not isinstance(observation.get("error"), str):
        raise ContractError("output_invalid", "backend error must be a string or null")

    requested = observation.get("requested_options")
    effective = observation.get("effective_options")
    statuses = observation.get("option_set_statuses")
    core_gap = observation.get("core_gap_criterion")
    native_fields = {"time_limit_seconds", "threads", "random_seed", "warm_start", "parameters"}
    core_fields = {"relative_gap", "absolute_gap", "feasibility_tolerance", "integrality_tolerance"}
    effective_fields = native_fields
    status_fields = {
        "time_limit_seconds", "threads", "random_seed", "warm_start", "relative_gap", "absolute_gap",
        "feasibility_tolerance", "integrality_tolerance",
    }
    if (
        not isinstance(requested, dict)
        or set(requested) != {"native", "core"}
        or not isinstance(requested.get("native"), dict)
        or set(requested["native"]) != native_fields
        or not isinstance(requested.get("core"), dict)
        or set(requested["core"]) != core_fields
        or not isinstance(effective, dict)
        or set(effective) != effective_fields
        or not isinstance(statuses, dict)
        or set(statuses) != status_fields
    ):
        raise ContractError("output_invalid", "backend option observation has invalid fields")
    if not isinstance(requested["native"]["parameters"], dict) or not isinstance(effective["parameters"], dict):
        raise ContractError("output_invalid", "backend native parameter observations must be objects")
    for parameter_map in (requested["native"]["parameters"], effective["parameters"]):
        if any(
            not isinstance(name, str)
            or not name
            or not (value is None or isinstance(value, (str, bool)) or _finite_number(value))
            for name, value in parameter_map.items()
        ):
            raise ContractError("output_invalid", "backend native parameter values must be finite JSON scalars")
    frozen_options = manifest.get("solve_options")
    if not isinstance(frozen_options, dict):
        raise ContractError("output_invalid", "frozen solver options are missing")
    expected_native = {
        "time_limit_seconds": frozen_options.get("time_limit_seconds"),
        "threads": frozen_options.get("threads"),
        "random_seed": frozen_options.get("random_seed"),
    }
    for name, frozen_value in expected_native.items():
        requested_value = requested["native"][name]
        if name == "time_limit_seconds":
            valid = requested_value is None or (
                _finite_number(requested_value)
                and requested_value > 0
                and _finite_number(frozen_value)
                and float(requested_value) == float(frozen_value)
            )
            matches = requested_value is None if frozen_value is None else bool(valid and requested_value is not None)
        elif name == "threads":
            valid = requested_value is None or (type(requested_value) is int and requested_value >= 1)
            matches = requested_value is None if frozen_value is None else valid and requested_value == frozen_value
        else:
            valid = requested_value is None or (type(requested_value) is int and requested_value >= 0)
            matches = requested_value is None if frozen_value is None else valid and requested_value == frozen_value
        if not valid or not matches:
            raise ContractError("artifact_changed", "backend native requests differ from frozen solve options")
    expected_warm_start = frozen_options.get("warm_start", False)
    requested_warm_start = requested["native"]["warm_start"]
    if type(expected_warm_start) is not bool or type(requested_warm_start) is not bool or requested_warm_start != expected_warm_start:
        raise ContractError("artifact_changed", "backend warm-start request differs from frozen solve options")
    expected_core = {
        "relative_gap": frozen_options.get("relative_gap"),
        "absolute_gap": frozen_options.get("absolute_gap"),
        "feasibility_tolerance": frozen_options.get("feasibility_tolerance"),
        "integrality_tolerance": frozen_options.get("integrality_tolerance"),
    }
    for name, expected in expected_core.items():
        actual = requested["core"][name]
        if not _finite_number(actual) or (name in {"relative_gap", "absolute_gap"} and actual < 0) or (
            name in {"feasibility_tolerance", "integrality_tolerance"} and actual < 1e-10
        ):
            raise ContractError("output_invalid", "backend Core/checker option value is invalid")
        if not _finite_number(expected) or float(actual) != float(expected):
            raise ContractError("artifact_changed", "backend Core/checker criteria differ from frozen solve options")
    if requested["core"] != expected_core:
        raise ContractError("artifact_changed", "backend Core/checker criteria differ from frozen solve options")
    allowed_statuses = {
        "not_requested", "not_applied_solver_not_invoked", "accepted_readback", "submitted_no_readback",
        "core_criterion_only", "core_checker_only",
    }
    if any(not isinstance(value, str) or value not in allowed_statuses for value in statuses.values()):
        raise ContractError("output_invalid", "backend option status is invalid")
    warm_start_status = statuses["warm_start"]
    if effective["warm_start"] is not None:
        raise ContractError("artifact_changed", "warm-start request cannot claim an effective readback")
    if not requested_warm_start:
        if warm_start_status != "not_requested":
            raise ContractError("artifact_changed", "disabled warm-start option has a submission status")
    elif warm_start_status == "not_applied_solver_not_invoked":
        if observation.get("solver_invoked") is not False:
            raise ContractError("artifact_changed", "warm-start was marked unapplied despite a solver invocation")
    elif warm_start_status == "submitted_no_readback":
        if observation.get("solver_invoked") is not True:
            raise ContractError("artifact_changed", "warm-start was marked submitted without a solver invocation")
    else:
        raise ContractError("artifact_changed", "warm-start request lacks a valid submission status")
    for name in ("time_limit_seconds", "threads", "random_seed"):
        requested_value = requested["native"][name]
        effective_value = effective[name]
        status = statuses[name]
        if effective_value is not None:
            if name == "time_limit_seconds" and (not _finite_number(effective_value) or effective_value <= 0):
                raise ContractError("output_invalid", "native time limit readback is invalid")
            if name == "threads" and (type(effective_value) is not int or effective_value < 1):
                raise ContractError("output_invalid", "native thread count readback is invalid")
            if name == "random_seed" and (type(effective_value) is not int or effective_value < 0):
                raise ContractError("output_invalid", "native seed readback is invalid")
        if requested_value is None:
            if status != "not_requested":
                raise ContractError("artifact_changed", "unrequested native solver option has a submission or readback status")
        elif status == "not_applied_solver_not_invoked":
            if observation.get("solver_invoked") is not False or effective_value is not None:
                raise ContractError("artifact_changed", "native option was marked unapplied despite a solver invocation")
        elif status == "accepted_readback":
            if effective_value != requested_value:
                raise ContractError("artifact_changed", "native solver option readback differs from its request")
        elif status == "submitted_no_readback":
            if observation.get("solver_invoked") is not True:
                raise ContractError("artifact_changed", "native solver option was marked submitted without a solver invocation")
            if effective_value is not None:
                raise ContractError("artifact_changed", "submitted native solver option has a claimed effective readback")
        else:
            raise ContractError("output_invalid", "requested native solver option has no valid submission or readback status")
    for name in ("relative_gap", "absolute_gap"):
        if statuses[name] != "core_criterion_only":
            raise ContractError("artifact_changed", "Core gap target was mislabeled as a native solver setting")
    for name in ("feasibility_tolerance", "integrality_tolerance"):
        if statuses[name] != "core_checker_only":
            raise ContractError("artifact_changed", "checker tolerance was mislabeled as a native solver setting")
    expected_gap = {
        "relative_gap": frozen_options["relative_gap"],
        "absolute_gap": frozen_options["absolute_gap"],
        "formula": "gap <= max(absolute_gap, relative_gap * abs(checked_objective))",
        "source": "frozen_core_check",
    }
    if (
        not isinstance(core_gap, dict)
        or set(core_gap) != set(expected_gap)
        or not _finite_number(core_gap.get("relative_gap"))
        or not _finite_number(core_gap.get("absolute_gap"))
    ):
        raise ContractError("output_invalid", "backend Core gap criterion is invalid")
    if core_gap != expected_gap:
        raise ContractError("artifact_changed", "backend Core gap criterion differs from frozen options")

    bound_value = observation.get("best_objective_bound")
    bound_evidence = observation.get("bound_evidence")
    if bound_evidence is not None:
        if not isinstance(bound_evidence, dict) or not {"source", "scope", "objective_sense"}.issubset(bound_evidence):
            raise ContractError("output_invalid", "backend bound evidence is incomplete")
        if bound_value is None:
            raise ContractError("artifact_changed", "backend bound evidence has no accepted global bound")
        if not isinstance(bound_evidence.get("source"), str) or not bound_evidence["source"]:
            raise ContractError("output_invalid", "backend bound source is invalid")
        if bound_evidence.get("objective_sense") != _planned_objective_sense(
            manifest["candidate"]["objective_plan"], result["layer"]
        ):
            raise ContractError("artifact_changed", "backend bound evidence has the wrong objective sense")
        if bound_value is not None and bound_evidence.get("scope") != "global":
            raise ContractError("artifact_changed", "a non-global backend bound was exposed as a global bound")
    if result.get("bound") is not None and (bound_value is None or not isinstance(bound_evidence, dict)):
        raise ContractError("artifact_changed", "Result global bound lacks matching native bound evidence")
    if bound_value is not None and not isinstance(bound_evidence, dict):
        raise ContractError("artifact_changed", "backend global bound has no source evidence")
    if isinstance(bound_evidence, dict):
        sense = _planned_objective_sense(manifest["candidate"]["objective_plan"], result["layer"])
        expected_direction = "lower" if sense == "min" else "upper"
        if (
            bound_evidence.get("scope") != "global"
            or bound_evidence.get("value_direction") != expected_direction
            or not isinstance(bound_evidence.get("interface"), str)
            or not bound_evidence["interface"]
            or not isinstance(bound_evidence.get("objective_transform"), dict)
            or not isinstance(bound_evidence["objective_transform"].get("formula"), str)
            or not bound_evidence["objective_transform"]["formula"]
            or ("raw_value" in bound_evidence and not _finite_number(bound_evidence["raw_value"]))
        ):
            raise ContractError("output_invalid", "backend bound transformation evidence is invalid")
    if result.get("has_solver_incumbent") is True and observation.get("raw_solution_value_valid") is not True:
        raise ContractError("artifact_changed", "Result claims an incumbent that the backend did not validate")
    if result.get("has_solver_incumbent") is True and observation.get("best_feasible_objective") is not None:
        objective = result.get("objective")
        exact_native_objective = check_record.get("method") == NATIVE_CP_SAT_METHOD
        objective_value = objective.get("value") if isinstance(objective, dict) else None
        raw_best = observation.get("best_feasible_objective")
        objective_matches = (
            objective_value == raw_best
            if exact_native_objective
            else isinstance(objective_value, (int, float))
            and not isinstance(objective_value, bool)
            and isinstance(raw_best, (int, float))
            and not isinstance(raw_best, bool)
            and math.isclose(float(objective_value), float(raw_best), rel_tol=1e-8, abs_tol=1e-8)
        )
        if not isinstance(objective, dict) or not _finite_number(objective_value) or not objective_matches:
            raise ContractError("artifact_changed", "Result incumbent differs from the backend feasible objective")
    numerical = result.get("numerical")
    numeric_fields = {"requested_options", "effective_options", "check_tolerances", "optimality", "core_gap_target"}
    if not isinstance(numerical, dict) or set(numerical) != numeric_fields:
        raise ContractError("output_invalid", "schema-3 Result numerical evidence has invalid fields")
    if (
        canonical_json_bytes(numerical.get("requested_options")) != canonical_json_bytes(requested)
        or canonical_json_bytes(numerical.get("effective_options")) != canonical_json_bytes(effective)
    ):
        raise ContractError("artifact_changed", "Result options differ from the raw backend observation")
    if numerical.get("check_tolerances") != check_record.get("tolerances"):
        raise ContractError("artifact_changed", "Result check tolerances differ from ModelCheck")
    core_gap_target = numerical.get("core_gap_target")
    core_gap_fields = {"relative_gap", "absolute_gap"}
    if (
        not isinstance(core_gap_target, dict)
        or set(core_gap_target) != core_gap_fields
        or any(
            not _finite_number(core_gap_target.get(name)) or core_gap_target[name] < 0
            for name in core_gap_fields
        )
    ):
        raise ContractError("output_invalid", "Result Core gap target is invalid")
    if any(core_gap_target[name] != expected_core[name] for name in core_gap_fields):
        raise ContractError("artifact_changed", "Result Core gap target differs from frozen options")
    return requested, effective, expected_core


def _business_objective_evidence_agrees(
    business_values: Sequence[Mapping[str, Any]],
    source_values: Sequence[Mapping[str, Any]],
    status: str,
    *,
    exact_values: bool = False,
) -> bool:
    compare = objective_values_exactly_match if exact_values else objective_values_match
    if status == "pass":
        return compare(business_values, source_values)

    # A failed or uncovered checker has not established a business objective.
    # Empty evidence is valid; any rows it does return must still be complete,
    # well formed, and identify the same objective rows in the same order.
    compare(business_values, business_values)
    if not business_values:
        return True
    if len(business_values) != len(source_values):
        raise ContractError("output_invalid", "objective evidence row counts differ")
    identity_rows = [
        {**row, "value": source_values[index]["value"]}
        for index, row in enumerate(business_values)
    ]
    compare(identity_rows, source_values)
    return True


def _validate_native_cp_sat_evidence(
    store: CaseStore,
    *,
    output_root: str,
    output_ref_map: Mapping[str, Mapping[str, Any]],
    evidence: Any,
    raw_primals_ref: Mapping[str, Any],
    check_record: Mapping[str, Any],
    witness_record: Mapping[str, Any] | None,
    result: Mapping[str, Any],
    observation: Mapping[str, Any],
    objective_plan: Mapping[str, Any],
) -> None:
    """Seal-check Core's native CpSolver point, proto and typed projection."""
    result_backend = result.get("backend")
    if not isinstance(result_backend, Mapping) or result_backend.get("id") != "cp_sat":
        raise ContractError("artifact_changed", "native CpModel evidence is attached to a non-cp_sat Result")
    if check_record.get("method") != NATIVE_CP_SAT_METHOD or check_record.get("scope") != NATIVE_CP_SAT_SCOPE:
        raise ContractError("output_invalid", "native CpModel ModelCheck method or scope is invalid")
    if not isinstance(evidence, Mapping) or set(evidence) != {"model_proto", "variable_bindings", "response_vector"}:
        raise ContractError("output_invalid", "native CpModel evidence refs are invalid")

    model_ref, _ = _validate_ref_in_run(store, evidence["model_proto"], output_root, pointer="/model-check/native_evidence/model_proto")
    binding_ref, _ = _validate_ref_in_run(store, evidence["variable_bindings"], output_root, pointer="/model-check/native_evidence/variable_bindings")
    vector_ref, _ = _validate_ref_in_run(store, evidence["response_vector"], output_root, pointer="/model-check/native_evidence/response_vector")
    if (
        model_ref["media_type"] != "application/x-protobuf"
        or binding_ref["media_type"] != "application/json"
        or vector_ref["media_type"] != "application/x-ndjson"
        or vector_ref != raw_primals_ref
        or any(output_ref_map.get(ref["path"]) != ref for ref in (model_ref, binding_ref, vector_ref))
    ):
        raise ContractError("output_invalid", "native CpModel evidence refs are not canonical sealed outputs")

    try:
        model_bytes = store.read_ref(model_ref)
        binding_value = strict_json_loads(store.read_ref(binding_ref))
        vector_bytes = store.read_ref(vector_ref)
    except (ValueError, UnicodeError) as exc:
        raise ContractError("output_invalid", "native CpModel variable bindings are invalid JSON") from exc

    try:
        # Keep the host verifier independent from the OR-Tools solver SDK.
        # The helper creates CpModelProto in its own DescriptorPool and imports
        # protobuf only on this native-evidence path.
        from .native_cp_proto import cp_model_proto_class

        proto_class = cp_model_proto_class()
    except ImportError as exc:
        raise ContractError("capability_uncovered", "the trusted protobuf runtime is unavailable for native evidence verification") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise ContractError("capability_uncovered", "the trusted CpModel schema descriptor is unavailable for native evidence verification") from exc

    try:
        proto = proto_class()
        proto.ParseFromString(model_bytes)
        canonical_proto = proto.SerializeToString(deterministic=True)
    except Exception as exc:
        raise ContractError("output_invalid", "native CpModel proto cannot be parsed by the trusted v9.12 schema") from exc
    if canonical_proto != model_bytes:
        raise ContractError("artifact_changed", "native CpModel proto bytes are not canonical")

    plan_mode = objective_plan.get("mode")
    if plan_mode == "scalar" and result.get("layer") is None:
        sense = objective_plan.get("sense")
        objective_component = objective_plan.get("objective_component")
        objective_unit = objective_plan.get("unit")
        if not isinstance(objective_component, str) or not objective_component or not isinstance(objective_unit, str) or not objective_unit:
            raise ContractError("output_invalid", "native CpModel scalar objective plan is invalid")
    elif plan_mode == "feasibility" and result.get("layer") is None:
        sense = None
        objective_component = None
        objective_unit = None
    else:
        raise ContractError("capability_uncovered", "native CpModel evidence requires a scalar or feasibility objective plan")
    if not isinstance(binding_value, dict) or set(binding_value) != {"variables"} or not isinstance(binding_value["variables"], list):
        raise ContractError("output_invalid", "native CpModel typed binding artifact has invalid fields")
    bindings: list[tuple[str, tuple[str | int, ...], int]] = []
    identities: set[tuple[str, tuple[str | int, ...]]] = set()
    for row_index, row in enumerate(binding_value["variables"]):
        if not isinstance(row, dict) or set(row) != {"component", "key", "index"}:
            raise ContractError("output_invalid", "native CpModel typed binding row has invalid fields", details={"row": row_index})
        component, key, index = row["component"], row["key"], row["index"]
        if (
            not isinstance(component, str) or not component
            or not isinstance(key, list) or any(type(part) not in (str, int) for part in key)
            or type(index) is not int or not 0 <= index < len(proto.variables)
        ):
            raise ContractError("output_invalid", "native CpModel typed binding row is invalid", details={"row": row_index})
        identity = (component, tuple(key))
        if identity in identities:
            raise ContractError("output_invalid", "native CpModel typed binding identity is duplicated", details={"row": row_index})
        identities.add(identity)
        bindings.append((component, tuple(key), index))

    objective_info = validate_native_cp_sat_model_contract(
        proto,
        sense,
        mapped_variable_indexes=tuple(index for _, _, index in bindings),
    )

    if vector_bytes and not vector_bytes.endswith(b"\n"):
        raise ContractError("output_invalid", "native CpSolver response vector is not newline terminated")
    response_values: list[int] = []
    for row_index, raw_line in enumerate(vector_bytes.splitlines()):
        try:
            row = strict_json_loads(raw_line)
        except (ValueError, UnicodeError) as exc:
            raise ContractError("output_invalid", "native CpSolver response vector contains invalid JSON", details={"row": row_index}) from exc
        if not isinstance(row, dict) or set(row) != {"index", "value"} or type(row.get("index")) is not int or type(row.get("value")) is not int or row["index"] != row_index:
            raise ContractError("output_invalid", "native CpSolver response vector is not a dense indexed integer vector", details={"row": row_index})
        response_values.append(row["value"])

    accepted_point = (
        observation.get("raw_status") in {"OPTIMAL", "FEASIBLE"}
        and observation.get("solver_invoked") is True
        and observation.get("solver_returned") is True
    )
    vector_complete = accepted_point and len(response_values) == len(proto.variables)
    expected_coverage = {
        "required": len(proto.variables),
        "present": len(response_values),
        "completed": len(response_values) if vector_complete else 0,
        "missing_count": 0 if vector_complete else max(0, len(proto.variables) - len(response_values)),
    }
    if check_record.get("variable_coverage") != expected_coverage or check_record.get("component_coverage") != []:
        raise ContractError("artifact_changed", "native ModelCheck coverage differs from the sealed response vector")

    domain_valid = vector_complete
    if vector_complete:
        for index, value in enumerate(response_values):
            domain = objective_info["domains"][index]
            if not any(domain[position] <= value <= domain[position + 1] for position in range(0, len(domain), 2)):
                domain_valid = False
        if any(abs(response_values[index]) > (1 << 53) - 1 for _, _, index in bindings):
            domain_valid = False

    expected_objectives: list[dict[str, Any]] = []
    objective_valid = vector_complete
    exact_value: int | None = None
    if vector_complete and objective_info["objective_present"]:
        inner = objective_info["objective_offset"]
        for index, coefficient in zip(objective_info["objective_vars"], objective_info["objective_coeffs"], strict=True):
            inner += coefficient * response_values[index]
        exact_value = int(objective_info["objective_scale"] * inner)
        incumbent = observation.get("best_feasible_objective")
        objective_valid = type(incumbent) in (int, float) and math.isfinite(float(incumbent)) and exact_value == incumbent
        if objective_valid:
            expected_objectives.append({
                "component": objective_component,
                "sense": sense,
                "unit": objective_unit,
                "value": float(exact_value),
            })
    elif vector_complete:
        objective_valid = observation.get("best_feasible_objective") is None

    if not objective_values_exactly_match(check_record.get("objective_values", []), expected_objectives):
        raise ContractError("artifact_changed", "native ModelCheck objective differs from its exact proto recomputation")
    result_objective = result.get("objective")
    if expected_objectives:
        if not isinstance(result_objective, Mapping) or not objective_values_exactly_match([result_objective], expected_objectives):
            raise ContractError("artifact_changed", "native Result objective differs from its exact proto recomputation")
    elif result_objective is not None:
        raise ContractError("artifact_changed", "native feasibility or no-point Result contains an objective")

    expected_status = "uncovered" if not accepted_point else "pass" if vector_complete and domain_valid and objective_valid else "fail"
    if check_record.get("status") != expected_status or result.get("has_checked_candidate") is not (expected_status == "pass"):
        raise ContractError("artifact_changed", "native ModelCheck status differs from the sealed CpSolver response")
    if result.get("has_solver_incumbent") is not (expected_status == "pass"):
        raise ContractError("artifact_changed", "native Result incumbent flag differs from its mechanically checked point")

    expected_witness = None
    if expected_status == "pass":
        expected_witness = {
            "variables": [
                {
                    "component": component,
                    "key": list(key),
                    "domain": "binary" if objective_info["domains"][index] == (0, 1) else "integer",
                    "value": float(response_values[index]),
                    "origin": "solver_primal",
                }
                for component, key, index in bindings
            ],
            "objective_values": expected_objectives,
        }
    if expected_witness is None:
        if witness_record is not None:
            raise ContractError("artifact_changed", "native Run emitted a Witness without a complete checked point")
    elif witness_record is None or witness_record.get("variables") != expected_witness["variables"] or not objective_values_exactly_match(witness_record.get("objective_values", []), expected_witness["objective_values"]):
        raise ContractError("artifact_changed", "native Witness differs from its explicit index projection")


def _read_worker_seal(
    store: CaseStore,
    case_id: str,
    run: Mapping[str, Any],
    prepared: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]]]:
    run_id = run["run_id"]
    prepared_id = run["prepared_id"]
    output_root = f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output"
    seal_path = f"{output_root}/seal.json"
    try:
        seal_value, seal_ref = _json_ref(store, seal_path, "worker seal")
    except ContractError as exc:
        if exc.code == "not_found":
            return None, None, []
        raise
    fields = {
        "schema_version", "run_id", "prepared_id", "dispatch_nonce", "input_digest", "output_refs",
        "solver_returned", "worker_completed", "consumed_inputs", "started_at", "completed_at",
    }
    if set(seal_value) != fields or seal_value.get("schema_version") != 2:
        raise ContractError("output_invalid", "worker seal fields are invalid")
    for field, expected in (
        ("run_id", run_id),
        ("prepared_id", prepared_id),
        ("dispatch_nonce", run["dispatch_nonce"]),
        ("input_digest", prepared["input_digest"]),
    ):
        if seal_value.get(field) != expected:
            raise ContractError("artifact_changed", "worker seal identity does not match the run")
    if not isinstance(seal_value["solver_returned"], bool) or not isinstance(seal_value["worker_completed"], bool):
        raise ContractError("output_invalid", "worker seal completion flags are invalid")
    if not isinstance(seal_value["output_refs"], list) or not isinstance(seal_value["consumed_inputs"], list):
        raise ContractError("output_invalid", "worker seal refs must be arrays")
    source_records = read_prepared_source_records(store, manifest.get("sources", []))
    source_record_ref_ids = {
        (validate_ref_shape(source["record"])["path"], validate_ref_shape(source["record"])["sha256"], validate_ref_shape(source["record"])["byte_len"], validate_ref_shape(source["record"])["media_type"])
        for source in manifest.get("sources", [])
    }
    runtime = manifest["runtime_bundle"]
    runtime_root = f".reaslab/optagent/runtime/{runtime['digest']}"
    allowed_input_roots = (
        f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/input/",
        f".reaslab/optagent/cases/{case_id}/runs/{run_id}/invocation.json",
        f"{runtime_root}/manifest.json",
        f"{runtime_root}/python/",
    )
    seen_consumed: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(seal_value["consumed_inputs"]):
        ref = validate_ref_shape(value, pointer=f"/seal/consumed_inputs/{index}")
        if not any(ref["path"] == root or ref["path"].startswith(root) for root in allowed_input_roots):
            raise ContractError("input_closure_unknown", "worker seal reports an input outside its frozen closure")
        previous = seen_consumed.get(ref["path"])
        if previous is not None and previous != ref:
            raise ContractError("artifact_changed", "worker seal repeats one input path with different hashes")
        seen_consumed[ref["path"]] = ref

    required_refs: list[Mapping[str, Any]] = [
        prepared["input_manifest"],
        runtime["manifest"],
    ]
    required_refs.extend(
        ref
        for source in manifest["sources"]
        for ref in (source["record"], source["bytes"])
    )
    candidate = manifest["candidate"]
    required_refs.extend([candidate["model"]["bytes"], *(item["bytes"] for item in candidate["local_modules"])])
    if candidate["business_checker"] is not None:
        required_refs.append(candidate["business_checker"]["bytes"])
    if candidate.get("requirements_file") is not None:
        required_refs.append(candidate["requirements_file"]["bytes"])
    data_info = manifest["data"]
    if data_info is None:
        data_bundle = load_data_bundle(
            store,
            None,
            candidate_binding_digest=manifest["binding_digest"],
        )
    else:
        if not isinstance(data_info, dict) or set(data_info) != {"data_id", "manifest", "instance_parameter_object_id"}:
            raise ContractError("output_invalid", "prepared data identity has invalid fields")
        if not isinstance(data_info["data_id"], str) or not data_info["data_id"]:
            raise ContractError("output_invalid", "prepared data identity is invalid")
        if data_info["instance_parameter_object_id"] is not None and not isinstance(data_info["instance_parameter_object_id"], str):
            raise ContractError("output_invalid", "prepared instance parameter identity is invalid")
        data_manifest_ref = validate_ref_shape(data_info["manifest"], pointer="/data/manifest")
        data_manifest_raw = store.read_ref(data_manifest_ref)
        try:
            data_manifest = strict_json_loads(data_manifest_raw, max_bytes=len(data_manifest_raw))
        except (ValueError, UnicodeError) as exc:
            raise ContractError("output_invalid", "worker DataManifest is invalid JSON") from exc
        if not isinstance(data_manifest, dict):
            raise ContractError("output_invalid", "worker DataManifest must be an object")
        data_bundle = load_data_bundle(
            store,
            data_manifest,
            candidate_binding_digest=manifest["binding_digest"],
            source_records=source_records,
        )
        if data_bundle.data_id != data_info["data_id"]:
            raise ContractError("artifact_changed", "worker DataBundle identity differs from PreparedInputManifest")
        required_refs.append(data_manifest_ref)
        required_refs.extend([data_manifest["trace"], *(item["values"] for item in data_manifest["objects"])])
    data_consumed_ref_ids = {
        (ref["path"], ref["sha256"], ref["byte_len"], ref["media_type"])
        for ref in (data_bundle.consumed_inputs if data_info is not None else ())
    }
    if data_info is not None:
        data_consumed_ref_ids.add((data_manifest_ref["path"], data_manifest_ref["sha256"], data_manifest_ref["byte_len"], data_manifest_ref["media_type"]))
    _, runtime_refs = verify_runtime_bundle(
        store.workspace,
        runtime["digest"],
        runtime["manifest"],
        prepared_schema_version=manifest["schema_version"],
    )
    _, invocation_ref = store.read_workspace_file(f".reaslab/optagent/cases/{case_id}/runs/{run_id}/invocation.json")
    already_verified_ref_ids = data_consumed_ref_ids | source_record_ref_ids | {
        (ref["path"], ref["sha256"], ref["byte_len"], ref["media_type"]) for ref in runtime_refs
    } | {(invocation_ref["path"], invocation_ref["sha256"], invocation_ref["byte_len"], invocation_ref["media_type"])}
    for ref in seen_consumed.values():
        identity = (ref["path"], ref["sha256"], ref["byte_len"], ref["media_type"])
        if identity not in already_verified_ref_ids:
            store.read_ref(ref)
    required_refs.extend(runtime_refs)
    required_refs.append(invocation_ref)
    for ref in required_refs:
        checked = validate_ref_shape(dict(ref))
        actual = seen_consumed.get(checked["path"])
        if actual != checked:
            raise ContractError("input_closure_unknown", "worker seal omitted an actually consumed frozen input", details={"path": checked["path"]})

    parsed_outputs: dict[str, dict[str, Any]] = {}
    output_refs: list[dict[str, Any]] = []
    for index, value in enumerate(seal_value["output_refs"]):
        ref, raw = _validate_ref_in_run(store, value, output_root, pointer=f"/seal/output_refs/{index}")
        if ref["path"] == seal_path:
            raise ContractError("output_invalid", "worker seal cannot include itself as an output")
        if ref["path"] in {item["path"] for item in output_refs}:
            raise ContractError("output_invalid", "worker seal repeats an output ref")
        if ref["media_type"] == "application/json":
            try:
                parsed = strict_json_loads(raw, max_bytes=len(raw))
            except (ValueError, UnicodeError) as exc:
                raise ContractError("output_invalid", "worker JSON artifact is invalid") from exc
            if not isinstance(parsed, dict):
                raise ContractError("output_invalid", "worker JSON artifact must be an object")
            parsed_outputs[ref["path"]] = parsed
        output_refs.append(ref)
    output_ref_map = {item["path"]: item for item in output_refs}
    if output_refs != [output_ref_map[path] for path in sorted(output_ref_map)]:
        raise ContractError("output_invalid", "worker output refs are not canonical path order")

    worker_failure = None
    failure_path = f"{output_root}/worker-failure.json"
    if not seal_value["worker_completed"] and failure_path in parsed_outputs:
        worker_failure = _worker_failure_diagnostic(
            parsed_outputs[failure_path],
            output_ref_map[failure_path],
            run_id=run_id,
            prepared_id=prepared_id,
            input_digest=prepared["input_digest"],
        )

    business_checker_diagnostics: list[dict[str, Any]] = []
    solver_invoked_observed = False
    for path, artifact in parsed_outputs.items():
        if not path.endswith("/backend-observation.json"):
            continue
        raw_observation = artifact.get("observation")
        if not isinstance(raw_observation, dict) or not isinstance(raw_observation.get("solver_invoked"), bool):
            raise ContractError("output_invalid", "raw backend observation lacks a solver invocation flag")
        solver_invoked_observed = solver_invoked_observed or raw_observation["solver_invoked"]

    direct_family = manifest.get("candidate", {}).get("authoring") == "direct_python"
    if direct_family and any(
        path.endswith(("/backend-observation.json", "/raw-primals.jsonl", "/model-check.json"))
        for path in output_ref_map
    ):
        raise ContractError("output_invalid", "direct_python output contains native solver or ModelCheck artifacts")

    result_refs: list[dict[str, Any]] = []
    result_keys: set[tuple[bytes, int | None]] = set()
    result_instance_keys: set[bytes] = set()
    solver_returned_observed = any(
        isinstance(value.get("observation"), dict)
        and value["observation"].get("solver_returned") is True
        for path, value in parsed_outputs.items()
        if path.endswith("/backend-observation.json")
    )
    for path, result in parsed_outputs.items():
        if not path.endswith("/result.json"):
            continue
        legacy_result_fields = {
            "instance_key", "instance_binding_digest", "layer", "source_revision", "candidate_revision", "input_digest",
            "backend", "raw_status", "termination", "solver_invoked", "has_solver_incumbent", "has_checked_candidate",
            "witness", "objective", "bound", "gap", "check", "coverage", "warnings",
        }
        result_fields = legacy_result_fields | {"numerical", "lexicographic"}
        if frozenset(result) not in {frozenset(legacy_result_fields), frozenset(result_fields)}:
            raise ContractError("output_invalid", "worker Result fields are invalid")
        if not isinstance(result["solver_invoked"], bool):
            raise ContractError("output_invalid", "Result solver_invoked field is invalid")
        if result.get("input_digest") != prepared["input_digest"] or result.get("source_revision") != prepared["source_revision"] or result.get("candidate_revision") != prepared["candidate_revision"]:
            raise ContractError("artifact_changed", "worker Result is bound to different prepared revisions")
        key = result["instance_key"]
        if not isinstance(key, list) or any(type(item) not in (str, int) for item in key):
            raise ContractError("output_invalid", "Result instance key has invalid typed values")
        expected_binding = _check_instance_identity(data_bundle, manifest, key)
        if result.get("instance_binding_digest") != expected_binding:
            raise ContractError("artifact_changed", "Result instance binding does not match the Core DataBundle")
        layer = result["layer"]
        if layer is not None and (isinstance(layer, bool) or not isinstance(layer, int) or layer < 0):
            raise ContractError("output_invalid", "Result layer is invalid")
        identity = (canonical_json_bytes(key), layer)
        if identity in result_keys:
            raise ContractError("output_invalid", "worker emitted a duplicate Result for one instance and layer")
        result_keys.add(identity)
        result_instance_keys.add(canonical_json_bytes(key))

        if direct_family:
            if frozenset(result) != frozenset(legacy_result_fields):
                raise ContractError("output_invalid", "direct_python Result fields are invalid")
            if (
                not isinstance(result.get("backend"), Mapping)
                or result["backend"].get("id") != "direct_python"
                or result["solver_invoked"] is not False
                or result["has_solver_incumbent"] is not False
                or result.get("bound") is not None
                or result.get("numerical") is not None
                or not isinstance(result.get("warnings"), list)
                or not isinstance(result.get("coverage"), list)
            ):
                raise ContractError("output_invalid", "direct_python Result claims unsupported native evidence")
            gap = result.get("gap")
            if (
                not isinstance(gap, dict)
                or set(gap) != {"absolute", "relative", "definition"}
                or gap.get("absolute") is not None
                or gap.get("relative") is not None
                or gap.get("definition") != "direct_python does not provide a native global bound or a bound-derived gap"
            ):
                raise ContractError("output_invalid", "direct_python Result must not report a native bound gap")
            check_ref, check_raw = _validate_ref_in_run(store, result["check"], output_root, pointer="/result/check")
            business_path = path.rsplit("/", 1)[0] + "/business-check.json"
            if check_ref["path"] != business_path or check_ref["media_type"] != "application/json":
                raise ContractError("output_invalid", "direct_python Result check must point to its BusinessCheck")
            business = parsed_outputs.get(business_path)
            if business is None:
                try:
                    business = strict_json_loads(check_raw, max_bytes=len(check_raw))
                except (ValueError, UnicodeError) as exc:
                    raise ContractError("output_invalid", "direct_python BusinessCheck is invalid JSON") from exc
                if not isinstance(business, dict):
                    raise ContractError("output_invalid", "direct_python BusinessCheck must be an object")
                parsed_outputs[business_path] = business
            _validate_identity_triplet(
                business,
                input_digest=prepared["input_digest"], key=key, binding_digest=expected_binding,
                expected_fields={
                    "input_digest", "instance_key", "instance_binding_digest", "status",
                    "checked_obligations", "objective_values", "violations", "uncovered",
                },
                label="BusinessCheck",
                optional_fields={"solution"},
            )
            if business["status"] not in {"pass", "fail", "uncovered"}:
                raise ContractError("output_invalid", "direct_python BusinessCheck status is invalid")
            if output_ref_map.get(business_path) != check_ref or result.get("check") != check_ref:
                raise ContractError("output_invalid", "direct_python check anchor is not the canonical sealed BusinessCheck")
            witness_record = None
            if result["witness"] is not None:
                witness_ref, witness_raw = _validate_ref_in_run(store, result["witness"], output_root, pointer="/result/witness")
                if witness_ref["path"] not in parsed_outputs:
                    try:
                        parsed_outputs[witness_ref["path"]] = strict_json_loads(witness_raw, max_bytes=len(witness_raw))
                    except (ValueError, UnicodeError) as exc:
                        raise ContractError("output_invalid", "direct_python Witness is invalid JSON") from exc
                witness_record = validate_direct_witness_record(
                    parsed_outputs[witness_ref["path"]],
                    manifest["candidate"]["objective_plan"],
                    input_digest=prepared["input_digest"],
                    instance_key=key,
                    instance_binding_digest=expected_binding,
                )
                if output_ref_map.get(witness_ref["path"]) != witness_ref or result["witness"] != witness_ref:
                    raise ContractError("output_invalid", "direct_python Witness is not a canonical sealed output")
            objective_values = witness_record["objective_values"] if witness_record is not None else []
            if not _business_objective_evidence_agrees(
                business["objective_values"], objective_values, business["status"]
            ):
                raise ContractError("artifact_changed", "direct_python BusinessCheck objective differs from its Witness")
            objective = result.get("objective")
            if objective_values:
                if not isinstance(objective, dict) or not objective_values_match([objective], objective_values):
                    raise ContractError("artifact_changed", "direct_python Result objective differs from its Witness")
            elif objective is not None:
                raise ContractError("output_invalid", "direct feasibility Result cannot contain an objective")
            fully_checked = witness_record is not None and business_check_is_fully_passed(business)
            if result["has_checked_candidate"] is not fully_checked:
                raise ContractError("artifact_changed", "direct_python checked-candidate flag differs from its BusinessCheck")
            if result.get("termination") != ("feasible" if fully_checked else "unknown"):
                raise ContractError("artifact_changed", "direct_python termination differs from its checked point status")
            result_ref = output_ref_map[path]
            result_refs.append(result_ref)
            diagnostic = _business_checker_diagnostic(result.get("warnings"), result_ref)
            if diagnostic is not None:
                business_checker_diagnostics.append(diagnostic)
            continue

        check_ref, check_raw = _validate_ref_in_run(store, result["check"], output_root, pointer="/result/check")
        if check_ref["media_type"] != "application/json":
            raise ContractError("output_invalid", "Result ModelCheck must be JSON")
        if check_ref["path"] not in parsed_outputs:
            try:
                check_record = strict_json_loads(check_raw, max_bytes=len(check_raw))
            except (ValueError, UnicodeError) as exc:
                raise ContractError("output_invalid", "ModelCheck is invalid JSON") from exc
            parsed_outputs[check_ref["path"]] = check_record
        check_record = _validate_identity_triplet(
            parsed_outputs[check_ref["path"]],
            input_digest=prepared["input_digest"],
            key=key,
            binding_digest=expected_binding,
            expected_fields={
                "input_digest", "instance_key", "instance_binding_digest", "status", "variable_coverage",
                "component_coverage", "violations", "objective_values", "tolerances", "uncovered",
            },
            label="ModelCheck",
            optional_fields={"method", "scope", "native_evidence"},
        )
        native_method_fields = {"method", "scope", "native_evidence"} & set(check_record)
        if native_method_fields and native_method_fields != {"method", "scope", "native_evidence"}:
            raise ContractError("output_invalid", "native ModelCheck method, scope and evidence must be recorded together")
        if "method" in check_record and check_record.get("method") != NATIVE_CP_SAT_METHOD:
            raise ContractError("output_invalid", "worker ModelCheck method is unsupported")
        if check_record["status"] not in {"pass", "fail", "uncovered"}:
            raise ContractError("output_invalid", "ModelCheck status is invalid")
        if check_record["violations"] is not None:
            violations_ref = validate_ref_shape(check_record["violations"], pointer="/model-check/violations")
            if violations_ref.get("media_type") != "application/x-ndjson" or output_ref_map.get(violations_ref["path"]) != violations_ref:
                raise ContractError("output_invalid", "ModelCheck violations are not a sealed JSONL artifact")
        if result["has_checked_candidate"] != (check_record["status"] == "pass"):
            raise ContractError("artifact_changed", "Result checked-candidate flag differs from ModelCheck")
        if check_ref["path"] not in output_ref_map or result["check"] != check_ref:
            raise ContractError("output_invalid", "Result ModelCheck is not a canonical sealed output")
        witness_record: Mapping[str, Any] | None = None
        if result["witness"] is not None:
            witness_ref, witness_raw = _validate_ref_in_run(store, result["witness"], output_root, pointer="/result/witness")
            if witness_ref["path"] not in parsed_outputs:
                try:
                    parsed = strict_json_loads(witness_raw, max_bytes=len(witness_raw))
                except (ValueError, UnicodeError) as exc:
                    raise ContractError("output_invalid", "Witness is invalid JSON") from exc
                parsed_outputs[witness_ref["path"]] = parsed
            witness_record = _validate_identity_triplet(
                parsed_outputs[witness_ref["path"]],
                input_digest=prepared["input_digest"], key=key, binding_digest=expected_binding,
                expected_fields={"input_digest", "instance_key", "instance_binding_digest", "variables", "objective_values"},
                label="Witness",
            )
            if witness_ref["path"] not in output_ref_map or result["witness"] != witness_ref:
                raise ContractError("output_invalid", "Witness is not a canonical sealed output")

        business_path = path.rsplit("/", 1)[0] + "/business-check.json"
        business = parsed_outputs.get(business_path)
        if business is None or business_path not in output_ref_map:
            raise ContractError("output_invalid", "Result has no matching BusinessCheck artifact")
        _validate_identity_triplet(
            business,
            input_digest=prepared["input_digest"],
            key=key,
            binding_digest=expected_binding,
            expected_fields={
                "input_digest", "instance_key", "instance_binding_digest", "status",
                "checked_obligations", "objective_values", "violations", "uncovered",
            },
            label="BusinessCheck",
            optional_fields={"solution"},
        )
        if business["status"] not in {"pass", "fail", "uncovered"}:
            raise ContractError("output_invalid", "BusinessCheck status is invalid")
        objective_values_agree = _business_objective_evidence_agrees(
            business["objective_values"],
            check_record["objective_values"],
            business["status"],
            exact_values=check_record.get("method") == NATIVE_CP_SAT_METHOD,
        )
        if not objective_values_agree:
            raise ContractError("artifact_changed", "BusinessCheck objective values differ from ModelCheck")

        observation_path = path.rsplit("/", 1)[0] + "/backend-observation.json"
        observation_artifact_ref = output_ref_map.get(observation_path)
        observation = parsed_outputs.get(observation_path)
        if observation_artifact_ref is None or observation is None or not isinstance(observation.get("observation"), dict):
            raise ContractError("output_invalid", "Result has no matching backend observation")
        raw_observation = observation["observation"]
        if raw_observation.get("solver_invoked") is not result["solver_invoked"]:
            raise ContractError("artifact_changed", "Result solver_invoked differs from its backend observation")
        if manifest.get("schema_version") == 3:
            requested, effective, core_values = _validate_schema3_backend_observation(
                observation=raw_observation,
                result=result,
                manifest=manifest,
                check_record=check_record,
            )
            objective = result.get("objective")
            result_gap = result.get("gap")
            recomputed_numerical = _result_numerical_summary(
                check_record=check_record,
                objective_value=objective.get("value") if isinstance(objective, dict) else None,
                objective_sense=_planned_objective_sense(manifest["candidate"]["objective_plan"], result["layer"]),
                bound=result.get("bound"),
                absolute_gap=result_gap.get("absolute") if isinstance(result_gap, dict) else None,
                termination=result["termination"],
                has_incumbent=result["has_solver_incumbent"],
                requested_options=requested,
                effective_options=effective,
                native_observation=raw_observation,
                core_gap_target={
                    "relative_gap": core_values["relative_gap"],
                    "absolute_gap": core_values["absolute_gap"],
                },
            )
            if canonical_json_bytes(result["numerical"]) != canonical_json_bytes(recomputed_numerical):
                raise ContractError("artifact_changed", "schema-3 numerical closure differs from the raw observation and Core criteria")
        elif "numerical" in result:
            numeric_fields = {"requested_options", "effective_options", "check_tolerances", "optimality"}
            if not isinstance(result["numerical"], dict) or set(result["numerical"]) != numeric_fields:
                raise ContractError("output_invalid", "Result numerical evidence has invalid fields")
            option_names = {"mip_rel_gap", "mip_abs_gap", "primal_feasibility_tolerance", "mip_feasibility_tolerance"}
            requested = raw_observation.get("requested_options")
            effective = raw_observation.get("effective_options")
            statuses = raw_observation.get("option_set_statuses")
            if (
                not isinstance(requested, dict)
                or set(requested) != option_names
                or not isinstance(effective, dict)
                or set(effective) != option_names
                or not isinstance(statuses, dict)
            ):
                raise ContractError("output_invalid", "backend numeric option observation is incomplete")
            for name in option_names:
                request_value = requested[name]
                effective_value = effective[name]
                if request_value is not None and (
                    not _finite_number(request_value)
                    or (name in {"mip_rel_gap", "mip_abs_gap"} and request_value < 0)
                ):
                    raise ContractError("output_invalid", "backend requested numeric option is invalid")
                if effective_value is not None and (
                    not _finite_number(effective_value)
                ):
                    raise ContractError("output_invalid", "backend effective numeric option is invalid")
                if name in {"mip_rel_gap", "mip_abs_gap"} and effective_value is not None and effective_value < 0:
                    raise ContractError("output_invalid", "backend effective gap tolerance is negative")
                if request_value is not None and (
                    statuses.get(name) != "kOk" or effective_value != request_value
                ):
                    raise ContractError("artifact_changed", "effective backend option differs from its accepted request")
            if result["numerical"]["requested_options"] != requested or result["numerical"]["effective_options"] != effective:
                raise ContractError("artifact_changed", "Result numeric options differ from the raw backend observation")
            if result["numerical"]["check_tolerances"] != check_record.get("tolerances"):
                raise ContractError("artifact_changed", "Result check tolerances differ from ModelCheck")
            objective = result.get("objective")
            result_gap = result.get("gap")
            recomputed_numerical = _result_numerical_summary(
                check_record=check_record,
                objective_value=objective.get("value") if isinstance(objective, dict) else None,
                objective_sense=_planned_objective_sense(manifest["candidate"]["objective_plan"], result["layer"]),
                bound=result.get("bound"),
                absolute_gap=result_gap.get("absolute") if isinstance(result_gap, dict) else None,
                termination=result["termination"],
                has_incumbent=result["has_solver_incumbent"],
                requested_options=requested,
                effective_options=effective,
                native_observation=raw_observation,
            )
            if result["numerical"] != recomputed_numerical:
                raise ContractError("artifact_changed", "Result numeric closure does not match its objective, bound, gap and tolerances")
        raw_primals_ref = validate_ref_shape(observation.get("raw_primals"), pointer="/backend-observation/raw_primals")
        if raw_primals_ref.get("media_type") != "application/x-ndjson" or output_ref_map.get(raw_primals_ref["path"]) != raw_primals_ref:
            raise ContractError("output_invalid", "raw backend primals are not a sealed JSONL artifact")
        if check_record.get("method") == NATIVE_CP_SAT_METHOD:
            _validate_native_cp_sat_evidence(
                store,
                output_root=output_root,
                output_ref_map=output_ref_map,
                evidence=check_record.get("native_evidence"),
                raw_primals_ref=raw_primals_ref,
                check_record=check_record,
                witness_record=witness_record,
                result=result,
                observation=raw_observation,
                objective_plan=manifest["candidate"]["objective_plan"],
            )
        raw_bound_value = observation["observation"].get("best_objective_bound")
        if result["bound"] is not None:
            bound = result["bound"]
            if (
                not isinstance(bound, dict)
                or set(bound) != {"value", "direction", "scope", "evidence"}
                or bound["scope"] != "global"
                or bound["evidence"] != observation_artifact_ref
                or bound["direction"] not in {"lower", "upper"}
                or not _finite_number(bound["value"])
                or not _finite_number(raw_bound_value)
                or (manifest.get("schema_version") != 3 and raw_bound_value != bound["value"])
            ):
                raise ContractError("output_invalid", "Result global bound is unsupported by its raw backend observation")
        _validate_normalized_bound_and_gap(
            result,
            check_record,
            manifest["candidate"]["objective_plan"],
            raw_bound_value=raw_bound_value,
        )

        result_ref = output_ref_map[path]
        result_refs.append(result_ref)
        diagnostic = _business_checker_diagnostic(result.get("warnings"), result_ref)
        if diagnostic is not None:
            business_checker_diagnostics.append(diagnostic)

    objective_plan = manifest["candidate"]["objective_plan"]
    for path, result in parsed_outputs.items():
        if not path.endswith("/result.json") or "numerical" not in result:
            continue
        lexicographic = result.get("lexicographic")
        layer = result.get("layer")
        if layer is None or layer == 0:
            if lexicographic is not None:
                raise ContractError("output_invalid", "first or scalar Result cannot claim a prior-layer restriction")
            continue
        layers = objective_plan.get("layers") if objective_plan.get("mode") == "lexicographic" else None
        if not isinstance(layers, list) or layer >= len(layers) or not isinstance(lexicographic, dict):
            raise ContractError("output_invalid", "later lexicographic Result lacks its restriction evidence")
        lex_fields = {
            "previous_layer", "previous_result_ref", "sense", "objective", "global_bound", "absolute_gap",
            "declared_tolerance", "restriction_operator", "restriction_value", "scope",
        }
        if set(lexicographic) != lex_fields:
            raise ContractError("output_invalid", "lexicographic restriction fields are invalid")
        previous_ref = validate_ref_shape(lexicographic["previous_result_ref"], pointer="/result/lexicographic/previous_result_ref")
        if output_ref_map.get(previous_ref["path"]) != previous_ref or not previous_ref["path"].endswith("/result.json"):
            raise ContractError("artifact_changed", "lexicographic restriction does not reference a sealed Result")
        previous = parsed_outputs.get(previous_ref["path"])
        if (
            not isinstance(previous, dict)
            or previous.get("instance_key") != result.get("instance_key")
            or previous.get("layer") != layer - 1
            or previous.get("has_checked_candidate") is not True
        ):
            raise ContractError("artifact_changed", "lexicographic restriction references the wrong prior Result")
        prior_layer = layers[layer - 1]
        prior_bound = previous.get("bound")
        prior_gap = previous.get("gap")
        prior_objective = previous.get("objective")
        expected_lock = (
            checked_lexicographic_lock(
                bound_value=prior_bound.get("value") if isinstance(prior_bound, dict) else None,
                sense=prior_layer.get("sense"),
                objective_value=prior_objective.get("value") if isinstance(prior_objective, dict) else None,
                absolute_tolerance=prior_layer.get("absolute_tolerance"),
                relative_tolerance=prior_layer.get("relative_tolerance"),
                stored_absolute_gap=prior_gap.get("absolute") if isinstance(prior_gap, dict) else None,
            )
            if isinstance(prior_layer, dict)
            and isinstance(prior_bound, dict)
            and prior_bound.get("scope") == "global"
            and prior_bound.get("direction") == ("lower" if prior_layer.get("sense") == "min" else "upper")
            else None
        )
        expected_lex = {"previous_layer": layer - 1, "previous_result_ref": previous_ref, **expected_lock} if expected_lock is not None else None
        if lexicographic != expected_lex:
            raise ContractError("artifact_changed", "lexicographic restriction is not justified by its checked bound and declared tolerance")

    for path, check in parsed_outputs.items():
        if path.endswith("/business-check.json"):
            key = check.get("instance_key")
            if not isinstance(key, list):
                raise ContractError("output_invalid", "BusinessCheck instance key is invalid")
            expected_binding = _check_instance_identity(data_bundle, manifest, key)
            _validate_identity_triplet(
                check,
                input_digest=prepared["input_digest"], key=key, binding_digest=expected_binding,
                expected_fields={"input_digest", "instance_key", "instance_binding_digest", "status", "checked_obligations", "objective_values", "violations", "uncovered"},
                label="BusinessCheck",
                optional_fields={"solution"},
            )
            if check["status"] not in {"pass", "fail", "uncovered"}:
                raise ContractError("output_invalid", "BusinessCheck status is invalid")
    batch_summary_items = [
        (path, value)
        for path, value in parsed_outputs.items()
        if path.endswith("/batch-summary.json")
    ]
    batch_summaries = [value for _, value in batch_summary_items]
    seal_value = dict(seal_value)
    if len(batch_summaries) > 1:
        raise ContractError("output_invalid", "worker output has an invalid number of batch summaries")
    seal_value["batch_complete"] = None
    seal_value["batch_summary"] = None
    if batch_summaries:
        batch_path, batch = batch_summary_items[0]
        batch_ref = next(
            (
                validate_ref_shape(item, pointer="/seal/output_refs")
                for item in seal_value["output_refs"]
                if isinstance(item, Mapping) and item.get("path") == batch_path
            ),
            None,
        )
        if batch_ref is None:
            raise ContractError("artifact_changed", "validated batch summary has no exact seal Ref")
        batch_fields = {
            "schema_version", "planned_instance_count", "started_instance_count", "instances_with_results",
            "instances_with_all_layers_attempted", "partial_instance_count", "remaining_instance_count", "stop_reason",
        }
        if not isinstance(batch, Mapping) or set(batch) != batch_fields or batch["schema_version"] != 1:
            raise ContractError("output_invalid", "worker batch summary fields are invalid")
        count_fields = batch_fields - {"schema_version", "stop_reason"}
        if any(type(batch[field]) is not int or batch[field] < 0 for field in count_fields):
            raise ContractError("output_invalid", "worker batch summary counts are invalid")
        if batch["stop_reason"] not in {
            None,
            "execution_budget_reserved",
            "lexicographic_bound_not_within_tolerance",
            "lexicographic_layer_unavailable",
        }:
            raise ContractError("output_invalid", "worker batch summary stop reason is invalid")
        if (
            batch["planned_instance_count"] != batch["started_instance_count"] + batch["remaining_instance_count"]
            or batch["started_instance_count"] != batch["instances_with_all_layers_attempted"] + batch["partial_instance_count"]
            or batch["instances_with_results"] != len(result_instance_keys)
            or batch["instances_with_results"] > batch["started_instance_count"]
        ):
            raise ContractError("artifact_changed", "worker batch summary counts differ from sealed Results")
        seal_value["batch_complete"] = batch["remaining_instance_count"] == 0 and batch["partial_instance_count"] == 0
        seal_value["batch_summary"] = {"ref": batch_ref, "summary": dict(batch)}
    if solver_returned_observed != seal_value["solver_returned"]:
        raise ContractError("artifact_changed", "WorkerSeal solver_returned differs from raw backend observations")
    if solver_returned_observed and not solver_invoked_observed:
        raise ContractError("artifact_changed", "backend reports a returned result without a solver invocation")
    seal_value["solver_invoked_observed"] = solver_invoked_observed
    seal_value["_verified_worker_failure"] = worker_failure
    seal_value["_verified_business_checker_diagnostics"] = business_checker_diagnostics
    return seal_value, seal_ref, result_refs


def _reconcile_run(store: CaseStore, case_id: str, run: Mapping[str, Any], prepared: Mapping[str, Any], manifest: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    current = dict(run)
    old_artifacts = {ref["path"] for ref in current["results"]}
    if current["worker"]["seal"] is not None:
        old_seal = validate_ref_shape(current["worker"]["seal"], pointer="/run/worker/seal")
        old_seal_value = strict_json_loads(store.read_ref(old_seal))
        if isinstance(old_seal_value, dict) and isinstance(old_seal_value.get("output_refs"), list):
            old_artifacts.update(
                validate_ref_shape(item)["path"]
                for item in old_seal_value["output_refs"]
            )
        old_artifacts.add(old_seal["path"])
    seal, seal_ref, result_refs = _read_worker_seal(store, case_id, current, prepared, manifest)
    if seal is None:
        current["_verified_batch_summary"] = None
        current["_verified_worker_failure"] = None
        current["_verified_business_checker_diagnostics"] = []
        if current["transport"]["response"] is not None and current["worker"]["status"] == "not_observed":
            current["worker"]["status"] = "incomplete"
        return current, []
    current["_verified_batch_summary"] = seal.get("batch_summary")
    current["_verified_worker_failure"] = seal.get("_verified_worker_failure")
    current["_verified_business_checker_diagnostics"] = seal.get("_verified_business_checker_diagnostics", [])
    current["worker"] = {
        "status": "complete" if seal["worker_completed"] and seal.get("batch_complete") is not False else "incomplete",
        "seal": seal_ref,
        "solver_invoked": seal["solver_invoked_observed"],
    }
    current["results"] = result_refs
    artifacts = [*result_refs, *[validate_ref_shape(item) for item in seal["output_refs"]], seal_ref]
    unique = {item["path"]: item for item in artifacts}
    return current, [unique[path] for path in sorted(unique) if path not in old_artifacts]


def _transport_receipt(value: Any) -> tuple[dict[str, Any], bytes]:
    if not isinstance(value, dict) or set(value) != _RECEIPT_FIELDS:
        raise ContractError("bridge_protocol_error", "transport receipt has invalid fields")
    receipt = dict(value)
    status = receipt["http_status"]
    if status is not None and (isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599):
        raise ContractError("bridge_protocol_error", "transport HTTP status is invalid")
    if not isinstance(receipt["body_complete"], bool) or receipt["send_observation"] not in {"not_sent", "attempted"}:
        raise ContractError("bridge_protocol_error", "transport receipt status fields are invalid")
    if receipt["error_code"] is not None and (not isinstance(receipt["error_code"], str) or not re.fullmatch(r"[a-z0-9_]{1,64}", receipt["error_code"])):
        raise ContractError("bridge_protocol_error", "transport error code is invalid")
    body = decode_base64(receipt["body_base64"], pointer="/receipt/body_base64")
    if receipt["send_observation"] == "not_sent" and (status is not None or body or receipt["body_complete"]):
        raise ContractError("bridge_protocol_error", "not_sent receipt contains an HTTP observation")
    return receipt, body


def _worker_failure_diagnostic(
    value: Any,
    ref: Mapping[str, Any],
    *,
    run_id: str,
    prepared_id: str,
    input_digest: str,
) -> dict[str, Any]:
    """Validate one failure artifact before exposing it as a current diagnosis.

    Older seals did not store a message. They remain readable, but the
    projection preserves only their recorded code, type, and immutable Ref.
    """
    if not isinstance(value, Mapping):
        raise ContractError("output_invalid", "worker failure must be an object")
    if (
        value.get("run_id") != run_id
        or value.get("prepared_id") != prepared_id
        or value.get("input_digest") != input_digest
    ):
        raise ContractError("artifact_changed", "worker failure identity does not match the run")
    error_code = value.get("error_code")
    error_type = value.get("error_type")
    if not isinstance(error_code, str):
        raise ContractError("output_invalid", "worker failure code is invalid")
    if not isinstance(error_type, str):
        raise ContractError("output_invalid", "worker failure type is invalid")
    diagnostic = {
        "kind": "worker_failure",
        # `_read_worker_seal` supplies this only after validating the exact
        # output Ref against the current seal and its run-owned tree.
        "ref": dict(ref),
        "error_code": error_code,
        "error_type": error_type,
    }
    if "message" in value:
        if not isinstance(value["message"], str):
            raise ContractError("output_invalid", "worker failure message is invalid")
        diagnostic["message"] = value["message"]
    if "details" in value:
        if not isinstance(value["details"], Mapping):
            raise ContractError("output_invalid", "worker failure details are invalid")
        diagnostic["details"] = dict(value["details"])
    return diagnostic


def _business_checker_diagnostic(warnings: Any, ref: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project one canonical, controlled checker diagnostic from Result warnings."""
    if not isinstance(warnings, list):
        return None
    for warning in warnings:
        if not isinstance(warning, str) or not warning.startswith(_BUSINESS_CHECKER_DIAGNOSTIC_PREFIX):
            continue
        payload = warning[len(_BUSINESS_CHECKER_DIAGNOSTIC_PREFIX):]
        try:
            value = strict_json_loads(payload)
            canonical_payload = canonical_json_bytes(value).decode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            continue
        if (
            not isinstance(value, dict)
            or set(value) != {"code", "message", "pointer"}
            or not isinstance(value["code"], str)
            or not value["code"]
            or not isinstance(value["message"], str)
            or value["pointer"] is not None and not isinstance(value["pointer"], str)
            or canonical_payload != payload
        ):
            continue
        return {"kind": "business_checker_diagnostic", "ref": dict(ref), **value}
    return None


def _run_response(
    store: CaseStore,
    case_id: str,
    run: Mapping[str, Any],
) -> tuple[dict[str, Any], bytes, dict[str, Any] | None, bytes | None] | None:
    transport = run.get("transport")
    if not isinstance(transport, Mapping):
        return None
    value = transport.get("response")
    if value is None:
        return None
    try:
        ref = validate_ref_shape(value, pointer="/run/transport/response")
        run_parts = PurePosixPath(f".reaslab/optagent/cases/{case_id}/runs/{run['run_id']}").parts
        response_parts = PurePosixPath(ref["path"]).parts
        if response_parts[: len(run_parts)] != run_parts or len(response_parts) <= len(run_parts):
            return None
        raw = store.read_ref(ref)
    except ContractError:
        # Diagnostics are optional. Preserve a readable historical Run when
        # its nested observation Ref is absent, stale, malformed, or external.
        return None
    receipt_path = f".reaslab/optagent/cases/{case_id}/runs/{run['run_id']}/transport/execute-receipt.json"
    if ref["path"] != receipt_path:
        return ref, raw, None, None
    try:
        receipt_value = strict_json_loads(raw, max_bytes=len(raw))
        receipt, body = _transport_receipt(receipt_value)
    except (ContractError, ValueError, UnicodeError):
        return None
    return ref, raw, receipt, body


def _transport_diagnostic(
    store: CaseStore,
    case_id: str,
    run: Mapping[str, Any],
) -> dict[str, Any] | None:
    response = _run_response(store, case_id, run)
    if response is None:
        return None
    ref, _, receipt, body = response
    if receipt is None or body is None:
        return {"kind": "transport_observation", "ref": ref}
    diagnostic: dict[str, Any] = {
        "kind": "execute_receipt",
        "ref": ref,
        "send_observation": receipt["send_observation"],
        "http_status": receipt["http_status"],
        "body_complete": receipt["body_complete"],
        "error_code": receipt["error_code"],
    }
    if _valid_execute_envelope(
        receipt["http_status"], receipt["body_complete"], receipt["error_code"], body
    ):
        envelope = strict_json_loads(body)
        diagnostic["execute_success"] = envelope["success"]
        diagnostic["exit_code"] = envelope["exit_code"]
        if envelope["error"] is not None:
            diagnostic["message"] = envelope["error"]
        elif not envelope["success"] or envelope["exit_code"] not in (None, 0):
            diagnostic["message"] = envelope["output"]
    return diagnostic


def _run_diagnostics(
    store: CaseStore,
    case_id: str,
    run: Mapping[str, Any],
    *,
    worker_failure: Mapping[str, Any] | None = None,
    business_checker_diagnostics: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = [dict(item) for item in business_checker_diagnostics]
    if worker_failure is not None:
        diagnosis = dict(worker_failure)
        response = _run_response(store, case_id, run)
        if response is not None:
            response_ref, _, receipt, _ = response
            if receipt is not None:
                diagnosis["receipt_ref"] = response_ref
        diagnostics.append(diagnosis)
    elif run.get("worker", {}).get("seal") is None:
        transport = _transport_diagnostic(store, case_id, run)
        if transport is not None:
            diagnostics.append(transport)
    return diagnostics


def _valid_execute_envelope(status: int | None, body_complete: bool, error_code: str | None, body: bytes) -> bool:
    if status is None or not 200 <= status <= 299 or not body_complete or error_code is not None:
        return False
    try:
        envelope = strict_json_loads(body)
    except (ValueError, UnicodeError):
        return False
    if not isinstance(envelope, dict) or set(envelope) != {"success", "output", "error", "exit_code"}:
        return False
    return (
        isinstance(envelope["success"], bool)
        and isinstance(envelope["output"], str)
        and (envelope["error"] is None or isinstance(envelope["error"], str))
        and (envelope["exit_code"] is None or (isinstance(envelope["exit_code"], int) and not isinstance(envelope["exit_code"], bool)))
    )


def _locate_run(store: CaseStore, case_id: str, run_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    state = store.read_case(case_id)
    if run_id not in state["run_ids"]:
        raise ContractError("not_found", "run is not registered on this case")
    run, run_ref = _load_run_record(store, case_id, run_id, state=state)
    return state, run, run_ref


def _inspect(store: CaseStore, args: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    case_id = require_id(args["case_id"], pointer="/case_id")
    run_id = require_id(args["run_id"], pointer="/run_id")
    state, run, run_ref = _locate_run(store, case_id, run_id)
    prepared, _, manifest = _load_prepared(store, case_id, run["prepared_id"])
    try:
        reconciled, new_artifacts = _reconcile_run(store, case_id, run, prepared, manifest)
    except ContractError as exc:
        invalid_marked = False

        def mark_invalid(latest: dict[str, Any]) -> None:
            nonlocal invalid_marked
            if latest["worker"]["status"] not in {"complete", "invalid"}:
                latest["worker"] = {
                    "status": "invalid",
                    "seal": latest["worker"]["seal"],
                    "solver_invoked": latest["worker"]["solver_invoked"],
                }
                invalid_marked = True

        invalid_run, invalid_ref = _publish_run_update(
            store, case_id, run_id, mark_invalid, expected_nonce=run["dispatch_nonce"]
        )
        details = {**exc.details, "run": _run_summary(invalid_run, invalid_ref)}
        response = _run_response(store, case_id, run)
        if response is not None:
            response_ref, _, receipt, _ = response
            if receipt is not None:
                details["receipt_ref"] = response_ref
        raise ContractError(
            exc.code,
            exc.message,
            pointer=exc.pointer,
            details=details,
            effect="local_committed" if invalid_marked else exc.effect,
        ) from exc

    batch_summary = reconciled.get("_verified_batch_summary")

    def merge_worker(latest: dict[str, Any]) -> None:
        # Preserve a concurrent actual receipt/cancel and merge only
        # worker-owned facts from the completed artifact inspection.
        latest["worker"] = reconciled["worker"]
        latest["results"] = reconciled["results"]
        if latest["dispatch_state"] == "requested":
            latest["dispatch_state"] = "unknown"

    run, run_ref = _publish_run_update(store, case_id, run_id, merge_worker, expected_nonce=run["dispatch_nonce"])
    state = store.read_case(case_id)

    start_after = require_int(args.get("after_instance", 0), pointer="/after_instance")
    limit = require_int(args.get("limit", 20), pointer="/limit", minimum=1)
    if limit > 100:
        raise ContractError("invalid_argument", "limit must not exceed 100", pointer="/limit")
    page = _page_results_by_instance(run["results"], after_instance=start_after, limit=limit)
    result_paths = {ref["path"] for ref in page["result_refs"]}
    page_business_checker_diagnostics = [
        item
        for item in reconciled.get("_verified_business_checker_diagnostics", [])
        if isinstance(item, Mapping)
        and isinstance(item.get("ref"), Mapping)
        and item["ref"].get("path") in result_paths
    ]
    return {
        "kind": "complete",
        "reply": public_success(
            "optagent.run",
            "inspect",
            case_id=case_id,
            state_revision=state["state_revision"],
            result={
                "run": _run_summary(run, run_ref),
                "result_refs": page["result_refs"],
                "diagnostics": _run_diagnostics(
                    store,
                    case_id,
                    reconciled,
                    worker_failure=reconciled.get("_verified_worker_failure"),
                    business_checker_diagnostics=page_business_checker_diagnostics,
                ),
                "new_artifacts": new_artifacts,
                "batch_summary": batch_summary,
                "total": page["instance_total"],
                "result_total": page["result_total"],
                "truncated": page["truncated"],
                "next_after": page["next_after"],
            },
        ),
    }


def _page_results_by_instance(
    result_refs: Sequence[Mapping[str, Any]], *, after_instance: int, limit: int
) -> dict[str, Any]:
    """Page whole instances so every available lexicographic layer travels together."""
    refs_by_instance: dict[int, list[dict[str, Any]]] = {}
    for ref in result_refs:
        path = ref.get("path")
        match = re.search(r"/instances/(\d{6})/", path) if isinstance(path, str) else None
        if match is None:
            raise ContractError("output_invalid", "sealed Result path has no instance index")
        refs_by_instance.setdefault(int(match.group(1)), []).append(dict(ref))
    instance_ids = sorted(index for index in refs_by_instance if index >= after_instance)
    selected = instance_ids[:limit]
    truncated = len(instance_ids) > limit
    return {
        "result_refs": [ref for index in selected for ref in refs_by_instance[index]],
        "instance_total": len(refs_by_instance),
        "result_total": len(result_refs),
        "truncated": truncated,
        "next_after": selected[-1] + 1 if truncated and selected else None,
    }


def _cancel(store: CaseStore, args: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    stamp = require_stamp(context["stamp"])
    case_id = require_id(args["case_id"], pointer="/case_id")
    run_id = require_id(args["run_id"], pointer="/run_id")
    state, run, _ = _locate_run(store, case_id, run_id)
    from optagent_core.api import _require_owner

    _require_owner(state, stamp)
    if state["state_revision"] != args["expected_state_revision"]:
        raise ContractError("stale_revision", "case revision changed", details={"actual": state["state_revision"]})
    local_process = run.get("local_process")
    is_local = isinstance(local_process, dict)
    if is_local and run["worker"]["status"] == "complete":
        _, run_ref = _load_run_record(store, case_id, run_id)
        return {
            "kind": "complete",
            "reply": public_success(
                "optagent.run",
                "cancel",
                case_id=case_id,
                state_revision=state["state_revision"],
                result={
                    "state": _state_summary(store, state),
                    "run": _run_summary(run, run_ref),
                    "actor_cancelled": False,
                    "local_stop": "already_completed",
                },
            ),
        }
    if run["cancel"]["requested"]:
        _, run_ref = _load_run_record(store, case_id, run_id)
        return {
            "kind": "complete",
            "reply": public_success(
                "optagent.run",
                "cancel",
                case_id=case_id,
                state_revision=state["state_revision"],
                result={
                    "state": _state_summary(store, state),
                    "run": _run_summary(run, run_ref),
                    "actor_cancelled": False,
                    **({"local_stop": "confirmed" if run["cancel"].get("stop_ack") else "requested_not_confirmed"} if is_local else {"remote_stop": "not_available"}),
                },
                limitations=[] if is_local else [{"code": "remote_cancel_unavailable", "message": "the execute platform did not return a verified execution ID"}],
            ),
        }
    evidence = {
        "origin": "product_tool",
        "session_id": stamp["session_id"],
        "event_id": stamp["call_id"],
        "turn_id": stamp["turn_id"],
        "call_id": stamp["call_id"],
    }

    with store.run_lock(run_id):
        fresh_state = store.read_case(case_id)
        if fresh_state["state_revision"] != args["expected_state_revision"]:
            raise ContractError("stale_revision", "case revision changed", details={"actual": fresh_state["state_revision"]})
        if fresh_state["owner_session_id"] != stamp["session_id"] or run_id not in fresh_state["run_ids"]:
            raise ContractError("permission_denied", "run cancellation is outside the current owner session")
        latest, old_ref = _load_run_record(store, case_id, run_id, state=fresh_state)
        if not _stamp_equal(latest.get("created_by"), run["created_by"]):
            raise ContractError("artifact_changed", "run stamp changed while cancelling")
        if latest["cancel"]["requested"]:
            # Another cancel committed while this call was preparing its
            # business CAS. It is a no-op and must not bump the Case revision.
            updated_state = fresh_state
            state_revision = fresh_state["state_revision"]
            run, run_ref = latest, old_ref
        else:
            next_run = copy.deepcopy(latest)
            next_run["cancel"]["requested"] = True
            next_run["cancel"]["evidence"] = evidence
            run_ref = store.write_family_version(case_id, "runs", run_id, next_run)

            def fence(current: dict[str, Any]) -> None:
                records = current.get("current_records")
                if records is None:
                    records = {"questions": {}, "reviews": {}, "runs": {}, "presentation": None}
                    current["current_records"] = records
                if records["runs"].get(run_id) != old_ref:
                    raise ContractError("stale_revision", "run changed while cancelling")
                if current["owner_session_id"] != stamp["session_id"] or run_id not in current["run_ids"]:
                    raise ContractError("permission_denied", "run cancellation is outside the current owner session")
                records["runs"][run_id] = dict(run_ref)
                current["promotion_generation"] += 1
                current["decisions"].append({
                    "kind": "run_cancel_requested",
                    "note": args["reason"],
                    "refs": [latest["worker_invocation"]],
                    "stamp": dict(stamp),
                    "created_at": utc_now(),
                })

            updated_state = store.cas_case(case_id, fresh_state["state_revision"], fence)
            state_revision = updated_state["state_revision"]
            run = next_run
    return {
        "kind": "complete",
        "reply": public_success(
            "optagent.run",
            "cancel",
            case_id=case_id,
            state_revision=state_revision,
            result={
                "state": _state_summary(store, updated_state),
                "run": _run_summary(run, run_ref),
                "actor_cancelled": False,
                **({"local_stop": "confirmed" if run["cancel"].get("stop_ack") else "requested_not_confirmed"} if is_local else {"remote_stop": "not_available"}),
            },
            limitations=[] if is_local else [{"code": "remote_cancel_unavailable", "message": "the execute platform did not return a verified execution ID"}],
        ),
    }


def run_tool(store: CaseStore, arguments: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    """Fixed optagent.run dispatch called only by the trusted API module."""
    op = arguments.get("op")
    if op == "prepare":
        return _prepare(store, arguments, context)
    if op == "start":
        return _begin_start(store, arguments, context)
    if op == "inspect":
        return _inspect(store, arguments, context)
    if op == "cancel":
        return _cancel(store, arguments, context)
    raise ContractError("unknown_operation", "unknown run operation")


def _bridge_complete(reply: Mapping[str, Any]) -> dict[str, Any]:
    return {"kind": "complete", "reply": dict(reply)}


def _bridge_failure(
    op: str,
    error: ContractError,
    *,
    case_id: str | None = None,
    state_revision: int | None = None,
) -> dict[str, Any]:
    return _bridge_complete(
        public_failure(
            "optagent.run",
            op,
            error,
            case_id=case_id,
            state_revision=state_revision,
        )
    )


def _check_bridge_identity(
    store: CaseStore,
    phase: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    kind: str,
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    stamp = require_stamp(context["stamp"])
    case_id = require_id(phase.get("case_id"), pointer="/phase/case_id")
    run_id = require_id(phase.get("run_id"), pointer="/phase/run_id")
    state, run, _ = _locate_run(store, case_id, run_id)
    if kind in {"start_recheck", "start_receipt"}:
        if not _stamp_equal(run.get("created_by"), stamp):
            raise ContractError("permission_denied", "bridge stamp does not match the original run call")
        prepared_id = require_id(phase.get("prepared_id"), pointer="/phase/prepared_id")
        if run.get("prepared_id") != prepared_id:
            raise ContractError("bridge_protocol_error", "bridge prepared ID does not match the run")
    else:
        stop = run["cancel"].get("stop_attempt")
        if not isinstance(stop, dict) or not _stamp_equal(stop.get("stamp"), stamp):
            raise ContractError("permission_denied", "cancel bridge stamp does not match its stop attempt")
        prepared_id = run["prepared_id"]
    nonce = require_id(phase.get("dispatch_nonce"), pointer="/phase/dispatch_nonce")
    if run.get("dispatch_nonce") != nonce:
        raise ContractError("bridge_protocol_error", "bridge nonce does not match the run")
    digest = require_hash(phase.get("request_digest"), pointer="/phase/request_digest")
    if kind in {"start_recheck", "start_receipt"}:
        expected_digest = (
            run.get("local_process", {}).get("request_digest")
            if context.get("local_mode") is True
            else run.get("http_request_digest")
        )
    else:
        expected_digest = run["cancel"]["stop_attempt"].get("request_digest")
    if expected_digest != digest:
        raise ContractError("bridge_protocol_error", "bridge request digest does not match the run")
    return case_id, state, run, _load_prepared(store, case_id, prepared_id)[0]


def _mark_without_dispatch(
    store: CaseStore,
    case_id: str,
    run_id: str,
    *,
    expected_nonce: str,
    error: ContractError,
) -> dict[str, Any]:
    def mark(run: dict[str, Any]) -> None:
        if run["dispatch_state"] != "requested":
            raise ContractError("execution_unknown", "the one-time run dispatch gate was already consumed", details={"run_id": run_id})
        run["dispatch_state"] = "without_dispatch"
        run["transport"]["error_code"] = error.code

    _publish_run_update(store, case_id, run_id, mark, expected_nonce=expected_nonce)
    latest_state = store.read_case(case_id)
    return _bridge_failure(
        "start",
        ContractError(error.code, error.message, pointer=error.pointer, details={"run_id": run_id, **error.details}, effect="local_committed"),
        case_id=case_id,
        state_revision=latest_state["state_revision"],
    )


def _start_recheck(store: CaseStore, phase: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    expected = {"kind", "case_id", "run_id", "prepared_id", "dispatch_nonce", "request_digest"}
    if set(phase) != expected:
        raise ContractError("bridge_protocol_error", "start_recheck phase has invalid fields")
    case_id, state, run, prepared = _check_bridge_identity(store, phase, context, kind="start_recheck")
    run_id = run["run_id"]
    nonce = run["dispatch_nonce"]
    observed_run, observed_run_ref = _load_run_record(store, case_id, run_id, state=state)
    observed_head = _latest_run_head(store, case_id, state)
    if run["dispatch_state"] != "requested":
        raise ContractError("execution_unknown", "the one-time run dispatch gate was already consumed", details={"run_id": run_id})
    if observed_head is None or observed_head[0] != run_id or observed_head[2] != observed_run_ref:
        return _mark_without_dispatch(
            store,
            case_id,
            run_id,
            expected_nonce=nonce,
            error=ContractError("stale_revision", "a newer start attempt was registered before dispatch"),
        )
    if context.get("cancellation_observed"):
        return _mark_without_dispatch(
            store,
            case_id,
            run_id,
            expected_nonce=nonce,
            error=ContractError("stale_revision", "the original start call was cancelled before HTTP dispatch"),
        )
    try:
        _validate_owner_and_barrier(store, case_id, state, context["stamp"])
        manifest = _load_prepared(store, case_id, prepared["prepared_id"])[2]
        if (
            state["source_revision"] != prepared["source_revision"]
            or state["candidate_revision"] != prepared["candidate_revision"]
            or state["candidate_id"] != prepared["candidate_id"]
            or state["promotion_generation"] != prepared["promotion_generation"]
            or run_id not in state["run_ids"]
        ):
            raise ContractError("stale_revision", "case changed after run begin")
        candidate, _ = _json_ref(
            store,
            f".reaslab/optagent/cases/{case_id}/candidates/{prepared['candidate_id']}.json",
            "candidate record",
        )
        if candidate.get("binding_digest") != manifest["binding_digest"]:
            raise ContractError("candidate_dirty", "current candidate differs from the frozen input binding")
        verify_runtime_bundle(
            store.workspace,
            manifest["runtime_bundle"]["digest"],
            manifest["runtime_bundle"]["manifest"],
            prepared_schema_version=manifest["schema_version"],
        )
        plan = _dispatch_plan(store, case_id, run, prepared)
    except ContractError as exc:
        return _mark_without_dispatch(store, case_id, run_id, expected_nonce=nonce, error=exc)

    # The expensive byte/data recheck above holds no lock. Both immutable
    # outcomes are prepared before the final session→run→Case publication.
    stale_at_fence = False
    ready = copy.deepcopy(observed_run)
    ready["dispatch_state"] = "unknown"
    rejected = copy.deepcopy(observed_run)
    rejected["dispatch_state"] = "without_dispatch"
    rejected["transport"]["error_code"] = "stale_revision"
    ready_ref = store.write_family_version(case_id, "runs", run_id, ready)
    rejected_ref = store.write_family_version(case_id, "runs", run_id, rejected)
    from optagent_core.api import _SessionRouteFence, _with_session_route_commit

    def commit_recheck(fence: _SessionRouteFence) -> dict[str, Any]:
        nonlocal stale_at_fence

        def consume(current_case: dict[str, Any]) -> None:
            nonlocal stale_at_fence
            try:
                fence.validate_current(current_case, context["stamp"], require_no_pending=True)
            except ContractError:
                stale_at_fence = True
            run_ids = current_case.get("run_ids", [])
            latest_id = run_ids[-1] if run_ids else None
            latest_ref = (current_case.get("current_records") or {}).get("runs", {}).get(latest_id) if latest_id else None
            stale_at_fence = stale_at_fence or (
                current_case["owner_session_id"] != context["stamp"]["session_id"]
                or current_case["source_revision"] != prepared["source_revision"]
                or current_case["candidate_revision"] != prepared["candidate_revision"]
                or current_case["candidate_id"] != prepared["candidate_id"]
                or current_case["promotion_generation"] != prepared["promotion_generation"]
                or latest_id != run_id
                or latest_ref != observed_run_ref
                or run_id not in run_ids
                or bool(current_case["pending_inputs"])
            )
            records = current_case.get("current_records")
            if records is None:
                records = {"questions": {}, "reviews": {}, "runs": {}, "presentation": None}
                current_case["current_records"] = records
            records["runs"][run_id] = dict(rejected_ref if stale_at_fence else ready_ref)

        with store.run_lock(run_id):
            return store.cas_case_family(
                case_id,
                "runs",
                run_id,
                observed_run_ref,
                consume,
                legacy_path=_run_path(case_id, run_id),
            )

    _with_session_route_commit(store, state, context["stamp"], commit_recheck)
    if stale_at_fence:
        return _bridge_failure(
            "start",
            ContractError("stale_revision", "case changed at the final dispatch fence", details={"run_id": run_id}, effect="local_committed"),
            case_id=case_id,
            state_revision=store.read_case(case_id)["state_revision"],
        )
    return {"kind": "start_ready", "plan": plan}


def _start_receipt(store: CaseStore, phase: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    expected = {"kind", "case_id", "run_id", "prepared_id", "dispatch_nonce", "request_digest", "receipt"}
    if set(phase) != expected:
        raise ContractError("bridge_protocol_error", "start_receipt phase has invalid fields")
    case_id, state, run, prepared = _check_bridge_identity(store, phase, context, kind="start_receipt")
    if context.get("local_mode") is True:
        return _start_local_process_receipt(store, case_id, run, prepared, phase, context)
    receipt, body = _transport_receipt(phase["receipt"])
    receipt_raw = canonical_json_bytes(receipt)
    receipt_path = f".reaslab/optagent/cases/{case_id}/runs/{run['run_id']}/transport/execute-receipt.json"
    try:
        receipt_ref = store.write_immutable(receipt_path, receipt_raw, "application/json")
    except ContractError as exc:
        if exc.code == "artifact_changed":
            raise ContractError("bridge_protocol_error", "conflicting execute receipts cannot replace one another") from exc
        raise

    valid_envelope = _valid_execute_envelope(
        receipt["http_status"],
        receipt["body_complete"],
        receipt["error_code"],
        body,
    )

    def merge_receipt(latest: dict[str, Any]) -> None:
        if not _stamp_equal(latest.get("created_by"), context["stamp"]) or latest["http_request_digest"] != phase["request_digest"]:
            raise ContractError("bridge_protocol_error", "execute receipt identity changed while persisting")
        prior = latest["transport"]["response"]
        if prior is not None and validate_ref_shape(prior) != receipt_ref:
            old_raw = store.read_ref(prior)
            if old_raw != receipt_raw:
                raise ContractError("bridge_protocol_error", "a run already has a different execute receipt")
        if receipt["send_observation"] == "not_sent":
            latest["dispatch_state"] = "without_dispatch"
        elif valid_envelope:
            latest["dispatch_state"] = "response_received"
        else:
            latest["dispatch_state"] = "unknown"
        latest["transport"] = {
            "http_status": receipt["http_status"],
            "response": receipt_ref,
            "error_code": receipt["error_code"],
        }

    run, _ = _publish_run_update(store, case_id, run["run_id"], merge_receipt, expected_nonce=phase["dispatch_nonce"])

    new_artifacts: list[dict[str, Any]] = []
    reconcile_error: ContractError | None = None
    try:
        reconciled, new_artifacts = _reconcile_run(store, case_id, run, prepared, _load_prepared(store, case_id, run["prepared_id"])[2])
    except ContractError as exc:
        reconcile_error = exc
        reconciled = dict(run)
        reconciled["worker"] = {"status": "invalid", "seal": run["worker"]["seal"], "solver_invoked": run["worker"]["solver_invoked"]}
    def merge_reconciled(latest: dict[str, Any]) -> None:
        if latest["worker"]["status"] not in {"complete", "invalid"}:
            latest["worker"] = reconciled["worker"]
            latest["results"] = reconciled["results"]

    run, run_ref = _publish_run_update(store, case_id, run["run_id"], merge_reconciled, expected_nonce=run["dispatch_nonce"])
    if reconcile_error is not None:
        details = dict(reconcile_error.details)
        details["receipt_ref"] = receipt_ref
        details["run"] = _run_summary(run, run_ref)
        raise ContractError(
            reconcile_error.code,
            reconcile_error.message,
            pointer=reconcile_error.pointer,
            details=details,
            effect="local_committed",
        ) from reconcile_error

    now_state = store.read_case(case_id)
    limitations = []
    if run["dispatch_state"] == "unknown":
        limitations.append({"code": "execution_unknown", "message": "the HTTP attempt has no complete valid execute response"})
    if run["worker"]["status"] in {"invalid", "not_observed"} or run["worker"]["seal"] is None:
        limitations.append({"code": "worker_result_unavailable", "message": "a complete matching worker seal is not available"})
    return {
        "kind": "complete",
        "reply": public_success(
            "optagent.run",
            "start",
            case_id=case_id,
            state_revision=now_state["state_revision"],
            result={
                "state": _state_summary(store, now_state),
                "run": _run_summary(run, run_ref),
                "result_refs": [dict(ref) for ref in run["results"]],
                "diagnostics": _run_diagnostics(
                    store,
                    case_id,
                    reconciled,
                    worker_failure=reconciled.get("_verified_worker_failure"),
                ),
                "new_artifacts": new_artifacts,
            },
            limitations=limitations,
        ),
    }


def _start_local_process_receipt(
    store: CaseStore,
    case_id: str,
    run: Mapping[str, Any],
    prepared: Mapping[str, Any],
    phase: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    receipt = phase["receipt"]
    fields = {
        "schema_version", "request_digest", "observation", "pid", "returncode",
        "timed_out", "stop_reason", "stop_confirmed", "started_at", "completed_at",
        "stdout_base64", "stderr_base64", "error_code",
    }
    if not isinstance(receipt, dict) or set(receipt) != fields or receipt.get("schema_version") != 2:
        raise ContractError("bridge_protocol_error", "local process receipt has invalid fields")
    if receipt["request_digest"] != phase["request_digest"]:
        raise ContractError("bridge_protocol_error", "local process receipt digest does not match the run")
    if receipt["observation"] not in {"not_started", "spawned"}:
        raise ContractError("bridge_protocol_error", "local process observation is invalid")
    if receipt["observation"] == "not_started":
        if receipt["pid"] is not None or receipt["returncode"] is not None or receipt["timed_out"]:
            raise ContractError("bridge_protocol_error", "unstarted local process receipt contains process state")
        if receipt["stop_reason"] is not None or receipt["stop_confirmed"] is not None:
            raise ContractError("bridge_protocol_error", "unstarted local process receipt contains stop state")
    elif isinstance(receipt["pid"], bool) or not isinstance(receipt["pid"], int) or receipt["pid"] <= 0:
        raise ContractError("bridge_protocol_error", "spawned local process receipt has no valid PID")
    if receipt["returncode"] is not None and isinstance(receipt["returncode"], bool):
        raise ContractError("bridge_protocol_error", "local process return code is invalid")
    if receipt["returncode"] is not None and not isinstance(receipt["returncode"], int):
        raise ContractError("bridge_protocol_error", "local process return code is invalid")
    if not isinstance(receipt["timed_out"], bool):
        raise ContractError("bridge_protocol_error", "local process timeout marker is invalid")
    if receipt["stop_reason"] not in {None, "timeout", "cancel_requested", "keyboard_interrupt"}:
        raise ContractError("bridge_protocol_error", "local process stop reason is invalid")
    if receipt["stop_reason"] is None:
        if receipt["stop_confirmed"] is not None:
            raise ContractError("bridge_protocol_error", "local process has a stop result without a stop request")
    elif not isinstance(receipt["stop_confirmed"], bool):
        raise ContractError("bridge_protocol_error", "local process stop confirmation is invalid")
    if receipt["timed_out"] != (receipt["stop_reason"] == "timeout"):
        raise ContractError("bridge_protocol_error", "local process timeout and stop reason disagree")
    for key in ("started_at", "completed_at"):
        if receipt[key] is not None and not isinstance(receipt[key], str):
            raise ContractError("bridge_protocol_error", "local process timestamp is invalid")
    if receipt["observation"] == "spawned" and (not receipt["started_at"] or not receipt["completed_at"]):
        raise ContractError("bridge_protocol_error", "spawned local process receipt lacks timestamps")
    for key in ("stdout_base64", "stderr_base64"):
        if not isinstance(receipt[key], str):
            raise ContractError("bridge_protocol_error", "local process output is invalid")
        decode_base64(receipt[key], pointer=f"/receipt/{key}")
    allowed_errors = {"spawn_failed", "timeout", "worker_failed", "cancelled", "interrupted", "termination_unconfirmed"}
    if receipt["error_code"] is not None and receipt["error_code"] not in allowed_errors:
        raise ContractError("bridge_protocol_error", "local process error code is invalid")
    if receipt["stop_reason"] is not None:
        expected_error = (
            "timeout" if receipt["stop_reason"] == "timeout"
            else "cancelled" if receipt["stop_reason"] == "cancel_requested"
            else "interrupted"
        )
        if receipt["stop_confirmed"] is True:
            if receipt["returncode"] is None or receipt["error_code"] != expected_error:
                raise ContractError("bridge_protocol_error", "confirmed local process stop disagrees with its exit receipt")
        elif receipt["error_code"] != "termination_unconfirmed":
            raise ContractError("bridge_protocol_error", "unconfirmed local process stop has a confirmed error code")
    receipt_raw = canonical_json_bytes(receipt)
    receipt_path = f".reaslab/optagent/cases/{case_id}/runs/{run['run_id']}/local-process/execute-receipt.json"
    receipt_ref = store.write_immutable(receipt_path, receipt_raw, "application/json")

    def merge_receipt(latest: dict[str, Any]) -> None:
        local = latest.get("local_process")
        if not isinstance(local, dict) or local.get("request_digest") != phase["request_digest"]:
            raise ContractError("bridge_protocol_error", "local process binding changed while persisting receipt")
        prior = local.get("receipt")
        if prior is not None and validate_ref_shape(prior) != receipt_ref:
            if store.read_ref(prior) != receipt_raw:
                raise ContractError("bridge_protocol_error", "a run already has a different local process receipt")
        local["receipt"] = receipt_ref
        local["stop_confirmed"] = receipt["stop_confirmed"]
        local["status"] = (
            "not_started" if receipt["observation"] == "not_started"
            else "stop_unconfirmed" if receipt["error_code"] == "termination_unconfirmed"
            else "timed_out" if receipt["timed_out"]
            else "cancelled" if receipt["error_code"] == "cancelled"
            else "interrupted" if receipt["error_code"] == "interrupted"
            else "completed"
        )
        if receipt["observation"] == "not_started":
            latest["dispatch_state"] = "without_dispatch"
        elif receipt["timed_out"] or receipt["stop_reason"] is not None:
            latest["dispatch_state"] = "unknown"
        else:
            latest["dispatch_state"] = "response_received"
        if (
            receipt["stop_reason"] == "cancel_requested"
            and receipt["stop_confirmed"] is True
            and receipt["returncode"] is not None
        ):
            latest["cancel"]["stop_ack"] = {
                "kind": "local_process_exit",
                "receipt": dict(receipt_ref),
                "pid": receipt["pid"],
                "returncode": receipt["returncode"],
            }

    run, _ = _publish_run_update(store, case_id, run["run_id"], merge_receipt, expected_nonce=phase["dispatch_nonce"])
    new_artifacts: list[dict[str, Any]] = []
    reconcile_error: ContractError | None = None
    try:
        reconciled, new_artifacts = _reconcile_run(
            store, case_id, run, prepared, _load_prepared(store, case_id, run["prepared_id"])[2]
        )
    except ContractError as exc:
        reconcile_error = exc
        reconciled = dict(run)
        reconciled["worker"] = {
            "status": "invalid",
            "seal": run["worker"]["seal"],
            "solver_invoked": run["worker"]["solver_invoked"],
        }

    def merge_reconciled(latest: dict[str, Any]) -> None:
        if latest["worker"]["status"] not in {"complete", "invalid"}:
            latest["worker"] = reconciled["worker"]
            latest["results"] = reconciled["results"]

    run, run_ref = _publish_run_update(store, case_id, run["run_id"], merge_reconciled, expected_nonce=run["dispatch_nonce"])
    if reconcile_error is not None:
        raise ContractError(
            reconcile_error.code,
            reconcile_error.message,
            pointer=reconcile_error.pointer,
            details={**reconcile_error.details, "receipt_ref": receipt_ref, "run": _run_summary(run, run_ref)},
            effect="local_committed",
        ) from reconcile_error
    current_state = store.read_case(case_id)
    limitations = []
    if run["dispatch_state"] == "unknown":
        limitations.append({"code": "execution_unknown", "message": "local execution has no complete valid result; inspect the receipt before any new attempt"})
    if run["worker"]["status"] in {"invalid", "not_observed"} or run["worker"]["seal"] is None:
        limitations.append({"code": "worker_result_unavailable", "message": "a complete matching worker seal is not available"})
    return {
        "kind": "complete",
        "reply": public_success(
            "optagent.run",
            "start",
            case_id=case_id,
            state_revision=current_state["state_revision"],
            result={
                "state": _state_summary(store, current_state),
                "run": _run_summary(run, run_ref),
                "result_refs": [dict(ref) for ref in run["results"]],
                "diagnostics": _run_diagnostics(store, case_id, reconciled, worker_failure=reconciled.get("_verified_worker_failure")),
                "new_artifacts": new_artifacts,
            },
            limitations=limitations,
        ),
    }


def _cancel_receipt(store: CaseStore, phase: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    expected = {"kind", "case_id", "run_id", "dispatch_nonce", "request_digest", "receipt"}
    if set(phase) != expected:
        raise ContractError("bridge_protocol_error", "cancel_receipt phase has invalid fields")
    case_id, _, run, _ = _check_bridge_identity(store, phase, context, kind="cancel_receipt")
    stop = run["cancel"]["stop_attempt"]
    if stop is None:
        raise ContractError("bridge_protocol_error", "this run has no verified execution ID or stop attempt")
    if not _stamp_equal(stop.get("stamp"), context["stamp"]) or stop.get("request_digest") != phase["request_digest"]:
        raise ContractError("bridge_protocol_error", "cancel receipt does not match the original stop attempt")
    receipt, _ = _transport_receipt(phase["receipt"])
    receipt_raw = canonical_json_bytes(receipt)
    receipt_path = f".reaslab/optagent/cases/{case_id}/runs/{run['run_id']}/transport/cancel-receipt.json"
    receipt_ref = store.write_immutable(receipt_path, receipt_raw, "application/json")
    def merge_stop_receipt(latest: dict[str, Any]) -> None:
        current_stop = latest["cancel"]["stop_attempt"]
        if current_stop is None or not _stamp_equal(current_stop.get("stamp"), context["stamp"]) or current_stop.get("request_digest") != phase["request_digest"]:
            raise ContractError("bridge_protocol_error", "stop attempt changed before receipt persistence")
        current_stop["receipt"] = receipt_ref
        current_stop["state"] = "without_dispatch" if receipt["send_observation"] == "not_sent" else "unknown"

    try:
        _publish_run_update(store, case_id, run["run_id"], merge_stop_receipt, expected_nonce=phase["dispatch_nonce"])
    except ContractError as exc:
        raise ContractError(
            exc.code,
            exc.message,
            pointer=exc.pointer,
            details={**exc.details, "receipt_ref": receipt_ref},
            effect="remote_possible",
        ) from exc
    return _bridge_failure(
        "cancel",
        ContractError("execution_unknown", "remote stop receipt is preserved but the platform does not expose terminal status", details={"run_id": run["run_id"]}, effect="remote_possible"),
        case_id=case_id,
        state_revision=store.read_case(case_id)["state_revision"],
    )


def dispatch_run_bridge(store: CaseStore, phase: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    """Process only the three fixed private transport phases."""
    kind = phase.get("kind") if isinstance(phase, Mapping) else None
    try:
        if kind == "start_recheck":
            return _start_recheck(store, phase, context)
        if kind == "start_receipt":
            return _start_receipt(store, phase, context)
        if kind == "cancel_receipt":
            return _cancel_receipt(store, phase, context)
        raise ContractError("bridge_protocol_error", "unsupported private run phase")
    except ContractError as exc:
        op = "cancel" if kind == "cancel_receipt" else "start"
        case_id = phase.get("case_id") if isinstance(phase, Mapping) and isinstance(phase.get("case_id"), str) else None
        revision = None
        if case_id is not None:
            try:
                revision = store.read_case(case_id)["state_revision"]
            except ContractError:
                pass
        return _bridge_failure(op, exc, case_id=case_id, state_revision=revision)


__all__ = ["dispatch_run_bridge", "run_tool"]


