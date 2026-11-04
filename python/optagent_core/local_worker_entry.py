"""Installed-package entry point for a frozen local Core invocation."""

from __future__ import annotations

import sys

from .contracts import ContractError, canonical_json_bytes


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) != 3:
        sys.stderr.write("usage: optagent-local-worker WORKSPACE INVOCATION_PATH SHA256\n")
        return 2
    if sys.stdin.buffer.read(1) != b"\x01":
        sys.stderr.write("local worker start was not authorized by its parent process\n")
        return 2
    workspace, invocation_path, expected_sha256 = values
    try:
        from .worker import run_invocation

        result = run_invocation(workspace, invocation_path, expected_sha256)
    except ContractError as exc:
        sys.stderr.write(f"local worker failed: {exc.code}\n")
        return 1
    except Exception as exc:
        sys.stderr.write(f"local worker failed: {type(exc).__name__}\n")
        return 1
    sys.stdout.buffer.write(canonical_json_bytes(result) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
