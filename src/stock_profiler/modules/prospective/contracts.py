"""Immutable preregistration and privacy-safe prospective cycle projections."""

from calendar import monthrange
from datetime import datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, model_validator

from stock_profiler.foundation.decision_versions import DecisionCaseVersionBundle
from stock_profiler.modules.evaluation.contracts import EvaluationContract


class PlanNode(EvaluationContract):
    plan_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    knowledge_cutoff: AwareDatetime
    first_entry_at: AwareDatetime
    disclosure_at: AwareDatetime
    matures_at: AwareDatetime

    @model_validator(mode="after")
    def validate_calendar(self) -> "PlanNode":
        local = self.knowledge_cutoff.astimezone(ZoneInfo("Asia/Shanghai"))
        if (
            local.strftime("%Y-%m") != self.plan_month
            or local.strftime("%H:%M:%S") != "23:59:59"
            or local.microsecond != 0
            or local.day != monthrange(local.year, local.month)[1]
        ):
            raise ValueError("plan node requires its original monthly knowledge cutoff")
        if not self.knowledge_cutoff < self.first_entry_at < self.disclosure_at < self.matures_at:
            raise ValueError("plan, entry, disclosure and maturity clocks must be ordered")
        disclosure = self.disclosure_at.astimezone(ZoneInfo("Asia/Shanghai"))
        total = disclosure.year * 12 + disclosure.month - 1 + 6
        minimum = disclosure.replace(
            year=total // 12,
            month=total % 12 + 1,
            day=min(disclosure.day, monthrange(total // 12, total % 12 + 1)[1]),
        )
        if self.matures_at < minimum:
            raise ValueError("maturity cannot shorten the six-calendar-month result window")
        return self


class OperationsPolicy(EvaluationContract):
    window_months: int = Field(ge=1, strict=True)
    completion_floor: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    coverage_floor: Decimal = Field(gt=0, le=1, allow_inf_nan=False)


class EvidenceFloor(EvaluationContract):
    mature_batches: int = Field(ge=1, strict=True)
    nonoverlapping_windows: int = Field(ge=1, strict=True)
    high_band_records: int = Field(ge=1, strict=True)


class CycleRegistration(EvaluationContract):
    version_id: str = Field(min_length=1)
    capability_version: str = Field(min_length=1)
    source_version_bundle: DecisionCaseVersionBundle
    calendar_version: str = Field(min_length=1)
    source_scopes: tuple[str, ...] = Field(min_length=1)
    plan_nodes: tuple[PlanNode, ...] = Field(min_length=1)
    operations_policy: OperationsPolicy
    source_months_required: int = Field(ge=1, strict=True)
    formal_floor: EvidenceFloor

    @model_validator(mode="after")
    def validate_timeline(self) -> "CycleRegistration":
        months = tuple(node.plan_month for node in self.plan_nodes)
        if len(set(self.source_scopes)) != len(self.source_scopes):
            raise ValueError("source scopes must be unique")
        if len(set(months)) != len(months) or months != tuple(sorted(months)):
            raise ValueError("plan months must be unique and ordered")
        for earlier, later in zip(months, months[1:], strict=False):
            first = datetime.fromisoformat(earlier + "-01")
            second = datetime.fromisoformat(later + "-01")
            if second.year * 12 + second.month != first.year * 12 + first.month + 1:
                raise ValueError("plan timeline must include every consecutive calendar month")
        return self


FailureReason = Literal["NONE", "DATA", "SYSTEM", "GOVERNANCE", "MISSING", "UNKNOWN"]


class SourceSnapshot(EvaluationContract):
    scope: str = Field(min_length=1)
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    knowledge_cutoff: AwareDatetime
    published_at: AwareDatetime
    acquired_at: AwareDatetime
    validated_at: AwareDatetime
    watermark_complete: bool
    critical_error: bool

    @model_validator(mode="after")
    def validate_clocks(self) -> "SourceSnapshot":
        if not self.published_at <= self.acquired_at <= self.validated_at:
            raise ValueError("source clocks must retain publication, acquisition and validation")
        return self

    @property
    def on_time(self) -> bool:
        return (
            self.validated_at <= self.knowledge_cutoff
            and self.watermark_complete
            and not self.critical_error
        )


class NotificationObligation(EvaluationContract):
    kind: Literal["INITIAL", "FINAL", "CORRECTION"]
    due_at: AwareDatetime
    delivered_at: AwareDatetime | None


class ShadowIncident(EvaluationContract):
    incident_id: str = Field(min_length=1)
    kind: Literal["DATA", "OPERATIONS", "PRIVACY", "SHADOW"]
    occurred_at: AwareDatetime
    closed_at: AwareDatetime | None

    @model_validator(mode="after")
    def validate_closure(self) -> "ShadowIncident":
        if self.closed_at is not None and self.closed_at < self.occurred_at:
            raise ValueError("incident cannot close before it occurs")
        return self


class IncidentFact(ShadowIncident):
    operation: Literal["RECORD", "CLOSE"]
    plan_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")

    @model_validator(mode="after")
    def validate_operation(self) -> "IncidentFact":
        if (self.operation == "CLOSE") != (self.closed_at is not None):
            raise ValueError("record the original incident before appending a closure")
        return self


class MonthlyObservation(EvaluationContract):
    plan_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    completed_at: AwareDatetime
    status: Literal["CANDIDATES", "ABSTAINED", "FAILED", "BLOCKED", "UNKNOWN"]
    reason: FailureReason
    sources: tuple[SourceSnapshot, ...]
    batch_saved_at: AwareDatetime | None
    report_saved_at: AwareDatetime | None
    first_delivery_at: AwareDatetime | None
    notifications: tuple[NotificationObligation, ...] = Field(max_length=3)
    incidents: tuple[ShadowIncident, ...]

    @model_validator(mode="after")
    def validate_outcome(self) -> "MonthlyObservation":
        valid = self.status in {"CANDIDATES", "ABSTAINED"}
        if (self.reason == "NONE") != valid:
            raise ValueError("failed and blocked months require an original failure reason")
        if self.status != "CANDIDATES" and (self.notifications or self.first_delivery_at):
            raise ValueError("empty and failed months have no candidate delivery obligations")
        kinds = tuple(item.kind for item in self.notifications)
        if len(set(kinds)) != len(kinds):
            raise ValueError("notification obligations must retain their original natural kind")
        if self.status == "CANDIDATES" and not {"INITIAL", "FINAL"}.issubset(kinds):
            raise ValueError("candidate delivery retains both initial and final obligations")
        if len({item.incident_id for item in self.incidents}) != len(self.incidents):
            raise ValueError("incident identities cannot be duplicated")
        if len({item.scope for item in self.sources}) != len(self.sources):
            raise ValueError("source scopes cannot be duplicated")
        clocks = [self.batch_saved_at, self.report_saved_at, self.first_delivery_at]
        clocks += [item.validated_at for item in self.sources]
        clocks += [item.delivered_at for item in self.notifications]
        clocks += [item.occurred_at for item in self.incidents]
        clocks += [item.closed_at for item in self.incidents]
        if any(clock is not None and clock > self.completed_at for clock in clocks):
            raise ValueError("observation cannot contain future evidence")
        return self


class MonthFailure(EvaluationContract):
    plan_month: str
    reason: FailureReason


class SourceWatermark(EvaluationContract):
    scope: str
    consecutive_months: int
    required_months: int
    watermark_reached: bool
    qualification_granted: Literal[False] = False


class CycleCommand(EvaluationContract):
    operation: Literal["REGISTER", "OBSERVE", "INCIDENT", "REVIEW"]
    synthetic: Literal[True]
    generator_version: str = Field(min_length=1)
    seed: int
    cutoff_at: AwareDatetime
    registration: CycleRegistration | None
    registration_event_id: str | None
    previous_event_id: str | None
    observation: MonthlyObservation | None
    incident_fact: IncidentFact | None = Field(default=None, exclude_if=lambda value: value is None)
    maturity: None

    @model_validator(mode="after")
    def validate_operation(self) -> "CycleCommand":
        if self.operation == "REGISTER":
            if self.registration is None or self.registration_event_id or self.previous_event_id:
                raise ValueError("registration precedes observation and review")
            if self.cutoff_at >= self.registration.plan_nodes[0].knowledge_cutoff:
                raise ValueError("plan must be registered before its first knowledge cutoff")
        elif self.registration is not None or not self.registration_event_id:
            raise ValueError("cycle operation requires its saved preregistration")
        if (self.operation == "OBSERVE") != (self.observation is not None):
            raise ValueError("only an observation operation can append original monthly evidence")
        if (self.operation == "INCIDENT") != (self.incident_fact is not None):
            raise ValueError("only an incident operation can append an incident fact")
        if self.incident_fact is not None and any(
            clock is not None and clock > self.cutoff_at
            for clock in (self.incident_fact.occurred_at, self.incident_fact.closed_at)
        ):
            raise ValueError("incident facts must be known at the cutoff")
        if self.observation is not None and self.observation.completed_at > self.cutoff_at:
            raise ValueError("monthly evidence must already exist at the review cutoff")
        return self


class CompletionRate(EvaluationContract):
    numerator: int
    denominator: int
    rate: Decimal | None
    disposition: Literal["NOT_APPLICABLE", "OBSERVED", "WITHHELD"]


class CycleOperations(EvaluationContract):
    planned_months: int
    window_months: tuple[str, ...]
    full_window: bool
    data: CompletionRate
    pipeline: CompletionRate
    batches: CompletionRate
    notifications: CompletionRate
    reports: CompletionRate
    timeliness: CompletionRate
    coverage: CompletionRate
    failures: tuple[MonthFailure, ...]
    incident_count: int
    unclosed_incidents: int
    safety_clear: bool
    consecutive_obligation_failure: bool
    gates_passed: bool
    consecutive_failure: bool
    qualified: Literal[False] = False


class CycleWatermark(EvaluationContract):
    required: EvidenceFloor
    mature_batches: int
    nonoverlapping_windows: int
    high_band_records: int
    pending_batches: int
    due_missing_batches: int
    observation_reached: bool
    formal_sufficient: bool
    waiting_for: tuple[str, ...]


class CycleFormalLook(EvaluationContract):
    disposition: Literal["WAITING_FOR_MATURITY"]
    actual_ordinal: Literal[0] = 0
    alpha_spent: Decimal = Field(default=Decimal(0), ge=0, le=0)


class CycleReport(EvaluationContract):
    disposition: str
    registration_event_id: str | None
    version_id: str
    cutoff_at: AwareDatetime
    previous_event_id: str | None = None
    report_version: int = 1
    operations: CycleOperations | None = None
    source_watermarks: tuple[SourceWatermark, ...] = ()
    watermark: CycleWatermark | None = None
    formal_look: CycleFormalLook | None = None
    actionable: Literal[False] = False
    authorization_granted: Literal[False] = False
