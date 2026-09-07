"""Synthetic, immutable decision-portfolio contracts for the frozen D0 seam."""

from __future__ import annotations

import calendar
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, TypeAlias

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class PortfolioContract(BaseModel):
    """Reject fields outside the versioned portfolio contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CompleteAccountSnapshot(PortfolioContract):
    """One whole account at the portfolio cutoff, never a virtual allocation slice."""

    account_id: str = Field(min_length=1)
    account_type: str = Field(min_length=1)
    currency: str = Field(min_length=1)
    permissions: tuple[str, ...] = Field(min_length=1)
    scope: Literal["FULL_ACCOUNT"]
    captured_at: AwareDatetime
    cash_fact_id: str = Field(min_length=1)
    positions_fact_id: str = Field(min_length=1)
    receivables_fact_id: str = Field(min_length=1)
    payables_fact_id: str = Field(min_length=1)
    unfinished_trades_fact_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_permissions(self) -> CompleteAccountSnapshot:
        if len(set(self.permissions)) != len(self.permissions):
            raise ValueError("account permissions must be unique")
        return self


class PortfolioSnapshot(PortfolioContract):
    """The common-cutoff universe from which full accounts can be selected."""

    snapshot_id: str = Field(min_length=1)
    cutoff_at: AwareDatetime
    accounts: tuple[CompleteAccountSnapshot, ...] = Field(min_length=1)
    selected_account_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_complete_account_selection(self) -> PortfolioSnapshot:
        account_ids = tuple(account.account_id for account in self.accounts)
        if len(set(account_ids)) != len(account_ids):
            raise ValueError("portfolio snapshot accounts must be unique")
        if len(set(self.selected_account_ids)) != len(self.selected_account_ids):
            raise ValueError("portfolio selection accounts must be unique")
        if not set(self.selected_account_ids).issubset(account_ids):
            raise ValueError("portfolio selection must use captured full accounts")
        if any(account.captured_at != self.cutoff_at for account in self.accounts):
            raise ValueError("portfolio accounts must share the portfolio cutoff")
        return self

    @property
    def selected_accounts(self) -> tuple[CompleteAccountSnapshot, ...]:
        selected = set(self.selected_account_ids)
        return tuple(account for account in self.accounts if account.account_id in selected)

    @property
    def account_ids(self) -> tuple[str, ...]:
        """Preserve the complete candidate-account universe that a report can expose."""
        return tuple(account.account_id for account in self.accounts)


class TwoThresholdBudget(PortfolioContract):
    target_ratio: Decimal = Field(gt=0, lt=1)
    hard_ratio: Decimal = Field(gt=0, lt=1)

    @model_validator(mode="after")
    def validate_threshold_order(self) -> TwoThresholdBudget:
        if self.target_ratio >= self.hard_ratio:
            raise ValueError("target ratio must be lower than hard ratio")
        return self


class CashBudget(PortfolioContract):
    target_ratio: Decimal = Field(gt=0, lt=1)
    hard_ratio: Decimal = Field(gt=0, lt=1)

    @model_validator(mode="after")
    def validate_threshold_order(self) -> CashBudget:
        if self.target_ratio <= self.hard_ratio:
            raise ValueError("cash target ratio must be higher than hard ratio")
        return self


class DrawdownBudget(PortfolioContract):
    caution_ratio: Decimal = Field(gt=0, lt=1)
    defensive_ratio: Decimal = Field(gt=0, lt=1)
    preservation_ratio: Decimal = Field(gt=0, lt=1)

    @model_validator(mode="after")
    def validate_threshold_order(self) -> DrawdownBudget:
        if not self.caution_ratio < self.defensive_ratio < self.preservation_ratio:
            raise ValueError("drawdown ratios must be strictly increasing")
        return self


class DeterministicProtectionFloor(PortfolioContract):
    """The actions retained when an authorization can no longer admit new exposure."""

    new_exposure_blocked: Literal[True]
    retained_directions: tuple[Literal["REDUCE", "EXIT"], ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_retained_directions(self) -> DeterministicProtectionFloor:
        if len(set(self.retained_directions)) != len(self.retained_directions):
            raise ValueError("protection directions must be unique")
        return self


class PersonalRiskBudget(PortfolioContract):
    """The explicit synthetic risk configuration bound to exactly six calendar months."""

    contract_version: Literal["1.0.0"]
    version_id: str = Field(min_length=1)
    synthetic: Literal[True]
    generator_version: str = Field(min_length=1)
    seed: int
    effective_at: AwareDatetime
    expires_at: AwareDatetime
    concentration: TwoThresholdBudget
    stress: TwoThresholdBudget
    cash: CashBudget
    drawdown: DrawdownBudget
    downside_grid: tuple[Decimal, ...] = Field(min_length=1)
    protection_floor: DeterministicProtectionFloor

    @model_validator(mode="after")
    def validate_duration_and_grid(self) -> PersonalRiskBudget:
        if self.expires_at != _add_calendar_months(self.effective_at, 6):
            raise ValueError(
                "risk budget must expire exactly six calendar months after effective_at"
            )
        if any(value <= 0 or value >= 1 for value in self.downside_grid):
            raise ValueError("downside grid values must be ratios between zero and one")
        if tuple(sorted(self.downside_grid)) != self.downside_grid:
            raise ValueError("downside grid values must be ordered")
        if len(set(self.downside_grid)) != len(self.downside_grid):
            raise ValueError("downside grid values must be unique")
        return self

    def relaxes(self, previous: PersonalRiskBudget) -> bool:
        """Conservatively identify a successor that permits more statistical risk."""
        return (
            self.concentration.target_ratio > previous.concentration.target_ratio
            or self.concentration.hard_ratio > previous.concentration.hard_ratio
            or self.stress.target_ratio > previous.stress.target_ratio
            or self.stress.hard_ratio > previous.stress.hard_ratio
            or self.cash.target_ratio < previous.cash.target_ratio
            or self.cash.hard_ratio < previous.cash.hard_ratio
            or self.drawdown.caution_ratio > previous.drawdown.caution_ratio
            or self.drawdown.defensive_ratio > previous.drawdown.defensive_ratio
            or self.drawdown.preservation_ratio > previous.drawdown.preservation_ratio
            or self.downside_grid != previous.downside_grid
            or not set(previous.protection_floor.retained_directions).issubset(
                self.protection_floor.retained_directions
            )
        )


class DatedCashObligation(PortfolioContract):
    """A forward-recorded cash requirement that the selected portfolio must preserve."""

    obligation_id: str = Field(min_length=1)
    amount: Decimal = Field(gt=0)
    purpose: str = Field(min_length=1)
    latest_usable_at: AwareDatetime
    target_account_id: str = Field(min_length=1)


class PortfolioProposal(PortfolioContract):
    """The complete selection and policy snapshot shown before a user confirmation."""

    portfolio_id: str = Field(min_length=1)
    snapshot: PortfolioSnapshot
    risk_budget: PersonalRiskBudget
    cash_obligations: tuple[DatedCashObligation, ...] = ()

    @model_validator(mode="after")
    def validate_proposal_binding(self) -> PortfolioProposal:
        if self.risk_budget.effective_at < self.snapshot.cutoff_at:
            raise ValueError("risk budget cannot become effective before the portfolio cutoff")
        if len({item.obligation_id for item in self.cash_obligations}) != len(
            self.cash_obligations
        ):
            raise ValueError("cash obligations must be unique")
        selected = set(self.snapshot.selected_account_ids)
        for obligation in self.cash_obligations:
            if obligation.target_account_id not in selected:
                raise ValueError("cash obligation must target a selected account")
        return self

    def evidence_available_by(self, cutoff: datetime) -> bool:
        """Keep the account snapshot within the frozen information set."""
        return self.snapshot.cutoff_at <= cutoff


class PortfolioPreviewCommand(PortfolioContract):
    operation: Literal["PORTFOLIO_PREVIEW"]
    proposal: PortfolioProposal


class RiskBudgetRelaxationEvidence(PortfolioContract):
    """Synthetic proof required before a successor can relax a frozen risk budget."""

    evidence_id: str = Field(min_length=1)
    predecessor_authorization_id: str = Field(min_length=1)
    predecessor_risk_budget_version_id: str = Field(min_length=1)
    normal_from_at: AwareDatetime
    normal_through_at: AwareDatetime
    normal_market_session_evidence_ids: tuple[Annotated[str, Field(min_length=1)], ...] = Field(
        min_length=20
    )
    monthly_selection_cutoff_at: AwareDatetime
    available_at: AwareDatetime

    @model_validator(mode="after")
    def validate_normal_window(self) -> RiskBudgetRelaxationEvidence:
        if not (
            self.normal_from_at
            <= self.normal_through_at
            <= self.available_at
            <= self.monthly_selection_cutoff_at
        ):
            raise ValueError("risk relaxation evidence must preserve its normal-state window")
        if len(set(self.normal_market_session_evidence_ids)) != len(
            self.normal_market_session_evidence_ids
        ):
            raise ValueError("risk relaxation market-session evidence must be unique")
        return self


class PortfolioConfirmation(PortfolioContract):
    confirmation_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    risk_budget_version_id: str = Field(min_length=1)
    confirmed_at: AwareDatetime
    confirmed: Literal[True]
    relaxation_evidence: RiskBudgetRelaxationEvidence | None = None


class PortfolioConfirmationCommand(PortfolioContract):
    operation: Literal["PORTFOLIO_CONFIRM"]
    proposal: PortfolioProposal
    previous_authorization_id: str | None = None
    confirmation: PortfolioConfirmation

    @model_validator(mode="after")
    def validate_confirmation_binding(self) -> PortfolioConfirmationCommand:
        if (
            self.confirmation.portfolio_id != self.proposal.portfolio_id
            or self.confirmation.snapshot_id != self.proposal.snapshot.snapshot_id
            or self.confirmation.risk_budget_version_id != self.proposal.risk_budget.version_id
        ):
            raise ValueError("confirmation must bind the proposed portfolio snapshot and budget")
        if self.confirmation.confirmed_at > self.proposal.risk_budget.effective_at:
            raise ValueError("confirmation must not postdate the risk budget effective_at")
        if self.confirmation.confirmed_at < self.proposal.snapshot.cutoff_at:
            raise ValueError("confirmation must not predate the portfolio cutoff")
        return self


class PortfolioUseCommand(PortfolioContract):
    operation: Literal["PORTFOLIO_USE"]
    portfolio_id: str = Field(min_length=1)
    authorization_id: str = Field(min_length=1)
    requested_action: Literal["NEW_EXPOSURE", "DETERMINISTIC_PROTECTION"]


PortfolioCommand: TypeAlias = Annotated[
    PortfolioPreviewCommand | PortfolioConfirmationCommand | PortfolioUseCommand,
    Field(discriminator="operation"),
]


class ExcludedAccount(PortfolioContract):
    account_id: str
    account_type: str
    reason: Literal["NOT_SELECTED", "UNSUPPORTED_ACCOUNT_TYPE", "UNKNOWN_ACCOUNT_TYPE"]


class PortfolioPreview(PortfolioContract):
    portfolio_id: str
    snapshot_id: str
    included_accounts: tuple[CompleteAccountSnapshot, ...] = Field(min_length=1)
    included_account_ids: tuple[str, ...]
    excluded_accounts: tuple[ExcludedAccount, ...]
    blocking_account_ids: tuple[str, ...]
    blocking_accounts: tuple[ExcludedAccount, ...] = ()


class PortfolioAuthorization(PortfolioContract):
    """The append-only risk-budget authorization retained by a formal report."""

    authorization_id: str
    proposal: PortfolioProposal
    confirmation: PortfolioConfirmation
    previous_authorization_id: str | None = None
    recorded_at: AwareDatetime

    def evidence_available_by(self, cutoff: datetime) -> bool:
        relaxation = self.confirmation.relaxation_evidence
        return (
            self.proposal.evidence_available_by(cutoff)
            and self.confirmation.confirmed_at <= cutoff
            and (relaxation is None or relaxation.available_at <= cutoff)
        )


class PortfolioAuthorizationUsage(PortfolioContract):
    requested_action: Literal["NEW_EXPOSURE", "DETERMINISTIC_PROTECTION"]
    allowed: bool
    authorization_snapshot: PortfolioAuthorization
    retained_protection_floor: DeterministicProtectionFloor
    unfinished_cash_obligations: tuple[DatedCashObligation, ...]
    reasons: tuple[str, ...]
    checked_at: AwareDatetime


class PortfolioAuthorizationOutcome(PortfolioContract):
    disposition: Literal["PREVIEWED", "APPROVED", "DENIED"]
    reasons: tuple[str, ...]
    preview: PortfolioPreview | None = None
    authorization: PortfolioAuthorization | None = None
    usage: PortfolioAuthorizationUsage | None = None


def preview_for(proposal: PortfolioProposal) -> PortfolioPreview:
    """Describe the complete candidate scope without treating preview as authorization."""
    selected = set(proposal.snapshot.selected_account_ids)
    excluded = tuple(
        ExcludedAccount(
            account_id=account.account_id,
            account_type=account.account_type,
            reason=_exclusion_reason(account.account_type),
        )
        for account in proposal.snapshot.accounts
        if account.account_id not in selected
    )
    blocking_accounts = tuple(
        ExcludedAccount(
            account_id=account.account_id,
            account_type=account.account_type,
            reason=_exclusion_reason(account.account_type),
        )
        for account in proposal.snapshot.selected_accounts
        if account.account_type != "SIMULATED_CASH"
    )
    return PortfolioPreview(
        portfolio_id=proposal.portfolio_id,
        snapshot_id=proposal.snapshot.snapshot_id,
        included_accounts=proposal.snapshot.selected_accounts,
        included_account_ids=proposal.snapshot.selected_account_ids,
        excluded_accounts=excluded,
        blocking_account_ids=tuple(item.account_id for item in blocking_accounts),
        blocking_accounts=blocking_accounts,
    )


def confirmation_block_reasons(preview: PortfolioPreview) -> tuple[str, ...]:
    """Reject selected non-cash or unknown accounts without hiding them from preview."""
    return tuple(item.reason for item in preview.blocking_accounts)


def portfolio_id_for(command: PortfolioCommand) -> str:
    """Identify the one portfolio whose immutable lineage a command may inspect."""
    if isinstance(command, PortfolioUseCommand):
        return command.portfolio_id
    return command.proposal.portfolio_id


def _exclusion_reason(
    account_type: str,
) -> Literal["NOT_SELECTED", "UNSUPPORTED_ACCOUNT_TYPE", "UNKNOWN_ACCOUNT_TYPE"]:
    if account_type == "SIMULATED_CASH":
        return "NOT_SELECTED"
    if account_type in {
        "SIMULATED_MARGIN",
        "SIMULATED_SECURITIES_BORROWING",
        "SIMULATED_PLEDGED",
        "SIMULATED_COLLATERAL",
        "SIMULATED_LIABILITY",
    }:
        return "UNSUPPORTED_ACCOUNT_TYPE"
    return "UNKNOWN_ACCOUNT_TYPE"


def _add_calendar_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)
