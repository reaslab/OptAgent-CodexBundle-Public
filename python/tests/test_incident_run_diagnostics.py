from __future__ import annotations

import base64
import copy
import hashlib
import json
import uuid
from pathlib import Path

import pytest

from optagent_core.contracts import ContractError, canonical_json_bytes, ref_from_bytes
from optagent_core import runner as runner_module
from optagent_core.runner import _inspect, _start_receipt, _worker_failure_diagnostic
from optagent_core.store import CaseStore
from optagent_core.worker import _OUTPUT_REFS, _fail_seal


def _transport_receipt(*, error: str | None, output: str = "", exit_code: int | None = 0) -> dict:
    body = canonical_json_bytes({
        "success": error is None,
        "output": output,
        "error": error,
        "exit_code": exit_code,
    })
    return {
        "http_status": 200,
        "body_base64": base64.b64encode(body).decode("ascii"),
        "body_complete": True,
        "error_code": None,
        "send_observation": "attempted",
    }


class _RunStore:
    def __init__(self, case_id: str, run: dict):
        self.case_id = case_id
        self.run = run
        self.state = {"state_revision": 7, "run_ids": [run["run_id"]]}
        self.refs: dict[tuple[str, str], bytes] = {}

    def read_case(self, case_id: str) -> dict:
        assert case_id == self.case_id
        return dict(self.state)

    def read_ref(self, ref: dict) -> bytes:
        raw = self.refs[(ref["path"], ref["sha256"])]
        assert len(raw) == ref["byte_len"]
        assert hashlib.sha256(raw).hexdigest() == ref["sha256"]
        return raw

    def write_immutable(self, path: str, raw: bytes, media_type: str) -> dict:
        ref = ref_from_bytes(path, raw, media_type)
        self.refs[(path, ref["sha256"])] = bytes(raw)
        return ref


def _run(case_id: str, *, dispatch_state: str = "response_received") -> dict:
    run_id = str(uuid.uuid4())
    return {
        "run_id": run_id,
        "prepared_id": str(uuid.uuid4()),
        "dispatch_nonce": str(uuid.uuid4()),
        "dispatch_state": dispatch_state,
        "created_by": {"session_id": "session", "role": "root", "turn_id": "turn", "call_id": "call"},
        "http_request_digest": "b" * 64,
        "transport": {"http_status": None, "response": None, "error_code": None},
        "platform": {"execution_id": None, "id_binding": "absent", "job_status": "unknown"},
        "worker": {"status": "not_observed", "seal": None, "solver_invoked": None},
        "cancel": {"requested": False, "evidence": None, "stop_attempt": None, "stop_ack": None},
        "results": [],
        "case_id": case_id,
    }


def test_failure_diagnostic_checks_current_run_prepared_and_input_identity():
    run_id = str(uuid.uuid4())
    prepared_id = str(uuid.uuid4())
    input_digest = "c" * 64
    ref = ref_from_bytes(".reaslab/optagent/cases/c/runs/r/output/worker-failure.json", b"{}", "application/json")
    failure = {
        "run_id": run_id,
        "prepared_id": prepared_id,
        "input_digest": input_digest,
        "error_code": "engine:stopped",
        "error_type": "RuntimeError",
        "message": "the actual worker failure",
        "future_worker_field": {"kept": "in the sealed artifact"},
    }

    diagnosis = _worker_failure_diagnostic(
        failure, ref, run_id=run_id, prepared_id=prepared_id, input_digest=input_digest
    )
    assert diagnosis["ref"] == ref
    assert diagnosis["message"] == "the actual worker failure"

    old_failure = {key: value for key, value in failure.items() if key != "message"}
    old_diagnosis = _worker_failure_diagnostic(
        old_failure, ref, run_id=run_id, prepared_id=prepared_id, input_digest=input_digest
    )
    assert "message" not in old_diagnosis
    with pytest.raises(ContractError, match="message is invalid"):
        _worker_failure_diagnostic(
            {**failure, "message": None},
            ref,
            run_id=run_id,
            prepared_id=prepared_id,
            input_digest=input_digest,
        )

    with pytest.raises(ContractError, match="identity does not match"):
        _worker_failure_diagnostic(
            {**failure, "prepared_id": str(uuid.uuid4())},
            ref,
            run_id=run_id,
            prepared_id=prepared_id,
            input_digest=input_digest,
        )


def test_worker_failure_seal_keeps_the_actual_exception_message(tmp_path: Path, monkeypatch):
    CaseStore(tmp_path, create=True)
    case_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    prepared_id = str(uuid.uuid4())
    output_path = f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output"
    monkeypatch.setitem(_OUTPUT_REFS, output_path, {})

    _fail_seal(
        tmp_path,
        {
            "case_id": case_id,
            "run_id": run_id,
            "prepared_id": prepared_id,
            "dispatch_nonce": str(uuid.uuid4()),
            "input_digest": "d" * 64,
            "output_path": output_path,
        },
        "d" * 64,
        "2026-10-09T00:00:00Z",
        ContractError("output_invalid", "the precise model validation error"),
        [],
    )

    failure_path = tmp_path / output_path / "worker-failure.json"
    failure = json.loads(failure_path.read_text(encoding="utf-8"))
    assert failure["error_type"] == "ContractError"
    assert failure["error_code"] == "output_invalid"
    assert failure["message"] == "the precise model validation error"


def test_inspect_repeats_current_sealed_failure_while_new_artifacts_stay_delta(monkeypatch):
    case_id = str(uuid.uuid4())
    run = _run(case_id)
    run_id = run["run_id"]
    prepared_id = run["prepared_id"]
    transport_raw = canonical_json_bytes(_transport_receipt(error=None, output="worker returned", exit_code=0))
    transport_ref = ref_from_bytes(
        f".reaslab/optagent/cases/{case_id}/runs/{run_id}/transport/execute-receipt.json",
        transport_raw,
        "application/json",
    )
    run["transport"]["response"] = transport_ref
    run["cancel"]["requested"] = True
    input_digest = "e" * 64
    failure_raw = canonical_json_bytes({
        "run_id": run_id,
        "prepared_id": prepared_id,
        "input_digest": input_digest,
        "error_code": "worker_error",
        "error_type": "RuntimeError",
        "message": "candidate import failed at the recorded line",
    })
    failure_ref = ref_from_bytes(
        f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/worker-failure.json",
        failure_raw,
        "application/json",
    )
    diagnosis = _worker_failure_diagnostic(
        json.loads(failure_raw),
        failure_ref,
        run_id=run_id,
        prepared_id=prepared_id,
        input_digest=input_digest,
    )
    result_ref = ref_from_bytes(
        f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/instances/000000/layer-single/result.json",
        b"{}",
        "application/json",
    )
    seal_raw = canonical_json_bytes({"output_refs": [failure_ref, result_ref]})
    seal_ref = ref_from_bytes(
        f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/seal.json", seal_raw, "application/json"
    )
    seal = {
        "worker_completed": False,
        "batch_complete": False,
        "batch_summary": None,
        "solver_invoked_observed": False,
        "_verified_worker_failure": diagnosis,
        "output_refs": [failure_ref, result_ref],
    }
    store = _RunStore(case_id, run)
    store.refs[(transport_ref["path"], transport_ref["sha256"])] = transport_raw
    store.refs[(seal_ref["path"], seal_ref["sha256"])] = seal_raw
    holder = {"run": run, "record_ref": {"path": f".reaslab/optagent/cases/{case_id}/runs/{run_id}/versions/0.json"}}

    def locate(_store, actual_case_id, actual_run_id):
        assert actual_case_id == case_id and actual_run_id == run_id
        return store.state, holder["run"], holder["record_ref"]

    def publish(_store, _case_id, _run_id, update, *, expected_nonce=None):
        assert expected_nonce == run["dispatch_nonce"]
        updated = copy.deepcopy(holder["run"])
        update(updated)
        if updated != holder["run"]:
            holder["record_ref"] = {
                "path": f".reaslab/optagent/cases/{case_id}/runs/{run_id}/versions/{len(holder['run'].get('results', [])) + (1 if holder['run']['worker']['seal'] is None else 2)}.json"
            }
        holder["run"] = updated
        return updated, holder["record_ref"]

    monkeypatch.setattr(runner_module, "_locate_run", locate)
    monkeypatch.setattr(runner_module, "_load_prepared", lambda *_args: ({}, {}, {}))
    monkeypatch.setattr(runner_module, "_read_worker_seal", lambda *_args: (seal, seal_ref, [result_ref]))
    monkeypatch.setattr(runner_module, "_publish_run_update", publish)

    first = _inspect(store, {"case_id": case_id, "run_id": run_id}, {})["reply"]["result"]
    second = _inspect(store, {"case_id": case_id, "run_id": run_id}, {})["reply"]["result"]

    presented_diagnosis = {**diagnosis, "receipt_ref": transport_ref}
    assert first["diagnostics"] == second["diagnostics"] == [presented_diagnosis]
    assert failure_ref in first["new_artifacts"] and seal_ref in first["new_artifacts"]
    assert second["new_artifacts"] == []
    assert first["result_refs"] == second["result_refs"] == [result_ref]
    assert first["run"]["worker_status"] == "incomplete"
    assert first["run"]["cancel_requested"] is True
    assert "actor_cancelled" not in first


def test_start_receipt_preserves_invalid_seal_error_and_receipt_ref(monkeypatch):
    case_id = str(uuid.uuid4())
    run = _run(case_id, dispatch_state="requested")
    stamp = run["created_by"]
    prepared = {"prepared_id": run["prepared_id"]}
    state = {"state_revision": 12}
    store = _RunStore(case_id, run)
    holder = {"run": run, "record_ref": {"path": f".reaslab/optagent/cases/{case_id}/runs/{run['run_id']}/versions/0.json"}}
    receipt = _transport_receipt(error="early execute failure", output="bootstrap output", exit_code=1)
    phase = {
        "kind": "start_receipt",
        "case_id": case_id,
        "run_id": run["run_id"],
        "prepared_id": run["prepared_id"],
        "dispatch_nonce": run["dispatch_nonce"],
        "request_digest": run["http_request_digest"],
        "receipt": receipt,
    }

    monkeypatch.setattr(
        runner_module,
        "_check_bridge_identity",
        lambda *_args, **_kwargs: (case_id, state, run, prepared),
    )
    monkeypatch.setattr(runner_module, "_load_prepared", lambda *_args: (prepared, {}, {}))
    monkeypatch.setattr(
        runner_module,
        "_reconcile_run",
        lambda *_args: (_ for _ in ()).throw(
            ContractError("output_invalid", "the worker seal failed validation", pointer="/seal", details={"field": "input_digest"})
        ),
    )

    def publish(_store, _case_id, _run_id, update, *, expected_nonce=None):
        assert expected_nonce == run["dispatch_nonce"]
        updated = copy.deepcopy(holder["run"])
        update(updated)
        holder["run"] = updated
        holder["record_ref"] = {"path": f".reaslab/optagent/cases/{case_id}/runs/{run['run_id']}/versions/{len(store.refs)}.json"}
        return updated, holder["record_ref"]

    monkeypatch.setattr(runner_module, "_publish_run_update", publish)

    returned = runner_module.dispatch_run_bridge(store, phase, {"stamp": stamp})
    error = returned["reply"]["error"]

    receipt_ref = holder["run"]["transport"]["response"]
    assert returned["kind"] == "complete"
    assert error["code"] == "output_invalid"
    assert error["message"] == "the worker seal failed validation"
    assert error["pointer"] == "/seal"
    assert error["details"]["field"] == "input_digest"
    assert error["details"]["receipt_ref"] == receipt_ref
    assert error["details"]["run"]["worker_status"] == "invalid"
    assert "diagnostics" not in error["details"]


def test_start_returns_current_failure_diagnostic_and_new_artifact_delta(monkeypatch):
    case_id = str(uuid.uuid4())
    run = _run(case_id, dispatch_state="requested")
    stamp = run["created_by"]
    prepared = {"prepared_id": run["prepared_id"]}
    manifest = {"input_digest": "f" * 64}
    store = _RunStore(case_id, run)
    holder = {"run": run, "record_ref": {"path": f".reaslab/optagent/cases/{case_id}/runs/{run['run_id']}/versions/0.json"}}
    receipt = _transport_receipt(error=None, output="worker returned", exit_code=0)
    receipt_ref = ref_from_bytes(
        f".reaslab/optagent/cases/{case_id}/runs/{run['run_id']}/transport/execute-receipt.json",
        canonical_json_bytes(receipt),
        "application/json",
    )
    phase = {
        "kind": "start_receipt",
        "case_id": case_id,
        "run_id": run["run_id"],
        "prepared_id": run["prepared_id"],
        "dispatch_nonce": run["dispatch_nonce"],
        "request_digest": run["http_request_digest"],
        "receipt": receipt,
    }
    failure_value = {
        "run_id": run["run_id"],
        "prepared_id": run["prepared_id"],
        "input_digest": manifest["input_digest"],
        "error_code": "worker_error",
        "error_type": "ImportError",
        "message": "candidate import failed",
    }
    failure_raw = canonical_json_bytes(failure_value)
    failure_ref = ref_from_bytes(
        f".reaslab/optagent/cases/{case_id}/runs/{run['run_id']}/output/worker-failure.json",
        failure_raw,
        "application/json",
    )
    seal_ref = ref_from_bytes(
        f".reaslab/optagent/cases/{case_id}/runs/{run['run_id']}/output/seal.json",
        b"seal",
        "application/json",
    )
    failure = _worker_failure_diagnostic(
        failure_value,
        failure_ref,
        run_id=run["run_id"],
        prepared_id=run["prepared_id"],
        input_digest=manifest["input_digest"],
    )
    reconciled = {
        **run,
        "transport": {**run["transport"], "http_status": receipt["http_status"], "response": receipt_ref},
        "worker": {"status": "incomplete", "seal": seal_ref, "solver_invoked": False},
        "results": [],
        "_verified_worker_failure": failure,
    }

    monkeypatch.setattr(
        runner_module,
        "_check_bridge_identity",
        lambda *_args, **_kwargs: (case_id, {"state_revision": 3}, run, prepared),
    )
    monkeypatch.setattr(runner_module, "_load_prepared", lambda *_args: (prepared, {}, manifest))
    monkeypatch.setattr(runner_module, "_reconcile_run", lambda *_args: (reconciled, [failure_ref, seal_ref]))
    monkeypatch.setattr(runner_module, "_state_summary", lambda *_args: {"state_revision": 3})

    def publish(_store, _case_id, _run_id, update, *, expected_nonce=None):
        assert expected_nonce == run["dispatch_nonce"]
        updated = copy.deepcopy(holder["run"])
        update(updated)
        holder["run"] = updated
        holder["record_ref"] = {"path": f".reaslab/optagent/cases/{case_id}/runs/{run['run_id']}/versions/{len(store.refs)}.json"}
        return updated, holder["record_ref"]

    monkeypatch.setattr(runner_module, "_publish_run_update", publish)

    started = _start_receipt(store, phase, {"stamp": stamp})["reply"]["result"]

    assert started["diagnostics"][0] == {**failure, "receipt_ref": receipt_ref}
    assert started["new_artifacts"] == [failure_ref, seal_ref]
    assert started["run"]["worker_status"] == "incomplete"


@pytest.mark.parametrize("response_kind", ["execute_receipt", "same_run_observation", "cross_run", "invalid_ref"])
def test_inspect_diagnostics_preserve_unknown_without_valid_seal(monkeypatch, response_kind):
    case_id = str(uuid.uuid4())
    run = _run(case_id, dispatch_state="unknown")
    receipt = _transport_receipt(error="worker bootstrap failed", output="import failed", exit_code=1)
    store = _RunStore(case_id, run)
    if response_kind == "execute_receipt":
        response_path = f".reaslab/optagent/cases/{case_id}/runs/{run['run_id']}/transport/execute-receipt.json"
        response_raw = canonical_json_bytes(receipt)
    elif response_kind == "same_run_observation":
        response_path = f".reaslab/optagent/cases/{case_id}/runs/{run['run_id']}/output/arbitrary-response-observation.json"
        response_raw = canonical_json_bytes({
            "success": False,
            "output": "same-run observation is not an execute receipt",
            "error": "this arbitrary JSON must not create a terminal execution diagnosis",
            "exit_code": 1,
        })
    elif response_kind == "cross_run":
        other_run_id = str(uuid.uuid4())
        response_path = f".reaslab/optagent/cases/{case_id}/runs/{other_run_id}/output/transport-response.json"
        response_raw = b'{"dispatch":"unknown"}'
    else:
        response_path = f".reaslab/optagent/cases/{case_id}/runs/{run['run_id']}/output/transport-response.json"
        response_raw = b'{"dispatch":"unknown"}'

    if response_kind == "invalid_ref":
        run["transport"]["response"] = {
            "path": response_path,
            "sha256": hashlib.sha256(response_raw).hexdigest(),
        }
        expected_diagnostics = []
    else:
        response_ref = ref_from_bytes(response_path, response_raw, "application/json")
        run["transport"]["response"] = response_ref
        store.refs[(response_ref["path"], response_ref["sha256"])] = response_raw
        if response_kind == "execute_receipt":
            expected_diagnostics = [{
                "kind": "execute_receipt",
                "ref": response_ref,
                "send_observation": "attempted",
                "http_status": 200,
                "body_complete": True,
                "error_code": None,
                "execute_success": False,
                "exit_code": 1,
                "message": "worker bootstrap failed",
            }]
        elif response_kind == "same_run_observation":
            expected_diagnostics = [{"kind": "transport_observation", "ref": response_ref}]
        else:
            expected_diagnostics = []
    holder = {"run": run, "record_ref": {"path": f".reaslab/optagent/cases/{case_id}/runs/{run['run_id']}/versions/0.json"}}

    monkeypatch.setattr(runner_module, "_locate_run", lambda *_args: (store.state, holder["run"], holder["record_ref"]))
    monkeypatch.setattr(runner_module, "_load_prepared", lambda *_args: ({}, {}, {}))
    monkeypatch.setattr(runner_module, "_read_worker_seal", lambda *_args: (None, None, []))

    def publish(_store, _case_id, _run_id, update, *, expected_nonce=None):
        updated = copy.deepcopy(holder["run"])
        update(updated)
        holder["run"] = updated
        holder["record_ref"] = {"path": f".reaslab/optagent/cases/{case_id}/runs/{run['run_id']}/versions/incomplete.json"}
        return updated, holder["record_ref"]

    monkeypatch.setattr(runner_module, "_publish_run_update", publish)

    result = _inspect(store, {"case_id": case_id, "run_id": run["run_id"]}, {})["reply"]["result"]

    assert result["run"]["dispatch_state"] == "unknown"
    assert result["run"]["worker_status"] == "incomplete"
    assert result["run"]["result_count"] == 0
    assert result["diagnostics"] == expected_diagnostics
    assert not any(item["kind"] == "worker_failure" for item in result["diagnostics"])

