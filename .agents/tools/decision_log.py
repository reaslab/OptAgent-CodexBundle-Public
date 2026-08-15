#!/usr/bin/env python3
"""Create and append validated OptAgent decision-log entries atomically."""

from __future__ import annotations

import argparse
import json
import os
import sys
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


STAGES = {
    "initializing", "formulating", "solving", "verifying", "reporting", "completed", "error"
}
CATEGORIES = {
    "ambiguity_resolution", "data_generation", "data_imputation", "modeling_choice",
    "solver_fallback", "time_budget_change", "approximation", "constraint_relaxation",
    "verification_disposition", "rework_routing", "failure_disposition", "other",
}
CATEGORY_ALIASES = {
    "ambiguity": "ambiguity_resolution",
    "data_synthesis": "data_generation",
    "model_choice": "modeling_choice",
    "solver_switch": "solver_fallback",
    "verifier_disposition": "verification_disposition",
    "failure": "failure_disposition",
}
ACTORS = {"coordinator", "formulator", "solver", "verifier", "reporter", "system"}
STATUSES = {"active", "rejected"}
APPROVAL_MODES = {"manual", "auto_approve"}
USER_VISIBLE_CATEGORIES = {
    "ambiguity_resolution", "data_generation", "data_imputation", "approximation",
    "constraint_relaxation", "failure_disposition",
}
REPRODUCIBLE_DATA_CATEGORIES = {"data_generation", "data_imputation"}
ID_PATTERN = re.compile(r"^decision_[0-9]{3,}$")
DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage an OptAgent decision_log.json file.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init")
    init.add_argument("--path", required=True)
    init.add_argument("--case-id", required=True)
    init.add_argument("--approval-mode", choices=sorted(APPROVAL_MODES), required=True)

    append = subparsers.add_parser("append")
    append.add_argument("--path", required=True)
    append.add_argument("--entry", required=True, help="JSON file containing one decision entry")

    validate = subparsers.add_parser("validate")
    validate.add_argument("--path", required=True)
    return parser.parse_args()


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


@contextmanager
def exclusive_lock(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 10
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            os.close(fd)
            break
        except FileExistsError:
            try:
                stale = time.time() - lock_path.stat().st_mtime > 300
                if stale:
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for decision log lock: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def require_text(entry: dict, key: str) -> None:
    if not isinstance(entry.get(key), str) or not entry[key].strip():
        raise ValueError(f"Entry field '{key}' must be a non-empty string")


def require_string_array(entry: dict, key: str) -> None:
    value = entry.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Entry field '{key}' must be a string array")


def validate_ref(value: str, field: str) -> None:
    if not value or "\\" in value or DRIVE_PATTERN.match(value):
        raise ValueError(f"{field} must use an internal-case-relative POSIX path: {value}")
    path_part = value.split("#", 1)[0]
    path = PurePosixPath(path_part)
    if (
        not path_part
        or path.is_absolute()
        or ".." in path.parts
        or (path.parts and path.parts[0] == ".optagent")
    ):
        raise ValueError(f"{field} must be relative to the internal case directory: {value}")


def active_ids(entries: list[dict]) -> set[str]:
    superseded = {
        item["supersedes"] for item in entries
        if item.get("status") == "active" and isinstance(item.get("supersedes"), str)
    }
    return {
        item["id"] for item in entries
        if item.get("status") == "active" and item.get("id") not in superseded
    }


def validate_entry(entry: dict, entries: list[dict]) -> None:
    existing_ids = {item.get("id") for item in entries}
    for key in ("id", "stage", "category", "status", "actor", "trigger", "decision", "rationale"):
        require_text(entry, key)
    for key in ("alternatives_considered", "assumptions", "evidence_refs", "affected_artifacts"):
        require_string_array(entry, key)

    if not ID_PATTERN.fullmatch(entry["id"]):
        raise ValueError("Entry id must match decision_NNN")
    if entry["id"] in existing_ids:
        raise ValueError(f"Duplicate decision id: {entry['id']}")
    if entry["stage"] not in STAGES:
        raise ValueError(f"Unsupported stage: {entry['stage']}")
    if entry["category"] not in CATEGORIES:
        raise ValueError(f"Unsupported category: {entry['category']}")
    if entry["status"] not in STATUSES:
        raise ValueError(f"Unsupported status: {entry['status']}")
    if entry["actor"] not in ACTORS:
        raise ValueError(f"Unsupported actor: {entry['actor']}")

    for value in entry["evidence_refs"]:
        validate_ref(value, "evidence_refs")
    for value in entry["affected_artifacts"]:
        validate_ref(value, "affected_artifacts")

    impact = entry.get("impact")
    if not isinstance(impact, dict):
        raise ValueError("Entry field 'impact' must be an object")
    scope = impact.get("scope")
    if not isinstance(scope, list) or not all(isinstance(item, str) for item in scope):
        raise ValueError("impact.scope must be a string array")
    if not isinstance(impact.get("risk"), str):
        raise ValueError("impact.risk must be a string")
    if not isinstance(impact.get("user_visible"), bool):
        raise ValueError("impact.user_visible must be boolean")
    if entry["category"] in USER_VISIBLE_CATEGORIES and not impact["user_visible"]:
        raise ValueError(f"{entry['category']} decisions must be user-visible")

    reproducibility = entry.get("reproducibility")
    if entry["category"] in REPRODUCIBLE_DATA_CATEGORIES and not isinstance(reproducibility, dict):
        raise ValueError(f"{entry['category']} requires reproducibility metadata")
    if reproducibility is not None:
        if not isinstance(reproducibility, dict):
            raise ValueError("reproducibility must be null or an object")
        require_text(reproducibility, "method")
        if not isinstance(reproducibility.get("randomness_used"), bool):
            raise ValueError("reproducibility.randomness_used must be boolean")
        if reproducibility["randomness_used"] and not isinstance(reproducibility.get("seed"), (int, str)):
            raise ValueError("reproducibility.seed is required when randomness_used is true")

    supersedes = entry.get("supersedes")
    if supersedes is not None:
        if entry["status"] != "active":
            raise ValueError("Only an active decision may supersede another decision")
        if supersedes not in active_ids(entries):
            raise ValueError(f"supersedes must reference a currently active decision: {supersedes}")


def init_log(path: Path, case_id: str, approval_mode: str) -> None:
    with exclusive_lock(path):
        if path.exists():
            value = read_json(path)
            if value.get("case_id") != case_id:
                raise ValueError("Existing decision log belongs to a different case")
            if value.get("approval_mode") != approval_mode:
                raise ValueError("Existing decision log uses a different approval mode")
            return
        atomic_write(path, {
            "version": 1, "case_id": case_id, "approval_mode": approval_mode, "entries": []
        })


def append_entry(path: Path, entry_source: str) -> None:
    if entry_source == "-":
        entry = json.load(sys.stdin)
        if not isinstance(entry, dict):
            raise ValueError("decision entry must be a JSON object")
    else:
        entry = read_json(Path(entry_source))
    entry["category"] = CATEGORY_ALIASES.get(entry.get("category"), entry.get("category"))
    impact = entry.get("impact")
    if (
        entry.get("category") == "modeling_choice"
        and isinstance(impact, dict)
        and impact.get("user_visible") is False
    ):
        print("decision not appended: non-user-visible modeling choices belong in opt_spec.json")
        return
    with exclusive_lock(path):
        log = read_json(path)
        entries = log.get("entries")
        if (
            log.get("version") != 1
            or log.get("approval_mode") not in APPROVAL_MODES
            or not isinstance(entries, list)
            or not all(isinstance(item, dict) for item in entries)
        ):
            raise ValueError("Unsupported or malformed decision log")
        validate_entry(entry, entries)
        entry["recorded_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        entries.append(entry)
        atomic_write(path, log)


def validate_log(path: Path) -> None:
    log = read_json(path)
    entries = log.get("entries")
    if (
        log.get("version") != 1
        or not isinstance(log.get("case_id"), str)
        or not log["case_id"].strip()
        or log.get("approval_mode") not in APPROVAL_MODES
        or not isinstance(entries, list)
        or not all(isinstance(item, dict) for item in entries)
    ):
        raise ValueError("Unsupported or malformed decision log")
    prior: list[dict] = []
    for entry in entries:
        validate_entry(entry, prior)
        recorded_at = entry.get("recorded_at")
        if not isinstance(recorded_at, str) or not recorded_at.endswith("Z"):
            raise ValueError(f"Entry {entry.get('id')} has an invalid recorded_at timestamp")
        prior.append(entry)


def main() -> int:
    args = parse_args()
    if args.command == "init":
        init_log(Path(args.path), args.case_id, args.approval_mode)
    elif args.command == "append":
        append_entry(Path(args.path), args.entry)
    else:
        validate_log(Path(args.path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
