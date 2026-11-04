from __future__ import annotations

import builtins
import ctypes
import errno
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import optagent_core.contracts as contracts
import optagent_core.store as store_module
from optagent_core.contracts import ContractError, file_signature, publish_no_clobber, workspace_path_identity
from optagent_core.store import CaseStore


def _force_linux_rename_error(monkeypatch, error_number):
    calls = []

    class NativeCall:
        argtypes = None
        restype = None

        def __call__(self, *arguments):
            calls.append(arguments)
            ctypes.set_errno(error_number)
            return -1

    native_call = NativeCall()

    class FakeLibc:
        renameat2 = native_call

    def fake_cdll(name, *, use_errno):
        assert name is None
        assert use_errno is True
        return FakeLibc()

    monkeypatch.setattr(contracts.sys, "platform", "linux")
    monkeypatch.setattr(contracts.ctypes, "CDLL", fake_cdll)
    return calls, native_call


@pytest.mark.parametrize("force_fallback", [False, True], ids=["rename", "link-fallback"])
def test_publish_no_clobber_moves_unicode_source_and_leaves_one_name(
    tmp_path, monkeypatch, force_fallback
):
    source = tmp_path / "输入文件.tmp"
    destination = tmp_path / "发布结果.json"
    raw = b"complete synthetic payload"
    source.write_bytes(raw)
    if force_fallback:
        _force_linux_rename_error(monkeypatch, errno.EINVAL)

    assert publish_no_clobber(source, destination)

    assert destination.read_bytes() == raw
    assert not source.exists()
    assert destination.stat().st_nlink == 1


@pytest.mark.parametrize("force_fallback", [False, True], ids=["rename", "link-fallback"])
def test_publish_no_clobber_keeps_existing_destination_and_source(
    tmp_path, monkeypatch, force_fallback
):
    source = tmp_path / "source.tmp"
    destination = tmp_path / "destination.json"
    source.write_bytes(b"new bytes")
    destination.write_bytes(b"existing bytes")
    if force_fallback:
        _force_linux_rename_error(monkeypatch, errno.EINVAL)

    assert not publish_no_clobber(source, destination)

    assert source.read_bytes() == b"new bytes"
    assert destination.read_bytes() == b"existing bytes"


@pytest.mark.parametrize("force_fallback", [False, True], ids=["rename", "link-fallback"])
def test_publish_no_clobber_concurrent_different_payloads_has_one_complete_winner(
    tmp_path, monkeypatch, force_fallback
):
    count = 12
    target = tmp_path / "shared.json"
    payloads = [f"payload-{index}:".encode() + bytes([index]) * 4096 for index in range(count)]
    sources = [tmp_path / f"source-{index}.tmp" for index in range(count)]
    for source, payload in zip(sources, payloads, strict=True):
        source.write_bytes(payload)
    if force_fallback:
        _force_linux_rename_error(monkeypatch, errno.EINVAL)

    start = threading.Barrier(count)

    def publish(item: tuple[Path, bytes]) -> tuple[bool, Path, bytes]:
        source, payload = item
        start.wait(timeout=10)
        return publish_no_clobber(source, target), source, payload

    with ThreadPoolExecutor(max_workers=count) as pool:
        results = list(pool.map(publish, zip(sources, payloads, strict=True)))

    winners = [(source, payload) for won, source, payload in results if won]
    losers = [source for won, source, _ in results if not won]
    assert len(winners) == 1
    assert target.read_bytes() == winners[0][1]
    assert not winners[0][0].exists()
    assert all(source.exists() for source in losers)
    assert target.stat().st_nlink == 1


@pytest.mark.parametrize("force_fallback", [False, True], ids=["rename", "link-fallback"])
def test_store_stable_reader_crossing_temp_cleanup_sees_unchanged_published_signature(
    tmp_path, monkeypatch, force_fallback
):
    workspace = tmp_path / "workspace"
    store = CaseStore(workspace, create=True)
    target = store.resolve_path("cases/state.json")
    raw = b'{"state_revision":1}'
    published = threading.Event()
    reader_at_open = threading.Event()
    release_reader = threading.Event()
    writer_finished = threading.Event()
    reader_context = threading.local()
    before_open_signatures: list[tuple[int, int, int, int, int, int]] = []
    published_sources: list[Path] = []
    if force_fallback:
        _force_linux_rename_error(monkeypatch, errno.EINVAL)

    real_publish = store_module.publish_no_clobber
    real_open = builtins.open
    real_lstat = Path.lstat

    def publish_then_pause(source, destination):
        result = real_publish(source, destination)
        assert result
        published_sources.append(Path(source))
        published.set()
        assert reader_at_open.wait(timeout=10)
        return result

    def observe_lstat(path, *args, **kwargs):
        result = real_lstat(path, *args, **kwargs)
        if getattr(reader_context, "active", False) and workspace_path_identity(path) == workspace_path_identity(target) and not before_open_signatures:
            before_open_signatures.append(file_signature(result))
        return result

    def pause_before_open(file, mode="r", *args, **kwargs):
        if getattr(reader_context, "active", False) and workspace_path_identity(file) == workspace_path_identity(target) and mode == "rb":
            reader_at_open.set()
            assert release_reader.wait(timeout=10)
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(store_module, "publish_no_clobber", publish_then_pause)
    monkeypatch.setattr(Path, "lstat", observe_lstat)
    monkeypatch.setattr(builtins, "open", pause_before_open)

    def write_state():
        try:
            store._atomic_write(target, raw, replace=False)
        finally:
            writer_finished.set()

    def read_state():
        reader_context.active = True
        assert published.wait(timeout=10)
        return store._read_stable_path(target)

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(write_state)
        reader = pool.submit(read_state)
        try:
            assert reader_at_open.wait(timeout=10)
            assert writer_finished.wait(timeout=10)
            assert len(published_sources) == 1
            assert not published_sources[0].exists()
            assert len(before_open_signatures) == 1
            published_info = target.lstat()
            assert file_signature(published_info) == before_open_signatures[0]
            assert published_info.st_nlink == 1
        finally:
            release_reader.set()
        writer.result(timeout=10)
        assert reader.result(timeout=10) == raw


def test_store_stable_reader_interleaved_with_link_cleanup_rejects_or_reads_complete_payload(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    store = CaseStore(workspace, create=True)
    target = store.resolve_path("cases/state.json")
    raw = b'{"state_revision":2}'
    link_created = threading.Event()
    reader_at_final_path_check = threading.Event()
    writer_finished = threading.Event()
    reader_context = threading.local()
    real_link = contracts.os.link
    real_lstat = Path.lstat
    _force_linux_rename_error(monkeypatch, errno.EINVAL)

    def pause_after_link(source, destination, *, follow_symlinks=True):
        result = real_link(source, destination, follow_symlinks=follow_symlinks)
        if workspace_path_identity(os.fsdecode(destination)) == workspace_path_identity(target):
            link_created.set()
            assert reader_at_final_path_check.wait(timeout=10)
        return result

    def observe_final_lstat(path, *args, **kwargs):
        if getattr(reader_context, "active", False) and workspace_path_identity(path) == workspace_path_identity(target):
            count = getattr(reader_context, "target_lstat_count", 0) + 1
            reader_context.target_lstat_count = count
            # _read_stable_path checks the absolute chain, takes its initial
            # path stat, reads/fstats the file, then takes this final path stat.
            if count == 3:
                reader_at_final_path_check.set()
                assert writer_finished.wait(timeout=10)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(contracts.os, "link", pause_after_link)
    monkeypatch.setattr(Path, "lstat", observe_final_lstat)

    def write_state():
        try:
            store._atomic_write(target, raw, replace=False)
        finally:
            writer_finished.set()

    def read_state():
        reader_context.active = True
        assert link_created.wait(timeout=10)
        try:
            return store._read_stable_path(target)
        except ContractError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(write_state)
        reader = pool.submit(read_state)
        writer.result(timeout=10)
        observed = reader.result(timeout=10)

    assert target.read_bytes() == raw
    assert target.stat().st_nlink == 1
    if isinstance(observed, ContractError):
        assert observed.code == "source_changed_during_read"
    else:
        assert observed == raw
    assert store._read_stable_path(target) == raw


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EXDEV, errno.EIO])
def test_linux_non_capability_rename_errors_remain_unavailable_without_fallback(
    tmp_path, monkeypatch, error_number
):
    source = tmp_path / "source.tmp"
    destination = tmp_path / "destination.json"
    source.write_bytes(b"source bytes")
    calls, native_call = _force_linux_rename_error(monkeypatch, error_number)

    with pytest.raises(ContractError) as error:
        publish_no_clobber(source, destination)

    assert error.value.code == "environment_unavailable"
    assert isinstance(error.value.__cause__, OSError)
    assert error.value.__cause__.errno == error_number
    assert len(calls) == 1
    assert native_call.argtypes == (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    assert native_call.restype is ctypes.c_int
    assert calls[0] == (-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    assert source.read_bytes() == b"source bytes"
    assert not destination.exists()


@pytest.mark.parametrize("error_number", [errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP])
def test_linux_capability_errors_fall_back_to_real_link(tmp_path, monkeypatch, error_number):
    source = tmp_path / "source.tmp"
    destination = tmp_path / "destination.json"
    raw = b"complete fallback payload"
    source.write_bytes(raw)
    _force_linux_rename_error(monkeypatch, error_number)
    real_link = contracts.os.link
    linked = []

    def observe_real_link(source_path, destination_path, *, follow_symlinks=True):
        linked.append((source_path, destination_path, follow_symlinks))
        return real_link(source_path, destination_path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(contracts.os, "link", observe_real_link)

    assert publish_no_clobber(source, destination)

    assert destination.read_bytes() == raw
    assert not source.exists()
    assert destination.stat().st_nlink == 1
    assert linked == [(os.fsencode(source), os.fsencode(destination), False)]


def test_linux_missing_renameat2_symbol_falls_back_to_real_link(tmp_path, monkeypatch):
    source = tmp_path / "source.tmp"
    destination = tmp_path / "destination.json"
    source.write_bytes(b"source bytes")
    monkeypatch.setattr(contracts.sys, "platform", "linux")
    monkeypatch.setattr(contracts.ctypes, "CDLL", lambda name, *, use_errno: object())

    assert publish_no_clobber(source, destination)
    assert destination.read_bytes() == b"source bytes"
    assert not source.exists()
    assert destination.stat().st_nlink == 1


def test_linux_libc_load_error_does_not_fall_back(tmp_path, monkeypatch):
    source = tmp_path / "source.tmp"
    destination = tmp_path / "destination.json"
    source.write_bytes(b"source bytes")
    monkeypatch.setattr(contracts.sys, "platform", "linux")

    def fail_cdll(*_args, **_kwargs):
        raise OSError(errno.ENOENT, "injected libc load failure")

    monkeypatch.setattr(contracts.ctypes, "CDLL", fail_cdll)
    with pytest.raises(ContractError) as error:
        publish_no_clobber(source, destination)
    assert error.value.code == "environment_unavailable"
    assert source.read_bytes() == b"source bytes"
    assert not destination.exists()


def test_linux_link_error_other_than_exists_remains_failure(tmp_path, monkeypatch):
    source = tmp_path / "source.tmp"
    destination = tmp_path / "destination.json"
    source.write_bytes(b"source bytes")
    _force_linux_rename_error(monkeypatch, errno.EINVAL)

    def fail_link(*_args, **_kwargs):
        raise OSError(errno.EACCES, "injected link failure")

    monkeypatch.setattr(contracts.os, "link", fail_link)
    with pytest.raises(ContractError) as error:
        publish_no_clobber(source, destination)
    assert error.value.code == "environment_unavailable"
    assert error.value.__cause__.errno == errno.EACCES
    assert source.read_bytes() == b"source bytes"
    assert not destination.exists()


def test_linux_link_success_is_not_rolled_back_when_source_unlink_fails(tmp_path, monkeypatch):
    source = tmp_path / "source.tmp"
    destination = tmp_path / "destination.json"
    raw = b"already published payload"
    source.write_bytes(raw)
    _force_linux_rename_error(monkeypatch, errno.EINVAL)
    real_unlink = contracts.os.unlink

    def fail_source_unlink(path, *args, **kwargs):
        if workspace_path_identity(os.fsdecode(path)) == workspace_path_identity(source):
            raise OSError(errno.EACCES, "injected temp cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(contracts.os, "unlink", fail_source_unlink)
    with pytest.raises(ContractError) as error:
        publish_no_clobber(source, destination)
    assert error.value.code == "environment_unavailable"
    assert error.value.__cause__.errno == errno.EACCES
    assert destination.read_bytes() == raw
    assert source.read_bytes() == raw


def test_darwin_branch_uses_renamex_np_exclusive_flag_simulated(tmp_path, monkeypatch):
    source = tmp_path / "source.tmp"
    destination = tmp_path / "destination.json"
    source.write_bytes(b"source bytes")
    calls = []

    class NativeCall:
        argtypes = None
        restype = None

        def __call__(self, *arguments):
            calls.append(arguments)
            os.rename(os.fsdecode(arguments[0]), os.fsdecode(arguments[1]))
            return 0

    native_call = NativeCall()

    class FakeLibc:
        renamex_np = native_call

    monkeypatch.setattr(contracts.sys, "platform", "darwin")
    monkeypatch.setattr(contracts.ctypes, "CDLL", lambda name, *, use_errno: FakeLibc())

    assert publish_no_clobber(source, destination)

    assert calls == [(os.fsencode(source), os.fsencode(destination), 4)]
    assert native_call.argtypes == (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    assert native_call.restype is ctypes.c_int
    assert destination.read_bytes() == b"source bytes"
    assert not source.exists()


def test_windows_branch_uses_os_rename_no_replace_contract_simulated(tmp_path, monkeypatch):
    source = tmp_path / "source.tmp"
    destination = tmp_path / "destination.json"
    source.write_bytes(b"new bytes")
    real_rename = os.rename
    calls = []

    def no_replace_rename(source_path, destination_path):
        calls.append((source_path, destination_path))
        if os.path.lexists(destination_path):
            raise FileExistsError(errno.EEXIST, "destination exists", os.fsdecode(destination_path))
        real_rename(source_path, destination_path)

    monkeypatch.setattr(contracts.sys, "platform", "win32")
    monkeypatch.setattr(contracts.os, "rename", no_replace_rename)

    assert publish_no_clobber(source, destination)
    assert destination.read_bytes() == b"new bytes"
    assert not source.exists()

    contender = tmp_path / "contender.tmp"
    contender.write_bytes(b"contender bytes")
    assert not publish_no_clobber(contender, destination)
    assert destination.read_bytes() == b"new bytes"
    assert contender.read_bytes() == b"contender bytes"
    assert len(calls) == 2


def test_publish_no_clobber_rejects_embedded_nul_before_native_call(tmp_path, monkeypatch):
    source = tmp_path / "source.tmp"
    destination = tmp_path / "destination.json"
    source.write_bytes(b"source bytes")
    called = False

    def unexpected_cdll(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("native rename must not be called for an invalid path")

    monkeypatch.setattr(contracts.ctypes, "CDLL", unexpected_cdll)

    with pytest.raises(ValueError, match="embedded null byte"):
        publish_no_clobber(str(source) + "\0suffix", destination)

    assert not called
    assert source.read_bytes() == b"source bytes"
    assert not destination.exists()
