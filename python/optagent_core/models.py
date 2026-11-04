"""Native Pyomo model loading from a verified, relative-path code closure."""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import inspect
import math
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import ModuleType
from typing import Any, Mapping, Sequence

from .contracts import ContractError, require_hash, sha256_bytes
from .data import DataBundle, DataReader


@dataclass(frozen=True)
class FrozenCode:
    """One candidate Python file with its original relative path and byte ref."""

    original_path: str
    bytes_ref: Mapping[str, Any]


@dataclass(frozen=True)
class NativeCpModelBuild:
    """Core-validated wrapper for the native CP-SAT builder result."""

    model: Any
    variable_bindings: tuple[tuple[str, tuple[str | int, ...], int], ...]


def is_native_cp_model_build(value: Any) -> bool:
    return isinstance(value, NativeCpModelBuild)


def native_variable_binding_rows(value: NativeCpModelBuild) -> list[dict[str, Any]]:
    return [
        {"component": component, "key": list(key), "index": index}
        for component, key, index in value.variable_bindings
    ]


def require_supported_active_components(model: Any) -> None:
    """Reject active Pyomo component semantics outside the result checker scope."""
    try:
        from pyomo.environ import (
            Block,
            BuildAction,
            BuildCheck,
            Constraint,
            Expression,
            Objective,
            Param,
            RangeSet,
            Set,
            Suffix,
            Var,
        )
    except ImportError as exc:
        raise ContractError("environment_unavailable", "the pinned Pyomo runtime is unavailable") from exc

    supported_ctypes = frozenset({
        Var,
        Constraint,
        Objective,
        Block,
        Param,
        Set,
        RangeSet,
        Expression,
        Suffix,
        BuildAction,
        BuildCheck,
    })

    def visit(block: Any) -> None:
        try:
            components = block.component_objects(active=True, descend_into=False)
        except AttributeError as exc:
            raise ContractError(
                "capability_uncovered",
                "model components cannot be enumerated for a complete capability check",
            ) from exc
        for component in components:
            try:
                active = component.active
                ctype = component.ctype
                name = component.name
            except AttributeError as exc:
                raise ContractError(
                    "capability_uncovered",
                    "model component cannot be inspected for a complete capability check",
                ) from exc
            if not active:
                continue
            try:
                supported = ctype in supported_ctypes
            except TypeError:
                supported = False
            if not supported:
                ctype_name = getattr(ctype, "__name__", None) or type(ctype).__name__
                raise ContractError(
                    "capability_uncovered",
                    "active model component is outside the supported Pyomo component checker scope",
                    details={"component": str(name), "ctype": ctype_name},
                )
            if ctype is Block:
                try:
                    block_data = component.values()
                except AttributeError as exc:
                    raise ContractError(
                        "capability_uncovered",
                        "active Pyomo Block contents cannot be enumerated",
                        details={"component": str(name)},
                    ) from exc
                for child in block_data:
                    try:
                        child_active = child.active
                    except AttributeError as exc:
                        raise ContractError(
                            "capability_uncovered",
                            "active Pyomo Block contents cannot be inspected",
                            details={"component": str(name)},
                        ) from exc
                    if child_active:
                        visit(child)

    visit(model)


def _path_parts(value: Any, *, pointer: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ContractError("output_invalid", "frozen code path is invalid", pointer=pointer)
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    has_drive_prefix = (
        len(normalized) >= 2
        and normalized[0].isascii()
        and normalized[0].isalpha()
        and normalized[1] == ":"
    )
    if path.is_absolute() or has_drive_prefix or any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ContractError("path_denied", "frozen code path must stay relative", pointer=pointer)
    parts = tuple(path.parts)
    if path.suffix != ".py" or not path.stem.isidentifier():
        raise ContractError("invalid_argument", "candidate code must use Python module paths", pointer=pointer)
    return parts


def _encode_module_component(value: str) -> str:
    """Keep package components distinct in the import system's dot namespace."""
    return value.replace("%", "%25").replace(".", "%2e")


def _module_path_name(path: str) -> tuple[str, bool]:
    """Map one original .py path to its frozen-namespace relative module name."""
    parts = PurePosixPath(path).parts
    is_package = parts[-1] == "__init__.py"
    module_parts = parts[:-1] if is_package else parts[:-1] + (PurePosixPath(parts[-1]).stem,)
    return ".".join(_encode_module_component(part) for part in module_parts), is_package


def _frozen_code(value: Any, *, pointer: str) -> FrozenCode:
    if not isinstance(value, dict) or set(value) != {"original_path", "bytes"}:
        raise ContractError("output_invalid", "frozen code record has invalid fields", pointer=pointer)
    parts = _path_parts(value["original_path"], pointer=f"{pointer}/original_path")
    ref = value["bytes"]
    if not isinstance(ref, dict):
        raise ContractError("output_invalid", "frozen code ref is invalid", pointer=f"{pointer}/bytes")
    return FrozenCode("/".join(parts), dict(ref))


def _read_frozen_file(reader: DataReader, code: FrozenCode) -> bytes:
    raw = reader.read_ref(code.bytes_ref)
    if sha256_bytes(raw) != code.bytes_ref.get("sha256"):
        raise ContractError("artifact_changed", "candidate code no longer matches its frozen ref")
    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ContractError("output_invalid", "candidate Python source must be UTF-8") from exc
    return raw


class _FrozenSourceLoader(importlib.abc.Loader):
    def __init__(self, fullname: str, code: FrozenCode, source: bytes, *, package: bool) -> None:
        self.fullname = fullname
        self.code = code
        self.source = source
        self.package = package

    def create_module(self, spec: Any) -> ModuleType | None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        module.__file__ = f"<frozen-candidate>/{self.code.original_path}"
        module.__cached__ = None
        if self.package:
            module.__path__ = [f"optagent-candidate:{self.fullname}"]
        text = self.source.decode("utf-8", errors="strict")
        compiled = compile(text, module.__file__, "exec", dont_inherit=True)
        exec(compiled, module.__dict__)


class _FrozenCandidateFinder(importlib.abc.MetaPathFinder):
    """Expose only candidate modules named in the frozen model/module refs."""

    def __init__(self, prefix: str, files: Mapping[str, tuple[FrozenCode, bytes]]) -> None:
        self.prefix = prefix
        self.modules: dict[str, tuple[FrozenCode, bytes, bool]] = {}
        directories: set[str] = {""}
        for path, (code, source) in files.items():
            parts = PurePosixPath(path).parts
            for length in range(1, len(parts)):
                directories.add(".".join(_encode_module_component(part) for part in parts[:length]))
            module_name, is_package = _module_path_name(path)
            previous = self.modules.get(module_name)
            # Match Python's package-before-module lookup if both forms are
            # explicitly present in the declared closure.
            if previous is None or (is_package and not previous[2]):
                self.modules[module_name] = (code, source, is_package)
        self.directories = directories

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname == self.prefix:
            spec = importlib.machinery.ModuleSpec(fullname, loader=None, is_package=True)
            spec.submodule_search_locations = [f"optagent-candidate:{fullname}"]
            return spec
        if not fullname.startswith(self.prefix + "."):
            return None
        relative_name = fullname[len(self.prefix) + 1 :]
        selected = self.modules.get(relative_name)
        if selected is not None:
            code, source, is_package = selected
            loader = _FrozenSourceLoader(fullname, code, source, package=is_package)
            return importlib.util.spec_from_loader(fullname, loader, is_package=is_package)
        if relative_name in self.directories:
            spec = importlib.machinery.ModuleSpec(fullname, loader=None, is_package=True)
            spec.submodule_search_locations = [f"optagent-candidate:{fullname}"]
            return spec
        raise ContractError(
            "input_closure_unknown",
            "candidate imported a local module outside its declared closure",
            details={"module": fullname.removeprefix(self.prefix + ".")},
        )


def _model_from_mapping(
    model: Any,
    *,
    backend_id: str | None = None,
    pointer: str = "/candidate",
) -> Any:
    """Check a builder result without translating its mathematics."""
    try:
        from pyomo.environ import ConcreteModel
    except ImportError as exc:
        raise ContractError("environment_unavailable", "the pinned Pyomo runtime is unavailable") from exc
    if isinstance(model, ConcreteModel):
        if not model.is_constructed():
            raise ContractError("invalid_argument", "Pyomo model is not constructed", pointer=pointer)
        require_supported_active_components(model)
        # Import here only so this module remains importable before the runtime bundle
        # and its Pyomo dependencies have been verified.
        try:
            from pyomo.environ import Var, value
        except ImportError as exc:
            raise ContractError("environment_unavailable", "the pinned Pyomo runtime is unavailable") from exc

        for item in model.component_data_objects(Var, active=True, descend_into=True):
            for label, bound in (("lower", item.lb), ("upper", item.ub)):
                try:
                    number = value(bound, exception=False) if bound is not None else None
                except Exception as exc:
                    raise ContractError(
                        "invalid_argument",
                        "variable bound could not be evaluated",
                        details={"component": item.name, "bound": label},
                    ) from exc
                if number is not None and (isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(float(number))):
                    raise ContractError(
                        "invalid_argument",
                        "variable bound must be finite",
                        details={"component": item.name, "bound": label},
                    )
        return model

    if backend_id != "cp_sat":
        raise ContractError(
            "capability_uncovered",
            "only cp_sat accepts a native CpModel builder result",
            pointer=pointer,
        )
    if not isinstance(model, Mapping) or set(model) != {"model", "variables"}:
        raise ContractError(
            "invalid_argument",
            "build_model must return a constructed Pyomo ConcreteModel or the cp_sat native model envelope",
            pointer=pointer,
        )
    try:
        cp_model = importlib.import_module("ortools.sat.python.cp_model")
    except ImportError as exc:
        raise ContractError("environment_unavailable", "the selected OR-Tools CP-SAT runtime is unavailable") from exc
    native_model = model.get("model")
    if not isinstance(native_model, cp_model.CpModel):
        raise ContractError("invalid_argument", "cp_sat native envelope model must be an actual CpModel", pointer=f"{pointer}/model")
    bindings = model.get("variables")
    if not isinstance(bindings, Mapping):
        raise ContractError("invalid_argument", "cp_sat native envelope variables must be an object", pointer=f"{pointer}/variables")
    variable_count = len(native_model.proto.variables)
    normalized: list[tuple[str, tuple[str | int, ...], int]] = []
    for component, keyed_indexes in bindings.items():
        if not isinstance(component, str) or not component:
            raise ContractError("invalid_argument", "native variable component names must be nonempty strings", pointer=f"{pointer}/variables")
        if not isinstance(keyed_indexes, Mapping):
            raise ContractError("invalid_argument", "native variable components must map typed keys to OR-Tools variables", pointer=f"{pointer}/variables/{component}")
        for key, variable in keyed_indexes.items():
            if not isinstance(key, tuple) or any(type(part) not in (str, int) for part in key):
                raise ContractError("invalid_argument", "native variable keys must be typed string/integer tuples", pointer=f"{pointer}/variables/{component}")
            if not isinstance(variable, cp_model.IntVar):
                raise ContractError(
                    "invalid_argument",
                    "native variable binding values must be OR-Tools IntVar objects",
                    pointer=f"{pointer}/variables/{component}",
                )
            try:
                index = variable.Index()
            except Exception as exc:
                raise ContractError(
                    "invalid_argument",
                    "native variable binding does not expose a valid proto index",
                    pointer=f"{pointer}/variables/{component}",
                ) from exc
            if type(index) is not int:
                raise ContractError(
                    "invalid_argument",
                    "native variable proto index must be an integer",
                    pointer=f"{pointer}/variables/{component}",
                )
            if index < 0 or index >= variable_count:
                raise ContractError(
                    "invalid_argument",
                    "native variable proto index is outside the returned model",
                    pointer=f"{pointer}/variables/{component}",
                )
            try:
                belongs_to_model = variable.proto is native_model.proto.variables[index]
            except Exception as exc:
                raise ContractError(
                    "invalid_argument",
                    "native variable binding is not a current variable of the returned CpModel",
                    pointer=f"{pointer}/variables/{component}",
                ) from exc
            if not belongs_to_model:
                raise ContractError(
                    "invalid_argument",
                    "native variable binding is not a current variable of the returned CpModel",
                    pointer=f"{pointer}/variables/{component}",
                )
            normalized.append((component, key, index))
    return NativeCpModelBuild(native_model, tuple(normalized))


def build_native_model(
    reader: DataReader,
    bundle: DataBundle,
    model_spec: Any,
    local_module_specs: Any,
    *,
    backend_id: str | None = None,
    require_extract_solution: bool = False,
) -> tuple[Any, tuple[Mapping[str, Any], ...]]:
    """Import the frozen native module closure and call its one build function.

    Candidate code executes only in the worker. Every module source is first
    loaded through the shared verified Ref reader and compiled from those exact
    bytes; Python never has to reopen a workspace source file during import.
    Relative module directories and names remain intact.
    """
    built, consumed = call_frozen_function(
        reader,
        model_spec,
        local_module_specs,
        function_name="build_model",
        args=(bundle,),
        pointer="/candidate/model",
        require_extract_solution=require_extract_solution,
    )
    checked_model = _model_from_mapping(built, backend_id=backend_id)
    return checked_model, consumed


def call_frozen_function(
    reader: DataReader,
    primary_spec: Any,
    local_module_specs: Any,
    *,
    function_name: str,
    args: Sequence[Any],
    pointer: str,
    require_extract_solution: bool = False,
) -> tuple[Any, tuple[Mapping[str, Any], ...]]:
    """Call one named function from a verified in-memory Python closure."""
    primary_code = _frozen_code(primary_spec, pointer=pointer)
    if not isinstance(local_module_specs, list):
        raise ContractError("output_invalid", "candidate local_modules must be an array")
    codes = [primary_code]
    codes.extend(
        _frozen_code(item, pointer=f"/candidate/local_modules/{index}")
        for index, item in enumerate(local_module_specs)
    )
    by_path: dict[str, FrozenCode] = {}
    sources: dict[str, tuple[FrozenCode, bytes]] = {}
    for item in codes:
        previous = by_path.get(item.original_path)
        if previous is not None:
            if dict(previous.bytes_ref) != dict(item.bytes_ref):
                raise ContractError("artifact_changed", "candidate path is bound to conflicting byte refs")
            continue
        by_path[item.original_path] = item
        sources[item.original_path] = (item, _read_frozen_file(reader, item))
    if primary_code.original_path not in sources:
        raise ContractError("output_invalid", "frozen callable source is missing")

    # The namespace changes only the import prefix. The candidate's original
    # package tree is represented under that prefix, so same-named helpers in
    # different directories stay distinct.
    seed = primary_code.bytes_ref.get("sha256")
    require_hash(seed, pointer=f"{pointer}/bytes/sha256")
    prefix = f"_optagent_candidate_{seed[:20]}"
    finder = _FrozenCandidateFinder(prefix, sources)
    sys.meta_path.insert(0, finder)
    try:
        source_relative_name, _ = _module_path_name(primary_code.original_path)
        source_module_name = prefix + "." + source_relative_name if source_relative_name else prefix
        module = importlib.import_module(source_module_name)
        function = getattr(module, function_name, None)
        if not callable(function):
            raise ContractError("invalid_argument", f"frozen module must define {function_name}(...)")
        try:
            signature = inspect.signature(function)
            signature.bind(*args)
        except (TypeError, ValueError) as exc:
            raise ContractError("invalid_argument", f"{function_name} has an incompatible signature") from exc
        if require_extract_solution:
            extractor = getattr(module, "extract_solution", None)
            if not callable(extractor):
                raise ContractError("invalid_argument", "frozen module must define extract_solution(...)")
            try:
                extractor_signature = inspect.signature(extractor)
                extractor_signature.bind(args[0], object())
            except (TypeError, ValueError, IndexError) as exc:
                raise ContractError("invalid_argument", "extract_solution has an incompatible signature") from exc
        result = function(*args)
        consumed: list[Mapping[str, Any]] = []
        for name, module_obj in tuple(sys.modules.items()):
            if name == prefix or name.startswith(prefix + "."):
                path = getattr(module_obj, "__file__", None)
                if not isinstance(path, str) or not path.startswith("<frozen-candidate>/"):
                    continue
                rel = path.removeprefix("<frozen-candidate>/")
                code = by_path.get(rel)
                if code is None:
                    raise ContractError("input_closure_unknown", "candidate imported an undeclared local module", details={"path": rel})
                # The module body ran from this verified in-memory source. Keep
                # its actual bytes ref in the consumed closure evidence.
                consumed.append(dict(code.bytes_ref))
        if not consumed or not any(dict(item) == dict(primary_code.bytes_ref) for item in consumed):
            raise ContractError("input_closure_unknown", "the frozen entry module was not observed in its import closure")
        return result, tuple(consumed)
    finally:
        sys.meta_path.remove(finder)
        for name in tuple(sys.modules):
            if name == prefix or name.startswith(prefix + "."):
                del sys.modules[name]
        # Do not remove unrelated modules imported by the candidate; Python
        # module cache ownership is process-wide and doing so could corrupt the
        # trusted runtime. The unique namespace prevents candidate collisions.

