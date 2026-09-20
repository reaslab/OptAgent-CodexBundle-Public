from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
REFERENCE = ROOT / ".agents" / "skills" / "opt-formulation" / "reference"
OUTPUT = ROOT / ".agents" / "skills" / "opt-formulation" / "knowledge" / "legacy_reference_optgraph.json"


def slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    return value or "unnamed"


def sections(text: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"(?m)^##\s+(?:\d+\.\s+)?(.+?)\s*$", text))
    return [(match.group(1).strip(), text[match.end():matches[i + 1].start() if i + 1 < len(matches) else len(text)].strip()) for i, match in enumerate(matches)]


def summary(body: str, limit: int = 360) -> str:
    lines = [re.sub(r"[*`$]", "", line).strip() for line in body.splitlines()]
    lines = [line for line in lines if line and not line.startswith("|") and not line.startswith("---")]
    return (lines[0] if lines else "Converted reference knowledge node")[:limit]


def source_payload(path: Path, heading: str, body: str) -> dict[str, str]:
    return {
        "source_file": path.relative_to(ROOT).as_posix(),
        "source_heading": heading,
        "source_excerpt": body[:1800],
        "conversion": "deterministic Markdown section conversion; source remains authoritative",
    }


MODEL_RETRIEVAL: dict[str, tuple[list[str], list[str]]] = {
    # These are aliases and discriminating phrases from the original model
    # headings.  Generic parameter words (for example ``demand`` or
    # ``capacity``) are deliberately excluded because they occur in many
    # formulations and otherwise create retrieval ties.
    "set_covering_problem_scp": (
        ["SCP", "set covering", "set cover", "minimum cost coverage"],
        ["select sets", "cover every element", "cover each demand point", "binary sets", "at least one set"],
    ),
    "set_partitioning_problem_spp": (
        ["SPP", "set partitioning", "set partition"],
        ["exactly one set", "partition every element", "cover each element exactly once"],
    ),
    "bin_packing_problem_bpp": (
        ["BPP", "bin packing", "pack items into bins"],
        ["bin capacity", "minimize number of bins", "item weight", "assign each item to one bin"],
    ),
    "0_1_knapsack_problem": (
        ["0-1 knapsack", "binary knapsack", "knapsack"],
        ["maximize value", "weight capacity", "select items", "value and weight"],
    ),
    "linear_assignment_problem_lap": (
        ["LAP", "linear assignment", "assignment problem", "bipartite matching"],
        ["one agent per task", "one task per agent", "match agents to tasks", "assign each agent to one task"],
    ),
    "generalized_assignment_problem_gap": (
        ["GAP", "generalized assignment", "resource constrained assignment"],
        ["agent capacity", "resource consumption", "assign tasks to agents", "assign tasks to capacitated agents"],
    ),
    "uncapacitated_facility_location_uflp": (
        ["UFLP", "uncapacitated facility location", "fixed-charge location"],
        ["fixed opening cost", "open facilities", "serve every customer", "no capacity limit"],
    ),
    "traveling_salesman_problem_tsp_dfj_formulation": (
        ["TSP", "traveling salesman", "travelling salesman", "DFJ"],
        ["visit every city once", "single tour", "subtour elimination", "return to the depot"],
    ),
    "capacitated_vrp_cvrp": (
        ["CVRP", "capacitated vehicle routing", "vehicle routing problem"],
        ["vehicle capacity", "depot routes", "visit each customer", "return to depot"],
    ),
    "job_shop_scheduling_jssp": (
        ["JSSP", "job shop scheduling", "job-shop"],
        ["operation precedence", "sequence jobs on machines", "makespan", "machine order"],
    ),
    "flow_shop_scheduling_fssp_permutation": (
        ["FSSP", "flow shop scheduling", "permutation flow shop"],
        ["job permutation", "same order on every machine", "completion time on machines"],
    ),
    "max_flow": (
        ["maximum flow", "max flow", "network flow maximum"],
        ["source to sink", "arc capacity", "maximize flow"],
    ),
    "min_cost_flow": (
        ["minimum cost flow", "min-cost flow", "transshipment"],
        ["supply and demand balance", "flow conservation", "unit flow cost"],
    ),
    "transportation_problem": (
        ["transportation problem", "transportation model", "supply-demand transportation"],
        ["ship from supply nodes", "meet demand nodes", "unit transport cost"],
    ),
    "blending_diet_problem": (
        ["blending problem", "diet problem", "blending / diet"],
        ["blend ingredients", "nutrient bounds", "ingredient quantities", "minimum cost mixture"],
    ),
    "shortest_path_as_lp": (
        ["shortest path", "shortest-path LP", "single source shortest path"],
        ["source to destination", "minimum path cost", "path flow"],
    ),
    "portfolio_optimization_markowitz_mean_variance": (
        ["portfolio optimization", "Markowitz", "mean-variance portfolio", "portfolio"],
        ["minimize variance", "target return", "portfolio weights", "covariance matrix"],
    ),
}

# Semantic links are derived from the existing formulation headings and
# source sections.  They make the converted graph navigable without creating
# a second copy of the original knowledge.
MODEL_FAMILY_LINKS: dict[str, list[str]] = {
    "set_covering_problem_scp": ["family.reference.set_cover_set_packing_set_partitioning"],
    "set_partitioning_problem_spp": ["family.reference.set_cover_set_packing_set_partitioning"],
    "bin_packing_problem_bpp": ["family.reference.bin_packing_cutting_stock"],
    "0_1_knapsack_problem": ["family.reference.knapsack"],
    "linear_assignment_problem_lap": ["family.reference.assignment"],
    "generalized_assignment_problem_gap": ["family.reference.generalized_assignment_gap"],
    "uncapacitated_facility_location_uflp": ["family.reference.facility_location_uflp_cflp_p_median_p_center"],
    "traveling_salesman_problem_tsp_dfj_formulation": ["family.reference.tsp_vehicle_routing_cvrp_vrptw"],
    "capacitated_vrp_cvrp": ["family.reference.tsp_vehicle_routing_cvrp_vrptw"],
    "job_shop_scheduling_jssp": ["family.reference.scheduling_job_shop_flow_shop_single_parallel_crew_scheduling"],
    "flow_shop_scheduling_fssp_permutation": ["family.reference.scheduling_job_shop_flow_shop_single_parallel_crew_scheduling"],
    "max_flow": ["family.reference.min_cost_flow_max_flow_network_flow_transshipment_circulation_shortest_path"],
    "min_cost_flow": ["family.reference.min_cost_flow_max_flow_network_flow_transshipment_circulation_shortest_path"],
    "transportation_problem": ["family.reference.transportation"],
    "blending_diet_problem": ["family.reference.production_mix_blending_diet_linear_programming"],
    "shortest_path_as_lp": ["family.reference.min_cost_flow_max_flow_network_flow_transshipment_circulation_shortest_path"],
    "portfolio_optimization_markowitz_mean_variance": ["family.reference.portfolio_markowitz"],
}

MODEL_PATTERN_LINKS: dict[str, list[str]] = {
    "bin_packing_problem_bpp": ["knowledge.linearization.product_of_binary_and_continuous_z_y_cdot_x_y_binary_0_leq_x_leq_u"],
    "uncapacitated_facility_location_uflp": [
        "knowledge.linearization.fixed_charge_cost_f_if_x_0_plus_variable_cost_c_cdot_x",
        "knowledge.linearization.logical_implication_x_1_1_rightarrow_x_2_1",
    ],
    "traveling_salesman_problem_tsp_dfj_formulation": ["knowledge.linearization.big_m_method_indicator_linear"],
    "capacitated_vrp_cvrp": ["knowledge.linearization.big_m_method_indicator_linear"],
    "job_shop_scheduling_jssp": [
        "knowledge.linearization.big_m_method_indicator_linear",
        "knowledge.linearization.disjunctive_constraints_either_or",
    ],
}

MODEL_VARIABLE_RULE_LINKS: dict[str, list[str]] = {
    "set_covering_problem_scp": ["knowledge.variable_domain.source"],
    "set_partitioning_problem_spp": ["knowledge.variable_domain.source"],
    "bin_packing_problem_bpp": ["knowledge.variable_domain.source"],
    "0_1_knapsack_problem": ["knowledge.variable_domain.source"],
    "linear_assignment_problem_lap": ["knowledge.variable_domain.source", "knowledge.variable_domain.tu"],
    "generalized_assignment_problem_gap": ["knowledge.variable_domain.source"],
    "uncapacitated_facility_location_uflp": ["knowledge.variable_domain.source"],
    "traveling_salesman_problem_tsp_dfj_formulation": ["knowledge.variable_domain.source"],
    "capacitated_vrp_cvrp": ["knowledge.variable_domain.source"],
    "job_shop_scheduling_jssp": ["knowledge.variable_domain.source"],
    "flow_shop_scheduling_fssp_permutation": ["knowledge.variable_domain.source", "knowledge.variable_domain.tu"],
    "max_flow": ["knowledge.variable_domain.tu"],
    "min_cost_flow": ["knowledge.variable_domain.tu"],
    "transportation_problem": ["knowledge.variable_domain.tu"],
    "blending_diet_problem": ["knowledge.variable_domain.source"],
    "shortest_path_as_lp": ["knowledge.variable_domain.tu"],
    "portfolio_optimization_markowitz_mean_variance": ["knowledge.variable_domain.source"],
}

MODEL_TAXONOMY_DETAIL_LINKS: dict[str, list[str]] = {
    "set_covering_problem_scp": ["set_covering"],
    "set_partitioning_problem_spp": ["set_partitioning"],
    "bin_packing_problem_bpp": ["bin_packing"],
    "0_1_knapsack_problem": ["0_1_knapsack"],
    "linear_assignment_problem_lap": ["assignment_problem"],
    "generalized_assignment_problem_gap": ["assignment_problem"],
    "uncapacitated_facility_location_uflp": ["facility_location_uflp_cflp"],
    "traveling_salesman_problem_tsp_dfj_formulation": ["tsp"],
    "capacitated_vrp_cvrp": ["vrp_capacitated"],
    "job_shop_scheduling_jssp": ["job_shop_scheduling_jssp"],
    "flow_shop_scheduling_fssp_permutation": ["flow_shop_scheduling_fssp"],
    "max_flow": ["max_flow"],
    "min_cost_flow": ["min_cost_flow"],
    "transportation_problem": ["transportation_problem"],
    "blending_diet_problem": ["blending_diet_problem"],
    "portfolio_optimization_markowitz_mean_variance": ["portfolio_optimization_markowitz"],
}


def retrieval_terms(heading: str, body: str) -> list[str]:
    """Return heading plus model-specific aliases/signals for routing.

    The Markdown body is retained verbatim in ``source_excerpt``.  It is not
    mined for broad vocabulary here: shared words such as ``sets``, ``demand``
    and ``capacity`` are useful context but poor model discriminators.
    """
    key = slug(heading)
    aliases, signals = MODEL_RETRIEVAL.get(key, ([], []))
    return list(dict.fromkeys([heading, *aliases, *signals]))


def parse_standard(path: Path) -> tuple[list[dict], list[dict], list[str], list[dict], list[dict]]:
    text = path.read_text(encoding="utf-8")
    models: list[dict] = []
    variants: list[dict] = []
    signals: list[str] = []
    source_nodes: list[dict] = []
    source_edges: list[dict] = []
    for heading, body in sections(text):
        if heading.casefold() in {"standard mathematical formulations", "tsp — mtz formulation (compact)"}:
            continue
        key = slug(heading)
        model_id = f"reference.model.{key}"
        models.append({"id": model_id, "type": "formulation", "title": heading, "summary": summary(body), "payload": source_payload(path, heading, body)})
        source_id = f"source.reference.standard_formulations.{key}"
        source_nodes.append({"id": source_id, "type": "source_section", "title": heading, "summary": f"Source section for {heading}.", "payload": source_payload(path, heading, body)})
        source_edges.append({"source": "knowledge.source", "relation": "HAS_SECTION", "target": source_id})
        source_edges.append({"source": model_id, "relation": "DERIVED_FROM", "target": source_id})
        terms = retrieval_terms(heading, body)
        signals.extend(terms)
        variant_id = f"variant.reference.{key}"
        variants.append({
            "id": variant_id,
            "type": "retrieval_profile",
            "title": heading,
            "summary": summary(body),
            "retrieval": {"aliases": terms, "signals": terms, "feature_weights": {}},
            "payload": {"reference_model": model_id, "source_file": path.relative_to(ROOT).as_posix()},
        })
    return models, variants, signals, source_nodes, source_edges


def parse_linearization(path: Path) -> tuple[list[dict], list[dict]]:
    text = path.read_text(encoding="utf-8")
    nodes: list[dict] = []
    edges: list[dict] = []
    for heading, body in sections(text):
        if heading.casefold() in {"mip linearization techniques", "big-m computation checklist"}:
            continue
        node_id = f"knowledge.linearization.{slug(heading)}"
        nodes.append({"id": node_id, "type": "formulation_pattern", "title": heading, "summary": summary(body), "payload": source_payload(path, heading, body)})
        edges.append({"source": "knowledge.linearization", "relation": "HAS_PATTERN", "target": node_id})
    return nodes, edges


def parse_variable_domain(path: Path) -> tuple[list[dict], list[dict]]:
    """Convert every section of the original variable-domain guide."""
    text = path.read_text(encoding="utf-8")
    nodes: list[dict] = []
    edges: list[dict] = []
    stable_ids = {
        "decision tree": "knowledge.variable_domain.source",
        "tu (total unimodularity) rules": "knowledge.variable_domain.tu",
    }
    for heading, body in sections(text):
        node_id = stable_ids.get(heading.casefold(), f"knowledge.variable_domain.{slug(heading)}")
        nodes.append({"id": node_id, "type": "variable_rule", "title": heading, "summary": summary(body), "payload": source_payload(path, heading, body)})
        edges.append({"source": "knowledge.variable_domain", "relation": "HAS_PATTERN", "target": node_id})
    return nodes, edges


def parse_pitfalls(path: Path) -> tuple[list[dict], list[dict], list[dict]]:
    text = path.read_text(encoding="utf-8")
    errors: list[dict] = []
    fixes: list[dict] = []
    edges: list[dict] = []
    general = re.search(r"(?ms)^## General Pitfalls\s*(.+?)(?=^## |\Z)", text)
    if general:
        for row in general.group(1).splitlines():
            cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
            if len(cells) != 4 or not re.fullmatch(r"G\d+", cells[0]):
                continue
            code, title, symptom, fix_text = cells
            error_id = f"error.reference.{code.casefold()}"
            fix_id = f"fix.reference.{code.casefold()}"
            errors.append({"id": error_id, "type": "error", "title": title, "summary": symptom, "payload": {"code": code, "source_file": path.relative_to(ROOT).as_posix(), "source_heading": "General Pitfalls", "severity": "general"}})
            fixes.append({"id": fix_id, "type": "fix", "title": f"Fix: {title}", "summary": fix_text, "payload": {"source_file": path.relative_to(ROOT).as_posix(), "source_heading": "General Pitfalls"}})
            edges.append({"source": error_id, "relation": "FIXED_BY", "target": fix_id})
    per_problem = re.split(r"(?m)^###\s+(.+?)\s*$", text)
    for i in range(1, len(per_problem), 2):
        heading, body = per_problem[i].strip(), per_problem[i + 1]
        problem_key = slug(heading)
        for index, line in enumerate(body.splitlines(), 1):
            line = line.strip().lstrip("- ")
            if not line or line.startswith("|") or line.startswith("#"):
                continue
            error_id = f"error.reference.{problem_key}.{index}"
            fix_id = f"fix.reference.{problem_key}.{index}"
            errors.append({"id": error_id, "type": "error", "title": f"{heading}: {line[:120]}", "summary": line, "payload": {"source_file": path.relative_to(ROOT).as_posix(), "source_heading": heading, "severity": "domain"}})
            fixes.append({"id": fix_id, "type": "fix", "title": f"Review: {heading} pitfall {index}", "summary": f"Check and correct: {line}", "payload": {"source_file": path.relative_to(ROOT).as_posix(), "source_heading": heading}})
            edges.append({"source": error_id, "relation": "FIXED_BY", "target": fix_id})
    return errors, fixes, edges


def parse_taxonomy(path: Path) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    text = path.read_text(encoding="utf-8")
    families: list[dict] = []
    edges: list[dict] = []
    details: list[dict] = []
    detail_edges: list[dict] = []
    table = re.search(r"(?ms)^\| Family \| Possible classes \| Structural condition \|\s*\n\|[-| ]+\|\s*(.+?)(?=^## |\Z)", text)
    if table:
        for row in table.group(1).splitlines():
            cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
            if len(cells) != 3 or cells[0].casefold() == "family":
                continue
            family, classes, condition = cells
            family_id = f"family.reference.{slug(family)}"
            families.append({"id": family_id, "type": "problem_family", "title": family, "summary": condition, "payload": {"possible_classes": classes, "source_file": path.relative_to(ROOT).as_posix(), "source_heading": "Family -> Expected Class"}})
            edges.append({"source": "knowledge.taxonomy", "relation": "HAS_FAMILY", "target": family_id})
    for heading, body in sections(text):
        heading_key = heading.casefold()
        if heading_key.startswith("family"):
            continue
        if heading_key == "problem details":
            parts = re.split(r"(?m)^###\s+(.+?)\s*$", body)
            for index in range(1, len(parts), 2):
                detail_heading, detail_body = parts[index].strip(), parts[index + 1].strip()
                detail_key = re.sub(r"^\d+_", "", slug(detail_heading))
                detail_id = f"knowledge.taxonomy.detail.{detail_key}"
                details.append({"id": detail_id, "type": "source_section", "title": detail_heading, "summary": summary(detail_body), "payload": source_payload(path, detail_heading, detail_body)})
                detail_edges.append({"source": "knowledge.taxonomy", "relation": "HAS_DETAIL", "target": detail_id})
        else:
            detail_id = f"knowledge.taxonomy.rule.{slug(heading)}"
            details.append({"id": detail_id, "type": "source_section", "title": heading, "summary": summary(body), "payload": source_payload(path, heading, body)})
            detail_edges.append({"source": "knowledge.taxonomy", "relation": "HAS_RULE", "target": detail_id})
    return families, edges, details, detail_edges


def parse_pitfall_guidance(path: Path) -> tuple[list[dict], list[dict]]:
    """Retain the non-error severity guidance as an addressable node."""
    text = path.read_text(encoding="utf-8")
    for heading, body in sections(text):
        if heading.casefold() == "pitfall severity guide":
            node = {"id": "knowledge.pitfalls.severity_guide", "type": "source_section", "title": heading, "summary": summary(body), "payload": source_payload(path, heading, body)}
            return [node], [{"source": "knowledge.pitfalls", "relation": "HAS_GUIDANCE", "target": node["id"]}]
    return [], []


def main() -> int:
    nodes: list[dict] = [
        {"id": "domain.optimization", "type": "domain", "title": "Optimization", "summary": "Original OptAgent optimization knowledge base converted into addressable graph knowledge."},
        {"id": "knowledge.taxonomy", "type": "collection", "title": "Original problem taxonomy", "summary": "Converted hierarchy and model-class boundaries from the original OptAgent taxonomy.", "payload": {"source_file": ".agents/skills/opt-formulation/reference/problem-taxonomy.md"}},
        {"id": "knowledge.linearization", "type": "collection", "title": "Original MIP linearization library", "summary": "Converted logical, disjunctive, piecewise, and fixed-charge formulation patterns.", "payload": {"source_file": ".agents/skills/opt-formulation/reference/linearization-tricks.md"}},
        {"id": "knowledge.variable_domain", "type": "collection", "title": "Original variable-domain guide", "summary": "Converted domain-selection and total-unimodularity rules.", "payload": {"source_file": ".agents/skills/opt-formulation/reference/variable-domain-guide.md"}},
        {"id": "knowledge.pitfalls", "type": "collection", "title": "Original common pitfalls", "summary": "Converted general and problem-family modeling pitfalls with repair nodes.", "payload": {"source_file": ".agents/skills/opt-formulation/reference/common-pitfalls.md"}},
        {"id": "knowledge.source", "type": "collection", "title": "Original source sections", "summary": "Source-section nodes preserve the exact Markdown provenance behind canonical formulation nodes."},
    ]
    edges: list[dict] = [
        {"source": "domain.optimization", "relation": "HAS_KNOWLEDGE", "target": "knowledge.taxonomy"},
        {"source": "domain.optimization", "relation": "HAS_KNOWLEDGE", "target": "knowledge.linearization"},
        {"source": "domain.optimization", "relation": "HAS_KNOWLEDGE", "target": "knowledge.variable_domain"},
        {"source": "domain.optimization", "relation": "HAS_KNOWLEDGE", "target": "knowledge.pitfalls"},
        {"source": "domain.optimization", "relation": "HAS_KNOWLEDGE", "target": "knowledge.source"},
    ]
    standard = REFERENCE / "standard-formulations.md"
    models, variants, signals, source_nodes, source_edges = parse_standard(standard)
    nodes.extend(models)
    nodes.extend(variants)
    nodes.extend(source_nodes)
    edges.extend(source_edges)
    edges.extend({"source": v["id"], "relation": "HAS_MODEL", "target": v["payload"]["reference_model"]} for v in variants)
    edges.extend({"source": "knowledge.taxonomy", "relation": "HAS_MODEL", "target": m["id"]} for m in models)
    linear_nodes, linear_edges = parse_linearization(REFERENCE / "linearization-tricks.md")
    nodes.extend(linear_nodes)
    edges.extend(linear_edges)
    errors, fixes, pitfall_edges = parse_pitfalls(REFERENCE / "common-pitfalls.md")
    nodes.extend(errors)
    nodes.extend(fixes)
    edges.extend(pitfall_edges)
    families, taxonomy_edges, taxonomy_details, taxonomy_detail_edges = parse_taxonomy(REFERENCE / "problem-taxonomy.md")
    nodes.extend(families)
    nodes.extend(taxonomy_details)
    edges.extend(taxonomy_edges)
    edges.extend(taxonomy_detail_edges)
    pitfall_guidance, pitfall_guidance_edges = parse_pitfall_guidance(REFERENCE / "common-pitfalls.md")
    nodes.extend(pitfall_guidance)
    edges.extend(pitfall_guidance_edges)
    variable_nodes, variable_edges = parse_variable_domain(REFERENCE / "variable-domain-guide.md")
    nodes.extend(variable_nodes)
    edges.extend(variable_edges)
    node_ids = {node["id"] for node in nodes}
    model_by_key = {model["id"].removeprefix("reference.model."): model["id"] for model in models}
    for key, model_id in model_by_key.items():
        for family_id in MODEL_FAMILY_LINKS.get(key, []):
            if family_id not in node_ids:
                raise ValueError(f"model-family mapping references unknown node: {family_id}")
            edges.append({"source": model_id, "relation": "BELONGS_TO", "target": family_id})
        for pattern_id in MODEL_PATTERN_LINKS.get(key, []):
            if pattern_id not in node_ids:
                raise ValueError(f"model-pattern mapping references unknown node: {pattern_id}")
            edges.append({"source": model_id, "relation": "USES_PATTERN", "target": pattern_id})
        for rule_id in MODEL_VARIABLE_RULE_LINKS.get(key, []):
            if rule_id not in node_ids:
                raise ValueError(f"model-variable mapping references unknown node: {rule_id}")
            edges.append({"source": model_id, "relation": "USES_VARIABLE_RULE", "target": rule_id})
        for detail_key in MODEL_TAXONOMY_DETAIL_LINKS.get(key, []):
            detail_id = f"knowledge.taxonomy.detail.{detail_key}"
            if detail_id not in node_ids:
                raise ValueError(f"model-taxonomy mapping references unknown node: {detail_id}")
            edges.append({"source": model_id, "relation": "DESCRIBED_BY", "target": detail_id})
    # Attach converted domain pitfalls to the corresponding converted reference model.
    model_by_keyword = [(m["id"], set(re.findall(r"[a-z0-9]+", m["title"].casefold()))) for m in models]
    for error in errors:
        heading = str(error.get("payload", {}).get("source_heading", "")).casefold()
        if not heading or heading in {"general pitfalls", "pitfall severity guide"}:
            continue
        words = set(re.findall(r"[a-z0-9]+", heading))
        matches = [model_id for model_id, model_words in model_by_keyword if len(words & model_words) >= 1]
        for model_id in matches[:3]:
            edges.append({"source": model_id, "relation": "PRONE_TO", "target": error["id"]})
    # Make domain pitfalls reachable from the selected variant as well as its
    # reference model, so retrieval returns the original error checklist.
    model_to_variants = {v["payload"]["reference_model"]: v["id"] for v in variants}
    for edge in list(edges):
        if edge.get("relation") == "PRONE_TO" and edge.get("source") in model_to_variants:
            edges.append({"source": model_to_variants[edge["source"]], "relation": "PRONE_TO", "target": edge["target"]})
    edges.extend({"source": "knowledge.pitfalls", "relation": "HAS_ERROR", "target": error["id"]} for error in errors)
    graph = {
        "schema_version": "1.3",
        "graph_id": "legacy-reference-optgraph",
        "graph_version": "2026-09-04.3",
        "domain": "optimization",
        "domain_signals": sorted(set(signals + [family["title"] for family in families])),
        "nodes": nodes,
        "edges": edges,
        "source_manifest": {
            "converted_files": [path.relative_to(ROOT).as_posix() for path in REFERENCE.glob("*.md")],
            "conversion_tool": ".agents/skills/opt-formulation/tools/convert_reference_kb.py",
            "authority": "Original Markdown references remain authoritative; graph nodes are a derived index with source excerpts.",
        },
    }
    OUTPUT.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "nodes": len(nodes), "edges": len(edges), "variants": len(variants), "models": len(models), "errors": len(errors), "fixes": len(fixes), "families": len(families)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
