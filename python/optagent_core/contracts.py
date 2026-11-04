"""Strict deterministic types and parsers for Core envelopes and raw evidence.

A host may normalize dynamic tool arguments into JSON Values before API
dispatch; parsers here still enforce the limits for bridge envelopes and JSON
artifacts loaded from their original bytes.
"""

from __future__ import annotations

import base64
import ctypes
import errno
import hashlib
import json
import math
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping


CONTRACT_VERSION = 1
TOOL_NAMES = (
    "optagent.case",
    "optagent.data",
    "optagent.run",
    "optagent.review",
    "optagent.deliver",
)
ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_JSON_DEPTH = 64


def file_signature(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Return the single-file identity/version tuple used by stable readers."""
    return (
        int(info.st_dev),
        int(info.st_ino),
        stat.S_IFMT(info.st_mode),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def windows_long_path(path: str | os.PathLike[str]) -> Path:
    """Return an absolute path usable by Windows APIs beyond MAX_PATH."""
    raw = os.fspath(path)
    if os.name != "nt":
        return Path(os.path.abspath(raw))
    if raw.startswith("\\\\?\\"):
        return Path(raw)
    absolute = os.path.abspath(raw)
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute[2:])
    return Path("\\\\?\\" + absolute)


def workspace_path_identity(path: str | os.PathLike[str]) -> str:
    """Normalize ordinary and extended Windows spellings for identity checks."""
    value = os.path.abspath(os.fspath(path))
    if os.name == "nt":
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[len("\\\\?\\UNC\\"):]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
    return os.path.normcase(value)


def _publish_no_clobber_via_link(source_path: bytes, destination_path: bytes) -> bool:
    try:
        os.link(source_path, destination_path, follow_symlinks=False)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            return False
        raise ContractError(
            "environment_unavailable",
            "filesystem cannot atomically publish a new file without replacement",
        ) from exc

    try:
        os.unlink(source_path)
    except OSError as exc:
        # The destination is already published. Leave it in place; callers may
        # still clean up the temporary name in their own finally block.
        raise ContractError(
            "environment_unavailable",
            "published file temporary name could not be removed",
        ) from exc
    return True


def publish_no_clobber(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> bool:
    """Atomically publish a fully written same-directory temp file without replacement.

    Linux falls back to a hard link only when ``renameat2(RENAME_NOREPLACE)`` is
    unavailable. Returns False only when the destination already exists; callers
    must never emulate this operation with an exists check followed by a rename.
    """
    source_path = os.fsencode(source)
    destination_path = os.fsencode(destination)
    if b"\0" in source_path or b"\0" in destination_path:
        raise ValueError("embedded null byte")

    if sys.platform == "win32":
        try:
            os.rename(source_path, destination_path)
        except FileExistsError:
            return False
        except OSError as exc:
            raise ContractError(
                "environment_unavailable",
                "filesystem cannot atomically publish a new file without replacement",
            ) from exc
        return True

    if sys.platform.startswith("linux"):
        try:
            libc = ctypes.CDLL(None, use_errno=True)
        except OSError as exc:
            raise ContractError(
                "environment_unavailable",
                "atomic no-replace rename is unavailable on this platform",
            ) from exc
        try:
            renameat2 = libc.renameat2
        except AttributeError:
            return _publish_no_clobber_via_link(source_path, destination_path)
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        result = renameat2(-100, source_path, -100, destination_path, 1)
        if result == 0:
            return True
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            return False
        if error_number in {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}:
            return _publish_no_clobber_via_link(source_path, destination_path)
        error = OSError(error_number, "atomic no-replace rename failed")
        raise ContractError(
            "environment_unavailable",
            "filesystem cannot atomically publish a new file without replacement",
        ) from error

    if sys.platform == "darwin":
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            renamex_np = libc.renamex_np
        except (AttributeError, OSError) as exc:
            raise ContractError(
                "environment_unavailable",
                "atomic no-replace rename is unavailable on this platform",
            ) from exc
        renamex_np.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        renamex_np.restype = ctypes.c_int
        result = renamex_np(source_path, destination_path, 4)
        if result == 0:
            return True
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            return False
        error = OSError(error_number, "atomic no-replace rename failed")
        raise ContractError(
            "environment_unavailable",
            "filesystem cannot atomically publish a new file without replacement",
        ) from error

    raise ContractError(
        "environment_unavailable",
        "atomic no-replace rename is unavailable on this platform",
    )


class ContractError(Exception):
    """A stable product error; message never contains raw user bytes or secrets."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        pointer: str | None = None,
        details: Mapping[str, Any] | None = None,
        effect: str = "none",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.pointer = pointer
        self.details = dict(details or {})
        self.effect = effect


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("JSON number is outside the finite float range")
    return parsed


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _depth(value: Any, current: int = 0) -> int:
    if isinstance(value, dict):
        container_depth = current + 1
        if container_depth > MAX_JSON_DEPTH:
            raise ValueError("JSON nesting too deep")
        return max(
            (_depth(child, container_depth) for child in value.values()),
            default=container_depth,
        )
    if isinstance(value, list):
        container_depth = current + 1
        if container_depth > MAX_JSON_DEPTH:
            raise ValueError("JSON nesting too deep")
        return max(
            (_depth(child, container_depth) for child in value),
            default=container_depth,
        )
    return current


def strict_json_loads(raw: bytes | str, *, max_bytes: int = MAX_JSON_BYTES) -> Any:
    """Parse JSON while rejecting duplicate keys, nonfinite values and deep trees."""
    if isinstance(raw, bytes):
        if len(raw) > max_bytes:
            raise ValueError("JSON document too large")
        text = raw.decode("utf-8", errors="strict")
    else:
        text = raw
        if len(text.encode("utf-8")) > max_bytes:
            raise ValueError("JSON document too large")
    value = json.loads(
        text,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
        parse_float=_finite_float,
    )
    _depth(value)
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """The frozen canon_v1 encoding used for manifests and records."""
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def normalize_json_value(value: Any, *, max_depth: int = MAX_JSON_DEPTH) -> Any:
    """Return a plain finite JSON tree, rejecting cycles and non-string keys.

    Python tuples follow the standard JSON encoder's array behavior. Other
    custom values are rejected instead of being silently converted.
    """
    active: set[int] = set()

    def visit(node: Any, container_depth: int) -> Any:
        if node is None or isinstance(node, bool):
            return node
        if isinstance(node, str):
            return node if type(node) is str else str.__str__(node)
        if isinstance(node, int) and not isinstance(node, bool):
            return node if type(node) is int else int.__int__(node)
        if isinstance(node, float):
            number = node if type(node) is float else float.__float__(node)
            if not math.isfinite(number):
                raise ValueError("JSON number must be finite")
            return number
        if isinstance(node, (dict, list, tuple)):
            depth = container_depth + 1
            if depth > max_depth:
                raise ValueError("JSON nesting too deep")
            identity = id(node)
            if identity in active:
                raise ValueError("cyclic JSON value")
            active.add(identity)
            try:
                if isinstance(node, dict):
                    result: dict[str, Any] = {}
                    for key, child in node.items():
                        if not isinstance(key, str):
                            raise ValueError("JSON object keys must be strings")
                        plain_key = key if type(key) is str else str.__str__(key)
                        result[plain_key] = visit(child, depth)
                    return result
                return [visit(child, depth) for child in node]
            finally:
                active.remove(identity)
        raise TypeError("value is not JSON serializable")

    return visit(value, 0)


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def decode_base64(value: Any, *, pointer: str) -> bytes:
    if not isinstance(value, str):
        raise ContractError("invalid_argument", "expected base64 text", pointer=pointer)
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ContractError("invalid_argument", "invalid base64 text", pointer=pointer) from exc
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ContractError("invalid_argument", "non-canonical base64 text", pointer=pointer)
    return decoded


def encode_base64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def require_object(value: Any, *, pointer: str = "") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("invalid_argument", "expected object", pointer=pointer or None)
    return value


def require_string(value: Any, *, pointer: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ContractError("invalid_argument", "expected string", pointer=pointer)
    if "\x00" in value:
        raise ContractError("invalid_argument", "NUL is not allowed", pointer=pointer)
    return value


def require_int(value: Any, *, pointer: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError("invalid_argument", "expected integer in range", pointer=pointer)
    return value


def require_number(value: Any, *, pointer: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError("invalid_argument", "expected finite number", pointer=pointer)
    number = float(value)
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise ContractError("invalid_argument", "expected finite number in range", pointer=pointer)
    return number


def require_id(value: Any, *, pointer: str) -> str:
    text = require_string(value, pointer=pointer)
    if not ID_RE.fullmatch(text):
        raise ContractError("invalid_argument", "expected UUID identifier", pointer=pointer)
    return text


def require_hash(value: Any, *, pointer: str) -> str:
    text = require_string(value, pointer=pointer)
    if not HASH_RE.fullmatch(text):
        raise ContractError("invalid_argument", "expected lowercase SHA256", pointer=pointer)
    return text


def require_stamp(value: Any, *, pointer: str = "/context/stamp") -> dict[str, str]:
    obj = require_object(value, pointer=pointer)
    allowed = {"session_id", "role", "turn_id", "call_id"}
    if set(obj) != allowed:
        raise ContractError("invalid_argument", "invalid trusted stamp shape", pointer=pointer)
    stamp = {
        "session_id": require_string(obj["session_id"], pointer=f"{pointer}/session_id"),
        "role": require_string(obj["role"], pointer=f"{pointer}/role"),
        "turn_id": require_string(obj["turn_id"], pointer=f"{pointer}/turn_id"),
        "call_id": require_string(obj["call_id"], pointer=f"{pointer}/call_id"),
    }
    if stamp["role"] not in {"root", "opt-verifier", "opt-worker"}:
        raise ContractError("permission_denied", "unsupported trusted role", pointer=f"{pointer}/role")
    return stamp


def validate_schema(
    value: Any,
    schema: Mapping[str, Any],
    *,
    pointer: str = "",
    root: Mapping[str, Any] | None = None,
) -> None:
    """Validate the small JSON Schema subset used by the checked-in tool schemas."""
    if root is None:
        root = schema
    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise ContractError("environment_unavailable", "tool schema uses an unsupported reference")
        target: Any = root
        for token in ref[2:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, Mapping) or token not in target:
                raise ContractError("environment_unavailable", "tool schema reference is invalid")
            target = target[token]
        if not isinstance(target, Mapping):
            raise ContractError("environment_unavailable", "tool schema reference is invalid")
        validate_schema(value, target, pointer=pointer, root=root)
        return
    if "oneOf" in schema:
        successes = 0
        errors: list[ContractError] = []
        for variant in schema["oneOf"]:
            try:
                validate_schema(value, variant, pointer=pointer, root=root)
                successes += 1
            except ContractError as exc:
                errors.append(exc)
        if successes == 0:
            selected = _select_const_discriminator(value, schema["oneOf"], pointer=pointer)
            if selected is not None:
                # A caller that supplied the fixed operation/discriminator has
                # identified one branch. Preserve its actual required-field,
                # type, and unknown-field error instead of hiding it behind a
                # generic oneOf failure.
                validate_schema(value, selected, pointer=pointer, root=root)
            if errors:
                best = max(errors, key=lambda error: len(error.pointer or ""))
                if best.pointer and best.pointer != pointer:
                    raise best
            raise ContractError("invalid_argument", "arguments do not match exactly one allowed form", pointer=pointer or None)
        if successes > 1:
            raise ContractError("invalid_argument", "arguments match more than one allowed form", pointer=pointer or None)
    if "anyOf" in schema:
        for variant in schema["anyOf"]:
            try:
                validate_schema(value, variant, pointer=pointer, root=root)
                return
            except ContractError:
                continue
        raise ContractError("invalid_argument", "arguments do not match allowed forms", pointer=pointer or None)
    if "const" in schema and value != schema["const"]:
        raise ContractError(
            "invalid_argument",
            "unexpected constant value",
            pointer=pointer or None,
            details={"expected": _schema_expectation(schema, "const")},
        )
    if "enum" in schema and value not in schema["enum"]:
        raise ContractError("invalid_argument", "value is outside the allowed set", pointer=pointer or None)
    expected = schema.get("type")
    if isinstance(expected, list):
        valid = any(_matches_type(value, item) for item in expected)
    elif expected is not None:
        valid = _matches_type(value, expected)
    else:
        valid = True
    if not valid:
        raise ContractError("invalid_argument", f"expected {expected}", pointer=pointer or None)
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 2**63 - 1):
            raise ContractError("invalid_argument", "string length out of range", pointer=pointer or None)
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise ContractError(
                "invalid_argument",
                "string does not match required format",
                pointer=pointer or None,
                details={"expected": _schema_expectation(schema, "type", "pattern", "description")},
            )
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise ContractError("invalid_argument", "number must be finite", pointer=pointer or None)
        if "minimum" in schema and value < schema["minimum"]:
            raise ContractError("invalid_argument", "number below minimum", pointer=pointer or None)
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ContractError("invalid_argument", "number is below exclusive minimum", pointer=pointer or None)
        if "maximum" in schema and value > schema["maximum"]:
            raise ContractError("invalid_argument", "number above maximum", pointer=pointer or None)
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            raise ContractError("invalid_argument", "number is above exclusive maximum", pointer=pointer or None)
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 2**63 - 1):
            raise ContractError("invalid_argument", "array length out of range", pointer=pointer or None)
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                validate_schema(item, item_schema, pointer=f"{pointer}/{index}", root=root)
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                property_schema = properties.get(key)
                expected = _schema_expectation(property_schema, "type", "const", "description")
                raise ContractError(
                    "invalid_argument",
                    "required field missing",
                    pointer=f"{pointer}/{key}",
                    details={"expected": expected} if expected else None,
                )
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                key = sorted(extra)[0]
                raise ContractError("invalid_argument", "unknown field", pointer=f"{pointer}/{key}")
        for key, item in value.items():
            child = properties.get(key)
            if child is not None:
                validate_schema(item, child, pointer=f"{pointer}/{key}", root=root)


def _schema_expectation(schema: Any, *keys: str) -> dict[str, Any]:
    """Return only selected, shallow schema keywords for an error diagnostic."""
    if not isinstance(schema, Mapping):
        return {}
    return {key: schema[key] for key in keys if key in schema}


def _select_const_discriminator(
    value: Any,
    variants: Any,
    *,
    pointer: str,
) -> Mapping[str, Any] | None:
    """Choose a unique object branch from supplied const-valued fields.

    This is intentionally limited to direct object properties with ``const``
    values. It supports fixed operation/discriminator unions while leaving
    ordinary nested oneOf constraints (such as column selectors) to oneOf's
    normal validation.
    """
    if not isinstance(value, dict) or not isinstance(variants, list):
        return None
    candidates = [
        variant
        for variant in variants
        if isinstance(variant, Mapping)
        and variant.get("type") == "object"
        and isinstance(variant.get("properties"), Mapping)
    ]
    if len(candidates) != len(variants):
        return None

    while len(candidates) > 1:
        common_keys = set(candidates[0]["properties"])
        for candidate in candidates[1:]:
            common_keys.intersection_update(candidate["properties"])
        discriminator = None
        for key in candidates[0]["properties"]:
            if key not in common_keys:
                continue
            constants = [
                candidate["properties"][key].get("const")
                if isinstance(candidate["properties"][key], Mapping)
                else _NO_CONST
                for candidate in candidates
            ]
            if all(constant is not _NO_CONST for constant in constants) and len(set(map(_const_key, constants))) > 1:
                discriminator = (key, constants)
                break
        if discriminator is None:
            return None

        key, constants = discriminator
        if key not in value:
            raise ContractError(
                "invalid_argument",
                f"field '{key}' is required to select one allowed form",
                pointer=f"{pointer}/{key}",
            )
        narrowed = [
            candidate
            for candidate, constant in zip(candidates, constants)
            if value[key] == constant
        ]
        if not narrowed:
            raise ContractError(
                "invalid_argument",
                f"field '{key}' does not select a supported form",
                pointer=f"{pointer}/{key}",
            )
        candidates = narrowed
    return candidates[0]


_NO_CONST = object()


def _const_key(value: Any) -> tuple[type[Any], str]:
    """Keep Python's bool/int equality from merging distinct JSON consts."""
    return type(value), json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def public_success(
    tool: str,
    op: str,
    *,
    case_id: str | None,
    state_revision: int | None,
    result: Mapping[str, Any],
    warnings: Iterable[Mapping[str, Any]] = (),
    limitations: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "ok": True,
        "contract_version": CONTRACT_VERSION,
        "tool": tool,
        "op": op,
        "case_id": case_id,
        "state_revision": state_revision,
        "result": dict(result),
        "warnings": [dict(item) for item in warnings],
        "limitations": [dict(item) for item in limitations],
    }


def public_failure(
    tool: str,
    op: str,
    error: ContractError,
    *,
    case_id: str | None = None,
    state_revision: int | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "contract_version": CONTRACT_VERSION,
        "tool": tool,
        "op": op,
        "case_id": case_id,
        "state_revision": state_revision,
        "error": {
            "code": error.code,
            "message": error.message,
            "pointer": error.pointer,
            "details": error.details,
            "effect": error.effect,
        },
    }


def ref_from_bytes(path: str, raw: bytes, media_type: str) -> dict[str, Any]:
    return {"path": path, "sha256": sha256_bytes(raw), "byte_len": len(raw), "media_type": media_type}


def validate_ref_shape(value: Any, *, pointer: str = "/ref") -> dict[str, Any]:
    obj = require_object(value, pointer=pointer)
    expected = {"path", "sha256", "byte_len", "media_type"}
    if set(obj) != expected:
        raise ContractError("invalid_argument", "invalid artifact reference", pointer=pointer)
    return {
        "path": require_string(obj["path"], pointer=f"{pointer}/path"),
        "sha256": require_hash(obj["sha256"], pointer=f"{pointer}/sha256"),
        "byte_len": require_int(obj["byte_len"], pointer=f"{pointer}/byte_len"),
        "media_type": require_string(obj["media_type"], pointer=f"{pointer}/media_type"),
    }


def schema_asset_path() -> Path:
    source_asset = Path(__file__).resolve().parents[2] / "assets" / "tool_schemas.json"
    if source_asset.is_file():
        return source_asset
    installed_asset = Path(sys.prefix) / "assets" / "tool_schemas.json"
    if installed_asset.is_file():
        return installed_asset
    return source_asset


def load_tool_schemas(path: Path | None = None) -> dict[str, dict[str, Any]]:
    schema_path = path or schema_asset_path()
    try:
        value = strict_json_loads(schema_path.read_bytes())
    except (OSError, UnicodeError, ValueError) as exc:
        raise ContractError("environment_unavailable", "tool schema asset is unavailable") from exc
    if not isinstance(value, dict) or set(value) != {"schema_version", "tools"} or value["schema_version"] != 1:
        raise ContractError("environment_unavailable", "tool schema asset has invalid shape")
    tools = value["tools"]
    if not isinstance(tools, list) or len(tools) != len(TOOL_NAMES):
        raise ContractError("environment_unavailable", "tool schema asset has invalid tool set")
    result: dict[str, dict[str, Any]] = {}
    for entry in tools:
        if not isinstance(entry, dict) or set(entry) != {"name", "description", "input_schema"}:
            raise ContractError("environment_unavailable", "tool schema entry has invalid shape")
        name = entry["name"]
        if name in result or name not in TOOL_NAMES:
            raise ContractError("environment_unavailable", "tool schema asset has invalid tool name")
        if not isinstance(entry["description"], str) or not isinstance(entry["input_schema"], dict):
            raise ContractError("environment_unavailable", "tool schema entry has invalid types")
        result[name] = entry
    if set(result) != set(TOOL_NAMES):
        raise ContractError("environment_unavailable", "tool schema asset is missing a required tool")
    return result
