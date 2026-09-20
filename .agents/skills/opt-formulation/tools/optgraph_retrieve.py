#!/usr/bin/env python3
"""Retrieve a bounded, provenance-preserving context from an OptGraph JSON file."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
import time
import hashlib
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


DEFAULT_GRAPH = Path(__file__).resolve().parents[1] / "knowledge" / "optimization_optgraph.json"
DEFAULT_NETWORK_TIMEOUT = 5.0
ONLINE_CACHE_NAME = "online_case_cache.json"
# The fallback deliberately uses a small allow-list of public, source-backed
# indexes.  It is not an arbitrary web search and never treats fetched text as
# authoritative model data.
ONLINE_SOURCE_REGISTRY = {
    "or_tools": {
        "organization": "Google OR-Tools",
        "tree_url": "https://api.github.com/repos/google/or-tools/git/trees/stable?recursive=1",
        "raw_base": "https://raw.githubusercontent.com/google/or-tools/stable/",
        "repo_url": "https://github.com/google/or-tools/tree/stable",
        "documentation_url": "https://developers.google.com/optimization/examples",
        "path_prefixes": ("ortools/constraint_solver/samples/", "ortools/algorithms/samples/", "examples/python/"),
    },
    "gurobi_modeling_examples": {
        "organization": "Gurobi Optimization",
        "tree_url": "https://api.github.com/repos/Gurobi/modeling-examples/git/trees/master?recursive=1",
        "raw_base": "https://raw.githubusercontent.com/Gurobi/modeling-examples/master/",
        "repo_url": "https://github.com/Gurobi/modeling-examples/tree/master",
        "documentation_url": "https://gurobi.github.io/modeling-examples/",
        "path_prefixes": ("",),
    },
}
DEFAULT_FEATURE_CATALOG = {
    "capacity": ("capacity", "capacitated", "load limit", "vehicle capacity", "truck capacity", "warehouse capacity", "supply", "demand", "车辆容量", "仓容", "产能"),
    "time_windows": ("time window", "time-window", "appointment", "appointment window", "latest arrival", "earliest arrival", "时间窗", "预约时段"),
    # A plain Chinese "送货/取货" describes ordinary delivery just as often
    # as a paired pickup-delivery request.  Keep the positive aliases for
    # explicit paired-service wording only; ordinary delivery is handled by
    # the vehicle-routing profile and must not activate DARP constraints.
    "pickup_delivery": ("pickup and delivery", "pickup-delivery", "pickup delivery", "pick-up and drop-off", "precedence pickup", "接送服务", "接送请求", "接送点", "接送节点", "取送节点", "取送任务", "上车点", "下车点"),
    "same_vehicle": ("same vehicle", "same truck", "same driver", "同一辆车", "统一车辆", "同一车辆", "同一司机"),
    "ride_time": ("ride time", "maximum ride time", "max ride time", "longest ride time", "passenger ride duration", "在途时间", "最长乘车时间"),
    "service_time": ("service time", "loading time", "unloading time", "服务时间", "装卸时间"),
    "depot_start_end": ("start and end at the depot", "return to depot", "depot start", "depot end", "从仓库出发", "返回仓库", "回到基地"),
    "subtour_elimination": ("subtour elimination", "eliminate subtours", "disconnected cycle", "消除子回路", "子回路"),
    "passenger_load": ("passenger load", "on-board passengers", "乘客载荷", "在途乘客", "车上乘客"),
    "breaks": ("driver break", "mandatory break", "break time", "rest rule", "rest period"),
    "reload": ("reload", "refill", "replenishment station", "reload station"),
    "optional_nodes": ("optional customer", "dropped node", "skip customer", "penalty for dropping"),
    "multi_depot": ("multi-depot", "multiple depots", "different starts and ends", "individual starts"),
    "battery": ("battery", "electric vehicle", "charging station", "state of charge"),
    "setup_times": ("setup time", "sequence-dependent setup", "changeover", "setup cost", "换型时间", "准备时间"),
    "precedence": ("precedence", "job shop", "operation order", "no overlap", "before task", "工序顺序", "前置"),
    # Bare ``task``/``worker`` occur in nearly every scheduling description;
    # only explicit assignment phrases should activate this feature.
    "assignment": ("assign", "assignment", "worker-task", "facility assignment", "task assignment", "worker assignment", "team assignment", "任务分配", "分配"),
    "fixed_opening_cost": ("fixed opening cost", "facility opening cost", "facility opening costs", "opening cost", "open facilities", "activation cost", "设施开设成本", "设施启用成本"),
    "inventory": ("inventory", "lot sizing", "holding cost", "backlog", "stock", "safety stock", "库存", "补货", "批量"),
    "maintenance": ("maintenance", "maintenance interval", "检修", "维护窗口"),
    "stochastic_demand": ("stochastic demand", "uncertain demand", "demand scenarios", "scenario-based", "随机需求", "需求不确定", "需求场景"),
    "robustness": ("robust optimization", "robust counterpart", "worst-case uncertainty", "鲁棒优化", "最坏情况不确定性"),
    "chance_constraints": ("chance constraint", "probabilistic constraint", "service level probability", "机会约束", "概率约束"),
    "multiobjective": ("multi-objective", "multiobjective", "lexicographic objective", "pareto", "多目标", "词典序目标", "帕累托"),
    "grid_assignment": ("grid assignment", "board assignment", "fill every cell", "fill all cells", "棋盘填充", "填满所有格子", "网格赋值"),
    "grid_line_count": ("3d tic-tac-toe", "3d tic tac toe", "complete line", "full line", "monochromatic line", "三维井字棋", "完整直线", "同色直线"),
    "line_count_objective": ("line count", "number of lines", "complete line", "full line", "直线数量", "对角线数量"),
    "exact_fill": ("all cells must be filled", "exactly one symbol per cell", "every cell occupied", "每个格子恰好一个", "所有格子填满"),
}
FEATURE_PATH_HINTS = {
    "time_windows": ("time_window", "cvrptw"),
    "breaks": ("break",),
    "reload": ("reload",),
    "battery": ("battery", "resource"),
    "optional_nodes": ("drop", "penalty"),
    "multi_depot": ("depot", "starts_ends"),
    "pickup_delivery": ("pickup_delivery",),
    "capacity": ("capacity",),
    "setup_times": ("setup",),
}
ONLINE_STOPWORDS = {"a", "an", "and", "the", "for", "with", "from", "to", "of", "in", "on", "or", "using", "use", "optimize", "optimization", "problem", "model", "schedule"}
WORD_RE = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")
SCORING_STOPWORDS = {
    "a", "an", "and", "the", "for", "from", "with", "into", "onto", "using",
    "use", "problem", "model", "optimization", "objective", "constraints", "constraint",
    "minimize", "maximize", "total", "value", "cost", "number", "one", "two",
}
HARD_FEATURES = {
    "dial_a_ride", "pickup_delivery", "same_vehicle", "ride_time",
    "grid_line_count", "line_count_objective", "fixed_opening_cost",
    "inventory_balance", "setup_binary", "stochastic_demand", "robustness",
    "chance_constraints", "battery", "mode_selection", "renewable_resource",
    "nonrenewable_resource", "split_demand", "required_arc_service",
    "alternative_machine", "sequence_dependent_setup", "upper_lower_bounds",
    "multi_depot", "optional_customers", "car_sequencing", "cluster_assignment",
    "line_balancing", "project_resource", "commodity_flow", "revenue_capacity",
    "steel_slab", "batch_scheduling", "open_shop", "backhaul_service",
    "clustered_routing", "transshipment_facility", "curve_fitting",
    "nonlinear_benchmark", "continuous_design",
}
SPECIALIZED_FEATURES = HARD_FEATURES | {
    "time_window", "time_windows", "multi_depot", "battery", "breaks",
    "reload", "optional_nodes", "setup_times", "maintenance", "single_source",
    "precedence", "pickup_delivery", "same_vehicle", "subtour_elimination",
    "flow_balance", "inventory_balance", "vehicle_capacity", "renewable_resource",
    "sequence_constraints", "cycle_time", "station_order", "resource_capacity",
    "shared_capacity", "penalty_objective", "expected_revenue", "demand_bounds",
    "batch_assignment", "waste_objective", "route_continuity", "nonlinear",
    "continuous_variables", "bounded_domain", "global_optimization", "least_squares",
    "data_fitting", "engineering_constraints", "disjunctive_scheduling",
}
TYPE_ORDER = {
    "family": 0,
    "problem_family": 0,
    "variant": 0,
    "retrieval_profile": 0,
    "math_model": 1,
    "reference_model": 1,
    "formulation": 1,
    "source_section": 2,
    "extension": 2,
    "formulation_pattern": 3,
    "variable_rule": 3,
    "solver_pattern": 3,
    "case": 4,
    "typical_error": 5,
    "error": 5,
    "fix_hint": 6,
    "fix": 6,
    "validation_check": 7,
    "problem_type": 8,
    "domain": 8,
    "collection": 9,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--problem-file", type=Path)
    source.add_argument("--query")
    parser.add_argument("--graph", type=Path, default=DEFAULT_GRAPH)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--max-cases", type=int, default=2)
    parser.add_argument("--max-context-chars", type=int, default=12000)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--network-fallback", dest="network_fallback", action=argparse.BooleanOptionalAction,
                        default=True, help="query allow-listed official indexes only when local retrieval is insufficient")
    parser.add_argument("--network-timeout", type=float, default=DEFAULT_NETWORK_TIMEOUT)
    parser.add_argument("--cache", type=Path, help="online case cache path; defaults beside --output")
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def load_graph(path: Path, _seen: set[Path] | None = None) -> dict[str, Any]:
    """Load a graph and its relative ``includes`` slices into one view."""
    seen = set() if _seen is None else set(_seen)
    resolved = path.resolve()
    if resolved in seen:
        raise ValueError(f"recursive graph include: {path}")
    seen.add(resolved)
    graph = load_json(path)
    merged = dict(graph)
    merged["nodes"] = list(graph.get("nodes", []))
    merged["edges"] = list(graph.get("edges", []))
    merged["domain_signals"] = list(graph.get("domain_signals", []))
    merged["feature_catalog"] = dict(graph.get("feature_catalog", {}))
    for include in graph.get("includes", []):
        include_path = (path.parent / include).resolve()
        child = load_graph(include_path, seen)
        merged["nodes"].extend(child.get("nodes", []))
        merged["edges"].extend(child.get("edges", []))
        merged["domain_signals"].extend(child.get("domain_signals", []))
        for name, rules in child.get("feature_catalog", {}).items():
            if name not in merged["feature_catalog"]:
                merged["feature_catalog"][name] = rules
                continue
            # Slices often add domain-specific aliases for a shared feature
            # such as capacity or time windows. Merge those aliases instead
            # of silently discarding the child slice.
            current = merged["feature_catalog"][name]
            if isinstance(current, dict) and isinstance(rules, dict):
                for polarity in ("positive", "negative"):
                    values = list(current.get(polarity, []))
                    for phrase in rules.get(polarity, []):
                        if phrase not in values:
                            values.append(phrase)
                    current[polarity] = values
    merged.pop("includes", None)
    return merged


def tokenize(text: str) -> list[str]:
    return WORD_RE.findall(text.casefold())


def scoring_tokens(text: str) -> set[str]:
    """Tokens useful for semantic ranking, excluding symbols and boilerplate."""
    return {
        token for token in tokenize(text)
        if len(token) > 1 and token not in SCORING_STOPWORDS
    }


def contains_phrase(text: str, phrase: str) -> bool:
    return phrase.casefold() in text.casefold()


def feature_phrase_hits(text: str, feature: str, phrases: list[str]) -> list[str]:
    """Match feature aliases with small domain-specific boundary guards.

    Chinese has no whitespace boundary, so substring matching can turn
    ``直接送往`` into the paired-service phrase ``接送``.  Explicit paired
    wording is accepted; delivery-only wording is intentionally not.
    """
    normalized = text.casefold()
    hits: list[str] = []
    for phrase in phrases:
        p = str(phrase).casefold()
        # A few legacy Markdown slices contain mojibake fragments that are
        # single CJK characters (for example ``仓`` under inventory).  Such
        # one-character aliases are far too broad in Chinese prose and turn a
        # normal depot/capacity description into a false feature hit.
        if len(p.strip()) <= 1:
            continue
        if feature == "pickup_delivery" and p == "接送" and (
            "直接送" in normalized or "配送" in normalized or "送货" in normalized
        ):
            continue
        if p in normalized:
            hits.append(str(phrase))
    return hits


def infer_features(query: str, catalog: dict[str, Any]) -> tuple[dict[str, bool], dict[str, list[str]]]:
    values: dict[str, bool] = {}
    evidence: dict[str, list[str]] = {}
    merged_catalog: dict[str, Any] = {
        name: {"positive": list(phrases), "negative": []}
        for name, phrases in DEFAULT_FEATURE_CATALOG.items()
    }
    for name, rules in (catalog or {}).items():
        if not isinstance(rules, dict):
            continue
        merged_catalog.setdefault(name, {"positive": []})
        for polarity in ("positive", "negative"):
            merged_catalog[name].setdefault(polarity, [])
            for phrase in rules.get(polarity, []):
                if phrase not in merged_catalog[name][polarity]:
                    merged_catalog[name][polarity].append(phrase)
    for name, rules in merged_catalog.items():
        positive = feature_phrase_hits(query, name, rules.get("positive", []))
        negative = feature_phrase_hits(query, name, rules.get("negative", []))
        if negative:
            values[name] = False
            evidence[name] = [f"negative:{p}" for p in negative]
        elif positive:
            values[name] = True
            evidence[name] = [f"positive:{p}" for p in positive]
    return values, evidence


def _http_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "OptAgent-OptGraph/1.0", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_text(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "OptAgent-OptGraph/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(12000).decode("utf-8", errors="replace")


def _query_key(query: str) -> str:
    normalized = " ".join(tokenize(query))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _load_cache(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"schema_version": "1.0", "queries": {}, "indexes": {}}
    try:
        value = load_json(path)
        return value if isinstance(value, dict) else {"schema_version": "1.0", "queries": {}, "indexes": {}}
    except (OSError, ValueError, json.JSONDecodeError):
        return {"schema_version": "1.0", "queries": {}, "indexes": {}}


def _feature_terms(features: dict[str, bool], catalog: dict[str, Any] | None = None) -> set[str]:
    terms: set[str] = set()
    for feature, enabled in features.items():
        if enabled:
            terms.add(feature.replace("_", " "))
            terms.update(DEFAULT_FEATURE_CATALOG.get(feature, ()))
            rules = (catalog or {}).get(feature, {})
            if isinstance(rules, dict):
                terms.update(rules.get("positive", []))
    return set(tokenize(" ".join(terms)))


def _source_excerpt(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    useful = [line for line in lines if not line.startswith(("#", "//")) or "ortools" in line.casefold()]
    return " ".join(useful[:12])[:900]


def online_case_fallback(query: str, features: dict[str, bool], *, cache_path: Path | None,
                         timeout: float = DEFAULT_NETWORK_TIMEOUT, max_cases: int = 2,
                         catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    """Find source-backed examples after local retrieval misses.

    Results are compact case nodes.  A URL is verified by a successful HTTP
    response, but no objective or solver result is inferred from the source.
    """
    cache = _load_cache(cache_path)
    key = _query_key(query)
    cached = cache.get("queries", {}).get(key)
    if isinstance(cached, dict):
        return {"cases": cached.get("cases", [])[:max_cases], "source": "online_cached", "cache_hit": True}
    feature_terms = _feature_terms(features, catalog)
    query_tokens = (set(tokenize(query)) | feature_terms) - ONLINE_STOPWORDS
    candidates: list[tuple[float, str, dict[str, Any]]] = []
    errors: list[str] = []
    for source_id, source in ONLINE_SOURCE_REGISTRY.items():
        try:
            index = cache.setdefault("indexes", {}).get(source_id)
            if not isinstance(index, list):
                payload = _http_json(source["tree_url"], timeout)
                supported_extensions = {".py", ".ipynb", ".mps", ".lp", ".json"}
                index = [item["path"] for item in payload.get("tree", []) if item.get("type") == "blob" and Path(item.get("path", "")).suffix.casefold() in supported_extensions and any(item.get("path", "").startswith(prefix) for prefix in source["path_prefixes"])]
                cache.setdefault("indexes", {})[source_id] = index
            for path in index:
                stem_tokens = set(tokenize(Path(path).stem.replace("_", " "))) - ONLINE_STOPWORDS
                path_tokens = set(tokenize(path.replace("_", " "))) - ONLINE_STOPWORDS
                overlap = len(query_tokens & (stem_tokens | path_tokens))
                feature_overlap = sum(1 for token in feature_terms if token in (stem_tokens | path_tokens))
                path_text = path.casefold()
                hint_overlap = sum(
                    1 for feature, enabled in features.items() if enabled
                    and any(hint in path_text for hint in FEATURE_PATH_HINTS.get(feature, ()))
                )
                if overlap == 0 and feature_overlap == 0:
                    continue
                score = float(overlap) + 1.5 * feature_overlap + 3.0 * hint_overlap
                code_url = source["repo_url"].replace("/tree/", "/blob/") + "/" + path
                path_features = {feature for feature, enabled in features.items() if enabled and any(hint in path_text for hint in FEATURE_PATH_HINTS.get(feature, ()))}
                candidates.append((score, path, {"source_id": source_id, "path": path, "code_url": code_url, "raw_url": source["raw_base"] + path, "source": source, "feature_hits": path_features}))
        except (OSError, ValueError, urllib.error.URLError, TimeoutError, KeyError) as exc:
            errors.append(f"{source_id}: {exc.__class__.__name__}")
    cases: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    seen_examples: set[str] = set()
    extension_rank = {".py": 0, ".cc": 1, ".java": 2, ".cs": 3, ".go": 4}
    remaining = sorted(candidates, key=lambda value: (-value[0], extension_rank.get(Path(value[1]).suffix, 9), value[1]))
    covered_online_features: set[str] = set()
    while remaining and len(cases) < max_cases:
        # Greedy diversity keeps a compound request from returning language
        # ports of one example and favors a second source file covering a
        # different detected constraint (breaks, reload, optional nodes, ...).
        best_index = max(
            range(len(remaining)),
            key=lambda index: (
                remaining[index][0] + 4.0 * len(remaining[index][2].get("feature_hits", set()) - covered_online_features),
                -extension_rank.get(Path(remaining[index][1]).suffix, 9),
                remaining[index][1],
            ),
        )
        _, path, item = remaining.pop(best_index)
        # Prefer one implementation per example (Python before C++/Java) so
        # a compound query receives distinct modeling patterns instead of
        # three language ports of the same source case.
        example_key = re.sub(r"_(?:py|cc|java|cs|go)$", "", Path(path).stem.casefold())
        if path in seen_paths or example_key in seen_examples or len(cases) >= max_cases:
            continue
        seen_paths.add(path)
        seen_examples.add(example_key)
        covered_online_features.update(item.get("feature_hits", set()))
        source = item["source"]
        excerpt = ""
        try:
            excerpt = _source_excerpt(_http_text(item["raw_url"], timeout))
        except (OSError, urllib.error.URLError, TimeoutError):
            pass
        case_id = "case.online." + re.sub(r"[^a-z0-9]+", "_", path.casefold()).strip("_")
        cases.append({
            "id": case_id,
            "type": "case",
            "title": f"{source['organization']} online example: {Path(path).stem.replace('_', ' ')}",
            "summary": excerpt or f"Source-backed example from {source['organization']} ({path}).",
            "retrieval": {"aliases": [Path(path).stem.replace("_", " "), path], "signals": [Path(path).stem.replace("_", " ")]},
            "payload": {
                "status": "online_source_verified_not_reproduced",
                "source": {"organization": source["organization"], "code_url": item["code_url"], "raw_url": item["raw_url"], "documentation_url": source["documentation_url"], "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                "source_path": path,
                "validation_note": "Source URL was fetched; objective values and feasibility were not inferred or reproduced by the fallback.",
            },
        })
    if cases:
        cache.setdefault("queries", {})[key] = {"query": query, "cases": cases, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if cache_path is not None:
            atomic_write_json(cache_path, cache)
    return {"cases": cases, "source": "online_uncached" if cases else "online_unavailable", "cache_hit": False, "errors": errors}


def variant_document(node: dict[str, Any]) -> str:
    retrieval = node.get("retrieval", {})
    parts = [node.get("title", ""), node.get("summary", "")]
    parts.extend(retrieval.get("aliases", []))
    parts.extend(retrieval.get("signals", []))
    # Official case cards keep their useful modeling signature in payload
    # fields (constraint_patterns, data_contract, and solution_recipe). Bring
    # those compact fields into ranking so a pickup-delivery case beats a
    # merely capacity-similar routing case.
    payload = node.get("payload", {})
    if isinstance(payload, dict):
        for key in (
            "model_family", "data_contract", "objective_pattern",
            "constraint_patterns", "solution_recipe", "solver_hint",
            "validation_checks", "expected_variant",
        ):
            value = payload.get(key)
            if value is not None:
                parts.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    return " ".join(str(part) for part in parts)


def idf_table(variants: list[dict[str, Any]]) -> dict[str, float]:
    documents = [scoring_tokens(variant_document(node)) for node in variants]
    frequency = Counter(token for document in documents for token in document)
    count = len(documents)
    return {
        token: math.log((count + 1) / (seen + 0.5)) + 1
        for token, seen in frequency.items()
    }


def parse_feature_key(key: str) -> tuple[str, bool]:
    if key.endswith(":true"):
        return key[:-5], True
    if key.endswith(":false"):
        return key[:-6], False
    return key, True


def node_feature_support(node: dict[str, Any], catalog: dict[str, Any] | None = None) -> set[str]:
    """Return the features a node explicitly advertises or visibly covers.

    Older graph slices only store feature weights, while newer composite
    profiles carry a ``feature_support`` signature. Supporting both formats
    keeps the graph backward compatible and lets the scorer penalize missing
    special constraints instead of rewarding generic word overlap.
    """
    retrieval = node.get("retrieval", {})
    support = {
        str(feature)
        for feature in retrieval.get("feature_support", [])
        if isinstance(feature, str)
    }
    for key in retrieval.get("feature_weights", {}):
        feature, expected = parse_feature_key(str(key))
        if expected:
            support.add(feature)
    document = variant_document(node)
    merged_catalog: dict[str, Any] = {name: {"positive": list(phrases)} for name, phrases in DEFAULT_FEATURE_CATALOG.items()}
    for name, rules in (catalog or {}).items():
        if not isinstance(rules, dict):
            continue
        merged_catalog.setdefault(name, {"positive": []})
        merged_catalog[name]["positive"] = list(dict.fromkeys(
            [*merged_catalog[name].get("positive", []), *rules.get("positive", [])]
        ))
    for feature, rules in merged_catalog.items():
        if feature_phrase_hits(document, feature, rules.get("positive", [])):
            support.add(feature)
    return support


def score_variant(
    query: str,
    query_tokens: set[str],
    features: dict[str, bool],
    node: dict[str, Any],
    idf: dict[str, float],
    catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    retrieval = node.get("retrieval", {})
    shared = sorted(query_tokens & scoring_tokens(variant_document(node)))
    score = sum(idf.get(token, 1.0) for token in shared)
    reasons: list[str] = []
    if shared:
        reasons.append(f"token overlap: {', '.join(shared[:8])}")

    phrase_hits = [p for p in retrieval.get("signals", []) if contains_phrase(query, p)]
    if phrase_hits:
        score += 1.5 * len(phrase_hits)
        reasons.append(f"signal phrases: {', '.join(phrase_hits)}")

    # The first line is normally the user's short problem label.  An exact
    # profile signal there is stronger evidence than generic words in the
    # long business description (e.g. Max-Cut must not lose to TSP because
    # both descriptions contain ``graph`` and ``problem``).
    label = query.splitlines()[0].strip().casefold() if query.splitlines() else ""
    exact_label_hits = [p for p in retrieval.get("signals", [])
                        if str(p).strip().casefold() == label and len(str(p).strip()) >= 3]
    if exact_label_hits:
        score += 16.0 * len(exact_label_hits)
        reasons.append(f"exact problem label: {', '.join(exact_label_hits)}")

    for key, weight in retrieval.get("feature_weights", {}).items():
        feature, expected = parse_feature_key(key)
        if features.get(feature) is expected:
            score += float(weight)
            reasons.append(f"feature match: {feature}={str(expected).lower()}")
    for key, penalty in retrieval.get("feature_conflicts", {}).items():
        feature, conflicting = parse_feature_key(key)
        observed = features.get(feature)
        if observed is conflicting or (conflicting is False and observed is None):
            score -= float(penalty)
            reasons.append(f"feature conflict: {feature}={str(conflicting).lower()}")
    requested = {name for name, enabled in features.items() if enabled}
    if requested:
        supported = node_feature_support(node, catalog)
        covered = requested & supported
        missing = requested - supported
        # Coverage is the primary signal for compound problems. A candidate
        # that matches generic words but omits ride time, pairing, or another
        # explicitly requested feature should be demoted sharply.
        hard_covered = covered & HARD_FEATURES
        hard_missing = missing & HARD_FEATURES
        unrequested_specialized = (supported - requested) & SPECIALIZED_FEATURES
        score += 3.5 * len(covered)
        score += 3.0 * len(hard_covered)
        score += 2.0 * (len(covered) / len(requested))
        score -= 4.0 * len(missing)
        score -= 3.0 * len(hard_missing)
        score -= 1.5 * len(unrequested_specialized)
        if missing:
            reasons.append(f"missing features: {', '.join(sorted(missing)[:8])}")
        if unrequested_specialized:
            reasons.append(f"extra specialized features: {', '.join(sorted(unrequested_specialized)[:8])}")
        reasons.append(f"feature coverage: {len(covered)}/{len(requested)}")
    return {"node_id": node["id"], "title": node["title"], "score": round(score, 4), "reasons": reasons}


def validate_graph(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("graph requires nodes and edges arrays")
    by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        node_id = node.get("id")
        if not node_id or node_id in by_id:
            raise ValueError(f"missing or duplicate node id: {node_id!r}")
        by_id[node_id] = node
    for edge in edges:
        if edge.get("source") not in by_id or edge.get("target") not in by_id:
            raise ValueError(f"edge references an unknown node: {edge}")
    return by_id


def project_node(node: dict[str, Any]) -> dict[str, Any]:
    return {key: node[key] for key in ("id", "type", "title", "summary", "payload") if key in node}


def render_context(nodes: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    for node in nodes:
        lines = [f"[{node['id']}] {node['type']}: {node['title']}", node.get("summary", "")]
        if "payload" in node:
            lines.append(json.dumps(node["payload"], ensure_ascii=False, separators=(",", ":")))
        sections.append("\n".join(line for line in lines if line))
    return "\n\n".join(sections)


def retrieve(
    graph: dict[str, Any], query: str, *, top_k: int = 3, max_cases: int = 2,
    max_context_chars: int = 12000, network_fallback: bool = False,
    network_timeout: float = DEFAULT_NETWORK_TIMEOUT, cache_path: Path | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    by_id = validate_graph(graph)
    variants = [node for node in graph["nodes"] if node.get("type") in {"variant", "retrieval_profile"}]
    all_cases = [node for node in graph["nodes"] if node.get("type") == "case"]
    if not variants:
        raise ValueError("graph has no variant nodes")

    features, feature_evidence = infer_features(query, graph.get("feature_catalog", {}))
    query_tokens = scoring_tokens(query)
    idf = idf_table(variants)
    candidates = sorted(
        (score_variant(query, query_tokens, features, node, idf, graph.get("feature_catalog", {})) for node in variants),
        key=lambda item: (-item["score"], item["node_id"]),
    )[: max(1, top_k)]

    best = candidates[0]["score"]
    second = candidates[1]["score"] if len(candidates) > 1 else float("-inf")
    margin = best - second
    domain_hits = [p for p in graph.get("domain_signals", []) if contains_phrase(query, p)]
    label = query.splitlines()[0].strip().casefold() if query.splitlines() else ""
    label_hits = [
        signal for item in candidates
        for signal in by_id[item["node_id"]].get("retrieval", {}).get("signals", [])
        if str(signal).strip().casefold() == label and len(str(signal).strip()) >= 3
    ]
    # A short, explicit model name is itself a domain signal.  Requiring a
    # second generic word made ``Max-Cut Problem`` and other perfectly valid
    # title-only prompts look insufficient even when an exact profile won.
    if label_hits:
        domain_hits = [*domain_hits, *[f"label:{hit}" for hit in label_hits]]
    if (not domain_hits and not label_hits) or best < 2.0:
        status = "insufficient"
    elif margin < 1.25:
        status = "ambiguous"
    else:
        status = "sufficient"

    # Generic words such as "customer", "model", and "schedule" can make a
    # broad profile score look healthy even when a compound problem has
    # several constraints absent from that profile. Require the winner to
    # cover most requested features, with a stricter rule for a missing
    # pairing, uncertainty, or objective signature.
    active_features = {name for name, enabled in features.items() if enabled}
    if active_features and candidates:
        winning_node = by_id[candidates[0]["node_id"]]
        covered_features = active_features & node_feature_support(winning_node, graph.get("feature_catalog", {}))
        hard_features = active_features & HARD_FEATURES
        # Generic words such as ``capacity``, ``inventory`` and ``assignment``
        # are useful ranking signals but are not enough to reject an otherwise
        # exact model.  Completeness is checked against the semantic signature
        # (hard + specialized features) only; this prevents mojibake or a
        # routine business word in a long description from turning a valid
        # exact-label hit into ``insufficient``.
        required_features = active_features & SPECIALIZED_FEATURES
        coverage = (
            len(covered_features & required_features) / len(required_features)
            if required_features else 1.0
        )
        if (hard_features - covered_features) or (len(required_features) >= 2 and coverage < 0.75):
            status = "insufficient"

    seed_ids = [] if status == "insufficient" else [candidates[0]["node_id"]]
    if status == "ambiguous" and len(candidates) > 1:
        seed_ids.append(candidates[1]["node_id"])

    outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in graph["edges"]:
        outgoing[edge["source"]].append(edge)

    # Official examples and multi-stage workflows can be more specific than
    # any one formulation token. Rank these case nodes directly when their
    # own signal phrase is present, then seed the linked formulations.
    direct_case_ids: list[str] = []
    direct_case_sources: list[str] = []
    if all_cases:
        case_idf = idf_table(all_cases)
        special_cases = [
            node for node in all_cases
            if node.get("payload", {}).get("status") in {
                "official_example", "expert_composed_workflow",
            }
        ]
        detail_cases = [node for node in all_cases if node.get("payload", {}).get("status") == "detail_verified"]
        ranked_special = sorted(
            (score_variant(query, query_tokens, features, node, case_idf, graph.get("feature_catalog", {})) for node in special_cases),
            key=lambda item: (-item["score"], item["node_id"]),
        )
        for item in ranked_special:
            node = by_id[item["node_id"]]
            aliases = [
                *node.get("retrieval", {}).get("aliases", []),
                *node.get("retrieval", {}).get("signals", []),
            ]
            signal_hits = [
                signal for signal in aliases
                if len(str(signal).strip()) >= 4 and contains_phrase(query, str(signal))
            ]
            # ReasLab detail cases carry the exact template title and aliases;
            # selecting them directly avoids falling back to a generic family
            # when a user names a template from the catalog.
            if signal_hits:
                direct_case_ids.append(item["node_id"])
                direct_case_sources.extend(
                    edge["source"] for edge in graph["edges"]
                    if edge["target"] == item["node_id"] and edge["relation"] == "EXEMPLIFIED_BY"
                )
                if len(direct_case_ids) >= max(1, max_cases):
                    break
        # A direct case signal is evidence for a case, not automatically for
        # a formulation.  Do not mark a query sufficient when it has no
        # viable local profile (for example a revenue case card matched only
        # the generic word ``capacity``).
        if direct_case_ids and domain_hits and seed_ids:
            status = "sufficient"

    selected_ids = set(seed_ids)
    local_status = status
    if direct_case_ids:
        winner = by_id[candidates[0]["node_id"]]
        winner_support = node_feature_support(winner, graph.get("feature_catalog", {}))
        requested_features = {name for name, enabled in features.items() if enabled}
        winner_coverage = (
            len(winner_support & requested_features) / len(requested_features)
            if requested_features else 1.0
        )
        preserve_composite_profile = (
            by_id[candidates[0]["node_id"]].get("type") in {"variant", "retrieval_profile"}
            and bool(by_id[candidates[0]["node_id"]].get("retrieval", {}).get("feature_support"))
            and winner_coverage >= 0.75
        )
        variant_seeds = []
        for source_id in direct_case_sources:
            if source_id.startswith("reference.model."):
                variant_id = source_id.replace("reference.model.", "variant.reference.", 1)
            elif by_id.get(source_id, {}).get("type") in {"variant", "retrieval_profile"}:
                variant_id = source_id
            else:
                # A composite formulation is a semantic seed, not a
                # retrieval profile. Keep the winning profile rather than
                # exposing a formulation ID in selected_variant_ids.
                continue
            if variant_id in by_id and variant_id not in variant_seeds:
                variant_seeds.append(variant_id)
        if variant_seeds and not preserve_composite_profile:
            seed_ids = variant_seeds
            selected_ids.update(seed_ids)
            selected_ids.difference_update({node_id for node_id in selected_ids if by_id[node_id].get("type") == "case"})
    selected_edges: list[dict[str, Any]] = []
    # Walk the semantic neighborhood from the selected retrieval profile.  A
    # bounded relation allow-list keeps the bundle focused while allowing the
    # canonical model to expose its family, patterns, variable rules, and
    # source section.
    expansion_relations = {
        "IS_A", "BELONGS_TO", "HAS_FAMILY", "HAS_VARIANT", "HAS_MODEL",
        "PRONE_TO", "CHECK_WITH", "USES_PATTERN", "COMPOSES",
        "USES_VARIABLE_RULE", "DERIVED_FROM", "DESCRIBED_BY",
        "HAS_DETAIL", "HAS_RULE", "HAS_GUIDANCE",
    }
    frontier = list(seed_ids)
    while frontier:
        source_id = frontier.pop(0)
        for edge in outgoing.get(source_id, []):
            if edge["relation"] not in expansion_relations:
                continue
            selected_edges.append(edge)
            target_id = edge["target"]
            if target_id not in selected_ids:
                selected_ids.add(target_id)
                frontier.append(target_id)

    # Cases can grow much faster than the formulation ontology. Rank only
    # cases attached to the selected neighborhood and keep a small bounded
    # sample so adding cases does not inflate every prompt.
    # Do not collect every case hanging below a broad family node.  That made
    # a unit-commitment query inherit unrelated revenue-management examples
    # merely because both lived under operations planning.  Cases may be
    # attached to the winning profile itself or to its selected formulation /
    # reference model, but not to a family reached during taxonomy expansion.
    case_sources = set(seed_ids)
    case_sources.update(
        node_id for node_id in selected_ids
        if by_id[node_id].get("type") in {"math_model", "formulation", "reference_model"}
    )
    attached_case_edges = [
        edge for source_id in case_sources for edge in outgoing.get(source_id, [])
        if edge["relation"] == "EXEMPLIFIED_BY"
        and by_id[edge["target"]].get("type") == "case"
    ]
    attached_cases = {edge["target"]: by_id[edge["target"]] for edge in attached_case_edges}
    selected_case_ids: list[str] = list(direct_case_ids[:max_cases])
    if selected_case_ids:
        selected_case_set = set(selected_case_ids)
        selected_ids.update(selected_case_set)
        selected_edges.extend(
            edge for edge in graph["edges"]
            if edge["target"] in selected_case_set and edge["relation"] == "EXEMPLIFIED_BY"
        )
    # An exact ReasLab detail match is already the most useful case.  Do not
    # dilute it with an unrelated generic example merely to fill max_cases.
    has_exact_detail_case = any(
        by_id.get(case_id, {}).get("payload", {}).get("status") == "detail_verified"
        for case_id in selected_case_ids
    )
    remaining_case_slots = 0 if has_exact_detail_case else max(0, max_cases - len(selected_case_ids))
    if attached_cases and remaining_case_slots > 0:
        case_nodes = list(attached_cases.values())
        case_idf = idf_table(case_nodes)
        requested_case_features = {name for name, enabled in features.items() if enabled}
        # A query label such as ``DARP`` or ``MDVRP`` may not spell out every
        # constraint.  Use the winning profile's declared signature as a
        # second case-ranking target, so a pickup-delivery/time-window example
        # beats a generic VRP card inherited through the shared CVRP model.
        profile_features: set[str] = set()
        for profile_id in seed_ids:
            profile_features.update(node_feature_support(by_id[profile_id], graph.get("feature_catalog", {})))
        scored_cases = [
            (node, score_variant(query, query_tokens, features, node, case_idf, graph.get("feature_catalog", {})))
            for node in case_nodes
        ]
        # Prefer cases that cover the query's hard constraints before using
        # generic token overlap. This keeps a pickup-and-delivery example
        # ahead of a merely time-window-compatible routing example.
        ranked_cases = [item for _, item in sorted(
            scored_cases,
            key=lambda pair: (
                -len(node_feature_support(pair[0], graph.get("feature_catalog", {})) & profile_features & HARD_FEATURES),
                -len(node_feature_support(pair[0], graph.get("feature_catalog", {})) & requested_case_features & HARD_FEATURES),
                -len(node_feature_support(pair[0], graph.get("feature_catalog", {})) & requested_case_features),
                -pair[1]["score"],
                pair[1]["node_id"],
            ),
        )][:remaining_case_slots]
        selected_case_ids.extend(item["node_id"] for item in ranked_cases if item["node_id"] not in selected_case_ids)
        selected_ids.update(selected_case_ids)
        selected_case_set = set(selected_case_ids)
        selected_edges.extend(edge for edge in attached_case_edges if edge["target"] in selected_case_set)

    # Extension nodes are selected by explicit feature matches, then attached
    # only when the chosen variant advertises support for that extension.
    selected_variants = set(seed_ids)
    for extension in (node for node in graph["nodes"] if node.get("type") == "extension"):
        retrieval = extension.get("retrieval", {})
        feature = retrieval.get("feature")
        if not feature or features.get(feature) is not True:
            continue
        if any(
            edge["source"] in selected_variants
            and edge["target"] == extension["id"]
            and edge["relation"] == "SUPPORTS_EXTENSION"
            for edge in graph["edges"]
        ):
            selected_ids.add(extension["id"])
            selected_edges.extend(
                edge for edge in graph["edges"]
                if edge["source"] in selected_variants
                and edge["target"] == extension["id"]
                and edge["relation"] == "SUPPORTS_EXTENSION"
            )
    for error_id in [node_id for node_id in selected_ids if by_id[node_id].get("type") in {"typical_error", "error"}]:
        for edge in outgoing.get(error_id, []):
            if edge["relation"] == "FIXED_BY":
                selected_ids.add(edge["target"])
                selected_edges.append(edge)

    selected_edges = list({
        (edge["source"], edge["relation"], edge["target"]): edge
        for edge in selected_edges
    }.values())
    selected = [project_node(by_id[node_id]) for node_id in selected_ids]
    selected.sort(key=lambda node: (TYPE_ORDER.get(node["type"], 99), node["id"]))
    context = render_context(selected)
    if len(context) > max_context_chars:
        # Keep explicitly selected official/workflow cases in the bundle even
        # when large source excerpts consume most of the context budget.
        required_case_ids = set(selected_case_ids)
        priority = [node for node in selected if node["id"] in required_case_ids]
        remainder = [node for node in selected if node["id"] not in required_case_ids]
        kept: list[dict[str, Any]] = []
        for node in priority + remainder:
            if len(render_context(kept + [node])) <= max_context_chars:
                kept.append(node)
        selected = kept
        selected_ids = {node["id"] for node in selected}
        selected_edges = list({
            (edge["source"], edge["relation"], edge["target"]): edge
            for edge in selected_edges
            if edge["source"] in selected_ids and edge["target"] in selected_ids
        }.values())
        context = render_context(selected)

    # Network is deliberately the last resort.  Local graph retrieval remains
    # deterministic and fast; only an insufficient local result can trigger a
    # source-allowlisted lookup.  Online nodes are advisory and carry explicit
    # provenance/status so they cannot be mistaken for reproduced solutions.
    online_result: dict[str, Any] = {"cases": [], "source": "disabled", "cache_hit": False, "errors": []}
    if network_fallback and local_status == "insufficient":
        online_result = online_case_fallback(
            query, features, cache_path=cache_path, timeout=max(0.5, min(network_timeout, 20.0)), max_cases=max_cases,
            catalog=graph.get("feature_catalog", {}),
        )
        online_cases = online_result.get("cases", [])
        if online_cases:
            selected_case_ids = [case["id"] for case in online_cases]
            online_nodes = [project_node(case) for case in online_cases]
            selected = [*selected, *online_nodes]
            selected.sort(key=lambda node: (TYPE_ORDER.get(node["type"], 99), node["id"]))
            context = render_context(selected)
            if len(context) > max_context_chars:
                selected = [node for node in selected if node["id"] in set(selected_case_ids)]
                context = render_context(selected)[:max_context_chars]
            status = "sufficient_online"

    graph_chars = len(json.dumps(graph, ensure_ascii=False, separators=(",", ":")))
    blueprint_ids = [
        node["id"] for node in selected
        if isinstance(node.get("payload"), dict) and node["payload"].get("blueprint")
    ]
    return {
        "schema_version": "1.0",
        "graph": {"id": graph.get("graph_id"), "version": graph.get("graph_version"), "domain": graph.get("domain")},
        "retrieval": {
            "status": status,
            "local_status": local_status,
            "selected_variant_ids": seed_ids,
            "selected_case_ids": selected_case_ids,
            "score_margin": round(margin, 4) if math.isfinite(margin) else None,
            "domain_signal_hits": domain_hits,
            "inferred_features": features,
            "feature_evidence": feature_evidence,
            "fast_path": {
                "available": bool(blueprint_ids),
                "blueprint_ids": blueprint_ids,
                "instruction": "instantiate_selected_blueprint" if blueprint_ids else "derive_from_references",
            },
            "candidates": candidates,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "network_fallback": online_result,
        },
        "selected_nodes": selected,
        "selected_edges": selected_edges,
        "knowledge_refs": [node["id"] for node in selected],
        "context": context,
        "metrics": {
            "graph_chars": graph_chars,
            "context_chars": len(context),
            "context_reduction_percent": round(100 * (1 - len(context) / graph_chars), 2) if graph_chars else 0.0,
        },
        "advisory_notice": "Knowledge is advisory; Problem.md and observed case data remain authoritative.",
    }


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            json.dump(value, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    args = parse_args()
    query = args.query
    if args.problem_file is not None:
        query = args.problem_file.read_text(encoding="utf-8")
    assert query is not None
    cache_path = args.cache
    if cache_path is None and args.output is not None:
        cache_path = args.output.parent / ONLINE_CACHE_NAME
    result = retrieve(
        load_graph(args.graph), query, top_k=args.top_k,
        max_cases=args.max_cases, max_context_chars=args.max_context_chars,
        network_fallback=args.network_fallback, network_timeout=args.network_timeout,
        cache_path=cache_path,
    )
    if args.output:
        atomic_write_json(args.output, result)
        printed = {
            "output": str(args.output),
            "status": result["retrieval"]["status"],
            "selected_variant_ids": result["retrieval"]["selected_variant_ids"],
            "knowledge_refs": result["knowledge_refs"],
            "metrics": result["metrics"],
        }
    else:
        printed = result
    print(json.dumps(printed, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
