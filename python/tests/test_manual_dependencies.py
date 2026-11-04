from __future__ import annotations

import hashlib
import uuid

def test_capture_fence_classification_is_local_and_nonmathematical(core):
    case_id, _, created = core["create_case"]()
    initial = core["store"]().read_case(case_id)
    incoming = core["capture"](b"Use a different report format only.", seq=2)
    event = {
        "event_id": core["capture"](b"event", seq=2)["capture_id"],
        "kind": "capture_arrived",
        "capture": incoming,
        "bindings": [{"case_id": case_id, "run_id": None, "pending_start": False}],
    }
    result = core["control"](event)
    assert result == {"ok": True, "event_id": event["event_id"], "affected_case_ids": [case_id], "error": None}

    fence = core["store"]().read_case(case_id)["pending_inputs"][0]
    assert core["store"]().read_ref(fence["raw_ref"]) == b"Use a different report format only."
    barrier_path = f".reaslab/optagent/input-fences/{hashlib.sha256(core['session_id'].encode()).hexdigest()}.json"
    barrier = core["store"]().read_record(barrier_path)
    assert barrier["latest_observed_seq"] == 2
    assert set(barrier) == {"session_id", "arrival_epoch", "latest_observed_seq"}

    state = core["store"]().read_case(case_id)
    reply = core["call"](
        "optagent.case",
        {
            "op": "revise",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "kind": "note_input",
            "capture_id": incoming["capture_id"],
            "disposition": "no_math_change",
            "reason": "Presentation-only request; it does not change the mathematics.",
        },
        captures=[incoming],
    )
    assert reply["ok"], reply
    updated = core["store"]().read_case(case_id)
    assert updated["source_revision"] == initial["source_revision"]
    assert updated["candidate_revision"] == initial["candidate_revision"]
    assert updated["promotion_generation"] == initial["promotion_generation"]
    # One state transition materializes the new input fence and one classifies it.
    assert updated["state_revision"] == initial["state_revision"] + 2
    assert updated["pending_inputs"] == []
    assert created["state_revision"] == initial["state_revision"]


def test_later_capture_does_not_erase_an_unmaterialized_old_input(core):
    case_id, _, _ = core["create_case"]()
    old = core["capture"](b"Change the capacity limit to 10.", seq=2)
    assert core["control"]({
        "event_id": core["capture"](b"event", seq=2)["capture_id"],
        "kind": "capture_arrived",
        "capture": old,
        "bindings": [{"case_id": case_id, "run_id": None, "pending_start": False}],
    })["ok"]
    state = core["store"]().read_case(case_id)
    old_fence = state["pending_inputs"][0]
    core["store"]().remove_file_if_hash(old_fence["raw_ref"]["path"], old_fence["raw_ref"]["sha256"])

    later = core["capture"](b"Just change the output format.", seq=3)
    assert core["control"]({
        "event_id": core["capture"](b"event", seq=3)["capture_id"],
        "kind": "capture_arrived",
        "capture": later,
        "bindings": [{"case_id": case_id, "run_id": None, "pending_start": False}],
    })["ok"]
    # The missing older raw bytes remain an unresolved fence; the later event
    # cannot stand in for them or advance materialization past the gap.
    state = core["store"]().read_case(case_id)
    assert [item["capture_id"] for item in state["pending_inputs"]] == [old["capture_id"], later["capture_id"]]
    assert state["pending_inputs"][0]["raw_ref"] == old_fence["raw_ref"]
    assert state["pending_inputs"][0]["disposition"] == "unclassified"
    reply = core["call"](
        "optagent.case",
        {
            "op": "revise",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "kind": "note_input",
            "capture_id": old["capture_id"],
            "disposition": "no_math_change",
            "reason": "The missing raw edit must be recovered before classifying it.",
        },
        captures=[],
    )
    assert not reply["ok"]
    assert reply["error"]["code"] == "capture_unavailable"
    assert core["store"]().read_case(case_id)["pending_inputs"][0]["capture_id"] == old["capture_id"]


def test_answer_uses_materialized_capture_and_rejects_replay_without_state_changes(core):
    case_id, _, _ = core["create_case"](raw=b"The capacity must be chosen.")
    store = core["store"]()
    question_capture = core["capture"](b"Please clarify the capacity.", seq=2)
    state = store.read_case(case_id)
    asked = core["call"](
        "optagent.case",
        {
            "op": "question",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "text": "What is the capacity?",
            "missing_fact": "The capacity value.",
            "blocks": [],
            "independent_work": ["Review the source while waiting."],
        },
        captures=[question_capture],
    )
    assert asked["ok"], asked
    question = asked["result"]["question"]
    before_answer = store.read_case(case_id)
    source_ids_before = list(before_answer["source_ids"])
    source_revision_before = before_answer["source_revision"]

    answer = core["capture"](b"The capacity is 17 units.", seq=3)
    arrived = core["control"](
        {
            "event_id": str(uuid.uuid4()),
            "kind": "capture_arrived",
            "capture": answer,
            "bindings": [{"case_id": case_id, "run_id": None, "pending_start": False}],
        }
    )
    assert arrived["ok"] and arrived["affected_case_ids"] == [case_id]
    fenced = store.read_case(case_id)
    assert [item["capture_id"] for item in fenced["pending_inputs"]] == [answer["capture_id"]]
    assert store.read_ref(fenced["pending_inputs"][0]["raw_ref"]) == b"The capacity is 17 units."

    stale = core["call"](
        "optagent.case",
        {
            "op": "answer",
            "case_id": case_id,
            "expected_state_revision": fenced["state_revision"] - 1,
            "request_id": question["request_id"],
            "expected_question_version": question["question_version"],
            "capture_id": answer["capture_id"],
            "interpretation": "Capacity is 17 units.",
        },
        captures=[answer],
    )
    assert not stale["ok"] and stale["error"]["code"] == "stale_revision"
    unchanged = store.read_case(case_id)
    assert unchanged["source_ids"] == source_ids_before
    assert unchanged["source_revision"] == source_revision_before
    assert unchanged["state_revision"] == fenced["state_revision"]
    question_ref = unchanged["current_records"]["questions"][question["request_id"]]
    assert store.read_family_record(
        case_id, "questions", question["request_id"], state=unchanged
    )[0]["status"] == "pending"

    answered = core["call"](
        "optagent.case",
        {
            "op": "answer",
            "case_id": case_id,
            "expected_state_revision": fenced["state_revision"],
            "request_id": question["request_id"],
            "expected_question_version": question["question_version"],
            "capture_id": answer["capture_id"],
            "interpretation": "Capacity is 17 units.",
        },
        captures=[answer],
    )
    assert answered["ok"], answered
    updated = store.read_case(case_id)
    answered_question, answered_question_ref = store.read_family_record(
        case_id, "questions", question["request_id"], state=updated
    )
    assert answered_question_ref != question_ref
    assert answered_question["status"] == "answered"
    assert answered_question["question_version"] == question["question_version"] + 1
    assert answered_question["answer_source_id"] in updated["source_ids"]
    assert updated["source_revision"] == source_revision_before + 1
    assert updated["pending_inputs"] == []
    source = store.read_record(
        f".reaslab/optagent/cases/{case_id}/sources/{answered_question['answer_source_id']}.json"
    )
    assert store.read_ref(source["bytes"]) == b"The capacity is 17 units."
    assert answered["result"]["state"]["pending_question_count"] == 0
    assert answered["result"]["state"]["state_revision"] == updated["state_revision"]
    fetched = core["call"]("optagent.case", {"op": "get", "case_id": case_id})
    assert fetched["ok"]
    assert fetched["result"]["state"]["pending_question_count"] == 0

    repeated = core["call"](
        "optagent.case",
        {
            "op": "answer",
            "case_id": case_id,
            "expected_state_revision": updated["state_revision"],
            "request_id": question["request_id"],
            "expected_question_version": question["question_version"],
            "capture_id": answer["capture_id"],
            "interpretation": "Capacity is 17 units.",
        },
        captures=[answer],
    )
    assert not repeated["ok"] and repeated["error"]["code"] == "question_stale"
    unknown = core["call"](
        "optagent.case",
        {
            "op": "answer",
            "case_id": case_id,
            "expected_state_revision": updated["state_revision"],
            "request_id": str(uuid.uuid4()),
            "expected_question_version": question["question_version"],
            "capture_id": answer["capture_id"],
            "interpretation": "Capacity is 17 units.",
        },
        captures=[answer],
    )
    assert not unknown["ok"] and unknown["error"]["code"] == "not_found"
    after_replays = store.read_case(case_id)
    assert after_replays["state_revision"] == updated["state_revision"]
    assert after_replays["source_revision"] == updated["source_revision"]
    assert after_replays["source_ids"] == updated["source_ids"]
