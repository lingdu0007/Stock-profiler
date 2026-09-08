"""Existing-target-first proportional reduction with explicit hypothetical gaps."""

from decimal import Decimal

from stock_profiler.modules.portfolio.drawdown_contracts import DrawdownState
from stock_profiler.modules.portfolio.liquidity import LiquidityCommand, LiquidityOutcome
from stock_profiler.modules.portfolio.liquidity_funding import (
    AvailableCashResource,
    assess_available_cash,
)
from stock_profiler.modules.portfolio.stress import PortfolioStressOutcome
from stock_profiler.modules.position_management.contracts import ReconciledPositionSnapshot
from stock_profiler.modules.position_management.execution_contracts import (
    ExecutionLeg,
    ExecutionPlanCommand,
    ExecutionPlanOutcome,
    ExecutionTarget,
)
from stock_profiler.modules.position_management.execution_routing import route_targets


def conjoin_portfolio_targets(
    command: ExecutionPlanCommand,
    snapshot: ReconciledPositionSnapshot,
    targets: tuple[ExecutionTarget, ...],
    stress: PortfolioStressOutcome,
    liquidity: LiquidityOutcome,
    drawdown: DrawdownState,
    liquidity_command: LiquidityCommand,
) -> ExecutionPlanOutcome:
    stock = sum(
        (
            unit.total_quantity * unit.market_price
            for unit in snapshot.action_units
            if unit.total_quantity is not None and unit.market_price is not None
        ),
        Decimal(0),
    )
    stress_active = stress.obligation is not None and stress.obligation.status == "OUTSTANDING"
    cash_required = liquidity.remediation_shortfall or Decimal(0)
    assert liquidity.protection_authorization is not None
    usage = liquidity.protection_authorization.usage
    assert usage is not None
    paid: dict[str, Decimal] = {}
    for receipt in liquidity.settled_coverage:
        paid[receipt.obligation_id] = paid.get(receipt.obligation_id, Decimal(0)) + receipt.amount
    obligations = tuple(
        obligation.model_copy(
            update={"amount": obligation.amount - paid.get(obligation.obligation_id, Decimal(0))}
        )
        for obligation in usage.unfinished_cash_obligations
        if obligation.amount > paid.get(obligation.obligation_id, Decimal(0))
    )

    def funding_gap(legs: tuple[ExecutionLeg, ...]) -> Decimal:
        resources = tuple(
            AvailableCashResource(
                account_id=cash.account_id,
                available_at=snapshot.cutoff_at,
                amount=max(
                    Decimal(0),
                    (cash.transferable_cash or Decimal(0))
                    + sum(
                        (
                            min(Decimal(0), leg.net_proceeds)
                            for leg in legs
                            if leg.account_id == cash.account_id
                        ),
                        Decimal(0),
                    ),
                ),
            )
            for cash in snapshot.cash_states
        ) + tuple(
            AvailableCashResource(
                account_id=leg.account_id,
                available_at=leg.route.transferable_at,
                amount=leg.net_proceeds,
            )
            for leg in legs
            if leg.net_proceeds > 0
        )
        funding = assess_available_cash(resources, liquidity_command.transfer_routes, obligations)
        return sum((item.uncovered_gap for item in funding), Decimal(0))

    initial = route_targets(command, snapshot, targets, funding_gap if obligations else None)
    routing_failed = (
        initial.reasons
        and initial.reasons != ("RISK_REMEDIATION_BLOCKED",)
        and "TRADING_UNIT_FULL_SALE" not in initial.reasons
    )
    initial_value = sum((leg.gross_proceeds for leg in initial.legs), Decimal(0))

    def project(plan: ExecutionPlanOutcome) -> ExecutionPlanOutcome:
        sold = sum((leg.gross_proceeds for leg in plan.legs), Decimal(0))
        net_cash = sum((leg.net_proceeds for leg in plan.legs), Decimal(0))
        stress_gap = Decimal(0)
        if stress_active:
            assert stress.obligation is not None and stress.calculation_policy is not None
            assert stress.net_liquidation_equity is not None
            policy = stress.calculation_policy
            stress_gap = max(
                Decimal(0),
                (stock - sold) * (policy.shock_ratio + policy.disposal_friction_ratio)
                - stress.obligation.target_stress_ratio * stress.net_liquidation_equity,
            )
        exposure_gap = (
            max(Decimal(0), stock - sold - drawdown.stock_exposure_target_value)
            if drawdown.stock_exposure_target_value is not None
            else Decimal(0)
        )
        deadline_gap = funding_gap(plan.legs)
        cash_gap = max(Decimal(0), cash_required - net_cash, deadline_gap)
        return plan.model_copy(
            update={
                "initial_target_sale_value": initial_value,
                "projected_stress_gap": stress_gap,
                "projected_cash_gap": cash_gap,
                "projected_exposure_gap": exposure_gap,
            }
        )

    def restored(plan: ExecutionPlanOutcome) -> bool:
        return (
            plan.projected_stress_gap == plan.projected_cash_gap == plan.projected_exposure_gap == 0
        )

    initial = project(initial)
    ids = (
        *(
            (stress.obligation.obligation_id,)
            if stress_active and stress.obligation is not None
            else ()
        ),
        *((liquidity.remediation_id,) if liquidity.remediation_id else ()),
        *((drawdown.decision_id,) if drawdown.risk_direction else ()),
    )
    initial = initial.model_copy(
        update={
            "new_exposure_blocked": initial.new_exposure_blocked
            or stress.new_exposure_blocked
            or liquidity.new_exposure_blocked
            or drawdown.new_exposure_blocked,
            "targets": tuple(
                target.model_copy(
                    update={
                        "source_obligation_ids": tuple(
                            dict.fromkeys((*target.source_obligation_ids, *ids))
                        ),
                        "direction": "REDUCE"
                        if ids and target.direction == "HOLD"
                        else target.direction,
                    }
                )
                for target in initial.targets
            ),
        }
    )
    if routing_failed:
        return initial
    if restored(initial):
        return initial
    remaining_sellable: dict[str, Decimal] = {}
    current: dict[str, Decimal] = {}
    for unit in snapshot.action_units:
        assert unit.total_quantity is not None
        current[unit.security_id] = current.get(unit.security_id, Decimal(0)) + unit.total_quantity
        remaining_sellable[unit.security_id] = remaining_sellable.get(
            unit.security_id, Decimal(0)
        ) + (unit.exact_statistical_action_quantity or Decimal(0))
    for leg in initial.legs:
        remaining_sellable[leg.security_id] -= leg.quantity

    def allocate(fraction: Decimal) -> ExecutionPlanOutcome:
        revised = []
        for target in initial.targets:
            assert target.remaining_quantity is not None
            cap = min(
                target.target_quantity,
                target.remaining_quantity - fraction * remaining_sellable[target.security_id],
            )
            if target.rounding_induced_full_sale:
                cap = target.target_quantity
            revised.append(
                target.model_copy(
                    update={
                        "target_quantity": cap,
                        "required_sale_quantity": max(
                            Decimal(0), current[target.security_id] - cap
                        ),
                        "source_obligation_ids": tuple(
                            dict.fromkeys((*target.source_obligation_ids, *ids))
                        ),
                        "direction": "EXIT" if target.direction == "EXIT" else "REDUCE",
                    }
                )
            )
        return project(
            route_targets(command, snapshot, tuple(revised), funding_gap if obligations else None)
        )

    maximum = allocate(Decimal(1))
    if not restored(maximum):
        gaps = ("projected_stress_gap", "projected_cash_gap", "projected_exposure_gap")
        if all(getattr(maximum, gap) == getattr(initial, gap) for gap in gaps):
            maximum = initial
        return maximum.model_copy(
            update={
                "disposition": "BLOCKED",
                "reasons": tuple(dict.fromkeys((*maximum.reasons, "RISK_REMEDIATION_BLOCKED"))),
            }
        )
    low, high = Decimal(0), Decimal(1)
    best = maximum
    # Every candidate is legally rounded and rechecked; never return an unverified midpoint.
    for _ in range(64):
        middle = (low + high) / 2
        candidate = allocate(middle)
        if restored(candidate):
            high, best = middle, candidate
        else:
            low = middle
    return best
