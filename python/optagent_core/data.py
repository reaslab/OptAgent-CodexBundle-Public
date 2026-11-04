"""Explicit source import, one-time normalization, and the shared DataBundle loader."""

from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import math
import os
import re
import stat
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .contracts import (
    ContractError,
    canonical_json_bytes,
    normalize_json_value,
    require_id,
    require_hash,
    sha256_bytes,
    strict_json_loads,
    validate_ref_shape,
)


class DataReader(Protocol):
    """Read the exact Ref bytes, verifying containment, stability, length, and digest."""

    def read_ref(self, ref: Mapping[str, Any]) -> bytes: ...


class DataWriter(DataReader, Protocol):
    """Local ingestion extends the worker reader only with immutable writes."""

    def write_immutable(self, relative_path: str, raw: bytes, media_type: str) -> dict[str, Any]: ...


_PORTABLE_REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _portable_is_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _PORTABLE_REPARSE_FLAG)


def _portable_ref_key(ref: Mapping[str, Any]) -> tuple[str, str, int, str]:
    checked = validate_ref_shape(ref, pointer="/ref_map/ref")
    return (checked["path"], checked["sha256"], checked["byte_len"], checked["media_type"])


class PortableReader:
    """Read only original Ref identities explicitly mapped inside one extracted package."""

    def __init__(self, package_root: str | Path, ref_map: Sequence[Mapping[str, Any]]) -> None:
        from .contracts import ContractError

        root = Path(package_root)
        if not root.is_absolute():
            raise ContractError("path_denied", "portable package root must be absolute")
        self.root = Path(os.path.abspath(os.fspath(root)))
        try:
            cursor = Path(self.root.anchor)
            root_info = cursor.lstat()
            if _portable_is_reparse(root_info):
                raise ContractError("path_denied", "portable package path contains a link")
            for part in self.root.parts[1:]:
                cursor = cursor / part
                root_info = cursor.lstat()
                if _portable_is_reparse(root_info):
                    raise ContractError("path_denied", "portable package path contains a link")
        except OSError as exc:
            raise ContractError("capture_unavailable", "portable package root is unavailable") from exc
        if _portable_is_reparse(root_info) or not stat.S_ISDIR(root_info.st_mode):
            raise ContractError("path_denied", "portable package root must be a regular directory")
        self._mapped: dict[tuple[str, str, int, str], str] = {}
        physical: dict[str, tuple[str, int]] = {}
        if not isinstance(ref_map, Sequence) or isinstance(ref_map, (str, bytes)):
            raise ContractError("output_invalid", "portable ref map must be an array")
        for index, item in enumerate(ref_map):
            if not isinstance(item, Mapping) or set(item) != {"ref", "package_path"}:
                raise ContractError("output_invalid", "portable ref map item has invalid fields")
            ref_key = _portable_ref_key(item["ref"])
            package_path = item["package_path"]
            if not isinstance(package_path, str) or not package_path or "\\" in package_path or "\x00" in package_path:
                raise ContractError("path_denied", "portable package path is invalid")
            pure = PurePosixPath(package_path)
            if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts) or pure.as_posix() != package_path:
                raise ContractError("path_denied", "portable package path escapes its root")
            prior = self._mapped.get(ref_key)
            if prior is not None and prior != package_path:
                raise ContractError("package_collision", "one original reference maps to multiple package paths")
            content_identity = (ref_key[1], ref_key[2])
            existing_content = physical.get(package_path)
            if existing_content is not None and existing_content != content_identity:
                raise ContractError("package_collision", "package path maps to conflicting byte identities")
            self._mapped[ref_key] = package_path
            physical[package_path] = content_identity

    def read_ref(self, ref: Mapping[str, Any]) -> bytes:
        from .contracts import ContractError

        ref_key = _portable_ref_key(ref)
        try:
            package_path = self._mapped[ref_key]
        except KeyError as exc:
            raise ContractError("capture_unavailable", "reference is not included in the portable package") from exc
        path = self.root.joinpath(*PurePosixPath(package_path).parts)
        cursor = self.root
        try:
            for part in PurePosixPath(package_path).parts:
                cursor = cursor / part
                info = cursor.lstat()
                if _portable_is_reparse(info):
                    raise ContractError("path_denied", "portable artifact path contains a link")
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise ContractError("path_denied", "portable artifact must be a regular file")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            with os.fdopen(fd, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ContractError("source_changed_during_read", "portable artifact changed while opening")
                raw = stream.read()
                after_fd = os.fstat(stream.fileno())
            after_path = path.lstat()
        except ContractError:
            raise
        except OSError as exc:
            raise ContractError("capture_unavailable", "portable artifact is unavailable") from exc
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if (
            identity(before) != identity(opened)
            or identity(opened) != identity(after_fd)
            or identity(after_fd) != identity(after_path)
        ):
            raise ContractError("source_changed_during_read", "portable artifact changed while being read")
        if len(raw) != ref_key[2] or hashlib.sha256(raw).hexdigest() != ref_key[1]:
            raise ContractError("artifact_changed", "portable artifact bytes do not match their original reference")
        return raw


Primitive = str | int
_VALID_TYPES = {"string", "integer", "number", "boolean", "date"}
_VALID_KINDS = {"table", "scalar", "set", "relation", "json"}


@dataclass(frozen=True)
class DataObject:
    object_id: str
    kind: str
    key_fields: tuple[str, ...]
    columns: tuple[Mapping[str, Any], ...]
    values_ref: Mapping[str, Any] | None
    source_locations: tuple[Mapping[str, str], ...]
    rows: tuple[Any, ...]
    keys: tuple[tuple[Any, ...], ...]
    _json_value_bytes: bytes | None = None


@dataclass(frozen=True)
class InstanceContext:
    """One explicit parameter-table row shared by builder and checker."""

    instance_key: tuple[Primitive, ...]
    parameter_object_id: str | None
    parameters: Mapping[str, Any]
    row_digest: str
    instance_binding_digest: str


@dataclass(frozen=True)
class DataBundle:
    """Normalized objects, loaded once from verified bytes for every consumer."""

    data_id: str | None
    digest: str
    loader_version: str
    objects: Mapping[str, DataObject]
    consumed_inputs: tuple[Mapping[str, Any], ...]
    candidate_binding_digest: str
    instance_context: InstanceContext | None = None

    def table(self, object_id: str) -> Mapping[tuple[Any, ...], Mapping[str, Any]]:
        obj = self._object(object_id, "table")
        return MappingProxyType(
            {
                key: MappingProxyType(dict(row))
                for key, row in zip(obj.keys, obj.rows, strict=True)
            }
        )

    def scalar(self, object_id: str) -> Any:
        obj = self._object(object_id, "scalar")
        return obj.rows[0]

    def json(self, object_id: str) -> Any:
        """Return an independent ordinary JSON tree for one native JSON node."""
        obj = self._object(object_id, "json")
        if obj._json_value_bytes is None:
            raise ContractError("artifact_changed", "JSON data object has no frozen node")
        return strict_json_loads(obj._json_value_bytes, max_bytes=len(obj._json_value_bytes))

    def set(self, object_id: str) -> tuple[Any, ...]:
        obj = self._object(object_id, "set")
        return tuple(obj.rows)

    def relation(self, object_id: str) -> tuple[tuple[Any, ...], ...]:
        obj = self._object(object_id, "relation")
        return tuple(obj.keys)

    def rows(self, object_id: str) -> tuple[Any, ...]:
        """Ordered normalized rows for code that needs source/table order."""
        obj = self._object(object_id)
        if obj.kind == "json":
            raise ContractError(
                "invalid_argument",
                "data object has a different kind",
                details={"object_id": object_id, "expected_kind": "table"},
            )
        return obj.rows

    def object(self, object_id: str) -> DataObject:
        return self._object(object_id)

    def _object(self, object_id: str, expected_kind: str | None = None) -> DataObject:
        try:
            obj = self.objects[object_id]
        except KeyError as exc:
            raise ContractError(
                "not_found",
                "data object does not exist",
                details={"object_id": object_id},
            ) from exc
        if expected_kind is not None and obj.kind != expected_kind:
            raise ContractError(
                "invalid_argument",
                "data object has a different kind",
                details={"object_id": object_id, "expected_kind": expected_kind},
            )
        return obj

    def for_instance(self, object_id: str | None, instance_key: Sequence[Primitive] = ()) -> "DataBundle":
        """Return a view bound to one parameter row without duplicating data."""
        if object_id is None:
            if instance_key:
                raise ContractError("invalid_argument", "single-instance key must be empty")
            row: Mapping[str, Any] = MappingProxyType({})
            key: tuple[Primitive, ...] = ()
            parameter_object_id = None
        else:
            obj = self._object(object_id, "table")
            key = tuple(instance_key)
            if len(key) != len(obj.key_fields) or any(type(v) not in (str, int) for v in key):
                raise ContractError("invalid_argument", "instance key has the wrong typed shape")
            try:
                row_index = obj.keys.index(key)
            except ValueError as exc:
                raise ContractError("not_found", "instance key does not exist") from exc
            row = MappingProxyType(dict(obj.rows[row_index]))
            parameter_object_id = object_id
        row_bytes = canonical_json_bytes(dict(row))
        row_digest = sha256_bytes(row_bytes)
        binding_bytes = canonical_json_bytes(
            {
                "candidate_binding_digest": self.candidate_binding_digest,
                "parameter_object_id": parameter_object_id,
                "instance_key": list(key),
                "row_digest": row_digest,
            }
        )
        context = InstanceContext(
            instance_key=key,
            parameter_object_id=parameter_object_id,
            parameters=row,
            row_digest=row_digest,
            instance_binding_digest=sha256_bytes(binding_bytes),
        )
        return replace(self, instance_context=context)


def _store(value: DataReader | str | Path) -> DataReader:
    if hasattr(value, "read_ref"):
        return value  # type: ignore[return-value]
    from .store import CaseStore

    return CaseStore(value, create=False)


def _read_json_ref(store: DataReader, ref: Mapping[str, Any], label: str) -> Any:
    raw = store.read_ref(ref)
    try:
        value = strict_json_loads(raw, max_bytes=len(raw))
    except (ValueError, UnicodeError) as exc:
        raise ContractError("output_invalid", f"{label} is not strict JSON") from exc
    return value


def read_prepared_source_records(
    reader: DataReader,
    source_specs: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Read Source records from the explicit Prepared input list and verify their byte Refs."""
    records: dict[str, dict[str, Any]] = {}
    for index, spec in enumerate(source_specs):
        pointer = f"/sources/{index}"
        if not isinstance(spec, Mapping) or set(spec) != {"source_id", "record", "bytes"}:
            raise ContractError("output_invalid", "prepared source input has invalid fields", pointer=pointer)
        source_id = require_id(spec["source_id"], pointer=f"{pointer}/source_id")
        if source_id in records:
            raise ContractError("output_invalid", "prepared Source ID repeats", pointer=f"{pointer}/source_id")
        record_ref = validate_ref_shape(spec["record"], pointer=f"{pointer}/record")
        bytes_ref = validate_ref_shape(spec["bytes"], pointer=f"{pointer}/bytes")
        if bytes_ref["media_type"] != "application/octet-stream":
            raise ContractError("output_invalid", "prepared Source bytes must be binary", pointer=f"{pointer}/bytes")
        raw = reader.read_ref(record_ref)
        try:
            record = strict_json_loads(raw, max_bytes=len(raw))
        except (ValueError, UnicodeError) as exc:
            raise ContractError("output_invalid", "prepared Source record is invalid JSON", pointer=f"{pointer}/record") from exc
        if (
            not isinstance(record, dict)
            or record.get("source_id") != source_id
            or validate_ref_shape(record.get("bytes"), pointer=f"{pointer}/record/bytes") != bytes_ref
        ):
            raise ContractError("artifact_changed", "prepared Source record does not bind its listed bytes Ref", pointer=pointer)
        records[source_id] = record
    return records


def load_data_bundle(
    workspace: DataReader | str | Path,
    data_ref_or_manifest: Mapping[str, Any] | None,
    *,
    instance_parameter_object_id: str | None = None,
    instance_key: Sequence[Primitive] = (),
    candidate_binding_digest: str | None = None,
    source_records: Mapping[str, Mapping[str, Any]] | None = None,
) -> DataBundle:
    """Load all normalized objects and verify every consumed bytes reference.

    The same function is called by native builders, model checkers, and the
    packaged runner. `data_ref_or_manifest` may be either a manifest Ref or the
    already-decoded DataManifest object, or exactly `None` for a candidate
    with no external data. No consumer has its own loader.
    """
    if data_ref_or_manifest is None:
        binding_digest = require_hash(candidate_binding_digest, pointer="/candidate_binding_digest")
        if instance_parameter_object_id is not None:
            raise ContractError("invalid_argument", "no-data bundle cannot bind an instance parameter object")
        if instance_key:
            raise ContractError("invalid_argument", "no-data bundle cannot have an instance key")
        return DataBundle(
            data_id=None,
            digest=sha256_bytes(canonical_json_bytes(None)),
            loader_version="optagent-data-v1",
            objects=MappingProxyType({}),
            consumed_inputs=(),
            candidate_binding_digest=binding_digest,
        )

    store = _store(workspace)
    if "path" in data_ref_or_manifest and "sha256" in data_ref_or_manifest:
        manifest_ref = dict(data_ref_or_manifest)
        manifest = _read_json_ref(store, manifest_ref, "data manifest")
        consumed: list[Mapping[str, Any]] = [manifest_ref]
    else:
        manifest = dict(data_ref_or_manifest)
        consumed = []
    expected_fields = {"data_id", "source_ids", "import_plan", "objects", "trace", "loader_version", "digest"}
    if set(manifest) != expected_fields:
        raise ContractError("output_invalid", "data manifest has invalid fields")
    if not isinstance(manifest["data_id"], str) or not manifest["data_id"]:
        raise ContractError("output_invalid", "data manifest has invalid identity")
    require_hash(manifest["digest"], pointer="/digest")
    source_ids = manifest["source_ids"]
    import_plan = manifest["import_plan"]
    if (
        not isinstance(source_ids, list)
        or not source_ids
        or any(not isinstance(source_id, str) for source_id in source_ids)
        or len(set(source_ids)) != len(source_ids)
        or not isinstance(import_plan, dict)
    ):
        raise ContractError("output_invalid", "data manifest source and plan bindings are invalid")
    if source_records is None or any(source_id not in source_records for source_id in source_ids):
        raise ContractError("input_closure_unknown", "DataBundle Sources are outside the verified Case or Prepared source set")
    _validate_plan(import_plan, set(source_ids))
    planned_source_ids = list(dict.fromkeys(block["source_id"] for block in import_plan["blocks"]))
    if planned_source_ids != source_ids or manifest["loader_version"] != "optagent-data-v1":
        raise ContractError("artifact_changed", "data manifest source order or loader version differs from its plan")
    planned_blocks = {block["object_id"]: block for block in import_plan["blocks"]}
    objects_value = manifest["objects"]
    verified_source_bytes: dict[tuple[str, str, int, str], bytes] = {}
    if not isinstance(objects_value, list):
        raise ContractError("output_invalid", "data manifest objects must be an array")
    loaded: dict[str, DataObject] = {}
    object_refs: list[dict[str, Any]] = []
    locations_by_object: dict[str, tuple[Mapping[str, str], ...]] = {}
    for index, spec in enumerate(objects_value):
        pointer = f"/objects/{index}"
        if not isinstance(spec, dict) or set(spec) != {
            "object_id",
            "kind",
            "key_fields",
            "columns",
            "values",
            "source_locations",
        }:
            raise ContractError("output_invalid", "data object has invalid fields", pointer=pointer)
        object_id = spec["object_id"]
        kind = spec["kind"]
        key_fields = spec["key_fields"]
        columns = spec["columns"]
        if not isinstance(object_id, str) or not object_id or object_id in loaded:
            raise ContractError("output_invalid", "data object identity is invalid or repeated", pointer=f"{pointer}/object_id")
        if not isinstance(kind, str) or kind not in _VALID_KINDS or not isinstance(key_fields, list) or not all(isinstance(k, str) and k for k in key_fields):
            raise ContractError("output_invalid", "data object key or kind is invalid", pointer=pointer)
        if len(set(key_fields)) != len(key_fields):
            raise ContractError("output_invalid", "data object key fields repeat", pointer=f"{pointer}/key_fields")
        if not isinstance(columns, list):
            raise ContractError("output_invalid", "data object columns are invalid", pointer=f"{pointer}/columns")
        value_ref = spec["values"]
        if not isinstance(value_ref, dict):
            raise ContractError("output_invalid", "normalized object reference is invalid", pointer=f"{pointer}/values")
        value_ref = validate_ref_shape(value_ref, pointer=f"{pointer}/values")
        if value_ref["media_type"] != "application/json":
            raise ContractError("output_invalid", "normalized object values must be JSON", pointer=f"{pointer}/values")
        rows = _read_json_ref(store, value_ref, f"data object {object_id}")
        object_refs.append(dict(value_ref))
        source_locations = spec["source_locations"]
        planned = planned_blocks.get(object_id)
        if planned is None or (
            planned["kind"] != kind
            or planned["key_fields"] != key_fields
            or [
                {"name": col["name"], "type": col["type"], "unit": col["unit"], "nullable": col["nullable"]}
                for col in planned["columns"]
            ] != columns
            or not isinstance(source_locations, list)
        ):
            raise ContractError("artifact_changed", "normalized object differs from its declared import plan", pointer=pointer)

        def check_locations(row_count: int) -> tuple[Mapping[str, str], ...]:
            if len(source_locations) != row_count:
                raise ContractError("output_invalid", "source locator count differs from normalized values", pointer=f"{pointer}/source_locations")
            expected_source_id = planned["source_id"]
            checked: list[Mapping[str, str]] = []
            for loc_index, location in enumerate(source_locations):
                if (
                    not isinstance(location, dict)
                    or set(location) != {"source_id", "locator"}
                    or location["source_id"] != expected_source_id
                    or not isinstance(location["locator"], str)
                    or not location["locator"]
                ):
                    raise ContractError("output_invalid", "normalized source locator is invalid", pointer=f"{pointer}/source_locations/{loc_index}")
                checked.append(dict(location))
            return tuple(checked)

        normalized_object = _normalized_data_object(
            object_id=object_id,
            kind=kind,
            key_fields=key_fields,
            columns=columns,
            values=rows,
            values_ref=value_ref,
            source_locations=(),
            pointer=pointer,
        )
        location_count = 1 if kind == "json" else len(normalized_object.rows)
        checked_locations = check_locations(location_count)
        locations_by_object[object_id] = checked_locations
        loaded[object_id] = replace(normalized_object, source_locations=checked_locations)
    if set(loaded) != set(planned_blocks):
        raise ContractError("artifact_changed", "normalized object set differs from its declared import plan")
    trace_ref = manifest["trace"]
    if not isinstance(trace_ref, dict):
        raise ContractError("output_invalid", "data trace reference is invalid")
    trace_ref = validate_ref_shape(trace_ref, pointer="/trace")
    if trace_ref["media_type"] != "application/json":
        raise ContractError("output_invalid", "data trace must be JSON")
    trace_value = _read_json_ref(store, trace_ref, "data trace")
    if (
        not isinstance(trace_value, dict)
        or set(trace_value) != {"schema_version", "plan", "source_refs", "steps"}
        or trace_value.get("schema_version") != 1
        or trace_value.get("plan") != import_plan
        or not isinstance(trace_value.get("source_refs"), list)
        or len(trace_value["source_refs"]) != len(source_ids)
        or not isinstance(trace_value.get("steps"), list)
    ):
        raise ContractError("artifact_changed", "data trace does not match its normalized manifest")
    for trace_index, (source_id, trace_source) in enumerate(zip(source_ids, trace_value["source_refs"], strict=True)):
        pointer = f"/trace/source_refs/{trace_index}"
        trace_source_ref = validate_ref_shape(trace_source, pointer=pointer)
        source = source_records[source_id]
        if (
            not isinstance(source, Mapping)
            or source.get("source_id") != source_id
            or source.get("purpose") != "data"
        ):
            raise ContractError("artifact_changed", "DataManifest Source ID is not a verified data Source", pointer=pointer)
        source_bytes_ref = validate_ref_shape(source.get("bytes"), pointer=f"/sources/{source_id}/bytes")
        if (
            source_bytes_ref["media_type"] != "application/octet-stream"
            or trace_source_ref != source_bytes_ref
        ):
            raise ContractError("artifact_changed", "data trace Source Ref differs from its verified Source record", pointer=pointer)
        source_identity = (
            trace_source_ref["path"],
            trace_source_ref["sha256"],
            trace_source_ref["byte_len"],
            trace_source_ref["media_type"],
        )
        source_raw = verified_source_bytes.get(source_identity)
        if source_raw is None:
            source_raw = store.read_ref(trace_source_ref)
            verified_source_bytes[source_identity] = source_raw
        if (
            source.get("original_sha256") != trace_source_ref["sha256"]
            or source.get("original_size") != len(source_raw)
        ):
            raise ContractError("artifact_changed", "data trace Source bytes differ from the verified Source record", pointer=pointer)
        consumed.append(dict(trace_source_ref))
    for step_index, step in enumerate(trace_value["steps"]):
        if (
            not isinstance(step, dict)
            or set(step) != {"object_id", "column", "kind", "row", "source_id", "source_location", "basis", "input", "output"}
            or not isinstance(step.get("object_id"), str)
            or step["object_id"] not in planned_blocks
            or step.get("source_id") != planned_blocks[step["object_id"]]["source_id"]
            or not isinstance(step.get("column"), str)
            or not isinstance(step.get("kind"), str)
            or type(step.get("row")) is not int
            or step["row"] < 0
            or not isinstance(step.get("source_location"), str)
            or not isinstance(step.get("basis"), list)
        ):
            raise ContractError("output_invalid", "data conversion trace step is invalid", pointer=f"/trace/steps/{step_index}")
        locations = locations_by_object[step["object_id"]]
        if step["row"] >= len(locations) or step["source_location"] != locations[step["row"]]["locator"]:
            raise ContractError("artifact_changed", "data conversion trace locator differs from its normalized value", pointer=f"/trace/steps/{step_index}")
        if not any(
            conversion["object_id"] == step["object_id"]
            and conversion["column"] == step["column"]
            and conversion["kind"] == step["kind"]
            and conversion["basis"] == step["basis"]
            for conversion in import_plan["conversions"]
        ):
            raise ContractError("artifact_changed", "data conversion trace is not declared by its import plan", pointer=f"/trace/steps/{step_index}")
    consumed.append(dict(trace_ref))
    consumed.extend(object_refs)
    digest_material = {key: value for key, value in manifest.items() if key != "digest"}
    computed = sha256_bytes(canonical_json_bytes(digest_material))
    if computed != manifest["digest"]:
        raise ContractError("artifact_changed", "normalized data manifest digest does not match")
    binding_digest = candidate_binding_digest or manifest["digest"]
    bundle = DataBundle(
        data_id=manifest["data_id"],
        digest=manifest["digest"],
        loader_version=manifest["loader_version"],
        objects=MappingProxyType(loaded),
        consumed_inputs=tuple(consumed),
        candidate_binding_digest=binding_digest,
    )
    if instance_parameter_object_id is not None:
        return bundle.for_instance(instance_parameter_object_id, instance_key)
    if instance_key:
        raise ContractError("invalid_argument", "instance key provided without a parameter object")
    return bundle


def load_local_data_bundle(
    payload: Mapping[str, Any],
    *,
    candidate_binding_digest: str | None = None,
) -> DataBundle:
    """Build the same normalized objects from a visible editable inputs.json.

    Local values have no SourceRecord, trace, or values Ref: editing this file
    creates a new local input identity and does not claim the rows came from
    the original captured source. The values payload is validated by the same
    object constructor used by :func:`load_data_bundle`.
    """
    if not isinstance(payload, Mapping) or set(payload) != {
        "schema_version",
        "instance_parameter_object_id",
        "objects",
    } or type(payload.get("schema_version")) is not int or payload.get("schema_version") != 1:
        raise ContractError("output_invalid", "local normalized input has invalid fields")
    object_specs = payload["objects"]
    if not isinstance(object_specs, list):
        raise ContractError("output_invalid", "local normalized objects must be an array", pointer="/objects")
    loaded: dict[str, DataObject] = {}
    normalized_identity: list[dict[str, Any]] = []
    for index, spec in enumerate(object_specs):
        pointer = f"/objects/{index}"
        if not isinstance(spec, dict) or set(spec) != {"object_id", "kind", "key_fields", "columns", "values"}:
            raise ContractError("output_invalid", "local normalized object has invalid fields", pointer=pointer)
        object_id = spec["object_id"]
        if not isinstance(object_id, str) or not object_id or object_id in loaded:
            raise ContractError("output_invalid", "local normalized object identity is invalid or repeated", pointer=f"{pointer}/object_id")
        loaded[object_id] = _normalized_data_object(
            object_id=object_id,
            kind=spec["kind"],
            key_fields=spec["key_fields"],
            columns=spec["columns"],
            values=spec["values"],
            values_ref=None,
            source_locations=(),
            pointer=pointer,
        )
        normalized_values = spec["values"]
        if spec["kind"] == "json":
            json_value_bytes = loaded[object_id]._json_value_bytes
            if json_value_bytes is None:
                raise ContractError("output_invalid", "JSON data object has no normalized node", pointer=f"{pointer}/values")
            normalized_values = [strict_json_loads(json_value_bytes, max_bytes=len(json_value_bytes))]
        normalized_identity.append(
            {
                "object_id": object_id,
                "kind": spec["kind"],
                "key_fields": list(spec["key_fields"]),
                "columns": [dict(column) for column in spec["columns"]],
                "values": normalized_values,
            }
        )
    parameter_id = payload["instance_parameter_object_id"]
    if parameter_id is not None and (not isinstance(parameter_id, str) or not parameter_id):
        raise ContractError("output_invalid", "local instance parameter object identity is invalid", pointer="/instance_parameter_object_id")
    if parameter_id is not None:
        parameter = loaded.get(parameter_id)
        if parameter is None or parameter.kind != "table":
            raise ContractError("invalid_argument", "local instance parameter object must name a table", pointer="/instance_parameter_object_id")
    digest = sha256_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "instance_parameter_object_id": parameter_id,
                "objects": normalized_identity,
            }
        )
    )
    binding_digest = require_hash(candidate_binding_digest, pointer="/candidate_binding_digest") if candidate_binding_digest is not None else digest
    data_id = f"local-{digest}" if loaded else None
    return DataBundle(
        data_id=data_id,
        digest=digest,
        loader_version="optagent-local-data-v1",
        objects=MappingProxyType(loaded),
        consumed_inputs=(),
        candidate_binding_digest=binding_digest,
    )


def _normalized_data_object(
    *,
    object_id: Any,
    kind: Any,
    key_fields: Any,
    columns: Any,
    values: Any,
    values_ref: Mapping[str, Any] | None,
    source_locations: Sequence[Mapping[str, str]],
    pointer: str,
) -> DataObject:
    """Validate and construct one typed normalized object for both loaders."""
    if not isinstance(object_id, str) or not object_id:
        raise ContractError("output_invalid", "data object identity is invalid", pointer=f"{pointer}/object_id")
    if (
        not isinstance(kind, str)
        or kind not in _VALID_KINDS
        or not isinstance(key_fields, list)
        or any(not isinstance(field, str) or not field for field in key_fields)
        or len(set(key_fields)) != len(key_fields)
    ):
        raise ContractError("output_invalid", "data object key or kind is invalid", pointer=pointer)
    if kind in {"table", "relation"} and not key_fields:
        raise ContractError("output_invalid", "tabular object must declare key fields", pointer=f"{pointer}/key_fields")
    if not isinstance(columns, list):
        raise ContractError("output_invalid", "data object columns are invalid", pointer=f"{pointer}/columns")
    names: list[str] = []
    checked_columns: list[Mapping[str, Any]] = []
    for column_index, column in enumerate(columns):
        column_pointer = f"{pointer}/columns/{column_index}"
        if not isinstance(column, dict) or set(column) != {"name", "type", "unit", "nullable"}:
            raise ContractError("output_invalid", "data column has invalid fields", pointer=column_pointer)
        if (
            not isinstance(column["name"], str)
            or not column["name"]
            or column["name"] in names
            or not isinstance(column["type"], str)
            or column["type"] not in _VALID_TYPES
            or not isinstance(column["nullable"], bool)
            or (column["unit"] is not None and not isinstance(column["unit"], str))
        ):
            raise ContractError("output_invalid", "data column name or type is invalid", pointer=column_pointer)
        names.append(column["name"])
        checked_columns.append(MappingProxyType(dict(column)))
    unknown_keys = [field for field in key_fields if field not in names]
    if unknown_keys:
        raise ContractError(
            "output_invalid",
            "data object key fields must name declared columns",
            pointer=f"{pointer}/key_fields",
            details={"unknown_fields": unknown_keys},
        )
    if not isinstance(values, list):
        raise ContractError("output_invalid", "normalized values must be an array", pointer=f"{pointer}/values")
    keys: list[tuple[Any, ...]] = []
    seen_keys: set[tuple[Any, ...]] = set()
    rows: list[Any] = []
    json_value_bytes: bytes | None = None
    if kind == "json":
        if key_fields or columns:
            raise ContractError("output_invalid", "JSON data objects cannot declare keys or columns", pointer=pointer)
        if len(values) != 1:
            raise ContractError("output_invalid", "JSON values must contain exactly one selected node", pointer=f"{pointer}/values")
        try:
            normalized_node = normalize_json_value(values[0])
            json_value_bytes = canonical_json_bytes(normalized_node)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ContractError("output_invalid", "JSON data object contains an invalid JSON value", pointer=f"{pointer}/values/0") from exc
    elif kind in {"table", "relation"}:
        for row_num, row in enumerate(values):
            row_pointer = f"{pointer}/values/{row_num}"
            if not isinstance(row, dict):
                raise ContractError("output_invalid", "tabular values must be JSON records", pointer=row_pointer)
            if set(row) != set(names):
                raise ContractError("output_invalid", "row fields do not match declared columns", pointer=row_pointer)
            _check_row_types(row, columns, row_pointer)
            key = tuple(row[field] for field in key_fields)
            if any(value is None or type(value) not in (str, int) or value == "" for value in key):
                raise ContractError("output_invalid", "primary key must be non-empty string or integer", pointer=row_pointer)
            if key in seen_keys:
                raise ContractError("output_invalid", "duplicate primary key", pointer=row_pointer)
            keys.append(key)
            seen_keys.add(key)
            rows.append(MappingProxyType(dict(row)))
    elif kind == "scalar":
        if len(values) != 1:
            raise ContractError("output_invalid", "scalar values must contain one item", pointer=f"{pointer}/values")
        if len(columns) != 1:
            raise ContractError("output_invalid", "scalar must declare one column", pointer=f"{pointer}/columns")
        _check_value(values[0], columns[0], f"{pointer}/values/0")
        rows.append(values[0])
        keys.append(())
    else:
        if len(columns) != 1:
            raise ContractError("output_invalid", "set must declare one column", pointer=f"{pointer}/columns")
        for row_num, value in enumerate(values):
            _check_value(value, columns[0], f"{pointer}/values/{row_num}")
        if len(set(canonical_json_bytes(value) for value in values)) != len(values):
            raise ContractError("output_invalid", "set values must be unique", pointer=f"{pointer}/values")
        rows.extend(values)
        keys.extend((value,) for value in values)
    return DataObject(
        object_id=object_id,
        kind=kind,
        key_fields=tuple(key_fields),
        columns=tuple(checked_columns),
        values_ref=MappingProxyType(dict(values_ref)) if values_ref is not None else None,
        source_locations=tuple(MappingProxyType(dict(location)) for location in source_locations),
        rows=tuple(rows),
        keys=tuple(keys),
        _json_value_bytes=json_value_bytes,
    )


def _check_row_types(row: Mapping[str, Any], columns: Sequence[Mapping[str, Any]], pointer: str) -> None:
    by_name = {column["name"]: column for column in columns}
    if set(row) != set(by_name):
        raise ContractError("output_invalid", "row fields do not match declared columns", pointer=pointer)
    for name, value in row.items():
        _check_value(value, by_name[name], f"{pointer}/{name}")


def _check_value(value: Any, column: Mapping[str, Any], pointer: str) -> None:
    if value is None:
        if column["nullable"]:
            return
        raise ContractError("output_invalid", "null in non-nullable data column", pointer=pointer)
    kind = column["type"]
    if kind == "string":
        matches = isinstance(value, str)
    elif kind == "integer":
        matches = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "number":
        matches = _finite_number(value)
    elif kind == "boolean":
        matches = isinstance(value, bool)
    else:
        matches = isinstance(value, str) and _valid_date_text(value)
    if not matches:
        raise ContractError("output_invalid", "value does not match declared column type", pointer=pointer)


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _valid_date_text(value: str) -> bool:
    try:
        _dt.date.fromisoformat(value)
        return True
    except ValueError:
        try:
            _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return True
        except ValueError:
            return False


def normalize_data(
    store: DataWriter,
    *,
    data_id: str,
    source_records: Mapping[str, Mapping[str, Any]],
    source_bytes: Mapping[str, bytes] | None,
    plan: Mapping[str, Any],
    target_prefix: str,
) -> dict[str, Any]:
    """Normalize an explicit CSV/JSON/whitespace plan exactly once."""
    _validate_plan(plan, set(source_records))
    normalized_objects: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    bytes_cache: dict[tuple[str, str, int, str], bytes] = {}
    for block_index, block in enumerate(plan["blocks"]):
        source_id = block["source_id"]
        source_record = source_records[source_id]
        source_ref = validate_ref_shape(source_record.get("bytes"), pointer=f"/sources/{source_id}/bytes")
        byte_key = (source_ref["path"], source_ref["sha256"], source_ref["byte_len"], source_ref["media_type"])
        raw = bytes_cache.get(byte_key)
        if source_bytes is not None and source_id not in source_bytes:
            raise ContractError("not_found", "source bytes are unavailable", details={"source_id": source_id})
        supplied_raw = source_bytes.get(source_id) if source_bytes is not None else None
        if raw is None:
            raw = store.read_ref(source_ref)
            if supplied_raw is not None and supplied_raw != raw:
                raise ContractError("artifact_changed", "supplied source bytes do not match captured source")
            bytes_cache[byte_key] = raw
        elif supplied_raw is not None and supplied_raw != raw:
            raise ContractError("artifact_changed", "supplied source bytes conflict for one captured Ref")
        if source_record.get("origin", {}).get("transport_admission") == "unconfirmed_steer":
            raise ContractError("capture_unconfirmed", "unconfirmed input cannot be normalized as accepted facts")
        encoding = plan["encoding"]
        try:
            text = raw.decode("utf-8-sig" if encoding == "utf-8-sig" else "utf-8", errors="strict")
        except UnicodeError as exc:
            raise ContractError("invalid_argument", "source is not valid declared UTF-8", pointer=f"/blocks/{block_index}") from exc
        rows, row_locations = _parse_block(text, block)
        rows = _apply_conversions(rows, block, plan["conversions"], trace, source_id, row_locations)
        _validate_normalized_rows(block, rows, row_locations)
        values_raw = canonical_json_bytes(rows)
        values_ref = store.write_immutable(
            f"{target_prefix}/objects/{block['object_id']}.json",
            values_raw,
            "application/json",
        )
        normalized_objects.append(
            {
                "object_id": block["object_id"],
                "kind": block["kind"],
                "key_fields": list(block["key_fields"]),
                "columns": [
                    {
                        "name": col["name"],
                        "type": col["type"],
                        "unit": col.get("unit"),
                        "nullable": col["nullable"],
                    }
                    for col in block["columns"]
                ],
                "values": values_ref,
                "source_locations": [
                    {"source_id": source_id, "locator": loc} for loc in row_locations[: len(rows)]
                ],
            }
        )
    plan_source_ids = list(dict.fromkeys(block["source_id"] for block in plan["blocks"]))
    trace_ref = store.write_immutable(
        f"{target_prefix}/trace.json",
        canonical_json_bytes(
            {
                "schema_version": 1,
                "plan": dict(plan),
                "source_refs": [source_records[source_id]["bytes"] for source_id in plan_source_ids],
                "steps": trace,
            }
        ),
        "application/json",
    )
    manifest_base = {
        "data_id": data_id,
        "source_ids": plan_source_ids,
        "import_plan": dict(plan),
        "objects": normalized_objects,
        "trace": trace_ref,
        "loader_version": "optagent-data-v1",
    }
    digest = sha256_bytes(canonical_json_bytes(manifest_base))
    manifest = {**manifest_base, "digest": digest}
    manifest_ref = store.write_immutable(
        f"{target_prefix}/manifest.json", canonical_json_bytes(manifest), "application/json"
    )
    return {
        "manifest": manifest,
        "manifest_ref": manifest_ref,
        "trace_ref": trace_ref,
        "object_count": len(normalized_objects),
    }


def _validate_plan(plan: Mapping[str, Any], known_source_ids: set[str]) -> None:
    if set(plan) != {"encoding", "blocks", "conversions"}:
        raise ContractError("invalid_argument", "import plan has unknown or missing fields", pointer="/plan")
    if not isinstance(plan["encoding"], str) or plan["encoding"] not in {"utf-8", "utf-8-sig"}:
        raise ContractError("invalid_argument", "unsupported text encoding", pointer="/plan/encoding")
    if not isinstance(plan["blocks"], list) or not plan["blocks"] or not isinstance(plan["conversions"], list):
        raise ContractError("invalid_argument", "import plan needs blocks and conversions arrays", pointer="/plan")
    object_ids: set[str] = set()
    for index, block in enumerate(plan["blocks"]):
        ptr = f"/plan/blocks/{index}"
        required = {"object_id", "source_id", "format", "layout", "kind", "key_fields", "columns", "duplicate_policy", "missing_policy"}
        if not isinstance(block, dict) or set(block) != required:
            raise ContractError("invalid_argument", "import block has invalid fields", pointer=ptr)
        if not isinstance(block["object_id"], str) or not block["object_id"] or block["object_id"] in object_ids:
            raise ContractError("invalid_argument", "object_id is empty or repeated", pointer=f"{ptr}/object_id")
        object_ids.add(block["object_id"])
        if not isinstance(block["source_id"], str) or not block["source_id"]:
            raise ContractError("invalid_argument", "block source_id must be non-empty text", pointer=f"{ptr}/source_id")
        if block["source_id"] not in known_source_ids:
            raise ContractError("not_found", "block source_id is not registered", pointer=f"{ptr}/source_id")
        if not isinstance(block["format"], str) or not isinstance(block["kind"], str):
            raise ContractError("invalid_argument", "import block format and kind must be strings", pointer=ptr)
        if block["format"] in {"xlsx_values", "pdf_text"}:
            raise ContractError("capability_uncovered", "this import format is not enabled in the current ingestion batch", pointer=f"{ptr}/format")
        if block["format"] not in {"csv", "json", "whitespace_table"} or block["kind"] not in _VALID_KINDS:
            raise ContractError("invalid_argument", "unsupported import block format or kind", pointer=ptr)
        if (
            not isinstance(block["duplicate_policy"], str)
            or block["duplicate_policy"] != "error"
            or not isinstance(block["missing_policy"], str)
            or block["missing_policy"] not in {"error", "allow_nullable"}
        ):
            raise ContractError("invalid_argument", "unsupported duplicate or missing policy", pointer=ptr)
        if not isinstance(block["columns"], list):
            raise ContractError("invalid_argument", "block columns must be an array", pointer=f"{ptr}/columns")
        if (
            not isinstance(block["key_fields"], list)
            or any(not isinstance(key, str) or not key for key in block["key_fields"])
            or len(set(block["key_fields"])) != len(block["key_fields"])
        ):
            raise ContractError("invalid_argument", "key_fields must be unique non-empty column names", pointer=f"{ptr}/key_fields")
        colnames: set[str] = set()
        for col_index, col in enumerate(block["columns"]):
            cp = f"{ptr}/columns/{col_index}"
            if not isinstance(col, dict) or set(col) != {"name", "position", "input_name", "type", "unit", "nullable"}:
                raise ContractError("invalid_argument", "import column has invalid fields", pointer=cp)
            if not isinstance(col["name"], str) or not col["name"] or col["name"] in colnames:
                raise ContractError("invalid_argument", "import column name is empty or repeated", pointer=f"{cp}/name")
            colnames.add(col["name"])
            if not isinstance(col["type"], str) or col["type"] not in _VALID_TYPES or not isinstance(col["nullable"], bool):
                raise ContractError("invalid_argument", "import column type is invalid", pointer=cp)
            if col["unit"] is not None and not isinstance(col["unit"], str):
                raise ContractError("invalid_argument", "import column unit must be text or null", pointer=f"{cp}/unit")
            if (col["position"] is None) == (col["input_name"] is None):
                raise ContractError("invalid_argument", "exactly one column selector is required", pointer=cp)
            if col["position"] is not None and (
                isinstance(col["position"], bool)
                or not isinstance(col["position"], int)
                or col["position"] < 0
            ):
                raise ContractError("invalid_argument", "column position must be a non-negative integer", pointer=f"{cp}/position")
            if col["input_name"] is not None and (not isinstance(col["input_name"], str) or not col["input_name"]):
                raise ContractError("invalid_argument", "input_name must be non-empty text", pointer=f"{cp}/input_name")
        for key in block["key_fields"]:
            if key not in colnames:
                raise ContractError("invalid_argument", "key field is not a declared column", pointer=f"{ptr}/key_fields")
        if block["kind"] == "json":
            if block["format"] != "json" or block["key_fields"] or block["columns"]:
                raise ContractError("invalid_argument", "JSON kind requires format=json with empty keys and columns", pointer=ptr)
        elif not block["columns"]:
            raise ContractError("invalid_argument", "block must declare columns", pointer=f"{ptr}/columns")
        if block["kind"] in {"table", "relation"} and not block["key_fields"]:
            raise ContractError("invalid_argument", "table and relation need key fields", pointer=f"{ptr}/key_fields")
        if block["kind"] in {"scalar", "set"} and len(block["columns"]) != 1:
            raise ContractError(
                "invalid_argument",
                f"{block['kind']} import requires exactly one column",
                pointer=f"{ptr}/columns",
            )
        _validate_layout(block["format"], block["layout"], ptr)
    for index, conversion in enumerate(plan["conversions"]):
        ptr = f"/plan/conversions/{index}"
        if not isinstance(conversion, dict) or set(conversion) != {
            "object_id", "column", "kind", "factor", "offset", "format", "timezone", "basis"
        }:
            raise ContractError("invalid_argument", "conversion has invalid fields", pointer=ptr)
        if not isinstance(conversion["object_id"], str) or conversion["object_id"] not in object_ids:
            raise ContractError("invalid_argument", "conversion names an unknown object", pointer=f"{ptr}/object_id")
        if not isinstance(conversion["column"], str) or not conversion["column"]:
            raise ContractError("invalid_argument", "conversion column is required", pointer=f"{ptr}/column")
        block = next(item for item in plan["blocks"] if item["object_id"] == conversion["object_id"])
        if block["kind"] == "json":
            raise ContractError("invalid_argument", "JSON data objects do not support conversions", pointer=f"{ptr}/object_id")
        column = next((item for item in block["columns"] if item["name"] == conversion["column"]), None)
        if column is None:
            raise ContractError("invalid_argument", "conversion column is not declared", pointer=f"{ptr}/column")
        if not isinstance(conversion["kind"], str) or conversion["kind"] not in {"scale", "offset", "date_parse"} or not isinstance(conversion["basis"], list):
            raise ContractError("invalid_argument", "conversion kind or basis is invalid", pointer=ptr)
        if conversion["kind"] == "scale":
            _exact_nonnull(conversion, "factor", ptr)
            _exact_null(conversion, ("offset", "format", "timezone"), ptr)
        elif conversion["kind"] == "offset":
            _exact_nonnull(conversion, "offset", ptr)
            _exact_null(conversion, ("factor", "format", "timezone"), ptr)
        else:
            _exact_nonnull(conversion, "format", ptr)
            _exact_null(conversion, ("factor", "offset"), ptr)
            if not isinstance(conversion["format"], str) or not conversion["format"]:
                raise ContractError("invalid_argument", "date conversion format must be non-empty text", pointer=f"{ptr}/format")
            if conversion["timezone"] is not None and conversion["timezone"] != "UTC":
                raise ContractError("invalid_argument", "only explicit UTC timezone is supported", pointer=f"{ptr}/timezone")
            if column["type"] != "date":
                raise ContractError("invalid_argument", "date conversion needs a date column", pointer=f"{ptr}/column")
        if conversion["kind"] in {"scale", "offset"}:
            parameter_name = "factor" if conversion["kind"] == "scale" else "offset"
            parameter = conversion[parameter_name]
            if type(parameter) not in (int, float) or not _finite_number(parameter):
                raise ContractError("invalid_argument", "numeric conversion parameter must be finite", pointer=f"{ptr}/{parameter_name}")
            if column["type"] not in {"integer", "number"}:
                raise ContractError("invalid_argument", "numeric conversion needs an integer or number column", pointer=f"{ptr}/column")
            if column["type"] == "integer" and type(parameter) is not int:
                raise ContractError("invalid_argument", "integer conversions require integer arithmetic; declare number for fractional conversion", pointer=f"{ptr}/{parameter_name}")
    return None


def _exact_nonnull(obj: Mapping[str, Any], key: str, pointer: str) -> None:
    if obj.get(key) is None:
        raise ContractError("invalid_argument", f"{key} is required for this conversion", pointer=f"{pointer}/{key}")


def _exact_null(obj: Mapping[str, Any], keys: Iterable[str], pointer: str) -> None:
    for key in keys:
        if obj.get(key) is not None:
            raise ContractError("invalid_argument", f"{key} is unused for this conversion", pointer=f"{pointer}/{key}")


def _validate_layout(fmt: str, layout: Any, pointer: str) -> None:
    if not isinstance(layout, dict):
        raise ContractError("invalid_argument", "layout must be an object", pointer=f"{pointer}/layout")
    shapes = {
        "csv": {"delimiter", "header", "skip_lines"},
        "json": {"pointer"},
        "whitespace_table": {"skip_lines", "header", "take_rows", "skip_blank_lines", "field_count"},
    }
    actual_fields = set(layout)
    expected_fields = shapes[fmt]
    missing = sorted(expected_fields - actual_fields)
    unknown = sorted(actual_fields - expected_fields)
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown fields: {', '.join(unknown)}")
        field = (missing or unknown)[0]
        raise ContractError(
            "invalid_argument",
            "layout has " + "; ".join(details),
            pointer=f"{pointer}/layout/{field}",
        )
    if fmt == "csv":
        _validate_skip_lines(layout["skip_lines"], f"{pointer}/layout/skip_lines")
        if (
            not isinstance(layout["delimiter"], str)
            or len(layout["delimiter"]) != 1
            or not isinstance(layout["header"], str)
            or layout["header"] not in {"first_row", "none"}
        ):
            raise ContractError("invalid_argument", "CSV layout is invalid", pointer=f"{pointer}/layout")
    elif fmt == "json":
        if (
            not isinstance(layout["pointer"], str)
            or (layout["pointer"] and not layout["pointer"].startswith("/"))
            or any(re.search(r"~(?![01])", part) for part in layout["pointer"].split("/"))
        ):
            raise ContractError("invalid_argument", "JSON pointer must be RFC6901", pointer=f"{pointer}/layout/pointer")
    else:
        if (
            not isinstance(layout["skip_blank_lines"], bool)
            or not isinstance(layout["header"], str)
            or layout["header"] not in {"none", "first_row"}
        ):
            raise ContractError("invalid_argument", "whitespace layout is invalid", pointer=f"{pointer}/layout")
        _validate_skip_lines(layout["skip_lines"], f"{pointer}/layout/skip_lines")
        if isinstance(layout["field_count"], bool) or not isinstance(layout["field_count"], int) or layout["field_count"] < 1:
            raise ContractError("invalid_argument", "field_count must be positive", pointer=f"{pointer}/layout/field_count")
        if layout["take_rows"] is not None and (isinstance(layout["take_rows"], bool) or not isinstance(layout["take_rows"], int) or layout["take_rows"] < 0):
            raise ContractError("invalid_argument", "take_rows must be null or non-negative", pointer=f"{pointer}/layout/take_rows")


def _validate_skip_lines(value: Any, pointer: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError("invalid_argument", "skip_lines must be a non-negative integer", pointer=pointer)


def _parse_block(text: str, block: Mapping[str, Any]) -> tuple[Any, list[str]]:
    fmt = block["format"]
    layout = block["layout"]
    columns = block["columns"]
    if fmt == "json":
        try:
            value = strict_json_loads(text, max_bytes=len(text.encode("utf-8")))
        except (ValueError, UnicodeError) as exc:
            raise ContractError("invalid_argument", "JSON source is not strict finite JSON") from exc
        selected = _json_pointer(value, layout["pointer"])
        if block["kind"] == "json":
            locator = f"JSON pointer {layout['pointer'] or '(root)'}"
            return [selected], [locator]
        if block["kind"] == "scalar":
            values = [selected]
        elif block["kind"] == "set":
            if not isinstance(selected, list):
                raise ContractError("invalid_argument", "JSON set pointer must select an array")
            values = selected
        elif isinstance(selected, list):
            values = selected
        else:
            raise ContractError("invalid_argument", "JSON table pointer must select an array")
        row_locs = [f"JSON pointer {layout['pointer']} item {i}" for i in range(len(values))]
        if block["kind"] in {"table", "relation"}:
            rows = [_select_json_row(item, columns, f"/blocks/{block['object_id']}/{i}") for i, item in enumerate(values)]
            return rows, row_locs
        return [_convert_json_value(v, columns[0], f"/blocks/{block['object_id']}/{i}") for i, v in enumerate(values)], row_locs
    if fmt == "csv":
        return _parse_csv(text, block)
    return _parse_whitespace(text, block)


def _parse_csv(text: str, block: Mapping[str, Any]) -> tuple[list[Any], list[str]]:
    layout = block["layout"]
    physical = text.splitlines(keepends=True)
    if len(physical) < layout["skip_lines"]:
        raise ContractError("invalid_argument", "CSV skip_lines exceeds source length")
    skipped = sum(1 for _ in physical[: layout["skip_lines"]])
    stream = io.StringIO("".join(physical[layout["skip_lines"] :]), newline="")
    reader = csv.reader(stream, delimiter=layout["delimiter"], strict=True)
    source_header: list[str] | None = None
    records: list[tuple[int, int, list[str]]] = []
    try:
        last_reader_line = 0
        for row in reader:
            end_line = skipped + reader.line_num
            start_line = skipped + last_reader_line + 1
            last_reader_line = reader.line_num
            if source_header is None and layout["header"] == "first_row":
                source_header = row
            else:
                records.append((start_line, end_line, row))
    except csv.Error as exc:
        raise ContractError("invalid_argument", "CSV syntax is invalid") from exc
    if layout["header"] == "first_row":
        if not source_header:
            raise ContractError("invalid_argument", "CSV header is missing")
        if any(not field for field in source_header) or len(set(source_header)) != len(source_header):
            raise ContractError("invalid_argument", "CSV header has empty or duplicate fields")
    selected: list[Any] = []
    locs: list[str] = []
    for start_line, end_line, row in records:
        if not row:
            continue
        locator = f"line {start_line}" if start_line == end_line else f"lines {start_line}-{end_line}"
        selected.append(_select_tabular_row(row, source_header, block, locator))
        locs.append(locator)
    return selected, locs


def _parse_whitespace(text: str, block: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    layout = block["layout"]
    lines = text.splitlines()
    if len(lines) < layout["skip_lines"]:
        raise ContractError("invalid_argument", "whitespace skip_lines exceeds source length")
    header: list[str] | None = None
    rows: list[tuple[int, list[str]]] = []
    for row_no, raw_line in enumerate(lines[layout["skip_lines"] :], start=layout["skip_lines"] + 1):
        if (
            layout["take_rows"] is not None
            and len(rows) >= layout["take_rows"]
            and (layout["header"] == "none" or header is not None)
        ):
            break
        if not raw_line.strip():
            if layout["skip_blank_lines"]:
                continue
            raise ContractError("invalid_argument", "blank line is not allowed", pointer=f"/line/{row_no}")
        fields = raw_line.split()
        if len(fields) != layout["field_count"]:
            raise ContractError("invalid_argument", "whitespace row has unexpected field count", pointer=f"/line/{row_no}")
        if header is None and layout["header"] == "first_row":
            header = fields
            if len(set(header)) != len(header):
                raise ContractError("invalid_argument", "whitespace header fields repeat", pointer=f"/line/{row_no}")
            continue
        rows.append((row_no, fields))
    if layout["header"] == "first_row" and header is None:
        raise ContractError("invalid_argument", "whitespace header is missing")
    result = [_select_tabular_row(row, header, block, f"line {line_no}") for line_no, row in rows]
    return result, [f"line {line_no}" for line_no, _ in rows]


def _select_tabular_row(
    row: Sequence[str], header: Sequence[str] | None, block: Mapping[str, Any], locator: str
) -> Any:
    output: dict[str, Any] = {}
    for column in block["columns"]:
        if column["input_name"] is not None:
            if header is None or column["input_name"] not in header:
                raise ContractError("invalid_argument", "named input column has no matching header", pointer=f"/columns/{column['name']}")
            index = list(header).index(column["input_name"])
        else:
            index = column["position"]
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise ContractError("invalid_argument", "column position must be non-negative", pointer=f"/columns/{column['name']}")
        if index >= len(row):
            if column["nullable"] and block["missing_policy"] == "allow_nullable":
                token: Any = None
            else:
                raise ContractError(
                    "invalid_argument",
                    "source row is missing a required field",
                    pointer=f"/columns/{column['name']}",
                    details={"position": index, "column_count": len(row)},
                )
        else:
            token = row[index]
            if token == "":
                if column["nullable"] and block["missing_policy"] == "allow_nullable":
                    token = None
                else:
                    raise ContractError("invalid_argument", "missing value is not allowed", pointer=f"{locator}/{column['name']}")
        output[column["name"]] = _convert_text_value(token, column, f"{locator}/{column['name']}")
    if block["kind"] == "scalar":
        return output[block["columns"][0]["name"]]
    if block["kind"] == "set":
        return output[block["columns"][0]["name"]]
    return output


def _convert_text_value(token: Any, column: Mapping[str, Any], locator: str) -> Any:
    if token is None:
        if column["nullable"]:
            return None
        raise ContractError("invalid_argument", "missing value is not allowed", pointer=locator)
    kind = column["type"]
    if kind == "string":
        value = token
    elif kind == "integer":
        if not isinstance(token, str) or not token or token.strip() != token or not re_integer(token):
            raise ContractError("invalid_argument", "value is not an exact integer token", pointer=locator)
        value = int(token, 10)
    elif kind == "number":
        if not isinstance(token, str) or not token or token.strip() != token:
            raise ContractError("invalid_argument", "value is not an exact number token", pointer=locator)
        try:
            value = float(token)
        except ValueError as exc:
            raise ContractError("invalid_argument", "value is not a number", pointer=locator) from exc
        if not math.isfinite(value):
            raise ContractError("invalid_argument", "number must be finite", pointer=locator)
    elif kind == "boolean":
        if token not in {"true", "false"}:
            raise ContractError("invalid_argument", "boolean must be true or false", pointer=locator)
        value = token == "true"
    else:
        if not isinstance(token, str):
            raise ContractError("invalid_argument", "date token must be text", pointer=locator)
        value = token
        if not _valid_date_text(value):
            raise ContractError("invalid_argument", "date is not ISO formatted", pointer=locator)
    return value


def re_integer(value: str) -> bool:
    if value.startswith(("+", "-")):
        return value[1:].isdigit()
    return value.isdigit()


def _convert_json_value(value: Any, column: Mapping[str, Any], pointer: str) -> Any:
    if value is None and column["nullable"]:
        return None
    _check_value(value, {**column, "nullable": False}, pointer)
    return value


def _select_json_row(value: Any, columns: Sequence[Mapping[str, Any]], pointer: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("invalid_argument", "JSON table item must be an object", pointer=pointer)
    expected = {column["name"] for column in columns}
    if set(value) != expected:
        raise ContractError("invalid_argument", "JSON row fields do not match declared columns", pointer=pointer)
    return {column["name"]: _convert_json_value(value[column["name"]], column, f"{pointer}/{column['name']}") for column in columns}


def _json_pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    current = value
    for encoded in pointer[1:].split("/"):
        if re.search(r"~(?![01])", encoded):
            raise ContractError("invalid_argument", "invalid RFC6901 escape", pointer="/layout/pointer")
        part = encoded.replace("~1", "/").replace("~0", "~")
        try:
            if isinstance(current, list):
                if (
                    not part
                    or any(char not in "0123456789" for char in part)
                    or (part.startswith("0") and part != "0")
                ):
                    raise KeyError
                current = current[int(part)]
            elif isinstance(current, dict):
                current = current[part]
            else:
                raise KeyError
        except (KeyError, IndexError, ValueError) as exc:
            raise ContractError("not_found", "JSON pointer did not resolve", pointer="/layout/pointer") from exc
    return current


def _apply_conversions(
    rows: Any,
    block: Mapping[str, Any],
    conversions: Sequence[Mapping[str, Any]],
    trace: list[dict[str, Any]],
    source_id: str,
    locs: Sequence[str],
) -> Any:
    matching = [conv for conv in conversions if conv["object_id"] == block["object_id"]]
    if not matching:
        return rows
    result = list(rows)
    for conv in matching:
        column_name = conv["column"]
        if column_name not in {col["name"] for col in block["columns"]}:
            raise ContractError("invalid_argument", "conversion column is not declared", pointer="/plan/conversions")
        for index, row in enumerate(result):
            raw = row if block["kind"] == "scalar" else (row[column_name] if isinstance(row, dict) else row)
            if raw is None:
                continue
            if conv["kind"] in {"scale", "offset"}:
                column = next(col for col in block["columns"] if col["name"] == column_name)
                if column["type"] == "integer":
                    valid_input = type(raw) is int
                else:
                    valid_input = _finite_number(raw)
                if not valid_input:
                    raise ContractError("invalid_argument", "numeric conversion needs finite numeric input", pointer=f"/plan/conversions/{column_name}")
                try:
                    converted = raw * conv["factor"] if conv["kind"] == "scale" else raw + conv["offset"]
                except (OverflowError, TypeError, ValueError) as exc:
                    raise ContractError("invalid_argument", "numeric conversion overflowed or has incompatible types", pointer=f"/plan/conversions/{column_name}") from exc
                valid_output = type(converted) is int if column["type"] == "integer" else _finite_number(converted)
                if not valid_output:
                    raise ContractError("invalid_argument", "numeric conversion produced a non-finite value", pointer=f"/plan/conversions/{column_name}")
            else:
                if not isinstance(raw, str):
                    raise ContractError("invalid_argument", "date conversion needs text input", pointer=f"/plan/conversions/{column_name}")
                converted = _parse_explicit_date(raw, conv)
            if isinstance(row, dict):
                row = dict(row)
                row[column_name] = converted
                result[index] = row
            else:
                result[index] = converted
            trace.append(
                {
                    "object_id": block["object_id"],
                    "column": column_name,
                    "kind": conv["kind"],
                    "row": index,
                    "source_id": source_id,
                    "source_location": locs[index] if index < len(locs) else "source-level",
                    "basis": conv["basis"],
                    "input": raw,
                    "output": converted,
                }
            )
    return result


def _parse_explicit_date(value: str, conversion: Mapping[str, Any]) -> str:
    try:
        parsed = _dt.datetime.strptime(value, conversion["format"])
    except ValueError as exc:
        try:
            date_value = _dt.datetime.strptime(value, conversion["format"]).date()
            return date_value.isoformat()
        except ValueError:
            raise ContractError("invalid_argument", "date does not match explicit format") from exc
    timezone = conversion["timezone"]
    if timezone is not None:
        if parsed.tzinfo is None:
            if timezone.upper() != "UTC":
                raise ContractError("invalid_argument", "only explicit UTC timezone is supported")
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        parsed = parsed.astimezone(_dt.timezone.utc)
        return parsed.isoformat().replace("+00:00", "Z")
    return parsed.isoformat()


def _validate_normalized_rows(block: Mapping[str, Any], rows: Any, locs: Sequence[str]) -> None:
    kind = block["kind"]
    if kind == "json":
        if len(rows) != 1:
            raise ContractError("invalid_argument", "JSON import must select exactly one node")
        try:
            normalize_json_value(rows[0])
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ContractError("invalid_argument", "selected JSON node is not a finite JSON value") from exc
        return
    if kind == "scalar":
        if len(rows) != 1:
            raise ContractError("invalid_argument", "scalar import must produce exactly one value")
        _check_value(rows[0], block["columns"][0], "/values/0")
        return
    if kind == "set":
        for index, value in enumerate(rows):
            _check_value(value, block["columns"][0], f"/values/{index}")
        if len(set(canonical_json_bytes(v) for v in rows)) != len(rows):
            raise ContractError("invalid_argument", "set contains a duplicate value")
        return
    names = {col["name"] for col in block["columns"]}
    seen: set[tuple[Any, ...]] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != names:
            raise ContractError("invalid_argument", "row does not match declared columns", pointer=f"/line/{locs[index] if index < len(locs) else index}")
        for col in block["columns"]:
            _check_value(row[col["name"]], col, f"/line/{locs[index] if index < len(locs) else index}/{col['name']}")
        key = tuple(row[field] for field in block["key_fields"])
        if any(value is None or type(value) not in (str, int) or value == "" for value in key):
            raise ContractError("invalid_argument", "key fields must be non-empty strings or integers", pointer=f"/line/{locs[index] if index < len(locs) else index}")
        if key in seen:
            raise ContractError("invalid_argument", "duplicate primary key", pointer=f"/line/{locs[index] if index < len(locs) else index}")
        seen.add(key)
