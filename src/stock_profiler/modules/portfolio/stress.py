"""Parameterized portfolio gross-stress decisions from reconciled frozen facts."""

from __future__ import annotations

from datetime import datetime
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
    AuthoritativeLedgerEntry,
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
    risk_budget_version_id: str | None
    calculation_policy: StressCalculationPolicy | None
    budget: TwoThresholdBudget | None


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
    residual_restoration_gap: Decimal | None = None
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
        with localcontext(_DECIMAL_CONTEXT):
            still_confirmed = position.disposition == "RECONCILED" and _has_effective_sale(
                snapshot.authoritative_ledger,
                after=obligation.triggered_at,
                cutoff_at=snapshot.cutoff_at,
            )
        if not still_confirmed:
            obligation = obligation.model_copy(update={"status": "OUTSTANDING"})
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
        "risk_budget_version_id": (
            usage.authorization_snapshot.proposal.risk_budget.version_id
            if usage is not None and usage.allowed
            else None
        ),
        "calculation_policy": policy if usage is not None and usage.allowed else None,
        "budget": (
            usage.authorization_snapshot.proposal.risk_budget.stress
            if usage is not None and usage.allowed
            else None
        ),
    }
    unavailable = PortfolioStressOutcome(
        **common,
        state="UNKNOWN",
        reasons=tuple(reasons),
        new_exposure_blocked=True,
        obligation=obligation,
        execution_blocked=True,
    )
    if reasons:
        return unavailable
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
        loss = sum(
            (item.adverse_price_loss + item.disposal_friction for item in contributions),
            Decimal(0),
        )
        if equity <= 0:
            return unavailable.model_copy(
                update={
                    "reasons": ("STRESS_NET_EQUITY_NON_POSITIVE",),
                    "contributions": tuple(contributions),
                    "gross_stress_loss": loss,
                    "net_liquidation_equity": equity,
                }
            )
        assert usage is not None
        budget = usage.authorization_snapshot.proposal.risk_budget.stress
        if obligation is not None and obligation.status == "OUTSTANDING":
            obligation = obligation.model_copy(
                update={
                    "target_stress_ratio": min(obligation.target_stress_ratio, budget.target_ratio)
                }
            )
        state: Literal["NORMAL", "BUFFER", "HARD_BREACH"] = "NORMAL"
        if loss > budget.hard_ratio * equity:
            state = "HARD_BREACH"
            if obligation is None or obligation.status == "SATISFIED":
                obligation = StressObligation(
                    obligation_id=f"stress-obligation:{command.authorization_id}:{snapshot.snapshot_id}",
                    direction="REDUCE_TOTAL_STOCK_EXPOSURE",
                    target_stress_ratio=budget.target_ratio,
                    status="OUTSTANDING",
                    triggered_at=snapshot.cutoff_at,
                )
        elif loss > budget.target_ratio * equity:
            state = "BUFFER"
        if obligation is not None and loss <= obligation.target_stress_ratio * equity:
            confirmed_reduction = _has_effective_sale(
                snapshot.authoritative_ledger,
                after=obligation.triggered_at,
                cutoff_at=snapshot.cutoff_at,
            )
            if confirmed_reduction and position.disposition == "RECONCILED":
                obligation = obligation.model_copy(update={"status": "SATISFIED"})
        outstanding = obligation is not None and obligation.status == "OUTSTANDING"
        residual_gap: Decimal | None = None
        if (
            outstanding
            and obligation is not None
            and all(
                unit.exact_statistical_action_quantity is not None for unit in snapshot.action_units
            )
        ):
            # Liquidation friction is already reserved for the entire stock book.
            remaining_loss = sum(
                (
                    (unit.total_quantity - unit.exact_statistical_action_quantity)
                    * unit.market_price
                    * (policy.shock_ratio + policy.disposal_friction_ratio)
                    for unit in snapshot.action_units
                    if unit.total_quantity is not None
                    and unit.exact_statistical_action_quantity is not None
                    and unit.market_price is not None
                ),
                Decimal(0),
            )
            residual_gap = max(Decimal(0), remaining_loss - obligation.target_stress_ratio * equity)
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
            residual_restoration_gap=residual_gap,
            execution_blocked=outstanding and (residual_gap is None or residual_gap > 0),
        )


def _has_effective_sale(
    entries: tuple[AuthoritativeLedgerEntry, ...],
    *,
    after: datetime,
    cutoff_at: datetime,
) -> bool:
    """Count a fill only after applying its visible additive correction lineage."""
    indexed = {(entry.account_id, entry.entry_id): entry for entry in entries}
    quantities: dict[tuple[str, str], Decimal] = {}
    for entry in entries:
        if not ledger_entry_evidence_is_visible(entry, cutoff_at):
            return False
        root = entry
        visited: set[str] = set()
        while root.corrects_entry_id is not None:
            if root.entry_id in visited:
                return False
            visited.add(root.entry_id)
            target = indexed.get((root.account_id, root.corrects_entry_id))
            if target is None:
                return False
            root = target
        key = (root.account_id, root.entry_id)
        quantities[key] = quantities.get(key, Decimal(0)) + entry.quantity_delta
    return any(
        quantity < 0 and indexed[key].entry_type == "FILL" and indexed[key].occurred_at > after
        for key, quantity in quantities.items()
    )
