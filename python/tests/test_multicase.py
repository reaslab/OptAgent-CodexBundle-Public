from __future__ import annotations

import hashlib


def test_control_replay_is_idempotent_per_single_case_event(core):
    case_a, _, _ = core["create_case"](raw=b"Case A source.", seq=1)
    case_b, _, _ = core["create_case"](raw=b"Case B source.", seq=2)
    transfer = core["capture"](b"Shared session update.", seq=3)
    snapshots = {}
    for case_id in (case_a, case_b):
        event = {
            "event_id": core["capture"](b"event id", seq=3)["capture_id"],
            "kind": "capture_arrived",
            "capture": transfer,
            "bindings": [{"case_id": case_id, "run_id": None, "pending_start": False}],
        }
        first = core["control"](event)
        assert first["ok"]
        assert first["affected_case_ids"] == [case_id]
        snapshots[case_id] = core["store"]().ref_for_path(f".reaslab/optagent/cases/{case_id}/state.json")["sha256"]

        replay = core["control"](event)
        assert replay["ok"]
        assert replay["affected_case_ids"] == [case_id]
        ref = core["store"]().ref_for_path(f".reaslab/optagent/cases/{case_id}/state.json")
        assert ref["sha256"] == snapshots[case_id]
        assert len(core["store"]().read_case(case_id)["pending_inputs"]) == 1

    barrier_path = f".reaslab/optagent/input-fences/{hashlib.sha256(core['session_id'].encode()).hexdigest()}.json"
    barrier = core["store"]().read_record(barrier_path)
    assert barrier["latest_observed_seq"] == 3
    assert set(barrier) == {"session_id", "arrival_epoch", "latest_observed_seq"}
