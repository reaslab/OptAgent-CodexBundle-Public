from __future__ import annotations

import math

import pytest

from optagent_core.contracts import ContractError, canonical_json_bytes
from optagent_core import worker


_INPUT_DIGEST = "a" * 64
_BINDING_DIGEST = "b" * 64
_INSTANCE_KEY = (7, "west")
_OBJECTIVE = {"component": "total_cost", "sense": "min", "unit": "currency", "value": 12.5}


def _check_payload(status="pass", *, objective_values=(), extra=None):
    payload = {
        "status": status,
        "checked_obligations": [],
        "objective_values": [dict(row) for row in objective_values],
        "violations": [],
        "uncovered": [],
    }
    if extra:
        payload.update(extra)
    return payload


def _invoke_check(monkeypatch, payload, solution, *, expected_objectives=None, mutate=None, raises=None):
    calls = []

    def frozen_call(_reader, _spec, _modules, *, function_name, args, **kwargs):
        calls.append((function_name, args))
        assert function_name == "check_solution"
        if mutate is not None:
            mutate(args[1])
        if raises is not None:
            raise raises
        return payload, ({"path": "checker.py"},)

    monkeypatch.setattr(worker, "call_frozen_function", frozen_call)
    return worker._business_check_solution(
        object(),
        {"path": "checker.py"},
        (),
        object(),
        solution,
        input_digest=_INPUT_DIGEST,
        instance_key=_INSTANCE_KEY,
        instance_binding_digest=_BINDING_DIGEST,
        expected_objective_values=expected_objectives,
    ), calls


def test_core_stamps_current_instance_identity_and_s_is_not_checker_returned(monkeypatch):
    solution = {"plan": [1, 2]}
    payload = _check_payload(extra={
        "input_digest": "c" * 64,
        "instance_key": [999],
        "instance_binding_digest": "d" * 64,
        "solution": {"attacker_selected": True},
    })

    (business, consumed, diagnostic, saved_solution), calls = _invoke_check(
        monkeypatch,
        payload,
        solution,
    )

    assert business["status"] == "uncovered"
    assert business["input_digest"] == _INPUT_DIGEST
    assert business["instance_key"] == [7, "west"]
    assert business["instance_binding_digest"] == _BINDING_DIGEST
    assert business["solution"] == {"plan": [1, 2]}
    assert saved_solution == {"plan": [1, 2]}
    assert consumed == ()
    assert diagnostic["code"] == "output_invalid"
    assert calls[0][1][1] is solution
    assert business["solution"] is solution


def test_extract_solution_normalizes_only_before_checker(monkeypatch):
    bundle, witness = object(), {"variables": []}
    returned = {"quantities": (2, [3, 4])}
    observed = []

    def frozen_call(_reader, _spec, _modules, *, function_name, args, **kwargs):
        observed.append((function_name, args))
        return returned, ()

    monkeypatch.setattr(worker, "call_frozen_function", frozen_call)
    solution, consumed = worker._extract_business_solution(
        object(), {"path": "model.py"}, (), bundle, witness
    )

    assert solution == {"quantities": [2, [3, 4]]}
    assert observed == [("extract_solution", (bundle, witness))]
    assert consumed == ()


@pytest.mark.parametrize("invalid", [math.inf, {"bad": float("nan")}, {1: "key"}, object()])
def test_extract_solution_rejects_non_json_values(monkeypatch, invalid):
    monkeypatch.setattr(worker, "call_frozen_function", lambda *args, **kwargs: (invalid, ()))
    with pytest.raises(ContractError, match="finite JSON"):
        worker._extract_business_solution(object(), {"path": "model.py"}, (), object(), {})


def test_checker_cannot_return_a_second_solution_and_core_keeps_extracted_s(monkeypatch):
    extracted = {"route": ["A", "B"]}
    payload = _check_payload(extra={"solution": {"route": ["wrong"]}})
    (business, consumed, diagnostic, saved), _ = _invoke_check(
        monkeypatch,
        payload,
        extracted,
    )
    assert business["status"] == "uncovered"
    assert business["solution"] == extracted
    assert saved == extracted
    assert consumed == ()
    assert diagnostic["code"] == "output_invalid"


def test_checker_mutation_is_detected_and_original_s_is_saved(monkeypatch):
    solution = {"inventory": {"period_0": 4.0}}
    original = canonical_json_bytes(solution)

    def mutate(candidate):
        candidate["inventory"]["period_0"] = 0.0
        candidate["inventory"]["period_1"] = 4.0

    (business, consumed, diagnostic, saved), _ = _invoke_check(
        monkeypatch,
        _check_payload(),
        solution,
        mutate=mutate,
    )
    assert business["status"] == "fail"
    assert business["violations"] == ["business_checker_modified_solution"]
    assert canonical_json_bytes(business["solution"]) == original
    assert canonical_json_bytes(saved) == original
    assert consumed == ()
    assert diagnostic["code"] == "business_checker_modified_solution"


def test_checker_exception_after_mutation_preserves_precheck_s(monkeypatch):
    solution = {"decision": [1, 0]}
    original = canonical_json_bytes(solution)

    def mutate(candidate):
        candidate["decision"][0] = 0

    (business, consumed, diagnostic, saved), _ = _invoke_check(
        monkeypatch,
        None,
        solution,
        mutate=mutate,
        raises=RuntimeError("checker stopped"),
    )
    assert business["status"] == "fail"
    assert canonical_json_bytes(business["solution"]) == original
    assert canonical_json_bytes(saved) == original
    assert consumed == ()
    assert diagnostic["error_type"] == "RuntimeError"


def test_checker_exception_without_mutation_keeps_s_but_is_uncovered(monkeypatch):
    solution = {"decision": [1, 0]}
    original = canonical_json_bytes(solution)
    (business, consumed, diagnostic, saved), _ = _invoke_check(
        monkeypatch,
        None,
        solution,
        raises=RuntimeError("checker stopped"),
    )
    assert business["status"] == "uncovered"
    assert business["solution"] == solution
    assert canonical_json_bytes(saved) == original
    assert consumed == ()
    assert diagnostic["error_type"] == "RuntimeError"


@pytest.mark.parametrize("status", ["fail", "uncovered"])
def test_failed_or_uncovered_checker_retains_core_solution(monkeypatch, status):
    solution = {"plan": ["real"]}
    (business, _, _, saved), _ = _invoke_check(monkeypatch, _check_payload(status), solution)
    assert business["status"] == status
    assert business["solution"] == solution
    assert saved == solution


def test_run_checks_pass_objective_against_current_witness_but_local_check_does_not(monkeypatch):
    solution = {"plan": [1]}
    payload = _check_payload(objective_values=[_OBJECTIVE])

    (run_business, _, _, _), _ = _invoke_check(
        monkeypatch,
        payload,
        solution,
        expected_objectives=[{**_OBJECTIVE, "value": 10.0}],
    )
    (local_business, _, _, _), _ = _invoke_check(
        monkeypatch,
        payload,
        solution,
        expected_objectives=None,
    )
    assert run_business["status"] == "fail"
    assert "business_checker_objective_mismatch" in run_business["violations"]
    assert local_business["status"] == "pass"
    assert local_business["objective_values"] == [_OBJECTIVE]
