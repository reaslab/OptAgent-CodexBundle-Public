# Final Report: Synthetic Dial-A-Ride Problem

## Public audit status

**PASS - optimal solution independently verified.**

The deterministic four-request DARP instance was solved as a MILP. The preferred Gurobi backend was unavailable, so the configured fallback `pulp`/CBC backend was used. CBC returned status `optimal` with zero MIP gap and objective `35.388533`.

## Instance and interpretation

The published data describe two identical vehicles (`V1`, `V2`), each with capacity 2, starting and ending at depot `D0`, and four passenger requests:

| Request | Pickup | Delivery | Demand | Max ride time |
|---|---|---|---:|---:|
| R1 | P1 | Q1 | 1 | 80 |
| R2 | P2 | Q2 | 1 | 80 |
| R3 | P3 | Q3 | 1 | 80 |
| R4 | P4 | Q4 | 1 | 80 |

Travel time is Euclidean distance between the deterministic node coordinates. Service takes 5 minutes at each customer node, waiting is allowed, and service starts must respect the node time windows. The complete source basis is the synthetic snapshot in `code/data/`, recorded in `code/data/manifest.json` with `authenticity: synthetic` and `randomness_used: false`.

## Formulation

The model uses binary directed-arc variables, vehicle activation variables, request-to-vehicle assignment variables, continuous service-start times and passenger loads, and integer visit-order variables. Constraints enforce:

- exactly-once service of every pickup and delivery;
- same-vehicle pickup/delivery pairing and pickup precedence;
- one depot departure and return per active vehicle;
- route flow conservation and MTZ subtour elimination;
- service-start time windows and time propagation;
- capacity propagation (capacity 2) and empty return;
- maximum ride time measured from pickup-service completion to delivery-service start.

The objective minimizes total travel time over selected route arcs.

## Published route and metrics

The optimal route uses only `V1`; `V2` is unused:

```text
V1: D0 -> P1 -> P2 -> Q2 -> P4 -> Q4 -> P3 -> Q3 -> Q1 -> D0
V2: D0
```

Service starts and post-service loads for `V1` are:

| Stop | Service start | Load after service |
|---|---:|---:|
| P1 | 2.236068 | 1 |
| P2 | 11.359174 | 2 |
| Q2 | 19.521451 | 1 |
| P4 | 30.000000 | 2 |
| Q4 | 38.605551 | 1 |
| P3 | 50.885661 | 2 |
| Q3 | 58.714088 | 1 |
| Q1 | 66.876366 | 0 |

The resulting ride times are R1 `59.640298`, R2 `3.162277`, R3 `2.828427`, and R4 `3.605551` minutes, all below the 80-minute limit. The route contains 9 travel arcs and sums to `35.388533`.

## Graph-assisted knowledge

The local graph was insufficient for a direct DARP case, so the configured allowlisted online fallback fetched two official Google OR-Tools examples:

1. [VRP time windows per vehicles](https://github.com/google/or-tools/blob/stable/ortools/constraint_solver/samples/vrp_time_windows_per_vehicles.py) - time-window and vehicle-horizon pattern.
2. [VRP pickup delivery LIFO](https://github.com/google/or-tools/blob/stable/ortools/constraint_solver/samples/vrp_pickup_delivery_lifo.py) - paired pickup/delivery and vehicle-consistency pattern.

The retrieval bundle marks both as `online_source_verified_not_reproduced`: their source code was fetched for modeling guidance, while this report's objective and feasibility come solely from the synthetic data and published MILP artifacts.

## Independent verification

The verifier reports `passed` with full coverage and no violations:

- route topology: all 8 customer nodes exactly once; both routes begin and end at `D0`; no subtours;
- request pairing: all four pickup/delivery pairs served once on `V1`;
- time windows: every published service start is within its node window;
- capacity: recomputed load never exceeds 2 and returns to 0;
- ride time: all four limits hold;
- objective: independently recomputed as `35.388533` over the 9 published arcs;
- reproducibility: recorded entry-point and data-snapshot SHA-256 hashes match; `--check-inputs` and `--dry-run` both pass.

The generic structural redundancy audit issued an **advisory** parser hint about `load` and `u`; the case-specific verifier confirms that load propagation and MTZ order constraints are present and satisfied in the public implementation. This advisory does not change the PASS result.

## Artifacts

- Problem and data: `Problem.md`, `code/data/manifest.json`, `code/data/{vehicles,requests,nodes,travel_times}.json`
- Model and runner: `code/model.lp`, `code/model.mps`, `code/run.py`
- Solution: `outputs/route_solution.json`, `outputs/service_times_loads.json`
- Machine-readable verification records were generated alongside this case and the public route and service-time outputs.
