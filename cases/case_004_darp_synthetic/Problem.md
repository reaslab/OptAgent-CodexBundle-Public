# Synthetic Dial-A-Ride Problem (DARP)

Solve a small but nontrivial Dial-A-Ride Problem using the local OptAgent.
All four passenger requests must be served exactly once by a fleet of two
identical vehicles. Each vehicle starts at and returns to the depot, has
capacity 2, and may carry at most two passengers simultaneously.

## Deterministic synthetic data

Use the following tables as the complete generated instance. Travel time and
travel distance are the Euclidean distance between node coordinates. Service
time is 5 minutes at every pickup and delivery node and 0 at the depot. The
planning horizon is 0..240 minutes. Waiting is allowed when a vehicle arrives
before a node's time window.

### Vehicles

| vehicle_id | capacity | start_depot | end_depot |
|---|---:|---|---|
| V1 | 2 | D0 | D0 |
| V2 | 2 | D0 | D0 |

### Requests

| request_id | pickup_node | delivery_node | demand | max_ride_time |
|---|---|---|---:|---:|
| R1 | P1 | Q1 | 1 | 80 |
| R2 | P2 | Q2 | 1 | 80 |
| R3 | P3 | Q3 | 1 | 80 |
| R4 | P4 | Q4 | 1 | 80 |

### Nodes and time windows

| node_id | role | request_id | x | y | service_minutes | earliest | latest |
|---|---|---|---:|---:|---:|---:|---:|
| D0 | depot | - | 0 | 0 | 0 | 0 | 240 |
| P1 | pickup | R1 | 2 | 1 | 5 | 0 | 40 |
| Q1 | delivery | R1 | 5 | 2 | 5 | 0 | 100 |
| P2 | pickup | R2 | 1 | 5 | 5 | 0 | 50 |
| Q2 | delivery | R2 | 2 | 8 | 5 | 0 | 120 |
| P3 | pickup | R3 | 6 | 1 | 5 | 20 | 80 |
| Q3 | delivery | R3 | 8 | 3 | 5 | 20 | 160 |
| P4 | pickup | R4 | 5 | 6 | 5 | 30 | 90 |
| Q4 | delivery | R4 | 8 | 8 | 5 | 30 | 180 |

## Required model

Minimize total vehicle travel time while satisfying:

- every request has exactly one pickup and one delivery;
- pickup precedes its paired delivery;
- pickup and delivery of a request use the same vehicle;
- vehicle routes start and end at D0;
- vehicle capacity is never exceeded;
- service starts occur inside every node time window;
- each passenger's delivery time minus pickup time is at most 80 minutes;
- subtours disconnected from the depot are forbidden.

Use a MILP or routing formulation with explicit binary arc/assignment and
continuous service-time/load variables. The data are synthetic but
deterministic and should be recorded with their source basis and no random
seed requirement.
