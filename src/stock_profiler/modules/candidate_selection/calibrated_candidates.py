"""Frozen, deterministic candidate calibration and release contracts."""

from __future__ import annotations

import math
from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Literal, NamedTuple

from pydantic import AwareDatetime, Field, model_validator

from stock_profiler.modules.candidate_selection.universe import UniverseContract
from stock_profiler.modules.portfolio.market_calendar import synthetic_market_calendar

CALIBRATOR_VERSION: Literal["monotone-firth-logistic-v1"] = "monotone-firth-logistic-v1"
PROBABILITY_THRESHOLD = Decimal("0.80")
_MINIMUM_MATURE_MONTHS = 60
CandidateReleaseDisposition = Literal[
    "CANDIDATES", "VALID_NO_CANDIDATES", "RECOMMENDATION_ABSTAINED", "FAILED", "BLOCKED"
]
CandidateAvailabilityFailure = Literal["DATA", "CALIBRATION", "VERSION"]


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
    terminal_success: bool
    entry_at: AwareDatetime | None = None
    entry_window_ends_at: AwareDatetime
    unified_maturity_at: AwareDatetime
    label_available_at: AwareDatetime


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


class CandidateInput(UniverseContract):
    """Frozen member evidence handed from research and independent risk."""

    security_id: str = Field(min_length=1)
    research_id: str = Field(min_length=1)
    raw_success_score: Decimal
    data_complete: bool
    risk_status: Literal["ACCEPTED", "REJECTED", "FAILED"]
    thesis: str = Field(min_length=1)
    principal_risks: tuple[str, ...] = Field(min_length=1)
    evidence_freshness: str = Field(min_length=1)


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
    training_window_months: tuple[str, ...]
    training_records: tuple[CalibrationRecord, ...]
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
    market_state_qualified: bool
    data_complete: bool
    thesis: str
    principal_risks: tuple[str, ...]
    evidence_freshness: str
    valid_market_dates: tuple[date, ...]
    reasons: tuple[str, ...]


class CalibrationSnapshot(UniverseContract):
    calibrator_version: Literal["monotone-firth-logistic-v1"]
    intercept: Decimal
    slope: Decimal
    training_window_months: tuple[str, ...]
    label_watermark_at: AwareDatetime
    training_record_count: int
    positive_record_count: int
    negative_record_count: int


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
) -> CandidateReleaseOutcome:
    """Calibrate each frozen member, combine independent gates, and fix its only window."""
    publication_time = published_at or command.published_at
    command = command.model_copy(update={"published_at": publication_time})
    window, window_failure = _candidate_window(command)
    window_dates = tuple(session.market_date for session in window)
    calendar_failure = _candidate_window_failure(command, window, window_failure)
    if calendar_failure is not None:
        return calendar_failure
    if command.calibrator_version != CALIBRATOR_VERSION:
        return _release_outcome(
            command,
            window_dates,
            disposition="FAILED",
            reasons=("CANDIDATE_CALIBRATOR_VERSION_UNSUPPORTED",),
            availability_failure="VERSION",
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
    state_qualified = qualification is not None and qualification.status in {"VALID", "AT_RISK"}
    try:
        calibration = _fit_calibrator(command)
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
                market_state_qualified=state_qualified,
                data_complete=candidate.data_complete,
                thesis=candidate.thesis,
                principal_risks=candidate.principal_risks,
                evidence_freshness=candidate.evidence_freshness,
                valid_market_dates=window_dates,
                reasons=tuple(reasons) or ("ALL_CANDIDATE_GATES_PASSED",),
            )
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
        historical_members = _exclude_candidates(tuple(members), (late_reason,))
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
) -> CandidateReleaseOutcome:
    """Freeze a non-actionable availability result when host-side version checks fail."""
    command = command.model_copy(update={"published_at": published_at})
    window, window_failure = _candidate_window(command)
    calendar_failure = _candidate_window_failure(command, window, window_failure)
    if calendar_failure is not None:
        return calendar_failure
    return _release_outcome(
        command,
        tuple(session.market_date for session in window),
        disposition="FAILED",
        reasons=(reason,),
        availability_failure=availability_failure,
    )


def candidate_release_blocked_by_business_prerequisite(
    command: CandidateReleaseCommand,
    *,
    published_at: datetime,
    reason: str,
) -> CandidateReleaseOutcome:
    """Record that an unsuccessful earlier business outcome prevented release."""
    command = command.model_copy(update={"published_at": published_at})
    window, _ = _candidate_window(command)
    return _release_outcome(
        command,
        tuple(session.market_date for session in window),
        disposition="BLOCKED",
        reasons=(reason,),
    )


def finalize_candidate_release_publication(
    command: CandidateReleaseCommand,
    outcome: CandidateReleaseOutcome,
    *,
    published_at: datetime,
) -> CandidateReleaseOutcome:
    """Fail an otherwise valid batch if its durable publication time misses the window."""
    window, _ = _candidate_window(command)
    if (
        outcome.disposition in {"CANDIDATES", "VALID_NO_CANDIDATES", "RECOMMENDATION_ABSTAINED"}
        and len(window) == 5
        and published_at > window[-1].closes_at
    ):
        late_reason = "PUBLICATION_AFTER_CANDIDATE_WINDOW"
        members = _exclude_candidates(outcome.members, (late_reason,))
        return outcome.model_copy(
            update={
                "published_at": published_at,
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
    published = outcome.model_copy(update={"published_at": published_at})
    if published.disposition not in {
        "CANDIDATES",
        "VALID_NO_CANDIDATES",
        "RECOMMENDATION_ABSTAINED",
    }:
        return published

    qualification = _matching_qualification(
        command,
        published_at,
    )
    qualification_current = (
        qualification is not None
        and qualification.status in {"VALID", "AT_RISK"}
        and qualification.valid_through >= published_at
    )
    if qualification_current or published.disposition == "RECOMMENDATION_ABSTAINED":
        return published

    expired = qualification is not None and qualification.valid_through < published_at
    added_reasons: tuple[str, ...] = ("MARKET_STATE_NOT_QUALIFIED",)
    if expired:
        added_reasons = (*added_reasons, "MARKET_STATE_QUALIFICATION_EXPIRED")
    members = _exclude_candidates(
        published.members,
        added_reasons,
        market_state_qualified=False,
        remove_all_passed_reason=True,
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
    if any(
        right.session_sequence != left.session_sequence + 1
        for left, right in zip(window, window[1:], strict=False)
    ):
        return window, "CANDIDATE_WINDOW_CALENDAR_SEQUENCE_INVALID"
    calendar = synthetic_market_calendar(command.market_calendar_version)
    if calendar is None:
        return window, "CANDIDATE_WINDOW_CALENDAR_SNAPSHOT_MISMATCH"
    saved_window = tuple(
        session
        for session in calendar.sessions
        if session.closed_at - timedelta(hours=7) > command.knowledge_cutoff
    )[:5]
    if len(saved_window) != 5 or any(
        (saved := calendar.session_for(session.session_sequence)) is None
        or saved.closed_at.date() != session.market_date
        or saved.closed_at - timedelta(hours=7) != session.opens_at
        or saved.closed_at != session.closes_at
        for session in window
    ):
        return window, "CANDIDATE_WINDOW_CALENDAR_SNAPSHOT_MISMATCH"
    if any(
        session.session_sequence != saved.ordinal
        or session.market_date != saved.closed_at.date()
        or session.opens_at != saved.closed_at - timedelta(hours=7)
        or session.closes_at != saved.closed_at
        for session, saved in zip(window, saved_window, strict=True)
    ):
        return window, "CANDIDATE_WINDOW_CALENDAR_SNAPSHOT_MISMATCH"
    completed_sessions = tuple(
        session for session in calendar.sessions if session.closed_at <= command.knowledge_cutoff
    )
    last_completed_sequence = (
        max(session.ordinal for session in completed_sessions)
        if completed_sessions
        else saved_window[0].ordinal - 1
    )
    if last_completed_sequence != command.last_completed_market_session_sequence:
        return window, "CANDIDATE_WINDOW_CALENDAR_SEQUENCE_INVALID"
    return window, None


def _exclude_candidates(
    members: tuple[CalibratedMember, ...],
    reasons: tuple[str, ...],
    *,
    market_state_qualified: bool | None = None,
    remove_all_passed_reason: bool = False,
) -> tuple[CalibratedMember, ...]:
    """Remove candidate eligibility while retaining the original score and other evidence."""
    excluded: list[CalibratedMember] = []
    for member in members:
        member_reasons = (
            tuple(reason for reason in member.reasons if reason != "ALL_CANDIDATE_GATES_PASSED")
            if remove_all_passed_reason
            else member.reasons
        )
        updates: dict[str, object] = {
            "candidate": False,
            "reasons": tuple(dict.fromkeys((*member_reasons, *reasons))),
        }
        if market_state_qualified is not None:
            updates["market_state_qualified"] = market_state_qualified
        excluded.append(member.model_copy(update=updates))
    return tuple(excluded)


def _candidate_window_failure(
    command: CandidateReleaseCommand,
    window: tuple[MarketSession, ...],
    failure: str | None,
) -> CandidateReleaseOutcome | None:
    if failure is None:
        return None
    return _release_outcome(
        command,
        tuple(session.market_date for session in window),
        disposition="FAILED",
        reasons=(failure,),
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
    valid_monthly = disposition in {
        "CANDIDATES",
        "VALID_NO_CANDIDATES",
        "RECOMMENDATION_ABSTAINED",
    }
    if members is None:
        qualification = _matching_qualification(command, command.published_at)
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
                data_complete=candidate.data_complete,
                thesis=candidate.thesis,
                principal_risks=candidate.principal_risks,
                evidence_freshness=candidate.evidence_freshness,
                valid_market_dates=valid_market_dates,
                reasons=unavailable_reasons,
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


def _fit_calibrator(command: CandidateReleaseCommand) -> CalibrationSnapshot:
    if command.calibrator_version != CALIBRATOR_VERSION:
        raise ValueError("CALIBRATOR_VERSION_UNSUPPORTED")
    months = command.training_window_months
    if len(months) != _MINIMUM_MATURE_MONTHS:
        raise ValueError("CALIBRATION_REQUIRES_60_MATURE_MONTHS")
    by_month: dict[str, list[CalibrationRecord]] = {}
    for record in command.training_records:
        if record.raw_score_frozen_at.strftime("%Y-%m") != record.month:
            raise ValueError("CALIBRATION_RAW_SCORE_MONTH_MISMATCH")
        if record.raw_score_training_watermark_at > record.raw_score_frozen_at:
            raise ValueError("CALIBRATION_RAW_SCORE_NOT_OUT_OF_SAMPLE")
        if record.entry_window_ends_at <= record.raw_score_frozen_at:
            raise ValueError("CALIBRATION_ENTRY_WINDOW_NOT_AFTER_PREDICTION")
        if record.entry_at is None:
            if record.terminal_success:
                raise ValueError("CALIBRATION_ENTRY_INVALID_CANNOT_SUCCEED")
        elif (
            record.entry_at < record.raw_score_frozen_at
            or record.entry_at >= record.entry_window_ends_at
        ):
            raise ValueError("CALIBRATION_EXECUTABLE_ENTRY_OUTSIDE_ENTRY_WINDOW")
        if record.unified_maturity_at < _six_month_anniversary(record.entry_window_ends_at):
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
    if not _are_consecutive_months(months):
        raise ValueError("CALIBRATION_TRAINING_WINDOW_NOT_CONSECUTIVE")
    records = tuple(record for month in months for record in by_month[month])
    positives = sum(record.terminal_success for record in records)
    negatives = len(records) - positives
    try:
        intercept, slope = _firth_logistic(records)
    except (ArithmeticError, OverflowError, ValueError, ZeroDivisionError) as error:
        raise ValueError("CALIBRATION_FIT_FAILED") from error
    return CalibrationSnapshot(
        calibrator_version=command.calibrator_version,
        intercept=Decimal(str(intercept)).quantize(Decimal("0.00000001")),
        slope=Decimal(str(slope)).quantize(Decimal("0.00000001")),
        training_window_months=months,
        label_watermark_at=command.label_watermark_at,
        training_record_count=len(records),
        positive_record_count=positives,
        negative_record_count=negatives,
    )


def _are_consecutive_months(months: tuple[str, ...]) -> bool:
    month_indices = tuple(int(month[:4]) * 12 + int(month[5:]) - 1 for month in months)
    return all(
        month_indices[index + 1] == month_indices[index] + 1
        for index in range(len(month_indices) - 1)
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
    with localcontext() as context:
        context.prec = 32
        value = calibration.intercept + calibration.slope * score
        if value >= 0:
            inverse_odds = (-value).exp()
            return Decimal(1) / (Decimal(1) + inverse_odds)
        odds = value.exp()
        return odds / (Decimal(1) + odds)


def _six_month_anniversary(value: datetime) -> datetime:
    utc_value = value.astimezone(UTC)
    month_index = utc_value.year * 12 + utc_value.month - 1 + 6
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    return utc_value.replace(
        year=year,
        month=month,
        day=min(utc_value.day, monthrange(year, month)[1]),
    )
