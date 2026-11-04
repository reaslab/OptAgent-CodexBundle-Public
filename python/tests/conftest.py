from __future__ import annotations

import base64
import hashlib
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from optagent_core.api import dispatch_control_event, dispatch_dynamic
from optagent_core.store import CaseStore


@pytest.fixture
def core(tmp_path: Path):
    session_id = str(uuid.uuid4())
    arrival_epoch = str(uuid.uuid4())
    stamp = {
        "session_id": session_id,
        "role": "root",
        "turn_id": str(uuid.uuid4()),
        "call_id": str(uuid.uuid4()),
    }

    def context(
        *,
        role: str = "root",
        captures: list[dict] | None = None,
        cancellation_observed: bool = False,
        question_turn_context: dict | None = None,
    ) -> dict:
        trusted_stamp = {**stamp, "role": role, "turn_id": str(uuid.uuid4()), "call_id": str(uuid.uuid4())}
        return {
            "stamp": trusted_stamp,
            "workspace_absolute": str(tmp_path),
            "cancellation_observed": cancellation_observed,
            "captures": list(captures or []),
            "question_turn_context": question_turn_context,
        }

    def capture(raw: bytes, *, seq: int, channel: str = "prompt", admission: str | None = None) -> dict:
        if admission is None:
            admission = "received_prompt" if channel == "prompt" else "pending_steer"
        return {
            "capture_id": str(uuid.uuid4()),
            "arrival_epoch": arrival_epoch,
            "arrival_seq": seq,
            "session_id": session_id,
            "channel": channel,
            "admission": admission,
            "utf8_bytes_base64": base64.b64encode(raw).decode("ascii"),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    def call(
        tool: str,
        args: dict,
        *,
        role: str = "root",
        captures: list[dict] | None = None,
        cancellation_observed: bool = False,
        question_turn_context: dict | None = None,
    ) -> dict:
        response = dispatch_dynamic(
            tool,
            args,
            context(
                role=role,
                captures=captures,
                cancellation_observed=cancellation_observed,
                question_turn_context=question_turn_context,
            ),
        )
        assert response.get("kind") == "complete"
        return response["reply"]

    def create_case(*, raw: bytes = b"Minimize total cost.", seq: int = 1, task_kind: str = "optimize") -> tuple[str, dict, dict]:
        transfer = capture(raw, seq=seq)
        reply = call(
            "optagent.case",
            {
                "op": "create",
                "capture_id": transfer["capture_id"],
                "title": "Probe case",
                "task_kind": task_kind,
                "world_relation": "single",
                "authorization_note": "Explicitly authorized",
            },
            captures=[transfer],
        )
        assert reply["ok"], reply
        return reply["case_id"], transfer, reply

    return {
        "workspace": tmp_path,
        "session_id": session_id,
        "arrival_epoch": arrival_epoch,
        "context": context,
        "capture": capture,
        "call": call,
        "create_case": create_case,
        "store": lambda: CaseStore(tmp_path, create=True),
        "control": lambda event: dispatch_control_event(
            event, {"session_id": session_id, "workspace_absolute": str(tmp_path)}
        ),
    }
