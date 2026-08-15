#!/usr/bin/env python3
"""Read-only, bounded views over OptAgent evidence artifacts.

This helper never writes artifacts and never replaces their authority. It is a
token-conscious inspection aid: callers must reopen the source file whenever a
preview is truncated, a check fails, or a claim needs full supporting detail.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from collections import deque
from pathlib import Path
from typing import Any, Iterator


VERSION = 1
DEFAULT_MAX_ITEMS = 20
DEFAULT_MAX_STRING_CHARS = 240
DEFAULT_HEAD_LINES = 20
DEFAULT_TAIL_LINES = 20
DEFAULT_MATCH_LINES = 20
DEFAULT_CSV_SAMPLE_ROWS = 5
DEFAULT_MAX_KEY_TRACKING = 100_000
CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
CASE_STAGES = ("formulation", "solver", "verification", "reporting")
PUBLIC_BUILD_SUFFIXES = {
    ".aux",
    ".fdb_latexmk",
    ".fls",
    ".log",
    ".out",
    ".synctex.gz",
}
MAX_HASHED_PUBLIC_ARTIFACT_BYTES = 64 * 1024 * 1024


class EvidenceError(ValueError):
    """Expected input error rendered as structured JSON rather than a traceback."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    json_view = sub.add_parser("json", help="Inspect selected JSON Pointer values.")
    json_view.add_argument("--path", required=True)
    json_view.add_argument(
        "--pointer",
        action="append",
        default=[],
        help="RFC 6901 JSON Pointer to inspect; repeatable. Without one, only root shape is returned.",
    )
    add_json_preview_args(json_view)

    csv_view = sub.add_parser("csv", help="Stream CSV shape, missing-value, and sample evidence.")
    csv_view.add_argument("--path", required=True)
    csv_view.add_argument("--sample-rows", type=positive_int, default=DEFAULT_CSV_SAMPLE_ROWS)
    csv_view.add_argument("--tail-rows", type=nonnegative_int, default=DEFAULT_CSV_SAMPLE_ROWS)
    csv_view.add_argument("--key-field", action="append", default=[])
    csv_view.add_argument(
        "--check-duplicates",
        action="store_true",
        help="Check duplicate composite keys up to --max-key-tracking entries.",
    )
    csv_view.add_argument(
        "--max-key-tracking",
        type=positive_int,
        default=DEFAULT_MAX_KEY_TRACKING,
        help="Maximum distinct key tuples retained for an exact duplicate check.",
    )
    add_json_preview_args(csv_view)

    text_view = sub.add_parser("text", help="Inspect bounded head, tail, and matching log lines.")
    text_view.add_argument("--path", required=True)
    text_view.add_argument("--head-lines", type=nonnegative_int, default=DEFAULT_HEAD_LINES)
    text_view.add_argument("--tail-lines", type=nonnegative_int, default=DEFAULT_TAIL_LINES)
    text_view.add_argument(
        "--match",
        action="append",
        default=[],
        help="Regular expression to count and sample; repeatable.",
    )
    text_view.add_argument("--max-match-lines", type=positive_int, default=DEFAULT_MATCH_LINES)
    text_view.add_argument("--max-line-chars", type=positive_int, default=DEFAULT_MAX_STRING_CHARS)

    stage_view = sub.add_parser(
        "stage",
        help="Summarize one case stage from authoritative artifacts without writing files.",
    )
    add_case_view_args(stage_view)
    stage_view.add_argument("--stage", choices=CASE_STAGES, required=True)

    claims_view = sub.add_parser(
        "claims",
        help="Build a bounded, source-linked claim index for reporting.",
    )
    add_case_view_args(claims_view)

    return parser.parse_args()


def add_json_preview_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-items", type=positive_int, default=DEFAULT_MAX_ITEMS)
    parser.add_argument(
        "--max-string-chars", type=positive_int, default=DEFAULT_MAX_STRING_CHARS
    )
    parser.add_argument(
        "--max-depth",
        type=nonnegative_int,
        default=2,
        help="Nested preview depth; deeper values are represented by shape only.",
    )


def add_case_view_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace-root", default=".")
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--max-items", type=positive_int, default=DEFAULT_MAX_ITEMS)
    parser.add_argument(
        "--max-string-chars", type=positive_int, default=DEFAULT_MAX_STRING_CHARS
    )


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def source_metadata(path: Path, sha256: str | None = None) -> dict[str, Any]:
    if not path.is_file():
        raise EvidenceError(f"source file does not exist or is not a regular file: {path}")
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": sha256,
    }


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def clip_text(value: str, max_chars: int) -> tuple[str, bool]:
    if len(value) <= max_chars:
        return value, False
    if max_chars == 1:
        return value[:1], True
    head = max(1, max_chars // 2)
    tail = max(1, max_chars - head - 1)
    return f"{value[:head]}...{value[-tail:]}", True


def shape(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {"type": "object", "item_count": len(value)}
    if isinstance(value, list):
        return {"type": "array", "item_count": len(value)}
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, (int, float)):
        return {"type": "number"}
    return {"type": "string"}


def compact_json(
    value: Any,
    max_items: int,
    max_string_chars: int,
    max_depth: int,
    depth: int = 0,
) -> tuple[Any, bool]:
    if isinstance(value, str):
        return clip_text(value, max_string_chars)
    if not isinstance(value, (dict, list)):
        return value, False
    if depth >= max_depth:
        result = shape(value)
        result["preview_omitted"] = True
        return result, True
    if isinstance(value, list):
        preview = []
        truncated = len(value) > max_items
        for item in value[:max_items]:
            compact, item_truncated = compact_json(
                item, max_items, max_string_chars, max_depth, depth + 1
            )
            preview.append(compact)
            truncated = truncated or item_truncated
        return {
            "type": "array",
            "item_count": len(value),
            "items": preview,
            "truncated": truncated,
        }, truncated

    keys = sorted(value)
    preview = {}
    truncated = len(keys) > max_items
    for key in keys[:max_items]:
        compact, item_truncated = compact_json(
            value[key], max_items, max_string_chars, max_depth, depth + 1
        )
        preview[key] = compact
        truncated = truncated or item_truncated
    return {
        "type": "object",
        "item_count": len(keys),
        "keys": keys[:max_items],
        "items": preview,
        "truncated": truncated,
    }, truncated


def decode_pointer_token(token: str) -> str:
    value = ""
    index = 0
    while index < len(token):
        if token[index] != "~":
            value += token[index]
            index += 1
            continue
        if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
            raise EvidenceError(f"invalid JSON Pointer escape in token: {token!r}")
        value += "~" if token[index + 1] == "0" else "/"
        index += 2
    return value


def resolve_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise EvidenceError(f"JSON Pointer must be empty or start with '/': {pointer!r}")
    current = document
    for raw_token in pointer[1:].split("/"):
        token = decode_pointer_token(raw_token)
        if isinstance(current, dict):
            if token not in current:
                raise EvidenceError(f"JSON Pointer not found: {pointer}")
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit():
                raise EvidenceError(f"JSON Pointer array token must be an index: {pointer}")
            index = int(token)
            if index >= len(current):
                raise EvidenceError(f"JSON Pointer not found: {pointer}")
            current = current[index]
        else:
            raise EvidenceError(f"JSON Pointer traverses a scalar value: {pointer}")
    return current


def json_view(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.path)
    if not path.is_file():
        raise EvidenceError(f"source file does not exist or is not a regular file: {path}")
    raw = path.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"failed to parse JSON: {exc}") from exc

    source = source_metadata(path, sha256_bytes(raw))
    selections = []
    truncated = False
    for pointer in args.pointer:
        selected = resolve_pointer(document, pointer)
        preview, selection_truncated = compact_json(
            selected, args.max_items, args.max_string_chars, args.max_depth
        )
        selections.append(
            {
                "pointer": pointer,
                "shape": shape(selected),
                "preview": preview,
                "truncated": selection_truncated,
            }
        )
        truncated = truncated or selection_truncated

    root = shape(document)
    if not args.pointer:
        if isinstance(document, dict):
            root["keys"] = sorted(document)[: args.max_items]
            root["truncated"] = len(document) > args.max_items or bool(document)
        elif isinstance(document, list):
            root["truncated"] = bool(document)
        else:
            root["value"] = document
            root["truncated"] = False
        truncated = bool(root["truncated"])

    return {
        "version": VERSION,
        "view": "json",
        "authoritative": False,
        "source": source,
        "root": root,
        "selections": selections,
        "truncated": truncated,
        "open_source_required": truncated,
    }


def binary_lines(path: Path, digest: hashlib._Hash) -> Iterator[str]:
    with path.open("rb") as handle:
        first = True
        for raw_line in handle:
            digest.update(raw_line)
            encoding = "utf-8-sig" if first else "utf-8"
            first = False
            yield raw_line.decode(encoding, errors="replace")


def compact_row(
    row: dict[str | None, str | None], max_items: int, max_string_chars: int
) -> tuple[dict[str, str], bool]:
    result = {}
    truncated = False
    keys = sorted(key for key in row if key is not None)
    if len(keys) > max_items:
        truncated = True
    for key in keys[:max_items]:
        value = row.get(key) or ""
        clipped, value_truncated = clip_text(value, max_string_chars)
        result[key] = clipped
        truncated = truncated or value_truncated
    return result, truncated


def limited_mapping(mapping: dict[str, Any], max_items: int) -> tuple[dict[str, Any], bool]:
    keys = sorted(mapping)
    return ({key: mapping[key] for key in keys[:max_items]}, len(keys) > max_items)


def csv_view(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.path)
    digest = hashlib.sha256()
    reader = csv.DictReader(binary_lines(path, digest))
    fieldnames = reader.fieldnames
    if not fieldnames:
        raise EvidenceError("CSV has no header row")
    if None in fieldnames:
        raise EvidenceError("CSV header contains an unnamed column")
    missing_keys = sorted(set(args.key_field) - set(fieldnames))
    if missing_keys:
        raise EvidenceError(f"CSV key fields are missing from the header: {missing_keys}")

    total_rows = 0
    missing_counts = {field: 0 for field in fieldnames}
    head = []
    tail: deque[dict[str, str]] = deque(maxlen=args.tail_rows)
    output_truncated = len(fieldnames) > args.max_items
    duplicate_count = 0
    key_tracking_complete = True
    seen_keys: set[tuple[str, ...]] = set()

    try:
        for raw_row in reader:
            total_rows += 1
            if None in raw_row:
                raise EvidenceError(f"CSV row {total_rows + 1} has more fields than the header")
            for field in fieldnames:
                if not (raw_row.get(field) or "").strip():
                    missing_counts[field] += 1
            row, row_truncated = compact_row(
                raw_row, args.max_items, args.max_string_chars
            )
            output_truncated = output_truncated or row_truncated
            if len(head) < args.sample_rows:
                head.append(row)
            if args.tail_rows:
                tail.append(row)

            if args.check_duplicates and args.key_field:
                key = tuple(raw_row.get(field) or "" for field in args.key_field)
                if len(seen_keys) >= args.max_key_tracking:
                    key_tracking_complete = False
                elif key in seen_keys:
                    duplicate_count += 1
                else:
                    seen_keys.add(key)
    except csv.Error as exc:
        raise EvidenceError(f"failed to parse CSV: {exc}") from exc

    fields, fields_truncated = limited_mapping(missing_counts, args.max_items)
    output_truncated = output_truncated or fields_truncated
    if total_rows > args.sample_rows + args.tail_rows:
        output_truncated = True
    duplicate_check: dict[str, Any]
    if not args.check_duplicates:
        duplicate_check = {"status": "not_requested"}
    elif not args.key_field:
        duplicate_check = {"status": "not_requested", "reason": "no key fields supplied"}
    elif key_tracking_complete:
        duplicate_check = {
            "status": "complete",
            "key_fields": args.key_field,
            "duplicate_rows": duplicate_count,
        }
    else:
        duplicate_check = {
            "status": "partial",
            "key_fields": args.key_field,
            "duplicate_rows_observed_before_limit": duplicate_count,
            "max_key_tracking": args.max_key_tracking,
        }

    return {
        "version": VERSION,
        "view": "csv",
        "authoritative": False,
        "source": source_metadata(path, digest.hexdigest()),
        "schema": {
            "field_count": len(fieldnames),
            "fields": fieldnames[: args.max_items],
            "fields_truncated": len(fieldnames) > args.max_items,
        },
        "row_count": total_rows,
        "missing_values": {
            "by_field": fields,
            "fields_truncated": fields_truncated,
        },
        "duplicate_check": duplicate_check,
        "head_rows": head,
        "tail_rows": list(tail),
        "truncated": output_truncated,
        "open_source_required": output_truncated or duplicate_check["status"] == "partial",
    }


def text_view(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.path)
    try:
        patterns = [(pattern, re.compile(pattern)) for pattern in args.match]
    except re.error as exc:
        raise EvidenceError(f"invalid match regular expression: {exc}") from exc

    digest = hashlib.sha256()
    head = []
    tail: deque[dict[str, Any]] = deque(maxlen=args.tail_lines)
    matches = {
        pattern: {"count": 0, "lines": [], "truncated": False}
        for pattern, _ in patterns
    }
    total_lines = 0
    output_truncated = False

    for raw_line in binary_lines(path, digest):
        total_lines += 1
        clipped, line_truncated = clip_text(raw_line.rstrip("\r\n"), args.max_line_chars)
        output_truncated = output_truncated or line_truncated
        entry = {"line": total_lines, "text": clipped, "truncated": line_truncated}
        if len(head) < args.head_lines:
            head.append(entry)
        if args.tail_lines:
            tail.append(entry)
        for pattern, compiled in patterns:
            if compiled.search(raw_line):
                result = matches[pattern]
                result["count"] += 1
                if len(result["lines"]) < args.max_match_lines:
                    result["lines"].append(entry)
                else:
                    result["truncated"] = True
                    output_truncated = True

    if total_lines > args.head_lines + args.tail_lines:
        output_truncated = True
    return {
        "version": VERSION,
        "view": "text",
        "authoritative": False,
        "source": source_metadata(path, digest.hexdigest()),
        "line_count": total_lines,
        "head_lines": head,
        "tail_lines": list(tail),
        "matches": matches,
        "truncated": output_truncated,
        "open_source_required": output_truncated,
    }


def validate_case_id(case_id: str) -> None:
    if not CASE_ID_PATTERN.fullmatch(case_id):
        raise EvidenceError("case_id must be a path-safe case identifier")


def relative_to_workspace(path: Path, workspace: Path) -> str:
    try:
        return path.resolve().relative_to(workspace).as_posix()
    except ValueError:
        return str(path.resolve())


class CaseEvidenceContext:
    """Lazy, read-only access to the authoritative artifacts of one case."""

    def __init__(self, workspace: Path, case_id: str, max_items: int, max_chars: int):
        self.workspace = workspace
        self.case_id = case_id
        self.max_items = max_items
        self.max_chars = max_chars
        self.public_case = workspace / "cases" / case_id
        self.internal_case = workspace / ".optagent" / "cases" / case_id
        self.sources: dict[str, dict[str, Any]] = {}
        self.documents: dict[str, Any | None] = {}
        self.issues: list[dict[str, str]] = []
        self.truncated = False
        self.open_source_required = False

    def path(self, *parts: str) -> Path:
        return self.internal_case.joinpath(*parts)

    def public_path(self, *parts: str) -> Path:
        return self.public_case.joinpath(*parts)

    def add_issue(self, source: str, kind: str, detail: str) -> None:
        entry = {"source": source, "kind": kind, "detail": detail}
        if entry not in self.issues:
            self.issues.append(entry)
        self.open_source_required = True

    def file(self, name: str, path: Path) -> dict[str, Any]:
        existing = self.sources.get(name)
        if existing is not None:
            return existing
        record: dict[str, Any] = {
            "path": relative_to_workspace(path, self.workspace),
            "exists": path.is_file(),
            "bytes": None,
            "sha256": None,
        }
        if path.is_file():
            try:
                record["bytes"] = path.stat().st_size
                record["sha256"] = sha256_file(path)
            except OSError as exc:
                record["read_error"] = str(exc)
                self.add_issue(name, "read_error", str(exc))
        else:
            self.add_issue(name, "missing", f"required or expected artifact is missing: {record['path']}")
        self.sources[name] = record
        return record

    def json(self, name: str, path: Path) -> Any | None:
        if name in self.documents:
            return self.documents[name]
        record: dict[str, Any] = {
            "path": relative_to_workspace(path, self.workspace),
            "exists": path.is_file(),
            "bytes": None,
            "sha256": None,
        }
        self.sources[name] = record
        if not path.is_file():
            self.documents[name] = None
            self.add_issue(name, "missing", f"required or expected artifact is missing: {record['path']}")
            return None
        try:
            raw = path.read_bytes()
            record["bytes"] = len(raw)
            record["sha256"] = sha256_bytes(raw)
            value = json.loads(raw.decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.documents[name] = None
            record["parse_error"] = str(exc)
            self.add_issue(name, "invalid_json", str(exc))
            return None
        if not isinstance(value, dict):
            self.documents[name] = None
            record["parse_error"] = "top-level JSON value must be an object for a case view"
            self.add_issue(name, "invalid_shape", record["parse_error"])
            return None
        self.documents[name] = value
        return value

    def optional_json(self, name: str, path: Path) -> Any | None:
        if path.is_file() or name in self.sources or name in self.documents:
            return self.json(name, path)
        self.sources[name] = {
            "path": relative_to_workspace(path, self.workspace),
            "exists": False,
            "bytes": None,
            "sha256": None,
            "optional": True,
        }
        self.documents[name] = None
        return None

    def linked_scalar(
        self, source: str, document: Any | None, pointer: str
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "source": source,
            "pointer": pointer,
            "present": False,
            "value": None,
            "truncated": False,
        }
        if document is None:
            return result
        try:
            value = resolve_pointer(document, pointer)
        except EvidenceError:
            return result
        if isinstance(value, str):
            clipped, was_truncated = clip_text(value, self.max_chars)
            result.update({"present": True, "value": clipped, "truncated": was_truncated})
            if was_truncated:
                self.truncated = True
                self.open_source_required = True
            return result
        if value is None or isinstance(value, (bool, int, float)):
            result.update({"present": True, "value": value})
            return result
        result["shape"] = shape(value)
        return result

    def linked_count(
        self, source: str, document: Any | None, pointer: str
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "source": source,
            "pointer": pointer,
            "present": False,
            "value": None,
        }
        if document is None:
            return result
        try:
            value = resolve_pointer(document, pointer)
        except EvidenceError:
            return result
        if isinstance(value, (dict, list)):
            result.update({"present": True, "value": len(value)})
        else:
            result["shape"] = shape(value)
        return result


def source_link(source: str, pointer: str = "") -> dict[str, str]:
    return {"source": source, "pointer": pointer}


def case_context(args: argparse.Namespace) -> CaseEvidenceContext:
    validate_case_id(args.case_id)
    workspace = Path(args.workspace_root).resolve()
    if not workspace.is_dir():
        raise EvidenceError(f"workspace root does not exist or is not a directory: {workspace}")
    return CaseEvidenceContext(
        workspace, args.case_id, args.max_items, args.max_string_chars
    )


def count_authenticity(
    context: CaseEvidenceContext, manifest: Any | None
) -> dict[str, Any]:
    result: dict[str, int] = {}
    if not isinstance(manifest, dict):
        return {"source": "manifest", "pointer": "/data_objects", "counts": result}
    objects = manifest.get("data_objects")
    if not isinstance(objects, dict):
        return {"source": "manifest", "pointer": "/data_objects", "counts": result}
    for item in objects.values():
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata")
        authenticity = metadata.get("authenticity") if isinstance(metadata, dict) else None
        if isinstance(authenticity, str) and authenticity:
            result[authenticity] = result.get(authenticity, 0) + 1
    values, was_truncated = limited_mapping(result, context.max_items)
    if was_truncated:
        context.truncated = True
        context.open_source_required = True
    return {
        "source": "manifest",
        "pointer": "/data_objects",
        "counts": values,
        "truncated": was_truncated,
    }


def formulation_summary(context: CaseEvidenceContext) -> tuple[dict[str, Any], list[str]]:
    context.file("problem", context.public_path("Problem.md"))
    manifest = context.json("manifest", context.path("data", "data_manifest.json"))
    spec = context.json("spec", context.path("formulation", "opt_spec.json"))
    return (
        {
            "data": {
                "object_count": context.linked_count("manifest", manifest, "/data_objects"),
                "relationship_count": context.linked_count("manifest", manifest, "/relationships"),
                "authenticity_counts": count_authenticity(context, manifest),
            },
            "model": {
                "problem_class": context.linked_scalar("spec", spec, "/problem_class"),
                "sense": context.linked_scalar("spec", spec, "/sense"),
                "set_count": context.linked_count("spec", spec, "/sets"),
                "parameter_count": context.linked_count("spec", spec, "/parameters"),
                "decision_variable_count": context.linked_count("spec", spec, "/decision_variables"),
                "constraint_count": context.linked_count("spec", spec, "/constraints"),
                "indexed_constraint_count": context.linked_count("spec", spec, "/indexed_constraints"),
                "modeling_decision_count": context.linked_count("spec", spec, "/modeling_decisions"),
            },
        },
        [
            "Open opt_spec.json for equations, variable/index semantics, data_ref details, or any absent field.",
            "Open data_manifest.json for provenance, data objects, and unresolved ambiguities.",
        ],
    )


def solver_summary(context: CaseEvidenceContext) -> tuple[dict[str, Any], list[str]]:
    solution = context.json("solution", context.path("solver", "solution.json"))
    record = context.json("execution_record", context.path("solver", "execution_record.json"))
    context.file("solver_log", context.path("solver", "solver_log.txt"))
    status = context.linked_scalar("solution", solution, "/status")
    if status["present"] and status["value"] not in {
        "optimal", "feasible", "infeasible", "unbounded", "timeout", "error",
        "solver_unavailable", "not_implemented",
    }:
        context.add_issue("solution", "unknown_status", "solution status is missing or unsupported")
    return (
        {
            "solution": {
                "status": status,
                "has_incumbent": context.linked_scalar("solution", solution, "/has_incumbent"),
                "solution_count": context.linked_scalar("solution", solution, "/solution_count"),
                "objective_value": context.linked_scalar("solution", solution, "/objective_value"),
                "backend": context.linked_scalar("solution", solution, "/backend"),
                "termination_condition": context.linked_scalar("solution", solution, "/termination_condition"),
                "solve_time_sec": context.linked_scalar("solution", solution, "/solve_time_sec"),
                "mip_gap": context.linked_scalar("solution", solution, "/mip_gap"),
                "known_optimal_value": context.linked_scalar("solution", solution, "/known_optimal_value"),
            },
            "execution": {
                "entrypoint": context.linked_scalar("execution_record", record, "/entrypoint"),
                "entrypoint_sha256": context.linked_scalar("execution_record", record, "/entrypoint_sha256"),
                "data_snapshot_sha256": context.linked_scalar("execution_record", record, "/data_snapshot_sha256"),
                "recorded_status": context.linked_scalar("execution_record", record, "/status"),
                "time_limit_sec": context.linked_scalar("execution_record", record, "/time_limit_sec"),
            },
        },
        [
            "Open solution.json for variables, solver notes, bounds, or any non-scalar result.",
            "Open execution_record.json and solver_log.txt for execution discrepancies or failure details.",
        ],
    )


def bundle_check_summary(
    context: CaseEvidenceContext, bundle: Any | None
) -> dict[str, Any]:
    if not isinstance(bundle, dict):
        return {"source": "precheck_bundle", "pointer": "/checks", "available": False}
    checks = bundle.get("checks")
    if not isinstance(checks, list):
        return {"source": "precheck_bundle", "pointer": "/checks", "available": False}
    counts: dict[str, int] = {}
    nonpass = []
    for index, item in enumerate(checks):
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if isinstance(status, str):
            counts[status] = counts.get(status, 0) + 1
        if status != "pass":
            if len(nonpass) >= context.max_items:
                context.truncated = True
                context.open_source_required = True
                continue
            name, name_truncated = clip_text(str(item.get("name", "unnamed_check")), context.max_chars)
            detail, detail_truncated = clip_text(str(item.get("detail", "")), context.max_chars)
            nonpass.append(
                {
                    "name": name,
                    "status": status,
                    "detail": detail,
                    "source": "precheck_bundle",
                    "pointer": f"/checks/{index}",
                    "truncated": name_truncated or detail_truncated,
                }
            )
            if name_truncated or detail_truncated:
                context.truncated = True
                context.open_source_required = True
            if status in {"fail", "unsupported", "environment_limited"}:
                context.open_source_required = True
    count_values, count_truncated = limited_mapping(counts, context.max_items)
    if count_truncated:
        context.truncated = True
        context.open_source_required = True
    return {
        "source": "precheck_bundle",
        "pointer": "/checks",
        "available": True,
        "status_counts": count_values,
        "nonpass_checks": nonpass,
        "truncated": count_truncated or len(nonpass) < sum(1 for item in checks if isinstance(item, dict) and item.get("status") != "pass"),
    }


def verification_summary(context: CaseEvidenceContext) -> tuple[dict[str, Any], list[str]]:
    diagnosis = context.json("diagnosis", context.path("verification", "diagnosis.json"))
    bundle = context.json("precheck_bundle", context.path("verification", "precheck_bundle.json"))
    validation = context.linked_scalar("diagnosis", diagnosis, "/validation/status")
    coverage = context.linked_scalar("diagnosis", diagnosis, "/validation/coverage")
    if validation["present"] and validation["value"] != "passed":
        context.open_source_required = True
    if coverage["present"] and coverage["value"] != "full":
        context.open_source_required = True
    return (
        {
            "diagnosis": {
                "validation_status": validation,
                "validation_coverage": coverage,
                "failure_type": context.linked_scalar("diagnosis", diagnosis, "/failure_type"),
                "attribution_owner": context.linked_scalar("diagnosis", diagnosis, "/attribution/owner"),
                "attribution_confidence": context.linked_scalar("diagnosis", diagnosis, "/attribution/confidence"),
                "next_action": context.linked_scalar("diagnosis", diagnosis, "/next_action"),
            },
            "precheck": bundle_check_summary(context, bundle),
        },
        [
            "Open diagnosis.json for evidence, notes, attribution rationale, and any partial or failed validation.",
            "Open precheck_bundle.json or precheck_raw outputs before acting on a bundle finding.",
        ],
    )


def active_user_visible_decisions(
    context: CaseEvidenceContext, log: Any | None
) -> dict[str, Any]:
    entries = log.get("entries") if isinstance(log, dict) else None
    if not isinstance(entries, list):
        return {"source": "decision_log", "pointer": "/entries", "items": [], "truncated": False}
    valid = [(index, item) for index, item in enumerate(entries) if isinstance(item, dict)]
    superseded = {
        item.get("supersedes")
        for _, item in valid
        if item.get("status") == "active" and isinstance(item.get("supersedes"), str)
    }
    items = []
    omitted = 0
    for index, item in valid:
        impact = item.get("impact")
        if (
            item.get("status") != "active"
            or item.get("id") in superseded
            or not isinstance(impact, dict)
            or impact.get("user_visible") is not True
        ):
            continue
        if len(items) >= context.max_items:
            omitted += 1
            continue
        decision, decision_truncated = clip_text(str(item.get("decision", "")), context.max_chars)
        rationale, rationale_truncated = clip_text(str(item.get("rationale", "")), context.max_chars)
        risk, risk_truncated = clip_text(str(impact.get("risk", "")), context.max_chars)
        items.append(
            {
                "id": item.get("id"),
                "stage": item.get("stage"),
                "category": item.get("category"),
                "decision": decision,
                "rationale": rationale,
                "risk": risk,
                "source": "decision_log",
                "pointer": f"/entries/{index}",
                "truncated": decision_truncated or rationale_truncated or risk_truncated,
            }
        )
        if decision_truncated or rationale_truncated or risk_truncated:
            context.truncated = True
            context.open_source_required = True
    if omitted:
        context.truncated = True
        context.open_source_required = True
    return {
        "source": "decision_log",
        "pointer": "/entries",
        "items": items,
        "truncated": bool(omitted),
        "omitted_count": omitted,
    }


def public_artifact_inventory(context: CaseEvidenceContext) -> dict[str, Any]:
    candidates: list[tuple[str, Path]] = []
    problem = context.public_path("Problem.md")
    if problem.is_file():
        candidates.append(("problem", problem))
    for name in ("run.py", "README.md", "requirements.txt"):
        path = context.public_path("code", name)
        if path.is_file():
            candidates.append(("code", path))
    for category, root in (
        ("run_output", context.public_path("code", "run_outputs")),
        ("output", context.public_path("outputs")),
        ("paper", context.public_path("paper")),
    ):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if (
                not path.is_file()
                or path.is_symlink()
                or "__pycache__" in path.parts
                or path.name.startswith(".")
                or any(path.name.endswith(suffix) for suffix in PUBLIC_BUILD_SUFFIXES)
            ):
                continue
            candidates.append((category, path))

    unique: dict[str, tuple[str, Path]] = {}
    for category, path in candidates:
        unique.setdefault(relative_to_workspace(path, context.workspace), (category, path))
    artifacts = []
    for index, (relative, (category, path)) in enumerate(sorted(unique.items())):
        if index >= context.max_items:
            context.truncated = True
            context.open_source_required = True
            break
        source = f"public:{relative}"
        size = path.stat().st_size
        record: dict[str, Any] = {
            "path": relative,
            "exists": True,
            "bytes": size,
            "sha256": None,
        }
        if size <= MAX_HASHED_PUBLIC_ARTIFACT_BYTES:
            try:
                record["sha256"] = sha256_file(path)
            except OSError as exc:
                record["read_error"] = str(exc)
                context.add_issue(source, "read_error", str(exc))
        else:
            record["hash_status"] = "not_computed_large"
            context.open_source_required = True
        context.sources[source] = record
        artifacts.append({"category": category, "source": source, "pointer": ""})
    return {
        "items": artifacts,
        "truncated": len(unique) > len(artifacts),
        "total_count": len(unique),
    }


def reporting_summary(context: CaseEvidenceContext) -> tuple[dict[str, Any], list[str]]:
    state = context.json("state", context.path("state.json"))
    analysis_case = isinstance(state, dict) and state.get("type") == "analysis"
    loader = context.optional_json if analysis_case else context.json
    solution = loader("solution", context.path("solver", "solution.json"))
    diagnosis = loader("diagnosis", context.path("verification", "diagnosis.json"))
    decisions = context.json("decision_log", context.path("decision_log.json"))
    status = context.linked_scalar("solution", solution, "/status")
    validation = context.linked_scalar("diagnosis", diagnosis, "/validation/status")
    coverage = context.linked_scalar("diagnosis", diagnosis, "/validation/coverage")
    if status["present"] and status["value"] != "optimal":
        context.open_source_required = True
    if validation["present"] and validation["value"] != "passed":
        context.open_source_required = True
    if coverage["present"] and coverage["value"] != "full":
        context.open_source_required = True
    return (
        {
            "case": {
                "type": context.linked_scalar("state", state, "/type"),
                "stage": context.linked_scalar("state", state, "/stage"),
                "approval_status": context.linked_scalar("state", state, "/approval_status"),
            },
            "result": {
                "solver_status": status,
                "objective_value": context.linked_scalar("solution", solution, "/objective_value"),
                "backend": context.linked_scalar("solution", solution, "/backend"),
                "solve_time_sec": context.linked_scalar("solution", solution, "/solve_time_sec"),
                "has_incumbent": context.linked_scalar("solution", solution, "/has_incumbent"),
            },
            "verification": {
                "status": validation,
                "coverage": coverage,
                "failure_type": context.linked_scalar("diagnosis", diagnosis, "/failure_type"),
                "next_action": context.linked_scalar("diagnosis", diagnosis, "/next_action"),
            },
            "active_user_visible_decisions": active_user_visible_decisions(context, decisions),
            "public_artifacts": public_artifact_inventory(context),
        },
        [
            "Open opt_spec.json when writing equations or interpreting model semantics.",
            "Open solution.json, diagnosis.json, and decision_log.json for any non-optimal, partial, failed, missing, or disputed claim.",
            "Open the cited public artifact for tables, figures, exact values, or content not represented by this index.",
        ],
    )


def case_stage_view(args: argparse.Namespace) -> dict[str, Any]:
    context = case_context(args)
    if args.stage == "formulation":
        summary, required_source_reads = formulation_summary(context)
    elif args.stage == "solver":
        summary, required_source_reads = solver_summary(context)
    elif args.stage == "verification":
        summary, required_source_reads = verification_summary(context)
    else:
        summary, required_source_reads = reporting_summary(context)
    return {
        "version": VERSION,
        "view": "stage",
        "authoritative": False,
        "workspace": str(context.workspace),
        "case_id": context.case_id,
        "stage": args.stage,
        "sources": context.sources,
        "summary": summary,
        "required_source_reads": required_source_reads,
        "issues": context.issues,
        "truncated": context.truncated,
        "open_source_required": context.open_source_required,
    }


def direct_claim(
    kind: str, value: dict[str, Any], label: str
) -> dict[str, Any] | None:
    if not value.get("present"):
        return None
    return {
        "kind": kind,
        "label": label,
        "value": value.get("value"),
        "source": value["source"],
        "pointer": value["pointer"],
        "truncated": value.get("truncated", False),
        "claim_class": "direct_artifact_value",
    }


def claims_view(args: argparse.Namespace) -> dict[str, Any]:
    context = case_context(args)
    summary, required_source_reads = reporting_summary(context)
    claims: list[dict[str, Any]] = []
    for kind, label, value in (
        ("case_type", "case type", summary["case"]["type"]),
        ("case_stage", "workflow stage", summary["case"]["stage"]),
        ("solver_status", "solver status", summary["result"]["solver_status"]),
        ("objective_value", "reported objective value", summary["result"]["objective_value"]),
        ("solver_backend", "solver backend", summary["result"]["backend"]),
        ("solve_time_sec", "reported solve time", summary["result"]["solve_time_sec"]),
        ("verification_status", "verification status", summary["verification"]["status"]),
        ("verification_coverage", "verification coverage", summary["verification"]["coverage"]),
        ("failure_type", "diagnosis failure type", summary["verification"]["failure_type"]),
    ):
        claim = direct_claim(kind, value, label)
        if claim is not None:
            claims.append(claim)

    for item in summary["active_user_visible_decisions"]["items"]:
        claims.append(
            {
                "kind": "user_visible_decision",
                "label": "active user-visible decision",
                "value": {
                    "id": item.get("id"),
                    "decision": item.get("decision"),
                    "rationale": item.get("rationale"),
                    "risk": item.get("risk"),
                },
                "source": item["source"],
                "pointer": item["pointer"],
                "truncated": item["truncated"],
                "claim_class": "direct_artifact_value",
            }
        )
    for item in summary["public_artifacts"]["items"]:
        claims.append(
            {
                "kind": "public_artifact",
                "label": item["category"],
                "value": context.sources[item["source"]]["path"],
                "source": item["source"],
                "pointer": item["pointer"],
                "truncated": False,
                "claim_class": "artifact_inventory",
            }
        )

    return {
        "version": VERSION,
        "view": "claims",
        "authoritative": False,
        "workspace": str(context.workspace),
        "case_id": context.case_id,
        "sources": context.sources,
        "claims": claims,
        "required_source_reads": required_source_reads,
        "issues": context.issues,
        "truncated": context.truncated,
        "open_source_required": context.open_source_required,
        "limitations": [
            "This is a deterministic index, not a substitute for source artifacts or semantic verification.",
            "Do not infer equations, variable assignments, large tables, or causal explanations from this view.",
        ],
    }


def error_result(message: str) -> dict[str, Any]:
    return {
        "version": VERSION,
        "authoritative": False,
        "error": message,
        "truncated": False,
        "open_source_required": True,
    }


def main() -> int:
    args = parse_args()
    try:
        if args.command == "json":
            result = json_view(args)
        elif args.command == "csv":
            result = csv_view(args)
        elif args.command == "text":
            result = text_view(args)
        elif args.command == "stage":
            result = case_stage_view(args)
        else:
            result = claims_view(args)
    except (EvidenceError, OSError) as exc:
        print(json.dumps(error_result(str(exc)), ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
