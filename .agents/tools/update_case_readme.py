#!/usr/bin/env python3
"""Generate the public case README from stable public and internal evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path


LEGACY_REPORTS = {"formulation.md", "summary.md", "technical_report.md"}
BUILD_SUFFIXES = {".aux", ".fls", ".log", ".out", ".synctex.gz", ".fdb_latexmk"}
BUILD_DIRECTORIES = {".latexmk_auxdir", ".latexmk_outdir"}
INTERNAL_PATH = re.compile(r"(?:[A-Za-z]:)?[^\s`]*\.optagent[/\\][^\s`]*")
INTERNAL_RUNTIME_DETAIL = re.compile(
    r"(?i)internal[- ]runtime|internal[- ]runner|minimal runner|launcher|wrapper|"
    r"internal (?:workflow|execution) records|hidden workspace path|"
    r"approval_status|ask_user_for_clarification|next_action|"
    r"(?:auxiliary|portability|public[- ]entrypoint)[- ]probes?|"
    r"verification (?:setting|environment)"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate cases/{case_id}/README.md.")
    parser.add_argument("--case-dir", required=True)
    parser.add_argument("--internal-case-dir", required=True)
    parser.add_argument("--title")
    return parser.parse_args()


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def public_files(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(
        path.relative_to(root.parent).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and path.name not in LEGACY_REPORTS
        and not path.name.startswith(".")
        and not any(part in BUILD_DIRECTORIES for part in path.relative_to(root).parts)
        and not any(path.name.endswith(suffix) for suffix in BUILD_SUFFIXES)
    )


def active_decisions(entries: object) -> list[dict]:
    if not isinstance(entries, list):
        return []
    valid = [item for item in entries if isinstance(item, dict)]
    superseded = {
        item["supersedes"] for item in valid
        if item.get("status") == "active" and isinstance(item.get("supersedes"), str)
    }
    return [
        item for item in valid
        if item.get("status") == "active"
        and item.get("id") not in superseded
        and (item.get("impact") or {}).get("user_visible") is True
    ]


def public_text(value: object) -> str:
    text = str(value)
    return INTERNAL_PATH.sub("[internal evidence]", text).replace(".optagent", "[internal evidence]")


def public_limitation(value: object) -> str | None:
    raw = str(value)
    if INTERNAL_PATH.search(raw) or ".optagent" in raw:
        return None
    text = public_text(raw).strip()
    if not text or INTERNAL_RUNTIME_DETAIL.search(text):
        return None
    return text


def render_readme(case_dir: Path, internal: Path, title: str | None = None) -> str:
    case_dir.mkdir(parents=True, exist_ok=True)

    state = load_json(internal / "state.json")
    solution = load_json(internal / "solver" / "solution.json")
    diagnosis = load_json(internal / "verification" / "diagnosis.json")
    decision_log = load_json(internal / "decision_log.json")
    decisions = active_decisions(decision_log.get("entries"))

    title = title or case_dir.name
    case_type = state.get("type", "optimization")
    report_contract = state.get("report_contract", "latex")
    solver_status = solution.get("status", "not available")
    objective = solution.get("objective_value", solution.get("objective", "not available"))
    if objective is None:
        objective = "not applicable"
    verification = diagnosis.get("validation", {}).get(
        "status", diagnosis.get("failure_type", "not available")
    )

    lines = [
        f"# {title}", "",
        "## Overview", "",
        f"This directory contains the public artifacts for this {case_type} case.", "",
        "## Status", "",
        f"- Case type: `{public_text(case_type)}`",
    ]
    if case_type != "analysis":
        lines.extend([
            f"- Solver status: `{public_text(solver_status)}`",
            f"- Objective value: `{public_text(objective)}`",
            f"- Verification: `{public_text(verification)}`",
        ])
    narrative_structure = (
        "- `report/final.md`: final Markdown report."
        if report_contract == "markdown"
        else "- `paper/`: final paper source and `main.pdf` when compilation succeeds."
    )
    lines.extend([
        "", "## Structure", "",
        "- `Problem.md`: problem statement when present.",
        "- `code/`: executable solver, public data snapshot, model exports, and run summaries when present.",
        "- `outputs/`: result tables, figures, exported data, and case-specific attachments.",
        narrative_structure, "",
        "## Main Artifacts", "",
    ])
    narrative_dir = case_dir / ("report" if report_contract == "markdown" else "paper")
    files = public_files(case_dir / "outputs") + public_files(narrative_dir)
    lines.extend([f"- `{item}`" for item in files] or ["- No final public artifacts are available yet."])

    if (case_dir / "code" / "README.md").is_file():
        lines.extend(["", "## Reproduce", "", "See `code/README.md` for dependencies and commands."])
    if decisions:
        lines.extend(["", "## Assumptions and Automated Decisions", ""])
        for item in decisions:
            decision = public_text(item.get("decision", "Unspecified decision"))
            rationale = public_text(item.get("rationale", "no rationale recorded"))
            risk = public_text((item.get("impact") or {}).get("risk", ""))
            suffix = f" Risk: {risk}" if risk else ""
            lines.append(f"- {decision} ({rationale}).{suffix}")

    validation_status = (diagnosis.get("validation") or {}).get("status")
    limitations = diagnosis.get("notes", []) if validation_status in {"partial", "failed"} else []
    if isinstance(limitations, list):
        visible = [
            text
            for item in limitations
            if isinstance(item, str)
            for text in [public_limitation(item)]
            if text
        ]
        if visible:
            lines.extend(["", "## Known Limitations", ""])
            lines.extend(f"- {item}" for item in visible)

    content = "\n".join(lines).rstrip() + "\n"
    if ".optagent" in content:
        raise ValueError("Generated README contains an internal path")
    return content


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
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


def write_readme(case_dir: Path, internal: Path, title: str | None = None) -> None:
    atomic_write_text(case_dir / "README.md", render_readme(case_dir, internal, title))


def main() -> int:
    args = parse_args()
    write_readme(Path(args.case_dir), Path(args.internal_case_dir), args.title)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
