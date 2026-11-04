from __future__ import annotations

import sys
from typing import Any, Mapping

import pytest

from optagent_core.contracts import ContractError, ref_from_bytes
from optagent_core.models import _model_from_mapping, build_native_model, call_frozen_function
from optagent_core.results import ObjectiveSpec, check_pyomo_model


class MemoryReader:
    def __init__(self, values: Mapping[str, bytes]):
        self.values = dict(values)

    def read_ref(self, ref: Mapping[str, Any]) -> bytes:
        raw = self.values[ref["path"]]
        assert len(raw) == ref["byte_len"]
        return raw


def _code_spec(path: str, raw: bytes) -> tuple[dict[str, Any], dict[str, bytes]]:
    ref = ref_from_bytes(f".reaslab/optagent/cases/frozen/{path}", raw, "text/x-python; charset=utf-8")
    return {"original_path": path, "bytes": ref}, {ref["path"]: raw}


def test_declared_relative_helper_builds_a_native_concrete_model():
    pyo = pytest.importorskip("pyomo.environ")
    builder, builder_bytes = _code_spec(
        "models/build.py",
        b"from .helper import lower_bound\n"
        b"from pyomo.environ import ConcreteModel, Objective, Var, minimize\n"
        b"def build_model(bundle):\n"
        b"    m = ConcreteModel()\n"
        b"    m.x = Var(bounds=(lower_bound(), 4), initialize=3)\n"
        b"    m.cost = Objective(expr=m.x, sense=minimize)\n"
        b"    return m\n",
    )
    helper, helper_bytes = _code_spec("models/helper.py", b"def lower_bound():\n    return 2\n")
    reader = MemoryReader({**builder_bytes, **helper_bytes})

    model, consumed = build_native_model(reader, object(), builder, [helper])

    assert isinstance(model, pyo.ConcreteModel)
    assert model.x.lb == 2
    assert model.x.ub == 4
    assert {item["path"] for item in consumed} == {builder["bytes"]["path"], helper["bytes"]["path"]}


def test_undeclared_relative_helper_is_rejected_before_model_is_returned():
    builder, builder_bytes = _code_spec(
        "models/build.py",
        b"from .hidden import VALUE\n"
        b"def build_model(bundle):\n"
        b"    return VALUE\n",
    )
    reader = MemoryReader(builder_bytes)

    with pytest.raises(ContractError) as error:
        call_frozen_function(
            reader,
            builder,
            [],
            function_name="build_model",
            args=(object(),),
            pointer="/candidate/model",
        )

    assert error.value.code == "input_closure_unknown"


@pytest.mark.parametrize(
    "directory",
    ["model-dir", "model dir", "model.part", "模型目录", "my:folder", "folder/dir:name"],
)
def test_frozen_import_accepts_nonidentifier_parent_directories_and_keeps_original_paths(directory):
    builder, builder_bytes = _code_spec(
        f"{directory}/model.py",
        b"from .helper import HELPER_FILE, VALUE\n"
        b"def build_model(bundle):\n"
        b"    return VALUE, __file__, HELPER_FILE\n",
    )
    helper, helper_bytes = _code_spec(
        f"{directory}/helper.py",
        b"VALUE = 17\nHELPER_FILE = __file__\n",
    )
    prefix = f"_optagent_candidate_{builder['bytes']['sha256'][:20]}"
    reader = MemoryReader({**builder_bytes, **helper_bytes})

    result, consumed = call_frozen_function(
        reader,
        builder,
        [helper],
        function_name="build_model",
        args=(object(),),
        pointer="/candidate/model",
    )

    assert result == (
        17,
        f"<frozen-candidate>/{directory}/model.py",
        f"<frozen-candidate>/{directory}/helper.py",
    )
    assert {item["path"] for item in consumed} == {
        builder["bytes"]["path"], helper["bytes"]["path"]
    }
    assert not any(name == prefix or name.startswith(prefix + ".") for name in sys.modules)


def test_frozen_import_keeps_nested_packages_and_collision_prone_directories_distinct():
    builder, builder_bytes = _code_spec(
        "entry.py",
        b"from importlib import import_module\n"
        b"def build_model(bundle):\n"
        b"    names = ('mode-dir.helper', 'mode_dir.helper', 'a%2eb.helper', 'a%252eb.helper')\n"
        b"    return tuple(import_module(f'{__package__}.{name}').VALUE for name in names)\n",
    )
    helper_specs = []
    helper_bytes = {}
    for directory, value in (
        ("mode-dir", 11),
        ("mode_dir", 22),
        ("a.b", 33),
        ("a%2eb", 44),
    ):
        spec, values = _code_spec(f"{directory}/helper.py", f"VALUE = {value}\n".encode())
        helper_specs.append(spec)
        helper_bytes.update(values)
    nested_entry, nested_entry_bytes = _code_spec(
        "top.area/pkg.v1/__init__.py",
        b"from .helper import VALUE, HELPER_FILE\n"
        b"from .nested.subpkg import OTHER\n"
        b"def build_model(bundle):\n"
        b"    return VALUE, OTHER, __file__, HELPER_FILE\n",
    )
    nested_helper, nested_helper_bytes = _code_spec(
        "top.area/pkg.v1/helper.py",
        b"VALUE = 5\nHELPER_FILE = __file__\n",
    )
    nested_package, nested_package_bytes = _code_spec(
        "top.area/pkg.v1/nested/subpkg/__init__.py",
        b"from .values import OTHER\n",
    )
    nested_values, nested_values_bytes = _code_spec(
        "top.area/pkg.v1/nested/subpkg/values.py",
        b"OTHER = 8\n",
    )
    all_bytes = {
        **builder_bytes,
        **helper_bytes,
        **nested_entry_bytes,
        **nested_helper_bytes,
        **nested_package_bytes,
        **nested_values_bytes,
    }
    reader = MemoryReader(all_bytes)
    prefix = f"_optagent_candidate_{builder['bytes']['sha256'][:20]}"
    nested_prefix = f"_optagent_candidate_{nested_entry['bytes']['sha256'][:20]}"

    collision_values, collision_consumed = call_frozen_function(
        reader,
        builder,
        helper_specs + [nested_entry, nested_helper, nested_package, nested_values],
        function_name="build_model",
        args=(object(),),
        pointer="/candidate/model",
    )
    nested_values_result, nested_consumed = call_frozen_function(
        reader,
        nested_entry,
        [builder, *helper_specs, nested_helper, nested_package, nested_values],
        function_name="build_model",
        args=(object(),),
        pointer="/candidate/model",
    )

    assert collision_values == (11, 22, 33, 44)
    assert nested_values_result == (
        5,
        8,
        "<frozen-candidate>/top.area/pkg.v1/__init__.py",
        "<frozen-candidate>/top.area/pkg.v1/helper.py",
    )
    assert {item["path"] for item in collision_consumed} == {
        builder["bytes"]["path"], *(item["bytes"]["path"] for item in helper_specs)
    }
    assert {item["path"] for item in nested_consumed} == {
        nested_entry["bytes"]["path"],
        nested_helper["bytes"]["path"],
        nested_package["bytes"]["path"],
        nested_values["bytes"]["path"],
    }
    assert not any(name == prefix or name.startswith(prefix + ".") for name in sys.modules)
    assert not any(name == nested_prefix or name.startswith(nested_prefix + ".") for name in sys.modules)


def test_frozen_import_failure_cleans_candidate_namespace_and_keeps_module_stem_contract():
    builder, builder_bytes = _code_spec(
        "pkg/build.py",
        b"from .missing import VALUE\n"
        b"def build_model(bundle):\n"
        b"    return VALUE\n",
    )
    prefix = f"_optagent_candidate_{builder['bytes']['sha256'][:20]}"

    with pytest.raises(ContractError) as error:
        call_frozen_function(
            MemoryReader(builder_bytes),
            builder,
            [],
            function_name="build_model",
            args=(object(),),
            pointer="/candidate/model",
        )

    assert error.value.code == "input_closure_unknown"
    assert not any(name == prefix or name.startswith(prefix + ".") for name in sys.modules)
    invalid_stem, _ = _code_spec("model-dir/not-a-module.py", b"def build_model(bundle):\n    return None\n")
    with pytest.raises(ContractError) as invalid:
        call_frozen_function(
            MemoryReader({}),
            invalid_stem,
            [],
            function_name="build_model",
            args=(object(),),
            pointer="/candidate/model",
        )
    assert invalid.value.code == "invalid_argument"


@pytest.mark.parametrize("path", ["C:/code/model.py", r"C:\code\model.py"])
def test_frozen_code_paths_reject_windows_drive_prefixes(path):
    candidate, _ = _code_spec(path, b"def build_model(bundle):\n    return None\n")

    with pytest.raises(ContractError) as error:
        call_frozen_function(
            MemoryReader({}),
            candidate,
            [],
            function_name="build_model",
            args=(object(),),
            pointer="/candidate/model",
        )

    assert error.value.code == "path_denied"


def _check_model_without_raw_primal(model):
    return check_pyomo_model(
        model,
        {},
        ObjectiveSpec(None, None, None, None, None),
        input_digest="a" * 64,
        instance_key=[],
        instance_binding_digest="b" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
    )


def test_active_sos1_is_rejected_by_builder_and_direct_checker():
    pyo = pytest.importorskip("pyomo.environ")
    model = pyo.ConcreteModel()
    model.x = pyo.Var([1, 2], domain=pyo.NonNegativeReals)
    model.x[1].fix(1)
    model.x[2].fix(1)
    model.sos = pyo.SOSConstraint(var=model.x, sos=1)

    for operation in (lambda: _model_from_mapping(model), lambda: _check_model_without_raw_primal(model)):
        with pytest.raises(ContractError) as error:
            operation()
        assert error.value.code == "capability_uncovered"
        assert error.value.details["component"] == "sos"


def test_active_sos_nested_in_active_block_is_rejected():
    pyo = pytest.importorskip("pyomo.environ")
    model = pyo.ConcreteModel()
    model.x = pyo.Var([1, 2], domain=pyo.NonNegativeReals)
    model.x[1].fix(1)
    model.x[2].fix(1)
    model.group = pyo.Block()
    model.group.sos = pyo.SOSConstraint(var=model.x, sos=1)

    with pytest.raises(ContractError) as error:
        _model_from_mapping(model)
    assert error.value.code == "capability_uncovered"
    assert error.value.details["component"] == "group.sos"


@pytest.mark.parametrize("inactive_parent", [False, True])
def test_inactive_sos_is_not_rejected(inactive_parent):
    pyo = pytest.importorskip("pyomo.environ")
    model = pyo.ConcreteModel()
    model.x = pyo.Var([1, 2], domain=pyo.NonNegativeReals)
    model.x[1].fix(1)
    model.x[2].fix(1)
    if inactive_parent:
        model.group = pyo.Block()
        model.group.sos = pyo.SOSConstraint(var=model.x, sos=1)
        model.group.deactivate()
    else:
        model.sos = pyo.SOSConstraint(var=model.x, sos=1)
        model.sos.deactivate()

    _model_from_mapping(model)
    checked = _check_model_without_raw_primal(model)
    assert checked.record["status"] == "pass"


def test_supported_active_auxiliary_components_remain_checkable():
    pyo = pytest.importorskip("pyomo.environ")
    model = pyo.ConcreteModel()
    model.p = pyo.Param(initialize=1)
    model.s = pyo.Set(initialize=[1, 2])
    model.r = pyo.RangeSet(2)
    model.x = pyo.Var(domain=pyo.NonNegativeReals)
    model.x.fix(2)
    model.group = pyo.Block()
    model.group.q = pyo.Param(initialize=3)
    model.group.y = pyo.Var()
    model.group.y.fix(4)
    model.expr = pyo.Expression(expr=model.x + model.p)
    model.suffix = pyo.Suffix()
    model.build_action = pyo.BuildAction(rule=lambda current: None)
    model.build_check = pyo.BuildCheck(rule=lambda current: True)
    model.cost = pyo.Objective(expr=model.expr + model.group.y)

    checked_model = _model_from_mapping(model)
    assert checked_model is model
    checked = check_pyomo_model(
        model,
        {},
        ObjectiveSpec("cost", "min", None, model.cost.expr, None),
        input_digest="a" * 64,
        instance_key=[],
        instance_binding_digest="b" * 64,
        feasibility_tolerance=1e-7,
        integrality_tolerance=1e-7,
    )
    assert checked.record["status"] == "pass"

