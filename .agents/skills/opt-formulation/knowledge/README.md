# Optimization OptGraph

The default `optimization_optgraph.json` is a small root that includes the
legacy conversion, teaching cases, official cases/catalog, ReasLab's template
catalog, the facility/production/scheduling/network slices, and
`compound_profiles_optgraph.json` and `coverage_profiles_optgraph.json`. The first slice is a structured
conversion of the original OptAgent formulation knowledge base:

- `reference/problem-taxonomy.md`
- `reference/standard-formulations.md`
- `reference/common-pitfalls.md`
- `reference/linearization-tricks.md`
- `reference/variable-domain-guide.md`

The second slice contains 67 compact, deterministic teaching cases: one base
case for each existing canonical formulation plus boundary and business-rule
variants. They are marked
`expert_curated_synthetic`; they are small regression/teaching instances, not
claims about public benchmark data.
The case slice intentionally references model IDs from the legacy slice and is
therefore loaded through the default root.

The official slice adds ten cases copied from complete Google OR-Tools and
Gurobi examples, with source and code URLs, data contracts, solver recipes,
validation checks, and known outputs where the example was reproduced locally.
The added cases cover capacity-constrained assignment, team assignment,
multiple knapsack, maintenance-aware job shop, pickup-and-delivery routing, and
resource-constrained routing. No locally invented workflow is counted as a
case.

The catalog slice adds 75 more official OR-Tools source cases. It covers the
remaining routing, TSP, assignment, packing, scheduling, CP-SAT, and linear
examples in the stable official source tree. Each node stores its exact
`code_url`, related formulation, constraint/data summary, solve recipe, and an
explicit `source_verified_not_reproduced_in_catalog` status for output values
that were not rerun in this pass.

The source Markdown remains authoritative. Converted nodes carry source-file,
source-heading, and bounded source-excerpt provenance.

The graph keeps one canonical `formulation` node per standard model. It is
linked to its `problem_family` with `BELONGS_TO`, reusable
`formulation_pattern` nodes with `USES_PATTERN`, `variable_rule` nodes with
`USES_VARIABLE_RULE`, and its original Markdown section with `DERIVED_FROM`.
`retrieval_profile` nodes remain as compatibility search profiles, not
duplicate model knowledge.

The domain slices add structured feature catalogs and reusable variants for
facility location, production/inventory, scheduling/routing, and network
design. The compound slice adds composite profiles for DARP, integrated
location-routing, grid line-labeling, and stochastic/robust planning. Composite profiles advertise a
feature signature, compose existing formulations, and reuse source-backed
cases; they do not claim a new benchmark result.

The iteration slice (`iteration_profiles_optgraph.json`) adds reusable
profiles and blueprints for high-frequency variants found in the coverage
audit: multi-depot and prize-collecting routing, general pickup/delivery,
car sequencing, K-means clustering, assembly-line balancing, RCPSP, steel-mill
slab design, multi-commodity network flow, revenue management, open-shop and
batch scheduling, backhauls, clustered routing, transshipment facilities,
curve fitting, and bounded nonlinear/structural design. Routing and
scheduling case links point to existing official OR-Tools/Gurobi source cards;
the blueprints state which base-case pieces still need adaptation.

The coverage slice adds general (not website-specific) profiles for Max-Cut,
QAP, set multi-cover, smallest enclosing circle, Social Golfer, pooling, unit
commitment, flexible job shop, MRCPSP, split-delivery and arc routing, CVRP,
refinery planning, CFLP, EVRPTW, crew scheduling, inventory-routing, and
bounded portfolio selection. Its new case cards point to reachable Google
OR-Tools or Gurobi repository files and are explicitly marked
`source_verified_not_reproduced`; a URL check is not a solver reproduction.

Taxonomy rules/details and the pitfall severity guide are also addressable
nodes, so the conversion does not discard the original classification guidance
that is outside the standard-formulation headings.

The root graph uses relative `includes`, so the converted slice can later be
supplemented without rewriting existing case artifacts. Stable node IDs and
typed edges preserve provenance across the merged view.

Retrieval is deterministic and dependency-free: infer a feature signature,
rank candidates by feature coverage and missing-feature penalties, expand the
selected model's semantic neighborhood, then follow `FIXED_BY` edges so every
selected error carries a repair. Compound profiles reuse several existing
formulations and official cases. When a selected formulation carries a
`blueprint`, the bundle advertises a fast modeling path so the formulator can
bind data directly instead of rebuilding a standard model from prose. The
complete bounded bundle is written to the case while stdout stays compact.

Regenerate the converted graph after editing the original references:

```powershell
python .agents/skills/opt-formulation/tools/convert_reference_kb.py
```

Inside a case:

```powershell
python .agents/skills/opt-formulation/tools/optgraph_retrieve.py `
  --problem-file cases/{case_id}/Problem.md `
  --output .optagent/cases/{case_id}/knowledge/retrieval_bundle.json
```

Case expansion is bounded by default: the retriever returns at most two
`case` nodes attached to the selected formulation. Use `--max-cases N` for a
deliberate larger sample; the full case slice remains available in the graph.

To query a specific slice or a test prompt, use `--query` and optionally
`--graph .agents/skills/opt-formulation/knowledge/{slice}.json`.

The graph is advisory. `Problem.md` and observed data remain authoritative.
Seed cases are marked `expert_curated_synthetic`; they are reusable modeling
patterns, not empirical benchmark claims.

Current merged inventory: 792 nodes, 960 edges, 194 cases (70 expert-curated
synthetic teaching cases and 124 official examples), 62 feature groups, and 72
variant/retrieval profiles. The latest iteration adds model blueprints and
source links without inventing benchmark objective values. The latest official
additions are flexible job shop, shift scheduling, sequence-dependent
single-machine setup scheduling, CVRP with reload stations, VRPTW with driver
breaks, and optional dropped-node VRP. Official local reproductions recorded in
the graph include capacity VRP (6208), time-window VRP (objective 71, route
time 82), FT06 job shop (makespan 55), task-size assignment (326), team
assignment (250), multiple knapsack (value 395), maintenance job shop (15),
pickup-and-delivery VRP (distance 6916), resource VRP (route time 98), flexible
job shop (makespan 6), shift scheduling (penalty 38), single-machine setup
scheduling (makespan 112605), reload CVRP (distance 7464), VRPTW breaks
(distance 6596), dropped-node VRP (objective 7548, distance 5548), grouped
assignment (239), nurse scheduling (five enumerated solutions), bus-driver
scheduling, global-span VRP (maximum route 1712), individual-start/end VRP
(maximum route 1118), mandatory-break VRP (total time 578), time-limited VRP
(objective 524), and distance-matrix TSP (distance 4384). The
Gurobi facility example is source-verified but requires a licensed
installation, so no run-specific objective is invented.

The official slice follows GraphRAG guidance: entities are nodes, typed
relationships retain formulation/stage dependencies, retrieval starts from
specific signals and follows bounded multi-hop edges, and every external
example keeps provenance URLs. This keeps the prompt small while preserving
traceable source context.

## Local-first online fallback

The retriever only consults the allow-listed source indexes in the root graph
when local retrieval is `insufficient`, including a compound-feature mismatch.
The current indexes are the Google OR-Tools stable source tree and the Gurobi
`modeling-examples` repository. It fetches a bounded file index, ranks paths
against detected features, and stores compact case nodes beside the retrieval
bundle in `online_case_cache.json`. Every online node includes a repository URL,
raw source URL, retrieval timestamp, and the explicit status
`online_source_verified_not_reproduced`; no objective or feasibility claim is
inferred. A subsequent query with the same normalized text uses the cache and
does not make another network request. Pass `--no-network-fallback` for an
offline-only run.
## ReasLab coverage benchmark

`reaslab_template_catalog_optgraph.json` is a catalog/benchmark slice only. It
records the 173 public template names and URLs used to measure coverage; it is
not executable or authoritative case knowledge. Retrieval uses its names as
coverage signals while the linked formulation and feature nodes remain the
modeling source.
