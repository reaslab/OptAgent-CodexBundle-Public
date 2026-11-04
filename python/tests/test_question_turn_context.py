from __future__ import annotations

import uuid
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from optagent_core.api import (
    _dynamic_context,
    _question_turn_context_for_answer,
    _review_source_provenance,
)
from optagent_core.contracts import ContractError, canonical_json_bytes, strict_json_loads
from optagent_core.delivery import _package_review_closure, _zip_bytes
from optagent_core.review import _resolve_evidence_paths, _review_evidence_index
from optagent_core.sources import source_projection


def _assistant_message(turn_id: str, message_id: str, phase: str, text: str) -> dict:
    return {
        "source": {
            "id": {"message_id": message_id, "turn_id": turn_id, "role": "assistant"},
            "revision": f"revision-{message_id}",
            "complete": True,
        },
        "phase": phase,
        "text": text,
    }


def test_answer_persists_only_the_question_turn_context_and_review_can_read_both_refs(core):
    case_id, _, _ = core["create_case"](raw=b"The capacity is unspecified.")
    store = core["store"]()
    state = store.read_case(case_id)
    question_capture = core["capture"](b"Please clarify the capacity.", seq=2)
    asked = core["call"](
        "optagent.case",
        {
            "op": "question",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "text": "What is the capacity?",
            "missing_fact": "The capacity value.",
            "blocks": [],
            "independent_work": [],
        },
        captures=[question_capture],
    )
    assert asked["ok"], asked
    question = asked["result"]["question"]
    assert question["asked_by"]["role"] == "root"
    assert question["asked_by"]["session_id"] == core["session_id"]
    pending_state = store.read_case(case_id)
    exact_question, exact_question_ref = store.read_family_record(
        case_id, "questions", question["request_id"], state=pending_state
    )
    assert exact_question["asked_by"] == question["asked_by"]

    answer = core["capture"](b"The capacity is 17 units.", seq=3)
    arrived = core["control"](
        {
            "event_id": str(uuid.uuid4()),
            "kind": "capture_arrived",
            "capture": answer,
            "bindings": [{"case_id": case_id, "run_id": None, "pending_start": False}],
        }
    )
    assert arrived["ok"]
    other_turn = str(uuid.uuid4())
    qturn = question["asked_by"]["turn_id"]
    retained = {
        "assistant_family_omitted": True,
        "unavailable_turn_ids": [],
        "messages": [
            _assistant_message(qturn, "assistant-1", "commentary", "Use capacity c >= 10."),
            _assistant_message(other_turn, "assistant-other", "final_answer", "Unrelated later answer."),
            _assistant_message(qturn, "assistant-2", "final_answer", "Let the user choose c."),
        ],
    }
    state = store.read_case(case_id)
    answered = core["call"](
        "optagent.case",
        {
            "op": "answer",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "request_id": question["request_id"],
            "expected_question_version": question["question_version"],
            "capture_id": answer["capture_id"],
            "interpretation": "Capacity is 17 units.",
        },
        captures=[answer],
        question_turn_context=retained,
    )
    assert answered["ok"], answered
    source_id = answered["result"]["source"]["id"]
    state = store.read_case(case_id)
    source = store.read_record(f".reaslab/optagent/cases/{case_id}/sources/{source_id}.json")
    assert store.read_ref(source["bytes"]) == b"The capacity is 17 units."
    retained["messages"][0]["text"] = "Later live-history replacement."
    context_ref = source["conversation_context"]
    context_raw = store.read_ref(context_ref)
    context = strict_json_loads(context_raw, max_bytes=len(context_raw))
    assert context["kind"] == "question_turn_context"
    assert context["capture_binding"]["capture_id"] == answer["capture_id"]
    assert context["capture_binding"]["sha256"] == source["bytes"]["sha256"]
    assert context["question_ref"] == exact_question_ref
    assert context["asked_by"] == question["asked_by"]
    assert context["assistant_family_omitted"] is True
    assert context["messages"][0]["text"] == "Use capacity c >= 10."
    assert [item["source"]["id"]["message_id"] for item in context["messages"]] == [
        "assistant-1", "assistant-2"
    ]
    assert [item["phase"] for item in context["messages"]] == ["commentary", "final_answer"]

    provenance, raw_refs = _review_source_provenance(store, state, source_projection(store, state))
    projected_context = next(
        item["conversation_context"]
        for item in provenance["sources"]
        if item["source_id"] == source_id
    )
    assert projected_context["ref"] == context_ref
    assert projected_context["question_ref"] == exact_question_ref
    assert "不包含完整中间对话" in projected_context["scope"]
    assert context_ref in raw_refs and exact_question_ref in raw_refs

    review_id = str(uuid.uuid4())
    provenance_raw = canonical_json_bytes(provenance)
    provenance_ref = store.write_immutable(
        f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/source-provenance.json",
        provenance_raw,
        "application/json",
    )
    review = {
        "review_id": review_id,
        "source_provenance_ref": provenance_ref,
        "note": None,
        "binding_evidence_refs": {},
        "binding": {"candidate_id": None, "proof_ids": [], "run_ids": []},
    }
    allowed = _resolve_evidence_paths(
        store,
        state,
        review,
        [context_ref["path"], exact_question_ref["path"]],
        pointer="/evidence_paths",
    )
    assert allowed == [context_ref, exact_question_ref]

    context_path = store.resolve_path(context_ref["path"], must_exist=True)
    context_path.write_bytes(context_raw + b" ")
    with pytest.raises(ContractError, match="artifact bytes no longer match their reference"):
        store.read_ref(context_ref)


def test_question_turn_context_selection_degrades_without_rejecting_answer():
    question = {
        "asked_by": {
            "session_id": "session-a",
            "role": "root",
            "turn_id": "question-turn",
            "call_id": "question-call",
        }
    }
    question_ref = {
        "path": ".reaslab/optagent/cases/c/questions/q/versions/ref.json",
        "sha256": "a" * 64,
        "byte_len": 1,
        "media_type": "application/json",
    }
    prompt = {
        "session_id": "session-a",
        "capture_id": "capture-a",
        "arrival_epoch": "epoch-a",
        "arrival_seq": 2,
        "channel": "prompt",
        "admission": "received_prompt",
        "sha256": "b" * 64,
    }
    stamp = {
        "session_id": "session-a",
        "role": "root",
        "turn_id": "answer-turn",
        "call_id": "answer-call",
    }
    retained = {
        "assistant_family_omitted": True,
        "unavailable_turn_ids": [],
        "messages": [
            _assistant_message("other-turn", "unrelated", "final_answer", "Ignore me."),
            _assistant_message("question-turn", "first", "commentary", "First candidate."),
            _assistant_message("question-turn", "second", "final_answer", "Second candidate."),
        ],
    }
    trusted = _dynamic_context(
        {
            "stamp": stamp,
            "workspace_absolute": str(Path.cwd()),
            "cancellation_observed": False,
            "captures": [],
            "question_turn_context": retained,
        }
    )
    assert trusted["question_turn_context"] == retained
    context = _question_turn_context_for_answer(question, question_ref, prompt, stamp, retained)
    assert context is not None
    assert context["assistant_family_omitted"] is True
    assert [item["source"]["id"]["message_id"] for item in context["messages"]] == ["first", "second"]

    no_match = {
        **retained,
        "messages": [_assistant_message("other-turn", "only-other", "final_answer", "No match.")],
    }
    unavailable = {**retained, "unavailable_turn_ids": ["question-turn"]}
    no_question_candidates = [
        ("legacy question", {}, question_ref, prompt, stamp, retained),
        ("malformed anchor", {"asked_by": {"role": "root"}}, question_ref, prompt, stamp, retained),
        ("same turn", question, question_ref, prompt, {**stamp, "turn_id": "question-turn"}, retained),
        ("other session", question, question_ref, prompt, {**stamp, "session_id": "session-b"}, retained),
        ("child caller", question, question_ref, prompt, {**stamp, "role": "opt-verifier"}, retained),
        (
            "steer capture",
            question,
            question_ref,
            {**prompt, "channel": "steer", "admission": "accepted_steer"},
            stamp,
            retained,
        ),
        (
            "unconfirmed steer",
            question,
            question_ref,
            {**prompt, "channel": "steer", "admission": "unconfirmed_steer"},
            stamp,
            retained,
        ),
        ("target turn unavailable", question, question_ref, prompt, stamp, unavailable),
        ("no retained context", question, question_ref, prompt, stamp, None),
        ("no message for target turn", question, question_ref, prompt, stamp, no_match),
    ]
    for label, candidate, exact_ref, capture, caller, retained_context in no_question_candidates:
        assert _question_turn_context_for_answer(
            candidate, exact_ref, capture, caller, retained_context
        ) is None, label


def test_adopted_question_source_refs_reach_review_and_portable_package_closure(core):
    case_id, _, _ = core["create_case"](raw=b"The source may later leave the active source list.")
    store = core["store"]()
    state = store.read_case(case_id)
    question_capture = core["capture"](b"Please clarify the value.", seq=2)
    asked = core["call"](
        "optagent.case",
        {
            "op": "question",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "text": "What value should be used?",
            "missing_fact": "The requested value.",
            "blocks": [],
            "independent_work": [],
        },
        captures=[question_capture],
    )
    assert asked["ok"], asked
    question = asked["result"]["question"]

    answer_capture = core["capture"](b"Use value 17.", seq=3)
    arrival = core["control"](
        {
            "event_id": str(uuid.uuid4()),
            "kind": "capture_arrived",
            "capture": answer_capture,
            "bindings": [{"case_id": case_id, "run_id": None, "pending_start": False}],
        }
    )
    assert arrival["ok"], arrival
    state = store.read_case(case_id)
    answer = core["call"](
        "optagent.case",
        {
            "op": "answer",
            "case_id": case_id,
            "expected_state_revision": state["state_revision"],
            "request_id": question["request_id"],
            "expected_question_version": question["question_version"],
            "capture_id": answer_capture["capture_id"],
            "interpretation": "The user specified value 17.",
        },
        captures=[answer_capture],
        question_turn_context={
            "assistant_family_omitted": False,
            "unavailable_turn_ids": [],
            "messages": [
                _assistant_message(
                    question["asked_by"]["turn_id"],
                    "proposal-final",
                    "final_answer",
                    "Use value 17 for the current calculation.",
                )
            ],
        },
    )
    assert answer["ok"], answer

    state = store.read_case(case_id)
    provenance, _ = _review_source_provenance(store, state, source_projection(store, state))
    adopted_source = next(
        source for source in provenance["sources"] if isinstance(source.get("conversation_context"), dict)
    )
    context_ref = adopted_source["conversation_context"]["ref"]
    exact_question_ref = adopted_source["conversation_context"]["question_ref"]
    gap_ref = store.write_immutable(
        f".reaslab/optagent/host/route_audits/{uuid.uuid4()}.json",
        canonical_json_bytes({"synthetic_gap": True}),
        "application/json",
    )
    adoption_provenance = {
        **provenance,
        "source_binding": {**provenance["source_binding"], "math_source_ids": []},
        "provenance_source_ids": [],
        "sources": [],
        "route": {
            **provenance["route"],
            "guards": [
                {
                    "gap_ref": gap_ref,
                    "scope": {"session_id": core["session_id"], "covered_seq": 1, "missing_seq_ranges": []},
                    "unresolved": False,
                    "adoption": {"sources": [adopted_source]},
                }
            ],
        },
    }
    review_id = str(uuid.uuid4())
    provenance_ref = store.write_immutable(
        f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/source-provenance.json",
        canonical_json_bytes(adoption_provenance),
        "application/json",
    )
    review = {
        "case_id": case_id,
        "review_id": review_id,
        "source_provenance_ref": provenance_ref,
        "note": None,
        "binding": {"candidate_id": None, "proof_ids": [], "run_ids": []},
        "binding_evidence_refs": {"candidate": None, "proofs": {}, "runs": {}},
    }
    allowed = _resolve_evidence_paths(
        store,
        state,
        review,
        [context_ref["path"], exact_question_ref["path"]],
        pointer="/adoption_evidence_paths",
    )
    assert allowed == [context_ref, exact_question_ref]
    indexed = _review_evidence_index(store, state, review)
    assert context_ref["path"] in indexed
    assert exact_question_ref["path"] in indexed

    review_snapshot = {
        **review,
        "status": "closed",
        "conclusion": {"claims": [], "remaining": []},
        "issues": [],
        "outcomes": [],
        "answer_ref": None,
        "prior_review_ref": None,
    }
    review_ref = store.write_family_version(case_id, "reviews", review_id, review_snapshot)
    files: dict[str, bytes] = {}
    objects: list[dict] = []
    ref_map: dict[tuple[str, str, int, str], dict] = {}
    _projection, closure_refs = _package_review_closure(
        store,
        state,
        review_id,
        files,
        objects,
        ref_map,
        snapshot_ref=review_ref,
    )
    mapped_paths = {
        (row["ref"]["path"], row["ref"]["sha256"], row["ref"]["byte_len"], row["ref"]["media_type"]): row["package_path"]
        for row in ref_map.values()
    }
    archive_bytes = _zip_bytes(files)
    with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
        names = set(archive.namelist())
        for ref in (context_ref, exact_question_ref):
            identity = (ref["path"], ref["sha256"], ref["byte_len"], ref["media_type"])
            assert ref in closure_refs
            package_path = mapped_paths[identity]
            assert package_path in names
            assert archive.read(package_path) == store.read_ref(ref)
