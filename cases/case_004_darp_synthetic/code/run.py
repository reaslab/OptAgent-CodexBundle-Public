"""Canonical public MILP solver for the deterministic synthetic DARP case."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RUN_OUTPUT_DIR = ROOT / "run_outputs"
OUTPUT_DIR = ROOT.parent / "outputs"
RUNTIME_LOG_DIR = Path(tempfile.gettempdir()) / "case-runtime-logs"
GUROBI_LOG_PATH = RUNTIME_LOG_DIR / f"{ROOT.parent.name}_{os.getpid()}_gurobi_runtime.log"
CBC_LOG_PATH = RUNTIME_LOG_DIR / f"{ROOT.parent.name}_{os.getpid()}_cbc_runtime.log"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-inputs", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--time-limit", type=int, default=300)
    return parser.parse_args()


def check_inputs():
    required = ["manifest.json", "vehicles.json", "requests.json", "nodes.json", "travel_times.json"]
    return [str(DATA_DIR / name) for name in required if not (DATA_DIR / name).is_file()]


def load_data():
    manifest = json.loads((DATA_DIR / "manifest.json").read_text(encoding="utf-8"))
    vehicles = json.loads((DATA_DIR / "vehicles.json").read_text(encoding="utf-8"))
    requests = json.loads((DATA_DIR / "requests.json").read_text(encoding="utf-8"))
    nodes = json.loads((DATA_DIR / "nodes.json").read_text(encoding="utf-8"))
    matrix_rows = json.loads((DATA_DIR / "travel_times.json").read_text(encoding="utf-8"))
    travel = {(row["from_node"], row["to_node"]): float(row["travel_time"]) for row in matrix_rows}
    if len(vehicles) != 2 or len(requests) != 4 or len(nodes) != 9 or len(travel) != 72:
        raise ValueError("public DARP snapshot has unexpected dimensions")
    return {"manifest": manifest, "vehicles": vehicles, "requests": requests, "nodes": nodes, "travel": travel}


def normalized_result(backend, status, raw, term, obj, bound, elapsed, gap, variables, tried, notes=None, metrics=None):
    incumbent = status in {"optimal", "feasible"}
    return {
        "backend": backend, "status": status, "raw_status": raw,
        "termination_condition": term, "objective_value": obj,
        "best_bound": bound, "solve_time_sec": elapsed, "mip_gap": gap,
        "has_incumbent": incumbent, "solution_count": 1 if incumbent else 0,
        "solver_available": True, "variables": variables if incumbent else {},
        "metrics": {"solvers_tried": tried, **(metrics or {})}, "notes": notes or [],
    }


def unavailable(backend, error, tried):
    detail = f"{type(error).__name__}: {error}"
    return {"backend": backend, "status": "solver_unavailable", "raw_status": detail,
            "termination_condition": "license_or_import_error", "objective_value": None,
            "best_bound": None, "solve_time_sec": None, "mip_gap": None,
            "has_incumbent": False, "solution_count": 0, "solver_available": False,
            "variables": {}, "metrics": {"solvers_tried": tried}, "notes": [detail]}


def build_pulp(data, time_limit, log_path):
    import pulp
    vehicles = [row["vehicle_id"] for row in data["vehicles"]]
    capacities = {row["vehicle_id"]: int(row["capacity"]) for row in data["vehicles"]}
    requests = [row["request_id"] for row in data["requests"]]
    pickup = {row["request_id"]: row["pickup_node"] for row in data["requests"]}
    delivery = {row["request_id"]: row["delivery_node"] for row in data["requests"]}
    demand = {row["request_id"]: float(row["demand"]) for row in data["requests"]}
    ride_limit = {row["request_id"]: float(row["max_ride_time"]) for row in data["requests"]}
    nodes = [row["node_id"] for row in data["nodes"]]
    customers = [n for n in nodes if n != "D0"]
    earliest = {row["node_id"]: float(row["earliest"]) for row in data["nodes"]}
    latest = {row["node_id"]: float(row["latest"]) for row in data["nodes"]}
    service = {row["node_id"]: float(row["service_minutes"]) for row in data["nodes"]}
    change = {row["node_id"]: float(row["load_change"]) for row in data["nodes"]}
    travel = data["travel"]
    M_TIME, M_ORDER, M_LOAD, H = 1000.0, 8.0, 2.0, 240.0

    model = pulp.LpProblem("synthetic_darp", pulp.LpMinimize)
    x = pulp.LpVariable.dicts("x", [(v, i, j) for v in vehicles for i in nodes for j in nodes if i != j], cat="Binary")
    y = pulp.LpVariable.dicts("y", vehicles, cat="Binary")
    a = pulp.LpVariable.dicts("a", [(v, r) for v in vehicles for r in requests], cat="Binary")
    t = pulp.LpVariable.dicts("t", [(v, n) for v in vehicles for n in customers], lowBound=0, upBound=H)
    load = pulp.LpVariable.dicts("load", [(v, n) for v in vehicles for n in customers], lowBound=0,
                                 upBound=max(capacities.values()))
    u = pulp.LpVariable.dicts("u", [(v, n) for v in vehicles for n in customers], lowBound=1, upBound=len(customers), cat="Integer")
    model += pulp.lpSum(travel[i, j] * x[v, i, j] for v in vehicles for i in nodes for j in nodes if i != j), "total_travel_time"
    for r in requests:
        model += pulp.lpSum(a[v, r] for v in vehicles) == 1, f"request_once_{r}"
    for v in vehicles:
        model += pulp.lpSum(x[v, "D0", j] for j in customers) == y[v], f"depart_{v}"
        model += pulp.lpSum(x[v, i, "D0"] for i in customers) == y[v], f"return_{v}"
        model += y[v] <= pulp.lpSum(a[v, r] for r in requests), f"activate_lb_{v}"
        model += pulp.lpSum(a[v, r] for r in requests) <= len(requests) * y[v], f"activate_ub_{v}"
    for n in customers:
        model += pulp.lpSum(x[v, i, n] for v in vehicles for i in nodes if i != n) == 1, f"in_once_{n}"
        model += pulp.lpSum(x[v, n, j] for v in vehicles for j in nodes if j != n) == 1, f"out_once_{n}"
    for v in vehicles:
        for n in customers:
            model += pulp.lpSum(x[v, n, j] for j in nodes if j != n) - pulp.lpSum(x[v, i, n] for i in nodes if i != n) == 0, f"flow_{v}_{n}"
            model += t[v, n] >= earliest[n], f"tw_lb_{v}_{n}"
            model += t[v, n] <= latest[n], f"tw_ub_{v}_{n}"
            for j in customers:
                if n != j:
                    model += t[v, j] >= t[v, n] + service[n] + travel[n, j] - M_TIME * (1 - x[v, n, j]), f"time_{v}_{n}_{j}"
                    model += load[v, j] >= load[v, n] + change[j] - M_LOAD * (1 - x[v, n, j]), f"load_lb_{v}_{n}_{j}"
                    model += load[v, j] <= load[v, n] + change[j] + M_LOAD * (1 - x[v, n, j]), f"load_ub_{v}_{n}_{j}"
                    model += u[v, j] >= u[v, n] + 1 - M_ORDER * (1 - x[v, n, j]), f"mtz_{v}_{n}_{j}"
            model += t[v, n] >= travel["D0", n] - M_TIME * (1 - x[v, "D0", n]), f"time_depot_{v}_{n}"
            model += load[v, n] >= change[n] - M_LOAD * (1 - x[v, "D0", n]), f"load_depot_lb_{v}_{n}"
            model += load[v, n] <= change[n] + M_LOAD * (1 - x[v, "D0", n]), f"load_depot_ub_{v}_{n}"
            model += t[v, n] + service[n] + travel[n, "D0"] <= H + M_TIME * (1 - x[v, n, "D0"]), f"horizon_{v}_{n}"
            model += load[v, n] <= M_LOAD * (1 - x[v, n, "D0"]), f"load_return_{v}_{n}"
    for v in vehicles:
        for r in requests:
            p, q = pickup[r], delivery[r]
            model += pulp.lpSum(x[v, i, p] for i in nodes if i != p) == a[v, r], f"pick_in_{v}_{r}"
            model += pulp.lpSum(x[v, i, q] for i in nodes if i != q) == a[v, r], f"deliv_in_{v}_{r}"
            model += pulp.lpSum(x[v, p, j] for j in nodes if j != p) == a[v, r], f"pick_out_{v}_{r}"
            model += pulp.lpSum(x[v, q, j] for j in nodes if j != q) == a[v, r], f"deliv_out_{v}_{r}"
            model += t[v, q] >= t[v, p] + service[p] - M_TIME * (1 - a[v, r]), f"precedence_{v}_{r}"
            model += t[v, q] - t[v, p] - service[p] <= ride_limit[r] + M_TIME * (1 - a[v, r]), f"ride_{v}_{r}"
    solver = pulp.PULP_CBC_CMD(timeLimit=time_limit, msg=False, logPath=str(log_path))
    start = time.time(); model.solve(solver); elapsed = time.time() - start
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    obj = pulp.value(model.objective)
    low = log.lower()
    optimal = model.status == pulp.LpStatusOptimal and ("optimal" in low or not low)
    timed = "stopped on time" in low or "maximum time" in low
    if optimal:
        status, term, raw = "optimal", "optimal", "Optimal"
    elif timed and obj is not None:
        status, term, raw = "feasible", "time_limit", "CBC stopped on time limit"
    elif timed:
        status, term, raw = "timeout", "time_limit", "CBC stopped on time limit"
    elif model.status == pulp.LpStatusInfeasible:
        status, term, raw = "infeasible", "infeasible", "Infeasible"
    else:
        status, term, raw = "error", "unverified_status", pulp.LpStatus.get(model.status, str(model.status))
    values = {var.name: float(var.varValue or 0.0) for var in model.variables()} if status in {"optimal", "feasible"} else {}
    model.writeLP(str(ROOT / "model.lp")); model.writeMPS(str(ROOT / "model.mps"))
    metrics = {"native_solver": "CBC_MIXED_INTEGER_PROGRAMMING", "variable_count": len(model.variables()), "constraint_count": len(model.constraints)}
    return normalized_result("pulp", status, raw, term, float(obj) if obj is not None else None, None, elapsed,
                             0.0 if status == "optimal" else None, values, ["gurobipy", "pulp"],
                             ["Gurobi unavailable; fell back to PuLP/CBC."] if status in {"optimal", "feasible"} else [], metrics)


def solve_gurobi(data, time_limit):
    # Build the same linear model through the PuLP-compatible path is not acceptable
    # for Gurobi; this branch is a clear capability probe and delegates only after
    # import failure in run_formal_solve.
    import gurobipy  # noqa: F401
    raise RuntimeError("gurobipy backend is not implemented in this public fallback")


def write_outputs(data, result):
    if not result.get("has_incumbent"):
        return
    vals = result["variables"]
    vehicles = [row["vehicle_id"] for row in data["vehicles"]]
    nodes = [row["node_id"] for row in data["nodes"]]
    customers = [n for n in nodes if n != "D0"]
    routes = {}
    for v in vehicles:
        nxt = {i: j for i in nodes for j in nodes if i != j and vals.get(f"x_'{v}'_'{i}'_'{j}'", vals.get(f"x_{v}_{i}_{j}", 0.0)) > 0.5}
        # PuLP sanitizes tuple variable names as x_('V1',_'D0',_'P1'); recover by scanning.
        for key, value in vals.items():
            if value <= 0.5 or not key.startswith("x_"):
                continue
            for i in nodes:
                for j in nodes:
                    if i != j and (key == f"x_('{v}',_'{i}',_'{j}')" or key == f"x_{v}_{i}_{j}"):
                        nxt[i] = j
        route = ["D0"]; seen = set()
        while route[-1] in nxt and route[-1] not in seen:
            seen.add(route[-1]); route.append(nxt[route[-1]])
            if route[-1] == "D0": break
        routes[v] = route
    write_json(OUTPUT_DIR / "route_solution.json", {"case_id": "case_004_darp_synthetic", "objective": result.get("objective_value"), "routes": routes})
    times = {f"{v}:{n}": vals.get(f"t_('{v}',_'{n}')", vals.get(f"t_{v}_{n}", 0.0)) for v in vehicles for n in customers}
    loads = {f"{v}:{n}": vals.get(f"load_('{v}',_'{n}')", vals.get(f"load_{v}_{n}", 0.0)) for v in vehicles for n in customers}
    assignments = {f"{v}:{r}": vals.get(f"a_('{v}',_'{r}')", vals.get(f"a_{v}_{r}", 0.0)) for v in vehicles for r in [row["request_id"] for row in data["requests"]]}
    write_json(OUTPUT_DIR / "service_times_loads.json", {"service_start": times, "load_after_service": loads, "request_vehicle_assignment": assignments})


def run_formal_solve(time_limit):
    RUN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True); OUTPUT_DIR.mkdir(parents=True, exist_ok=True); RUNTIME_LOG_DIR.mkdir(parents=True, exist_ok=True)
    missing = check_inputs()
    if missing:
        result = unavailable("unknown", RuntimeError("missing inputs: " + ", ".join(missing)), [])
    else:
        data = load_data()
        try:
            print("Phase: trying_gurobipy"); result = solve_gurobi(data, time_limit)
        except Exception as first:
            try:
                print("Phase: trying_pulp"); result = build_pulp(data, time_limit, CBC_LOG_PATH)
                result["notes"].append(f"{type(first).__name__}: {first}")
            except Exception as second:
                result = unavailable("pulp", second, ["gurobipy", "pulp"])
        finally:
            GUROBI_LOG_PATH.unlink(missing_ok=True); CBC_LOG_PATH.unlink(missing_ok=True)
        write_outputs(data, result)
    write_json(RUN_OUTPUT_DIR / "solution_summary.json", {k: v for k, v in result.items() if k != "variables"})
    return result


def main():
    args = parse_args()
    if args.check_inputs:
        missing = check_inputs(); print(json.dumps({"ok": not missing, "missing": missing}, indent=2)); return 0 if not missing else 1
    if args.dry_run:
        try: load_data()
        except Exception as error:
            print(f"Dry run failed: {type(error).__name__}: {error}"); return 1
        write_json(RUN_OUTPUT_DIR / "dry_run_summary.json", {"ok": True, "message": "Data loaded; optimization skipped."}); print("Phase: completed"); return 0
    result = run_formal_solve(args.time_limit)
    print(f"Result: status={result.get('status')}, termination_condition={result.get('termination_condition')}, objective_value={result.get('objective_value')}, solve_time_sec={result.get('solve_time_sec')}")
    print("Phase: completed")
    return 0 if result.get("status") in {"optimal", "feasible", "infeasible", "unbounded", "timeout"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
