"""Pure account cost qualification against a frozen exit-program budget."""

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from stock_profiler.modules.position_management.action_contracts import (
    PositionActionCommand,
    StatisticalReductionAnchor,
)
from stock_profiler.modules.position_management.contracts import PositionReconciliationOutcome
from stock_profiler.modules.position_management.execution_contracts import ExecutionPlanOutcome


@dataclass(frozen=True)
class ActionCostAssessment:
    evidence_failed: bool
    outside_envelope: bool


def assess_action_costs(
    command: PositionActionCommand,
    position: PositionReconciliationOutcome,
    plan: ExecutionPlanOutcome,
    *,
    cost_anchor: StatisticalReductionAnchor | None,
    first_exit_seen: bool,
) -> ActionCostAssessment:
    units = [u for u in position.snapshot.action_units if u.security_id == command.security_id]
    quantity = sum((u.total_quantity or Decimal(0) for u in units), Decimal(0))
    market_value = sum(
        ((u.total_quantity or Decimal(0)) * (u.market_price or Decimal(0)) for u in units),
        Decimal(0),
    )
    cost_failure = first_exit_seen and cost_anchor is None
    outside = False
    route_map = {(r.account_id, r.security_id): r for r in command.execution.routes}
    for unit in units:
        route = route_map.get((unit.account_id, unit.security_id))
        curve = route.cost_curve if route is not None else None
        if curve is None or curve.evidence.problem_codes(
            command.cutoff_at, require_current_completeness=True
        ):
            cost_failure = True
            continue
        rate = (
            curve.commission_ratio
            + curve.trading_fee_ratio
            + curve.settlement_fee_ratio
            + curve.tax_ratio
            + curve.slippage_ratio
            + curve.market_impact_ratio
        )
        outside = outside or (
            rate > command.policy.maximum_cost_ratio
            or curve.minimum_commission > command.policy.maximum_minimum_commission
            or any(
                lot.tax_per_share > command.policy.maximum_dividend_tax_per_share
                for lot in curve.dividend_lots
            )
        )
    accrued_cost = sum(
        (
            -entry.cash_delta
            for entry in position.snapshot.authoritative_ledger
            if entry.security_id == command.security_id
            and entry.entry_type == "FEE"
            and cost_anchor is not None
            and entry.occurred_at > cost_anchor.frozen_at
        ),
        Decimal(0),
    )
    planned_cost = sum(
        (leg.disposal_cost for leg in plan.legs if leg.security_id == command.security_id),
        Decimal(0),
    )
    conjoined_target = next(
        (t.target_quantity for t in plan.targets if t.security_id == command.security_id), None
    )
    if (
        plan.disposition == "BLOCKED"
        and conjoined_target is not None
        and quantity > 0
        and not cost_failure
    ):
        # A blocked route is not a zero-cost forecast. Bound the full remaining
        # target sale using account-local legal quantities, even without bids.
        required = max(Decimal(0), quantity - conjoined_target)
        bound = Decimal(0)
        for unit in units:
            route = route_map[(unit.account_id, unit.security_id)]
            assert route.cost_curve is not None and unit.total_quantity is not None
            amount = required * unit.total_quantity / quantity
            if amount:
                increments = max(Decimal(0), amount - route.minimum_quantity)
                increments = (increments / route.quantity_increment).to_integral_value(
                    rounding=ROUND_CEILING
                )
                amount = min(
                    unit.total_quantity,
                    route.minimum_quantity + increments * route.quantity_increment,
                )
            bound += route.cost_curve.cost(amount, unit.market_price or Decimal(0))
        planned_cost = max(planned_cost, bound)
    outside = (
        outside
        or max(Decimal(0), accrued_cost) + planned_cost
        > (cost_anchor.market_value if cost_anchor is not None else market_value)
        * command.policy.lifecycle_cost_ratio
    )
    return ActionCostAssessment(cost_failure, outside)
