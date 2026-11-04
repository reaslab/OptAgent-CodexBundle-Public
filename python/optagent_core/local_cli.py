"""Small workspace-local command line for the file-backed OptAgent Core."""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

from .api import (
    _control_capture_arrived,
    _full_state_summary,
    _load_barrier,
    _object_summary,
    _require_root,
    dispatch_envelope,
)
from .contracts import (
    CONTRACT_VERSION,
    ContractError,
    canonical_json_bytes,
    load_tool_schemas,
    public_success,
    sha256_bytes,
    strict_json_loads,
    validate_schema,
    windows_long_path,
)
from .sources import capture_file_source, initial_case_state, new_id, utc_now
from .store import CaseStore


def _workspace() -> Path:
    return Path.cwd().absolute()


def _session_id(workspace: Path, supplied: str | None) -> str:
    if supplied:
        return supplied
    path = workspace / ".reaslab" / "optagent" / "local-session.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and isinstance(value.get("session_id"), str) and value["session_id"]:
            return value["session_id"]
    except FileNotFoundError:
        pass
    except (OSError, ValueError, UnicodeError):
        raise ContractError("output_invalid", "local session record is unreadable")
    value = f"local-{uuid.uuid4()}"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(canonical_json_bytes({"session_id": value}) + b"\n")
    os.replace(temporary, path)
    return value


def _read_json_argument(args: argparse.Namespace, workspace: Path) -> dict[str, Any]:
    if args.json is not None and args.json_file is not None:
        raise ContractError("invalid_argument", "choose either --json or --json-file")
    if args.json is not None:
        raw = args.json
    elif args.json_file is not None:
        path = Path(args.json_file)
        if not path.is_absolute():
            path = workspace / path
        raw = windows_long_path(path).read_bytes()
    else:
        raw = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
    value = strict_json_loads(raw, max_bytes=8 * 1024 * 1024)
    if not isinstance(value, dict):
        raise ContractError("invalid_argument", "tool arguments must be one JSON object")
    return value


def _public_operation_specs() -> list[dict[str, Any]]:
    """Derive local public aliases from the checked-in tool schema branches."""
    result = []
    for group_name, entry in load_tool_schemas().items():
        group_schema = entry["input_schema"]
        branches = group_schema.get("oneOf") if isinstance(group_schema, dict) else None
        if not isinstance(branches, list):
            continue
        for branch in branches:
            properties = branch.get("properties", {})
            if not isinstance(properties, dict):
                continue
            op_value = properties.get("op", {}).get("const")
            if not isinstance(op_value, str):
                continue
            discriminator = None
            kind_value = None
            for key in ("kind", "record_kind"):
                value = properties.get(key, {}).get("const")
                if isinstance(value, str):
                    discriminator, kind_value = key, value
                    break
            public_name = f"{group_name.removeprefix('optagent.')}_{op_value}"
            if kind_value is not None:
                public_name += f"_{kind_value}"
            if public_name == "deliver_audit":
                continue
            result.append({
                "name": public_name,
                "group_name": group_name,
                "group_schema": group_schema,
                "branch": branch,
                "op_value": op_value,
                "discriminator": discriminator,
                "kind_value": kind_value,
            })
    return result


def _public_operation_names() -> list[str]:
    return [spec["name"] for spec in _public_operation_specs()]


def _unknown_operation(operation: str) -> ContractError:
    return ContractError(
        "unknown_operation",
        f"unknown public operation: {operation}; run 'schema' without an operation to list available names",
        details={"available_operations": _public_operation_names()},
    )


def _operation_binding(operation: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Map one public alias to its Core group using the schema-derived index."""
    name = operation.removeprefix("optagent.")
    for spec in _public_operation_specs():
        if name != spec["name"]:
            continue
        full = dict(arguments)
        full["op"] = spec["op_value"]
        if spec["discriminator"] is not None:
            full[spec["discriminator"]] = spec["kind_value"]
        if spec["name"] == "case_create":
            full.setdefault("capture_id", str(uuid.uuid4()))
        validate_schema(full, spec["group_schema"])
        return spec["group_name"], full
    raise _unknown_operation(operation)


def _operation_schema(operation: str | None) -> dict[str, Any]:
    if operation is None:
        return {"operations": _public_operation_names()}
    name = operation.removeprefix("optagent.")
    for spec in _public_operation_specs():
        if name != spec["name"]:
            continue
        schema = json.loads(json.dumps(spec["branch"]))
        schema["properties"].pop("op", None)
        schema["required"] = [key for key in schema.get("required", []) if key != "op"]
        discriminator = spec["discriminator"]
        if discriminator:
            schema["properties"].pop(discriminator, None)
            schema["required"] = [key for key in schema.get("required", []) if key != discriminator]
        local_options = None
        if spec["name"] == "case_create":
            schema["properties"].pop("capture_id", None)
            schema["required"] = [key for key in schema["required"] if key != "capture_id"]
            local_options = {
                "--capture-file": {
                    "required": True,
                    "type": "workspace-relative UTF-8 text file",
                    "provenance": "local workspace file; no host Capture or session authenticity is asserted",
                }
            }
        elif spec["name"] == "case_question":
            local_options = {
                "--capture-file": {
                    "required": True,
                    "type": "workspace-relative UTF-8 text file",
                    "provenance": "explicit local workspace import; not an authenticated host capture",
                }
            }
        elif spec["name"] == "case_answer":
            schema["required"] = [key for key in schema["required"] if key != "capture_id"]
            local_options = {
                "--capture-file": {
                    "required": True,
                    "type": "workspace-relative UTF-8 text file",
                    "sets_argument": "capture_id",
                    "provenance": "explicit local workspace import; not an authenticated host capture",
                }
            }
        result = {"operation": spec["name"], "schema": schema}
        if local_options is not None:
            result["local_cli_options"] = local_options
        return result
    raise _unknown_operation(operation)


def _create_local_case(arguments: dict[str, Any], file_value: str, workspace: Path, stamp: dict[str, str]) -> dict[str, Any]:
    _require_root(stamp)
    file_path = Path(file_value)
    if not file_path.is_absolute():
        file_path = workspace / file_path
    try:
        relative = file_path.absolute().relative_to(workspace).as_posix()
    except ValueError as exc:
        raise ContractError("path_denied", "case_create source file must be inside the current workspace") from exc
    store = CaseStore(workspace, local_mode=True)
    stable_read = store.read_user_file(relative)
    try:
        stable_read[0].decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ContractError("invalid_argument", "local case source file must contain UTF-8 text") from exc
    case_id = new_id()
    source, source_ref = capture_file_source(
        store,
        case_id=case_id,
        path=relative,
        purpose="requirement",
        locations=[],
        stamp=stamp,
        stable_read=stable_read,
    )
    state = initial_case_state(
        case_id=case_id,
        title=arguments["title"],
        task_kind=arguments["task_kind"],
        world_relation=arguments["world_relation"],
        authorization_note=arguments["authorization_note"],
        owner_session_id=stamp["session_id"],
        source_id=source["source_id"],
        stamp=stamp,
    )
    state = store.create_case_state(state, existing_source_id=source["source_id"])
    return {
        "kind": "complete",
        "reply": public_success(
            "optagent.case",
            "create",
            case_id=case_id,
            state_revision=state["state_revision"],
            result={
                "state": _full_state_summary(store, state),
                "source": _object_summary(source["source_id"], source_ref, "captured", state["source_revision"], None),
                "local_import": {"path": relative, "provenance": "workspace_file"},
            },
        ),
    }


def _import_local_capture(
    *,
    file_value: str,
    workspace: Path,
    stamp: dict[str, str],
    case_id: str,
    expected_state_revision: int,
    capture_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    file_path = Path(file_value)
    if not file_path.is_absolute():
        file_path = workspace / file_path
    try:
        relative = file_path.absolute().relative_to(workspace).as_posix()
    except ValueError as exc:
        raise ContractError("path_denied", "local capture file must be inside the current workspace") from exc
    store = CaseStore(workspace, local_mode=True)
    state = store.read_case(case_id)
    if state["state_revision"] != expected_state_revision:
        raise ContractError("stale_revision", "case revision changed before local input import", details={"actual": state["state_revision"]})
    stable_read = store.read_user_file(relative)
    raw = stable_read[0]
    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ContractError("invalid_argument", "local capture file must contain UTF-8 text") from exc
    if not raw.strip():
        raise ContractError("invalid_argument", "local capture file must not be empty")

    session_id = stamp["session_id"]
    session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    local_lock_digest = hashlib.sha256(f"local-import:{session_id}".encode("utf-8")).hexdigest()
    barrier_path = f".reaslab/optagent/input-fences/{session_digest}.json"
    with store.session_lock(local_lock_digest):
        barrier = _load_barrier(store, barrier_path, session_id)
        arrival_seq = barrier["latest_observed_seq"] + 1
        arrival_epoch = barrier["arrival_epoch"]
        if arrival_seq == 1 and arrival_epoch == "00000000-0000-4000-8000-000000000000":
            arrival_epoch = str(uuid.uuid4())
        selected_capture_id = capture_id or str(uuid.uuid4())
        transfer = {
            "capture_id": selected_capture_id,
            "arrival_epoch": arrival_epoch,
            "arrival_seq": arrival_seq,
            "session_id": session_id,
            "channel": "prompt",
            "admission": "received_prompt",
            "utf8_bytes_base64": base64.b64encode(raw).decode("ascii"),
            "sha256": sha256_bytes(raw),
        }
        metadata = {
            "schema_version": 1,
            "capture_id": selected_capture_id,
            "session_id": session_id,
            "path": relative,
            "sha256": transfer["sha256"],
            "byte_len": len(raw),
            "arrival_epoch": arrival_epoch,
            "arrival_seq": arrival_seq,
            "channel": "prompt",
            "admission": "received_prompt",
            "provenance": "workspace_file",
            "host_capture_authenticated": False,
            "imported_at": utc_now(),
        }
        store.write_record(f".reaslab/optagent/local-imports/{selected_capture_id}.json", metadata)
        affected = _control_capture_arrived(
            store,
            {
                "event_id": str(uuid.uuid4()),
                "kind": "capture_arrived",
                "capture": transfer,
                "bindings": [{"case_id": case_id, "run_id": None, "pending_start": False}],
            },
            {"session_id": session_id},
        )
    if case_id not in affected:
        raise ContractError("capture_unavailable", "local input was not materialized for this Case")
    current = store.read_case(case_id)
    context_import = {
        "capture_id": selected_capture_id,
        "workspace_path": relative,
        "sha256": transfer["sha256"],
        "byte_len": len(raw),
        "provenance": "workspace_file",
        "host_capture_authenticated": False,
    }
    return transfer, {**metadata, "state_revision": current["state_revision"]}, context_import


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="在当前目录运行 OptAgent Core 本地命令。")
    parser.add_argument("--session-id", help="显式使用已有 Case 的 owner_session_id；默认保存在本工作区。")
    parser.add_argument("--role", choices=("root", "opt-verifier", "opt-worker"), default="root")
    sub = parser.add_subparsers(dest="command", required=True)
    schema = sub.add_parser("schema", help="输出 operation 输入 JSON Schema；不带名称时列出公开 operation。")
    schema.add_argument("operation", nargs="?", help="可选公开 operation 名称")
    call = sub.add_parser("call", help="按工具 schema 调用一个 Core 工具。")
    call.add_argument("operation", help="例如 case_create、case_get、data_capture、run_prepare")
    call.add_argument("--json", help="工具参数 JSON object")
    call.add_argument("--json-file", help="从 JSON 文件读取工具参数，路径相对当前工作区")
    call.add_argument("--capture-file", help="case_create 来源文件，或 case_question/case_answer 的本地 UTF-8 输入文件")
    return parser


def _windows_job(kernel32: Any) -> int:
    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel32.CloseHandle(handle)
        raise error
    return int(handle)


def _spawn_owned_process(plan: dict[str, Any], workspace: Path) -> tuple[subprocess.Popen[bytes], int | None, Any | None]:
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        [plan["executable"], *plan["arguments"]],
        cwd=str(workspace),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=flags,
        start_new_session=os.name != "nt",
    )
    if process.stdin is None:
        process.kill()
        process.wait()
        raise OSError("owned worker stdin handshake is unavailable")

    job_handle = None
    kernel32 = None
    if os.name == "nt":
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
            kernel32.AssignProcessToJobObject.restype = ctypes.c_int
            job_handle = _windows_job(kernel32)
            if not kernel32.AssignProcessToJobObject(job_handle, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
        except (OSError, AttributeError) as exc:
            process.stdin.close()
            process.kill()
            process.wait()
            if job_handle is not None:
                kernel32.CloseHandle(job_handle)
            raise OSError("Windows Job Object containment is unavailable; local worker was not started") from exc

    try:
        process.stdin.write(b"\x01")
        process.stdin.flush()
    except OSError:
        process.kill()
        process.wait()
        if job_handle is not None and kernel32 is not None:
            kernel32.CloseHandle(job_handle)
        raise
    finally:
        process.stdin.close()
    return process, job_handle, kernel32


def _local_receipt(ready: dict[str, Any], workspace: Path, request_digest: str) -> dict[str, Any]:
    plan = ready["local_process"]
    started_at = utc_now()
    process: subprocess.Popen[bytes] | None = None
    stdout = b""
    stderr = b""
    timed_out = False
    error_code = None
    stop_reason: str | None = None
    stop_confirmed: bool | None = None

    store = CaseStore(workspace, create=False, local_mode=True)

    def cancellation_requested() -> bool:
        try:
            state = store.read_case(ready["case_id"])
            run, _ = store.read_family_record(ready["case_id"], "runs", ready["run_id"], state=state)
        except ContractError:
            return False
        return run.get("cancel", {}).get("requested") is True

    job_handle: int | None = None
    kernel32: Any | None = None

    def stop_owned_process(reason: str) -> tuple[bytes, bytes, bool]:
        assert process is not None
        confirmed = False
        if process.poll() is not None and (os.name != "nt" or job_handle is None):
            out, err = process.communicate()
            return out, err, False
        if os.name == "nt":
            if job_handle is not None and kernel32 is not None:
                kernel32.TerminateJobObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
                kernel32.TerminateJobObject.restype = ctypes.c_int
                confirmed = bool(kernel32.TerminateJobObject(job_handle, 1))
            if process.poll() is None and not confirmed:
                process.kill()
            try:
                out, err = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                out, err = process.communicate()
            return out, err, confirmed and process.returncode is not None

        try:
            os.killpg(process.pid, signal.SIGTERM)
            confirmed = True
        except ProcessLookupError:
            confirmed = process.poll() is not None
        except OSError:
            confirmed = False
        try:
            out, err = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                confirmed = True
            except OSError:
                confirmed = False
            out, err = process.communicate()
        return out, err, confirmed and process.returncode is not None

    try:
        if cancellation_requested():
            error_code = "cancelled"
        else:
            process, job_handle, kernel32 = _spawn_owned_process(plan, workspace)
            deadline = time.monotonic() + float(plan["timeout_seconds"])
            while True:
                remaining = deadline - time.monotonic()
                try:
                    stdout, stderr = process.communicate(timeout=max(0.05, min(0.25, remaining)))
                    break
                except subprocess.TimeoutExpired:
                    if cancellation_requested():
                        stop_reason = "cancel_requested"
                        stdout, stderr, stop_confirmed = stop_owned_process(stop_reason)
                        error_code = "cancelled" if stop_confirmed else "termination_unconfirmed"
                        break
                    if remaining <= 0:
                        timed_out = True
                        stop_reason = "timeout"
                        stdout, stderr, stop_confirmed = stop_owned_process(stop_reason)
                        error_code = "timeout" if stop_confirmed else "termination_unconfirmed"
                        break
    except KeyboardInterrupt:
        if process is not None:
            stop_reason = "keyboard_interrupt"
            stdout, stderr, stop_confirmed = stop_owned_process(stop_reason)
            error_code = "interrupted" if stop_confirmed else "termination_unconfirmed"
        else:
            error_code = "interrupted"
    except OSError as exc:
        error_code = "spawn_failed"
        observation = "not_started"
        stderr = str(exc).encode("utf-8", errors="replace")
    else:
        observation = "spawned"
    finally:
        if job_handle is not None and kernel32 is not None:
            kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
            kernel32.CloseHandle.restype = ctypes.c_int
            kernel32.CloseHandle(job_handle)
    if process is not None:
        observation = "spawned"
        if not timed_out and process.returncode != 0:
            if error_code is None:
                error_code = "worker_failed"
    else:
        observation = "not_started"
    completed_at = utc_now()
    return {
        "schema_version": 2,
        "request_digest": request_digest,
        "observation": observation,
        "pid": process.pid if process is not None else None,
        "returncode": process.returncode if process is not None else None,
        "timed_out": timed_out,
        "stop_reason": stop_reason,
        "stop_confirmed": stop_confirmed,
        "started_at": started_at if process is not None else None,
        "completed_at": completed_at if process is not None else None,
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "error_code": error_code,
    }


def _invoke_local_run_start(envelope: dict[str, Any], context: dict[str, Any], case_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    plan_reply = dispatch_envelope(envelope)
    if plan_reply.get("kind") != "start_plan":
        return plan_reply
    plan = plan_reply["plan"]
    phase = {
        "kind": "start_recheck",
        "case_id": case_id,
        "run_id": plan["run_id"],
        "prepared_id": plan["prepared_id"],
        "dispatch_nonce": plan["dispatch_nonce"],
        "request_digest": plan["request_digest"],
    }
    bridge = {
        "contract_version": CONTRACT_VERSION,
        "kind": "run_bridge",
        "context": context,
        "phase": phase,
    }
    ready_reply = dispatch_envelope(bridge)
    if ready_reply.get("kind") != "start_ready":
        return ready_reply
    receipt_plan = {**ready_reply["plan"], "case_id": arguments["case_id"]}
    receipt = _local_receipt(receipt_plan, Path(context["workspace_absolute"]), plan["request_digest"])
    bridge["phase"] = {**phase, "kind": "start_receipt", "receipt": receipt}
    return dispatch_envelope(bridge)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        workspace = _workspace()
        if args.command == "schema":
            return _write_response({"kind": "complete", "reply": {"ok": True, "result": _operation_schema(args.operation)}})
        session_id = _session_id(workspace, args.session_id)
        stamp = {
            "session_id": session_id,
            "role": args.role,
            "turn_id": str(uuid.uuid4()),
            "call_id": str(uuid.uuid4()),
        }
        arguments = _read_json_argument(args, workspace)
        operation_name = args.operation.removeprefix("optagent.")
        if operation_name in {"case_question", "case_answer"} and not args.capture_file:
            raise ContractError("invalid_argument", f"{args.operation} requires --capture-file for explicit local input import")
        if args.capture_file and operation_name == "case_answer":
            arguments.setdefault("capture_id", str(uuid.uuid4()))
        group, full_arguments = _operation_binding(args.operation, arguments)
        if operation_name == "case_create":
            if not args.capture_file:
                raise ContractError("invalid_argument", "case_create requires --capture-file in the local bundle")
            return _write_response(_create_local_case(arguments, args.capture_file, workspace, stamp))
        if args.capture_file and operation_name not in {"case_question", "case_answer"}:
            raise ContractError("invalid_argument", "--capture-file is supported only for case_create, case_question, and case_answer")
        captures = []
        local_imports = []
        local_import_record = None
        if operation_name in {"case_question", "case_answer"}:
            if args.role != "root":
                raise ContractError("permission_denied", "local question and answer imports require the root collaboration role")
            transfer, local_import_record, context_import = _import_local_capture(
                file_value=args.capture_file,
                workspace=workspace,
                stamp=stamp,
                case_id=full_arguments["case_id"],
                expected_state_revision=full_arguments["expected_state_revision"],
                capture_id=full_arguments.get("capture_id"),
            )
            captures = [transfer]
            local_imports = [context_import]
            if operation_name == "case_answer":
                full_arguments["capture_id"] = transfer["capture_id"]
            full_arguments["expected_state_revision"] = local_import_record["state_revision"]
        envelope = {
            "contract_version": 1,
            "kind": "dynamic_tool",
            "tool": group,
            "arguments": full_arguments,
            "context": {
                "stamp": stamp,
                "workspace_absolute": str(workspace),
                "cancellation_observed": False,
                "captures": captures,
                "local_imports": local_imports,
                "local_mode": True,
            },
        }
        response = (
            _invoke_local_run_start(envelope, envelope["context"], arguments["case_id"], full_arguments)
            if group == "optagent.run" and full_arguments.get("op") == "start"
            else dispatch_envelope(envelope)
        )
        if local_import_record is not None:
            response["local_import"] = local_import_record
        return _write_response(response)
    except ContractError as exc:
        error = {"code": exc.code, "message": exc.message}
        if exc.pointer is not None:
            error["pointer"] = exc.pointer
        if exc.details:
            error["details"] = exc.details
        payload = {"kind": "complete", "reply": {"ok": False, "error": error}}
        sys.stdout.buffer.write(canonical_json_bytes(payload) + b"\n")
        return 2
    except (OSError, ValueError, UnicodeError) as exc:
        payload = {"kind": "complete", "reply": {"ok": False, "error": {"code": "local_input_error", "message": str(exc)}}}
        sys.stdout.buffer.write(canonical_json_bytes(payload) + b"\n")
        return 2


def _write_response(response: dict[str, Any]) -> int:
    sys.stdout.buffer.write(canonical_json_bytes(response) + b"\n")
    reply = response.get("reply")
    return 0 if isinstance(reply, dict) and reply.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
