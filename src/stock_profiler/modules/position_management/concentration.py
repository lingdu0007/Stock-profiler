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
    ConcentrationQuantityBasis,
    ConcentrationTarget,
    IssuerConcentration,
)
from stock_profiler.modules.position_management.contracts import (
    PositionActionUnit,
    PositionReconciliationOutcome,
)


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
    ordered_history = history.outcomes
    stale = any(item.cutoff_at >= snapshot.cutoff_at for item in ordered_history)
    if stale:
        reasons.append("CONCENTRATION_SNAPSHOT_OUT_OF_ORDER")
    prior_issuers: dict[str, IssuerConcentration] = {}
    for item in ordered_history:
        if item.cutoff_at > snapshot.cutoff_at:
            continue
        for issuer in item.issuers:
            prior = prior_issuers.get(issuer.issuer_id)
            if prior is not None and prior.direction == "REDUCE" and issuer.obligation_id is None:
                continue
            prior_issuers[issuer.issuer_id] = issuer
    current_issuer_ids = {item.issuer_id for item in snapshot.issuer_exposures}
    missing_issuers = [
        issuer
        for issuer in prior_issuers.values()
        if issuer.direction == "REDUCE" and issuer.issuer_id not in current_issuer_ids
    ]
    closed_issuers = {
        issuer.issuer_id
        for issuer in missing_issuers
        if all(_closed_by_fills(position, issuer, target.security_id) for target in issuer.targets)
    }
    if any(issuer.issuer_id not in closed_issuers for issuer in missing_issuers):
        reasons.append("CONCENTRATION_ISSUER_LINEAGE_UNRESOLVED")
    pending_bases: dict[str, tuple[ConcentrationQuantityBasis, ...]] = {}
    for issuer in prior_issuers.values():
        if issuer.direction != "REDUCE":
            continue
        basis = issuer.obligation_quantity_basis
        if (
            basis
            and usage is not None
            and usage.allowed
            and set(usage.authorization_snapshot.proposal.snapshot.selected_account_ids)
            == set(command.position_snapshot.account_ids)
        ):
            basis = _extend_quantity_basis(command, position, issuer.targets, basis)
        pending_bases[issuer.issuer_id] = basis
        if not _quantities_explained_by_fills(position, basis):
            reasons.append("CONCENTRATION_EXECUTION_LINEAGE_UNRESOLVED")
        current_codes = {
            unit.security_id for unit in snapshot.action_units if unit.issuer_id == issuer.issuer_id
        }
        target_codes = {target.security_id for target in issuer.targets}
        if current_codes - target_codes or any(
            not _closed_by_fills(position, issuer, code) for code in target_codes - current_codes
        ):
            reasons.append("CONCENTRATION_SECURITY_LINEAGE_UNRESOLVED")
            break
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
    if position.disposition != "RECONCILED":
        reasons.append("CONCENTRATION_POSITION_FACTS_UNRESOLVED")
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
                targets = _quantity_targets(units, previous, target_value)
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
                            (
                                unit.market_price
                                for unit in units
                                if unit.security_id == target.security_id
                                and unit.market_price is not None
                            ),
                            Decimal(0),
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
                obligation_quantity_basis=(
                    pending_bases.get(exposure.issuer_id, ())
                    if pending
                    else _extend_quantity_basis(command, position, tuple(targets), ())
                    if obligation_id is not None
                    else ()
                ),
                targets=tuple(targets),
                exposure_gap=gap,
                execution_blocked=blocked,
            )
        )
    for issuer in missing_issuers:
        closed = issuer.issuer_id in closed_issuers and not reasons and equity is not None
        issuers.append(
            issuer.model_copy(
                update={
                    "state": "RESOLVED" if closed else "REMEDIATION_REQUIRED",
                    "direction": None if closed else "REDUCE",
                    "new_exposure_blocked": not closed,
                    "current_market_exposure": Decimal(0) if closed else None,
                    "position_weight": Decimal(0) if closed else None,
                    "exposure_gap": Decimal(0) if closed else None,
                    "execution_blocked": not closed,
                    "obligation_quantity_basis": pending_bases.get(issuer.issuer_id, ()),
                    "targets": tuple(
                        target.model_copy(
                            update={"required_reduction_quantity": Decimal(0) if closed else None}
                        )
                        for target in issuer.targets
                    ),
                }
            )
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


def _quantity_targets(
    units: tuple[PositionActionUnit, ...],
    previous: IssuerConcentration | None,
    target_value: Decimal,
) -> list[ConcentrationTarget]:
    quantities: dict[str, Decimal] = {}
    prices: dict[str, Decimal] = {}
    for unit in units:
        assert unit.total_quantity is not None and unit.market_price is not None
        quantities[unit.security_id] = (
            quantities.get(unit.security_id, Decimal(0)) + unit.total_quantity
        )
        prices[unit.security_id] = unit.market_price
    caps = (
        {target.security_id: target.target_quantity for target in previous.targets}
        if previous is not None and previous.obligation_id is not None
        else dict(quantities)
    )
    retained = {code: min(quantities.get(code, Decimal(0)), cap) for code, cap in caps.items()}
    retained_value = sum(
        (quantity * prices.get(code, Decimal(0)) for code, quantity in retained.items()),
        Decimal(0),
    )
    if retained_value > target_value:
        with localcontext(Context(prec=34, rounding=ROUND_FLOOR)):
            caps = {
                code: quantity * target_value / retained_value
                for code, quantity in retained.items()
            }
    return [
        ConcentrationTarget(
            security_id=code,
            target_quantity=cap,
            required_reduction_quantity=max(Decimal(0), quantities.get(code, Decimal(0)) - cap),
        )
        for code, cap in sorted(caps.items())
    ]


def _closed_by_fills(
    position: PositionReconciliationOutcome,
    issuer: IssuerConcentration,
    security_id: str,
) -> bool:
    if position.disposition != "RECONCILED" or issuer.obligation_started_at is None:
        return False
    entries = tuple(
        entry
        for entry in position.snapshot.authoritative_ledger
        if entry.security_id == security_id
    )
    subsequent = tuple(
        entry for entry in entries if entry.occurred_at > issuer.obligation_started_at
    )
    return (
        bool(entries)
        and sum((entry.quantity_delta for entry in entries), Decimal(0)) == 0
        and sum(
            (entry.quantity_delta for entry in subsequent if entry.entry_type == "FILL"),
            Decimal(0),
        )
        < 0
        and sum(
            (entry.quantity_delta for entry in subsequent if entry.entry_type != "FILL"),
            Decimal(0),
        )
        == 0
        and not any(
            unit.security_id == security_id and unit.total_quantity != 0
            for unit in position.snapshot.action_units
        )
    )


def _quantities_explained_by_fills(
    position: PositionReconciliationOutcome, basis: tuple[ConcentrationQuantityBasis, ...]
) -> bool:
    if not basis:
        return False
    for security_id in {item.security_id for item in basis}:
        quantities = tuple(
            unit.total_quantity
            for unit in position.snapshot.action_units
            if unit.security_id == security_id
        )
        if any(quantity is None for quantity in quantities):
            return False
        current = sum((quantity for quantity in quantities if quantity is not None), Decimal(0))
        expected = Decimal(0)
        for item in basis:
            if item.security_id != security_id:
                continue
            expected += item.quantity + sum(
                (
                    entry.quantity_delta
                    for entry in position.snapshot.authoritative_ledger
                    if entry.account_id == item.account_id
                    and entry.security_id == security_id
                    and entry.entry_type == "FILL"
                    and item.cutoff_at < entry.occurred_at <= position.snapshot.cutoff_at
                ),
                Decimal(0),
            )
        if current < expected:
            return False
    return True


def _extend_quantity_basis(
    command: ConcentrationCommand,
    position: PositionReconciliationOutcome,
    targets: tuple[ConcentrationTarget, ...],
    prior: tuple[ConcentrationQuantityBasis, ...],
) -> tuple[ConcentrationQuantityBasis, ...]:
    if position.snapshot.total_account_equity is None:
        return prior
    basis = {(item.account_id, item.security_id): item for item in prior}
    for account_id in sorted(command.position_snapshot.account_ids):
        for target in targets:
            key = (account_id, target.security_id)
            if key in basis:
                continue
            quantities = tuple(
                unit.total_quantity
                for unit in position.snapshot.action_units
                if unit.account_id == account_id and unit.security_id == target.security_id
            )
            if any(quantity is None for quantity in quantities):
                return prior
            basis[key] = ConcentrationQuantityBasis(
                account_id=account_id,
                security_id=target.security_id,
                quantity=sum(
                    (quantity for quantity in quantities if quantity is not None), Decimal(0)
                ),
                cutoff_at=position.snapshot.cutoff_at,
            )
    return tuple(basis.values())
