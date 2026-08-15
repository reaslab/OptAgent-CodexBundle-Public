#!/usr/bin/env python3
"""Initialize an OptAgent LaTeX paper workspace from a bundled template."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize an OptAgent LaTeX paper directory.")
    parser.add_argument("--lang", required=True, choices=["cn", "en"], help="Template language to copy.")
    parser.add_argument("--output", required=True, help="Destination paper directory.")
    parser.add_argument("--force", action="store_true", help="Replace an existing output directory.")
    return parser.parse_args()


def is_dangerous_delete_target(path: Path) -> bool:
    resolved = path.resolve()
    anchors = {Path(resolved.anchor).resolve()} if resolved.anchor else set()
    protected = {
        Path.cwd().resolve(),
        Path.home().resolve(),
        Path(__file__).resolve().parent,
        Path(__file__).resolve().parent.parent,
    }
    return resolved in anchors or resolved in protected or resolved.parent == resolved


def ensure_output_ready(output: Path, force: bool) -> None:
    if output.exists() and not output.is_dir():
        raise RuntimeError(f"Output exists and is not a directory: {output}")

    if not output.exists():
        output.mkdir(parents=True, exist_ok=True)
        return

    if any(output.iterdir()):
        if not force:
            raise RuntimeError(f"Output directory is non-empty; use --force to replace it: {output}")
        if is_dangerous_delete_target(output):
            raise RuntimeError(f"Refusing to delete unsafe output directory: {output}")
        shutil.rmtree(output)
        output.mkdir(parents=True, exist_ok=True)


def copy_template(template: Path, output: Path) -> None:
    if not template.is_dir():
        raise RuntimeError(f"Template directory not found: {template}")

    for item in template.iterdir():
        target = output / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def main() -> int:
    args = parse_args()
    skill_dir = Path(__file__).resolve().parent
    template = skill_dir / f"template_{args.lang}"
    output = Path(args.output).expanduser()

    try:
        ensure_output_ready(output, args.force)
        copy_template(template, output)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"Initialized {args.lang} paper template at {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
