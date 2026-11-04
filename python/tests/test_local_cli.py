from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from optagent_core import local_cli
from optagent_core import api
from optagent_core.contracts import windows_long_path


def _call_local_cli(tmp_path: Path, capsysbinary, operation: str, arguments: dict, *, capture_file: str | None = None):
    path = tmp_path / f"{operation}-{uuid.uuid4()}.json"
    path.write_text(json.dumps(arguments), encoding="utf-8")
    argv = ["--session-id", "local-cli-regression", "--role", "root", "call", operation, "--json-file", str(path)]
    if capture_file is not None:
        argv.extend(["--capture-file", capture_file])
    return_code = local_cli.main(argv)
    output = capsysbinary.readouterr().out
    return return_code, json.loads(output)


def test_local_cli_question_and_answer_import_workspace_files_with_provenance(tmp_path, capsysbinary, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "requirement.md").write_text("The capacity is not specified.\n", encoding="utf-8")
    question_bytes = "Clarification about the capacity.\n".encode("utf-8")
    answer_bytes = "Use a capacity of 9.\n".encode("utf-8")
    (tmp_path / "question.md").write_bytes(question_bytes)
    (tmp_path / "answer.md").write_bytes(answer_bytes)

    code, created = _call_local_cli(
        tmp_path,
        capsysbinary,
        "case_create",
        {"title": "Local Q&A CLI test", "task_kind": "optimize", "world_relation": "single", "authorization_note": "Local regression test."},
        capture_file="requirement.md",
    )
    assert code == 0, created
    case_id = created["reply"]["result"]["state"]["case_id"]
    revision = created["reply"]["state_revision"]

    code, asked = _call_local_cli(
        tmp_path,
        capsysbinary,
        "case_question",
        {
            "case_id": case_id,
            "expected_state_revision": revision,
            "text": "What capacity should I use?",
            "missing_fact": "the capacity parameter",
            "blocks": ["model"],
            "independent_work": [],
        },
        capture_file="question.md",
    )
    assert code == 0
    assert asked["local_import"]["provenance"] == "workspace_file"
    assert asked["local_import"]["host_capture_authenticated"] is False
    question = asked["reply"]["result"]["question"]

    code, answered = _call_local_cli(
        tmp_path,
        capsysbinary,
        "case_answer",
        {
            "case_id": case_id,
            "expected_state_revision": asked["reply"]["state_revision"],
            "request_id": question["request_id"],
            "expected_question_version": question["question_version"],
            "interpretation": "Set capacity to 9.",
        },
        capture_file="answer.md",
    )
    assert code == 0
    assert answered["reply"]["result"]["question"]["status"] == "answered"
    assert answered["reply"]["result"]["state"]["pending_question_count"] == 0
    assert answered["local_import"]["host_capture_authenticated"] is False
    source_ref = answered["reply"]["result"]["source"]["record_ref"]
    source = json.loads((tmp_path / source_ref["path"]).read_text(encoding="utf-8"))
    assert source["origin"]["workspace_path"] == "answer.md"
    assert source["original_sha256"] == hashlib.sha256(answer_bytes).hexdigest()


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH argument-file regression")
def test_local_cli_reads_json_argument_file_beyond_windows_max_path(tmp_path):
    target_length = 261
    long_dir = tmp_path
    component_length = target_length - len(str(long_dir / "arguments.json")) - 1
    assert 0 < component_length <= 255
    long_dir = long_dir / ("x" * component_length)
    windows_long_path(long_dir).mkdir()
    arguments_path = long_dir / "arguments.json"
    windows_long_path(arguments_path).write_text('{"op":"list"}', encoding="utf-8")
    assert len(str(arguments_path)) == target_length
    env = os.environ.copy()
    package_root = str(Path(local_cli.__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (package_root, env.get("PYTHONPATH")) if value
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "optagent_core.local_cli",
            "--session-id",
            "local-long-json-file-regression",
            "--role",
            "root",
            "call",
            "case_list",
            "--json-file",
            str(arguments_path),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    response = json.loads(completed.stdout)
    assert response["kind"] == "complete"
    assert response["reply"]["ok"] is True


@pytest.mark.parametrize("stop_confirmed", [False, True])
def test_case_next_actions_state_whether_local_process_stop_is_confirmed(stop_confirmed):
    case_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    receipt_ref = {"path": "receipt.json", "sha256": "a" * 64, "byte_len": 1, "media_type": "application/json"}
    run = {
        "dispatch_state": "unknown",
        "local_process": {"receipt": receipt_ref},
        "cancel": {"stop_ack": None},
    }

    class Store:
        def read_family_record(self, requested_case_id, family, requested_run_id, **_kwargs):
            assert (requested_case_id, family, requested_run_id) == (case_id, "runs", run_id)
            return run, receipt_ref

        def read_ref(self, _ref):
            return json.dumps({"stop_confirmed": stop_confirmed}).encode("utf-8")

    actions = api._next_actions(
        Store(),
        {"case_id": case_id, "candidate_id": str(uuid.uuid4()), "pending_inputs": [], "question_ids": [], "run_ids": [run_id]},
        "none",
    )
    run_action = next(action for action in actions if "latest Run" in action)
    if stop_confirmed:
        assert "process tree is confirmed stopped" in run_action
        assert "does not prove" not in run_action
    else:
        assert "does not prove the earlier execution stopped" in run_action


@pytest.mark.parametrize("stop_mode", ["timeout", "cancel"])
def test_local_receipt_stops_owned_worker_and_child(stop_mode, tmp_path, monkeypatch):
    cancel_requested = threading.Event()
    pid_file = tmp_path / "child.pid"

    class Store:
        def __init__(self, *_args, **_kwargs):
            pass

        def read_case(self, _case_id):
            return {"state_revision": 1}

        def read_family_record(self, _case_id, _family, _record_id, *, state):
            return {"cancel": {"requested": cancel_requested.is_set()}}, {}

    monkeypatch.setattr(local_cli, "CaseStore", Store)
    child = "child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(30)']); pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)"
    code = "import pathlib,subprocess,sys,time; sys.stdin.buffer.read(1); " + child
    ready = {
        "case_id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "local_process": {
            "executable": sys.executable,
            "arguments": ["-I", "-c", code, str(pid_file)],
            "timeout_seconds": 3.0 if stop_mode == "timeout" else 30.0,
        },
    }

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(local_cli._local_receipt, ready, tmp_path, "a" * 64)
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            if future.done():
                break
            time.sleep(0.05)
        assert pid_file.exists(), "owned worker did not launch the child process"
        child_pid = int(pid_file.read_text(encoding="ascii"))
        if stop_mode == "cancel":
            cancel_requested.set()
        receipt = future.result(timeout=10)

    assert receipt["observation"] == "spawned"
    assert receipt["stop_reason"] == ("timeout" if stop_mode == "timeout" else "cancel_requested")
    assert receipt["stop_confirmed"] is True
    assert receipt["error_code"] == ("timeout" if stop_mode == "timeout" else "cancelled")
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000, False, child_pid)
        if handle:
            kernel32.CloseHandle(handle)
        assert not handle, "the child process remains after Job Object termination"
