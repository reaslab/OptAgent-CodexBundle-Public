"""Fetch and normalize ReasLab optimization modeling templates.

The site exposes the template service through gRPC-Web.  This importer keeps
the source material and provenance explicit, while extracting compact search
signals so the local OptGraph can retrieve a matching template case directly.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import struct
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from google.protobuf import descriptor_pool, message_factory
from google.protobuf.descriptor_pb2 import FileDescriptorProto


ASSET_URL = "https://reaslab.io/assets/announcements-BmL7RNQt.js"
API_ROOT = "https://reaslab.io/apis/reaslab.v1.TemplatesService"
DETAIL_ROOT = "https://reaslab.io/modeling-templates"
SOURCE_ROOT = "https://reaslab.io"


def make_classes() -> dict[str, Any]:
    text = requests.get(ASSET_URL, timeout=30).text
    match = re.search(r'nc=h\("([^"]+)"', text)
    if not match:
        raise RuntimeError("could not locate ReasLab template protobuf descriptor")
    encoded = match.group(1) + "=" * ((-len(match.group(1))) % 4)
    descriptor = FileDescriptorProto.FromString(base64.b64decode(encoded))
    pool = descriptor_pool.DescriptorPool()
    pool.Add(descriptor)
    factory = message_factory.MessageFactory(pool)
    return {
        name: factory.GetPrototype(pool.FindMessageTypeByName(f"reaslab.v1.{name}"))
        for name in (
            "ListTemplatesRequest", "ListTemplatesResponse", "GetTemplateInfoRequest",
            "GetTemplateInfoResponse", "GetTemplateFileRequest", "GetTemplateFileResponse",
        )
    }


def call(classes: dict[str, Any], method: str, request: Any, response_name: str) -> Any:
    payload = request.SerializeToString()
    body = b"\x00" + struct.pack(">I", len(payload)) + payload
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            response = requests.post(
                f"{API_ROOT}/{method}",
                data=body,
                headers={"Content-Type": "application/grpc-web+proto", "X-Grpc-Web": "1"},
                timeout=60,
            )
            response.raise_for_status()
            if len(response.content) < 5:
                message = response.headers.get("grpc-message", "empty response")
                raise RuntimeError(f"{message} for {method}")
            length = struct.unpack(">I", response.content[1:5])[0]
            if length <= 0 or len(response.content) < 5 + length:
                raise RuntimeError(f"truncated response for {method}")
            return classes[response_name].FromString(response.content[5 : 5 + length])
        except Exception as exc:  # transient CDN/API throttling is common on bulk reads
            last_error = exc
            time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(str(last_error))


def clean_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def decode_content(content: bytes) -> str:
    for encoding in ("utf-8", "gb18030", "latin-1"):
        try:
            return clean_text(content.decode(encoding))
        except UnicodeDecodeError:
            continue
    return clean_text(content.decode("utf-8", errors="replace"))


def headings(text: str) -> dict[str, str]:
    matches = list(re.finditer(r"(?m)^#{1,4}\s+(.+?)\s*$", text))
    result: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        result[match.group(1).strip()] = text[match.end() : end].strip()
    return result


def lines_matching(text: str, patterns: tuple[str, ...], limit: int = 12) -> list[str]:
    found: list[str] = []
    for line in text.splitlines():
        compact = " ".join(line.strip().split())
        if compact and any(re.search(pattern, compact, re.I) for pattern in patterns):
            if compact not in found:
                found.append(compact)
        if len(found) >= limit:
            break
    return found


def infer_family(title: str, text: str) -> str:
    blob = f"{title} {text}".casefold()
    families = (
        ("balanced_binary_line_avoidance", ("tic-tac-toe", "tic tac toe", "井字棋")),
        ("vehicle_routing", ("vehicle routing", "vrp", "pickup and delivery", "dial-a-ride", "routing")),
        ("scheduling", ("scheduling", "job shop", "flow shop", "lot-sizing", "lot sizing", "sequencing")),
        ("facility_location", ("facility location", "p-median", "dispersion", "location")),
        ("network_flow", ("network flow", "network design", "shortest path", "minimum cost flow", "multi-commodity")),
        ("assignment", ("assignment", "resource allocation", "team formation")),
        ("packing_cutting", ("bin packing", "cutting stock", "shelf packing", "knapsack", "blending")),
        ("production_planning", ("production planning", "factory", "refinery", "manufacturing", "inventory")),
        ("finance_revenue", ("portfolio", "revenue", "pricing", "market share")),
        ("power_energy", ("unit commitment", "power generation", "wind farm", "power grid")),
        ("graph_partitioning", ("max-cut", "max cut", "social golfer", "quadratic assignment")),
        ("continuous_nonlinear", ("branin", "hosaki", "curve fitting", "smallest circle", "beam")),
    )
    for family, signals in families:
        if any(signal in blob for signal in signals):
            return family
    return "mixed_integer_optimization"


def choose_files(files: list[Any]) -> tuple[Any | None, Any | None]:
    markdown = next((item for item in files if item.path.casefold().endswith((".md", ".markdown"))), None)
    code = next(
        (
            item for item in files
            if item.path.casefold().endswith((".py", ".lp", ".mps", ".mod", ".jl", ".r", ".cpp", ".java"))
        ),
        None,
    )
    return markdown, code


def fetch_template(classes: dict[str, Any], template: Any) -> dict[str, Any]:
    info_request = classes["GetTemplateInfoRequest"](
        template_id=template.id,
        template_type="modeling",
        force_refresh=False,
    )
    info = call(classes, "GetTemplateInfo", info_request, "GetTemplateInfoResponse")
    markdown, code = choose_files(list(info.files))

    def fetch_file(file_info: Any | None) -> str:
        if file_info is None:
            return ""
        request = classes["GetTemplateFileRequest"](
            template_id=template.id,
            template_type="modeling",
            filename=file_info.path,
        )
        result = call(classes, "GetTemplateFile", request, "GetTemplateFileResponse")
        return decode_content(bytes(result.content))

    readme = fetch_file(markdown)
    code_text = fetch_file(code)
    source_url = f"{SOURCE_ROOT}{template.url}" if template.url.startswith("/") else template.url
    detail_url = f"{DETAIL_ROOT}/{quote(template.id, safe='-_.~')}"
    source_files = [
        {
            "name": file_info.name,
            "path": file_info.path,
            "type": file_info.type,
            "size": file_info.size,
            "url": f"{API_ROOT}/GetTemplateFile?template_id={quote(template.id)}&filename={quote(file_info.path)}",
        }
        for file_info in info.files
    ]
    text = f"{info.title}\n{info.desc}\n{readme}"
    family = infer_family(info.title, text)
    objective = lines_matching(readme, (r"objective", r"目标", r"minimi", r"maximi", r"min ", r"max "), 10)
    variables = lines_matching(readme, (r"decision variable", r"变量", r"binary", r"integer", r"continuous", r"\bx[_a-z]*\s*\{"), 14)
    constraints = lines_matching(readme, (r"constraint", r"约束", r"subject to", r"indicator", r"flow conservation", r"<=", r">="), 16)
    data_contract = lines_matching(readme, (r"data", r"数据", r"parameter", r"参数", r"instance", r"输入", r"csv", r"benchmark"), 12)
    solver_hits = sorted(set(re.findall(r"\b(?:Gurobi|OR-Tools|CP-SAT|PuLP|Pyomo|COPT|MOSEK|SCIP|MIP|MILP|MIQP)\b", code_text + "\n" + readme, re.I)))
    recipe = lines_matching(code_text, (r"model", r"addVar", r"addConstr", r"addGenConstr", r"setObjective", r"solve", r"optimi", r"constraint", r"route", r"line"), 18)
    title_core = re.sub(r"\s+problem\s*", " ", info.title, flags=re.I).strip()
    title_core = re.sub(r"\s*\([^)]*\)", "", title_core).strip()
    aliases = [info.title, title_core, re.sub(r"\s+problem\s*", " ", info.title, flags=re.I).strip(), template.id, template.title]
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9-]*", title_core)
    aliases.extend(" ".join(words[:count]) for count in (2, 3) if len(words) >= count)
    aliases.extend(re.findall(r"[A-Z]{2,}[A-Z0-9-]*", info.title))
    aliases = list(dict.fromkeys(alias for alias in aliases if alias))
    summary = info.desc.strip() or f"ReasLab source-verified modeling template for {info.title}."
    payload = {
        "status": "detail_verified",
        "source": {
            "organization": "ReasLab",
            "detail_url": detail_url,
            "template_id": template.id,
            "template_version": info.version or template.version,
            "template_api_url": API_ROOT,
            "readme_url": source_url,
            "retrieved_at": "2026-09-06",
        },
        "problem_semantics": summary,
        "model_family": family,
        "data_contract": data_contract,
        "sets_and_parameters": lines_matching(readme, (r"^[*-]\\s*\$?[A-Z][A-Za-z0-9_]*", r"集合", r"set", r"parameter", r"参数"), 18),
        "decision_variables": variables,
        "objective_pattern": objective,
        "constraint_patterns": constraints,
        "solution_recipe": recipe,
        "solver_signals": solver_hits,
        "files": source_files,
        "readme_excerpt": readme[:5000],
        "code_excerpt": code_text[:4000],
        "reproduction": {"status": "source_verified_not_reproduced", "local_solver_run": False},
    }
    return {
        "id": "case.reaslab." + re.sub(r"[^a-z0-9]+", "_", template.id.casefold()).strip("_"),
        "type": "case",
        "title": info.title,
        "summary": summary,
        "retrieval": {"aliases": aliases, "signals": aliases + [family] + solver_hits, "feature_tags": [family]},
        "payload": payload,
        "template_id": template.id,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-workers", type=int, default=4)
    args = parser.parse_args()
    classes = make_classes()
    listing = call(
        classes,
        "ListTemplates",
        classes["ListTemplatesRequest"](template_type="modeling", page=1, limit=200, force_refresh=False),
        "ListTemplatesResponse",
    )
    templates = list(listing.templates)
    cases: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as executor:
        futures = {executor.submit(fetch_template, classes, template): template for template in templates}
        for future in as_completed(futures):
            template = futures[future]
            try:
                cases.append(future.result())
            except Exception as exc:  # keep one bad source from losing the catalog
                errors.append({"template_id": template.id, "error": str(exc)})
    cases.sort(key=lambda item: item["id"])
    nodes = []
    edges = []
    for case in cases:
        template_id = case.pop("template_id")
        nodes.append(case)
        edges.append({"source": case["id"], "relation": "SOURCED_FROM", "target": f"source.reaslab.{re.sub(r'[^a-z0-9]+', '_', template_id.casefold()).strip('_')}"})
    sources = [
        {
            "id": "source.reaslab." + re.sub(r"[^a-z0-9]+", "_", case["payload"]["source"]["template_id"].casefold()).strip("_"),
            "type": "source_section",
            "title": case["title"] + " source",
            "summary": "ReasLab template API source for a detail-verified modeling case.",
            "payload": {"source_file": case["payload"]["source"]["readme_url"], "detail_url": case["payload"]["source"]["detail_url"], "status": "detail_verified"},
        }
        for case in nodes
    ]
    nodes.extend(sources)
    output = {
        "schema_version": "1.0",
        "graph_id": "reaslab-optimization-detail-cases",
        "graph_version": "2026-09-06.1",
        "domain": "optimization",
        "source_manifest": {
            "source": DETAIL_ROOT,
            "api": API_ROOT,
            "observed_at": "2026-09-06",
            "status": "detail_verified",
            "template_count": len(templates),
            "case_count": len(cases),
            "errors": errors,
        },
        "nodes": nodes,
        "edges": edges,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"templates": len(templates), "cases": len(cases), "errors": errors}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
