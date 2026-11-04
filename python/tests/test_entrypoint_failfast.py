from __future__ import annotations

import builtins
import importlib
import sys
import types
from pathlib import Path
from typing import Any, Mapping

import pytest

from optagent_core.contracts import ContractError, ref_from_bytes, strict_json_loads
from optagent_core.data import load_data_bundle
from optagent_core import models as models_module
from optagent_core import worker as worker_module
from optagent_core.worker import _OutputTarget, _run_direct_instances, _run_instances


_INPUT_DIGEST = "a" * 64
_BINDING_DIGEST = "b" * 64
_STATE_NAME = "_optagent_entrypoint_failfast_test_state"


class MemoryReader:
    def __init__(self, values: Mapping[str, bytes]):
        self.values = dict(values)

    def read_ref(self, ref: Mapping[str, Any]) -> bytes:
        raw = self.values[ref["path"]]
        assert len(raw) == ref["byte_len"]
        return raw


def _code_spec(path: str, source: str) -> tuple[dict[str, Any], dict[str, bytes]]:
    raw = source.encode("utf-8")
    ref = ref_from_bytes(
        f".reaslab/optagent/cases/frozen/{path}",
        raw,
        "text/x-python; charset=utf-8",
    )
    return {"original_path": path, "bytes": ref}, {ref["path"]: raw}


def _state(monkeypatch):
    value = {
        "compute_calls": 0,
        "model_imports": 0,
        "extract_calls": 0,
        "checker_imports": 0,
        "checker_calls": 0,
        "events": [],
    }
    monkeypatch.setattr(builtins, _STATE_NAME, value, raising=False)
    return value


def _manifest(model_spec, *, authoring: str, backend_id: str, business_checker):
    return {
        "schema_version": 3,
        "input_digest": _INPUT_DIGEST,
        "source_revision": 1,
        "candidate_revision": 2,
        "candidate": {
            "authoring": authoring,
            "model": model_spec,
            "local_modules": [],
            "business_checker": business_checker,
            "backend_id": backend_id,
            "objective_plan": {"mode": "feasibility", "preference_note": "entrypoint test"},
        },
        "data": None,
        "solve_options": {"execution_budget_seconds": 60, "time_limit_seconds": 30},
    }


def _run_direct(reader, model_spec, checker_spec, tmp_path):
    bundle = load_data_bundle(reader, None, candidate_binding_digest=_BINDING_DIGEST)
    output = _OutputTarget.create_local(tmp_path / "direct-output")
    records, refs, _consumed, _solver_invoked, _solver_returned = _run_direct_instances(
        reader,
        output,
        _manifest(
            model_spec,
            authoring="direct_python",
            backend_id="direct_python",
            business_checker=checker_spec,
        ),
        input_digest=_INPUT_DIGEST,
        data_bundle=bundle,
        base_consumed_inputs=(),
        execution_started=0.0,
        backend_profile={"id": "direct_python", "version": "test-only"},
        enforce_execution_budget=False,
    )
    return output, records, refs


def _run_native_build(reader, model_spec, checker_spec, tmp_path, *, backend_id="cp_sat"):
    bundle = load_data_bundle(reader, None, candidate_binding_digest=_BINDING_DIGEST)
    output = _OutputTarget.create_local(tmp_path / "native-output")
    return _run_instances(
        reader,
        output,
        _manifest(
            model_spec,
            authoring="pyomo",
            backend_id=backend_id,
            business_checker=checker_spec,
        ),
        (),
        input_digest=_INPUT_DIGEST,
        data_bundle=bundle,
        base_consumed_inputs=(),
        execution_started=0.0,
        backend_profile={"id": backend_id, "version": "test-only"},
        enforce_execution_budget=False,
    )


def _model_source(entry: str, extractor: str) -> str:
    source = (
        "import builtins\n"
        f"builtins.{_STATE_NAME}['model_imports'] += 1\n"
        f"def {entry}(bundle):\n"
        f"    builtins.{_STATE_NAME}['compute_calls'] += 1\n"
        "    raise AssertionError('compute body must not run for an invalid extractor entry')\n"
    )
    if extractor == "noncallable":
        source += "extract_solution = 0\n"
    elif extractor == "bad_signature":
        source += "def extract_solution(bundle):\n    raise AssertionError('extractor body must not run')\n"
    elif extractor == "valid":
        source += "def extract_solution(bundle, witness):\n    return {}\n"
    return source


@pytest.mark.parametrize("entry_kind", ["direct", "native"])
@pytest.mark.parametrize("extractor", ["missing", "noncallable", "bad_signature"])
def test_required_extract_entry_fails_before_direct_or_native_compute(
    tmp_path: Path, monkeypatch, entry_kind: str, extractor: str,
):
    state = _state(monkeypatch)
    entry = "solve" if entry_kind == "direct" else "build_model"
    model, model_refs = _code_spec("model.py", _model_source(entry, extractor))
    checker = {"original_path": "checker.py"}
    reader = MemoryReader(model_refs)
    source_prefix = f"_optagent_candidate_{model['bytes']['sha256'][:20]}"

    with pytest.raises(ContractError) as error:
        if entry_kind == "direct":
            _run_direct(reader, model, checker, tmp_path)
        else:
            _run_native_build(reader, model, checker, tmp_path)

    assert error.value.code == "invalid_argument"
    if extractor == "bad_signature":
        assert error.value.message == "extract_solution has an incompatible signature"
    else:
        assert error.value.message == "frozen module must define extract_solution(...)"
    assert state["compute_calls"] == 0
    assert state["model_imports"] == 1
    assert not any(name == source_prefix or name.startswith(source_prefix + ".") for name in sys.modules)
    output_root = tmp_path / ("direct-output" if entry_kind == "direct" else "native-output")
    assert not [path for path in output_root.rglob("*") if path.is_file()]


@pytest.mark.parametrize("extractor", ["missing", "bad_signature"])
def test_required_extract_entry_fails_before_non_cp_native_build(
    tmp_path: Path, monkeypatch, extractor: str,
):
    state = _state(monkeypatch)
    model, model_refs = _code_spec("model.py", _model_source("build_model", extractor))
    reader = MemoryReader(model_refs)

    with pytest.raises(ContractError) as error:
        _run_native_build(reader, model, {"original_path": "checker.py"}, tmp_path, backend_id="highs")

    assert error.value.code == "invalid_argument"
    if extractor == "bad_signature":
        assert error.value.message == "extract_solution has an incompatible signature"
    else:
        assert error.value.message == "frozen module must define extract_solution(...)"
    assert state["compute_calls"] == 0
    assert state["model_imports"] == 1
    assert not [path for path in (tmp_path / "native-output").rglob("*") if path.is_file()]


def test_valid_extract_precheck_keeps_direct_witness_and_checker_chain(monkeypatch, tmp_path: Path):
    state = _state(monkeypatch)
    model_source = (
        "import builtins\n"
        f"builtins.{_STATE_NAME}['model_imports'] += 1\n"
        f"def solve(bundle):\n"
        f"    builtins.{_STATE_NAME}['compute_calls'] += 1\n"
        f"    builtins.{_STATE_NAME}['events'].append('solve')\n"
        "    return {'variables': [], 'objective_values': []}\n"
        "def extract_solution(bundle, witness):\n"
        f"    builtins.{_STATE_NAME}['extract_calls'] += 1\n"
        f"    builtins.{_STATE_NAME}['events'].append('extract')\n"
        "    assert witness['variables'] == []\n"
        "    return {'quantity': 4}\n"
    )
    checker_source = (
        "import builtins\n"
        f"builtins.{_STATE_NAME}['checker_imports'] += 1\n"
        "def check_solution(bundle, solution):\n"
        f"    builtins.{_STATE_NAME}['checker_calls'] += 1\n"
        f"    builtins.{_STATE_NAME}['events'].append('check')\n"
        "    assert solution == {'quantity': 4}\n"
        "    return {'status': 'pass', 'checked_obligations': [], 'objective_values': [], 'violations': [], 'uncovered': []}\n"
    )
    model, model_refs = _code_spec("model.py", model_source)
    checker, checker_refs = _code_spec("checker.py", checker_source)
    reader = MemoryReader({**model_refs, **checker_refs})
    model_prefix = f"_optagent_candidate_{model['bytes']['sha256'][:20]}"
    checker_prefix = f"_optagent_candidate_{checker['bytes']['sha256'][:20]}"
    imported_candidate_modules = []
    real_import_module = importlib.import_module

    def tracked_import(name, package=None):
        if name.startswith(model_prefix) or name.startswith(checker_prefix):
            imported_candidate_modules.append(name)
        return real_import_module(name, package)

    monkeypatch.setattr(models_module.importlib, "import_module", tracked_import)
    output, records, _refs = _run_direct(reader, model, checker, tmp_path)

    result = strict_json_loads(output.path(records[0]["result"]["path"]).read_bytes())
    witness = strict_json_loads(output.path(records[0]["witness"]["path"]).read_bytes())
    business = strict_json_loads(output.path(records[0]["business_check"]["path"]).read_bytes())
    assert result["termination"] == "feasible"
    assert result["solver_invoked"] is False
    assert witness["variables"] == []
    assert business["status"] == "pass"
    assert business["solution"] == {"quantity": 4}
    assert state["compute_calls"] == 1
    assert state["extract_calls"] == 1
    assert state["model_imports"] == 2
    assert state["checker_imports"] == 1
    assert state["checker_calls"] == 1
    assert state["events"] == ["solve", "extract", "check"]
    assert imported_candidate_modules == [
        f"{model_prefix}.model",
        f"{model_prefix}.model",
        f"{checker_prefix}.checker",
    ]
    assert not any(name == model_prefix or name.startswith(model_prefix + ".") for name in sys.modules)
    assert not any(name == checker_prefix or name.startswith(checker_prefix + ".") for name in sys.modules)


def test_direct_candidate_without_checker_does_not_require_extractor(monkeypatch, tmp_path: Path):
    state = _state(monkeypatch)
    source = (
        "import builtins\n"
        f"builtins.{_STATE_NAME}['model_imports'] += 1\n"
        "def solve(bundle):\n"
        f"    builtins.{_STATE_NAME}['compute_calls'] += 1\n"
        "    return {'variables': [], 'objective_values': []}\n"
    )
    model, model_refs = _code_spec("model.py", source)
    reader = MemoryReader(model_refs)
    output, records, _refs = _run_direct(reader, model, None, tmp_path)

    result = strict_json_loads(output.path(records[0]["result"]["path"]).read_bytes())
    business = strict_json_loads(output.path(records[0]["business_check"]["path"]).read_bytes())
    assert state["compute_calls"] == 1
    assert state["model_imports"] == 1
    assert records[0]["witness"] is not None
    assert result["termination"] == "unknown"
    assert business["status"] == "uncovered"
    assert business["uncovered"] == ["business_checker_not_declared"]


def test_native_candidate_without_checker_does_not_require_extractor(monkeypatch, tmp_path: Path):
    state = _state(monkeypatch)

    class FakeCpModel:
        def __init__(self):
            self.proto = types.SimpleNamespace(variables=[])

    fake_cp_model = types.SimpleNamespace(CpModel=FakeCpModel)
    real_import_module = importlib.import_module
    pyomo_package = types.ModuleType("pyomo")
    pyomo_package.__path__ = []
    pyomo_environ = types.ModuleType("pyomo.environ")
    pyomo_environ.ConcreteModel = type("NonNativePyomoModel", (), {})
    monkeypatch.setitem(sys.modules, "pyomo", pyomo_package)
    monkeypatch.setitem(sys.modules, "pyomo.environ", pyomo_environ)

    def import_with_fake_cp_model(name, package=None):
        if name == "ortools.sat.python.cp_model":
            return fake_cp_model
        return real_import_module(name, package)

    monkeypatch.setattr(models_module.importlib, "import_module", import_with_fake_cp_model)
    monkeypatch.setattr(
        builtins,
        "_optagent_entrypoint_failfast_fake_cp_model",
        FakeCpModel(),
        raising=False,
    )
    source = (
        "import builtins\n"
        f"builtins.{_STATE_NAME}['model_imports'] += 1\n"
        "def build_model(bundle):\n"
        f"    builtins.{_STATE_NAME}['compute_calls'] += 1\n"
        "    return {'model': builtins._optagent_entrypoint_failfast_fake_cp_model, 'variables': {}}\n"
    )
    model, model_refs = _code_spec("model.py", source)
    reader = MemoryReader(model_refs)

    class StopAfterNativeBuild(Exception):
        pass

    monkeypatch.setattr(worker_module, "objective_for_layer", lambda *_args: (_ for _ in ()).throw(StopAfterNativeBuild()))
    with pytest.raises(StopAfterNativeBuild):
        _run_native_build(reader, model, None, tmp_path)

    assert state["compute_calls"] == 1
    assert state["model_imports"] == 1
    output_root = tmp_path / "native-output"
    assert not [path for path in output_root.rglob("*") if path.is_file()]


def test_compatible_extractor_body_error_remains_late_business_uncovered(monkeypatch, tmp_path: Path):
    state = _state(monkeypatch)
    model_source = (
        "import builtins\n"
        f"builtins.{_STATE_NAME}['model_imports'] += 1\n"
        "def solve(bundle):\n"
        f"    builtins.{_STATE_NAME}['compute_calls'] += 1\n"
        f"    builtins.{_STATE_NAME}['events'].append('solve')\n"
        "    return {'variables': [], 'objective_values': []}\n"
        "def extract_solution(bundle, witness):\n"
        f"    builtins.{_STATE_NAME}['extract_calls'] += 1\n"
        f"    builtins.{_STATE_NAME}['events'].append('extract')\n"
        "    raise RuntimeError('extractor body failure')\n"
    )
    checker_source = (
        "import builtins\n"
        f"builtins.{_STATE_NAME}['checker_imports'] += 1\n"
        "def check_solution(bundle, solution):\n"
        f"    builtins.{_STATE_NAME}['checker_calls'] += 1\n"
        "    return {'status': 'pass', 'checked_obligations': [], 'objective_values': [], 'violations': [], 'uncovered': []}\n"
    )
    model, model_refs = _code_spec("model.py", model_source)
    checker, checker_refs = _code_spec("checker.py", checker_source)
    reader = MemoryReader({**model_refs, **checker_refs})
    output, records, _refs = _run_direct(reader, model, checker, tmp_path)

    result = strict_json_loads(output.path(records[0]["result"]["path"]).read_bytes())
    business = strict_json_loads(output.path(records[0]["business_check"]["path"]).read_bytes())
    assert state["events"] == ["solve", "extract"]
    assert state["compute_calls"] == 1
    assert state["extract_calls"] == 1
    assert state["checker_calls"] == 0
    assert records[0]["witness"] is not None
    assert result["termination"] == "unknown"
    assert business["status"] == "uncovered"
    assert business["uncovered"] == ["business_solution_extraction_error"]
    assert any("business_solution_extraction_error" in item for item in result["warnings"])
