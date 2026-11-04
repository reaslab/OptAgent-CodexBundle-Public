from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

cp_model = pytest.importorskip("ortools.sat.python.cp_model")

from optagent_core.contracts import ContractError, canonical_json_bytes
from optagent_core.models import _model_from_mapping, native_variable_binding_rows
from optagent_core.results import (
    NATIVE_CP_SAT_METHOD,
    NATIVE_CP_SAT_SCOPE,
    check_native_cp_sat_model,
    objective_for_layer,
    objective_values_exactly_match,
    objective_values_match,
    result_record,
)
from optagent_core.solver_protocol import backend_profile_for, run_solver_once
from optagent_core.runner import _validate_native_cp_sat_evidence
from optagent_core.store import CaseStore
from optagent_core.worker import _native_cp_model_textproto_bytes


_EVIDENCE = {
    "model_proto": {"path": "model.pb", "sha256": "a" * 64, "byte_len": 1, "media_type": "application/x-protobuf"},
    "variable_bindings": {"path": "bindings.json", "sha256": "b" * 64, "byte_len": 1, "media_type": "application/json"},
    "response_vector": {"path": "response.jsonl", "sha256": "c" * 64, "byte_len": 1, "media_type": "application/x-ndjson"},
}


def _solve(model, variables, plan):
    native = _model_from_mapping({"model": model, "variables": variables}, backend_id="cp_sat")
    objective = objective_for_layer(native, plan, None)
    observation = run_solver_once(
        native,
        backend_id="cp_sat",
        objective_sense=objective.sense,
        time_limit_seconds=10.0,
        relative_gap=0.0,
        absolute_gap=0.0,
        threads=1,
        random_seed=0,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
        backend_profile=backend_profile_for("cp_sat"),
        warm_start=False,
    )
    checked = check_native_cp_sat_model(
        native,
        observation,
        objective,
        input_digest="d" * 64,
        instance_key=(),
        instance_binding_digest="e" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
        native_evidence=_EVIDENCE,
    )
    return native, objective, observation, checked


def _scalar_plan(sense: str) -> dict[str, str]:
    return {
        "mode": "scalar",
        "objective_component": "cost",
        "sense": sense,
        "unit": "units",
        "preference_note": "test contract",
    }


def test_host_proto_parser_isolated_pool_coexists_with_official_ortools():
    from google.protobuf import descriptor_pool
    from ortools.sat import cp_model_pb2
    from optagent_core.native_cp_proto import cp_model_proto_class

    cp_model.CpModel()  # Materialize the SDK's generated descriptor as a live solve does.
    source_proto = cp_model_pb2.CpModelProto()
    source_proto.variables.add(name="start", domain=[0, 10])
    source_proto.constraints.add().no_overlap.intervals.append(0)
    source_proto.objective.vars.append(0)
    source_proto.objective.coeffs.append(1)
    original = source_proto.SerializeToString(deterministic=True)

    default_pool = descriptor_pool.Default()
    default_file = default_pool.FindFileByName("ortools/sat/cp_model.proto")
    default_message = default_pool.FindMessageTypeByName("operations_research.sat.CpModelProto")
    proto_class = cp_model_proto_class()
    parsed = proto_class()
    parsed.ParseFromString(original)

    assert parsed.SerializeToString(deterministic=True) == original
    assert any(constraint.WhichOneof("constraint") == "no_overlap" for constraint in parsed.constraints)
    assert parsed.DESCRIPTOR.file is not default_file
    assert default_pool.FindFileByName("ortools/sat/cp_model.proto") is default_file
    assert default_pool.FindMessageTypeByName("operations_research.sat.CpModelProto") is default_message


def test_native_cp_sat_export_bytes_are_parseable_textproto():
    model = cp_model.CpModel()
    x = model.NewIntVar(0, 2, "x")
    model.Add(x >= 1)
    model.Minimize(x)
    native = _model_from_mapping({"model": model, "variables": {"x": {(): x}}}, backend_id="cp_sat")

    raw = _native_cp_model_textproto_bytes(native)
    assert b"variables {" in raw

    from google.protobuf import text_format

    parsed = cp_model.CpModel().proto
    text_format.Parse(raw.decode("utf-8"), parsed)
    assert parsed.SerializeToString(deterministic=True) == model.proto.SerializeToString(deterministic=True)


def test_native_variable_bindings_accept_intvar_and_boolvar_aliases():
    model = cp_model.CpModel()
    start = model.NewIntVar(0, 4, "start")
    selected = model.NewBoolVar("selected")

    native = _model_from_mapping(
        {
            "model": model,
            "variables": {
                "start": {("job",): start},
                "selected": {(3,): selected, ("alias",): selected},
            },
        },
        backend_id="cp_sat",
    )

    assert native.variable_bindings == (
        ("start", ("job",), start.Index()),
        ("selected", (3,), selected.Index()),
        ("selected", ("alias",), selected.Index()),
    )
    assert native.variable_bindings[1][2] == native.variable_bindings[2][2]


def test_native_mapping_preserves_cp_solver_value_semantics_for_direct_fallback():
    model = cp_model.CpModel()
    first = model.NewIntVar(3, 3, "first")
    second = model.NewIntVar(7, 7, "second")
    model.Minimize(first + second)

    native, _, observation, checked = _solve(
        model,
        {"first": {(): first}, "second": {(): second}},
        _scalar_plan("min"),
    )

    solver = cp_model.CpSolver()
    assert solver.Solve(model) == cp_model.OPTIMAL
    assert first.Index() == 0 and second.Index() == 1
    assert solver.Value(first) == 3
    assert solver.Value(second) == 7
    assert observation.native_response_solution is not None
    projected = {
        component: observation.native_response_solution[index]
        for component, key, index in native.variable_bindings
        if key == ()
    }
    assert projected == {"first": 3, "second": 7}
    assert solver.Value(first.Index()) == 0
    assert solver.Value(second.Index()) == 1
    assert checked.record["status"] == "pass"


def test_native_variable_bindings_reject_non_variables_foreign_and_stale_handles():
    model = cp_model.CpModel()
    variable = model.NewIntVar(0, 2, "x")
    boolean = model.NewBoolVar("b")

    other_model = cp_model.CpModel()
    foreign = other_model.NewIntVar(0, 2, "x")
    assert foreign.Index() == variable.Index()
    assert foreign.proto == variable.proto
    assert foreign.proto is not model.proto.variables[variable.Index()]

    invalid_values = [
        (0, "OR-Tools IntVar objects"),
        (variable + 1, "OR-Tools IntVar objects"),
        (boolean.Not(), "OR-Tools IntVar objects"),
        (foreign, "not a current variable of the returned CpModel"),
    ]
    for value, message in invalid_values:
        with pytest.raises(ContractError, match=message):
            _model_from_mapping(
                {"model": model, "variables": {"x": {(): value}}},
                backend_id="cp_sat",
            )

    replacement_model = cp_model.CpModel()
    replacement = replacement_model.NewIntVar(0, 2, "x")
    assert replacement.proto == variable.proto
    model.proto.CopyFrom(replacement_model.proto)
    assert variable.proto == model.proto.variables[variable.Index()]
    assert variable.proto is not model.proto.variables[variable.Index()]
    with pytest.raises(ContractError, match="not a current variable of the returned CpModel"):
        _model_from_mapping(
            {"model": model, "variables": {"x": {(): variable}}},
            backend_id="cp_sat",
        )

    model.proto.ClearField("variables")
    with pytest.raises(ContractError, match="outside the returned model"):
        _model_from_mapping(
            {"model": model, "variables": {"x": {(): variable}}},
            backend_id="cp_sat",
        )


def test_native_cp_sat_checks_complete_response_with_aliases_domain_and_offset():
    model = cp_model.CpModel()
    x = model.NewIntVarFromDomain(cp_model.Domain.FromIntervals([[0, 0], [2, 2]]), "x")
    y = model.NewIntVar(0, 2, "unexposed_aux")
    model.Add(x + y == 2)
    model.Minimize(1_000_000_000 + x)

    native, _, observation, checked = _solve(
        model,
        {"start": {("job",): x}, "selected_start": {("job",): x}},
        _scalar_plan("min"),
    )

    assert observation.raw_status == "OPTIMAL"
    assert observation.native_response_solution is not None
    assert len(observation.native_response_solution) == len(native.model.proto.variables) == 2
    assert observation.native_response_solution[0] + observation.native_response_solution[1] == 2
    assert checked.record["status"] == "pass"
    assert checked.record["method"] == NATIVE_CP_SAT_METHOD
    assert checked.record["scope"] == NATIVE_CP_SAT_SCOPE
    assert checked.record["variable_coverage"] == {
        "required": 2,
        "present": 2,
        "completed": 2,
        "missing_count": 0,
    }
    assert [row["component"] for row in checked.witness_variables] == ["start", "selected_start"]
    assert [row["value"] for row in checked.witness_variables] == [0.0, 0.0]
    assert checked.objective_values == ({"component": "cost", "sense": "min", "unit": "units", "value": 1_000_000_000.0},)


def test_native_cp_sat_maximum_uses_proto_sign_and_nonzero_offset():
    model = cp_model.CpModel()
    x = model.NewIntVar(0, 3, "x")
    model.Add(x <= 2)
    model.Maximize(7 + 2 * x)

    _, objective, observation, checked = _solve(model, {"level": {(): x}}, _scalar_plan("max"))

    assert objective.sense == "max"
    assert observation.raw_status == "OPTIMAL"
    assert checked.record["status"] == "pass"
    assert checked.objective_values[0]["value"] == 11.0


def test_native_mapped_domain_endpoints_respect_exact_witness_precision():
    model = cp_model.CpModel()
    wide_aux = model.NewIntVar(-(1 << 60), 1 << 60, "wide_aux")
    decision = model.NewIntVar(0, 1, "decision")
    model.Minimize(decision)

    auxiliary_only = _model_from_mapping(
        {"model": model, "variables": {"decision": {(): decision}}},
        backend_id="cp_sat",
    )
    assert objective_for_layer(auxiliary_only, _scalar_plan("min"), None).sense == "min"

    mapped_wide_domain = _model_from_mapping(
        {
            "model": model,
            "variables": {
                "decision": {(): decision},
                "wide_business_variable": {(): wide_aux},
            },
        },
        backend_id="cp_sat",
    )
    with pytest.raises(ContractError, match="mapped native variable domain exceeds exact Witness precision"):
        objective_for_layer(mapped_wide_domain, _scalar_plan("min"), None)


def test_native_integer_objective_mismatch_of_one_is_not_hidden_by_float_tolerance():
    model = cp_model.CpModel()
    x = model.NewIntVar(0, 1, "x")
    model.Minimize(1_000_000_000 + x)
    native = _model_from_mapping({"model": model, "variables": {"x": {(): x}}}, backend_id="cp_sat")
    objective = objective_for_layer(native, _scalar_plan("min"), None)
    observation = run_solver_once(
        native,
        backend_id="cp_sat",
        objective_sense="min",
        time_limit_seconds=10.0,
        relative_gap=0.0,
        absolute_gap=0.0,
        threads=1,
        random_seed=0,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
        backend_profile=backend_profile_for("cp_sat"),
    )
    record = dict(observation.record)
    record["best_feasible_objective"] += 1
    inconsistent = replace(observation, record=record, solver_incumbent_objective=observation.solver_incumbent_objective + 1)
    checked = check_native_cp_sat_model(
        native,
        inconsistent,
        objective,
        input_digest="d" * 64,
        instance_key=(),
        instance_binding_digest="e" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
        native_evidence=_EVIDENCE,
    )
    left = [{"component": "cost", "sense": "min", "unit": "units", "value": 1_000_000_000.0}]
    right = [{"component": "cost", "sense": "min", "unit": "units", "value": 1_000_000_001.0}]
    assert objective_values_match(left, right)
    assert not objective_values_exactly_match(left, right)
    assert checked.record["status"] == "fail"
    assert checked.witness_variables is None


def test_native_unknown_response_does_not_create_a_point_or_accept_bound(monkeypatch):
    model = cp_model.CpModel()
    x = model.NewIntVar(0, 1, "x")
    model.Minimize(x)
    native = _model_from_mapping({"model": model, "variables": {"x": {(): x}}}, backend_id="cp_sat")

    class FakeSolver:
        def __init__(self):
            self.parameters = SimpleNamespace()
            self.response_proto = SimpleNamespace(solution=[], objective_value=0.0, best_objective_bound=0.0)

        def Solve(self, actual_model):
            assert actual_model is model
            return cp_model.UNKNOWN

        @staticmethod
        def StatusName(_status):
            return "UNKNOWN"

    monkeypatch.setattr(cp_model, "CpSolver", FakeSolver)
    observation = run_solver_once(
        native,
        backend_id="cp_sat",
        objective_sense="min",
        time_limit_seconds=1.0,
        relative_gap=0.0,
        absolute_gap=0.0,
        threads=1,
        random_seed=0,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
        backend_profile=backend_profile_for("cp_sat"),
    )
    checked = check_native_cp_sat_model(
        native,
        observation,
        objective_for_layer(native, _scalar_plan("min"), None),
        input_digest="d" * 64,
        instance_key=(),
        instance_binding_digest="e" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
        native_evidence=_EVIDENCE,
    )

    assert observation.raw_status == "UNKNOWN"
    assert observation.native_response_solution is None
    assert observation.bound_value is None
    assert checked.record["status"] == "uncovered"
    assert checked.record["uncovered"] == ["no_solver_feasible_point"]


def test_native_envelope_rejects_builder_self_reported_bound():
    model = cp_model.CpModel()
    variable = model.NewIntVar(0, 1, "x")
    model.Minimize(variable)
    with pytest.raises(ContractError, match="native model envelope"):
        _model_from_mapping(
            {"model": model, "variables": {"x": {(): variable}}, "bound": 0},
            backend_id="cp_sat",
        )


def test_runner_replays_sealed_native_proto_vector_and_alias_projection(tmp_path):
    model = cp_model.CpModel()
    x = model.NewIntVar(0, 1, "x")
    model.Minimize(1_000_000_000 + x)
    proto_bytes = model.proto.SerializeToString(deterministic=True)
    binding_bytes = canonical_json_bytes({
        "variables": [
            {"component": "x", "key": [], "index": x.Index()},
            {"component": "selected", "key": [], "index": x.Index()},
        ],
    })
    vector_bytes = b'{"index":0,"value":0}\n'
    store = CaseStore(tmp_path)
    root = ".reaslab/optagent/cases/c/runs/r/output"
    model_ref = store.write_immutable(f"{root}/model.pb", proto_bytes, "application/x-protobuf")
    binding_ref = store.write_immutable(f"{root}/bindings.json", binding_bytes, "application/json")
    vector_ref = store.write_immutable(f"{root}/response.jsonl", vector_bytes, "application/x-ndjson")
    evidence = {"model_proto": model_ref, "variable_bindings": binding_ref, "response_vector": vector_ref}
    check_record = {
        "method": NATIVE_CP_SAT_METHOD,
        "scope": NATIVE_CP_SAT_SCOPE,
        "native_evidence": evidence,
        "status": "pass",
        "variable_coverage": {"required": 1, "present": 1, "completed": 1, "missing_count": 0},
        "component_coverage": [],
        "objective_values": [{"component": "cost", "sense": "min", "unit": "units", "value": 1_000_000_000.0}],
    }
    witness = {
        "variables": [
            {"component": "x", "key": [], "domain": "binary", "value": 0.0, "origin": "solver_primal"},
            {"component": "selected", "key": [], "domain": "binary", "value": 0.0, "origin": "solver_primal"},
        ],
        "objective_values": check_record["objective_values"],
    }
    result = {
        "backend": {"id": "cp_sat"},
        "layer": None,
        "objective": check_record["objective_values"][0],
        "has_checked_candidate": True,
        "has_solver_incumbent": True,
    }
    observation = {
        "raw_status": "OPTIMAL",
        "solver_invoked": True,
        "solver_returned": True,
        "best_feasible_objective": 1_000_000_000.0,
    }
    ref_map = {ref["path"]: ref for ref in (model_ref, binding_ref, vector_ref)}

    _validate_native_cp_sat_evidence(
        store,
        output_root=root,
        output_ref_map=ref_map,
        evidence=evidence,
        raw_primals_ref=vector_ref,
        check_record=check_record,
        witness_record=witness,
        result=result,
        observation=observation,
        objective_plan=_scalar_plan("min"),
    )

    bad_vector = store.write_immutable(f"{root}/bad-response.jsonl", b'{"index":0,"value":1}\n', "application/x-ndjson")
    bad_evidence = {**evidence, "response_vector": bad_vector}
    with pytest.raises(ContractError, match="objective evidence row counts differ"):
        _validate_native_cp_sat_evidence(
            store,
            output_root=root,
            output_ref_map={**ref_map, bad_vector["path"]: bad_vector},
            evidence=bad_evidence,
            raw_primals_ref=bad_vector,
            check_record=check_record,
            witness_record=witness,
            result=result,
            observation=observation,
            objective_plan=_scalar_plan("min"),
        )


def test_native_cp_sat_add_element_seals_real_point_typed_projection_and_bound(tmp_path):
    model = cp_model.CpModel()
    choice = model.NewIntVar(0, 2, "choice")
    score = model.NewIntVar(0, 9, "score")
    auxiliary = model.NewIntVar(0, 1, "unmapped_auxiliary")
    model.AddElement(choice, [9, 4, 7], score)
    model.Add(auxiliary == 1)
    model.Minimize(1_000 + score)

    assert any(constraint.WhichOneof("constraint") == "element" for constraint in model.proto.constraints)
    native, objective, observation, checked = _solve(
        model,
        {
            "choice": {(): choice},
            "choice_alias": {("business",): choice},
            "score": {(): score},
        },
        _scalar_plan("min"),
    )

    assert observation.raw_status == "OPTIMAL"
    assert observation.bound_value == 1_004.0
    assert observation.record["best_objective_bound"] == 1_004.0
    assert observation.record["bound_evidence"]["value_direction"] == "lower"
    assert observation.native_response_solution is not None
    assert observation.native_response_solution[choice.Index()] == 1
    assert observation.native_response_solution[score.Index()] == 4
    assert observation.native_response_solution[auxiliary.Index()] == 1
    assert len(observation.native_response_solution) == len(native.model.proto.variables) == 3
    assert checked.record["status"] == "pass"
    assert [row["component"] for row in checked.witness_variables] == ["choice", "choice_alias", "score"]
    assert [row["value"] for row in checked.witness_variables] == [1.0, 1.0, 4.0]
    assert checked.objective_values == ({"component": "cost", "sense": "min", "unit": "units", "value": 1_004.0},)

    store = CaseStore(tmp_path)
    output_root = ".reaslab/optagent/cases/c/runs/r/output"
    model_ref = store.write_immutable(
        f"{output_root}/native-model.pb",
        observation.native_model_proto,
        "application/x-protobuf",
    )
    binding_ref = store.write_immutable(
        f"{output_root}/native-bindings.json",
        canonical_json_bytes({"variables": native_variable_binding_rows(native)}),
        "application/json",
    )
    vector_bytes = b"".join(
        canonical_json_bytes({"index": index, "value": value}) + b"\n"
        for index, value in enumerate(observation.native_response_solution)
    )
    vector_ref = store.write_immutable(
        f"{output_root}/native-response.jsonl",
        vector_bytes,
        "application/x-ndjson",
    )
    evidence = {"model_proto": model_ref, "variable_bindings": binding_ref, "response_vector": vector_ref}
    backend_ref = store.write_immutable(
        f"{output_root}/backend-observation.json",
        canonical_json_bytes({
            "observation": observation.record,
            "backend": observation.backend,
            "nonfinite_fields": observation.nonfinite_fields,
            "raw_primals": vector_ref,
            "phase_seconds": {"build": 0.0, "solve": observation.solve_wall_seconds},
        }),
        "application/json",
    )
    check_record = {**checked.record, "native_evidence": evidence, "violations": None}
    witness = {
        "variables": [dict(row) for row in checked.witness_variables],
        "objective_values": [dict(row) for row in checked.objective_values],
    }
    result = result_record(
        observation,
        checked,
        objective,
        instance_key=(),
        instance_binding_digest="e" * 64,
        source_revision=1,
        candidate_revision=1,
        input_digest="d" * 64,
        layer=None,
        bound_evidence_ref=backend_ref,
        core_gap_target={"relative_gap": 0.0, "absolute_gap": 0.0},
        backend_profile=observation.backend,
    )
    assert result["has_solver_incumbent"] is True
    assert result["bound"]["value"] == 1_004.0
    assert result["bound"]["direction"] == "lower"

    _validate_native_cp_sat_evidence(
        store,
        output_root=output_root,
        output_ref_map={ref["path"]: ref for ref in (model_ref, binding_ref, vector_ref, backend_ref)},
        evidence=evidence,
        raw_primals_ref=vector_ref,
        check_record=check_record,
        witness_record=witness,
        result=result,
        observation=observation.record,
        objective_plan=_scalar_plan("min"),
    )
