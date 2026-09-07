"""Concentration uses reconciled current value, never cost or pending sales."""

import json
from decimal import ROUND_FLOOR, Context, Decimal, localcontext
from hashlib import sha256
from typing import Literal

from stock_profiler.modules.portfolio.contracts import PortfolioAuthorizationOutcome
from stock_profiler.modules.position_management.concentration_contracts import (
    ConcentrationCommand,
    ConcentrationHistory,
    ConcentrationOutcome,
    ConcentrationTarget,
    IssuerConcentration,
)
from stock_profiler.modules.position_management.contracts import PositionReconciliationOutcome


def assess_concentration(
    command: ConcentrationCommand,
    portfolio: PortfolioAuthorizationOutcome,
    position: PositionReconciliationOutcome,
    *,
    history: ConcentrationHistory | None = None,
    authorization_lineage: tuple[PortfolioAuthorizationOutcome, ...] = (),
) -> ConcentrationOutcome:
    with localcontext(Context(prec=34)):
        return _assess(
            command, portfolio, position, history or ConcentrationHistory(), authorization_lineage
        )


def _assess(
    command: ConcentrationCommand,
    portfolio: PortfolioAuthorizationOutcome,
    position: PositionReconciliationOutcome,
    history: ConcentrationHistory,
    authorization_lineage: tuple[PortfolioAuthorizationOutcome, ...],
) -> ConcentrationOutcome:
    snapshot = position.snapshot
    usage = portfolio.usage
    budget = usage.authorization_snapshot.proposal.risk_budget if usage is not None else None
    reasons: list[str] = []
    if history.uncovered_obligation:
        reasons.append("CONCENTRATION_HISTORY_SCOPE_UNRESOLVED")
    ordered_history = tuple(
        item
        for item in sorted(history.outcomes, key=lambda item: item.cutoff_at)
        if "CONCENTRATION_SNAPSHOT_OUT_OF_ORDER" not in item.reasons
    )
    stale = any(item.cutoff_at >= snapshot.cutoff_at for item in ordered_history)
    if stale:
        reasons.append("CONCENTRATION_SNAPSHOT_OUT_OF_ORDER")
    prior_issuers = {
        issuer.issuer_id: issuer
        for item in ordered_history
        if item.cutoff_at <= snapshot.cutoff_at
        for issuer in item.issuers
    }
    current_issuer_ids = {item.issuer_id for item in snapshot.issuer_exposures}
    missing_issuers = [
        issuer
        for issuer in prior_issuers.values()
        if issuer.direction == "REDUCE" and issuer.issuer_id not in current_issuer_ids
    ]
    if missing_issuers:
        reasons.append("CONCENTRATION_ISSUER_LINEAGE_UNRESOLVED")
    if any(
        issuer.direction == "REDUCE"
        and issuer.obligation_started_at is not None
        and entry.entry_type == "CORPORATE_ACTION"
        and entry.quantity_delta != 0
        and issuer.obligation_started_at < entry.occurred_at <= snapshot.cutoff_at
        and entry.security_id in {target.security_id for target in issuer.targets}
        for issuer in prior_issuers.values()
        for account in command.position_snapshot.accounts
        for entry in account.ledger_entries
    ):
        reasons.append("CONCENTRATION_QUANTITY_BASIS_UNRESOLVED")
    if usage is None or not usage.allowed:
        reasons.extend(portfolio.reasons)
    elif set(usage.authorization_snapshot.proposal.snapshot.selected_account_ids) != set(
        command.position_snapshot.account_ids
    ):
        reasons.append("CONCENTRATION_PORTFOLIO_SCOPE_MISMATCH")
    if budget is not None and any(
        item.authorization is not None
        and item.authorization.authorization_id != command.authorization.authorization_id
        and budget.effective_at
        < item.authorization.proposal.risk_budget.effective_at
        <= snapshot.cutoff_at
        for item in authorization_lineage
    ):
        reasons.append("CONCENTRATION_CURRENT_AUTHORIZATION_REQUIRED")
    if position.disposition != "RECONCILED":
        reasons.append("CONCENTRATION_POSITION_FACTS_UNRESOLVED")
    prices: dict[str, set[Decimal | None]] = {}
    for unit in snapshot.action_units:
        prices.setdefault(unit.security_id, set()).add(unit.market_price)
    if any(len(values) != 1 for values in prices.values()):
        reasons.append("CONCENTRATION_SECURITY_PRICE_CONFLICT")
    if set(cost.account_id for cost in command.liquidation_costs) != set(
        command.position_snapshot.account_ids
    ) or any(
        cost.amount is None
        or cost.evidence.problem_codes(snapshot.cutoff_at, require_current_completeness=True)
        for cost in command.liquidation_costs
    ):
        reasons.append("CONCENTRATION_LIQUIDATION_COSTS_UNRESOLVED")
    equity = None
    if not reasons and snapshot.total_account_equity is not None:
        equity = snapshot.total_account_equity - sum(
            (cost.amount for cost in command.liquidation_costs if cost.amount is not None),
            Decimal(0),
        )
        if equity <= 0:
            reasons.append("CONCENTRATION_NET_EQUITY_NONPOSITIVE")
            equity = None
    issuers = []
    for exposure in snapshot.issuer_exposures:
        previous = prior_issuers.get(exposure.issuer_id)
        if previous is not None and previous.state == "RESOLVED":
            previous = None
        pending = previous is not None and previous.obligation_id is not None
        value = exposure.current_market_exposure
        weight = value / equity if value is not None and equity is not None else None
        normal = (
            value is not None
            and equity is not None
            and budget is not None
            and value <= equity * budget.concentration.target_ratio
        )
        state: Literal["NORMAL", "BUFFER", "REMEDIATION_REQUIRED", "RESOLVED", "UNKNOWN"] = (
            "NORMAL" if normal else "UNKNOWN"
        )
        direction: Literal["REDUCE"] | None = None
        obligation_id = None
        targets: list[ConcentrationTarget] = []
        gap = Decimal(0) if normal else None
        blocked = not normal
        if (
            value is not None
            and equity is not None
            and budget is not None
            and (not normal or pending)
        ):
            if value <= equity * budget.concentration.hard_ratio and not pending:
                state = "BUFFER"
                gap = Decimal(0)
                blocked = False
            else:
                state = "REMEDIATION_REQUIRED"
                direction = "REDUCE"
                target_value = equity * budget.concentration.target_ratio
                gap = value - target_value
                units = tuple(
                    unit for unit in snapshot.action_units if unit.issuer_id == exposure.issuer_id
                )
                for security_id in sorted({unit.security_id for unit in units}):
                    quantity = sum(
                        (
                            unit.total_quantity
                            for unit in units
                            if unit.security_id == security_id and unit.total_quantity is not None
                        ),
                        Decimal(0),
                    )
                    with localcontext(Context(prec=34, rounding=ROUND_FLOOR)):
                        target = quantity * target_value / value if value > 0 else Decimal(0)
                    if previous is not None:
                        old_targets = {
                            item.security_id: item.target_quantity for item in previous.targets
                        }
                        if security_id in old_targets:
                            target = min(target, old_targets[security_id])
                    targets.append(
                        ConcentrationTarget(
                            security_id=security_id,
                            target_quantity=target,
                            required_reduction_quantity=max(Decimal(0), quantity - target),
                        )
                    )
                blocked = any(
                    sum(
                        (
                            unit.exact_statistical_action_quantity or Decimal(0)
                            for unit in units
                            if unit.security_id == target.security_id
                        ),
                        Decimal(0),
                    )
                    < (target.required_reduction_quantity or Decimal(0))
                    for target in targets
                )
                obligation_id = (
                    "concentration-obligation-"
                    + sha256(
                        json.dumps(
                            [
                                command.authorization.portfolio_id,
                                snapshot.snapshot_id,
                                exposure.issuer_id,
                            ],
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()
                )
                if previous is not None and previous.obligation_id is not None:
                    obligation_id = previous.obligation_id
                gap = sum(
                    (
                        (target.required_reduction_quantity or Decimal(0))
                        * next(
                            unit.market_price
                            for unit in units
                            if unit.security_id == target.security_id
                            and unit.market_price is not None
                        )
                        for target in targets
                    ),
                    Decimal(0),
                )
                normal = False
                if pending and gap == 0 and value <= target_value:
                    state = "RESOLVED"
                    direction = None
                    normal = True
        elif pending and previous is not None:
            state = "REMEDIATION_REQUIRED"
            direction = "REDUCE"
            obligation_id = previous.obligation_id
            targets = [
                target.model_copy(update={"required_reduction_quantity": None})
                for target in previous.targets
            ]
            normal = False
        issuers.append(
            IssuerConcentration(
                issuer_id=exposure.issuer_id,
                current_market_exposure=value,
                position_weight=weight,
                state=state,
                new_exposure_blocked=not normal,
                direction=direction,
                obligation_id=obligation_id,
                obligation_started_at=(
                    previous.obligation_started_at
                    if pending and previous is not None
                    else snapshot.cutoff_at
                    if obligation_id is not None
                    else None
                ),
                obligation_risk_budget_version_id=(
                    previous.obligation_risk_budget_version_id
                    if pending and previous is not None
                    else budget.version_id
                    if obligation_id is not None and budget
                    else None
                ),
                obligation_thresholds=(
                    previous.obligation_thresholds
                    if pending and previous is not None
                    else budget.concentration
                    if obligation_id is not None and budget
                    else None
                ),
                targets=tuple(targets),
                exposure_gap=gap,
                execution_blocked=blocked,
            )
        )
    issuers.extend(
        issuer.model_copy(
            update={
                "current_market_exposure": None,
                "position_weight": None,
                "exposure_gap": None,
                "execution_blocked": True,
                "targets": tuple(
                    target.model_copy(update={"required_reduction_quantity": None})
                    for target in issuer.targets
                ),
            }
        )
        for issuer in missing_issuers
    )
    return ConcentrationOutcome(
        disposition="BLOCKED" if reasons else "ASSESSED",
        reasons=tuple(reasons) or ("CONCENTRATION_ASSESSED",),
        portfolio_id=command.authorization.portfolio_id,
        snapshot_id=snapshot.snapshot_id,
        cutoff_at=snapshot.cutoff_at,
        valuation_currency=snapshot.valuation_currency,
        portfolio_net_liquidation_equity=equity,
        risk_budget_version_id=budget.version_id if budget is not None else None,
        thresholds=budget.concentration if budget is not None else None,
        issuers=tuple(issuers),
    )
