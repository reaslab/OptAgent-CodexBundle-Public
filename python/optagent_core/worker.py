"""Fixed, evidence-producing native Pyomo worker entry point.

This module is copied as part of the small verified runtime closure. It has no
CaseStore, HTTP, provider, or Core-state dependency. The only permitted writes
are below the output directory fixed in WorkerInvocation.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from .contracts import (
    ContractError,
    canonical_json_bytes,
    file_signature,
    normalize_json_value,
    publish_no_clobber,
    ref_from_bytes,
    require_hash,
    require_id,
    sha256_bytes,
    strict_json_loads,
    validate_ref_shape,
    windows_long_path,
)
from .data import DataBundle, DataReader, load_data_bundle, read_prepared_source_records
from .models import build_native_model, call_frozen_function, is_native_cp_model_build, native_variable_binding_rows
from .results import (
    CheckedModel,
    HighsObservation,
    ObjectiveSpec,
    business_check_uncovered,
    check_pyomo_model,
    check_native_cp_sat_model,
    checked_lexicographic_lock,
    empty_domain,
    objective_for_layer,
    objective_values_match,
    objective_values_exactly_match,
    result_record,
    direct_witness_from_output,
    business_check_is_fully_passed,
    _backend_versions,
    run_highs_once,
)


_LEGACY_RUNTIME_FILES = (
    "python/optagent_core/__init__.py",
    "python/optagent_core/contracts.py",
    "python/optagent_core/data.py",
    "python/optagent_core/models.py",
    "python/optagent_core/results.py",
    "python/optagent_core/worker.py",
)
_RUNTIME_FILES = (
    "python/optagent_core/__init__.py",
    "python/optagent_core/contracts.py",
    "python/optagent_core/data.py",
    "python/optagent_core/models.py",
    "python/optagent_core/results.py",
    "python/optagent_core/solver_protocol.py",
    "python/optagent_core/worker.py",
)
_INVOCATION_FIELDS = {
    "schema_version",
    "stamp",
    "case_id",
    "prepared_id",
    "run_id",
    "dispatch_nonce",
    "input_manifest",
    "input_digest",
    "output_path",
}
_PREPARED_MANIFEST_FIELDS = {
    "schema_version",
    "prepared_id",
    "case_id",
    "source_revision",
    "candidate_revision",
    "candidate_id",
    "promotion_generation",
    "binding_digest",
    "sources",
    "candidate",
    "data",
    "runtime_bundle",
    "solve_options",
}
_OUTPUT_REFS: dict[str, dict[str, dict[str, Any]]] = {}


def _remember_output(output_path: str, ref: Mapping[str, Any]) -> None:
    outputs = _OUTPUT_REFS.setdefault(output_path, {})
    previous = outputs.get(str(ref["path"]))
    if previous is not None and previous != dict(ref):
        raise ContractError("artifact_changed", "one worker output path has conflicting content refs")
    outputs[str(ref["path"])] = dict(ref)


class FrozenReader(DataReader):
    """Read-only reader restricted to one prepared input tree."""

    def __init__(self, workspace: Path, case_id: str, prepared_id: str) -> None:
        self.workspace = workspace
        self.prefix = f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/input/"

    def read_ref(self, ref: Mapping[str, Any]) -> bytes:
        validated = validate_ref_shape(dict(ref))
        relative = _safe_relative(validated["path"])
        if not validated["path"].startswith(self.prefix):
            raise ContractError("path_denied", "worker read escaped the frozen input tree")
        raw = _read_regular(self.workspace, relative)
        if len(raw) != validated["byte_len"] or sha256_bytes(raw) != validated["sha256"]:
            raise ContractError("artifact_changed", "frozen input bytes do not match their reference")
        return raw


def _safe_relative(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ContractError("path_denied", "worker path is not a safe workspace-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ContractError("path_denied", "worker path escapes the workspace")
    return tuple(path.parts)


def _path(workspace: Path, relative: str) -> Path:
    parts = _safe_relative(relative)
    return workspace.joinpath(*parts)


def _is_link_or_reparse(info: os.stat_result) -> bool:
    reparse = bool(getattr(info, "st_file_attributes", 0) & 0x400)
    return stat.S_ISLNK(info.st_mode) or reparse


@contextmanager
def _open_stable_binary(workspace: Path, parts: Sequence[str]):
    """Open one regular file only if the checked path and opened fd are identical."""
    workspace = windows_long_path(workspace)
    if not parts:
        raise ContractError("path_denied", "worker path does not name a file")
    cursor = workspace
    chain: list[tuple[Path, os.stat_result, bool]] = []
    for index, part in enumerate(parts):
        cursor = cursor / part
        try:
            info = cursor.lstat()
        except OSError as exc:
            raise ContractError("not_found", "worker input file is unavailable") from exc
        final = index == len(parts) - 1
        expected_type = stat.S_ISREG(info.st_mode) if final else stat.S_ISDIR(info.st_mode)
        if _is_link_or_reparse(info) or not expected_type:
            raise ContractError("path_denied", "worker path contains a link or non-regular component")
        chain.append((cursor, info, final))
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        cursor.relative_to(workspace)
        fd = os.open(cursor, flags)
    except (OSError, ValueError) as exc:
        raise ContractError("path_denied", "worker input path could not be opened safely") from exc
    try:
        stream = os.fdopen(fd, "rb", closefd=False)
    except OSError as exc:
        os.close(fd)
        raise ContractError("path_denied", "worker input file descriptor could not be read") from exc
    try:
        opened: os.stat_result | None = None
        try:
            opened = os.fstat(fd)
            expected = chain[-1][1]
            if not stat.S_ISREG(opened.st_mode) or file_signature(opened) != file_signature(expected):
                raise ContractError("source_changed_during_read", "opened worker file differs from the checked path")
            yield stream
        finally:
            try:
                after_fd = os.fstat(fd)
                if opened is not None and file_signature(opened) != file_signature(after_fd):
                    raise ContractError("source_changed_during_read", "worker file changed while being read")
                for path, before_path, final in chain:
                    after_path = path.lstat()
                    if _is_link_or_reparse(after_path) or file_signature(before_path) != file_signature(after_path):
                        raise ContractError("source_changed_during_read", "worker path changed while being read")
                    if final and file_signature(after_fd) != file_signature(after_path):
                        raise ContractError("source_changed_during_read", "opened worker file no longer matches its path")
            except OSError as exc:
                raise ContractError("source_changed_during_read", "worker path changed while being read") from exc
            finally:
                stream.close()
                os.close(fd)
    except ContractError:
        raise
    except OSError as exc:
        raise ContractError("path_denied", "worker input file could not be read safely") from exc


def _read_regular(workspace: Path, parts: Sequence[str]) -> bytes:
    with _open_stable_binary(workspace, parts) as stream:
        expected_size = os.fstat(stream.fileno()).st_size
        raw = stream.read()
    if len(raw) != expected_size:
        raise ContractError("source_changed_during_read", "worker file size changed while being read")
    return raw


def _mkdir_checked(workspace: Path, relative: str) -> Path:
    parts = _safe_relative(relative)
    cursor = workspace
    for part in parts:
        cursor = cursor / part
        try:
            cursor.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            raise ContractError("path_denied", "worker output directory could not be created") from exc
        try:
            info = cursor.lstat()
        except OSError as exc:
            raise ContractError("path_denied", "worker output path could not be checked") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ContractError("path_denied", "worker output path contains a link")
    return cursor


def _write_output(workspace: Path, output_path: str, name: str, raw: bytes, media_type: str) -> dict[str, Any]:
    output_parts = _safe_relative(output_path)
    if (
        len(output_parts) != 7
        or output_parts[0:3] != (".reaslab", "optagent", "cases")
        or output_parts[4] != "runs"
        or output_parts[6] != "output"
    ):
        raise ContractError("path_denied", "worker own-output path is not in the fixed run location")
    name_parts = _safe_relative(name)
    base = "/".join((*output_parts, *name_parts[:-1]))
    if name_parts[:-1]:
        _mkdir_checked(workspace, base)
    target_rel = "/".join((*output_parts, *name_parts))
    target = _path(workspace, target_rel)
    _mkdir_checked(workspace, output_path)
    try:
        target.parent.relative_to(Path(os.path.abspath(os.fspath(workspace))))
    except ValueError as exc:
        raise ContractError("path_denied", "worker output escaped its own-output directory") from exc
    if target.exists():
        existing = _read_regular(workspace, _safe_relative(target_rel))
        if existing != raw:
            raise ContractError("artifact_changed", "worker output path already contains different bytes")
    else:
        temp = target.with_name(f".{target.name}.{os.urandom(8).hex()}.tmp")
        try:
            with open(temp, "xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if temp.is_symlink() or target.parent.is_symlink():
                raise ContractError("package_collision", "worker output path changed during write")
            if not publish_no_clobber(temp, target):
                existing = _read_regular(workspace, _safe_relative(target_rel))
                if existing != raw:
                    raise ContractError("artifact_changed", "worker output path already contains different bytes")
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
    ref = ref_from_bytes(target_rel, raw, media_type)
    _remember_output(output_path, ref)
    return ref


def _write_stream_output(
    workspace: Path,
    output_path: str,
    name: str,
    chunks: Iterable[bytes],
    media_type: str,
) -> dict[str, Any]:
    """Write a large own-output artifact without retaining it in memory."""
    output_parts = _safe_relative(output_path)
    if (
        len(output_parts) != 7
        or output_parts[0:3] != (".reaslab", "optagent", "cases")
        or output_parts[4] != "runs"
        or output_parts[6] != "output"
    ):
        raise ContractError("path_denied", "worker own-output path is not in the fixed run location")
    name_parts = _safe_relative(name)
    target_rel = "/".join((*output_parts, *name_parts))
    _mkdir_checked(workspace, output_path)
    if name_parts[:-1]:
        _mkdir_checked(workspace, "/".join((*output_parts, *name_parts[:-1])))
    target = _path(workspace, target_rel)
    parent = target.parent
    try:
        parent.relative_to(Path(os.path.abspath(os.fspath(workspace))))
    except ValueError as exc:
        raise ContractError("path_denied", "worker output escaped its own-output directory") from exc

    temp = target.with_name(f".{target.name}.{os.urandom(8).hex()}.tmp")
    digest = hashlib.sha256()
    byte_len = 0
    try:
        with open(temp, "xb") as stream:
            for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise ContractError("output_invalid", "streamed worker output must be bytes")
                stream.write(chunk)
                digest.update(chunk)
                byte_len += len(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        sha256 = digest.hexdigest()
        if temp.is_symlink() or target.parent.is_symlink():
            raise ContractError("package_collision", "worker output path changed during write")
        if not publish_no_clobber(temp, target):
            existing = _hash_regular_file(workspace, _safe_relative(target_rel))
            if existing != (byte_len, sha256):
                raise ContractError("artifact_changed", "worker output path already contains different bytes")
        ref = {"path": target_rel, "sha256": sha256, "byte_len": byte_len, "media_type": media_type}
        _remember_output(output_path, ref)
        return ref
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _hash_regular_file(workspace: Path, parts: Sequence[str]) -> tuple[int, str]:
    """Hash a regular workspace file incrementally after checking every path component."""
    digest = hashlib.sha256()
    byte_len = 0
    expected_size: int | None = None
    try:
        with _open_stable_binary(workspace, parts) as stream:
            expected_size = os.fstat(stream.fileno()).st_size
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                byte_len += len(chunk)
    except OSError as exc:
        raise ContractError("path_denied", "worker file could not be hashed") from exc
    if expected_size is None or byte_len != expected_size:
        raise ContractError("source_changed_during_read", "worker file size changed while hashing")
    return byte_len, digest.hexdigest()


def _json_output(workspace: Path, output_path: str, name: str, value: Mapping[str, Any]) -> dict[str, Any]:
    return _write_output(workspace, output_path, name, canonical_json_bytes(dict(value)), "application/json")


class _OutputTarget:
    """A single output root shared by the platform and local compute paths."""

    def __init__(self, *, workspace: Path | None, output_path: str | None, local_root: Path | None) -> None:
        self.workspace = workspace
        self.output_path = output_path
        self.local_root = local_root
        self.local = local_root is not None
        self.refs: dict[str, dict[str, Any]] = {}

    @classmethod
    def platform(cls, workspace: Path, output_path: str) -> _OutputTarget:
        return cls(workspace=workspace, output_path=output_path, local_root=None)

    @staticmethod
    def checked_local_root(output_directory: str | os.PathLike[str]) -> Path:
        requested = Path(output_directory)
        if not requested.is_absolute():
            raise ContractError("path_denied", "local reproduction output directory must be absolute")
        logical_root = Path(os.path.abspath(os.fspath(requested)))
        # Preserve relative artifact names while using a Windows-capable path
        # for all physical I/O rooted at this local output directory.
        root = windows_long_path(logical_root)
        if os.path.lexists(root):
            raise ContractError("package_collision", "local reproduction output directory already exists")
        parent = root.parent
        cursor = Path(parent.anchor)
        if not cursor.anchor:
            raise ContractError("path_denied", "local reproduction output parent is not absolute")
        for part in parent.parts[1:]:
            cursor = cursor / part
            try:
                info = cursor.lstat()
            except OSError as exc:
                raise ContractError("not_found", "local reproduction output parent is unavailable") from exc
            if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
                raise ContractError("path_denied", "local reproduction output parent contains a link or non-directory")
        return root

    @classmethod
    def create_local(cls, output_directory: str | os.PathLike[str]) -> _OutputTarget:
        root = cls.checked_local_root(output_directory)
        try:
            root.mkdir()
            root_info = root.lstat()
        except FileExistsError as exc:
            raise ContractError("package_collision", "local reproduction output directory already exists") from exc
        except OSError as exc:
            raise ContractError("path_denied", "local reproduction output directory could not be created") from exc
        if _is_link_or_reparse(root_info) or not stat.S_ISDIR(root_info.st_mode):
            raise ContractError("path_denied", "local reproduction output root is not a regular directory")
        return cls(workspace=None, output_path=None, local_root=root)

    def _relative(self, name: str) -> tuple[str, ...]:
        name_parts = _safe_relative(name)
        if self.local:
            return name_parts
        assert self.output_path is not None
        return (*_safe_relative(self.output_path), *name_parts)

    def path(self, name: str) -> Path:
        parts = self._relative(name)
        if self.local:
            assert self.local_root is not None
            target = self.local_root.joinpath(*parts)
            try:
                target.relative_to(self.local_root)
            except ValueError as exc:
                raise ContractError("path_denied", "local reproduction output escaped its output root") from exc
            return target
        assert self.workspace is not None
        return self.workspace.joinpath(*parts)

    def ensure_dir(self, name: str = "") -> Path:
        if not self.local:
            assert self.workspace is not None and self.output_path is not None
            relative = self.output_path if not name else f"{self.output_path}/{name}"
            return _mkdir_checked(self.workspace, relative)
        assert self.local_root is not None
        root_info = self.local_root.lstat()
        if _is_link_or_reparse(root_info) or not stat.S_ISDIR(root_info.st_mode):
            raise ContractError("path_denied", "local reproduction output root changed")
        cursor = self.local_root
        parts = () if not name else _safe_relative(name)
        for part in parts:
            cursor = cursor / part
            try:
                cursor.mkdir()
            except FileExistsError:
                pass
            except OSError as exc:
                raise ContractError("path_denied", "local reproduction output subdirectory could not be created") from exc
            try:
                info = cursor.lstat()
            except OSError as exc:
                raise ContractError("path_denied", "local reproduction output path could not be checked") from exc
            if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
                raise ContractError("path_denied", "local reproduction output contains a link or non-directory")
        return cursor

    def _remember(self, ref: Mapping[str, Any]) -> dict[str, Any]:
        item = dict(ref)
        previous = self.refs.get(str(item["path"]))
        if previous is not None and previous != item:
            raise ContractError("artifact_changed", "one local output path has conflicting content refs")
        self.refs[str(item["path"])] = item
        if not self.local:
            assert self.output_path is not None
            _remember_output(self.output_path, item)
        return item

    def write(self, name: str, raw: bytes, media_type: str) -> dict[str, Any]:
        if not self.local:
            assert self.workspace is not None and self.output_path is not None
            return self._remember(_write_output(self.workspace, self.output_path, name, raw, media_type))
        parts = _safe_relative(name)
        parent_name = "/".join(parts[:-1])
        self.ensure_dir(parent_name)
        target = self.path(name)
        if os.path.lexists(target):
            raise ContractError("package_collision", "local reproduction output file already exists")
        temp = target.with_name(f".{target.name}.{os.urandom(8).hex()}.tmp")
        try:
            with open(temp, "xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if os.path.lexists(target):
                raise ContractError("package_collision", "local reproduction output path changed during write")
            if not publish_no_clobber(temp, target):
                raise ContractError("package_collision", "local reproduction output path already exists")
        except OSError as exc:
            raise ContractError("path_denied", "local reproduction output could not be written") from exc
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        return self._remember(ref_from_bytes("/".join(parts), raw, media_type))

    def write_stream(self, name: str, chunks: Iterable[bytes], media_type: str) -> dict[str, Any]:
        if not self.local:
            assert self.workspace is not None and self.output_path is not None
            return self._remember(_write_stream_output(self.workspace, self.output_path, name, chunks, media_type))
        parts = _safe_relative(name)
        self.ensure_dir("/".join(parts[:-1]))
        target = self.path(name)
        if os.path.lexists(target):
            raise ContractError("package_collision", "local reproduction output file already exists")
        temp = target.with_name(f".{target.name}.{os.urandom(8).hex()}.tmp")
        digest = hashlib.sha256()
        byte_len = 0
        try:
            with open(temp, "xb") as stream:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise ContractError("output_invalid", "streamed worker output must be bytes")
                    stream.write(chunk)
                    digest.update(chunk)
                    byte_len += len(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if os.path.lexists(target):
                raise ContractError("package_collision", "local reproduction output path changed during write")
            if not publish_no_clobber(temp, target):
                raise ContractError("package_collision", "local reproduction output path already exists")
        except OSError as exc:
            raise ContractError("path_denied", "local reproduction output could not be streamed") from exc
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        return self._remember({
            "path": "/".join(parts),
            "sha256": digest.hexdigest(),
            "byte_len": byte_len,
            "media_type": media_type,
        })

    def write_json(self, name: str, value: Mapping[str, Any]) -> dict[str, Any]:
        return self.write(name, canonical_json_bytes(dict(value)), "application/json")

    def hash_file(self, name: str) -> tuple[int, str]:
        parts = self._relative(name)
        if self.local:
            assert self.local_root is not None
            return _hash_regular_file(self.local_root, parts)
        assert self.workspace is not None
        return _hash_regular_file(self.workspace, parts)

    def register_file(self, name: str, media_type: str) -> dict[str, Any]:
        size, digest = self.hash_file(name)
        path = "/".join(self._relative(name))
        return self._remember({"path": path, "sha256": digest, "byte_len": size, "media_type": media_type})


def _json_output_to(output: _OutputTarget, name: str, value: Mapping[str, Any]) -> dict[str, Any]:
    return output.write_json(name, value)


def _jsonl_chunks(rows: Iterable[Mapping[str, Any]]) -> Iterable[bytes]:
    for row in rows:
        yield canonical_json_bytes(dict(row)) + b"\n"


def _rust_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _runtime_identity(manifest: Mapping[str, Any]) -> bytes:
    files = [
        {"path": item["path"], "sha256": item["sha256"], "byte_len": item["byte_len"]}
        for item in manifest["files"]
    ]
    return _rust_json({"schema_version": 1, "files": files})


def _runtime_files_for_prepared_schema(schema_version: Any) -> tuple[str, ...]:
    if schema_version == 2 and type(schema_version) is int:
        return _LEGACY_RUNTIME_FILES
    if schema_version == 3 and type(schema_version) is int:
        return _RUNTIME_FILES
    raise ContractError("input_closure_unknown", "prepared schema has no fixed runtime closure")


def _verify_runtime_bundle_reader(
    reader: DataReader,
    digest: str,
    manifest_ref: Mapping[str, Any] | None = None,
    *,
    prepared_schema_version: int | None = None,
) -> tuple[dict[str, Any], tuple[Mapping[str, Any], ...]]:
    """Verify the frozen runtime closure through the caller's exact Ref reader.

    The isolated standalone bootstrap verifies the fixed-tree COMPLETE marker
    before importing this module. This function verifies the immutable
    manifest Ref and every helper Ref through the same PortableReader used by
    the subsequent model/data computation.
    """
    require_hash(digest, pointer="/runtime_bundle/digest")
    if manifest_ref is None:
        raise ContractError("output_invalid", "runtime manifest Ref is required")
    validated_manifest_ref = validate_ref_shape(dict(manifest_ref), pointer="/runtime_bundle/manifest")
    root_relative = f".reaslab/optagent/runtime/{digest}"
    manifest_path = f"{root_relative}/manifest.json"
    if validated_manifest_ref["path"] != manifest_path:
        raise ContractError("input_closure_unknown", "runtime manifest Ref is not at its fixed digest path")
    manifest_raw = _read_verified_ref(reader, validated_manifest_ref, "/runtime_bundle/manifest")
    try:
        manifest = strict_json_loads(manifest_raw)
    except (ValueError, UnicodeError) as exc:
        raise ContractError("output_invalid", "worker runtime manifest is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "digest", "files"}
        or manifest["schema_version"] != 1
        or manifest["digest"] != digest
        or not isinstance(manifest["files"], list)
    ):
        raise ContractError("output_invalid", "worker runtime manifest has invalid fields")

    paths: list[str] = []
    entries: list[Mapping[str, Any]] = []
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "byte_len"}:
            raise ContractError("output_invalid", "worker runtime file entry has invalid fields")
        relative = item["path"]
        _safe_relative(relative)
        require_hash(item["sha256"], pointer="/runtime_bundle/files/sha256")
        if type(item["byte_len"]) is not int or item["byte_len"] < 0:
            raise ContractError("output_invalid", "worker runtime byte length is invalid")
        paths.append(relative)
        entries.append(item)
    expected_files = _RUNTIME_FILES if prepared_schema_version is None else _runtime_files_for_prepared_schema(prepared_schema_version)
    if paths != sorted(paths) or len(paths) != len(set(paths)) or tuple(paths) != expected_files:
        raise ContractError("input_closure_unknown", "worker runtime allowlist is not the fixed native closure")
    runtime_refs: list[Mapping[str, Any]] = []
    for item in entries:
        relative = item["path"]
        helper_ref = {
            "path": f"{root_relative}/{relative}",
            "sha256": item["sha256"],
            "byte_len": item["byte_len"],
            "media_type": "text/x-python; charset=utf-8",
        }
        _read_verified_ref(reader, helper_ref, "/runtime_bundle/helper")
        runtime_refs.append(helper_ref)
    if sha256_bytes(_runtime_identity(manifest)) != digest:
        raise ContractError("artifact_changed", "worker runtime identity digest does not match")
    return manifest, tuple(runtime_refs)


def verify_runtime_bundle(
    workspace_absolute: str | os.PathLike[str] | DataReader,
    digest: str,
    manifest_ref: Mapping[str, Any] | None = None,
    *,
    prepared_schema_version: int | None = None,
) -> tuple[dict[str, Any], tuple[Mapping[str, Any], ...]]:
    """Verify the fixed runtime closure from a workspace or an exact Ref reader.

    Platform launch uses a trusted workspace and checks COMPLETE directly.
    Portable reproduction uses its pre-import stdlib bootstrap for COMPLETE,
    then calls this reader path so all Ref-addressed bytes use one reader.
    """
    if callable(getattr(workspace_absolute, "read_ref", None)):
        return _verify_runtime_bundle_reader(
            workspace_absolute,
            digest,
            manifest_ref,
            prepared_schema_version=prepared_schema_version,
        )  # type: ignore[arg-type]
    require_hash(digest, pointer="/runtime_bundle/digest")
    workspace = windows_long_path(workspace_absolute)
    if not workspace.is_absolute():
        raise ContractError("path_denied", "trusted worker workspace must be absolute")
    root_relative = f".reaslab/optagent/runtime/{digest}"
    root = _path(workspace, root_relative)
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise ContractError("environment_unavailable", "worker runtime bundle is unavailable") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ContractError("path_denied", "worker runtime bundle path is unsafe")

    marker = _read_regular(workspace, _safe_relative(f"{root_relative}/COMPLETE"))
    if marker != digest.encode("ascii"):
        raise ContractError("artifact_changed", "worker runtime completion marker does not match")
    manifest_path = f"{root_relative}/manifest.json"
    manifest_raw = _read_regular(workspace, _safe_relative(manifest_path))
    if manifest_ref is not None:
        ref = validate_ref_shape(dict(manifest_ref), pointer="/runtime_bundle/manifest")
        if ref["path"] != manifest_path or len(manifest_raw) != ref["byte_len"] or sha256_bytes(manifest_raw) != ref["sha256"]:
            raise ContractError("artifact_changed", "worker runtime manifest reference does not match")
    try:
        manifest = strict_json_loads(manifest_raw)
    except (ValueError, UnicodeError) as exc:
        raise ContractError("output_invalid", "worker runtime manifest is invalid") from exc
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "digest", "files"} or manifest["schema_version"] != 1 or manifest["digest"] != digest or not isinstance(manifest["files"], list):
        raise ContractError("output_invalid", "worker runtime manifest has invalid fields")
    paths: list[str] = []
    runtime_refs: list[Mapping[str, Any]] = []
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "byte_len"}:
            raise ContractError("output_invalid", "worker runtime file entry has invalid fields")
        relative = item["path"]
        _safe_relative(relative)
        require_hash(item["sha256"], pointer="/runtime_bundle/files/sha256")
        if type(item["byte_len"]) is not int or item["byte_len"] < 0:
            raise ContractError("output_invalid", "worker runtime byte length is invalid")
        paths.append(relative)
        raw = _read_regular(workspace, _safe_relative(f"{root_relative}/{relative}"))
        if len(raw) != item["byte_len"] or sha256_bytes(raw) != item["sha256"]:
            raise ContractError("artifact_changed", "worker runtime helper bytes changed", details={"path": relative})
        runtime_refs.append(ref_from_bytes(f"{root_relative}/{relative}", raw, "text/x-python; charset=utf-8"))
    expected_files = _RUNTIME_FILES if prepared_schema_version is None else _runtime_files_for_prepared_schema(prepared_schema_version)
    if paths != sorted(paths) or len(paths) != len(set(paths)) or tuple(paths) != expected_files:
        raise ContractError("input_closure_unknown", "worker runtime allowlist is not the fixed native closure")
    if sha256_bytes(_runtime_identity(manifest)) != digest:
        raise ContractError("artifact_changed", "worker runtime identity digest does not match")
    return manifest, tuple(runtime_refs)


def _read_verified_ref(reader: DataReader, ref: Mapping[str, Any], pointer: str = "/ref") -> bytes:
    validated = validate_ref_shape(dict(ref), pointer=pointer)
    raw = reader.read_ref(validated)
    if len(raw) != validated["byte_len"] or sha256_bytes(raw) != validated["sha256"]:
        raise ContractError("artifact_changed", "reader bytes do not match their Ref", pointer=pointer)
    return raw


def _parse_json_ref(reader: DataReader, ref: Mapping[str, Any], label: str) -> dict[str, Any]:
    raw = _read_verified_ref(reader, ref, "/" + label.replace(" ", "_"))
    try:
        value = strict_json_loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ContractError("output_invalid", f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise ContractError("output_invalid", f"{label} must be a JSON object")
    return value


def _load_prepared_manifest(
    reader: DataReader,
    manifest_ref: Mapping[str, Any],
    *,
    expected_case_id: str | None = None,
    expected_prepared_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Read and validate the original prepared manifest through one reader."""
    ref = validate_ref_shape(dict(manifest_ref), pointer="/prepared_manifest_ref")
    raw = _read_verified_ref(reader, ref, "/prepared_manifest_ref")
    input_digest = sha256_bytes(raw)
    if input_digest != ref["sha256"]:
        raise ContractError("artifact_changed", "prepared manifest Ref does not bind its canonical bytes")
    try:
        manifest = strict_json_loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ContractError("output_invalid", "prepared input manifest is invalid") from exc
    manifest_version = manifest.get("schema_version") if isinstance(manifest, dict) else None
    if not isinstance(manifest, dict) or set(manifest) != _PREPARED_MANIFEST_FIELDS or manifest_version not in {2, 3}:
        raise ContractError("output_invalid", "prepared input manifest has invalid fields")
    case_id = require_id(manifest["case_id"], pointer="/case_id")
    prepared_id = require_id(manifest["prepared_id"], pointer="/prepared_id")
    expected_path = f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/input/manifest.json"
    if ref["path"] != expected_path:
        raise ContractError("input_closure_unknown", "prepared manifest Ref is not its fixed frozen input path")
    if expected_case_id is not None and case_id != expected_case_id:
        raise ContractError("artifact_changed", "prepared manifest case identity differs from invocation")
    if expected_prepared_id is not None and prepared_id != expected_prepared_id:
        raise ContractError("artifact_changed", "prepared manifest identity differs from invocation")
    for field in ("source_revision", "candidate_revision", "promotion_generation"):
        if type(manifest[field]) is not int or manifest[field] < 0:
            raise ContractError("output_invalid", "prepared manifest revision is invalid", pointer=f"/{field}")
    require_id(manifest["candidate_id"], pointer="/candidate_id")
    require_hash(manifest["binding_digest"], pointer="/binding_digest")
    if not isinstance(manifest["sources"], list):
        raise ContractError("output_invalid", "prepared sources must be an array")

    candidate = manifest["candidate"]
    base_candidate_fields = {
        "authoring",
        "model",
        "local_modules",
        "business_checker",
        "backend_id",
        "objective_plan",
        "input_closure",
        "closure_note",
    }
    candidate_fields = set(candidate) if isinstance(candidate, dict) else set()
    if candidate_fields not in (base_candidate_fields, base_candidate_fields | {"requirements_file"}):
        raise ContractError("output_invalid", "prepared candidate closure has invalid fields")
    if candidate["authoring"] not in {"pyomo", "direct_python"}:
        raise ContractError("capability_uncovered", "prepared candidate entry family is unsupported")
    if not isinstance(candidate.get("objective_plan"), dict):
        raise ContractError("output_invalid", "prepared objective plan is invalid")
    if candidate["authoring"] == "direct_python" and "requirements_file" not in candidate:
        raise ContractError("output_invalid", "direct_python Prepared input is missing its frozen requirements field")
    requirements_file = candidate.get("requirements_file")
    if requirements_file is not None:
        if not isinstance(requirements_file, dict) or set(requirements_file) != {"path", "bytes"} or requirements_file["path"] != "requirements.txt":
            raise ContractError("output_invalid", "frozen root requirements file identity is invalid", pointer="/candidate/requirements_file")
        requirements_ref = validate_ref_shape(requirements_file["bytes"], pointer="/candidate/requirements_file/bytes")
        expected_requirements_path = f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/input/requirements.txt"
        if requirements_ref["path"] != expected_requirements_path:
            raise ContractError("input_closure_unknown", "requirements Ref is outside the frozen root requirements path", pointer="/candidate/requirements_file/bytes/path")
    if manifest_version == 2:
        if candidate["authoring"] != "pyomo" or candidate["backend_id"] != "highs":
            raise ContractError("capability_uncovered", "legacy sealed runs are supported only with their original Highs backend")
    else:
        from .solver_protocol import (
            DIRECT_PYTHON_BACKEND_ID,
            SUPPORTED_BACKEND_IDS,
            validate_direct_python_options,
            validate_solver_options,
        )
        if candidate["authoring"] == "direct_python":
            if candidate["backend_id"] != DIRECT_PYTHON_BACKEND_ID:
                raise ContractError("artifact_changed", "direct candidate entry family differs from backend id")
            if candidate["objective_plan"].get("mode") == "lexicographic":
                raise ContractError("capability_uncovered", "direct_python supports scalar and feasibility objective plans only")
            validate_direct_python_options(manifest["solve_options"])
        else:
            if candidate["backend_id"] not in SUPPORTED_BACKEND_IDS:
                raise ContractError("capability_uncovered", "prepared run names an unsupported solver backend")
            validate_solver_options(candidate["backend_id"], manifest["solve_options"])
    if candidate["input_closure"] != "declared" or not isinstance(candidate["local_modules"], list):
        raise ContractError("input_closure_unknown", "prepared candidate module closure is not declared")
    data_info = manifest["data"]
    if data_info is not None:
        if not isinstance(data_info, dict) or set(data_info) != {"data_id", "manifest", "instance_parameter_object_id"}:
            raise ContractError("output_invalid", "prepared data identity has invalid fields")
        if not isinstance(data_info["data_id"], str) or not data_info["data_id"]:
            raise ContractError("output_invalid", "prepared data identity is invalid")
        if data_info["instance_parameter_object_id"] is not None and not isinstance(data_info["instance_parameter_object_id"], str):
            raise ContractError("output_invalid", "prepared instance parameter identity is invalid")

    runtime = manifest["runtime_bundle"]
    if not isinstance(runtime, dict) or set(runtime) != {"digest", "manifest", "worker_entry"}:
        raise ContractError("input_closure_unknown", "prepared runtime bundle reference is invalid")
    require_hash(runtime["digest"], pointer="/runtime_bundle/digest")
    runtime_ref = validate_ref_shape(runtime["manifest"], pointer="/runtime_bundle/manifest")
    if runtime_ref["path"] != f".reaslab/optagent/runtime/{runtime['digest']}/manifest.json" or runtime["worker_entry"] != "python/optagent_core/worker.py":
        raise ContractError("input_closure_unknown", "prepared manifest worker entry or runtime path is not fixed")
    if not isinstance(manifest["solve_options"], dict):
        raise ContractError("output_invalid", "prepared solve options are invalid")
    return manifest, ref, input_digest


def _load_invocation(
    workspace: Path,
    invocation_path: str,
    expected_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    require_hash(expected_sha256, pointer="/expected_sha256")
    raw = _read_regular(workspace, _safe_relative(invocation_path))
    if sha256_bytes(raw) != expected_sha256:
        raise ContractError("artifact_changed", "worker invocation bytes do not match the planned ref")
    try:
        invocation = strict_json_loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ContractError("output_invalid", "worker invocation is not strict JSON") from exc
    if not isinstance(invocation, dict) or set(invocation) != _INVOCATION_FIELDS or invocation.get("schema_version") != 2:
        raise ContractError("output_invalid", "worker invocation has invalid fields")
    case_id = require_id(invocation["case_id"], pointer="/case_id")
    prepared_id = require_id(invocation["prepared_id"], pointer="/prepared_id")
    run_id = require_id(invocation["run_id"], pointer="/run_id")
    require_id(invocation["dispatch_nonce"], pointer="/dispatch_nonce")
    if invocation_path != f".reaslab/optagent/cases/{case_id}/runs/{run_id}/invocation.json":
        raise ContractError("path_denied", "worker invocation path does not match its run identity")
    stamp = invocation["stamp"]
    if not isinstance(stamp, dict) or set(stamp) != {"session_id", "role", "turn_id", "call_id"} or stamp.get("role") != "root":
        raise ContractError("output_invalid", "worker invocation stamp is invalid")
    for field in ("session_id", "turn_id", "call_id"):
        if not isinstance(stamp[field], str) or not stamp[field]:
            raise ContractError("output_invalid", "worker invocation stamp is invalid")
    input_digest = require_hash(invocation["input_digest"], pointer="/input_digest")
    manifest_ref = validate_ref_shape(invocation["input_manifest"], pointer="/input_manifest")
    expected_manifest_path = f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/input/manifest.json"
    if manifest_ref["path"] != expected_manifest_path or manifest_ref["sha256"] != input_digest:
        raise ContractError("artifact_changed", "worker invocation does not bind the fixed prepared manifest")
    expected_output = f".reaslab/optagent/cases/{case_id}/runs/{run_id}/output"
    if invocation["output_path"] != expected_output:
        raise ContractError("path_denied", "worker invocation output path is not its own run output")
    reader = FrozenReader(workspace, case_id, prepared_id)
    prepared_manifest, _, manifest_digest = _load_prepared_manifest(
        reader,
        manifest_ref,
        expected_case_id=case_id,
        expected_prepared_id=prepared_id,
    )
    if manifest_digest != input_digest:
        raise ContractError("artifact_changed", "prepared input manifest bytes do not match")
    prepared_record_path = f".reaslab/optagent/cases/{case_id}/prepared/{prepared_id}/prepared.json"
    try:
        prepared_record = strict_json_loads(_read_regular(workspace, _safe_relative(prepared_record_path)))
    except (ValueError, UnicodeError) as exc:
        raise ContractError("output_invalid", "prepared record is invalid") from exc
    if not isinstance(prepared_record, dict) or prepared_record.get("prepared_id") != prepared_id or prepared_record.get("case_id") != case_id or prepared_record.get("status") != "frozen" or prepared_record.get("input_digest") != input_digest:
        raise ContractError("artifact_changed", "prepared record does not bind this invocation")
    prepared_record_manifest = validate_ref_shape(prepared_record.get("input_manifest"), pointer="/prepared/input_manifest")
    if prepared_record_manifest != manifest_ref:
        raise ContractError("artifact_changed", "prepared record manifest reference differs from invocation")
    case_state_path = f".reaslab/optagent/cases/{case_id}/state.json"
    try:
        case_state_raw = _read_regular(workspace, _safe_relative(case_state_path))
        case_state = strict_json_loads(case_state_raw)
    except (ValueError, UnicodeError) as exc:
        raise ContractError("output_invalid", "Case state for worker authorization is invalid") from exc
    if not isinstance(case_state, dict) or case_state.get("case_id") != case_id or run_id not in case_state.get("run_ids", []):
        raise ContractError("artifact_changed", "Case does not register this worker run")
    records = case_state.get("current_records")
    run_ref = records.get("runs", {}).get(run_id) if isinstance(records, dict) and isinstance(records.get("runs"), dict) else None
    if run_ref is None:
        raise ContractError("legacy_read_only", "worker run has no Case-published immutable Ref")
    run_ref = validate_ref_shape(run_ref, pointer=f"/current_records/runs/{run_id}")
    run_record_path = f".reaslab/optagent/cases/{case_id}/runs/{run_id}/versions/{run_ref['sha256']}.json"
    if run_ref["path"] != run_record_path or run_ref["media_type"] != "application/json":
        raise ContractError("artifact_changed", "Case run Ref is not its immutable version path")
    try:
        run_raw = _read_regular(workspace, _safe_relative(run_ref["path"]))
        run_record = strict_json_loads(run_raw)
    except (ValueError, UnicodeError) as exc:
        raise ContractError("output_invalid", "current run snapshot is invalid") from exc
    if len(run_raw) != run_ref["byte_len"] or sha256_bytes(run_raw) != run_ref["sha256"]:
        raise ContractError("artifact_changed", "current run snapshot bytes differ from the Case Ref")
    if canonical_json_bytes(case_state) != case_state_raw:
        raise ContractError("artifact_changed", "Case authorization state is not canonical JSON")
    if canonical_json_bytes(run_record) != run_raw:
        raise ContractError("artifact_changed", "current run snapshot is not canonical JSON")
    invocation_ref = ref_from_bytes(invocation_path, raw, "application/json")
    if not isinstance(run_record, dict) or run_record.get("run_id") != run_id or run_record.get("prepared_id") != prepared_id or run_record.get("dispatch_nonce") != invocation["dispatch_nonce"] or run_record.get("dispatch_state") != "unknown":
        raise ContractError("artifact_changed", "run record does not bind the consumed invocation")
    if validate_ref_shape(run_record.get("worker_invocation"), pointer="/run/worker_invocation") != invocation_ref:
        raise ContractError("artifact_changed", "run record invocation ref does not match consumed bytes")
    if run_record.get("created_by") != invocation["stamp"]:
        raise ContractError("artifact_changed", "run record stamp does not match invocation")
    return invocation, prepared_manifest, invocation_ref


def _objective_list(plan: Mapping[str, Any]) -> list[int | None]:
    if plan.get("mode") == "lexicographic":
        return list(range(len(plan["layers"])))
    return [None]


def _raw_primal_rows(primals: Mapping[Any, Any]) -> Iterable[Mapping[str, Any]]:
    for variable, raw_value in primals.items():
        parent = variable.parent_component().name
        index = variable.index()
        key = list(index) if isinstance(index, tuple) else [] if index is None else [index]
        if any(type(item) not in (str, int) for item in key):
            yield {"component": parent, "key": [], "value": None, "error": "unsupported_variable_key"}
        elif isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)) or not math.isfinite(float(raw_value)):
            yield {"component": parent, "key": key, "value": None, "raw_repr": repr(raw_value)}
        else:
            yield {"component": parent, "key": key, "value": float(raw_value)}


def _raw_native_cp_sat_rows(values: Sequence[int] | None) -> Iterable[Mapping[str, Any]]:
    if values is None:
        return
    for index, value in enumerate(values):
        yield {"index": index, "value": value}


def _native_cp_model_textproto_bytes(model: Any) -> bytes:
    """Serialize the actual CpModelProto as readable text for editable export."""
    if not is_native_cp_model_build(model):
        raise ContractError("invalid_argument", "native CP-SAT text export requires a Core-validated model envelope")
    try:
        from google.protobuf import text_format

        return text_format.MessageToString(model.model.proto).encode("utf-8")
    except Exception as exc:
        raise ContractError("capability_uncovered", "OR-Tools could not render the native CpModel proto as text") from exc


def _requires_solver(model: Any) -> bool:
    if is_native_cp_model_build(model):
        return True
    from pyomo.common.collections import ComponentSet
    from pyomo.environ import Constraint, Objective, Var
    from pyomo.core.expr.visitor import identify_variables

    eligible = ComponentSet()
    eligible.update(
        variable
        for variable in model.component_data_objects(Var, active=True, descend_into=True)
        if not variable.fixed
    )
    if not eligible:
        return False

    for constraint in model.component_data_objects(Constraint, active=True, descend_into=True):
        if any(variable in eligible for variable in identify_variables(constraint.body, include_fixed=True)):
            return True
    for objective in model.component_data_objects(Objective, active=True, descend_into=True):
        if any(variable in eligible for variable in identify_variables(objective.expr, include_fixed=True)):
            return True
    return False


def _extract_business_solution(
    reader: DataReader,
    model_spec: Any,
    module_specs: Sequence[Any],
    bundle: DataBundle,
    witness: Mapping[str, Any],
) -> tuple[Any, tuple[Mapping[str, Any], ...]]:
    try:
        result, consumed = call_frozen_function(
            reader,
            model_spec,
            list(module_specs),
            function_name="extract_solution",
            args=(bundle, witness),
            pointer="/candidate/model",
        )
    except ContractError:
        raise RuntimeError("business solution extraction failed") from None
    try:
        return normalize_json_value(result), consumed
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ContractError("output_invalid", "model solution is not finite JSON") from exc


def _business_check_solution(
    reader: DataReader,
    checker_spec: Any,
    module_specs: Sequence[Any],
    bundle: DataBundle,
    solution: Any,
    *,
    input_digest: str,
    instance_key: Sequence[str | int],
    instance_binding_digest: str,
    expected_objective_values: Sequence[Mapping[str, Any]] | None,
    exact_objective_values: bool = False,
) -> tuple[dict[str, Any], tuple[Mapping[str, Any], ...], dict[str, Any] | None, Any]:
    before = canonical_json_bytes(solution)

    def failure(reason: str, *, modified: bool = False) -> dict[str, Any]:
        record = business_check_uncovered(
            input_digest=input_digest,
            instance_key=instance_key,
            instance_binding_digest=instance_binding_digest,
            reason=reason,
        )
        record["solution"] = solution
        if modified:
            record["status"] = "fail"
            record["violations"] = ["business_checker_modified_solution"]
            record["uncovered"] = []
        return record

    try:
        result, consumed = call_frozen_function(
            reader,
            checker_spec,
            list(module_specs),
            function_name="check_solution",
            args=(bundle, solution),
            pointer="/candidate/business_checker",
        )
    except Exception as exc:
        try:
            after = canonical_json_bytes(solution)
        except (TypeError, ValueError, UnicodeError):
            after = None
        modified = after != before
        if modified:
            solution = strict_json_loads(before)
        diagnostic = None if isinstance(exc, ContractError) else {
            "code": "business_checker_error",
            "error_type": type(exc).__name__,
        }
        return failure(
            "business_checker_modified_solution" if modified else "business_checker_error",
            modified=modified,
        ), (), diagnostic, solution

    try:
        after = canonical_json_bytes(solution)
    except (TypeError, ValueError, UnicodeError):
        after = None
    if after != before:
        solution = strict_json_loads(before)
        return failure("business_checker_modified_solution", modified=True), (), {
            "code": "business_checker_modified_solution",
        }, solution

    required = {"status", "checked_obligations", "objective_values", "violations", "uncovered"}
    try:
        result = normalize_json_value(result)
        if not isinstance(result, dict) or set(result) != required:
            unexpected_fields = (
                [field for field in result if isinstance(field, str) and field not in required]
                if isinstance(result, dict)
                else []
            )
            pointer = "/candidate/business_checker"
            if unexpected_fields:
                field = min(unexpected_fields)
                escaped_field = field.replace("~", "~0").replace("/", "~1")
                pointer = f"{pointer}/{escaped_field}"
            raise ContractError(
                "output_invalid",
                "business checker returned an invalid BusinessCheck",
                pointer=pointer,
            )
    except (TypeError, ValueError, UnicodeError, ContractError) as exc:
        diagnostic = (
            {"code": exc.code, "message": exc.message, "pointer": exc.pointer}
            if isinstance(exc, ContractError)
            else {"code": "output_invalid", "error_type": type(exc).__name__}
        )
        record = failure("business_checker_error")
        return record, (), diagnostic, solution
    if not isinstance(result["status"], str) or result["status"] not in {"pass", "fail", "uncovered"} or not isinstance(result["checked_obligations"], list) or not isinstance(result["objective_values"], list) or not isinstance(result["violations"], list) or not isinstance(result["uncovered"], list):
        return failure("business_checker_error"), (), {
            "code": "output_invalid",
            "message": "business checker returned invalid status or arrays",
            "pointer": "/candidate/business_checker",
        }, solution
    try:
        objective_values_match(result["objective_values"], result["objective_values"])
    except ContractError as exc:
        return failure("business_checker_error"), (), {
            "code": exc.code,
            "message": exc.message,
            "pointer": "/candidate/business_checker/objective_values",
        }, solution
    for obligation in result["checked_obligations"]:
        if not isinstance(obligation, dict) or set(obligation) != {"label", "source_locations", "evidence"} or not isinstance(obligation["label"], str) or not obligation["label"] or not isinstance(obligation["evidence"], str) or not isinstance(obligation["source_locations"], list):
            return failure("business_checker_error"), (), {
                "code": "output_invalid",
                "message": "business checker obligation evidence is invalid",
                "pointer": "/candidate/business_checker/checked_obligations",
            }, solution
        for location in obligation["source_locations"]:
            if not isinstance(location, dict) or set(location) != {"source_id", "locator"} or not isinstance(location["source_id"], str) or not isinstance(location["locator"], str):
                return failure("business_checker_error"), (), {
                    "code": "output_invalid",
                    "message": "business checker source location is invalid",
                    "pointer": "/candidate/business_checker/checked_obligations/source_locations",
                }, solution
    if not all(isinstance(value, str) for value in [*result["violations"], *result["uncovered"]]):
        return failure("business_checker_error"), (), {
            "code": "output_invalid",
            "message": "business checker violation fields must be strings",
            "pointer": "/candidate/business_checker",
        }, solution
    if result["status"] == "pass" and expected_objective_values is not None:
        try:
            objective_agrees = (
                objective_values_exactly_match(result["objective_values"], expected_objective_values)
                if exact_objective_values
                else objective_values_match(result["objective_values"], expected_objective_values)
            )
        except ContractError:
            objective_agrees = False
        if not objective_agrees:
            result = dict(result)
            result["status"] = "fail"
            result["violations"] = [*result["violations"], "business_checker_objective_mismatch"]
    result = {
        "input_digest": input_digest,
        "instance_key": list(instance_key),
        "instance_binding_digest": instance_binding_digest,
        **result,
        "solution": solution,
    }
    return result, consumed, None, solution


def _write_violation_stream(output: _OutputTarget, name: str, model: Any, primals: Mapping[Any, Any], objective: ObjectiveSpec, identity: Mapping[str, Any], options: Mapping[str, Any]) -> tuple[CheckedModel, dict[str, Any] | None]:
    name_parts = _safe_relative(name)
    output.ensure_dir("/".join(name_parts[:-1]))
    target = output.path(name)
    try:
        with open(target, "xb") as stream:
            def sink(row: Mapping[str, Any]) -> None:
                stream.write(canonical_json_bytes(dict(row)) + b"\n")

            checked = check_pyomo_model(
                model,
                primals,
                objective,
                input_digest=str(identity["input_digest"]),
                instance_key=identity["instance_key"],
                instance_binding_digest=str(identity["instance_binding_digest"]),
                feasibility_tolerance=float(options["feasibility_tolerance"]),
                integrality_tolerance=float(options["integrality_tolerance"]),
                violation_sink=sink,
            )
            stream.flush()
            os.fsync(stream.fileno())
        size, digest = output.hash_file(name)
        if size == 0:
            target.unlink()
            return checked, None
        ref = {"path": "/".join(output._relative(name)), "sha256": digest, "byte_len": size, "media_type": "application/x-ndjson"}
        output._remember(ref)
        return checked, ref
    except FileExistsError as exc:
        raise ContractError("package_collision", "worker violation output already exists") from exc


def _write_native_violation_stream(
    output: _OutputTarget,
    name: str,
    model: Any,
    observation: Any,
    objective: ObjectiveSpec,
    identity: Mapping[str, Any],
    options: Mapping[str, Any],
    native_evidence: Mapping[str, Any],
) -> tuple[CheckedModel, dict[str, Any] | None]:
    name_parts = _safe_relative(name)
    output.ensure_dir("/".join(name_parts[:-1]))
    target = output.path(name)
    try:
        with open(target, "xb") as stream:
            def sink(row: Mapping[str, Any]) -> None:
                stream.write(canonical_json_bytes(dict(row)) + b"\n")

            checked = check_native_cp_sat_model(
                model,
                observation,
                objective,
                input_digest=str(identity["input_digest"]),
                instance_key=identity["instance_key"],
                instance_binding_digest=str(identity["instance_binding_digest"]),
                feasibility_tolerance=float(options["feasibility_tolerance"]),
                integrality_tolerance=float(options["integrality_tolerance"]),
                native_evidence=native_evidence,
                violation_sink=sink,
            )
            stream.flush()
            os.fsync(stream.fileno())
        size, digest = output.hash_file(name)
        if size == 0:
            target.unlink()
            return checked, None
        ref = {"path": "/".join(output._relative(name)), "sha256": digest, "byte_len": size, "media_type": "application/x-ndjson"}
        output._remember(ref)
        return checked, ref
    except FileExistsError as exc:
        raise ContractError("package_collision", "worker native violation output already exists") from exc


def _write_layer(
    *,
    reader: DataReader,
    output: _OutputTarget,
    stem: str,
    model: Any,
    model_spec: Any,
    instance_bundle: DataBundle,
    prepared: Mapping[str, Any],
    objective: ObjectiveSpec,
    layer: int | None,
    observation: Any | None,
    primals: Mapping[Any, Any],
    deterministic_termination: str | None,
    solver_invoked: bool,
    warnings: Sequence[str],
    checker_spec: Any,
    module_specs: Sequence[Any],
    runtime_refs: Sequence[Mapping[str, Any]],
    backend_profile: Mapping[str, Any],
    backend_observation_ref: Mapping[str, Any] | None,
    raw_primals_ref: Mapping[str, Any],
    model_consumed: Sequence[Mapping[str, Any]],
    native_evidence: Mapping[str, Any] | None = None,
    lex_context: Mapping[str, Any] | None = None,
    ancillary_refs: Sequence[Mapping[str, Any]] = (),
) -> tuple[dict[str, Any], dict[str, Any] | None, bool, dict[str, Any]]:
    identity = {
        "input_digest": prepared["input_digest"],
        "instance_key": list(instance_bundle.instance_context.instance_key if instance_bundle.instance_context else ()),
        "instance_binding_digest": instance_bundle.instance_context.instance_binding_digest if instance_bundle.instance_context else instance_bundle.candidate_binding_digest,
    }
    options = prepared["solve_options"]
    if is_native_cp_model_build(model):
        if observation is None or native_evidence is None:
            raise ContractError("artifact_changed", "native CpModel checking is missing its same-run solver evidence")
        checked, violations_ref = _write_native_violation_stream(
            output,
            f"{stem}/violations.jsonl",
            model,
            observation,
            objective,
            identity,
            options,
            native_evidence,
        )
    else:
        checked, violations_ref = _write_violation_stream(
            output,
            f"{stem}/violations.jsonl",
            model,
            primals,
            objective,
            identity,
            options,
        )
    check_record = dict(checked.record)
    check_record["violations"] = violations_ref
    check_ref = _json_output_to(output, f"{stem}/model-check.json", check_record)
    if observation is None and checked.record["status"] == "fail" and deterministic_termination in {"optimal", "feasible"}:
        deterministic_termination = "infeasible"
    witness_record: dict[str, Any] | None = None
    witness_ref: dict[str, Any] | None = None
    if checked.witness_variables is not None:
        witness_record = {
            "input_digest": identity["input_digest"],
            "instance_key": identity["instance_key"],
            "instance_binding_digest": identity["instance_binding_digest"],
            "variables": [dict(item) for item in checked.witness_variables],
            "objective_values": [dict(item) for item in checked.objective_values],
        }
        witness_ref = _json_output_to(output, f"{stem}/witness.json", witness_record)
    checker_consumed: tuple[Mapping[str, Any], ...] = ()
    extractor_consumed: tuple[Mapping[str, Any], ...] = ()
    business_diagnostic: dict[str, Any] | None = None
    checked_witness = witness_record if checked.candidate_complete else None
    if checker_spec is None:
        business = business_check_uncovered(
            input_digest=str(identity["input_digest"]),
            instance_key=identity["instance_key"],
            instance_binding_digest=str(identity["instance_binding_digest"]),
            reason="business_checker_not_declared",
        )
    elif checked_witness is None:
        business = business_check_uncovered(
            input_digest=str(identity["input_digest"]),
            instance_key=identity["instance_key"],
            instance_binding_digest=str(identity["instance_binding_digest"]),
            reason="no_checked_witness",
        )
    else:
        try:
            business_solution, extractor_consumed = _extract_business_solution(
                reader,
                model_spec,
                module_specs,
                instance_bundle,
                checked_witness,
            )
            business, checker_consumed, business_diagnostic, business_solution = _business_check_solution(
                reader,
                checker_spec,
                module_specs,
                instance_bundle,
                business_solution,
                input_digest=str(identity["input_digest"]),
                instance_key=identity["instance_key"],
                instance_binding_digest=str(identity["instance_binding_digest"]),
                expected_objective_values=checked.objective_values,
                exact_objective_values=is_native_cp_model_build(model),
            )
            business["solution"] = business_solution
        except ContractError as exc:
            business = business_check_uncovered(
                input_digest=str(identity["input_digest"]),
                instance_key=identity["instance_key"],
                instance_binding_digest=str(identity["instance_binding_digest"]),
                reason="business_solution_extraction_error",
            )
            business_diagnostic = {
                "code": exc.code,
                "message": exc.message,
                "pointer": exc.pointer,
            }
        except Exception as exc:
            business = business_check_uncovered(
                input_digest=str(identity["input_digest"]),
                instance_key=identity["instance_key"],
                instance_binding_digest=str(identity["instance_binding_digest"]),
                reason="business_solution_extraction_error",
            )
            business_diagnostic = {"code": "business_solution_extraction_error", "error_type": type(exc).__name__}
    if business_diagnostic is not None:
        warnings = (*warnings, "business_checker_unavailable", f"business_checker_diagnostic:{canonical_json_bytes(business_diagnostic).decode('utf-8')}")
    business_ref = _json_output_to(output, f"{stem}/business-check.json", business)
    result = result_record(
        observation,
        checked,
        objective,
        instance_key=identity["instance_key"],
        instance_binding_digest=str(identity["instance_binding_digest"]),
        source_revision=prepared.get("source_revision"),
        candidate_revision=prepared.get("candidate_revision"),
        input_digest=str(identity["input_digest"]),
        layer=layer,
        bound_evidence_ref=backend_observation_ref,
        solver_invoked=solver_invoked,
        deterministic_termination=deterministic_termination,
        warnings=warnings,
        core_gap_target=(
            {"relative_gap": options["relative_gap"], "absolute_gap": options["absolute_gap"]}
            if prepared.get("schema_version") == 3
            else None
        ),
        backend_profile=backend_profile,
        frozen_solve_options=options,
    )
    result["witness"] = witness_ref
    result["check"] = check_ref
    result["lexicographic"] = dict(lex_context) if lex_context is not None else None
    result_ref = _json_output_to(output, f"{stem}/result.json", result)
    return result, witness_record, checked.candidate_complete, {
        "result": result_ref,
        "model_check": check_ref,
        "business_check": business_ref,
        "witness": witness_ref,
        "backend_observation": dict(backend_observation_ref) if backend_observation_ref is not None else None,
        "raw_primals": dict(raw_primals_ref),
        "checker_consumed": list(checker_consumed),
        "model_consumed": [dict(ref) for ref in (*model_consumed, *extractor_consumed)],
        "runtime_refs": [dict(ref) for ref in runtime_refs],
        "violations": violations_ref,
        "ancillary": [dict(ref) for ref in ancillary_refs],
    }


def _run_direct_instances(
    reader: DataReader,
    output: _OutputTarget,
    prepared_manifest: Mapping[str, Any],
    *,
    input_digest: str,
    data_bundle: DataBundle,
    base_consumed_inputs: Sequence[Mapping[str, Any]],
    execution_started: float,
    backend_profile: Mapping[str, Any],
    enforce_execution_budget: bool,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], list[Mapping[str, Any]], bool, bool]:
    frozen = prepared_manifest["candidate"]
    data_info = prepared_manifest["data"]
    parameter_id = data_info["instance_parameter_object_id"] if data_info is not None else None
    if parameter_id is None:
        instance_keys: list[tuple[str | int, ...]] = [()]
    else:
        parameter_table = data_bundle.object(parameter_id)
        if parameter_table.kind != "table":
            raise ContractError("invalid_argument", "instance parameter object must be a table")
        instance_keys = list(parameter_table.keys)
    if not instance_keys:
        raise ContractError("capability_uncovered", "the frozen batch contains no instances")

    options = prepared_manifest["solve_options"]
    plan = frozen["objective_plan"]
    output_records: list[Mapping[str, Any]] = []
    output_refs: list[Mapping[str, Any]] = []
    consumed_records: list[Mapping[str, Any]] = [dict(ref) for ref in base_consumed_inputs]
    started_count = 0
    stop_reason: str | None = None
    for index, instance_key in enumerate(instance_keys):
        if enforce_execution_budget and time.monotonic() - execution_started >= float(options["execution_budget_seconds"]) - 30.0:
            stop_reason = "execution_budget_reserved"
            break
        started_count += 1
        instance_bundle = data_bundle.for_instance(parameter_id, instance_key)
        context = instance_bundle.instance_context
        binding_digest = context.instance_binding_digest if context is not None else instance_bundle.candidate_binding_digest
        identity = {
            "input_digest": input_digest,
            "instance_key": list(instance_key),
            "instance_binding_digest": binding_digest,
        }
        point_value, program_consumed = call_frozen_function(
            reader,
            frozen["model"],
            frozen["local_modules"],
            function_name="solve",
            args=(instance_bundle,),
            pointer="/candidate/model",
            require_extract_solution=frozen["business_checker"] is not None,
        )
        witness = direct_witness_from_output(
            point_value,
            plan,
            input_digest=input_digest,
            instance_key=instance_key,
            instance_binding_digest=binding_digest,
        )
        stem = f"instances/{index:06d}/layer-single"
        witness_ref = output.write_json(f"{stem}/witness.json", witness) if witness is not None else None
        objective_values = witness["objective_values"] if witness is not None else []
        warnings: list[str] = []
        checker_consumed: tuple[Mapping[str, Any], ...] = ()
        extractor_consumed: tuple[Mapping[str, Any], ...] = ()
        business_diagnostic: dict[str, Any] | None = None
        if frozen["business_checker"] is None:
            business = business_check_uncovered(
                input_digest=input_digest,
                instance_key=instance_key,
                instance_binding_digest=binding_digest,
                reason="business_checker_not_declared",
            )
        elif witness is None:
            business = business_check_uncovered(
                input_digest=input_digest,
                instance_key=instance_key,
                instance_binding_digest=binding_digest,
                reason="no_checked_witness",
            )
        else:
            try:
                business_solution, extractor_consumed = _extract_business_solution(
                    reader,
                    frozen["model"],
                    frozen["local_modules"],
                    instance_bundle,
                    witness,
                )
                business, checker_consumed, business_diagnostic, business_solution = _business_check_solution(
                    reader,
                    frozen["business_checker"],
                    frozen["local_modules"],
                    instance_bundle,
                    business_solution,
                    input_digest=input_digest,
                    instance_key=instance_key,
                    instance_binding_digest=binding_digest,
                    expected_objective_values=objective_values,
                )
                business["solution"] = business_solution
            except ContractError as exc:
                business = business_check_uncovered(
                    input_digest=input_digest,
                    instance_key=instance_key,
                    instance_binding_digest=binding_digest,
                    reason="business_solution_extraction_error",
                )
                business_diagnostic = {"code": exc.code, "message": exc.message, "pointer": exc.pointer}
            except Exception as exc:
                business = business_check_uncovered(
                    input_digest=input_digest,
                    instance_key=instance_key,
                    instance_binding_digest=binding_digest,
                    reason="business_solution_extraction_error",
                )
                business_diagnostic = {"code": "business_solution_extraction_error", "error_type": type(exc).__name__}
        if business_diagnostic is not None:
            warnings.extend((
                "business_checker_unavailable",
                f"business_checker_diagnostic:{canonical_json_bytes(business_diagnostic).decode('utf-8')}",
            ))
        if witness is None:
            warnings.append("direct_python_returned_no_point")
        business_ref = output.write_json(f"{stem}/business-check.json", business)
        fully_checked = witness is not None and business_check_is_fully_passed(business)
        result = {
            "instance_key": list(instance_key),
            "instance_binding_digest": binding_digest,
            "layer": None,
            "source_revision": prepared_manifest.get("source_revision"),
            "candidate_revision": prepared_manifest.get("candidate_revision"),
            "input_digest": input_digest,
            "backend": dict(backend_profile),
            "raw_status": "direct_python_completed" if witness is not None else "direct_python_no_point",
            "termination": "feasible" if fully_checked else "unknown",
            "solver_invoked": False,
            "has_solver_incumbent": False,
            "has_checked_candidate": fully_checked,
            "witness": witness_ref,
            "objective": dict(objective_values[0]) if objective_values else None,
            "bound": None,
            "gap": {
                "absolute": None,
                "relative": None,
                "definition": "direct_python does not provide a native global bound or a bound-derived gap",
            },
            "check": business_ref,
            "coverage": list(business.get("uncovered", [])),
            "warnings": warnings,
        }
        result_ref = output.write_json(f"{stem}/result.json", result)
        output_records.append({
            "instance_key": list(instance_key),
            "row_digest": context.row_digest if context is not None else None,
            "instance_binding_digest": binding_digest,
            "layer": None,
            "result": result_ref,
            "model_check": None,
            "business_check": business_ref,
            "witness": witness_ref,
            "backend_observation": None,
            "raw_primals": None,
            "checker_consumed": list(checker_consumed),
            "model_consumed": [dict(ref) for ref in (*program_consumed, *extractor_consumed)],
            "runtime_refs": [],
            "violations": None,
            "ancillary": [],
        })
        output_refs.extend(ref for ref in (result_ref, business_ref, witness_ref) if isinstance(ref, Mapping))
        consumed_records.extend(program_consumed)
        consumed_records.extend(extractor_consumed)
        consumed_records.extend(checker_consumed)

    remaining_count = len(instance_keys) - started_count
    batch_summary = {
        "schema_version": 1,
        "planned_instance_count": len(instance_keys),
        "started_instance_count": started_count,
        "instances_with_results": len(output_records),
        "instances_with_all_layers_attempted": started_count,
        "partial_instance_count": 0,
        "remaining_instance_count": remaining_count,
        "stop_reason": stop_reason,
    }
    output_refs.append(output.write_json("batch-summary.json", batch_summary))
    return output_records, output_refs, _unique_refs(consumed_records), False, False


def _run_instances(
    reader: DataReader,
    output: _OutputTarget,
    prepared_manifest: Mapping[str, Any],
    runtime_refs: Sequence[Mapping[str, Any]],
    *,
    input_digest: str,
    data_bundle: DataBundle,
    base_consumed_inputs: Sequence[Mapping[str, Any]],
    execution_started: float,
    backend_profile: Mapping[str, Any],
    enforce_execution_budget: bool = True,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], list[Mapping[str, Any]], bool, bool]:
    frozen = prepared_manifest["candidate"]
    if frozen.get("authoring", "pyomo") == "direct_python":
        return _run_direct_instances(
            reader,
            output,
            prepared_manifest,
            input_digest=input_digest,
            data_bundle=data_bundle,
            base_consumed_inputs=base_consumed_inputs,
            execution_started=execution_started,
            backend_profile=backend_profile,
            enforce_execution_budget=enforce_execution_budget,
        )
    if backend_profile.get("interface_available") is False:
        raise ContractError(
            "environment_unavailable",
            "the selected solver interface is unavailable in the worker environment",
            details={"backend_profile": dict(backend_profile)},
        )
    data_info = prepared_manifest["data"]
    if data_info is None:
        if data_bundle.data_id is not None:
            raise ContractError("artifact_changed", "empty PreparedInputManifest data does not match the loaded DataBundle")
        parameter_id = None
    else:
        parameter_id = data_info["instance_parameter_object_id"]
    if parameter_id is None:
        instance_keys: list[tuple[str | int, ...]] = [()]
    else:
        parameter_table = data_bundle.object(parameter_id)
        if parameter_table.kind != "table":
            raise ContractError("invalid_argument", "instance parameter object must be a table")
        instance_keys = list(parameter_table.keys)
    if not instance_keys:
        raise ContractError("capability_uncovered", "the frozen batch contains no instances")
    plan = frozen["objective_plan"]
    options = prepared_manifest["solve_options"]
    started_mono = execution_started
    output_records: list[Mapping[str, Any]] = []
    output_refs: list[Mapping[str, Any]] = []
    any_solver_invoked = False
    any_solver_returned = False
    started_instance_count = 0
    completed_instance_count = 0
    partial_instance_count = 0
    stop_reason: str | None = None
    for instance_index, instance_key in enumerate(instance_keys):
        if enforce_execution_budget and time.monotonic() - started_mono >= float(options["execution_budget_seconds"]) - 30.0:
            stop_reason = "execution_budget_reserved"
            break
        started_instance_count += 1
        instance_bundle = data_bundle.for_instance(parameter_id, instance_key)
        build_start = time.perf_counter()
        model, model_consumed = build_native_model(
            reader,
            instance_bundle,
            frozen["model"],
            frozen["local_modules"],
            backend_id=str(frozen["backend_id"]),
            require_extract_solution=frozen["business_checker"] is not None,
        )
        build_seconds = time.perf_counter() - build_start
        layers = _objective_list(plan)
        if not layers:
            raise ContractError("capability_uncovered", "the frozen objective plan contains no layers")
        prior_result: dict[str, Any] | None = None
        completed_all_layers = True
        for layer_index, layer in enumerate(layers):
            native_budget_remaining: float | None = None
            if enforce_execution_budget:
                current = time.monotonic() - started_mono
                remaining = float(options["execution_budget_seconds"]) - current
                native_budget_remaining = remaining - 30.0
                if native_budget_remaining <= 0:
                    completed_all_layers = False
                    stop_reason = "execution_budget_reserved"
                    break
            legacy_manifest = prepared_manifest.get("schema_version") == 2
            # Schema 2 froze the old HiGHS cap semantics. Schema 3 keeps the
            # managed execution budget separate from an explicitly requested
            # native time limit; the outer worker timeout remains authoritative.
            if legacy_manifest and enforce_execution_budget:
                solve_budget = min(float(options["time_limit_seconds"]), float(native_budget_remaining))
            else:
                solve_budget = options["time_limit_seconds"]
            objective = objective_for_layer(model, plan, layer)
            lex_context: dict[str, Any] | None = None
            if layer is not None and layer > 0:
                if prior_result is None or not prior_result["has_checked_candidate"]:
                    completed_all_layers = False
                    stop_reason = stop_reason or "lexicographic_bound_not_within_tolerance"
                    break
                previous_layer = plan["layers"][layer - 1]
                previous_objective = prior_result.get("objective")
                previous_bound = prior_result.get("bound")
                previous_gap = prior_result.get("gap")
                if (
                    not isinstance(previous_objective, Mapping)
                    or not isinstance(previous_bound, Mapping)
                    or previous_bound.get("scope") != "global"
                    or previous_bound.get("direction") != ("lower" if previous_layer["sense"] == "min" else "upper")
                    or not isinstance(previous_gap, Mapping)
                ):
                    completed_all_layers = False
                    stop_reason = stop_reason or "lexicographic_bound_not_within_tolerance"
                    break
                lock = checked_lexicographic_lock(
                    bound_value=previous_bound.get("value"),
                    sense=previous_layer["sense"],
                    objective_value=previous_objective.get("value"),
                    absolute_tolerance=previous_layer["absolute_tolerance"],
                    relative_tolerance=previous_layer["relative_tolerance"],
                    stored_absolute_gap=previous_gap.get("absolute"),
                )
                if lock is None:
                    completed_all_layers = False
                    stop_reason = stop_reason or "lexicographic_bound_not_within_tolerance"
                    break
                previous_record = next(
                    (
                        record
                        for record in reversed(output_records)
                        if record["instance_key"] == list(instance_key) and record["layer"] == layer - 1
                    ),
                    None,
                )
                if previous_record is None:
                    raise ContractError("artifact_changed", "previous lexicographic Result Ref is missing")
                lex_context = {"previous_layer": layer - 1, "previous_result_ref": dict(previous_record["result"]), **lock}
                from pyomo.environ import Constraint, Objective

                lock_name = f"_optagent_lex_lock_{layer - 1}"
                if model.component(lock_name) is not None:
                    raise ContractError("package_collision", "candidate model already uses a reserved lex lock name")
                if previous_layer["sense"] == "min":
                    lock = previous_layer["objective_component"]
                    prev_obj = next(obj for obj in model.component_data_objects(Objective, active=None, descend_into=True) if obj.parent_component().name == lock)
                    model.add_component(lock_name, Constraint(expr=prev_obj.expr <= lex_context["restriction_value"]))
                else:
                    lock = previous_layer["objective_component"]
                    prev_obj = next(obj for obj in model.component_data_objects(Objective, active=None, descend_into=True) if obj.parent_component().name == lock)
                    model.add_component(lock_name, Constraint(expr=prev_obj.expr >= lex_context["restriction_value"]))
                objective = objective_for_layer(model, plan, layer)
            if layer is None:
                stem = f"instances/{instance_index:06d}/layer-single"
            else:
                stem = f"instances/{instance_index:06d}/layer-{layer:04d}"
            domain_failures = []
            if not is_native_cp_model_build(model):
                from pyomo.environ import Var

                for variable in model.component_data_objects(Var, active=True, descend_into=True):
                    impossible, reason = empty_domain(variable)
                    if impossible:
                        domain_failures.append(reason)
            solve_start = time.perf_counter()
            deterministic_termination: str | None = None
            observation: Any | None = None
            if domain_failures:
                deterministic_termination = "infeasible"
                primals: Mapping[Any, Any] = {}
                warnings = ("deterministic_empty_domain",)
                invoked = False
            elif _requires_solver(model):
                any_solver_invoked = True
                if options["write_log"]:
                    output.ensure_dir(stem)
                log_name = "highs.log" if legacy_manifest else "solver.log"
                log_path = str(output.path(f"{stem}/{log_name}")) if options["write_log"] else None
                if legacy_manifest:
                    observation = run_highs_once(
                        model,
                        time_limit_seconds=float(solve_budget),
                        relative_gap=options["relative_gap"],
                        absolute_gap=options["absolute_gap"],
                        threads=int(options["threads"]),
                        random_seed=int(options["random_seed"]),
                        feasibility_tolerance=float(options["feasibility_tolerance"]),
                        integrality_tolerance=float(options["integrality_tolerance"]),
                        log_path=log_path,
                        backend_profile=backend_profile,
                    )
                else:
                    from .solver_protocol import run_solver_once
                    observation = run_solver_once(
                        model,
                        backend_id=str(frozen["backend_id"]),
                        objective_sense=objective.sense,
                        time_limit_seconds=solve_budget,
                        relative_gap=float(options["relative_gap"]),
                        absolute_gap=float(options["absolute_gap"]),
                        threads=options["threads"],
                        random_seed=options["random_seed"],
                        feasibility_tolerance=float(options["feasibility_tolerance"]),
                        integrality_tolerance=float(options["integrality_tolerance"]),
                        backend_profile=backend_profile,
                        log_path=log_path,
                        warm_start=options.get("warm_start", False),
                    )
                any_solver_returned = any_solver_returned or observation.record.get("solver_returned") is True
                primals = observation.primals
                warnings = ()
                invoked = observation.solver_invoked
            else:
                deterministic_termination = "optimal" if objective.component is not None else "feasible"
                primals = {}
                warnings = ("deterministic_constant_model",)
                invoked = False
            solve_seconds = time.perf_counter() - solve_start
            native_evidence: dict[str, Any] | None = None
            if is_native_cp_model_build(model):
                response_values = observation.native_response_solution if observation is not None else None
                raw_primal_ref = output.write_stream(
                    f"{stem}/raw-primals.jsonl",
                    _jsonl_chunks(_raw_native_cp_sat_rows(response_values)),
                    "application/x-ndjson",
                )
                if observation is None or not isinstance(observation.native_model_proto, bytes):
                    raise ContractError("artifact_changed", "native CP-SAT solve did not preserve the exact input CpModel proto")
                model_proto_ref = output.write(
                    f"{stem}/native-model.pb",
                    observation.native_model_proto,
                    "application/x-protobuf",
                )
                binding_ref = output.write_json(
                    f"{stem}/native-variable-bindings.json",
                    {"variables": native_variable_binding_rows(model)},
                )
                native_evidence = {
                    "model_proto": model_proto_ref,
                    "variable_bindings": binding_ref,
                    "response_vector": raw_primal_ref,
                }
                output_refs.extend((model_proto_ref, binding_ref))
            else:
                raw_primal_ref = output.write_stream(
                    f"{stem}/raw-primals.jsonl",
                    _jsonl_chunks(_raw_primal_rows(primals)),
                    "application/x-ndjson",
                )
            if observation is not None:
                observation_record = dict(observation.record)
                observation_backend = dict(observation.backend)
                observation_nonfinite = dict(observation.nonfinite_fields)
                defer_synthetic_observation = False
            elif not legacy_manifest:
                native = {
                    "time_limit_seconds": options["time_limit_seconds"],
                    "threads": options["threads"],
                    "random_seed": options["random_seed"],
                    "warm_start": options.get("warm_start", False),
                    "parameters": {},
                }
                observation_record = {
                    "termination": deterministic_termination,
                    "raw_status": "deterministic_model_check",
                    "solver_invoked": False,
                    "solver_returned": False,
                    "raw_solution_value_valid": None,
                    "best_feasible_objective": None,
                    "best_objective_bound": None,
                    "bound_evidence": None,
                    "termination_condition": deterministic_termination,
                    "solve_wall_seconds": 0.0,
                    "error": None,
                    "requested_options": {
                        "native": native,
                        "core": {
                            "relative_gap": options["relative_gap"],
                            "absolute_gap": options["absolute_gap"],
                            "feasibility_tolerance": options["feasibility_tolerance"],
                            "integrality_tolerance": options["integrality_tolerance"],
                        },
                    },
                    "effective_options": {
                        "time_limit_seconds": None,
                        "threads": None,
                        "random_seed": None,
                        "warm_start": None,
                        "parameters": {},
                    },
                    "option_set_statuses": {
                        "time_limit_seconds": "not_applied_solver_not_invoked" if options["time_limit_seconds"] is not None else "not_requested",
                        "threads": "not_applied_solver_not_invoked" if options["threads"] is not None else "not_requested",
                        "random_seed": "not_applied_solver_not_invoked" if options["random_seed"] is not None else "not_requested",
                        "warm_start": "not_applied_solver_not_invoked" if options.get("warm_start", False) else "not_requested",
                        "relative_gap": "core_criterion_only",
                        "absolute_gap": "core_criterion_only",
                        "feasibility_tolerance": "core_checker_only",
                        "integrality_tolerance": "core_checker_only",
                    },
                    "core_gap_criterion": {
                        "relative_gap": options["relative_gap"],
                        "absolute_gap": options["absolute_gap"],
                        "formula": "gap <= max(absolute_gap, relative_gap * abs(checked_objective))",
                        "source": "frozen_core_check",
                    },
                }
                observation_backend = None
                observation_nonfinite = {}
                defer_synthetic_observation = True
            else:
                observation_record = {
                    "raw_status": "deterministic_model_check",
                    "termination": deterministic_termination,
                    "solver_invoked": False,
                }
                observation_backend = None
                observation_nonfinite = {}
                defer_synthetic_observation = True
            backend_observation_payload = {
                "observation": observation_record,
                "backend": observation_backend,
                "nonfinite_fields": observation_nonfinite,
                "raw_primals": raw_primal_ref,
                "phase_seconds": {"build": build_seconds, "solve": solve_seconds},
            }
            backend_observation_ref: dict[str, Any] | None = None
            if not defer_synthetic_observation:
                backend_observation_ref = _json_output_to(
                    output,
                    f"{stem}/backend-observation.json",
                    backend_observation_payload,
                )
                output_refs.extend((raw_primal_ref, backend_observation_ref))
            else:
                output_refs.append(raw_primal_ref)
            if options["export_model"]:
                try:
                    output.ensure_dir(stem)
                    export_name = f"{stem}/model.textproto" if is_native_cp_model_build(model) else f"{stem}/model.lp"
                    export_path = output.path(export_name)
                    if os.path.lexists(export_path):
                        raise ContractError("package_collision", "worker model export path already exists")
                    if is_native_cp_model_build(model):
                        export_ref = output.write(
                            export_name,
                            _native_cp_model_textproto_bytes(model),
                            "text/plain; charset=utf-8",
                        )
                    else:
                        model.write(str(export_path), io_options={"symbolic_solver_labels": True})
                        export_ref = output.register_file(export_name, "text/plain; charset=utf-8")
                except Exception as exc:
                    if isinstance(exc, ContractError):
                        raise
                    raise ContractError("capability_uncovered", "Pyomo could not export this native model", details={"error_type": type(exc).__name__}) from exc
            else:
                export_ref = None
            log_ref = None
            if options["write_log"]:
                log_name = "highs.log" if prepared_manifest.get("schema_version") == 2 else "solver.log"
                log_path = output.path(f"{stem}/{log_name}")
                if os.path.lexists(log_path):
                    log_ref = output.register_file(f"{stem}/{log_name}", "text/plain; charset=utf-8")
            result, witness, complete, refs = _write_layer(
                reader=reader,
                output=output,
                stem=stem,
                model=model,
                model_spec=frozen["model"],
                instance_bundle=instance_bundle,
                prepared={
                    **prepared_manifest,
                    "input_digest": input_digest,
                    "source_revision": prepared_manifest["source_revision"],
                    "candidate_revision": prepared_manifest["candidate_revision"],
                },
                objective=objective,
                layer=layer,
                observation=observation,
                primals=primals,
                deterministic_termination=deterministic_termination,
                solver_invoked=invoked,
                warnings=warnings,
                checker_spec=frozen["business_checker"],
                module_specs=frozen["local_modules"],
                runtime_refs=runtime_refs,
                backend_profile=backend_profile,
                backend_observation_ref=backend_observation_ref,
                raw_primals_ref=raw_primal_ref,
                model_consumed=model_consumed,
                lex_context=lex_context,
                ancillary_refs=[ref for ref in (export_ref, log_ref) if ref is not None],
                native_evidence=native_evidence,
            )
            if defer_synthetic_observation:
                final_termination = result["termination"]
                observation_record["termination"] = final_termination
                if "termination_condition" in observation_record:
                    observation_record["termination_condition"] = final_termination
                backend_observation_ref = _json_output_to(
                    output,
                    f"{stem}/backend-observation.json",
                    backend_observation_payload,
                )
                refs["backend_observation"] = backend_observation_ref
                output_refs.append(backend_observation_ref)
            instance_context = instance_bundle.instance_context
            output_records.append({
                "instance_key": list(instance_key),
                "row_digest": instance_context.row_digest if instance_context is not None else None,
                "instance_binding_digest": instance_context.instance_binding_digest if instance_context is not None else instance_bundle.candidate_binding_digest,
                "layer": layer,
                **refs,
            })
            output_refs.extend(
                refs[key]
                for key in ("result", "model_check", "business_check", "witness", "violations")
                if isinstance(refs.get(key), dict)
            )
            output_refs.extend(refs["ancillary"])
            prior_result = result
            if not complete:
                if layer_index + 1 < len(layers):
                    completed_all_layers = False
                    stop_reason = stop_reason or "lexicographic_layer_unavailable"
                break
        if completed_all_layers:
            completed_instance_count += 1
        else:
            partial_instance_count += 1
        if stop_reason == "execution_budget_reserved":
            break
    remaining_instance_count = len(instance_keys) - started_instance_count
    batch_summary = {
        "schema_version": 1,
        "planned_instance_count": len(instance_keys),
        "started_instance_count": started_instance_count,
        "instances_with_results": len({canonical_json_bytes(list(item["instance_key"])) for item in output_records}),
        "instances_with_all_layers_attempted": completed_instance_count,
        "partial_instance_count": partial_instance_count,
        "remaining_instance_count": remaining_instance_count,
        "stop_reason": stop_reason,
    }
    batch_ref = _json_output_to(output, "batch-summary.json", batch_summary)
    output_refs.append(batch_ref)
    consumed = [
        dict(item)
        for item in [
            *base_consumed_inputs,
            *(ref for record in output_records for ref in [*record.get("model_consumed", []), *record.get("checker_consumed", [])]),
        ]
    ]
    # Ref paths are the identity; repeated imports or shared data bytes are a
    # single consumed input, while distinct refs with one hash remain distinct.
    unique: dict[str, Mapping[str, Any]] = {}
    for ref in consumed:
        if isinstance(ref, dict) and isinstance(ref.get("path"), str):
            previous = unique.get(ref["path"])
            if previous is not None and dict(previous) != dict(ref):
                raise ContractError("artifact_changed", "one consumed path has conflicting content refs")
            unique[ref["path"]] = ref
    return output_records, tuple(output_refs), tuple(unique[path] for path in sorted(unique)), any_solver_invoked, any_solver_returned


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _unique_refs(refs: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    unique: dict[str, dict[str, Any]] = {}
    for ref_value in refs:
        ref = validate_ref_shape(dict(ref_value))
        previous = unique.get(ref["path"])
        if previous is not None and previous != ref:
            raise ContractError("artifact_changed", "one consumed path has conflicting content refs")
        unique[ref["path"]] = ref
    return tuple(unique[path] for path in sorted(unique))


def _collect_worker_inputs(
    reader: DataReader,
    prepared_manifest: Mapping[str, Any],
    manifest_ref: Mapping[str, Any],
    runtime_refs: Sequence[Mapping[str, Any]],
    *,
    invocation_ref: Mapping[str, Any] | None = None,
) -> tuple[DataBundle, tuple[dict[str, Any], ...]]:
    """Verify the full frozen native input closure using the caller's Reader."""
    refs: list[Mapping[str, Any]] = [manifest_ref]
    if invocation_ref is not None:
        refs.insert(0, invocation_ref)
    source_records = read_prepared_source_records(reader, prepared_manifest["sources"])
    for source in prepared_manifest["sources"]:
        refs.extend((source["record"], source["bytes"]))

    candidate = prepared_manifest["candidate"]
    refs.append(candidate["model"]["bytes"])
    refs.extend(module["bytes"] for module in candidate["local_modules"])
    if candidate["business_checker"] is not None:
        refs.append(candidate["business_checker"]["bytes"])
    if candidate.get("requirements_file") is not None:
        refs.append(candidate["requirements_file"]["bytes"])
    data_info = prepared_manifest["data"]
    if data_info is None:
        data_bundle = load_data_bundle(
            reader,
            None,
            candidate_binding_digest=prepared_manifest["binding_digest"],
        )
        expected_data_id = None
    else:
        if not isinstance(data_info, dict) or set(data_info) != {"data_id", "manifest", "instance_parameter_object_id"}:
            raise ContractError("output_invalid", "prepared data identity has invalid fields")
        data_bundle = load_data_bundle(
            reader,
            data_info["manifest"],
            candidate_binding_digest=prepared_manifest["binding_digest"],
            source_records=source_records,
        )
        expected_data_id = data_info["data_id"]
    if data_bundle.data_id != expected_data_id:
        raise ContractError("artifact_changed", "frozen DataBundle identity differs from PreparedInputManifest")
    already_read = {
        (ref["path"], ref["sha256"], ref["byte_len"], ref["media_type"])
        for ref in data_bundle.consumed_inputs
    }
    already_read.update(
        (validate_ref_shape(source["record"])["path"], validate_ref_shape(source["record"])["sha256"], validate_ref_shape(source["record"])["byte_len"], validate_ref_shape(source["record"])["media_type"])
        for source in prepared_manifest["sources"]
    )
    seen: set[tuple[str, str, int, str]] = set()
    for ref in refs[1:]:
        checked = validate_ref_shape(ref, pointer="/prepared/consumed_input")
        identity = (checked["path"], checked["sha256"], checked["byte_len"], checked["media_type"])
        if identity not in already_read and identity not in seen:
            _read_verified_ref(reader, checked, "/prepared/consumed_input")
        seen.add(identity)
    refs.extend(data_bundle.consumed_inputs)
    refs.extend(runtime_refs)
    refs.append(prepared_manifest["runtime_bundle"]["manifest"])
    return data_bundle, _unique_refs(refs)


def _fail_seal(
    workspace: Path,
    invocation: Mapping[str, Any],
    input_digest: str,
    started_at: str,
    error: BaseException,
    consumed_inputs: Sequence[Mapping[str, Any]],
) -> None:
    output_path = invocation["output_path"]
    try:
        failure = {
            "run_id": invocation["run_id"],
            "prepared_id": invocation["prepared_id"],
            "input_digest": input_digest,
            "error_code": error.code if isinstance(error, ContractError) else "worker_error",
            "error_type": type(error).__name__,
            "message": error.message if isinstance(error, ContractError) else str(error),
        }
        if isinstance(error, ContractError) and error.details:
            failure["details"] = dict(error.details)
        output_ref = _json_output(workspace, output_path, "worker-failure.json", failure)
        outputs = dict(_OUTPUT_REFS.get(output_path, {}))
        outputs[output_ref["path"]] = output_ref
        solver_returned = False
        for ref in outputs.values():
            if not ref["path"].endswith("/backend-observation.json"):
                continue
            try:
                artifact = strict_json_loads(_read_regular(workspace, _safe_relative(ref["path"])))
            except (ContractError, ValueError, UnicodeError):
                continue
            if isinstance(artifact, dict) and isinstance(artifact.get("observation"), dict):
                solver_returned = solver_returned or artifact["observation"].get("solver_returned") is True
        seal = {
            "schema_version": 2,
            "run_id": invocation["run_id"],
            "prepared_id": invocation["prepared_id"],
            "dispatch_nonce": invocation["dispatch_nonce"],
            "input_digest": input_digest,
            "output_refs": [outputs[path] for path in sorted(outputs)],
            "solver_returned": solver_returned,
            "worker_completed": False,
            "consumed_inputs": [dict(ref) for ref in _unique_refs(consumed_inputs)],
            "started_at": started_at,
            "completed_at": _utc_now(),
        }
        _json_output(workspace, output_path, "seal.json", seal)
    except Exception:
        # Never mask the triggering failure with an incomplete best-effort seal.
        return


def run_invocation(workspace_absolute: str, invocation_path: str, expected_sha256: str) -> dict[str, Any]:
    """Execute one immutable invocation and seal only its own output tree."""
    started_at = _utc_now()
    execution_started = time.monotonic()
    workspace = windows_long_path(workspace_absolute)
    if not workspace.is_absolute():
        raise ContractError("path_denied", "trusted worker workspace must be absolute")
    invocation, manifest, invocation_ref = _load_invocation(workspace, invocation_path, expected_sha256)
    _OUTPUT_REFS.pop(invocation["output_path"], None)
    reader = FrozenReader(workspace, invocation["case_id"], invocation["prepared_id"])
    output = _OutputTarget.platform(workspace, invocation["output_path"])
    consumed_inputs: tuple[Mapping[str, Any], ...] = (invocation_ref, invocation["input_manifest"])
    try:
        _, runtime_refs = verify_runtime_bundle(
            workspace,
            manifest["runtime_bundle"]["digest"],
            manifest["runtime_bundle"]["manifest"],
            prepared_schema_version=manifest["schema_version"],
        )
        data_bundle, consumed_inputs = _collect_worker_inputs(
            reader,
            manifest,
            invocation["input_manifest"],
            runtime_refs,
            invocation_ref=invocation_ref,
        )
        # Refuse a drifted managed interpreter before importing candidate code.
        if manifest.get("schema_version") == 2:
            backend_profile = _backend_versions()
        else:
            from .solver_protocol import DIRECT_PYTHON_BACKEND_ID, backend_profile_for, direct_python_profile
            if manifest["candidate"]["backend_id"] == DIRECT_PYTHON_BACKEND_ID:
                requirements_spec = manifest["candidate"].get("requirements_file")
                requirements_bytes = (
                    b""
                    if requirements_spec is None
                    else reader.read_ref(requirements_spec["bytes"])
                )
                backend_profile = direct_python_profile(requirements_bytes)
            else:
                backend_profile = backend_profile_for(str(manifest["candidate"]["backend_id"]))
        records, output_refs, consumed_inputs, solver_invoked, solver_returned = _run_instances(
            reader,
            output,
            manifest,
            runtime_refs,
            input_digest=invocation["input_digest"],
            data_bundle=data_bundle,
            base_consumed_inputs=consumed_inputs,
            execution_started=execution_started,
            backend_profile=backend_profile,
        )
        seal = {
            "schema_version": 2,
            "run_id": invocation["run_id"],
            "prepared_id": invocation["prepared_id"],
            "dispatch_nonce": invocation["dispatch_nonce"],
            "input_digest": invocation["input_digest"],
            "output_refs": [dict(ref) for ref in output_refs],
            "solver_returned": solver_returned,
            "worker_completed": True,
            "consumed_inputs": [dict(ref) for ref in consumed_inputs],
            "started_at": started_at,
            "completed_at": _utc_now(),
        }
        # Canonicalize refs once in the seal; a record may reference the same
        # output file through multiple evidence fields.
        by_path = {ref["path"]: ref for ref in seal["output_refs"]}
        seal["output_refs"] = [by_path[path] for path in sorted(by_path)]
        seal_ref = _json_output(workspace, invocation["output_path"], "seal.json", seal)
        return {"seal": seal_ref, "result_count": len(records), "solver_invoked": solver_invoked}
    except BaseException as exc:
        _fail_seal(workspace, invocation, invocation["input_digest"], started_at, exc, consumed_inputs)
        raise


def run_local_reproduction(
    reader: DataReader,
    prepared_manifest_ref: Mapping[str, Any],
    output_directory: str | os.PathLike[str],
) -> dict[str, Any]:
    """Recompute one frozen prepared input without creating platform authority.

    The caller supplies the portable Ref reader and a fresh output directory.
    This function consumes the original prepared closure and calls the same
    DataBundle loader, frozen-module loader, native adapter, model checker,
    business checker, and per-instance/lexicographic loop as the platform
    worker. It does not read workspace state or create Run/Stamp/nonce/Seal
    records. COMPLETE and fixed import-tree attestation happen in the isolated
    standard-library bootstrap before this helper module is imported.
    """
    # Check the caller-selected destination before reading inputs; an existing
    # directory is never overwritten or reused for a second observation.
    _OutputTarget.checked_local_root(output_directory)
    started_at = _utc_now()
    execution_started = time.monotonic()
    prepared_manifest, manifest_ref, input_digest = _load_prepared_manifest(reader, prepared_manifest_ref)
    runtime = prepared_manifest["runtime_bundle"]
    _, runtime_refs = verify_runtime_bundle(
        reader,
        runtime["digest"],
        runtime["manifest"],
        prepared_schema_version=prepared_manifest["schema_version"],
    )
    data_bundle, consumed_inputs = _collect_worker_inputs(
        reader,
        prepared_manifest,
        manifest_ref,
        runtime_refs,
    )
    if prepared_manifest.get("schema_version") == 2:
        backend_profile = _backend_versions()
    else:
        from .solver_protocol import DIRECT_PYTHON_BACKEND_ID, backend_profile_for, direct_python_profile
        if prepared_manifest["candidate"]["backend_id"] == DIRECT_PYTHON_BACKEND_ID:
            requirements_spec = prepared_manifest["candidate"].get("requirements_file")
            requirements_bytes = (
                b""
                if requirements_spec is None
                else reader.read_ref(requirements_spec["bytes"])
            )
            backend_profile = direct_python_profile(requirements_bytes)
        else:
            backend_profile = backend_profile_for(str(prepared_manifest["candidate"]["backend_id"]))

    # Run through the same interpreter environment as the selected solver
    # interface; the caller controls the managed execution context.
    output = _OutputTarget.create_local(output_directory)
    records, output_refs, consumed_inputs, solver_invoked, solver_returned = _run_instances(
        reader,
        output,
        prepared_manifest,
        runtime_refs,
        input_digest=input_digest,
        data_bundle=data_bundle,
        base_consumed_inputs=consumed_inputs,
        execution_started=execution_started,
        backend_profile=backend_profile,
    )
    completed_at = _utc_now()
    measured = {
        "schema_version": 1,
        "kind": "local-reproduction",
        "prepared_manifest": dict(manifest_ref),
        "input_digest": input_digest,
        "case_id": prepared_manifest["case_id"],
        "prepared_id": prepared_manifest["prepared_id"],
        "candidate_id": prepared_manifest["candidate_id"],
        "source_revision": prepared_manifest["source_revision"],
        "candidate_revision": prepared_manifest["candidate_revision"],
        "promotion_generation": prepared_manifest["promotion_generation"],
        "binding_digest": prepared_manifest["binding_digest"],
        "runtime_bundle": {
            "digest": runtime["digest"],
            "manifest": dict(runtime["manifest"]),
            "worker_entry": runtime["worker_entry"],
        },
        "data_bundle": {
            "data_id": data_bundle.data_id,
            "digest": data_bundle.digest,
            "loader_version": data_bundle.loader_version,
            "instance_parameter_object_id": None if prepared_manifest["data"] is None else prepared_manifest["data"]["instance_parameter_object_id"],
        },
        "backend_profile": backend_profile,
        "solve_options": dict(prepared_manifest["solve_options"]),
        "measurement_started_at": started_at,
        "measurement_completed_at": completed_at,
        "execution_elapsed_seconds": time.monotonic() - execution_started,
        "consumed_inputs": [dict(ref) for ref in consumed_inputs],
        "instances": [dict(record) for record in records],
        "output_refs": [dict(ref) for ref in output_refs],
        "solver_invoked": solver_invoked,
        "solver_returned": solver_returned,
    }
    report_ref = output.write_json("local-reproduction.json", measured)
    return {"report": measured, "report_ref": report_ref}


__all__ = ["FrozenReader", "run_invocation", "run_local_reproduction", "verify_runtime_bundle"]
