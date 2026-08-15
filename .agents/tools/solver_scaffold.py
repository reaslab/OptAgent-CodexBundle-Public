#!/usr/bin/env python3
"""Create a checked public solver-entrypoint scaffold for a supported backend.

The tool copies static OptAgent boilerplate; it never selects a backend, reads
case data, or overwrites an existing implementation unless --force is explicit.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path


TEMPLATES = {
    "gurobipy": "template_gurobipy.py",
    "pulp": "template_pulp.py",
    "ortools-linear": "template_ortools_linear.py",
    "ortools-cp-sat": "template_ortools_cpsat.py",
    "cvxpy": "template_cvxpy.py",
    "pyomo-cbc": "template_pyomo.py",
    "pyomo-ipopt": "template_pyomo.py",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", required=True, choices=sorted(TEMPLATES))
    parser.add_argument("--case-dir", required=True, help="Public case directory under cases/.")
    parser.add_argument("--force", action="store_true", help="Replace an existing run.py.")
    return parser.parse_args()


def case_target(case_dir: str) -> Path:
    path = Path(case_dir)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "cases":
        raise ValueError("--case-dir must be a relative directory under cases/")
    return path / "code" / "run.py"


def resolve_template(backend: str) -> Path:
    """Locate the repository-local template for ``backend``."""
    template = TEMPLATES[backend]
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / "solver-templates" / template,
        here.parents[1] / "assets" / "solver-templates" / template,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def rendered_template(source: Path, backend: str) -> str:
    content = source.read_text(encoding="utf-8")
    if backend != "pyomo-ipopt":
        return content
    default = 'DEFAULT_PYOMO_SOLVER = "cbc"'
    if default not in content:
        raise RuntimeError("Pyomo template is missing its default-solver marker")
    return content.replace(default, 'DEFAULT_PYOMO_SOLVER = "ipopt"', 1)


def atomic_copy(source: Path, target: Path, backend: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(rendered_template(source, backend), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    try:
        target = case_target(args.case_dir)
    except ValueError as error:
        print(json.dumps({"status": "error", "error": str(error)}))
        return 2

    source = resolve_template(args.backend)
    if not source.is_file():
        print(
            json.dumps(
                {
                    "status": "error",
                    "backend": args.backend,
                    "error": f"bundled template is missing: {source}",
                }
            )
        )
        return 2

    if target.exists() and not args.force:
        print(
            json.dumps(
                {
                    "status": "preserved",
                    "backend": args.backend,
                    "template": source.name,
                    "target": str(target),
                    "message": "Existing run.py was not replaced; patch it in place for rework.",
                }
            )
        )
        return 0

    atomic_copy(source, target, args.backend)
    print(
        json.dumps(
            {
                "status": "created",
                "backend": args.backend,
                "template": source.name,
                "target": str(target),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
