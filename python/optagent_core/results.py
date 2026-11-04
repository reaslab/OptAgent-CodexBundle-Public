"""Highs result normalization and full Pyomo witness checks."""

from __future__ import annotations

import math
import platform
import sys
import time
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from typing import Any, Callable, Iterable, Mapping, Sequence

from .contracts import ContractError


ViolationSink = Callable[[Mapping[str, Any]], None]

_EXPECTED_PLATFORM = {
    "os": "Linux",
    "architecture": "x86_64/amd64",
    "python_implementation": "CPython",
    "python_version": "3.12.x",
}

_OBJECTIVE_VALUE_FIELDS = {"component", "sense", "unit", "value"}
_OBJECTIVE_VALUE_REL_TOLERANCE = 1e-8
_OBJECTIVE_VALUE_ABS_TOLERANCE = 1e-8
_NATIVE_CP_EXACT_INTEGER_LIMIT = (1 << 53) - 1
NATIVE_CP_SAT_METHOD = "core_cp_sat_response"
NATIVE_CP_SAT_SCOPE = {
    "solver_point": "accepted_by_same_cp_solver_invocation",
    "response_vector": "complete_int64_vector_mechanically_checked",
    "variable_domains": "checked_against_input_model_proto",
    "typed_mapping": "checked_as_explicit_proto_index_projection",
    "objective": "integer_proto_recomputed_exactly",
    "constraints": "not_independently_rechecked_by_core",
}
_DIRECT_VARIABLE_FIELDS = {"component", "key", "domain", "value"}
_DIRECT_WITNESS_FIELDS = {
    "input_digest", "instance_key", "instance_binding_digest", "variables", "objective_values"
}
_DIRECT_WITNESS_ORIGIN = "direct_python"


def direct_witness_from_output(
    value: Any,
    objective_plan: Mapping[str, Any],
    *,
    input_digest: str,
    instance_key: Sequence[str | int],
    instance_binding_digest: str,
) -> dict[str, Any] | None:
    """Validate one solve(bundle) point and attach runtime-owned witness identity."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"variables", "objective_values"}:
        raise ContractError(
            "output_invalid",
            "direct solve must return None or variables and objective_values",
            pointer="/candidate/model/solve",
        )
    variables = value.get("variables")
    if not isinstance(variables, list):
        raise ContractError("output_invalid", "direct variables must be an array", pointer="/candidate/model/solve/variables")
    normalized_variables: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str | int, ...]]] = set()
    for index, row in enumerate(variables):
        pointer = f"/candidate/model/solve/variables/{index}"
        if not isinstance(row, Mapping) or set(row) != _DIRECT_VARIABLE_FIELDS:
            raise ContractError("output_invalid", "direct variable row fields are invalid", pointer=pointer)
        component = row.get("component")
        key = row.get("key")
        domain = row.get("domain")
        raw_value = row.get("value")
        if not isinstance(component, str) or not component:
            raise ContractError("output_invalid", "direct variable component is invalid", pointer=f"{pointer}/component")
        if not isinstance(key, list) or any(type(part) not in (str, int) for part in key):
            raise ContractError("output_invalid", "direct variable key must be a typed string/integer array", pointer=f"{pointer}/key")
        identity = (component, tuple(key))
        if identity in seen:
            raise ContractError("output_invalid", "direct point repeats a variable key", pointer=pointer)
        seen.add(identity)
        if domain is not None and not isinstance(domain, str):
            raise ContractError("output_invalid", "direct variable domain must be a string or null", pointer=f"{pointer}/domain")
        numeric = _finite(raw_value)
        if numeric is None:
            raise ContractError("output_invalid", "direct variable value must be finite", pointer=f"{pointer}/value")
        normalized_variables.append({
            "component": component,
            "key": list(key),
            "domain": domain,
            "value": numeric,
            "origin": _DIRECT_WITNESS_ORIGIN,
        })
    objective_rows = _objective_value_rows(value.get("objective_values"))
    mode = objective_plan.get("mode") if isinstance(objective_plan, Mapping) else None
    if mode == "feasibility":
        if set(objective_plan) != {"mode", "preference_note"} or objective_rows:
            raise ContractError("output_invalid", "direct feasibility result must not contain an objective", pointer="/candidate/model/solve/objective_values")
    elif mode == "scalar":
        if set(objective_plan) != {"mode", "objective_component", "sense", "unit", "preference_note"}:
            raise ContractError("output_invalid", "direct scalar objective plan is invalid", pointer="/candidate/objective_plan")
        expected = (objective_plan.get("objective_component"), objective_plan.get("sense"), objective_plan.get("unit"))
        if len(objective_rows) != 1 or tuple(objective_rows[0][field] for field in ("component", "sense", "unit")) != expected:
            raise ContractError("output_invalid", "direct objective identity does not match the frozen objective plan", pointer="/candidate/model/solve/objective_values")
    else:
        raise ContractError("capability_uncovered", "direct_python supports scalar and feasibility objective plans only", pointer="/candidate/objective_plan/mode")
    return {
        "input_digest": input_digest,
        "instance_key": list(instance_key),
        "instance_binding_digest": instance_binding_digest,
        "variables": normalized_variables,
        "objective_values": list(objective_rows),
    }


def validate_direct_witness_record(
    value: Any,
    objective_plan: Mapping[str, Any],
    *,
    input_digest: str | None = None,
    instance_key: Sequence[str | int] | None = None,
    instance_binding_digest: str | None = None,
) -> dict[str, Any]:
    """Validate a persisted direct point without solving or changing its values."""
    if not isinstance(value, Mapping) or set(value) != _DIRECT_WITNESS_FIELDS:
        raise ContractError("output_invalid", "saved direct witness fields are invalid", pointer="/witness")
    digest = value.get("input_digest")
    binding = value.get("instance_binding_digest")
    key = value.get("instance_key")
    if not isinstance(digest, str) or not digest or not isinstance(binding, str) or not binding:
        raise ContractError("output_invalid", "saved direct witness identity is invalid", pointer="/witness")
    if not isinstance(key, list) or any(type(part) not in (str, int) for part in key):
        raise ContractError("output_invalid", "saved direct witness instance key is invalid", pointer="/witness/instance_key")
    if input_digest is not None and digest != input_digest:
        raise ContractError("artifact_changed", "saved direct witness input identity differs from its Result", pointer="/witness/input_digest")
    if instance_key is not None and list(key) != list(instance_key):
        raise ContractError("artifact_changed", "saved direct witness instance key differs from its Result", pointer="/witness/instance_key")
    if instance_binding_digest is not None and binding != instance_binding_digest:
        raise ContractError("artifact_changed", "saved direct witness binding differs from its Result", pointer="/witness/instance_binding_digest")
    raw_variables = value.get("variables")
    if not isinstance(raw_variables, list):
        raise ContractError("output_invalid", "saved direct witness variables must be an array", pointer="/witness/variables")
    stripped_variables: list[dict[str, Any]] = []
    for index, row in enumerate(raw_variables):
        pointer = f"/witness/variables/{index}"
        if not isinstance(row, Mapping) or set(row) != _DIRECT_VARIABLE_FIELDS | {"origin"} or row.get("origin") != _DIRECT_WITNESS_ORIGIN:
            raise ContractError("output_invalid", "saved direct witness variable fields or origin are invalid", pointer=pointer)
        stripped_variables.append({field: row[field] for field in _DIRECT_VARIABLE_FIELDS})
    normalized = direct_witness_from_output(
        {"variables": stripped_variables, "objective_values": value.get("objective_values")},
        objective_plan,
        input_digest=digest,
        instance_key=key,
        instance_binding_digest=binding,
    )
    if normalized is None:
        raise ContractError("output_invalid", "saved direct witness cannot be empty sentinel", pointer="/witness")
    return normalized


def business_check_is_fully_passed(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("status") == "pass"
        and value.get("violations") == []
        and value.get("uncovered") == []
    )


@dataclass(frozen=True)
class ObjectiveSpec:
    component: str | None
    sense: str | None
    unit: str | None
    expression: Any | None
    layer: int | None


@dataclass(frozen=True)
class CandidateValues:
    values: Mapping[Any, float]
    witness_variables: tuple[Mapping[str, Any], ...]
    unavailable_variables: tuple[Any, ...]
    required: int
    present: int
    completed: int
    missing: int
    variable_failures: int
    variable_violations: int
    uncovered: tuple[str, ...]
    has_active_primal_gap: bool


@dataclass(frozen=True)
class CheckedModel:
    record: Mapping[str, Any]
    witness_variables: tuple[Mapping[str, Any], ...] | None
    objective_values: tuple[Mapping[str, Any], ...]
    candidate_complete: bool
    raw_vector_complete: bool


@dataclass(frozen=True)
class HighsObservation:
    record: Mapping[str, Any]
    backend: Mapping[str, Any]
    termination: str
    raw_status: str
    solver_invoked: bool
    solver_returned: bool
    primals: Mapping[Any, Any]
    solver_incumbent_objective: float | None
    bound_value: float | None
    nonfinite_fields: Mapping[str, str]
    solve_wall_seconds: float
    error: str | None


def objective_values_match(
    left: Sequence[Mapping[str, Any]],
    right: Sequence[Mapping[str, Any]],
) -> bool:
    """Validate objective row identity and compare independently checked values.

    Row counts, order, exact fields, metadata identity, and numeric types are
    part of the contract. Only finite non-boolean numeric values receive the
    worker's established comparison tolerance. A legal numeric mismatch is
    data, so it returns False; malformed rows raise ContractError.
    """
    left_rows = _objective_value_rows(left)
    right_rows = _objective_value_rows(right)
    if len(left_rows) != len(right_rows):
        raise ContractError("output_invalid", "objective evidence row counts differ")

    equal = True
    for index, (left_row, right_row) in enumerate(zip(left_rows, right_rows, strict=True)):
        for field in ("component", "sense", "unit"):
            if left_row[field] != right_row[field]:
                raise ContractError(
                    "output_invalid",
                    "objective evidence row identity or order differs",
                    details={"row": index, "field": field},
                )
        if not math.isclose(
            left_row["value"],
            right_row["value"],
            rel_tol=_OBJECTIVE_VALUE_REL_TOLERANCE,
            abs_tol=_OBJECTIVE_VALUE_ABS_TOLERANCE,
        ):
            equal = False
    return equal


def objective_values_exactly_match(
    left: Sequence[Mapping[str, Any]],
    right: Sequence[Mapping[str, Any]],
) -> bool:
    """Compare objective identities and values exactly for native integer targets."""
    left_rows = _objective_value_rows(left)
    right_rows = _objective_value_rows(right)
    if len(left_rows) != len(right_rows):
        raise ContractError("output_invalid", "objective evidence row counts differ")
    return all(
        all(left_row[field] == right_row[field] for field in ("component", "sense", "unit"))
        and left_row["value"] == right_row["value"]
        for left_row, right_row in zip(left_rows, right_rows, strict=True)
    )


def _objective_value_rows(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        raise ContractError("output_invalid", "objective evidence must be an array")
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping) or set(row) != _OBJECTIVE_VALUE_FIELDS:
            raise ContractError(
                "output_invalid",
                "objective evidence row fields are invalid",
                details={"row": index},
            )
        component = row.get("component")
        sense = row.get("sense")
        unit = row.get("unit")
        raw_number = row.get("value")
        if (
            not isinstance(component, str)
            or not component
            or not isinstance(sense, str)
            or sense not in {"min", "max"}
            or not isinstance(unit, str)
            or not unit
            or not isinstance(raw_number, (int, float))
            or isinstance(raw_number, bool)
        ):
            raise ContractError(
                "output_invalid",
                "objective evidence row metadata or value type is invalid",
                details={"row": index},
            )
        try:
            number = float(raw_number)
        except (OverflowError, ValueError) as exc:
            raise ContractError(
                "output_invalid",
                "objective evidence value is outside the finite numeric range",
                details={"row": index},
            ) from exc
        if not math.isfinite(number):
            raise ContractError(
                "output_invalid",
                "objective evidence value must be finite",
                details={"row": index},
            )
        rows.append({"component": component, "sense": sense, "unit": unit, "value": number})
    return tuple(rows)


def _value(expr: Any) -> Any:
    from pyomo.environ import value

    return value(expr, exception=False)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def checked_global_bound_gap(
    bound_value: Any,
    sense: str,
    objective_value: Any,
) -> tuple[float, float | None] | None:
    """Return gap values only for a finite global bound on its legal side.

    This helper deliberately uses strict numeric ordering and never adjusts a
    native bound. Raw observations and native evidence remain unchanged.
    """
    bound = _finite(bound_value)
    objective = _finite(objective_value)
    if bound is None or objective is None or sense not in {"min", "max"}:
        return None
    if sense == "min" and bound > objective or sense == "max" and bound < objective:
        return None
    absolute = abs(objective - bound)
    if not math.isfinite(absolute):
        return None, None
    relative = absolute / abs(objective) if objective != 0 else None
    if relative is not None and not math.isfinite(relative):
        relative = None
    return absolute, relative


def checked_lexicographic_lock(
    *,
    bound_value: Any,
    sense: str,
    objective_value: Any,
    absolute_tolerance: Any,
    relative_tolerance: Any,
    stored_absolute_gap: Any,
) -> dict[str, Any] | None:
    """Return a valid bounded lexicographic restriction, or None if unsupported."""
    objective = _finite(objective_value)
    absolute = _finite(absolute_tolerance)
    relative = _finite(relative_tolerance)
    stated_gap = _finite(stored_absolute_gap)
    bound = _finite(bound_value)
    if objective is None or bound is None or absolute is None or relative is None or stated_gap is None:
        return None
    if absolute < 0 or relative < 0 or stated_gap < 0 or sense not in {"min", "max"}:
        return None
    checked_gap = checked_global_bound_gap(bound, sense, objective)
    if checked_gap is None or checked_gap[0] != stated_gap:
        return None
    relative_limit = relative * abs(objective)
    if not math.isfinite(relative_limit):
        return None
    tolerance = max(absolute, relative_limit)
    if not math.isfinite(tolerance) or stated_gap > tolerance:
        return None
    restriction_value = bound + tolerance if sense == "min" else bound - tolerance
    if not math.isfinite(restriction_value):
        return None
    return {
        "sense": sense,
        "objective": objective,
        "global_bound": bound,
        "absolute_gap": stated_gap,
        "declared_tolerance": tolerance,
        "restriction_operator": "<=" if sense == "min" else ">=",
        "restriction_value": restriction_value,
        "scope": "subsequent layers are optimized subject to this previous-layer restriction",
    }


def _enum_name(value: Any) -> str:
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    text = str(value)
    return text.rsplit(".", 1)[-1]


def _lookup_primal(primals: Mapping[Any, Any], variable: Any) -> tuple[bool, Any]:
    """Read an APPSI ComponentMap while tolerating an ordinary empty mapping."""
    try:
        return True, primals[variable]
    except (KeyError, TypeError):
        return False, None


def _key(value: Any, *, component: str) -> list[str | int]:
    if value is None:
        return []
    values = value if isinstance(value, tuple) else (value,)
    if any(type(item) not in (str, int) for item in values):
        raise ContractError(
            "capability_uncovered",
            "model component indices must be typed strings or integers",
            details={"component": component},
        )
    return list(values)


def objective_for_layer(model: Any, plan: Mapping[str, Any], layer: int | None) -> ObjectiveSpec:
    """Select exactly the objective named by a scalar, feasibility, or lex plan."""
    from .models import is_native_cp_model_build

    if is_native_cp_model_build(model):
        proto = model.model.proto
        mapped_variable_indexes = tuple(index for _, _, index in model.variable_bindings)
        mode = plan.get("mode")
        if mode == "lexicographic":
            raise ContractError("capability_uncovered", "native CpModel supports scalar and feasibility objective plans only", pointer="/objective_plan/mode")
        try:
            has_objective = bool(proto.HasField("objective"))
            has_float_objective = bool(proto.HasField("floating_point_objective"))
        except Exception as exc:
            raise ContractError("capability_uncovered", "native CpModel objective fields are not inspectable") from exc
        if has_float_objective:
            raise ContractError("capability_uncovered", "native CpModel supports integer objectives only")
        if mode == "feasibility":
            if layer is not None:
                raise ContractError("invalid_argument", "feasibility objective has no layers")
            if set(plan) != {"mode", "preference_note"} or not isinstance(plan.get("preference_note"), str):
                raise ContractError("output_invalid", "feasibility objective plan has invalid fields")
            if has_objective:
                raise ContractError("invalid_argument", "feasibility objective plan requires a CpModel without an objective")
            validate_native_cp_sat_model_contract(
                proto,
                None,
                mapped_variable_indexes=mapped_variable_indexes,
            )
            return ObjectiveSpec(None, None, None, None, None)
        if mode != "scalar" or layer is not None:
            raise ContractError("invalid_argument", "native CpModel supports scalar or feasibility plans only", pointer="/objective_plan/mode")
        if set(plan) != {"mode", "objective_component", "sense", "unit", "preference_note"}:
            raise ContractError("output_invalid", "scalar objective plan has invalid fields")
        name = plan.get("objective_component")
        sense = plan.get("sense")
        unit = plan.get("unit")
        if not isinstance(name, str) or not name or sense not in {"min", "max"} or not isinstance(unit, str) or not unit:
            raise ContractError("invalid_argument", "objective component, sense, or unit is invalid")
        if not has_objective:
            raise ContractError("invalid_argument", "scalar objective plan requires an integer CpModel objective")
        validate_native_cp_sat_model_contract(
            proto,
            sense,
            mapped_variable_indexes=mapped_variable_indexes,
        )
        objective = proto.objective
        scale = float(objective.scaling_factor)
        scale = 1.0 if scale == 0.0 else scale
        actual_sense = "max" if scale == -1.0 else "min" if scale == 1.0 else None
        if actual_sense is None:
            raise ContractError("capability_uncovered", "native CpModel objective uses an unsupported scaling factor")
        if actual_sense != sense:
            raise ContractError("invalid_argument", "objective plan sense does not match the native CpModel objective", details={"expected": actual_sense, "provided": sense})
        return ObjectiveSpec(name, sense, unit, objective, None)

    from pyomo.environ import Objective, maximize, minimize

    mode = plan.get("mode")
    all_objectives = list(model.component_data_objects(Objective, active=None, descend_into=True))
    if mode == "feasibility":
        if layer is not None:
            raise ContractError("invalid_argument", "feasibility objective has no layers")
        if set(plan) != {"mode", "preference_note"} or not isinstance(plan.get("preference_note"), str):
            raise ContractError("output_invalid", "feasibility objective plan has invalid fields")
        for obj in all_objectives:
            obj.deactivate()
        return ObjectiveSpec(None, None, None, None, None)
    if mode == "scalar":
        if layer is not None:
            raise ContractError("invalid_argument", "scalar objective has no layers")
        if set(plan) != {"mode", "objective_component", "sense", "unit", "preference_note"}:
            raise ContractError("output_invalid", "scalar objective plan has invalid fields")
        descriptor = {
            "objective_component": plan.get("objective_component"),
            "sense": plan.get("sense"),
            "unit": plan.get("unit"),
        }
        index = None
    elif mode == "lexicographic":
        if set(plan) != {"mode", "layers", "preference_note"} or not isinstance(plan.get("layers"), list) or not plan["layers"]:
            raise ContractError("output_invalid", "lexicographic objective plan has invalid fields")
        if layer is None or isinstance(layer, bool) or not isinstance(layer, int) or not 0 <= layer < len(plan["layers"]):
            raise ContractError("invalid_argument", "lexicographic layer is out of range")
        descriptor = plan["layers"][layer]
        if not isinstance(descriptor, dict) or set(descriptor) != {
            "objective_component", "sense", "unit", "absolute_tolerance", "relative_tolerance"
        }:
            raise ContractError("output_invalid", "lexicographic objective layer has invalid fields")
        index = layer
    else:
        raise ContractError("invalid_argument", "objective plan mode is unsupported")
    name = descriptor.get("objective_component")
    sense = descriptor.get("sense")
    unit = descriptor.get("unit")
    if not isinstance(name, str) or not name or sense not in {"min", "max"} or not isinstance(unit, str) or not unit:
        raise ContractError("invalid_argument", "objective component, sense, or unit is invalid")
    matches = [obj for obj in all_objectives if obj.parent_component().name == name]
    if len(matches) != 1:
        raise ContractError(
            "invalid_argument",
            "objective plan must name exactly one scalar Pyomo objective",
            details={"objective_component": name, "matches": len(matches)},
        )
    selected = matches[0]
    actual_sense = "min" if selected.sense == minimize else "max" if selected.sense == maximize else None
    if actual_sense != sense:
        raise ContractError(
            "invalid_argument",
            "objective plan sense does not match the Pyomo objective",
            details={"objective_component": name, "expected": actual_sense, "provided": sense},
        )
    for obj in all_objectives:
        obj.deactivate()
    selected.activate()
    return ObjectiveSpec(name, sense, unit, selected.expr, index)


def _domain_info(var: Any) -> tuple[float | None, float | None, bool, bool, str | None]:
    """Return standard-domain limits, integrality, and a portable label."""
    try:
        domain_lower, domain_upper = var.domain.bounds()
    except Exception:
        return None, None, False, False, None
    low = _finite(_value(domain_lower)) if domain_lower is not None else None
    high = _finite(_value(domain_upper)) if domain_upper is not None else None
    is_binary = bool(var.is_binary())
    is_integer = bool(var.is_integer()) or is_binary
    is_continuous = bool(var.is_continuous())
    if is_binary:
        low = 0.0 if low is None else max(low, 0.0)
        high = 1.0 if high is None else min(high, 1.0)
        label = "binary"
    elif is_integer:
        label = "integer"
    elif is_continuous:
        label = "continuous"
    else:
        label = None
    return low, high, is_integer, is_binary, label


def _bounds(var: Any) -> tuple[float | None, float | None, str | None]:
    domain_lower, domain_upper, is_integer, is_binary, label = _domain_info(var)
    raw_lower = _finite(_value(var.lb)) if var.lb is not None else None
    raw_upper = _finite(_value(var.ub)) if var.ub is not None else None
    if var.lb is not None and raw_lower is None:
        raise ContractError("capability_uncovered", "variable lower bound is not a finite constant", details={"component": var.name})
    if var.ub is not None and raw_upper is None:
        raise ContractError("capability_uncovered", "variable upper bound is not a finite constant", details={"component": var.name})
    lower = max(value for value in (domain_lower, raw_lower) if value is not None) if domain_lower is not None or raw_lower is not None else None
    upper = min(value for value in (domain_upper, raw_upper) if value is not None) if domain_upper is not None or raw_upper is not None else None
    if is_binary:
        lower = max(0.0, lower) if lower is not None else 0.0
        upper = min(1.0, upper) if upper is not None else 1.0
    return lower, upper, label


def empty_domain(var: Any) -> tuple[bool, Mapping[str, Any] | None]:
    """Prove only standard real/integer/binary bound contradictions."""
    low, high, label = _bounds(var)
    if label is None:
        return False, {"component": var.name, "reason": "custom_domain"}
    if low is not None and high is not None and low > high:
        return True, {"component": var.name, "lower": low, "upper": high, "reason": "lower_exceeds_upper"}
    if label in {"integer", "binary"} and low is not None and high is not None and math.ceil(low) > math.floor(high):
        return True, {"component": var.name, "lower": low, "upper": high, "reason": "empty_integer_interval"}
    return False, None


def _deterministic_value(var: Any) -> tuple[float | None, str | None]:
    low, high, label = _bounds(var)
    if label is None:
        return None, None
    if low is not None and high is not None and low > high:
        return None, label
    if label in {"integer", "binary"}:
        min_int = math.ceil(low) if low is not None else None
        max_int = math.floor(high) if high is not None else None
        if min_int is not None and max_int is not None and min_int > max_int:
            return None, label
        if min_int is not None and min_int > 0:
            selected = min_int
        elif max_int is not None and max_int < 0:
            selected = max_int
        else:
            selected = 0
    else:
        selected = 0.0
        if low is not None and selected < low:
            selected = low
        if high is not None and selected > high:
            selected = high
    if low is not None and selected < low or high is not None and selected > high:
        return None, label
    return float(selected), label


def collect_candidate_values(
    model: Any,
    primals: Mapping[Any, Any],
    objective_spec: ObjectiveSpec,
    feasibility_tolerance: float,
    integrality_tolerance: float,
    violation_sink: ViolationSink | None = None,
) -> CandidateValues:
    from pyomo.common.collections import ComponentMap, ComponentSet
    from pyomo.environ import Constraint, Objective, Var
    from pyomo.core.expr.visitor import identify_variables

    active_vars = list(model.component_data_objects(Var, active=True, descend_into=True))
    referenced = ComponentSet()
    for constraint in model.component_data_objects(Constraint, active=True, descend_into=True):
        referenced.update(identify_variables(constraint.body, include_fixed=True))
    for objective in model.component_data_objects(Objective, active=True, descend_into=True):
        referenced.update(identify_variables(objective.expr, include_fixed=True))

    # Pyomo NumericValue overloads equality and is unhashable in supported
    # releases; identity collections are required for variable membership.
    assignments = ComponentMap()
    unavailable: list[Any] = []
    witness: list[Mapping[str, Any]] = []
    present = completed = missing = failures = violations = 0
    uncovered: list[str] = []
    uncovered_seen: set[str] = set()

    def mark_uncovered(reason: str) -> bool:
        if reason in uncovered_seen:
            return False
        uncovered_seen.add(reason)
        uncovered.append(reason)
        return True

    active_primal_gap = False
    for var in active_vars:
        component = var.parent_component().name
        key = _key(var.index(), component=component)
        low, high, domain = _bounds(var)
        coverage_errors: list[str] = []
        origin: str
        has_primal, primal_value = _lookup_primal(primals, var) if not var.fixed else (False, None)
        if var.fixed:
            raw = _finite(var.value)
            origin = "fixed_model_value"
            if raw is None:
                missing += 1
            else:
                completed += 1
        elif has_primal:
            raw = _finite(primal_value)
            origin = "solver_primal"
            if raw is not None:
                present += 1
            else:
                missing += 1
                active_primal_gap = True
        elif var in referenced:
            raw = None
            origin = "solver_primal"
            active_primal_gap = True
            missing += 1
        else:
            raw, domain = _deterministic_value(var)
            origin = "deterministic_completion"
            if raw is not None:
                completed += 1
            else:
                missing += 1
                active_primal_gap = True
                if mark_uncovered(f"unused_variable_domain:{component}"):
                    coverage_errors.append("missing_or_nonfinite_value")

        checked_value = raw
        if (
            raw is not None
            and origin == "solver_primal"
            and domain in {"integer", "binary"}
        ):
            rounded = float(round(raw))
            if abs(raw - rounded) <= integrality_tolerance:
                # Keep the solver's raw vector untouched. All subsequent
                # variable, row, and objective checks use this checked copy.
                checked_value = rounded

        if checked_value is None:
            unavailable.append(var)

        error: str | None = None
        if domain is None:
            error = "unsupported_domain"
            if mark_uncovered(f"unsupported_domain:{component}"):
                coverage_errors.append("unsupported_domain")
        elif checked_value is None:
            error = "missing_or_nonfinite_value"
            if origin == "solver_primal" and var in referenced:
                if mark_uncovered(f"active_variable_missing_primal:{component}"):
                    coverage_errors.append("missing_or_nonfinite_value")
            elif origin == "fixed_model_value":
                if mark_uncovered(f"fixed_variable_value_missing:{component}"):
                    coverage_errors.append("missing_or_nonfinite_value")
        else:
            if low is not None and checked_value < low - feasibility_tolerance:
                error = "lower_bound_violation"
            elif high is not None and checked_value > high + feasibility_tolerance:
                error = "upper_bound_violation"
            elif bool(var.is_integer()) or bool(var.is_binary()):
                if abs(checked_value - round(checked_value)) > integrality_tolerance:
                    error = "integrality_violation"
        if error is not None:
            failures += 1
            if error in {"lower_bound_violation", "upper_bound_violation", "integrality_violation"}:
                violations += 1
            actual_violation = error in {
                "lower_bound_violation",
                "upper_bound_violation",
                "integrality_violation",
            }
            if violation_sink is not None and actual_violation:
                violation_sink({
                    "kind": "variable",
                    "component": component,
                    "key": key,
                    "lower": low,
                    "body": checked_value,
                    "upper": high,
                    "violation": None,
                    "error": error,
                })
            elif violation_sink is not None:
                for coverage_error in coverage_errors:
                    violation_sink({
                        "kind": "variable",
                        "component": component,
                        "key": key,
                        "lower": low,
                        "body": checked_value,
                        "upper": high,
                        "violation": None,
                        "error": coverage_error,
                    })
        if checked_value is not None:
            # Keep finite but infeasible candidate values for row evaluation;
            # otherwise an active row could see a stale Var.value.
            assignments[var] = checked_value
        if error is not None:
            continue
        witness.append({
            "component": component,
            "key": key,
            "domain": domain,
            "value": checked_value,
            "origin": origin,
        })
    return CandidateValues(
        values=assignments,
        witness_variables=tuple(witness),
        unavailable_variables=tuple(unavailable),
        required=len(active_vars),
        present=present,
        completed=completed,
        missing=missing,
        variable_failures=failures,
        variable_violations=violations,
        uncovered=tuple(dict.fromkeys(uncovered)),
        has_active_primal_gap=active_primal_gap,
    )


def check_pyomo_model(
    model: Any,
    primals: Mapping[Any, Any],
    objective_spec: ObjectiveSpec,
    *,
    input_digest: str,
    instance_key: Sequence[str | int],
    instance_binding_digest: str,
    feasibility_tolerance: float,
    integrality_tolerance: float,
    violation_sink: ViolationSink | None = None,
) -> CheckedModel:
    """Check one full raw solver vector through a checked point copy.

    Supported integer and binary solver primals within the declared
    integrality tolerance are rounded in the copy before variable, row, and
    objective checks. The caller's raw primal mapping remains unchanged.
    """
    from .models import require_supported_active_components

    require_supported_active_components(model)
    from pyomo.common.collections import ComponentMap, ComponentSet
    from pyomo.core.expr.visitor import identify_variables
    from pyomo.environ import Constraint, Objective

    candidate = collect_candidate_values(
        model,
        primals,
        objective_spec,
        feasibility_tolerance,
        integrality_tolerance,
        violation_sink,
    )
    unavailable = ComponentSet(candidate.unavailable_variables)
    available = ComponentSet(candidate.values)
    old_values = ComponentMap()
    for var in candidate.values:
        old_values[var] = var.value
    for var, number in candidate.values.items():
        var.set_value(number, skip_validation=True)
    try:
        groups: dict[str, dict[str, Any]] = {}
        any_failure = candidate.variable_violations > 0
        uncovered = list(candidate.uncovered)
        uncovered_seen = set(uncovered)

        def mark_uncovered(reason: str) -> bool:
            if reason in uncovered_seen:
                return False
            uncovered_seen.add(reason)
            uncovered.append(reason)
            return True

        for constraint in model.component_data_objects(Constraint, active=True, descend_into=True):
            component = constraint.parent_component().name
            key = _key(constraint.index(), component=component)
            stats = groups.setdefault(component, {
                "component": component,
                "required": 0,
                "checked": 0,
                "failures": 0,
                "uncovered": 0,
                "max_violation": None,
                "max_violation_key": None,
            })
            stats["required"] += 1
            row_expressions = [constraint.body]
            if constraint.lower is not None:
                row_expressions.append(constraint.lower)
            if constraint.upper is not None:
                row_expressions.append(constraint.upper)
            row_variables = ComponentSet()
            for expression in row_expressions:
                row_variables.update(identify_variables(expression, include_fixed=True))
            if any(var in unavailable or var not in available for var in row_variables):
                stats["uncovered"] += 1
                first_uncovered = mark_uncovered(f"row_value_unavailable:{component}")
                if first_uncovered and violation_sink is not None:
                    violation_sink({
                        "kind": "constraint",
                        "component": component,
                        "key": key,
                        "lower": None,
                        "body": None,
                        "upper": None,
                        "violation": None,
                        "error": "row_value_unavailable",
                    })
                continue
            try:
                body = _finite(_value(constraint.body))
                lower = _finite(_value(constraint.lower)) if constraint.lower is not None else None
                upper = _finite(_value(constraint.upper)) if constraint.upper is not None else None
            except Exception:
                body = lower = upper = None
            if body is None or constraint.lower is not None and lower is None or constraint.upper is not None and upper is None:
                stats["uncovered"] += 1
                first_uncovered = mark_uncovered(f"row_value_unavailable:{component}")
                if first_uncovered and violation_sink is not None:
                    violation_sink({
                        "kind": "constraint",
                        "component": component,
                        "key": key,
                        "lower": lower,
                        "body": body,
                        "upper": upper,
                        "violation": None,
                        "error": "row_value_unavailable",
                    })
                continue
            violation = 0.0
            if lower is not None:
                violation = max(violation, lower - body)
            if upper is not None:
                violation = max(violation, body - upper)
            violation = max(0.0, violation)
            stats["checked"] += 1
            old_max = stats["max_violation"]
            if old_max is None or violation > old_max:
                stats["max_violation"] = violation
                stats["max_violation_key"] = key
            if violation > feasibility_tolerance:
                any_failure = True
                stats["failures"] += 1
                if violation_sink is not None:
                    violation_sink({
                        "kind": "constraint",
                        "component": component,
                        "key": key,
                        "lower": lower,
                        "body": body,
                        "upper": upper,
                        "violation": violation,
                        "error": "constraint_violation",
                    })

        objective_values: list[Mapping[str, Any]] = []
        if objective_spec.component is not None:
            matching = [
                item for item in model.component_data_objects(Objective, active=True, descend_into=True)
                if item.parent_component().name == objective_spec.component
            ]
            if len(matching) != 1:
                mark_uncovered(f"objective_missing_or_ambiguous:{objective_spec.component}")
            else:
                objective_variables = identify_variables(matching[0].expr, include_fixed=True)
                objective_unavailable = any(var in unavailable or var not in available for var in objective_variables)
                value = None if objective_unavailable else _finite(_value(matching[0].expr))
                if value is None:
                    mark_uncovered(f"objective_value_unavailable:{objective_spec.component}")
                else:
                    objective_values.append({
                        "component": objective_spec.component,
                        "sense": objective_spec.sense,
                        "unit": objective_spec.unit,
                        "value": value,
                    })
        if any_failure:
            status = "fail"
        elif uncovered:
            status = "uncovered"
        else:
            status = "pass"
        record = {
            "input_digest": input_digest,
            "instance_key": list(instance_key),
            "instance_binding_digest": instance_binding_digest,
            "status": status,
            "variable_coverage": {
                "required": candidate.required,
                "present": candidate.present,
                "completed": candidate.completed,
                "missing_count": candidate.missing,
            },
            "component_coverage": [groups[name] for name in sorted(groups)],
            "violations": None,
            "objective_values": objective_values,
            "tolerances": {"feasibility": feasibility_tolerance, "integrality": integrality_tolerance},
            "uncovered": sorted(set(uncovered)),
        }
        witness_variables = (
            candidate.witness_variables
            if candidate.required == len(candidate.values) and candidate.variable_failures == 0
            else None
        )
        return CheckedModel(
            record=record,
            witness_variables=witness_variables,
            objective_values=tuple(objective_values),
            candidate_complete=witness_variables is not None and status == "pass",
            raw_vector_complete=not candidate.has_active_primal_gap,
        )
    finally:
        for var, old in old_values.items():
            var.set_value(old, skip_validation=True)


def validate_native_cp_sat_model_contract(
    proto: Any,
    objective_sense: str | None,
    *,
    mapped_variable_indexes: Sequence[int] = (),
) -> dict[str, Any]:
    """Validate finite domains, mapped Witness precision, and exact integer objective form."""
    try:
        has_objective = bool(proto.HasField("objective"))
        has_float_objective = bool(proto.HasField("floating_point_objective"))
    except Exception as exc:
        raise ContractError("capability_uncovered", "native CpModel objective fields are not inspectable") from exc
    if has_float_objective:
        raise ContractError("capability_uncovered", "native cp_sat currently supports integer objectives only")
    if has_objective:
        objective = proto.objective
        raw_scale = float(objective.scaling_factor)
        scale = 1.0 if raw_scale == 0.0 else raw_scale
        if scale not in {-1.0, 1.0}:
            raise ContractError("capability_uncovered", "native CpModel objective uses an unsupported scaling factor")
        actual_sense = "max" if scale == -1.0 else "min"
        if objective_sense != actual_sense:
            raise ContractError("invalid_argument", "objective plan sense does not match the native CpModel objective")
        offset = float(objective.offset)
        if not math.isfinite(offset) or not offset.is_integer() or abs(offset) > _NATIVE_CP_EXACT_INTEGER_LIMIT:
            raise ContractError("capability_uncovered", "native integer objective offset is outside exact Witness precision")
        offset_int = int(offset)
        if len(objective.vars) != len(objective.coeffs):
            raise ContractError("capability_uncovered", "native CpModel objective variable and coefficient arrays differ")
        objective_vars = tuple(int(index) for index in objective.vars)
        objective_coeffs = tuple(int(coefficient) for coefficient in objective.coeffs)
        if any(index < 0 or index >= len(proto.variables) for index in objective_vars):
            raise ContractError("capability_uncovered", "native CpModel objective references an invalid proto index")
    else:
        if objective_sense is not None:
            raise ContractError("invalid_argument", "scalar objective plan requires an integer CpModel objective")
        actual_sense = None
        scale = 1.0
        offset_int = 0
        objective_vars = ()
        objective_coeffs = ()

    domains: list[tuple[int, ...]] = []
    for index, variable in enumerate(proto.variables):
        domain = tuple(int(value) for value in variable.domain)
        if not domain or len(domain) % 2:
            raise ContractError("capability_uncovered", "native CpModel variable has an invalid integer domain", details={"index": index})
        for position in range(0, len(domain), 2):
            low, high = domain[position : position + 2]
            if low > high:
                raise ContractError("capability_uncovered", "native CpModel variable domain has a reversed interval", details={"index": index})
        domains.append(domain)

    for index in mapped_variable_indexes:
        if type(index) is not int or index < 0 or index >= len(domains):
            raise ContractError("capability_uncovered", "native Witness mapping references an invalid proto index")
        domain = domains[index]
        if any(
            abs(domain[position]) > _NATIVE_CP_EXACT_INTEGER_LIMIT
            or abs(domain[position + 1]) > _NATIVE_CP_EXACT_INTEGER_LIMIT
            for position in range(0, len(domain), 2)
        ):
            raise ContractError(
                "capability_uncovered",
                "mapped native variable domain exceeds exact Witness precision",
                details={"index": index},
            )

    objective_activity = abs(offset_int)
    for index, coefficient in zip(objective_vars, objective_coeffs, strict=True):
        domain = domains[index]
        objective_activity += abs(coefficient) * max(abs(domain[0]), abs(domain[-1]))
        if objective_activity > _NATIVE_CP_EXACT_INTEGER_LIMIT:
            raise ContractError("capability_uncovered", "native integer objective activity exceeds exact Witness precision")
    return {
        "objective_present": has_objective,
        "objective_sense": actual_sense,
        "objective_scale": scale,
        "objective_offset": offset_int,
        "objective_vars": objective_vars,
        "objective_coeffs": objective_coeffs,
        "domains": tuple(domains),
    }


def check_native_cp_sat_model(
    native_build: Any,
    observation: Any,
    objective_spec: ObjectiveSpec,
    *,
    input_digest: str,
    instance_key: Sequence[str | int],
    instance_binding_digest: str,
    feasibility_tolerance: float,
    integrality_tolerance: float,
    native_evidence: Mapping[str, Any],
    violation_sink: ViolationSink | None = None,
) -> CheckedModel:
    """Mechanically check one Core CpSolver response without reinterpreting CP globals."""
    from .models import NativeCpModelBuild

    if not isinstance(native_build, NativeCpModelBuild):
        raise ContractError("invalid_argument", "native CP-SAT checking requires a Core-validated model envelope")
    expected_evidence = {"model_proto", "variable_bindings", "response_vector"}
    if set(native_evidence) != expected_evidence:
        raise ContractError("output_invalid", "native CP-SAT evidence refs have invalid fields")
    proto = native_build.model.proto
    model_proto_bytes = getattr(observation, "native_model_proto", None)
    try:
        current_proto = proto.SerializeToString(deterministic=True)
    except TypeError:
        current_proto = proto.SerializeToString()
    if not isinstance(model_proto_bytes, bytes) or current_proto != model_proto_bytes:
        raise ContractError("artifact_changed", "native CpModel changed after the Core solve input was frozen")
    objective_info = validate_native_cp_sat_model_contract(
        proto,
        objective_spec.sense,
        mapped_variable_indexes=tuple(index for _, _, index in native_build.variable_bindings),
    )
    variable_count = len(proto.variables)
    response_values = getattr(observation, "native_response_solution", None)
    raw_status = getattr(observation, "raw_status", None)
    solver_record = getattr(observation, "record", {})
    accepted_status = (
        raw_status in {"OPTIMAL", "FEASIBLE"}
        and isinstance(solver_record, Mapping)
        and solver_record.get("solver_invoked") is True
        and solver_record.get("solver_returned") is True
    )
    accepted_point = accepted_status and isinstance(response_values, tuple)
    vector_complete = accepted_point and len(response_values) == variable_count and all(type(value) is int for value in response_values)
    coverage = {
        "required": variable_count,
        "present": len(response_values) if isinstance(response_values, tuple) else 0,
        "completed": len(response_values) if vector_complete else 0,
        "missing_count": 0 if vector_complete else max(0, variable_count - (len(response_values) if isinstance(response_values, tuple) else 0)),
    }
    failures: list[dict[str, Any]] = []
    uncovered: list[str] = []
    witness: list[dict[str, Any]] = []
    objective_values: list[dict[str, Any]] = []
    if not accepted_status:
        uncovered.append("no_solver_feasible_point")
    elif not vector_complete:
        failures.append({"kind": "native_response", "error": "response_vector_incomplete_or_noninteger"})
    else:
        for index, value in enumerate(response_values):
            domain = objective_info["domains"][index]
            if not any(domain[position] <= value <= domain[position + 1] for position in range(0, len(domain), 2)):
                failures.append({"kind": "native_variable", "index": index, "value": value, "error": "value_outside_proto_domain"})
        for component, key, index in native_build.variable_bindings:
            value = response_values[index]
            if abs(value) > _NATIVE_CP_EXACT_INTEGER_LIMIT:
                failures.append({
                    "kind": "native_variable",
                    "component": component,
                    "key": list(key),
                    "index": index,
                    "value": value,
                    "error": "value_exceeds_exact_witness_precision",
                })
        if objective_info["objective_present"]:
            inner_value = objective_info["objective_offset"]
            for index, coefficient in zip(objective_info["objective_vars"], objective_info["objective_coeffs"], strict=True):
                inner_value += coefficient * response_values[index]
            exact_value = int(objective_info["objective_scale"] * inner_value)
            raw_incumbent = getattr(observation, "solver_incumbent_objective", None)
            if abs(exact_value) > _NATIVE_CP_EXACT_INTEGER_LIMIT or isinstance(raw_incumbent, bool) or not isinstance(raw_incumbent, (int, float)) or not math.isfinite(float(raw_incumbent)) or exact_value != raw_incumbent:
                failures.append({
                    "kind": "native_objective",
                    "component": objective_spec.component,
                    "recomputed_value": exact_value,
                    "solver_value": raw_incumbent if _finite(raw_incumbent) is not None else None,
                    "error": "integer_objective_mismatch",
                })
            else:
                objective_values.append({
                    "component": objective_spec.component,
                    "sense": objective_spec.sense,
                    "unit": objective_spec.unit,
                    "value": float(exact_value),
                })
        if not failures:
            for component, key, index in native_build.variable_bindings:
                value = response_values[index]
                domain = objective_info["domains"][index]
                label = "binary" if domain == (0, 1) else "integer"
                witness.append({
                    "component": component,
                    "key": list(key),
                    "domain": label,
                    "value": float(value),
                    "origin": "solver_primal",
                })
    status = "fail" if failures else "uncovered" if uncovered else "pass"
    if violation_sink is not None:
        for failure in failures:
            violation_sink(failure)
    record = {
        "input_digest": input_digest,
        "instance_key": list(instance_key),
        "instance_binding_digest": instance_binding_digest,
        "status": status,
        "variable_coverage": coverage,
        "component_coverage": [],
        "violations": None,
        "objective_values": objective_values,
        "tolerances": {"feasibility": feasibility_tolerance, "integrality": integrality_tolerance},
        "uncovered": sorted(set(uncovered)),
        "method": NATIVE_CP_SAT_METHOD,
        "scope": dict(NATIVE_CP_SAT_SCOPE),
        "native_evidence": {key: dict(value) for key, value in native_evidence.items()},
    }
    witness_variables = tuple(witness) if status == "pass" else None
    return CheckedModel(
        record=record,
        witness_variables=witness_variables,
        objective_values=tuple(objective_values),
        candidate_complete=status == "pass",
        raw_vector_complete=bool(vector_complete),
    )


def _require_supported_platform() -> dict[str, str]:
    operating_system = platform.system()
    architecture = platform.machine().lower()
    python_implementation = platform.python_implementation()
    python_version = platform.python_version()
    return {
        "os": operating_system,
        "architecture": architecture,
        "python_implementation": python_implementation,
        "python_version": python_version,
    }


def _backend_versions() -> dict[str, Any]:
    profile = _require_supported_platform()
    operating_system = profile["os"]
    architecture = profile["architecture"]
    python_implementation = profile["python_implementation"]
    python_version = profile["python_version"]
    try:
        __import__("numpy")
        __import__("pyomo")
        import highspy
    except ImportError as exc:
        raise ContractError("environment_unavailable", "the managed Pyomo and Highs packages are unavailable") from exc

    package_versions: dict[str, str] = {}
    for distribution in ("pyomo", "highspy", "numpy"):
        try:
            package_versions[distribution] = str(importlib_metadata.version(distribution))
        except importlib_metadata.PackageNotFoundError as exc:
            raise ContractError(
                "environment_unavailable",
                "managed solver package distribution metadata is unavailable",
                details={"distribution": distribution},
            ) from exc
    try:
        highs_version = highspy.Highs().version()
    except Exception:
        highs_version = package_versions["highspy"]
    return {
        "id": "highs",
        "version": str(highs_version),
        "adapter_version": package_versions["pyomo"],
        "python_version": python_version,
        "python_implementation": python_implementation,
        "os": operating_system,
        "architecture": architecture,
        "package_versions": package_versions,
    }


def run_highs_once(
    model: Any,
    *,
    time_limit_seconds: float,
    relative_gap: float | None,
    absolute_gap: float | None,
    threads: int,
    random_seed: int,
    feasibility_tolerance: float,
    integrality_tolerance: float,
    log_path: str | None = None,
    backend_profile: Mapping[str, Any] | None = None,
) -> HighsObservation:
    """Run the pinned APPSI Highs adapter once and preserve observed raw state."""
    # This stdlib-only gate must precede every numerical import. The worker
    # passes its already verified profile to avoid repeating package probes
    # for each lexicographic layer; direct callers still get full verification.
    for name, value in (("relative_gap", relative_gap), ("absolute_gap", absolute_gap)):
        if value is not None and (_finite(value) is None or float(value) < 0):
            raise ContractError("invalid_argument", "gap must be null or a finite nonnegative number", pointer=f"/solve_options/{name}")
    for name, value in (("feasibility_tolerance", feasibility_tolerance), ("integrality_tolerance", integrality_tolerance)):
        finite = _finite(value)
        if finite is None or finite < 1e-10:
            raise ContractError("invalid_argument", "HiGHS feasibility tolerances must be finite and at least 1e-10", pointer=f"/solve_options/{name}")
    _require_supported_platform()
    backend = dict(backend_profile) if backend_profile is not None else _backend_versions()
    # _backend_versions performs the stdlib-only OS/architecture/interpreter
    # gate before importing numpy, Pyomo, or highspy. Keep the APPSI import
    # after it so direct adapter calls fail closed on unsupported hosts too.
    from pyomo.contrib.appsi.solvers import Highs

    solver = Highs()
    solver.config.load_solution = False
    solver.config.time_limit = float(time_limit_seconds)
    solver.highs_options["threads"] = int(threads)
    solver.highs_options["random_seed"] = int(random_seed)
    requested_options: dict[str, float | None] = {
        "mip_rel_gap": None if relative_gap is None else float(relative_gap),
        "mip_abs_gap": None if absolute_gap is None else float(absolute_gap),
        "primal_feasibility_tolerance": float(feasibility_tolerance),
        "mip_feasibility_tolerance": float(integrality_tolerance),
    }
    solver.highs_options.update(
        {name: value for name, value in requested_options.items() if value is not None}
    )
    if log_path is not None:
        solver.highs_options["log_file"] = log_path
        solver.highs_options["log_to_console"] = False

    started = time.perf_counter()
    result: Any = None
    error: str | None = None
    solver_invoked = False
    option_set_statuses: dict[str, str] = {}
    effective_options: dict[str, float | None] = {name: None for name in requested_options}
    try:
        # APPSI's solve(model) would construct a new Highs object when the
        # model identity differs. Bind once, validate every requested numeric
        # option on that active object, and then solve the same model identity.
        from highspy import HighsStatus

        solver.set_instance(model)
        raw_model = getattr(solver, "_solver_model", None)
        if raw_model is None:
            raise ContractError("capability_uncovered", "APPSI did not expose its active HiGHS model")
        active_model = raw_model
        for name, requested in requested_options.items():
            if requested is None:
                continue
            status = raw_model.setOptionValue(name, requested)
            status_name = _enum_name(status)
            option_set_statuses[name] = status_name
            if status != HighsStatus.kOk:
                raise ContractError(
                    "invalid_argument",
                    "HiGHS rejected a requested numeric option",
                    details={"option": name, "status": status_name},
                )
            get_status, applied = raw_model.getOptionValue(name)
            if get_status != HighsStatus.kOk or isinstance(applied, bool) or not isinstance(applied, (int, float)) or not math.isfinite(float(applied)) or float(applied) != requested:
                raise ContractError(
                    "invalid_argument",
                    "HiGHS did not retain a requested numeric option",
                    details={"option": name, "status": _enum_name(get_status)},
                )

        # Record the actual adapter call, including a call that raises before
        # producing an APPSI result or a populated HiGHS model status.
        solver_invoked = True
        result = solver.solve(model)
    except Exception as exc:
        if isinstance(exc, ContractError):
            raise
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - started

    raw_status = "kNotset"
    raw_valid: bool | None = None
    primals: Mapping[Any, Any] = {}
    best: float | None = None
    bound: float | None = None
    nonfinite: dict[str, str] = {}
    if result is not None:
        for field, target in (("best_feasible_objective", "best"), ("best_objective_bound", "bound")):
            observed = getattr(result, field, None)
            finite = _finite(observed)
            if target == "best":
                best = finite
            else:
                bound = finite
            if observed is not None and finite is None:
                nonfinite[field] = repr(observed)
        termination = _enum_name(getattr(result, "termination_condition", "unknown"))
    else:
        termination = "error"

    try:
        raw_model = getattr(solver, "_solver_model", None)
        if raw_model is not None:
            if "active_model" in locals() and raw_model is not active_model:
                raise ContractError("capability_uncovered", "APPSI replaced the validated HiGHS model during solve")
            raw_status = _enum_name(raw_model.getModelStatus())
            solution = raw_model.getSolution()
            raw_valid = bool(solution.value_valid)
            for name, requested in requested_options.items():
                try:
                    get_status, value = raw_model.getOptionValue(name)
                except Exception:
                    get_status, value = None, None
                status_name = _enum_name(get_status) if get_status is not None else None
                if get_status == HighsStatus.kOk and type(value) in (int, float) and math.isfinite(float(value)):
                    effective_options[name] = float(value)
                    if name in {"mip_rel_gap", "mip_abs_gap"} and effective_options[name] < 0:
                        raise ContractError(
                            "capability_uncovered",
                            "HiGHS reported a negative effective gap tolerance",
                            details={"option": name, "status": status_name},
                        )
                if requested is not None and (
                    get_status != HighsStatus.kOk
                    or effective_options[name] is None
                    or effective_options[name] != requested
                ):
                    raise ContractError(
                        "capability_uncovered",
                        "HiGHS failed to report the requested effective numeric option",
                        details={"option": name, "status": status_name},
                    )
    except ContractError:
        raise
    except Exception:
        raw_valid = None
    try:
        primals = solver.get_primals()
    except Exception as exc:
        if error is None:
            error = f"get_primals:{type(exc).__name__}"

    termination_map = {
        "optimal": "optimal",
        "feasible": "feasible",
        "locallyOptimal": "local_optimal",
        "localOptimal": "local_optimal",
        "infeasible": "infeasible",
        "unbounded": "unbounded",
        "infeasibleOrUnbounded": "infeasible_or_unbounded",
        "maxTimeLimit": "time_limit",
        "maxIterations": "iteration_limit",
        "error": "error",
        "unknown": "unknown",
    }
    mapped = termination_map.get(termination, "unknown")
    if mapped == "error" and raw_status in {"kSolveError", "kPostsolveError"}:
        mapped = "numeric_error"
    if error is not None and result is None and raw_status in {"kSolveError", "kPostsolveError"}:
        mapped = "numeric_error"
    record = {
        "termination": mapped,
        "raw_status": raw_status,
        "solver_invoked": solver_invoked,
        "solver_returned": result is not None,
        "raw_solution_value_valid": raw_valid,
        "best_feasible_objective": best,
        "best_objective_bound": bound,
        "termination_condition": termination,
        "solve_wall_seconds": elapsed,
        "error": error,
        "requested_options": requested_options,
        "effective_options": effective_options,
        "option_set_statuses": option_set_statuses,
    }
    return HighsObservation(
        record=record,
        backend=backend,
        termination=mapped,
        raw_status=raw_status,
        solver_invoked=solver_invoked,
        solver_returned=result is not None,
        primals=primals,
        solver_incumbent_objective=best,
        bound_value=bound,
        nonfinite_fields=nonfinite,
        solve_wall_seconds=elapsed,
        error=error,
    )


def result_record(
    observation: Any | None,
    checked: CheckedModel,
    objective_spec: ObjectiveSpec,
    *,
    instance_key: Sequence[str | int],
    instance_binding_digest: str,
    source_revision: int | None,
    candidate_revision: int | None,
    input_digest: str,
    layer: int | None,
    bound_evidence_ref: Mapping[str, Any] | None = None,
    solver_invoked: bool | None = None,
    deterministic_termination: str | None = None,
    warnings: Iterable[str] = (),
    core_gap_target: Mapping[str, Any] | None = None,
    backend_profile: Mapping[str, Any] | None = None,
    frozen_solve_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if observation is None:
        backend = dict(backend_profile) if backend_profile is not None else _backend_versions()
        raw_status = "deterministic_model_check"
        termination = deterministic_termination or "unknown"
        invoked = False if solver_invoked is None else solver_invoked
        solver_best = None
        bound_value = None
        solve_error = None
    else:
        backend = dict(observation.backend)
        raw_status = observation.raw_status
        termination = observation.termination
        invoked = observation.solver_invoked if solver_invoked is None else solver_invoked
        solver_best = observation.solver_incumbent_objective
        bound_value = observation.bound_value
        solve_error = observation.error
    objective_value = checked.objective_values[0]["value"] if checked.objective_values else None
    best_matches = (
        checked.candidate_complete
        and objective_spec.component is not None
        and solver_best is not None
        and objective_value is not None
        and (
            float(objective_value) == solver_best
            if checked.record.get("method") == NATIVE_CP_SAT_METHOD
            else math.isclose(float(objective_value), solver_best, rel_tol=1e-8, abs_tol=1e-8)
        )
    )
    # Feasibility has no business objective. A complete checked raw solution is
    # the solver incumbent evidence for that mode; no synthetic objective leaks.
    raw_solution_valid = observation is not None and observation.record.get("raw_solution_value_valid") is True
    has_incumbent = raw_solution_valid and checked.candidate_complete and (
        best_matches if objective_spec.component is not None else checked.raw_vector_complete
    )
    bound: dict[str, Any] | None = None
    bound_conflict = False
    if (
        observation is not None
        and objective_spec.component is not None
        and bound_value is not None
        and bound_evidence_ref is not None
        and termination in {"optimal", "time_limit", "iteration_limit", "feasible"}
        and objective_spec.sense in {"min", "max"}
    ):
        finite_bound = _finite(bound_value)
        if finite_bound is not None:
            # Keep Result.bound on the strict checked-point side. Native bound
            # evidence remains in backend-observation.json and can qualify a
            # same-point numerical closure without changing this field.
            feasible_objective = (
                checked.record.get("status") == "pass"
                and checked.candidate_complete
                and objective_value is not None
                and _finite(objective_value) is not None
            )
            if feasible_objective:
                normalized = (
                    (finite_bound, False)
                    if checked_global_bound_gap(finite_bound, objective_spec.sense, objective_value) is not None
                    else None
                )
            else:
                normalized = (finite_bound, False)
            bound_conflict = feasible_objective and normalized is None
            if normalized is not None:
                bound = {
                    "value": normalized[0],
                    "direction": "lower" if objective_spec.sense == "min" else "upper",
                    "scope": "global",
                    "evidence": dict(bound_evidence_ref),
                }
    absolute_gap: float | None = None
    relative_gap_value: float | None = None
    gap_definition = (
        "absolute=|checked_objective-global_bound| only for a direction-consistent global bound; "
        "relative=absolute/|checked_objective|; relative is null when the denominator is zero or either value is absent; "
        "no numerical tolerance is applied to bound direction"
    )
    if has_incumbent and objective_value is not None and bound is not None:
        normalized_gap = checked_global_bound_gap(bound["value"], objective_spec.sense or "", objective_value)
        if normalized_gap is None:
            # Defensive invariant: the bound gate above must have suppressed
            # any direction-conflicting value before it can produce a gap.
            bound = None
        else:
            absolute_gap, relative_gap_value = normalized_gap
    checked_for_witness = checked.candidate_complete
    result_warnings = list(warnings)
    coverage = [] if checked.record["status"] == "pass" else list(checked.record["uncovered"])
    if bound_conflict:
        result_warnings.append("global_bound_conflicts_with_checked_feasible_objective")
        coverage.append("global_bound_uncovered:direction_conflicts_with_checked_objective")
    requested_options = (
        dict(observation.record.get("requested_options", {}))
        if observation is not None
        else (
            {
                "native": {
                    "time_limit_seconds": frozen_solve_options["time_limit_seconds"],
                    "threads": frozen_solve_options["threads"],
                    "random_seed": frozen_solve_options["random_seed"],
                    "warm_start": frozen_solve_options.get("warm_start", False),
                    "parameters": {},
                },
                "core": {
                    "relative_gap": frozen_solve_options["relative_gap"],
                    "absolute_gap": frozen_solve_options["absolute_gap"],
                    "feasibility_tolerance": frozen_solve_options["feasibility_tolerance"],
                    "integrality_tolerance": frozen_solve_options["integrality_tolerance"],
                },
            }
            if core_gap_target is not None and frozen_solve_options is not None
            else ({name: None for name in ("mip_rel_gap", "mip_abs_gap", "primal_feasibility_tolerance", "mip_feasibility_tolerance")} if core_gap_target is None else {})
        )
    )
    effective_options = (
        dict(observation.record.get("effective_options", {}))
        if observation is not None
        else (
            {
                "time_limit_seconds": None,
                "threads": None,
                "random_seed": None,
                "warm_start": None,
                "parameters": {},
            }
            if core_gap_target is not None
            else {name: None for name in ("mip_rel_gap", "mip_abs_gap", "primal_feasibility_tolerance", "mip_feasibility_tolerance")}
        )
    )
    numerical = _result_numerical_summary(
        check_record=checked.record,
        objective_value=objective_value,
        objective_sense=objective_spec.sense,
        bound=bound,
        absolute_gap=absolute_gap,
        native_observation=(
            observation.record
            if observation is not None and bound_evidence_ref is not None
            else None
        ),
        termination=termination,
        has_incumbent=bool(has_incumbent),
        requested_options=requested_options,
        effective_options=effective_options,
        core_gap_target=core_gap_target,
    )
    return {
        "instance_key": list(instance_key),
        "instance_binding_digest": instance_binding_digest,
        "layer": layer,
        "source_revision": source_revision,
        "candidate_revision": candidate_revision,
        "input_digest": input_digest,
        "backend": backend,
        "raw_status": raw_status,
        "termination": termination,
        "solver_invoked": invoked,
        "has_solver_incumbent": bool(has_incumbent),
        "has_checked_candidate": bool(checked_for_witness),
        "witness": None,
        "objective": dict(checked.objective_values[0]) if checked.objective_values else None,
        "bound": bound,
        "gap": {"absolute": absolute_gap, "relative": relative_gap_value, "definition": gap_definition},
        "numerical": numerical,
        "check": None,
        "coverage": coverage,
        "warnings": result_warnings + (["solver_error_observed"] if solve_error else []),
    }


def _result_numerical_summary(
    *,
    check_record: Mapping[str, Any],
    objective_value: Any,
    objective_sense: str | None,
    bound: Mapping[str, Any] | None,
    absolute_gap: float | None,
    termination: str,
    has_incumbent: bool,
    requested_options: Mapping[str, Any],
    effective_options: Mapping[str, Any],
    core_gap_target: Mapping[str, Any] | None = None,
    native_observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if core_gap_target is None:
        relative = _finite(effective_options.get("mip_rel_gap"))
        absolute = _finite(effective_options.get("mip_abs_gap"))
        rule = "gap <= max(effective mip_abs_gap, effective mip_rel_gap * abs(checked objective))"
    else:
        relative = _finite(core_gap_target.get("relative_gap"))
        absolute = _finite(core_gap_target.get("absolute_gap"))
        rule = "gap <= max(Core absolute_gap target, Core relative_gap target * abs(checked objective))"
    z = _finite(objective_value)
    b = _finite(bound.get("value")) if isinstance(bound, Mapping) else None
    allowed_gap: float | None = None
    gap = _finite(absolute_gap)
    effective_known = (
        relative is not None
        and relative >= 0
        and absolute is not None
        and absolute >= 0
    )
    if effective_known and z is not None:
        relative_limit = relative * abs(z)
        if math.isfinite(relative_limit):
            allowed_gap = max(absolute, relative_limit)
    native_reported_bound: float | None = None
    native_gap: float | None = None
    objective_recomputation_residual: float | None = None
    closure_residual: float | None = None
    qualification = "not_qualified"
    raw_incumbent: float | None = None
    native_evidence_valid = False
    if isinstance(native_observation, Mapping) and objective_sense in {"min", "max"}:
        candidate_bound = _finite(native_observation.get("best_objective_bound"))
        candidate_incumbent = _finite(native_observation.get("best_feasible_objective"))
        evidence = native_observation.get("bound_evidence")
        transform = evidence.get("objective_transform") if isinstance(evidence, Mapping) else None
        expected_direction = "lower" if objective_sense == "min" else "upper"
        native_evidence_valid = (
            candidate_bound is not None
            and isinstance(evidence, Mapping)
            and isinstance(evidence.get("source"), str)
            and bool(evidence.get("source"))
            and evidence.get("scope") == "global"
            and evidence.get("objective_sense") == objective_sense
            and evidence.get("value_direction") == expected_direction
            and isinstance(evidence.get("interface"), str)
            and bool(evidence.get("interface"))
            and isinstance(transform, Mapping)
            and isinstance(transform.get("formula"), str)
            and bool(transform.get("formula"))
            and (
                "raw_value" not in evidence
                or _finite(evidence.get("raw_value")) is not None
            )
        )
        if native_evidence_valid:
            native_reported_bound = candidate_bound
            if native_observation.get("raw_solution_value_valid") is True:
                raw_incumbent = candidate_incumbent

    if z is not None and raw_incumbent is not None:
        residual = abs(z - raw_incumbent)
        if math.isfinite(residual):
            objective_recomputation_residual = residual

    closure_status = "unknown" if not effective_known else "not_closed"
    supported_termination = termination in {"optimal", "time_limit", "iteration_limit", "feasible"}
    native_gap_values = (
        checked_global_bound_gap(native_reported_bound, objective_sense or "", raw_incumbent)
        if native_reported_bound is not None and raw_incumbent is not None
        else None
    )
    if native_gap_values is not None:
        native_gap = native_gap_values[0]
    strict_bound_matches = (
        native_reported_bound is not None
        and isinstance(bound, Mapping)
        and _finite(bound.get("value")) == native_reported_bound
        and bound.get("scope") == "global"
        and bound.get("direction") == ("lower" if objective_sense == "min" else "upper")
    )
    exact_native_objective = check_record.get("method") == NATIVE_CP_SAT_METHOD
    objective_identity_matches = (
        z is not None
        and raw_incumbent is not None
        and (
            z == raw_incumbent
            if exact_native_objective
            else math.isclose(
                z,
                raw_incumbent,
                rel_tol=_OBJECTIVE_VALUE_REL_TOLERANCE,
                abs_tol=_OBJECTIVE_VALUE_ABS_TOLERANCE,
            )
        )
    )
    checked_native_point = (
        native_evidence_valid
        and native_gap is not None
        and objective_recomputation_residual is not None
        and objective_identity_matches
        and check_record.get("status") == "pass"
        and has_incumbent
        and supported_termination
        and native_observation is not None
        and native_observation.get("termination") == termination
    )
    if checked_native_point:
        residual_total = native_gap + objective_recomputation_residual
        if math.isfinite(residual_total):
            closure_residual = residual_total
            checked_bound_gap = (
                checked_global_bound_gap(native_reported_bound, objective_sense or "", z)
                if z is not None
                else None
            )
            if checked_bound_gap is not None and strict_bound_matches:
                qualification = "strict_direction"
            elif checked_bound_gap is None:
                qualification = "objective_recomputation_residual"
            if allowed_gap is not None and closure_residual <= allowed_gap:
                closure_status = "closed"
        else:
            qualification = "not_qualified_nonfinite_residual"
    elif native_reported_bound is not None and raw_incumbent is not None and native_gap is None:
        qualification = "not_qualified_native_direction"
    elif raw_incumbent is not None and z is not None and not objective_identity_matches:
        qualification = "not_qualified_objective_identity"
    result = {
        "requested_options": dict(requested_options),
        "effective_options": dict(effective_options),
        "check_tolerances": dict(check_record.get("tolerances", {})),
        "optimality": {
            "status": closure_status,
            "direction": objective_sense,
            "objective": z,
            "global_bound": b,
            "native_reported_bound": native_reported_bound,
            "native_gap": native_gap,
            "objective_recomputation_residual": objective_recomputation_residual,
            "closure_residual": closure_residual,
            "qualification": qualification,
            "absolute_gap": gap,
            "allowed_gap": allowed_gap,
            "rule": f"native_gap + objective_recomputation_residual <= max(absolute, relative * abs(checked objective)); {rule}",
        },
    }
    if core_gap_target is not None:
        result["core_gap_target"] = {"relative_gap": relative, "absolute_gap": absolute}
    return result


def business_check_uncovered(
    *,
    input_digest: str,
    instance_key: Sequence[str | int],
    instance_binding_digest: str,
    reason: str,
    objective_values: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "input_digest": input_digest,
        "instance_key": list(instance_key),
        "instance_binding_digest": instance_binding_digest,
        "status": "uncovered",
        "checked_obligations": [],
        "objective_values": [dict(item) for item in objective_values],
        "violations": [],
        "uncovered": [reason],
    }

