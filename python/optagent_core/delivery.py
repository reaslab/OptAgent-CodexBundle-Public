"""Claim-scoped selection, explicit report output, audit, and single-case export."""

from __future__ import annotations

import ast
import copy
import io
import json
import math
import posixpath
import re
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

from .contracts import ContractError, canonical_json_bytes, ref_from_bytes, require_id, require_stamp, sha256_bytes, strict_json_loads, validate_ref_shape
from .data import PortableReader, load_data_bundle, read_prepared_source_records
from .sources import case_artifact_dir_for_state, new_id, source_projection, utc_now
from .results import _result_numerical_summary
from .store import CaseStore, _media_type as _workspace_media_type


_CLAIM_KINDS = {
    "formulation_fidelity",
    "feasibility",
    "optimality",
    "local_optimality",
    "infeasibility",
    "unboundedness",
    "bound",
    "tradeoff",
    "diagnostic",
}
_NO_PACKAGE_PARTS = {".env", "env", "secrets", "credentials", "runtime", "__pycache__"}
_LEGACY_RUNTIME_HELPER_PATHS = (
    "python/optagent_core/__init__.py",
    "python/optagent_core/contracts.py",
    "python/optagent_core/data.py",
    "python/optagent_core/models.py",
    "python/optagent_core/results.py",
    "python/optagent_core/worker.py",
)
_RUNTIME_HELPER_PATHS = (
    *_LEGACY_RUNTIME_HELPER_PATHS[:5],
    "python/optagent_core/solver_protocol.py",
    *_LEGACY_RUNTIME_HELPER_PATHS[5:],
)
_EDITABLE_SUPPORT_FILES = (
    "__init__.py",
    "contracts.py",
    "data.py",
    "models.py",
    "results.py",
    "solver_protocol.py",
    "worker.py",
    "case_execution.py",
)
_EDITABLE_CASE_RUN_PY = (
    b'"""Thin CLI entry point for an editable local case."""\n'
    b"\n"
    b"from __future__ import annotations\n"
    b"\n"
    b"import sys\n"
    b"from pathlib import Path\n"
    b"\n"
    b"\n"
    b"CASE_ROOT = Path(__file__).absolute().parent\n"
    b"RUNTIME_ZIP = CASE_ROOT / \".optagent\" / \"runtime.zip\"\n"
    b"sys.path.insert(0, str(RUNTIME_ZIP))\n"
    b"\n"
    b"from optagent_core.case_execution import main  # noqa: E402\n"
    b"\n"
    b"\n"
    b"if __name__ == \"__main__\":\n"
    b"    raise SystemExit(main(case_directory=CASE_ROOT))\n"
)
_PREPARED_MANIFEST_FIELDS = {
    "schema_version", "prepared_id", "case_id", "source_revision", "candidate_revision", "candidate_id",
    "promotion_generation", "binding_digest", "sources", "candidate", "data", "runtime_bundle", "solve_options",
}


def _solver_requirements_bytes(
    profiles: Sequence[Mapping[str, Any]],
    *,
    include_legacy_highs: bool = False,
    declared_requirements: Sequence[bytes] = (),
) -> bytes:
    from .solver_protocol import DIRECT_PYTHON_BACKEND_ID, _DISTRIBUTIONS, _direct_python_requirement_entries

    requirements: dict[str, tuple[str, str, set[str | None]]] = {}
    missing: dict[str, str] = {}
    normalized_declarations: list[bytes] = []
    declared_constraints: set[str] = set()
    declared_distributions: set[str] = set()
    observed_only: dict[str, tuple[str, str]] = {}
    observed_versions: dict[str, tuple[str, str]] = {}
    bare_requirements: dict[str, str] = {}
    informational_observations: set[str] = set()
    seen_declarations: set[bytes] = set()
    for raw in declared_requirements:
        if not isinstance(raw, bytes):
            raise ContractError("output_invalid", "frozen root requirements must be bytes", pointer="/requirements.txt")
        if raw in seen_declarations:
            continue
        seen_declarations.add(raw)
        normalized_declarations.append(raw if not raw or raw.endswith((b"\n", b"\r")) else raw + b"\n")
        for distribution, marker, has_constraint in _direct_python_requirement_entries(raw):
            declared_distributions.add(distribution)
            if marker is not None or has_constraint:
                declared_constraints.add(distribution)

    def record_observed(distribution: str, version: str) -> str:
        key = re.sub(r"[-_.]+", "-", distribution).casefold()
        previous_observation = observed_versions.get(key)
        if previous_observation is not None and previous_observation[1] != version:
            raise ContractError(
                "capability_uncovered",
                "selected runs observed conflicting versions of a Python package",
                details={"distribution": distribution},
            )
        observed_versions[key] = (distribution, version)
        return key

    def add_requirement(distribution: str, version: str, markers: set[str | None]) -> None:
        key = record_observed(distribution, version)
        previous = requirements.get(key)
        if previous is not None and previous[1] != version:
            raise ContractError(
                "capability_uncovered",
                "selected runs require conflicting versions of a Python package",
                details={"distribution": distribution},
            )
        combined_markers = set(markers)
        if previous is not None:
            combined_markers.update(previous[2])
        if None in combined_markers:
            combined_markers = {None}
        requirements[key] = (distribution, version, combined_markers)

    if include_legacy_highs:
        for distribution, version in (("numpy", "2.3.2"), ("Pyomo", "6.9.4"), ("highspy", "1.10.0")):
            add_requirement(distribution, version, {None})
    for profile in profiles:
        backend_id = profile.get("id")
        package_versions = profile.get("package_versions")
        if not isinstance(backend_id, str) or not isinstance(package_versions, Mapping):
            continue
        if backend_id == DIRECT_PYTHON_BACKEND_ID:
            for distribution, version in package_versions.items():
                if not isinstance(distribution, str) or not distribution or not isinstance(version, str) or not version or re.fullmatch(r"[A-Za-z0-9.+!_-]+", version) is None:
                    continue
                key = record_observed(distribution, version)
                if key in declared_constraints:
                    previous = observed_only.get(key)
                    if previous is not None and previous[1] != version:
                        raise ContractError(
                            "capability_uncovered",
                            "selected runs observed conflicting versions of a declared Python package",
                            details={"distribution": distribution},
                        )
                    observed_only[key] = (distribution, version)
                else:
                    add_requirement(distribution, version, {None})
            continue
        if backend_id not in _DISTRIBUTIONS:
            continue
        for distribution in _DISTRIBUTIONS[backend_id]:
            version = package_versions.get(distribution)
            if not isinstance(version, str) or not version or re.fullmatch(r"[A-Za-z0-9.+!_-]+", version) is None:
                if backend_id == "cp_sat" and distribution.casefold() == "ortools" and distribution.casefold() in declared_distributions:
                    continue
                missing.setdefault(distribution.casefold(), distribution)
                continue
            key = record_observed(distribution, version)
            if backend_id == "cp_sat" and key == "ortools":
                if key not in declared_distributions:
                    bare_requirements[key] = distribution
                observed_only[key] = (distribution, version)
                informational_observations.add(key)
                missing.pop(key, None)
                continue
            if key in declared_constraints:
                observed_only[key] = (distribution, version)
            else:
                add_requirement(distribution, version, {None})
            missing.pop(key, None)
    if any(profile.get("id") == "cp_sat" for profile in profiles):
        protobuf_key = re.sub(r"[-_.]+", "-", "protobuf").casefold()
        if protobuf_key not in declared_constraints:
            add_requirement("protobuf", "5.29.5", {None})
    lines = [
        "# Core-recorded Python packages; declared Case dependencies and observed runtime versions are included."
    ]
    for _, name in sorted(bare_requirements.items()):
        lines.append(name)
    for _, (name, version, markers) in sorted(requirements.items()):
        for marker in sorted(markers, key=lambda value: "" if value is None else value):
            lines.append(f"{name}=={version}" + (f" ; {marker}" if marker else ""))
    lines.extend(
        f"# Observed runtime version: {name}=={version} (informational only; it does not determine the package requirement)."
        if key in informational_observations
        else f"# Observed runtime version: {name}=={version} (the frozen declaration above includes a version constraint or marker)."
        for key, (name, version) in sorted(observed_only.items())
    )
    if missing:
        lines.extend(
            f"{name}  # version not recorded for the selected solver interface"
            for _, name in sorted(missing.items())
        )
    generated = ("\n".join(lines) + "\n").encode("utf-8")
    declared = b"".join(normalized_declarations)
    if declared and not declared.endswith((b"\n", b"\r")):
        declared += b"\n"
    if declared:
        return declared + (b"\n" if generated else b"") + generated
    return generated


def _observed_default_interface_requirements(
    profiles: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Describe package versions recorded by selected interfaces, without claiming completeness."""
    from .solver_protocol import _DISTRIBUTIONS

    rows: dict[str, dict[str, Any]] = {}
    for profile in profiles:
        backend_id = profile.get("id")
        if not isinstance(backend_id, str) or backend_id not in _DISTRIBUTIONS:
            continue
        package_versions = profile.get("package_versions")
        if not isinstance(package_versions, Mapping):
            package_versions = {}
        for distribution in _DISTRIBUTIONS[backend_id]:
            version = package_versions.get(distribution)
            if not isinstance(version, str) or not version or re.fullmatch(r"[A-Za-z0-9.+!_-]+", version) is None:
                version = None
            key = distribution.casefold()
            previous = rows.get(key)
            if previous is not None and previous["observed_version"] is not None and version is not None and previous["observed_version"] != version:
                # Conflicts are rejected by _solver_requirements_bytes before a plan is returned.
                raise ContractError(
                    "capability_uncovered",
                    "selected runs require conflicting versions of a Python package",
                    details={"distribution": distribution},
                )
            if previous is None or (previous["observed_version"] is None and version is not None):
                rows[key] = {"distribution": distribution, "observed_version": version}
    return [rows[key] for key in sorted(rows)]


def _editable_json_bytes(value: Any) -> bytes:
    """Pretty-print user-editable JSON without changing Core's canonical record encoding."""
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ContractError("output_invalid", "editable JSON projection is not serializable") from exc
    return (rendered + "\n").encode("utf-8")


def _editable_case_not_generated(artifact_dir: str, reason: str, **details: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "not_generated",
        "artifact_dir": artifact_dir,
        "reason": reason,
    }
    if details:
        result["details"] = details
    return result


def _select_editable_inputs_path(
    store: CaseStore,
    artifact_dir: str,
    protected_source_paths: set[str],
) -> str:
    """Choose a readable path for normalized inputs without claiming a Source file."""
    data_dir = _visible_target(store, f"{artifact_dir}/data")
    try:
        data_dir_stat = data_dir.lstat()
    except FileNotFoundError:
        data_dir_stat = None
    if data_dir_stat is not None and (
        stat.S_ISLNK(data_dir_stat.st_mode) or not stat.S_ISDIR(data_dir_stat.st_mode)
    ):
        raise ContractError("path_denied", "editable-case data directory is not a regular directory")

    protected = {path.replace("\\", "/").casefold() for path in protected_source_paths}
    suffix = 0
    while True:
        name = "inputs.json" if suffix == 0 else "run-inputs.json" if suffix == 1 else f"run-inputs-{suffix}.json"
        relative = f"{artifact_dir}/data/{name}"
        if relative.replace("\\", "/").casefold() in protected:
            suffix += 1
            continue
        target = data_dir / name
        try:
            target.lstat()
        except FileNotFoundError:
            return relative
        except OSError as exc:
            raise ContractError("environment_unavailable", "editable-case input path is unavailable") from exc
        suffix += 1


def _read_fixed_product_file(path: Path, *, product_relative_path: str) -> bytes:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ContractError(
                "path_denied",
                f"fixed editable-case support file {product_relative_path} is not regular",
            )
        raw = path.read_bytes()
        after = path.lstat()
    except ContractError:
        raise
    except OSError as exc:
        raise ContractError(
            "environment_unavailable",
            f"fixed editable-case support file {product_relative_path} is unavailable",
        ) from exc
    if (
        stat.S_ISLNK(after.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or len(raw) != after.st_size
    ):
        raise ContractError(
            "source_changed_during_read",
            f"fixed editable-case support file {product_relative_path} changed while reading",
        )
    return raw


def _editable_case_file_bytes(
    store: CaseStore,
    state: Mapping[str, Any],
    delivery: Mapping[str, Any],
    result_projection: Sequence[Mapping[str, Any]],
    *,
    existing_inputs_path: str | None,
) -> tuple[str, dict[str, bytes], str, set[str], list[dict[str, Any]], str] | dict[str, Any]:
    """Project one selected, current Prepared into a small editable local case."""
    artifact_dir = case_artifact_dir_for_state(state)
    run_ids = delivery.get("run_ids")
    if not isinstance(run_ids, list) or not run_ids:
        return _editable_case_not_generated(artifact_dir, "no_selected_run")

    cache: dict[str, dict[str, Any]] = {}
    prepared_contexts: dict[str, Mapping[str, Any]] = {}
    for run_id in run_ids:
        run, _ = _load_run(store, state["case_id"], run_id)
        worker = run.get("worker")
        if run.get("results") or (isinstance(worker, Mapping) and worker.get("seal") is not None):
            context = _verified_run(store, state, run_id, cache, require_current=True)
        else:
            context = _diagnostic_run_context(store, state, run_id)
        prepared = context.get("prepared")
        prepared_id = prepared.get("prepared_id") if isinstance(prepared, Mapping) else None
        if not isinstance(prepared_id, str):
            raise ContractError("artifact_changed", "selected Run has no Prepared identity")
        prepared_contexts[prepared_id] = context

    selected: dict[
        tuple[str, str, int, str],
        tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None],
    ] = {}
    instance_layers: dict[tuple[bytes, int | None], tuple[str, str, int, str]] = {}
    for projected in result_projection:
        result_ref = validate_ref_shape(dict(projected["result_ref"]), pointer="/result_ref")
        parts = _result_path_parts(state["case_id"], result_ref["path"])
        if parts is None:
            continue
        run_id, _ = parts
        verified = _verified_run(store, state, run_id, cache, require_current=True)
        identity = _ref_identity(result_ref)
        result = verified["results"].get(identity)
        if not isinstance(result, dict):
            raise ContractError("artifact_changed", "selected Result is not present in its verified Run")
        witness: dict[str, Any] | None = None
        witness_ref = result.get("witness")
        if witness_ref is not None:
            checked_witness_ref = validate_ref_shape(witness_ref, pointer="/result/witness")
            witness_value = _read_verified_output_json(store, verified, checked_witness_ref)
            if not isinstance(witness_value, dict):
                raise ContractError("output_invalid", "selected Witness is not a JSON object")
            witness = witness_value
        model_check: dict[str, Any] | None = None
        check_value = result.get("check")
        if check_value is not None:
            check_ref = validate_ref_shape(check_value, pointer="/result/check")
            if verified["manifest"].get("candidate", {}).get("authoring") != "direct_python":
                parsed_check = _read_verified_output_json(store, verified, check_ref)
                if not isinstance(parsed_check, dict):
                    raise ContractError("output_invalid", "selected ModelCheck is not a JSON object")
                model_check = parsed_check
        business_check: dict[str, Any] | None = None
        business_path = result_ref["path"].rsplit("/", 1)[0] + "/business-check.json"
        seal_outputs = verified["seal"].get("output_refs", [])
        business_ref = next(
            (
                validate_ref_shape(dict(item), pointer="/seal/output_refs")
                for item in seal_outputs
                if isinstance(item, Mapping) and item.get("path") == business_path
            ),
            None,
        )
        if business_ref is not None:
            parsed_business_check = _read_verified_output_json(store, verified, business_ref)
            if not isinstance(parsed_business_check, dict):
                raise ContractError("output_invalid", "selected BusinessCheck is not a JSON object")
            business_check = parsed_business_check
        instance_identity = (
            canonical_json_bytes(result.get("instance_key")),
            result.get("layer"),
        )
        previous = instance_layers.get(instance_identity)
        if previous is not None and previous != identity:
            return _editable_case_not_generated(
                artifact_dir,
                "multiple_results_for_instance_layer",
                instance_key=result.get("instance_key"),
                layer=result.get("layer"),
            )
        instance_layers[instance_identity] = identity
        selected[identity] = (result, witness, model_check, business_check)

    if len(prepared_contexts) != 1:
        return _editable_case_not_generated(
            artifact_dir,
            "multiple_prepared_inputs_selected" if prepared_contexts else "no_selected_run",
        )
    prepared_id = next(iter(prepared_contexts))
    verified_for_prepared = prepared_contexts[prepared_id]
    manifest = verified_for_prepared["manifest"]
    if manifest.get("schema_version") != 3:
        return _editable_case_not_generated(artifact_dir, "legacy_prepared_has_no_editable_execution_profile")
    candidate = manifest.get("candidate")
    if not isinstance(candidate, Mapping) or not isinstance(candidate.get("model"), Mapping):
        return _editable_case_not_generated(artifact_dir, "selected_prepared_has_no_executable_model")

    planned_files: dict[str, bytes] = {}
    protected_source_paths: set[str] = set()

    def add_file(relative: str, raw: bytes) -> None:
        target_relative = f"{artifact_dir}/{relative}"
        _visible_target(store, target_relative)
        if target_relative in planned_files:
            raise ContractError("package_collision", "selected code paths overlap editable case support files")
        planned_files[target_relative] = raw

    code_specs: list[tuple[Mapping[str, Any], str]] = []
    model_spec = candidate["model"]
    code_specs.append((model_spec, "model"))
    modules = candidate.get("local_modules")
    if not isinstance(modules, list):
        raise ContractError("output_invalid", "selected Prepared local modules are invalid")
    code_specs.extend((item, "module") for item in modules)
    checker_spec = candidate.get("business_checker")
    if checker_spec is not None:
        code_specs.append((checker_spec, "business checker"))

    normalized_paths: dict[str, bytes] = {}
    physical_code_paths: dict[str, str] = {}
    code_import_paths: dict[str, str] = {}
    artifact_prefix = artifact_dir.rstrip("/") + "/"
    for spec, label in code_specs:
        if not isinstance(spec, Mapping) or set(spec) != {"original_path", "bytes"}:
            raise ContractError("output_invalid", f"selected Prepared {label} specification is invalid")
        original = spec.get("original_path")
        if not isinstance(original, str):
            raise ContractError("output_invalid", f"selected Prepared {label} path is invalid")
        _candidate_package_path(original)
        relative = PurePosixPath(original.replace("\\", "/")).as_posix()
        raw = store.read_ref(validate_ref_shape(dict(spec["bytes"])))
        previous_raw = normalized_paths.get(relative)
        if previous_raw is not None:
            if previous_raw != raw:
                raise ContractError(
                    "package_collision",
                    "selected model, modules, or checker path repeats with conflicting bytes",
                )
            protected_source_paths.add(relative)
            continue
        normalized_paths[relative] = raw
        if relative.startswith(artifact_prefix):
            case_relative = relative[len(artifact_prefix):]
            physical = (
                case_relative
                if case_relative == "code" or case_relative.startswith("code/")
                else f"code/{case_relative}"
            )
        else:
            physical = f"code/external/{relative}"
        _candidate_package_path(physical)
        if physical in physical_code_paths:
            raise ContractError("package_collision", "selected code paths map to the same visible case file")
        physical_code_paths[physical] = relative
        code_import_paths[physical] = relative
        protected_source_paths.add(relative)
        add_file(physical, raw)

    source_records = read_prepared_source_records(store, manifest.get("sources", []))
    for source in source_records.values():
        origin = source.get("origin")
        workspace_path = origin.get("workspace_path") if isinstance(origin, Mapping) else None
        if isinstance(workspace_path, str):
            normalized_source_path = PurePosixPath(workspace_path.replace("\\", "/")).as_posix()
            try:
                _candidate_package_path(normalized_source_path)
            except ContractError:
                continue
            protected_source_paths.add(normalized_source_path)

    data_info = manifest.get("data")
    parameter_id: str | None = None
    input_objects: list[dict[str, Any]] = []
    if data_info is not None:
        if not isinstance(data_info, Mapping) or set(data_info) != {"data_id", "manifest", "instance_parameter_object_id"}:
            raise ContractError("output_invalid", "selected Prepared data identity is invalid")
        parameter_id = data_info["instance_parameter_object_id"]
        data_manifest_ref = validate_ref_shape(dict(data_info["manifest"]), pointer="/prepared/data/manifest")
        data_manifest_raw = store.read_ref(data_manifest_ref)
        data_manifest = strict_json_loads(data_manifest_raw, max_bytes=len(data_manifest_raw))
        if not isinstance(data_manifest, Mapping) or not isinstance(data_manifest.get("objects"), list):
            raise ContractError("output_invalid", "selected frozen DataManifest is invalid")
        for index, item in enumerate(data_manifest["objects"]):
            pointer = f"/prepared/data/objects/{index}"
            if not isinstance(item, Mapping) or set(item) != {
                "object_id", "kind", "key_fields", "columns", "values", "source_locations"
            }:
                raise ContractError("output_invalid", "selected frozen DataObject is invalid", pointer=pointer)
            values_ref = validate_ref_shape(dict(item["values"]), pointer=f"{pointer}/values")
            values_raw = store.read_ref(values_ref)
            values = strict_json_loads(values_raw, max_bytes=len(values_raw))
            input_objects.append({
                "object_id": item["object_id"],
                "kind": item["kind"],
                "key_fields": item["key_fields"],
                "columns": item["columns"],
                "values": values,
            })
    inputs = {
        "schema_version": 1,
        "instance_parameter_object_id": parameter_id,
        "objects": input_objects,
    }

    inputs_path = existing_inputs_path or _select_editable_inputs_path(
        store, artifact_dir, protected_source_paths
    )
    inputs_relative = inputs_path.removeprefix(artifact_prefix)

    run_config = {
        "schema_version": 1,
        "model_path": next(
            physical for physical, logical in physical_code_paths.items()
            if logical == PurePosixPath(model_spec["original_path"].replace("\\", "/")).as_posix()
        ),
        "local_modules": [
            next(
                physical for physical, logical in physical_code_paths.items()
                if logical == PurePosixPath(item["original_path"].replace("\\", "/")).as_posix()
            )
            for item in modules
        ],
        "business_checker_path": (
            next(
                physical for physical, logical in physical_code_paths.items()
                if logical == PurePosixPath(checker_spec["original_path"].replace("\\", "/")).as_posix()
            ) if checker_spec is not None else None
        ),
        "inputs_path": inputs_relative,
        "instance_parameter_object_id": parameter_id,
        "backend_id": candidate["backend_id"],
        "objective_plan": candidate["objective_plan"],
        "solve_options": manifest["solve_options"],
    }
    if code_import_paths:
        run_config["code_import_paths"] = code_import_paths

    rows: list[dict[str, Any]] = []
    for identity in sorted(selected):
        result, witness, model_check, business_check = selected[identity]
        rows.append({
            "result": result,
            "witness": witness,
            "model_check": model_check,
            "business_check": business_check,
        })
    checked_points = sum(
        result.get("has_checked_candidate") is True and witness is not None
        for result, witness, _, _ in selected.values()
    )
    status = "complete" if selected and checked_points == len(selected) else "partial" if checked_points else "no_machine_point"
    solution = {
        "schema_version": 2,
        "kind": "selected-run-solution",
        "status": status,
        "results": rows,
    }

    profile = next((result.get("backend") for result, _, _, _ in selected.values()), None)
    observed_profiles = [profile] if isinstance(profile, Mapping) else []
    requirement_profiles = observed_profiles or [{"id": candidate["backend_id"], "package_versions": {}}]
    observed_requirements = _observed_default_interface_requirements(observed_profiles)
    requirements_spec = candidate.get("requirements_file")
    declared_requirements: list[bytes] = []
    if requirements_spec is not None:
        if not isinstance(requirements_spec, Mapping) or set(requirements_spec) != {"path", "bytes"} or requirements_spec.get("path") != "requirements.txt":
            raise ContractError("output_invalid", "selected Prepared root requirements specification is invalid")
        declared_requirements = store.read_ref(validate_ref_shape(dict(requirements_spec["bytes"]), pointer="/prepared/candidate/requirements_file/bytes"))
        declared_requirements = [declared_requirements]
    requirements = _solver_requirements_bytes(
        requirement_profiles,
        declared_requirements=declared_requirements,
    )

    native_cp_sat_selected = any(
        isinstance(row.get("model_check"), Mapping)
        and row["model_check"].get("method") == "core_cp_sat_response"
        for row in rows
    )
    product_python = Path(__file__).resolve().parent.parent
    support_sources = product_python / "optagent_core"
    runtime_files = {
        f"optagent_core/{name}": _read_fixed_product_file(
            support_sources / name,
            product_relative_path=f"python/optagent_core/{name}",
        )
        for name in _EDITABLE_SUPPORT_FILES
    }
    add_file(".optagent/runtime.zip", _zip_bytes(runtime_files))
    add_file("run.py", _EDITABLE_CASE_RUN_PY)
    add_file(inputs_relative, _editable_json_bytes(inputs))
    add_file(".optagent/run.json", _editable_json_bytes(run_config))
    add_file("results/selected/solution.json", canonical_json_bytes(solution))
    add_file("requirements.txt", requirements)
    python_version = profile.get("python_version") if isinstance(profile, Mapping) else None
    implementation = profile.get("python_implementation") if isinstance(profile, Mapping) else None
    direct_python = candidate.get("backend_id") == "direct_python"
    native_budget = manifest["solve_options"].get("time_limit_seconds")
    if direct_python:
        budget_line = "direct_python 不调用托管 native solver；本地执行预算限制整次算法运行或检查，取消机制仍适用。"
        backend_dependencies = "direct_python adapter 不添加接口包；请在工作区根 `requirements.txt` 声明算法与 checker 实际使用的 Python 包。"
        native_controls = "direct_python 不使用 solver 原生时限、线程、随机种子、warm start、日志或模型导出选项；整次操作仍受 `execution_budget_seconds` 限制。"
        model_export_description = ""
        check_scope = "有保存点时，direct_python 的 check 仅按当前输入和 BusinessCheck 核验指定保存点并重算目标；它不调用 `solve`，不生成 ModelCheck，也不检查全局最优性。"
    elif native_cp_sat_selected:
        budget_line = (
            "冻结的求解器原生秒数上限未设置；本地执行预算仍限制整次求解或检查。"
            if native_budget is None
            else f"冻结的求解器原生秒数上限为 {native_budget} 秒；本地执行预算也限制整次操作。"
        )
        backend_dependencies = "Core 保留工作区根 `requirements.txt` 中冻结的任何 `ortools` 声明；未声明时添加裸 `ortools` API 依赖。Result 记录的 OR-Tools 版本仅是托管运行时观测值，不用于推断本地安装版本；`protobuf==5.29.5` 保持固定。"
        native_controls = "应根据总预算与 CP-SAT 实际能力设置 `time_limit_seconds`、`threads` 和 `random_seed`；已拒绝通用 `warm_start` 选项。模型自身的 AddHint 按模型事实保留，并在 Run 记录中记录提示变量数量，不代表冷启动。"
        model_export_description = "启用模型导出时，native CP-SAT 模型使用 OR-Tools 原生 `.textproto` 格式；求解记录另保存二进制 proto、完整整数响应向量与 typed-index 映射。"
        check_scope = "有保存点时，native CpModel 的 check 会用当前输入重新调用 builder，并重新运行已配置的 BusinessCheck、比较整数目标；它不调用 Solve、不重新抽取解，也不独立重核 CP 全局约束或全局最优性，因此 ModelCheck 的当前全局约束状态为未复核。"
    else:
        budget_line = (
            "冻结的求解器原生秒数上限未设置；本地执行预算仍限制整次求解或检查。"
            if native_budget is None
            else f"冻结的求解器原生秒数上限为 {native_budget} 秒；本地执行预算也限制整次操作。"
        )
        backend_dependencies = "Core 附加所选 native backend 的运行接口包；只有所选 Result 记录的版本会固定，未记录版本的包保持未固定。"
        native_controls = "应根据任务目标、总预算和所选接口的实际能力设置 `time_limit_seconds` 与 `threads`；需要可复现且接口支持时再设置 `random_seed`。这些选项不能替代整次执行预算。"
        model_export_description = "启用模型导出时，Pyomo 模型使用 LP 格式。"
        check_scope = "有保存点时，检查依据当前基础模型、当前目标层和已配置的业务约束，不重验历史分层锁定或全局最优性。"
    checker_description = (
        f"业务复核代码位于 `{run_config['business_checker_path']}`。"
        if isinstance(run_config.get("business_checker_path"), str)
        else "本案例没有配置业务复核代码。"
    )
    modules_description = (
        "声明的辅助模块位于 " + ", ".join(f"`{path}`" for path in run_config["local_modules"]) + "."
        if run_config["local_modules"]
        else "本案例没有声明辅助模块。"
    )
    solution_description = (
        "`results/selected/solution.json` 保存本次选择的冻结 Result 与已取得的检查记录。"
        if rows
        else "本次交付未选择可在此展示的数值结果；`results/selected/solution.json` 的 `results` 为空，`no_machine_point` 表示没有可展示的机器点，不代表所选 Run 没有其他状态记录，也不是 Proof witness。"
    )
    runtime_description = (
        f"本次托管运行记录的 Python 实现和版本为 {implementation} {python_version}。"
        if isinstance(implementation, str) and implementation and isinstance(python_version, str) and python_version
        else f"当前 Prepared 声明的 solver 接口为 `{candidate['backend_id']}`；所选 Run 未同时记录可观察的托管 Python 实现和版本。"
    )
    report_refs = delivery.get("reports", [])
    report_entries = []
    if not isinstance(report_refs, list) or any(not isinstance(value, Mapping) for value in report_refs):
        raise ContractError("artifact_changed", "selected Delivery reports are invalid")
    for index, value in enumerate(report_refs):
        report_ref = validate_ref_shape(dict(value), pointer=f"/delivery/reports/{index}")
        workspace_path = report_ref["path"]
        if workspace_path == f"{artifact_dir}/README.md":
            protected_source_paths.add(workspace_path)
        case_relative_path = posixpath.relpath(workspace_path, artifact_dir)
        report_entries.append(
            f"- 案例目录相对路径：`{case_relative_path}`。"
        )
    report_description = (
        "本次 Review 选定的报告/说明：\n" + "\n".join(report_entries)
        if report_entries
        else "本次交付没有选定的 Review 报告/说明。"
    )
    report_preservation = "本地投影不会复制、覆盖或改写该报告。" if report_entries else ""
    readme = (
        "# 可编辑的优化案例\n\n"
        f"本目录包含选定的模型、检查代码、数据和本次选择的结果（若有）。`{inputs_relative}` 是求解与检查实际使用的数据；原 CSV、PDF 等文件只是导入来源，修改来源不会自动更新此文件。\n\n"
        f"模型代码位于 `{run_config['model_path']}`。{checker_description} {modules_description} `.optagent/run.json` 记录这些文件的路径和导入映射。请在复制或下载案例时保留隐藏目录 `.optagent/`，其中包含运行必需的配置和 helper。\n\n"
        f"{report_description} {report_preservation} {solution_description}\n\n"
        "## 求解与检查\n\n"
        "`run.py` 从 `.optagent/runtime.zip` 加载本地运行代码。`solve` 按当前模型和数据重新计算；本地新计算不继承原交付的审核结论，也不会改变平台运行状态。请使用与模型兼容的 Python 环境，并按 `requirements.txt` 安装所需 Python 包；系统求解器程序和商业许可可能需要另行配置。"
        f"{backend_dependencies} 本镜像提供本地 Python 解释器与标准库，但不保证提供本地 pip 或这些第三方包。若需安装，请在该案例实际使用的本地 Python 环境中使用其包管理器；`python-execute` 与本地环境分离，其包安装不会为本地案例安装依赖。`requirements.txt` 不保证列出模型的全部依赖，也不包含系统求解器程序或商业许可。\n\n"
        f"{runtime_description}\n\n"
        f"{model_export_description}\n\n"
        f"运行 `python run.py solve` 可按当前可见模型和 `{inputs_relative}` 重新计算；最新本地结果写入 `results/local/`，不会覆盖已选结果或数学报告。求解摘要查看 `results/local/summary.md`，仅在实际求解后生成；机器记录与原始产物位于 `results/local/.optagent/`。运行 `python run.py check` 时，若还没有本地尝试且 selected 文件包含可检查的点则检查该点；`no_machine_point` 不提供已选点。若最近一次本地尝试有保存点，则检查该次保存点，同时保留其 failed、timeout 或 partial 状态；若该次尝试没有保存点，则报告没有可核对的点，不回退到更早的本地结果或已选解，也不为补点而重新计算。检查输出写入 `results/check/`，摘要查看 `results/check/summary.md`，仅在实际检查后生成；机器记录与原始产物位于 `results/check/.optagent/`。如需明确检查已选文件，可运行 `python run.py check --solution results/selected/solution.json`；该文件若无数值点，检查会报告没有可核对的点。"
        f"{check_scope}\n\n"
        f"{budget_line}\n\n"
        "`.optagent/run.json` 的 `solve_options.execution_budget_seconds` 必须是有限正数，限制整次本地运行或检查。"
        f"{native_controls} Linux 超时时会终止子进程组；Windows 只保证终止直接子进程。\n\n"
        f"编辑 `code/` 中的模型代码或 `{inputs_relative}` 后重新计算，不会沿用旧的已核验结论；若要在平台重新运行，需重新捕获变更、登记候选并正常执行。\n"
    ).encode("utf-8")
    add_file("README.md", readme)
    return artifact_dir, planned_files, status, protected_source_paths, observed_requirements, inputs_path


def _editable_export_manifest_path(case_id: str) -> str:
    return f".reaslab/optagent/cases/{case_id}/editable-export.json"


def _load_editable_export_manifest(store: CaseStore, case_id: str, artifact_dir: str) -> dict[str, Any] | None:
    path = _editable_export_manifest_path(case_id)
    try:
        value = store.read_record(path)
    except ContractError as exc:
        if exc.code == "not_found":
            return None
        raise
    legacy_fields = {"schema_version", "case_id", "artifact_dir", "delivery_id", "files"}
    current_fields = legacy_fields | {"inputs_path"}
    if (
        frozenset(value) not in {frozenset(legacy_fields), frozenset(current_fields)}
        or value.get("schema_version") != 1
        or value.get("case_id") != case_id
        or value.get("artifact_dir") != artifact_dir
        or not isinstance(value.get("files"), dict)
    ):
        raise ContractError("artifact_changed", "editable export record has invalid identity")
    for path_value, ref_value in value["files"].items():
        ref = validate_ref_shape(ref_value, pointer="/editable_export/files")
        if path_value != ref["path"] or not path_value.startswith(artifact_dir.rstrip("/") + "/"):
            raise ContractError("artifact_changed", "editable export record contains a path outside its Case folder")
    inputs_path = value.get("inputs_path", f"{artifact_dir}/data/inputs.json")
    prefix = artifact_dir.rstrip("/") + "/"
    if not isinstance(inputs_path, str) or not inputs_path.startswith(prefix):
        raise ContractError("artifact_changed", "editable export input path is outside its Case folder")
    input_relative = inputs_path[len(prefix):]
    if not input_relative.startswith("data/"):
        raise ContractError("artifact_changed", "editable export input path is outside its data folder")
    _candidate_package_path(input_relative)
    if inputs_path not in value["files"]:
        raise ContractError("artifact_changed", "editable export input path is not owned by this export")
    value["inputs_path"] = inputs_path
    return value


def _editable_previous_source_paths(
    store: CaseStore,
    case_id: str,
    artifact_dir: str,
    previous: Mapping[str, Any] | None,
) -> tuple[set[str], set[str]]:
    """Resolve source paths and generated code paths from the immediately prior export only."""
    protected: set[str] = set()
    confirmed_generated: set[str] = set()
    if previous is None:
        return protected, confirmed_generated

    prefix = artifact_dir.rstrip("/") + "/"
    owned_files = previous.get("files")
    if not isinstance(owned_files, Mapping):
        return protected, confirmed_generated

    # These are paths this Core projection has generated in either the old or new layout.
    standard_paths = {
        "README.md", "run.py", "requirements.txt",
        "inputs.json", "run.json", "solution.json",
        "data/inputs.json", "support/run.json", "results/selected/solution.json",
        "support/runtime.zip", ".optagent/run.json", ".optagent/runtime.zip",
        *(f"support/optagent_core/{name}" for name in _EDITABLE_SUPPORT_FILES),
    }
    recorded_inputs_path = previous.get("inputs_path", prefix + "data/inputs.json")
    if isinstance(recorded_inputs_path, str) and recorded_inputs_path.startswith(prefix):
        confirmed_generated.add(recorded_inputs_path)
    confirmed_standard: set[str] = set()
    for relative in standard_paths:
        target = prefix + relative
        if target in owned_files:
            confirmed_generated.add(target)
            confirmed_standard.add(target)

    delivery_id = previous.get("delivery_id")
    if not isinstance(delivery_id, str):
        return protected, set()
    try:
        delivery, _ = _load_delivery(store, case_id, delivery_id)
        report_refs = delivery.get("reports", [])
        if not isinstance(report_refs, list):
            raise ContractError("artifact_changed", "prior editable export Delivery reports are invalid")
        for index, value in enumerate(report_refs):
            if not isinstance(value, Mapping):
                raise ContractError("artifact_changed", "prior editable export Delivery report is invalid")
            report_ref = validate_ref_shape(dict(value), pointer=f"/delivery/reports/{index}")
            if report_ref["path"] == f"{prefix}README.md":
                protected.add(report_ref["path"])
        selected_run_ids = delivery.get("run_ids")
        if not isinstance(selected_run_ids, list) or not selected_run_ids:
            return protected, set()
        from .runner import _load_prepared

        new_layout = any(
            prefix + path in owned_files
            for path in (
                "data/inputs.json",
                "support/run.json",
                "results/selected/solution.json",
                "support/runtime.zip",
                ".optagent/run.json",
                ".optagent/runtime.zip",
            )
        )
        for run_id in sorted(set(selected_run_ids)):
            run, _ = _load_run(store, case_id, run_id)
            prepared_id = run.get("prepared_id")
            if not isinstance(prepared_id, str):
                raise ContractError("artifact_changed", "prior editable export Run has no Prepared identity")
            _, _, prepared_manifest = _load_prepared(store, case_id, prepared_id)
            candidate = prepared_manifest.get("candidate")
            if not isinstance(candidate, Mapping):
                raise ContractError("artifact_changed", "prior editable export Prepared has no Candidate")
            model = candidate.get("model")
            modules = candidate.get("local_modules")
            checker = candidate.get("business_checker")
            if not isinstance(model, Mapping) or not isinstance(modules, list):
                raise ContractError("artifact_changed", "prior editable export Candidate code closure is incomplete")
            code_specs: list[Mapping[str, Any]] = [model]
            code_specs.extend(item for item in modules if isinstance(item, Mapping))
            if any(not isinstance(item, Mapping) for item in modules):
                raise ContractError("artifact_changed", "prior editable export module closure is invalid")
            if checker is not None:
                if not isinstance(checker, Mapping):
                    raise ContractError("artifact_changed", "prior editable export checker closure is invalid")
                code_specs.append(checker)
            for spec in code_specs:
                original = spec.get("original_path")
                if not isinstance(original, str):
                    raise ContractError("artifact_changed", "prior editable export source path is invalid")
                logical = PurePosixPath(original.replace("\\", "/")).as_posix()
                try:
                    _candidate_package_path(logical)
                except ContractError:
                    raise ContractError("artifact_changed", "prior editable export source path is unsafe")
                protected.add(logical)
                if logical.startswith(prefix):
                    inside = logical[len(prefix):]
                else:
                    inside = logical
                if new_layout and logical.startswith(prefix):
                    physical = (
                        inside
                        if inside == "code" or inside.startswith("code/")
                        else f"code/{inside}"
                    )
                elif new_layout:
                    physical = f"code/external/{inside}"
                else:
                    physical = inside if logical.startswith(prefix) else f"support/modules/{inside}"
                target = prefix + physical
                if target in owned_files:
                    confirmed_generated.add(target)

            source_specs = prepared_manifest.get("sources")
            if not isinstance(source_specs, list):
                raise ContractError("artifact_changed", "prior editable export Prepared sources are invalid")
            source_records = read_prepared_source_records(store, source_specs)
            for source in source_records.values():
                origin = source.get("origin")
                workspace_path = origin.get("workspace_path") if isinstance(origin, Mapping) else None
                if isinstance(workspace_path, str):
                    logical_source = PurePosixPath(workspace_path.replace("\\", "/")).as_posix()
                    try:
                        _candidate_package_path(logical_source)
                    except ContractError:
                        continue
                    protected.add(logical_source)
    except (ContractError, OSError, ValueError, TypeError):
        # Source identity is uncertain; keep unclassified paths rather than deleting them.
        return protected, set()
    return protected, confirmed_generated


def _editable_case_projection_plan(
    store: CaseStore,
    state: Mapping[str, Any],
    delivery: Mapping[str, Any],
) -> dict[str, Any]:
    artifact_dir = case_artifact_dir_for_state(state)
    try:
        result_projection = _delivery_result_projection(store, state, delivery)
        previous = _load_editable_export_manifest(store, state["case_id"], artifact_dir)
        existing_inputs_path = previous["inputs_path"] if previous is not None else None
        prepared = _editable_case_file_bytes(
            store,
            state,
            delivery,
            result_projection,
            existing_inputs_path=existing_inputs_path,
        )
        if isinstance(prepared, dict):
            return prepared
        root, files, solution_status, protected_source_paths, observed_requirements, inputs_path = prepared
        return {
            "status": "planned",
            "artifact_dir": root,
            "delivery_id": delivery["delivery_id"],
            "expected_binding": {
                "source_revision": state["source_revision"],
                "candidate_revision": state["candidate_revision"],
                "candidate_id": state["candidate_id"],
                "promotion_generation": state["promotion_generation"],
            },
            "files": files,
            "solution_status": solution_status,
            "protected_source_paths": sorted(protected_source_paths),
            "observed_default_interface_requirements": observed_requirements,
            "requirements_path": f"{root}/requirements.txt",
            "inputs_path": inputs_path,
        }
    except ContractError as exc:
        error_details: dict[str, Any] = {
            "error_code": exc.code,
            "message": exc.message,
        }
        if exc.pointer is not None:
            error_details["pointer"] = exc.pointer
        if exc.details:
            error_details["details"] = exc.details
        return _editable_case_not_generated(
            artifact_dir,
            "projection_failed",
            **error_details,
        )
    except OSError:
        return _editable_case_not_generated(artifact_dir, "editable_support_unavailable")


def _preflight_editable_case_projection(
    store: CaseStore,
    *,
    case_id: str,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    if plan.get("status") != "planned":
        return dict(plan)
    artifact_dir = plan["artifact_dir"]
    requirements_path = plan.get("requirements_path")
    readme_path = f"{artifact_dir}/README.md"
    default_paths = {readme_path}
    if isinstance(requirements_path, str):
        default_paths.add(requirements_path)
    try:
        with store.case_lock(case_id):
            previous = _load_editable_export_manifest(store, case_id, artifact_dir)
            previous_files = previous["files"] if previous is not None else {}
            protected_source_paths = set(plan.get("protected_source_paths", []))
            previous_protected, previous_generated = _editable_previous_source_paths(
                store, case_id, artifact_dir, previous
            )
            protected_source_paths.update(previous_protected)
            preserved_defaults: set[str] = set()
            for relative, raw in plan["files"].items():
                if relative in default_paths and relative in protected_source_paths:
                    # A Source or approved report path remains protected even
                    # if its visible file was later removed from the workspace.
                    preserved_defaults.add(relative)
                    continue
                _visible_target(store, relative)
                try:
                    _, existing_ref = store.read_workspace_file(relative)
                except ContractError as exc:
                    if exc.code == "not_found":
                        continue
                    return _editable_case_not_generated(
                        artifact_dir,
                        "existing_case_file_conflict",
                        path=relative,
                        error_code=exc.code,
                    )
                desired_ref = ref_from_bytes(relative, raw, _media_type(relative))
                previous_ref = previous_files.get(relative)
                previously_owned = (
                    isinstance(previous_ref, Mapping)
                    and dict(previous_ref) == existing_ref
                    and (relative not in default_paths or relative in previous_generated)
                )
                if relative == plan.get("inputs_path") and not previously_owned:
                    return _editable_case_not_generated(
                        artifact_dir,
                        "existing_case_file_conflict",
                        path=relative,
                    )
                if relative in default_paths and (relative in protected_source_paths or not previously_owned):
                    # README and requirements are user-owned unless this exact
                    # unchanged file is confirmed as a prior Core-generated default.
                    preserved_defaults.add(relative)
                    continue
                if existing_ref == desired_ref:
                    continue
                if not previously_owned:
                    return _editable_case_not_generated(
                        artifact_dir,
                        "existing_case_file_conflict",
                        path=relative,
                    )
    except ContractError as exc:
        return _editable_case_not_generated(
            artifact_dir,
            "editable_case_preflight_failed",
            error_code=exc.code,
        )
    except OSError:
        return _editable_case_not_generated(artifact_dir, "editable_case_preflight_failed")
    result = dict(plan)
    result["readme_preserved"] = readme_path in preserved_defaults
    result["requirements_preserved"] = isinstance(requirements_path, str) and requirements_path in preserved_defaults
    return result


def _publish_editable_case_projection(
    store: CaseStore,
    *,
    case_id: str,
    owner_session_id: str,
    expected_selected_delivery_id: str,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    if plan.get("status") != "planned":
        return dict(plan)
    artifact_dir = plan["artifact_dir"]
    planned_files = plan["files"]
    delivery_id = plan["delivery_id"]
    readme_path = f"{artifact_dir}/README.md"
    written: list[str] = []
    completed: list[str] = []
    retired: list[str] = []
    relinquished: list[str] = []
    cleanup_failed: list[str] = []
    actual_files: dict[str, bytes] = {}
    try:
        with store.case_lock(case_id):
            current_state = store.read_case(case_id)
            if (
                current_state.get("owner_session_id") != owner_session_id
                or current_state.get("selected_delivery_id") != expected_selected_delivery_id
                or delivery_id != expected_selected_delivery_id
                or any(current_state.get(key) != value for key, value in plan["expected_binding"].items())
            ):
                return _editable_case_not_generated(artifact_dir, "selection_changed_before_export")
            previous = _load_editable_export_manifest(store, case_id, artifact_dir)
            previous_files = previous["files"] if previous is not None else {}
            protected_source_paths = set(plan.get("protected_source_paths", []))
            previous_protected, previous_generated = _editable_previous_source_paths(
                store, case_id, artifact_dir, previous
            )
            protected_source_paths.update(previous_protected)
            actual_files = dict(planned_files)
            requirements_path = plan.get("requirements_path")
            default_paths = {readme_path}
            if isinstance(requirements_path, str):
                default_paths.add(requirements_path)
            for default_path in default_paths:
                if default_path not in actual_files:
                    continue
                if default_path in protected_source_paths:
                    actual_files.pop(default_path)
                else:
                    try:
                        _, existing_ref = store.read_workspace_file(default_path)
                    except ContractError as exc:
                        if exc.code != "not_found":
                            return _editable_case_not_generated(
                                artifact_dir,
                                "existing_case_file_conflict",
                                path=default_path,
                                error_code=exc.code,
                            )
                    else:
                        previous_ref = previous_files.get(default_path)
                        previously_owned = (
                            isinstance(previous_ref, Mapping)
                            and dict(previous_ref) == existing_ref
                            and default_path in previous_generated
                        )
                        if not previously_owned:
                            actual_files.pop(default_path)

            desired_refs = {
                relative: ref_from_bytes(relative, raw, _media_type(relative))
                for relative, raw in actual_files.items()
            }
            for relative, raw in list(actual_files.items()):
                try:
                    existing_raw, existing_ref = store.read_workspace_file(relative)
                except ContractError as exc:
                    if exc.code != "not_found":
                        return _editable_case_not_generated(
                            artifact_dir,
                            "existing_case_file_conflict",
                            path=relative,
                            error_code=exc.code,
                        )
                    continue
                desired = desired_refs[relative]
                prior_ref = previous_files.get(relative)
                previously_owned = (
                    isinstance(prior_ref, Mapping)
                    and dict(prior_ref) == existing_ref
                    and (relative not in default_paths or relative in previous_generated)
                )
                if relative == plan.get("inputs_path") and not previously_owned:
                    return _editable_case_not_generated(
                        artifact_dir,
                        "existing_case_file_conflict",
                        path=relative,
                    )
                if relative in default_paths and relative in protected_source_paths:
                    actual_files.pop(relative)
                    desired_refs.pop(relative)
                    continue
                if relative in default_paths and not previously_owned:
                    actual_files.pop(relative)
                    desired_refs.pop(relative)
                    continue
                if existing_ref == desired:
                    continue
                if not previously_owned:
                    return _editable_case_not_generated(artifact_dir, "existing_case_file_conflict", path=relative)

            for relative, raw in actual_files.items():
                desired = desired_refs[relative]
                try:
                    existing_raw, existing_ref = store.read_workspace_file(relative)
                except ContractError as exc:
                    if exc.code != "not_found":
                        raise
                    store.write_immutable(relative, raw, desired["media_type"])
                    written.append(relative)
                    continue
                if existing_ref == desired:
                    if relative == plan.get("inputs_path"):
                        prior_ref = previous_files.get(relative)
                        previously_owned = (
                            isinstance(prior_ref, Mapping)
                            and dict(prior_ref) == existing_ref
                            and (relative not in default_paths or relative in previous_generated)
                        )
                        if not previously_owned:
                            raise ContractError(
                                "package_collision",
                                "editable case input path was occupied after preflight",
                            )
                    completed.append(relative)
                    continue
                prior_ref = previous_files.get(relative)
                if not isinstance(prior_ref, Mapping) or dict(prior_ref) != existing_ref:
                    raise ContractError("package_collision", "editable case file changed after preflight")
                # Recheck immediately before replacement. Only bytes previously
                # published by this projection may be replaced; user edits remain untouched.
                current_raw, current_ref = store.read_workspace_file(relative)
                if current_raw != existing_raw or current_ref != existing_ref:
                    raise ContractError("package_collision", "editable case file changed after preflight")
                store._atomic_write(store.resolve_path(relative, must_exist=True), raw, replace=True)
                written.append(relative)
                completed.append(relative)

            record = {
                "schema_version": 1,
                "case_id": case_id,
                "artifact_dir": artifact_dir,
                "delivery_id": delivery_id,
                "inputs_path": plan["inputs_path"],
                "files": desired_refs,
            }
            store.write_record(_editable_export_manifest_path(case_id), record, replace=True)
            for relative, previous_ref in previous_files.items():
                if relative in desired_refs:
                    continue
                if relative in protected_source_paths:
                    relinquished.append(relative)
                    continue
                if relative not in previous_generated:
                    relinquished.append(relative)
                    continue
                if not isinstance(previous_ref, Mapping):
                    relinquished.append(relative)
                    continue
                try:
                    _, current_ref = store.read_workspace_file(relative)
                except ContractError as exc:
                    if exc.code == "not_found":
                        relinquished.append(relative)
                        continue
                    cleanup_failed.append(relative)
                    continue
                if dict(previous_ref) != current_ref:
                    # The Owner edited this formerly generated file; keep it and
                    # stop claiming ownership in the newly written manifest.
                    relinquished.append(relative)
                    continue
                try:
                    removed = store.remove_file_if_hash(relative, previous_ref["sha256"])
                except ContractError as exc:
                    if exc.code == "artifact_changed":
                        relinquished.append(relative)
                    else:
                        cleanup_failed.append(relative)
                    continue
                except OSError:
                    cleanup_failed.append(relative)
                    continue
                if removed:
                    retired.append(relative)
                else:
                    relinquished.append(relative)
    except ContractError as exc:
        return {
            "status": "partial" if completed or written else "not_generated",
            "artifact_dir": artifact_dir,
            "reason": "editable_case_publication_failed",
            "error_code": exc.code,
            "written_paths": written,
            "completed_paths": completed,
            "retired_paths": retired,
            "relinquished_paths": relinquished,
            "cleanup_failed_paths": cleanup_failed,
        }
    except OSError:
        return {
            "status": "partial" if completed or written else "not_generated",
            "artifact_dir": artifact_dir,
            "reason": "editable_case_publication_failed",
            "written_paths": written,
            "completed_paths": completed,
            "retired_paths": retired,
            "relinquished_paths": relinquished,
            "cleanup_failed_paths": cleanup_failed,
        }
    return {
        "status": "generated",
        "artifact_dir": artifact_dir,
        "solution_status": plan["solution_status"],
        "files": sorted(path.removeprefix(artifact_dir.rstrip("/") + "/") for path in actual_files),
        "solution_path": f"{artifact_dir}/results/selected/solution.json",
        "run_path": f"{artifact_dir}/run.py",
        "inputs_path": plan["inputs_path"],
        "run_config_path": f"{artifact_dir}/.optagent/run.json",
        "observed_default_interface_requirements": plan.get("observed_default_interface_requirements", []),
        "readme_preserved": readme_path not in actual_files,
        "requirements_preserved": isinstance(plan.get("requirements_path"), str)
        and plan["requirements_path"] not in actual_files,
        "retired_paths": retired,
        "relinquished_paths": relinquished,
        "cleanup_failed_paths": cleanup_failed,
    }


def select_delivery(
    store: CaseStore,
    *,
    case_id: str,
    expected_state_revision: int,
    kind: str,
    candidate_id: str | None,
    review_ids: Sequence[str],
    proof_ids: Sequence[str],
    run_ids: Sequence[str],
    stamp: Mapping[str, str],
    validate_current: Callable[[Mapping[str, Any]], None] | None = None,
    session_commit: Callable[..., Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = store.read_case(case_id)
    _require_owner(state, stamp)
    if validate_current is not None:
        validate_current(state)
    if state["state_revision"] != expected_state_revision:
        raise ContractError("stale_revision", "case revision changed", details={"actual": state["state_revision"]})
    if kind not in {"run", "proof", "reuse", "mixed"}:
        raise ContractError("invalid_argument", "delivery selection is empty or has an invalid kind")
    if len(review_ids) != 1:
        raise ContractError("invalid_argument", "selection must name exactly one ready Review")
    if len(set(review_ids)) != len(review_ids) or len(set(proof_ids)) != len(proof_ids) or len(set(run_ids)) != len(run_ids):
        raise ContractError("invalid_argument", "selected evidence identifiers must not repeat")
    if candidate_id != state["candidate_id"]:
        raise ContractError("evidence_insufficient", "candidate binding is not current")
    source_view = _check_pending_and_current_inputs(store, state)
    math_source_ids = source_view["math_source_ids"]
    review, review_ref = _load_review(store, case_id, review_ids[0])
    outcomes = review.get("outcomes", [])
    conclusion = review.get("conclusion")
    answer_ref_value = review.get("answer_ref")
    binding = review.get("binding")
    if (
        review.get("schema_version") != 4
        or review.get("status") != "closed"
        or not isinstance(binding, Mapping)
        or binding.get("source_revision") != state["source_revision"]
        or binding.get("candidate_revision") != state["candidate_revision"]
        or binding.get("candidate_id") != state["candidate_id"]
        or binding.get("math_source_ids") != list(math_source_ids)
        or not isinstance(outcomes, list)
        or not outcomes
        or not isinstance(outcomes[-1], Mapping)
        or outcomes[-1].get("decision") != "ready"
        or not isinstance(conclusion, Mapping)
        or not isinstance(conclusion.get("claims"), list)
        or answer_ref_value is None
    ):
        raise ContractError("evidence_insufficient", "delivery selection requires one current ready Review with its approved answer")
    answer_ref = validate_ref_shape(dict(answer_ref_value), pointer="/review/answer_ref")
    approved_answers = [answer_ref]
    claims = copy.deepcopy(list(conclusion["claims"]))
    if not claims:
        raise ContractError("evidence_insufficient", "ready Review has no supported evidence claims to select")
    proofs = [_load_proof(store, case_id, proof_id) for proof_id in proof_ids]
    runs = [_load_run(store, case_id, run_id) for run_id in run_ids]
    formulation_only = _is_formulation_only_selection(kind, state, candidate_id, claims, proofs, runs)
    if any(proof.get("proof_id") not in state["proof_ids"] for proof, _ in proofs):
        raise ContractError("not_found", "selected Proof is not registered on this case")
    if any(run.get("run_id") not in state["run_ids"] for run, _ in runs):
        raise ContractError("not_found", "selected run is not registered on this case")
    if kind in {"proof", "reuse"} and not proofs:
        raise ContractError("evidence_insufficient", "proof or reuse selection needs a proof record")
    if kind == "run" and not runs:
        raise ContractError("evidence_insufficient", "run selection needs a run record")
    if kind == "mixed" and not proofs and not runs and not formulation_only:
        raise ContractError("evidence_insufficient", "mixed selection needs proof or run evidence")
    evidence_refs: list[dict[str, Any]] = [dict(ref) for ref in approved_answers]
    validation_cache: dict[str, dict[str, Any]] = {}
    used_proofs: set[str] = set()
    used_runs: set[str] = set()
    for claim in claims:
        _validate_claim(claim)
        if claim["support"] in {"unresolved", "refuted"} and claim["kind"] not in {"diagnostic", "tradeoff"}:
            raise ContractError("evidence_insufficient", "unresolved or refuted claims cannot be presented as supported")
        for ref in claim["evidence_refs"]:
            _validate_case_evidence_ref(store, state, ref)
            evidence_refs.append(dict(ref))
        if claim["support"] in {"supported", "conditional"}:
            selected_refs = _refs_by_identity(claim["evidence_refs"], pointer="/claim/evidence_refs")
            anchors, anchor_proofs, anchor_runs = _claim_anchors(
                store,
                state,
                claim,
                validation_cache,
                require_support=True,
                bound_run_ids=binding.get("run_ids", []),
            )
            if not selected_refs or not anchors or not anchors.issubset(selected_refs):
                raise ContractError(
                    "evidence_insufficient",
                    "selected claim must preserve every primary evidence anchor",
                    details={"claim_kind": claim["kind"]},
                )
            for proof_id in anchor_proofs:
                if proof_id not in state["proof_ids"] or proof_id not in {item[0].get("proof_id") for item in proofs}:
                    raise ContractError("evidence_insufficient", "selected evidence omits a reviewed primary Proof")
                used_proofs.add(proof_id)
            for run_id in anchor_runs:
                if run_id not in {item[0].get("run_id") for item in runs}:
                    raise ContractError("evidence_insufficient", "selected evidence omits a reviewed primary Result run")
                used_runs.add(run_id)
            # Re-read primary records so stale or substituted Refs cannot pass.
            _claim_primary_records_are_current(store, state, claim, anchors, validation_cache)
            used_runs.update(_claim_selected_runs(store, state, claim, validation_cache))
            used_proofs.update(_claim_selected_proofs(store, state, claim))
        elif claim["kind"] in {"diagnostic", "tradeoff"}:
            observed = _diagnostic_observation_anchors(store, state, claim, runs)
            if runs and not observed:
                raise ContractError("evidence_insufficient", "selected diagnostic run needs an actual linked observation")
            used_runs.update(observed)
            used_proofs.update(_claim_selected_proofs(store, state, claim))
        else:
            raise ContractError("evidence_insufficient", "unsupported claim has no permitted evidence route")
    evidence_refs.append(review_ref)
    for proof, ref in proofs:
        _validate_proof_binding(store, state, proof, ref, require_current=True)
        if proof["proof_id"] not in used_proofs:
            raise ContractError("evidence_insufficient", "selected Proof is not a primary ref of any selected claim")
        evidence_refs.extend([ref, dict(proof["body"]), *[dict(item) for item in proof["evidence_refs"]]])
    for run, ref in runs:
        if run["run_id"] not in used_runs:
            raise ContractError("evidence_insufficient", "selected run is not tied to a selected Result or diagnostic observation")
        evidence_refs.append(ref)
        evidence_refs.extend(dict(item) for item in run.get("results", []))
    # Each reference is checked against its actual bytes before a current selection can be committed.
    for ref in evidence_refs:
        store.read_ref(ref)
    delivery_id = new_id()
    delivery = {
        "schema_version": 2,
        "created_at": utc_now(),
        "created_by": dict(stamp),
        "delivery_id": delivery_id,
        "kind": kind,
        "source_revision": state["source_revision"],
        "math_source_ids": list(math_source_ids),
        "candidate_id": candidate_id,
        "promotion_generation": state["promotion_generation"],
        "claims": [dict(claim) for claim in claims],
        "evidence_refs": _unique_refs(evidence_refs),
        "review_ids": list(review_ids),
        "proof_ids": list(proof_ids),
        "run_ids": list(run_ids),
        "authority_records": {
            "reviews": {review["review_id"]: dict(review_ref)},
            "runs": {run["run_id"]: dict(ref) for run, ref in runs},
        },
        "summary": None,
        "reports": [dict(ref) for ref in approved_answers],
        "package": None,
    }
    editable_plan = _preflight_editable_case_projection(
        store,
        case_id=case_id,
        plan=_editable_case_projection_plan(store, state, delivery),
    )
    record_ref = store.write_record(f".reaslab/optagent/cases/{case_id}/deliveries/{delivery_id}/delivery.json", delivery)

    def commit(route_validate: Callable[[Mapping[str, Any]], None] | None) -> dict[str, Any]:
        def update(current: dict[str, Any]) -> None:
            _require_owner(current, stamp)
            if validate_current is not None:
                validate_current(current)
            if route_validate is not None:
                route_validate(current)
            if (
                current["pending_inputs"]
                or current["source_revision"] != delivery["source_revision"]
                or current["candidate_revision"] != state["candidate_revision"]
                or current["promotion_generation"] != delivery["promotion_generation"]
                or current.get("candidate_id") != delivery["candidate_id"]
            ):
                raise ContractError("stale_revision", "case evidence binding changed before selection")
            for family, records in delivery["authority_records"].items():
                current_family = (current.get("current_records") or {}).get(family, {})
                for object_id, expected_ref in records.items():
                    if current_family.get(object_id) != expected_ref:
                        raise ContractError("stale_revision", "selected family evidence changed before selection")
            current["selected_delivery_id"] = delivery_id
            records = current.get("current_records")
            if records is None:
                records = {"questions": {}, "reviews": {}, "runs": {}, "presentation": None}
                current["current_records"] = records
            records["presentation"] = dict(record_ref)
            current["decisions"].append(
                {
                    "kind": "selection",
                    "note": f"selected {kind} evidence",
                    "refs": [record_ref],
                    "stamp": dict(stamp),
                    "created_at": utc_now(),
                }
            )

        return store.cas_case(case_id, expected_state_revision, update)

    if session_commit is not None:
        next_state = session_commit(commit)
    else:
        session_digest = sha256_bytes(stamp["session_id"].encode("utf-8"))
        with store.session_lock(session_digest):
            next_state = commit(None)
    editable_case = _publish_editable_case_projection(
        store,
        case_id=case_id,
        owner_session_id=stamp["session_id"],
        expected_selected_delivery_id=delivery_id,
        plan=editable_plan,
    )
    if editable_case.get("status") == "generated":
        message = (
            f"Local editable case generated at {editable_case['artifact_dir']} with inputs at "
            f"{editable_case['inputs_path']}."
        )
        if editable_case.get("readme_preserved") is True:
            message += (
                " The existing README was preserved; from the case directory, run `python run.py solve` "
                f"and `python run.py check` using `{editable_case['inputs_path']}`."
            )
    else:
        reason = editable_case.get("reason", "unknown")
        conflict_path = editable_case.get("details", {}).get("path")
        if reason == "existing_case_file_conflict" and isinstance(conflict_path, str):
            message = (
                f"Mathematical delivery remains selected; the local editable case was not generated "
                f"because an existing file conflicts at {conflict_path}."
            )
        else:
            message = f"Mathematical delivery remains selected; the local editable case was not generated: {reason}."
    return {
        "message": message,
        "state": state_summary(store, next_state),
        "delivery": object_summary(delivery_id, record_ref, "selected", delivery["source_revision"], state["candidate_revision"]),
        "approved_answers": [dict(ref) for ref in approved_answers],
        "editable_case": editable_case,
    }, delivery


def build_report(
    store: CaseStore,
    *,
    case_id: str,
    expected_state_revision: int,
    delivery_id: str,
    target_path: str,
    stamp: Mapping[str, str],
    validate_current: Callable[[Mapping[str, Any]], None] | None = None,
    session_commit: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    state = store.read_case(case_id)
    _require_owner(state, stamp)
    if state["state_revision"] != expected_state_revision or state["selected_delivery_id"] != delivery_id:
        raise ContractError("stale_revision", "delivery is no longer selected")
    source_view = _check_pending_and_current_inputs(store, state)
    delivery, selection_ref = _load_delivery(store, case_id, delivery_id)
    prior_presentation = _load_presentation(store, state, delivery_id, selection_ref, delivery=delivery)
    expected_presentation = (state.get("current_records") or {}).get("presentation")
    _check_delivery_binding(store, state, delivery, math_source_ids=source_view["math_source_ids"])
    _check_delivery_authorities(store, state, delivery)
    _validate_delivery_diagnostic_context(store, state, delivery)
    target = _visible_target(store, target_path)
    if target.exists():
        raise ContractError("package_collision", "report target already exists")
    result_projection = _delivery_result_projection(store, state, delivery)
    review_projection = _delivery_review_projection(store, state, delivery)
    body = _render_report(state, delivery, target.suffix.lower(), result_projection, review_projection)
    store._atomic_write(target, body, replace=False)
    report_ref = ref_from_bytes(target_path.replace("\\", "/"), body, _media_type(target.suffix.lower()))
    try:
        summary = {
            "schema_version": 1,
            "case_id": case_id,
            "delivery_id": delivery_id,
            "source_revision": delivery["source_revision"],
            "candidate_id": delivery["candidate_id"],
            "claims": delivery["claims"],
            "review_findings": review_projection,
            "result_evidence": result_projection,
            "evidence_refs": delivery["evidence_refs"],
            "remaining": _delivery_remaining(review_projection),
        }
        summary_ref = store.write_content_addressed(
            f".reaslab/optagent/cases/{case_id}/deliveries/{delivery_id}/summaries",
            canonical_json_bytes(summary),
            "application/json",
        )
        next_presentation = dict(prior_presentation)
        next_presentation["summary_ref"] = summary_ref
        presentation_ref = _write_presentation(store, case_id, next_presentation)
        next_state = _publish_presentation(
            store,
            case_id=case_id,
            state=state,
            delivery=delivery,
            selection_ref=selection_ref,
            expected_presentation=expected_presentation,
            next_presentation=presentation_ref,
            stamp=stamp,
            validate_current=validate_current,
            session_commit=session_commit,
        )
    except ContractError as exc:
        raise _local_committed_error(exc, "report", report_path=target_path, report_ref=report_ref) from exc
    except OSError as exc:
        failure = ContractError(
            "environment_unavailable",
            "The report was committed, but its summary or presentation could not be published.",
            details={"errno": exc.errno},
        )
        raise _local_committed_error(
            failure, "report", report_path=target_path, report_ref=report_ref
        ) from exc
    return {
        "state": state_summary(store, next_state),
        "delivery": object_summary(delivery_id, selection_ref, "selected", delivery["source_revision"], next_state["candidate_revision"]),
        "summary_ref": summary_ref,
        "report_ref": report_ref,
        "presentation_ref": presentation_ref,
    }


def audit_report(
    store: CaseStore,
    *,
    case_id: str,
    delivery_id: str,
    report_path: str,
) -> dict[str, Any]:
    state = store.read_case(case_id)
    delivery, selection_ref = _load_delivery(store, case_id, delivery_id)
    _check_delivery_binding(store, state, delivery)
    _check_delivery_authorities(store, state, delivery)
    if state.get("selected_delivery_id") != delivery_id:
        raise ContractError("stale_revision", "delivery is no longer selected")
    presentation = _load_presentation(store, state, delivery_id, selection_ref, delivery=delivery)
    body, report_ref = store.read_user_file(report_path)
    registered_ref = next((ref for ref in presentation["reports"] if ref == report_ref), None)
    if registered_ref is not None:
        _delivery_review_projection(store, state, delivery)
    expected: bytes | None = body if registered_ref is not None else None
    comparison_basis = (
        "registered_presentation" if registered_ref is not None else "selected_evidence_rendering"
    )
    if expected is None:
        # A report can be audited before build; compare with the deterministic rendering.
        result_projection = _delivery_result_projection(store, state, delivery)
        review_projection = _delivery_review_projection(store, state, delivery)
        expected = _render_report(
            state, delivery, Path(report_path).suffix.lower(), result_projection, review_projection
        )
    consistent = body == expected or _layout_equivalent(body, expected, Path(report_path).suffix.lower())
    return {
        "report_ref": report_ref,
        "comparison_basis": comparison_basis,
        "content_differences": []
        if consistent
        else ["Report content differs from the selected evidence comparison basis."],
        "scientific_changes": [],
        "status": "consistent" if consistent else "needs_review",
    }


def _package_data_bundle(
    store: CaseStore,
    case_id: str,
    data_id: str,
    files: dict[str, bytes],
    objects: list[dict[str, Any]],
    ref_map: dict[tuple[str, str, int, str], dict[str, Any]],
    source_records: Mapping[str, Mapping[str, Any]],
    source_record_refs: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    original_path = f".reaslab/optagent/cases/{case_id}/data/{data_id}/manifest.json"
    original_ref = store.ref_for_path(original_path)
    manifest_raw = _register_portable_ref(
        store,
        original_ref,
        f"data/{data_id}/manifest.json",
        files,
        objects,
        ref_map,
    )
    manifest = strict_json_loads(manifest_raw, max_bytes=len(manifest_raw))
    if not isinstance(manifest, dict) or manifest.get("data_id") != data_id or not isinstance(manifest.get("objects"), list):
        raise ContractError("artifact_changed", "candidate DataManifest identity is invalid")
    for spec in manifest["objects"]:
        if not isinstance(spec, dict) or not isinstance(spec.get("object_id"), str) or not spec["object_id"]:
            raise ContractError("output_invalid", "candidate DataManifest object is invalid")
        values_ref = validate_ref_shape(spec.get("values"))
        object_token = sha256_bytes(spec["object_id"].encode("utf-8"))
        _register_portable_ref(
            store,
            values_ref,
            f"data/{data_id}/objects/{object_token}.json",
            files,
            objects,
            ref_map,
        )
    trace_ref = validate_ref_shape(manifest.get("trace"))
    trace_path = f"data/{data_id}/trace.json"
    trace_raw = _register_portable_ref(store, trace_ref, trace_path, files, objects, ref_map)
    trace = strict_json_loads(trace_raw, max_bytes=len(trace_raw))
    if not isinstance(trace, dict) or not isinstance(trace.get("source_refs"), list):
        raise ContractError("output_invalid", "candidate DataBundle trace is invalid")
    source_ids = manifest.get("source_ids")
    if not isinstance(source_ids, list) or not source_ids or len(set(source_ids)) != len(source_ids) or len(trace["source_refs"]) != len(source_ids):
        raise ContractError("output_invalid", "candidate DataManifest Source order is invalid")
    for index, (source_id, trace_source) in enumerate(zip(source_ids, trace["source_refs"], strict=True)):
        source = source_records.get(source_id)
        trace_source_ref = validate_ref_shape(trace_source, pointer=f"/data/trace/source_refs/{index}")
        if not isinstance(source, Mapping) or source.get("source_id") != source_id or source.get("purpose") != "data":
            raise ContractError("input_closure_unknown", "candidate Data Source is outside the current Case Source records")
        source_bytes_ref = validate_ref_shape(source.get("bytes"), pointer=f"/sources/{source_id}/bytes")
        if source_bytes_ref != trace_source_ref:
            raise ContractError("artifact_changed", "candidate Data trace Ref differs from its exact Source ID")
        source_record_ref = validate_ref_shape(
            source_record_refs.get(source_id), pointer=f"/sources/{source_id}/record"
        )
        expected_record_path = f".reaslab/optagent/cases/{case_id}/sources/{source_id}.json"
        if source_record_ref["path"] != expected_record_path or source_record_ref["media_type"] != "application/json":
            raise ContractError("artifact_changed", "candidate Data Source record Ref is outside its exact Case path")
        record_raw = _register_portable_ref(
            store,
            source_record_ref,
            f"data/{data_id}/sources/{source_id}/record.json",
            files,
            objects,
            ref_map,
        )
        try:
            record_value = strict_json_loads(record_raw, max_bytes=len(record_raw))
        except (ValueError, UnicodeError) as exc:
            raise ContractError("artifact_changed", "candidate Data Source record is invalid JSON") from exc
        if record_value != dict(source):
            raise ContractError("artifact_changed", "candidate Data Source record changed after projection")
        _register_portable_ref(
            store,
            source_bytes_ref,
            f"data/{data_id}/sources/{source_id}/bytes.bin",
            files,
            objects,
            ref_map,
        )
    # Re-load through the same Ref identities using only the package map. This
    # proves that the original manifest, trace, and values remain readable
    # without changing their bytes, digest, or path identities. Each mapped
    # payload came directly from a stable CaseStore.read_ref above, so reuse
    # those verified bytes instead of hashing them a second time in-process.
    class _PackageReader:
        def read_ref(self, ref: Mapping[str, Any]) -> bytes:
            checked = validate_ref_shape(dict(ref))
            key = (checked["path"], checked["sha256"], checked["byte_len"], checked["media_type"])
            row = ref_map.get(key)
            if row is None:
                raise ContractError("capture_unavailable", "packaged data reference is not mapped")
            payload = files.get(row["package_path"])
            if payload is None or len(payload) != checked["byte_len"]:
                raise ContractError("artifact_changed", "packaged data reference differs from its original identity")
            return payload

    bundle = load_data_bundle(_PackageReader(), original_ref, source_records=source_records)
    return {
        "data_id": data_id,
        "digest": bundle.digest,
        "manifest": original_ref,
        "loader_version": bundle.loader_version,
    }


def _package_runtime_bundle(
    store: CaseStore,
    files: dict[str, bytes],
    objects: list[dict[str, Any]],
    ref_map: dict[tuple[str, str, int, str], dict[str, Any]],
    runtime: Mapping[str, Any],
    *,
    prepared_schema_version: int,
) -> dict[str, Any]:
    if not isinstance(runtime, Mapping) or set(runtime) != {"digest", "manifest", "worker_entry"}:
        raise ContractError("output_invalid", "prepared runtime bundle record is invalid")
    bundle_digest = runtime["digest"]
    if not isinstance(bundle_digest, str) or re.fullmatch(r"[0-9a-f]{64}", bundle_digest) is None:
        raise ContractError("output_invalid", "runtime bundle digest is invalid")
    manifest_ref = validate_ref_shape(dict(runtime["manifest"]))
    prefix = f".reaslab/optagent/runtime/{bundle_digest}/"
    if manifest_ref["path"] != prefix + "manifest.json" or runtime["worker_entry"] != "python/optagent_core/worker.py":
        raise ContractError("path_denied", "runtime bundle identity is outside its fixed digest root")
    manifest_raw = store.read_ref(manifest_ref)
    runtime_manifest = strict_json_loads(manifest_raw)
    if not isinstance(runtime_manifest, dict) or set(runtime_manifest) != {"schema_version", "digest", "files"}:
        raise ContractError("output_invalid", "runtime bundle manifest has invalid fields")
    if runtime_manifest["schema_version"] != 1 or runtime_manifest["digest"] != bundle_digest or not isinstance(runtime_manifest["files"], list):
        raise ContractError("artifact_changed", "runtime bundle manifest identity is invalid")
    if prepared_schema_version == 2 and type(prepared_schema_version) is int:
        expected_helper_paths = _LEGACY_RUNTIME_HELPER_PATHS
    elif prepared_schema_version == 3 and type(prepared_schema_version) is int:
        expected_helper_paths = _RUNTIME_HELPER_PATHS
    else:
        raise ContractError("input_closure_unknown", "prepared schema has no fixed runtime package closure")
    identity_files: list[dict[str, Any]] = []
    for item in runtime_manifest["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "byte_len"}:
            raise ContractError("output_invalid", "runtime bundle file record is invalid")
        identity_files.append({"path": item["path"], "sha256": item["sha256"], "byte_len": item["byte_len"]})
    identity = {"schema_version": 1, "files": identity_files}
    identity_bytes = json.dumps(identity, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if sha256_bytes(identity_bytes) != bundle_digest:
        raise ContractError("artifact_changed", "runtime bundle manifest digest is invalid")
    listed = [item["path"] for item in runtime_manifest["files"]]
    if listed != sorted(expected_helper_paths) or len(listed) != len(set(listed)):
        raise ContractError("input_closure_unknown", "runtime bundle does not match its frozen prepared-schema closure")

    package_root = f"reproduce/runtime/{bundle_digest}"
    _register_portable_ref(store, manifest_ref, f"{package_root}/manifest.json", files, objects, ref_map, allow_runtime=True)
    worker_raw: bytes | None = None
    for entry in runtime_manifest["files"]:
        relative = entry["path"]
        if set(entry) != {"path", "sha256", "byte_len"} or type(entry["byte_len"]) is not int or entry["byte_len"] < 0:
            raise ContractError("output_invalid", "runtime bundle file record is invalid")
        raw, _ = store.read_workspace_file(prefix + relative)
        if len(raw) != entry["byte_len"] or sha256_bytes(raw) != entry["sha256"]:
            raise ContractError("artifact_changed", "runtime bundle file bytes do not match its manifest")
        helper_ref = ref_from_bytes(prefix + relative, raw, "text/x-python; charset=utf-8")
        _register_portable_ref(store, helper_ref, f"{package_root}/{relative}", files, objects, ref_map, allow_runtime=True)
        if relative == "python/optagent_core/worker.py":
            worker_raw = raw
    complete_raw, _ = store.read_workspace_file(prefix + "COMPLETE")
    if complete_raw != bundle_digest.encode("ascii"):
        raise ContractError("artifact_changed", "runtime bundle completion marker is invalid")
    _add_package_file(files, f"{package_root}/COMPLETE", complete_raw, objects, allow_runtime=True)
    if worker_raw is None:
        raise ContractError("input_closure_unknown", "runtime bundle omitted its worker entry")
    try:
        worker_tree = ast.parse(worker_raw.decode("utf-8"), filename="worker.py")
    except (SyntaxError, UnicodeError) as exc:
        raise ContractError("output_invalid", "pinned worker helper is not valid UTF-8 Python") from exc
    local_entry = any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_local_reproduction" for node in worker_tree.body)
    return {
        "digest": bundle_digest,
        "manifest": manifest_ref,
        "worker_entry": runtime["worker_entry"],
        "helper_paths": listed,
        "standalone_supported": local_entry,
    }


def _register_portable_ref(
    store: CaseStore,
    ref_value: Mapping[str, Any],
    package_path: str,
    files: dict[str, bytes],
    objects: list[dict[str, Any]],
    ref_map: dict[tuple[str, str, int, str], dict[str, Any]],
    *,
    allow_runtime: bool = False,
    raw_bytes: bytes | None = None,
) -> bytes:
    ref = validate_ref_shape(dict(ref_value))
    key = (ref["path"], ref["sha256"], ref["byte_len"], ref["media_type"])
    previous = ref_map.get(key)
    if previous is not None:
        cached = files.get(previous["package_path"])
        if cached is None or len(cached) != ref["byte_len"]:
            raise ContractError("artifact_changed", "cached portable Ref bytes are unavailable")
        return cached
    raw = store.read_ref(ref) if raw_bytes is None else raw_bytes
    if len(raw) != ref["byte_len"]:
        raise ContractError("artifact_changed", "portable Ref bytes have an invalid length")
    _add_package_file(files, package_path, raw, objects, allow_runtime=allow_runtime, digest=ref["sha256"])
    ref_map[key] = {"ref": ref, "package_path": package_path}
    return raw


def _no_data_has_instance_keys(manifest: Mapping[str, Any]) -> bool:
    options = manifest.get("solve_options")
    if not isinstance(options, Mapping):
        raise ContractError("output_invalid", "PreparedInputManifest solve options are invalid")
    keys = options.get("instance_keys", [])
    if not isinstance(keys, list):
        raise ContractError("output_invalid", "PreparedInputManifest instance keys are invalid")
    return bool(keys)


def _prepared_package_path(prepared_id: str, ref: Mapping[str, Any], *, logical_path: str | None = None) -> str:
    if logical_path is not None:
        normalized = logical_path.replace("\\", "/")
        pure = PurePosixPath(normalized)
        if pure.is_absolute() or any(part in {"", ".", ".."} or _is_reserved_package_part(part) for part in pure.parts):
            raise ContractError("path_denied", "frozen candidate path cannot be packaged")
        marker = f"/prepared/{prepared_id}/input/source/"
        if marker not in ref["path"] or ref["path"].split(marker, 1)[1] != pure.as_posix():
            raise ContractError("input_closure_unknown", "frozen code Ref path differs from its original module path")
        return f"reproduce/prepared/{prepared_id}/source/{pure.as_posix()}"
    prefix = ".reaslab/optagent/cases/"
    path = ref["path"]
    marker = f"/prepared/{prepared_id}/input/"
    marker_at = path.find(marker)
    if not path.startswith(prefix) or marker_at < 0:
        raise ContractError("input_closure_unknown", "frozen Ref is outside its prepared input tree")
    suffix = path[marker_at + len(marker):]
    pure = PurePosixPath(suffix)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ContractError("path_denied", "frozen Ref has an unsafe logical suffix")
    return f"reproduce/prepared/{prepared_id}/{pure.as_posix()}"


def _package_prepared_closure(
    store: CaseStore,
    input_ref: Mapping[str, Any],
    case_id: str,
    prepared_id: str,
    files: dict[str, bytes],
    objects: list[dict[str, Any]],
    ref_map: dict[tuple[str, str, int, str], dict[str, Any]],
) -> dict[str, Any]:
    input_ref = validate_ref_shape(dict(input_ref))
    raw = _register_portable_ref(
        store,
        input_ref,
        f"reproduce/prepared/{prepared_id}/input/manifest.json",
        files,
        objects,
        ref_map,
    )
    manifest = strict_json_loads(raw)
    if not isinstance(manifest, dict) or set(manifest) != _PREPARED_MANIFEST_FIELDS or manifest.get("schema_version") not in {2, 3}:
        raise ContractError("output_invalid", "prepared input manifest has invalid fields")
    if manifest.get("case_id") != case_id or manifest.get("prepared_id") != prepared_id:
        raise ContractError("artifact_changed", "prepared input manifest identity does not match selected run")
    if input_ref["path"] != f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/input/manifest.json":
        raise ContractError("input_closure_unknown", "prepared input Ref is outside its fixed frozen tree")

    expected_sources: dict[str, dict[str, Any]] = {}
    prepared_source_records: dict[str, dict[str, Any]] = {}
    for source in manifest["sources"]:
        if not isinstance(source, dict) or set(source) != {"source_id", "record", "bytes"}:
            raise ContractError("output_invalid", "prepared source closure has invalid fields")
        source_id = source["source_id"]
        if not isinstance(source_id, str) or re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", source_id) is None:
            raise ContractError("output_invalid", "prepared source identity is invalid")
        record_ref = validate_ref_shape(source["record"])
        bytes_ref = validate_ref_shape(source["bytes"])
        record_raw = _register_portable_ref(
            store,
            record_ref,
            f"reproduce/prepared/{prepared_id}/sources/{source_id}/record.json",
            files,
            objects,
            ref_map,
        )
        record = strict_json_loads(record_raw)
        if not isinstance(record, dict) or record.get("source_id") != source_id or validate_ref_shape(record.get("bytes")) != bytes_ref:
            raise ContractError("artifact_changed", "frozen source record does not bind its bytes Ref")
        _register_portable_ref(
            store,
            bytes_ref,
            f"reproduce/prepared/{prepared_id}/sources/{source_id}/bytes.bin",
            files,
            objects,
            ref_map,
        )
        expected_sources[source_id] = {"record": record_ref, "bytes": bytes_ref}
        prepared_source_records[source_id] = record

    candidate = manifest.get("candidate")
    expected_candidate_fields = {"authoring", "model", "local_modules", "business_checker", "backend_id", "objective_plan", "input_closure", "closure_note"}
    if not isinstance(candidate, dict) or set(candidate) not in (expected_candidate_fields, expected_candidate_fields | {"requirements_file"}):
        raise ContractError("output_invalid", "prepared candidate closure has invalid fields")
    if manifest["schema_version"] == 2:
        if candidate.get("authoring") != "pyomo" or candidate.get("backend_id") != "highs":
            raise ContractError("capability_uncovered", "legacy prepared inputs retain their original HiGHS backend")
    else:
        from .solver_protocol import (
            DIRECT_PYTHON_BACKEND_ID,
            SUPPORTED_BACKEND_IDS,
            validate_direct_python_options,
            validate_solver_options,
        )
        if candidate.get("authoring") == "direct_python":
            if candidate.get("backend_id") != DIRECT_PYTHON_BACKEND_ID or "requirements_file" not in candidate:
                raise ContractError("artifact_changed", "direct Prepared input family or requirements identity is invalid")
            if candidate.get("objective_plan", {}).get("mode") == "lexicographic":
                raise ContractError("capability_uncovered", "direct_python does not support lexicographic objective plans")
            validate_direct_python_options(manifest["solve_options"])
        elif candidate.get("authoring") == "pyomo":
            if candidate.get("backend_id") not in SUPPORTED_BACKEND_IDS:
                raise ContractError("capability_uncovered", "prepared input names an unsupported solver backend")
            validate_solver_options(candidate["backend_id"], manifest["solve_options"])
        else:
            raise ContractError("capability_uncovered", "prepared input names an unsupported candidate entry family")
    for code_spec in [candidate["model"], *candidate["local_modules"], *([candidate["business_checker"]] if candidate["business_checker"] else [])]:
        if not isinstance(code_spec, dict) or set(code_spec) != {"original_path", "bytes"}:
            raise ContractError("output_invalid", "prepared code closure has invalid fields")
        code_ref = validate_ref_shape(code_spec["bytes"])
        _register_portable_ref(
            store,
            code_ref,
            _prepared_package_path(prepared_id, code_ref, logical_path=code_spec["original_path"]),
            files,
            objects,
            ref_map,
        )

    requirements_spec = candidate.get("requirements_file")
    if requirements_spec is not None:
        if not isinstance(requirements_spec, Mapping) or set(requirements_spec) != {"path", "bytes"} or requirements_spec.get("path") != "requirements.txt":
            raise ContractError("output_invalid", "prepared root requirements specification is invalid")
        requirements_ref = validate_ref_shape(dict(requirements_spec["bytes"]), pointer="/prepared/candidate/requirements_file/bytes")
        if requirements_ref["path"] != f".reaslab/optagent/cases/{manifest['case_id']}/prepared/{prepared_id}/input/requirements.txt":
            raise ContractError("input_closure_unknown", "prepared requirements Ref is outside its frozen root path")
        _register_portable_ref(
            store,
            requirements_ref,
            f"reproduce/prepared/{prepared_id}/requirements.txt",
            files,
            objects,
            ref_map,
        )

    data_info = manifest.get("data")
    if data_info is None:
        bundle = load_data_bundle(store, None, candidate_binding_digest=manifest["binding_digest"])
        if bundle.data_id is not None or bundle.consumed_inputs or bundle.objects:
            raise ContractError("artifact_changed", "no-data bundle identity is not empty")
        if _no_data_has_instance_keys(manifest):
            raise ContractError("output_invalid", "no-data PreparedInputManifest cannot select parameter rows")
    else:
        if not isinstance(data_info, dict) or set(data_info) != {"data_id", "manifest", "instance_parameter_object_id"}:
            raise ContractError("output_invalid", "frozen data identity has invalid fields")
        data_manifest_ref = validate_ref_shape(data_info["manifest"])
        data_raw = _register_portable_ref(
            store,
            data_manifest_ref,
            f"reproduce/prepared/{prepared_id}/data/manifest.json",
            files,
            objects,
            ref_map,
        )
        data_manifest = strict_json_loads(data_raw, max_bytes=len(data_raw))
        if not isinstance(data_manifest, dict) or not isinstance(data_manifest.get("objects"), list):
            raise ContractError("output_invalid", "frozen DataManifest is invalid")
        for index, spec in enumerate(data_manifest["objects"]):
            if not isinstance(spec, dict) or not isinstance(spec.get("object_id"), str):
                raise ContractError("output_invalid", "frozen data object is invalid", pointer=f"/data/objects/{index}")
            value_ref = validate_ref_shape(spec.get("values"))
            object_token = sha256_bytes(spec["object_id"].encode("utf-8"))
            _register_portable_ref(
                store,
                value_ref,
                f"reproduce/prepared/{prepared_id}/data/objects/{object_token}.json",
                files,
                objects,
                ref_map,
            )
        trace_ref = validate_ref_shape(data_manifest.get("trace"))
        trace_raw = _register_portable_ref(
            store,
            trace_ref,
            f"reproduce/prepared/{prepared_id}/data/trace.json",
            files,
            objects,
            ref_map,
        )
        trace = strict_json_loads(trace_raw, max_bytes=len(trace_raw))
        if not isinstance(trace, dict) or not isinstance(trace.get("source_refs"), list):
            raise ContractError("output_invalid", "frozen data trace is invalid")
        data_source_ids = data_manifest.get("source_ids")
        if (
            not isinstance(data_source_ids, list)
            or not data_source_ids
            or len(set(data_source_ids)) != len(data_source_ids)
            or len(trace["source_refs"]) != len(data_source_ids)
        ):
            raise ContractError("output_invalid", "frozen DataManifest Source order is invalid")
        for index, (source_id, trace_source_ref) in enumerate(zip(data_source_ids, trace["source_refs"], strict=True)):
            expected = expected_sources.get(source_id)
            source_record = prepared_source_records.get(source_id)
            checked = validate_ref_shape(trace_source_ref, pointer=f"/data/trace/source_refs/{index}")
            if (
                expected is None
                or source_record is None
                or source_record.get("purpose") != "data"
                or expected["bytes"] != checked
            ):
                raise ContractError("input_closure_unknown", "frozen Data trace Source Ref differs from its declared Source ID", pointer=f"/data/trace/source_refs/{index}")

    runtime = manifest.get("runtime_bundle")
    runtime_info = _package_runtime_bundle(
        store,
        files,
        objects,
        ref_map,
        runtime,
        prepared_schema_version=manifest["schema_version"],
    )
    return {"prepared_manifest_ref": input_ref, "manifest": manifest, "runtime": runtime_info}


def _verify_portable_reproductions(
    workspace_root: Path,
    files: Mapping[str, bytes],
    ref_map_items: Sequence[Mapping[str, Any]],
    reproduction_items: Sequence[Mapping[str, Any]],
) -> None:
    if not reproduction_items:
        return
    with tempfile.TemporaryDirectory(prefix=".p-", dir=workspace_root / ".reaslab/optagent") as temporary:
        root = Path(temporary)
        for item in ref_map_items:
            package_path = item["package_path"]
            raw = files[package_path]
            destination = root.joinpath(*PurePosixPath(package_path).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
        reader = PortableReader(root, ref_map_items)
        for entry in reproduction_items:
            manifest_raw = reader.read_ref(entry["prepared_manifest_ref"])
            manifest = strict_json_loads(manifest_raw)
            if not isinstance(manifest, dict) or set(manifest) != _PREPARED_MANIFEST_FIELDS:
                raise ContractError("output_invalid", "portable PreparedInputManifest is invalid")
            if sha256_bytes(manifest_raw) != entry["prepared_manifest_ref"]["sha256"]:
                raise ContractError("artifact_changed", "portable PreparedInputManifest Ref changed")
            source_records = read_prepared_source_records(reader, manifest["sources"])
            candidate = manifest["candidate"]
            for code_spec in [candidate["model"], *candidate["local_modules"], *([candidate["business_checker"]] if candidate["business_checker"] else [])]:
                reader.read_ref(code_spec["bytes"])
            data_info = manifest["data"]
            if data_info is None:
                bundle = load_data_bundle(reader, None, candidate_binding_digest=manifest["binding_digest"])
                if bundle.data_id is not None or bundle.consumed_inputs or bundle.objects:
                    raise ContractError("artifact_changed", "portable no-data identity is not empty")
                if _no_data_has_instance_keys(manifest):
                    raise ContractError("output_invalid", "portable no-data input cannot select parameter rows")
            else:
                if not isinstance(data_info, dict) or set(data_info) != {"data_id", "manifest", "instance_parameter_object_id"}:
                    raise ContractError("output_invalid", "portable data identity is invalid")
                bundle = load_data_bundle(
                    reader,
                    data_info["manifest"],
                    candidate_binding_digest=manifest["binding_digest"],
                    source_records=source_records,
                )
                if bundle.data_id != data_info["data_id"]:
                    raise ContractError("artifact_changed", "portable DataBundle identity changed")
                parameter_id = data_info["instance_parameter_object_id"]
                if parameter_id is not None:
                    bundle.table(parameter_id)
                    for instance_key in manifest["solve_options"].get("instance_keys", []):
                        bundle.for_instance(parameter_id, instance_key)
            already_read = {
                (ref["path"], ref["sha256"], ref["byte_len"], ref["media_type"])
                for ref in bundle.consumed_inputs
            }
            already_read.update(
                (validate_ref_shape(source["record"])["path"], validate_ref_shape(source["record"])["sha256"], validate_ref_shape(source["record"])["byte_len"], validate_ref_shape(source["record"])["media_type"])
                for source in manifest["sources"]
            )
            seen_source_refs: set[tuple[str, str, int, str]] = set()
            for source in manifest["sources"]:
                source_ref = validate_ref_shape(source["bytes"], pointer="/sources/bytes")
                key = (source_ref["path"], source_ref["sha256"], source_ref["byte_len"], source_ref["media_type"])
                if key not in already_read and key not in seen_source_refs:
                    reader.read_ref(source_ref)
                seen_source_refs.add(key)
            runtime = manifest["runtime_bundle"]
            runtime_ref = validate_ref_shape(runtime["manifest"])
            runtime_manifest = strict_json_loads(reader.read_ref(runtime_ref))
            if runtime_manifest.get("digest") != runtime["digest"]:
                raise ContractError("artifact_changed", "portable runtime bundle identity changed")


def package_delivery(
    store: CaseStore,
    *,
    case_id: str,
    expected_state_revision: int,
    delivery_id: str,
    target_path: str,
    include_raw_sources: bool,
    stamp: Mapping[str, str],
    validate_current: Callable[[Mapping[str, Any]], None] | None = None,
    session_commit: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    state = store.read_case(case_id)
    _require_owner(state, stamp)
    if state["state_revision"] != expected_state_revision or state["selected_delivery_id"] != delivery_id:
        raise ContractError("stale_revision", "delivery is no longer selected")
    source_view = _check_pending_and_current_inputs(store, state)
    delivery, selection_ref = _load_delivery(store, case_id, delivery_id)
    prior_presentation = _load_presentation(store, state, delivery_id, selection_ref, delivery=delivery)
    expected_presentation = (state.get("current_records") or {}).get("presentation")
    _check_delivery_binding(store, state, delivery, math_source_ids=source_view["math_source_ids"])
    _check_delivery_authorities(store, state, delivery)
    _validate_delivery_diagnostic_context(store, state, delivery)
    _delivery_result_projection(store, state, delivery)
    target = _visible_target(store, target_path)
    if target.suffix.lower() != ".zip":
        raise ContractError("invalid_argument", "package target must end in .zip", pointer="/target_path")
    if target.exists():
        raise ContractError("package_collision", "package target already exists")
    files: dict[str, bytes] = {}
    objects: list[dict[str, Any]] = []
    portable_data: list[dict[str, Any]] = []
    portable_reviews: list[dict[str, Any]] = []
    review_closure_refs: list[dict[str, Any]] = []
    runtime_bundles: dict[str, dict[str, Any]] = {}
    ref_map: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    reproduction_runs: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    reproduction_profiles: list[dict[str, Any]] = []
    reproduction_requirements: list[bytes] = []
    unsupported_runs: list[str] = []
    candidate_record: dict[str, Any] | None = None
    selection_raw = store.read_ref(selection_ref)
    _register_portable_ref(
        store, selection_ref, "evidence/selection.json", files, objects, ref_map, raw_bytes=selection_raw
    )
    if delivery["candidate_id"] and not delivery["run_ids"]:
        candidate_record = store.read_record(f".reaslab/optagent/cases/{case_id}/candidates/{delivery['candidate_id']}.json")
        for ref in [candidate_record["model"], *candidate_record["local_modules"], *([candidate_record["business_checker"]] if candidate_record["business_checker"] else [])]:
            raw = store.read_ref(ref)
            package_path = _candidate_package_path(ref["path"])
            _add_package_file(files, package_path, raw, objects, digest=ref["sha256"])
        if candidate_record.get("data_id") is not None:
            portable_data.append(_package_data_bundle(
                store,
                case_id,
                candidate_record["data_id"],
                files,
                objects,
                ref_map,
                source_view["records"],
                source_view["record_refs"],
            ))
    for proof_id in delivery["proof_ids"]:
        proof = store.read_record(f".reaslab/optagent/cases/{case_id}/proofs/{proof_id}.json")
        body = store.read_ref(proof["body"])
        _add_package_file(files, f"evidence/proofs/{proof_id}{_suffix(proof['body']['path'])}", body, objects, digest=proof["body"]["sha256"])
        _add_package_file(files, f"evidence/proofs/{proof_id}.json", canonical_json_bytes(proof), objects)
        for ref in proof["evidence_refs"]:
            raw = _validate_case_evidence_ref(store, state, ref)
            _add_package_file(files, f"evidence/attachments/{ref['sha256']}{_suffix(ref['path'])}", raw, objects, digest=ref["sha256"])
    for run_id in delivery["run_ids"]:
        run, run_ref = _load_run(store, case_id, run_id)
        run_raw = store.read_ref(run_ref)
        _register_portable_ref(
            store, run_ref, f"evidence/runs/{run_id}/record.json", files, objects, ref_map, raw_bytes=run_raw
        )
        run_output_root = f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output/"
        seal_ref_value = run.get("worker", {}).get("seal")
        if run.get("results") and seal_ref_value is None:
            raise ContractError("artifact_changed", "selected run results have no worker seal")
        seal_ref: dict[str, Any] | None = None
        if seal_ref_value is not None:
            seal_ref = validate_ref_shape(dict(seal_ref_value))
            if seal_ref["path"] != run_output_root + "seal.json":
                raise ContractError("artifact_changed", "selected run seal is outside its fixed output root")
            seal_raw = _register_portable_ref(
                store,
                seal_ref,
                f"evidence/runs/{run_id}/seal.json",
                files,
                objects,
                ref_map,
            )
            seal = strict_json_loads(seal_raw)
            if not isinstance(seal, dict) or not isinstance(seal.get("output_refs"), list):
                raise ContractError("output_invalid", "selected run seal has no output reference list")
            output_refs: list[dict[str, Any]] = []
            for index, value in enumerate(seal["output_refs"]):
                output_ref = validate_ref_shape(value, pointer=f"/seal/output_refs/{index}")
                path = output_ref["path"]
                if not path.startswith(run_output_root) or path == seal_ref["path"]:
                    raise ContractError("artifact_changed", "sealed output is outside its fixed run output root")
                suffix = path[len(run_output_root):]
                pure_suffix = PurePosixPath(suffix)
                if pure_suffix.is_absolute() or not pure_suffix.parts or any(part in {"", ".", ".."} or _is_reserved_package_part(part) for part in pure_suffix.parts):
                    raise ContractError("path_denied", "sealed output path is not packageable")
                output_refs.append(output_ref)
            if len({item["path"] for item in output_refs}) != len(output_refs):
                raise ContractError("output_invalid", "selected run seal repeats an output path")
            if output_refs != sorted(output_refs, key=lambda item: item["path"]):
                raise ContractError("output_invalid", "selected run seal output references are not in canonical order")
            output_ref_keys = {
                (item["path"], item["sha256"], item["byte_len"], item["media_type"])
                for item in output_refs
            }
            for result_ref_value in run.get("results", []):
                result_ref = validate_ref_shape(dict(result_ref_value))
                result_key = (result_ref["path"], result_ref["sha256"], result_ref["byte_len"], result_ref["media_type"])
                if result_key not in output_ref_keys:
                    raise ContractError("artifact_changed", "selected Result is absent from the sealed output closure")
            for output_ref in output_refs:
                suffix = output_ref["path"][len(run_output_root):]
                _register_portable_ref(
                    store,
                    output_ref,
                    f"evidence/runs/{run_id}/outputs/{suffix}",
                    files,
                    objects,
                    ref_map,
                )
        run_profiles: list[dict[str, Any]] = []
        for index, ref in enumerate(run.get("results", [])):
            result_raw = store.read_ref(ref)
            try:
                result_value = strict_json_loads(result_raw, max_bytes=len(result_raw))
            except (ValueError, UnicodeError) as exc:
                raise ContractError("artifact_changed", "selected Result is invalid JSON") from exc
            if not isinstance(result_value, dict):
                raise ContractError("artifact_changed", "selected Result is not a JSON object")
            backend_profile = result_value.get("backend")
            if isinstance(backend_profile, Mapping):
                run_profiles.append(dict(backend_profile))
            _add_package_file(files, f"evidence/runs/{run_id}/result-{index}.json", result_raw, objects, digest=ref["sha256"])
        for name, ref in (
            ("seal", None),
            ("transport", run.get("transport", {}).get("response")),
            ("platform-status", run.get("platform", {}).get("status_evidence")),
            ("stop-receipt", (run.get("cancel", {}).get("stop_attempt") or {}).get("receipt")),
            ("stop-ack", run.get("cancel", {}).get("stop_ack")),
        ):
            if ref is not None:
                _add_package_file(files, f"evidence/runs/{run_id}/{name}{_suffix(ref['path'])}", store.read_ref(ref), objects, digest=ref["sha256"])
        prepared = store.read_record(f".reaslab/optagent/cases/{case_id}/prepared/{run['prepared_id']}/prepared.json")
        input_manifest_ref = prepared.get("input_manifest")
        input_manifest_raw = store.read_ref(input_manifest_ref)
        _add_package_file(files, f"evidence/runs/{run_id}/prepared-input-manifest.json", input_manifest_raw, objects)
        input_manifest = strict_json_loads(input_manifest_raw)
        if not isinstance(input_manifest, dict) or input_manifest.get("case_id") != case_id or input_manifest.get("prepared_id") != run["prepared_id"]:
            raise ContractError("artifact_changed", "selected run PreparedInputManifest identity is invalid")
        if type(input_manifest.get("schema_version")) is not int or input_manifest["schema_version"] not in {2, 3}:
            raise ContractError("output_invalid", "selected run PreparedInputManifest schema is invalid")
        runtime_info = _package_runtime_bundle(
            store,
            files,
            objects,
            ref_map,
            input_manifest.get("runtime_bundle"),
            prepared_schema_version=input_manifest["schema_version"],
        )
        runtime_bundles[runtime_info["digest"]] = runtime_info
        input_ref = validate_ref_shape(input_manifest_ref)
        if include_raw_sources:
            closure = _package_prepared_closure(store, input_ref, case_id, run["prepared_id"], files, objects, ref_map)
            runtime_info = closure["runtime"]
            runtime_bundles[runtime_info["digest"]] = runtime_info
            if runtime_info["standalone_supported"]:
                requirements_spec = closure["manifest"]["candidate"].get("requirements_file")
                if requirements_spec is not None:
                    reproduction_requirements.append(store.read_ref(requirements_spec["bytes"]))
                key = (input_ref["path"], input_ref["sha256"], input_ref["byte_len"], input_ref["media_type"])
                group = reproduction_runs.setdefault(key, {"prepared_manifest_ref": input_ref, "evidence_run_ids": []})
                if run_id not in group["evidence_run_ids"]:
                    group["evidence_run_ids"].append(run_id)
                prepared_schema_version = closure["manifest"]["schema_version"]
                backend_id = closure["manifest"]["candidate"]["backend_id"]
                if prepared_schema_version == 2:
                    reproduction_profiles.append({"schema_version": 2, "id": "highs", "package_versions": {}})
                else:
                    matching_profiles = [profile for profile in run_profiles if profile.get("id") == backend_id]
                    if matching_profiles:
                        reproduction_profiles.extend(
                            {"schema_version": 3, **profile} for profile in matching_profiles
                        )
                    else:
                        reproduction_profiles.append({
                            "schema_version": 3,
                            "id": backend_id,
                            "package_versions": {},
                        })
            else:
                unsupported_runs.append(run_id)
    for review_id in delivery["review_ids"]:
        review_projection, closure_refs = _package_review_closure(
            store, state, review_id, files, objects, ref_map
        )
        portable_reviews.append(review_projection)
        review_closure_refs.extend(closure_refs)
    if include_raw_sources:
        for source_id in source_view["source_ids"]:
            source = source_view["records"][source_id]
            raw = store.read_ref(source["bytes"])
            original = source["origin"].get("workspace_path")
            if original:
                package_path = _candidate_package_path(original)
            else:
                package_path = f"source/capture-{source_id}.txt"
            _add_package_file(files, package_path, raw, objects, digest=source["bytes"]["sha256"])
    claim_attachments: list[dict[str, Any]] = []
    portable_claims: list[dict[str, Any]] = []
    for claim_index, claim in enumerate(delivery["claims"]):
        portable_claim = dict(claim)
        portable_refs: list[dict[str, Any]] = []
        for ref in claim["evidence_refs"]:
            raw = _validate_case_evidence_ref(store, state, ref)
            package_path = f"evidence/claim-{claim_index}/{ref['sha256']}{_suffix(ref['path'])}"
            _add_package_file(files, package_path, raw, objects, digest=ref["sha256"])
            claim_attachments.append({"claim_index": claim_index, "path": package_path, "sha256": ref["sha256"]})
            portable_refs.append(ref_from_bytes(package_path, raw, ref["media_type"]))
        portable_claim["evidence_refs"] = portable_refs
        portable_claims.append(portable_claim)
    reproductions = [
        {"prepared_manifest_ref": group["prepared_manifest_ref"], "evidence_run_ids": sorted(group["evidence_run_ids"])}
        for _, group in sorted(reproduction_runs.items())
        if not any(run_id in unsupported_runs for run_id in group["evidence_run_ids"])
    ]
    if reproductions:
        bootstrap_path = Path(__file__).resolve().parents[1] / "optagent_reproduce.py"
        try:
            bootstrap_path.lstat()
            if not bootstrap_path.is_file() or bootstrap_path.is_symlink():
                raise OSError("fixed product entry is not a regular file")
            bootstrap_raw = bootstrap_path.read_bytes()
        except OSError as exc:
            raise ContractError("environment_unavailable", "fixed standalone entry is unavailable") from exc
        _add_package_file(files, "reproduce/run.py", bootstrap_raw, objects)
        legacy_requirements = any(profile.get("schema_version") == 2 for profile in reproduction_profiles)
        requirements = _solver_requirements_bytes(
            [profile for profile in reproduction_profiles if profile.get("schema_version") == 3],
            include_legacy_highs=legacy_requirements,
            declared_requirements=reproduction_requirements,
        )
        _add_package_file(files, "reproduce/requirements.txt", requirements, objects)
        ref_map_items = sorted(ref_map.values(), key=lambda item: (item["ref"]["path"], item["ref"]["sha256"], item["ref"]["byte_len"], item["ref"]["media_type"]))
        _verify_portable_reproductions(store.workspace, files, ref_map_items, reproductions)
    else:
        ref_map_items = sorted(ref_map.values(), key=lambda item: (item["ref"]["path"], item["ref"]["sha256"], item["ref"]["byte_len"], item["ref"]["media_type"]))

    proof_only = delivery["kind"] == "proof" and not delivery["run_ids"]
    if proof_only:
        reproduce = "# Proof review\n\nThis package contains the selected proof, its stated premises and conditions, review records, and any explicitly selected raw sources. Recheck the argument within that scope. No prepared input or solver run is implied.\n"
    elif reproductions:
        reproduce = (
            "# Local reproduction\n\n"
            "This package includes the selected runs' frozen input manifests and Ref-addressed closures. "
            "It performs a new local measurement; it does not create a platform Run, approval, or Seal. "
            "Extract the archive in a supported Linux x86_64 / CPython 3.12 environment, run "
            "`python -m pip install -r reproduce/requirements.txt`, then run "
            "`python -I reproduce/run.py --output-dir /absolute/path/to/fresh-output`. "
            "The output path must not exist and must remain outside the extracted package. The program does not install dependencies or access the network.\n"
        )
    else:
        limitations = []
        if delivery["run_ids"] and not include_raw_sources:
            limitations.append("Raw frozen source inputs were omitted by the package option, so no standalone entry is provided.")
        if unsupported_runs:
            limitations.append("The selected frozen worker snapshot has no local reproduction entry; its helper bytes were preserved unchanged.")
        if delivery["run_ids"] and not limitations:
            limitations.append("No selected run has a verified local reproduction entry.")
        detail = " ".join(limitations) if limitations else "No standalone native run was selected."
        reproduce = f"# Evidence package\n\nThe archive preserves selected evidence and any permitted source files. {detail} Packaging did not execute a model.\n"
    approved_answer_links = []
    for review in portable_reviews:
        answer_ref = review.get("answer_ref")
        if answer_ref is None:
            continue
        answer_mapping = ref_map[_ref_identity(answer_ref)]
        approved_answer_links.append(
            f"- [Approved deliverable]({quote(answer_mapping['package_path'], safe='/')})"
        )
    if approved_answer_links:
        reproduce = reproduce.rstrip() + "\n\n## Approved deliverable\n\n" + "\n".join(approved_answer_links) + "\n"
    files["README.md"] = reproduce.encode("utf-8")
    consumed_refs = _unique_refs(
        [selection_ref, *delivery.get("evidence_refs", [])]
        + [ref for family in ("reviews", "runs") for ref in delivery["authority_records"][family].values()]
        + review_closure_refs
    )
    manifest = {
        "schema_version": 1,
        "case_id": case_id,
        "delivery_id": delivery_id,
        "selection_ref": selection_ref,
        "authority_records": delivery["authority_records"],
        "consumed_refs": consumed_refs,
        "objects": objects,
        "claims": portable_claims,
        "reviews": portable_reviews,
        "claim_attachments": claim_attachments,
        "data_bundles": portable_data,
        "runtime_bundles": list(runtime_bundles.values()),
        "reproductions": reproductions,
        "ref_map": ref_map_items,
        "entry": None if proof_only else ("reproduce/run.py" if reproductions else "README.md"),
    }
    inner_manifest_raw = canonical_json_bytes(manifest)
    inner_manifest_sha256 = sha256_bytes(inner_manifest_raw)
    _add_package_file(files, "manifest.json", inner_manifest_raw, objects, digest=inner_manifest_sha256)
    package_bytes = _zip_bytes(files)
    store._atomic_write(target, package_bytes, replace=False)
    package_ref = ref_from_bytes(target_path.replace("\\", "/"), package_bytes, "application/zip")
    try:
        external_manifest = {
            "schema_version": 1,
            "case_id": case_id,
            "delivery_id": delivery_id,
            "selection_ref": selection_ref,
            "authority_records": delivery["authority_records"],
            "consumed_refs": consumed_refs,
            "package_ref": package_ref,
            "inner_manifest_sha256": inner_manifest_sha256,
        }
        manifest_ref = store.write_content_addressed(
            f".reaslab/optagent/cases/{case_id}/deliveries/{delivery_id}/package-manifests",
            canonical_json_bytes(external_manifest),
            "application/json",
        )
        next_presentation = dict(prior_presentation)
        packages = list(next_presentation["packages"])
        packages.append({"include_raw_sources": include_raw_sources, "package_ref": package_ref, "manifest_ref": manifest_ref})
        next_presentation["packages"] = packages
        presentation_ref = _write_presentation(store, case_id, next_presentation)
        next_state = _publish_presentation(
            store,
            case_id=case_id,
            state=state,
            delivery=delivery,
            selection_ref=selection_ref,
            expected_presentation=expected_presentation,
            next_presentation=presentation_ref,
            stamp=stamp,
            validate_current=validate_current,
            session_commit=session_commit,
        )
    except ContractError as exc:
        raise _local_committed_error(exc, "package", package_path=target_path, package_ref=package_ref) from exc
    except OSError as exc:
        failure = ContractError(
            "environment_unavailable",
            "The package was committed, but its manifest or presentation could not be published.",
            details={"errno": exc.errno},
        )
        raise _local_committed_error(
            failure, "package", package_path=target_path, package_ref=package_ref
        ) from exc
    return {
        "state": state_summary(store, next_state),
        "delivery": object_summary(delivery_id, selection_ref, "selected", delivery["source_revision"], next_state["candidate_revision"]),
        "package_ref": package_ref,
        "manifest_ref": manifest_ref,
        "presentation_ref": presentation_ref,
    }


def _render_report(
    state: Mapping[str, Any],
    delivery: Mapping[str, Any],
    suffix: str,
    result_projection: Sequence[Mapping[str, Any]] = (),
    review_projection: Sequence[Mapping[str, Any]] = (),
) -> bytes:
    review_findings = [dict(item) for item in review_projection]
    base = {
        "case": state["title"],
        "task_kind": state["task_kind"],
        "evidence_kind": delivery["kind"],
        "claims": [
            {
                "kind": claim["kind"],
                "scope": claim["scope"],
                "support": claim["support"],
                "conditions": list(claim["conditions"]),
                "evidence_refs": [ref["path"] for ref in claim["evidence_refs"]],
            }
            for claim in delivery["claims"]
        ],
        "remaining": _delivery_remaining(review_findings),
        "review_findings": review_findings,
        "result_evidence": [dict(item) for item in result_projection],
        "limitations": [],
    }
    if suffix == ".json":
        return canonical_json_bytes(base) + b"\n"
    if suffix != ".md":
        raise ContractError("invalid_argument", "report target must end in .md or .json", pointer="/target_path")
    lines = [
        f"# {state['title']}",
        "",
        f"Scenario type: {state['task_kind']}",
        f"Evidence type: {delivery['kind']}",
        "",
        "## Reviewed claims",
    ]
    for claim in base["claims"]:
        lines.extend(
            [
                f"### {claim['kind']} — {claim['support']}",
                "",
                claim["scope"],
            ]
        )
        for condition in claim["conditions"]:
            lines.append(f"- Condition: {condition}")
        if claim["evidence_refs"]:
            lines.append("- Evidence: " + ", ".join(claim["evidence_refs"]))
        lines.append("")
    if base["remaining"]:
        lines.append("## Remaining conditions")
        lines.extend(f"- {item}" for item in base["remaining"])
        lines.append("")
    if base["review_findings"]:
        lines.extend(["## Review findings", ""])
        for finding in base["review_findings"]:
            review_id = finding["review_id"]
            lines.extend([f"### Review {review_id}", ""])
            if finding["conclusion_claims"]:
                lines.extend(["#### Verifier conclusions", ""])
                for claim in finding["conclusion_claims"]:
                    lines.extend([
                        f"- {claim['kind']} — {claim['support']}: {claim['scope']}",
                    ])
                    lines.extend(f"  - Condition: {condition}" for condition in claim["conditions"])
                    if claim["evidence_refs"]:
                        lines.append("  - Evidence: " + ", ".join(ref["path"] for ref in claim["evidence_refs"]))
            for item in finding["remaining"]:
                lines.append(f"- Remaining: {item}")
            for issue in finding["issues"]:
                lines.extend([f"#### Issue {issue['issue_id']}: {issue['claim']}", ""])
                if issue["locations"]:
                    lines.append("- Original locations: " + ", ".join(
                        f"{location['source_id']} — {location['locator']}" for location in issue["locations"]
                    ))
                if issue["candidate_locations"]:
                    lines.append("- Candidate locations: " + ", ".join(issue["candidate_locations"]))
                if issue["evidence_refs"]:
                    lines.append("- Issue evidence: " + ", ".join(ref["path"] for ref in issue["evidence_refs"]))
                for disposition in issue["owner_dispositions"]:
                    lines.append(
                        f"- Owner disposition: {disposition['diagnosis']}; {disposition['patch_status']}; {disposition['reason']}"
                    )
                lines.append("")
    if base["result_evidence"]:
        lines.extend(["## Selected numerical evidence", ""])
        for item in base["result_evidence"]:
            objective = item.get("objective")
            objective_text = "no objective value"
            if isinstance(objective, Mapping):
                objective_text = f"{objective.get('value')} {objective.get('unit') or ''}".rstrip()
            sense = objective.get("sense") if isinstance(objective, Mapping) else None
            model_check = item.get("model_check") if isinstance(item.get("model_check"), Mapping) else {}
            business_check = item.get("business_check") if isinstance(item.get("business_check"), Mapping) else {}
            lines.extend([
                f"### {item['claim_kind']} — instance {item['instance_key']} — layer {item['layer']}",
                "",
                f"Objective: {objective_text}",
                f"Objective sense: {_report_label_value(sense)}",
                f"Backend: {item['backend'].get('id')} {item['backend'].get('version')} ({item['raw_status']}; {item['termination']})",
                f"Model check: {_report_label_value(model_check.get('status'))}",
                *(
                    [
                        f"Model check method: {_report_label_value(model_check.get('method'))}",
                        "Model check scope: " + _report_json_value(model_check.get("scope")),
                    ]
                    if model_check.get("method") == "core_cp_sat_response"
                    else []
                ),
                f"Model variable coverage: {_report_json_value(model_check.get('variable_coverage'))}",
                f"Model component coverage: {_report_json_value(model_check.get('component_coverage'))}",
                f"Model uncovered items: {_report_json_value(model_check.get('uncovered'))}",
                f"Business check: {_report_label_value(business_check.get('status'))}",
                f"Business checked obligations: {_report_json_value(business_check.get('checked_obligations'))}",
                f"Business uncovered items: {_report_json_value(business_check.get('uncovered'))}",
            ])
            bound = item.get("bound")
            if isinstance(bound, Mapping):
                lines.append(f"Global {bound['direction']} bound: {bound['value']} {bound.get('scope')}")
            gap = item.get("gap")
            if isinstance(gap, Mapping):
                lines.append(f"Gap: absolute {gap.get('absolute')}; relative {gap.get('relative')}")
            numerical = item.get("numerical")
            if isinstance(numerical, Mapping):
                lines.append("Requested numeric options: " + _report_json_value(numerical.get("requested_options")))
                lines.append("Effective numeric options: " + _report_json_value(numerical.get("effective_options")))
                lines.append("Model-check tolerances: " + _report_json_value(numerical.get("check_tolerances")))
                closure = numerical.get("optimality")
                if isinstance(closure, Mapping):
                    lines.append(
                        "Numerical optimality closure: "
                        f"{closure.get('status')} ({closure.get('direction')}; {closure.get('qualification')}); "
                        f"objective={closure.get('objective')}, global_bound={closure.get('global_bound')}, "
                        f"native_reported_bound={closure.get('native_reported_bound')}, "
                        f"native_gap={closure.get('native_gap')}, "
                        f"objective_recomputation_residual={closure.get('objective_recomputation_residual')}, "
                        f"closure_residual={closure.get('closure_residual')}, "
                        f"strict_gap={closure.get('absolute_gap')}, allowed_gap={closure.get('allowed_gap')}"
                    )
                    if closure.get("status") == "closed":
                        lines.append("Numerically closed under the declared tolerance; this is not an exact optimality proof.")
            lexicographic = item.get("lexicographic")
            if isinstance(lexicographic, Mapping):
                lines.append(
                    "Lexicographic restriction: "
                    f"previous layer {lexicographic.get('previous_layer')} {lexicographic.get('sense')}; "
                    f"bound={lexicographic.get('global_bound')}, gap={lexicographic.get('absolute_gap')}, "
                    f"tolerance={lexicographic.get('declared_tolerance')}; subsequent layer scope is restricted"
                )
            batch = item.get("batch")
            if isinstance(batch, Mapping):
                lines.append(
                    "Batch coverage: "
                    f"{batch['instances_with_results']}/{batch['planned_instance_count']} instances with Results; "
                    f"{batch['started_instance_count']} started; "
                    f"{batch['instances_with_all_layers_attempted']} had all target layers attempted "
                    "(attempted does not imply successful or optimal); "
                    f"{batch['partial_instance_count']} partial; "
                    f"{batch['remaining_instance_count']} remaining; "
                    f"stop reason={batch.get('stop_reason') or 'none'}"
                )
                batch_ref = item.get("batch_summary_ref")
                if isinstance(batch_ref, Mapping) and isinstance(batch_ref.get("path"), str):
                    lines.append(f"Batch summary evidence: {batch_ref['path']}")
            else:
                lines.append("Batch coverage: unknown (no verified batch summary).")
            lines.append(f"Result evidence: {item['result_ref']['path']}")
            lines.append("")
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _report_label_value(value: Any) -> str:
    return "not provided" if value is None else str(value)


def _report_json_value(value: Any) -> str:
    if value is None:
        return "not provided"
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _delivery_remaining(
    review_projection: Sequence[Mapping[str, Any]],
) -> list[str]:
    remaining: list[str] = []
    for finding in review_projection:
        for item in finding.get("remaining", []):
            if isinstance(item, str) and item not in remaining:
                remaining.append(item)
    return remaining


def _delivery_review_projection(
    store: CaseStore,
    state: Mapping[str, Any],
    delivery: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    review_ids = delivery.get("review_ids", [])
    if not isinstance(review_ids, list):
        raise ContractError("output_invalid", "selected review identifiers are invalid")
    for review_id in review_ids:
        if not isinstance(review_id, str):
            raise ContractError("output_invalid", "selected review identifier is invalid")
        review, review_ref, _ = _load_review_snapshot(store, state["case_id"], review_id)
        outcomes = review.get("outcomes", [])
        answer_ref_value = review.get("answer_ref")
        if (
            review.get("status") != "closed"
            or not isinstance(outcomes, list)
            or not outcomes
            or not isinstance(outcomes[-1], Mapping)
            or outcomes[-1].get("decision") != "ready"
            or answer_ref_value is None
        ):
            raise ContractError("output_invalid", "selected ready Review has no approved answer Ref")
        answer_ref = validate_ref_shape(dict(answer_ref_value), pointer="/review/answer_ref")
        _validate_case_evidence_ref(store, state, answer_ref)
        conclusion = review.get("conclusion")
        if conclusion is not None:
            if not isinstance(conclusion, Mapping) or not isinstance(conclusion.get("claims", []), list):
                raise ContractError("output_invalid", "selected Review conclusion is invalid")
            for claim in conclusion.get("claims", []):
                if not isinstance(claim, Mapping) or not isinstance(claim.get("evidence_refs", []), list):
                    raise ContractError("output_invalid", "selected Review claim references are invalid")
                for value in claim.get("evidence_refs", []):
                    _validate_case_evidence_ref(
                        store, state,
                        validate_ref_shape(dict(value), pointer="/review/conclusion/claims/evidence_refs"),
                    )
        issues_value = review.get("issues", [])
        if not isinstance(issues_value, list):
            raise ContractError("output_invalid", "selected Review Issue references are invalid")
        issues: list[dict[str, Any]] = []
        for issue_ref_value in issues_value:
            issue, _, issue_ref = _load_review_issue(store, state["case_id"], review_id, issue_ref_value)
            for value in issue.get("evidence_refs", []):
                _validate_case_evidence_ref(
                    store, state,
                    validate_ref_shape(dict(value), pointer="/review/issues/evidence_refs"),
                )
            issues.append({"issue_ref": issue_ref, **issue})
        rows.append(_review_projection(review, review_ref, issues))
    return rows


def _delivery_result_projection(
    store: CaseStore,
    state: Mapping[str, Any],
    delivery: Mapping[str, Any],
) -> list[dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    selected_runs = set(delivery.get("run_ids", []))
    for claim in delivery.get("claims", []):
        for raw_ref in claim.get("evidence_refs", []):
            ref = validate_ref_shape(dict(raw_ref), pointer="/delivery/claims/evidence_refs")
            parts = _result_path_parts(state["case_id"], ref["path"])
            if parts is None or claim.get("kind") not in {"feasibility", "optimality", "local_optimality", "bound"}:
                continue
            run_id, _ = parts
            if run_id not in selected_runs:
                raise ContractError("evidence_insufficient", "report Result is not tied to a selected run")
            verified = _verified_run(store, state, run_id, cache, require_current=True)
            result = verified["results"].get(_ref_identity(ref))
            if result is None or not _result_supports_claim(
                store,
                state,
                result,
                ref,
                claim["kind"],
                cache,
                run_id,
                claim_support=claim["support"],
                claim_conditions=claim["conditions"],
            ):
                raise ContractError("evidence_insufficient", "report Result no longer matches its selected claim")
            check_ref = validate_ref_shape(result["check"], pointer="/result/check")
            check = _read_verified_output_json(store, verified, check_ref)
            business_path = ref["path"].rsplit("/", 1)[0] + "/business-check.json"
            business_ref = next(
                (validate_ref_shape(item) for item in verified["seal"]["output_refs"] if item["path"] == business_path),
                None,
            )
            batch_summary = verified["seal"].get("batch_summary")
            batch = batch_summary.get("summary") if isinstance(batch_summary, Mapping) else None
            if not isinstance(check, dict) or business_ref is None:
                raise ContractError("output_invalid", "report Result is missing its verified check records")
            business = _read_verified_output_json(store, verified, business_ref)
            if not isinstance(business, dict) or (batch is not None and not isinstance(batch, dict)):
                raise ContractError("output_invalid", "report check or batch summary is invalid")
            rows.append({
                "claim_kind": claim["kind"],
                "claim_scope": claim["scope"],
                "claim_support": claim["support"],
                "result_ref": ref,
                "instance_key": list(result["instance_key"]),
                "instance_binding_digest": result["instance_binding_digest"],
                "layer": result["layer"],
                "objective": result["objective"],
                "bound": result["bound"],
                "gap": result["gap"],
                "numerical": result.get("numerical"),
                "lexicographic": result.get("lexicographic"),
                "batch_summary_ref": batch_summary.get("ref") if isinstance(batch_summary, Mapping) else None,
                "backend": result["backend"],
                "raw_status": result["raw_status"],
                "termination": result["termination"],
                "solver_invoked": result["solver_invoked"],
                "model_check": {
                    "status": check.get("status"),
                    "variable_coverage": check.get("variable_coverage"),
                    "component_coverage": check.get("component_coverage"),
                    "uncovered": check.get("uncovered"),
                },
                "business_check": {
                    "status": business.get("status"),
                    "checked_obligations": business.get("checked_obligations"),
                    "uncovered": business.get("uncovered"),
                },
                "batch": batch,
            })
    return rows


def _layout_equivalent(actual: bytes, expected: bytes, suffix: str) -> bool:
    try:
        if suffix == ".json":
            return canonical_json_bytes(strict_json_loads(actual)) == canonical_json_bytes(
                strict_json_loads(expected)
            )
        left = " ".join(actual.decode("utf-8").split())
        right = " ".join(expected.decode("utf-8").split())
        return left == right
    except (ValueError, UnicodeError):
        return False


def _visible_target(store: CaseStore, target_path: str) -> Path:
    path = store.resolve_path(target_path)
    parts = PurePosixPath(target_path.replace("\\", "/")).parts
    if any(_is_reserved_package_part(part) for part in parts):
        raise ContractError("path_denied", "target path is reserved")
    if parts and parts[0].casefold() == ".reaslab":
        raise ContractError("path_denied", "report targets must be user-visible paths")
    return path


def _candidate_package_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or (pure.parts and pure.parts[0].casefold() == ".reaslab")
        or any(_is_reserved_package_part(part) for part in pure.parts)
    ):
        raise ContractError("path_denied", "source path is not safe to include")
    return f"source/{pure.as_posix()}"


def _add_package_file(
    files: dict[str, bytes],
    package_path: str,
    raw: bytes,
    objects: list[dict[str, Any]],
    *,
    allow_runtime: bool = False,
    digest: str | None = None,
) -> None:
    path = PurePosixPath(package_path)
    safe_runtime_tree = allow_runtime and len(path.parts) >= 4 and path.parts[:2] == ("reproduce", "runtime")
    if (
        path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or (not safe_runtime_tree and any(_is_reserved_package_part(part) for part in path.parts))
    ):
        raise ContractError("path_denied", "package path is not safe")
    safe = path.as_posix()
    if safe in files:
        if files[safe] != raw:
            raise ContractError("package_collision", "two selected inputs map to the same package path")
        return
    files[safe] = raw
    objects.append({"path": safe, "sha256": digest or sha256_bytes(raw), "byte_len": len(raw)})


def _is_reserved_package_part(part: str) -> bool:
    folded = part.casefold()
    return (
        folded in _NO_PACKAGE_PARTS
        or folded.startswith(".env.")
        or folded.endswith((".pem", ".key"))
    )


def _zip_bytes(files: Mapping[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o600 << 16
            info.date_time = (1980, 1, 1, 0, 0, 0)
            archive.writestr(info, files[name])
    return output.getvalue()


def _load_review(store: CaseStore, case_id: str, review_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    path = f".reaslab/optagent/cases/{case_id}/reviews/{review_id}.json"
    review, ref = store.read_family_record(case_id, "reviews", review_id, legacy_path=path)
    if review.get("case_id") != case_id or review.get("review_id") != review_id:
        raise ContractError("artifact_changed", "review identity does not match its fixed path")
    return review, ref


def _load_review_snapshot(
    store: CaseStore,
    case_id: str,
    review_id: str,
    review_ref: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    if review_ref is not None:
        ref = validate_ref_shape(dict(review_ref), pointer="/review_ref")
        prefix = f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/versions/"
        if ref["path"] != prefix + ref["sha256"] + ".json" or ref["media_type"] != "application/json":
            raise ContractError("artifact_changed", "pinned Review Ref is outside its immutable family path")
        raw = store.read_ref(ref)
        try:
            review = strict_json_loads(raw, max_bytes=len(raw))
        except (ValueError, UnicodeError) as exc:
            raise ContractError("artifact_changed", "pinned Review snapshot is invalid JSON") from exc
        if not isinstance(review, dict) or canonical_json_bytes(review) != raw:
            raise ContractError("artifact_changed", "pinned Review snapshot is not canonical")
        if review.get("case_id") != case_id or review.get("review_id") != review_id:
            raise ContractError("artifact_changed", "pinned Review snapshot identity does not match its Ref")
        return review, ref, raw
    path = f".reaslab/optagent/cases/{case_id}/reviews/{review_id}.json"
    review, ref, raw = store.read_family_record(
        case_id, "reviews", review_id, legacy_path=path, include_raw=True
    )
    if review.get("case_id") != case_id or review.get("review_id") != review_id:
        raise ContractError("artifact_changed", "review identity does not match its fixed path")
    return review, ref, raw


def _load_review_issue(
    store: CaseStore,
    case_id: str,
    review_id: str,
    raw_ref: Mapping[str, Any],
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    from .review import _read_issue_snapshot

    ref = validate_ref_shape(dict(raw_ref), pointer="/review/issues")
    return _read_issue_snapshot(store, case_id, review_id, ref)


def _review_projection(
    review: Mapping[str, Any],
    review_ref: Mapping[str, Any],
    issues: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    conclusion = review.get("conclusion")
    conclusion_claims: list[dict[str, Any]] = []
    remaining: list[str] = []
    if conclusion is not None:
        if not isinstance(conclusion, Mapping):
            raise ContractError("output_invalid", "selected Review conclusion is invalid")
        claims_value = conclusion.get("claims", [])
        remaining_value = conclusion.get("remaining", [])
        if (
            not isinstance(claims_value, list)
            or not isinstance(remaining_value, list)
            or not all(isinstance(item, str) for item in remaining_value)
        ):
            raise ContractError("output_invalid", "selected Review conclusion fields are invalid")
        for claim in claims_value:
            if not isinstance(claim, Mapping):
                raise ContractError("output_invalid", "selected Review claim is invalid")
            conditions = claim.get("conditions", [])
            evidence_refs = claim.get("evidence_refs", [])
            if not isinstance(conditions, list) or not all(isinstance(item, str) for item in conditions) or not isinstance(evidence_refs, list):
                raise ContractError("output_invalid", "selected Review claim conditions or references are invalid")
            conclusion_claims.append({
                "kind": claim.get("kind"),
                "scope": claim.get("scope"),
                "support": claim.get("support"),
                "conditions": list(conditions),
                "evidence_refs": [
                    validate_ref_shape(dict(ref), pointer="/review/conclusion/claims/evidence_refs")
                    for ref in evidence_refs
                ],
            })
        remaining = list(remaining_value)
    return {
        "review_id": review.get("review_id"),
        "review_ref": validate_ref_shape(dict(review_ref), pointer="/review_ref"),
        "answer_ref": (
            validate_ref_shape(dict(review["answer_ref"]), pointer="/review/answer_ref")
            if isinstance(review.get("answer_ref"), Mapping)
            else None
        ),
        "outcome": (
            copy.deepcopy(review.get("outcomes", [])[-1])
            if isinstance(review.get("outcomes"), list) and review.get("outcomes")
            else None
        ),
        "prior_review_ref": (
            validate_ref_shape(dict(review["prior_review_ref"]), pointer="/review/prior_review_ref")
            if review.get("prior_review_ref") is not None
            else None
        ),
        "status": review.get("status"),
        "conclusion_claims": conclusion_claims,
        "remaining": remaining,
        "issues": [dict(item) for item in issues],
    }


def _package_review_closure(
    store: CaseStore,
    state: Mapping[str, Any],
    review_id: str,
    files: dict[str, bytes],
    objects: list[dict[str, Any]],
    ref_map: dict[tuple[str, str, int, str], dict[str, Any]],
    *,
    snapshot_ref: Mapping[str, Any] | None = None,
    verified_raw: dict[tuple[str, str, int, str], bytes] | None = None,
    lineage_seen: set[tuple[str, str, int, str]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    case_id = state["case_id"]
    review, review_ref, review_raw = _load_review_snapshot(store, case_id, review_id, snapshot_ref)
    closure_refs: list[dict[str, Any]] = []
    verified_raw = verified_raw if verified_raw is not None else {}
    lineage_seen = lineage_seen if lineage_seen is not None else set()
    review_identity = _ref_identity(review_ref)
    if review_identity in lineage_seen:
        raise ContractError("artifact_changed", "pinned Review lineage contains a repeated snapshot")
    lineage_seen.add(review_identity)
    verified_raw.setdefault(review_identity, review_raw)

    def read_ref_once(ref: Mapping[str, Any], *, case_evidence: bool = False) -> bytes:
        key = _ref_identity(ref)
        cached = verified_raw.get(key)
        if cached is not None:
            return cached
        raw = (
            _validate_case_evidence_ref(store, state, ref)
            if case_evidence
            else store.read_ref(ref)
        )
        verified_raw[key] = raw
        return raw

    def add_ref(ref_value: Mapping[str, Any], package_path: str, raw: bytes | None = None) -> tuple[dict[str, Any], bytes]:
        ref = validate_ref_shape(dict(ref_value), pointer="/review/refs")
        if raw is None:
            raw = read_ref_once(ref)
        else:
            verified_raw.setdefault(_ref_identity(ref), raw)
        content = _register_portable_ref(
            store, ref, package_path, files, objects, ref_map, raw_bytes=raw
        )
        closure_refs.append(ref)
        return ref, content

    review_ref, _ = add_ref(review_ref, f"evidence/reviews/{review_id}.json", review_raw)
    provenance_ref = review.get("source_provenance_ref")
    if provenance_ref is not None:
        provenance_ref = validate_ref_shape(dict(provenance_ref), pointer="/review/source_provenance_ref")
        if provenance_ref["path"] != f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/source-provenance.json":
            raise ContractError("artifact_changed", "Review source provenance Ref is outside its fixed path")
        provenance_raw = read_ref_once(provenance_ref)
        add_ref(provenance_ref, f"evidence/reviews/{review_id}/source-provenance.json", provenance_raw)
        provenance = strict_json_loads(provenance_raw, max_bytes=len(provenance_raw))
        for child_ref in _review_provenance_child_refs(case_id, provenance):
            raw = read_ref_once(child_ref)
            package_path = f"evidence/attachments/{child_ref['sha256']}{_suffix(child_ref['path'])}"
            add_ref(child_ref, package_path, raw)
    prior_review: dict[str, Any] | None = None
    prior_review_ref = review.get("prior_review_ref")
    if prior_review_ref is not None:
        if not isinstance(prior_review_ref, Mapping):
            raise ContractError("output_invalid", "pinned prior Review Ref is invalid")
        prior_review_ref = validate_ref_shape(dict(prior_review_ref), pointer="/review/prior_review_ref")
        prior_review_id = _review_id_from_snapshot_ref(case_id, prior_review_ref)
        prior_review, prior_review_ref, prior_review_raw = _load_review_snapshot(
            store, case_id, prior_review_id, prior_review_ref
        )
        verified_raw.setdefault(_ref_identity(prior_review_ref), prior_review_raw)
        if prior_review.get("note") != review.get("note"):
            raise ContractError("artifact_changed", "Review note does not match its pinned prior snapshot")

    note = review.get("note")
    if note is not None:
        if not isinstance(note, Mapping) or not isinstance(note.get("body"), Mapping):
            raise ContractError("output_invalid", "selected Review note reference is invalid")
        note_ref = validate_ref_shape(dict(note["body"]), pointer="/review/note/body")
        note_owner_id = review_id
        note_paths = {f".reaslab/optagent/cases/{case_id}/reviews/{review_id}/note.txt"}
        if prior_review is not None and isinstance(prior_review.get("note"), Mapping):
            prior_note_ref = validate_ref_shape(prior_review["note"].get("body"), pointer="/prior_review/note/body")
            note_paths.add(prior_note_ref["path"])
            if note_ref == prior_note_ref:
                note_owner_id = prior_review_id
        if note_ref["path"] not in note_paths or note_ref["media_type"] != "text/plain; charset=utf-8":
            raise ContractError("artifact_changed", "Review note Ref is outside its fixed path")
        add_ref(note_ref, f"evidence/reviews/{note_owner_id}/note.txt", read_ref_once(note_ref))

    answer_ref_value = review.get("answer_ref")
    if answer_ref_value is not None:
        answer_ref = validate_ref_shape(dict(answer_ref_value), pointer="/review/answer_ref")
        answer_raw = read_ref_once(answer_ref, case_evidence=True)
        add_ref(answer_ref, f"evidence/approved-answers/{answer_ref['sha256']}{_suffix(answer_ref['path'])}", answer_raw)

    conclusion = review.get("conclusion")
    if conclusion is not None:
        if not isinstance(conclusion, Mapping) or not isinstance(conclusion.get("claims", []), list):
            raise ContractError("output_invalid", "selected Review conclusion is invalid")
        for claim in conclusion.get("claims", []):
            if not isinstance(claim, Mapping) or not isinstance(claim.get("evidence_refs", []), list):
                raise ContractError("output_invalid", "selected Review claim references are invalid")
            for ref_value in claim.get("evidence_refs", []):
                ref = validate_ref_shape(dict(ref_value), pointer="/review/conclusion/claims/evidence_refs")
                raw = read_ref_once(ref, case_evidence=True)
                add_ref(ref, f"evidence/attachments/{ref['sha256']}{_suffix(ref['path'])}", raw)

    raw_issue_refs = review.get("issues", [])
    if not isinstance(raw_issue_refs, list):
        raise ContractError("output_invalid", "selected Review Issue references are invalid")
    issue_projections: list[dict[str, Any]] = []
    for raw_issue_ref in raw_issue_refs:
        issue, issue_raw, issue_ref = _load_review_issue(store, case_id, review_id, raw_issue_ref)
        issue_ref, _ = add_ref(
            issue_ref,
            f"evidence/reviews/{review_id}/issues/{issue['issue_id']}.json",
            issue_raw,
        )
        for raw_evidence_ref in issue.get("evidence_refs", []):
            ref = validate_ref_shape(dict(raw_evidence_ref), pointer="/review/issues/evidence_refs")
            raw = read_ref_once(ref, case_evidence=True)
            add_ref(ref, f"evidence/attachments/{ref['sha256']}{_suffix(ref['path'])}", raw)
        dispositions = issue.get("owner_dispositions", [])
        if not isinstance(dispositions, list):
            raise ContractError("output_invalid", "Issue owner dispositions are invalid")
        for index, disposition in enumerate(dispositions):
            if not isinstance(disposition, Mapping):
                raise ContractError("output_invalid", "Issue owner disposition is invalid", pointer=f"/review/issues/owner_dispositions/{index}")
        issue_projections.append({"issue_ref": issue_ref, **issue})

    projection = _review_projection(review, review_ref, issue_projections)
    if prior_review_ref is not None:
        _, ancestor_refs = _package_review_closure(
            store,
            state,
            prior_review_id,
            files,
            objects,
            ref_map,
            snapshot_ref=prior_review_ref,
            verified_raw=verified_raw,
            lineage_seen=lineage_seen,
        )
        closure_refs.extend(ancestor_refs)
    return projection, closure_refs


def _review_id_from_snapshot_ref(case_id: str, raw_ref: Mapping[str, Any]) -> str:
    ref = validate_ref_shape(dict(raw_ref), pointer="/review/prior_review_ref")
    prefix = f".reaslab/optagent/cases/{case_id}/reviews/"
    if not ref["path"].startswith(prefix) or ref["media_type"] != "application/json":
        raise ContractError("artifact_changed", "pinned prior Review Ref is outside this Case")
    tail = ref["path"][len(prefix) :]
    parts = tail.split("/")
    if len(parts) != 3 or parts[1] != "versions" or parts[2] != f"{ref['sha256']}.json":
        raise ContractError("artifact_changed", "pinned prior Review Ref has an invalid immutable path")
    return require_id(parts[0], pointer="/review/prior_review_ref")


def _review_provenance_child_refs(case_id: str, provenance: Any) -> list[dict[str, Any]]:
    """Return only the explicit Ref-bearing children in the fixed provenance projection."""
    if (
        not isinstance(provenance, Mapping)
        or set(provenance) != {"schema_version", "case_id", "source_binding", "provenance_source_ids", "sources", "route"}
        or provenance.get("schema_version") != 1
        or provenance.get("case_id") != case_id
        or not isinstance(provenance.get("provenance_source_ids"), list)
        or not isinstance(provenance.get("sources"), list)
        or not isinstance(provenance.get("route"), Mapping)
    ):
        raise ContractError("output_invalid", "Review source provenance projection has invalid fields")
    source_binding = provenance["source_binding"]
    if (
        not isinstance(source_binding, Mapping)
        or set(source_binding) != {"source_revision", "math_source_ids"}
        or type(source_binding.get("source_revision")) is not int
        or source_binding["source_revision"] < 0
        or not isinstance(source_binding.get("math_source_ids"), list)
    ):
        raise ContractError("output_invalid", "Review source provenance binding is invalid")
    for index, source_id in enumerate(source_binding["math_source_ids"]):
        require_id(source_id, pointer=f"/source_provenance/source_binding/math_source_ids/{index}")

    refs: list[dict[str, Any]] = []
    seen_refs: set[tuple[str, str, int, str]] = set()
    source_ids: list[str] = []

    def append_ref(value: Any, pointer: str) -> None:
        ref = validate_ref_shape(value, pointer=pointer)
        identity = _ref_identity(ref)
        if identity not in seen_refs:
            seen_refs.add(identity)
            refs.append(ref)

    def source_raw_ref(source: Any, pointer: str, *, active_source: bool = False) -> None:
        if not isinstance(source, Mapping) or not isinstance(source.get("source_id"), str):
            raise ContractError("output_invalid", "Review provenance Source entry is invalid", pointer=pointer)
        source_id = require_id(source["source_id"], pointer=f"{pointer}/source_id")
        raw = source.get("raw")
        if not isinstance(raw, Mapping) or set(raw) != {"sha256", "byte_len", "ref"}:
            raise ContractError("output_invalid", "Review provenance raw Source reference is invalid", pointer=f"{pointer}/raw")
        ref = validate_ref_shape(raw["ref"], pointer=f"{pointer}/raw/ref")
        if raw["sha256"] != ref["sha256"] or raw["byte_len"] != ref["byte_len"]:
            raise ContractError("artifact_changed", "Review provenance raw Source identity differs from its Ref", pointer=f"{pointer}/raw")
        prefix = ".reaslab/optagent/source-bytes/"
        if ref["path"] != prefix + ref["sha256"] or ref["media_type"] != "application/octet-stream":
            raise ContractError("artifact_changed", "Review provenance Source bytes are outside the shared immutable pool", pointer=f"{pointer}/raw/ref")
        if active_source:
            source_ids.append(source_id)
        append_ref(ref, f"{pointer}/raw/ref")
        conversation_context = source.get("conversation_context")
        if conversation_context is not None:
            if (
                not isinstance(conversation_context, Mapping)
                or set(conversation_context) != {"ref", "question_ref", "assistant_family_omitted", "scope"}
                or type(conversation_context.get("assistant_family_omitted")) is not bool
                or not isinstance(conversation_context.get("scope"), str)
            ):
                raise ContractError(
                    "output_invalid",
                    "Review provenance conversation context is invalid",
                    pointer=f"{pointer}/conversation_context",
                )
            append_ref(conversation_context["ref"], f"{pointer}/conversation_context/ref")
            append_ref(conversation_context["question_ref"], f"{pointer}/conversation_context/question_ref")

    for index, source in enumerate(provenance["sources"]):
        source_raw_ref(source, f"/source_provenance/sources/{index}", active_source=True)
    if source_ids != provenance["provenance_source_ids"] or len(set(source_ids)) != len(source_ids):
        raise ContractError("artifact_changed", "Review provenance Source list differs from its declared IDs")
    for index, source_id in enumerate(provenance["provenance_source_ids"]):
        require_id(source_id, pointer=f"/source_provenance/provenance_source_ids/{index}")

    route = provenance["route"]
    if (
        set(route) != {"session_id", "unknown", "reason", "member", "guards", "handoffs"}
        or type(route.get("unknown")) is not bool
        or not isinstance(route.get("guards"), list)
        or not isinstance(route.get("handoffs"), list)
    ):
        raise ContractError("output_invalid", "Review provenance route projection is invalid")
    for index, guard in enumerate(route["guards"]):
        pointer = f"/source_provenance/route/guards/{index}"
        if not isinstance(guard, Mapping) or not {"gap_ref", "scope", "unresolved"}.issubset(guard):
            raise ContractError("output_invalid", "Review provenance route guard is invalid", pointer=pointer)
        append_ref(guard["gap_ref"], f"{pointer}/gap_ref")
        adoption = guard.get("adoption")
        if adoption is not None:
            if not isinstance(adoption, Mapping) or not isinstance(adoption.get("sources"), list):
                raise ContractError("output_invalid", "Review provenance adoption is invalid", pointer=f"{pointer}/adoption")
            for source_index, source in enumerate(adoption["sources"]):
                source_raw_ref(source, f"{pointer}/adoption/sources/{source_index}")
    for index, handoff in enumerate(route["handoffs"]):
        pointer = f"/source_provenance/route/handoffs/{index}"
        if not isinstance(handoff, Mapping) or not isinstance(handoff.get("guard_refs"), list):
            raise ContractError("output_invalid", "Review provenance handoff is invalid", pointer=pointer)
        for ref_index, ref in enumerate(handoff["guard_refs"]):
            append_ref(ref, f"{pointer}/guard_refs/{ref_index}")
    return refs


def _load_proof(store: CaseStore, case_id: str, proof_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    path = f".reaslab/optagent/cases/{case_id}/proofs/{proof_id}.json"
    proof = store.read_record(path)
    if proof.get("proof_id") != proof_id:
        raise ContractError("artifact_changed", "Proof identity does not match its fixed path")
    return proof, store.ref_for_path(path)


def _load_run(store: CaseStore, case_id: str, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    path = f".reaslab/optagent/cases/{case_id}/runs/{run_id}/record.json"
    run, ref = store.read_family_record(case_id, "runs", run_id, legacy_path=path)
    if run.get("run_id") != run_id:
        raise ContractError("artifact_changed", "run identity does not match its fixed path")
    return run, ref


def _load_delivery(store: CaseStore, case_id: str, delivery_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    path = f".reaslab/optagent/cases/{case_id}/deliveries/{delivery_id}/delivery.json"
    raw = store._read_stable_path(store.resolve_path(path, must_exist=True))
    try:
        delivery = strict_json_loads(raw, max_bytes=len(raw))
    except (ValueError, UnicodeError) as exc:
        raise ContractError("output_invalid", "Delivery selection is invalid JSON") from exc
    if not isinstance(delivery, dict):
        raise ContractError("output_invalid", "Delivery selection is invalid")
    if delivery.get("case_id", case_id) != case_id or delivery.get("delivery_id") != delivery_id:
        raise ContractError("not_found", "delivery does not belong to this case")
    return delivery, ref_from_bytes(path, raw, "application/json")


def _load_presentation(
    store: CaseStore,
    state: Mapping[str, Any],
    delivery_id: str,
    selection_ref: Mapping[str, Any],
    *,
    delivery: Mapping[str, Any],
) -> dict[str, Any]:
    records = state.get("current_records") or {"presentation": None}
    current_ref = records.get("presentation")
    if current_ref is None or state.get("selected_delivery_id") != delivery_id:
        raise ContractError("stale_revision", "selected delivery has no current presentation")
    current_ref = validate_ref_shape(dict(current_ref), pointer="/current_records/presentation")
    selection = validate_ref_shape(dict(selection_ref))
    delivery_reports = delivery.get("reports", [])
    if not isinstance(delivery_reports, list):
        raise ContractError("output_invalid", "selected Delivery report references are invalid")
    base = {
        "schema_version": 1,
        "kind": "delivery_presentation",
        "delivery_id": delivery_id,
        "selection_ref": selection,
        "summary_ref": None,
        "reports": [
            validate_ref_shape(dict(ref), pointer="/delivery/reports")
            for ref in delivery_reports
        ],
        "packages": [],
    }
    if current_ref == selection:
        return base
    raw = store.read_ref(current_ref)
    try:
        value = strict_json_loads(raw, max_bytes=len(raw))
    except (ValueError, UnicodeError) as exc:
        raise ContractError("output_invalid", "current Delivery presentation is invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or canonical_json_bytes(value) != raw
        or set(value) != set(base)
        or value.get("schema_version") != 1
        or value.get("kind") != "delivery_presentation"
        or value.get("delivery_id") != delivery_id
        or value.get("selection_ref") != selection
        or not isinstance(value.get("reports"), list)
        or not isinstance(value.get("packages"), list)
        or (value.get("summary_ref") is not None and not isinstance(value.get("summary_ref"), dict))
    ):
        raise ContractError("artifact_changed", "current presentation does not bind the selected Delivery")
    for ref in value["reports"]:
        validate_ref_shape(ref, pointer="/presentation/reports")
    required_report_ids = _refs_by_identity(base["reports"], pointer="/delivery/reports")
    if not required_report_ids.issubset(_refs_by_identity(value["reports"], pointer="/presentation/reports")):
        raise ContractError("artifact_changed", "presentation omits an approved answer Ref")
    for item in value["packages"]:
        if not isinstance(item, dict) or set(item) != {"include_raw_sources", "package_ref", "manifest_ref"}:
            raise ContractError("output_invalid", "presentation package entry is invalid")
        if type(item["include_raw_sources"]) is not bool:
            raise ContractError("output_invalid", "presentation package option is invalid")
        validate_ref_shape(item["package_ref"], pointer="/presentation/packages/package_ref")
        validate_ref_shape(item["manifest_ref"], pointer="/presentation/packages/manifest_ref")
    if value["summary_ref"] is not None:
        validate_ref_shape(value["summary_ref"], pointer="/presentation/summary_ref")
    return value


def _write_presentation(store: CaseStore, case_id: str, value: Mapping[str, Any]) -> dict[str, Any]:
    return store.write_content_addressed(
        f".reaslab/optagent/cases/{case_id}/presentations",
        canonical_json_bytes(dict(value)),
        "application/json",
    )


def _publish_presentation(
    store: CaseStore,
    *,
    case_id: str,
    state: Mapping[str, Any],
    delivery: Mapping[str, Any],
    selection_ref: Mapping[str, Any],
    expected_presentation: Mapping[str, Any] | None,
    next_presentation: Mapping[str, Any],
    stamp: Mapping[str, str],
    validate_current: Callable[[Mapping[str, Any]], None] | None,
    session_commit: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    if delivery.get("delivery_id") != state.get("selected_delivery_id"):
        raise ContractError("stale_revision", "selected Delivery changed before presentation publication")
    expected_ref = validate_ref_shape(dict(expected_presentation)) if expected_presentation is not None else None
    selection = validate_ref_shape(dict(selection_ref))
    if expected_ref is None or (
        expected_ref != selection
        and not expected_ref["path"].startswith(f".reaslab/optagent/cases/{case_id}/presentations/")
    ):
        raise ContractError("stale_revision", "current presentation is not based on the selected Delivery")
    binding = {
        "source_revision": delivery["source_revision"],
        "candidate_revision": state["candidate_revision"],
        "candidate_id": delivery["candidate_id"],
        "promotion_generation": delivery["promotion_generation"],
    }
    def publish(route_validate: Callable[[Mapping[str, Any]], None] | None) -> dict[str, Any]:
        def validate(current: Mapping[str, Any]) -> None:
            if validate_current is not None:
                validate_current(current)
            if route_validate is not None:
                route_validate(current)

        return store.cas_presentation(
            case_id,
            delivery["delivery_id"],
            expected_presentation,
            next_presentation,
            expected_state_revision=state["state_revision"],
            owner_session_id=stamp["session_id"],
            expected_binding=binding,
            expected_authorities=delivery["authority_records"],
            validate_current=validate,
        )

    return session_commit(publish) if session_commit is not None else publish(None)


def _local_committed_error(exc: ContractError, kind: str, **artifact: Any) -> ContractError:
    details = dict(exc.details)
    details["local_artifact"] = {"kind": kind, **artifact}
    return ContractError(exc.code, exc.message, pointer=exc.pointer, details=details, effect="local_committed")


_REF_IDENTITY_FIELDS = ("path", "sha256", "byte_len", "media_type")


def _ref_identity(value: Mapping[str, Any]) -> tuple[str, str, int, str]:
    ref = validate_ref_shape(dict(value), pointer="/evidence_refs")
    return tuple(ref[field] for field in _REF_IDENTITY_FIELDS)  # type: ignore[return-value]


def _refs_by_identity(values: Sequence[Mapping[str, Any]], *, pointer: str) -> set[tuple[str, str, int, str]]:
    result: set[tuple[str, str, int, str]] = set()
    for index, value in enumerate(values):
        result.add(_ref_identity(validate_ref_shape(dict(value), pointer=f"{pointer}/{index}")))
    return result


def validate_review_claims(
    store: CaseStore,
    state: Mapping[str, Any],
    review: Mapping[str, Any],
    claims: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Validate ready-outcome Ref identities and require real anchors for strong claims."""
    binding = review.get("binding")
    if not isinstance(binding, Mapping) or any(
        binding.get(key) != state.get(state_key)
        for key, state_key in (
            ("source_revision", "source_revision"),
            ("candidate_revision", "candidate_revision"),
            ("candidate_id", "candidate_id"),
        )
    ):
        raise ContractError("review_binding_mismatch", "review is outside the current source or candidate binding")
    cache: dict[str, dict[str, Any]] = {}
    source_view = source_projection(store, state)
    cache["__source_projection__"] = source_view
    if review.get("schema_version") != 4 or binding.get("math_source_ids") != source_view["math_source_ids"]:
        raise ContractError("review_binding_mismatch", "new Review must bind the current mathematical Source projection")

    candidate_id = binding.get("candidate_id")
    canonical_claims: list[dict[str, Any]] = []
    needs_candidate_anchor = False
    for claim in claims:
        _validate_claim(claim)
        canonical = dict(claim)
        canonical["evidence_refs"] = [dict(ref) for ref in claim["evidence_refs"]]
        canonical_claims.append(canonical)
        if (
            candidate_id is not None
            and claim.get("kind") == "formulation_fidelity"
            and claim.get("support") in {"supported", "conditional"}
            and not _claim_has_proof_record_ref(state["case_id"], claim["evidence_refs"])
            and not any(
                isinstance(ref.get("path"), str)
                and _candidate_id_from_path(state["case_id"], ref["path"]) == candidate_id
                for ref in claim["evidence_refs"]
            )
        ):
            needs_candidate_anchor = True

    candidate_ref: dict[str, Any] | None = None
    if needs_candidate_anchor and candidate_id is not None:
        candidate_path = f".reaslab/optagent/cases/{state['case_id']}/candidates/{candidate_id}.json"
        binding_evidence_refs = review.get("binding_evidence_refs")
        pinned_candidate_ref = (
            binding_evidence_refs.get("candidate")
            if isinstance(binding_evidence_refs, Mapping)
            else None
        )
        candidate_ref = (
            validate_ref_shape(dict(pinned_candidate_ref), pointer="/binding_evidence_refs/candidate")
            if isinstance(pinned_candidate_ref, Mapping)
            else store.ref_for_path(candidate_path, media_type="application/json")
        )
        if candidate_ref["path"] != candidate_path or candidate_ref["media_type"] != "application/json":
            raise ContractError("artifact_changed", "Review Candidate pin does not match its binding")
        store.read_ref(candidate_ref)
        candidate = _read_exact_record_ref(store, state, candidate_ref, f"candidates/{candidate_id}.json")
        _validate_candidate_record(state, candidate, candidate_id, source_view["math_source_ids"])

    if candidate_ref is not None:
        for claim in canonical_claims:
            if (
                claim.get("kind") == "formulation_fidelity"
                and claim.get("support") in {"supported", "conditional"}
                and not _claim_has_proof_record_ref(state["case_id"], claim["evidence_refs"])
                and not any(
                    isinstance(ref.get("path"), str)
                    and _candidate_id_from_path(state["case_id"], ref["path"]) == candidate_id
                    for ref in claim["evidence_refs"]
                )
            ):
                claim["evidence_refs"].insert(0, dict(candidate_ref))

    for claim_index, claim in enumerate(canonical_claims):
        refs = claim["evidence_refs"]
        for ref in refs:
            _validate_case_evidence_ref(store, state, ref)
        if claim["support"] in {"supported", "conditional"}:
            anchors, _, _ = _claim_anchors(
                store,
                state,
                claim,
                cache,
                require_support=True,
                bound_run_ids=review.get("binding", {}).get("run_ids", []),
                claim_index=claim_index,
            )
            if not anchors or not _refs_by_identity(refs, pointer="/claims/evidence_refs").issuperset(anchors):
                raise ContractError(
                    "evidence_insufficient",
                    "strong review claim is missing a required claim evidence anchor",
                    pointer=f"/claims/{claim_index}/evidence_refs",
                    details={"claim_index": claim_index, "missing_claim_anchor": "required_claim_anchor"},
                )
    return canonical_claims


def _claim_anchors(
    store: CaseStore,
    state: Mapping[str, Any],
    claim: Mapping[str, Any],
    cache: dict[str, dict[str, Any]],
    *,
    require_support: bool,
    bound_run_ids: Sequence[str] = (),
    claim_index: int | None = None,
) -> tuple[set[tuple[str, str, int, str]], set[str], set[str]]:
    """Identify primary anchors from the complete ReviewedClaim Ref set."""
    case_id = state["case_id"]
    refs = [validate_ref_shape(dict(ref), pointer="/claim/evidence_refs") for ref in claim.get("evidence_refs", [])]
    anchors: set[tuple[str, str, int, str]] = set()
    anchor_proofs: set[str] = set()
    anchor_runs: set[str] = set()
    current_proofs: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    current_results: list[tuple[dict[str, Any], dict[str, Any], str, str]] = []
    result_candidates: list[tuple[dict[str, Any], str, tuple[str, str, int, str]]] = []
    candidate_records: list[tuple[dict[str, Any], dict[str, Any]]] = []
    raw_sources: dict[tuple[str, str, int, str], str] = {}
    source_view = cache.get("__source_projection__")
    if source_view is None:
        source_view = source_projection(store, state)
        cache["__source_projection__"] = source_view
    for source_id in source_view["math_source_ids"]:
        source = source_view["records"][source_id]
        source_ref = validate_ref_shape(source["bytes"], pointer="/source/bytes")
        store.read_ref(source_ref)
        raw_sources[_ref_identity(source_ref)] = source_id

    prefix = f".reaslab/optagent/cases/{case_id}/"
    for ref in refs:
        identity = _ref_identity(ref)
        store.read_ref(ref)
        path = ref["path"]
        proof_prefix = prefix + "proofs/"
        proof_suffix = path[len(proof_prefix) :] if path.startswith(proof_prefix) else ""
        if path.startswith(proof_prefix) and path.endswith(".json") and "/" not in proof_suffix:
            proof_id = proof_suffix[:-5]
            if proof_id not in state["proof_ids"] or "/" in proof_id:
                raise ContractError("evidence_insufficient", "claim Proof is not registered on this case")
            proof = _read_exact_record_ref(store, state, ref, f"proofs/{proof_id}.json")
            _validate_proof_binding(store, state, proof, ref, require_current=True)
            current_proofs.append((proof, ref, proof_id))
            anchors.add(identity)
            anchor_proofs.add(proof_id)
            continue
        result_path = _result_path_parts(case_id, path)
        if result_path is not None:
            run_id, _ = result_path
            if run_id not in state["run_ids"]:
                raise ContractError("evidence_insufficient", "claim Result belongs to an unregistered run")
            result_candidates.append((ref, run_id, identity))
            continue
        candidate_id = _candidate_id_from_path(case_id, path)
        if candidate_id is not None:
            if candidate_id != state.get("candidate_id"):
                raise ContractError("evidence_insufficient", "claim Candidate is not the current candidate")
            candidate = _read_exact_record_ref(store, state, ref, f"candidates/{candidate_id}.json")
            _validate_candidate_record(state, candidate, candidate_id, source_view["math_source_ids"])
            candidate_records.append((candidate, ref))
            continue
        if identity in raw_sources:
            continue

    declared_historical_results: set[tuple[str, str, int, str]] = set()
    for proof, proof_ref, proof_id in current_proofs:
        declared_historical_results.update(_validate_proof_dependencies(store, state, proof, cache))

    for ref, run_id, identity in result_candidates:
        verified = _verified_run(store, state, run_id, cache, require_current=False)
        result = verified["results"].get(identity)
        if result is None:
            raise ContractError("evidence_insufficient", "claim Result is not a matching Run.results and Seal member")
        if verified["current"]:
            current_results.append((result, ref, run_id, verified["run_ref"]["path"]))
            anchors.add(identity)
            anchor_runs.add(run_id)
        elif identity not in declared_historical_results:
            raise ContractError("evidence_insufficient", "historical Result is not an exact declared Proof dependency")

    if claim["kind"] == "formulation_fidelity" and candidate_records:
        if len(candidate_records) != 1:
            raise ContractError("evidence_insufficient", "formulation claim has ambiguous Candidate anchors")
        candidate, candidate_ref = candidate_records[0]
        active_raws = set(raw_sources).intersection(_refs_by_identity(refs, pointer="/claim/evidence_refs"))
        if not active_raws:
            raise ContractError("evidence_insufficient", "formulation claim needs current active Source bytes")
        anchors.add(_ref_identity(candidate_ref))
        anchors.update(active_raws)

    # A_D is derived from the full reviewed set, before selected IDs are read.
    diagnostic_run_ids: set[str] = set()
    if claim["kind"] in {"diagnostic", "tradeoff"}:
        for ref in refs:
            run_id = _run_id_from_child_path(case_id, ref["path"])
            if run_id is not None:
                if run_id not in state["run_ids"]:
                    continue
                if not _has_observation_ref(store, state, run_id, ref):
                    continue
            else:
                run_id = next(
                    (
                        candidate
                        for candidate in bound_run_ids
                        if candidate in state["run_ids"] and _has_observation_ref(store, state, candidate, ref)
                    ),
                    None,
                )
            if run_id is None:
                continue
            anchors.add(_ref_identity(ref))
            diagnostic_run_ids.add(run_id)
        for run_id in diagnostic_run_ids:
            verified = _diagnostic_run_context(store, state, run_id)
            # Keep every actual RunRecord/transport observation included in this
            # one ReviewedClaim; none can disappear by filtering selected IDs.
            for ref in _run_observation_refs(verified["run"], verified["run_ref"]):
                if _ref_identity(ref) in _refs_by_identity(refs, pointer="/claim/evidence_refs"):
                    anchors.add(_ref_identity(ref))
                    anchor_runs.add(run_id)

    proof_support = any(claim["kind"] in proof.get("claim_kinds", []) for proof, _, _ in current_proofs)
    result_support = bool(current_results) and all(
        _result_supports_claim(
            store,
            state,
            result,
            ref,
            claim["kind"],
            cache,
            run_id,
            claim_support=claim["support"],
            claim_conditions=claim["conditions"],
        )
        for result, ref, run_id, _ in current_results
    )
    if require_support and current_results and claim["kind"] in {
        "feasibility", "optimality", "local_optimality", "bound"
    } and not result_support:
        # A proof or another Result cannot make an unsupported Result in the
        # same reviewed numerical scope look supported. The complete Review
        # set is authoritative here; selection must not filter away a bad
        # primary Result before its predicate is checked.
        details: dict[str, Any] = {}
        pointer = "/claim/evidence_refs"
        if claim_index is not None:
            details["claim_index"] = claim_index
            pointer = f"/claims/{claim_index}/evidence_refs"
        missing_optimality_conditions = claim["kind"] == "optimality" and (
            claim.get("support") != "conditional" or not claim.get("conditions")
        )
        message = "each reviewed numerical Result must independently support its claim kind"
        if missing_optimality_conditions:
            details["required_support"] = "conditional_with_nonempty_conditions"
            if claim_index is not None:
                pointer = f"/claims/{claim_index}/support"
            if proof_support:
                message = "an optimality claim with numerical evidence needs conditional support and nonempty conditions"
        else:
            details["required_support"] = f"checked_result_supports_{claim['kind']}"
            if claim["kind"] == "optimality" and proof_support:
                message = "each numerical Result in an optimality claim must independently support optimality; a matching Proof cannot cover an unsupported Result"
        raise ContractError(
            "evidence_insufficient",
            message,
            pointer=pointer,
            details=details,
        )
    formulation_support = claim["kind"] == "formulation_fidelity" and bool(candidate_records) and bool(
        set(raw_sources).intersection(_refs_by_identity(refs, pointer="/claim/evidence_refs"))
    )
    diagnostic_support = claim["kind"] in {"diagnostic", "tradeoff"} and bool(diagnostic_run_ids)
    if require_support and not (proof_support or result_support or formulation_support or diagnostic_support):
        expected_anchor_types = _expected_claim_anchor_types(claim["kind"])
        details: dict[str, Any] = {}
        if expected_anchor_types:
            details["expected_anchor_types"] = expected_anchor_types
        pointer = "/claim/evidence_refs"
        if claim_index is not None:
            details["claim_index"] = claim_index
            pointer = f"/claims/{claim_index}/evidence_refs"
        raise ContractError(
            "evidence_insufficient",
            "claim evidence does not contain a primary anchor allowed for this claim kind",
            pointer=pointer,
            details=details or None,
        )
    return anchors, anchor_proofs, anchor_runs | diagnostic_run_ids


def _expected_claim_anchor_types(kind: str) -> list[str]:
    if kind == "formulation_fidelity":
        return ["current_candidate_and_active_source", "matching_formal_proof"]
    if kind == "feasibility":
        return ["checked_result_with_passed_checks", "matching_formal_proof"]
    if kind == "optimality":
        return ["checked_result_with_closed_global_bound", "matching_formal_proof"]
    if kind == "local_optimality":
        return ["checked_result_with_local_optimality_status", "matching_formal_proof"]
    if kind == "bound":
        return ["checked_result_with_global_bound", "matching_formal_proof"]
    if kind in {"infeasibility", "unboundedness"}:
        return ["matching_formal_proof"]
    if kind in {"diagnostic", "tradeoff"}:
        return ["linked_run_observation", "matching_formal_proof"]
    return []


def _read_exact_record_ref(
    store: CaseStore,
    state: Mapping[str, Any],
    ref: Mapping[str, Any],
    suffix: str,
) -> dict[str, Any]:
    expected = f".reaslab/optagent/cases/{state['case_id']}/{suffix}"
    if ref["path"] != expected or ref["media_type"] != "application/json":
        raise ContractError("evidence_insufficient", "primary record Ref is not at its canonical case path")
    raw = store.read_ref(ref)
    value = strict_json_loads(raw)
    if not isinstance(value, dict):
        raise ContractError("artifact_changed", "primary record bytes are not a JSON object")
    return value


def _validate_proof_binding(
    store: CaseStore,
    state: Mapping[str, Any],
    proof: Mapping[str, Any],
    proof_ref: Mapping[str, Any],
    *,
    require_current: bool,
) -> None:
    proof_id = proof.get("proof_id")
    if not isinstance(proof_id, str) or proof_ref["path"] != f".reaslab/optagent/cases/{state['case_id']}/proofs/{proof_id}.json":
        raise ContractError("artifact_changed", "Proof path identity is invalid")
    if proof.get("status") != "proposed" or proof_id not in state["proof_ids"]:
        raise ContractError("evidence_insufficient", "Proof is not a registered proposed record")
    if require_current and (proof.get("source_revision") != state["source_revision"] or proof.get("candidate_id") != state["candidate_id"]):
        raise ContractError("evidence_insufficient", "Proof is outside the current mathematical binding")
    body = validate_ref_shape(proof.get("body"), pointer="/proof/body")
    if not body["path"].startswith(f".reaslab/optagent/cases/{state['case_id']}/proofs/{proof_id}/body"):
        raise ContractError("artifact_changed", "Proof body Ref is outside its fixed proof tree")
    store.read_ref(body)
    if not isinstance(proof.get("premises"), list) or not isinstance(proof.get("evidence_refs"), list):
        raise ContractError("artifact_changed", "Proof premises or evidence refs are invalid")
    for index, raw_ref in enumerate(proof["evidence_refs"]):
        ref = validate_ref_shape(raw_ref, pointer=f"/proof/evidence_refs/{index}")
        _validate_case_evidence_ref(store, state, ref)


def _validate_proof_dependencies(
    store: CaseStore,
    state: Mapping[str, Any],
    proof: Mapping[str, Any],
    cache: dict[str, dict[str, Any]],
) -> set[tuple[str, str, int, str]]:
    old_run_ids = proof.get("old_run_ids", [])
    if not isinstance(old_run_ids, list) or any(not isinstance(item, str) for item in old_run_ids):
        raise ContractError("artifact_changed", "Proof historical run IDs are invalid")
    declared: set[tuple[str, str, int, str]] = set()
    for ref in proof.get("evidence_refs", []):
        identity = _ref_identity(ref)
        result_path = _result_path_parts(state["case_id"], ref["path"])
        if result_path is None:
            continue
        run_id, _ = result_path
        if run_id not in old_run_ids or run_id not in state["run_ids"]:
            raise ContractError("evidence_insufficient", "historical Result lacks a registered Proof dependency")
        verified = _verified_run(store, state, run_id, cache, require_current=False)
        if identity not in verified["results"]:
            raise ContractError("evidence_insufficient", "historical Proof Result is not in its original Seal")
        declared.add(identity)
    return declared


def _validate_candidate_record(
    state: Mapping[str, Any],
    candidate: Mapping[str, Any],
    candidate_id: str,
    math_source_ids: Sequence[str],
) -> None:
    if (
        candidate.get("candidate_id") != candidate_id
        or candidate.get("source_revision") != state["source_revision"]
        or candidate.get("candidate_revision") != state["candidate_revision"]
        or candidate.get("source_ids") != list(math_source_ids)
        or candidate.get("binding_digest") is None
    ):
        raise ContractError("evidence_insufficient", "Candidate record is outside the current source binding")


def _candidate_id_from_path(case_id: str, path: str) -> str | None:
    prefix = f".reaslab/optagent/cases/{case_id}/candidates/"
    if not path.startswith(prefix) or not path.endswith(".json"):
        return None
    value = path[len(prefix) : -5]
    return value if value and "/" not in value else None


def _claim_has_proof_record_ref(case_id: str, refs: Sequence[Mapping[str, Any]]) -> bool:
    prefix = f".reaslab/optagent/cases/{case_id}/proofs/"
    for ref in refs:
        path = ref.get("path")
        if not isinstance(path, str) or not path.startswith(prefix) or not path.endswith(".json"):
            continue
        proof_id = path[len(prefix) : -5]
        if proof_id and "/" not in proof_id:
            return True
    return False


def _run_id_from_record_path(case_id: str, path: str) -> str | None:
    run_id = _run_id_from_child_path(case_id, path)
    return run_id if run_id is not None and path == f".reaslab/optagent/cases/{case_id}/runs/{run_id}/record.json" else None


def _run_id_from_child_path(case_id: str, path: str) -> str | None:
    prefix = f".reaslab/optagent/cases/{case_id}/runs/"
    if not isinstance(path, str) or not path.startswith(prefix) or "\\" in path:
        return None
    parts = path[len(prefix) :].split("/")
    if len(parts) < 2 or any(part in {"", ".", ".."} for part in parts):
        return None
    return parts[0]


def _result_path_parts(case_id: str, path: str) -> tuple[str, str] | None:
    prefix = f".reaslab/optagent/cases/{case_id}/runs/"
    if not path.startswith(prefix) or not path.endswith("/result.json"):
        return None
    rest = path[len(prefix) :]
    run_id, marker, relative = rest.partition("/output/")
    if not marker or not run_id or not relative or "/" not in relative:
        return None
    if any(part in {"", ".", ".."} for part in relative.split("/")):
        return None
    return run_id, relative


def _verified_run(
    store: CaseStore,
    state: Mapping[str, Any],
    run_id: str,
    cache: dict[str, dict[str, Any]],
    *,
    require_current: bool,
) -> dict[str, Any]:
    cached = cache.get(run_id)
    if cached is not None:
        if require_current and not cached["current"]:
            raise ContractError("evidence_insufficient", "run is outside the current mathematical binding")
        return cached
    if run_id not in state["run_ids"]:
        raise ContractError("evidence_insufficient", "run is not registered on this case")
    run, run_ref = _load_run(store, state["case_id"], run_id)
    if run.get("dispatch_state") not in {"unknown", "response_received"}:
        raise ContractError("evidence_insufficient", "run has no eligible actual dispatch observation")
    worker = run.get("worker")
    if not isinstance(worker, Mapping) or worker.get("status") == "invalid":
        raise ContractError("output_invalid", "run worker result was marked invalid")
    from .runner import _load_prepared, _read_worker_seal

    prepared, prepared_ref, manifest = _load_prepared(store, state["case_id"], run.get("prepared_id"))
    source_view = cache.get("__source_projection__")
    if source_view is None:
        source_view = source_projection(store, state)
        cache["__source_projection__"] = source_view
    current = (
        prepared.get("source_revision") == state["source_revision"]
        and prepared.get("candidate_revision") == state["candidate_revision"]
        and prepared.get("candidate_id") == state["candidate_id"]
        and prepared.get("source_ids") == source_view["math_source_ids"]
    )
    if require_current and not current:
        raise ContractError("evidence_insufficient", "run Prepared binding is not current")
    if run.get("prepared_id") != prepared.get("prepared_id"):
        raise ContractError("artifact_changed", "Run and Prepared identities do not match")
    if current and state.get("candidate_id") is not None:
        candidate = store.read_record(f".reaslab/optagent/cases/{state['case_id']}/candidates/{state['candidate_id']}.json")
        if (
            candidate.get("candidate_id") != state["candidate_id"]
            or candidate.get("candidate_revision") != state["candidate_revision"]
            or candidate.get("source_ids") != source_view["math_source_ids"]
            or candidate.get("binding_digest") != manifest.get("binding_digest")
        ):
            raise ContractError("candidate_dirty", "Prepared input is not bound to the current Candidate")
    seal, seal_ref, validated_result_refs = _read_worker_seal(store, state["case_id"], run, prepared, manifest)
    if seal is None or seal_ref is None:
        raise ContractError("evidence_insufficient", "run has no matching WorkerSeal")
    if worker.get("seal") != seal_ref:
        raise ContractError("artifact_changed", "Run does not link the actual WorkerSeal Ref")
    run_result_refs = run.get("results")
    if not isinstance(run_result_refs, list):
        raise ContractError("artifact_changed", "Run result refs are invalid")
    validated_by_identity = {_ref_identity(ref): ref for ref in validated_result_refs}
    if any(_ref_identity(ref) not in validated_by_identity or validated_by_identity[_ref_identity(ref)] != ref for ref in run_result_refs):
        raise ContractError("artifact_changed", "Run.results contains a Ref outside its matching WorkerSeal")
    parsed_results: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    for ref in validated_result_refs:
        raw = store.read_ref(ref)
        result = strict_json_loads(raw, max_bytes=len(raw))
        if not isinstance(result, dict):
            raise ContractError("output_invalid", "sealed Result is not a JSON object")
        parsed_results[_ref_identity(ref)] = result
    entry = {
        "run": run,
        "run_ref": run_ref,
        "prepared": prepared,
        "prepared_ref": prepared_ref,
        "manifest": manifest,
        "seal": seal,
        "seal_ref": seal_ref,
        "validated_result_refs": validated_result_refs,
        "results": parsed_results,
        "current": current,
    }
    cache[run_id] = entry
    return entry


def _read_verified_output_json(
    store: CaseStore,
    verified: Mapping[str, Any],
    ref_value: Mapping[str, Any],
) -> Any:
    """Parse JSON only from an exact output Ref admitted by this Run's Seal."""
    ref = validate_ref_shape(dict(ref_value))
    if ref["media_type"] != "application/json":
        raise ContractError("artifact_changed", "sealed business output is not JSON")
    seal = verified.get("seal")
    output_refs = seal.get("output_refs") if isinstance(seal, Mapping) else None
    if not isinstance(output_refs, list) or not any(
        isinstance(item, Mapping) and _ref_identity(item) == _ref_identity(ref)
        for item in output_refs
    ):
        raise ContractError("artifact_changed", "business output Ref is outside the matching WorkerSeal")
    raw = store.read_ref(ref)
    return strict_json_loads(raw, max_bytes=len(raw))


def _result_supports_claim(
    store: CaseStore,
    state: Mapping[str, Any],
    result: Mapping[str, Any],
    result_ref: Mapping[str, Any],
    kind: str,
    cache: dict[str, dict[str, Any]],
    run_id: str,
    *,
    claim_support: str,
    claim_conditions: Sequence[str],
) -> bool:
    verified = _verified_run(store, state, run_id, cache, require_current=True)
    if _ref_identity(result_ref) not in {_ref_identity(item) for item in verified["run"].get("results", [])}:
        return False
    objective = result.get("objective")
    plan = verified["manifest"].get("candidate", {}).get("objective_plan", {})
    sense = _result_sense(plan, result.get("layer"))
    if kind == "bound":
        return _valid_global_bound(result.get("bound"), sense)
    if not result.get("has_checked_candidate") or result.get("witness") is None:
        return False
    check_ref = validate_ref_shape(result.get("check"), pointer="/result/check")
    check = _read_verified_output_json(store, verified, check_ref)
    if not isinstance(check, dict) or check.get("status") != "pass":
        return False
    business_path = result_ref["path"].rsplit("/", 1)[0] + "/business-check.json"
    output_refs = { _ref_identity(item) for item in verified["seal"]["output_refs"] }
    business_ref = next(
        (validate_ref_shape(item) for item in verified["seal"]["output_refs"] if item["path"] == business_path),
        None,
    )
    if business_ref is None or _ref_identity(business_ref) not in output_refs:
        return False
    business = _read_verified_output_json(store, verified, business_ref)
    if verified["manifest"].get("candidate", {}).get("authoring") == "direct_python":
        from .results import business_check_is_fully_passed

        return kind == "feasibility" and check_ref == business_ref and business_check_is_fully_passed(business)
    if not isinstance(business, dict) or business.get("status") not in {"pass", "uncovered"}:
        return False
    if kind == "feasibility":
        return True
    if kind == "local_optimality":
        return result.get("termination") == "local_optimal"
    if kind == "optimality":
        if claim_support != "conditional" or not claim_conditions:
            return False
        numerical = result.get("numerical")
        if not isinstance(numerical, Mapping):
            return False
        bound = result.get("bound")
        observation_path = result_ref["path"].rsplit("/", 1)[0] + "/backend-observation.json"
        observation_ref = next(
            (
                validate_ref_shape(item)
                for item in verified["seal"].get("output_refs", [])
                if isinstance(item, Mapping) and item.get("path") == observation_path
            ),
            None,
        )
        if observation_ref is None:
            return False
        if bound is not None:
            if not _valid_global_bound(bound, sense):
                return False
            try:
                bound_evidence_ref = validate_ref_shape(bound["evidence"], pointer="/result/bound/evidence")
            except ContractError:
                return False
            if _ref_identity(bound_evidence_ref) != _ref_identity(observation_ref):
                return False
        observation = _read_verified_output_json(store, verified, observation_ref)
        if not isinstance(observation, Mapping) or not isinstance(observation.get("observation"), Mapping):
            return False
        raw = observation["observation"]
        if bound is not None and raw.get("best_objective_bound") != bound.get("value"):
            return False
        requested = raw.get("requested_options")
        effective = raw.get("effective_options")
        check_tolerances = check.get("tolerances")
        if (
            not isinstance(requested, Mapping)
            or not isinstance(effective, Mapping)
            or numerical.get("requested_options") != requested
            or numerical.get("effective_options") != effective
            or numerical.get("check_tolerances") != check_tolerances
        ):
            return False
        expected_numerical = _result_numerical_summary(
            check_record=check,
            objective_value=objective.get("value") if isinstance(objective, Mapping) else None,
            objective_sense=sense,
            bound=bound,
            absolute_gap=result.get("gap", {}).get("absolute") if isinstance(result.get("gap"), Mapping) else None,
            termination=result.get("termination"),
            has_incumbent=result.get("has_solver_incumbent") is True,
            requested_options=requested,
            effective_options=effective,
            core_gap_target=(
                numerical.get("core_gap_target")
                if isinstance(numerical.get("core_gap_target"), Mapping)
                else None
            ),
            native_observation=raw,
        )
        return numerical == expected_numerical and expected_numerical["optimality"]["status"] == "closed"
    return False


def _finite_number(value: Any) -> bool:
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _valid_global_bound(bound: Any, sense: str | None) -> bool:
    if not isinstance(bound, Mapping) or set(bound) != {"value", "direction", "scope", "evidence"}:
        return False
    direction = "lower" if sense == "min" else "upper" if sense == "max" else None
    return (
        bound.get("scope") == "global"
        and bound.get("direction") == direction
        and _finite_number(bound.get("value"))
    )


def _result_sense(plan: Any, layer: Any) -> str | None:
    if not isinstance(plan, Mapping):
        return None
    if plan.get("mode") == "scalar":
        value = plan.get("sense")
        return value if value in {"min", "max"} else None
    if plan.get("mode") == "lexicographic" and isinstance(layer, int) and not isinstance(layer, bool):
        layers = plan.get("layers")
        if isinstance(layers, list) and 0 <= layer < len(layers) and isinstance(layers[layer], Mapping):
            value = layers[layer].get("sense")
            return value if value in {"min", "max"} else None
    return None


def _claim_primary_records_are_current(
    store: CaseStore,
    state: Mapping[str, Any],
    claim: Mapping[str, Any],
    anchors: set[tuple[str, str, int, str]],
    cache: dict[str, dict[str, Any]],
) -> None:
    for path, _, _, _ in anchors:
        if path.endswith(".json") and "/proofs/" in path:
            store.read_ref(next(ref for ref in claim["evidence_refs"] if ref["path"] == path))
        result_parts = _result_path_parts(state["case_id"], path)
        if result_parts is not None:
            _verified_run(store, state, result_parts[0], cache, require_current=True)


def _claim_selected_proofs(store: CaseStore, state: Mapping[str, Any], claim: Mapping[str, Any]) -> set[str]:
    found: set[str] = set()
    for ref in claim.get("evidence_refs", []):
        path = ref["path"]
        prefix = f".reaslab/optagent/cases/{state['case_id']}/proofs/"
        proof_suffix = path[len(prefix) :] if path.startswith(prefix) else ""
        if path.startswith(prefix) and path.endswith(".json") and "/" not in proof_suffix:
            proof_id = proof_suffix[:-5]
            proof = _read_exact_record_ref(store, state, ref, f"proofs/{proof_id}.json")
            _validate_proof_binding(store, state, proof, ref, require_current=True)
            found.add(proof_id)
    return found


def _claim_selected_runs(
    store: CaseStore,
    state: Mapping[str, Any],
    claim: Mapping[str, Any],
    cache: dict[str, dict[str, Any]],
) -> set[str]:
    result: set[str] = set()
    for ref in claim.get("evidence_refs", []):
        parts = _result_path_parts(state["case_id"], ref["path"])
        if parts is None:
            continue
        verified = _verified_run(store, state, parts[0], cache, require_current=True)
        if _ref_identity(ref) in verified["results"]:
            result.add(parts[0])
    return result


def _run_observation_refs(run: Mapping[str, Any], run_ref: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = [dict(run_ref)]
    for container, key in ((run.get("transport"), "response"), (run.get("platform"), "status_evidence")):
        if isinstance(container, Mapping) and isinstance(container.get(key), Mapping):
            result.append(validate_ref_shape(dict(container[key])))
    cancel = run.get("cancel")
    if isinstance(cancel, Mapping):
        for value in (cancel.get("evidence"), cancel.get("stop_ack")):
            if isinstance(value, Mapping) and {"path", "sha256", "byte_len", "media_type"}.issubset(value):
                result.append(validate_ref_shape(dict(value)))
        stop = cancel.get("stop_attempt")
        if isinstance(stop, Mapping) and isinstance(stop.get("receipt"), Mapping):
            result.append(validate_ref_shape(dict(stop["receipt"])))
    unique: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    for ref in result:
        unique[_ref_identity(ref)] = ref
    return list(unique.values())


def _has_observation_ref(store: CaseStore, state: Mapping[str, Any], run_id: str, ref: Mapping[str, Any]) -> bool:
    try:
        verified = _diagnostic_run_context(store, state, run_id)
    except ContractError:
        return False
    allowed = _run_observation_refs(verified["run"], verified["run_ref"])
    if _ref_identity(ref) not in {_ref_identity(item) for item in allowed}:
        return False
    store.read_ref(ref)
    return True


def _diagnostic_run_context(store: CaseStore, state: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    if run_id not in state["run_ids"]:
        raise ContractError("evidence_insufficient", "diagnostic run is not registered on this case")
    run, run_ref = _load_run(store, state["case_id"], run_id)
    cancel = run.get("cancel")
    if not isinstance(cancel, Mapping):
        raise ContractError("artifact_changed", "diagnostic Run cancellation record is invalid")
    cancel_requested = cancel.get("requested") is True
    if run.get("dispatch_state") not in {"requested", "without_dispatch", "unknown", "response_received"}:
        raise ContractError("evidence_insufficient", "diagnostic run has no actual dispatch observation")
    worker = run.get("worker")
    if not isinstance(worker, Mapping) or worker.get("status") == "invalid":
        raise ContractError("output_invalid", "diagnostic run worker result was marked invalid")
    if run.get("owner_session_id") != state.get("owner_session_id"):
        raise ContractError("evidence_insufficient", "diagnostic Run belongs to a prior owner session")
    run_creator = run.get("created_by")
    if not isinstance(run_creator, Mapping) or run_creator.get("session_id") != state.get("owner_session_id"):
        raise ContractError("evidence_insufficient", "diagnostic Run creator is outside the current owner session")

    from .runner import _dispatch_plan, _load_prepared

    prepared_id = run.get("prepared_id")
    if prepared_id not in state.get("prepared_ids", []):
        raise ContractError("evidence_insufficient", "diagnostic Run Prepared is not registered on this case")
    prepared, _, manifest = _load_prepared(store, state["case_id"], prepared_id)
    prepared_creator = prepared.get("created_by")
    if not isinstance(prepared_creator, Mapping) or prepared_creator.get("session_id") != state.get("owner_session_id"):
        raise ContractError("evidence_insufficient", "diagnostic Prepared belongs to a prior owner session")
    if run.get("prepared_id") != prepared_id or prepared.get("prepared_id") != prepared_id:
        raise ContractError("artifact_changed", "diagnostic Run and Prepared identities do not match")
    invocation_ref = validate_ref_shape(run.get("worker_invocation"), pointer="/run/worker_invocation")
    expected_invocation_path = f".reaslab/optagent/cases/{state['case_id']}/runs/{run_id}/invocation.json"
    if invocation_ref["path"] != expected_invocation_path:
        raise ContractError("artifact_changed", "diagnostic WorkerInvocation is outside its fixed run path")
    _dispatch_plan(store, state["case_id"], run, prepared)

    candidate_id = state.get("candidate_id")
    if candidate_id is None or candidate_id != prepared.get("candidate_id"):
        raise ContractError("evidence_insufficient", "diagnostic Prepared does not match the current Candidate")
    candidate = store.read_record(f".reaslab/optagent/cases/{state['case_id']}/candidates/{candidate_id}.json")
    math_source_ids = source_projection(store, state)["math_source_ids"]
    candidate_binding_digest = candidate.get("binding_digest")
    prepared_generation = prepared.get("promotion_generation")
    case_generation = state.get("promotion_generation")
    if (
        not isinstance(candidate_binding_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", candidate_binding_digest) is None
        or type(prepared_generation) is not int
        or type(case_generation) is not int
    ):
        raise ContractError("artifact_changed", "diagnostic mathematical binding identity is invalid")
    manifest_sources = manifest.get("sources")
    manifest_source_ids = [item.get("source_id") for item in manifest_sources] if isinstance(manifest_sources, list) and all(isinstance(item, Mapping) for item in manifest_sources) else None
    math_binding_matches = (
        prepared.get("source_revision") == state.get("source_revision")
        and prepared.get("candidate_revision") == state.get("candidate_revision")
        and prepared.get("candidate_id") == candidate_id
        and prepared.get("source_ids") == math_source_ids
        and manifest_source_ids == math_source_ids
        and candidate.get("candidate_id") == candidate_id
        and candidate.get("source_revision") == state.get("source_revision")
        and candidate.get("candidate_revision") == state.get("candidate_revision")
        and candidate.get("source_ids") == math_source_ids
        and candidate_binding_digest == manifest.get("binding_digest")
        and manifest.get("candidate_id") == candidate_id
        and manifest.get("candidate_revision") == state.get("candidate_revision")
        and manifest.get("source_revision") == state.get("source_revision")
        and prepared.get("promotion_generation") == manifest.get("promotion_generation")
    )
    if not math_binding_matches:
        raise ContractError("evidence_insufficient", "diagnostic Prepared is outside the current mathematical binding")

    if cancel_requested:
        if (
            prepared_generation + 1 != case_generation
            or not state.get("run_ids")
            or state["run_ids"][-1] != run_id
            or not _valid_current_cancel_observation(store, state, run, invocation_ref)
        ):
            raise ContractError("evidence_insufficient", "diagnostic cancellation is outside the single current generation")
        binding_mode = "cancel_observation"
    else:
        if (
            cancel.get("requested") is not False
            or cancel.get("evidence") is not None
            or cancel.get("stop_attempt") is not None
            or cancel.get("stop_ack") is not None
        ):
            raise ContractError("artifact_changed", "Run has inconsistent cancellation fields")
        if prepared_generation != case_generation:
            raise ContractError("evidence_insufficient", "diagnostic Prepared generation is not current")
        binding_mode = "current"
    return {
        "run": run,
        "run_ref": run_ref,
        "prepared": prepared,
        "manifest": manifest,
        "invocation_ref": invocation_ref,
        "binding_mode": binding_mode,
    }


def _valid_current_cancel_observation(
    store: CaseStore,
    state: Mapping[str, Any],
    run: Mapping[str, Any],
    invocation_ref: Mapping[str, Any],
) -> bool:
    cancel = run.get("cancel")
    if not isinstance(cancel, Mapping) or cancel.get("requested") is not True:
        return False
    evidence = cancel.get("evidence")
    if not isinstance(evidence, Mapping):
        return False
    origin = evidence.get("origin")
    if evidence.get("session_id") != state.get("owner_session_id"):
        return False
    try:
        require_id(evidence.get("event_id"), pointer="/run/cancel/evidence/event_id")
    except ContractError:
        return False
    if origin == "product_tool":
        if set(evidence) != {"origin", "session_id", "event_id", "turn_id", "call_id"}:
            return False
        try:
            require_id(evidence.get("turn_id"), pointer="/run/cancel/evidence/turn_id")
            require_id(evidence.get("call_id"), pointer="/run/cancel/evidence/call_id")
        except ContractError:
            return False
        if evidence.get("event_id") != evidence.get("call_id"):
            return False
        for decision in state.get("decisions", []):
            if not isinstance(decision, Mapping) or decision.get("kind") != "run_cancel_requested":
                continue
            try:
                stamp = require_stamp(decision.get("stamp"), pointer="/decision/stamp")
            except ContractError:
                continue
            refs = decision.get("refs")
            if (
                stamp.get("role") == "root"
                and stamp.get("session_id") == evidence.get("session_id")
                and stamp.get("turn_id") == evidence.get("turn_id")
                and stamp.get("call_id") == evidence.get("call_id")
                and isinstance(refs, list)
                and len(refs) == 1
                and refs[0] == dict(invocation_ref)
            ):
                return _valid_stop_observations(store, state["case_id"], run)
        return False
    if origin == "external_acp":
        if (
            set(evidence) != {"origin", "session_id", "event_id", "turn_id", "call_id"}
            or evidence.get("turn_id") is not None
            or evidence.get("call_id") is not None
        ):
            return False
        return _valid_stop_observations(store, state["case_id"], run)
    return False


def _valid_stop_observations(store: CaseStore, case_id: str, run: Mapping[str, Any]) -> bool:
    cancel = run.get("cancel")
    if not isinstance(cancel, Mapping):
        return False
    run_id = run.get("run_id")
    stop_attempt = cancel.get("stop_attempt")
    stop_ack = cancel.get("stop_ack")
    run_prefix = f".reaslab/optagent/cases/{case_id}/runs/{run_id}/"
    if stop_attempt is None:
        return stop_ack is None
    if not isinstance(stop_attempt, Mapping):
        return False
    try:
        stamp = require_stamp(stop_attempt.get("stamp"), pointer="/run/cancel/stop_attempt/stamp")
    except ContractError:
        return False
    platform = run.get("platform")
    if not isinstance(platform, Mapping):
        return False
    if (
        stamp.get("role") != "root"
        or stamp.get("session_id") != run.get("owner_session_id")
        or not isinstance(stop_attempt.get("request_digest"), str)
        or re.fullmatch(r"[0-9a-f]{64}", stop_attempt["request_digest"]) is None
        or platform.get("id_binding") != "verified"
        or not isinstance(platform.get("execution_id"), str)
        or not platform["execution_id"]
    ):
        return False
    receipt_value = stop_attempt.get("receipt")
    if receipt_value is not None:
        try:
            receipt_ref = validate_ref_shape(receipt_value, pointer="/run/cancel/stop_attempt/receipt")
            if receipt_ref["path"] != run_prefix + "transport/cancel-receipt.json":
                return False
            raw = store.read_ref(receipt_ref)
            receipt = strict_json_loads(raw)
            from .runner import _transport_receipt

            parsed, _ = _transport_receipt(receipt)
            if canonical_json_bytes(parsed) != raw:
                return False
            if stop_attempt.get("state") != ("without_dispatch" if parsed["send_observation"] == "not_sent" else "unknown"):
                return False
        except (ContractError, ValueError, UnicodeError):
            return False
    if stop_ack is not None:
        try:
            ack_ref = validate_ref_shape(stop_ack, pointer="/run/cancel/stop_ack")
            if not ack_ref["path"].startswith(run_prefix) or _run_id_from_child_path(case_id, ack_ref["path"]) != run_id:
                return False
            store.read_ref(ack_ref)
        except ContractError:
            return False
    return True


def _diagnostic_observation_anchors(
    store: CaseStore,
    state: Mapping[str, Any],
    claim: Mapping[str, Any],
    runs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> set[str]:
    selected_run_ids = {run["run_id"] for run, _ in runs}
    evidence_ids = _refs_by_identity(claim.get("evidence_refs", []), pointer="/claim/evidence_refs")
    used: set[str] = set()
    for run, run_ref in runs:
        run_id = run["run_id"]
        if not any(
            _ref_identity(ref) in evidence_ids and _has_observation_ref(store, state, run_id, ref)
            for ref in [*claim.get("evidence_refs", [])]
        ):
            continue
        used.add(run_id)
    if not used.issubset(selected_run_ids):
        raise ContractError("evidence_insufficient", "diagnostic observation is not tied to a selected run")
    return used


def _validate_delivery_diagnostic_context(
    store: CaseStore,
    state: Mapping[str, Any],
    delivery: Mapping[str, Any],
) -> None:
    diagnostic_claims = [
        claim
        for claim in delivery.get("claims", [])
        if isinstance(claim, Mapping) and claim.get("kind") in {"diagnostic", "tradeoff"}
    ]
    if not diagnostic_claims:
        return
    run_ids = delivery.get("run_ids", [])
    if not isinstance(run_ids, list) or any(not isinstance(run_id, str) for run_id in run_ids):
        raise ContractError("artifact_changed", "selected diagnostic run IDs are invalid")
    runs = [_load_run(store, state["case_id"], run_id) for run_id in run_ids]
    selected_run_ids = set(run_ids)
    for claim in diagnostic_claims:
        for raw_ref in claim.get("evidence_refs", []):
            ref = validate_ref_shape(dict(raw_ref), pointer="/delivery/claims/evidence_refs")
            candidate_run_id = _run_id_from_child_path(state["case_id"], ref["path"])
            if candidate_run_id is not None and candidate_run_id in state["run_ids"] and _has_observation_ref(
                store, state, candidate_run_id, ref
            ) and candidate_run_id not in selected_run_ids:
                raise ContractError("evidence_insufficient", "diagnostic observation run is absent from the selected Delivery")
        observed = _diagnostic_observation_anchors(store, state, claim, runs)
        if run_ids and not observed:
            raise ContractError("evidence_insufficient", "selected diagnostic run no longer has a current linked observation")


def _is_formulation_only_selection(
    kind: str,
    state: Mapping[str, Any],
    candidate_id: str | None,
    claims: Sequence[Mapping[str, Any]],
    proofs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    runs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> bool:
    return (
        kind == "mixed"
        and candidate_id is not None
        and candidate_id == state.get("candidate_id")
        and not proofs
        and not runs
        and bool(claims)
        and all(
            claim.get("kind") == "formulation_fidelity"
            and claim.get("support") in {"supported", "conditional"}
            for claim in claims
        )
    )


def _result_has_bound(store: CaseStore, ref: Mapping[str, Any]) -> bool:
    try:
        result = strict_json_loads(store.read_ref(ref))
    except (ValueError, ContractError):
        return False
    bound = result.get("bound") if isinstance(result, dict) else None
    value = bound.get("value") if isinstance(bound, dict) else None
    finite_number = type(value) is int or (isinstance(value, float) and math.isfinite(value))
    return isinstance(bound, dict) and finite_number and bound.get("scope") == "global"


def _validate_claim(claim: Mapping[str, Any]) -> None:
    if set(claim) != {"kind", "scope", "conditions", "evidence_refs", "support"}:
        raise ContractError("invalid_argument", "claim has invalid fields")
    if claim["kind"] not in _CLAIM_KINDS or claim["support"] not in {"supported", "conditional", "unresolved", "refuted"}:
        raise ContractError("invalid_argument", "claim kind or support is invalid")
    if not isinstance(claim["scope"], str) or not isinstance(claim["conditions"], list) or any(not isinstance(item, str) or not item for item in claim["conditions"]):
        raise ContractError("invalid_argument", "claim scope or conditions are invalid")
    if not isinstance(claim["evidence_refs"], list) or any(not isinstance(item, Mapping) for item in claim["evidence_refs"]):
        raise ContractError("invalid_argument", "claim evidence references are invalid")
    if claim["kind"] == "optimality" and claim["support"] == "conditional" and not claim["conditions"]:
        raise ContractError("invalid_argument", "conditional optimality needs an explicit nonempty condition")


def _validate_case_evidence_ref(store: CaseStore, state: Mapping[str, Any], ref: Mapping[str, Any]) -> bytes:
    raw = store.read_ref(ref)
    path = ref["path"].replace("\\", "/")
    parts = PurePosixPath(path).parts
    if path.startswith(".reaslab/optagent/cases/"):
        expected = f".reaslab/optagent/cases/{state['case_id']}/"
        if not path.startswith(expected):
            raise ContractError("permission_denied", "evidence reference belongs to another case")
        return raw
    if path.startswith(".reaslab/optagent/source-bytes/"):
        own_sources = {
            (source["bytes"]["path"], source["bytes"]["sha256"])
            for source in (
                store.read_record(f".reaslab/optagent/cases/{state['case_id']}/sources/{source_id}.json")
                for source_id in state["source_ids"]
            )
        }
        if (path, ref["sha256"]) not in own_sources:
            raise ContractError("permission_denied", "raw evidence is not registered on this case")
        return raw
    if path == ".reaslab" or path.startswith(".reaslab/"):
        raise ContractError("permission_denied", "internal evidence reference is outside this case")
    if any(_is_reserved_package_part(part) for part in parts):
        raise ContractError("path_denied", "evidence reference points to a reserved path")
    return raw


def _check_pending_and_current_inputs(
    store: CaseStore,
    state: Mapping[str, Any],
    *,
    verify_source_bytes: bool = True,
    verify_candidate_data: bool = True,
) -> dict[str, Any]:
    if state["pending_inputs"]:
        raise ContractError("input_unclassified", "case has unclassified input")
    source_view = source_projection(store, state)
    bundle = None
    candidate_id = state.get("candidate_id")
    if candidate_id:
        candidate = store.read_record(f".reaslab/optagent/cases/{state['case_id']}/candidates/{candidate_id}.json")
        if candidate.get("source_ids") != source_view["math_source_ids"]:
            raise ContractError("candidate_dirty", "registered Candidate has a different mathematical Source projection")
        for ref in [candidate["model"], *candidate["local_modules"], *([candidate["business_checker"]] if candidate.get("business_checker") else [])]:
            current = store.ref_for_path(ref["path"], media_type=ref["media_type"])
            if current["sha256"] != ref["sha256"] or current["byte_len"] != ref["byte_len"]:
                raise ContractError("candidate_dirty", "registered model bytes changed", details={"path": ref["path"]})
        if candidate.get("data_id") is not None and verify_candidate_data:
            manifest_path = f".reaslab/optagent/cases/{state['case_id']}/data/{candidate['data_id']}/manifest.json"
            manifest_ref = store.ref_for_path(manifest_path)
            bundle = load_data_bundle(
                store,
                manifest_ref,
                candidate_binding_digest=candidate.get("binding_digest"),
                source_records=source_view["records"],
            )
            if candidate.get("instance_parameter_object_id") is not None:
                bundle.table(candidate["instance_parameter_object_id"])
    bundle_source_ref_ids = {
        (ref["path"], ref["sha256"], ref["byte_len"], ref["media_type"])
        for ref in (bundle.consumed_inputs if bundle is not None else ())
    }
    for source_id in source_view["math_source_ids"]:
        source = source_view["records"][source_id]
        source_ref = validate_ref_shape(source["bytes"], pointer=f"/sources/{source_id}/bytes")
        identity = (source_ref["path"], source_ref["sha256"], source_ref["byte_len"], source_ref["media_type"])
        if verify_source_bytes and identity not in bundle_source_ref_ids:
            store.read_ref(source_ref)
        path = source["origin"].get("workspace_path")
        if path is not None:
            current = store.ref_for_path(path)
            if current["sha256"] != source["original_sha256"]:
                raise ContractError("artifact_changed", "a source file changed after capture", details={"source_id": source_id, "path": path})
    return source_view


def _check_delivery_binding(
    store: CaseStore,
    state: Mapping[str, Any],
    delivery: Mapping[str, Any],
    *,
    math_source_ids: Sequence[str] | None = None,
) -> None:
    if math_source_ids is None:
        math_source_ids = source_projection(store, state)["math_source_ids"]
    if (
        delivery["source_revision"] != state["source_revision"]
        or delivery.get("math_source_ids") != list(math_source_ids)
        or delivery["candidate_id"] != state["candidate_id"]
        or delivery["promotion_generation"] != state["promotion_generation"]
    ):
        raise ContractError("evidence_insufficient", "selected delivery is stale for the current case")


def _check_delivery_authorities(store: CaseStore, state: Mapping[str, Any], delivery: Mapping[str, Any]) -> None:
    authorities = delivery.get("authority_records")
    if not isinstance(authorities, Mapping) or set(authorities) != {"reviews", "runs"}:
        raise ContractError("evidence_insufficient", "selected evidence lacks exact current Review/Run Refs")
    for family in ("reviews", "runs"):
        records = authorities.get(family)
        ids = delivery.get("review_ids" if family == "reviews" else "run_ids")
        if not isinstance(records, Mapping) or not isinstance(ids, list) or set(records) != set(ids):
            raise ContractError("artifact_changed", "selection authority records do not match its family IDs")
        for object_id in ids:
            expected = validate_ref_shape(records[object_id], pointer=f"/delivery/authority_records/{family}/{object_id}")
            legacy_path = (
                f".reaslab/optagent/cases/{state['case_id']}/reviews/{object_id}.json"
                if family == "reviews"
                else f".reaslab/optagent/cases/{state['case_id']}/runs/{object_id}/record.json"
            )
            current_record, current_ref = store.read_family_record(
                state["case_id"], family, object_id, state=state, legacy_path=legacy_path
            )
            if current_ref != expected:
                raise ContractError("evidence_insufficient", "selected Review/Run Ref is no longer current")
            if family == "reviews":
                outcomes = current_record.get("outcomes", [])
                answer_value = current_record.get("answer_ref")
                if (
                    current_record.get("status") != "closed"
                    or not isinstance(outcomes, list)
                    or not outcomes
                    or not isinstance(outcomes[-1], Mapping)
                    or outcomes[-1].get("decision") != "ready"
                    or not isinstance(answer_value, Mapping)
                ):
                    raise ContractError("evidence_insufficient", "selected Review no longer has a ready approved answer")
                answer_ref = validate_ref_shape(dict(answer_value), pointer="/review/answer_ref")
                report_ids = _refs_by_identity(delivery.get("reports", []), pointer="/delivery/reports")
                evidence_ids = _refs_by_identity(delivery.get("evidence_refs", []), pointer="/delivery/evidence_refs")
                answer_identity = _ref_identity(answer_ref)
                if answer_identity not in report_ids or answer_identity not in evidence_ids:
                    raise ContractError("artifact_changed", "approved answer Ref is missing from selected Delivery")


def _assert_selected(current: dict[str, Any], delivery_id: str) -> None:
    if current["selected_delivery_id"] != delivery_id:
        raise ContractError("stale_revision", "selected delivery changed")


def _require_owner(state: Mapping[str, Any], stamp: Mapping[str, str]) -> None:
    if stamp.get("role") != "root" or state.get("owner_session_id") != stamp.get("session_id"):
        raise ContractError("permission_denied", "only the current owner can change delivery state")


def _unique_refs(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        key = (value["path"], value["sha256"])
        if key not in seen:
            out.append(dict(value))
            seen.add(key)
    return out


def _media_type(suffix: str) -> str:
    path = suffix if Path(suffix).suffix else f"file{suffix}"
    return _workspace_media_type(path)


def _suffix(path: str) -> str:
    suffix = Path(path).suffix
    return suffix if suffix and len(suffix) < 16 else ".bin"


def _state_summary(store: CaseStore, state: Mapping[str, Any]) -> dict[str, Any]:
    return state_summary(store, state)


def state_summary(store: CaseStore, state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "case_id": state["case_id"],
        "title": state["title"],
        "artifact_dir": case_artifact_dir_for_state(state),
        "owner_session_id": state["owner_session_id"],
        "state_revision": state["state_revision"],
        "source_revision": state["source_revision"],
        "candidate_revision": state["candidate_revision"],
        "promotion_generation": state["promotion_generation"],
        "current_validity": "none" if state["selected_delivery_id"] is None else "valid",
        "pending_question_count": pending_question_count(store, state),
        "pending_input_count": len(state["pending_inputs"]),
    }


def pending_question_count(store: CaseStore, state: Mapping[str, Any]) -> int:
    """Count every open clarification, whether or not it blocks delivery."""
    case_id = state["case_id"]
    count = 0
    for question_id in state["question_ids"]:
        question, _ = store.read_family_record(
            case_id,
            "questions",
            question_id,
            state=state,
            legacy_path=f".reaslab/optagent/cases/{case_id}/questions/{question_id}.json",
        )
        if question.get("status") == "pending":
            count += 1
    return count


def object_summary(object_id: str, record_ref: Mapping[str, Any], status: str, source_revision: int | None, candidate_revision: int | None) -> dict[str, Any]:
    return {
        "id": object_id,
        "record_ref": dict(record_ref),
        "status": status,
        "source_revision": source_revision,
        "candidate_revision": candidate_revision,
    }
