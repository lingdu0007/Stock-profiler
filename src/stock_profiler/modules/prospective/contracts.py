"""Immutable preregistration and privacy-safe prospective cycle projections."""

from calendar import monthrange
from datetime import datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, model_validator

from stock_profiler.foundation.decision_versions import DecisionCaseVersionBundle
from stock_profiler.modules.candidate_selection.selection import SelectionPolicy
from stock_profiler.modules.evaluation.cohort_metrics import historical_sessions
from stock_profiler.modules.evaluation.contracts import (
    EvaluationContract,
    EvaluationMember,
    EvaluationRegistration,
)
from stock_profiler.modules.evaluation.historical_contracts import (
    CohortEvaluationPolicy,
    HistoricalMonthInput,
    HistoricalMonthResult,
)
from stock_profiler.modules.portfolio.market_calendar import (
    next_market_session_open_after,
    six_month_terminal_evaluation_at,
    synthetic_market_calendar,
)


class PlanNode(EvaluationContract):
    plan_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    knowledge_cutoff: AwareDatetime
    first_entry_at: AwareDatetime
    disclosure_at: AwareDatetime
    matures_at: AwareDatetime
    calendar_version: str | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_calendar(self) -> "PlanNode":
        local = self.knowledge_cutoff.astimezone(ZoneInfo("Asia/Shanghai"))
        if (
            local.strftime("%Y-%m") != self.plan_month
            or local.strftime("%H:%M:%S") != "23:59:59"
            or local.microsecond != 0
            or (
                self.calendar_version is None
                and local.day != monthrange(local.year, local.month)[1]
            )
        ):
            raise ValueError("plan node requires its original monthly knowledge cutoff")
        if not self.knowledge_cutoff < self.first_entry_at < self.disclosure_at < self.matures_at:
            raise ValueError("plan, entry, disclosure and maturity clocks must be ordered")
        if self.calendar_version is not None:
            validate_market_plan(self, self.calendar_version)
            return self
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


def validate_market_plan(plan: PlanNode, version: str) -> None:
    calendar = synthetic_market_calendar(version)
    if calendar is None:
        raise ValueError("PROSPECTIVE_REGISTERED_CALENDAR_UNAVAILABLE")
    local = plan.knowledge_cutoff.astimezone(ZoneInfo("Asia/Shanghai"))
    sessions = tuple(
        session
        for session in historical_sessions(calendar)
        if session.closed_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m")
        == plan.plan_month
    )
    if (
        not sessions
        or local.date() != sessions[-1].closed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    ):
        raise ValueError("plan requires its original monthly trading cutoff")
    entry = calendar.sessions_after_open(plan.knowledge_cutoff, 5)
    if (
        len(entry) != 5
        or plan.first_entry_at != next_market_session_open_after(plan.knowledge_cutoff, version)
        or plan.disclosure_at != entry[-1].closed_at
        or plan.matures_at < six_month_terminal_evaluation_at(entry[-1].closed_at, version)
    ):
        raise ValueError("PROSPECTIVE_REGISTERED_CALENDAR_WINDOW_MISMATCH")


class OperationsPolicy(EvaluationContract):
    window_months: int = Field(ge=1, strict=True)
    completion_floor: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    coverage_floor: Decimal = Field(gt=0, le=1, allow_inf_nan=False)


class EvidenceFloor(EvaluationContract):
    mature_batches: int = Field(ge=1, strict=True)
    nonoverlapping_windows: int = Field(ge=1, strict=True)
    high_band_records: int = Field(ge=1, strict=True)


class MaturityPolicy(EvaluationContract):
    observation_batches: int = Field(ge=1, strict=True)
    observation_windows: int = Field(ge=1, strict=True)
    high_band_threshold: Decimal = Field(gt=0, lt=1, allow_inf_nan=False)


class BatchPopulation(EvaluationContract):
    """Host-resolved members joined to their saved original admission decisions."""

    plan_month: str
    registrations: tuple[EvaluationRegistration, ...]
    members: tuple[EvaluationMember, ...]
    standard_event_id: str | None
    selection_abstained: bool = False

    @model_validator(mode="after")
    def validate_abstention(self) -> "BatchPopulation":
        if self.selection_abstained and (
            self.registrations or self.members or self.standard_event_id is not None
        ):
            raise ValueError("selection abstention cannot invent an evaluation population")
        return self


class PopulationPolicy(EvaluationContract):
    maturity: MaturityPolicy
    cohort_size: int = Field(ge=1, strict=True)
    selection_bundle: DecisionCaseVersionBundle
    research_bundle: DecisionCaseVersionBundle
    candidate_bundle: DecisionCaseVersionBundle
    standard_bundle: DecisionCaseVersionBundle
    selection_policy: SelectionPolicy
    selection_strategy_version: str = Field(min_length=1)
    raw_score_model_version: str = Field(min_length=1)
    calibrator_version: str = Field(min_length=1)
    standard_quantity: Decimal = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_cohort(self) -> "PopulationPolicy":
        if self.cohort_size != self.selection_policy.cohort_size:
            raise ValueError("population size must preserve its frozen selection policy")
        return self


class BatchLink(EvaluationContract):
    selection_event_id: str = Field(min_length=1)
    candidate_event_id: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )


class FormalPolicy(EvaluationContract):
    cohort: CohortEvaluationPolicy
    node_months: tuple[str, ...] = Field(min_length=1)
    increment_batches: int = Field(gt=0, strict=True)
    increment_windows: int = Field(gt=0, strict=True)
    total_alpha: Decimal = Field(gt=0, lt=1, allow_inf_nan=False)
    block_lengths: tuple[int, ...] = Field(min_length=1)
    bootstrap_repetitions: int = Field(ge=99, strict=True)
    inner_repetitions: int = Field(ge=49, strict=True)
    overall_pass_floor: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    overall_drawdown_floor: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    calibration_success_floor: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    overconfidence_ceiling: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    minimum_availability: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    coverage_floor: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    regime_pass_floor: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    regime_drawdown_floor: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    regime_batches: int = Field(gt=0, strict=True)
    regime_formed: int = Field(gt=0, strict=True)
    regime_windows: int = Field(gt=0, strict=True)
    regime_periods: int = Field(gt=0, strict=True)
    regime_high_band_records: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def validate_sequence(self) -> "FormalPolicy":
        if self.node_months != tuple(sorted(set(self.node_months))):
            raise ValueError("formal nodes must retain their unique preregistered order")
        if any(length < 6 for length in self.block_lengths) or len(set(self.block_lengths)) != len(
            self.block_lengths
        ):
            raise ValueError("formal blocks must be distinct and cover the outcome horizon")
        return self


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
    population_policy: PopulationPolicy | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    formal_policy: FormalPolicy | None = Field(default=None, exclude_if=lambda value: value is None)

    def same_scientific_population_as(self, other: "CycleRegistration") -> bool:
        """Delivery provenance, new node dates and gate edits cannot erase a sequence."""
        if self.population_policy is None or other.population_policy is None:
            return False
        scientific_fields = {
            "cohort_size",
            "selection_policy",
            "selection_strategy_version",
            "raw_score_model_version",
            "calibrator_version",
            "standard_quantity",
        }
        return (
            self.calendar_version == other.calendar_version
            and set(self.source_scopes) == set(other.source_scopes)
            and self.population_policy.model_dump(include=scientific_fields)
            == other.population_policy.model_dump(include=scientific_fields)
        )

    @model_validator(mode="after")
    def validate_timeline(self) -> "CycleRegistration":
        policy = self.population_policy
        for node in self.plan_nodes:
            if node.calendar_version is not None and node.calendar_version != self.calendar_version:
                raise ValueError("plan nodes must preserve the registered calendar version")
            if policy is not None:
                validate_market_plan(node, self.calendar_version)
        if policy is not None:
            for bundle in (
                policy.selection_bundle,
                policy.research_bundle,
                policy.candidate_bundle,
                policy.standard_bundle,
            ):
                if (
                    bundle.host_source_sha != self.source_version_bundle.host_source_sha
                    or bundle.runtime_release != self.source_version_bundle.runtime_release
                ):
                    raise ValueError("population sources require the locked host and runtime")
            if (
                policy.maturity.observation_batches >= self.formal_floor.mature_batches
                or policy.maturity.observation_windows >= self.formal_floor.nonoverlapping_windows
            ):
                raise ValueError("observation milestones must precede formal maturity")
        months = tuple(node.plan_month for node in self.plan_nodes)
        formal = self.formal_policy
        if formal is not None:
            if policy is None:
                raise ValueError("formal inference requires an original population policy")
            if (
                set(formal.node_months) - set(months)
                or formal.cohort.source_version_bundle != policy.selection_bundle
                or formal.cohort.selection_policy != policy.selection_policy
                or formal.cohort.strategy_version != policy.selection_strategy_version
                or formal.cohort.standard_quantity != policy.standard_quantity
                or formal.cohort.market_calendar_version != self.calendar_version
            ):
                raise ValueError("formal inference must preserve original source and plan locks")
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
    batch_link: BatchLink | None = Field(default=None, exclude_if=lambda value: value is None)

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
    operation: Literal["REGISTER", "OBSERVE", "INCIDENT", "REVIEW", "CHECK"]
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
    formal_node_month: str | None = Field(default=None, exclude_if=lambda value: value is None)
    formal_months: tuple[HistoricalMonthInput, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def validate_operation(self) -> "CycleCommand":
        if (self.operation == "CHECK") != (self.formal_node_month is not None):
            raise ValueError("only a formal check names its original preregistered node")
        if self.operation != "CHECK" and self.formal_months:
            raise ValueError("only formal inference consumes cohort evaluation evidence")
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


class FormalMonth(EvaluationContract):
    cohort: HistoricalMonthResult
    probabilities: tuple[EvaluationMember, ...]
    batch_due: bool = True
    missing_high_band_records: int = Field(default=0, ge=0, strict=True)
    evaluation_start_at: AwareDatetime | None = None
    valid_monthly: bool = False
    has_candidates: bool = False


class FormalGate(EvaluationContract):
    estimate: Decimal | None
    bound: Decimal | None
    direction: Literal["LOWER", "UPPER"]
    threshold: Decimal
    strict: bool
    passed: bool | None
    block_bounds: dict[str, Decimal | None]
    undefined_resamples: dict[str, int]


class FormalRegimeWatermark(EvaluationContract):
    mature_batches: int
    formed_batches: int
    stock_windows: int
    stock_periods: int
    probability_months: int
    high_band_records: int
    probability_windows: int
    probability_periods: int
    stock_sufficient: bool
    probability_sufficient: bool


class FormalInference(EvaluationContract):
    disposition: Literal["PASSED", "FAILED", "INDETERMINATE"]
    gates: dict[str, FormalGate]
    regimes: dict[str, FormalRegimeWatermark]
    sampling_digests: dict[str, str]
    unreliable_tail: bool
    calibration_records: int
    high_band_records: int
    due_missing_high_band_records: int = 0
    qualification_scope: Literal["D0_SYNTHETIC_CONTRACT_ONLY"] = "D0_SYNTHETIC_CONTRACT_ONLY"
    qualified: Literal[False] = False


class CycleFormalLook(EvaluationContract):
    disposition: Literal[
        "WAITING_FOR_MATURITY",
        "WAITING_FOR_INFERENCE",
        "SKIPPED",
        "READY",
        "PASSED",
        "FAILED",
        "INDETERMINATE",
    ]
    actual_ordinal: int = Field(default=0, ge=0, strict=True)
    alpha_spent: Decimal = Field(default=Decimal(0), ge=0, lt=1)
    alpha_this: Decimal = Field(default=Decimal(0), ge=0, lt=1)
    node_month: str | None = None
    mature_batches: int = 0
    nonoverlapping_windows: int = 0
    waiting_for: tuple[str, ...] = ()
    inference: FormalInference | None = None


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
