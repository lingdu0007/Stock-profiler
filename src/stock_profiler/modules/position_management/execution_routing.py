"""Account-local legal quantities and deterministic full-cost comparisons."""

from collections.abc import Callable
from decimal import ROUND_FLOOR, Decimal
from itertools import product
from math import prod

from stock_profiler.modules.position_management.contracts import ReconciledPositionSnapshot
from stock_profiler.modules.position_management.execution_contracts import (
    ExecutionLeg,
    ExecutionPlanCommand,
    ExecutionPlanOutcome,
    ExecutionTarget,
)


def route_targets(
    command: ExecutionPlanCommand,
    snapshot: ReconciledPositionSnapshot,
    targets: tuple[ExecutionTarget, ...],
    funding_gap: Callable[[tuple[ExecutionLeg, ...]], Decimal] | None = None,
) -> ExecutionPlanOutcome:
    blocked = ExecutionPlanOutcome(
        disposition="BLOCKED",
        reasons=("EXECUTION_COST_EVIDENCE_FAILED",),
        new_exposure_blocked=True,
        targets=targets,
    )
    routes = {(route.account_id, route.security_id): route for route in command.routes}
    units = {(unit.account_id, unit.security_id): unit for unit in snapshot.action_units}
    if len(routes) != len(command.routes) or not set(routes).issubset(units):
        return blocked.model_copy(update={"reasons": ("EXECUTION_ROUTE_SCOPE_INVALID",)})
    options: dict[tuple[str, str], tuple[Decimal, ...]] = {}
    conservative = False
    for key, unit in units.items():
        maximum = unit.exact_statistical_action_quantity
        if maximum == 0:
            options[key] = (Decimal(0),)
            continue
        route = routes.get(key)
        if maximum is None or unit.market_price is None:
            return blocked.model_copy(update={"reasons": ("EXECUTION_QUANTITY_UNKNOWN",)})
        if route is None or route.qualified_curve(command.cutoff_at) is None:
            return blocked
        conservative = conservative or route.qualified_curve(command.cutoff_at) != route.cost_curve
        if (
            route.rules_evidence.problem_codes(command.cutoff_at, require_current_completeness=True)
            or route.first_sellable_at < command.cutoff_at
            or route.transferable_at < route.first_sellable_at
        ):
            return blocked.model_copy(update={"reasons": ("EXECUTION_RULE_EVIDENCE_FAILED",)})
        count = (
            int(
                ((maximum - route.minimum_quantity) / route.quantity_increment).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            )
            + 1
            if maximum >= route.minimum_quantity
            else 0
        )
        if count > 100_000:
            return blocked.model_copy(update={"reasons": ("EXECUTION_SEARCH_LIMIT",)})
        values = {
            Decimal(0),
            *(route.minimum_quantity + i * route.quantity_increment for i in range(count)),
        }
        if route.allow_full_odd_lot and maximum == unit.total_quantity:
            values.add(maximum)
        options[key] = tuple(sorted(values))
    alternatives: list[tuple[tuple[ExecutionLeg, ...], ...]] = []
    revised: list[ExecutionTarget] = []

    def make_legs(
        keys: list[tuple[str, str]], values: tuple[Decimal, ...]
    ) -> tuple[ExecutionLeg, ...]:
        result = []
        for key, quantity in zip(keys, values, strict=True):
            if quantity == 0:
                continue
            route = routes[key]
            curve = route.qualified_curve(command.cutoff_at)
            price = units[key].market_price
            assert curve is not None and price is not None
            gross = quantity * price
            cost = curve.cost(quantity, price)
            result.append(
                ExecutionLeg(
                    account_id=key[0],
                    security_id=key[1],
                    quantity=quantity,
                    gross_proceeds=gross,
                    disposal_cost=cost,
                    net_proceeds=gross - cost,
                    route=route,
                )
            )
        return tuple(result)

    def rank(candidate: tuple[ExecutionLeg, ...]) -> tuple[Decimal, ...]:
        quantities = {(leg.account_id, leg.security_id): leg.quantity for leg in candidate}
        windows = sorted({route.first_sellable_at for route in command.routes})
        earlier = tuple(
            -sum(
                (leg.gross_proceeds for leg in candidate if leg.route.first_sellable_at == window),
                Decimal(0),
            )
            for window in windows
        )
        cost = sum((leg.disposal_cost for leg in candidate), Decimal(0))
        deviation = Decimal(0)
        for security in {key[1] for key in units}:
            keys = [key for key in units if key[1] == security]
            total = sum((quantities.get(key, Decimal(0)) for key in keys), Decimal(0))
            sellable = sum(
                (units[key].exact_statistical_action_quantity or Decimal(0) for key in keys),
                Decimal(0),
            )
            deviation += sum(
                (
                    abs(
                        quantities.get(key, Decimal(0)) * sellable
                        - total * (units[key].exact_statistical_action_quantity or Decimal(0))
                    )
                    for key in keys
                ),
                Decimal(0),
            )
        return (
            funding_gap(candidate) if funding_gap is not None else Decimal(0),
            *earlier,
            *((deviation, cost) if conservative else (cost, deviation)),
            *(quantities.get(key, Decimal(0)) for key in sorted(units)),
        )

    for target in targets:
        keys = sorted(key for key in units if key[1] == target.security_id)
        if prod(len(options[key]) for key in keys) > 200_000:
            return blocked.model_copy(update={"reasons": ("EXECUTION_SEARCH_LIMIT",)})
        required = target.required_sale_quantity
        if required is None:
            return blocked.model_copy(update={"reasons": ("EXECUTION_QUANTITY_UNKNOWN",)})
        candidates = tuple(product(*(options[key] for key in keys)))
        maximum_sale = sum((max(options[key]) for key in keys), Decimal(0))
        achievable = min(required, maximum_sale)
        eligible = tuple(values for values in candidates if sum(values) >= achievable)
        total = min(sum(values, Decimal(0)) for values in eligible)
        variants = tuple(
            make_legs(keys, values) for values in eligible if sum(values, Decimal(0)) == total
        )
        alternatives.append(variants if funding_gap is not None else (min(variants, key=rank),))
        current = target.target_quantity + required
        revised.append(
            target.model_copy(
                update={
                    "remaining_quantity": current - total,
                    "remaining_gap": max(Decimal(0), required - total),
                    "rounding_induced_full_sale": total == current and target.target_quantity > 0,
                }
            )
        )
    if prod(len(variants) for variants in alternatives) > 200_000:
        return blocked.model_copy(update={"reasons": ("EXECUTION_SEARCH_LIMIT",)})
    legs = min(
        (tuple(leg for group in groups for leg in group) for groups in product(*alternatives)),
        key=rank,
    )
    incomplete = any(target.remaining_gap is None or target.remaining_gap > 0 for target in revised)
    reconfirm = any(target.rounding_induced_full_sale for target in revised)
    return ExecutionPlanOutcome(
        disposition="BLOCKED"
        if incomplete
        else "RECONFIRMATION_REQUIRED"
        if reconfirm
        else "PLANNED",
        reasons=(
            *(("RISK_REMEDIATION_BLOCKED",) if incomplete else ()),
            *(("TRADING_UNIT_FULL_SALE",) if reconfirm else ()),
        ),
        new_exposure_blocked=True,
        targets=tuple(revised),
        legs=tuple(legs),
        requires_confirmation=bool(legs),
        cost_routing_basis="CONSERVATIVE_BOUND_PROPORTIONAL"
        if conservative
        else "VERIFIED_FULL_COST",
    )
