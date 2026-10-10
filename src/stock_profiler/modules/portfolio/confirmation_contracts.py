"""Complete synthetic plan choices and reservation facts; no order authority."""

from typing import Literal

from pydantic import Field

from stock_profiler.modules.portfolio.allocation_contracts import (
    AllocationCommitment,
    CandidateAllocationCommand,
)
from stock_profiler.modules.position_management.contracts import PositionContract


class CandidateChoice(PositionContract):
    security_id: str = Field(min_length=1)
    choice: Literal["ACCEPT", "DECLINE", "DEFER"]


class SeenCandidateExecution(PositionContract):
    execution_id: str | None


class CandidateConfirmationCommand(PositionContract):
    contract_version: Literal["1.0.0"]
    operation: Literal["SUBMIT", "WITHDRAW", "REVIEW"]
    user_id: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    candidate_batch_id: str = Field(min_length=1)
    plan_event_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    seen_confirmation_id: str | None
    seen_execution: SeenCandidateExecution | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    idempotency_key: str = Field(min_length=1)
    withdrawal_position_event_id: str | None
    choices: tuple[CandidateChoice, ...]
    revalidation: CandidateAllocationCommand


class CandidateConfirmationOutcome(PositionContract):
    disposition: Literal["CONFIRMED", "REVALIDATED", "BLOCKED"]
    reasons: tuple[str, ...]
    confirmation_id: str | None = None
    plan_id: str | None = None
    portfolio_id: str | None = None
    choices: tuple[CandidateChoice, ...] = ()
    reservations: tuple[AllocationCommitment, ...] = ()
    supersedes_confirmation_id: str | None = None
    released_reservation_ids: tuple[str, ...] = ()
    actionable: Literal[False] = False
