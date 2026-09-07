"""Parameterized portfolio gross-stress decisions from reconciled frozen facts."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Literal, TypedDict

from pydantic import AwareDatetime, Field

from stock_profiler.modules.portfolio.contracts import (
    PortfolioAuthorizationOutcome,
    PortfolioContract,
    StressCalculationPolicy,
    TwoThresholdBudget,
)
from stock_profiler.modules.position_management.contracts import (
    PositionReconciliationOutcome,
    PositionSnapshotCommand,
    ledger_entry_evidence_is_visible,
)

_DECIMAL_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN)
_LINEAGE_DENIALS = frozenset(
    {
        "STRESS_AUTHORIZATION_UNAVAILABLE",
        "STRESS_ACCOUNT_SCOPE_MISMATCH",
        "STRESS_RETAINED_SCOPE_NOT_COVERED",
        "STRESS_SNAPSHOT_NOT_FORWARD",
    }
)


class PortfolioStressCommand(PortfolioContract):
    operation: Literal["PORTFOLIO_STRESS_ASSESS"]
    contract_version: Literal["1.0.0"]
    portfolio_id: str = Field(min_length=1)
    authorization_id: str = Field(min_length=1)
    position_snapshot: PositionSnapshotCommand


class StressContribution(PortfolioContract):
    account_id: str
    security_id: str
    current_exposure: Decimal
    adverse_price_loss: Decimal
    disposal_friction: Decimal


class StressObligation(PortfolioContract):
    obligation_id: str
    direction: Literal["REDUCE_TOTAL_STOCK_EXPOSURE"]
    target_stress_ratio: Decimal
    status: Literal["OUTSTANDING", "SATISFIED"]
    triggered_at: AwareDatetime


class _StressIdentity(TypedDict):
    portfolio_id: str
    authorization_id: str
    snapshot_id: str
    cutoff_at: AwareDatetime
    account_ids: tuple[str, ...]


class PortfolioStressOutcome(PortfolioContract):
    state: Literal["NORMAL", "BUFFER", "HARD_BREACH", "UNKNOWN"]
    reasons: tuple[str, ...]
    portfolio_id: str
    authorization_id: str
    snapshot_id: str
    cutoff_at: AwareDatetime
    account_ids: tuple[str, ...]
    gross_stress_loss: Decimal | None = None
    net_liquidation_equity: Decimal | None = None
    stress_ratio: Decimal | None = None
    contributions: tuple[StressContribution, ...] = ()
    new_exposure_blocked: bool
    obligation: StressObligation | None = None
    execution_blocked: bool = False
    risk_budget_version_id: str | None = None
    calculation_policy: StressCalculationPolicy | None = None
    budget: TwoThresholdBudget | None = None


def assess_stress(
    command: PortfolioStressCommand,
    authorization: PortfolioAuthorizationOutcome,
    position: PositionReconciliationOutcome,
    *,
    history: tuple[PortfolioStressOutcome, ...] = (),
) -> PortfolioStressOutcome:
    """Keep stress independent of the framework's original business result."""
    usage = authorization.usage
    reasons: list[str] = []
    if usage is None or not usage.allowed:
        reasons.append("STRESS_AUTHORIZATION_UNAVAILABLE")
    elif set(usage.authorization_snapshot.proposal.snapshot.selected_account_ids) != set(
        command.position_snapshot.account_ids
    ):
        reasons.append("STRESS_ACCOUNT_SCOPE_MISMATCH")
    policy = (
        usage.authorization_snapshot.proposal.risk_budget.stress_calculation
        if usage is not None
        else None
    )
    if policy is None:
        reasons.append("STRESS_POLICY_REQUIRED")
    snapshot = position.snapshot
    lineage = tuple(item for item in history if not _LINEAGE_DENIALS.intersection(item.reasons))
    visible = tuple(item for item in lineage if item.cutoff_at < snapshot.cutoff_at)
    prior = visible[-1] if visible else None
    obligation = prior.obligation if prior is not None else None
    if obligation is not None and obligation.status == "SATISFIED":
        obligation = None
    if any(item.cutoff_at >= snapshot.cutoff_at for item in lineage):
        reasons.append("STRESS_SNAPSHOT_NOT_FORWARD")
    if any(
        not set(item.account_ids).issubset(command.position_snapshot.account_ids)
        for item in lineage
    ):
        reasons.append("STRESS_RETAINED_SCOPE_NOT_COVERED")
        obligation = None
    if snapshot.total_account_equity is None or any(
        exposure.current_market_exposure is None for exposure in snapshot.issuer_exposures
    ):
        reasons.append("STRESS_VALUATION_UNKNOWN")
    common: _StressIdentity = {
        "portfolio_id": command.portfolio_id,
        "authorization_id": command.authorization_id,
        "snapshot_id": snapshot.snapshot_id,
        "cutoff_at": snapshot.cutoff_at,
        "account_ids": command.position_snapshot.account_ids,
    }
    if reasons:
        return PortfolioStressOutcome(
            **common,
            state="UNKNOWN",
            reasons=tuple(reasons),
            new_exposure_blocked=True,
            obligation=obligation,
            execution_blocked=True,
        )
    assert policy is not None and snapshot.total_account_equity is not None
    with localcontext(_DECIMAL_CONTEXT):
        contributions: list[StressContribution] = []
        for unit in snapshot.action_units:
            assert unit.total_quantity is not None and unit.market_price is not None
            exposure = unit.total_quantity * unit.market_price
            contributions.append(
                StressContribution(
                    account_id=unit.account_id,
                    security_id=unit.security_id,
                    current_exposure=exposure,
                    adverse_price_loss=exposure * policy.shock_ratio,
                    disposal_friction=exposure * policy.disposal_friction_ratio,
                )
            )
        friction = sum((item.disposal_friction for item in contributions), Decimal(0))
        equity = snapshot.total_account_equity - friction
        if equity <= 0:
            return PortfolioStressOutcome(
                **common,
                state="UNKNOWN",
                reasons=("STRESS_NET_EQUITY_NON_POSITIVE",),
                new_exposure_blocked=True,
                obligation=obligation,
                execution_blocked=True,
            )
        loss = sum(
            (item.adverse_price_loss + item.disposal_friction for item in contributions),
            Decimal(0),
        )
        assert usage is not None
        budget = usage.authorization_snapshot.proposal.risk_budget.stress
        state: Literal["NORMAL", "BUFFER", "HARD_BREACH"] = "NORMAL"
        if loss > budget.hard_ratio * equity:
            state = "HARD_BREACH"
            obligation = obligation or StressObligation(
                obligation_id=f"stress-obligation:{command.authorization_id}:{snapshot.snapshot_id}",
                direction="REDUCE_TOTAL_STOCK_EXPOSURE",
                target_stress_ratio=budget.target_ratio,
                status="OUTSTANDING",
                triggered_at=snapshot.cutoff_at,
            )
        elif loss > budget.target_ratio * equity:
            state = "BUFFER"
        if obligation is not None and loss <= obligation.target_stress_ratio * equity:
            confirmed_reduction = any(
                entry.entry_type == "FILL"
                and entry.quantity_delta < 0
                and entry.occurred_at > obligation.triggered_at
                and ledger_entry_evidence_is_visible(entry, snapshot.cutoff_at)
                for entry in snapshot.authoritative_ledger
            )
            if confirmed_reduction and position.disposition == "RECONCILED":
                obligation = obligation.model_copy(update={"status": "SATISFIED"})
        outstanding = obligation is not None and obligation.status == "OUTSTANDING"
        expired = usage.checked_at >= usage.authorization_snapshot.proposal.risk_budget.expires_at
        return PortfolioStressOutcome(
            **common,
            state=state,
            reasons=("RISK_BUDGET_EXPIRED",) if expired else (),
            gross_stress_loss=loss,
            net_liquidation_equity=equity,
            stress_ratio=loss / equity,
            contributions=tuple(contributions),
            new_exposure_blocked=state != "NORMAL" or outstanding or expired,
            obligation=obligation,
            risk_budget_version_id=usage.authorization_snapshot.proposal.risk_budget.version_id,
            calculation_policy=policy,
            budget=budget,
            execution_blocked=outstanding
            and any(
                unit.exact_statistical_action_quantity is None
                or (
                    unit.total_quantity is not None
                    and unit.total_quantity > 0
                    and unit.exact_statistical_action_quantity == 0
                )
                for unit in snapshot.action_units
            ),
        )
