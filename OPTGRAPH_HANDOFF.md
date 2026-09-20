# Standalone OptAgent Bundle

This directory is the standalone runtime copy of the enhanced local OptAgent.

## Runtime status

- The bundle contains the current OptGraph knowledge base and local retrieval tools.
- `.optagent/registry.json` is intentionally initialized with no active cases.
- There is no pending formulation, solver, verification, or reporting workflow to resume.
- New user problems should start a new case under `.optagent/cases/` and `cases/`.

## Included runtime components

- `.agents/`: bundled skills, schemas, tools, solver templates, and OptGraph data
- `.codex/`: coordinator and native role configuration
- `.optagent/`: fresh runtime state and cache directories
- `agent.yaml`: standalone Agent manifest
- `AGENTS.md`: workspace execution rules

## Knowledge graph

The root graph is `optimization_optgraph.json` (version `2026-09-19.1`). It
includes the legacy reference conversion, core formulation cases, official
source-backed cases and catalog entries, the ReasLab template catalog, four
domain slices, and composite profiles for DARP, CVRPTW, integrated
location-routing, grid line-labeling, and stochastic/robust planning. ReasLab templates are retained as an
external coverage catalog, not as active unfinished cases.

The merged graph currently contains 713 nodes, 864 edges, 181 cases, and 36
feature groups. The retriever ranks feature coverage and missing hard
constraints before generic token overlap. Selected formulations may expose a
`blueprint` fast path for direct data binding and indexed constraint
instantiation.

Do not treat historical paths or case states from another working copy as
current runtime state for this directory.
