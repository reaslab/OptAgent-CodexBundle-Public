"""Self-contained teaching model for shared quantities and complete GDP branches.

All constants are small illustrative data. This is not a benchmark decoder,
Core execution hook or reusable Core API.
"""

from __future__ import annotations

from typing import Any

from pyomo.environ import (
    ConcreteModel,
    Constraint,
    ConstraintList,
    NonNegativeReals,
    Objective,
    Reals,
    Set,
    Var,
    minimize,
    value,
)
from pyomo.gdp import Disjunct, Disjunction


PRODUCTS = ("grade-X", "grade-Y")
BRANCHES: dict[str, dict[str, dict[str, Any]]] = {
    "cell-A": {
        "route-red": {
            "box": {"grade-X": (0.0, 5.0), "grade-Y": (0.0, 4.0)},
            "rows": (("combined", {"grade-X": 1.0, "grade-Y": 1.0}, 7.0),
                     ("mix", {"grade-X": 1.0, "grade-Y": -1.0}, 3.0)),
            "cost": (7.0, {"grade-X": 2.0, "grade-Y": 1.0}),
            "resources": {
                "crew": (8.0, {"grade-X": 0.5, "grade-Y": 0.1}),
                "power": (5.0, {"grade-X": -0.2, "grade-Y": 0.5}),
            },
        },
        "route-blue": {
            "box": {"grade-X": (1.0, 5.0), "grade-Y": (0.0, 3.0)},
            "rows": (("blend", {"grade-X": 1.0, "grade-Y": 2.0}, 9.0),),
            "cost": (11.0, {"grade-X": -1.0, "grade-Y": 3.0}),
            "resources": {
                "crew": (10.0, {"grade-X": -0.2, "grade-Y": 0.6}),
                "power": (6.0, {"grade-X": 0.1, "grade-Y": -0.3}),
            },
        },
    },
    "cell-B": {
        "route-red": {
            "box": {"grade-X": (0.0, 5.0), "grade-Y": (0.0, 4.0)},
            "rows": (("combined", {"grade-X": 1.0, "grade-Y": 1.0}, 6.0),),
            "cost": (5.0, {"grade-X": 1.0, "grade-Y": 2.0}),
            "resources": {
                "crew": (9.0, {"grade-X": 0.2, "grade-Y": -0.1}),
                "power": (4.0, {"grade-X": 0.3, "grade-Y": 0.2}),
            },
        },
        "route-blue": {
            "box": {"grade-X": (0.0, 4.0), "grade-Y": (1.0, 5.0)},
            "rows": (("weighted", {"grade-X": 2.0, "grade-Y": 1.0}, 8.0),
                     ("difference", {"grade-X": -1.0, "grade-Y": 1.0}, 3.0)),
            "cost": (9.0, {"grade-X": 2.0, "grade-Y": -1.0}),
            "resources": {
                "crew": (8.0, {"grade-X": 0.4, "grade-Y": 0.2}),
                "power": (7.0, {"grade-X": -0.1, "grade-Y": 0.5}),
            },
        },
        "route-green": {
            "box": {"grade-X": (0.0, 3.0), "grade-Y": (0.0, 4.0)},
            "rows": (("mix", {"grade-X": 1.0, "grade-Y": -1.0}, 1.0),),
            "cost": (3.0, {"grade-X": -0.5, "grade-Y": 1.0}),
            "resources": {
                "crew": (6.0, {"grade-X": 0.5, "grade-Y": 0.3}),
                "power": (9.0, {"grade-X": 0.2, "grade-Y": -0.1}),
            },
        },
    },
}
DEMAND = {"grade-X": 4.0, "grade-Y": 3.0}
RESOURCE_LIMIT = {"crew": 30.0, "power": 20.0}


def _affine_bounds(constant: float, coefficients: dict[str, float], box: dict[str, tuple[float, float]]) -> tuple[float, float]:
    lo = hi = constant
    for product, (lower, upper) in box.items():
        coefficient = coefficients.get(product, 0.0)
        lo += coefficient * (lower if coefficient >= 0 else upper)
        hi += coefficient * (upper if coefficient >= 0 else lower)
    return lo, hi


def _outer_bounds(unit: str, expression: str, resource: str | None = None) -> tuple[float, float]:
    ranges = []
    for branch in BRANCHES[unit].values():
        constant, coefficients = branch[expression] if resource is None else branch[expression][resource]
        ranges.append(_affine_bounds(constant, coefficients, branch["box"]))
    return min(item[0] for item in ranges), max(item[1] for item in ranges)


def build_model(bundle: Any) -> ConcreteModel:
    """Build and hull-transform a linear GDP; ``bundle`` is unused teaching data."""
    del bundle
    units = tuple(BRANCHES)
    options = tuple((unit, mode) for unit in units for mode in BRANCHES[unit])
    resources = tuple(next(iter(BRANCHES[unit].values()))["resources"] for unit in units)[0].keys()
    model = ConcreteModel(name="shared-quantity-complete-gdp-example")
    model.UNITS = Set(initialize=units, ordered=True)
    model.PRODUCTS = Set(initialize=PRODUCTS, ordered=True)
    model.OPTIONS = Set(dimen=2, initialize=options, ordered=True)
    model.RESOURCES = Set(initialize=resources, ordered=True)
    model.q = Var(
        model.UNITS, model.PRODUCTS, domain=NonNegativeReals,
        bounds=lambda _m, unit, product: (
            min(BRANCHES[unit][mode]["box"][product][0] for mode in BRANCHES[unit]),
            max(BRANCHES[unit][mode]["box"][product][1] for mode in BRANCHES[unit]),
        ),
    )
    model.cost = Var(model.UNITS, domain=Reals, bounds=lambda _m, unit: _outer_bounds(unit, "cost"))
    model.resource = Var(
        model.UNITS, model.RESOURCES, domain=Reals,
        bounds=lambda _m, unit, resource: _outer_bounds(unit, "resources", resource),
    )

    def branch_rule(disjunct: Any, unit: str, mode: str) -> None:
        branch = BRANCHES[unit][mode]
        disjunct.local_box = ConstraintList()
        for product, (lower, upper) in branch["box"].items():
            disjunct.local_box.add(model.q[unit, product] >= lower)
            disjunct.local_box.add(model.q[unit, product] <= upper)
        disjunct.complete_rows = ConstraintList()
        for _row_id, coefficients, rhs in branch["rows"]:
            disjunct.complete_rows.add(
                sum(coefficient * model.q[unit, product] for product, coefficient in coefficients.items()) <= rhs
            )
        fixed, coefficients = branch["cost"]
        disjunct.affine_cost = Constraint(
            expr=model.cost[unit] == fixed + sum(coefficients.get(p, 0.0) * model.q[unit, p] for p in PRODUCTS)
        )
        disjunct.affine_resources = ConstraintList()
        for resource in resources:
            fixed, coefficients = branch["resources"][resource]
            disjunct.affine_resources.add(
                model.resource[unit, resource]
                == fixed + sum(coefficients.get(p, 0.0) * model.q[unit, p] for p in PRODUCTS)
            )

    model.branch = Disjunct(model.OPTIONS, rule=branch_rule)
    model.select_one = Disjunction(
        model.UNITS,
        rule=lambda _m, unit: [model.branch[unit, mode] for mode in BRANCHES[unit]],
        xor=True,
    )
    model.external_demand = Constraint(
        model.PRODUCTS,
        rule=lambda _m, product: sum(model.q[unit, product] for unit in units) >= DEMAND[product],
    )
    model.shared_resource = Constraint(
        model.RESOURCES,
        rule=lambda _m, resource: sum(model.resource[unit, resource] for unit in units) <= RESOURCE_LIMIT[resource],
    )
    model.total_cost = Objective(expr=sum(model.cost[unit] for unit in units), sense=minimize)

    from pyomo.environ import TransformationFactory

    TransformationFactory("gdp.hull").apply_to(model)
    return model


def recover_source_solution(model: ConcreteModel, tolerance: float = 1e-7) -> dict[str, Any]:
    """Recover a teaching solution; this is not a Core execution hook.

    Callers must validate the recovered values against their source problem.
    """
    choices = {}
    for unit, modes in BRANCHES.items():
        selected = [
            mode for mode in modes
            if value(model.branch[unit, mode].binary_indicator_var) is not None
            and value(model.branch[unit, mode].binary_indicator_var) >= 1.0 - tolerance
        ]
        if len(selected) != 1:
            raise ValueError(f"expected one selected mode for {unit}; found {selected}")
        choices[unit] = selected[0]
    quantities = {
        unit: {product: float(value(model.q[unit, product])) for product in PRODUCTS}
        for unit in BRANCHES
    }
    return {"selected_mode_by_unit": choices, "quantity_by_unit": quantities}
