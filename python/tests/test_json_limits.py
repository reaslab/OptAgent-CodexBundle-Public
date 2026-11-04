from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from optagent_core.api import dispatch_dynamic
from optagent_core.contracts import canonical_json_bytes, strict_json_loads


def _nested_containers(count: int):
    value = "leaf"
    for index in range(count):
        value = {"object": value} if index % 2 == 0 else [value]
    return value


def _create_arguments(capture_id: str, target_bytes: int) -> dict:
    args = {
        "op": "create",
        "capture_id": capture_id,
        "title": '标题\n"\\',
        "task_kind": "optimize",
        "world_relation": "single",
        "authorization_note": "Explicitly authorized",
    }
    used = len(canonical_json_bytes(args))
    remaining = target_bytes - used
    assert remaining >= 0
    args["title"] += "é" * (remaining // 2) + "a" * (remaining % 2)
    assert len(canonical_json_bytes(args)) == target_bytes
    return args


def test_raw_json_depth_walks_object_values_and_counts_all_containers():
    exact_object = canonical_json_bytes({"root": _nested_containers(63)})
    assert strict_json_loads(exact_object)
    exact_array = canonical_json_bytes(_nested_containers(64))
    assert strict_json_loads(exact_array)

    over_object = canonical_json_bytes({"root": _nested_containers(64)})
    over_array = canonical_json_bytes(_nested_containers(65))
    for raw in (over_object, over_array):
        with pytest.raises(ValueError, match="JSON nesting too deep"):
            strict_json_loads(raw)

    deep_object = 0
    deep_array = 0
    for _ in range(80):
        deep_object = {"nested": deep_object}
        deep_array = [deep_array]
    for raw in (
        canonical_json_bytes({"root": deep_object}),
        canonical_json_bytes({"root": deep_array}),
        canonical_json_bytes(deep_array),
    ):
        with pytest.raises(ValueError, match="JSON nesting too deep"):
            strict_json_loads(raw)


def test_raw_json_duplicate_and_nonfinite_guards_remain_strict():
    for raw in (
        b'{"outer":{"key":1,"key":2}}',
        b'{"value":NaN}',
        b'{"value":1e999}',
    ):
        with pytest.raises(ValueError):
            strict_json_loads(raw)


def test_dynamic_arguments_use_canonical_utf8_byte_budget_before_store(core):
    exact_transfer = core["capture"](b"A real source for the admitted create.", seq=1)
    exact_args = _create_arguments(exact_transfer["capture_id"], 65_536)
    accepted = dispatch_dynamic(
        "optagent.case",
        exact_args,
        core["context"](captures=[exact_transfer]),
    )
    assert accepted["kind"] == "complete"
    assert accepted["reply"]["ok"], accepted["reply"]
    assert accepted["reply"]["result"]["state"]["title"] == exact_args["title"]

    over_transfer = core["capture"](b"This transfer must not be adopted.", seq=2)
    over_args = _create_arguments(over_transfer["capture_id"], 65_537)
    rejected_root = core["workspace"] / "rejected-oversized-tool-args"
    context = core["context"](captures=[over_transfer])
    context["workspace_absolute"] = str(rejected_root)
    rejected = dispatch_dynamic("optagent.case", over_args, context)
    assert rejected["kind"] == "complete"
    assert not rejected["reply"]["ok"]
    assert rejected["reply"]["error"]["code"] == "invalid_argument"
    assert "65536 UTF-8 bytes" in rejected["reply"]["error"]["message"]
    assert not rejected_root.exists()


def test_real_stdin_bridge_counts_argument_containers_without_envelope_depth(core):
    root = Path(__file__).resolve().parents[2]
    api = root / "python" / "optagent_core" / "api.py"

    def call_at_depth(nested_count: int):
        workspace = core["workspace"] / f"depth-{nested_count}-workspace"
        request = {
            "contract_version": 1,
            "kind": "dynamic_tool",
            "context": {
                **core["context"](),
                "workspace_absolute": str(workspace),
            },
            "tool": "optagent.case",
            "arguments": {"op": "list", "extra": _nested_containers(nested_count)},
        }
        completed = subprocess.run(
            [sys.executable, "-I", str(api)],
            input=canonical_json_bytes(request),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=root,
            check=False,
        )
        assert completed.returncode == 0
        assert completed.stderr == b""
        assert completed.stdout.count(b"\n") == 1
        return json.loads(completed.stdout)["reply"], workspace

    at_limit, workspace32 = call_at_depth(31)  # root object plus 31 nested containers
    assert not at_limit["ok"]
    assert at_limit["error"]["code"] == "invalid_argument"
    assert at_limit["error"]["message"] == "unknown field"
    assert at_limit["error"]["pointer"] == "/extra"
    assert not workspace32.exists()

    over_limit, workspace33 = call_at_depth(32)
    assert not over_limit["ok"]
    assert over_limit["error"]["code"] == "invalid_argument"
    assert "32-container depth" in over_limit["error"]["message"]
    assert not workspace33.exists()


def test_real_capture_context_is_not_charged_to_tool_argument_budget(core):
    root = Path(__file__).resolve().parents[2]
    api = root / "python" / "optagent_core" / "api.py"
    transfer = core["capture"](b"x" * 70_000, seq=1)
    request = {
        "contract_version": 1,
        "kind": "dynamic_tool",
        "context": core["context"](captures=[transfer]),
        "tool": "optagent.case",
        "arguments": {"op": "inputs", "limit": 10},
    }
    request_bytes = canonical_json_bytes(request)
    assert len(request_bytes) > 65_536
    assert len(request_bytes) < 8 * 1024 * 1024
    completed = subprocess.run(
        [sys.executable, "-I", str(api)],
        input=request_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=root,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stderr == b""
    reply = json.loads(completed.stdout)["reply"]
    assert reply["ok"], reply
    assert reply["result"]["captures"][0]["total_bytes"] == 70_000
    assert not (core["workspace"] / ".reaslab/optagent").exists()


def test_real_stdin_request_keeps_separate_eight_mib_budget(core):
    root = Path(__file__).resolve().parents[2]
    api = root / "python" / "optagent_core" / "api.py"
    request = {
        "contract_version": 1,
        "kind": "dynamic_tool",
        "context": core["context"](),
        "tool": "optagent.case",
        "arguments": {"op": "inputs", "limit": 10},
    }
    prefix = canonical_json_bytes(request)
    assert len(prefix) < 8 * 1024 * 1024
    exact_request = prefix + b" " * (8 * 1024 * 1024 - len(prefix))
    exact = subprocess.run(
        [sys.executable, "-I", str(api)],
        input=exact_request,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=root,
        check=False,
    )
    assert exact.returncode == 0
    assert exact.stderr == b""
    exact_reply = json.loads(exact.stdout)["reply"]
    assert exact_reply["ok"], exact_reply

    oversized = subprocess.run(
        [sys.executable, "-I", str(api)],
        input=b" " * (8 * 1024 * 1024 + 1),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=root,
        check=False,
    )
    assert oversized.returncode == 0
    assert b"core request rejected: invalid_argument" in oversized.stderr
    rejected_reply = json.loads(oversized.stdout)["reply"]
    assert not rejected_reply["ok"]
    assert rejected_reply["error"]["message"] == "bridge request is too large"
    assert not (core["workspace"] / ".reaslab/optagent").exists()
