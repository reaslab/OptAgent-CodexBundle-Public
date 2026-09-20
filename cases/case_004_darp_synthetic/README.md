# Synthetic Dial-A-Ride Problem (DARP)

## Public status

- Case ID: `case_004_darp_synthetic`
- Instance type: deterministic synthetic data; no random seed or external benchmark is used.
- Model class: mixed-integer linear programming (MILP).
- Solver result: **optimal** (`pulp`/CBC fallback; the preferred `gurobipy` backend was unavailable).
- Objective: **35.388533** total Euclidean travel time.
- Independent verification: **passed**.

## Instance

The instance contains four paired passenger requests (`R1`--`R4`), eight customer nodes, one depot (`D0`), and two vehicles (`V1`, `V2`) with capacity 2. Each request must be picked up and delivered by the same vehicle. Customer service takes 5 minutes, each node has an explicit service-start time window, and each request has an 80-minute maximum ride-time limit. Travel time is the Euclidean distance between the published coordinates. The complete authoritative snapshot is in `code/data/` and is described by `code/data/manifest.json`.

## Modeling structure

The public implementation in `code/run.py` uses binary arc variables, vehicle-use flags, request-to-vehicle assignment variables, and continuous service-time and load variables, together with pickup precedence, same-vehicle pairing, capacity propagation, time windows, ride-time limits, depot start/end, and MTZ subtour elimination. The objective sums travel time over all selected arcs.

## Solution

Only `V1` is used:

```text
D0 -> P1 -> P2 -> Q2 -> P4 -> Q4 -> P3 -> Q3 -> Q1 -> D0
```

`V2` remains at `D0`. Service starts, loads, assignments, and the route objective are published in `outputs/route_solution.json` and `outputs/service_times_loads.json`.

## Graph references

The retrieval bundle records two allowlisted official Google OR-Tools examples as structural references:

- [VRP with time windows per vehicles](https://github.com/google/or-tools/blob/stable/ortools/constraint_solver/samples/vrp_time_windows_per_vehicles.py)
- [VRP pickup-delivery LIFO](https://github.com/google/or-tools/blob/stable/ortools/constraint_solver/samples/vrp_pickup_delivery_lifo.py)

These references informed the time-window and paired pickup/delivery patterns. They are source references, not the synthetic instance data, and were not used as an external known-optimal benchmark.

## Verification and reproducibility

The independent verifier reports full coverage and no violations: all eight customer nodes are visited exactly once, each request is paired on one vehicle, routes start/end at `D0`, maximum load is 2, all service starts lie in their windows, all ride-time limits hold, and the objective recomputes to `35.388533`. The recorded entry-point and data-snapshot hashes match; `--check-inputs` and `--dry-run` pass. See `report/final.md` for the public report and the machine-readable verification records maintained with this case.

## Files

- `Problem.md`: deterministic problem statement and data tables.
- `code/data/`: public data snapshot and manifest.
- `code/run.py`, `code/model.lp`, `code/model.mps`: executable entry point and model exports.
- `outputs/`: route, service-time, load, and assignment results.
- `report/final.md`: final public report.
