from __future__ import annotations

import builtins
import os
from pathlib import Path
from uuid import uuid4

import pytest

from optagent_core.contracts import ContractError
from optagent_core.store import CaseStore


def test_read_case_reports_a_missing_case_state_with_its_identity(core):
    store = core["store"]()
    case_id = str(uuid4())

    with pytest.raises(ContractError) as missing:
        store.read_case(case_id)

    assert missing.value.code == "not_found"
    assert f"case state does not exist for case {case_id}" in str(missing.value)


def test_data_capture_distinguishes_a_missing_case_from_a_missing_source_file(core):
    missing_case_id = str(uuid4())
    missing_case = core["call"]("optagent.data", {
        "op": "capture",
        "case_id": missing_case_id,
        "expected_state_revision": 0,
        "path": "inputs/missing.txt",
        "purpose": "data",
        "locations": ["fixture"],
    })
    assert not missing_case["ok"]
    assert missing_case["error"]["code"] == "not_found"
    assert f"case state does not exist for case {missing_case_id}" in missing_case["error"]["message"]

    case_id, _, _ = core["create_case"]()
    state = core["store"]().read_case(case_id)
    missing_source = core["call"]("optagent.data", {
        "op": "capture",
        "case_id": case_id,
        "expected_state_revision": state["state_revision"],
        "path": "inputs/missing.txt",
        "purpose": "data",
        "locations": ["fixture"],
    })
    assert not missing_source["ok"]
    assert missing_source["error"]["code"] == "not_found"
    assert missing_source["error"]["message"] == "path does not exist"


def test_read_case_does_not_hide_invalid_state_json(core):
    case_id, _, _ = core["create_case"]()
    store = core["store"]()
    store.case_state_path(case_id).write_bytes(b"{")

    with pytest.raises(ContractError) as malformed:
        store.read_case(case_id)

    assert malformed.value.code == "output_invalid"
    assert str(malformed.value) == "stored JSON record is invalid"


def test_create_captures_original_bytes_and_case_state(core):
    case_id, transfer, reply = core["create_case"](raw=b"Minimize cost; preserve capacity.")
    store = core["store"]()
    state = store.read_case(case_id)
    assert state["state_revision"] == 1
    assert state["source_revision"] == 1
    assert state["candidate_id"] is None
    source_id = state["source_ids"][0]
    source = store.read_record(f".reaslab/optagent/cases/{case_id}/sources/{source_id}.json")
    raw = store.read_ref(source["bytes"])
    assert raw == b"Minimize cost; preserve capacity."
    assert source["origin"]["capture_id"] == transfer["capture_id"]
    assert reply["result"]["source"]["record_ref"]["sha256"] == store.ref_for_path(
        f".reaslab/optagent/cases/{case_id}/sources/{source_id}.json"
    )["sha256"]


def test_empty_and_unconfirmed_captures_cannot_create_cases(core):
    empty = core["capture"](b"", seq=1)
    reply = core["call"](
        "optagent.case",
        {
            "op": "create",
            "capture_id": empty["capture_id"],
            "title": "Empty",
            "task_kind": "optimize",
            "world_relation": "single",
            "authorization_note": "Explicitly authorized",
        },
        captures=[empty],
    )
    assert not reply["ok"]
    assert reply["error"]["code"] == "invalid_argument"

    steer = core["capture"](b"Use the later steer", seq=2, channel="steer", admission="unconfirmed_steer")
    reply = core["call"](
        "optagent.case",
        {
            "op": "create",
            "capture_id": steer["capture_id"],
            "title": "Unconfirmed",
            "task_kind": "optimize",
            "world_relation": "single",
            "authorization_note": "Explicitly authorized",
        },
        captures=[steer],
    )
    assert not reply["ok"]
    assert reply["error"]["code"] == "capture_unconfirmed"


def test_case_store_rejects_traversal_and_stale_revision(core):
    store = core["store"]()
    with pytest.raises(ContractError, match="traversal"):
        store.resolve_path(".reaslab/optagent/../outside")

    case_id, _, _ = core["create_case"]()
    state = store.read_case(case_id)
    store.cas_case(case_id, state["state_revision"], lambda current: current.update({"title": "Revised"}))
    with pytest.raises(ContractError) as error:
        store.cas_case(case_id, state["state_revision"], lambda current: current.update({"title": "Lost update"}))
    assert error.value.code == "stale_revision"
    assert store.read_case(case_id)["title"] == "Revised"


def test_read_user_file_rejects_reserved_root_but_internal_reader_keeps_state_access(core):
    store = core["store"]()
    state_path = ".reaslab/optagent/cases"
    internal_path = f"{state_path}/{core['create_case']()[0]}/state.json"
    with pytest.raises(ContractError) as error:
        store.read_user_file(internal_path)
    assert error.value.code == "path_denied"
    assert store.read_workspace_file(internal_path)[1]["path"] == internal_path

    workspace_file = core["workspace"] / "ordinary.txt"
    workspace_file.write_bytes(b"user input")
    raw, ref = store.read_user_file("ordinary.txt")
    assert raw == b"user input"
    assert ref["path"] == "ordinary.txt"


@pytest.mark.skipif(os.name == "nt", reason="directory symlink creation is platform-restricted")
def test_case_store_rejects_symlinked_reserved_namespace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / ".reaslab").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ContractError, match="internal state root cannot be a link"):
        CaseStore(workspace, create=True)
    assert not (outside / "optagent").exists()


def test_stable_read_rejects_a_different_file_opened_after_path_check(core, monkeypatch):
    workspace = core["workspace"] / "workspace"
    workspace.mkdir()
    victim = workspace / "record.json"
    victim.write_bytes(b"inside")
    outside = core["workspace"] / "outside-sentinel.txt"
    outside.write_bytes(b"OUTSIDE-SENTINEL")
    store = CaseStore(workspace, create=True)
    real_open = builtins.open
    injected = False

    def open_different_inode(path, mode="r", *args, **kwargs):
        nonlocal injected
        if not injected and Path(path) == victim and mode == "rb":
            injected = True
            return real_open(outside, mode, *args, **kwargs)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_different_inode)
    with pytest.raises(ContractError) as error:
        store._read_stable_path(victim)
    assert injected
    assert error.value.code == "source_changed_during_read"
    assert victim.read_bytes() == b"inside"
    assert outside.read_bytes() == b"OUTSIDE-SENTINEL"
