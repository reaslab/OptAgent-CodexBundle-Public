"""Visible, editable case execution through the existing worker/checker.

This module is intentionally a local facade. It creates no Case, Run, Stamp,
Seal, Source, or platform review authority. Solver work stays in
``worker._run_instances``; check-only validates the saved business solution
directly and separately checks a saved raw point against the current model.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .contracts import (
    ContractError,
    canonical_json_bytes,
    file_signature,
    ref_from_bytes,
    sha256_bytes,
    strict_json_loads,
    validate_ref_shape,
)
from .data import DataBundle, load_local_data_bundle
from .models import _path_parts, build_native_model, is_native_cp_model_build
from .results import (
    ObjectiveSpec,
    business_check_uncovered,
    check_pyomo_model,
    NATIVE_CP_SAT_METHOD,
    objective_for_layer,
    objective_values_exactly_match,
    objective_values_match,
)
from .worker import _OutputTarget, _business_check_solution, _run_instances


_RUN_FIELDS = {
    "schema_version",
    "model_path",
    "local_modules",
    "business_checker_path",
    "inputs_path",
    "instance_parameter_object_id",
    "backend_id",
    "objective_plan",
    "solve_options",
}
_RUN_CONFIG_PATH = ".optagent/run.json"
_RUN_RUNTIME_PATH = ".optagent/runtime.zip"
_LOCAL_EXECUTION_PATH = ".optagent/execution.json"
_LOCAL_ARTIFACT_DIRECTORY = ".optagent/artifacts"
_CHECK_RECORD_PATH = ".optagent/check.json"
_OPTIONAL_RUN_FIELDS = {"code_import_paths"}
_INPUT_FIELDS = {"schema_version", "instance_parameter_object_id", "objects"}
_SOLUTION_FIELDS = {"schema_version", "kind", "status", "results"}
_SOLUTION_SCHEMA_VERSION = 2
_SOLUTION_ROW_FIELDS = {"result", "witness", "model_check", "business_check"}
_WITNESS_FIELDS = {"input_digest", "instance_key", "instance_binding_digest", "variables", "objective_values"}
_VARIABLE_FIELDS = {"component", "key", "domain", "value", "origin"}
_SOLVE_OPTION_FIELDS = {
    "execution_budget_seconds",
    "time_limit_seconds",
    "relative_gap",
    "absolute_gap",
    "threads",
    "random_seed",
    "feasibility_tolerance",
    "integrality_tolerance",
    "write_log",
    "export_model",
}
_OPTIONAL_SOLVE_OPTION_FIELDS = {"warm_start"}
_NO_SAVED_POINT = "__NO_SAVED_POINT__"


class _LocalReader:
    """In-memory Ref reader containing one captured local case snapshot."""

    def __init__(self) -> None:
        self._bytes: dict[tuple[str, str, int, str], bytes] = {}

    def add(self, logical_path: str, raw: bytes, media_type: str = "application/octet-stream") -> dict[str, Any]:
        ref = ref_from_bytes(logical_path, raw, media_type)
        key = (ref["path"], ref["sha256"], ref["byte_len"], ref["media_type"])
        previous = self._bytes.get(key)
        if previous is not None and previous != raw:
            raise ContractError("artifact_changed", "local capture has a conflicting byte identity")
        self._bytes[key] = raw
        return ref

    def read_ref(self, ref: Mapping[str, Any]) -> bytes:
        checked = validate_ref_shape(dict(ref))
        key = (checked["path"], checked["sha256"], checked["byte_len"], checked["media_type"])
        try:
            return self._bytes[key]
        except KeyError as exc:
            raise ContractError("capture_unavailable", "reference is outside this local file snapshot") from exc


def _absolute_directory(value: str | os.PathLike[str], *, label: str) -> Path:
    requested = Path(value)
    if not requested.is_absolute():
        raise ContractError("path_denied", f"{label} must be absolute")
    root = Path(os.path.abspath(os.fspath(requested)))
    cursor = Path(root.anchor)
    try:
        info = cursor.lstat()
        if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
            raise ContractError("path_denied", f"{label} contains a link")
        for part in root.parts[1:]:
            cursor = cursor / part
            info = cursor.lstat()
            if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
                raise ContractError("path_denied", f"{label} contains a link")
        if not stat.S_ISDIR(info.st_mode):
            raise ContractError("path_denied", f"{label} must be a directory")
    except OSError as exc:
        raise ContractError("not_found", f"{label} is unavailable") from exc
    return root


def _relative_path(value: Any, *, pointer: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ContractError("path_denied", "case file path must be a relative POSIX path", pointer=pointer)
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ContractError("path_denied", "case file path escapes its case directory", pointer=pointer)
    return value


def _read_case_bytes(case_dir: Path, relative: str) -> bytes:
    parts = tuple(PurePosixPath(relative).parts)
    cursor = case_dir
    before: os.stat_result | None = None
    try:
        for index, part in enumerate(parts):
            cursor = cursor / part
            info = cursor.lstat()
            linked = stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)
            expected = stat.S_ISREG(info.st_mode) if index == len(parts) - 1 else stat.S_ISDIR(info.st_mode)
            if linked or not expected:
                raise ContractError("path_denied", "case file path contains a link or non-regular component")
            if index == len(parts) - 1:
                before = info
        if before is None:
            raise ContractError("path_denied", "case file path does not name a file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(cursor, flags)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if file_signature(opened) != file_signature(before):
                raise ContractError("source_changed_during_read", "case file changed while opening")
            raw = stream.read()
            after_fd = os.fstat(stream.fileno())
        after_path = cursor.lstat()
    except ContractError:
        raise
    except OSError as exc:
        raise ContractError("not_found", "case file is unavailable", details={"path": relative}) from exc
    if any(file_signature(item) != file_signature(before) for item in (opened, after_fd, after_path)):
        raise ContractError("source_changed_during_read", "case file changed while being captured", details={"path": relative})
    return raw


def _json_bytes(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = strict_json_loads(raw, max_bytes=len(raw))
    except (ValueError, UnicodeError) as exc:
        raise ContractError("output_invalid", f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise ContractError("output_invalid", f"{label} must be a JSON object")
    return value


def _valid_run_fields(value: Mapping[str, Any]) -> bool:
    fields = set(value)
    return fields == _RUN_FIELDS or fields == _RUN_FIELDS | _OPTIONAL_RUN_FIELDS


def _validate_local_option_shape(options: Any) -> dict[str, Any]:
    if not isinstance(options, dict) or frozenset(options) not in {
        frozenset(_SOLVE_OPTION_FIELDS),
        frozenset(_SOLVE_OPTION_FIELDS | _OPTIONAL_SOLVE_OPTION_FIELDS),
    }:
        raise ContractError("output_invalid", "solve_options has invalid fields", pointer="/solve_options")
    warm_start = options.get("warm_start", False)
    if type(warm_start) is not bool:
        raise ContractError("invalid_argument", "warm_start must be a boolean", pointer="/solve_options/warm_start")
    for name in ("execution_budget_seconds", "feasibility_tolerance", "integrality_tolerance", "relative_gap", "absolute_gap"):
        value = options[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ContractError("output_invalid", f"{name} must be a finite number", pointer=f"/solve_options/{name}")
    if float(options["execution_budget_seconds"]) <= 0:
        raise ContractError("invalid_argument", "execution budget must be positive", pointer="/solve_options/execution_budget_seconds")
    if options["time_limit_seconds"] is not None and (
        isinstance(options["time_limit_seconds"], bool)
        or not isinstance(options["time_limit_seconds"], (int, float))
        or not math.isfinite(float(options["time_limit_seconds"]))
        or float(options["time_limit_seconds"]) <= 0
    ):
        raise ContractError("invalid_argument", "native time limit must be null or finite and positive", pointer="/solve_options/time_limit_seconds")
    if float(options["relative_gap"]) < 0 or float(options["absolute_gap"]) < 0:
        raise ContractError("invalid_argument", "Core gap targets must be nonnegative", pointer="/solve_options")
    if options["threads"] is not None and (type(options["threads"]) is not int or options["threads"] <= 0):
        raise ContractError("invalid_argument", "threads must be null or a positive integer", pointer="/solve_options/threads")
    if options["random_seed"] is not None and (type(options["random_seed"]) is not int or options["random_seed"] < 0):
        raise ContractError("invalid_argument", "random_seed must be null or a nonnegative integer", pointer="/solve_options/random_seed")
    if float(options["feasibility_tolerance"]) < 1e-10 or float(options["integrality_tolerance"]) < 1e-10:
        raise ContractError("invalid_argument", "checker tolerances must be at least 1e-10", pointer="/solve_options")
    if type(options["write_log"]) is not bool or type(options["export_model"]) is not bool:
        raise ContractError("invalid_argument", "write_log and export_model must be booleans", pointer="/solve_options")
    checked = dict(options)
    checked["warm_start"] = warm_start
    return checked


def _capture_case(
    case_dir_value: str | os.PathLike[str],
    *,
    run_bytes_override: bytes | None = None,
) -> dict[str, Any]:
    case_dir = _absolute_directory(case_dir_value, label="case directory")
    run_relative = _RUN_CONFIG_PATH
    run_raw = run_bytes_override if run_bytes_override is not None else _read_case_bytes(case_dir, run_relative)
    run_config = _json_bytes(run_raw, label="run.json")
    if not _valid_run_fields(run_config) or type(run_config.get("schema_version")) is not int or run_config["schema_version"] != 1:
        raise ContractError("output_invalid", "run.json has invalid fields")
    model_path = _relative_path(run_config["model_path"], pointer="/model_path")
    modules_value = run_config["local_modules"]
    if not isinstance(modules_value, list):
        raise ContractError("output_invalid", "local_modules must be an array", pointer="/local_modules")
    module_paths: list[str] = []
    for index, value in enumerate(modules_value):
        relative = _relative_path(value, pointer=f"/local_modules/{index}")
        if relative in module_paths:
            raise ContractError("output_invalid", "local_modules path repeats", pointer=f"/local_modules/{index}")
        module_paths.append(relative)
    checker_value = run_config["business_checker_path"]
    if checker_value is None:
        checker_path = None
    else:
        checker_path = _relative_path(checker_value, pointer="/business_checker_path")
    inputs_path = _relative_path(run_config["inputs_path"], pointer="/inputs_path")
    if not isinstance(run_config["backend_id"], str) or not run_config["backend_id"]:
        raise ContractError("output_invalid", "backend_id must be a non-empty string", pointer="/backend_id")
    if not isinstance(run_config["objective_plan"], dict) or not isinstance(run_config["solve_options"], dict):
        raise ContractError("output_invalid", "objective_plan and solve_options must be objects")
    run_config["solve_options"] = _validate_local_option_shape(run_config["solve_options"])
    parameter_id = run_config["instance_parameter_object_id"]
    if parameter_id is not None and (not isinstance(parameter_id, str) or not parameter_id):
        raise ContractError("output_invalid", "instance_parameter_object_id is invalid")

    input_raw = _read_case_bytes(case_dir, inputs_path)
    inputs = _json_bytes(input_raw, label=inputs_path)
    if set(inputs) != _INPUT_FIELDS or type(inputs.get("schema_version")) is not int or inputs["schema_version"] != 1:
        raise ContractError("output_invalid", "inputs.json has invalid fields")
    if inputs["instance_parameter_object_id"] != parameter_id:
        raise ContractError("artifact_changed", "run.json and inputs.json parameter object identities differ")

    paths = [model_path, *module_paths, *([checker_path] if checker_path is not None else [])]
    unique_paths = list(dict.fromkeys(paths))
    code_import_paths = run_config.get("code_import_paths", {})
    if not isinstance(code_import_paths, dict):
        raise ContractError("output_invalid", "code_import_paths must be an object", pointer="/code_import_paths")
    if any(physical not in paths for physical in code_import_paths):
        raise ContractError(
            "output_invalid",
            "code_import_paths keys must name declared code files",
            pointer="/code_import_paths",
        )
    logical_by_physical: dict[str, str] = {}
    for physical, logical_value in code_import_paths.items():
        if not isinstance(logical_value, str):
            raise ContractError("output_invalid", "code import logical paths must be strings", pointer="/code_import_paths")
        logical_parts = _path_parts(logical_value, pointer="/code_import_paths")
        logical_path = "/".join(logical_parts)
        if logical_path != logical_value:
            raise ContractError("output_invalid", "code import logical paths must use canonical POSIX paths", pointer="/code_import_paths")
        logical_by_physical[physical] = logical_path
    effective_paths = [logical_by_physical.get(physical, physical) for physical in unique_paths]
    for physical, logical_path in zip(unique_paths, effective_paths, strict=True):
        _path_parts(logical_path, pointer=f"/code_import_paths/{physical}")
    if len(effective_paths) != len(set(effective_paths)):
        raise ContractError(
            "output_invalid",
            "declared code files must have unique effective logical paths",
            pointer="/code_import_paths",
        )
    logical_by_physical = dict(zip(unique_paths, effective_paths, strict=True))
    reader = _LocalReader()
    run_ref = reader.add("local-case/run.json", run_raw, "application/json")
    inputs_ref = reader.add("local-case/inputs.json", input_raw, "application/json")
    requirements_path = case_dir / "requirements.txt"
    requirements_spec = None
    requirements_refs: list[dict[str, Any]] = []
    try:
        requirements_info = requirements_path.lstat()
    except FileNotFoundError:
        requirements_info = None
    if requirements_info is not None:
        requirements_raw = _read_case_bytes(case_dir, "requirements.txt")
        try:
            requirements_raw.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ContractError("output_invalid", "root requirements.txt must be UTF-8", pointer="/requirements.txt") from exc
        requirements_ref = reader.add("local-case/requirements.txt", requirements_raw, "text/plain; charset=utf-8")
        requirements_spec = {"path": "requirements.txt", "bytes": requirements_ref}
        requirements_refs.append(dict(requirements_ref))
    code_specs: dict[str, dict[str, Any]] = {}
    code_refs: list[dict[str, Any]] = []
    for relative in unique_paths:
        raw = _read_case_bytes(case_dir, relative)
        try:
            raw.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ContractError("output_invalid", "local Python source must be UTF-8", details={"path": relative}) from exc
        ref = reader.add(f"local-case/code/{relative}", raw)
        spec = {"original_path": logical_by_physical[relative], "bytes": ref}
        code_specs[relative] = spec
        code_refs.append(dict(ref))

    captured_identity = {
        "schema_version": 1,
        "run": {"path": run_ref["path"], "sha256": run_ref["sha256"], "byte_len": run_ref["byte_len"]},
        "inputs": {"path": inputs_ref["path"], "sha256": inputs_ref["sha256"], "byte_len": inputs_ref["byte_len"]},
        "code": [
            {"path": item["path"], "sha256": item["sha256"], "byte_len": item["byte_len"]}
            for item in code_refs
        ],
        "requirements": [
            {"path": item["path"], "sha256": item["sha256"], "byte_len": item["byte_len"]}
            for item in requirements_refs
        ],
    }
    input_digest = sha256_bytes(canonical_json_bytes(captured_identity))
    data_bundle = load_local_data_bundle(inputs, candidate_binding_digest=input_digest)
    if parameter_id is not None and data_bundle.data_id is None:
        raise ContractError("invalid_argument", "parameterized run requires normalized input objects")
    return {
        "case_dir": case_dir,
        "run_config": run_config,
        "run_raw": run_raw,
        "inputs": inputs,
        "input_digest": input_digest,
        "data_bundle": data_bundle,
        "reader": reader,
        "model_spec": code_specs[model_path],
        "module_specs": [code_specs[path] for path in module_paths],
        "checker_spec": None if checker_path is None else code_specs[checker_path],
        "requirements_file": requirements_spec,
        "captured_refs": [dict(run_ref), dict(inputs_ref), *code_refs, *requirements_refs],
    }


def _prepared_for_local(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    config = snapshot["run_config"]
    data_bundle: DataBundle = snapshot["data_bundle"]
    parameter_id = config["instance_parameter_object_id"]
    data_info = None if data_bundle.data_id is None else {
        "data_id": data_bundle.data_id,
        "instance_parameter_object_id": parameter_id,
    }
    candidate = {
        "authoring": "direct_python" if config["backend_id"] == "direct_python" else "pyomo",
        "model": snapshot["model_spec"],
        "local_modules": list(snapshot["module_specs"]),
        "business_checker": snapshot["checker_spec"],
        "requirements_file": snapshot.get("requirements_file"),
        "backend_id": config["backend_id"],
        "objective_plan": config["objective_plan"],
        "input_closure": "declared",
        "closure_note": "Visible local case files captured for this invocation.",
    }
    return {
        "schema_version": 3,
        "prepared_id": None,
        "case_id": None,
        "source_revision": None,
        "candidate_revision": None,
        "candidate": candidate,
        "data": data_info,
        "solve_options": dict(config["solve_options"]),
        "input_digest": snapshot["input_digest"],
    }


def _read_output_json(output_root: Path, reference: Any, *, label: str) -> dict[str, Any] | None:
    if reference is None:
        return None
    if not isinstance(reference, Mapping):
        raise ContractError("output_invalid", f"{label} reference is invalid")
    checked = validate_ref_shape(dict(reference))
    relative = _relative_path(checked["path"], pointer=f"/{label}/path")
    target = output_root.joinpath(*PurePosixPath(relative).parts)
    try:
        target.relative_to(output_root)
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ContractError("path_denied", f"{label} output is not a regular file")
        raw = target.read_bytes()
    except ContractError:
        raise
    except OSError as exc:
        raise ContractError("not_found", f"{label} output is unavailable") from exc
    if len(raw) != checked["byte_len"] or sha256_bytes(raw) != checked["sha256"]:
        raise ContractError("artifact_changed", f"{label} output bytes do not match their reference")
    return _json_bytes(raw, label=label)


def _collect_solution_rows(output_root: Path, output_records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in output_records:
        result = _read_output_json(output_root, item.get("result"), label="Result")
        if result is None:
            continue
        witness = _read_output_json(output_root, item.get("witness"), label="Witness")
        model_check = _read_output_json(output_root, item.get("model_check"), label="ModelCheck")
        business_check = _read_output_json(output_root, item.get("business_check"), label="BusinessCheck")
        rows.append({
            "result": result,
            "witness": witness,
            "model_check": model_check,
            "business_check": business_check,
        })
    return rows


def _solution_result_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Count saved Result statuses; direct_python may be checked without ModelCheck."""
    termination_counts: Counter[str] = Counter()
    model_check_status_counts: Counter[str] = Counter()
    business_check_status_counts: Counter[str] = Counter()
    numerical_optimality_status_counts: Counter[str] = Counter()
    checked_point_count = 0
    solver_error_observed_count = 0

    for row in rows:
        result = row.get("result")
        result = result if isinstance(result, Mapping) else {}
        termination = result.get("termination")
        termination_counts[termination if isinstance(termination, str) else "unknown"] += 1
        if result.get("has_checked_candidate") is True:
            checked_point_count += 1
        warnings = result.get("warnings")
        if isinstance(warnings, list) and "solver_error_observed" in warnings:
            solver_error_observed_count += 1

        backend = result.get("backend")
        direct_python = isinstance(backend, Mapping) and backend.get("id") == "direct_python"
        model_check = row.get("model_check")
        model_status = model_check.get("status") if isinstance(model_check, Mapping) else None
        if not isinstance(model_status, str):
            model_status = "not_applicable" if direct_python and model_check is None else "unknown"
        model_check_status_counts[model_status] += 1

        business_check = row.get("business_check")
        business_status = business_check.get("status") if isinstance(business_check, Mapping) else None
        business_check_status_counts[business_status if isinstance(business_status, str) else "unknown"] += 1

        numerical = result.get("numerical")
        optimality = numerical.get("optimality") if isinstance(numerical, Mapping) else None
        optimality_status = optimality.get("status") if isinstance(optimality, Mapping) else None
        if not isinstance(optimality_status, str):
            optimality_status = "not_applicable" if direct_python and optimality is None else "unknown"
        numerical_optimality_status_counts[optimality_status] += 1

    return {
        "termination_counts": dict(termination_counts),
        "checked_point_count": checked_point_count,
        "solver_error_observed_count": solver_error_observed_count,
        "model_check_status_counts": dict(model_check_status_counts),
        "business_check_status_counts": dict(business_check_status_counts),
        "numerical_optimality_status_counts": dict(numerical_optimality_status_counts),
    }


def _recover_solution_rows(output_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not output_root.exists():
        return rows
    for path in sorted(output_root.rglob("result.json")):
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                continue
            raw = path.read_bytes()
            result = _json_bytes(raw, label="partial Result")
            witness = None
            witness_ref = result.get("witness")
            if isinstance(witness_ref, Mapping):
                try:
                    witness = _read_output_json(output_root, witness_ref, label="partial Witness")
                except (ContractError, OSError, ValueError, UnicodeError):
                    witness = None
            model_check = None
            check_ref = result.get("check")
            direct = isinstance(result.get("backend"), Mapping) and result["backend"].get("id") == "direct_python"
            if isinstance(check_ref, Mapping) and not direct:
                try:
                    model_check = _read_output_json(output_root, check_ref, label="partial ModelCheck")
                except (ContractError, OSError, ValueError, UnicodeError):
                    model_check = None
            business_check = None
            business_path = path.with_name("business-check.json")
            try:
                business_info = business_path.lstat()
                if not stat.S_ISLNK(business_info.st_mode) and stat.S_ISREG(business_info.st_mode):
                    candidate = _json_bytes(business_path.read_bytes(), label="partial BusinessCheck")
                    if (
                        _typed_key(candidate.get("instance_key"), pointer="/instance_key")
                        == _typed_key(result.get("instance_key"), pointer="/instance_key")
                        and candidate.get("input_digest") == result.get("input_digest")
                        and candidate.get("instance_binding_digest") == result.get("instance_binding_digest")
                    ):
                        business_check = candidate
            except (ContractError, OSError, ValueError, UnicodeError):
                business_check = None
            rows.append({
                "result": result,
                "witness": witness,
                "model_check": model_check,
                "business_check": business_check,
            })
        except (ContractError, OSError, ValueError, UnicodeError):
            continue
    return rows


def _atomic_stage_json(stage: Path, relative: str, value: Mapping[str, Any]) -> None:
    target = stage.joinpath(*PurePosixPath(_relative_path(relative, pointer="/output_path")).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical_json_bytes(dict(value))
    temp = target.with_name(f".{target.name}.{os.urandom(8).hex()}.tmp")
    try:
        with open(temp, "xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if os.path.lexists(target):
            info = target.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ContractError("path_denied", "local stage output is not a regular file")
        os.replace(temp, target)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _atomic_stage_text(stage: Path, relative: str, value: str) -> None:
    target = stage.joinpath(*PurePosixPath(_relative_path(relative, pointer="/output_path")).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = value.encode("utf-8")
    temp = target.with_name(f".{target.name}.{os.urandom(8).hex()}.tmp")
    try:
        with open(temp, "xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if os.path.lexists(target):
            info = target.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ContractError("path_denied", "local stage output is not a regular file")
        os.replace(temp, target)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _output_target(
    case_dir: Path,
    output_directory: str | os.PathLike[str] | None,
    *,
    default_name: str = "results/local",
) -> tuple[Path, bool]:
    if output_directory is None:
        return case_dir / default_name, True
    requested = Path(output_directory)
    target = requested if requested.is_absolute() else case_dir / requested
    target = Path(os.path.abspath(os.fspath(target)))
    return target, False


def _make_stage(target: Path, *, replace_existing: bool, create_results_parent: bool = False) -> Path:
    parent = target.parent
    if not parent.is_absolute():
        raise ContractError("path_denied", "local output parent must be absolute")
    if create_results_parent and not os.path.lexists(parent):
        if parent.name != "results" or target.name not in {"local", "check"}:
            raise ContractError("path_denied", "only the case results parent may be created for a default output")
        try:
            parent.mkdir()
        except FileExistsError:
            pass
    _absolute_directory(parent, label="local output parent")
    if os.path.lexists(target):
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
            raise ContractError("path_denied", "local output target is a link")
        if not replace_existing or not stat.S_ISDIR(info.st_mode):
            raise ContractError("package_collision", "explicit local output target already exists")
    return Path(tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=parent))


def _remove_owned_tree(path: Path, parent: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if absolute.parent != parent or not absolute.name.startswith(".local-results.old-"):
        raise ContractError("path_denied", "refusing to remove a non-owned local backup")
    info = absolute.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ContractError("path_denied", "local backup is not a regular directory")
    shutil.rmtree(absolute)


def _publish_stage(stage: Path, target: Path, *, replace_existing: bool) -> None:
    backup: Path | None = None
    if os.path.lexists(target):
        if not replace_existing:
            raise ContractError("package_collision", "explicit local output target already exists")
        target_info = target.lstat()
        if stat.S_ISLNK(target_info.st_mode) or bool(getattr(target_info, "st_file_attributes", 0) & 0x400) or not stat.S_ISDIR(target_info.st_mode):
            raise ContractError("path_denied", "local output target changed before publication")
        backup = target.parent / f".local-results.old-{os.urandom(8).hex()}"
        os.replace(target, backup)
    try:
        os.replace(stage, target)
    except OSError:
        if backup is not None and not os.path.lexists(target):
            os.replace(backup, target)
        raise
    if backup is not None:
        _remove_owned_tree(backup, target.parent)


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
        # Kill any solver descendants that remained after the child exited.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
    else:
        # Windows currently guarantees termination only for the direct child.
        # Linux is the accepted process-group termination target for this batch.
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _spawn_case_child(
    case_dir: Path,
    artifact_dir: Path,
    run_snapshot: Path,
    *,
    operation: str,
    budget_seconds: float,
    started_at: float,
    extra_arguments: Sequence[str] = (),
) -> tuple[int | None, bool]:
    """Run one operation under the parent-owned whole-call deadline."""
    if operation not in {"solve", "check"}:
        raise ContractError("invalid_argument", "local child operation is unsupported")
    remaining = budget_seconds - (time.monotonic() - started_at)
    if remaining <= 0:
        return None, True
    package_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    old_path = environment.get("PYTHONPATH")
    case_runtime = case_dir.joinpath(*PurePosixPath(_RUN_RUNTIME_PATH).parts)
    if os.path.lexists(case_runtime):
        _read_case_bytes(case_dir, _RUN_RUNTIME_PATH)
        import_paths = [os.fspath(case_runtime), os.fspath(package_root)]
    else:
        import_paths = [os.fspath(package_root)]
    if old_path:
        import_paths.append(old_path)
    environment["PYTHONPATH"] = os.pathsep.join(import_paths)
    command = [
        sys.executable,
        "-m",
        "optagent_core.case_execution",
        f"_child-{operation}",
        os.fspath(case_dir),
        os.fspath(artifact_dir),
        os.fspath(run_snapshot),
        *extra_arguments,
    ]
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        cwd=case_dir,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name == "posix",
        creationflags=creationflags,
    )
    try:
        process.wait(timeout=max(0.0, budget_seconds - (time.monotonic() - started_at)))
        return process.returncode, False
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        return process.returncode, True
    except BaseException:
        _terminate_process_group(process)
        raise


def _solve_child(snapshot: Mapping[str, Any], artifact_dir: Path) -> dict[str, Any]:
    from .solver_protocol import (
        DIRECT_PYTHON_BACKEND_ID,
        backend_profile_for,
        direct_python_profile,
        validate_direct_python_options,
        validate_solver_options,
    )

    config = snapshot["run_config"]
    backend_id = config["backend_id"]
    prepared = _prepared_for_local(snapshot)
    if backend_id == DIRECT_PYTHON_BACKEND_ID:
        if config["objective_plan"].get("mode") == "lexicographic":
            raise ContractError("capability_uncovered", "direct_python supports scalar and feasibility objective plans only", pointer="/objective_plan/mode")
        validate_direct_python_options(config["solve_options"])
        requirements_spec = prepared["candidate"].get("requirements_file")
        requirements_bytes = (
            b""
            if requirements_spec is None
            else snapshot["reader"].read_ref(requirements_spec["bytes"])
        )
        backend_profile = direct_python_profile(requirements_bytes)
    else:
        validate_solver_options(backend_id, config["solve_options"])
        backend_profile = backend_profile_for(backend_id)
    output = _OutputTarget.create_local(artifact_dir)
    started = time.monotonic()
    records, output_refs, consumed, solver_invoked, solver_returned = _run_instances(
        snapshot["reader"],
        output,
        prepared,
        (),
        input_digest=snapshot["input_digest"],
        data_bundle=snapshot["data_bundle"],
        base_consumed_inputs=snapshot["captured_refs"],
        execution_started=started,
        backend_profile=backend_profile,
        enforce_execution_budget=False,
    )
    batch_summary = _read_output_json(
        artifact_dir,
        next((ref for ref in output_refs if ref.get("path") == "batch-summary.json"), None),
        label="batch summary",
    )
    child_report = {
        "schema_version": 1,
        "kind": "local-computation",
        "status": "partial" if batch_summary and batch_summary.get("stop_reason") else "completed",
        "input_digest": snapshot["input_digest"],
        "backend_id": backend_id,
        "backend_profile": backend_profile,
        "solve_options": dict(config["solve_options"]),
        "execution_elapsed_seconds": time.monotonic() - started,
        "instances": [dict(item) for item in records],
        "batch_summary": batch_summary,
        "consumed_inputs": [dict(ref) for ref in consumed],
        "output_refs": [dict(ref) for ref in output_refs],
        "solver_invoked": solver_invoked,
        "solver_returned": solver_returned,
    }
    output.write_json("child-report.json", child_report)
    return child_report


def _child_main(case_dir: str, artifact_dir: str, run_snapshot: str) -> int:
    case_root = _absolute_directory(case_dir, label="case directory")
    try:
        run_raw = Path(run_snapshot).read_bytes()
        snapshot = _capture_case(case_root, run_bytes_override=run_raw)
        _solve_child(snapshot, Path(artifact_dir))
        return 0
    except Exception as exc:
        error = _error_record(exc)
        try:
            error_path = Path(run_snapshot).parent / "child-error.json"
            with open(error_path, "xb") as stream:
                stream.write(canonical_json_bytes(error))
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            pass
        return 2


def _child_check_main(case_dir: str, artifact_dir: str, run_snapshot: str, solution_path: str) -> int:
    case_root = _absolute_directory(case_dir, label="case directory")
    started = time.monotonic()
    try:
        run_raw = Path(run_snapshot).read_bytes()
        snapshot = _capture_case(case_root, run_bytes_override=run_raw)
        solution = (
            {"schema_version": _SOLUTION_SCHEMA_VERSION, "kind": "local-computation", "status": "no_saved_point", "results": []}
            if solution_path == _NO_SAVED_POINT
            else _load_solution(solution_path, case_root)
        )
        report = _make_check_report(snapshot, solution, started_at=started)
        artifact_root = Path(artifact_dir)
        artifact_root.mkdir()
        with open(artifact_root / "child-check.json", "xb") as stream:
            stream.write(canonical_json_bytes(report))
            stream.flush()
            os.fsync(stream.fileno())
        return 0
    except Exception as exc:
        error = _error_record(exc)
        try:
            error_path = Path(run_snapshot).parent / "child-error.json"
            with open(error_path, "xb") as stream:
                stream.write(canonical_json_bytes(error))
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            pass
        return 2


def _default_solution_path(case_dir: Path) -> str:
    """Use the latest local attempt if present, even when it has no point."""
    local_attempt = case_dir / "results" / "local"
    if os.path.lexists(local_attempt):
        return "results/local/solution.json" if os.path.lexists(local_attempt / "solution.json") else _NO_SAVED_POINT
    return "results/selected/solution.json" if os.path.lexists(case_dir / "results" / "selected" / "solution.json") else _NO_SAVED_POINT


def _process_tree_termination_scope() -> str:
    return "process_group" if os.name == "posix" else "direct_child_only"


def solve_case(
    case_directory: str | os.PathLike[str],
    *,
    output_directory: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Run the visible case in one child process with one whole-call deadline."""
    started = time.monotonic()
    case_dir = _absolute_directory(case_directory, label="case directory")
    target, replace_existing = _output_target(case_dir, output_directory)
    stage = _make_stage(target, replace_existing=replace_existing, create_results_parent=output_directory is None)
    artifact_dir = stage.joinpath(*PurePosixPath(_LOCAL_ARTIFACT_DIRECTORY).parts)
    status = "failed"
    exit_code: int | None = None
    timed_out = False
    error: dict[str, Any] | None = None
    run_snapshot: Path | None = None
    report: dict[str, Any] | None = None
    try:
        artifact_dir.parent.mkdir()
        run_raw = _read_case_bytes(case_dir, _RUN_CONFIG_PATH)
        config = _json_bytes(run_raw, label="run.json")
        if not _valid_run_fields(config) or type(config.get("schema_version")) is not int or config["schema_version"] != 1:
            raise ContractError("output_invalid", "run.json has invalid fields")
        options = config.get("solve_options")
        if not isinstance(options, dict):
            raise ContractError("output_invalid", "solve_options must be an object")
        options = _validate_local_option_shape(options)
        budget = options["execution_budget_seconds"]
        run_snapshot = stage / "run-config.snapshot.json"
        with open(run_snapshot, "xb") as stream:
            stream.write(run_raw)
            stream.flush()
            os.fsync(stream.fileno())
        exit_code, timed_out = _spawn_case_child(
            case_dir,
            artifact_dir,
            run_snapshot,
            operation="solve",
            budget_seconds=float(budget),
            started_at=started,
        )
        if not timed_out and exit_code == 0:
            try:
                report = _json_bytes((artifact_dir / "child-report.json").read_bytes(), label="child report")
            except (OSError, ContractError):
                report = None
        if timed_out:
            status = "timeout"
        elif report is None:
            status = "failed"
            child_error = None
            try:
                child_error = _json_bytes((stage / "child-error.json").read_bytes(), label="child error")
            except (OSError, ContractError):
                pass
            error = {"code": "child_execution_failed", "exit_code": exit_code, "child_error": child_error}
        else:
            status = report.get("status") if report.get("status") in {"completed", "partial"} else "unknown"
        solution_rows = (
            _collect_solution_rows(artifact_dir, report.get("instances", []))
            if report is not None
            else _recover_solution_rows(artifact_dir)
        )
        solution = {
            "schema_version": _SOLUTION_SCHEMA_VERSION,
            "kind": "local-computation",
            "status": status,
            "results": solution_rows,
        }
        _atomic_stage_json(stage, "solution.json", solution)
        result = {
            "schema_version": 1,
            "kind": "local-computation",
            "status": status,
            "input_digest": report.get("input_digest") if report else None,
            "backend_id": report.get("backend_id") if report else config.get("backend_id"),
            "execution_budget_seconds": float(budget),
            "execution_elapsed_seconds": time.monotonic() - started,
            "solver_invoked": report.get("solver_invoked") if report else None,
            "solver_returned": report.get("solver_returned") if report else None,
            "batch_summary": report.get("batch_summary") if report else None,
            "result_count": len(solution_rows),
            "solution_path": "solution.json",
            "diagnostics": {"exit_code": exit_code, "timed_out": timed_out},
            "process_tree_termination_scope": _process_tree_termination_scope(),
            "error": error,
        }
        result.update(_solution_result_counts(solution_rows))
        if report is not None:
            result["backend_profile"] = report.get("backend_profile")
            result["solve_options"] = report.get("solve_options")
            result["consumed_inputs"] = report.get("consumed_inputs")
            result["output_refs"] = report.get("output_refs")
        _atomic_stage_json(stage, _LOCAL_EXECUTION_PATH, result)
    except Exception as exc:
        error = _error_record(exc)
        status = "failed"
        recovered = _recover_solution_rows(artifact_dir)
        _atomic_stage_json(
            stage,
            "solution.json",
            {"schema_version": _SOLUTION_SCHEMA_VERSION, "kind": "local-computation", "status": status, "results": recovered},
        )
        _atomic_stage_json(
            stage,
            _LOCAL_EXECUTION_PATH,
            {
                "schema_version": 1,
                "kind": "local-computation",
                "status": status,
                "execution_elapsed_seconds": time.monotonic() - started,
                "result_count": len(recovered),
                "solution_path": "solution.json",
                "diagnostics": {"exit_code": exit_code, "timed_out": timed_out},
                "process_tree_termination_scope": _process_tree_termination_scope(),
                "error": error,
                **_solution_result_counts(recovered),
            },
        )
    if run_snapshot is not None:
        run_snapshot.unlink(missing_ok=True)
    (stage / "child-error.json").unlink(missing_ok=True)
    execution_result = _json_bytes(
        stage.joinpath(*PurePosixPath(_LOCAL_EXECUTION_PATH).parts).read_bytes(),
        label="local execution result",
    )
    _atomic_stage_text(stage, "summary.md", _solve_summary_markdown(execution_result, stage))
    if report is not None and status == "completed":
        artifact_dir.joinpath("child-report.json").unlink(missing_ok=True)
    _publish_stage(stage, target, replace_existing=replace_existing)
    return _json_bytes(
        target.joinpath(*PurePosixPath(_LOCAL_EXECUTION_PATH).parts).read_bytes(),
        label="local execution result",
    )


def _error_record(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, ContractError):
        return {"code": exc.code, "message": exc.message, "pointer": exc.pointer, "details": dict(exc.details)}
    return {"code": "local_execution_error", "error_type": type(exc).__name__}


def _typed_key(value: Any, *, pointer: str) -> tuple[str | int, ...]:
    if not isinstance(value, list) or any(type(item) not in (str, int) for item in value):
        raise ContractError("output_invalid", "instance key must be a typed string/integer array", pointer=pointer)
    return tuple(value)


def _variable_key(variable: Any) -> tuple[str | int, ...]:
    index = variable.index()
    value = index if isinstance(index, tuple) else () if index is None else (index,)
    if any(type(item) not in (str, int) for item in value):
        raise ContractError("capability_uncovered", "current model uses a variable key outside the typed witness format", details={"component": variable.parent_component().name})
    return tuple(value)


def _saved_primals(model: Any, witness: Mapping[str, Any], *, row_index: int) -> Any:
    from pyomo.common.collections import ComponentMap
    from pyomo.environ import Var, value as pyomo_value

    if set(witness) != _WITNESS_FIELDS or not isinstance(witness.get("variables"), list) or not isinstance(witness.get("objective_values"), list):
        raise ContractError("output_invalid", "saved witness has invalid fields", pointer=f"/results/{row_index}/witness")
    variables = list(model.component_data_objects(Var, active=True, descend_into=True))
    current: dict[tuple[str, tuple[str | int, ...]], Any] = {}
    for variable in variables:
        key = (variable.parent_component().name, _variable_key(variable))
        if key in current:
            raise ContractError("capability_uncovered", "current model has a duplicate typed variable key")
        current[key] = variable
    primals: Any = ComponentMap()
    seen: set[tuple[str, tuple[str | int, ...]]] = set()
    for index, item in enumerate(witness["variables"]):
        pointer = f"/results/{row_index}/witness/variables/{index}"
        if not isinstance(item, dict) or set(item) != _VARIABLE_FIELDS:
            raise ContractError("output_invalid", "saved variable entry has invalid fields", pointer=pointer)
        component = item["component"]
        if not isinstance(component, str) or not component:
            raise ContractError("output_invalid", "saved variable component is invalid", pointer=f"{pointer}/component")
        key = _typed_key(item["key"], pointer=f"{pointer}/key")
        identity = (component, key)
        if identity in seen:
            raise ContractError("output_invalid", "saved point contains a duplicate variable key", pointer=pointer)
        seen.add(identity)
        variable = current.get(identity)
        if variable is None:
            raise ContractError("artifact_changed", "saved point contains a variable absent from the current model", pointer=pointer)
        raw = item["value"]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
            raise ContractError("output_invalid", "saved variable value must be finite", pointer=f"{pointer}/value")
        if item["domain"] is not None and not isinstance(item["domain"], str):
            raise ContractError("output_invalid", "saved variable domain label is invalid", pointer=f"{pointer}/domain")
        if item["origin"] not in {"fixed_model_value", "solver_primal", "deterministic_completion"}:
            raise ContractError("output_invalid", "saved variable origin is invalid", pointer=f"{pointer}/origin")
        if bool(variable.fixed):
            try:
                fixed_value = pyomo_value(variable, exception=False)
            except Exception as exc:
                raise ContractError("capability_uncovered", "current fixed variable value cannot be read", pointer=pointer) from exc
            if isinstance(fixed_value, bool) or not isinstance(fixed_value, (int, float)) or not math.isfinite(float(fixed_value)) or raw != float(fixed_value):
                raise ContractError("artifact_changed", "saved point changes a currently fixed model value", pointer=f"{pointer}/value")
        primals[variable] = raw
    missing = set(current) - seen
    if missing:
        component, _ = sorted(missing, key=lambda item: (item[0], repr(item[1])))[0]
        raise ContractError("output_invalid", "saved point is missing an active model variable", details={"component": component, "missing_count": len(missing)})
    return primals


def _load_solution(solution_path: str | os.PathLike[str], case_dir: Path) -> dict[str, Any]:
    requested = Path(solution_path)
    target = requested if requested.is_absolute() else case_dir / requested
    target = Path(os.path.abspath(os.fspath(target)))
    try:
        target.relative_to(case_dir)
    except ValueError as exc:
        raise ContractError("path_denied", "solution file must stay inside the case directory") from exc
    relative = target.relative_to(case_dir).as_posix()
    raw = _read_case_bytes(case_dir, relative)
    value = _json_bytes(raw, label="solution.json")
    if set(value) != _SOLUTION_FIELDS or type(value.get("schema_version")) is not int or value["schema_version"] != _SOLUTION_SCHEMA_VERSION:
        raise ContractError("output_invalid", "solution.json has invalid fields")
    if value["kind"] not in {"selected-run-solution", "local-computation"} or not isinstance(value["results"], list):
        raise ContractError("output_invalid", "solution.json kind or results are invalid")
    return value


def _objective_values_have_same_identity(
    left: Sequence[Mapping[str, Any]],
    right: Sequence[Mapping[str, Any]],
) -> bool:
    # Validate each side while allowing the current model's objective metadata
    # to differ from the metadata frozen with the saved point.
    objective_values_match(left, left)
    objective_values_match(right, right)
    identity_fields = ("component", "sense", "unit")
    return len(left) == len(right) and all(
        all(left_row[field] == right_row[field] for field in identity_fields)
        for left_row, right_row in zip(left, right, strict=True)
    )


def _display_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _display_objectives(values: Any) -> str:
    if not isinstance(values, list) or not values:
        return "未提供"
    rendered: list[str] = []
    for item in values:
        if not isinstance(item, Mapping):
            rendered.append(_display_json(item))
            continue
        component = item.get("component", "目标")
        sense = item.get("sense")
        unit = item.get("unit")
        value = item.get("value", "未提供")
        parts = [str(component)]
        if isinstance(sense, str) and sense:
            parts.append(sense)
        text = f"{' / '.join(parts)} = {value}"
        if isinstance(unit, str) and unit:
            text += f" {unit}"
        rendered.append(text)
    return "；".join(rendered)


def _append_check_details(lines: list[str], label: str, record: Any) -> None:
    if not isinstance(record, Mapping):
        lines.append(f"- {label}：本次记录未取得。")
        return
    status = record.get("status", "unknown")
    lines.append(f"- {label}状态：`{status}`。")
    if "solution" in record:
        lines.append(f"- {label}结构化方案：已随结果 JSON 保存。")
    uncovered = record.get("uncovered")
    violations = record.get("violations")
    if isinstance(uncovered, list) and uncovered:
        lines.append("  - 未覆盖：" + "；".join(str(item) for item in uncovered))
    if isinstance(violations, list) and violations:
        lines.append("  - 记录的违反项：" + "；".join(str(item) for item in violations))
    obligations = record.get("checked_obligations")
    if isinstance(obligations, list):
        for obligation in obligations:
            if not isinstance(obligation, Mapping):
                continue
            obligation_label = obligation.get("label")
            evidence = obligation.get("evidence")
            if not isinstance(obligation_label, str) or not isinstance(evidence, str):
                continue
            lines.append(f"  - **{obligation_label}**")
            if evidence:
                lines.extend(f"    {line}" for line in evidence.splitlines())
            else:
                lines.append("    （无文本证据）")


def _append_current_violations(lines: list[str], violations: Any) -> None:
    if not isinstance(violations, list) or not violations:
        return
    concrete_errors = {
        "constraint_violation",
        "lower_bound_violation",
        "upper_bound_violation",
        "integrality_violation",
    }
    rendered_labels: set[str] = set()
    for item in violations:
        if not isinstance(item, Mapping):
            continue
        kind = item.get("kind")
        component = item.get("component")
        error = item.get("error")
        if not isinstance(kind, str) or not isinstance(component, str):
            continue
        is_violation = error in concrete_errors
        label = "当前模型实际违例" if is_violation else "当前模型核对明细"
        object_label = {"variable": "变量", "constraint": "约束"}.get(kind, kind)
        detail = f"  - {object_label} `{component}`"
        key = item.get("key")
        if isinstance(key, list):
            detail += f"（键 `{_display_json(key)}`）"
        if isinstance(error, str):
            detail += f"：`{error}`"
        bounds = []
        for field, field_label in (("lower", "下界"), ("body", "本次值"), ("upper", "上界"), ("violation", "违反量")):
            value = item.get(field)
            if value is not None:
                bounds.append(f"{field_label} `{_display_json(value)}`")
        if bounds:
            detail += "；" + "，".join(bounds)
        if label not in rendered_labels:
            lines.append(f"- {label}：")
            rendered_labels.add(label)
        lines.append(detail)


def _visible_log_paths(stage: Path) -> list[str]:
    artifact_root = stage.joinpath(*PurePosixPath(_LOCAL_ARTIFACT_DIRECTORY).parts)
    try:
        root_info = artifact_root.lstat()
    except OSError:
        return []
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        return []
    found: list[str] = []
    try:
        candidates = artifact_root.rglob("*.log")
        for path in candidates:
            try:
                info = path.lstat()
                if not stat.S_ISLNK(info.st_mode) and stat.S_ISREG(info.st_mode):
                    found.append(path.relative_to(stage).as_posix())
            except OSError:
                continue
    except OSError:
        return []
    return sorted(found)


def _solve_summary_markdown(execution: Mapping[str, Any], stage: Path) -> str:
    solution_relative = _relative_path(execution.get("solution_path"), pointer="/solution_path")
    solution_file = stage.joinpath(*PurePosixPath(solution_relative).parts)
    solution = _json_bytes(solution_file.read_bytes(), label="local solution")
    rows = solution.get("results")
    rows = rows if isinstance(rows, list) else []
    status = execution.get("status", "unknown")
    lines = [
        "# 本次求解",
        "",
        f"- 本次尝试状态：`{status}`。",
        f"- 本次保存结果数：{len(rows)}。",
    ]
    if execution.get("backend_id") is not None:
        lines.append(f"- 求解接口：`{execution['backend_id']}`。")
    diagnostics = execution.get("diagnostics")
    if isinstance(diagnostics, Mapping) and diagnostics.get("timed_out") is True:
        lines.append("- 本次整体执行达到时限；如有恢复点，仍按本次真实文件列出。")
    batch_summary = execution.get("batch_summary")
    if isinstance(batch_summary, Mapping) and batch_summary.get("stop_reason") is not None:
        lines.append(f"- 批次停止原因：`{batch_summary['stop_reason']}`。")
    error = execution.get("error")
    if isinstance(error, Mapping) and error.get("code"):
        lines.append(f"- 本次执行记录的错误：`{error['code']}`。")
    if not rows:
        lines.extend(["", "本次没有恢复或保存结果点。"])
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            continue
        result = row.get("result")
        result = result if isinstance(result, Mapping) else {}
        witness = row.get("witness")
        witness = witness if isinstance(witness, Mapping) else None
        instance_key = result.get("instance_key")
        layer = result.get("layer")
        lines.extend([
            "",
            f"## 结果 {index}",
            "",
            f"- 实例键：`{_display_json(instance_key)}`。",
            f"- 目标层：`{_display_json(layer)}`。",
        ])
        for field, label in (("raw_status", "求解状态"), ("termination", "终止原因")):
            if result.get(field) is not None:
                lines.append(f"- {label}：`{result[field]}`。")
        objective_values = witness.get("objective_values") if witness is not None else None
        if not isinstance(objective_values, list) or not objective_values:
            objective = result.get("objective")
            objective_values = [dict(objective)] if isinstance(objective, Mapping) else []
        lines.append(f"- 已保存目标值：{_display_objectives(objective_values)}。")
        if witness is None:
            lines.append("- 本次未取得对应见证。")
        _append_check_details(lines, "模型核对", row.get("model_check"))
        _append_check_details(lines, "业务核对", row.get("business_check"))
        warnings = result.get("warnings")
        if isinstance(warnings, list) and warnings:
            lines.append("- Result 中记录的提示：" + "；".join(str(item) for item in warnings))
    log_paths = _visible_log_paths(stage)
    lines.extend([
        "",
        "## 本次执行文件",
        "",
        "- 机器结果：`.optagent/execution.json`。",
        "- 保存结果：`solution.json`。",
    ])
    if log_paths:
        lines.append("- 本次求解日志：")
        lines.extend(f"  - [`{path}`]({path})" for path in log_paths)
    else:
        lines.append("- 本次没有生成求解器日志文件。")
    return "\n".join(lines) + "\n"


def _check_summary_markdown(report: Mapping[str, Any], stage: Path | None = None) -> str:
    rows = report.get("results")
    rows = rows if isinstance(rows, list) else []
    coverage = report.get("current_plan_coverage")
    coverage_known = isinstance(coverage, Mapping)
    coverage = coverage if coverage_known else {}
    provided_count = report.get("provided_result_count")
    missing_count = coverage.get("missing_count") if coverage_known else None
    status = report.get("status", "unknown")
    lines = [
        "# 本次核对",
        "",
        f"- 本次核对操作状态：`{status}`。",
    ]
    if type(provided_count) is int:
        lines.extend([
            f"- 本次提供的 {provided_count} 个保存结果的检查状态：`{report.get('validation_status', 'uncovered')}`。",
            f"- 对提供结果的逐点检查通过：`{report.get('checks_passed') is True}`。",
        ])
    else:
        lines.append("- 本次未能完成对保存结果的逐点检查。")
    source_path = report.get("source_solution_path")
    lines.append(f"- 保存点来源：`{source_path}`。" if isinstance(source_path, str) else "- 保存点来源：本次没有可用保存点。")
    source_status = report.get("source_solution_status")
    if source_status is not None:
        lines.append(f"- 来源 attempt 状态：`{source_status}`。")
    model_checked_rows = any(
        isinstance(row, Mapping) and isinstance(row.get("current_model_check"), Mapping)
        for row in rows
    )
    direct_checked_rows = any(
        isinstance(row, Mapping)
        and isinstance(row.get("scope"), Mapping)
        and row["scope"].get("current_base_model_feasibility") == "not_applicable_direct_python"
        and isinstance(row.get("current_business_check"), Mapping)
        for row in rows
    )
    if model_checked_rows:
        lines.append("- 已按本次输入和代码对可用保存见证进行逐点模型核对；实际目标核对见各点记录。")
    if direct_checked_rows:
        lines.append("- 已按本次输入和代码对保存的 direct_python 点重新执行 BusinessCheck；本次只核对该保存点，不调用 solve。")
    elif not rows and status == "completed" and (
        report.get("source_solution_status") == "no_saved_point"
        or type(provided_count) is int and provided_count == 0
    ):
        lines.append("- 本次没有可用保存结果点，未执行逐点模型重算。")
    elif not rows:
        lines.append("- 本次未取得完整逐点核对记录。")
    if coverage_known:
        lines.extend([
            "",
            "## 当前计划覆盖",
            "",
            f"- 当前 instance×layer 计划项：{coverage.get('expected_identity_count', 0)}。",
            f"- 有对应保存结果行：{coverage.get('matched_provided_identity_count', 0)}。",
            f"- 未提供项：{missing_count}。",
        ])
    else:
        lines.extend(["", "本次未能完成当前计划覆盖统计。"])
    if type(missing_count) is int and missing_count > 0:
        lines.append(f"当前计划另 {missing_count} 项未提供，未覆盖全计划。")
        missing = coverage.get("missing")
        if isinstance(missing, list):
            lines.append("")
            lines.append("未提供的实例键与层：")
            for item in missing:
                if isinstance(item, Mapping):
                    lines.append(f"- 实例键 `{_display_json(item.get('instance_key'))}`，层 `{_display_json(item.get('layer'))}`。")
    if coverage_known:
        lines.extend([
            "",
            "当前计划覆盖只说明保存结果身份是否提供；有结果行但无见证时，该点仍未完成核对。历史 lex 锁与全局最优性均未检查。",
        ])
    heading = f"## 提供的 {provided_count} 个保存结果的检查" if type(provided_count) is int else "## 提供结果的当前检查"
    lines.extend(["", heading])
    if not rows:
        lines.extend(["", "本次没有可逐点列出的保存结果。"])
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            continue
        result = row.get("saved_result")
        result = result if isinstance(result, Mapping) else {}
        lines.extend([
            "",
            f"### 结果 {index}",
            "",
            f"- 实例键：`{_display_json(result.get('instance_key'))}`。",
            f"- 目标层：`{_display_json(result.get('layer'))}`。",
            f"- 当前逐点状态：`{row.get('validation_status', row.get('status', 'unknown'))}`。",
        ])
        if row.get("reason") == "no_saved_witness":
            lines.append("- 保存结果没有见证，本次无法据此重算核对。")
            continue
        lines.append(f"- 保存目标：{_display_objectives(row.get('saved_objective_values'))}。")
        lines.append(f"- 当前重算目标：{_display_objectives(row.get('current_objective_values'))}。")
        _append_check_details(lines, "当前模型核对", row.get("current_model_check"))
        _append_current_violations(lines, row.get("current_violations"))
        _append_check_details(lines, "当前业务核对", row.get("current_business_check"))
        business_diagnostic = row.get("current_business_checker_diagnostic")
        if isinstance(business_diagnostic, Mapping):
            lines.append(
                "- 当前业务核对诊断："
                + _display_json({
                    "code": business_diagnostic.get("code"),
                    "message": business_diagnostic.get("message"),
                    "pointer": business_diagnostic.get("pointer"),
                })
                + "。"
            )
        lines.append(f"- 历史目标值与当前值相同：`{row.get('saved_objective_matches_current') is True}`。")
    lines.extend([
        "",
        "## 本次机器记录",
        "",
        "- `.optagent/check.json` 保存本次当前检查结果。",
    ])
    error = report.get("error")
    if isinstance(error, Mapping) and error.get("code"):
        lines.append(f"- 本次检查记录的错误：`{error['code']}`。")
    if stage is not None:
        log_paths = _visible_log_paths(stage)
        if log_paths:
            lines.append("- 本次检查日志：")
            lines.extend(f"  - [`{path}`]({path})" for path in log_paths)
        else:
            lines.append("- 本次没有生成检查日志文件。")
    return "\n".join(lines) + "\n"


def _check_rows(snapshot: Mapping[str, Any], solution: Mapping[str, Any]) -> list[dict[str, Any]]:
    from .worker import _objective_list

    def single_objective_matches(
        value: Any,
        objectives: Sequence[Mapping[str, Any]],
        *,
        exact_values: bool = False,
    ) -> bool:
        if value is None:
            return not objectives
        if not isinstance(value, Mapping):
            return False
        try:
            compare = objective_values_exactly_match if exact_values else objective_values_match
            return compare([dict(value)], objectives)
        except ContractError:
            return False

    config = snapshot["run_config"]
    bundle: DataBundle = snapshot["data_bundle"]
    plan = config["objective_plan"]
    options = config["solve_options"]
    feasibility = options.get("feasibility_tolerance")
    integrality = options.get("integrality_tolerance")
    for name, tolerance in (("feasibility_tolerance", feasibility), ("integrality_tolerance", integrality)):
        if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not math.isfinite(float(tolerance)) or float(tolerance) < 0:
            raise ContractError("output_invalid", f"{name} is invalid in run.json")
    layers = _objective_list(plan)
    parameter_id = config["instance_parameter_object_id"]
    expected_instance_keys = [()] if parameter_id is None else list(bundle.object(parameter_id).keys)
    expected_keys = set(expected_instance_keys)
    reader = snapshot["reader"]
    rows: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str | int, ...], int | None]] = set()
    for row_index, item in enumerate(solution["results"]):
        pointer = f"/results/{row_index}"
        if (
            not isinstance(item, dict)
            or set(item) != _SOLUTION_ROW_FIELDS
            or not isinstance(item.get("result"), dict)
            or item.get("model_check") is not None and not isinstance(item.get("model_check"), dict)
            or item.get("business_check") is not None and not isinstance(item.get("business_check"), dict)
        ):
            raise ContractError("output_invalid", "solution result entry has invalid fields", pointer=pointer)
        result = item["result"]
        instance_key = _typed_key(result.get("instance_key"), pointer=f"{pointer}/result/instance_key")
        if instance_key not in expected_keys:
            raise ContractError("artifact_changed", "saved result instance key is absent from current inputs", pointer=f"{pointer}/result/instance_key")
        layer = result.get("layer")
        if layer is not None and (type(layer) is not int or layer < 0):
            raise ContractError("output_invalid", "saved result layer is invalid", pointer=f"{pointer}/result/layer")
        layer_comparable = layer in layers
        identity = (instance_key, layer)
        if identity in seen:
            raise ContractError("output_invalid", "solution repeats a typed instance/layer identity", pointer=pointer)
        seen.add(identity)
        witness = item["witness"]
        current_bundle = bundle.for_instance(parameter_id, instance_key)
        instance_context = current_bundle.instance_context
        current_binding = instance_context.instance_binding_digest if instance_context is not None else current_bundle.candidate_binding_digest
        saved_business = item["business_check"]
        saved_model_check = item["model_check"]
        native_cp_sat_result = (
            isinstance(saved_model_check, Mapping)
            and saved_model_check.get("method") == NATIVE_CP_SAT_METHOD
        )
        saved_solution_present = isinstance(saved_business, Mapping) and "solution" in saved_business
        if snapshot["checker_spec"] is None:
            business = business_check_uncovered(
                input_digest=snapshot["input_digest"],
                instance_key=instance_key,
                instance_binding_digest=current_binding,
                reason="business_checker_not_declared",
            )
            if saved_solution_present:
                business["solution"] = saved_business["solution"]
            business_diagnostic = None
        elif not saved_solution_present:
            business = business_check_uncovered(
                input_digest=snapshot["input_digest"],
                instance_key=instance_key,
                instance_binding_digest=current_binding,
                reason="no_saved_business_solution",
            )
            business_diagnostic = None
        else:
            business, _, business_diagnostic, _ = _business_check_solution(
                reader,
                snapshot["checker_spec"],
                list(snapshot["module_specs"]),
                current_bundle,
                saved_business["solution"],
                input_digest=snapshot["input_digest"],
                instance_key=instance_key,
                instance_binding_digest=current_binding,
                expected_objective_values=None,
            )
        current_objectives = business.get("objective_values", [])
        saved_objectives: list[Mapping[str, Any]] = []
        witness_diagnostic: dict[str, Any] | None = None
        saved_witness: Mapping[str, Any] | None = None
        if isinstance(witness, dict) and set(witness) == _WITNESS_FIELDS:
            try:
                witness_key = _typed_key(witness.get("instance_key"), pointer=f"{pointer}/witness/instance_key")
                if witness_key != instance_key or witness.get("input_digest") != result.get("input_digest") or witness.get("instance_binding_digest") != result.get("instance_binding_digest"):
                    raise ContractError("artifact_changed", "saved witness does not match its Result identity", pointer=f"{pointer}/witness")
                if not isinstance(witness.get("objective_values"), list):
                    raise ContractError("output_invalid", "saved witness objective_values must be an array", pointer=f"{pointer}/witness/objective_values")
                saved_witness = witness
                saved_objectives = witness["objective_values"]
                objective_values_match(saved_objectives, saved_objectives)
            except ContractError as exc:
                witness_diagnostic = {"code": exc.code, "message": exc.message, "pointer": exc.pointer}
                saved_objectives = []
        elif witness is not None:
            witness_diagnostic = {"code": "output_invalid", "message": "saved witness has invalid fields", "pointer": f"{pointer}/witness"}
        saved_result_objective = result.get("objective")
        saved_result_objective_matches_witness = single_objective_matches(
            saved_result_objective,
            saved_objectives,
            exact_values=native_cp_sat_result,
        )
        saved_objective_matches_current = False
        saved_result_objective_matches_current_business = single_objective_matches(
            saved_result_objective,
            current_objectives,
            exact_values=native_cp_sat_result,
        )
        business_passed = business.get("status") == "pass" and not business.get("violations") and not business.get("uncovered")
        business_configured = snapshot["checker_spec"] is not None
        current_violations: list[dict[str, Any]] = []
        current_model_check: Mapping[str, Any] | None = None
        model_diagnostic: dict[str, Any] | None = None
        if config["backend_id"] == "direct_python":
            from .results import validate_direct_witness_record

            if not isinstance(result.get("backend"), Mapping) or result["backend"].get("id") != "direct_python":
                raise ContractError("artifact_changed", "saved Result does not match the direct_python case family", pointer=f"{pointer}/result/backend")
            if saved_witness is not None:
                try:
                    saved_witness = validate_direct_witness_record(
                        saved_witness,
                        plan,
                        input_digest=result.get("input_digest"),
                        instance_key=instance_key,
                        instance_binding_digest=result.get("instance_binding_digest"),
                    )
                    saved_objectives = saved_witness["objective_values"]
                except ContractError as exc:
                    witness_diagnostic = {"code": exc.code, "message": exc.message, "pointer": exc.pointer}
                    saved_witness = None
                    saved_objectives = []
            model_passed = True
            current_base_model_feasibility = "not_applicable_direct_python"
            historical_locks = "not_applicable_direct_python"
        else:
            model_passed = False
            current_base_model_feasibility = (
                "not_checked_saved_witness_missing"
                if witness is None
                else "not_checked_saved_witness_invalid"
            )
            historical_locks = "not_checked"
            if saved_witness is not None:
                try:
                    model, _ = build_native_model(
                        reader,
                        current_bundle,
                        snapshot["model_spec"],
                        list(snapshot["module_specs"]),
                        backend_id=str(config["backend_id"]),
                    )
                except ContractError as exc:
                    current_base_model_feasibility = "not_checked_current_model_rebuild_failed"
                    model_diagnostic = {
                        "stage": "rebuild",
                        "code": exc.code,
                        "message": exc.message,
                        "pointer": exc.pointer,
                    }
                except Exception as exc:
                    current_base_model_feasibility = "not_checked_current_model_rebuild_failed"
                    model_diagnostic = {
                        "stage": "rebuild",
                        "code": "current_model_rebuild_error",
                        "error_type": type(exc).__name__,
                    }
                else:
                    if is_native_cp_model_build(model):
                        current_base_model_feasibility = "native_cp_sat_global_constraints_not_rechecked"
                        historical_locks = "not_rechecked_native_cp_sat"
                    else:
                        try:
                            objective = (
                                objective_for_layer(model, plan, layer)
                                if layer_comparable
                                else ObjectiveSpec(None, None, None, None, None)
                            )
                            primals = _saved_primals(model, saved_witness, row_index=row_index)
                            checked = check_pyomo_model(
                                model,
                                primals,
                                objective,
                                input_digest=snapshot["input_digest"],
                                instance_key=instance_key,
                                instance_binding_digest=current_binding,
                                feasibility_tolerance=float(feasibility),
                                integrality_tolerance=float(integrality),
                                violation_sink=lambda value: current_violations.append(dict(value)),
                            )
                        except ContractError as exc:
                            current_base_model_feasibility = "not_checked_current_model_check_failed"
                            model_diagnostic = {
                                "stage": "check",
                                "code": exc.code,
                                "message": exc.message,
                                "pointer": exc.pointer,
                            }
                        except Exception as exc:
                            current_base_model_feasibility = "not_checked_current_model_check_failed"
                            model_diagnostic = {
                                "stage": "check",
                                "code": "current_model_check_error",
                                "error_type": type(exc).__name__,
                            }
                        else:
                            current_model_check = dict(checked.record)
                            model_passed = checked.record.get("status") == "pass" and checked.candidate_complete
                            current_base_model_feasibility = checked.record.get("status", "uncovered")
        saved_objective_matches_current = bool(
            saved_objectives
            and current_objectives
            and _objective_values_have_same_identity(saved_objectives, current_objectives)
            and (
                objective_values_exactly_match(saved_objectives, current_objectives)
                if native_cp_sat_result
                else objective_values_match(saved_objectives, current_objectives)
            )
        )
        business_required = business_configured or saved_solution_present or config["backend_id"] == "direct_python"
        checks_passed = bool(
            model_passed
            and saved_result_objective_matches_witness
            and (business_passed if business_required else True)
            and (business_required or layer_comparable)
        )
        if checks_passed:
            validation_status = "passed"
        elif business.get("status") == "fail" or isinstance(current_model_check, Mapping) and current_model_check.get("status") == "fail":
            validation_status = "failed"
        else:
            validation_status = "uncovered"
        row_record = {
            "saved_result": result,
            "saved_witness": witness,
            "status": "checked",
            "validation_status": validation_status,
            "checks_passed": checks_passed,
            "current_input_digest": snapshot["input_digest"],
            "current_instance_binding_digest": current_binding,
            "current_model_check": dict(current_model_check) if current_model_check is not None else None,
            "current_violations": current_violations,
            "current_business_check": business,
            "saved_objective_values": saved_objectives,
            "current_objective_values": list(current_objectives),
            "saved_objective_matches_current": saved_objective_matches_current,
            "saved_result_objective_matches_witness": saved_result_objective_matches_witness,
            "saved_result_objective_matches_current_business": saved_result_objective_matches_current_business,
            "scope": {
                "current_base_model_feasibility": current_base_model_feasibility,
                "current_business_obligations": business.get("status", "uncovered") if business_configured else "uncovered_not_configured",
                "current_layer_objective": "recomputed_by_business_check" if current_objectives else (
                    "unavailable_not_comparable" if not layer_comparable else "not_applicable_or_uncovered"
                ),
                "historical_lexicographic_locks": historical_locks,
                "global_optimality": "not_checked",
            },
        }
        if business_diagnostic is not None:
            row_record["current_business_checker_diagnostic"] = business_diagnostic
        if witness_diagnostic is not None:
            row_record["saved_witness_diagnostic"] = witness_diagnostic
        if model_diagnostic is not None:
            row_record["current_model_check_diagnostic"] = model_diagnostic
        rows.append(row_record)
    return rows


def _make_check_report(
    snapshot: Mapping[str, Any],
    solution: Mapping[str, Any],
    *,
    started_at: float,
) -> dict[str, Any]:
    rows = _check_rows(snapshot, solution)
    config = snapshot["run_config"]
    bundle: DataBundle = snapshot["data_bundle"]
    parameter_id = config["instance_parameter_object_id"]
    expected_instance_keys = [()] if parameter_id is None else list(bundle.object(parameter_id).keys)
    from .worker import _objective_list

    layers = _objective_list(config["objective_plan"])
    available_identities: set[tuple[tuple[str | int, ...], int | None]] = set()
    for item in solution["results"]:
        result = item["result"]
        key = tuple(result["instance_key"])
        layer = result.get("layer")
        if layer in layers:
            available_identities.add((key, layer))
    missing_identities = [
        {"instance_key": list(key), "layer": layer}
        for key in expected_instance_keys
        for layer in layers
        if (key, layer) not in available_identities
    ]
    coverage = {
        "expected_identity_count": len(expected_instance_keys) * len(layers),
        "matched_provided_identity_count": len(available_identities),
        "missing_count": len(missing_identities),
        "missing": missing_identities,
    }
    solution_incomplete = solution.get("status") in {"partial", "timeout", "failed", "unknown"}
    checks_passed = bool(rows) and not solution_incomplete and all(row.get("checks_passed") is True for row in rows)
    business_configured = snapshot["checker_spec"] is not None
    native_cp_sat_unrechecked = any(
        row.get("scope", {}).get("current_base_model_feasibility") == "native_cp_sat_global_constraints_not_rechecked"
        for row in rows
        if isinstance(row.get("scope"), Mapping)
    )
    current_model_not_checked = any(
        isinstance(row.get("scope"), Mapping)
        and isinstance(row["scope"].get("current_base_model_feasibility"), str)
        and row["scope"]["current_base_model_feasibility"].startswith("not_checked_")
        for row in rows
    )
    if checks_passed and not business_configured:
        validation_status = "passed_model_only"
    elif checks_passed:
        validation_status = "passed"
    elif solution_incomplete or not rows or any(row.get("validation_status") == "uncovered" for row in rows):
        validation_status = "uncovered"
    else:
        validation_status = "failed"
    direct_python = config.get("backend_id") == "direct_python"
    if direct_python:
        report_scope = "saved direct_python point checked by current BusinessCheck and objective recomputation; no solve, model-check artifact, or global optimality"
    elif current_model_not_checked:
        report_scope = (
            "current base-model feasibility was not checked for one or more results; "
            "see per-result scope and diagnostics; current business checks and objective comparisons "
            "are reported separately; "
        )
        if native_cp_sat_unrechecked:
            report_scope += "native CP-SAT global constraints are not rechecked where indicated; "
        report_scope += "global optimality not checked; no solve"
    elif native_cp_sat_unrechecked:
        report_scope = "saved point checked against current inputs by BusinessCheck and exact integer objective comparison; current CP-SAT global constraints and global optimality are not rechecked; no solve"
    else:
        report_scope = "current base-model feasibility, business obligations when configured, and current layer objectives; no historical lex locks or global optimality"
    return {
        "schema_version": 1,
        "kind": "local-check",
        "status": "completed",
        "validation_status": validation_status,
        "checks_passed": checks_passed,
        "scope": report_scope,
        "business_scope": (
            "configured_checker_checked" if business_configured else "direct_python_checker_not_configured"
        ) if direct_python else (
            "configured_checker_checked" if business_configured else "model_only_business_uncovered_not_configured"
        ),
        "input_digest": snapshot["input_digest"],
        "source_solution_kind": solution["kind"],
        "source_solution_status": solution["status"],
        "provided_result_count": len(solution["results"]),
        "current_plan_coverage": coverage,
        "process_tree_termination_scope": _process_tree_termination_scope(),
        "results": rows,
        "execution_elapsed_seconds": time.monotonic() - started_at,
    }


def check_case(
    case_directory: str | os.PathLike[str],
    *,
    solution_path: str | os.PathLike[str] | None = None,
    output_directory: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Check a saved raw point on the current base model under one wall budget."""
    started = time.monotonic()
    case_dir = _absolute_directory(case_directory, label="case directory")
    target, replace_existing = _output_target(case_dir, output_directory, default_name="results/check")
    stage = _make_stage(target, replace_existing=replace_existing, create_results_parent=output_directory is None)
    run_snapshot: Path | None = None
    exit_code: int | None = None
    timed_out = False
    try:
        run_raw = _read_case_bytes(case_dir, _RUN_CONFIG_PATH)
        config = _json_bytes(run_raw, label="run.json")
        if not _valid_run_fields(config) or type(config.get("schema_version")) is not int or config["schema_version"] != 1:
            raise ContractError("output_invalid", "run.json has invalid fields")
        options = config.get("solve_options")
        if not isinstance(options, dict):
            raise ContractError("output_invalid", "solve_options must be an object")
        options = _validate_local_option_shape(options)
        budget = float(options["execution_budget_seconds"])
        if solution_path is None:
            saved_relative = _default_solution_path(case_dir)
        else:
            requested_solution = Path(solution_path)
            absolute_solution = requested_solution if requested_solution.is_absolute() else case_dir / requested_solution
            absolute_solution = Path(os.path.abspath(os.fspath(absolute_solution)))
            try:
                saved_relative = absolute_solution.relative_to(case_dir).as_posix()
            except ValueError as exc:
                raise ContractError("path_denied", "solution file must stay inside the case directory") from exc
            saved_relative = _relative_path(saved_relative, pointer="/solution_path")
        run_snapshot = stage / "run-config.snapshot.json"
        with open(run_snapshot, "xb") as stream:
            stream.write(run_raw)
            stream.flush()
            os.fsync(stream.fileno())
        artifact_dir = stage.joinpath(*PurePosixPath(_LOCAL_ARTIFACT_DIRECTORY).parts)
        artifact_dir.parent.mkdir()
        exit_code, timed_out = _spawn_case_child(
            case_dir,
            artifact_dir,
            run_snapshot,
            operation="check",
            budget_seconds=budget,
            started_at=started,
            extra_arguments=(saved_relative,),
        )
        if timed_out:
            report = {
                "schema_version": 1,
                "kind": "local-check",
                "status": "timeout",
                "validation_status": "uncovered",
                "checks_passed": False,
                "scope": "current base-model feasibility, business obligations when configured, and current layer objectives; no historical lex locks or global optimality",
                "results": [],
                "diagnostics": {"exit_code": exit_code, "timed_out": True},
                "error": {"code": "execution_budget_exceeded"},
            }
        elif exit_code == 0:
            report = _json_bytes((artifact_dir / "child-check.json").read_bytes(), label="child check report")
            report["diagnostics"] = {"exit_code": exit_code, "timed_out": False}
        else:
            child_error = None
            try:
                child_error = _json_bytes((stage / "child-error.json").read_bytes(), label="child error")
            except (OSError, ContractError):
                pass
            report = {
                "schema_version": 1,
                "kind": "local-check",
                "status": "failed",
                "validation_status": "uncovered",
                "checks_passed": False,
                "scope": "current base-model feasibility, business obligations when configured, and current layer objectives; no historical lex locks or global optimality",
                "results": [],
                "diagnostics": {"exit_code": exit_code, "timed_out": False},
                "error": {"code": "child_execution_failed", "child_error": child_error},
            }
        report["source_solution_path"] = None if saved_relative == _NO_SAVED_POINT else saved_relative
    except Exception as exc:
        report = {
            "schema_version": 1,
            "kind": "local-check",
            "status": "failed",
            "validation_status": "uncovered",
            "checks_passed": False,
            "execution_elapsed_seconds": time.monotonic() - started,
            "results": [],
            "error": _error_record(exc),
            "source_solution_path": None,
        }
    if run_snapshot is not None:
        run_snapshot.unlink(missing_ok=True)
    (stage / "child-error.json").unlink(missing_ok=True)
    report.setdefault("execution_elapsed_seconds", time.monotonic() - started)
    report.setdefault("process_tree_termination_scope", _process_tree_termination_scope())
    _atomic_stage_json(stage, _CHECK_RECORD_PATH, report)
    _atomic_stage_text(stage, "summary.md", _check_summary_markdown(report, stage))
    if exit_code == 0 and not timed_out and report.get("status") == "completed":
        artifact_dir.joinpath("child-check.json").unlink(missing_ok=True)
    _publish_stage(stage, target, replace_existing=replace_existing)
    return _json_bytes(
        target.joinpath(*PurePosixPath(_CHECK_RECORD_PATH).parts).read_bytes(),
        label="local check result",
    )


def main(argv: Sequence[str] | None = None, *, case_directory: str | os.PathLike[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "_child-solve":
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("command")
        parser.add_argument("case_directory")
        parser.add_argument("artifact_directory")
        parser.add_argument("run_snapshot")
        parsed = parser.parse_args(arguments)
        return _child_main(parsed.case_directory, parsed.artifact_directory, parsed.run_snapshot)
    if arguments and arguments[0] == "_child-check":
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("command")
        parser.add_argument("case_directory")
        parser.add_argument("artifact_directory")
        parser.add_argument("run_snapshot")
        parser.add_argument("solution_path")
        parsed = parser.parse_args(arguments)
        return _child_check_main(parsed.case_directory, parsed.artifact_directory, parsed.run_snapshot, parsed.solution_path)

    parser = argparse.ArgumentParser(prog="run.py", description="Run or check this editable local case.")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    solve_parser = subparsers.add_parser("solve", help="solve current visible inputs and model")
    solve_parser.add_argument("--output", dest="output_directory")
    check_parser = subparsers.add_parser("check", help="check the saved point on the current base model")
    check_parser.add_argument("--solution", dest="solution_path")
    check_parser.add_argument("--output", dest="output_directory")
    parsed = parser.parse_args(arguments)
    root = Path(os.path.abspath(os.fspath(case_directory))) if case_directory is not None else Path.cwd()
    try:
        if parsed.operation == "solve":
            result = solve_case(root, output_directory=parsed.output_directory)
            success = result.get("status") in {"completed", "partial"}
            output_directory = _output_target(root, parsed.output_directory)[0]
            result_files = [output_directory / ".optagent" / "execution.json", output_directory / "solution.json", output_directory / "summary.md"]
        else:
            result = check_case(root, solution_path=parsed.solution_path, output_directory=parsed.output_directory)
            output_directory = _output_target(root, parsed.output_directory, default_name="results/check")[0]
            result_files = [output_directory / ".optagent" / "check.json", output_directory / "summary.md"]
        if parsed.operation == "check":
            success = result.get("checks_passed") is True
        count_value = result.get("result_count") if parsed.operation == "solve" else result.get("provided_result_count")
        summary: dict[str, Any] = {
            "kind": "local-cli-summary",
            "operation": parsed.operation,
            "status": result.get("status"),
            "result_count": count_value if type(count_value) is int and count_value >= 0 else 0,
            "output_directory": os.fspath(output_directory),
            "result_files": [os.fspath(path) for path in result_files],
        }
        for field in ("validation_status", "checks_passed", "backend_id"):
            if field in result:
                summary[field] = result[field]
        if parsed.operation == "solve":
            for field in (
                "termination_counts",
                "checked_point_count",
                "solver_error_observed_count",
                "model_check_status_counts",
                "business_check_status_counts",
                "numerical_optimality_status_counts",
            ):
                if field in result:
                    summary[field] = result[field]
        error = result.get("error")
        if isinstance(error, Mapping):
            error_summary = {key: error[key] for key in ("code", "message", "pointer", "error_type") if key in error}
            child_error = error.get("child_error")
            if isinstance(child_error, Mapping):
                error_summary["cause"] = {
                    key: child_error[key]
                    for key in ("code", "message", "pointer", "error_type")
                    if key in child_error
                }
            summary["error"] = error_summary
        elif isinstance(error, str):
            summary["error"] = error
        sys.stdout.write(json.dumps(summary, ensure_ascii=False, allow_nan=False) + "\n")
        return 0 if success else 1
    except ContractError as exc:
        sys.stderr.write(json.dumps(_error_record(exc), ensure_ascii=False, allow_nan=False) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["check_case", "main", "solve_case"]
