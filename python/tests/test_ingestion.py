from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from optagent_core.contracts import canonical_json_bytes, public_success


def test_real_stdin_create_then_read_only_get_keeps_state_bytes(core):
    root = Path(__file__).absolute().parents[2]
    api = root / "python" / "optagent_core" / "api.py"
    transfer = core["capture"](b"Minimize cost; report the limiting condition.", seq=1)
    context = core["context"](captures=[transfer])
    create = {
        "contract_version": 1,
        "kind": "dynamic_tool",
        "context": context,
        "tool": "optagent.case",
        "arguments": {
            "op": "create",
            "capture_id": transfer["capture_id"],
            "title": "stdin protocol test",
            "task_kind": "optimize",
            "world_relation": "single",
            "authorization_note": "Explicitly authorized",
        },
    }
    first = subprocess.run(
        [sys.executable, str(api)],
        input=canonical_json_bytes(create),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=root,
        check=False,
    )
    assert first.returncode == 0
    assert first.stderr == b""
    assert first.stdout.count(b"\n") == 1
    created = json.loads(first.stdout)
    assert created["kind"] == "complete"
    assert created["reply"]["ok"]
    case_id = created["reply"]["case_id"]

    store = core["store"]()
    state_ref = store.ref_for_path(f".reaslab/optagent/cases/{case_id}/state.json")
    before = store.read_ref(state_ref)
    get = {
        "contract_version": 1,
        "kind": "dynamic_tool",
        "context": {**context, "captures": []},
        "tool": "optagent.case",
        "arguments": {"op": "get", "case_id": case_id},
    }
    second = subprocess.run(
        [sys.executable, str(api)],
        input=canonical_json_bytes(get),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=root,
        check=False,
    )
    assert second.returncode == 0
    assert second.stderr == b""
    assert second.stdout.count(b"\n") == 1
    fetched = json.loads(second.stdout)
    assert fetched["reply"]["ok"]
    assert fetched["reply"]["result"]["state"]["case_id"] == case_id
    assert store.read_ref(state_ref) == before

    empty = core["capture"](b"", seq=2)
    empty_context = {**context, "captures": [empty]}
    list_empty = {
        "contract_version": 1,
        "kind": "dynamic_tool",
        "context": empty_context,
        "tool": "optagent.case",
        "arguments": {"op": "inputs", "limit": 10},
    }
    third = subprocess.run(
        [sys.executable, str(api)],
        input=canonical_json_bytes(list_empty),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=root,
        check=False,
    )
    assert third.returncode == 0
    assert third.stderr == b""
    empty_reply = json.loads(third.stdout)["reply"]
    assert empty_reply["ok"]
    assert empty_reply["result"]["captures"][0]["total_bytes"] == 0
    assert empty_reply["result"]["captures"][0]["preview"] == ""
    assert store.read_ref(state_ref) == before

    verifier_context = {**context, "stamp": {**context["stamp"], "role": "opt-verifier"}}
    unauthorized = {**create, "context": verifier_context}
    fourth = subprocess.run(
        [sys.executable, str(api)],
        input=canonical_json_bytes(unauthorized),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=root,
        check=False,
    )
    assert fourth.returncode == 0
    assert fourth.stderr == b""
    denied = json.loads(fourth.stdout)["reply"]
    assert not denied["ok"]
    assert denied["error"]["code"] == "permission_denied"
    assert store.read_ref(state_ref) == before


def test_run_inspect_is_read_only_for_non_root_roles(core, monkeypatch):
    import optagent_core.runner as runner
    from optagent_core.api import dispatch_dynamic

    seen = []

    def inspect_only(store, arguments, context):
        assert arguments["op"] == "inspect"
        assert not store.root.exists()
        seen.append(context["stamp"]["role"])
        return {
            "kind": "complete",
            "reply": public_success(
                "optagent.run",
                "inspect",
                case_id=arguments["case_id"],
                state_revision=None,
                result={"run": None},
            ),
        }

    monkeypatch.setattr(runner, "run_tool", inspect_only)
    context = core["context"](role="opt-worker")
    result = dispatch_dynamic(
        "optagent.run",
        {
            "op": "inspect",
            "case_id": "20c25d93-e027-48db-9e16-ccaa017751ee",
            "run_id": "39c4f3e2-76ed-4dc7-a875-628151a90eba",
        },
        context,
    )
    assert result["kind"] == "complete"
    assert result["reply"]["ok"]
    assert seen == ["opt-worker"]
    assert not (core["workspace"] / ".reaslab/optagent").exists()


def test_run_bridge_wrong_case_id_does_not_search_other_cases(core):
    from uuid import uuid4

    from optagent_core.api import dispatch_envelope

    case_a, _, _ = core["create_case"]()
    store = core["store"]()
    run_id = str(uuid4())
    original_run = {
        "run_id": run_id,
        "case_id": case_a,
        "dispatch_nonce": str(uuid4()),
        "created_by": core["context"]()["stamp"],
    }
    run_ref = store.write_family_version(case_a, "runs", run_id, original_run)
    state = store.read_case(case_a)

    def publish_run(current):
        current["run_ids"].append(run_id)
        current["current_records"]["runs"][run_id] = dict(run_ref)

    store.cas_case(case_a, state["state_revision"], publish_run)
    wrong_case = str(uuid4())
    context = core["context"]()
    result = dispatch_envelope(
        {
            "contract_version": 1,
            "kind": "run_bridge",
            "context": context,
            "phase": {
                "kind": "start_recheck",
                "case_id": wrong_case,
                "run_id": run_id,
                "prepared_id": str(uuid4()),
                "dispatch_nonce": str(uuid4()),
                "request_digest": "a" * 64,
            },
        }
    )
    assert result["kind"] == "complete"
    assert not result["reply"]["ok"]
    assert result["reply"]["case_id"] == wrong_case
    assert result["reply"]["error"]["code"] == "not_found"
    assert store.read_ref(run_ref) == canonical_json_bytes(original_run)
