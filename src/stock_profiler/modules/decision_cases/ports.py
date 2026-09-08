"""Host-owned persistence and framework contracts for the frozen journey."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypeVar

from stock_profiler.modules.decision_cases.domain import (
    DecisionEventFact,
    ExternalResult,
    FormalReport,
    FrameworkRunStatus,
    FrozenDecisionCase,
    NotificationAttempt,
    NotificationAttemptStatus,
    ResultAccessScope,
    StageResult,
)
from stock_profiler.modules.portfolio.contracts import PortfolioAuthorizationOutcome
from stock_profiler.modules.portfolio.liquidity import LiquidityOutcome
from stock_profiler.modules.portfolio.stress import PortfolioStressOutcome
from stock_profiler.modules.position_management.contracts import (
    AccountCashState,
    AuthoritativeLedgerEntry,
)
from stock_profiler.modules.qualification.contracts import GovernanceOutcome

Transaction = TypeVar("Transaction")


class DecisionEventCommitError(RuntimeError):
    """A host event was not reliably committed, so no report may be published."""


class DecisionEventCommitUncertainError(DecisionEventCommitError):
    """A database acknowledgement was lost, so the event may or may not exist."""


class FormalReportCommitUncertainError(DecisionEventCommitError):
    """A report write may have committed, but its acknowledgement was lost."""


class MappedDurableRunMissingError(ValueError):
    """A host mapping names an original Run that is no longer durable."""


@dataclass(frozen=True)
class BusinessObjectMapping:
    case_id: str
    frozen_input_fingerprint: str
    framework_run_id: str
    case: FrozenDecisionCase | None


@dataclass(frozen=True)
class FrameworkRunTransition:
    status: FrameworkRunStatus
    reason: str


@dataclass(frozen=True)
class FrameworkRunResult:
    run_id: str
    status: FrameworkRunStatus
    output: str | None
    waiting_reason: str | None = None
    error_code: str | None = None
    transitions: tuple[FrameworkRunTransition, ...] = ()
    transitions_durably_recorded: bool = False


FrameworkTransitionRecorder = Callable[[FrameworkRunTransition], Awaitable[None]]


class FrozenFramework(Protocol):
    async def validate_recovery(self, case: FrozenDecisionCase) -> None: ...

    async def recover_unmapped(
        self, case: FrozenDecisionCase, known_run_ids: frozenset[str]
    ) -> FrozenDecisionCase | None: ...

    async def execute(
        self, case: FrozenDecisionCase, record_transition: FrameworkTransitionRecorder
    ) -> FrameworkRunResult: ...


class DecisionLedger(Protocol[Transaction]):
    """Only the adapter interprets the opaque transaction handle."""

    def observed_at(self) -> str: ...

    def governance_history(
        self, connection: Transaction, access_scope: ResultAccessScope
    ) -> tuple[GovernanceOutcome, ...]: ...

    def portfolio_authorization_history(
        self,
        connection: Transaction,
        access_scope: ResultAccessScope,
        portfolio_id: str,
        knowledge_cutoff: str,
    ) -> tuple[PortfolioAuthorizationOutcome, ...]: ...

    def portfolio_authorization_lineage(
        self,
        connection: Transaction,
        access_scope: ResultAccessScope,
        portfolio_id: str,
    ) -> tuple[PortfolioAuthorizationOutcome, ...]: ...

    def portfolio_authorization_owner_lineage(
        self,
        connection: Transaction,
        access_scope: ResultAccessScope,
    ) -> tuple[PortfolioAuthorizationOutcome, ...]: ...

    def position_ledger_history(
        self,
        connection: Transaction,
        access_scope: ResultAccessScope,
        cutoff_at: datetime,
    ) -> tuple[AuthoritativeLedgerEntry, ...]: ...

    def portfolio_stress_history(
        self,
        connection: Transaction,
        access_scope: ResultAccessScope,
        portfolio_id: str,
    ) -> tuple[PortfolioStressOutcome, ...]: ...

    def position_cash_history(
        self,
        connection: Transaction,
        access_scope: ResultAccessScope,
        cutoff_at: datetime,
    ) -> tuple[AccountCashState, ...]: ...

    def liquidity_history(
        self,
        connection: Transaction,
        access_scope: ResultAccessScope,
        portfolio_id: str,
        cutoff_at: datetime,
    ) -> tuple[LiquidityOutcome, ...]: ...

    def mapped_framework_run_ids(self, connection: Transaction) -> frozenset[str]: ...

    def get_formal_report(self, report_version_id: str) -> FormalReport | None: ...

    def serialize_case_execution(self) -> AbstractContextManager[Transaction]: ...

    def get_business_object_mapping(
        self, business_object_id: str, connection: Transaction
    ) -> BusinessObjectMapping | None: ...

    def ensure_business_object(self, connection: Transaction, case: FrozenDecisionCase) -> None: ...

    def resolve_business_object_id(
        self, case: FrozenDecisionCase, connection: Transaction
    ) -> str: ...

    def persist_business_mapping_before_framework(self, case: FrozenDecisionCase) -> str: ...

    def get_original_decision_event(
        self, business_object_id: str, connection: Transaction
    ) -> DecisionEventFact | None: ...

    def get_correction_event(
        self, original_event_id: str, connection: Transaction
    ) -> DecisionEventFact | None: ...

    def get_stage_results(
        self, business_object_id: str, connection: Transaction | None = None
    ) -> tuple[StageResult, ...]: ...

    def record_stage_result(
        self,
        connection: Transaction,
        *,
        case: FrozenDecisionCase,
        stage_result: StageResult,
        decision_event_id: str | None = None,
        stage_event_id: str | None = None,
        framework_run_id: str | None = None,
        allow_repeated_occurrence: bool = False,
        recorded_at: str | None = None,
    ) -> None: ...

    def record_notification_attempt(
        self,
        connection: Transaction,
        *,
        report: FormalReport,
        status: NotificationAttemptStatus,
        reasons: tuple[str, ...],
    ) -> NotificationAttempt: ...

    def build_event_fact(
        self,
        *,
        case: FrozenDecisionCase,
        framework_run_id: str,
        result: ExternalResult,
        stage_results: tuple[StageResult, ...],
        decision_event_id: str | None = None,
        corrects_event_id: str | None = None,
        committed_at: str | None = None,
        generated_at: str | None = None,
    ) -> DecisionEventFact: ...

    def commit_event_fact(
        self, connection: Transaction, fact: DecisionEventFact
    ) -> DecisionEventFact: ...

    def reconcile_event_commit(
        self, connection: Transaction, attempted: DecisionEventFact
    ) -> DecisionEventFact | None: ...

    def reconcile_report_commit(
        self, connection: Transaction, attempted: FormalReport
    ) -> FormalReport | None: ...

    def ensure_event_stage_results(
        self, connection: Transaction, fact: DecisionEventFact
    ) -> None: ...

    def discard_unconfirmed_publication(self, connection: Transaction) -> None: ...

    def publish_report(
        self,
        connection: Transaction,
        fact: DecisionEventFact,
        report_version_id: str | None = None,
    ) -> FormalReport: ...

    def get_original_formal_report(
        self, business_object_id: str, connection: Transaction
    ) -> FormalReport | None: ...

    def get_formal_report_for_event(
        self, decision_event_id: str, connection: Transaction
    ) -> FormalReport | None: ...
