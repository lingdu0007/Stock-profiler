"""Bounded lexicographic allocation over a single immutable capacity polytope.

The numerical optimizer proposes quantities. Decimal validation by the caller is
the authority for publishing a plan; a limit or unverified optimum fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from math import inf
from time import monotonic
from typing import Literal

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from stock_profiler.modules.portfolio.allocation_contracts import (
    AcquisitionRoute,
    AllocationComparison,
    AllocationCriterion,
)


class AllocationSolveFailed(Exception):
    """No bounded, verified optimal allocation is available."""


class _InfeasibleAllocation(AllocationSolveFailed):
    """The immutable capacity polytope cannot satisfy mandatory coverage."""


@dataclass
class _SolveBudget:
    deadline: float
    solve_count: int = 0


def _number(value: Decimal) -> float:
    converted = float(value)
    if not np.isfinite(converted) or (value != 0 and converted == 0):
        raise AllocationSolveFailed("allocation numeric domain exceeded")
    return converted


@dataclass(frozen=True)
class Capacity:
    members: tuple[int, ...]
    remaining: Decimal
    reason: str


@dataclass(frozen=True)
class AllocationProblem:
    routes: tuple[AcquisitionRoute, ...]
    route_members: tuple[int, ...]
    issuers: tuple[str, ...]
    existing: tuple[Decimal, ...]
    targets: tuple[Decimal, ...]
    probabilities: tuple[Decimal, ...]
    account_cash: dict[str, Decimal]
    cash: Decimal
    stress_remaining: Decimal
    shock: Decimal
    capacities: tuple[Capacity, ...]


@dataclass(frozen=True)
class ContinuousAllocation:
    principals: tuple[Decimal, ...]
    route_principals: tuple[Decimal, ...]


@dataclass(frozen=True)
class DiscreteAllocation:
    quantities: tuple[Decimal, ...]
    objectives: tuple[tuple[Decimal, ...], ...]
    comparisons: dict[int, tuple[AllocationComparison, ...]]
    below_minimum: frozenset[int]


def capacity_is_feasible(
    problem: AllocationProblem,
    principals: tuple[Decimal, ...],
    route_principals: tuple[Decimal, ...],
) -> bool:
    """Check every monetary capacity using the original Decimal input facts."""
    expenditures = tuple(
        amount + route.cost(amount)
        for route, amount in zip(problem.routes, route_principals, strict=True)
    )
    return (
        all(amount >= 0 for amount in (*principals, *route_principals))
        and sum(expenditures, Decimal(0)) <= problem.cash
        and all(
            sum(
                (
                    amount
                    for route, amount in zip(problem.routes, expenditures, strict=True)
                    if route.account_id == account
                ),
                Decimal(0),
            )
            <= cash
            for account, cash in problem.account_cash.items()
        )
        and all(
            sum((principals[index] for index in capacity.members), Decimal(0))
            <= max(Decimal(0), capacity.remaining)
            for capacity in problem.capacities
        )
        and sum(
            (
                amount * (problem.shock + route.disposal_friction_ratio)
                for route, amount in zip(problem.routes, route_principals, strict=True)
            ),
            Decimal(0),
        )
        <= problem.stress_remaining
    )


class _Model:
    def __init__(self, budget: _SolveBudget | None = None) -> None:
        self.lower: list[float] = []
        self.upper: list[float] = []
        self.integer: list[int] = []
        self.constraints: list[tuple[dict[int, float], float, float]] = []
        self.budget = budget or _SolveBudget(monotonic() + 120)

    def variable(self, upper: float = inf, *, integer: bool = False) -> int:
        if len(self.lower) >= 2000 or np.isnan(upper):
            raise AllocationSolveFailed("allocation model budget exceeded")
        index = len(self.lower)
        self.lower.append(0.0)
        self.upper.append(upper)
        self.integer.append(int(integer))
        return index

    def constrain(self, values: dict[int, float], lower: float = -inf, upper: float = inf) -> None:
        if len(self.constraints) >= 5000:
            raise AllocationSolveFailed("allocation model budget exceeded")
        self.constraints.append((values, lower, upper))

    def solve(self, objective: dict[int, float]) -> list[float]:
        remaining = self.budget.deadline - monotonic()
        self.budget.solve_count += 1
        if remaining <= 0 or self.budget.solve_count > 512:
            raise AllocationSolveFailed("allocation solve budget exceeded")
        coefficients = np.zeros(len(self.lower))
        for index, value in objective.items():
            coefficients[index] = value
        matrix = np.zeros((len(self.constraints), len(self.lower)))
        for row, (values, _, _) in enumerate(self.constraints):
            for index, value in values.items():
                matrix[row, index] = value
        if not np.isfinite(matrix).all() or not np.isfinite(coefficients).all():
            raise AllocationSolveFailed("allocation numeric domain exceeded")
        try:
            result = milp(
                coefficients,
                integrality=np.array(self.integer),
                bounds=Bounds(self.lower, self.upper),
                constraints=LinearConstraint(
                    matrix,
                    [row[1] for row in self.constraints],
                    [row[2] for row in self.constraints],
                ),
                options={
                    "time_limit": min(15.0, remaining),
                    "node_limit": 100000,
                    "mip_rel_gap": 0.0,
                },
            )
        except ValueError as error:
            raise AllocationSolveFailed("allocation numeric domain exceeded") from error
        if result.status == 2:
            raise _InfeasibleAllocation("mandatory allocation infeasible")
        if result.status != 0 or result.x is None or not np.isfinite(result.x).all():
            raise AllocationSolveFailed("optimal allocation unavailable")
        return [float(value) for value in result.x]

    def maximize_and_fix(self, objective: dict[int, float]) -> list[float]:
        solution = self.solve({index: -value for index, value in objective.items()})
        optimum = sum(solution[index] * value for index, value in objective.items())
        self.constrain(objective, optimum, optimum)
        return solution


def _sum(expressions: list[dict[int, float]]) -> dict[int, float]:
    result: dict[int, float] = {}
    for expression in expressions:
        for index, value in expression.items():
            result[index] = result.get(index, 0.0) + value
    return result


def _base(
    problem: AllocationProblem, *, discrete: bool, budget: _SolveBudget | None = None
) -> tuple[_Model, list[dict[int, float]], list[int], list[int], list[int]]:
    model = _Model(budget)
    amounts: list[dict[int, float]] = []
    active: list[int] = []
    quantities: list[int] = []
    fees: list[int] = []
    for route, member in zip(problem.routes, problem.route_members, strict=True):
        assert route.price_cap is not None
        upper = _number(problem.targets[member])
        enabled = model.variable(1, integer=True)
        fee = model.variable()
        if discrete:
            quantity = model.variable(upper / _number(route.price_cap), integer=True)
            # Quantity is minimum * enabled + increment * integer increments.
            expression = {
                enabled: _number(route.minimum_quantity * route.price_cap),
                quantity: _number(route.quantity_increment * route.price_cap),
            }
            model.constrain(
                {
                    quantity: _number(route.quantity_increment),
                    enabled: -upper / _number(route.price_cap),
                },
                upper=0,
            )
        else:
            quantity = model.variable(upper)
            expression = {quantity: 1.0}
            model.constrain({quantity: 1, enabled: -upper}, upper=0)
        model.constrain({fee: 1, enabled: -_number(route.minimum_commission)}, lower=0)
        model.constrain(
            {
                fee: 1,
                **{
                    index: -value * _number(route.commission_ratio)
                    for index, value in expression.items()
                },
            },
            lower=0,
        )
        amounts.append(expression)
        active.append(enabled)
        quantities.append(quantity)
        fees.append(fee)
    expenditures = [
        {
            **{
                index: value * (1 + _number(route.other_cost_ratio))
                for index, value in amount.items()
            },
            fee: 1.0,
        }
        for route, amount, fee in zip(problem.routes, amounts, fees, strict=True)
    ]
    model.constrain(_sum(expenditures), upper=_number(max(Decimal(0), problem.cash)))
    for account, cash in problem.account_cash.items():
        model.constrain(
            _sum(
                [
                    value
                    for value, route in zip(expenditures, problem.routes, strict=True)
                    if route.account_id == account
                ]
            ),
            upper=_number(max(Decimal(0), cash)),
        )
    model.constrain(
        _sum(
            [
                {
                    index: value * _number(problem.shock + route.disposal_friction_ratio)
                    for index, value in amount.items()
                }
                for route, amount in zip(problem.routes, amounts, strict=True)
            ]
        ),
        upper=_number(max(Decimal(0), problem.stress_remaining)),
    )
    for capacity in problem.capacities:
        model.constrain(
            _sum(
                [
                    amount
                    for amount, member in zip(amounts, problem.route_members, strict=True)
                    if member in capacity.members
                ]
            ),
            upper=_number(max(Decimal(0), capacity.remaining)),
        )
    return model, amounts, active, quantities, fees


def _member_amounts(
    problem: AllocationProblem, amounts: list[dict[int, float]]
) -> list[dict[int, float]]:
    return [
        _sum(
            [
                amount
                for amount, member in zip(amounts, problem.route_members, strict=True)
                if member == index
            ]
        )
        for index in range(len(problem.issuers))
    ]


def _progressive(
    model: _Model, expressions: list[dict[int, float]], existing: list[float], ceilings: list[float]
) -> None:
    pending = set(range(len(expressions)))
    while pending:
        level = model.variable(max(ceilings))
        for index in sorted(pending):
            model.constrain({**expressions[index], level: -1}, lower=-existing[index])
        solution = model.solve({level: -1})
        floor = solution[level]
        model.lower[level] = floor
        model.upper[level] = floor
        bottlenecks = []
        for index in sorted(pending):
            raised = model.solve(
                {variable: -value for variable, value in expressions[index].items()}
            )
            maximum = existing[index] + sum(
                raised[variable] * value for variable, value in expressions[index].items()
            )
            if maximum <= max(floor, existing[index]) + 1e-6:
                bottlenecks.append(index)
        if not bottlenecks:
            raise AllocationSolveFailed("progressive equality ambiguous")
        for index in bottlenecks:
            amount = max(0.0, floor - existing[index])
            model.constrain(expressions[index], amount, amount)
            pending.remove(index)


def continuous_allocation(problem: AllocationProblem) -> ContinuousAllocation:
    if not problem.routes:
        return ContinuousAllocation(tuple(Decimal(0) for _ in problem.issuers), ())
    model, amounts, _, _, fees = _base(problem, discrete=False)
    members = _member_amounts(problem, amounts)
    issuers = sorted(set(problem.issuers))
    groups = [
        [index for index, issuer in enumerate(problem.issuers) if issuer == identity]
        for identity in issuers
    ]
    _progressive(
        model,
        [_sum([members[index] for index in group]) for group in groups],
        [_number(problem.existing[group[0]]) for group in groups],
        [_number(problem.existing[group[0]] + problem.targets[group[0]]) for group in groups],
    )
    # Codes sharing an issuer divide that issuer's fixed allocation without score weights.
    _progressive(
        model, members, [0.0] * len(members), [_number(value) for value in problem.targets]
    )
    solution = model.solve({index: 1.0 for index in fees})
    route_principals = tuple(
        Decimal(str(sum(solution[index] * value for index, value in amount.items()))).quantize(
            Decimal("0.000001"), rounding=ROUND_FLOOR
        )
        for amount in amounts
    )
    principals = tuple(
        sum(
            (
                amount
                for amount, member in zip(route_principals, problem.route_members, strict=True)
                if member == index
            ),
            Decimal(0),
        )
        for index in range(len(problem.issuers))
    )
    return ContinuousAllocation(principals, route_principals)


def discrete_principal_ceiling(
    problem: AllocationProblem,
    continuous: tuple[Decimal, ...],
    member: int,
    quantities: tuple[Decimal, ...],
) -> Decimal:
    unit = min(
        (
            route.minimum_quantity * (route.price_cap or Decimal(0))
            for route, index, quantity in zip(
                problem.routes, problem.route_members, quantities, strict=True
            )
            if index == member and quantity > 0
        ),
        default=Decimal(0),
    )
    return (
        min(problem.targets[member], continuous[member] + unit)
        if continuous[member] > 0
        else Decimal(0)
    )


def _discrete_solution(
    problem: AllocationProblem,
    continuous: tuple[Decimal, ...],
    budget: _SolveBudget,
    required_member: int | None = None,
) -> tuple[Decimal, ...]:
    if not problem.routes:
        return ()
    model, amounts, active, increments, fees = _base(problem, discrete=True, budget=budget)
    members = _member_amounts(problem, amounts)
    coverage = [model.variable(1, integer=True) for _ in members]
    for index, expression in enumerate(members):
        member_routes = [
            route for route, member in enumerate(problem.route_members) if member == index
        ]
        upper = problem.targets[index] if continuous[index] > 0 else Decimal(0)
        model.constrain(expression, upper=_number(upper))
        for route in member_routes:
            minimum = problem.routes[route].minimum_quantity * (
                problem.routes[route].price_cap or Decimal(0)
            )
            # Only a selected route can supply the one-minimum-unit residual allowance.
            model.constrain(
                {**expression, active[route]: _number(upper)},
                upper=_number(upper + continuous[index] + minimum),
            )
        model.constrain(
            {coverage[index]: 1, **{active[route]: -1 for route in member_routes}}, upper=0
        )
        for route in member_routes:
            model.constrain({active[route]: 1, coverage[index]: -1}, upper=0)
    model.constrain(_sum(amounts), upper=_number(sum(continuous, Decimal(0))))
    if required_member is not None:
        model.constrain({coverage[required_member]: 1}, lower=1)
    solution = model.maximize_and_fix(dict.fromkeys(coverage, 1.0))
    count = round(sum(solution[index] for index in coverage))
    if count == 0:
        return tuple(Decimal(0) for _ in problem.routes)
    # The sum of the k smallest ratios is max_t(k*t - sum(max(0, t-r_i))).
    # Fixing each prefix sum gives the same ascending-vector lexicographic order
    # without assigning candidates to permutation-symmetric rank slots.
    big = 2.0 + max(
        (
            _number(problem.targets[index] / value)
            for index, value in enumerate(continuous)
            if value > 0
        ),
        default=0.0,
    )
    for prefix_size in range(1, count + 1):
        threshold = model.variable(big)
        slacks = [model.variable(big) for _ in members]
        for index, expression in enumerate(members):
            ratio = (
                {
                    variable: value / _number(continuous[index])
                    for variable, value in expression.items()
                }
                if continuous[index] > 0
                else {}
            )
            # Uncovered candidates have ratio big and cannot enter a covered prefix.
            model.constrain(
                {**ratio, slacks[index]: 1, threshold: -1, coverage[index]: -big},
                lower=-big,
            )
        model.maximize_and_fix({threshold: prefix_size, **dict.fromkeys(slacks, -1.0)})
    model.maximize_and_fix(_sum(amounts))
    # Sorted covered probabilities: minimize the count at each low probability first.
    for probability in sorted(set(problem.probabilities))[:-1]:
        model.maximize_and_fix(
            {
                coverage[index]: -1
                for index, value in enumerate(problem.probabilities)
                if value == probability
            }
        )
    costs = _sum(
        [
            {
                **{
                    index: -value * _number(route.other_cost_ratio)
                    for index, value in amount.items()
                },
                fee: -1,
            }
            for route, amount, fee in zip(problem.routes, amounts, fees, strict=True)
        ]
    )
    model.maximize_and_fix(costs)
    model.maximize_and_fix(dict.fromkeys(active, -1.0))
    for route in sorted(
        range(len(amounts)),
        key=lambda index: (problem.routes[index].security_id, problem.routes[index].account_id),
    ):
        solution = model.maximize_and_fix(amounts[route])
    return tuple(
        route.minimum_quantity * round(solution[enabled])
        + route.quantity_increment * round(solution[increment])
        for route, enabled, increment in zip(problem.routes, active, increments, strict=True)
    )


def _objectives(
    problem: AllocationProblem, continuous: tuple[Decimal, ...], quantities: tuple[Decimal, ...]
) -> tuple[tuple[Decimal, ...], ...]:
    amounts = tuple(
        quantity * (route.price_cap or Decimal(0))
        for route, quantity in zip(problem.routes, quantities, strict=True)
    )
    members = tuple(
        sum(
            (
                amount
                for amount, owner in zip(amounts, problem.route_members, strict=True)
                if owner == index
            ),
            Decimal(0),
        )
        for index in range(len(continuous))
    )
    covered = tuple(index for index, amount in enumerate(members) if amount > 0)
    if (
        not capacity_is_feasible(problem, members, amounts)
        or any(
            quantity > 0
            and (
                quantity < route.minimum_quantity
                or (quantity - route.minimum_quantity) % route.quantity_increment != 0
            )
            for route, quantity in zip(problem.routes, quantities, strict=True)
        )
        or sum(amounts, Decimal(0)) > sum(continuous, Decimal(0))
        or any(
            amount > discrete_principal_ceiling(problem, continuous, index, quantities)
            for index, amount in enumerate(members)
        )
    ):
        raise AllocationSolveFailed("comparison capacity verification failed")
    return (
        (Decimal(len(covered)),),
        tuple(sorted(members[index] / continuous[index] for index in covered)),
        (sum(amounts, Decimal(0)),),
        tuple(sorted(problem.probabilities[index] for index in covered)),
        (
            sum(
                (route.cost(amount) for route, amount in zip(problem.routes, amounts, strict=True)),
                Decimal(0),
            ),
        ),
        (Decimal(sum(quantity > 0 for quantity in quantities)),),
        tuple(
            amounts[index]
            for index in sorted(
                range(len(amounts)),
                key=lambda index: (
                    problem.routes[index].security_id,
                    problem.routes[index].account_id,
                ),
            )
        ),
    )


def discrete_allocation(
    problem: AllocationProblem, continuous: tuple[Decimal, ...]
) -> DiscreteAllocation:
    """Select once; explain excluded members against the same frozen capacities.

    Mandatory-member counterfactuals are audit queries only. They neither apply
    allocations nor replenish capacity and share the original bounded solve budget.
    """
    budget = _SolveBudget(monotonic() + 120)
    quantities = _discrete_solution(problem, continuous, budget)
    selected = _objectives(problem, continuous, quantities)
    comparisons: dict[int, tuple[AllocationComparison, ...]] = {}
    below_minimum: set[int] = set()
    criteria: tuple[AllocationCriterion, ...] = (
        "COVERAGE",
        "COMPLETION_RATIOS",
        "PRINCIPAL",
        "PROBABILITIES",
        "PURCHASE_COST",
        "ORDER_COUNT",
        "STABLE_IDENTITIES",
    )
    for member, amount in enumerate(continuous):
        if amount == 0 or any(
            quantity > 0 and owner == member
            for quantity, owner in zip(quantities, problem.route_members, strict=True)
        ):
            continue
        try:
            alternative = _objectives(
                problem, continuous, _discrete_solution(problem, continuous, budget, member)
            )
        except _InfeasibleAllocation:
            alternative = None
            below_minimum.add(member)
        entries = []
        decided = False
        for index, criterion in enumerate(criteria):
            direction: Literal["MINIMIZE", "MAXIMIZE"] = (
                "MINIMIZE" if index in {4, 5} else "MAXIMIZE"
            )
            relation: Literal["INFEASIBLE", "NOT_REACHED", "EQUAL", "WORSE"] = (
                "INFEASIBLE" if alternative is None else "NOT_REACHED" if decided else "EQUAL"
            )
            if alternative is not None and not decided and selected[index] != alternative[index]:
                better = selected[index] > alternative[index]
                if direction == "MINIMIZE":
                    better = not better
                if not better:
                    raise AllocationSolveFailed("unverified lexicographic optimum")
                relation = "WORSE"
                decided = True
            entries.append(
                AllocationComparison(
                    criterion=criterion,
                    direction=direction,
                    selected_value=selected[index],
                    alternative_value=alternative[index] if alternative is not None else None,
                    relation=relation,
                )
            )
        if alternative is not None and not decided:
            raise AllocationSolveFailed("stable allocation identities ambiguous")
        comparisons[member] = tuple(entries)
    return DiscreteAllocation(quantities, selected, comparisons, frozenset(below_minimum))
