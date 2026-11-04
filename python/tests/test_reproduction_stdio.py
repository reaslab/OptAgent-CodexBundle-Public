from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


_BOOTSTRAP = Path(__file__).resolve().parents[1] / "optagent_reproduce.py"


_ISOLATED_DRIVER = r'''import importlib, os, sys, tempfile, types
from pathlib import Path

assert sys.flags.isolated
bootstrap = Path(sys.argv[1]).resolve()
mode = sys.argv[2]
sys.path.insert(0, str(bootstrap.parent))
import optagent_reproduce as reproduction

native_names = ("numpy", "pyomo", "highspy")
assert not any(name in sys.modules for name in native_names)
assert not any(name == "optagent_core" or name.startswith("optagent_core.") for name in sys.modules)
with tempfile.TemporaryDirectory(prefix="reproduction-stdio-") as temporary:
    root = Path(temporary)
    output = root / "output"
    digest = "a" * 64
    runtime_root = root / "reproduce" / "runtime" / digest
    package_root = runtime_root / "python" / "optagent_core"
    package_root.mkdir(parents=True)
    expected_paths = {
        "optagent_core": package_root / "__init__.py",
        "optagent_core.contracts": package_root / "contracts.py",
        "optagent_core.data": package_root / "data.py",
        "optagent_core.models": package_root / "models.py",
        "optagent_core.results": package_root / "results.py",
        "optagent_core.solver_protocol": package_root / "solver_protocol.py",
        "optagent_core.worker": package_root / "worker.py",
    }
    for path in expected_paths.values():
        path.write_text("# stdlib lifecycle fixture\n", encoding="utf-8")

    manifest = {"ref_map": []}
    entries = [{"prepared_manifest_ref": {"path": "prepared.json"}}]
    reproduction._validate_package = lambda package: (manifest, entries, object())
    reproduction._output_path = lambda value, package, must_exist: output
    reproduction._ref = lambda value: value
    reproduction._read_ref = lambda reader, ref: b"prepared"
    reproduction._json = lambda raw: {"schema_version": 3, "runtime_bundle": {"digest": digest}}

    worker_called = []
    def run_local_reproduction(reader, prepared_ref, output_path):
        worker_called.append(True)
        assert sys.stdout is not sys.stderr
        assert sys.stdout.fileno() != sys.stderr.fileno()
        assert sys.stdout.encoding == sys.stderr.encoding
        assert sys.stdout.errors == sys.stderr.errors
        diagnostic = "worker diagnostic 中文 Ω\n" if mode == "success" else "worker diagnostic line\n"
        sys.stdout.write(diagnostic)
        sys.stdout.flush()
        sys.stderr.write("worker wrote through a distinct stdout descriptor\n")
        sys.stderr.flush()
        if mode == "worker_raise":
            raise RuntimeError("injected worker failure")
        return {"report": {"status": "completed"}, "report_ref": {"path": "report.json"}}

    fake_modules = {}
    def fake_import_module(name):
        if not fake_modules:
            for module_name, module_path in expected_paths.items():
                module = types.ModuleType(module_name)
                module.__file__ = str(module_path)
                fake_modules[module_name] = module
                sys.modules[module_name] = module
            fake_modules["optagent_core.data"].PortableReader = lambda package, refs: (package, refs)
            fake_modules["optagent_core.worker"].run_local_reproduction = run_local_reproduction
        return fake_modules[name]
    reproduction.importlib.import_module = fake_import_module

    original_stdout = sys.stdout
    original_stderr = sys.stderr
    original_dup = os.dup
    controlled_stderr = None
    active_stderr = original_stderr
    if mode == "success":
        controlled_stderr_fd = original_dup(original_stderr.fileno())
        controlled_stderr = os.fdopen(
            controlled_stderr_fd,
            "w",
            buffering=1,
            encoding="ascii",
            errors="backslashreplace",
        )
        sys.stderr = controlled_stderr
        active_stderr = controlled_stderr
    duplicate_fds = []
    def tracking_dup(fd):
        duplicate = original_dup(fd)
        duplicate_fds.append(duplicate)
        return duplicate
    os.dup = tracking_dup
    fdopen_seen = []
    original_fdopen = os.fdopen
    if mode == "fdopen_raise":
        def failing_fdopen(fd, *args, **kwargs):
            fdopen_seen.append(fd)
            raise OSError("injected fdopen construction failure")
        os.fdopen = failing_fdopen

    try:
        try:
            if mode == "success":
                assert reproduction._child(root, 0, str(output)) == 0
            elif mode == "worker_raise":
                try:
                    reproduction._child(root, 0, str(output))
                except RuntimeError as exc:
                    assert str(exc) == "injected worker failure"
                else:
                    raise AssertionError("worker exception was swallowed")
            elif mode == "fdopen_raise":
                try:
                    reproduction._child(root, 0, str(output))
                except OSError as exc:
                    assert str(exc) == "injected fdopen construction failure"
                else:
                    raise AssertionError("fdopen exception was swallowed")
            else:
                raise AssertionError("unknown lifecycle case")
        finally:
            os.fdopen = original_fdopen
            os.dup = original_dup

        assert sys.stdout is original_stdout
        assert sys.stderr is active_stderr
        os.fstat(original_stdout.fileno())
        os.fstat(original_stderr.fileno())
        os.fstat(active_stderr.fileno())
        assert len(duplicate_fds) == 1
        duplicate_fd = duplicate_fds[0]
        try:
            os.fstat(duplicate_fd)
        except OSError:
            pass
        else:
            raise AssertionError("duplicated stderr descriptor was not closed")

        if mode == "success":
            assert worker_called == [True]
            assert not fdopen_seen
        elif mode == "worker_raise":
            assert worker_called == [True]
            assert not fdopen_seen
        else:
            assert worker_called == []
            assert fdopen_seen == [duplicate_fd]
        assert not any(name in sys.modules for name in native_names)
        sys.stderr.write("stdio lifecycle verified\n")
        sys.stderr.flush()
    finally:
        if controlled_stderr is not None:
            sys.stderr = original_stderr
            controlled_stderr.close()
'''


class ReproductionStdioLifecycleTests(unittest.TestCase):
    def run_case(self, mode: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", "-c", _ISOLATED_DRIVER, str(_BOOTSTRAP), mode],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )

    def test_success_restores_streams_preserves_json_and_closes_only_duplicate(self) -> None:
        completed = self.run_case("success")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.count("\n"), 1)
        self.assertEqual(json.loads(completed.stdout), {"report": {"status": "completed"}, "report_ref": {"path": "report.json"}})
        self.assertIn(r"worker diagnostic \u4e2d\u6587 \u03a9", completed.stderr)
        self.assertIn("worker wrote through a distinct stdout descriptor", completed.stderr)
        self.assertIn("stdio lifecycle verified", completed.stderr)

    def test_worker_exception_restores_streams_and_closes_duplicate(self) -> None:
        completed = self.run_case("worker_raise")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertIn("worker diagnostic line", completed.stderr)
        self.assertIn("stdio lifecycle verified", completed.stderr)

    def test_fdopen_failure_closes_duplicate_without_calling_worker(self) -> None:
        completed = self.run_case("fdopen_raise")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertNotIn("worker diagnostic", completed.stderr)
        self.assertIn("stdio lifecycle verified", completed.stderr)


if __name__ == "__main__":
    unittest.main()
