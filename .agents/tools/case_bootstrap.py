#!/usr/bin/env python3
"""Create one OptAgent case skeleton without making workflow decisions.

The coordinator supplies all case semantics. This helper only validates and
persists the resulting directory, state, decision-log, registry, problem, and
initial README contracts. It is intentionally not a dispatcher or planner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from case_state import DEFAULT_APPROVAL_STATUS, DEFAULT_REPORT_CONTRACT, REPORT_CONTRACTS
from case_state import init as init_state
from case_state import read as read_state
from case_state import validate_file as validate_state_file
from case_state import validate_initial_approval
from decision_log import atomic_write, exclusive_lock, read_json, validate_log
from decision_log import init_log
from update_case_readme import write_readme


VERSION = 1
CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
ANALYSIS_KINDS = {
    "comparison", "sensitivity", "pareto", "benchmark", "summary", "other"
}
PUBLIC_DIRECTORIES = (
    "code/data",
    "code/run_outputs",
    "outputs",
    "paper",
    "report",
)
INTERNAL_DIRECTORIES = (
    "data/objects",
    "data/revisions",
    "formulation",
    "solver/temp",
    "verification",
    "reporting",
    "subagents",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", default=".")
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--case-type", choices=["optimization", "analysis"], required=True)
    parser.add_argument(
        "--report-contract",
        choices=sorted(REPORT_CONTRACTS),
        default=DEFAULT_REPORT_CONTRACT,
    )
    parser.add_argument(
        "--approval-status",
        choices=["pending", "approved", "auto_approve"],
        default=DEFAULT_APPROVAL_STATUS,
    )
    parser.add_argument("--name", required=True)
    parser.add_argument(
        "--problem-text-source",
        help="Markdown source path, or '-' to read Problem.md content from stdin.",
    )
    parser.add_argument("--parent")
    parser.add_argument("--delta")
    parser.add_argument("--analysis-kind", choices=sorted(ANALYSIS_KINDS))
    parser.add_argument("--reference", action="append", default=[])
    parser.add_argument(
        "--set-active",
        action="store_true",
        help="Explicitly set registry.active_case to this case.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def normalize_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.rstrip() + "\n"


def read_problem_text(source: str | None) -> str | None:
    if source is None:
        return None
    if source == "-":
        value = sys.stdin.read()
    else:
        value = Path(source).read_text(encoding="utf-8")
    if not value.strip():
        raise ValueError("problem text must not be empty")
    return normalize_text(value)


def validate_case_id(case_id: str, field: str = "case_id") -> None:
    if not CASE_ID_PATTERN.fullmatch(case_id):
        raise ValueError(f"{field} must be a path-safe case identifier")


def relative(workspace: Path, path: Path) -> str:
    return path.relative_to(workspace).as_posix()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def load_registry(path: Path) -> tuple[dict[str, Any], bool]:
    if not path.exists():
        return {"cases": []}, False
    registry = read_json(path)
    cases = registry.get("cases")
    if not isinstance(cases, list) or not all(isinstance(item, dict) for item in cases):
        raise ValueError("registry.json cases must be an array of objects")
    ids = []
    for item in cases:
        case_id = item.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("each registry case must have a non-empty id")
        validate_case_id(case_id, "registry case id")
        ids.append(case_id)
    if len(ids) != len(set(ids)):
        raise ValueError("registry.json contains duplicate case ids")
    active_case = registry.get("active_case")
    if active_case is not None and (
        not isinstance(active_case, str) or active_case not in set(ids)
    ):
        raise ValueError("registry.json active_case must name an existing case")
    return registry, True


def build_registry_entry(args: argparse.Namespace) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": args.case_id,
        "type": args.case_type,
        "name": args.name.strip(),
        "status": "initializing",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if args.case_type == "optimization":
        entry.update(
            {
                "objective": None,
                "solver": None,
                "solve_time": None,
                "problem_class": None,
                "parent": args.parent,
                "delta": args.delta,
            }
        )
    else:
        entry.update(
            {
                "analysis_kind": args.analysis_kind,
                "references": list(args.reference),
            }
        )
    return entry


def identity_matches(existing: dict[str, Any], expected: dict[str, Any]) -> bool:
    common = ("id", "type", "name")
    optimization = ("parent", "delta")
    analysis = ("analysis_kind", "references")
    keys = common + (optimization if expected["type"] == "optimization" else analysis)
    return all(existing.get(key) == expected.get(key) for key in keys)


def validate_input(args: argparse.Namespace, known_ids: set[str]) -> None:
    validate_case_id(args.case_id)
    if not args.name.strip():
        raise ValueError("name must not be empty")
    validate_initial_approval(args.approval_status, args.case_type)

    if args.case_type == "optimization":
        if args.analysis_kind or args.reference:
            raise ValueError("optimization cases cannot define analysis_kind or references")
        if args.parent is not None:
            validate_case_id(args.parent, "parent")
            if args.parent == args.case_id:
                raise ValueError("a case cannot be its own parent")
            if args.parent not in known_ids:
                raise ValueError(f"parent case does not exist in registry: {args.parent}")
        elif args.delta is not None:
            raise ValueError("delta requires parent for an optimization case")
    else:
        if args.parent is not None or args.delta is not None:
            raise ValueError("analysis cases cannot define parent or delta")
        if not args.analysis_kind:
            raise ValueError("analysis cases require --analysis-kind")
        if not args.reference:
            raise ValueError("analysis cases require at least one --reference")
        if len(args.reference) != len(set(args.reference)):
            raise ValueError("analysis references must be unique")
        for reference in args.reference:
            validate_case_id(reference, "analysis reference")
            if reference == args.case_id:
                raise ValueError("an analysis case cannot reference itself")
            if reference not in known_ids:
                raise ValueError(f"analysis reference does not exist in registry: {reference}")


def unexpected_orphan_content(case_dir: Path, internal_dir: Path) -> list[str]:
    allowed_files = {
        case_dir / "Problem.md",
        case_dir / "README.md",
        internal_dir / "state.json",
        internal_dir / "decision_log.json",
    }
    unexpected = []
    for root in (case_dir, internal_dir):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path not in allowed_files:
                unexpected.append(str(path))
    return sorted(unexpected)


def preflight(
    args: argparse.Namespace,
    workspace: Path,
    registry: dict[str, Any],
    registry_exists: bool,
    problem_text: str | None,
) -> dict[str, Any]:
    case_dir = workspace / "cases" / args.case_id
    internal_dir = workspace / ".optagent" / "cases" / args.case_id
    registry_path = workspace / ".optagent" / "registry.json"
    state_path = internal_dir / "state.json"
    log_path = internal_dir / "decision_log.json"
    problem_path = case_dir / "Problem.md"
    readme_path = case_dir / "README.md"
    entries = registry["cases"]
    existing_entry = next((item for item in entries if item["id"] == args.case_id), None)
    known_ids = {item["id"] for item in entries}
    validate_input(args, known_ids)
    expected_entry = build_registry_entry(args)

    if existing_entry is not None and not identity_matches(existing_entry, expected_entry):
        raise ValueError("existing registry case conflicts with requested identity fields")
    if existing_entry is None:
        unexpected = unexpected_orphan_content(case_dir, internal_dir)
        if unexpected:
            raise ValueError(
                "unregistered case directory contains non-bootstrap artifacts: "
                + ", ".join(unexpected)
            )

    if state_path.exists():
        state = read_state(state_path)
        validate_state_file(state_path, state)
        if state.get("approval_status") != args.approval_status:
            raise ValueError("existing state uses a different approval status")
        if state.get("type", "optimization") != args.case_type:
            raise ValueError("existing state uses a different case type")
    if log_path.exists():
        validate_log(log_path)
        log = read_json(log_path)
        expected_mode = "auto_approve" if args.approval_status == "auto_approve" else "manual"
        if log.get("case_id") != args.case_id or log.get("approval_mode") != expected_mode:
            raise ValueError("existing decision log conflicts with requested case identity")

    if problem_text is None:
        if not problem_path.exists():
            raise ValueError("new case requires --problem-text-source")
    elif problem_path.exists():
        current = normalize_text(problem_path.read_text(encoding="utf-8"))
        if current != problem_text:
            raise ValueError("existing Problem.md differs from supplied problem text")

    return {
        "workspace": workspace,
        "case_dir": case_dir,
        "internal_dir": internal_dir,
        "registry_path": registry_path,
        "state_path": state_path,
        "log_path": log_path,
        "problem_path": problem_path,
        "readme_path": readme_path,
        "registry": registry,
        "registry_exists": registry_exists,
        "existing_entry": existing_entry,
        "expected_entry": expected_entry,
    }


def directory_actions(workspace: Path, context: dict[str, Any], dry_run: bool) -> list[dict[str, Any]]:
    paths = [
        workspace / ".optagent" / "cache",
        workspace / ".optagent" / "cases",
        workspace / "data",
        workspace / "cases",
    ]
    if context["expected_entry"]["type"] == "optimization":
        public_directories = PUBLIC_DIRECTORIES
        internal_directories = INTERNAL_DIRECTORIES
    else:
        public_directories = ("outputs", "paper", "report")
        internal_directories = ("reporting",)
    paths.extend(context["case_dir"] / item for item in public_directories)
    paths.extend(context["internal_dir"] / item for item in internal_directories)
    actions = []
    for path in paths:
        existed = path.exists()
        if not dry_run:
            path.mkdir(parents=True, exist_ok=True)
        actions.append(
            {
                "path": relative(workspace, path),
                "kind": "directory",
                "action": "reused" if existed else "created",
            }
        )
    return actions


def file_action(workspace: Path, path: Path, existed: bool) -> dict[str, Any]:
    return {
        "path": relative(workspace, path),
        "kind": "file",
        "action": "reused" if existed else "created",
        "sha256": sha256_file(path),
    }


def build_result(
    context: dict[str, Any],
    actions: list[dict[str, Any]],
    dry_run: bool,
    registry_entry_created: bool,
) -> dict[str, Any]:
    workspace = context["workspace"]
    return {
        "version": VERSION,
        "dry_run": dry_run,
        "case_id": context["expected_entry"]["id"],
        "case_type": context["expected_entry"]["type"],
        "registry_entry_created": registry_entry_created,
        "paths": {
            "public_case": relative(workspace, context["case_dir"]),
            "internal_case": relative(workspace, context["internal_dir"]),
            "registry": relative(workspace, context["registry_path"]),
        },
        "actions": actions,
        "validation": {
            "state": "valid" if context["state_path"].exists() else "planned",
            "decision_log": "valid" if context["log_path"].exists() else "planned",
            "registry": "valid" if context["registry_path"].exists() else "planned",
            "problem": "present" if context["problem_path"].exists() else "planned",
            "readme": "present" if context["readme_path"].exists() else "planned",
        },
    }


def bootstrap(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Path(args.workspace_root).resolve()
    problem_text = read_problem_text(args.problem_text_source)
    registry_path = workspace / ".optagent" / "registry.json"

    if args.dry_run:
        registry, registry_exists = load_registry(registry_path)
        context = preflight(args, workspace, registry, registry_exists, problem_text)
        actions = directory_actions(workspace, context, dry_run=True)
        for path in (
            context["problem_path"],
            context["state_path"],
            context["log_path"],
            context["registry_path"],
            context["readme_path"],
        ):
            actions.append(
                {
                    "path": relative(workspace, path),
                    "kind": "file",
                    "action": "reused" if path.exists() else "created",
                }
            )
        return build_result(context, actions, True, context["existing_entry"] is None)

    bootstrap_lock = workspace / ".optagent" / "case_bootstrap"
    with exclusive_lock(bootstrap_lock):
        registry, registry_exists = load_registry(registry_path)
        context = preflight(args, workspace, registry, registry_exists, problem_text)
        actions = directory_actions(workspace, context, dry_run=False)

        problem_existed = context["problem_path"].exists()
        if not problem_existed:
            if problem_text is None:
                raise ValueError("new case requires --problem-text-source")
            atomic_write_text(context["problem_path"], problem_text)
        actions.append(file_action(workspace, context["problem_path"], problem_existed))

        state_existed = context["state_path"].exists()
        init_state(
            context["state_path"],
            args.approval_status,
            args.case_type,
            args.report_contract,
        )
        validate_state_file(context["state_path"], read_state(context["state_path"]))
        actions.append(file_action(workspace, context["state_path"], state_existed))

        log_existed = context["log_path"].exists()
        approval_mode = "auto_approve" if args.approval_status == "auto_approve" else "manual"
        init_log(context["log_path"], args.case_id, approval_mode)
        validate_log(context["log_path"])
        actions.append(file_action(workspace, context["log_path"], log_existed))

        registry_entry_created = context["existing_entry"] is None
        if registry_entry_created:
            context["registry"]["cases"].append(context["expected_entry"])
        if args.set_active or context["registry"].get("active_case") is None:
            context["registry"]["active_case"] = args.case_id
        registry_existed = context["registry_path"].exists()
        atomic_write(context["registry_path"], context["registry"])
        actions.append(file_action(workspace, context["registry_path"], registry_existed))

        readme_existed = context["readme_path"].exists()
        if not readme_existed:
            write_readme(context["case_dir"], context["internal_dir"], args.name.strip())
        actions.append(file_action(workspace, context["readme_path"], readme_existed))

        return build_result(context, actions, False, registry_entry_created)


def error_result(message: str) -> dict[str, Any]:
    return {"version": VERSION, "error": message}


def main() -> int:
    try:
        result = bootstrap(parse_args())
    except (OSError, ValueError, json.JSONDecodeError, TimeoutError) as exc:
        print(json.dumps(error_result(str(exc)), ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
