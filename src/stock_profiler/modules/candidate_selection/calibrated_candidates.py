"""Frozen, deterministic candidate calibration and release contracts."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Literal, NamedTuple

from pydantic import AwareDatetime, Field, model_validator

from stock_profiler.modules.candidate_selection.universe import UniverseContract
from stock_profiler.modules.portfolio.market_calendar import (
    next_market_session_open_after,
    six_month_terminal_evaluation_at,
    synthetic_market_calendar,
)

CALIBRATOR_VERSION: Literal["monotone-firth-logistic-v1"] = "monotone-firth-logistic-v1"
PROBABILITY_THRESHOLD = Decimal("0.80")
_MINIMUM_REUSABLE_PROBABILITY = Decimal("1e-32")
_MAXIMUM_REUSABLE_PROBABILITY = Decimal("0." + "9" * 32)
_MINIMUM_MATURE_MONTHS = 60
_MINIMUM_MATURE_RECORDS = 500
_MINIMUM_RECORDS_PER_CLASS = 50
_MINIMUM_RECENT_DIAGNOSTIC_RECORDS = 200
_RELIABILITY_BIN_COUNT = 10
CandidateReleaseDisposition = Literal[
    "CANDIDATES", "VALID_NO_CANDIDATES", "RECOMMENDATION_ABSTAINED", "FAILED", "BLOCKED"
]
CandidateAvailabilityFailure = Literal["DATA", "SYSTEM", "CALIBRATION"]
CandidateQualificationExplanationStatus = Literal["VALID", "AT_RISK", "NOT_QUALIFIED"]


class _LogisticState(NamedTuple):
    score_intercept: float
    score_slope: float
    information_intercept: float
    inverse_intercept: float
    inverse_cross: float
    inverse_slope: float
    objective: float


MarketStateQualificationStatus = Literal[
    "NOT_OBTAINED", "VALID", "AT_RISK", "SUSPENDED", "REVOKED", "EXPIRED"
]


class CalibrationRecord(UniverseContract):
    """One already matured raw-score label in the calibration population."""

    record_id: str = Field(min_length=1)
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    source_research_event_id: str = Field(min_length=1)
    security_id: str = Field(min_length=1)
    research_id: str = Field(min_length=1)
    raw_score_model_version: str = Field(min_length=1)
    raw_score_frozen_at: AwareDatetime
    raw_score_training_watermark_at: AwareDatetime
    raw_success_score: Decimal
    out_of_sample_probability: Decimal = Field(gt=Decimal("0"), lt=Decimal("1"))
    terminal_success: bool
    entry_at: AwareDatetime | None = None
    entry_window_ends_at: AwareDatetime
    unified_maturity_at: AwareDatetime
    label_available_at: AwareDatetime
    market_calendar_version: str = Field(min_length=1)


class MarketStateQualification(UniverseContract):
    """Snapshot of one persisted qualification decision for one market state."""

    market_state: Literal["BULL", "BEAR", "SIDEWAYS"]
    status: MarketStateQualificationStatus
    qualification_id: str = Field(min_length=1)
    qualification_scope: Literal["D0_SYNTHETIC_CONTRACT_ONLY"] = "D0_SYNTHETIC_CONTRACT_ONLY"
    capability_version: str = Field(min_length=1)
    market_calendar_version: str = Field(min_length=1)
    recorded_at: AwareDatetime
    current_status_recorded_at: AwareDatetime | None = None
    valid_through: AwareDatetime

    @model_validator(mode="after")
    def validate_status_timestamp(self) -> MarketStateQualification:
        if (
            self.current_status_recorded_at is not None
            and self.current_status_recorded_at < self.recorded_at
        ):
            raise ValueError("qualification status cannot precede its authorization")
        return self


class CandidateRiskGate(UniverseContract):
    """One frozen independent-risk gate carried into a candidate explanation."""

    gate_id: str = Field(min_length=1)
    status: Literal["PASSED", "FAILED"]


class CandidateEvidenceClock(UniverseContract):
    """Source and validation clocks for one persisted research evidence item."""

    evidence_id: str = Field(min_length=1)
    effective_at: AwareDatetime | None = None
    source_published_at: AwareDatetime | None = None
    acquired_at: AwareDatetime | None = None
    validated_at: AwareDatetime | None = None
    knowledge_cutoff: AwareDatetime


class CandidateInput(UniverseContract):
    """Frozen member evidence handed from research and independent risk."""

    security_id: str = Field(min_length=1)
    research_id: str = Field(min_length=1)
    raw_success_score: Decimal
    data_complete: bool
    risk_status: Literal["ACCEPTED", "REJECTED", "FAILED"]
    risk_gates: tuple[CandidateRiskGate, ...] = Field(min_length=1)
    risk_reasons: tuple[str, ...] = Field(min_length=1)
    thesis: str = Field(min_length=1)
    principal_risks: tuple[str, ...] = Field(min_length=1)
    evidence_freshness: str = Field(min_length=1)
    evidence_clocks: tuple[CandidateEvidenceClock, ...] = ()


class MarketSession(UniverseContract):
    """A market session from the immutable calendar used for the candidate batch."""

    market_date: date
    opens_at: AwareDatetime
    closes_at: AwareDatetime
    session_sequence: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_session(self) -> MarketSession:
        if self.opens_at.date() != self.market_date or self.closes_at.date() != self.market_date:
            raise ValueError("market session date must match its open and close clocks")
        if self.closes_at <= self.opens_at:
            raise ValueError("market session must close after it opens")
        return self


class CandidateReleaseCommand(UniverseContract):
    """Complete D0 input for one immutable, calibrated candidate publication."""

    contract_version: Literal["1.0.0"]
    synthetic: Literal[True]
    generator_version: str = Field(min_length=1)
    seed: int
    batch_id: str = Field(min_length=1)
    qualification_scope: Literal["D0_SYNTHETIC_CONTRACT_ONLY"]
    capability_version: str = Field(min_length=1)
    research_object_id: str = Field(min_length=1)
    research_event_id: str = Field(min_length=1)
    purpose: Literal["CANDIDATE_BUY"]
    knowledge_cutoff: AwareDatetime
    published_at: AwareDatetime
    last_completed_market_session_sequence: int = Field(gt=0)
    market_state: Literal["BULL", "BEAR", "SIDEWAYS"]
    market_calendar_version: str = Field(min_length=1)
    qualifications: tuple[MarketStateQualification, ...] = ()
    calibrator_version: str = Field(default=CALIBRATOR_VERSION, min_length=1)
    label_watermark_at: AwareDatetime
    calibrator_selection_window_months: tuple[str, ...]
    calibrator_selection_records: tuple[CalibrationRecord, ...]
    training_window_months: tuple[str, ...]
    training_records: tuple[CalibrationRecord, ...]
    recent_diagnostic_window_months: tuple[str, ...]
    recent_diagnostic_records: tuple[CalibrationRecord, ...]
    candidates: tuple[CandidateInput, ...] = Field(min_length=1)
    market_sessions: tuple[MarketSession, ...]

    @model_validator(mode="after")
    def validate_frozen_inputs(self) -> CandidateReleaseCommand:
        if self.label_watermark_at > self.knowledge_cutoff:
            raise ValueError("label watermark cannot follow the frozen knowledge cutoff")
        if len(set(self.training_window_months)) != len(self.training_window_months):
            raise ValueError("calibration training months must be unique")
        if len({candidate.security_id for candidate in self.candidates}) != len(self.candidates):
            raise ValueError("candidate release members must have unique securities")
        if len({candidate.research_id for candidate in self.candidates}) != len(self.candidates):
            raise ValueError("candidate release members must have unique research identities")
        if tuple(sorted(self.market_sessions, key=lambda session: session.opens_at)) != (
            self.market_sessions
        ):
            raise ValueError("market sessions must be in chronological order")
        if len({session.market_date for session in self.market_sessions}) != len(
            self.market_sessions
        ):
            raise ValueError("market session dates must be unique")
        return self


class CalibratedMember(UniverseContract):
    security_id: str
    research_id: str
    raw_success_score: Decimal
    calibrated_probability: Decimal | None
    candidate: bool
    risk_status: Literal["ACCEPTED", "REJECTED", "FAILED"]
    risk_gates: tuple[CandidateRiskGate, ...]
    risk_reasons: tuple[str, ...]
    market_state_qualified: bool
    market_state_qualification_status: CandidateQualificationExplanationStatus
    data_complete: bool
    thesis: str
    principal_risks: tuple[str, ...]
    evidence_freshness: str
    evidence_clocks: tuple[CandidateEvidenceClock, ...] = ()
    valid_market_dates: tuple[date, ...]
    reasons: tuple[str, ...]


class CalibrationSnapshot(UniverseContract):
    calibrator_version: Literal["monotone-firth-logistic-v1"]
    intercept: Decimal
    slope: Decimal
    calibrator_selection: CalibratorSelectionSnapshot
    calibrator_selection_window_months: tuple[str, ...] = Field(min_length=1)
    training_window_months: tuple[str, ...]
    label_watermark_at: AwareDatetime
    training_record_count: int
    positive_record_count: int
    negative_record_count: int
    unavailable_probability_count: int = Field(default=0, ge=0)
    out_of_sample_diagnostics: CalibrationDiagnostics
    recent_diagnostic_months: tuple[str, ...] = Field(min_length=24, max_length=24)
    recent_diagnostic_sample_count: int = Field(ge=0)
    recent_diagnostic_status: Literal["AVAILABLE", "INSUFFICIENT_DATA", "CALCULATION_FAILED"]
    recent_diagnostics: CalibrationDiagnostics | None


class CalibrationReliabilityBin(UniverseContract):
    lower_probability: Decimal
    upper_probability: Decimal
    sample_count: int = Field(gt=0)
    mean_predicted_probability: Decimal
    observed_success_rate: Decimal


class CalibrationDiagnostics(UniverseContract):
    """Frozen goodness-of-fit diagnostics over one explicitly named score cohort."""

    log_loss: Decimal
    brier_score: Decimal
    reliability_curve: tuple[CalibrationReliabilityBin, ...]
    recalibration_fit_status: Literal["AVAILABLE", "CALCULATION_FAILED"]
    calibration_intercept: Decimal | None
    calibration_slope: Decimal | None


class CalibratorSelectionSnapshot(UniverseContract):
    """Persist the preregistered v1 family decision and its independent OOS evidence."""

    policy: Literal["PRE_REGISTERED_V1_SINGLE_FAMILY"]
    selected_calibrator_version: Literal["monotone-firth-logistic-v1"]
    window_months: tuple[str, ...] = Field(min_length=1)
    label_watermark_at: AwareDatetime
    record_count: int = Field(ge=1)
    diagnostics: CalibrationDiagnostics


class CandidatePopulation(UniverseContract):
    valid_monthly: bool
    recommendation_coverage_denominator: bool
    recommendation_coverage_pass: bool
    availability_failure: CandidateAvailabilityFailure | None = None


class CandidateReleaseOutcome(UniverseContract):
    batch_id: str
    disposition: CandidateReleaseDisposition
    knowledge_cutoff: AwareDatetime
    published_at: AwareDatetime
    qualification_scope: Literal["D0_SYNTHETIC_CONTRACT_ONLY"]
    capability_version: str
    research_object_id: str
    research_event_id: str
    market_calendar_version: str
    market_state: Literal["BULL", "BEAR", "SIDEWAYS"]
    qualification: MarketStateQualification | None = None
    calibration: CalibrationSnapshot | None
    members: tuple[CalibratedMember, ...]
    population: CandidatePopulation
    valid_market_dates: tuple[date, ...]
    reasons: tuple[str, ...]
    availability_failure: CandidateAvailabilityFailure | None = None
    actionable: Literal[False] = False


def freeze_candidate_release(
    command: CandidateReleaseCommand,
    *,
    published_at: datetime | None = None,
    initial_calibration: bool = True,
    recent_diagnostic_records: tuple[CalibrationRecord, ...] | None = None,
    unavailable_probability_count: int = 0,
    recent_diagnostic_integrity_valid: bool = True,
) -> CandidateReleaseOutcome:
    """Calibrate each frozen member, combine independent gates, and fix its only window."""
    publication_time = published_at or command.published_at
    command = command.model_copy(update={"published_at": publication_time})
    window, window_failure = _candidate_window(command)
    window_dates = tuple(session.market_date for session in window)
    calendar_failure = _candidate_window_failure(command, window, window_failure)
    if calendar_failure is not None:
        return calendar_failure
    if command.knowledge_cutoff > publication_time:
        return _release_outcome(
            command,
            window_dates,
            disposition="FAILED",
            reasons=("CANDIDATE_KNOWLEDGE_CUTOFF_AFTER_PUBLICATION",),
            availability_failure="DATA",
        )
    if command.calibrator_version != CALIBRATOR_VERSION:
        return _release_outcome(
            command,
            window_dates,
            disposition="FAILED",
            reasons=("CANDIDATE_CALIBRATOR_VERSION_UNSUPPORTED",),
            availability_failure="CALIBRATION",
        )
    if any(not candidate.data_complete for candidate in command.candidates):
        return _release_outcome(
            command,
            window_dates,
            disposition="FAILED",
            reasons=("CANDIDATE_DATA_INCOMPLETE",),
            availability_failure="DATA",
        )
    qualification = _matching_qualification(command, publication_time)
    if qualification is not None and qualification.valid_through < publication_time:
        qualification = None
    qualification_status = _qualification_explanation_status(qualification, publication_time)
    state_qualified = qualification_status != "NOT_QUALIFIED"
    try:
        calibration = _fit_calibrator(
            command,
            initial_calibration=initial_calibration,
            recent_diagnostic_records=(
                command.recent_diagnostic_records
                if recent_diagnostic_records is None
                else recent_diagnostic_records
            ),
            unavailable_probability_count=unavailable_probability_count,
            recent_diagnostic_integrity_valid=recent_diagnostic_integrity_valid,
        )
    except ValueError as error:
        return _release_outcome(
            command,
            window_dates,
            disposition="FAILED",
            reasons=(str(error),),
            availability_failure="CALIBRATION",
        )
    members: list[CalibratedMember] = []
    for candidate in command.candidates:
        try:
            probability = _probability(calibration, candidate.raw_success_score)
        except ArithmeticError:
            return _release_outcome(
                command,
                window_dates,
                disposition="FAILED",
                calibration=calibration,
                reasons=("CALIBRATION_PROBABILITY_FAILED",),
                availability_failure="CALIBRATION",
            )
        reasons: list[str] = []
        if probability < PROBABILITY_THRESHOLD:
            reasons.append("PROBABILITY_BELOW_THRESHOLD")
        if not state_qualified:
            reasons.append("MARKET_STATE_NOT_QUALIFIED")
        elif qualification is not None and qualification.status == "AT_RISK":
            reasons.append("MARKET_STATE_QUALIFICATION_AT_RISK")
        if candidate.risk_status == "REJECTED":
            reasons.append("INDEPENDENT_RISK_VETO")
        elif candidate.risk_status == "FAILED":
            reasons.append("INDEPENDENT_RISK_UNAVAILABLE")
        members.append(
            CalibratedMember(
                security_id=candidate.security_id,
                research_id=candidate.research_id,
                raw_success_score=candidate.raw_success_score,
                calibrated_probability=probability,
                candidate=(
                    probability >= PROBABILITY_THRESHOLD
                    and state_qualified
                    and candidate.risk_status == "ACCEPTED"
                ),
                risk_status=candidate.risk_status,
                risk_gates=candidate.risk_gates,
                risk_reasons=candidate.risk_reasons,
                market_state_qualified=state_qualified,
                market_state_qualification_status=qualification_status,
                data_complete=candidate.data_complete,
                thesis=candidate.thesis,
                principal_risks=candidate.principal_risks,
                evidence_freshness=candidate.evidence_freshness,
                evidence_clocks=candidate.evidence_clocks,
                valid_market_dates=window_dates,
                reasons=tuple(reasons) or ("ALL_CANDIDATE_GATES_PASSED",),
            )
        )
    if any(candidate.risk_status == "FAILED" for candidate in command.candidates):
        unavailable_members = tuple(
            member.model_copy(
                update={
                    "candidate": False,
                    "reasons": tuple(
                        dict.fromkeys((*member.reasons, "CANDIDATE_BATCH_AVAILABILITY_FAILED"))
                    ),
                }
            )
            for member in members
        )
        return _release_outcome(
            command,
            window_dates,
            disposition="FAILED",
            calibration=calibration,
            members=unavailable_members,
            reasons=("INDEPENDENT_RISK_UNAVAILABLE",),
            availability_failure="SYSTEM",
        )
    disposition: CandidateReleaseDisposition = (
        "CANDIDATES"
        if any(member.candidate for member in members)
        else "RECOMMENDATION_ABSTAINED"
        if not state_qualified
        else "VALID_NO_CANDIDATES"
    )
    if publication_time > window[-1].closes_at:
        late_reason = "PUBLICATION_AFTER_CANDIDATE_WINDOW"
        historical_members = exclude_candidate_members(tuple(members), (late_reason,))
        return _release_outcome(
            command,
            window_dates,
            disposition="FAILED",
            calibration=calibration,
            members=historical_members,
            reasons=(late_reason,),
        )
    return _release_outcome(
        command,
        window_dates,
        disposition=disposition,
        calibration=calibration,
        members=tuple(members),
        reasons=() if disposition == "CANDIDATES" else (disposition,),
    )


def candidate_release_availability_failure(
    command: CandidateReleaseCommand,
    *,
    published_at: datetime,
    reason: str,
    availability_failure: CandidateAvailabilityFailure,
    include_member_details: bool = True,
) -> CandidateReleaseOutcome:
    """Freeze a non-actionable availability result when host-side version checks fail."""
    command = command.model_copy(update={"published_at": published_at})
    window, window_failure = _candidate_window(command)
    members = None if include_member_details else ()
    calendar_failure = _candidate_window_failure(
        command,
        window,
        window_failure,
        members=members,
    )
    if calendar_failure is not None:
        return calendar_failure
    return _release_outcome(
        command,
        tuple(session.market_date for session in window),
        disposition="FAILED",
        reasons=(reason,),
        members=members,
        availability_failure=availability_failure,
    )


def candidate_release_blocked_by_business_prerequisite(
    command: CandidateReleaseCommand,
    *,
    published_at: datetime,
    reason: str,
    include_member_details: bool = True,
) -> CandidateReleaseOutcome:
    """Record that an unsuccessful earlier business outcome prevented release."""
    command = command.model_copy(update={"published_at": published_at})
    window, _ = _candidate_window(command)
    return _release_outcome(
        command,
        tuple(session.market_date for session in window),
        disposition="BLOCKED",
        reasons=(reason,),
        members=None if include_member_details else (),
    )


def finalize_candidate_release_publication(
    command: CandidateReleaseCommand,
    outcome: CandidateReleaseOutcome,
    *,
    published_at: datetime,
) -> CandidateReleaseOutcome:
    """Fail an otherwise valid batch if its durable publication time misses the window."""
    window, _ = _candidate_window(command)
    matching_qualification = _matching_qualification(command, published_at)
    preserve_frozen_qualification = outcome.published_at == published_at and (
        outcome.disposition == "RECOMMENDATION_ABSTAINED"
        or matching_qualification is None
        or (outcome.qualification is not None and matching_qualification == outcome.qualification)
    )
    qualification = (
        outcome.qualification if preserve_frozen_qualification else matching_qualification
    )
    if (
        outcome.disposition in {"CANDIDATES", "VALID_NO_CANDIDATES", "RECOMMENDATION_ABSTAINED"}
        and len(window) == 5
        and published_at > window[-1].closes_at
    ):
        late_reason = "PUBLICATION_AFTER_CANDIDATE_WINDOW"
        members = exclude_candidate_members(outcome.members, (late_reason,))
        return outcome.model_copy(
            update={
                "published_at": published_at,
                "qualification": qualification,
                "disposition": "FAILED",
                "members": members,
                "population": outcome.population.model_copy(
                    update={
                        "valid_monthly": False,
                        "recommendation_coverage_denominator": False,
                        "recommendation_coverage_pass": False,
                    }
                ),
                "reasons": (late_reason,),
            }
        )
    published = outcome.model_copy(
        update={"published_at": published_at, "qualification": qualification}
    )
    if published.disposition not in {
        "CANDIDATES",
        "VALID_NO_CANDIDATES",
        "RECOMMENDATION_ABSTAINED",
    }:
        return published

    qualification_current = (
        qualification is not None
        and qualification.status in {"VALID", "AT_RISK"}
        and qualification.valid_through >= published_at
    )
    if published.disposition == "RECOMMENDATION_ABSTAINED":
        return published
    if qualification_current:
        status = _qualification_explanation_status(qualification, published_at)
        return published.model_copy(
            update={"members": _members_with_qualification_status(published.members, status)}
        )

    expired = qualification is not None and qualification.valid_through < published_at
    additional_reasons: tuple[str, ...] = ()
    if expired:
        additional_reasons = ("MARKET_STATE_QUALIFICATION_EXPIRED",)
    added_reasons = ("MARKET_STATE_NOT_QUALIFIED", *additional_reasons)
    members = _members_with_qualification_status(
        published.members,
        "NOT_QUALIFIED",
        additional_reasons=additional_reasons,
    )
    return published.model_copy(
        update={
            "disposition": "RECOMMENDATION_ABSTAINED",
            "members": members,
            "population": published.population.model_copy(
                update={"recommendation_coverage_pass": False}
            ),
            "reasons": added_reasons,
        }
    )


def _matching_qualification(
    command: CandidateReleaseCommand,
    observed_at: datetime,
) -> MarketStateQualification | None:
    return next(
        (
            item
            for item in command.qualifications
            if item.market_state == command.market_state
            and item.capability_version == command.capability_version
            and item.market_calendar_version == command.market_calendar_version
            and item.qualification_scope == command.qualification_scope
            and item.recorded_at <= command.knowledge_cutoff
            and (item.current_status_recorded_at or item.recorded_at) <= observed_at
        ),
        None,
    )


def _qualification_explanation_status(
    qualification: MarketStateQualification | None,
    observed_at: datetime,
) -> CandidateQualificationExplanationStatus:
    if (
        qualification is None
        or qualification.status not in {"VALID", "AT_RISK"}
        or qualification.valid_through < observed_at
    ):
        return "NOT_QUALIFIED"
    if qualification.status == "AT_RISK":
        return "AT_RISK"
    return "VALID"


def _members_with_qualification_status(
    members: tuple[CalibratedMember, ...],
    status: CandidateQualificationExplanationStatus,
    *,
    additional_reasons: tuple[str, ...] = (),
) -> tuple[CalibratedMember, ...]:
    updated: list[CalibratedMember] = []
    for member in members:
        reasons = tuple(
            reason
            for reason in member.reasons
            if reason
            not in {
                "ALL_CANDIDATE_GATES_PASSED",
                "MARKET_STATE_NOT_QUALIFIED",
                "MARKET_STATE_QUALIFICATION_AT_RISK",
            }
        )
        if status == "AT_RISK":
            reasons = (*reasons, "MARKET_STATE_QUALIFICATION_AT_RISK")
        elif status == "NOT_QUALIFIED":
            reasons = (*reasons, "MARKET_STATE_NOT_QUALIFIED")
        reasons = tuple(dict.fromkeys((*reasons, *additional_reasons)))
        if not reasons:
            reasons = ("ALL_CANDIDATE_GATES_PASSED",)
        updated.append(
            member.model_copy(
                update={
                    "candidate": member.candidate and status != "NOT_QUALIFIED",
                    "market_state_qualified": status != "NOT_QUALIFIED",
                    "market_state_qualification_status": status,
                    "reasons": reasons,
                }
            )
        )
    return tuple(updated)


def _candidate_window(
    command: CandidateReleaseCommand,
) -> tuple[tuple[MarketSession, ...], str | None]:
    window = tuple(
        session
        for session in command.market_sessions
        if session.opens_at > command.knowledge_cutoff
    )[:5]
    if len(window) != 5:
        return window, "CANDIDATE_WINDOW_CALENDAR_INCOMPLETE"
    calendar = synthetic_market_calendar(command.market_calendar_version)
    if calendar is None:
        return window, "CANDIDATE_WINDOW_CALENDAR_SNAPSHOT_MISMATCH"
    saved_window = calendar.sessions_after_open(command.knowledge_cutoff, 5)
    if len(saved_window) != 5:
        return window, "CANDIDATE_WINDOW_CALENDAR_SNAPSHOT_MISMATCH"
    if any(
        session.market_date != saved.closed_at.date()
        or session.opens_at != saved.closed_at - timedelta(hours=7)
        or session.closes_at != saved.closed_at
        for session, saved in zip(window, saved_window, strict=True)
    ):
        return window, "CANDIDATE_WINDOW_CALENDAR_SNAPSHOT_MISMATCH"
    if any(
        session.session_sequence != saved.ordinal
        for session, saved in zip(window, saved_window, strict=True)
    ):
        return window, "CANDIDATE_WINDOW_CALENDAR_SEQUENCE_INVALID"
    last_completed_session = calendar.session_at_or_before(command.knowledge_cutoff)
    last_completed_sequence = (
        last_completed_session.ordinal
        if last_completed_session is not None
        else saved_window[0].ordinal - 1
    )
    if last_completed_sequence != command.last_completed_market_session_sequence:
        return window, "CANDIDATE_WINDOW_CALENDAR_SEQUENCE_INVALID"
    return window, None


def exclude_candidate_members(
    members: tuple[CalibratedMember, ...],
    reasons: tuple[str, ...],
) -> tuple[CalibratedMember, ...]:
    """Remove candidate eligibility while retaining the original score and other evidence."""
    excluded: list[CalibratedMember] = []
    for member in members:
        member_reasons = tuple(
            reason for reason in member.reasons if reason != "ALL_CANDIDATE_GATES_PASSED"
        )
        updates: dict[str, object] = {
            "candidate": False,
            "reasons": tuple(dict.fromkeys((*member_reasons, *reasons))),
        }
        excluded.append(member.model_copy(update=updates))
    return tuple(excluded)


def _candidate_window_failure(
    command: CandidateReleaseCommand,
    window: tuple[MarketSession, ...],
    failure: str | None,
    *,
    members: tuple[CalibratedMember, ...] | None = None,
) -> CandidateReleaseOutcome | None:
    if failure is None:
        return None
    return _release_outcome(
        command,
        tuple(session.market_date for session in window),
        disposition="FAILED",
        reasons=(failure,),
        members=members,
        availability_failure="DATA",
    )


def _release_outcome(
    command: CandidateReleaseCommand,
    valid_market_dates: tuple[date, ...],
    *,
    disposition: CandidateReleaseDisposition,
    reasons: tuple[str, ...],
    calibration: CalibrationSnapshot | None = None,
    members: tuple[CalibratedMember, ...] | None = None,
    availability_failure: CandidateAvailabilityFailure | None = None,
) -> CandidateReleaseOutcome:
    qualification = _matching_qualification(command, command.published_at)
    valid_monthly = disposition in {
        "CANDIDATES",
        "VALID_NO_CANDIDATES",
        "RECOMMENDATION_ABSTAINED",
    }
    if members is None:
        state_qualified = (
            qualification is not None
            and qualification.status in {"VALID", "AT_RISK"}
            and qualification.valid_through >= command.published_at
        )
        unavailable_reasons = tuple(dict.fromkeys((*reasons, "PROBABILITY_UNAVAILABLE")))
        members = tuple(
            CalibratedMember(
                security_id=candidate.security_id,
                research_id=candidate.research_id,
                raw_success_score=candidate.raw_success_score,
                calibrated_probability=None,
                candidate=False,
                risk_status=candidate.risk_status,
                market_state_qualified=state_qualified,
                market_state_qualification_status=_qualification_explanation_status(
                    qualification,
                    command.published_at,
                ),
                data_complete=candidate.data_complete,
                thesis=candidate.thesis,
                principal_risks=candidate.principal_risks,
                evidence_freshness=candidate.evidence_freshness,
                evidence_clocks=candidate.evidence_clocks,
                valid_market_dates=valid_market_dates,
                reasons=unavailable_reasons,
                risk_gates=candidate.risk_gates,
                risk_reasons=candidate.risk_reasons,
            )
            for candidate in command.candidates
        )
    return CandidateReleaseOutcome(
        batch_id=command.batch_id,
        disposition=disposition,
        knowledge_cutoff=command.knowledge_cutoff,
        published_at=command.published_at,
        qualification_scope=command.qualification_scope,
        capability_version=command.capability_version,
        research_object_id=command.research_object_id,
        research_event_id=command.research_event_id,
        market_calendar_version=command.market_calendar_version,
        market_state=command.market_state,
        qualification=qualification,
        calibration=calibration,
        members=members,
        population=CandidatePopulation(
            valid_monthly=valid_monthly,
            recommendation_coverage_denominator=valid_monthly,
            recommendation_coverage_pass=disposition == "CANDIDATES",
            availability_failure=availability_failure,
        ),
        valid_market_dates=valid_market_dates,
        reasons=reasons,
        availability_failure=availability_failure,
    )


def _fit_calibrator(
    command: CandidateReleaseCommand,
    *,
    initial_calibration: bool = True,
    recent_diagnostic_records: tuple[CalibrationRecord, ...] | None = None,
    unavailable_probability_count: int = 0,
    recent_diagnostic_integrity_valid: bool = True,
) -> CalibrationSnapshot:
    if command.calibrator_version != CALIBRATOR_VERSION:
        raise ValueError("CALIBRATOR_VERSION_UNSUPPORTED")
    months = command.training_window_months
    selection_months = tuple(
        sorted({record.month for record in command.calibrator_selection_records})
    )
    diagnostic_months = tuple(sorted(command.recent_diagnostic_window_months))
    if len(months) < _MINIMUM_MATURE_MONTHS:
        raise ValueError("CALIBRATION_REQUIRES_60_MATURE_MONTHS")
    by_month: dict[str, list[CalibrationRecord]] = {}
    for record in command.training_records:
        if record.raw_score_frozen_at.astimezone(UTC).strftime("%Y-%m") != record.month:
            raise ValueError("CALIBRATION_RAW_SCORE_MONTH_MISMATCH")
        if record.raw_score_training_watermark_at > record.raw_score_frozen_at:
            raise ValueError("CALIBRATION_RAW_SCORE_NOT_OUT_OF_SAMPLE")
        if record.entry_window_ends_at <= record.raw_score_frozen_at:
            raise ValueError("CALIBRATION_ENTRY_WINDOW_NOT_AFTER_PREDICTION")
        if record.entry_at is None:
            if record.terminal_success:
                raise ValueError("CALIBRATION_ENTRY_INVALID_CANNOT_SUCCEED")
        elif record.entry_at < record.raw_score_frozen_at:
            raise ValueError("CALIBRATION_EXECUTABLE_ENTRY_OUTSIDE_ENTRY_WINDOW")
        elif record.entry_at < next_market_session_open_after(
            record.raw_score_frozen_at,
            record.market_calendar_version,
        ):
            raise ValueError("CALIBRATION_ENTRY_BEFORE_WINDOW_OPEN")
        elif record.entry_at > record.entry_window_ends_at:
            raise ValueError("CALIBRATION_EXECUTABLE_ENTRY_OUTSIDE_ENTRY_WINDOW")
        maturity_anchor = record.entry_at or record.entry_window_ends_at
        if record.unified_maturity_at != six_month_terminal_evaluation_at(
            maturity_anchor, record.market_calendar_version
        ):
            raise ValueError("LABEL_BEFORE_UNIFIED_SIX_MONTH_MATURITY")
        if record.label_available_at < record.unified_maturity_at:
            raise ValueError("LABEL_BEFORE_UNIFIED_SIX_MONTH_MATURITY")
        by_month.setdefault(record.month, []).append(record)
    if len({record.record_id for record in command.training_records}) != len(
        command.training_records
    ):
        raise ValueError("CALIBRATION_RECORD_IDENTITIES_NOT_UNIQUE")
    mature_months = tuple(
        sorted(
            month
            for month, month_records in by_month.items()
            if all(
                record.unified_maturity_at <= command.label_watermark_at
                and record.label_available_at <= command.label_watermark_at
                for record in month_records
            )
        )
    )
    if len(mature_months) < _MINIMUM_MATURE_MONTHS:
        raise ValueError("CALIBRATION_REQUIRES_60_MATURE_MONTHS")
    if not set(months).issubset(mature_months):
        raise ValueError("CALIBRATION_TRAINING_WINDOW_NOT_MATURE")
    if initial_calibration:
        expected_months = mature_months[: len(months)]
    else:
        if len(months) != _MINIMUM_MATURE_MONTHS:
            raise ValueError("CALIBRATION_ROLLING_WINDOW_MUST_BE_60_MONTHS")
        expected_months = mature_months[-_MINIMUM_MATURE_MONTHS:]
    if months != expected_months:
        raise ValueError(
            "CALIBRATION_INITIAL_WINDOW_NOT_EARLIEST"
            if initial_calibration
            else "CALIBRATION_TRAINING_WINDOW_NOT_LATEST"
        )
    records = tuple(record for month in months for record in by_month[month])
    if len(months) > _MINIMUM_MATURE_MONTHS:
        for window_length in range(_MINIMUM_MATURE_MONTHS, len(months)):
            qualifying_records = tuple(
                record for month in months[:window_length] for record in by_month[month]
            )
            qualifying_positives = sum(record.terminal_success for record in qualifying_records)
            qualifying_negatives = len(qualifying_records) - qualifying_positives
            if (
                len(qualifying_records) >= _MINIMUM_MATURE_RECORDS
                and qualifying_positives >= _MINIMUM_RECORDS_PER_CLASS
                and qualifying_negatives >= _MINIMUM_RECORDS_PER_CLASS
            ):
                raise ValueError("CALIBRATION_INITIAL_WINDOW_EXCEEDS_MINIMUM")
    if selection_months != command.calibrator_selection_window_months:
        raise ValueError("CALIBRATOR_SELECTION_WINDOW_MISMATCH")
    if not selection_months:
        raise ValueError("CALIBRATOR_SELECTION_REQUIRES_MATURE_MONTHS")
    if len(set(diagnostic_months)) != 24:
        raise ValueError("RECENT_DIAGNOSTIC_REQUIRES_24_MATURE_MONTHS")
    if (
        set(selection_months) & set(months)
        or set(selection_months) & set(diagnostic_months)
        or set(months) & set(diagnostic_months)
    ):
        raise ValueError("CALIBRATION_WINDOWS_MUST_BE_DISJOINT")
    if not selection_months[-1] < months[0]:
        raise ValueError("CALIBRATOR_SELECTION_WINDOW_NOT_EARLIER")
    authoritative_months = tuple(
        sorted(set(selection_months) | set(months) | set(diagnostic_months))
    )
    if diagnostic_months != authoritative_months[-24:]:
        raise ValueError("RECENT_DIAGNOSTIC_WINDOW_NOT_LATEST")
    if len({record.record_id for record in command.calibrator_selection_records}) != len(
        command.calibrator_selection_records
    ):
        raise ValueError("CALIBRATOR_SELECTION_RECORD_IDENTITIES_NOT_UNIQUE")
    if any(
        record.label_available_at > command.label_watermark_at
        or record.unified_maturity_at > command.label_watermark_at
        for record in command.calibrator_selection_records
    ):
        raise ValueError("CALIBRATOR_SELECTION_LABEL_NOT_MATURE")
    if len(records) < _MINIMUM_MATURE_RECORDS:
        raise ValueError("CALIBRATION_REQUIRES_500_MATURE_RECORDS")
    positives = sum(record.terminal_success for record in records)
    negatives = len(records) - positives
    if positives < _MINIMUM_RECORDS_PER_CLASS or negatives < _MINIMUM_RECORDS_PER_CLASS:
        raise ValueError("CALIBRATION_CLASS_FLOOR_NOT_MET")
    try:
        intercept, slope = _firth_logistic(records)
    except (ArithmeticError, OverflowError, ValueError, ZeroDivisionError) as error:
        raise ValueError("CALIBRATION_FIT_FAILED") from error
    probabilities = tuple(float(record.out_of_sample_probability) for record in records)
    out_of_sample_diagnostics = _calibration_diagnostics(records, probabilities)
    selection_records = tuple(
        record
        for record in command.calibrator_selection_records
        if record.month in set(command.calibrator_selection_window_months)
    )
    selection_diagnostics = _calibration_diagnostics(
        selection_records,
        tuple(float(record.out_of_sample_probability) for record in selection_records),
    )
    selection_watermark_at = max(record.label_available_at for record in selection_records)
    diagnostic_population = (
        command.recent_diagnostic_records
        if recent_diagnostic_records is None
        else recent_diagnostic_records
    )
    diagnostic_watermark_at = (
        command.label_watermark_at
        if recent_diagnostic_records is None
        else command.knowledge_cutoff
    )
    diagnostic_records_by_month: dict[str, list[CalibrationRecord]] = {}
    recent_month_set = set(diagnostic_months)
    diagnostic_record_by_id: dict[str, CalibrationRecord] = {}
    diagnostic_sample_identities: set[tuple[str, str, str]] = set()
    diagnostic_integrity_failed = False
    for record in diagnostic_population:
        if (
            record.month in recent_month_set
            and record.unified_maturity_at <= diagnostic_watermark_at
            and record.label_available_at <= diagnostic_watermark_at
        ):
            previous = diagnostic_record_by_id.get(record.record_id)
            sample_identity = (record.month, record.security_id, record.research_id)
            if previous is not None or sample_identity in diagnostic_sample_identities:
                diagnostic_integrity_failed = True
                continue
            diagnostic_record_by_id[record.record_id] = record
            diagnostic_sample_identities.add(sample_identity)
            diagnostic_records_by_month.setdefault(record.month, []).append(record)
    recent_months = diagnostic_months
    recent_records = tuple(
        record for month in recent_months for record in diagnostic_records_by_month.get(month, ())
    )
    recent_diagnostics = None
    recent_diagnostic_status: Literal["AVAILABLE", "INSUFFICIENT_DATA", "CALCULATION_FAILED"]
    if diagnostic_integrity_failed or not recent_diagnostic_integrity_valid:
        recent_diagnostic_status = "CALCULATION_FAILED"
    elif len(recent_records) < _MINIMUM_RECENT_DIAGNOSTIC_RECORDS:
        recent_diagnostic_status = "INSUFFICIENT_DATA"
    else:
        try:
            recent_diagnostics = _calibration_diagnostics(
                recent_records,
                tuple(float(record.out_of_sample_probability) for record in recent_records),
            )
        except (ArithmeticError, ValueError):
            recent_diagnostic_status = "CALCULATION_FAILED"
        else:
            recent_diagnostic_status = "AVAILABLE"
    return CalibrationSnapshot(
        calibrator_version=command.calibrator_version,
        intercept=Decimal(str(intercept)).quantize(Decimal("0.00000001")),
        slope=Decimal(str(slope)).quantize(Decimal("0.00000001")),
        calibrator_selection=CalibratorSelectionSnapshot(
            policy="PRE_REGISTERED_V1_SINGLE_FAMILY",
            selected_calibrator_version=command.calibrator_version,
            window_months=command.calibrator_selection_window_months,
            label_watermark_at=selection_watermark_at,
            record_count=len(selection_records),
            diagnostics=selection_diagnostics,
        ),
        calibrator_selection_window_months=command.calibrator_selection_window_months,
        training_window_months=months,
        label_watermark_at=command.label_watermark_at,
        training_record_count=len(records),
        positive_record_count=positives,
        negative_record_count=negatives,
        unavailable_probability_count=unavailable_probability_count,
        out_of_sample_diagnostics=out_of_sample_diagnostics,
        recent_diagnostic_months=recent_months,
        recent_diagnostic_sample_count=len(recent_records),
        recent_diagnostic_status=recent_diagnostic_status,
        recent_diagnostics=recent_diagnostics,
    )


def _calibration_diagnostics(
    records: tuple[CalibrationRecord, ...],
    probabilities: tuple[float, ...],
) -> CalibrationDiagnostics:
    if not records or len(records) != len(probabilities):
        raise ValueError("CALIBRATION_DIAGNOSTICS_REQUIRE_MATCHED_RECORDS")
    clipped = tuple(min(max(probability, 1e-15), 1.0 - 1e-15) for probability in probabilities)
    labels = tuple(float(record.terminal_success) for record in records)
    log_loss = -sum(
        label * math.log(probability) + (1.0 - label) * math.log1p(-probability)
        for label, probability in zip(labels, clipped, strict=True)
    ) / len(records)
    brier_score = sum(
        (label - probability) ** 2 for label, probability in zip(labels, probabilities, strict=True)
    ) / len(records)
    bins: list[CalibrationReliabilityBin] = []
    for bin_index in range(_RELIABILITY_BIN_COUNT):
        lower = bin_index / _RELIABILITY_BIN_COUNT
        upper = (bin_index + 1) / _RELIABILITY_BIN_COUNT
        members = tuple(
            (label, probability)
            for label, probability in zip(labels, probabilities, strict=True)
            if lower <= probability < upper
            or (bin_index == _RELIABILITY_BIN_COUNT - 1 and probability == 1.0)
        )
        if not members:
            continue
        bins.append(
            CalibrationReliabilityBin(
                lower_probability=Decimal(str(lower)),
                upper_probability=Decimal(str(upper)),
                sample_count=len(members),
                mean_predicted_probability=Decimal(
                    str(sum(probability for _, probability in members) / len(members))
                ),
                observed_success_rate=Decimal(
                    str(sum(label for label, _ in members) / len(members))
                ),
            )
        )
    logits = tuple(math.log(p / (1.0 - p)) for p in clipped)
    recalibration_fit_status: Literal["AVAILABLE", "CALCULATION_FAILED"]
    if len(set(logits)) < 2:
        diagnostic_intercept = math.log((sum(labels) + 0.5) / (len(labels) - sum(labels) + 0.5))
        diagnostic_slope = 0.0
        recalibration_fit_status = "AVAILABLE"
    else:
        recalibration_records = tuple(
            record.model_copy(update={"raw_success_score": Decimal(str(logit))})
            for record, logit in zip(records, logits, strict=True)
        )
        try:
            diagnostic_intercept, diagnostic_slope = _firth_logistic(recalibration_records)
        except (ArithmeticError, ValueError):
            diagnostic_intercept = None
            diagnostic_slope = None
            recalibration_fit_status = "CALCULATION_FAILED"
        else:
            recalibration_fit_status = "AVAILABLE"

    def decimal(value: float) -> Decimal:
        return Decimal(str(value)).quantize(Decimal("0.00000001"))

    return CalibrationDiagnostics(
        log_loss=decimal(log_loss),
        brier_score=decimal(brier_score),
        reliability_curve=tuple(bins),
        recalibration_fit_status=recalibration_fit_status,
        calibration_intercept=(
            decimal(diagnostic_intercept) if diagnostic_intercept is not None else None
        ),
        calibration_slope=decimal(diagnostic_slope) if diagnostic_slope is not None else None,
    )


def _firth_logistic(records: tuple[CalibrationRecord, ...]) -> tuple[float, float]:
    """Fit two-parameter Firth logistic regression with a nonnegative slope."""
    scores = tuple(float(record.raw_success_score) for record in records)
    labels = tuple(float(record.terminal_success) for record in records)
    if not all(math.isfinite(score) for score in scores) or len(set(scores)) < 2:
        raise ValueError("CALIBRATION_SCORE_VARIATION_REQUIRED")
    intercept = slope = 0.0

    def state(a: float, b: float) -> _LogisticState:
        probabilities = tuple(_sigmoid(a + b * score) for score in scores)
        weights = tuple(probability * (1.0 - probability) for probability in probabilities)
        i00 = sum(weights)
        i01 = sum(weight * score for weight, score in zip(weights, scores, strict=True))
        i11 = sum(weight * score * score for weight, score in zip(weights, scores, strict=True))
        determinant = i00 * i11 - i01 * i01
        if determinant <= 1e-18:
            raise ValueError("CALIBRATION_INFORMATION_SINGULAR")
        inv00, inv01, inv11 = i11 / determinant, -i01 / determinant, i00 / determinant
        adjusted: list[float] = []
        for probability, weight, score, label in zip(
            probabilities, weights, scores, labels, strict=True
        ):
            leverage = weight * (inv00 + 2.0 * score * inv01 + score * score * inv11)
            adjusted.append(label - probability + leverage * (0.5 - probability))
        score0 = sum(adjusted)
        score1 = sum(value * raw for value, raw in zip(adjusted, scores, strict=True))
        objective = sum(
            label * (a + b * raw) - math.log1p(math.exp(min(700.0, a + b * raw)))
            for label, raw in zip(labels, scores, strict=True)
        ) + 0.5 * math.log(determinant)
        return _LogisticState(score0, score1, i00, inv00, inv01, inv11, objective)

    for _ in range(100):
        score0, score1, i00, inv00, inv01, inv11, objective = state(intercept, slope)
        delta_a = inv00 * score0 + inv01 * score1
        delta_b = inv01 * score0 + inv11 * score1
        if slope == 0 and delta_b <= 0:
            # At the nonnegative-slope boundary, a negative score direction
            # must not influence the intercept update through the inverse
            # information's off-diagonal term.
            delta_a = score0 / i00
            delta_b = 0.0
        scale = 1.0
        while scale >= 1e-8:
            next_a = intercept + scale * delta_a
            next_b = max(0.0, slope + scale * delta_b)
            try:
                next_objective = state(next_a, next_b).objective
            except ValueError:
                scale /= 2.0
                continue
            if next_objective >= objective - 1e-12:
                break
            scale /= 2.0
        if scale < 1e-8:
            if max(abs(delta_a), abs(delta_b)) < 1e-9:
                break
            raise ValueError("CALIBRATION_FIT_DID_NOT_CONVERGE")
        intercept, slope = next_a, next_b
        if max(abs(scale * delta_a), abs(scale * delta_b)) < 1e-9:
            break
    else:
        raise ValueError("CALIBRATION_FIT_DID_NOT_CONVERGE")
    if not math.isfinite(intercept) or not math.isfinite(slope) or slope < 0:
        raise ValueError("CALIBRATION_FIT_FAILED")
    return intercept, slope


def _sigmoid(value: float) -> float:
    if value >= 0:
        inverse = math.exp(-min(value, 745.0))
        return 1.0 / (1.0 + inverse)
    exponent = math.exp(max(value, -745.0))
    return exponent / (1.0 + exponent)


def _probability(calibration: CalibrationSnapshot, score: Decimal) -> Decimal:
    """Return an open-interval probability that can be reused in future calibration."""
    with localcontext() as context:
        context.prec = 32
        value = calibration.intercept + calibration.slope * score
        if value >= 0:
            inverse_odds = (-value).exp()
            probability = Decimal(1) / (Decimal(1) + inverse_odds)
        else:
            odds = value.exp()
            probability = odds / (Decimal(1) + odds)
        return min(
            max(probability, _MINIMUM_REUSABLE_PROBABILITY),
            _MAXIMUM_REUSABLE_PROBABILITY,
        )
