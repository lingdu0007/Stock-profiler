"""Read-only execution evidence and separate user declarations."""

from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field

from stock_profiler.modules.portfolio.allocation_contracts import AllocationCommitment
from stock_profiler.modules.position_management.contracts import PositionContract, PositionEvidence

ExecutionClassification = Literal["PLANNED", "DEVIATION", "EXTERNAL", "UNKNOWN"]


class BrokerExecutionOrder(PositionContract):
    order_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    security_id: str = Field(min_length=1)
    issuer_id: str = Field(min_length=1)
    side: Literal["BUY", "SELL"]
    status: Literal["OPEN", "UNKNOWN", "FILLED", "CANCELLED", "REJECTED", "EXPIRED"]
    quantity: Decimal = Field(gt=0)
    filled_quantity: Decimal = Field(ge=0)
    remaining_quantity: Decimal | None = Field(ge=0)
    limit_price: Decimal | None = Field(gt=0)
    reserved_cash: Decimal | None = Field(ge=0)
    submitted_at: AwareDatetime
    causal_reservation_id: str | None
    step_confirmation_id: str | None
    external_origin_id: str | None = Field(
        default=None, min_length=1, exclude_if=lambda v: v is None
    )
    evidence: PositionEvidence


class BrokerExecutionFill(PositionContract):
    entry_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    price: Decimal = Field(gt=0)
    fee_entry_ids: tuple[str, ...]


class AttributedOrder(PositionContract):
    account_id: str
    order_id: str
    reservation_id: str | None
    classification: ExecutionClassification
    reasons: tuple[str, ...]


class AttributedFill(PositionContract):
    entry_id: str
    order_id: str
    account_id: str
    security_id: str
    reservation_id: str | None
    classification: ExecutionClassification
    reasons: tuple[str, ...]
    quantity: Decimal
    intent_quantity: Decimal = Decimal(0)
    external_quantity: Decimal = Decimal(0)
    cash_used: Decimal
    fees: Decimal
    corrects_entry_id: str | None = None


class ExecutionRow(PositionContract):
    reservation_id: str
    security_id: str
    accepted_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    state: Literal["UNEXECUTED", "PARTIALLY_FILLED", "FILLED", "TERMINAL_UNFILLED", "UNKNOWN"]


class ExecutionDeclaration(PositionContract):
    security_id: str = Field(min_length=1)
    status: Literal["PREPARING", "SUBMITTED", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "UNABLE"]
    broker_order_id: str | None
    declared_at: AwareDatetime


class CandidateExecutionCommand(PositionContract):
    contract_version: Literal["1.0.0"]
    operation: Literal["DECLARE", "RECONCILE"]
    portfolio_id: str = Field(min_length=1)
    plan_event_id: str = Field(min_length=1)
    confirmation_event_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    cutoff_at: AwareDatetime
    seen_execution_id: str | None
    declaration: ExecutionDeclaration | None = None
    position_event_id: str | None = None
    broker_evidence: PositionEvidence | None = None
    funds_evidence: PositionEvidence | None = None
    orders: tuple[BrokerExecutionOrder, ...] = ()
    fills: tuple[BrokerExecutionFill, ...] = ()
    withdrawn_reservation_ids: tuple[str, ...] = ()


class CandidateExecutionOutcome(PositionContract):
    disposition: Literal["PENDING_RECONCILIATION", "RECONCILED", "BLOCKED"]
    reasons: tuple[str, ...]
    execution_id: str | None = None
    portfolio_id: str | None = None
    plan_id: str | None = None
    confirmation_id: str | None = None
    declarations: tuple[ExecutionDeclaration, ...] = ()
    reservations: tuple[AllocationCommitment, ...] = ()
    unassociated_commitments: tuple[AllocationCommitment, ...] = ()
    position_event_id: str | None = None
    orders: tuple[BrokerExecutionOrder, ...] = ()
    order_attributions: tuple[AttributedOrder, ...] = ()
    fills: tuple[AttributedFill, ...] = ()
    rows: tuple[ExecutionRow, ...] = ()
    released_reservation_ids: tuple[str, ...] = ()
    withdrawn_reservation_ids: tuple[str, ...] = ()
    unresolved_order_keys: tuple[tuple[str, str], ...] = ()
    terminal_outcome: (
        Literal["NO_TRADE", "ALL_DECLINED", "DEFERRED_EXPIRED", "PARTIALLY_FILLED", "FULLY_FILLED"]
        | None
    ) = None
    actionable: Literal[False] = False
