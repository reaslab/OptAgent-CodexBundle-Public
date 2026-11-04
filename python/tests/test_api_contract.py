from __future__ import annotations

import uuid

import pytest

from optagent_core.api import dispatch_envelope
from optagent_core.api import _revise_candidate
from optagent_core.contracts import ContractError, load_tool_schemas, validate_schema


def test_core_retains_29_schema_operations_and_legacy_delivery_audit():
    schemas = load_tool_schemas()
    operation_count = sum(
        len(schema["input_schema"]["oneOf"])
        for schema in schemas.values()
    )
    assert operation_count == 29
    audit_args = {
        "op": "audit",
        "case_id": str(uuid.uuid4()),
        "delivery_id": str(uuid.uuid4()),
        "report_path": "reports/legacy.md",
    }
    validate_schema(audit_args, schemas["optagent.deliver"]["input_schema"])


def test_review_outcome_and_delivery_select_public_contract():
    schemas = load_tool_schemas()
    review_schema = schemas["optagent.review"]["input_schema"]
    common = {
        "op": "submit",
        "case_id": str(uuid.uuid4()),
        "review_id": str(uuid.uuid4()),
        "expected_review_revision": 2,
        "record_kind": "outcome",
        "claims": [],
        "feedback": ["Revise the approved answer to include the remaining condition."],
        "remaining": [],
        "report_path": None,
    }
    validate_schema({**common, "decision": "needs_changes"}, review_schema)
    validate_schema({**common, "decision": "ready", "feedback": []}, review_schema)
    with pytest.raises(ContractError):
        validate_schema({**common, "record_kind": "close", "decision": "ready"}, review_schema)

    select_schema = schemas["optagent.deliver"]["input_schema"]
    selection = {
        "op": "select",
        "case_id": str(uuid.uuid4()),
        "expected_state_revision": 3,
        "kind": "proof",
        "candidate_id": None,
        "review_id": str(uuid.uuid4()),
        "proof_ids": [str(uuid.uuid4())],
        "run_ids": [],
    }
    validate_schema(selection, select_schema)
    for obsolete in (
        {**selection, "claims": []},
        {**selection, "review_ids": [selection["review_id"]]},
        {key: value for key, value in selection.items() if key != "review_id"},
    ):
        with pytest.raises(ContractError):
            validate_schema(obsolete, select_schema)


def test_proof_dependencies_are_derived_and_instance_table_field_is_optional():
    schemas = load_tool_schemas()
    review_schema = schemas["optagent.review"]["input_schema"]
    proof_branch = next(
        branch
        for branch in review_schema["oneOf"]
        if branch.get("properties", {}).get("record_kind", {}).get("const") == "proof"
    )
    assert "old_run_ids" not in proof_branch["properties"]
    assert "old_run_ids" not in proof_branch["required"]
    proof_request = {
        "op": "submit",
        "case_id": str(uuid.uuid4()),
        "expected_state_revision": 2,
        "review_id": str(uuid.uuid4()),
        "expected_review_revision": 2,
        "record_kind": "proof",
        "kind": "analytic",
        "body_path": "proof.md",
        "premises": ["the stated premise"],
        "evidence_refs": [],
        "claim_kinds": ["optimality"],
        "candidate_id": None,
    }
    validate_schema(proof_request, review_schema)
    with pytest.raises(ContractError):
        validate_schema({**proof_request, "old_run_ids": []}, review_schema)

    case_schema = schemas["optagent.case"]["input_schema"]
    candidate_branch = next(
        branch
        for branch in case_schema["oneOf"]
        if branch.get("properties", {}).get("op", {}).get("const") == "revise"
        and branch.get("properties", {}).get("kind", {}).get("const") == "candidate"
    )
    instance_property = candidate_branch["properties"]["instance_parameter_object_id"]
    assert instance_property["description"] == (
        "Only for per-row independent instances: normalized table object ID whose every keyed row "
        "is one separate model/run instance. For one instance using the whole bundle, omit this field."
    )
    assert "instance_parameter_object_id" not in candidate_branch["required"]
    candidate_request = {
        "op": "revise",
        "case_id": str(uuid.uuid4()),
        "expected_state_revision": 1,
        "kind": "candidate",
        "model_path": "model.py",
        "local_module_paths": [],
        "data_id": None,
        "backend_id": "highs",
        "objective_plan": {"mode": "feasibility", "preference_note": "test"},
        "business_checker_path": None,
        "input_closure": "declared",
        "closure_note": "All inputs are declared.",
    }
    validate_schema(candidate_request, case_schema)
    validate_schema(
        {**candidate_request, "instance_parameter_object_id": "instances"}, case_schema
    )
    with pytest.raises(ContractError):
        validate_schema(
            {**candidate_request, "instance_parameter_object_id": None}, case_schema
        )


def test_contract_version_requires_an_exact_json_integer(core):
    context = {
        "stamp": {
            "session_id": core["session_id"],
            "role": "root",
            "turn_id": str(uuid.uuid4()),
            "call_id": str(uuid.uuid4()),
        },
        "workspace_absolute": str(core["workspace"]),
        "cancellation_observed": False,
        "captures": [],
    }

    def request(version):
        return {
            "contract_version": version,
            "kind": "dynamic_tool",
            "context": context,
            "tool": "optagent.case",
            "arguments": {"op": "list"},
        }

    accepted = dispatch_envelope(request(1))
    assert accepted["kind"] == "complete"
    assert accepted["reply"]["ok"]

    for invalid_version in (True, 1.0):
        with pytest.raises(ContractError) as caught:
            dispatch_envelope(request(invalid_version))
        assert caught.value.code == "bridge_protocol_error"


def test_candidate_write_schema_accepts_registered_solver_backends(core):
    case_id, _, _ = core["create_case"]()
    args = {
        "op": "revise",
        "case_id": case_id,
        "expected_state_revision": 1,
        "kind": "candidate",
        "model_path": "model.py",
        "local_module_paths": [],
        "data_id": None,
        "backend_id": "highs",
        "objective_plan": {"mode": "feasibility", "preference_note": "test"},
        "business_checker_path": None,
        "input_closure": "declared",
        "closure_note": "All inputs are declared.",
    }
    schema = load_tool_schemas()["optagent.case"]["input_schema"]
    candidate_schema = next(
        branch
        for branch in schema["oneOf"]
        if branch.get("properties", {}).get("op", {}).get("const") == "revise"
        and branch.get("properties", {}).get("kind", {}).get("const") == "candidate"
    )
    validate_schema(args, schema)
    from optagent_core.solver_protocol import SUPPORTED_BACKEND_IDS

    assert "authoring" not in candidate_schema["properties"]
    from optagent_core.solver_protocol import DIRECT_PYTHON_BACKEND_ID

    assert set(candidate_schema["properties"]["backend_id"]["enum"]) == set(SUPPORTED_BACKEND_IDS) | {DIRECT_PYTHON_BACKEND_ID}
    for backend_id in SUPPORTED_BACKEND_IDS:
        validate_schema({**args, "backend_id": backend_id}, schema)

    for unsupported in (
        {"authoring": "v5", "backend_id": "highs"},
        {"authoring": "native_program", "backend_id": "highs"},
        {"authoring": "pyomo", "backend_id": "unknown-solver"},
    ):
        with pytest.raises(ContractError):
            validate_schema({**args, **unsupported}, schema)
        reply = core["call"]("optagent.case", {**args, **unsupported})
        assert not reply["ok"]
        assert reply["error"]["code"] == "invalid_argument"

    with pytest.raises(ContractError) as caught:
        _revise_candidate(
            core["store"](),
            {"case_id": case_id},
            {"authoring": "native_program", "backend_id": "highs"},
            core["context"]()["stamp"],
        )
    assert caught.value.code == "invalid_argument"


def test_direct_candidate_family_is_inferred_from_backend_and_lex_is_rejected(core):
    case_id, _, _ = core["create_case"]()
    model_path = core["workspace"] / "direct.py"
    model_path.write_text("def solve(bundle):\n    return None\n", encoding="utf-8")
    state = core["store"]().read_case(case_id)
    args = {
        "op": "revise",
        "kind": "candidate",
        "case_id": case_id,
        "expected_state_revision": state["state_revision"],
        "model_path": "direct.py",
        "local_module_paths": [],
        "data_id": None,
        "backend_id": "direct_python",
        "objective_plan": {"mode": "feasibility", "preference_note": "direct family test"},
        "business_checker_path": None,
        "input_closure": "declared",
        "closure_note": "Direct Python candidate fixture.",
    }

    registered = core["call"]("optagent.case", args)

    assert registered["ok"], registered
    candidate_id = registered["result"]["candidate"]["id"]
    candidate = core["store"]().read_record(f".reaslab/optagent/cases/{case_id}/candidates/{candidate_id}.json")
    assert candidate["authoring"] == "direct_python"
    assert candidate["backend_id"] == "direct_python"

    state = core["store"]().read_case(case_id)
    lex = core["call"]("optagent.case", {
        **args,
        "expected_state_revision": state["state_revision"],
        "objective_plan": {
            "mode": "lexicographic",
            "layers": [{
                "objective_component": "cost",
                "sense": "min",
                "unit": "USD",
                "absolute_tolerance": 0,
                "relative_tolerance": 0,
            }],
            "preference_note": "unsupported direct plan",
        },
    })
    assert not lex["ok"]
    assert lex["error"]["code"] == "capability_uncovered"


def test_candidate_reactivation_restores_committed_identity_and_keeps_revision_high_water(core):
    case_id, _, _ = core["create_case"]()
    workspace = core["workspace"]
    model_a = workspace / "candidate-a.py"
    model_b = workspace / "candidate-b.py"
    model_c = workspace / "candidate-c.py"
    model_a.write_text("def build_model(bundle):\n    return None\n", encoding="utf-8")
    model_b.write_text("def build_model(bundle):\n    return None  # B\n", encoding="utf-8")
    model_c.write_text("def build_model(bundle):\n    return None  # C\n", encoding="utf-8")

    def register(model_path):
        state = core["store"]().read_case(case_id)
        return core["call"]("optagent.case", {
            "op": "revise", "case_id": case_id, "expected_state_revision": state["state_revision"],
            "kind": "candidate", "model_path": model_path, "local_module_paths": [], "data_id": None,
            "backend_id": "highs", "objective_plan": {"mode": "feasibility", "preference_note": "history reuse"},
            "business_checker_path": None, "input_closure": "declared", "closure_note": "Exact fixture binding.",
        })

    first = register("candidate-a.py")
    assert first["ok"], first
    first_candidate = first["result"]["candidate"]
    first_id = first_candidate["id"]
    assert first_candidate["candidate_revision"] == 1

    state = core["store"]().read_case(case_id)
    prepared = core["call"]("optagent.run", {
        "op": "prepare", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "candidate_id": first_id,
        "solve_options": {
            "execution_budget_seconds": 60, "time_limit_seconds": 30, "relative_gap": 0.0,
            "absolute_gap": 0.0, "threads": 1, "random_seed": 0,
            "feasibility_tolerance": 1e-7, "integrality_tolerance": 1e-7,
            "write_log": False, "export_model": False,
        },
    })
    assert prepared["ok"], prepared
    old_prepared_id = prepared["result"]["prepared"]["id"]

    second = register("candidate-b.py")
    assert second["ok"], second
    second_id = second["result"]["candidate"]["id"]
    assert second_id != first_id
    assert second["result"]["candidate"]["candidate_revision"] == 2
    after_second = core["store"]().read_case(case_id)
    generation_after_second = after_second["promotion_generation"]
    decision_count_after_second = len(after_second["decisions"])

    restored = register("candidate-a.py")
    assert restored["ok"], restored
    assert restored["result"]["changed_candidate"] is True
    assert restored["result"]["candidate"]["id"] == first_id
    assert restored["result"]["candidate"]["candidate_revision"] == 1
    after_restore = core["store"]().read_case(case_id)
    assert after_restore["candidate_id"] == first_id
    assert after_restore["candidate_revision"] == 1
    assert after_restore["state_revision"] == after_second["state_revision"] + 1
    assert after_restore["promotion_generation"] == generation_after_second + 1
    assert len(after_restore["decisions"]) == decision_count_after_second + 1
    assert after_restore["decisions"][-1]["kind"] == "candidate_change"
    assert after_restore["decisions"][-1]["refs"] == [first_candidate["record_ref"]]

    # The old Prepared remains bound to the earlier promotion generation.
    stale_start = core["call"]("optagent.run", {
        "op": "start", "case_id": case_id, "prepared_id": old_prepared_id,
        "expected_state_revision": after_restore["state_revision"],
    })
    assert not stale_start["ok"]
    assert stale_start["error"]["code"] == "stale_revision"

    noop = register("candidate-a.py")
    assert noop["ok"], noop
    assert noop["result"]["changed_candidate"] is False
    after_noop = core["store"]().read_case(case_id)
    assert after_noop["candidate_id"] == first_id
    assert after_noop["candidate_revision"] == 1
    assert after_noop["promotion_generation"] == after_restore["promotion_generation"]
    assert len(after_noop["decisions"]) == len(after_restore["decisions"])

    third = register("candidate-c.py")
    assert third["ok"], third
    assert third["result"]["candidate"]["id"] not in {first_id, second_id}
    assert third["result"]["candidate"]["candidate_revision"] == 3


def test_candidate_registration_does_not_reactivate_uncommitted_orphan(core, monkeypatch):
    from optagent_core.contracts import ContractError
    from optagent_core.api import _revise_candidate

    case_id, _, _ = core["create_case"]()
    model_path = core["workspace"] / "orphan-candidate.py"
    model_path.write_text("def build_model(bundle):\n    return None\n", encoding="utf-8")
    store = core["store"]()
    state = store.read_case(case_id)
    arguments = {
        "op": "revise", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "candidate", "model_path": "orphan-candidate.py", "local_module_paths": [],
        "data_id": None, "backend_id": "highs",
        "objective_plan": {"mode": "feasibility", "preference_note": "orphan candidate fixture"},
        "business_checker_path": None, "input_closure": "declared", "closure_note": "Orphan must be ignored.",
    }
    candidates_dir = store.resolve_path(f".reaslab/optagent/cases/{case_id}/candidates")
    original_cas = store.cas_case

    def fail_commit(*_args, **_kwargs):
        raise ContractError("stale_revision", "forced CAS loss after candidate record write")

    monkeypatch.setattr(store, "cas_case", fail_commit)
    with pytest.raises(ContractError, match="forced CAS loss"):
        _revise_candidate(store, state, arguments, core["context"]()["stamp"])
    monkeypatch.setattr(store, "cas_case", original_cas)

    orphan_records = list(candidates_dir.glob("*.json"))
    assert len(orphan_records) == 1
    orphan_id = orphan_records[0].stem
    assert store.read_case(case_id)["decisions"] == []

    registered = core["call"]("optagent.case", arguments)
    assert registered["ok"], registered
    new_id = registered["result"]["candidate"]["id"]
    assert new_id != orphan_id
    assert registered["result"]["candidate"]["candidate_revision"] == 1
    committed = store.read_case(case_id)
    assert committed["decisions"][-1]["refs"][0]["path"].endswith(f"/{new_id}.json")


def test_candidate_identity_changes_for_each_binding_dimension(core):
    case_id, _, _ = core["create_case"]()
    workspace = core["workspace"]
    store = core["store"]()
    (workspace / "model-a.py").write_text("def build_model(bundle):\n    return None\n", encoding="utf-8")
    (workspace / "model-b.py").write_text("def build_model(bundle):\n    return None  # changed\n", encoding="utf-8")
    (workspace / "module-a.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workspace / "module-b.py").write_text("VALUE = 2\n", encoding="utf-8")
    (workspace / "checker-a.py").write_text("def check_solution(bundle, solution):\n    return {'status': 'pass'}\n", encoding="utf-8")
    (workspace / "checker-b.py").write_text("def check_solution(bundle, solution):\n    return {'status': 'uncovered'}\n", encoding="utf-8")

    input_path = workspace / "sites.csv"
    input_path.write_bytes(b"site,cost\nA,10\n")
    state = store.read_case(case_id)
    captured = core["call"]("optagent.data", {
        "op": "capture", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "path": "sites.csv", "purpose": "data", "locations": ["row A"],
    })
    assert captured["ok"], captured
    source_id = captured["result"]["source"]["id"]
    data_plan = {
        "encoding": "utf-8",
        "blocks": [{
            "object_id": "sites", "source_id": source_id, "format": "csv",
            "layout": {"delimiter": ",", "header": "first_row", "skip_lines": 0},
            "kind": "table", "key_fields": ["site"],
            "columns": [
                {"name": "site", "position": None, "input_name": "site", "type": "string", "unit": None, "nullable": False},
                {"name": "cost", "position": None, "input_name": "cost", "type": "number", "unit": "USD", "nullable": False},
            ],
            "duplicate_policy": "error", "missing_policy": "error",
        }],
        "conversions": [],
    }

    def normalize():
        current = store.read_case(case_id)
        result = core["call"]("optagent.data", {
            "op": "normalize", "case_id": case_id, "expected_state_revision": current["state_revision"],
            "source_ids": [source_id], "plan": data_plan,
        })
        assert result["ok"], result
        return result["result"]["data_manifest"]["data_id"]

    data_a = normalize()
    data_b = normalize()
    assert data_a != data_b

    arguments = {
        "op": "revise", "case_id": case_id, "kind": "candidate",
        "model_path": "model-a.py", "local_module_paths": ["module-a.py"],
        "data_id": data_a, "instance_parameter_object_id": "sites", "backend_id": "highs",
        "objective_plan": {"mode": "feasibility", "preference_note": "binding identity fixture"},
        "business_checker_path": "checker-a.py", "input_closure": "declared", "closure_note": "identity base",
    }

    def register():
        current = store.read_case(case_id)
        result = core["call"]("optagent.case", {**arguments, "expected_state_revision": current["state_revision"]})
        assert result["ok"], result
        return result["result"]["candidate"]

    prior = register()
    assert prior["candidate_revision"] == 1
    variants = [
        ("model_path", "model-b.py"),
        ("local_module_paths", ["module-b.py"]),
        ("business_checker_path", "checker-b.py"),
        ("data_id", data_b),
        ("instance_parameter_object_id", "other-instance-key"),
        ("objective_plan", {"mode": "feasibility", "preference_note": "changed objective binding"}),
        ("backend_id", "glpk"),
        ("closure_note", "changed declared closure"),
    ]
    for field, value in variants:
        arguments[field] = value
        current = register()
        assert current["id"] != prior["id"], field
        assert current["candidate_revision"] == prior["candidate_revision"] + 1, field
        prior = current

    state = store.read_case(case_id)
    source_id_for_fact = state["source_ids"][0]
    revised_source = core["call"]("optagent.case", {
        "op": "revise", "case_id": case_id, "expected_state_revision": state["state_revision"],
        "kind": "sources", "source_ids": state["source_ids"],
        "facts": [{
            "fact_id": str(uuid.uuid4()), "text": "A source-bound fact changes the revision.",
            "source_ids": [source_id_for_fact], "interpretation": "Source revision identity test.",
        }],
        "capture_ids": [], "reason": "Change a sourced mathematical fact for the identity test.",
    })
    assert revised_source["ok"], revised_source
    source_changed_candidate = register()
    assert source_changed_candidate["id"] != prior["id"]
    assert source_changed_candidate["candidate_revision"] == prior["candidate_revision"] + 1
