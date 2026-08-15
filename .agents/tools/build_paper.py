#!/usr/bin/env python3
"""Compile an OptAgent paper without exposing compiler scratch publicly."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


BUILD_DIRECTORIES = (".latexmk_auxdir", ".latexmk_outdir")
BUILD_SUFFIXES = (".aux", ".fls", ".fdb_latexmk", ".log", ".out", ".synctex.gz")
MAX_DIAGNOSTIC_CHARS = 16_000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-dir", required=True)
    parser.add_argument("--reporting-dir", required=True)
    parser.add_argument("--timeout-sec", type=int, default=180)
    return parser.parse_args()


def trim_diagnostics(value: str) -> str:
    if len(value) <= MAX_DIAGNOSTIC_CHARS:
        return value
    omitted = len(value) - MAX_DIAGNOSTIC_CHARS
    return value[:MAX_DIAGNOSTIC_CHARS] + f"\n[truncated {omitted} characters]\n"


def remove_generated_files(paper_dir: Path) -> list[str]:
    removed: list[str] = []
    for name in BUILD_DIRECTORIES:
        path = paper_dir / name
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(name)
    for path in paper_dir.iterdir() if paper_dir.is_dir() else []:
        if path.is_file() and any(path.name.endswith(suffix) for suffix in BUILD_SUFFIXES):
            path.unlink()
            removed.append(path.name)
    return removed


def write_log(path: Path, lines: list[str], diagnostics: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines) + "\n\n--- compiler output ---\n" + diagnostics
    if not content.endswith("\n"):
        content += "\n"
    fd, temporary = tempfile.mkstemp(prefix=".paper_build.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def copy_pdf(source: Path, destination: Path) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".main.", suffix=".pdf.tmp", dir=destination.parent)
    os.close(fd)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    args = parse_args()
    paper_dir = Path(args.case_dir) / "paper"
    reporting_dir = Path(args.reporting_dir)
    log_path = reporting_dir / "paper_build.log"
    timeout = max(1, min(args.timeout_sec, 600))
    compiler = None
    command: list[str] = []
    expected_pdf = paper_dir / ".latexmk_outdir" / "main.pdf"
    diagnostics = ""
    status = "failed"
    exit_code: int | None = None
    pdf_written = False
    error = None
    removed: list[str] = []
    stale_pdf_removed = False

    try:
        if not paper_dir.is_dir():
            error = "paper directory is missing"
        elif not (paper_dir / "main.tex").is_file():
            error = "paper/main.tex is missing"
        else:
            # A retained PDF after any source update can silently describe stale sources.
            stale_pdf_removed = (paper_dir / "main.pdf").exists()
            (paper_dir / "main.pdf").unlink(missing_ok=True)
            latexmk = shutil.which("latexmk")
            xelatex = shutil.which("xelatex")
            if latexmk:
                compiler = "latexmk"
                command = [
                    latexmk,
                    "-pdf",
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    "-outdir=.latexmk_outdir",
                    "main.tex",
                ]
            elif xelatex:
                compiler = "xelatex"
                command = [
                    xelatex,
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    "-output-directory=.latexmk_outdir",
                    "main.tex",
                ]
            else:
                status = "unavailable"
                diagnostics = "No supported LaTeX compiler was found on PATH."

            if command:
                completed = subprocess.run(
                    command,
                    cwd=paper_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                exit_code = completed.returncode
                diagnostics = completed.stdout or ""
                if completed.returncode == 0 and expected_pdf.is_file():
                    copy_pdf(expected_pdf, paper_dir / "main.pdf")
                    pdf_written = True
                    status = "success"
                elif completed.returncode == 0:
                    error = "compiler exited successfully but did not produce .latexmk_outdir/main.pdf"
                else:
                    error = f"compiler exited with status {completed.returncode}"
    except subprocess.TimeoutExpired as exc:
        diagnostics = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        error = f"compiler timed out after {timeout} seconds"
        status = "failed"
    except OSError as exc:
        error = f"compiler invocation failed: {exc}"
        status = "failed"
    except Exception as exc:  # Keep a repairable report build issue in-band for the reporter.
        error = f"paper build helper failed: {exc}"
        status = "failed"
    finally:
        try:
            removed = remove_generated_files(paper_dir)
        except OSError as exc:
            cleanup_error = f"cleanup failed: {exc}"
            error = f"{error}; {cleanup_error}" if error else cleanup_error
            status = "failed"

    diagnostics = trim_diagnostics(diagnostics)
    lines = [
        f"status={status}",
        f"compiler={compiler or 'none'}",
        f"command={json.dumps(command)}",
        f"exit_code={exit_code if exit_code is not None else 'not_run'}",
        f"timeout_sec={timeout}",
        f"pdf_written={'true' if pdf_written else 'false'}",
        f"stale_pdf_removed={'true' if stale_pdf_removed else 'false'}",
        f"removed_generated={json.dumps(removed)}",
    ]
    if error:
        lines.append(f"error={error}")
    try:
        write_log(log_path, lines, diagnostics)
    except OSError as exc:
        error = f"could not write paper_build.log: {exc}"
        status = "failed"

    print(
        json.dumps(
            {
                "status": status,
                "compiler": compiler,
                "exit_code": exit_code,
                "pdf_written": pdf_written,
                "error": error,
                "paper_build_log": str(log_path),
            },
            ensure_ascii=False,
        )
    )
    # Compilation is best effort. The caller must use status/pdf_written and never
    # claim a PDF unless both report success.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
