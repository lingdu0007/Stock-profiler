"""Versioned monitoring projections over committed host decisions."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from stock_profiler.modules.delivery.user_facts import UserFact
from stock_profiler.modules.position_management.contracts import (
    PositionActionUnit,
    PositionEvidence,
    PositionReconciliationOutcome,
)
from stock_profiler.modules.position_management.execution_contracts import (
    ExecutionPlanOutcome,
    ExecutionTarget,
)

MonitoringKind = Literal[
    "DAILY_CLOSE", "EVENT_REASSESS", "NOTIFICATION_RUN", "LIFECYCLE", "OPERATIONS"
]
ChannelResult = Literal["ACCEPTED", "REJECTED", "UNKNOWN", "TIMEOUT"]
EvidenceFamily = Literal[
    "MARKET_SECURITY", "COMPANY_EVENTS", "BROKER_ACCOUNT", "COST_RULES", "QUALIFICATION_VERSION"
]


class MonitoringContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MonitoringPublication(MonitoringContract):
    committed_at: str
    published_at: str


class MonitoringEvidenceInput(MonitoringContract):
    family: EvidenceFamily
    evidence: PositionEvidence


class MonitoringEvidenceStatus(MonitoringContract):
    family: EvidenceFamily
    status: Literal["VALIDATED", "UNKNOWN", "BLOCKED"]
    evidence: PositionEvidence | None
    reasons: tuple[str, ...] = ()


class MonitoringCommand(MonitoringContract):
    contract_version: Literal["1.0.0"]
    operation: Literal["MONITOR_ASSESS"]
    portfolio_id: str = Field(min_length=1)
    cutoff_at: AwareDatetime
    kind: MonitoringKind
    source_event_id: str = Field(min_length=1)
    calendar: MonitoringCalendar | None = None
    events: tuple[MonitoringEvent, ...] = ()
    notification: SyntheticNotificationInput | None = None
    reconciliation_event_id: str | None = None
    evidence_families: tuple[MonitoringEvidenceInput, ...] = ()
    planned_trading_dates: tuple[date, ...] = ()
    monthly_freeze: bool = False


class SyntheticNotificationInput(MonitoringContract):
    identity: str = Field(min_length=1)
    routing_version: str = Field(min_length=1)
    attempt_number: int = Field(default=1, ge=1)
    previous_event_id: str | None = None
    quiet_until: AwareDatetime | None
    immediate_result: ChannelResult
    persistent_result: ChannelResult


class MonitoringNotification(MonitoringContract):
    notification_id: str
    case_id: str
    source_event_id: str
    role: Literal["IMMEDIATE", "PERSISTENT"]
    result: ChannelResult
    attempted_at: str
    body: str
    delivered: None = None
    synthetic: Literal[True] = True


class MonitoringEvent(MonitoringContract):
    event_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    authority: str = Field(min_length=1)
    security_ids: tuple[str, ...] = ()
    evidence: PositionEvidence


class MonitoringCalendar(MonitoringContract):
    version_id: str = Field(min_length=1)
    market_date: date
    is_trading_day: bool
    close_at: AwareDatetime
    next_window_start: AwareDatetime | None
    next_window_end: AwareDatetime | None
    evidence: PositionEvidence


class MonitoringCase(MonitoringContract):
    case_id: str
    source_event_id: str
    source_event_ids: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)
    obligation_ids: tuple[str, ...]
    priority: Literal["P0", "P1"]
    plan: ExecutionPlanOutcome | None
    required_targets: tuple[ExecutionTarget, ...] = ()
    quantity_status: Literal["VERIFIED", "UNKNOWN"] = "VERIFIED"
    first_established_at: str
    last_reviewed_at: str


class MonitoringFreshness(MonitoringContract):
    evidence_cutoff: AwareDatetime
    assessed_at: str
    market_status: Literal["CLOSED", "WAITING_WINDOW", "OPEN"]
    next_window_start: AwareDatetime | None
    next_window_end: AwareDatetime | None
    account_evidence: tuple[PositionEvidence, ...]
    event_evidence: tuple[PositionEvidence, ...]
    calendar_evidence: PositionEvidence
    evidence_families: tuple[MonitoringEvidenceStatus, ...] = ()


class MonitoringOutcome(MonitoringContract):
    portfolio_id: str
    kind: MonitoringKind
    disposition: Literal["ASSESSED", "BLOCKED"]
    reasons: tuple[str, ...]
    cases: tuple[MonitoringCase, ...] = ()
    action_units: tuple[PositionActionUnit, ...] = ()
    freshness: MonitoringFreshness | None = None
    source_report_ids: tuple[str, ...] = ()
    notifications: tuple[MonitoringNotification, ...] = ()
    notification_due_at: AwareDatetime | None = None
    operations_dates: tuple[date, ...] = ()
    missing_daily_dates: tuple[date, ...] = ()
    user_facts: tuple[UserFact, ...] = ()
    interaction_cutoff_at: str | None = None
    reconciliation: PositionReconciliationOutcome | None = None
