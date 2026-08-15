#!/usr/bin/env python3
"""Run bounded deterministic prechecks before OptAgent's semantic verification.

The bundle is internal evidence, not a replacement for the verifier's
Problem-to-Model audit or case-specific mathematical reasoning. A check is
never reported as passed unless its declared coverage is fully machine-checkable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VERSION = 1
STATUSES = {
    "pass", "fail", "unsupported", "not_applicable", "environment_limited"
}
SOLUTION_STATUSES = {
    "optimal", "feasible", "infeasible", "unbounded", "timeout", "error",
    "solver_unavailable", "not_implemented",
}
PATH_ANCHOR_PATTERNS = (
    "Path(__file__).resolve().parent",
    "os.path.dirname(__file__)",
    "os.path.abspath(os.path.dirname(__file__))",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--solution", required=True)
    parser.add_argument("--execution-record", required=True)
    parser.add_argument("--public-code", required=True)
    parser.add_argument("--public-data-manifest")
    parser.add_argument("--solver-log")
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--model-auditor",
        default=resolve_helper(skills_rel="opt-verification/tools/model_auditor.py"),
    )
    parser.add_argument(
        "--solution-checker",
        default=resolve_helper(skills_rel="opt-verification/tools/solution_checker.py"),
    )
    parser.add_argument("--tool-python", default=sys.executable)
    parser.add_argument("--tool-timeout-sec", type=positive_int, default=30)
    parser.add_argument("--tolerance", type=positive_float, default=1e-4)
    return parser.parse_args()


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def resolve_helper(skills_rel: str | None = None, tools_name: str | None = None) -> str:
    """Locate a helper in the repository-local ``.agents`` bundle."""
    here = Path(__file__).resolve().parent
    candidates: list[Path] = []
    if tools_name:
        candidates.append(here / tools_name)
    if skills_rel:
        candidates += [
            here.parent / "skills" / skills_rel,
            here.parents[1] / "skills" / skills_rel,
        ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return str(candidates[0]) if candidates else ""


def positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return parsed


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def relative_to_workspace(path: Path, workspace: Path) -> str:
    try:
        return path.resolve().relative_to(workspace).as_posix()
    except ValueError:
        return str(path.resolve())


def infer_workspace(paths: list[Path]) -> Path:
    for path in paths:
        resolved = path.resolve()
        for parent in (resolved.parent, *resolved.parents):
            if parent.name == ".optagent":
                return parent.parent
    return Path.cwd().resolve()


def artifact_info(path: Path, workspace: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "path": relative_to_workspace(path, workspace),
            "exists": False,
            "bytes": None,
            "sha256": None,
        }
    return {
        "path": relative_to_workspace(path, workspace),
        "exists": True,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def load_json(path: Path) -> tuple[Any | None, str | None]:
    if not path.is_file():
        return None, f"file is missing: {path}"
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")), None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"failed to parse JSON: {exc}"


def check(
    name: str,
    status: str,
    detail: str,
    sources: dict[str, dict[str, Any]],
    source_names: list[str],
    coverage: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"invalid check status: {status}")
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "coverage": coverage or {},
        "source_hashes": {
            name: sources[name]["sha256"] for name in source_names if name in sources
        },
        "evidence": evidence or {},
    }


def numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def extract_variable_groups(spec: dict[str, Any]) -> tuple[list[tuple[str, dict[str, Any]]], str | None]:
    raw = spec.get("variable_groups", spec.get("decision_variables"))
    if isinstance(raw, list):
        groups = []
        for index, value in enumerate(raw):
            if not isinstance(value, dict):
                return [], "variable group list contains a non-object entry"
            name = value.get("name")
            if not isinstance(name, str) or not name:
                return [], f"variable group at index {index} has no name"
            groups.append((name, value))
        return groups, None
    if isinstance(raw, dict):
        groups = []
        for name, value in raw.items():
            if not isinstance(value, dict):
                return [], f"variable group {name!r} is not an object"
            groups.append((str(name), value))
        return groups, None
    return [], "spec has no supported variable_groups or decision_variables representation"


def variable_matches_group(variable_name: str, group_name: str) -> bool:
    return variable_name == group_name or variable_name.startswith(
        (f"{group_name}_", f"{group_name}[", f"{group_name}(")
    )


def explicit_variable_instances(definition: dict[str, Any]) -> list[str] | None:
    """Return an explicit instance list when the spec provides one.

    Indexed groups commonly describe their domain without listing every concrete
    variable name. In that case a checker may inspect observed values, but it
    cannot prove solution-vector completeness from names alone.
    """
    for key in ("instances", "instance_names", "variable_names"):
        value = definition.get(key)
        if isinstance(value, list) and all(isinstance(item, str) and item for item in value):
            return list(value)
    return None


def declares_unenumerated_indexing(definition: dict[str, Any]) -> bool:
    if explicit_variable_instances(definition) is not None:
        return False
    return any(
        definition.get(key) not in (None, "", [], {})
        for key in ("indexing", "index_sets", "indices", "dimensions", "shape")
    )


def solution_schema_check(
    solution: Any,
    sources: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(solution, dict):
        return check(
            "solution_schema", "fail", "solution.json must be an object", sources, ["solution"]
        )
    status = solution.get("status")
    incumbent = solution.get("has_incumbent")
    variables = solution.get("variables")
    errors = []
    if status not in SOLUTION_STATUSES:
        errors.append("status is missing or unsupported")
    if not isinstance(incumbent, bool):
        errors.append("has_incumbent must be boolean")
    if not isinstance(variables, dict):
        errors.append("variables must be an object")
    if status in {"optimal", "feasible"} and incumbent is not True:
        errors.append("optimal/feasible status requires has_incumbent=true")
    if status in {"infeasible", "unbounded"} and incumbent is True:
        errors.append("infeasible/unbounded status cannot declare an incumbent")
    if errors:
        return check(
            "solution_schema", "fail", "; ".join(errors), sources, ["solution"]
        )
    return check(
        "solution_schema",
        "pass",
        "normalized solution status and incumbent fields are structurally consistent",
        sources,
        ["solution"],
        {"variable_count": len(variables)},
    )


def variable_domain_check(
    spec: Any,
    solution: Any,
    sources: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(spec, dict) or not isinstance(solution, dict):
        return check(
            "variable_domains", "unsupported", "spec or solution is not a JSON object",
            sources, ["spec", "solution"],
        )
    if solution.get("has_incumbent") is not True:
        return check(
            "variable_domains", "not_applicable", "no incumbent is available for domain evaluation",
            sources, ["spec", "solution"],
        )
    variables = solution.get("variables")
    if not isinstance(variables, dict) or not variables:
        return check(
            "variable_domains", "fail", "incumbent is declared but variables are absent",
            sources, ["spec", "solution"],
        )
    groups, error = extract_variable_groups(spec)
    if error:
        return check(
            "variable_domains", "unsupported", error, sources, ["spec", "solution"]
        )
    if not groups:
        return check(
            "variable_domains", "not_applicable", "spec declares no decision variables",
            sources, ["spec", "solution"],
        )

    violations = []
    unsupported = []
    matched_variables = 0
    unenumerated_indexed_groups = 0
    for group_name, definition in groups:
        explicit_instances = explicit_variable_instances(definition)
        indexed_without_instances = declares_unenumerated_indexing(definition)
        if explicit_instances is not None:
            names = [name for name in explicit_instances if name in variables]
            missing_instances = [name for name in explicit_instances if name not in variables]
            if missing_instances:
                unsupported.append(
                    f"group {group_name!r} is missing explicit solution instances: {missing_instances[:5]}"
                )
        elif indexed_without_instances:
            names = [
                str(name) for name in variables if variable_matches_group(str(name), group_name)
            ]
            unenumerated_indexed_groups += 1
            unsupported.append(
                f"group {group_name!r} is indexed without an explicit instance list; completeness cannot be checked"
            )
        else:
            names = [group_name] if group_name in variables else []
        if not names:
            unsupported.append(f"no solution variable name matched group {group_name!r}")
            continue
        variable_type = definition.get("type", "continuous")
        lower = definition.get("lb", definition.get("lower_bound"))
        upper = definition.get("ub", definition.get("upper_bound"))
        lower_value = numeric(lower) if lower is not None else None
        upper_value = numeric(upper) if upper is not None else None
        if lower is not None and lower_value is None:
            unsupported.append(f"group {group_name!r} has a non-numeric lower bound")
        if upper is not None and upper_value is None:
            unsupported.append(f"group {group_name!r} has a non-numeric upper bound")
        if variable_type not in {"continuous", "integer", "binary"}:
            unsupported.append(f"group {group_name!r} has unsupported type {variable_type!r}")
            continue
        for name in names:
            value = numeric(variables[name])
            if value is None:
                unsupported.append(f"solution variable {name!r} is not numeric")
                continue
            matched_variables += 1
            if lower_value is not None and value < lower_value - 1e-6:
                violations.append(f"{name}={value} < lb={lower_value}")
            if upper_value is not None and value > upper_value + 1e-6:
                violations.append(f"{name}={value} > ub={upper_value}")
            if variable_type == "binary" and min(abs(value), abs(value - 1.0)) > 1e-6:
                violations.append(f"{name}={value} is not binary")
            if variable_type == "integer" and abs(value - round(value)) > 1e-6:
                violations.append(f"{name}={value} is not integer")
    coverage = {
        "groups_total": len(groups),
        "variables_matched": matched_variables,
        "unsupported_items": len(unsupported),
        "unenumerated_indexed_groups": unenumerated_indexed_groups,
    }
    if violations:
        return check(
            "variable_domains", "fail", "variable domain violations detected",
            sources, ["spec", "solution"], coverage,
            {"examples": violations[:10], "unsupported": unsupported[:10]},
        )
    if unsupported:
        return check(
            "variable_domains", "unsupported", "domain checks were only partially matchable",
            sources, ["spec", "solution"], coverage,
            {"unsupported": unsupported[:10]},
        )
    return check(
        "variable_domains", "pass", "all declared groups matched numeric solution variables",
        sources, ["spec", "solution"], coverage,
    )


def objective_check(
    spec: Any,
    solution: Any,
    sources: dict[str, dict[str, Any]],
    tolerance: float,
) -> dict[str, Any]:
    if not isinstance(spec, dict) or not isinstance(solution, dict):
        return check(
            "objective_recomputation", "unsupported", "spec or solution is not a JSON object",
            sources, ["spec", "solution"],
        )
    if solution.get("has_incumbent") is not True:
        return check(
            "objective_recomputation", "not_applicable", "no incumbent is available",
            sources, ["spec", "solution"],
        )
    objective = spec.get("objective")
    variables = solution.get("variables")
    reported = numeric(solution.get("objective_value"))
    if not isinstance(objective, dict) or not isinstance(variables, dict) or reported is None:
        return check(
            "objective_recomputation", "unsupported",
            "objective, numeric variables, or numeric reported objective is unavailable",
            sources, ["spec", "solution"],
        )
    if objective.get("indexed_terms") or objective.get("term_templates"):
        return check(
            "objective_recomputation", "unsupported",
            "indexed or template objective terms require case-specific expansion",
            sources, ["spec", "solution"],
        )
    constant = objective.get("constant", 0)
    constant_value = numeric(constant)
    terms = objective.get("linear_terms", [])
    if constant_value is None or not isinstance(terms, list):
        return check(
            "objective_recomputation", "unsupported",
            "objective constant or linear_terms is not machine-checkable",
            sources, ["spec", "solution"],
        )
    if not terms:
        expression = objective.get("expression")
        expression_value = numeric(expression)
        if expression is not None and expression_value != constant_value:
            return check(
                "objective_recomputation", "unsupported",
                "objective expression is not proven equivalent to the scalar constant",
                sources, ["spec", "solution"],
            )
    total = constant_value
    unsupported = []
    for index, term in enumerate(terms):
        if not isinstance(term, dict):
            unsupported.append(f"linear_terms[{index}] is not an object")
            continue
        name = term.get("variable")
        coefficient = numeric(term.get("coefficient"))
        value = numeric(variables.get(name)) if isinstance(name, str) else None
        if coefficient is None or value is None:
            unsupported.append(f"linear_terms[{index}] cannot resolve a numeric variable/coefficient")
            continue
        total += coefficient * value
    if unsupported:
        return check(
            "objective_recomputation", "unsupported",
            "objective was only partially machine-checkable",
            sources, ["spec", "solution"],
            {"linear_terms": len(terms), "unsupported_terms": len(unsupported)},
            {"unsupported": unsupported[:10]},
        )
    difference = abs(total - reported)
    allowed = tolerance * max(abs(reported), 1.0)
    evidence = {
        "reported_objective": reported,
        "recomputed_objective": total,
        "absolute_difference": difference,
        "allowed_difference": allowed,
    }
    if difference > allowed:
        return check(
            "objective_recomputation", "fail", "recomputed objective differs from solution.json",
            sources, ["spec", "solution"], {"linear_terms": len(terms)}, evidence,
        )
    return check(
        "objective_recomputation", "pass", "scalar linear objective matches solution.json",
        sources, ["spec", "solution"], {"linear_terms": len(terms)}, evidence,
    )


def evaluate_linear_constraint(
    checker_data: Any,
    variables: dict[str, Any],
) -> tuple[float | None, float | None, str | None, str | None]:
    if not isinstance(checker_data, dict) or checker_data.get("type") != "linear":
        return None, None, None, "checker is not a linear object"
    terms = checker_data.get("terms")
    sense = checker_data.get("sense")
    rhs = numeric(checker_data.get("rhs"))
    if not isinstance(terms, list) or sense not in {"<=", ">=", "=="} or rhs is None:
        return None, None, None, "linear checker lacks terms, sense, or numeric rhs"
    lhs = 0.0
    for index, term in enumerate(terms):
        if not isinstance(term, dict):
            return None, None, None, f"checker term {index} is not an object"
        name = term.get("variable")
        coefficient = numeric(term.get("coefficient"))
        value = numeric(variables.get(name)) if isinstance(name, str) else None
        if coefficient is None or value is None:
            return None, None, None, f"checker term {index} cannot resolve a numeric variable/coefficient"
        lhs += coefficient * value
    residual = lhs - rhs
    if sense == "<=":
        satisfied = residual <= 1e-6
    elif sense == ">=":
        satisfied = residual >= -1e-6
    else:
        satisfied = abs(residual) <= 1e-6
    return lhs, residual, "pass" if satisfied else "fail", None


def constraint_check(
    spec: Any,
    solution: Any,
    sources: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(spec, dict) or not isinstance(solution, dict):
        return check(
            "linear_constraints", "unsupported", "spec or solution is not a JSON object",
            sources, ["spec", "solution"],
        )
    if solution.get("has_incumbent") is not True:
        return check(
            "linear_constraints", "not_applicable", "no incumbent is available",
            sources, ["spec", "solution"],
        )
    variables = solution.get("variables")
    constraints = spec.get("constraints")
    indexed = spec.get("indexed_constraints", [])
    if not isinstance(variables, dict) or not isinstance(constraints, list) or not isinstance(indexed, list):
        return check(
            "linear_constraints", "unsupported", "constraint or variable representation is unsupported",
            sources, ["spec", "solution"],
        )
    if not constraints and not indexed:
        return check(
            "linear_constraints", "not_applicable", "spec declares no constraints",
            sources, ["spec", "solution"],
        )
    rows = []
    unsupported = []
    for index, constraint in enumerate(constraints):
        if not isinstance(constraint, dict):
            unsupported.append(f"constraints[{index}] is not an object")
            continue
        lhs, residual, status, error = evaluate_linear_constraint(
            constraint.get("checker"), variables
        )
        name = constraint.get("name", f"constraint_{index}")
        if error:
            unsupported.append(f"{name}: {error}")
            continue
        rows.append(
            {
                "name": name,
                "status": status,
                "lhs": lhs,
                "residual": residual,
            }
        )
    if indexed:
        unsupported.append("indexed constraints require case-specific expansion")
    failed = [row for row in rows if row["status"] == "fail"]
    coverage = {
        "scalar_constraints": len(constraints),
        "checked_constraints": len(rows),
        "indexed_constraints": len(indexed),
        "unsupported_items": len(unsupported),
    }
    if failed:
        return check(
            "linear_constraints", "fail", "machine-checkable constraint violations detected",
            sources, ["spec", "solution"], coverage,
            {"violations": failed[:10], "unsupported": unsupported[:10]},
        )
    if unsupported:
        return check(
            "linear_constraints", "unsupported", "constraint coverage is incomplete",
            sources, ["spec", "solution"], coverage,
            {"checked": rows[:10], "unsupported": unsupported[:10]},
        )
    return check(
        "linear_constraints", "pass", "all scalar linear constraints are satisfied",
        sources, ["spec", "solution"], coverage, {"checked": rows[:10]},
    )


def known_optimal_check(
    manifest: Any,
    solution: Any,
    sources: dict[str, dict[str, Any]],
    tolerance: float,
) -> dict[str, Any]:
    if not isinstance(manifest, dict) or not isinstance(solution, dict):
        return check(
            "known_optimal", "unsupported", "manifest or solution is not a JSON object",
            sources, ["manifest", "solution"],
        )
    if solution.get("has_incumbent") is not True:
        return check(
            "known_optimal", "not_applicable", "no incumbent is available",
            sources, ["manifest", "solution"],
        )
    known = None
    objects = manifest.get("data_objects")
    if isinstance(objects, dict):
        for name in sorted(objects):
            item = objects[name]
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            for location, value in ((name, item), (f"{name}.metadata", metadata)):
                for field in ("known_optimal_value", "optimal_value", "best_known_value"):
                    candidate = numeric(value.get(field))
                    if candidate is not None:
                        known = (location, field, candidate)
                        break
                if known:
                    break
            if known:
                break
    if known is None:
        return check(
            "known_optimal", "not_applicable", "manifest contains no numeric known-optimal metadata",
            sources, ["manifest", "solution"],
        )
    reported = numeric(solution.get("objective_value"))
    if reported is None:
        return check(
            "known_optimal", "unsupported", "known optimum exists but objective_value is not numeric",
            sources, ["manifest", "solution"],
        )
    location, field, expected = known
    difference = abs(reported - expected)
    allowed = tolerance * max(abs(expected), 1.0)
    evidence = {"location": location, "field": field, "known_value": expected, "reported": reported}
    if difference <= allowed:
        return check(
            "known_optimal", "pass", "reported objective matches known-optimal metadata",
            sources, ["manifest", "solution"], evidence=evidence,
        )
    if solution.get("status") == "optimal":
        return check(
            "known_optimal", "fail", "optimal solution conflicts with known-optimal metadata",
            sources, ["manifest", "solution"], evidence=evidence,
        )
    return check(
        "known_optimal", "unsupported", "feasible objective differs from benchmark and needs interpretation",
        sources, ["manifest", "solution"], evidence=evidence,
    )


def execution_record_check(
    execution_record: Any,
    public_code: Path,
    public_data_manifest: Path | None,
    workspace: Path,
    sources: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    source_names = ["execution_record", "public_code"]
    if public_data_manifest is not None:
        source_names.append("public_data_manifest")
    if not isinstance(execution_record, dict):
        return check(
            "execution_record", "fail", "execution_record.json must be an object",
            sources, source_names,
        )
    expected_entrypoint = relative_to_workspace(public_code, workspace)
    errors = []
    if execution_record.get("entrypoint") != expected_entrypoint:
        errors.append("entrypoint does not identify the supplied public code")
    if execution_record.get("entrypoint_sha256") != sources["public_code"]["sha256"]:
        errors.append("entrypoint_sha256 does not match current public code")
    expected_data_hash = (
        sources["public_data_manifest"]["sha256"]
        if public_data_manifest is not None and public_data_manifest.is_file()
        else "no_manifest"
    )
    if execution_record.get("data_snapshot_sha256") != expected_data_hash:
        errors.append("data_snapshot_sha256 does not match current public data manifest")
    if errors:
        return check(
            "execution_record", "fail", "; ".join(errors), sources, source_names,
            evidence={"expected_entrypoint": expected_entrypoint, "expected_data_hash": expected_data_hash},
        )
    return check(
        "execution_record", "pass", "entrypoint and recorded hashes match current public artifacts",
        sources, source_names,
        evidence={"entrypoint": expected_entrypoint, "data_snapshot_sha256": expected_data_hash},
    )


def public_code_check(
    public_code: Path,
    workspace: Path,
    sources: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not public_code.is_file():
        return check(
            "public_code_static", "fail", "public run.py is missing", sources, ["public_code"]
        )
    text = public_code.read_text(encoding="utf-8", errors="replace")
    if ".optagent" in text:
        return check(
            "public_code_static", "fail", "public code references internal .optagent artifacts",
            sources, ["public_code"],
        )
    workspace_text = str(workspace)
    if workspace_text and workspace_text in text:
        return check(
            "public_code_static", "fail", "public code embeds the current workspace path",
            sources, ["public_code"],
        )
    if not any(pattern in text for pattern in PATH_ANCHOR_PATTERNS):
        return check(
            "public_code_static", "unsupported",
            "no recognized file-relative path anchor was found; inspect portability manually",
            sources, ["public_code"],
        )
    return check(
        "public_code_static", "pass", "no internal dependency or current workspace path was detected",
        sources, ["public_code"],
    )


def solver_status_evidence_check(
    solution: Any,
    solver_log: Path | None,
    sources: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    source_names = ["solution"] + (["solver_log"] if solver_log is not None else [])
    if not isinstance(solution, dict):
        return check(
            "solver_status_evidence", "unsupported", "solution schema is unavailable",
            sources, source_names,
        )
    if solver_log is None:
        return check(
            "solver_status_evidence", "unsupported", "solver log was not supplied",
            sources, source_names,
        )
    if not solver_log.is_file():
        return check(
            "solver_status_evidence", "fail", "supplied solver log is missing",
            sources, source_names,
        )
    status = solution.get("status")
    log = solver_log.read_text(encoding="utf-8", errors="replace").lower()
    markers = {
        "optimal": ("optimal",),
        "feasible": ("feasible", "time limit"),
        "infeasible": ("infeasible",),
        "unbounded": ("unbounded",),
        "timeout": ("time limit", "timeout"),
    }
    expected = markers.get(status)
    if expected is None:
        return check(
            "solver_status_evidence", "not_applicable",
            f"no generic solver-log marker is defined for status {status!r}",
            sources, source_names,
        )
    if any(marker in log for marker in expected):
        return check(
            "solver_status_evidence", "pass", "solver log contains a compatible status marker",
            sources, source_names, evidence={"status": status, "markers": expected},
        )
    return check(
        "solver_status_evidence", "unsupported",
        "solver log lacks a generic marker; verifier must inspect backend-specific evidence",
        sources, source_names, evidence={"status": status, "markers": expected},
    )


def save_tool_output(raw_dir: Path, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{name}.json"
    atomic_write(path, payload)
    return {"path": path.as_posix(), "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def run_tool(
    name: str,
    command: list[str],
    workspace: Path,
    raw_dir: Path,
    timeout: int,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raw = save_tool_output(raw_dir, name, {"command": command, "error": str(exc)})
        return {"name": name, "status": "environment_limited", "detail": str(exc), "raw_output": raw}
    except subprocess.TimeoutExpired as exc:
        raw = save_tool_output(
            raw_dir, name, {"command": command, "error": f"timeout after {timeout}s", "stdout": exc.stdout, "stderr": exc.stderr}
        )
        return {"name": name, "status": "environment_limited", "detail": f"timed out after {timeout}s", "raw_output": raw}

    payload = {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    raw = save_tool_output(raw_dir, name, payload)
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError:
        parsed = None
    if parsed is None:
        return {
            "name": name,
            "status": "unsupported",
            "advisory": True,
            "detail": "tool did not return JSON; inspect raw output",
            "returncode": completed.returncode,
            "raw_output": raw,
        }
    summary = parsed.get("summary") if isinstance(parsed, dict) else None
    if completed.returncode != 0:
        return {
            "name": name,
            "status": "unsupported",
            "advisory": True,
            "detail": "heuristic helper returned a non-zero status; inspect raw output before using its findings",
            "returncode": completed.returncode,
            "summary": summary,
            "raw_output": raw,
        }
    return {
        "name": name,
        "status": "pass",
        "advisory": True,
        "detail": "heuristic helper completed; its coverage is advisory",
        "returncode": completed.returncode,
        "summary": summary,
        "raw_output": raw,
    }


def summarize(checks: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {status: 0 for status in sorted(STATUSES)}
    for item in checks:
        counts[item["status"]] += 1
    if counts["fail"]:
        outcome = "fail"
    elif counts["unsupported"] or counts["environment_limited"]:
        outcome = "partial"
    elif counts["not_applicable"]:
        outcome = "limited"
    else:
        outcome = "pass"
    return {"counts": counts, "applicable_checks_outcome": outcome}


def redundancy_audit_check(
    spec: dict[str, Any] | None,
    spec_path: Path | None,
    sources: dict[str, Any],
    workspace: Path | None = None,
) -> list[dict[str, Any]]:
    """Run the advisory redundancy/precision audit (redundancy_audit.py) on the
    opt_spec and record its findings as advisory evidence. This is deterministic
    and always runs, so the verifier does not need to remember to call it.
    It never changes correctness status: findings are recorded as advisory notes.
    The spec path is passed RELATIVE to the workspace so the subprocess (running
    inside the container with the project as cwd) can resolve it."""
    if spec is None or spec_path is None:
        return [check("redundancy_audit", "not_applicable",
                      "no opt_spec to audit", sources, ["spec"])]
    checks_out = []
    raw = spec_path.parent / "precheck_raw" / "redundancy_audit.json"
    # relative spec path for the in-container subprocess
    spec_arg = str(spec_path)
    if workspace is not None:
        try:
            spec_arg = str(spec_path.relative_to(workspace))
        except ValueError:
            spec_arg = str(spec_path)
    try:
        import subprocess
        r = subprocess.run(
            [sys.executable, resolve_helper(tools_name="redundancy_audit.py"),
             "--spec", spec_arg, "--output", str(raw)],
            capture_output=True, text=True, timeout=60,
        )
        if raw.exists():
            audit = json.loads(raw.read_text(encoding="utf-8"))
            findings = audit.get("findings", [])
            if findings:
                summary = "; ".join(
                    f"{f.get('type')}({f.get('certainty','?')}):{str(f.get('message',''))[:80]}"
                    for f in findings[:5])
                checks_out.append(check(
                    "redundancy_audit", "info",
                    f"advisory redundancy/precision findings: {summary}",
                    sources, ["spec"]))
            else:
                checks_out.append(check(
                    "redundancy_audit", "pass",
                    "no redundancy/precision findings", sources, ["spec"]))
        else:
            checks_out.append(check(
                "redundancy_audit", "not_applicable",
                f"redundancy audit produced no output: {r.stderr[:120] or r.stdout[:120]}",
                sources, ["spec"]))
    except Exception as exc:  # noqa: BLE001
        checks_out.append(check(
            "redundancy_audit", "not_applicable",
            f"redundancy audit failed: {exc}", sources, ["spec"]))
    return checks_out


def bundle(args: argparse.Namespace) -> dict[str, Any]:
    paths = {
        "spec": Path(args.spec),
        "manifest": Path(args.manifest),
        "solution": Path(args.solution),
        "execution_record": Path(args.execution_record),
        "public_code": Path(args.public_code),
    }
    if args.public_data_manifest:
        paths["public_data_manifest"] = Path(args.public_data_manifest)
    if args.solver_log:
        paths["solver_log"] = Path(args.solver_log)
    workspace = infer_workspace(list(paths.values()))
    sources = {name: artifact_info(path, workspace) for name, path in paths.items()}
    output = Path(args.output)
    raw_dir = output.parent / "precheck_raw"
    parsed: dict[str, Any | None] = {}
    errors: dict[str, str | None] = {}
    for name in ("spec", "manifest", "solution", "execution_record"):
        parsed[name], errors[name] = load_json(paths[name])

    checks = []
    for name in ("spec", "manifest", "solution", "execution_record", "public_code"):
        if not sources[name]["exists"]:
            checks.append(check(
                f"{name}_exists", "fail", f"required artifact is missing: {sources[name]['path']}",
                sources, [name],
            ))
        elif name in errors and errors[name]:
            checks.append(check(
                f"{name}_json", "fail", errors[name] or "invalid JSON", sources, [name]
            ))
    if "public_data_manifest" in sources and not sources["public_data_manifest"]["exists"]:
        checks.append(check(
            "public_data_manifest_exists", "fail", "supplied public data manifest is missing",
            sources, ["public_data_manifest"],
        ))

    checks.append(solution_schema_check(parsed["solution"], sources))
    checks.append(variable_domain_check(parsed["spec"], parsed["solution"], sources))
    checks.append(objective_check(parsed["spec"], parsed["solution"], sources, args.tolerance))
    checks.append(constraint_check(parsed["spec"], parsed["solution"], sources))
    checks.append(known_optimal_check(parsed["manifest"], parsed["solution"], sources, args.tolerance))
    checks.append(solver_status_evidence_check(
        parsed["solution"], paths.get("solver_log"), sources
    ))
    checks.append(
        execution_record_check(
            parsed["execution_record"],
            paths["public_code"],
            paths.get("public_data_manifest"),
            workspace,
            sources,
        )
    )
    checks.append(public_code_check(paths["public_code"], workspace, sources))
    checks.extend(redundancy_audit_check(parsed["spec"], paths["spec"], sources, workspace))
    checks.append(check(
        "managed_runtime_probes", "not_applicable",
        "bundle intentionally does not run project code; verifier must use managed python probes",
        sources, ["public_code"],
    ))

    tool_runs = []
    if all(sources[name]["exists"] for name in ("spec", "manifest", "public_code")):
        tool_runs.append(run_tool(
            "model_auditor",
            [args.tool_python, args.model_auditor, "--spec", str(paths["spec"].resolve()), "--code", str(paths["public_code"].resolve()), "--manifest", str(paths["manifest"].resolve())],
            workspace, raw_dir, args.tool_timeout_sec,
        ))
    if all(sources[name]["exists"] for name in ("spec", "manifest", "solution")):
        tool_runs.append(run_tool(
            "solution_checker",
            [args.tool_python, args.solution_checker, "--solution", str(paths["solution"].resolve()), "--spec", str(paths["spec"].resolve()), "--manifest", str(paths["manifest"].resolve())],
            workspace, raw_dir, args.tool_timeout_sec,
        ))

    result = {
        "version": VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "authoritative": False,
        "workspace": str(workspace),
        "sources": sources,
        "checks": checks,
        "summary": summarize(checks),
        "tool_runs": tool_runs,
        "limitations": [
            "Problem-to-Model semantics, indexed/nonlinear constraints, and unresolved evidence conflicts remain verifier responsibilities.",
            "A status other than pass is not evidence of success and must be represented truthfully in diagnosis.json.",
        ],
    }
    atomic_write(output, result)
    return result


def main() -> int:
    try:
        args = parse_args()
        result = bundle(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"version": VERSION, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"output": str(Path(args.output)), "summary": result["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
