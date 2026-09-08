"""Cash reserve adjudication over retained authorization and reconciled broker facts."""

import calendar
from decimal import Context, Decimal, localcontext
from typing import Literal

from pydantic import Field

from stock_profiler.modules.portfolio.contracts import (
    DatedCashObligation,
    PortfolioAuthorizationOutcome,
    PortfolioContract,
)
from stock_profiler.modules.portfolio.liquidity_funding import (
    CashTransferRoute,
    MaximumFundingLeg,
    ObligationFunding,
    SaleFundingTerms,
    assess_funding,
)
from stock_profiler.modules.position_management.contracts import (
    PositionEvidence,
    PositionReconciliationOutcome,
    PositionSnapshotCommand,
)


class SettledObligationCoverage(PortfolioContract):
    receipt_id: str = Field(min_length=1)
    kind: Literal["EXTERNAL_SETTLED_PAYMENT"]
    obligation_id: str = Field(min_length=1)
    amount: Decimal = Field(gt=0)
    evidence: PositionEvidence


class LiquidityCommand(PortfolioContract):
    operation: Literal["LIQUIDITY_ASSESS"]
    contract_version: Literal["1.0.0"]
    synthetic: Literal[True]
    generator_version: str = Field(min_length=1)
    seed: int
    portfolio_id: str = Field(min_length=1)
    authorization_id: str = Field(min_length=1)
    position_snapshot: PositionSnapshotCommand
    expected_purchase_fees: Decimal = Field(ge=0)
    expected_liquidation_fees: Decimal = Field(ge=0)
    sale_terms: tuple[SaleFundingTerms, ...]
    transfer_routes: tuple[CashTransferRoute, ...]
    settled_coverage: tuple[SettledObligationCoverage, ...]
    cost_evidence: PositionEvidence | None


class LiquidityOutcome(PortfolioContract):
    disposition: Literal[
        "AVAILABLE",
        "ZERO_DEPLOYABLE_CASH",
        "REMEDIATION_REQUIRED",
        "EVIDENCE_FAILED",
        "FUNDING_INFEASIBLE",
    ]
    reasons: tuple[str, ...]
    authorization_id: str
    protection_authorization: PortfolioAuthorizationOutcome | None = None
    purchase_authorization: PortfolioAuthorizationOutcome | None = None
    position_snapshot: PositionReconciliationOutcome
    net_liquidation_equity: Decimal | None = None
    normal_cash_target: Decimal | None = None
    hard_cash_floor: Decimal | None = None
    qualified_cash: Decimal | None = None
    reserved_buy_cash: Decimal | None = None
    six_month_obligations: Decimal | None = None
    deployable_purchase_cash: Decimal | None = None
    remediation_shortfall: Decimal | None = None
    remediation_id: str | None = None
    restoration_cash_confirmed: bool = False
    retained_remediation_shortfall: Decimal | None = None
    maximum_fundable_cash: Decimal | None = None
    uncovered_obligation_gap: Decimal | None = None
    maximum_funding_plan: tuple[MaximumFundingLeg, ...] = ()
    obligation_funding: tuple[ObligationFunding, ...] = ()
    settled_coverage: tuple[SettledObligationCoverage, ...] = ()
    new_exposure_blocked: bool = True


def assess_liquidity(
    command: LiquidityCommand,
    authorization: PortfolioAuthorizationOutcome,
    position: PositionReconciliationOutcome,
    *,
    purchase_authorization: PortfolioAuthorizationOutcome,
    event_id: str,
    history: tuple[LiquidityOutcome, ...],
) -> LiquidityOutcome:
    """Use explicit frozen policy only; unknown facts never become a zero balance."""
    usage = authorization.usage
    latest = max(
        (
            (index, outcome)
            for index, outcome in enumerate(history)
            if outcome.disposition != "EVIDENCE_FAILED" or outcome.remediation_id is not None
        ),
        key=lambda item: (item[1].position_snapshot.snapshot.cutoff_at, item[0]),
        default=None,
    )
    prior = latest[1] if latest is not None else None
    cash_progress = (
        _last_cash_assessment(history, prior.remediation_id)
        if prior is not None and prior.remediation_id is not None
        else None
    )
    history_scope_incomplete = (
        prior is not None
        and prior.remediation_id is not None
        and (
            cash_progress is None
            or not {
                cash.account_id for cash in cash_progress.position_snapshot.snapshot.cash_states
            }.issubset(command.position_snapshot.account_ids)
        )
    )
    if history_scope_incomplete:
        prior = None
    prior_remediation_id = prior.remediation_id if prior is not None else None
    retained_shortfall = (
        prior.remediation_shortfall
        if prior is not None and prior.remediation_shortfall is not None
        else prior.retained_remediation_shortfall
        if prior is not None
        else None
    )
    coverage: tuple[SettledObligationCoverage, ...] = ()

    def evidence_failure(reasons: tuple[str, ...]) -> LiquidityOutcome:
        return LiquidityOutcome(
            disposition="EVIDENCE_FAILED",
            reasons=reasons,
            authorization_id=command.authorization_id,
            protection_authorization=authorization,
            purchase_authorization=purchase_authorization,
            position_snapshot=position,
            remediation_id=prior_remediation_id,
            retained_remediation_shortfall=retained_shortfall,
            settled_coverage=coverage,
            restoration_cash_confirmed=(
                cash_progress.restoration_cash_confirmed
                if cash_progress is not None and not history_scope_incomplete
                else False
            ),
        )

    if history_scope_incomplete:
        return evidence_failure(("LIQUIDITY_HISTORY_SCOPE_INCOMPLETE",))
    if usage is None or not usage.allowed:
        return evidence_failure(authorization.reasons)
    proposal = usage.authorization_snapshot.proposal
    snapshot = position.snapshot
    with localcontext(Context(prec=38)):
        outstanding, coverage = _outstanding_obligations(
            usage.unfinished_cash_obligations, command, history
        )
    if outstanding is None:
        return evidence_failure(("SETTLED_OBLIGATION_COVERAGE_INVALID",))
    if command.cost_evidence is None or command.cost_evidence.problem_codes(
        snapshot.cutoff_at, require_current_completeness=True
    ):
        return evidence_failure(("LIQUIDITY_COST_EVIDENCE_FAILED",))
    if (
        set(proposal.snapshot.selected_account_ids)
        != {cash.account_id for cash in snapshot.cash_states}
        or position.disposition != "RECONCILED"
        or snapshot.total_account_equity is None
    ):
        return evidence_failure(("LIQUIDITY_POSITION_EVIDENCE_FAILED",))
    with localcontext(Context(prec=38)):
        equity = snapshot.total_account_equity - command.expected_liquidation_fees
        trading = sum(
            (cash.trading_cash for cash in snapshot.cash_states if cash.trading_cash is not None),
            Decimal(0),
        )
        reserved = sum(
            (
                order.reserved_cash
                for order in snapshot.unfinished_orders
                if order.side == "BUY" and order.reserved_cash is not None
            ),
            Decimal(0),
        )
        # Final broker trading cash already excludes frozen unfinished-buy reserves.
        qualified = max(Decimal(0), trading - command.expected_purchase_fees)
        cutoff = snapshot.cutoff_at
        month_index = cutoff.month - 1 + 6
        year = cutoff.year + month_index // 12
        month = month_index % 12 + 1
        horizon = cutoff.replace(
            year=year, month=month, day=min(cutoff.day, calendar.monthrange(year, month)[1])
        )
        obligations = sum(
            (item.amount for item in outstanding if item.latest_usable_at <= horizon),
            Decimal(0),
        )
        all_obligations = sum((item.amount for item in outstanding), Decimal(0))
        remaining_equity = max(Decimal(0), equity - obligations)
        system_reserve = remaining_equity * proposal.risk_budget.cash.target_ratio
        target = obligations + system_reserve
        floor = obligations + remaining_equity * proposal.risk_budget.cash.hard_ratio
        deployable = max(Decimal(0), qualified - system_reserve - all_obligations)
        purchase_blocked = purchase_authorization.disposition != "APPROVED" or equity <= 0
        if purchase_blocked:
            deployable = Decimal(0)
        restoration = (
            max(Decimal(0), target - qualified)
            if qualified < floor or prior_remediation_id is not None
            else Decimal(0)
        )
        funding = assess_funding(snapshot, command.sale_terms, outstanding, command.transfer_routes)
        cash_restoration_confirmed = (
            prior_remediation_id is not None
            and restoration == 0
            and _restoration_confirmed(prior_remediation_id, position, qualified, coverage, history)
        )
        remediation_active = restoration > 0 or (
            prior_remediation_id is not None
            and (bool(funding.reasons) or not cash_restoration_confirmed)
        )
        disposition: Literal["AVAILABLE", "ZERO_DEPLOYABLE_CASH", "REMEDIATION_REQUIRED"]
        if remediation_active:
            disposition = "REMEDIATION_REQUIRED"
        elif deployable == 0:
            disposition = "ZERO_DEPLOYABLE_CASH"
        else:
            disposition = "AVAILABLE"
        funding_infeasible = (
            bool(outstanding)
            and obligations >= equity
            or (funding.uncovered_gap is not None and funding.uncovered_gap > 0)
        )
        final_disposition = (
            "EVIDENCE_FAILED"
            if funding.reasons
            else "FUNDING_INFEASIBLE"
            if funding_infeasible
            else disposition
        )
        if funding.reasons or funding_infeasible or remediation_active:
            deployable = Decimal(0)
        return LiquidityOutcome(
            disposition=final_disposition,
            reasons=(
                f"LIQUIDITY_{final_disposition}",
                *(
                    purchase_authorization.reasons
                    if purchase_authorization.disposition != "APPROVED"
                    else ()
                ),
                *(("NONPOSITIVE_NET_LIQUIDATION_EQUITY",) if equity <= 0 else ()),
                *(
                    ("RESTORATION_CONFIRMATION_REQUIRED",)
                    if remediation_active and restoration == 0
                    else ()
                ),
                *funding.reasons,
            ),
            authorization_id=command.authorization_id,
            protection_authorization=authorization,
            purchase_authorization=purchase_authorization,
            position_snapshot=position,
            net_liquidation_equity=equity,
            normal_cash_target=target,
            hard_cash_floor=floor,
            qualified_cash=qualified,
            reserved_buy_cash=reserved,
            six_month_obligations=obligations,
            deployable_purchase_cash=deployable,
            remediation_shortfall=restoration,
            remediation_id=(prior_remediation_id or event_id) if remediation_active else None,
            restoration_cash_confirmed=cash_restoration_confirmed,
            retained_remediation_shortfall=retained_shortfall if funding.reasons else None,
            maximum_fundable_cash=funding.maximum_fundable_cash,
            uncovered_obligation_gap=funding.uncovered_gap,
            maximum_funding_plan=funding.plan,
            obligation_funding=funding.obligations,
            settled_coverage=coverage,
            new_exposure_blocked=qualified < target
            or purchase_blocked
            or funding_infeasible
            or remediation_active
            or bool(funding.reasons),
        )


def _last_cash_assessment(
    history: tuple[LiquidityOutcome, ...],
    remediation_id: str,
) -> LiquidityOutcome | None:
    latest = max(
        (
            (index, item)
            for index, item in enumerate(history)
            if item.remediation_id == remediation_id and item.qualified_cash is not None
        ),
        key=lambda pair: (pair[1].position_snapshot.snapshot.cutoff_at, pair[0]),
        default=None,
    )
    return latest[1] if latest is not None else None


def _restoration_confirmed(
    remediation_id: str,
    position: PositionReconciliationOutcome,
    qualified_cash: Decimal,
    coverage: tuple[SettledObligationCoverage, ...],
    history: tuple[LiquidityOutcome, ...],
) -> bool:
    progress = _last_cash_assessment(history, remediation_id)
    if progress is None or progress.qualified_cash is None:
        return False
    if progress.restoration_cash_confirmed:
        return True
    previous_receipts = {item.receipt_id for item in progress.settled_coverage}
    if any(item.receipt_id not in previous_receipts for item in coverage):
        return True
    if qualified_cash <= progress.qualified_cash:
        return False
    trigger = next(item for item in history if item.remediation_id == remediation_id)
    original_snapshot = trigger.position_snapshot.snapshot
    original_entries = {
        (entry.account_id, entry.entry_id) for entry in original_snapshot.authoritative_ledger
    }
    # A price-only target change is not a broker-confirmed cash restoration.
    return any(
        entry.entry_type == "FILL"
        and entry.quantity_delta < 0
        and entry.cash_delta > 0
        and entry.occurred_at > original_snapshot.cutoff_at
        and (entry.account_id, entry.entry_id) not in original_entries
        for entry in position.snapshot.authoritative_ledger
    )


def _outstanding_obligations(
    obligations: tuple[DatedCashObligation, ...],
    command: LiquidityCommand,
    history: tuple[LiquidityOutcome, ...],
) -> tuple[tuple[DatedCashObligation, ...] | None, tuple[SettledObligationCoverage, ...]]:
    retained: dict[str, SettledObligationCoverage] = {}
    for previous in history:
        for receipt in previous.settled_coverage:
            if receipt.receipt_id in retained and retained[receipt.receipt_id] != receipt:
                return None, ()
            retained[receipt.receipt_id] = receipt
    if len({item.receipt_id for item in command.settled_coverage}) != len(command.settled_coverage):
        return None, ()
    for receipt in command.settled_coverage:
        if (
            receipt.evidence.problem_codes(
                command.position_snapshot.cutoff_at, require_current_completeness=False
            )
            or receipt.receipt_id in retained
            and retained[receipt.receipt_id] != receipt
        ):
            return None, ()
        retained[receipt.receipt_id] = receipt
    remaining = {item.obligation_id: item.amount for item in obligations}
    for receipt in retained.values():
        if receipt.obligation_id not in remaining:
            return None, ()
        remaining[receipt.obligation_id] -= receipt.amount
        if remaining[receipt.obligation_id] < 0:
            return None, ()
    return (
        tuple(
            item.model_copy(update={"amount": remaining[item.obligation_id]})
            for item in obligations
            if remaining[item.obligation_id] > 0
        ),
        tuple(retained[key] for key in sorted(retained)),
    )
