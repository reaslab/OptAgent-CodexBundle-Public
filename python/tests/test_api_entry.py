from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import optagent_core.api as api
from optagent_core.contracts import canonical_json_bytes


def test_isolated_script_entry_uses_package_imports_for_candidate_revise(core):
    case_id, _, _ = core["create_case"]()
    model_path = core["workspace"] / "candidate_model.py"
    model_path.write_text("# frozen candidate source\n", encoding="utf-8")
    request = {
        "contract_version": 1,
        "kind": "dynamic_tool",
        "context": core["context"](),
        "tool": "optagent.case",
        "arguments": {
            "op": "revise",
            "case_id": case_id,
            "expected_state_revision": 1,
            "kind": "candidate",
            "model_path": model_path.name,
            "local_module_paths": [],
            "data_id": None,
            "backend_id": "highs",
            "objective_plan": {"mode": "feasibility", "preference_note": "test"},
            "business_checker_path": None,
            "input_closure": "declared",
            "closure_note": "All inputs are declared.",
        },
    }
    script = Path(api.__file__).resolve()
    completed = subprocess.run(
        [sys.executable, "-I", str(script)],
        input=canonical_json_bytes(request),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=script.parents[2],
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stderr == b""
    assert completed.stdout.count(b"\n") == 1
    envelope = json.loads(completed.stdout)
    assert envelope["kind"] == "complete"
    assert envelope["reply"]["ok"] is True
    assert envelope["reply"]["tool"] == "optagent.case"
    assert envelope["reply"]["op"] == "revise"
    assert envelope["reply"]["case_id"] == case_id
    assert envelope["reply"]["result"]["state"]["candidate_revision"] == 1


@pytest.mark.parametrize(
    ("failure_stage", "exception_type"),
    [("operation", RuntimeError), ("store", OSError)],
)
def test_validated_operation_unexpected_error_keeps_bound_unknown_reply(
    core, monkeypatch, capsys, failure_stage, exception_type
):
    case_id, _, _ = core["create_case"]()

    def fail(*_args, **_kwargs):
        raise exception_type("private sentinel must not escape")

    if failure_stage == "operation":
        monkeypatch.setattr(api, "_case_operation", fail)
    else:
        monkeypatch.setattr(api, "_store", fail)

    result = api.dispatch_dynamic(
        "optagent.case",
        {"op": "get", "case_id": case_id},
        core["context"](),
    )
    captured = capsys.readouterr()
    reply = result["reply"]
    assert result["kind"] == "complete"
    assert reply["ok"] is False
    assert reply["tool"] == "optagent.case"
    assert reply["op"] == "get"
    assert reply["case_id"] == case_id
    assert reply["state_revision"] is None
    assert reply["error"]["code"] == "bridge_protocol_error"
    assert reply["error"]["effect"] == "unknown"
    assert "private sentinel" not in str(reply)
    assert "private sentinel" not in captured.err
    assert exception_type.__name__ in captured.err


def test_invalid_dynamic_schema_does_not_enter_unexpected_failure_boundary(core, monkeypatch):
    def unexpected_store(*_args, **_kwargs):
        raise AssertionError("store must not be opened for invalid request identity")

    monkeypatch.setattr(api, "_store", unexpected_store)
    result = api.dispatch_dynamic(
        "optagent.case",
        {"op": "get", "case_id": "not-a-uuid"},
        core["context"](),
    )
    reply = result["reply"]
    assert result["kind"] == "complete"
    assert reply["ok"] is False
    assert reply["tool"] == "optagent.case"
    assert reply["op"] == "get"
    assert reply["case_id"] == "not-a-uuid"
    assert reply["state_revision"] is None
    assert reply["error"]["code"] == "invalid_argument"
    assert reply["error"]["effect"] == "none"
