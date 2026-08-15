#!/usr/bin/env python3
"""Run one public solver hook in the managed Python runtime.

The public ``run.py`` remains self-contained. This tool owns the internal
result, execution, and log records so a solver agent never has to pass an
internal output path into public code or transport a full variable vector via
stdout.

Execution context: this Codex migration runs the wrapper with the same local
Python interpreter as this orchestration tool. The subprocess timeout is
enforced here rather than through a platform-specific ``python --timeout``
launcher option.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from case_state import record_solver_attempt


DEFAULT_TIME_LIMIT_SEC = 300
BUSY_EXIT_CODE = 75
MAX_MANAGED_OUTPUT_TAIL_BYTES = 4096
RESULT_STATUSES = {
    "optimal",
    "feasible",
    "infeasible",
    "unbounded",
    "timeout",
    "error",
    "solver_unavailable",
    "not_implemented",
}
HANDLED_SOLVE_STATUSES = {"optimal", "feasible", "infeasible", "unbounded", "timeout"}
ROUTED_SOLVER_HINTS = ("gurobipy", "coptpy", "mosek")


MANAGED_WRAPPER_TEMPLATE = r'''# __ROUTING_HINT__
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

CONFIG = json.loads(__CONFIG_JSON__)
RESULT_STATUSES = {
    "optimal", "feasible", "infeasible", "unbounded", "timeout",
    "error", "solver_unavailable", "not_implemented",
}
HANDLED_SOLVE_STATUSES = {"optimal", "feasible", "infeasible", "unbounded", "timeout"}


def workspace_root() -> Path:
    current = Path(__file__).resolve()
    for candidate in (current.parent, *current.parents):
        if candidate.name == ".optagent":
            return candidate.parent
    raise RuntimeError("managed runner is not located below .optagent")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def sha256_or(path: Path, fallback: str) -> str:
    if not path.is_file():
        return fallback
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def failure_result(message: str, detail: str | None = None) -> dict:
    raw_status = detail or message
    return {
        "backend": "unknown",
        "status": "error",
        "raw_status": raw_status,
        "termination_condition": "error",
        "objective_value": None,
        "best_bound": None,
        "solve_time_sec": None,
        "mip_gap": None,
        "has_incumbent": False,
        "solution_count": 0,
        "solver_available": True,
        "variables": {},
        "metrics": {"last_phase": "managed_runner", "solvers_tried": []},
        "notes": [message],
    }


def normalize_result(value: object) -> dict:
    if not isinstance(value, dict):
        return failure_result("run_formal_solve must return a JSON object")

    result = dict(value)
    status = result.get("status")
    if not isinstance(status, str) or status not in RESULT_STATUSES:
        return failure_result(f"invalid normalized solver status: {status!r}")
    if not isinstance(result.get("has_incumbent"), bool):
        return failure_result("normalized result must contain boolean has_incumbent")
    if not isinstance(result.get("variables"), dict):
        return failure_result("normalized result must contain an object variables field")

    result.setdefault("backend", "unknown")
    result["backend"] = str(result["backend"])
    result.setdefault("raw_status", status)
    result["raw_status"] = str(result["raw_status"])
    result.setdefault("termination_condition", "error" if status == "error" else status)
    result["termination_condition"] = str(result["termination_condition"])
    result.setdefault("objective_value", None)
    result.setdefault("best_bound", None)
    result.setdefault("solve_time_sec", None)
    result.setdefault("mip_gap", None)
    result.setdefault("solution_count", 1 if result["has_incumbent"] else 0)
    result.setdefault("solver_available", status != "solver_unavailable")
    if not isinstance(result.get("solver_available"), bool):
        result["solver_available"] = status != "solver_unavailable"
    if not isinstance(result.get("metrics"), dict):
        result["metrics"] = {}
    if not isinstance(result.get("notes"), list):
        result["notes"] = [str(result["notes"])]
    return result


def execution_record(root: Path, entrypoint: Path, result: dict) -> dict:
    data_manifest = entrypoint.parent / "data" / "manifest.json"
    model_exports = {}
    for name, filename in (("lp", "model.lp"), ("mps", "model.mps")):
        candidate = entrypoint.parent / filename
        if candidate.is_file():
            model_exports[name] = relative(candidate, root)
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    solvers_tried = metrics.get("solvers_tried", [])
    if not isinstance(solvers_tried, list):
        solvers_tried = []
    return {
        "entrypoint": relative(entrypoint, root),
        "entrypoint_sha256": sha256_or(entrypoint, "missing_entrypoint"),
        "data_snapshot_sha256": sha256_or(data_manifest, "no_manifest"),
        "time_limit_sec": CONFIG["time_limit_sec"],
        "bash_grace_sec": CONFIG["bash_grace_sec"],
        "bash_timeout_ms": CONFIG["execution_timeout_sec"] * 1000,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "backend": result.get("backend"),
        "status": result.get("status"),
        "dry_run_supported": True,
        "check_inputs_supported": True,
        "model_exports": model_exports,
        "solvers_tried": solvers_tried,
        "managed_run_id": CONFIG["run_id"],
    }


def load_public_module(entrypoint: Path):
    spec = importlib.util.spec_from_file_location("optagent_public_solver", entrypoint)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import public solver entrypoint: {entrypoint}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    root = workspace_root()
    entrypoint = root / CONFIG["entrypoint"]
    solution_path = root / CONFIG["solution_path"]
    execution_path = root / CONFIG["execution_path"]
    try:
        module = load_public_module(entrypoint)
        hook = getattr(module, "run_formal_solve", None)
        if not callable(hook):
            raise RuntimeError("public run.py must define callable run_formal_solve(time_limit)")
        result = normalize_result(hook(int(CONFIG["time_limit_sec"])))
    except Exception as error:
        detail = f"{type(error).__name__}: {error}"
        print(f"Managed runner error: {detail}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr, limit=6)
        result = failure_result("managed formal solve failed", detail)

    write_json(solution_path, result)
    write_json(execution_path, execution_record(root, entrypoint, result))
    print(
        "Result: status={status}, termination_condition={termination}, "
        "objective_value={objective}, solve_time_sec={elapsed}".format(
            status=result.get("status"),
            termination=result.get("termination_condition"),
            objective=result.get("objective_value"),
            elapsed=result.get("solve_time_sec"),
        )
    )
    print("Phase: completed")
    return 0 if result.get("status") in HANDLED_SOLVE_STATUSES else 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one OptAgent public solver hook in the managed runtime."
    )
    parser.add_argument("--case-id", required=True)
    parser.add_argument(
        "--workspace-root",
        default=".",
        help="Project workspace root; defaults to the current directory.",
    )
    return parser.parse_args()


def atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def validate_case_id(case_id: str) -> str:
    candidate = Path(case_id)
    if (
        not case_id
        or candidate.name != case_id
        or case_id in {".", ".."}
        or "/" in case_id
        or "\\" in case_id
    ):
        raise ValueError("case-id must be a single case directory name")
    return case_id


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def read_time_limit(state: dict[str, Any]) -> int:
    value = state.get("time_limit_sec", DEFAULT_TIME_LIMIT_SEC)
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_TIME_LIMIT_SEC
    return limit if limit > 0 else DEFAULT_TIME_LIMIT_SEC


def timeout_values(time_limit_sec: int) -> tuple[int, int]:
    grace = max(60, min(300, (time_limit_sec + 4) // 5))
    return grace, time_limit_sec + grace


def bounded_output_tail(value: str, limit: int = MAX_MANAGED_OUTPUT_TAIL_BYTES) -> str:
    """Keep diagnostic output useful without turning a fallback record into a log."""
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return value
    return encoded[-limit:].decode("utf-8", errors="ignore")


def is_lock_busy(error: OSError) -> bool:
    return error.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}


class CaseSolveLock:
    """An advisory, process-scoped lock for one active local case helper."""

    def __init__(self, handle: TextIO, *, windows: bool) -> None:
        self.handle = handle
        self.windows = windows

    def release(self) -> None:
        try:
            if self.windows:
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()


def acquire_case_solve_lock(lock_path: Path) -> CaseSolveLock | None:
    """Acquire the normal-path local mutex without interpreting remote state.

    A missing lock is deliberately only a local re-entry signal. It cannot
    prove that an interrupted remote managed execution has stopped.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return CaseSolveLock(handle, windows=True)

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return CaseSolveLock(handle, windows=False)
    except OSError as error:
        handle.close()
        if is_lock_busy(error):
            return None
        raise


def sha256_or(path: Path, fallback: str) -> str:
    if not path.is_file():
        return fallback
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relative(path: Path, workspace: Path) -> str:
    return path.relative_to(workspace).as_posix()


def failure_result(reason: str, detail: str | None = None) -> dict[str, Any]:
    raw_status = detail or reason
    return {
        "backend": "unknown",
        "status": "error",
        "raw_status": raw_status,
        "termination_condition": "error",
        "objective_value": None,
        "best_bound": None,
        "solve_time_sec": None,
        "mip_gap": None,
        "has_incumbent": False,
        "solution_count": 0,
        "solver_available": True,
        "variables": {},
        "metrics": {"last_phase": "managed_runner", "solvers_tried": []},
        "notes": [reason],
    }


def observed_record_commit_status(solution_path: Path, execution_path: Path, run_id: str) -> str:
    """Describe the managed records seen before this helper writes a fallback."""
    if not solution_path.is_file() or not execution_path.is_file():
        return "missing"
    try:
        load_json(solution_path)
        record = load_json(execution_path)
    except Exception:
        return "invalid"
    return "committed" if record.get("managed_run_id") == run_id else "stale"


def fallback_failure_class(record_status: str, managed_output_tail: str) -> str:
    """Classify only evidence available locally; never infer OOM from an exit code."""
    if managed_output_tail.startswith("failed to start python:"):
        return "runner_start_failed"
    if "timed out after" in managed_output_tail:
        return "runner_timeout"
    if record_status in {"invalid", "stale"}:
        return "record_invalid"
    return "unknown"


def fallback_execution_record(
    workspace: Path,
    entrypoint: Path,
    result: dict[str, Any],
    time_limit_sec: int,
    grace_sec: int,
    execution_timeout_sec: int,
    run_id: str,
    process_exit_code: int,
    managed_output_tail: str,
    record_commit_status: str,
    failure_class: str,
) -> dict[str, Any]:
    data_manifest = entrypoint.parent / "data" / "manifest.json"
    model_exports: dict[str, str] = {}
    for name, filename in (("lp", "model.lp"), ("mps", "model.mps")):
        candidate = entrypoint.parent / filename
        if candidate.is_file():
            model_exports[name] = relative(candidate, workspace)
    return {
        "entrypoint": relative(entrypoint, workspace),
        "entrypoint_sha256": sha256_or(entrypoint, "missing_entrypoint"),
        "data_snapshot_sha256": sha256_or(data_manifest, "no_manifest"),
        "time_limit_sec": time_limit_sec,
        "bash_grace_sec": grace_sec,
        "bash_timeout_ms": execution_timeout_sec * 1000,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "backend": result.get("backend"),
        "status": result.get("status"),
        "dry_run_supported": True,
        "check_inputs_supported": True,
        "model_exports": model_exports,
        "solvers_tried": [],
        "managed_run_id": run_id,
        "process_exit_code": process_exit_code,
        "managed_output_tail": bounded_output_tail(managed_output_tail),
        "record_commit_status": record_commit_status,
        "failure_class": failure_class,
    }


def route_hint(entrypoint: Path) -> str:
    try:
        source = entrypoint.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return ""
    for solver in ROUTED_SOLVER_HINTS:
        if solver in source:
            return f"optagent-managed-solver-hint: {solver}"
    return ""


def managed_wrapper_source(
    case_id: str,
    time_limit_sec: int,
    grace_sec: int,
    execution_timeout_sec: int,
    run_id: str,
    entrypoint: Path,
    solution_path: Path,
    execution_path: Path,
    workspace: Path,
) -> str:
    config = {
        "case_id": case_id,
        "time_limit_sec": time_limit_sec,
        "bash_grace_sec": grace_sec,
        "execution_timeout_sec": execution_timeout_sec,
        "run_id": run_id,
        "entrypoint": relative(entrypoint, workspace),
        "solution_path": relative(solution_path, workspace),
        "execution_path": relative(execution_path, workspace),
    }
    hint = route_hint(entrypoint)
    return (
        MANAGED_WRAPPER_TEMPLATE.replace("__ROUTING_HINT__", hint)
        .replace("__CONFIG_JSON__", repr(json.dumps(config, ensure_ascii=False)))
    )


def log_tail(log_path: Path) -> str:
    if not log_path.is_file():
        return ""
    with log_path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - MAX_MANAGED_OUTPUT_TAIL_BYTES))
        return handle.read().decode("utf-8", errors="ignore").rstrip()


def run_managed_process(
    command: list[str],
    workspace: Path,
    log_path: Path,
    timeout_sec: int,
) -> tuple[int, str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", newline="\n") as log:
        log.write("Managed solver command: " + " ".join(command) + "\n")
        log.flush()
        try:
            process = subprocess.Popen(
                command,
                cwd=workspace,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as error:
            message = f"failed to start python: {type(error).__name__}: {error}"
            log.write(message + "\n")
            return 1, message
        try:
            process_exit_code = process.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            process_exit_code = 124
            log.write(f"managed solver timed out after {timeout_sec} seconds\n")
        log.flush()
    return process_exit_code, log_tail(log_path)


def attempt_record(
    run_id: str,
    result: dict[str, Any],
    record: dict[str, Any],
    process_exit_code: int,
    success: bool,
) -> dict[str, Any]:
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    solvers_tried = metrics.get("solvers_tried", [])
    if not isinstance(solvers_tried, list):
        solvers_tried = []
    return {
        "attempt_id": run_id,
        "recorded_at": record.get("executed_at", datetime.now(timezone.utc).isoformat()),
        "success": success,
        "status": result.get("status"),
        "raw_status": result.get("raw_status"),
        "termination_condition": result.get("termination_condition"),
        "backend": result.get("backend"),
        "solvers_tried": solvers_tried,
        "objective_value": result.get("objective_value"),
        "best_bound": result.get("best_bound"),
        "has_incumbent": result.get("has_incumbent"),
        "process_exit_code": process_exit_code,
        "failure_class": record.get("failure_class"),
    }


def persist_attempt(
    state_path: Path,
    run_id: str,
    result: dict[str, Any],
    record: dict[str, Any],
    process_exit_code: int,
    success: bool,
    detail: str | None = None,
) -> None:
    message = None if success else (detail or str(result.get("raw_status") or "formal solve failed"))
    record_solver_attempt(
        state_path,
        attempt_record(run_id, result, record, process_exit_code, success),
        message,
    )


def emit_summary(
    ok: bool,
    case_id: str,
    result: dict[str, Any],
    process_exit_code: int,
    detail: str | None = None,
) -> None:
    payload = {
        "ok": ok,
        "case_id": case_id,
        "status": result.get("status"),
        "termination_condition": result.get("termination_condition"),
        "objective_value": result.get("objective_value"),
        "has_incumbent": result.get("has_incumbent"),
        "process_exit_code": process_exit_code,
    }
    if detail:
        payload["detail"] = detail
    print(json.dumps(payload, ensure_ascii=False))


def emit_busy_summary(case_id: str) -> None:
    print(
        json.dumps(
            {
                "ok": False,
                "case_id": case_id,
                "status": "busy",
                "retryable": True,
                "detail": "another local managed formal solve currently owns this case",
            },
            ensure_ascii=False,
        )
    )


def main() -> int:
    args = parse_args()
    try:
        case_id = validate_case_id(args.case_id)
        workspace = Path(args.workspace_root).resolve()
        internal_case = workspace / ".optagent" / "cases" / case_id
        state = load_json(internal_case / "state.json")
        entrypoint = workspace / "cases" / case_id / "code" / "run.py"
        if not entrypoint.is_file():
            raise FileNotFoundError(f"public solver entrypoint is missing: {entrypoint}")
    except Exception as error:
        print(json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"}, ensure_ascii=False))
        return 1

    solver_dir = internal_case / "solver"
    temporary_dir = solver_dir / "temp"
    wrapper_path = temporary_dir / "managed_solver_runner.py"
    solution_path = solver_dir / "solution.json"
    execution_path = solver_dir / "execution_record.json"
    log_path = solver_dir / "solver_log.txt"
    time_limit_sec = read_time_limit(state)
    grace_sec, execution_timeout_sec = timeout_values(time_limit_sec)
    run_id = uuid.uuid4().hex
    case_lock = acquire_case_solve_lock(temporary_dir / ".managed_solver.lock")
    if case_lock is None:
        emit_busy_summary(case_id)
        return BUSY_EXIT_CODE

    try:
        # Never mistake an earlier result for the current managed invocation.
        solution_path.unlink(missing_ok=True)
        execution_path.unlink(missing_ok=True)
        wrapper_path.parent.mkdir(parents=True, exist_ok=True)
        wrapper_path.write_text(
            managed_wrapper_source(
                case_id,
                time_limit_sec,
                grace_sec,
                execution_timeout_sec,
                run_id,
                entrypoint,
                solution_path,
                execution_path,
                workspace,
            ),
            encoding="utf-8",
        )

        command = [sys.executable, relative(wrapper_path, workspace)]
        process_exit_code, tail = run_managed_process(
            command,
            workspace,
            log_path,
            execution_timeout_sec,
        )

        try:
            result = load_json(solution_path)
            record = load_json(execution_path)
            if record.get("managed_run_id") != run_id:
                raise ValueError("managed runner did not produce a fresh execution record")
        except Exception as error:
            record_status = observed_record_commit_status(solution_path, execution_path, run_id)
            bounded_tail = bounded_output_tail(tail)
            detail = f"{type(error).__name__}: {error}"
            if bounded_tail:
                detail = f"{detail}; managed output tail: {bounded_tail[-2000:]}"
            result = failure_result("managed runner did not produce a valid result record", detail)
            atomic_write_json(solution_path, result)
            record = fallback_execution_record(
                workspace,
                entrypoint,
                result,
                time_limit_sec,
                grace_sec,
                execution_timeout_sec,
                run_id,
                process_exit_code,
                bounded_tail,
                record_status,
                fallback_failure_class(record_status, bounded_tail),
            )
            atomic_write_json(execution_path, record)
            persist_attempt(
                internal_case / "state.json",
                run_id,
                result,
                record,
                process_exit_code,
                False,
                detail,
            )
            emit_summary(False, case_id, result, process_exit_code, detail)
            return process_exit_code if process_exit_code != 0 else 1

        success = process_exit_code == 0 and result.get("status") in HANDLED_SOLVE_STATUSES
        persist_attempt(
            internal_case / "state.json",
            run_id,
            result,
            record,
            process_exit_code,
            success,
        )
        emit_summary(success, case_id, result, process_exit_code)
        return 0 if success else (process_exit_code if process_exit_code != 0 else 1)
    finally:
        case_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
