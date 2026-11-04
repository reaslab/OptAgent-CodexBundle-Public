from __future__ import annotations

import uuid

import pytest

from optagent_core.contracts import ContractError, load_tool_schemas, validate_schema
from optagent_core.data import _validate_layout


CASE_ID = str(uuid.uuid4())
SOURCE_ID = str(uuid.uuid4())
REVIEW_ID = str(uuid.uuid4())


def _column(*, position: int | None, input_name: str | None) -> dict[str, object]:
    return {
        "name": "node",
        "position": position,
        "input_name": input_name,
        "type": "string",
        "unit": None,
        "nullable": False,
    }


def _normalize_args(
    fmt: str,
    layout: dict[str, object],
    column: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "op": "normalize",
        "case_id": CASE_ID,
        "expected_state_revision": 2,
        "source_ids": [SOURCE_ID],
        "plan": {
            "encoding": "utf-8",
            "blocks": [
                {
                    "object_id": "nodes",
                    "source_id": SOURCE_ID,
                    "format": fmt,
                    "layout": layout,
                    "kind": "table",
                    "key_fields": ["node"],
                    "columns": [column or _column(position=0, input_name=None)],
                    "duplicate_policy": "error",
                    "missing_policy": "error",
                }
            ],
            "conversions": [],
        },
    }


def _error_for(tool: str, arguments: dict[str, object]) -> ContractError:
    with pytest.raises(ContractError) as caught:
        validate_schema(arguments, load_tool_schemas()[tool]["input_schema"])
    return caught.value


@pytest.mark.parametrize(
    ("fmt", "layout"),
    [
        ("csv", {"delimiter": ",", "header": "first_row", "skip_lines": 0}),
        ("json", {"pointer": "/items"}),
        (
            "whitespace_table",
            {
                "skip_lines": 0,
                "header": "first_row",
                "take_rows": None,
                "skip_blank_lines": True,
                "field_count": 3,
            },
        ),
    ],
)
def test_normalize_schema_publishes_the_three_runtime_layouts(fmt, layout):
    validate_schema(_normalize_args(fmt, layout), load_tool_schemas()["optagent.data"]["input_schema"])


def test_layout_schema_reports_format_specific_missing_and_unknown_fields():
    bad = _normalize_args("csv", {"delimiter": ",", "header": "first_row", "take_rows": 2})
    error = _error_for("optagent.data", bad)
    assert error.pointer == "/plan/blocks/0/layout/skip_lines"

    with pytest.raises(ContractError) as caught:
        _validate_layout(
            "csv",
            {"delimiter": ",", "header": "first_row", "take_rows": 2},
            "/plan/blocks/0",
        )
    assert caught.value.pointer == "/plan/blocks/0/layout/skip_lines"
    assert "missing fields: skip_lines" in caught.value.message
    assert "unknown fields: take_rows" in caught.value.message


def test_shared_block_shape_still_rejects_missing_and_unknown_fields():
    missing = _normalize_args("csv", {"delimiter": ",", "header": "first_row", "skip_lines": 0})
    del missing["plan"]["blocks"][0]["object_id"]
    assert _error_for("optagent.data", missing).pointer == "/plan/blocks/0/object_id"

    unknown = _normalize_args("csv", {"delimiter": ",", "header": "first_row", "skip_lines": 0})
    unknown["plan"]["blocks"][0]["extra"] = True
    assert _error_for("optagent.data", unknown).pointer == "/plan/blocks/0/extra"


def test_schema_publication_keeps_nested_layout_and_selector_oneof():
    schema = load_tool_schemas()["optagent.data"]["input_schema"]
    normalize = next(
        branch for branch in schema["oneOf"]
        if branch["properties"].get("op", {}).get("const") == "normalize"
    )
    block = normalize["properties"]["plan"]["properties"]["blocks"]["items"]
    assert len(block["oneOf"]) == 5
    assert len(block["properties"]["columns"]["items"]["oneOf"]) == 2


@pytest.mark.parametrize(
    ("column", "valid"),
    [
        (_column(position=0, input_name=None), True),
        (_column(position=None, input_name="node_id"), True),
        (_column(position=None, input_name=None), False),
        (_column(position=0, input_name="node_id"), False),
    ],
)
def test_column_selector_requires_exactly_one_nonnull_value(column, valid):
    args = _normalize_args(
        "csv",
        {"delimiter": ",", "header": "first_row", "skip_lines": 0},
        column,
    )
    if valid:
        validate_schema(args, load_tool_schemas()["optagent.data"]["input_schema"])
    else:
        assert _error_for("optagent.data", args).code == "invalid_argument"


def test_schema_diagnostics_show_shallow_expectations_without_coercion(core):
    candidate = {
        "op": "revise",
        "case_id": CASE_ID,
        "expected_state_revision": 2,
        "kind": "candidate",
        "model_path": "model.py",
        "local_module_paths": [],
        "data_id": None,
        "backend_id": "highs",
        "objective_plan": {"mode": "feasibility", "preference_note": "Check feasibility."},
        "business_checker_path": None,
        "input_closure": "declared",
        "closure_note": "All model inputs are declared.",
    }
    schema = load_tool_schemas()["optagent.case"]["input_schema"]
    validate_schema(candidate, schema)

    reply = core["call"]("optagent.case", {**candidate, "data_id": "null"})
    assert reply["ok"] is False
    assert reply["error"] == {
        "code": "invalid_argument",
        "message": "string does not match required format",
        "pointer": "/data_id",
        "details": {
            "expected": {
                "type": ["string", "null"],
                "pattern": "^[0-9a-f-]{36}$",
            }
        },
        "effect": "none",
    }


def test_const_and_missing_field_diagnostics_expose_only_local_schema_expectations():
    invalid_selector = _normalize_args(
        "csv",
        {"delimiter": ",", "header": "first_row", "skip_lines": 0},
        _column(position=0, input_name="node_id"),
    )
    const_error = _error_for("optagent.data", invalid_selector)
    assert const_error.pointer == "/plan/blocks/0/columns/0/input_name"
    assert const_error.message == "unexpected constant value"
    assert const_error.details == {"expected": {"const": None}}

    missing_selector = _normalize_args(
        "csv",
        {"delimiter": ",", "header": "first_row", "skip_lines": 0},
        {key: value for key, value in _column(position=0, input_name=None).items() if key != "input_name"},
    )
    required_error = _error_for("optagent.data", missing_selector)
    assert required_error.pointer == "/plan/blocks/0/columns/0/input_name"
    assert required_error.message == "required field missing"
    assert required_error.details == {"expected": {"const": None}}


def test_fixed_candidate_branch_returns_its_missing_backend_field():
    arguments = {
        "op": "revise",
        "case_id": CASE_ID,
        "expected_state_revision": 2,
        "kind": "candidate",
        "model_path": "model.py",
        "local_module_paths": [],
        "data_id": None,
        "objective_plan": {"mode": "feasibility", "preference_note": "Check feasibility."},
        "business_checker_path": None,
        "input_closure": "declared",
        "closure_note": "All model inputs are declared.",
    }
    error = _error_for("optagent.case", arguments)
    schema = load_tool_schemas()["optagent.case"]["input_schema"]
    candidate_branch = next(
        branch
        for branch in schema["oneOf"]
        if branch.get("properties", {}).get("op", {}).get("const") == "revise"
        and branch.get("properties", {}).get("kind", {}).get("const") == "candidate"
    )
    assert error.code == "invalid_argument"
    assert error.message == "required field missing"
    assert error.pointer == "/backend_id"
    assert error.details == {
        "expected": {
            "type": candidate_branch["properties"]["backend_id"]["type"],
            "description": candidate_branch["properties"]["backend_id"]["description"],
        }
    }
    assert error.effect == "none"


def test_fixed_answer_branch_returns_its_missing_request_id():
    arguments = {
        "op": "answer",
        "case_id": CASE_ID,
        "expected_state_revision": 2,
        "expected_question_version": 1,
        "capture_id": SOURCE_ID,
        "interpretation": "The user confirmed the stated value.",
    }
    error = _error_for("optagent.case", arguments)
    assert error.pointer == "/request_id"


def test_fixed_ready_outcome_branch_returns_missing_claim_conditions():
    arguments = {
        "op": "submit",
        "case_id": CASE_ID,
        "review_id": REVIEW_ID,
        "expected_review_revision": 3,
        "record_kind": "outcome",
        "decision": "ready",
        "claims": [
            {
                "kind": "feasibility",
                "scope": "The registered instance.",
                "evidence_refs": [],
                "support": "unresolved",
            }
        ],
        "feedback": [],
        "remaining": [],
        "report_path": None,
    }
    error = _error_for("optagent.review", arguments)
    assert error.pointer == "/claims/0/conditions"
