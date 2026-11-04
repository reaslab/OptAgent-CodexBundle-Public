import sys

if not sys.flags.isolated:
    print("Run this package entry with `python -I reproduce/run.py`; no package files were read.", file=sys.stderr)
    raise SystemExit(2)

import argparse
import ast
import contextlib
import hashlib
import importlib
import json
import math
import os
import re
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_LEGACY_RUNTIME_HELPERS = (
    "python/optagent_core/__init__.py",
    "python/optagent_core/contracts.py",
    "python/optagent_core/data.py",
    "python/optagent_core/models.py",
    "python/optagent_core/results.py",
    "python/optagent_core/worker.py",
)
_RUNTIME_HELPERS = (
    *_LEGACY_RUNTIME_HELPERS[:5],
    "python/optagent_core/solver_protocol.py",
    *_LEGACY_RUNTIME_HELPERS[5:],
)


def _runtime_helpers_for_prepared_schema(schema_version: Any) -> tuple[str, ...]:
    if type(schema_version) is int and schema_version == 2:
        return _LEGACY_RUNTIME_HELPERS
    if type(schema_version) is int and schema_version == 3:
        return _RUNTIME_HELPERS
    _fail("PreparedInputManifest schema has no fixed runtime helper set")
_PREPARED_FIELDS = {
    "schema_version", "prepared_id", "case_id", "source_revision", "candidate_revision", "candidate_id",
    "promotion_generation", "binding_digest", "sources", "candidate", "data", "runtime_bundle", "solve_options",
}
_RUNTIME_PACKAGE_FIELDS = {"digest", "manifest", "worker_entry", "helper_paths", "standalone_supported"}
_PACKAGE_FIELDS = {
    "schema_version", "case_id", "delivery_id", "selection_ref", "authority_records", "consumed_refs",
    "objects", "claims", "reviews", "claim_attachments", "data_bundles", "runtime_bundles",
    "reproductions", "ref_map", "entry",
}
_DELIVERY_FIELDS = {
    "schema_version", "created_at", "created_by", "delivery_id", "kind", "source_revision",
    "math_source_ids", "candidate_id", "promotion_generation", "claims", "evidence_refs",
    "review_ids", "proof_ids", "run_ids", "authority_records", "summary", "reports", "package",
}
_REVIEW_FIELDS = {
    "schema_version", "created_at", "created_by", "review_id", "review_revision", "case_id",
    "status", "binding", "raw_source_ids", "source_provenance_ref", "source_provenance_digest",
    "binding_evidence_refs", "prior_review_ref", "note", "candidate_opened", "answer_ref", "outcomes",
    "issues", "conclusion",
}
_REVIEW_OUTCOME_FIELDS = {"decision", "feedback", "remaining", "stamp", "created_at"}
_ISSUE_FIELDS = {
    "issue_id", "claim", "locations", "candidate_locations", "evidence_refs", "proposal_stamp",
    "owner_dispositions",
}
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _fail(message: str) -> None:
    raise ValueError(message)


def _supported_review_fields(review: Any) -> bool:
    if not isinstance(review, dict):
        return False
    return set(review) == _REVIEW_FIELDS


def _validate_review_state(review: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    outcomes = review.get("outcomes")
    answer_value = review.get("answer_ref")
    conclusion = review.get("conclusion")
    if not isinstance(outcomes, list):
        _fail("Review outcomes are invalid")
    for outcome in outcomes:
        if (
            not isinstance(outcome, dict)
            or set(outcome) != _REVIEW_OUTCOME_FIELDS
            or outcome.get("decision") not in {"needs_changes", "ready"}
            or not isinstance(outcome.get("feedback"), list)
            or not all(isinstance(item, str) and item for item in outcome["feedback"])
            or not isinstance(outcome.get("remaining"), list)
            or not all(isinstance(item, str) for item in outcome["remaining"])
            or not isinstance(outcome.get("stamp"), dict)
            or not isinstance(outcome.get("created_at"), str)
            or not outcome["created_at"]
        ):
            _fail("Review outcome fields are invalid")
        if outcome["decision"] == "needs_changes" and not outcome["feedback"]:
            _fail("Review needs-changes outcome has no feedback")
        if outcome["decision"] == "ready" and outcome["feedback"]:
            _fail("Review ready outcome contains feedback")

    if review.get("status") == "open":
        if answer_value is not None or conclusion is not None or any(
            outcome["decision"] != "needs_changes" for outcome in outcomes
        ):
            _fail("open Review has a closed outcome, answer, or conclusion")
        return outcomes, None

    if (
        review.get("status") != "closed"
        or not outcomes
        or outcomes[-1]["decision"] != "ready"
        or any(outcome["decision"] != "needs_changes" for outcome in outcomes[:-1])
        or answer_value is None
        or not isinstance(conclusion, dict)
        or set(conclusion) != {"claims", "remaining", "stamp"}
        or not isinstance(conclusion.get("claims"), list)
        or not isinstance(conclusion.get("remaining"), list)
        or not all(isinstance(item, str) for item in conclusion["remaining"])
        or not isinstance(conclusion.get("stamp"), dict)
        or conclusion["remaining"] != outcomes[-1]["remaining"]
        or conclusion["stamp"] != outcomes[-1]["stamp"]
    ):
        _fail("closed Review lacks a valid latest ready outcome, approved answer, or conclusion")
    return outcomes, _ref(answer_value)


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _json(raw: bytes, *, max_bytes: int = 4 * 1024 * 1024) -> Any:
    if len(raw) > max_bytes:
        _fail("package JSON exceeds the fixed size limit")
    value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object, parse_constant=_reject_constant, parse_float=_finite_float)

    def check_depth(item: Any, parent_depth: int = 0) -> None:
        if isinstance(item, dict):
            depth = parent_depth + 1
            children = item.values()
        elif isinstance(item, list):
            depth = parent_depth + 1
            children = item
        else:
            return
        if depth > 64:
            _fail("package JSON exceeds the fixed nesting limit")
        for child in children:
            check_depth(child, depth)

    check_depth(value)
    return value


def _is_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE)


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (int(info.st_dev), int(info.st_ino), stat.S_IFMT(info.st_mode), int(info.st_size), int(info.st_mtime_ns), int(info.st_ctime_ns))


def _checked_absolute(path: Path) -> Path:
    if not path.is_absolute():
        _fail("package paths must be absolute")
    absolute = Path(os.path.abspath(os.fspath(path)))
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor = cursor / part
        info = cursor.lstat()
        if _is_reparse(info):
            _fail("package paths cannot contain links or reparse points")
    return absolute


def _script_and_package() -> tuple[Path, Path]:
    script = _checked_absolute(Path(__file__))
    if script.name != "run.py" or script.parent.name != "reproduce":
        _fail("fixed reproduction entry must remain at reproduce/run.py")
    info = script.lstat()
    if _is_reparse(info) or not stat.S_ISREG(info.st_mode):
        _fail("fixed reproduction entry must be a regular file")
    root = script.parent.parent
    if not root.is_dir():
        _fail("extracted package root is unavailable")
    return script, root


def _safe_package_relative(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        _fail("package path is invalid")
    pure = PurePosixPath(value)
    if pure.is_absolute() or pure.as_posix() != value or any(part in {"", ".", ".."} for part in value.split("/")):
        _fail("package path escapes the extraction root")
    return value


def _read_stable(root: Path, relative: str) -> bytes:
    relative = _safe_package_relative(relative)
    parts = PurePosixPath(relative).parts
    cursor = root
    chain: list[tuple[Path, os.stat_result, bool]] = []
    for index, part in enumerate(parts):
        cursor = cursor / part
        info = cursor.lstat()
        last = index == len(parts) - 1
        expected_type = stat.S_ISREG(info.st_mode) if last else stat.S_ISDIR(info.st_mode)
        if _is_reparse(info) or not expected_type:
            _fail("package input is not a regular contained file")
        chain.append((cursor, info, last))
    before = chain[-1][1]
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(cursor, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _identity(before) != _identity(opened):
            _fail("package input changed while opening")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after_fd = os.fstat(fd)
        after_path = cursor.lstat()
        if _identity(opened) != _identity(after_fd) or _identity(opened) != _identity(after_path):
            _fail("package input changed while reading")
        for parent, expected, _ in chain[:-1]:
            current = parent.lstat()
            if _is_reparse(current) or _identity(expected) != _identity(current):
                _fail("package path changed while reading")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _ref(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"path", "sha256", "byte_len", "media_type"}:
        _fail("reference fields are invalid")
    path = value["path"]
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path or PurePosixPath(path).is_absolute() or any(part in {"", ".", ".."} for part in path.split("/")):
        _fail("reference path is invalid")
    if not isinstance(value["sha256"], str) or not _HASH_RE.fullmatch(value["sha256"]):
        _fail("reference hash is invalid")
    if type(value["byte_len"]) is not int or value["byte_len"] < 0:
        _fail("reference length is invalid")
    if not isinstance(value["media_type"], str) or not value["media_type"]:
        _fail("reference media type is invalid")
    return dict(value)


def _ref_key(value: Any) -> tuple[str, str, int, str]:
    ref = _ref(value)
    return (ref["path"], ref["sha256"], ref["byte_len"], ref["media_type"])


class _PackageReader:
    def __init__(self, root: Path, ref_map: list[dict[str, Any]]) -> None:
        self.root = root
        self._mapped: dict[tuple[str, str, int, str], str] = {}
        for item in ref_map:
            key = _ref_key(item["ref"])
            previous = self._mapped.get(key)
            if previous is not None and previous != item["package_path"]:
                _fail("one original Ref maps to multiple package files")
            self._mapped[key] = item["package_path"]

    def read_ref(self, ref: Any) -> bytes:
        key = _ref_key(ref)
        package_path = self._mapped.get(key)
        if package_path is None:
            _fail("required frozen Ref is not included in this package")
        raw = _read_stable(self.root, package_path)
        if len(raw) != key[2] or hashlib.sha256(raw).hexdigest() != key[1]:
            _fail("packaged bytes do not match their original Ref")
        return raw

    def package_path_for_ref(self, ref: Any) -> str | None:
        return self._mapped.get(_ref_key(ref))


def _read_ref(reader: _PackageReader, ref: Any) -> bytes:
    return reader.read_ref(ref)


def _validate_package(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]], _PackageReader]:
    raw = _read_stable(root, "manifest.json")
    manifest = _json(raw)
    if not isinstance(manifest, dict) or set(manifest) != _PACKAGE_FIELDS or manifest.get("schema_version") != 1:
        _fail("package manifest fields are invalid")
    if not isinstance(manifest.get("case_id"), str) or not _ID_RE.fullmatch(manifest["case_id"]):
        _fail("package case identity is invalid")
    bundles = manifest.get("runtime_bundles")
    if not isinstance(bundles, list):
        _fail("runtime bundle list is invalid")
    bundle_by_digest: dict[str, dict[str, Any]] = {}
    for bundle in bundles:
        if not isinstance(bundle, dict) or set(bundle) != _RUNTIME_PACKAGE_FIELDS:
            _fail("runtime bundle summary fields are invalid")
        digest = bundle.get("digest")
        if not isinstance(digest, str) or not _HASH_RE.fullmatch(digest) or digest in bundle_by_digest:
            _fail("runtime bundle digest is invalid or repeated")
        if bundle.get("worker_entry") != "python/optagent_core/worker.py" or bundle.get("helper_paths") not in (list(_LEGACY_RUNTIME_HELPERS), list(_RUNTIME_HELPERS)):
            _fail("runtime bundle helper allowlist is invalid")
        if type(bundle.get("standalone_supported")) is not bool:
            _fail("runtime bundle support marker is invalid")
        bundle_manifest = _ref(bundle.get("manifest"))
        if bundle_manifest["path"] != f".reaslab/optagent/runtime/{digest}/manifest.json" or bundle_manifest["media_type"] != "application/json":
            _fail("runtime bundle manifest Ref is invalid")
        bundle_by_digest[digest] = bundle

    rows = manifest.get("ref_map")
    if not isinstance(rows, list):
        _fail("portable Ref map must be an array")
    ref_map: list[dict[str, Any]] = []
    seen: dict[tuple[str, str, int, str], str] = {}
    for item in rows:
        if not isinstance(item, dict) or set(item) != {"ref", "package_path"}:
            _fail("portable Ref map item fields are invalid")
        ref = _ref(item["ref"])
        package_path = _safe_package_relative(item["package_path"])
        key = _ref_key(ref)
        prior = seen.get(key)
        if prior is not None:
            _fail("portable Ref map contains a repeated original Ref")
        seen[key] = package_path
        ref_map.append({"ref": ref, "package_path": package_path})
    reader = _PackageReader(root, ref_map)
    # Hash every listed file before considering any helper import. This also
    # rejects unused arbitrary files smuggled through the map.
    for item in ref_map:
        reader.read_ref(item["ref"])

    selected_run_ids, expected_locations, expected_run_records = _validate_delivery_metadata(manifest, reader)
    expected_locations.update(_validate_data_bundles(manifest, reader))
    for item in ref_map:
        _validate_map_location(
            manifest["case_id"], item["ref"], item["package_path"], bundle_by_digest,
            selected_run_ids=selected_run_ids, expected_locations=expected_locations,
        )

    reproductions = manifest.get("reproductions")
    if not isinstance(reproductions, list):
        _fail("reproduction entry list is invalid")
    if reproductions and manifest.get("entry") != "reproduce/run.py":
        _fail("standalone package entry is not fixed")
    if not reproductions and manifest.get("entry") == "reproduce/run.py":
        _fail("package declares a standalone entry without native reproduction inputs")
    seen_run_ids: set[str] = set()
    for entry in reproductions:
        _validate_prepared_entry(root, manifest, entry, reader, bundle_by_digest, expected_run_records)
        for run_id in entry["evidence_run_ids"]:
            if run_id in seen_run_ids:
                _fail("one evidence run is repeated across reproduction entries")
            seen_run_ids.add(run_id)
            _validate_run_output_closure(root, manifest, run_id, reader)
    return manifest, reproductions, reader


def _portable_suffix(path: str) -> str:
    suffix = PurePosixPath(path).suffix
    return suffix if suffix and len(suffix) < 16 else ".bin"


def _unique_contract_refs(values: list[dict[str, Any]], *, label: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], tuple[str, str, int, str]] = {}
    for value in values:
        ref = _ref(value)
        identity = _ref_key(ref)
        path_hash = (ref["path"], ref["sha256"])
        previous = seen.get(path_hash)
        if previous is not None:
            if previous != identity:
                _fail(f"{label} contains conflicting identities for one path and hash")
            continue
        seen[path_hash] = identity
        result.append(ref)
    return result


def _validate_delivery_metadata(
    package: dict[str, Any], reader: _PackageReader
) -> tuple[
    set[str],
    dict[tuple[str, str, int, str], str],
    dict[str, tuple[dict[str, Any], dict[str, Any]]],
]:
    """Bind the portable projection to the exact selected Delivery and Review children."""
    case_id = package["case_id"]
    delivery_id = package["delivery_id"]
    if not isinstance(delivery_id, str) or not _ID_RE.fullmatch(delivery_id):
        _fail("package delivery identity is invalid")

    expected_locations: dict[tuple[str, str, int, str], str] = {}

    def authorize(ref_value: Any, package_path: str) -> dict[str, Any]:
        ref = _ref(ref_value)
        key = _ref_key(ref)
        expected_locations.setdefault(key, package_path)
        return ref

    selection_ref = _ref(package.get("selection_ref"))
    expected_selection_path = f".reaslab/optagent/cases/{case_id}/deliveries/{delivery_id}/delivery.json"
    if selection_ref["path"] != expected_selection_path or selection_ref["media_type"] != "application/json":
        _fail("selected Delivery Ref is outside its fixed path")
    authorize(selection_ref, "evidence/selection.json")
    selection_raw = reader.read_ref(selection_ref)
    selection = _json(selection_raw, max_bytes=len(selection_raw))
    if (
        not isinstance(selection, dict)
        or set(selection) != _DELIVERY_FIELDS
        or selection.get("schema_version") != 2
        or _canonical_json_bytes(selection) != selection_raw
        or selection.get("delivery_id") != delivery_id
        or selection.get("kind") not in {"run", "proof", "reuse", "mixed"}
    ):
        _fail("selected Delivery record fields or identity are invalid")

    authorities = selection.get("authority_records")
    if not isinstance(authorities, dict) or set(authorities) != {"reviews", "runs"}:
        _fail("selected Delivery authority records are invalid")
    if package.get("authority_records") != authorities:
        _fail("package authorities differ from the selected Delivery")

    def ids(value: Any, label: str) -> list[str]:
        if not isinstance(value, list):
            _fail(f"selected Delivery {label} must be an array")
        result: list[str] = []
        seen_ids: set[str] = set()
        for item in value:
            if not isinstance(item, str) or not _ID_RE.fullmatch(item) or item in seen_ids:
                _fail(f"selected Delivery {label} contains an invalid or repeated ID")
            seen_ids.add(item)
            result.append(item)
        return result

    review_ids = ids(selection.get("review_ids"), "Review IDs")
    if len(review_ids) != 1:
        _fail("selected Delivery must name exactly one Review")
    run_ids = ids(selection.get("run_ids"), "Run IDs")
    if not isinstance(authorities["reviews"], dict) or set(authorities["reviews"]) != set(review_ids):
        _fail("selected Delivery Review authorities do not match its IDs")
    if not isinstance(authorities["runs"], dict) or set(authorities["runs"]) != set(run_ids):
        _fail("selected Delivery Run authorities do not match its IDs")
    if not isinstance(selection.get("claims"), list) or not isinstance(selection.get("proof_ids"), list):
        _fail("selected Delivery evidence collections are invalid")
    selection_evidence = selection.get("evidence_refs")
    if not isinstance(selection_evidence, list):
        _fail("selected Delivery evidence Refs must be an array")
    selection_evidence = [_ref(item) for item in selection_evidence]
    if _unique_contract_refs(selection_evidence, label="selected Delivery evidence Refs") != selection_evidence:
        _fail("selected Delivery evidence Refs are repeated")

    consumed = package.get("consumed_refs")
    if not isinstance(consumed, list):
        _fail("consumed Refs must be an array")
    consumed_refs = [_ref(item) for item in consumed]
    if _unique_contract_refs(consumed_refs, label="consumed Refs") != consumed_refs:
        _fail("consumed Refs are repeated")

    # Selected Runs and their seals are registered before Reviews by the producer.
    # This keeps a Result Ref at its canonical selected-run output path if a Review
    # also cites the same bytes.
    expected_run_records: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for run_id in run_ids:
        run_ref = _ref(authorities["runs"][run_id])
        if run_ref["media_type"] != "application/json":
            _fail("selected Run authority Ref media type is invalid")
        legacy = f".reaslab/optagent/cases/{case_id}/runs/{run_id}/record.json"
        version_prefix = f".reaslab/optagent/cases/{case_id}/runs/{run_id}/versions/"
        is_version = (
            run_ref["path"].startswith(version_prefix)
            and run_ref["path"] == version_prefix + run_ref["sha256"] + ".json"
        )
        if run_ref["path"] != legacy and not is_version:
            _fail("selected Run authority Ref is outside its fixed path")
        run_package_path = f"evidence/runs/{run_id}/record.json"
        authorize(run_ref, run_package_path)
        run_raw = reader.read_ref(run_ref)
        run = _json(run_raw, max_bytes=len(run_raw))
        if not isinstance(run, dict) or _canonical_json_bytes(run) != run_raw or run.get("run_id") != run_id:
            _fail("selected Run authority bytes do not match its identity")
        if reader.package_path_for_ref(run_ref) != run_package_path:
            _fail("selected Run authority Ref is not mapped to its fixed evidence record")
        expected_run_records[run_id] = (run, run_ref)
        worker = run.get("worker")
        if not isinstance(worker, dict):
            _fail("selected Run worker record is invalid")
        seal_value = worker.get("seal")
        results = run.get("results")
        if not isinstance(results, list):
            _fail("selected Run Result list is invalid")
        if seal_value is None:
            if results:
                _fail("selected Run Results have no worker Seal")
            continue
        seal_ref = _ref(seal_value)
        output_root = f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/"
        if seal_ref["path"] != output_root + "seal.json" or seal_ref["media_type"] != "application/json":
            _fail("selected Run Seal Ref is outside its fixed output path")
        seal_package_path = f"evidence/runs/{run_id}/seal.json"
        authorize(seal_ref, seal_package_path)
        if reader.package_path_for_ref(seal_ref) != seal_package_path:
            _fail("selected Run Seal Ref is not mapped to its fixed evidence path")
        seal_raw = reader.read_ref(seal_ref)
        seal = _json(seal_raw, max_bytes=len(seal_raw))
        if not isinstance(seal, dict) or not isinstance(seal.get("output_refs"), list):
            _fail("selected Run Seal output list is invalid")
        output_refs: list[dict[str, Any]] = []
        for value in seal["output_refs"]:
            output_ref = _ref(value)
            path = output_ref["path"]
            if not path.startswith(output_root) or path == seal_ref["path"]:
                _fail("selected Run Seal output Ref is outside its fixed output root")
            suffix = path[len(output_root):]
            pure_suffix = PurePosixPath(suffix)
            if pure_suffix.is_absolute() or not pure_suffix.parts or any(
                part in {"", ".", ".."} or part.casefold() in {".env", "env", "secrets", "credentials", "runtime", "__pycache__"}
                or part.casefold().startswith(".env.") or part.casefold().endswith((".pem", ".key"))
                for part in pure_suffix.parts
            ):
                _fail("selected Run Seal output path is reserved")
            output_package_path = f"evidence/runs/{run_id}/outputs/{pure_suffix.as_posix()}"
            authorize(output_ref, output_package_path)
            if reader.package_path_for_ref(output_ref) != output_package_path:
                _fail("selected Run Seal output is not mapped to its fixed evidence path")
            reader.read_ref(output_ref)
            output_refs.append(output_ref)
        if len({item["path"] for item in output_refs}) != len(output_refs) or output_refs != sorted(output_refs, key=lambda item: item["path"]):
            _fail("selected Run Seal outputs are repeated or noncanonical")
        output_keys = {_ref_key(item) for item in output_refs}
        for result_value in results:
            if _ref_key(result_value) not in output_keys:
                _fail("selected Run Result is absent from its Seal closure")

    review_closure_refs: list[dict[str, Any]] = []
    review_projections: list[dict[str, Any]] = []
    active_review_refs: set[tuple[str, str, int, str]] = set()
    visited_review_refs: set[tuple[str, str, int, str]] = set()
    review_records: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    review_projections_by_identity: dict[tuple[str, str, int, str], dict[str, Any]] = {}

    def add_review_ref(value: Any, package_path: str) -> dict[str, Any]:
        ref = _ref(value)
        authorize(ref, package_path)
        review_closure_refs.append(ref)
        return ref

    def add_review_attachment(value: Any) -> dict[str, Any]:
        ref = _ref(value)
        package_path = f"evidence/attachments/{ref['sha256']}{_portable_suffix(ref['path'])}"
        return add_review_ref(ref, package_path)

    def provenance_children(value: Any) -> list[dict[str, Any]]:
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "case_id", "source_binding", "provenance_source_ids", "sources", "route"}
            or value.get("schema_version") != 1
            or value.get("case_id") != case_id
            or not isinstance(value.get("provenance_source_ids"), list)
            or not isinstance(value.get("sources"), list)
            or not isinstance(value.get("route"), dict)
        ):
            _fail("Review source provenance fields are invalid")
        binding = value["source_binding"]
        if (
            not isinstance(binding, dict) or set(binding) != {"source_revision", "math_source_ids"}
            or type(binding.get("source_revision")) is not int or binding["source_revision"] < 0
            or not isinstance(binding.get("math_source_ids"), list)
            or any(not isinstance(item, str) or not _ID_RE.fullmatch(item) for item in binding["math_source_ids"])
        ):
            _fail("Review source provenance binding is invalid")
        refs: list[dict[str, Any]] = []
        seen_refs: set[tuple[str, str, int, str]] = set()
        source_ids: list[str] = []

        def append_ref(value: Any) -> None:
            ref = _ref(value)
            key = _ref_key(ref)
            if key not in seen_refs:
                seen_refs.add(key)
                refs.append(ref)

        def source_raw(source: Any, *, active: bool = False) -> None:
            if not isinstance(source, dict) or not isinstance(source.get("source_id"), str) or not _ID_RE.fullmatch(source["source_id"]):
                _fail("Review provenance Source entry is invalid")
            raw = source.get("raw")
            if not isinstance(raw, dict) or set(raw) != {"sha256", "byte_len", "ref"}:
                _fail("Review provenance Source Ref is invalid")
            ref = _ref(raw["ref"])
            if (
                raw["sha256"] != ref["sha256"] or raw["byte_len"] != ref["byte_len"]
                or ref["path"] != f".reaslab/optagent/source-bytes/{ref['sha256']}"
                or ref["media_type"] != "application/octet-stream"
            ):
                _fail("Review provenance Source Ref identity is invalid")
            if active:
                source_ids.append(source["source_id"])
            append_ref(ref)

        for source in value["sources"]:
            source_raw(source, active=True)
        if source_ids != value["provenance_source_ids"] or len(set(source_ids)) != len(source_ids):
            _fail("Review provenance Source IDs do not match their explicit children")
        if any(not isinstance(item, str) or not _ID_RE.fullmatch(item) for item in value["provenance_source_ids"]):
            _fail("Review provenance Source IDs are invalid")
        route = value["route"]
        if (
            set(route) != {"session_id", "unknown", "reason", "member", "guards", "handoffs"}
            or type(route.get("unknown")) is not bool
            or not isinstance(route.get("guards"), list)
            or not isinstance(route.get("handoffs"), list)
        ):
            _fail("Review provenance route projection is invalid")
        for guard in route["guards"]:
            if not isinstance(guard, dict) or not {"gap_ref", "scope", "unresolved"}.issubset(guard):
                _fail("Review provenance route guard is invalid")
            append_ref(guard["gap_ref"])
            adoption = guard.get("adoption")
            if adoption is not None:
                if not isinstance(adoption, dict) or not isinstance(adoption.get("sources"), list):
                    _fail("Review provenance adoption is invalid")
                for source in adoption["sources"]:
                    source_raw(source)
        for handoff in route["handoffs"]:
            if not isinstance(handoff, dict) or not isinstance(handoff.get("guard_refs"), list):
                _fail("Review provenance handoff is invalid")
            for ref in handoff["guard_refs"]:
                append_ref(ref)
        return refs

    def review_snapshot(ref_value: Any, review_id: str, *, root: bool = False) -> dict[str, Any]:
        ref = _ref(ref_value)
        identity = _ref_key(ref)
        if identity in active_review_refs:
            _fail("pinned Review lineage contains a repeated snapshot")
        if identity in visited_review_refs:
            return review_projections_by_identity.get(identity, {})
        version_prefix = f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/versions/"
        legacy_path = f".reaslab/optagent/cases/{case_id}/reviews/{review_id}.json"
        versioned = ref["path"].startswith(version_prefix) and ref["path"] == version_prefix + ref["sha256"] + ".json"
        if (not versioned and not (root and ref["path"] == legacy_path)) or ref["media_type"] != "application/json":
            _fail("pinned Review Ref is outside its immutable family path")
        raw = reader.read_ref(ref)
        review = _json(raw, max_bytes=len(raw))
        if (
            not _supported_review_fields(review)
            or review.get("schema_version") != 4 or _canonical_json_bytes(review) != raw
            or review.get("case_id") != case_id or review.get("review_id") != review_id
            or type(review.get("review_revision")) is not int or review["review_revision"] < 1
            or review.get("status") not in {"open", "closed"}
            or not isinstance(review.get("issues"), list)
        ):
            _fail("pinned Review record fields or identity are invalid")
        outcomes, answer_ref = _validate_review_state(review)
        review_records[identity] = review
        active_review_refs.add(identity)
        visited_review_refs.add(identity)
        root_package_path = f"evidence/reviews/{review_id}.json"
        add_review_ref(ref, root_package_path)

        provenance_ref = _ref(review.get("source_provenance_ref"))
        provenance_path = f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/source-provenance.json"
        if provenance_ref["path"] != provenance_path or provenance_ref["media_type"] != "application/json":
            _fail("Review provenance Ref is outside its fixed path")
        provenance_package_path = f"evidence/reviews/{review_id}/source-provenance.json"
        add_review_ref(provenance_ref, provenance_package_path)
        provenance_raw = reader.read_ref(provenance_ref)
        if review.get("source_provenance_digest") != hashlib.sha256(provenance_raw).hexdigest():
            _fail("Review provenance digest does not match its Ref")
        provenance = _json(provenance_raw, max_bytes=len(provenance_raw))
        for child_ref in provenance_children(provenance):
            add_review_attachment(child_ref)

        prior_ref_value = review.get("prior_review_ref")
        prior_review_id: str | None = None
        prior_review: dict[str, Any] | None = None
        if prior_ref_value is not None:
            prior_ref = _ref(prior_ref_value)
            prefix = f".reaslab/optagent/cases/{case_id}/reviews/"
            tail = prior_ref["path"][len(prefix):] if prior_ref["path"].startswith(prefix) else ""
            parts = tail.split("/")
            if len(parts) != 3 or parts[1] != "versions" or parts[2] != f"{prior_ref['sha256']}.json":
                _fail("pinned prior Review Ref has an invalid immutable path")
            prior_review_id = parts[0]
            if not _ID_RE.fullmatch(prior_review_id):
                _fail("pinned prior Review ID is invalid")
            review_snapshot(prior_ref, prior_review_id)
            prior_review = review_records.get(_ref_key(prior_ref))
            if prior_review is None or prior_review.get("note") != review.get("note"):
                _fail("Review note does not match its pinned prior snapshot")

        note = review.get("note")
        if note is not None:
            if not isinstance(note, dict) or not isinstance(note.get("body"), dict):
                _fail("Review note Ref is invalid")
            note_ref = _ref(note["body"])
            allowed_note_paths = {f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/note.txt"}
            note_owner_id = review_id
            if prior_review is not None and isinstance(prior_review.get("note"), dict):
                prior_note = prior_review["note"].get("body")
                if isinstance(prior_note, dict):
                    prior_note_ref = _ref(prior_note)
                    allowed_note_paths.add(prior_note_ref["path"])
                    if note_ref == prior_note_ref:
                        note_owner_id = prior_review_id or review_id
            if note_ref["path"] not in allowed_note_paths or note_ref["media_type"] != "text/plain; charset=utf-8":
                _fail("Review note Ref is outside its fixed path")
            add_review_ref(note_ref, f"evidence/reviews/{note_owner_id}/note.txt")

        conclusion = review.get("conclusion")
        projected_claims: list[dict[str, Any]] = []
        remaining: list[str] = []
        if conclusion is not None:
            if not isinstance(conclusion, dict) or not isinstance(conclusion.get("claims", []), list):
                _fail("Review conclusion is invalid")
            if not isinstance(conclusion.get("remaining", []), list) or any(not isinstance(item, str) for item in conclusion.get("remaining", [])):
                _fail("Review conclusion remaining items are invalid")
            for claim in conclusion.get("claims", []):
                if not isinstance(claim, dict) or not isinstance(claim.get("conditions", []), list) or not isinstance(claim.get("evidence_refs", []), list):
                    _fail("Review conclusion claim is invalid")
                evidence_refs = [_ref(item) for item in claim.get("evidence_refs", [])]
                for child_ref in evidence_refs:
                    add_review_attachment(child_ref)
                projected_claims.append({
                    "kind": claim.get("kind"), "scope": claim.get("scope"), "support": claim.get("support"),
                    "conditions": list(claim.get("conditions", [])), "evidence_refs": evidence_refs,
                })
            remaining = list(conclusion.get("remaining", []))

        if answer_ref is not None:
            answer_package_path = (
                f"evidence/approved-answers/{answer_ref['sha256']}{_portable_suffix(answer_ref['path'])}"
            )
            add_review_ref(answer_ref, answer_package_path)
            reader.read_ref(answer_ref)

        issue_projections: list[dict[str, Any]] = []
        for raw_issue_ref in review["issues"]:
            issue_ref = _ref(raw_issue_ref)
            issue_prefix = f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/issues/"
            issue_tail = issue_ref["path"][len(issue_prefix):] if issue_ref["path"].startswith(issue_prefix) else ""
            issue_parts = issue_tail.split("/")
            if len(issue_parts) != 3 or issue_parts[1] != "versions" or issue_parts[2] != f"{issue_ref['sha256']}.json" or issue_ref["media_type"] != "application/json":
                _fail("Review Issue Ref is outside its immutable path")
            issue_id = issue_parts[0]
            if not _ID_RE.fullmatch(issue_id):
                _fail("Review Issue identity is invalid")
            issue_package_path = f"evidence/reviews/{review_id}/issues/{issue_id}.json"
            add_review_ref(issue_ref, issue_package_path)
            issue_raw = reader.read_ref(issue_ref)
            issue = _json(issue_raw, max_bytes=len(issue_raw))
            if not isinstance(issue, dict) or set(issue) != _ISSUE_FIELDS or _canonical_json_bytes(issue) != issue_raw or issue.get("issue_id") != issue_id:
                _fail("Review Issue snapshot fields or identity are invalid")
            if not isinstance(issue.get("evidence_refs"), list) or not isinstance(issue.get("owner_dispositions"), list):
                _fail("Review Issue references are invalid")
            for child_value in issue["evidence_refs"]:
                add_review_attachment(child_value)
            for disposition in issue["owner_dispositions"]:
                if not isinstance(disposition, dict):
                    _fail("Review Issue owner disposition is invalid")
            issue_projections.append({"issue_ref": issue_ref, **issue})

        projection = {
            "review_id": review_id,
            "review_ref": ref,
            "answer_ref": answer_ref,
            "outcome": outcomes[-1] if outcomes else None,
            "prior_review_ref": _ref(prior_ref_value) if prior_ref_value is not None else None,
            "status": review.get("status"),
            "conclusion_claims": projected_claims,
            "remaining": remaining,
            "issues": issue_projections,
        }
        active_review_refs.remove(identity)
        review_projections_by_identity[identity] = projection
        return projection

    for review_id in review_ids:
        projection = review_snapshot(authorities["reviews"][review_id], review_id, root=True)
        if projection.get("status") != "closed":
            _fail("selected Review authority is not closed")
        review_projections.append(projection)
    if package.get("reviews") != review_projections:
        _fail("Review projections do not match their selected authority Refs")

    selected_review = review_projections[0]
    if not selection.get("claims") or selection.get("claims") != selected_review["conclusion_claims"]:
        _fail("selected Delivery claims do not match the complete selected Review conclusion")
    approved_answer = selected_review["answer_ref"]
    reports = selection.get("reports")
    if (
        approved_answer is None
        or not isinstance(reports, list)
        or len(reports) != 1
        or _ref(reports[0]) != approved_answer
        or not any(_ref_key(item) == _ref_key(approved_answer) for item in selection_evidence)
    ):
        _fail("selected Delivery reports and evidence do not close over its approved Review answer")

    expected_consumed = _unique_contract_refs(
        [selection_ref, *selection_evidence, *authorities["reviews"].values(),
         *authorities["runs"].values(), *review_closure_refs],
        label="selected Delivery consumed Refs",
    )
    if [_ref_key(item) for item in consumed_refs] != [_ref_key(item) for item in expected_consumed]:
        _fail("consumed Refs do not match the selected Delivery and explicit Review closure")
    return set(run_ids), expected_locations, expected_run_records


def _validate_map_location(
    case_id: str,
    ref: dict[str, Any],
    package_path: str,
    bundles: dict[str, dict[str, Any]],
    *,
    selected_run_ids: set[str],
    expected_locations: dict[tuple[str, str, int, str], str],
) -> None:
    path = ref["path"]
    data_match = re.fullmatch(
        rf"\.reaslab/optagent/cases/{re.escape(case_id)}/data/({_ID_RE.pattern[1:-1]})/(manifest\.json|trace\.json|objects/.+)",
        path,
    )
    if data_match is not None:
        data_id, suffix = data_match.groups()
        if suffix == "manifest.json":
            expected = f"data/{data_id}/manifest.json"
        elif suffix == "trace.json":
            expected = f"data/{data_id}/trace.json"
        else:
            if ref["media_type"] != "application/json":
                _fail("normalized data values media type is invalid")
            if not re.fullmatch(r"objects/.+\.json", suffix):
                _fail("normalized data values path is invalid")
            if re.fullmatch(rf"data/{re.escape(data_id)}/objects/[0-9a-f]{{64}}\.json", package_path) is None:
                _fail("normalized data values package path is invalid")
            return
        if suffix in {"manifest.json", "trace.json"} and ref["media_type"] != "application/json":
            _fail("normalized data JSON media type is invalid")
        if package_path != expected:
            _fail("candidate DataBundle Ref is mapped outside its fixed package path")
        return
    run_match = re.fullmatch(
        rf"\.reaslab/optagent/cases/{re.escape(case_id)}/runs/({_ID_RE.pattern[1:-1]})/output/(.+)",
        path,
    )
    if run_match is not None:
        run_id, suffix = run_match.groups()
        pure_suffix = PurePosixPath(suffix)
        if pure_suffix.is_absolute() or not pure_suffix.parts or any(
            part in {"", ".", ".."} or part.casefold() in {".env", "env", "secrets", "credentials", "runtime", "__pycache__"}
            or part.casefold().startswith(".env.") or part.casefold().endswith((".pem", ".key"))
            for part in pure_suffix.parts
        ):
            _fail("run output Ref path is reserved")
        authorized = expected_locations.get(_ref_key(ref))
        if authorized is None or package_path != authorized:
            if run_id in selected_run_ids:
                _fail("selected run output Ref is not mapped to its authorized evidence path")
            _fail("unselected run output Ref is not an explicit Review child at its fixed attachment path")
        return
    prefix = f".reaslab/optagent/cases/{case_id}/prepared/"
    if path.startswith(prefix):
        tail = path[len(prefix):]
        prepared_id, slash, suffix = tail.partition("/")
        if not slash or not _ID_RE.fullmatch(prepared_id) or not suffix.startswith("input/"):
            _fail("frozen input Ref is outside a prepared input tree")
        logical = suffix[len("input/"):]
        package_prefix = f"reproduce/prepared/{prepared_id}/"
        if logical == "manifest.json":
            expected = package_prefix + "input/manifest.json"
        elif logical == "requirements.txt":
            expected = package_prefix + "requirements.txt"
        elif logical.startswith("sources/"):
            parts = PurePosixPath(logical).parts
            if len(parts) != 3 or not _ID_RE.fullmatch(parts[1]) or parts[2] not in {"record.json", "bytes.bin"}:
                _fail("frozen source Ref path is invalid")
            expected = package_prefix + logical
        elif logical.startswith("source/"):
            original = PurePosixPath(logical[len("source/"):])
            if (
                original.is_absolute()
                or not original.parts
                or original.parts[0].casefold() == ".reaslab"
                or any(
                    part in {"", ".", ".."}
                    or part.casefold() in {".env", "env", "secrets", "credentials", "runtime", "__pycache__"}
                    or part.casefold().startswith(".env.")
                    or part.casefold().endswith((".pem", ".key"))
                    for part in original.parts
                )
            ):
                _fail("frozen source code path is reserved")
            if original.suffix != ".py":
                _fail("frozen candidate code must retain its Python source suffix")
            expected = package_prefix + "source/" + original.as_posix()
        elif logical == "data/manifest.json" or logical == "data/trace.json":
            expected = package_prefix + logical
        elif logical.startswith("data/objects/"):
            object_path = logical[len("data/objects/"):]
            if not object_path.endswith(".json") or not object_path[:-5]:
                _fail("frozen data object path is invalid")
            if ref["media_type"] != "application/json":
                _fail("frozen data object media type is invalid")
            expected_package_path = re.escape(package_prefix + "data/objects/") + r"[0-9a-f]{64}\.json"
            if re.fullmatch(expected_package_path, package_path) is None:
                _fail("frozen data object package path is invalid")
            return
        else:
            _fail("frozen input Ref path is outside the fixed closure")
        if package_path != expected:
            _fail("frozen input Ref is mapped outside its fixed package path")
        return
    runtime_match = re.fullmatch(r"\.reaslab/optagent/runtime/([0-9a-f]{64})/(manifest\.json|python/optagent_core/(?:__init__|contracts|data|models|results|solver_protocol|worker)\.py)", path)
    if runtime_match is None:
        authorized = expected_locations.get(_ref_key(ref))
        if authorized is None or package_path != authorized:
            _fail("portable Ref is outside the selected Delivery and explicit Review closures")
        return
    digest, suffix = runtime_match.groups()
    if digest not in bundles:
        _fail("portable Ref names an unlisted runtime digest")
    expected = f"reproduce/runtime/{digest}/{suffix}"
    if package_path != expected:
        _fail("runtime helper Ref is mapped outside its fixed import tree")
    if suffix == "manifest.json" and ref["media_type"] != "application/json":
        _fail("runtime manifest media type is invalid")
    if suffix != "manifest.json" and ref["media_type"] != "text/x-python; charset=utf-8":
        _fail("runtime helper media type is invalid")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _validate_run_output_closure(root: Path, package: dict[str, Any], run_id: str, reader: _PackageReader) -> None:
    run_path = f"evidence/runs/{run_id}/record.json"
    run = _json(_read_stable(root, run_path))
    if not isinstance(run, dict) or run.get("run_id") != run_id or not isinstance(run.get("worker"), dict):
        _fail("packaged run record identity is invalid")
    seal_ref = _ref(run["worker"].get("seal"))
    expected_seal_path = f".reaslab/optagent/cases/{package['case_id']}/runs/{run_id}/output/seal.json"
    if seal_ref["path"] != expected_seal_path or reader.package_path_for_ref(seal_ref) != f"evidence/runs/{run_id}/seal.json":
        _fail("packaged run seal Ref is missing or misbound")
    seal = _json(reader.read_ref(seal_ref))
    if not isinstance(seal, dict) or not isinstance(seal.get("output_refs"), list):
        _fail("packaged run seal output list is invalid")
    output_refs: list[dict[str, Any]] = []
    output_root = f".reaslab/optagent/cases/{package['case_id']}/runs/{run_id}/output/"
    for value in seal["output_refs"]:
        ref = _ref(value)
        if not ref["path"].startswith(output_root) or ref["path"] == seal_ref["path"]:
            _fail("packaged seal output Ref is outside its run")
        suffix = ref["path"][len(output_root):]
        expected_package_path = f"evidence/runs/{run_id}/outputs/{suffix}"
        if reader.package_path_for_ref(ref) != expected_package_path:
            _fail("packaged Seal.output_refs closure is incomplete")
        reader.read_ref(ref)
        output_refs.append(ref)
    if len({ref["path"] for ref in output_refs}) != len(output_refs) or output_refs != sorted(output_refs, key=lambda ref: ref["path"]):
        _fail("packaged Seal.output_refs are repeated or noncanonical")
    results = run.get("results")
    if not isinstance(results, list):
        _fail("packaged run Result list is invalid")
    output_keys = {_ref_key(ref) for ref in output_refs}
    for result_ref in results:
        checked = _ref(result_ref)
        if _ref_key(checked) not in output_keys:
            _fail("packaged run Result Ref is absent from Seal.output_refs")
        reader.read_ref(checked)


def _validate_data_bundles(
    package: dict[str, Any], reader: _PackageReader
) -> dict[tuple[str, str, int, str], str]:
    bundles = package.get("data_bundles")
    if not isinstance(bundles, list):
        _fail("candidate DataBundle summary list is invalid")
    expected_keys: set[tuple[str, str, int, str]] = set()
    source_locations: dict[tuple[str, str, int, str], str] = {}
    seen_data_ids: set[str] = set()
    case_id = package["case_id"]
    refs_by_original_path: dict[str, list[dict[str, Any]]] = {}
    for row in package["ref_map"]:
        ref = _ref(row["ref"])
        refs_by_original_path.setdefault(ref["path"], []).append(ref)

    def authorize_source_ref(ref: dict[str, Any], package_path: str) -> None:
        key = _ref_key(ref)
        canonical_path = source_locations.setdefault(key, package_path)
        if reader.package_path_for_ref(ref) != canonical_path:
            _fail("candidate DataBundle Source Ref is not mapped to its first fixed package path")

    for item in bundles:
        if not isinstance(item, dict) or set(item) != {"data_id", "digest", "manifest", "loader_version"}:
            _fail("candidate DataBundle summary fields are invalid")
        data_id = item["data_id"]
        if not isinstance(data_id, str) or not _ID_RE.fullmatch(data_id) or data_id in seen_data_ids:
            _fail("candidate DataBundle identity is invalid or repeated")
        seen_data_ids.add(data_id)
        if not isinstance(item["digest"], str) or not _HASH_RE.fullmatch(item["digest"]):
            _fail("candidate DataBundle digest is invalid")
        manifest_ref = _ref(item["manifest"])
        expected_manifest_path = f".reaslab/optagent/cases/{case_id}/data/{data_id}/manifest.json"
        if manifest_ref["path"] != expected_manifest_path or manifest_ref["media_type"] != "application/json":
            _fail("candidate DataBundle manifest Ref is invalid")
        manifest_raw = reader.read_ref(manifest_ref)
        data_manifest = _json(manifest_raw, max_bytes=len(manifest_raw))
        fields = {"data_id", "source_ids", "import_plan", "objects", "trace", "loader_version", "digest"}
        if not isinstance(data_manifest, dict) or set(data_manifest) != fields:
            _fail("candidate DataManifest fields are invalid")
        if data_manifest["data_id"] != data_id or data_manifest["digest"] != item["digest"] or data_manifest["loader_version"] != item["loader_version"]:
            _fail("candidate DataManifest identity differs from its package summary")
        digest_material = {key: value for key, value in data_manifest.items() if key != "digest"}
        if hashlib.sha256(_canonical_json_bytes(digest_material)).hexdigest() != item["digest"]:
            _fail("candidate DataManifest digest is invalid")
        expected_keys.add(_ref_key(manifest_ref))
        specs = data_manifest["objects"]
        if not isinstance(specs, list):
            _fail("candidate DataManifest objects are invalid")
        object_ids: set[str] = set()
        for spec in specs:
            if not isinstance(spec, dict) or not isinstance(spec.get("object_id"), str) or not spec["object_id"] or spec["object_id"] in object_ids:
                _fail("candidate DataManifest object identity is invalid")
            object_ids.add(spec["object_id"])
            values_ref = _ref(spec.get("values"))
            if values_ref["path"] != f".reaslab/optagent/cases/{case_id}/data/{data_id}/objects/{spec['object_id']}.json" or values_ref["media_type"] != "application/json":
                _fail("candidate normalized values Ref is invalid")
            expected_package_path = f"data/{data_id}/objects/{hashlib.sha256(spec['object_id'].encode('utf-8')).hexdigest()}.json"
            if reader.package_path_for_ref(values_ref) != expected_package_path:
                _fail("candidate normalized values Ref is not mapped to its fixed object path")
            reader.read_ref(values_ref)
            expected_keys.add(_ref_key(values_ref))
        trace_ref = _ref(data_manifest.get("trace"))
        if trace_ref["path"] != f".reaslab/optagent/cases/{case_id}/data/{data_id}/trace.json" or trace_ref["media_type"] != "application/json":
            _fail("candidate DataBundle trace Ref is invalid")
        trace_raw = reader.read_ref(trace_ref)
        trace = _json(trace_raw, max_bytes=len(trace_raw))
        if (
            not isinstance(trace, dict)
            or set(trace) != {"schema_version", "plan", "source_refs", "steps"}
            or trace.get("schema_version") != 1
            or not isinstance(trace.get("source_refs"), list)
            or not isinstance(trace.get("steps"), list)
        ):
            _fail("candidate DataBundle trace fields are invalid")
        source_ids = data_manifest.get("source_ids")
        plan = data_manifest.get("import_plan")
        blocks = plan.get("blocks") if isinstance(plan, dict) else None
        if (
            not isinstance(source_ids, list)
            or not source_ids
            or any(not isinstance(source_id, str) or not _ID_RE.fullmatch(source_id) for source_id in source_ids)
            or len(set(source_ids)) != len(source_ids)
            or not isinstance(blocks, list)
            or len(trace["source_refs"]) != len(source_ids)
            or trace.get("plan") != plan
        ):
            _fail("candidate DataManifest, import plan, and trace Source order differ")
        planned_source_ids: list[str] = []
        for block in blocks:
            if not isinstance(block, dict) or not isinstance(block.get("source_id"), str):
                _fail("candidate DataManifest import plan Source identity is invalid")
            if block["source_id"] not in planned_source_ids:
                planned_source_ids.append(block["source_id"])
        if planned_source_ids != source_ids:
            _fail("candidate DataManifest, import plan, and trace Source order differ")
        for source_id, source_ref_value in zip(source_ids, trace["source_refs"], strict=True):
            source_ref = _ref(source_ref_value)
            if (
                source_ref["path"] != f".reaslab/optagent/source-bytes/{source_ref['sha256']}"
                or source_ref["media_type"] != "application/octet-stream"
            ):
                _fail("candidate Data Source bytes Ref is outside the shared immutable pool")

            source_record_path = f".reaslab/optagent/cases/{case_id}/sources/{source_id}.json"
            record_rows = refs_by_original_path.get(source_record_path, [])
            if len(record_rows) != 1:
                _fail("candidate Data Source record is missing from its exact Case path")
            record_ref = record_rows[0]
            if record_ref["media_type"] != "application/json":
                _fail("candidate Data Source record Ref is outside its exact Case path")
            record_package_path = f"data/{data_id}/sources/{source_id}/record.json"
            source_record_raw = reader.read_ref(record_ref)
            source_record = _json(source_record_raw, max_bytes=len(source_record_raw))
            if (
                not isinstance(source_record, dict)
                or source_record.get("source_id") != source_id
                or source_record.get("purpose") != "data"
                or _ref(source_record.get("bytes")) != source_ref
                or source_record.get("original_sha256") != source_ref["sha256"]
                or type(source_record.get("original_size")) is not int
                or source_record.get("original_size") != source_ref["byte_len"]
            ):
                _fail("candidate Data Source record does not bind its exact bytes Ref")
            authorize_source_ref(record_ref, record_package_path)

            bytes_package_path = f"data/{data_id}/sources/{source_id}/bytes.bin"
            authorize_source_ref(source_ref, bytes_package_path)
            expected_keys.add(_ref_key(record_ref))
            expected_keys.add(_ref_key(source_ref))
        expected_keys.add(_ref_key(trace_ref))
    for item in package["ref_map"]:
        ref = _ref(item["ref"])
        if ref["path"].startswith(f".reaslab/optagent/cases/{case_id}/data/") and _ref_key(ref) not in expected_keys:
            _fail("package contains an unreferenced candidate DataBundle Ref")
    return source_locations


def _validate_prepared_entry(
    root: Path,
    package: dict[str, Any],
    entry: Any,
    reader: _PackageReader,
    bundles: dict[str, dict[str, Any]],
    expected_run_records: dict[str, tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    if not isinstance(entry, dict) or set(entry) != {"prepared_manifest_ref", "evidence_run_ids"}:
        _fail("reproduction entry fields are invalid")
    manifest_ref = _ref(entry["prepared_manifest_ref"])
    run_ids = entry["evidence_run_ids"]
    if not isinstance(run_ids, list) or not run_ids or any(not isinstance(run, str) or not _ID_RE.fullmatch(run) for run in run_ids) or len(set(run_ids)) != len(run_ids):
        _fail("reproduction source run identifiers are invalid")
    if any(run_id not in expected_run_records for run_id in run_ids):
        _fail("reproduction evidence Run is not a selected Run")
    if manifest_ref["path"] != f".reaslab/optagent/cases/{package['case_id']}/prepared/{_prepared_id_from_ref(manifest_ref)}/input/manifest.json":
        _fail("reproduction manifest Ref has an unexpected identity path")
    raw = _read_ref(reader, manifest_ref)
    prepared = _json(raw)
    if not isinstance(prepared, dict) or set(prepared) != _PREPARED_FIELDS or type(prepared.get("schema_version")) is not int or prepared.get("schema_version") not in {2, 3}:
        _fail("PreparedInputManifest fields are invalid")
    if prepared.get("case_id") != package["case_id"] or manifest_ref["path"] != f".reaslab/optagent/cases/{prepared['case_id']}/prepared/{prepared['prepared_id']}/input/manifest.json":
        _fail("PreparedInputManifest identity does not match its Ref")
    if hashlib.sha256(raw).hexdigest() != manifest_ref["sha256"]:
        _fail("PreparedInputManifest digest does not match its Ref")
    for run_id in run_ids:
        run_record, _run_ref = expected_run_records[run_id]
        run_prepared_id = run_record.get("prepared_id")
        expected_run_manifest_path = (
            f".reaslab/optagent/cases/{package['case_id']}/prepared/{run_prepared_id}/input/manifest.json"
            if isinstance(run_prepared_id, str)
            else None
        )
        if (
            run_prepared_id != prepared["prepared_id"]
            or manifest_ref["path"] != expected_run_manifest_path
        ):
            _fail("PreparedInputManifest does not match its selected Run")
    sources = prepared.get("sources")
    if not isinstance(sources, list):
        _fail("PreparedInputManifest source closure is invalid")
    source_bytes: set[tuple[str, str, int, str]] = set()
    prepared_sources: dict[str, dict[str, Any]] = {}
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"source_id", "record", "bytes"} or not isinstance(source["source_id"], str) or not _ID_RE.fullmatch(source["source_id"]):
            _fail("prepared source fields are invalid")
        source_id = source["source_id"]
        if source_id in prepared_sources:
            _fail("PreparedInputManifest repeats a Source ID")
        record_ref = _ref(source["record"])
        bytes_ref = _ref(source["bytes"])
        prepared_prefix = f".reaslab/optagent/cases/{prepared['case_id']}/prepared/{prepared['prepared_id']}/input/sources/{source_id}/"
        if record_ref["path"] != prepared_prefix + "record.json" or bytes_ref["path"] != prepared_prefix + "bytes.bin":
            _fail("prepared Source Refs do not match their declared identities")
        record_raw = _read_ref(reader, record_ref)
        record = _json(record_raw)
        if not isinstance(record, dict) or record.get("source_id") != source["source_id"] or _ref(record.get("bytes")) != bytes_ref:
            _fail("prepared source record does not bind its frozen bytes")
        _read_ref(reader, bytes_ref)
        source_bytes.add(_ref_key(bytes_ref))
        prepared_sources[source_id] = {"record": record, "record_ref": record_ref, "bytes": bytes_ref}

    candidate = prepared.get("candidate")
    candidate_fields = {"authoring", "model", "local_modules", "business_checker", "backend_id", "objective_plan", "input_closure", "closure_note"}
    if (
        not isinstance(candidate, dict)
        or set(candidate) not in (candidate_fields, candidate_fields | {"requirements_file"})
    ):
        _fail("prepared candidate profile is unsupported")
    authoring = candidate.get("authoring")
    backend_id = candidate.get("backend_id")
    if prepared["schema_version"] == 2:
        if authoring != "pyomo" or backend_id != "highs":
            _fail("legacy prepared candidate profile is unsupported")
    else:
        if not isinstance(backend_id, str) or not backend_id:
            _fail("prepared candidate backend identity is invalid")
        if authoring == "direct_python":
            if backend_id != "direct_python" or "requirements_file" not in candidate:
                _fail("direct_python candidate profile does not match its frozen backend and requirements")
        elif authoring == "pyomo":
            if backend_id == "direct_python":
                _fail("Pyomo candidate cannot use the direct_python backend")
        else:
            _fail("prepared candidate profile is unsupported")
    code_specs = [candidate.get("model"), *candidate.get("local_modules", []), *([candidate.get("business_checker")] if candidate.get("business_checker") else [])]
    for spec in code_specs:
        if not isinstance(spec, dict) or set(spec) != {"original_path", "bytes"}:
            _fail("prepared code closure fields are invalid")
        _read_ref(reader, spec["bytes"])

    requirements_path = (
        f".reaslab/optagent/cases/{prepared['case_id']}/prepared/{prepared['prepared_id']}/input/requirements.txt"
    )
    requirements_refs = [
        _ref(item["ref"])
        for item in package["ref_map"]
        if _ref(item["ref"])["path"] == requirements_path
    ]
    requirements_spec = candidate.get("requirements_file")
    if requirements_spec is None:
        if requirements_refs:
            _fail("package contains an unreferenced Prepared requirements Ref")
    else:
        if (
            not isinstance(requirements_spec, dict)
            or set(requirements_spec) != {"path", "bytes"}
            or requirements_spec.get("path") != "requirements.txt"
        ):
            _fail("prepared requirements specification is invalid")
        requirements_ref = _ref(requirements_spec["bytes"])
        if (
            requirements_ref["path"] != requirements_path
            or len(requirements_refs) != 1
            or requirements_refs[0] != requirements_ref
        ):
            _fail("prepared requirements Ref does not match its fixed input path")
        expected_package_path = f"reproduce/prepared/{prepared['prepared_id']}/requirements.txt"
        if reader.package_path_for_ref(requirements_ref) != expected_package_path:
            _fail("prepared requirements Ref is mapped outside its fixed package path")
        reader.read_ref(requirements_ref)

    data = prepared.get("data")
    if data is None:
        if not isinstance(prepared.get("binding_digest"), str) or not _HASH_RE.fullmatch(prepared["binding_digest"]):
            _fail("no-data PreparedInputManifest candidate binding is invalid")
        solve_options = prepared.get("solve_options")
        if not isinstance(solve_options, dict) or not isinstance(solve_options.get("instance_keys", []), list) or solve_options.get("instance_keys", []):
            _fail("no-data PreparedInputManifest cannot select parameter rows")
        data_prefix = f".reaslab/optagent/cases/{package['case_id']}/prepared/{prepared['prepared_id']}/input/data/"
        if any(_ref(row["ref"])["path"].startswith(data_prefix) for row in package["ref_map"]):
            _fail("no-data PreparedInputManifest includes data references")
    else:
        if not isinstance(data, dict) or set(data) != {"data_id", "manifest", "instance_parameter_object_id"}:
            _fail("standalone native entry DataBundle fields are invalid")
        data_prefix = f".reaslab/optagent/cases/{package['case_id']}/prepared/{prepared['prepared_id']}/input/data/"
        data_manifest_ref = _ref(data["manifest"])
        if data_manifest_ref["path"] != data_prefix + "manifest.json" or data_manifest_ref["media_type"] != "application/json":
            _fail("frozen Prepared DataManifest Ref is invalid")
        expected_data_keys = {_ref_key(data_manifest_ref)}
        data_raw = _read_ref(reader, data_manifest_ref)
        data_manifest = _json(data_raw, max_bytes=len(data_raw))
        if not isinstance(data_manifest, dict) or set(data_manifest) != {"data_id", "source_ids", "import_plan", "objects", "trace", "loader_version", "digest"}:
            _fail("frozen DataManifest fields are invalid")
        if data_manifest.get("data_id") != data["data_id"]:
            _fail("frozen Prepared DataBundle identity does not match its DataManifest")
        specs = data_manifest.get("objects")
        if not isinstance(specs, list):
            _fail("frozen DataManifest objects are invalid")
        object_ids: set[str] = set()
        for spec in specs:
            if not isinstance(spec, dict) or not isinstance(spec.get("object_id"), str) or not spec["object_id"] or spec["object_id"] in object_ids:
                _fail("frozen data object identity is invalid")
            object_id = spec["object_id"]
            object_ids.add(object_id)
            values_ref = _ref(spec.get("values"))
            if values_ref["path"] != data_prefix + f"objects/{object_id}.json" or values_ref["media_type"] != "application/json":
                _fail("frozen data values Ref does not match its object identity")
            object_token = hashlib.sha256(object_id.encode("utf-8")).hexdigest()
            expected_package_path = f"reproduce/prepared/{prepared['prepared_id']}/data/objects/{object_token}.json"
            if reader.package_path_for_ref(values_ref) != expected_package_path:
                _fail("frozen data values Ref is not mapped to its fixed object path")
            reader.read_ref(values_ref)
            expected_data_keys.add(_ref_key(values_ref))
        trace_ref = _ref(data_manifest.get("trace"))
        if trace_ref["path"] != data_prefix + "trace.json" or trace_ref["media_type"] != "application/json":
            _fail("frozen Prepared DataBundle trace Ref is invalid")
        trace_raw = _read_ref(reader, trace_ref)
        trace = _json(trace_raw, max_bytes=len(trace_raw))
        if not isinstance(trace, dict) or not isinstance(trace.get("source_refs"), list):
            _fail("frozen data trace fields are invalid")
        source_ids = data_manifest.get("source_ids")
        plan = data_manifest.get("import_plan")
        blocks = plan.get("blocks") if isinstance(plan, dict) else None
        if (
            not isinstance(source_ids, list)
            or not source_ids
            or any(not isinstance(source_id, str) or not _ID_RE.fullmatch(source_id) for source_id in source_ids)
            or len(set(source_ids)) != len(source_ids)
            or not isinstance(blocks, list)
        ):
            _fail("frozen DataManifest source order or import plan is invalid")
        planned_source_ids: list[str] = []
        for block in blocks:
            if not isinstance(block, dict) or not isinstance(block.get("source_id"), str):
                _fail("frozen DataManifest import plan Source identity is invalid")
            if block["source_id"] not in planned_source_ids:
                planned_source_ids.append(block["source_id"])
        if planned_source_ids != source_ids or len(trace["source_refs"]) != len(source_ids):
            _fail("frozen DataManifest, import plan, and trace Source order differ")
        expected_data_keys.add(_ref_key(trace_ref))
        for source_id, raw_source_ref in zip(source_ids, trace["source_refs"], strict=True):
            source_ref = _ref(raw_source_ref)
            prepared_source = prepared_sources.get(source_id)
            if (
                prepared_source is None
                or prepared_source["record"].get("purpose") != "data"
                or prepared_source["bytes"] != source_ref
                or _ref_key(source_ref) not in source_bytes
            ):
                _fail("frozen data trace Source differs from its exact Prepared Source record")
        for item in package["ref_map"]:
            ref = _ref(item["ref"])
            if ref["path"].startswith(data_prefix) and _ref_key(ref) not in expected_data_keys:
                _fail("package contains an unreferenced Prepared DataBundle Ref")

    runtime = prepared.get("runtime_bundle")
    if not isinstance(runtime, dict) or set(runtime) != {"digest", "manifest", "worker_entry"} or runtime.get("worker_entry") != "python/optagent_core/worker.py":
        _fail("prepared runtime bundle fields are invalid")
    digest = runtime.get("digest")
    if not isinstance(digest, str) or not _HASH_RE.fullmatch(digest) or digest not in bundles:
        _fail("prepared runtime digest is not packaged")
    if not bundles[digest]["standalone_supported"]:
        _fail("pinned worker snapshot has no local reproduction entry")
    runtime_ref = _ref(runtime["manifest"])
    if runtime_ref["path"] != f".reaslab/optagent/runtime/{digest}/manifest.json":
        _fail("prepared runtime manifest Ref is invalid")
    if _ref(bundles[digest]["manifest"]) != runtime_ref:
        _fail("runtime summary does not bind the prepared runtime manifest")
    runtime_raw = _read_ref(reader, runtime_ref)
    runtime_manifest = _json(runtime_raw)
    if not isinstance(runtime_manifest, dict) or set(runtime_manifest) != {"schema_version", "digest", "files"} or runtime_manifest.get("schema_version") != 1 or runtime_manifest.get("digest") != digest:
        _fail("pinned runtime manifest identity is invalid")
    expected_helpers = _runtime_helpers_for_prepared_schema(prepared["schema_version"])
    if bundles[digest]["helper_paths"] != list(expected_helpers):
        _fail("runtime package helper list does not match its frozen prepared schema")
    entries = runtime_manifest["files"]
    if not isinstance(entries, list) or [item.get("path") for item in entries if isinstance(item, dict)] != list(expected_helpers) or len(entries) != len(expected_helpers):
        _fail("pinned runtime helper list is invalid")
    identity_files: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "byte_len"} or type(item["byte_len"]) is not int or item["byte_len"] < 0 or not isinstance(item["sha256"], str) or not _HASH_RE.fullmatch(item["sha256"]):
            _fail("pinned runtime file record is invalid")
        identity_files.append({"path": item["path"], "sha256": item["sha256"], "byte_len": item["byte_len"]})
        helper_ref = {
            "path": f".reaslab/optagent/runtime/{digest}/{item['path']}",
            "sha256": item["sha256"],
            "byte_len": item["byte_len"],
            "media_type": "text/x-python; charset=utf-8",
        }
        _read_ref(reader, helper_ref)
    identity = json.dumps({"schema_version": 1, "files": identity_files}, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if hashlib.sha256(identity).hexdigest() != digest:
        _fail("pinned runtime digest does not match its manifest")
    complete = _read_stable(root, f"reproduce/runtime/{digest}/COMPLETE")
    if complete != digest.encode("ascii"):
        _fail("pinned runtime COMPLETE marker is invalid")
    worker_path = f"reproduce/runtime/{digest}/python/optagent_core/worker.py"
    worker_raw = _read_stable(root, worker_path)
    if len(worker_raw) != next(item["byte_len"] for item in entries if item["path"] == "python/optagent_core/worker.py"):
        _fail("pinned worker length differs from its runtime manifest")
    try:
        worker_tree = ast.parse(worker_raw.decode("utf-8"), filename="worker.py")
    except (SyntaxError, UnicodeError):
        _fail("pinned worker entry is not valid UTF-8 Python")
    if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_local_reproduction" for node in worker_tree.body):
        _fail("pinned worker snapshot has no local reproduction entry")
    _verify_runtime_layout(root, digest, expected_helpers)


def _verify_runtime_layout(root: Path, digest: str, helper_paths: tuple[str, ...]) -> None:
    runtime = root / "reproduce" / "runtime" / digest
    expected_root = {"manifest.json", "COMPLETE", "python"}
    expected_python = {"optagent_core"}
    expected_helpers = {PurePosixPath(path).name for path in helper_paths}
    for directory, expected in (
        (runtime, expected_root),
        (runtime / "python", expected_python),
        (runtime / "python" / "optagent_core", expected_helpers),
    ):
        info = directory.lstat()
        if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            _fail("runtime import tree contains a link or non-directory")
        names = {entry.name for entry in directory.iterdir()}
        if names != expected:
            _fail("runtime import tree does not match the frozen prepared-schema helper allowlist")
    for name in expected_helpers:
        info = (runtime / "python" / "optagent_core" / name).lstat()
        if _is_reparse(info) or not stat.S_ISREG(info.st_mode):
            _fail("runtime helper is not a regular file")


def _prepared_id_from_ref(ref: dict[str, Any]) -> str:
    match = re.fullmatch(r"\.reaslab/optagent/cases/[0-9a-f-]+/prepared/([0-9a-f-]+)/input/manifest\.json", ref["path"])
    if match is None or not _ID_RE.fullmatch(match.group(1)):
        _fail("prepared input Ref path is invalid")
    return match.group(1)


def _output_path(value: str, package_root: Path, *, must_exist: bool) -> Path:
    path = Path(value)
    if not path.is_absolute():
        _fail("output directory must be an absolute path")
    absolute = Path(os.path.abspath(os.fspath(path)))
    try:
        absolute.relative_to(package_root)
    except ValueError:
        pass
    else:
        _fail("output directory must be outside the extracted package")
    parent = absolute if must_exist else absolute.parent
    parent = _checked_absolute(parent)
    if not parent.is_dir():
        _fail("output parent directory must already exist")
    if must_exist:
        info = absolute.lstat()
        if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            _fail("output root must be a regular directory")
    elif os.path.lexists(absolute):
        _fail("output directory must not already exist")
    return absolute


def _run_child(root: Path, script: Path, output_root: Path, index: int, entry: dict[str, Any], ref_map: list[dict[str, Any]]) -> dict[str, Any]:
    package_manifest = _read_stable(root, "manifest.json")
    package = _json(package_manifest)
    if not isinstance(package, dict) or set(package) != _PACKAGE_FIELDS:
        _fail("package manifest changed before child dispatch")
    _, _, _ = package, entry, ref_map
    leaf = output_root / f"entry-{index:04d}"
    if os.path.lexists(leaf):
        _fail("child output leaf already exists")
    argv = [sys.executable, "-I", os.fspath(script), "--_child-entry", str(index), "--output-dir", os.fspath(leaf)]
    completed = subprocess.run(argv, shell=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
    if completed.returncode != 0:
        _fail(f"local reproduction entry {index} failed with exit {completed.returncode}")
    try:
        result = _json(completed.stdout.encode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("local reproduction child returned invalid JSON") from exc
    return {"entry_index": index, "output_directory": os.fspath(leaf), "report": result}


def _child(root: Path, index: int, output_value: str) -> int:
    manifest, entries, reader = _validate_package(root)
    if type(index) is not int or index < 0 or index >= len(entries):
        _fail("child reproduction index is invalid")
    output = _output_path(output_value, root, must_exist=False)
    entry = entries[index]
    prepared_ref = _ref(entry["prepared_manifest_ref"])
    prepared = _json(_read_ref(reader, prepared_ref))
    helper_paths = _runtime_helpers_for_prepared_schema(prepared.get("schema_version"))
    digest = prepared["runtime_bundle"]["digest"]
    runtime_root = root / "reproduce" / "runtime" / digest
    python_root = runtime_root / "python"
    helper_python_root = Path(os.path.abspath(os.fspath(python_root)))
    if not helper_python_root.is_dir():
        _fail("verified runtime import root is absent")
    if any(name == "optagent_core" or name.startswith("optagent_core.") for name in sys.modules):
        _fail("an OptAgent module was loaded before the verified runtime")
    sys.path = [item for item in sys.path if item and Path(os.path.abspath(item)) != root and Path(os.path.abspath(item)) != root / "reproduce"]
    sys.path.insert(0, os.fspath(helper_python_root))
    sys.dont_write_bytecode = True
    package_module = importlib.import_module("optagent_core")
    worker = importlib.import_module("optagent_core.worker")
    data = importlib.import_module("optagent_core.data")
    if prepared["schema_version"] == 3:
        importlib.import_module("optagent_core.solver_protocol")
    expected_module_paths = {
        "optagent_core": runtime_root / "python/optagent_core/__init__.py",
        "optagent_core.contracts": runtime_root / "python/optagent_core/contracts.py",
        "optagent_core.data": runtime_root / "python/optagent_core/data.py",
        "optagent_core.models": runtime_root / "python/optagent_core/models.py",
        "optagent_core.results": runtime_root / "python/optagent_core/results.py",
        "optagent_core.worker": runtime_root / "python/optagent_core/worker.py",
    }
    if prepared["schema_version"] == 3:
        expected_module_paths["optagent_core.solver_protocol"] = runtime_root / "python/optagent_core/solver_protocol.py"
    if set(name for name in sys.modules if name == "optagent_core" or name.startswith("optagent_core.")) != set(expected_module_paths):
        _fail("verified runtime imported an unlisted OptAgent helper")
    for name, expected in expected_module_paths.items():
        loaded = package_module if name == "optagent_core" else sys.modules[name]
        if Path(os.path.abspath(os.fspath(loaded.__file__))) != Path(os.path.abspath(os.fspath(expected))):
            _fail("imported runtime module is outside the verified bundle")
    reader_type = getattr(data, "PortableReader", None)
    run_local = getattr(worker, "run_local_reproduction", None)
    if not callable(reader_type) or not callable(run_local):
        _fail("verified runtime does not expose the fixed local worker interface")
    portable_reader = reader_type(root, manifest["ref_map"])
    diagnostic_fd = os.dup(sys.stderr.fileno())
    try:
        with os.fdopen(
            diagnostic_fd,
            "w",
            buffering=1,
            encoding=sys.stderr.encoding,
            errors=sys.stderr.errors,
            closefd=False,
        ) as diagnostic_stdout:
            with contextlib.redirect_stdout(diagnostic_stdout):
                result = run_local(portable_reader, prepared_ref, output)
    finally:
        os.close(diagnostic_fd)
    if not isinstance(result, dict) or set(result) != {"report", "report_ref"} or not isinstance(result["report"], dict):
        _fail("local worker returned an invalid report shape")
    sys.stdout.write(json.dumps({"report": result["report"], "report_ref": result["report_ref"]}, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run selected frozen OptAgent inputs as a new local measurement.")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--_child-entry", type=int)
    arguments = parser.parse_args()
    script, root = _script_and_package()
    if arguments._child_entry is not None:
        return _child(root, arguments._child_entry, arguments.output_dir)
    package, entries, _ = _validate_package(root)
    if not entries:
        _fail("this package has no supported standalone native reproduction entry")
    output_root = _output_path(arguments.output_dir, root, must_exist=False)
    output_root.mkdir()
    results = []
    for index, entry in enumerate(entries):
        results.append(_run_child(root, script, output_root, index, entry, package["ref_map"]))
    sys.stdout.write(json.dumps({"kind": "local-reproductions", "case_id": package["case_id"], "entries": results}, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Standalone reproduction stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
