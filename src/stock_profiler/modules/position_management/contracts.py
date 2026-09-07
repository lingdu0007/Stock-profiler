"""Immutable authoritative position-state contracts for the frozen D0 seam."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class PositionContract(BaseModel):
    """Reject undeclared fields from every versioned position-state fact."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class PositionEvidenceClock(PositionContract):
    """The source clock retained with a displayed position-state snapshot."""

    business_effective_at: AwareDatetime | None
    source_observed_at: AwareDatetime | None
    locally_acquired_at: AwareDatetime | None
    validated_at: AwareDatetime | None


class PositionEvidence(PositionContract):
    """Source provenance remains explicit even when an incomplete fact is retained."""

    source: str | None
    business_effective_at: AwareDatetime | None
    source_observed_at: AwareDatetime | None
    locally_acquired_at: AwareDatetime | None
    validated_at: AwareDatetime | None
    cutoff_at: AwareDatetime | None
    expires_at: AwareDatetime | None = None

    @property
    def clock(self) -> PositionEvidenceClock:
        return PositionEvidenceClock(
            business_effective_at=self.business_effective_at,
            source_observed_at=self.source_observed_at,
            locally_acquired_at=self.locally_acquired_at,
            validated_at=self.validated_at,
        )

    def problem_codes(self, expected_cutoff: datetime) -> tuple[str, ...]:
        """Report retained evidence defects rather than guessing a broker fact."""
        codes: list[str] = []
        if not self.source:
            codes.append("SOURCE_MISSING")
        if self.cutoff_at is None:
            codes.append("FACT_CUTOFF_MISSING")
        elif self.cutoff_at != expected_cutoff:
            codes.append("FACT_CUTOFF_MISMATCH")
        clock = (
            self.business_effective_at,
            self.source_observed_at,
            self.locally_acquired_at,
            self.validated_at,
        )
        if any(value is None for value in clock):
            codes.append("EVIDENCE_CLOCK_MISSING")
        else:
            effective, observed, acquired, validated = clock
            assert (
                effective is not None
                and observed is not None
                and acquired is not None
                and validated is not None
            )
            if not effective <= observed <= acquired <= validated <= expected_cutoff:
                codes.append("EVIDENCE_CLOCK_INVALID")
        if self.expires_at is not None and self.expires_at < expected_cutoff:
            codes.append("FACT_EXPIRED")
        return tuple(codes)


class CashState(PositionContract):
    """Broker cash facts remain decomposed instead of being collapsed into a balance."""

    ledger_cash: Decimal | None
    trading_cash: Decimal | None
    transferable_cash: Decimal | None
    frozen_cash: Decimal | None
    receivable_cash: Decimal | None
    payable_cash: Decimal | None
    evidence: PositionEvidence


class BrokerPositionFact(PositionContract):
    """One broker summary for one account and security at the frozen cutoff."""

    position_id: str = Field(min_length=1)
    issuer_id: str = Field(min_length=1)
    security_id: str = Field(min_length=1)
    total_quantity: Decimal | None
    broker_sellable_quantity: Decimal | None
    sellable_quantity_semantics: Literal["BROKER_FINAL_SELLABLE", "UNKNOWN"]
    unsettled_quantity: Decimal | None
    frozen_quantity: Decimal | None
    restricted_quantity: Decimal | None
    open_sell_order_quantity: Decimal | None
    reported_cost_basis: Decimal | None
    market_price: Decimal | None
    evidence: PositionEvidence


class OpenOrder(PositionContract):
    """An unfinished broker order that is retained beside its position summary."""

    order_id: str = Field(min_length=1)
    security_id: str = Field(min_length=1)
    side: Literal["BUY", "SELL"]
    remaining_quantity: Decimal | None
    evidence: PositionEvidence


class PositionLedgerEntry(PositionContract):
    """An append-only broker record that can reconstruct position quantity and cost."""

    entry_id: str = Field(min_length=1)
    entry_type: Literal[
        "FILL",
        "FEE",
        "TRANSFER_IN",
        "TRANSFER_OUT",
        "CORPORATE_ACTION",
    ]
    security_id: str | None
    quantity_delta: Decimal
    cost_basis_delta: Decimal
    cash_delta: Decimal
    occurred_at: AwareDatetime
    evidence: PositionEvidence


class ExecutionRestriction(PositionContract):
    """A broker execution constraint that can block an otherwise known quantity."""

    restriction_id: str = Field(min_length=1)
    security_id: str | None = None
    kind: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    active: bool
    evidence: PositionEvidence


class UserPositionAnnotation(PositionContract):
    """A user-supplied note retained separately from broker-authoritative records."""

    annotation_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    security_id: str = Field(min_length=1)
    note: str = Field(min_length=1)
    claimed_total_quantity: Decimal | None = None
    claimed_cost_basis: Decimal | None = None
    created_at: AwareDatetime


class AccountPositionSnapshot(PositionContract):
    """All authoritative account facts needed for one same-cutoff position view."""

    account_id: str = Field(min_length=1)
    account_type: str = Field(min_length=1)
    currency: str = Field(min_length=1)
    snapshot_evidence: PositionEvidence
    account_equity: Decimal | None
    account_equity_evidence: PositionEvidence
    cash_state: CashState
    positions: tuple[BrokerPositionFact, ...]
    open_orders: tuple[OpenOrder, ...]
    ledger_entries: tuple[PositionLedgerEntry, ...]
    execution_restrictions: tuple[ExecutionRestriction, ...]

    @model_validator(mode="after")
    def validate_internal_position_identities(self) -> AccountPositionSnapshot:
        position_ids = tuple(position.position_id for position in self.positions)
        if len(set(position_ids)) != len(position_ids):
            raise ValueError("account position identifiers must be unique")
        security_ids = tuple(position.security_id for position in self.positions)
        if len(set(security_ids)) != len(security_ids):
            raise ValueError("account position securities must be unique")
        return self


class PositionSnapshotCommand(PositionContract):
    """The complete synthetic broker-fact snapshot reconciled by the host."""

    operation: Literal["POSITION_SNAPSHOT_RECONCILE"]
    contract_version: Literal["1.0.0"]
    synthetic: Literal[True]
    generator_version: str = Field(min_length=1)
    seed: int
    snapshot_id: str = Field(min_length=1)
    cutoff_at: AwareDatetime
    snapshot_evidence: PositionEvidence
    valuation_currency: str = Field(min_length=1)
    accounts: tuple[AccountPositionSnapshot, ...] = Field(min_length=1)
    annotations: tuple[UserPositionAnnotation, ...] = ()

    @model_validator(mode="after")
    def validate_common_snapshot_scope(self) -> PositionSnapshotCommand:
        account_ids = tuple(account.account_id for account in self.accounts)
        if len(set(account_ids)) != len(account_ids):
            raise ValueError("position snapshot account identities must be unique")
        if any(account.currency != self.valuation_currency for account in self.accounts):
            raise ValueError("D0 position snapshot accounts require one valuation currency")
        if any(annotation.account_id not in account_ids for annotation in self.annotations):
            raise ValueError("position annotation must belong to a captured account")
        if len({annotation.annotation_id for annotation in self.annotations}) != len(
            self.annotations
        ):
            raise ValueError("position annotation identities must be unique")
        return self

    @property
    def account_ids(self) -> tuple[str, ...]:
        return tuple(account.account_id for account in self.accounts)


class PositionAffectedScope(PositionContract):
    """The account/security scope for a conflict and its statistical consequence."""

    account_id: str | None = None
    security_id: str | None = None
    fields: tuple[str, ...] = Field(min_length=1)


class PositionFactConflict(PositionContract):
    """An unreconciled source fact; it never selects one conflicting value."""

    conflict_id: str = Field(min_length=1)
    code: str = Field(min_length=1)
    affected_scope: PositionAffectedScope
    blocks_exact_statistical_quantity: bool


class PositionActionUnit(PositionContract):
    """One account-local action quantity, never an issuer-level order instruction."""

    account_id: str
    account_type: str
    currency: str
    issuer_id: str
    security_id: str
    total_quantity: Decimal | None
    broker_sellable_quantity: Decimal | None
    unsettled_quantity: Decimal | None
    frozen_quantity: Decimal | None
    restricted_quantity: Decimal | None
    open_sell_order_quantity: Decimal | None
    exact_statistical_action_quantity: Decimal | None
    exact_quantity_status: Literal["AVAILABLE", "BLOCKED"]
    reasons: tuple[str, ...]


class IssuerExposure(PositionContract):
    """Current issuer exposure aggregates market value only after account facts stay separate."""

    issuer_id: str
    valuation_currency: str
    account_ids: tuple[str, ...] = Field(min_length=1)
    current_market_exposure: Decimal | None


class AccountCashState(PositionContract):
    """Cash decomposition retained with the account that supplied it."""

    account_id: str
    account_type: str
    currency: str
    ledger_cash: Decimal | None
    trading_cash: Decimal | None
    transferable_cash: Decimal | None
    frozen_cash: Decimal | None
    receivable_cash: Decimal | None
    payable_cash: Decimal | None


class AuthoritativeLedgerEntry(PositionLedgerEntry):
    """An append-only ledger entry projected with its source account identity."""

    account_id: str


class ReconciledPositionSnapshot(PositionContract):
    """A read-only, same-cutoff result derived from broker facts and their ledgers."""

    snapshot_id: str
    cutoff_at: AwareDatetime
    valuation_currency: str
    snapshot_source: str | None
    evidence_clock: PositionEvidenceClock
    total_account_equity: Decimal | None
    action_units: tuple[PositionActionUnit, ...]
    issuer_exposures: tuple[IssuerExposure, ...]
    cash_states: tuple[AccountCashState, ...]
    authoritative_ledger: tuple[AuthoritativeLedgerEntry, ...]
    user_annotations: tuple[UserPositionAnnotation, ...]


class PositionReconciliationOutcome(PositionContract):
    """The host-owned result appended after a valid frozen framework output."""

    disposition: Literal["RECONCILED", "CONFLICTED"]
    reasons: tuple[str, ...]
    snapshot: ReconciledPositionSnapshot
    conflicts: tuple[PositionFactConflict, ...]
