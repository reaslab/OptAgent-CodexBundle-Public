#!/usr/bin/env python3
"""Initialize, transition, and validate OptAgent case state atomically."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STAGES = {
    "initializing", "awaiting_approval", "formulating", "solving",
    "verifying", "reporting", "completed", "error",
}
APPROVALS = {"pending", "approved", "rejected", "auto_approve"}
REPORT_CONTRACTS = {"latex", "markdown"}
DEFAULT_APPROVAL_STATUS = "auto_approve"
DEFAULT_REPORT_CONTRACT = "latex"
TRANSITIONS = {
    "initializing": {"awaiting_approval", "formulating", "reporting", "error"},
    "awaiting_approval": {"formulating", "error"},
    "formulating": {"solving", "error"},
    "solving": {"verifying", "error"},
    "verifying": {"formulating", "solving", "reporting", "error"},
    "reporting": {"completed", "error"},
    "completed": set(),
    "error": set(),
}
COMPLETABLE = {"formulating", "solving", "verifying", "reporting"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--path", required=True)
    init.add_argument(
        "--approval-status",
        choices=sorted(APPROVALS),
        default=DEFAULT_APPROVAL_STATUS,
    )
    init.add_argument("--case-type", choices=["optimization", "analysis"], default="optimization")
    init.add_argument(
        "--report-contract",
        choices=sorted(REPORT_CONTRACTS),
        default=DEFAULT_REPORT_CONTRACT,
    )
    transition = sub.add_parser("transition")
    transition.add_argument("--path", required=True)
    transition.add_argument("--to", choices=sorted(STAGES), required=True)
    transition.add_argument("--approval-status", choices=sorted(APPROVALS))
    validate = sub.add_parser("validate")
    validate.add_argument("--path", required=True)
    report_contract = sub.add_parser("set-report-contract")
    report_contract.add_argument("--path", required=True)
    report_contract.add_argument("--contract", choices=sorted(REPORT_CONTRACTS), required=True)
    return parser.parse_args()


def read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("state.json must be an object")
    return value


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except Exception:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise


def validate(state: dict) -> None:
    stage = state.get("stage")
    approval = state.get("approval_status")
    completed = state.get("completed_stages")
    case_type = state.get("type", "optimization")
    report_contract = state.get("report_contract", DEFAULT_REPORT_CONTRACT)
    if stage not in STAGES:
        raise ValueError(f"invalid stage: {stage}")
    if approval not in APPROVALS:
        raise ValueError(f"invalid approval_status: {approval}")
    if report_contract not in REPORT_CONTRACTS:
        raise ValueError(f"invalid report_contract: {report_contract}")
    if not isinstance(state.get("awaiting_user"), bool):
        raise ValueError("awaiting_user must be boolean")
    if not isinstance(completed, list) or len(completed) != len(set(completed)):
        raise ValueError("completed_stages must be a unique string array")
    if not all(item in COMPLETABLE for item in completed):
        raise ValueError("completed_stages contains a non-completable stage")
    solver_attempts = state.get("solver_attempts")
    error_log = state.get("error_log")
    if not isinstance(solver_attempts, list) or not all(
        isinstance(item, dict) for item in solver_attempts
    ):
        raise ValueError("solver_attempts must be an object array")
    if not isinstance(error_log, list) or not all(isinstance(item, dict) for item in error_log):
        raise ValueError("error_log must be an object array")
    attempt_ids = [item.get("attempt_id") for item in solver_attempts]
    if any(not isinstance(item, str) or not item for item in attempt_ids):
        raise ValueError("every solver attempt requires a non-empty attempt_id")
    if len(attempt_ids) != len(set(attempt_ids)):
        raise ValueError("solver_attempts contains duplicate attempt_id values")
    if stage == "awaiting_approval" and not (
        approval == "pending" and state["awaiting_user"] is True
    ):
        raise ValueError("awaiting_approval requires pending and awaiting_user=true")
    if approval == "auto_approve" and state["awaiting_user"]:
        raise ValueError("auto_approve cannot await the user")
    if stage == "completed":
        required = {"reporting"} if case_type == "analysis" else COMPLETABLE
        missing = required - set(completed)
        if missing:
            raise ValueError(f"completed state is missing stages: {sorted(missing)}")


def validate_approval_record(path: Path, state: dict) -> None:
    decision_path = path.parent / "decision_log.json"
    if not decision_path.exists():
        return
    decision_log = json.loads(decision_path.read_text(encoding="utf-8"))
    if not isinstance(decision_log, dict):
        raise ValueError("decision_log.json must be an object")
    recorded_mode = decision_log.get("approval_mode")
    expected_mode = (
        "auto_approve" if state["approval_status"] == "auto_approve" else "manual"
    )
    if recorded_mode != expected_mode:
        raise ValueError(
            "state approval_status conflicts with decision_log approval_mode: "
            f"{state['approval_status']} vs {recorded_mode}"
        )


def validate_file(path: Path, state: dict) -> None:
    validate(state)
    validate_approval_record(path, state)


def validate_initial_approval(approval: str, case_type: str) -> None:
    allowed_initial = (
        {"pending", "auto_approve"}
        if case_type == "optimization"
        else {"approved", "auto_approve"}
    )
    if approval not in allowed_initial:
        raise ValueError(
            f"cannot initialize {case_type} with approval_status={approval}"
        )


def init(
    path: Path,
    approval: str,
    case_type: str,
    report_contract: str = DEFAULT_REPORT_CONTRACT,
) -> None:
    validate_initial_approval(approval, case_type)
    if report_contract not in REPORT_CONTRACTS:
        raise ValueError(f"invalid report_contract: {report_contract}")
    if path.exists():
        state = read(path)
        validate_file(path, state)
        if (
            state.get("approval_status") != approval
            or state.get("type", "optimization") != case_type
            or state.get("report_contract", DEFAULT_REPORT_CONTRACT) != report_contract
        ):
            raise ValueError(
                "existing state uses different approval status, case type, or report contract"
            )
        return
    manual = approval == "pending"
    if manual:
        initial_stage = "awaiting_approval"
    elif case_type == "analysis" and approval in {"approved", "auto_approve"}:
        initial_stage = "reporting"
    elif case_type == "optimization" and approval == "auto_approve":
        initial_stage = "formulating"
    else:
        initial_stage = "initializing"
    state = {
        "stage": initial_stage,
        "awaiting_user": manual,
        "completed_stages": [],
        "rework_count": 0,
        "solver_attempts": [],
        "error_log": [],
        "approval_status": approval,
        "type": case_type,
        "report_contract": report_contract,
        "preferred_solver": "gurobipy",
        "fallback_solvers": ["pulp", "ortools-linear"],
        "optimality_target": "global_optimum",
        "time_limit_sec": 300,
        "max_time_limit_sec": 1800,
        "preferred_model_class": "MILP",
        "allow_auto_solver_switch": True,
        "allow_time_budget_increase": True,
        "notes": [],
    }
    validate(state)
    write(path, state)


def set_report_contract(path: Path, contract: str) -> None:
    state = read(path)
    validate_file(path, state)
    if contract not in REPORT_CONTRACTS:
        raise ValueError(f"invalid report_contract: {contract}")
    state["report_contract"] = contract
    validate_file(path, state)
    write(path, state)


def record_solver_attempt(
    path: Path,
    attempt: dict[str, Any],
    error_message: str | None = None,
) -> None:
    """Atomically append one managed formal-solve attempt to case state.

    The managed run id is the idempotency key. Replaying the exact same record
    is harmless; conflicting data for an existing id is rejected.
    """
    state = read(path)
    validate_file(path, state)
    attempt_id = attempt.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ValueError("solver attempt requires a non-empty attempt_id")
    normalized = dict(attempt)
    normalized.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
    normalized["success"] = bool(normalized.get("success"))
    existing = next(
        (item for item in state["solver_attempts"] if item.get("attempt_id") == attempt_id),
        None,
    )
    if existing is not None:
        if existing != normalized:
            raise ValueError(f"conflicting solver attempt already exists: {attempt_id}")
        return
    state["solver_attempts"].append(normalized)
    if not normalized["success"]:
        message = (error_message or normalized.get("raw_status") or "formal solve failed")
        state["error_log"].append(
            {
                "recorded_at": normalized["recorded_at"],
                "source": "run_managed_solver",
                "attempt_id": attempt_id,
                "failure_class": normalized.get("failure_class") or "solver_attempt_failed",
                "message": str(message)[:2000],
            }
        )
    validate_file(path, state)
    write(path, state)


def transition(path: Path, target: str, approval: str | None) -> None:
    state = read(path)
    validate_file(path, state)
    current = state["stage"]
    if target == current:
        if approval is not None and approval != state["approval_status"]:
            raise ValueError(
                "an idempotent transition cannot change approval_status"
            )
        return
    if target not in TRANSITIONS[current]:
        raise ValueError(f"invalid state transition: {current} -> {target}")
    current_approval = state["approval_status"]
    if approval is not None and approval != current_approval:
        allowed_change = (
            current_approval == "pending"
            and (
                (approval == "approved" and target == "formulating")
                or (approval == "rejected" and target == "error")
            )
        )
        if not allowed_change:
            raise ValueError(
                f"invalid approval transition: {current_approval} -> {approval}"
            )
        state["approval_status"] = approval
    if current == "awaiting_approval":
        valid_manual_exit = (
            target == "formulating" and state["approval_status"] == "approved"
        ) or (
            target == "error" and state["approval_status"] in {"pending", "rejected"}
        )
        if not valid_manual_exit:
            raise ValueError("manual execution requires approval or rejection")
    if target != "error" and current in COMPLETABLE and current not in state["completed_stages"]:
        state["completed_stages"].append(current)
    state["stage"] = target
    state["awaiting_user"] = target == "awaiting_approval"
    validate_file(path, state)
    write(path, state)


def main() -> int:
    args = parse_args()
    path = Path(args.path)
    if args.command == "init":
        init(path, args.approval_status, args.case_type, args.report_contract)
    elif args.command == "transition":
        transition(path, args.to, args.approval_status)
    elif args.command == "set-report-contract":
        set_report_contract(path, args.contract)
    else:
        validate_file(path, read(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
