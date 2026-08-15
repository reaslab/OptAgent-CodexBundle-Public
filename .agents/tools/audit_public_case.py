#!/usr/bin/env python3
"""Validate OptAgent public case artifacts before workflow completion."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


SECTIONS = {
    "abstract.tex", "introduction.tex", "model.tex",
    "algorithm.tex", "experiments.tex", "conclusion.tex",
}
LEGACY_REPORTS = {"formulation.md", "summary.md", "technical_report.md"}
PAPER_BUILD_DIRECTORIES = {".latexmk_auxdir", ".latexmk_outdir"}
PAPER_BUILD_SUFFIXES = {".aux", ".fls", ".log", ".out", ".synctex.gz", ".fdb_latexmk"}
RUNTIME_DETAIL = re.compile(
    r"(?i)\.optagent|internal[- ]runtime|internal execution|launcher|"
    r"wrapper (?:rejected|failed|unavailable)|internal[- ]runner|minimal runner|"
    r"internal (?:workflow|execution) records|subagent|agent process|"
    r"approval_status|ask_user_for_clarification|next_action|"
    r"(?:auxiliary|portability|public[- ]entrypoint)[- ]probes?|"
    r"verification (?:setting|environment)"
)
TEMPLATE_TEXT = re.compile(
    r"(?i)TODO|TBD|Include the following content|Assumption content|"
    r"Example parameter|Describe the data structure|Summarize the modeling route|"
    r"Model Reliability \(Optional\)|Sensitivity Analysis \(Optional\)|"
    r"包含以下内容|假设内容|示例参数|描述数据结构|概述建模路线|"
    r"模型可靠性（可选）|敏感性分析（可选）|对比试验（可选）"
)
TEMPLATE_COMMENT = re.compile(r"(?mi)^\s*%\s*(?:replace with|替换为)")
EXCESS_PRECISION = re.compile(r"\b\d+\.\d{9,}\b")
REPORT_CONTRACTS = {"latex", "markdown"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-dir", required=True)
    parser.add_argument(
        "--report-contract",
        choices=sorted(REPORT_CONTRACTS),
        default="latex",
        help="Required final narrative format. Defaults to the standard LaTeX contract.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable result and keep recoverable audit failures in-band.",
    )
    return parser.parse_args()


def report_text_errors(text: str, label: str) -> list[str]:
    errors: list[str] = []
    if len(text.strip()) < 120:
        errors.append(f"{label} is not meaningfully populated")
    if RUNTIME_DETAIL.search(text):
        errors.append(f"{label} exposes internal runtime details")
    if TEMPLATE_TEXT.search(text) or TEMPLATE_COMMENT.search(text):
        errors.append(f"{label} contains template instructions or placeholders")
    if EXCESS_PRECISION.search(text):
        errors.append(f"{label} contains a decimal with excessive precision")
    return errors


def audit(case_dir: Path, report_contract: str = "latex") -> list[str]:
    if report_contract not in REPORT_CONTRACTS:
        raise ValueError(f"unsupported report contract: {report_contract}")
    errors: list[str] = []
    readme = case_dir / "README.md"
    if not readme.is_file():
        errors.append("README.md is missing")
    else:
        text = readme.read_text(encoding="utf-8", errors="replace")
        if RUNTIME_DETAIL.search(text):
            errors.append("README.md exposes internal runtime details")

    code_readme = case_dir / "code" / "README.md"
    if code_readme.is_file():
        text = code_readme.read_text(encoding="utf-8", errors="replace")
        if RUNTIME_DETAIL.search(text):
            errors.append("code/README.md exposes internal runtime details")

    paper = case_dir / "paper"
    if report_contract == "latex":
        main = paper / "main.tex"
        if not main.is_file():
            errors.append("paper/main.tex is missing")
        combined = ""
        for section in sorted(SECTIONS):
            path = paper / "sections" / section
            if not path.is_file():
                errors.append(f"paper/sections/{section} is missing")
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            combined += "\n" + text
            if len(text.strip()) < 120:
                errors.append(f"paper/sections/{section} is not meaningfully populated")
        if RUNTIME_DETAIL.search(combined):
            errors.append("paper exposes internal runtime details")
        if TEMPLATE_TEXT.search(combined) or TEMPLATE_COMMENT.search(combined):
            errors.append("paper contains template instructions or placeholders")
        if EXCESS_PRECISION.search(combined):
            errors.append("paper contains a decimal with excessive precision")
    else:
        final_markdown = case_dir / "report" / "final.md"
        if not final_markdown.is_file():
            errors.append("report/final.md is missing")
        else:
            errors.extend(
                report_text_errors(
                    final_markdown.read_text(encoding="utf-8", errors="replace"),
                    "report/final.md",
                )
            )
    if paper.is_dir():
        for path in paper.rglob("*"):
            relative = path.relative_to(paper)
            if path.is_dir() and path.name in PAPER_BUILD_DIRECTORIES:
                errors.append(f"paper build directory remains: {relative.as_posix()}")
            elif path.is_file() and any(
                path.name.endswith(suffix) for suffix in PAPER_BUILD_SUFFIXES
            ):
                errors.append(f"paper build artifact remains: {relative.as_posix()}")

    outputs = case_dir / "outputs"
    if outputs.is_dir():
        for path in outputs.rglob("*"):
            if path.is_file() and path.name in LEGACY_REPORTS:
                errors.append(f"legacy report remains under outputs/: {path.name}")
            if path.is_file() and path.suffix.lower() == ".md":
                errors.append(f"narrative Markdown remains under outputs/: {path.name}")
    for path in case_dir.rglob("*"):
        if path.is_file() and (path.suffix.lower() in {".pyc", ".pyo"} or "__pycache__" in path.parts):
            errors.append(f"public Python cache artifact remains: {path.relative_to(case_dir)}")
    return errors


def main() -> int:
    args = parse_args()
    errors = audit(Path(args.case_dir), args.report_contract)
    if args.json:
        print(
            json.dumps(
                {
                    "passed": not errors,
                    "report_contract": args.report_contract,
                    "errors": errors,
                },
                ensure_ascii=False,
            )
        )
        # A reporter must inspect passed before returning. Keeping recoverable public
        # quality findings in-band avoids turning a repair loop into a failed tool call.
        return 0
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"public case audit passed ({args.report_contract})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
