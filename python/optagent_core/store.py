"""Small file-backed case store with short per-case CAS locks.

The store contains business state and immutable evidence files. It deliberately
does not keep a second registry or hold locks while user code/HTTP/solvers run.
"""

from __future__ import annotations

import contextlib
import copy
import os
import re
import stat
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from .contracts import (
    ContractError,
    canonical_json_bytes,
    file_signature,
    publish_no_clobber,
    require_id,
    ref_from_bytes,
    require_hash,
    sha256_bytes,
    strict_json_loads,
    validate_ref_shape,
    windows_long_path,
)


_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _empty_current_records() -> dict[str, Any]:
    return {"questions": {}, "reviews": {}, "runs": {}, "presentation": None}


def _validate_current_records(value: Any, *, case_id: str) -> None:
    if value is None:
        return  # legacy Case v2 written before the index existed
    if not isinstance(value, dict) or set(value) != {"questions", "reviews", "runs", "presentation"}:
        raise ContractError("output_invalid", "Case current_records index has invalid fields")
    for family in ("questions", "reviews", "runs"):
        members = value[family]
        if not isinstance(members, dict):
            raise ContractError("output_invalid", "Case current_records family index is invalid")
        for object_id, ref_value in members.items():
            if not isinstance(object_id, str) or not object_id:
                raise ContractError("output_invalid", "Case current_records member identity is invalid")
            ref = validate_ref_shape(ref_value, pointer=f"/current_records/{family}/{object_id}")
            expected = f".reaslab/optagent/cases/{case_id}/{family}/{object_id}/versions/{ref['sha256']}.json"
            if ref["path"] != expected or ref["media_type"] != "application/json":
                raise ContractError("output_invalid", "Case current_records Ref is not a family version path")
    presentation = value["presentation"]
    if presentation is not None:
        ref = validate_ref_shape(presentation, pointer="/current_records/presentation")
        expected = f".reaslab/optagent/cases/{case_id}/presentations/{ref['sha256']}.json"
        selection_prefix = f".reaslab/optagent/cases/{case_id}/deliveries/"
        selection_path = ref["path"]
        is_selection = (
            selection_path.startswith(selection_prefix)
            and selection_path.endswith("/delivery.json")
            and len(selection_path[len(selection_prefix):].split("/")) == 2
            and re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                selection_path[len(selection_prefix):].split("/")[0],
            ) is not None
            and selection_path[len(selection_prefix):].split("/")[1] == "delivery.json"
        )
        if (ref["path"] != expected and not is_selection) or ref["media_type"] != "application/json":
            raise ContractError("output_invalid", "Case presentation Ref is not an immutable presentation or selection path")


def _is_reparse(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_FLAG)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(st.st_mode) or _is_reparse(st)


def _ensure_absolute_chain_has_no_reparse(path: Path) -> Path:
    """Return a normalized absolute path after checking every existing component.

    `Path.resolve(strict=True)` depends on the platform's final-path API. Some
    restricted Windows environments deny that API even for readable workspace
    directories, so containment is established lexically while explicitly
    rejecting links/reparse points in every path component.
    """
    absolute = windows_long_path(path)
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor = cursor / part
        if _is_link_or_reparse(cursor):
            raise ContractError("path_denied", "workspace path contains a link or reparse point")
    return absolute


def _validate_relative(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ContractError("path_denied", "path must be a non-empty workspace relative path")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or _DRIVE_RE.match(normalized):
        raise ContractError("path_denied", "absolute paths are not allowed")
    raw_parts = normalized.split("/")
    parts = tuple(PurePosixPath(normalized).parts)
    if not parts or any(part in {"", ".", ".."} for part in raw_parts):
        raise ContractError("path_denied", "path traversal is not allowed")
    return parts


def _ensure_no_reparse(root: Path, target: Path, *, allow_missing: bool) -> None:
    try:
        rel = target.relative_to(root)
    except ValueError as exc:
        raise ContractError("path_denied", "path escapes the workspace") from exc
    cursor = root
    for part in rel.parts:
        cursor = cursor / part
        if _is_link_or_reparse(cursor):
            raise ContractError("path_denied", "symlink or reparse path is not allowed")
        if not cursor.exists() and not allow_missing:
            raise ContractError("not_found", "path does not exist")


class _FileLock:
    def __init__(self, path: Path, timeout: float = 15.0) -> None:
        self.path = path
        self.timeout = timeout
        self._file: Any = None

    def __enter__(self) -> "_FileLock":
        if not self.path.parent.is_dir() or _is_link_or_reparse(self.path.parent) or _is_link_or_reparse(self.path):
            raise ContractError("path_denied", "lock path contains a link")
        self._file = open(self.path, "a+b")
        self._file.seek(0, os.SEEK_END)
        if self._file.tell() == 0:
            self._file.write(b"\0")
            self._file.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._lock_once()
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self._file.close()
                    self._file = None
                    raise ContractError("stale_revision", "case is busy; retry with its current revision")
                time.sleep(0.01)

    def _lock_once(self) -> None:
        self._file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._file is None:
            return
        try:
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


class StablePathSnapshot:
    """One bounded read tied to an open file identity until its final check."""

    def __init__(self, path: Path, stream: Any, signature: tuple[Any, ...] | None, raw: bytes | None) -> None:
        self.path = path
        self._stream = stream
        self._signature = signature
        self.raw = raw

    @property
    def missing(self) -> bool:
        return self._signature is None

    def unchanged(self) -> bool:
        try:
            _ensure_absolute_chain_has_no_reparse(self.path)
            if self._stream is None:
                try:
                    self.path.lstat()
                except FileNotFoundError:
                    return True
                return False
            opened = os.fstat(self._stream.fileno())
            current = self.path.lstat()
            if stat.S_ISLNK(current.st_mode) or _is_reparse(current) or not stat.S_ISREG(current.st_mode):
                return False
            return (
                file_signature(opened) == self._signature
                and file_signature(current) == self._signature
                and file_signature(opened) == file_signature(current)
            )
        except (OSError, ContractError, ValueError):
            return False

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def __enter__(self) -> "StablePathSnapshot":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


class CaseStore:
    """Access project-local records and files under a trusted workspace root."""

    def __init__(
        self,
        workspace: str | os.PathLike[str],
        *,
        create: bool = True,
        local_mode: bool = False,
    ) -> None:
        requested = Path(workspace)
        if not requested.is_absolute():
            raise ContractError("path_denied", "workspace root must be absolute")
        if create:
            windows_long_path(requested).mkdir(parents=True, exist_ok=True)
        try:
            self.workspace = _ensure_absolute_chain_has_no_reparse(windows_long_path(requested))
        except (OSError, ContractError) as exc:
            raise ContractError("path_denied", "workspace root is unavailable") from exc
        if not self.workspace.is_dir():
            raise ContractError("path_denied", "workspace root is not a directory")
        self.local_mode = local_mode is True
        reserved_root = self.workspace / ".reaslab"
        self.root = reserved_root / "optagent"
        if _is_link_or_reparse(reserved_root) or _is_link_or_reparse(self.root):
            raise ContractError("path_denied", "internal state root cannot be a link")
        if reserved_root.exists() and not reserved_root.is_dir():
            raise ContractError("path_denied", "reserved workspace root is not a directory")
        if create:
            reserved_root.mkdir(exist_ok=True)
            self.root.mkdir(exist_ok=True)
            for rel in ("cases", "source-bytes", "locks", "input-fences"):
                path = self.root / rel
                if _is_link_or_reparse(path):
                    raise ContractError("path_denied", "internal state directory cannot be a link")
                path.mkdir(exist_ok=True)
        _ensure_no_reparse(self.workspace, self.root, allow_missing=not create)

    def resolve_path(self, relative: str, *, must_exist: bool = False) -> Path:
        parts = _validate_relative(relative)
        target = windows_long_path(self.workspace.joinpath(*parts))
        _ensure_no_reparse(self.workspace, target, allow_missing=not must_exist)
        # `_validate_relative` removes traversal and `_ensure_no_reparse` checks
        # every child component. Since the workspace's full absolute chain was
        # checked at construction, this lexical child path is contained.
        return target

    def stable_path_snapshot(self, relative: str, *, max_bytes: int) -> StablePathSnapshot:
        """Read a bounded path once and retain its exact file descriptor for a later fence."""
        path = self.resolve_path(relative, must_exist=False)
        _ensure_absolute_chain_has_no_reparse(path)
        try:
            before_lstat = path.lstat()
        except FileNotFoundError:
            return StablePathSnapshot(path, None, None, None)
        if stat.S_ISLNK(before_lstat.st_mode) or _is_reparse(before_lstat) or not stat.S_ISREG(before_lstat.st_mode):
            raise ContractError("path_denied", "only regular files are allowed")
        stream: Any = None
        try:
            stream = open(path, "rb")
            opened = os.fstat(stream.fileno())
            if file_signature(before_lstat) != file_signature(opened):
                raise ContractError("source_changed_during_read", "opened file differs from the path checked before reading")
            if opened.st_size > max_bytes:
                raise ContractError("output_invalid", "stored JSON record exceeds the size limit")
            raw = stream.read(max_bytes + 1)
            after_opened = os.fstat(stream.fileno())
            after_lstat = path.lstat()
            _ensure_absolute_chain_has_no_reparse(path)
        except FileNotFoundError as exc:
            if stream is not None:
                stream.close()
            raise ContractError("source_changed_during_read", "path disappeared during its bounded read") from exc
        except BaseException:
            if stream is not None:
                stream.close()
            raise
        if stat.S_ISLNK(after_lstat.st_mode) or _is_reparse(after_lstat) or not stat.S_ISREG(after_lstat.st_mode):
            stream.close()
            raise ContractError("path_denied", "only regular files are allowed")
        signature = file_signature(opened)
        if (
            signature != file_signature(after_opened)
            or signature != file_signature(after_lstat)
            or len(raw) != opened.st_size
            or len(raw) != after_opened.st_size
        ):
            stream.close()
            raise ContractError("source_changed_during_read", "file or path changed during its bounded read")
        if len(raw) > max_bytes:
            stream.close()
            raise ContractError("output_invalid", "stored JSON record exceeds the size limit")
        return StablePathSnapshot(path, stream, signature, raw)

    def case_dir(self, case_id: str) -> Path:
        return self.resolve_path(f".reaslab/optagent/cases/{case_id}")

    def case_state_path(self, case_id: str) -> Path:
        return self.resolve_path(f".reaslab/optagent/cases/{case_id}/state.json")

    def case_lock(self, case_id: str) -> contextlib.AbstractContextManager[_FileLock]:
        path = self.resolve_path(f".reaslab/optagent/locks/{case_id}.lock")
        return _FileLock(path)

    def review_lock(self, review_id: str) -> contextlib.AbstractContextManager[_FileLock]:
        path = self.resolve_path(f".reaslab/optagent/locks/review-{review_id}.lock")
        return _FileLock(path)

    def run_lock(self, run_id: str) -> contextlib.AbstractContextManager[_FileLock]:
        path = self.resolve_path(f".reaslab/optagent/locks/run-{run_id}.lock")
        return _FileLock(path)

    def session_lock(self, session_digest: str) -> contextlib.AbstractContextManager[_FileLock]:
        path = self.resolve_path(f".reaslab/optagent/locks/session-{session_digest}.lock")
        return _FileLock(path)

    @staticmethod
    def _json_read(path: Path, *, max_bytes: int = 8 * 1024 * 1024) -> Any:
        raw = CaseStore._read_stable_path(path)
        if len(raw) > max_bytes:
            raise ContractError("output_invalid", "stored JSON record exceeds the size limit")
        try:
            return strict_json_loads(raw, max_bytes=max_bytes)
        except (ValueError, UnicodeError) as exc:
            raise ContractError("output_invalid", "stored JSON record is invalid") from exc

    def read_case(self, case_id: str) -> dict[str, Any]:
        path = self.case_state_path(case_id)
        try:
            value = self._json_read(path)
        except ContractError as exc:
            if exc.code != "not_found":
                raise
            raise ContractError("not_found", f"case state does not exist for case {case_id}") from exc
        if not isinstance(value, dict) or value.get("case_id") != case_id:
            raise ContractError("output_invalid", "stored case state is invalid")
        if value.get("schema_version") != 2:
            raise ContractError("legacy_read_only", "case schema is not writable by this version")
        if "current_records" in value and value["current_records"] is None:
            raise ContractError("output_invalid", "Case current_records index cannot be null")
        _validate_current_records(value.get("current_records"), case_id=case_id)
        return value

    def _remove_empty_case_artifact_directory(
        self,
        relative: str,
        identity: tuple[int, int, int, int, int, int],
    ) -> None:
        """Remove only this call's still-empty directory, never its contents."""
        try:
            parts = _validate_relative(relative)
            if len(parts) != 2 or parts[0] != "cases":
                return
            parent = self.resolve_path("cases", must_exist=True)
            candidate = parent / parts[1]
            info = candidate.lstat()
            if _is_link_or_reparse(candidate) or not stat.S_ISDIR(info.st_mode) or file_signature(info) != identity:
                return
            if next(candidate.iterdir(), None) is not None:
                return
            current = candidate.lstat()
            if _is_link_or_reparse(candidate) or file_signature(current) != identity:
                return
            candidate.rmdir()
        except (OSError, ContractError, ValueError):
            return

    def _allocate_case_artifact_directory(
        self,
        base_relative: str,
    ) -> tuple[str, tuple[int, int, int, int, int, int]]:
        parts = _validate_relative(base_relative)
        if len(parts) != 2 or parts[0] != "cases" or not parts[1].isidentifier():
            raise ContractError("invalid_argument", "new Case artifact directory must be one safe cases component")
        base_component = parts[1]
        reserved_paths: set[str] = set()
        stored_cases = self.resolve_path(".reaslab/optagent/cases", must_exist=True)
        for entry in stored_cases.iterdir():
            case_id = entry.name
            try:
                require_id(case_id, pointer="/case_id")
            except ContractError:
                continue
            try:
                self.resolve_path(f".reaslab/optagent/cases/{case_id}", must_exist=True)
                state = self.read_case(case_id)
            except ContractError as exc:
                if exc.code == "not_found":
                    continue
                if exc.code in {"legacy_read_only", "output_invalid", "artifact_changed"}:
                    # Only readable CaseState values participate in allocation.
                    # Do not make an unrelated malformed/unsupported Case block
                    # a new Case; path-safety errors still propagate.
                    continue
                raise
            artifact_dir = state.get("artifact_dir")
            if not isinstance(artifact_dir, str) or not artifact_dir:
                artifact_dir = f"cases/case_{case_id.replace('-', '')}"
            reserved_paths.add(artifact_dir.replace("\\", "/").casefold())

        cases_dir = self.resolve_path("cases")
        try:
            cases_dir.mkdir()
        except FileExistsError:
            pass
        cases_dir = self.resolve_path("cases", must_exist=True)
        if not cases_dir.is_dir():
            raise ContractError("path_denied", "workspace cases path is not a directory")
        existing_names = {entry.name.casefold() for entry in cases_dir.iterdir()}
        suffix = 1
        while True:
            component = base_component if suffix == 1 else f"{base_component}_{suffix}"
            relative = f"cases/{component}"
            if relative.casefold() in reserved_paths or component.casefold() in existing_names:
                suffix += 1
                continue
            candidate = cases_dir / component
            try:
                candidate.mkdir(exist_ok=False)
            except FileExistsError:
                # A pre-existing file, directory, or link occupies this spelling.
                # Never follow or remove it; the loop tries the next suffix.
                self.resolve_path("cases", must_exist=True)
                existing_names.add(component.casefold())
                suffix += 1
                continue
            info = candidate.lstat()
            identity = file_signature(info)
            try:
                if _is_link_or_reparse(candidate) or not stat.S_ISDIR(info.st_mode):
                    raise ContractError("path_denied", "reserved Case artifact path changed during creation")
                self.resolve_path(relative, must_exist=True)
            except BaseException:
                self._remove_empty_case_artifact_directory(relative, identity)
                raise
            return relative, identity

    def create_case_state(self, state: Mapping[str, Any], *, existing_source_id: str | None = None) -> dict[str, Any]:
        case_id = str(state.get("case_id", ""))
        case_dir = self.case_dir(case_id)
        if case_dir.exists():
            # Case creation captures its first immutable source before writing
            # state. Accept only that exact one-file staging shape; all other
            # pre-existing case directories remain collisions.
            if existing_source_id is None:
                raise ContractError("package_collision", "case identifier already exists")
            source_dir = case_dir / "sources"
            source_path = source_dir / f"{existing_source_id}.json"
            try:
                entries = list(case_dir.iterdir())
                source_entries = list(source_dir.iterdir())
            except OSError as exc:
                raise ContractError("package_collision", "case identifier already exists") from exc
            if entries != [source_dir] or source_entries != [source_path] or not source_path.is_file():
                raise ContractError("package_collision", "case identifier already exists")
            source = self.read_record(f".reaslab/optagent/cases/{case_id}/sources/{existing_source_id}.json")
            if source.get("source_id") != existing_source_id or existing_source_id not in state.get("source_ids", []):
                raise ContractError("package_collision", "case source staging record is invalid")
        else:
            case_dir.mkdir(parents=True, exist_ok=False)
        persisted_state = dict(state)
        base_artifact_dir = persisted_state.get("artifact_dir")
        if not isinstance(base_artifact_dir, str) or not base_artifact_dir:
            raise ContractError("invalid_argument", "new Case state requires an artifact directory suggestion")
        artifact_dir, reservation_identity = self._allocate_case_artifact_directory(base_artifact_dir)
        persisted_state["artifact_dir"] = artifact_dir
        try:
            raw = canonical_json_bytes(persisted_state)
            self._atomic_write(case_dir / "state.json", raw, replace=False)
        except BaseException:
            self._remove_empty_case_artifact_directory(artifact_dir, reservation_identity)
            raise
        return persisted_state

    def cas_case(
        self,
        case_id: str,
        expected_state_revision: int,
        update: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> dict[str, Any]:
        """Apply one short state mutation; all artifact work must happen first."""
        with self.case_lock(case_id):
            state = self.read_case(case_id)
            actual = state.get("state_revision")
            if actual != expected_state_revision:
                raise ContractError(
                    "stale_revision",
                    "case revision changed",
                    details={"expected": expected_state_revision, "actual": actual},
                )
            next_state = copy.deepcopy(state)
            returned = update(next_state)
            if returned is not None:
                next_state = returned
            if next_state.get("case_id") != case_id or next_state.get("schema_version") != 2:
                raise ContractError("output_invalid", "mutation produced invalid case state")
            if "current_records" in next_state and next_state["current_records"] is None:
                raise ContractError("output_invalid", "Case current_records index cannot be null")
            _validate_current_records(next_state.get("current_records"), case_id=case_id)
            next_state["state_revision"] = expected_state_revision + 1
            self._atomic_write(self.case_state_path(case_id), canonical_json_bytes(next_state), replace=True)
            return next_state

    def cas_case_family(
        self,
        case_id: str,
        family: str,
        object_id: str,
        expected_ref: Mapping[str, Any] | None,
        update: Callable[[dict[str, Any]], dict[str, Any] | None],
        *,
        legacy_path: str | None = None,
    ) -> dict[str, Any]:
        """Publish one family-local immutable Ref without advancing business revision."""
        if family not in {"questions", "reviews", "runs"}:
            raise ContractError("invalid_argument", "case family cannot be updated locally")
        with self.case_lock(case_id):
            state = self.read_case(case_id)
            records = state.get("current_records") or _empty_current_records()
            actual = records[family].get(object_id)
            normalized_expected = validate_ref_shape(dict(expected_ref)) if expected_ref is not None else None
            if actual is None and normalized_expected is not None and legacy_path is not None:
                try:
                    actual = self.ref_for_path(legacy_path, media_type="application/json")
                except ContractError as exc:
                    if exc.code != "not_found":
                        raise
            if actual != normalized_expected:
                raise ContractError("stale_revision", "current family Ref changed")
            next_state = copy.deepcopy(state)
            returned = update(next_state)
            if returned is not None:
                next_state = returned
            if next_state.get("case_id") != case_id or next_state.get("schema_version") != 2:
                raise ContractError("output_invalid", "family mutation produced invalid case state")
            if next_state.get("state_revision") != state.get("state_revision"):
                raise ContractError("output_invalid", "family-local mutation cannot advance business revision")
            _validate_current_records(next_state.get("current_records"), case_id=case_id)
            self._atomic_write(self.case_state_path(case_id), canonical_json_bytes(next_state), replace=True)
            return next_state

    def cas_presentation(
        self,
        case_id: str,
        delivery_id: str,
        expected_presentation: Mapping[str, Any] | None,
        next_presentation: Mapping[str, Any],
        *,
        expected_state_revision: int,
        owner_session_id: str,
        expected_binding: Mapping[str, Any],
        expected_authorities: Mapping[str, Mapping[str, Any]],
        validate_current: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Publish a report/package presentation without changing math revision."""
        new_ref = validate_ref_shape(dict(next_presentation), pointer="/current_records/presentation")
        _validate_current_records(
            {"questions": {}, "reviews": {}, "runs": {}, "presentation": new_ref},
            case_id=case_id,
        )
        normalized_expected = (
            validate_ref_shape(dict(expected_presentation), pointer="/current_records/presentation")
            if expected_presentation is not None
            else None
        )
        binding_fields = {"source_revision", "candidate_revision", "candidate_id", "promotion_generation"}
        if set(expected_binding) != binding_fields:
            raise ContractError("invalid_argument", "presentation binding fields are invalid")
        if set(expected_authorities) != {"reviews", "runs"}:
            raise ContractError("invalid_argument", "presentation authority families are invalid")
        with self.case_lock(case_id):
            state = self.read_case(case_id)
            if state.get("state_revision") != expected_state_revision:
                raise ContractError(
                    "stale_revision",
                    "case revision changed before presentation publication",
                    details={"expected": expected_state_revision, "actual": state.get("state_revision")},
                )
            if state.get("owner_session_id") != owner_session_id:
                raise ContractError("permission_denied", "caller is no longer the current case owner")
            if state.get("selected_delivery_id") != delivery_id:
                raise ContractError("stale_revision", "selected Delivery changed before presentation publication")
            if state.get("pending_inputs"):
                raise ContractError("input_unclassified", "case has unclassified input")
            if any(state.get(key) != expected_binding[key] for key in binding_fields):
                raise ContractError("stale_revision", "mathematical binding changed before presentation publication")
            current_records = state.get("current_records") or _empty_current_records()
            if current_records.get("presentation") != normalized_expected:
                raise ContractError("stale_revision", "current presentation changed before publication")
            for family in ("reviews", "runs"):
                expected_records = expected_authorities[family]
                if not isinstance(expected_records, Mapping):
                    raise ContractError("invalid_argument", "presentation family Ref map is invalid")
                current_family = current_records[family]
                if set(current_family) & set(expected_records) != set(expected_records):
                    raise ContractError("stale_revision", "selected authority record was removed")
                for object_id, ref_value in expected_records.items():
                    expected_ref = validate_ref_shape(dict(ref_value))
                    if current_family.get(object_id) != expected_ref:
                        raise ContractError("stale_revision", "selected Review/Run Ref changed before presentation publication")
            if validate_current is not None:
                validate_current(state)
            next_state = copy.deepcopy(state)
            records = next_state.get("current_records")
            if records is None:
                records = _empty_current_records()
                next_state["current_records"] = records
            records["presentation"] = new_ref
            _validate_current_records(records, case_id=case_id)
            self._atomic_write(self.case_state_path(case_id), canonical_json_bytes(next_state), replace=True)
            return next_state

    def write_family_version(self, case_id: str, family: str, object_id: str, value: Mapping[str, Any]) -> dict[str, Any]:
        """Write a content-addressed immutable Question/Review/Run snapshot."""
        if family not in {"questions", "reviews", "runs"}:
            raise ContractError("invalid_argument", "case family does not have version records")
        raw = canonical_json_bytes(dict(value))
        digest = sha256_bytes(raw)
        relative = f".reaslab/optagent/cases/{case_id}/{family}/{object_id}/versions/{digest}.json"
        return self._write_immutable_known_sha(relative, raw, "application/json", digest)

    def read_family_record(
        self,
        case_id: str,
        family: str,
        object_id: str,
        *,
        state: Mapping[str, Any] | None = None,
        legacy_path: str | None = None,
        include_raw: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]] | tuple[dict[str, Any], dict[str, Any], bytes]:
        """Read the Case-published immutable Ref, or an unindexed legacy file read-only."""
        if family not in {"questions", "reviews", "runs"}:
            raise ContractError("invalid_argument", "case family does not have version records")
        current = dict(state) if state is not None else self.read_case(case_id)
        if current.get("case_id") != case_id:
            raise ContractError("artifact_changed", "family record Case identity differs")
        records = current.get("current_records") or _empty_current_records()
        ref_value = records[family].get(object_id)
        if ref_value is not None:
            ref = validate_ref_shape(dict(ref_value), pointer=f"/current_records/{family}/{object_id}")
            expected_path = f".reaslab/optagent/cases/{case_id}/{family}/{object_id}/versions/{ref['sha256']}.json"
            if ref["path"] != expected_path or ref["media_type"] != "application/json":
                raise ContractError("artifact_changed", "current family Ref path is not content-addressed")
            raw = self.read_ref(ref)
            try:
                value = strict_json_loads(raw, max_bytes=len(raw))
            except (ValueError, UnicodeError) as exc:
                raise ContractError("output_invalid", "current family snapshot is invalid JSON") from exc
            if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
                raise ContractError("artifact_changed", "current family snapshot is not canonical JSON")
            return (value, ref, raw) if include_raw else (value, ref)
        if legacy_path is None:
            legacy_path = f".reaslab/optagent/cases/{case_id}/{family}/{object_id}.json"
            if family == "runs":
                legacy_path = f".reaslab/optagent/cases/{case_id}/runs/{object_id}/record.json"
        legacy_file = self.resolve_path(legacy_path)
        if not legacy_file.exists():
            raise ContractError("not_found", f"{family[:-1]} record does not exist")
        raw = self._read_stable_path(legacy_file)
        try:
            value = strict_json_loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise ContractError("output_invalid", "legacy family record is invalid JSON") from exc
        if not isinstance(value, dict):
            raise ContractError("output_invalid", "legacy family record is invalid")
        ref = ref_from_bytes(legacy_path.replace("\\", "/"), raw, "application/json")
        return (value, ref, raw) if include_raw else (value, ref)

    def read_record(self, relative_path: str) -> dict[str, Any]:
        path = self.resolve_path(relative_path, must_exist=True)
        value = self._json_read(path)
        if not isinstance(value, dict):
            raise ContractError("output_invalid", "stored record is invalid")
        return value

    def write_record(self, relative_path: str, value: Mapping[str, Any], *, replace: bool = False) -> dict[str, Any]:
        raw = canonical_json_bytes(dict(value))
        path = self.resolve_path(relative_path)
        self._atomic_write(path, raw, replace=replace)
        return ref_from_bytes(relative_path.replace("\\", "/"), raw, "application/json")

    def write_immutable(self, relative_path: str, raw: bytes, media_type: str) -> dict[str, Any]:
        digest = sha256_bytes(raw)
        return self._write_immutable_known_sha(relative_path, raw, media_type, digest)

    def write_content_addressed(
        self,
        directory: str,
        raw: bytes,
        media_type: str,
        *,
        suffix: str = ".json",
    ) -> dict[str, Any]:
        digest = sha256_bytes(raw)
        relative = f"{directory.rstrip('/')}/{digest}{suffix}"
        return self._write_immutable_known_sha(relative, raw, media_type, digest)

    def _write_immutable_known_sha(self, relative_path: str, raw: bytes, media_type: str, digest: str) -> dict[str, Any]:
        path = self.resolve_path(relative_path)
        try:
            self._atomic_write(path, raw, replace=False)
        except ContractError as exc:
            if exc.code != "package_collision":
                raise
            current = self._read_stable_path(path)
            if current != raw:
                raise ContractError("artifact_changed", "immutable artifact path already has different bytes") from exc
        return {
            "path": relative_path.replace("\\", "/"),
            "sha256": digest,
            "byte_len": len(raw),
            "media_type": media_type,
        }

    def read_ref(self, value: Mapping[str, Any]) -> bytes:
        ref = validate_ref_shape(value)
        path = self.resolve_path(ref["path"], must_exist=True)
        raw = self._read_stable_path(path)
        if len(raw) != ref["byte_len"] or sha256_bytes(raw) != ref["sha256"]:
            raise ContractError("artifact_changed", "artifact bytes no longer match their reference")
        return raw

    def read_workspace_file(self, relative_path: str) -> tuple[bytes, dict[str, Any]]:
        path = self.resolve_path(relative_path, must_exist=True)
        raw = self._read_stable_path(path)
        return raw, ref_from_bytes(relative_path.replace("\\", "/"), raw, _media_type(relative_path))

    def read_user_file(self, relative_path: str) -> tuple[bytes, dict[str, Any]]:
        parts = _validate_relative(relative_path)
        if parts[0].casefold() == ".reaslab":
            raise ContractError("path_denied", "path points into a reserved workspace area")
        return self.read_workspace_file(relative_path)

    def ref_for_path(self, relative_path: str, *, media_type: str | None = None) -> dict[str, Any]:
        path = self.resolve_path(relative_path, must_exist=True)
        raw = self._read_stable_path(path)
        return ref_from_bytes(relative_path.replace("\\", "/"), raw, media_type or _media_type(relative_path))

    def capture_source_bytes(self, raw: bytes, *, digest: str | None = None) -> dict[str, Any]:
        # `digest` is supplied only by a producer that hashed these exact
        # stable bytes already (workspace read or validated capture transfer).
        selected_digest = require_hash(digest, pointer="/digest") if digest is not None else sha256_bytes(raw)
        return self._write_immutable_known_sha(
            f".reaslab/optagent/source-bytes/{selected_digest}", raw, "application/octet-stream", selected_digest
        )

    def remove_file_if_hash(self, relative_path: str, expected_sha256: str) -> bool:
        """Remove a temporary raw file only if it still has the captured digest."""
        path = self.resolve_path(relative_path, must_exist=False)
        if not path.exists():
            return False
        raw = self._read_stable_path(path)
        if sha256_bytes(raw) != expected_sha256:
            raise ContractError("artifact_changed", "file changed before cleanup")
        path.unlink()
        return True

    @staticmethod
    def _read_stable_path(path: Path) -> bytes:
        _ensure_absolute_chain_has_no_reparse(path)
        try:
            before_lstat = path.lstat()
            if stat.S_ISLNK(before_lstat.st_mode) or _is_reparse(before_lstat) or not stat.S_ISREG(before_lstat.st_mode):
                raise ContractError("path_denied", "only regular files are allowed")
            with open(path, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if file_signature(before_lstat) != file_signature(opened):
                    raise ContractError("source_changed_during_read", "opened file differs from the path checked before reading")
                raw = stream.read()
                after_opened = os.fstat(stream.fileno())
            after_lstat = path.lstat()
            _ensure_absolute_chain_has_no_reparse(path)
        except FileNotFoundError as exc:
            raise ContractError("not_found", "file does not exist") from exc
        if stat.S_ISLNK(after_lstat.st_mode) or _is_reparse(after_lstat) or not stat.S_ISREG(opened.st_mode):
            raise ContractError("path_denied", "only regular files are allowed")
        if file_signature(opened) != file_signature(after_opened):
            raise ContractError("source_changed_during_read", "opened file changed while being read")
        if file_signature(after_opened) != file_signature(after_lstat):
            raise ContractError("source_changed_during_read", "path changed while being read")
        if len(raw) != opened.st_size or len(raw) != after_opened.st_size:
            raise ContractError("source_changed_during_read", "file size changed while being read")
        return raw

    def _atomic_write(self, path: Path, raw: bytes, *, replace: bool) -> None:
        try:
            rel_parent = path.parent.relative_to(self.workspace)
        except ValueError as exc:
            raise ContractError("path_denied", "write target escapes workspace") from exc
        cursor = self.workspace
        for part in rel_parent.parts:
            cursor = cursor / part
            if _is_link_or_reparse(cursor):
                raise ContractError("path_denied", "write path contains a link")
            if not cursor.exists():
                cursor.mkdir()
            if _is_link_or_reparse(cursor):
                raise ContractError("path_denied", "write path contains a link")
        if _is_link_or_reparse(path):
            raise ContractError("path_denied", "write path contains a link")
        if path.exists() and not replace:
            raise ContractError("package_collision", "target already exists")
        # Keep the temporary basename short. Repeating a UUID-heavy target
        # filename and another UUID can push otherwise valid nested evidence
        # paths past Windows' legacy path limit. mkstemp already supplies a
        # random collision-resistant component.
        fd, temp_name = tempfile.mkstemp(prefix=".oa-", suffix=".tmp", dir=path.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if _is_link_or_reparse(temp_path) or _is_link_or_reparse(path.parent):
                raise ContractError("path_denied", "write path changed during save")
            if replace:
                os.replace(temp_path, path)
            elif not publish_no_clobber(temp_path, path):
                raise ContractError("package_collision", "target already exists")
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _media_type(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".json": "application/json",
        ".csv": "text/csv; charset=utf-8",
        ".md": "text/markdown; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
        ".py": "text/x-python; charset=utf-8",
        ".zip": "application/zip",
    }.get(suffix, "application/octet-stream")
