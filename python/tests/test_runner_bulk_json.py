from __future__ import annotations

import pytest

from optagent_core.contracts import ContractError, canonical_json_bytes, strict_json_loads
from optagent_core.data import read_prepared_source_records
from optagent_core.runner import _read_worker_seal
from family_fixtures import read_run


def _put_json(store, output_root: str, relative_path: str, value: dict) -> dict:
    return store.write_immutable(
        f"{output_root}/{relative_path}",
        canonical_json_bytes(value),
        "application/json",
    )


def _replace_test_seal_bytes(store, case_id: str, run_id: str, seal_value: dict) -> None:
    relative = f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/seal.json"
    # This mutates only the pytest-owned temporary workspace to exercise two
    # sealed-output membership variants against the same fixed run identity.
    store.resolve_path(relative, must_exist=True).write_bytes(canonical_json_bytes(seal_value))


def test_read_worker_seal_parses_large_sealed_witness_before_binding_and_fallback(core):
    # Reuse Core10's public prepare/start and validator-shaped partial batch.
    # It writes synthetic output artifacts and explicitly does not invoke a solver.
    from test_delivery import _synthetic_reviewable_batch

    case_id, _, run_id, result_refs, store = _synthetic_reviewable_batch(core)
    run, _run_ref = read_run(store, case_id, run_id)
    prepared_path = f".reaslab/optagent/cases/{case_id}/prepared/{run['prepared_id']}/prepared.json"
    prepared = store.read_record(prepared_path)
    manifest = strict_json_loads(store.read_ref(prepared["input_manifest"]))
    output_root = f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output"
    prior_seal = strict_json_loads(store.read_ref(run["worker"]["seal"]))

    # Replace the synthetic A Result with another Result whose witness has
    # enough realistic variable rows to exceed strict_json_loads' 4 MiB default.
    result_a_ref = None
    result_a = None
    for result_ref in result_refs:
        value = strict_json_loads(store.read_ref(result_ref))
        if value["instance_key"] == ["A"]:
            result_a_ref, result_a = result_ref, value
            break
    assert result_a_ref is not None and result_a is not None
    observation_path = f"{result_a_ref['path'].rsplit('/', 1)[0]}/backend-observation.json"
    original_observation_ref = next(
        ref for ref in prior_seal["output_refs"] if ref["path"] == observation_path
    )
    original_observation = strict_json_loads(store.read_ref(original_observation_ref))

    instance_binding = result_a["instance_binding_digest"]
    input_digest = prepared["input_digest"]
    key = ["A"]
    objective = {"component": "cost", "sense": "min", "unit": "USD", "value": 10.0}
    variable_count = 56_000
    variables = [
        {
            "component": f"var_{index:06d}",
            "key": [],
            "domain": "Reals",
            "value": 1.0,
            "origin": "solver_primal",
        }
        for index in range(variable_count)
    ]
    witness_value = {
        "input_digest": input_digest,
        "instance_key": key,
        "instance_binding_digest": instance_binding,
        "variables": variables,
        "objective_values": [objective],
    }
    witness_raw = canonical_json_bytes(witness_value)
    assert len(witness_raw) > 4 * 1024 * 1024
    assert len(witness_raw) < 8 * 1024 * 1024

    stem = "instances/000003/layer-single"
    witness_ref = store.write_immutable(
        f"{output_root}/{stem}/witness.json", witness_raw, "application/json"
    )
    check_ref = _put_json(store, output_root, f"{stem}/model-check.json", {
        "input_digest": input_digest,
        "instance_key": key,
        "instance_binding_digest": instance_binding,
        "status": "pass",
        "variable_coverage": {
            "required": variable_count,
            "present": variable_count,
            "completed": 0,
            "missing_count": 0,
        },
        "component_coverage": [],
        "violations": None,
        "objective_values": [objective],
        "tolerances": {"feasibility": 1e-7, "integrality": 1e-7},
        "uncovered": [],
    })
    _put_json(store, output_root, f"{stem}/business-check.json", {
        "input_digest": input_digest,
        "instance_key": key,
        "instance_binding_digest": instance_binding,
        "status": "uncovered",
        "checked_obligations": [],
        "objective_values": [objective],
        "violations": [],
        "uncovered": ["business_checker_not_provided"],
    })
    raw_primals_ref = store.write_immutable(
        f"{output_root}/{stem}/raw-primals.jsonl", b"", "application/x-ndjson"
    )
    # Preserve the producer-shaped option observations bound to the synthetic
    # Result while replacing only the raw-primals Ref for this large-witness case.
    observation = {**original_observation, "raw_primals": raw_primals_ref}
    observation_ref = _put_json(store, output_root, f"{stem}/backend-observation.json", observation)
    result_ref = _put_json(store, output_root, f"{stem}/result.json", {
        **{name: value for name, value in result_a.items() if name not in {"witness", "check"}},
        "witness": witness_ref,
        "check": check_ref,
    })

    sealed_outputs = [
        dict(ref) for ref in prior_seal["output_refs"] if ref["path"] != result_a_ref["path"]
    ]
    sealed_outputs.extend([witness_ref, check_ref, raw_primals_ref, observation_ref])
    business_ref = store.ref_for_path(f"{output_root}/{stem}/business-check.json")
    sealed_outputs.extend([business_ref, result_ref])
    sealed_outputs.sort(key=lambda ref: ref["path"])
    positive_seal = {**prior_seal, "output_refs": sealed_outputs}
    _replace_test_seal_bytes(store, case_id, run_id, positive_seal)

    accepted, _, accepted_results = _read_worker_seal(store, case_id, run, prepared, manifest)
    assert accepted is not None
    assert accepted["batch_complete"] is False
    assert len(accepted_results) == 2
    assert witness_ref["byte_len"] == len(witness_raw)
    assert any(ref["path"] == result_ref["path"] for ref in accepted_results)

    # The second pass removes only the large Witness from the Seal. The Result
    # still references its real own-output Ref, so the fallback parser must
    # accept the actual raw length before the original seal-membership gate
    # rejects this incomplete evidence.
    fallback_seal = {
        **positive_seal,
        "output_refs": [ref for ref in sealed_outputs if ref["path"] != witness_ref["path"]],
    }
    _replace_test_seal_bytes(store, case_id, run_id, fallback_seal)
    with pytest.raises(ContractError, match="Witness is not a canonical sealed output"):
        _read_worker_seal(store, case_id, run, prepared, manifest)


def test_present_invalid_batch_summary_is_rejected_instead_of_becoming_unknown(core):
    from test_delivery import _synthetic_reviewable_batch

    case_id, _, run_id, _, store = _synthetic_reviewable_batch(core)
    run, _run_ref = read_run(store, case_id, run_id)
    prepared = store.read_record(f".reaslab/optagent/cases/{case_id}/prepared/{run['prepared_id']}/prepared.json")
    manifest = strict_json_loads(store.read_ref(prepared["input_manifest"]))
    old_seal_ref = run["worker"]["seal"]
    seal = strict_json_loads(store.read_ref(old_seal_ref))
    old_batch = next(ref for ref in seal["output_refs"] if ref["path"].endswith("/batch-summary.json"))
    batch_path = store.resolve_path(old_batch["path"], must_exist=True)
    batch_path.write_bytes(canonical_json_bytes({"schema_version": 1, "planned_instance_count": 3}))
    invalid_batch_ref = store.ref_for_path(old_batch["path"])
    seal["output_refs"] = [
        invalid_batch_ref if ref["path"] == old_batch["path"] else ref
        for ref in seal["output_refs"]
    ]
    seal_path = store.resolve_path(old_seal_ref["path"], must_exist=True)
    seal_path.write_bytes(canonical_json_bytes(seal))
    run["worker"]["seal"] = store.ref_for_path(old_seal_ref["path"])

    with pytest.raises(ContractError) as error:
        _read_worker_seal(store, case_id, run, prepared, manifest)

    assert error.value.code == "output_invalid"


def test_prepare_uses_frozen_data_manifest_after_original_path_changes(core, monkeypatch):
    from test_runner_multi_instance_freeze import _make_candidate, _public_prepare

    from optagent_core import runner as runner_module

    case_id, data_id, candidate = _make_candidate(
        core,
        rows="site_id,site,cost\n1,only,10\n",
        parameter_object_id="sites",
    )
    assert candidate["ok"], candidate
    candidate_id = candidate["result"]["candidate"]["id"]
    original_manifest_path = f".reaslab/optagent/cases/{case_id}/data/{data_id}/manifest.json"
    store = core["store"]()
    original_manifest_ref = store.ref_for_path(original_manifest_path)
    original_manifest_raw = store.read_ref(original_manifest_ref)
    original_manifest = strict_json_loads(original_manifest_raw)
    original_load_data_bundle = runner_module.load_data_bundle
    replaced = False

    def load_then_replace_workspace_bytes(reader, ref_or_manifest, **kwargs):
        nonlocal replaced
        bundle = original_load_data_bundle(reader, ref_or_manifest, **kwargs)
        if (
            not replaced
            and isinstance(ref_or_manifest, dict)
            and ref_or_manifest.get("path", "").startswith(
                f".reaslab/optagent/cases/{case_id}/prepared/"
            )
            and ref_or_manifest.get("path", "").endswith("/input/data/manifest.json")
        ):
            path = reader.resolve_path(original_manifest_path, must_exist=True)
            path.write_bytes(path.read_bytes() + b" ")
            replaced = True
        return bundle

    monkeypatch.setattr(runner_module, "load_data_bundle", load_then_replace_workspace_bytes)
    prepared_reply = _public_prepare(core, case_id, candidate_id)
    assert replaced
    assert prepared_reply["ok"] is True, prepared_reply
    prepared_id = prepared_reply["result"]["prepared"]["id"]
    current = store.read_case(case_id)
    assert current["prepared_ids"] == [prepared_id]
    prepared = store.read_record(f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/prepared.json")
    input_manifest = strict_json_loads(store.read_ref(prepared["input_manifest"]))
    frozen_data = input_manifest["data"]
    assert frozen_data["data_id"] == original_manifest["data_id"]
    assert frozen_data["manifest"]["path"] == (
        f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/input/data/manifest.json"
    )
    frozen_manifest = strict_json_loads(store.read_ref(frozen_data["manifest"]))
    frozen_trace = strict_json_loads(store.read_ref(frozen_manifest["trace"]))
    prepared_source_bytes = {
        item["source_id"]: item["bytes"] for item in input_manifest["sources"]
    }
    assert frozen_trace["source_refs"] == [
        prepared_source_bytes[source_id] for source_id in frozen_manifest["source_ids"]
    ]


def test_public_prepare_freezes_over_four_mib_data_manifest_and_values(core):
    from optagent_core.data import load_data_bundle

    case_id, _, _ = core["create_case"](raw=b"Normalize this explicitly declared large JSON table.")
    source_path = core["workspace"] / "inputs" / "large-table.json"
    source_path.parent.mkdir()
    pointer_token = "p" * 30_000
    payload = "v" * 30_000
    row_count = 150
    source_raw = canonical_json_bytes({
        pointer_token: [
            {"site_id": index, "payload": f"{payload}{index:03d}"}
            for index in range(row_count)
        ]
    })
    source_path.write_bytes(source_raw)

    state = core["store"]().read_case(case_id)
    captured = core["call"]("optagent.data", {
        "op": "capture",
        "case_id": case_id,
        "expected_state_revision": state["state_revision"],
        "path": "inputs/large-table.json",
        "purpose": "data",
        "locations": ["the explicit JSON table used by the bulk prepare regression"],
    })
    assert captured["ok"], captured
    source_id = captured["result"]["source"]["id"]
    plan = {
        "encoding": "utf-8",
        "blocks": [{
            "object_id": "sites",
            "source_id": source_id,
            "format": "json",
            "layout": {"pointer": f"/{pointer_token}"},
            "kind": "table",
            "key_fields": ["site_id"],
            "columns": [
                {"name": "site_id", "position": None, "input_name": "site_id", "type": "integer", "unit": None, "nullable": False},
                {"name": "payload", "position": None, "input_name": "payload", "type": "string", "unit": None, "nullable": False},
            ],
            "duplicate_policy": "error",
            "missing_policy": "error",
        }],
        "conversions": [],
    }
    state = core["store"]().read_case(case_id)
    normalized = core["call"]("optagent.data", {
        "op": "normalize",
        "case_id": case_id,
        "expected_state_revision": state["state_revision"],
        "source_ids": [source_id],
        "plan": plan,
    })
    assert normalized["ok"], normalized
    data_id = normalized["result"]["data_manifest"]["data_id"]

    (core["workspace"] / "model.py").write_text(
        "def build_model(bundle):\n    return None\n",
        encoding="utf-8",
    )
    state = core["store"]().read_case(case_id)
    candidate = core["call"]("optagent.case", {
        "op": "revise",
        "case_id": case_id,
        "expected_state_revision": state["state_revision"],
        "kind": "candidate",
        "model_path": "model.py",
        "local_module_paths": [],
        "data_id": data_id,
        "instance_parameter_object_id": "sites",
        "backend_id": "highs",
        "objective_plan": {"mode": "feasibility", "preference_note": "large exact JSON Ref prepare test"},
        "business_checker_path": None,
        "input_closure": "declared",
        "closure_note": "The table is consumed as typed input data.",
    })
    assert candidate["ok"], candidate
    store = core["store"]()
    original_manifest_path = f".reaslab/optagent/cases/{case_id}/data/{data_id}/manifest.json"
    original_manifest_ref = store.ref_for_path(original_manifest_path)
    original_raw = store.read_ref(original_manifest_ref)
    assert len(source_raw) > 4 * 1024 * 1024
    assert len(original_raw) > 4 * 1024 * 1024
    original_manifest = strict_json_loads(original_raw, max_bytes=len(original_raw))
    assert original_manifest["objects"][0]["values"]["byte_len"] > 4 * 1024 * 1024

    state = store.read_case(case_id)
    prepared_reply = core["call"]("optagent.run", {
        "op": "prepare",
        "case_id": case_id,
        "expected_state_revision": state["state_revision"],
        "candidate_id": candidate["result"]["candidate"]["id"],
        "solve_options": {
            "execution_budget_seconds": 60,
            "time_limit_seconds": 30,
            "relative_gap": 0.0,
            "absolute_gap": 0.0,
            "threads": 1,
            "random_seed": 0,
            "feasibility_tolerance": 1e-7,
            "integrality_tolerance": 1e-7,
            "write_log": False,
            "export_model": False,
        },
    })
    assert prepared_reply["ok"], prepared_reply
    prepared_id = prepared_reply["result"]["prepared"]["id"]
    prepared = store.read_record(f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/prepared.json")
    input_manifest_raw = store.read_ref(prepared["input_manifest"])
    input_manifest = strict_json_loads(input_manifest_raw)
    frozen_ref = input_manifest["data"]["manifest"]
    frozen_raw = store.read_ref(frozen_ref)
    assert len(frozen_raw) > 4 * 1024 * 1024
    frozen_manifest = strict_json_loads(frozen_raw, max_bytes=len(frozen_raw))
    assert frozen_manifest["data_id"] == data_id
    assert len(frozen_manifest["objects"][0]["source_locations"]) == row_count
    assert frozen_ref["byte_len"] == len(frozen_raw)
    bundle = load_data_bundle(
        store,
        frozen_ref,
        candidate_binding_digest=input_manifest["binding_digest"],
        source_records=read_prepared_source_records(store, input_manifest["sources"]),
    )
    assert len(bundle.rows("sites")) == row_count
    assert bundle.object("sites").keys[0] == (0,)
    assert bundle.object("sites").keys[-1] == (row_count - 1,)
