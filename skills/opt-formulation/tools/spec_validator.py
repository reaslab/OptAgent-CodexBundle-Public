#!/usr/bin/env python3
"""
spec_validator.py — Validates opt_spec.json structure and completeness.

Usage:
  python3 spec_validator.py --spec .optagent/cases/case_001_base/formulation/opt_spec.json --manifest .optagent/cases/case_001_base/data/data_manifest.json
  Inside OptAgent, run via: python .agents/skills/opt-formulation/tools/spec_validator.py

Checks:
  - Required top-level fields exist
  - Sets referenced by variables/constraints are defined
  - Parameters referenced have value or data_ref
  - data_ref objects exist in the case data manifest
  - opt_spec.json < 50KB
  - modeling_decisions present (warns if absent)
"""
import argparse
import json
import os
import sys
from pathlib import Path


DEFAULT_MAX_SPEC_BYTES = 50 * 1024
DEFAULT_INLINE_ENTRY_WARNING = 100


def emit_report(report):
    # JSON diagnostics are consumed by agents and test harnesses across shells.
    # ASCII escapes keep the bytes valid even when stdout uses a legacy code page.
    print(json.dumps(report, indent=2, ensure_ascii=True))


def workspace_root_for_manifest(manifest_path, explicit_workspace_root=None):
    """Resolve workspace-relative source paths without assuming a fixed depth."""
    if explicit_workspace_root:
        return Path(explicit_workspace_root).resolve()

    manifest = Path(manifest_path).resolve()
    for parent in (manifest.parent, *manifest.parents):
        if parent.name == ".optagent":
            return parent.parent
    return Path.cwd()


def resolve_path(path, workspace_root):
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return workspace_root / candidate


def scalar_entry_count(value):
    """Count materialized scalar values without treating object keys as entries."""
    if isinstance(value, dict):
        return sum(scalar_entry_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(scalar_entry_count(item) for item in value)
    return 1


def main():
    parser = argparse.ArgumentParser(description="Validate opt_spec.json")
    parser.add_argument("--spec", required=True, help="Path to opt_spec.json")
    parser.add_argument("--manifest", required=True, help="Path to case data_manifest.json")
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_SPEC_BYTES,
        help="Maximum allowed opt_spec.json size (default: 51200).",
    )
    parser.add_argument(
        "--inline-entry-warning",
        type=int,
        default=DEFAULT_INLINE_ENTRY_WARNING,
        help="Warn when an inline parameter or explicit set exceeds this entry count.",
    )
    parser.add_argument(
        "--workspace-root",
        help="Workspace root used to resolve relative manifest source_path values.",
    )
    args = parser.parse_args()

    if args.max_bytes <= 0:
        parser.error("--max-bytes must be positive")
    if args.inline_entry_warning <= 0:
        parser.error("--inline-entry-warning must be positive")

    report = {
        "valid": True,
        "errors": [],
        "warnings": [],
        "stats": {}
    }

    def add_error(message):
        report["valid"] = False
        report["errors"].append(message)

    # Check file exists and size
    if not os.path.exists(args.spec):
        add_error(f"opt_spec.json not found: {args.spec}")
        emit_report(report)
        return 1

    file_size = os.path.getsize(args.spec)
    report["stats"]["file_size_bytes"] = file_size
    report["stats"]["max_file_size_bytes"] = args.max_bytes
    report["stats"]["inline_entry_warning_threshold"] = args.inline_entry_warning
    if file_size > args.max_bytes:
        add_error(
            f"opt_spec.json exceeds size limit: {file_size} bytes > {args.max_bytes} bytes"
        )

    # Load spec
    try:
        with open(args.spec, "r", encoding="utf-8-sig") as f:
            spec = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        add_error(f"Failed to parse opt_spec.json: {e}")
        emit_report(report)
        return 1
    if not isinstance(spec, dict):
        add_error("opt_spec.json must contain an object")
        emit_report(report)
        return 1

    # Load manifest
    manifest = None
    if not os.path.exists(args.manifest):
        add_error(f"data_manifest.json not found: {args.manifest}")
    else:
        try:
            with open(args.manifest, "r", encoding="utf-8-sig") as f:
                manifest = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            add_error(f"Failed to parse data_manifest.json: {e}")
    if manifest is not None and not isinstance(manifest, dict):
        add_error("data_manifest.json must contain an object")
        manifest = {}

    workspace_root = workspace_root_for_manifest(args.manifest, args.workspace_root)
    report["stats"]["workspace_root"] = str(workspace_root)

    required_manifest_fields = {
        "version": int,
        "data_objects": dict,
        "relationships": list,
        "assumptions": list,
        "ambiguities": list,
    }
    if isinstance(manifest, dict):
        for field, expected_type in required_manifest_fields.items():
            if field not in manifest:
                add_error(f"data_manifest.json is missing required field: {field}")
            elif not isinstance(manifest[field], expected_type):
                add_error(
                    f"data_manifest.json field '{field}' must be a {expected_type.__name__}"
                )

    # Check required top-level fields
    required_fields = [
        "problem_class",
        "family",
        "sense",
        "sets",
        "parameters",
        "decision_variables",
        "objective",
        "constraints",
    ]
    for field in required_fields:
        if field not in spec:
            add_error(f"Missing required field: {field}")

    expected_types = {
        "problem_class": str,
        "family": str,
        "sense": str,
        "sets": dict,
        "parameters": dict,
        "decision_variables": list,
        "objective": dict,
        "constraints": list,
    }
    for field, expected_type in expected_types.items():
        if field in spec and not isinstance(spec[field], expected_type):
            add_error(
                f"Field '{field}' must be a {expected_type.__name__}"
            )

    if isinstance(spec.get("problem_class"), str) and not spec["problem_class"].strip():
        add_error("Field 'problem_class' must not be empty")
    if isinstance(spec.get("family"), str) and not spec["family"].strip():
        add_error("Field 'family' must not be empty")
    if isinstance(spec.get("sense"), str) and spec["sense"] not in {"minimize", "maximize"}:
        add_error("Field 'sense' must be either 'minimize' or 'maximize'")

    # Collect defined sets
    sets = spec.get("sets", {}) if isinstance(spec.get("sets"), dict) else {}
    defined_sets = set(sets.keys())
    report["stats"]["num_sets"] = len(defined_sets)

    # Collect defined parameters
    params = spec.get("parameters", {}) if isinstance(spec.get("parameters"), dict) else {}
    report["stats"]["num_parameters"] = len(params)

    data_objects = manifest.get("data_objects", {}) if isinstance(manifest, dict) else {}
    if not isinstance(data_objects, dict):
        add_error("data_manifest.json field 'data_objects' must be an object")
        data_objects = {}

    object_status = {}
    for obj_name, obj in data_objects.items():
        if not isinstance(obj, dict):
            add_error(f"Data object '{obj_name}' must be an object")
            object_status[obj_name] = {"materialized": None, "readable": False}
            continue
        metadata_value = obj.get("metadata", {})
        metadata = metadata_value if isinstance(metadata_value, dict) else {}
        materialized = metadata.get("materialized", True)
        source_path = obj.get("source_path") or obj.get("path")
        if not isinstance(metadata_value, dict):
            report["warnings"].append(f"Data object '{obj_name}' metadata is not an object")
        if not isinstance(materialized, bool):
            report["warnings"].append(
                f"Data object '{obj_name}' metadata.materialized is not a boolean"
            )
        status = {
            "materialized": materialized,
            "has_reason": bool(metadata.get("materialized_reason")),
            "has_source_path": isinstance(source_path, str) and bool(source_path.strip()),
            "readable": False,
        }
        if materialized is False:
            if not metadata.get("materialized_reason"):
                report["warnings"].append(f"Data object '{obj_name}' is materialized=false but has no materialized_reason")
            object_status[obj_name] = status
            continue
        if not source_path:
            report["warnings"].append(f"Data object '{obj_name}' has no source_path")
            object_status[obj_name] = status
            continue
        if not isinstance(source_path, str):
            report["warnings"].append(f"Data object '{obj_name}' source_path must be a string")
            object_status[obj_name] = status
            continue
        resolved_source = resolve_path(source_path, workspace_root)
        status["resolved_source_path"] = str(resolved_source)
        status["readable"] = resolved_source.is_file() and os.access(resolved_source, os.R_OK)
        if not status["readable"]:
            report["warnings"].append(f"Data object '{obj_name}' source_path is not readable: {source_path}")
        object_status[obj_name] = status

    # Check parameters have value or data_ref
    for pname, pdef in params.items():
        if not isinstance(pdef, dict):
            add_error(f"Parameter '{pname}' must be an object")
            continue
        if "value" not in pdef and "data_ref" not in pdef:
            add_error(f"Parameter '{pname}' has neither 'value' nor 'data_ref'")
        if "value" in pdef and "data_ref" in pdef:
            report["warnings"].append(
                f"Parameter '{pname}' contains both 'value' and 'data_ref'; avoid duplicate data unless both representations are necessary"
            )
        if "value" in pdef:
            inline_entries = scalar_entry_count(pdef["value"])
            report["stats"].setdefault("inline_parameter_entries", {})[pname] = inline_entries
            if inline_entries > args.inline_entry_warning:
                report["warnings"].append(
                    f"Parameter '{pname}' contains {inline_entries} inline values; consider data_ref"
                )
        if "data_ref" in pdef:
            ref = pdef["data_ref"]
            if not isinstance(ref, dict):
                add_error(f"Parameter '{pname}' data_ref must be an object")
                continue
            obj_name = ref.get("object")
            if not isinstance(obj_name, str) or not obj_name.strip():
                add_error(f"Parameter '{pname}' data_ref must name a data object via 'object'")
            elif obj_name not in data_objects:
                add_error(
                    f"Parameter '{pname}' references data object '{obj_name}' not found in data_manifest.json"
                )
            else:
                status = object_status.get(obj_name, {})
                if status.get("materialized") is False:
                    if not status.get("has_reason"):
                        add_error(
                            f"Parameter '{pname}' references non-materialized data object '{obj_name}' without materialized_reason"
                        )
                elif not status.get("has_source_path"):
                    add_error(
                        f"Parameter '{pname}' references data object '{obj_name}' without a readable source_path"
                    )
                elif not status.get("readable"):
                    add_error(
                        f"Parameter '{pname}' references data object '{obj_name}' whose source_path is not readable"
                    )

    # Check set provenance objects when large sets use manifest-backed objects.
    for sname, sdef in sets.items():
        if not isinstance(sdef, dict):
            add_error(f"Set '{sname}' must be an object")
            continue
        elements = sdef.get("elements")
        if elements is not None and not isinstance(elements, list):
            add_error(f"Set '{sname}' elements must be an array when present")
            continue
        if isinstance(elements, list) and len(elements) > args.inline_entry_warning:
            report["warnings"].append(
                f"Set '{sname}' contains {len(elements)} explicit elements; consider a compact source-backed representation"
            )
        obj_name = sdef.get("source_object")
        if not obj_name and isinstance(sdef.get("source"), str) and sdef["source"].startswith("object:"):
            obj_name = sdef["source"].split(":", 1)[1].split(".", 1)[0]
        if obj_name and obj_name not in data_objects:
            report["warnings"].append(f"Set '{sname}' references data object '{obj_name}' not found in data_manifest.json")

    # Check variable groups reference valid sets
    var_groups = spec.get("variable_groups")
    if var_groups is not None and not isinstance(var_groups, (dict, list)):
        add_error("Field 'variable_groups' must be an object or array when present")
    if isinstance(var_groups, dict):
        variable_groups = var_groups.items()
        report["stats"]["num_variable_groups"] = len(var_groups)
    elif isinstance(var_groups, list):
        variable_groups = ((str(index), value) for index, value in enumerate(var_groups))
        report["stats"]["num_variable_groups"] = len(var_groups)
    else:
        variable_groups = []
    for vname, vdef in variable_groups:
        if not isinstance(vdef, dict):
            add_error(f"Variable group '{vname}' must be an object")
            continue
        index_sets = vdef.get("index_sets", [])
        if not isinstance(index_sets, list):
            add_error(f"Variable group '{vname}' index_sets must be an array")
            continue
        for idx_set in index_sets:
            if idx_set not in defined_sets:
                report["warnings"].append(f"Variable group '{vname}' references undefined set '{idx_set}'")

    # Check constraints reference valid sets
    constraints = spec.get("constraints", []) if isinstance(spec.get("constraints"), list) else []
    indexed_constraints_value = spec.get("indexed_constraints", [])
    if not isinstance(indexed_constraints_value, list):
        add_error("Field 'indexed_constraints' must be an array when present")
        indexed_constraints = []
    else:
        indexed_constraints = indexed_constraints_value
    report["stats"]["num_constraints"] = len(constraints)
    report["stats"]["num_indexed_constraints"] = len(indexed_constraints)

    for ic in indexed_constraints:
        if not isinstance(ic, dict):
            add_error("Each indexed constraint must be an object")
            continue
        index_sets = ic.get("index_sets", [])
        if not isinstance(index_sets, list):
            add_error(f"Indexed constraint '{ic.get('name', '?')}' index_sets must be an array")
            continue
        for idx_set in index_sets:
            if idx_set not in defined_sets:
                report["warnings"].append(f"Indexed constraint '{ic.get('name', '?')}' references undefined set '{idx_set}'")

    # Check modeling_decisions presence
    if "modeling_decisions" not in spec:
        report["warnings"].append("No 'modeling_decisions' field in opt_spec.json — modeling choices are undocumented")
    else:
        decisions = spec["modeling_decisions"]
        if not isinstance(decisions, list):
            add_error("Field 'modeling_decisions' must be an array when present")
        else:
            report["stats"]["num_modeling_decisions"] = len(decisions)
            for i, d in enumerate(decisions):
                if not isinstance(d, dict):
                    add_error(f"modeling_decisions[{i}] must be an object")
                elif not d.get("justification"):
                    report["warnings"].append(f"modeling_decisions[{i}] has no justification")

    # Check objective exists and has terms
    obj = spec.get("objective", {})
    if isinstance(obj, dict):
        has_terms = any([
            obj.get("linear_terms"),
            obj.get("indexed_terms"),
            obj.get("term_templates"),
            obj.get("expression")
        ])
        if not has_terms:
            report["warnings"].append("Objective has no linear_terms, indexed_terms, term_templates, or expression")

    # Summary
    report["stats"]["total_errors"] = len(report["errors"])
    report["stats"]["total_warnings"] = len(report["warnings"])

    emit_report(report)
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
