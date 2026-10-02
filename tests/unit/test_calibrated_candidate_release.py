import math
import random
from calendar import monthrange
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Literal, cast

import pytest

import stock_profiler.modules.candidate_selection.calibrated_candidates as candidate_module
from stock_profiler.modules.candidate_selection.calibrated_candidates import (
    CalibrationRecord,
    CandidateInput,
    CandidateReleaseCommand,
    CandidateRiskGate,
    MarketSession,
    MarketStateQualification,
    candidate_release_availability_failure,
    finalize_candidate_release_publication,
    freeze_candidate_release,
)
from stock_profiler.modules.decision_cases import service as decision_case_service
from stock_profiler.modules.decision_cases.domain import DecisionEventFact
from stock_profiler.modules.decision_cases.ports import (
    CandidateAvailabilityFailureFact,
    ResearchAvailabilityFailureFact,
)
from stock_profiler.modules.portfolio.market_calendar import (
    six_month_terminal_evaluation_at,
    synthetic_market_calendar,
)
from stock_profiler.modules.qualification.contracts import GovernanceOutcome
from stock_profiler.modules.research.contracts import (
    RawScoreTrainingCohort,
    RawScoreTrainingRecord,
    _raw_score_evaluation_entry_at,
    frozen_raw_score_model_snapshot,
    raw_score_entry_window_end,
    raw_score_maturity_at,
)


def _calibration_record(
    month: str,
    member: int,
    *,
    record_prefix: str,
    research_prefix: str,
    security_prefix: str,
    source_prefix: str,
    raw_score_frozen_at: datetime,
    entry_window_ends_at: datetime,
    terminal_success: bool,
) -> CalibrationRecord:
    return CalibrationRecord(
        record_id=f"{record_prefix}-label-{month}-{member:02d}",
        month=month,
        source_research_event_id=f"{source_prefix}-research-event-{month}",
        security_id=f"{security_prefix}-{member:02d}",
        research_id=f"{research_prefix}-{month}-{member:02d}",
        raw_score_model_version="synthetic-elastic-net-v1",
        raw_score_frozen_at=raw_score_frozen_at,
        raw_score_training_watermark_at=raw_score_frozen_at - timedelta(days=1),
        raw_success_score=Decimal(member) / Decimal("10"),
        out_of_sample_probability=(
            Decimal("0.1") + Decimal(member) / Decimal("10") * Decimal("0.8")
        ),
        terminal_success=terminal_success,
        entry_window_ends_at=entry_window_ends_at,
        entry_at=entry_window_ends_at - timedelta(days=1),
        label_available_at=six_month_terminal_evaluation_at(
            entry_window_ends_at - timedelta(days=1), "synthetic-calendar-v1"
        ),
        unified_maturity_at=six_month_terminal_evaluation_at(
            entry_window_ends_at - timedelta(days=1), "synthetic-calendar-v1"
        ),
        market_calendar_version="synthetic-calendar-v1",
    )


def training_records() -> tuple[CalibrationRecord, ...]:
    records = calibration_history_records()
    months = tuple(dict.fromkeys(record.month for record in records))[24:84]
    return tuple(record for record in records if record.month in months)


def calibration_history_records() -> tuple[CalibrationRecord, ...]:
    records: list[CalibrationRecord] = []
    months = tuple(f"{year}-{month:02d}" for year in range(2036, 2046) for month in range(1, 13))[
        8:119
    ]
    for month in months:
        year, month_number = (int(part) for part in month.split("-"))
        month_end = datetime(year, month_number, monthrange(year, month_number)[1], 8, tzinfo=UTC)
        raw_score_frozen_at = month_end - timedelta(hours=1)
        entry_window_ends_at = month_end + timedelta(days=5)
        for member in range(10):
            records.append(
                _calibration_record(
                    month,
                    member,
                    record_prefix="synthetic",
                    source_prefix="synthetic",
                    security_prefix="SYNTH-SECURITY",
                    research_prefix="synthetic-research",
                    raw_score_frozen_at=raw_score_frozen_at,
                    entry_window_ends_at=entry_window_ends_at,
                    terminal_success=member >= 5,
                )
            )
    return tuple(records)


def weak_positive_signal_records() -> tuple[CalibrationRecord, ...]:
    generator = random.Random(10417)
    threshold = generator.random()
    strength = generator.uniform(-3, 3)
    records: list[CalibrationRecord] = []
    months = tuple(f"{year}-{month:02d}" for year in range(2036, 2046) for month in range(1, 13))[
        8:119
    ]
    for month in months:
        year, month_number = (int(part) for part in month.split("-"))
        entry_window_ends_at = datetime(
            year, month_number, monthrange(year, month_number)[1], 8, tzinfo=UTC
        ) + timedelta(days=5)
        raw_score_frozen_at = datetime(
            year,
            month_number,
            monthrange(year, month_number)[1],
            7,
            tzinfo=UTC,
        )
        for member in range(10):
            score = Decimal(member) / Decimal("10")
            probability = 1 / (1 + math.exp(-strength * (float(score) - threshold)))
            records.append(
                _calibration_record(
                    month,
                    member,
                    record_prefix="positive",
                    source_prefix="synthetic-positive",
                    security_prefix="SYNTH-POSITIVE",
                    research_prefix="synthetic-positive-research",
                    raw_score_frozen_at=raw_score_frozen_at,
                    entry_window_ends_at=entry_window_ends_at,
                    terminal_success=generator.random() < probability,
                )
            )
    fitting_months = set(months[24:84])
    return tuple(record for record in records if record.month in fitting_months)


def _six_month_anniversary(value: datetime) -> datetime:
    month_index = value.year * 12 + value.month - 1 + 6
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    return value.replace(
        year=year,
        month=month,
        day=min(value.day, monthrange(year, month)[1]),
    )


def _month_after(month: str, count: int = 1) -> str:
    year, month_number = (int(part) for part in month.split("-"))
    month_index = year * 12 + month_number - 1 + count
    next_year, zero_based_month = divmod(month_index, 12)
    return f"{next_year:04}-{zero_based_month + 1:02}"


def command(
    *,
    score: str = "0.95",
    state_status: Literal["NOT_OBTAINED", "VALID", "AT_RISK", "SUSPENDED", "REVOKED"] = "VALID",
    published: datetime | None = None,
) -> CandidateReleaseCommand:
    cutoff = datetime(2046, 7, 1, 8, tzinfo=UTC)
    market_calendar_version = "synthetic-calendar-v1"
    calendar = synthetic_market_calendar(market_calendar_version)
    assert calendar is not None
    last_completed_session = calendar.session_at_or_before(cutoff)
    assert last_completed_session is not None
    sessions = tuple(
        MarketSession(
            market_date=datetime(2046, 7, day, tzinfo=UTC).date(),
            opens_at=datetime(2046, 7, day, 1, tzinfo=UTC),
            closes_at=datetime(2046, 7, day, 8, tzinfo=UTC),
            session_sequence=99 + day,
        )
        for day in range(2, 7)
    )
    records = training_records()
    months = tuple(dict.fromkeys(record.month for record in records))
    history = calibration_history_records()
    history_months = tuple(dict.fromkeys(record.month for record in history))
    selection_months = history_months[:24]
    recent_months = history_months[-24:]
    selection_records = tuple(record for record in history if record.month in selection_months)
    recent_diagnostic_records = tuple(record for record in history if record.month in recent_months)
    return CandidateReleaseCommand(
        contract_version="1.0.0",
        synthetic=True,
        generator_version="candidate-release-v1",
        seed=17017,
        batch_id="synthetic-candidate-batch-001",
        qualification_scope="D0_SYNTHETIC_CONTRACT_ONLY",
        capability_version="candidate-v1",
        research_object_id="synthetic-research-object-001",
        research_event_id="synthetic-research-event-001",
        purpose="CANDIDATE_BUY",
        knowledge_cutoff=cutoff,
        published_at=published or datetime(2046, 7, 1, 9, tzinfo=UTC),
        last_completed_market_session_sequence=last_completed_session.ordinal,
        market_state="BULL",
        market_calendar_version=market_calendar_version,
        qualifications=(
            MarketStateQualification(
                market_state="BULL",
                status=state_status,
                qualification_id="synthetic-bull-qualification",
                capability_version="candidate-v1",
                market_calendar_version="synthetic-calendar-v1",
                recorded_at=datetime(2046, 1, 1, tzinfo=UTC),
                valid_through=datetime(2046, 12, 1, tzinfo=UTC),
            ),
        ),
        label_watermark_at=cutoff,
        calibrator_selection_window_months=selection_months,
        calibrator_selection_records=selection_records,
        training_window_months=months,
        training_records=records,
        recent_diagnostic_window_months=recent_months,
        recent_diagnostic_records=recent_diagnostic_records,
        candidates=(
            CandidateInput(
                security_id="SYNTH-ALPHA",
                research_id="synthetic-research-alpha",
                raw_success_score=Decimal(score),
                data_complete=True,
                risk_status="ACCEPTED",
                risk_gates=(CandidateRiskGate(gate_id="RISK_REVIEW", status="PASSED"),),
                risk_reasons=("INDEPENDENT_RISK_ACCEPTED",),
                thesis="Original synthetic investment thesis.",
                principal_risks=("Original synthetic risk.",),
                evidence_freshness="FRESH_AT_CUTOFF",
            ),
        ),
        # The five-session window is anchored to the frozen cutoff.
        # Publication time cannot move these dates.
        market_sessions=sessions,
    )


def test_freezes_calibration_probabilities_and_five_market_day_candidate_window() -> None:
    original = command()
    release = freeze_candidate_release(original)

    assert release.disposition == "CANDIDATES", release.reasons
    assert release.calibration is not None
    assert release.calibration.calibrator_selection.policy == "PRE_REGISTERED_V1_SINGLE_FAMILY"
    assert (
        release.calibration.calibrator_selection.selected_calibrator_version
        == "monotone-firth-logistic-v1"
    )
    assert release.calibration.calibrator_selection.record_count == len(
        command().calibrator_selection_records
    )
    assert release.calibration.calibrator_selection.diagnostics.log_loss.is_finite()
    assert release.calibration.label_watermark_at == datetime(2046, 7, 1, 8, tzinfo=UTC)
    assert release.calibration.training_record_count == 600
    assert release.calibration.out_of_sample_diagnostics.log_loss.is_finite()
    assert release.calibration.out_of_sample_diagnostics.brier_score.is_finite()
    assert release.calibration.out_of_sample_diagnostics.reliability_curve
    assert release.calibration.calibrator_selection_window_months == (
        original.calibrator_selection_window_months
    )
    assert release.calibration.recent_diagnostic_months == tuple(
        dict.fromkeys(record.month for record in original.recent_diagnostic_records)
    )
    selection_ids = {record.record_id for record in original.calibrator_selection_records}
    training_ids = {record.record_id for record in original.training_records}
    diagnostic_ids = {record.record_id for record in original.recent_diagnostic_records}
    assert not selection_ids & training_ids
    assert not training_ids & diagnostic_ids
    assert not selection_ids & diagnostic_ids
    assert release.calibration.recent_diagnostic_sample_count >= 200
    assert release.calibration.recent_diagnostic_status == "AVAILABLE"
    assert release.calibration.recent_diagnostics is not None
    assert release.calibration.recent_diagnostics.log_loss.is_finite()
    assert release.members[0].calibrated_probability is not None
    assert release.members[0].valid_market_dates == (
        datetime(2046, 7, 2, 1, tzinfo=UTC).date(),
        datetime(2046, 7, 3, 1, tzinfo=UTC).date(),
        datetime(2046, 7, 4, 1, tzinfo=UTC).date(),
        datetime(2046, 7, 5, 1, tzinfo=UTC).date(),
        datetime(2046, 7, 6, 1, tzinfo=UTC).date(),
    )


def test_calibrator_selection_window_has_no_unregistered_24_month_floor() -> None:
    original = command()
    selection_months = tuple(
        dict.fromkeys(record.month for record in original.calibrator_selection_records)
    )[:12]
    selection_records = tuple(
        record
        for record in original.calibrator_selection_records
        if record.month in set(selection_months)
    )
    shorter_selection = original.model_copy(
        update={
            "calibrator_selection_window_months": selection_months,
            "calibrator_selection_records": selection_records,
        }
    )

    release = freeze_candidate_release(shorter_selection)

    assert release.disposition == "CANDIDATES", release.reasons
    assert release.calibration is not None
    assert release.calibration.calibrator_selection_window_months == selection_months


def test_recent_diagnostic_window_cannot_overlap_the_fixed_fit_window() -> None:
    original = command()
    history = calibration_history_records()
    history_months = tuple(dict.fromkeys(record.month for record in history))
    fit_months = history_months[-60:]
    overlapping = original.model_copy(
        update={
            "training_window_months": fit_months,
            "training_records": tuple(record for record in history if record.month in fit_months),
        }
    )
    release = freeze_candidate_release(overlapping, initial_calibration=False)

    assert release.disposition == "FAILED"
    assert release.availability_failure == "CALIBRATION"
    assert release.reasons == ("CALIBRATION_WINDOWS_MUST_BE_DISJOINT",)


def test_calibrator_selection_records_cannot_be_reused_for_fitting() -> None:
    original = command()
    fitting_months = original.training_window_months[:24]
    reused_records = tuple(
        record.model_copy(update={"month": fitting_months[index // 10]})
        for index, record in enumerate(original.calibrator_selection_records)
    )
    overlapping = original.model_copy(
        update={
            "calibrator_selection_window_months": fitting_months,
            "calibrator_selection_records": reused_records,
        }
    )

    release = freeze_candidate_release(overlapping)

    assert release.disposition == "FAILED"
    assert release.availability_failure == "CALIBRATION"
    assert release.reasons == ("CALIBRATION_WINDOWS_MUST_BE_DISJOINT",)


def test_candidate_window_includes_terminal_sessions_before_saved_coverage() -> None:
    original = command()
    cutoff = datetime(2046, 6, 28, 8, tzinfo=UTC)
    calendar = synthetic_market_calendar(original.market_calendar_version)
    assert calendar is not None
    first_saved_date = calendar.sessions[0].closed_at.date()
    terminal_prefix = tuple(
        session
        for session in calendar.terminal_sessions
        if session.closed_at.date() < first_saved_date
    )
    last_completed_session = calendar.session_at_or_before(cutoff)
    assert last_completed_session is not None
    assert last_completed_session.ordinal == 1_002_475
    resolved_sessions = tuple(
        sorted((*terminal_prefix, *calendar.sessions), key=lambda session: session.closed_at)
    )
    expected_sessions = tuple(
        MarketSession(
            market_date=session.closed_at.date(),
            opens_at=session.closed_at - timedelta(hours=7),
            closes_at=session.closed_at,
            session_sequence=session.ordinal,
        )
        for session in resolved_sessions
        if session.closed_at - timedelta(hours=7) > cutoff
    )[:5]
    command_with_earlier_cutoff = original.model_copy(
        update={
            "knowledge_cutoff": cutoff,
            "label_watermark_at": cutoff,
            "last_completed_market_session_sequence": last_completed_session.ordinal,
            "market_sessions": expected_sessions,
        }
    )

    release = freeze_candidate_release(command_with_earlier_cutoff)
    late_publication = finalize_candidate_release_publication(
        command_with_earlier_cutoff,
        release,
        published_at=datetime(2046, 7, 6, 7, tzinfo=UTC),
    )

    assert release.disposition == "CANDIDATES", release.reasons
    assert release.valid_market_dates == tuple(session.market_date for session in expected_sessions)
    assert late_publication.disposition == "FAILED"
    assert late_publication.reasons == ("PUBLICATION_AFTER_CANDIDATE_WINDOW",)


def test_candidate_window_uses_terminal_sessions_after_saved_coverage() -> None:
    original = command()
    cutoff = datetime(2046, 8, 1, 8, tzinfo=UTC)
    calendar = synthetic_market_calendar(original.market_calendar_version)
    assert calendar is not None
    expected_sessions = tuple(
        MarketSession(
            market_date=session.closed_at.date(),
            opens_at=session.closed_at - timedelta(hours=7),
            closes_at=session.closed_at,
            session_sequence=session.ordinal,
        )
        for session in calendar.sessions_after_open(cutoff, 5)
    )
    last_completed_session = calendar.session_at_or_before(cutoff)
    assert last_completed_session is not None
    command_after_coverage = original.model_copy(
        update={
            "knowledge_cutoff": cutoff,
            "published_at": cutoff + timedelta(hours=1),
            "label_watermark_at": cutoff,
            "last_completed_market_session_sequence": last_completed_session.ordinal,
            "market_sessions": expected_sessions,
        }
    )

    release = freeze_candidate_release(command_after_coverage)

    assert release.disposition == "CANDIDATES", release.reasons
    assert release.valid_market_dates == tuple(session.market_date for session in expected_sessions)


def test_candidate_window_rejects_sessions_after_terminal_calendar_coverage() -> None:
    original = command()
    cutoff = datetime(2050, 12, 31, 9, tzinfo=UTC)
    submitted_sessions = tuple(
        MarketSession(
            market_date=datetime(2051, 1, day, tzinfo=UTC).date(),
            opens_at=datetime(2051, 1, day, 1, tzinfo=UTC),
            closes_at=datetime(2051, 1, day, 8, tzinfo=UTC),
            session_sequence=2_000_000 + day,
        )
        for day in range(3, 8)
    )
    outside_coverage = original.model_copy(
        update={
            "knowledge_cutoff": cutoff,
            "published_at": cutoff + timedelta(hours=1),
            "market_sessions": submitted_sessions,
        }
    )

    selected_window, failure = candidate_module._candidate_window(outside_coverage)

    assert selected_window == submitted_sessions
    assert failure == "CANDIDATE_WINDOW_CALENDAR_SNAPSHOT_MISMATCH"


def test_recent_diagnostic_failure_does_not_fail_production_calibration() -> None:
    original = command()
    recent_months = set(original.recent_diagnostic_window_months)
    recent_index = 0
    records: list[CalibrationRecord] = []
    for record in original.recent_diagnostic_records:
        if record.month in recent_months:
            probability = Decimal("0.5") if recent_index % 2 == 0 else Decimal("0.50000000000001")
            records.append(record.model_copy(update={"out_of_sample_probability": probability}))
            recent_index += 1
        else:
            records.append(record)

    release = freeze_candidate_release(
        original.model_copy(update={"recent_diagnostic_records": tuple(records)})
    )

    assert release.disposition == "CANDIDATES", release.reasons
    assert release.availability_failure is None
    assert release.calibration is not None
    assert release.calibration.recent_diagnostic_sample_count >= 200
    assert release.calibration.recent_diagnostic_status == "AVAILABLE"
    assert release.calibration.recent_diagnostics is not None
    assert release.calibration.recent_diagnostics.recalibration_fit_status == "CALCULATION_FAILED"
    assert release.calibration.recent_diagnostics.log_loss.is_finite()


def test_frozen_calibration_reports_missing_probability_availability_failures() -> None:
    original = command()

    release = freeze_candidate_release(original, unavailable_probability_count=3)

    assert release.calibration is not None
    assert release.calibration.unavailable_probability_count == 3


def test_incomplete_recent_diagnostic_cohort_is_visible_without_blocking_release() -> None:
    original = command()
    recent_months = set(original.recent_diagnostic_window_months)
    sparse_diagnostics = (
        tuple(
            record
            for record in original.recent_diagnostic_records
            if record.month not in recent_months
        )
        + tuple(
            record for record in original.recent_diagnostic_records if record.month in recent_months
        )[:80]
    )

    release = freeze_candidate_release(
        original.model_copy(update={"recent_diagnostic_records": sparse_diagnostics})
    )

    assert release.disposition == "CANDIDATES"
    assert release.calibration is not None
    assert release.calibration.recent_diagnostic_sample_count == 80
    assert release.calibration.recent_diagnostic_status == "INSUFFICIENT_DATA"
    assert release.calibration.recent_diagnostics is None


def test_duplicate_recent_diagnostic_record_is_visible_without_blocking_release() -> None:
    original = command()
    duplicate = original.recent_diagnostic_records[0]

    release = freeze_candidate_release(
        original.model_copy(
            update={"recent_diagnostic_records": (*original.recent_diagnostic_records, duplicate)}
        )
    )

    assert release.disposition == "CANDIDATES", release.reasons
    assert release.calibration is not None
    assert release.calibration.recent_diagnostic_status == "CALCULATION_FAILED"
    assert release.calibration.recent_diagnostic_sample_count == len(
        original.recent_diagnostic_records
    )
    assert release.calibration.recent_diagnostics is None


def test_duplicate_recent_diagnostic_sample_identity_is_not_counted_twice() -> None:
    original = command()
    duplicate = original.recent_diagnostic_records[0].model_copy(
        update={"record_id": "synthetic-duplicate-diagnostic-row-id"}
    )

    release = freeze_candidate_release(
        original.model_copy(
            update={"recent_diagnostic_records": (*original.recent_diagnostic_records, duplicate)}
        )
    )

    assert release.disposition == "CANDIDATES"
    assert release.calibration is not None
    assert release.calibration.recent_diagnostic_status == "CALCULATION_FAILED"
    assert release.calibration.recent_diagnostic_sample_count == len(
        original.recent_diagnostic_records
    )
    assert release.calibration.recent_diagnostics is None


def test_duplicate_outside_recent_diagnostic_window_does_not_change_diagnostics() -> None:
    original = command()
    older_record = next(
        record
        for record in original.training_records
        if record.month not in original.recent_diagnostic_window_months
    )

    release = freeze_candidate_release(
        original.model_copy(
            update={
                "recent_diagnostic_records": (
                    *original.recent_diagnostic_records,
                    older_record,
                    older_record,
                )
            }
        )
    )

    assert release.disposition == "CANDIDATES"
    assert release.calibration is not None
    assert release.calibration.recent_diagnostic_status == "AVAILABLE"
    assert release.calibration.recent_diagnostic_sample_count == len(
        original.recent_diagnostic_records
    )


def test_recent_diagnostic_cohort_drops_labels_after_the_frozen_watermark() -> None:
    original = command()
    after_watermark = original.recent_diagnostic_records[0].model_copy(
        update={
            "record_id": "synthetic-after-watermark-diagnostic",
            "label_available_at": original.label_watermark_at + timedelta(seconds=1),
        }
    )

    calibration = candidate_module._fit_calibrator(
        original,
        recent_diagnostic_records=(*original.recent_diagnostic_records, after_watermark),
    )

    assert calibration.recent_diagnostic_status == "AVAILABLE"
    assert calibration.recent_diagnostic_sample_count == 240


def test_recent_diagnostic_calculation_failure_is_visible_but_does_not_fail_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = command()
    diagnostics = candidate_module._calibration_diagnostics
    calls = 0

    def fail_recent_diagnostics(
        records: tuple[CalibrationRecord, ...], probabilities: tuple[float, ...]
    ) -> candidate_module.CalibrationDiagnostics:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise ValueError("synthetic recent diagnostic calculation failure")
        return diagnostics(records, probabilities)

    monkeypatch.setattr(candidate_module, "_calibration_diagnostics", fail_recent_diagnostics)

    calibration = candidate_module._fit_calibrator(original)

    assert calibration.recent_diagnostic_status == "CALCULATION_FAILED"
    assert calibration.recent_diagnostics is None


def test_calibration_diagnostics_reject_mismatched_records_and_probabilities() -> None:
    with pytest.raises(ValueError, match="CALIBRATION_DIAGNOSTICS_REQUIRE_MATCHED_RECORDS"):
        candidate_module._calibration_diagnostics((), (0.5,))


def test_calibration_diagnostics_use_constant_logit_fallback() -> None:
    records = command().training_records[:10]

    diagnostics = candidate_module._calibration_diagnostics(records, (0.5,) * len(records))

    assert diagnostics.recalibration_fit_status == "AVAILABLE"
    assert diagnostics.calibration_slope == Decimal("0.00000000")
    assert diagnostics.calibration_intercept is not None


def test_calibration_diagnostics_report_negative_slope_for_inverted_predictions() -> None:
    original = command()
    records = tuple(
        record.model_copy(update={"terminal_success": record.raw_success_score < Decimal("0.5")})
        for record in original.training_records
    )
    probabilities = tuple(
        candidate_module._sigmoid(2.0 * float(record.raw_success_score) - 1.0) for record in records
    )

    diagnostics = candidate_module._calibration_diagnostics(records, probabilities)

    assert diagnostics.recalibration_fit_status == "AVAILABLE"
    assert diagnostics.calibration_slope is not None
    assert diagnostics.calibration_slope < Decimal("0")


def test_duplicate_authoritative_research_snapshot_preserves_diagnostic_row_identity() -> None:
    record = command().recent_diagnostic_records[0]
    authoritative = record.model_copy(update={"source_research_event_id": "research-event-a"})
    duplicate_snapshot = record.model_copy(update={"source_research_event_id": "research-event-b"})

    assert decision_case_service._matches_authoritative_diagnostic_record(
        duplicate_snapshot,
        authoritative,
        {"research-event-a", "research-event-b"},
    )
    assert not decision_case_service._matches_authoritative_diagnostic_record(
        duplicate_snapshot,
        authoritative,
        {"research-event-a"},
    )


def test_prior_frozen_predictions_remain_in_scope_after_capability_version_change() -> None:
    original = command()
    prior_command = original.model_copy(update={"capability_version": "previous-capability-v0"})
    prior_event = cast(
        DecisionEventFact,
        SimpleNamespace(
            result=SimpleNamespace(candidate_release=freeze_candidate_release(original)),
            case=SimpleNamespace(
                candidate_release=prior_command,
                knowledge_cutoff=original.knowledge_cutoff.isoformat(),
            ),
            committed_at=original.knowledge_cutoff.isoformat(),
            corrects_event_id=None,
            validation_status="PASSED",
        ),
    )

    assert decision_case_service._prior_candidate_prediction_snapshot_matches(
        prior_event,
        original,
    )
    assert not decision_case_service._prior_calibration_snapshot_matches(prior_event, original)


@pytest.mark.parametrize(
    ("invalid_case", "expected_error"),
    (
        ("selection_window_mismatch", "CALIBRATOR_SELECTION_WINDOW_MISMATCH"),
        ("empty_selection", "CALIBRATOR_SELECTION_REQUIRES_MATURE_MONTHS"),
        ("diagnostic_month_count", "RECENT_DIAGNOSTIC_REQUIRES_24_MATURE_MONTHS"),
        ("selection_not_earlier", "CALIBRATOR_SELECTION_WINDOW_NOT_EARLIER"),
        ("diagnostics_not_latest", "RECENT_DIAGNOSTIC_WINDOW_NOT_LATEST"),
        ("duplicate_selection_identity", "CALIBRATOR_SELECTION_RECORD_IDENTITIES_NOT_UNIQUE"),
        ("selection_label_not_mature", "CALIBRATOR_SELECTION_LABEL_NOT_MATURE"),
    ),
)
def test_calibrator_rejects_invalid_selection_or_diagnostic_provenance(
    invalid_case: str,
    expected_error: str,
) -> None:
    original = command()
    update: dict[str, object] = {}
    if invalid_case == "selection_window_mismatch":
        update["calibrator_selection_window_months"] = ()
    elif invalid_case == "empty_selection":
        update.update(
            calibrator_selection_window_months=(),
            calibrator_selection_records=(),
        )
    elif invalid_case == "diagnostic_month_count":
        update["recent_diagnostic_window_months"] = original.recent_diagnostic_window_months[:-1]
    elif invalid_case == "selection_not_earlier":
        later_month = _month_after(original.training_window_months[-1])
        selection_record = original.calibrator_selection_records[0].model_copy(
            update={"month": later_month, "record_id": "synthetic-late-selection"}
        )
        update.update(
            calibrator_selection_window_months=(later_month,),
            calibrator_selection_records=(selection_record,),
            recent_diagnostic_window_months=tuple(
                _month_after(later_month, offset) for offset in range(1, 25)
            ),
        )
    elif invalid_case == "diagnostics_not_latest":
        history = calibration_history_records()
        history_months = tuple(dict.fromkeys(record.month for record in history))
        fitting_months = history_months[-60:]
        diagnostic_months = history_months[24:48]
        update.update(
            training_window_months=fitting_months,
            training_records=tuple(record for record in history if record.month in fitting_months),
            recent_diagnostic_window_months=diagnostic_months,
            recent_diagnostic_records=tuple(
                record for record in history if record.month in diagnostic_months
            ),
        )
    elif invalid_case == "duplicate_selection_identity":
        update["calibrator_selection_records"] = (
            *original.calibrator_selection_records,
            original.calibrator_selection_records[0],
        )
    elif invalid_case == "selection_label_not_mature":
        late_label = original.calibrator_selection_records[0].model_copy(
            update={"label_available_at": original.label_watermark_at + timedelta(seconds=1)}
        )
        update["calibrator_selection_records"] = (
            late_label,
            *original.calibrator_selection_records[1:],
        )

    with pytest.raises(ValueError, match=expected_error):
        candidate_module._fit_calibrator(original.model_copy(update=update))


def test_full_window_diagnostic_fit_failure_does_not_fail_production_calibration() -> None:
    original = command()
    records = tuple(
        record.model_copy(
            update={
                "out_of_sample_probability": (
                    Decimal("0.5") if index % 2 == 0 else Decimal("0.50000000000001")
                )
            }
        )
        for index, record in enumerate(original.training_records)
    )

    release = freeze_candidate_release(original.model_copy(update={"training_records": records}))

    assert release.disposition == "CANDIDATES", release.reasons
    assert release.availability_failure is None
    assert release.calibration is not None
    diagnostics = release.calibration.out_of_sample_diagnostics
    assert diagnostics.log_loss.is_finite()
    assert diagnostics.brier_score.is_finite()
    assert diagnostics.recalibration_fit_status == "CALCULATION_FAILED"
    assert diagnostics.calibration_intercept is None
    assert diagnostics.calibration_slope is None


def test_log_loss_uses_frozen_out_of_sample_probabilities_not_the_current_fit() -> None:
    original = command()
    baseline = freeze_candidate_release(original)
    assert baseline.calibration is not None
    changed_scores = tuple(
        record.model_copy(update={"raw_success_score": record.raw_success_score + Decimal("10")})
        for record in original.training_records
    )
    shifted = freeze_candidate_release(
        original.model_copy(update={"training_records": changed_scores})
    )

    assert shifted.calibration is not None
    assert shifted.calibration.intercept != baseline.calibration.intercept
    assert (
        shifted.calibration.out_of_sample_diagnostics.log_loss
        == baseline.calibration.out_of_sample_diagnostics.log_loss
    )


@pytest.mark.parametrize("published_at_override", [False, True])
def test_publication_before_knowledge_cutoff_fails_without_calibrating(
    published_at_override: bool,
) -> None:
    publication_time = datetime(2046, 7, 1, 7, tzinfo=UTC)
    candidate_command = command(published=None if published_at_override else publication_time)

    release = freeze_candidate_release(
        candidate_command,
        published_at=publication_time if published_at_override else None,
    )

    assert release.disposition == "FAILED"
    assert release.availability_failure == "DATA"
    assert release.calibration is None
    assert release.reasons == ("CANDIDATE_KNOWLEDGE_CUTOFF_AFTER_PUBLICATION",)


def test_intraday_cutoff_starts_at_the_next_purchasable_session() -> None:
    original = command()
    cutoff = datetime(2046, 7, 3, 2, tzinfo=UTC)
    calendar = synthetic_market_calendar(original.market_calendar_version)
    assert calendar is not None
    sessions = tuple(
        MarketSession(
            market_date=session.closed_at.date(),
            opens_at=session.closed_at - timedelta(hours=7),
            closes_at=session.closed_at,
            session_sequence=session.ordinal,
        )
        for session in calendar.sessions
        if session.closed_at - timedelta(hours=7) > cutoff
    )[:5]

    release = freeze_candidate_release(
        original.model_copy(
            update={
                "knowledge_cutoff": cutoff,
                "published_at": cutoff + timedelta(minutes=1),
                "last_completed_market_session_sequence": 101,
                "market_sessions": sessions,
            }
        )
    )

    assert release.disposition == "CANDIDATES"
    assert release.valid_market_dates == tuple(session.market_date for session in sessions)


def test_probability_below_threshold_forms_valid_no_candidate() -> None:
    release = freeze_candidate_release(command(score="0.1"))

    assert release.disposition == "VALID_NO_CANDIDATES"
    assert release.members[0].candidate is False
    assert "PROBABILITY_BELOW_THRESHOLD" in release.members[0].reasons


def test_inverse_signal_is_fitted_at_nonnegative_slope_boundary() -> None:
    original = command()
    inverse_signal = tuple(
        record.model_copy(update={"terminal_success": int(record.record_id[-2:]) < 8})
        for record in original.training_records
    )

    release = freeze_candidate_release(
        original.model_copy(update={"training_records": inverse_signal})
    )

    assert release.disposition == "VALID_NO_CANDIDATES"
    assert release.calibration is not None
    assert release.calibration.slope == Decimal("0E-8")
    assert release.members[0].calibrated_probability is not None
    assert release.members[0].calibrated_probability < Decimal("0.80")


def test_candidate_freezes_the_probability_used_by_the_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = command()
    monkeypatch.setattr(
        candidate_module,
        "_probability",
        lambda _calibration, _score: Decimal("0.799999999"),
    )

    release = freeze_candidate_release(original)

    assert release.members[0].calibrated_probability == Decimal("0.799999999")
    assert release.members[0].candidate is False
    assert "PROBABILITY_BELOW_THRESHOLD" in release.members[0].reasons


def test_extreme_negative_finite_score_yields_a_calibrated_non_candidate() -> None:
    release = freeze_candidate_release(command(score="-100000"))

    assert release.disposition == "VALID_NO_CANDIDATES"
    probability = release.members[0].calibrated_probability
    assert probability is not None
    assert Decimal("0") < probability < Decimal("1")
    assert release.members[0].candidate is False


def test_extreme_positive_finite_score_yields_a_reusable_calibrated_probability() -> None:
    release = freeze_candidate_release(command(score="100000"))

    probability = release.members[0].calibrated_probability
    assert release.disposition == "CANDIDATES"
    assert probability is not None
    assert Decimal("0") < probability < Decimal("1")


def test_probability_arithmetic_overflow_is_a_visible_calibration_failure() -> None:
    release = freeze_candidate_release(command(score="1e999999"))

    assert release.disposition == "FAILED"
    assert release.availability_failure == "CALIBRATION"
    assert "CALIBRATION_PROBABILITY_FAILED" in release.reasons


def test_weak_positive_signal_converges_without_crossing_slope_boundary() -> None:
    original = command()
    release = freeze_candidate_release(
        original.model_copy(update={"training_records": weak_positive_signal_records()})
    )

    assert release.calibration is not None
    assert release.calibration.slope > 0


def test_unqualified_market_state_abstains_without_lowering_probability_gate() -> None:
    release = freeze_candidate_release(command(state_status="NOT_OBTAINED"))

    assert release.disposition == "RECOMMENDATION_ABSTAINED"
    assert release.members[0].candidate is False
    assert "MARKET_STATE_NOT_QUALIFIED" in release.members[0].reasons


def test_at_risk_qualification_remains_effective_while_within_validity() -> None:
    release = freeze_candidate_release(command(state_status="AT_RISK"))

    assert release.disposition == "CANDIDATES"
    assert release.members[0].market_state_qualified is True
    assert release.members[0].candidate is True
    assert release.members[0].market_state_qualification_status == "AT_RISK"
    assert "MARKET_STATE_QUALIFICATION_AT_RISK" in release.members[0].reasons


def test_final_publication_freezes_a_new_at_risk_qualification_revision() -> None:
    original = command()
    frozen = freeze_candidate_release(original)
    assert frozen.members[0].market_state_qualification_status == "VALID"
    final_time = original.published_at + timedelta(minutes=1)
    at_risk = original.qualifications[0].model_copy(
        update={
            "status": "AT_RISK",
            "current_status_recorded_at": final_time - timedelta(seconds=1),
        }
    )

    published = finalize_candidate_release_publication(
        original.model_copy(update={"qualifications": (at_risk,)}),
        frozen,
        published_at=final_time,
    )

    assert published.disposition == "CANDIDATES"
    assert published.members[0].candidate is True
    assert published.members[0].market_state_qualified is True
    assert published.members[0].market_state_qualification_status == "AT_RISK"
    assert "ALL_CANDIDATE_GATES_PASSED" not in published.members[0].reasons
    assert "MARKET_STATE_QUALIFICATION_AT_RISK" in published.members[0].reasons
    assert published.qualification == at_risk


def test_same_timestamp_revocation_supersedes_the_frozen_qualification_revision() -> None:
    original = command()
    first_snapshot = freeze_candidate_release(original)
    final_time = original.published_at + timedelta(minutes=1)
    at_risk = original.qualifications[0].model_copy(
        update={
            "qualification_id": "synthetic-qualification-at-risk",
            "status": "AT_RISK",
            "current_status_recorded_at": final_time,
        }
    )
    published = finalize_candidate_release_publication(
        original.model_copy(update={"qualifications": (at_risk,)}),
        first_snapshot,
        published_at=final_time,
    )
    revoked = at_risk.model_copy(
        update={
            "qualification_id": "synthetic-qualification-revoked",
            "status": "REVOKED",
            "current_status_recorded_at": final_time,
        }
    )

    revalidated = finalize_candidate_release_publication(
        original.model_copy(update={"qualifications": (revoked,)}),
        published,
        published_at=final_time,
    )

    assert revalidated.disposition == "RECOMMENDATION_ABSTAINED"
    assert revalidated.qualification == revoked
    assert revalidated.members[0].candidate is False


def test_same_timestamp_qualification_expiry_supersedes_the_frozen_record() -> None:
    original = command()
    final_time = original.published_at + timedelta(minutes=1)
    frozen_command = original.model_copy(update={"published_at": final_time})
    first_snapshot = freeze_candidate_release(frozen_command)
    expired = original.qualifications[0].model_copy(
        update={"valid_through": final_time - timedelta(seconds=1)}
    )

    revalidated = finalize_candidate_release_publication(
        frozen_command.model_copy(update={"qualifications": (expired,)}),
        first_snapshot,
        published_at=final_time,
    )

    assert revalidated.disposition == "RECOMMENDATION_ABSTAINED"
    assert revalidated.qualification == expired
    assert revalidated.members[0].candidate is False


def test_post_cutoff_qualification_cannot_authorize_candidates() -> None:
    original = command(state_status="AT_RISK")
    qualification = original.qualifications[0].model_copy(
        update={"recorded_at": original.knowledge_cutoff + timedelta(minutes=2)}
    )

    release = freeze_candidate_release(
        original.model_copy(update={"qualifications": (qualification,)})
    )

    assert release.disposition == "RECOMMENDATION_ABSTAINED"
    assert release.members[0].market_state_qualified is False
    assert release.members[0].market_state_qualification_status == "NOT_QUALIFIED"


def test_final_publication_turns_unqualified_no_candidate_batch_into_abstention() -> None:
    original = command(score="0.01")
    frozen = freeze_candidate_release(original, published_at=original.published_at)
    assert frozen.disposition == "VALID_NO_CANDIDATES"
    revoked = original.qualifications[0].model_copy(update={"status": "REVOKED"})

    finalized = finalize_candidate_release_publication(
        original.model_copy(update={"qualifications": (revoked,)}),
        frozen,
        published_at=original.published_at + timedelta(hours=1),
    )

    assert finalized.disposition == "RECOMMENDATION_ABSTAINED"
    assert finalized.members[0].market_state_qualified is False
    assert finalized.members[0].market_state_qualification_status == "NOT_QUALIFIED"
    assert "MARKET_STATE_NOT_QUALIFIED" in finalized.reasons
    assert "MARKET_STATE_QUALIFICATION_EXPIRED" not in finalized.reasons


def test_final_abstention_keeps_its_frozen_unqualified_status_on_replay() -> None:
    original = command()
    frozen = freeze_candidate_release(original)
    final_time = original.published_at + timedelta(minutes=1)
    expired = original.qualifications[0].model_copy(
        update={"valid_through": final_time - timedelta(seconds=1)}
    )
    abstained = finalize_candidate_release_publication(
        original.model_copy(update={"qualifications": (expired,)}),
        frozen,
        published_at=final_time,
    )

    replayed = finalize_candidate_release_publication(
        original,
        abstained,
        published_at=final_time,
    )

    assert abstained.disposition == "RECOMMENDATION_ABSTAINED"
    assert abstained.members[0].market_state_qualification_status == "NOT_QUALIFIED"
    assert replayed == abstained


def test_risk_veto_remains_independent_of_research_probability() -> None:
    rejected = (
        command()
        .candidates[0]
        .model_copy(
            update={
                "risk_status": "REJECTED",
                "risk_gates": (CandidateRiskGate(gate_id="LIQUIDITY", status="FAILED"),),
                "risk_reasons": ("LIQUIDITY_BELOW_MINIMUM",),
            }
        )
    )
    candidate = command().model_copy(update={"candidates": (rejected,)})

    release = freeze_candidate_release(candidate)

    assert release.disposition == "VALID_NO_CANDIDATES"
    assert "INDEPENDENT_RISK_VETO" in release.members[0].reasons
    assert release.members[0].risk_gates == rejected.risk_gates
    assert release.members[0].risk_reasons == ("LIQUIDITY_BELOW_MINIMUM",)


def test_incomplete_candidate_data_fails_the_whole_release() -> None:
    candidate = command()
    incomplete = candidate.candidates[0].model_copy(update={"data_complete": False})
    candidate = candidate.model_copy(update={"candidates": (incomplete,)})

    release = freeze_candidate_release(candidate)

    assert release.disposition == "FAILED"
    assert release.availability_failure == "DATA"
    assert len(release.members) == 1
    assert release.members[0].calibrated_probability is None
    assert release.members[0].thesis == incomplete.thesis
    assert release.members[0].principal_risks == incomplete.principal_risks
    assert release.members[0].evidence_freshness == incomplete.evidence_freshness
    assert release.members[0].risk_status == incomplete.risk_status
    assert "CANDIDATE_DATA_INCOMPLETE" in release.members[0].reasons


def test_fewer_than_five_post_cutoff_sessions_fail_without_moving_window() -> None:
    candidate = command()
    before_cutoff_sessions = tuple(
        MarketSession(
            market_date=datetime(2046, 6, day, tzinfo=UTC).date(),
            opens_at=datetime(2046, 6, day, 1, tzinfo=UTC),
            closes_at=datetime(2046, 6, day, 8, tzinfo=UTC),
            session_sequence=80 + day,
        )
        for day in range(1, 6)
    )
    candidate = candidate.model_copy(update={"market_sessions": before_cutoff_sessions})

    release = freeze_candidate_release(candidate)

    assert release.disposition == "FAILED"
    assert release.availability_failure == "DATA"
    assert release.valid_market_dates == ()


def test_invalid_session_clocks_are_rejected() -> None:
    with pytest.raises(ValueError, match="match its open and close clocks"):
        MarketSession(
            market_date=datetime(2046, 7, 2, tzinfo=UTC).date(),
            opens_at=datetime(2046, 7, 1, 1, tzinfo=UTC),
            closes_at=datetime(2046, 7, 2, 8, tzinfo=UTC),
            session_sequence=101,
        )


def test_incomplete_market_session_sequence_fails_without_moving_window() -> None:
    original = command()
    skipped_session = original.market_sessions[1].model_copy(update={"session_sequence": 103})
    sessions = (*original.market_sessions[:1], skipped_session, *original.market_sessions[2:])

    release = freeze_candidate_release(original.model_copy(update={"market_sessions": sessions}))

    assert release.disposition == "FAILED"
    assert release.availability_failure == "DATA"
    assert release.reasons == ("CANDIDATE_WINDOW_CALENDAR_SEQUENCE_INVALID",)
    with pytest.raises(ValueError, match="must close after it opens"):
        MarketSession(
            market_date=datetime(2046, 7, 2, tzinfo=UTC).date(),
            opens_at=datetime(2046, 7, 2, 8, tzinfo=UTC),
            closes_at=datetime(2046, 7, 2, 8, tzinfo=UTC),
            session_sequence=101,
        )


def test_shifted_sessions_do_not_match_the_saved_market_calendar() -> None:
    original = command()
    sessions = tuple(
        session.model_copy(
            update={
                "market_date": session.market_date + timedelta(days=30),
                "opens_at": session.opens_at + timedelta(days=30),
                "closes_at": session.closes_at + timedelta(days=30),
            }
        )
        for session in original.market_sessions
    )

    release = freeze_candidate_release(original.model_copy(update={"market_sessions": sessions}))

    assert release.disposition == "FAILED"
    assert release.availability_failure == "DATA"
    assert release.reasons == ("CANDIDATE_WINDOW_CALENDAR_SNAPSHOT_MISMATCH",)


def test_unknown_market_calendar_version_fails_closed() -> None:
    original = command().model_copy(
        update={"market_calendar_version": "unregistered-market-calendar-v1"}
    )

    release = freeze_candidate_release(original)

    assert release.disposition == "FAILED"
    assert release.availability_failure == "DATA"
    assert release.reasons == ("CANDIDATE_WINDOW_CALENDAR_SNAPSHOT_MISMATCH",)


def test_candidate_window_cannot_start_after_the_first_saved_post_cutoff_session() -> None:
    original = command()
    later_sessions = tuple(
        session.model_copy(
            update={
                "market_date": session.market_date + timedelta(days=7),
                "opens_at": session.opens_at + timedelta(days=7),
                "closes_at": session.closes_at + timedelta(days=7),
                "session_sequence": session.session_sequence + 5,
            }
        )
        for session in original.market_sessions
    )
    later_command = original.model_copy(
        update={
            "market_sessions": later_sessions,
            "last_completed_market_session_sequence": 105,
        }
    )

    release = freeze_candidate_release(later_command)

    assert release.disposition == "FAILED"
    assert release.availability_failure == "DATA"
    assert release.reasons == ("CANDIDATE_WINDOW_CALENDAR_SNAPSHOT_MISMATCH",)


def test_candidate_window_rejects_mismatched_last_completed_session_anchor() -> None:
    original = command()
    incorrect_anchor = original.model_copy(update={"last_completed_market_session_sequence": 99})

    release = freeze_candidate_release(incorrect_anchor)

    assert release.disposition == "FAILED"
    assert release.availability_failure == "DATA"
    assert release.reasons == ("CANDIDATE_WINDOW_CALENDAR_SEQUENCE_INVALID",)


def test_unavailable_calibration_inputs_are_saved_as_visible_failures() -> None:
    original = command()
    unsupported = CandidateReleaseCommand.model_validate(
        original.model_dump(mode="python") | {"calibrator_version": "unknown-calibrator-v2"}
    )
    missing_records = CandidateReleaseCommand.model_validate(
        original.model_dump(mode="python") | {"training_records": ()}
    )
    missing_window = CandidateReleaseCommand.model_validate(
        original.model_dump(mode="python") | {"training_window_months": ()}
    )
    missing_sessions = CandidateReleaseCommand.model_validate(
        original.model_dump(mode="python") | {"market_sessions": ()}
    )

    version_failure = freeze_candidate_release(unsupported)
    data_failure = freeze_candidate_release(missing_records)
    window_failure = freeze_candidate_release(missing_window)
    calendar_failure = freeze_candidate_release(missing_sessions)

    assert version_failure.disposition == "FAILED"
    assert version_failure.availability_failure == "CALIBRATION"
    assert version_failure.reasons == ("CANDIDATE_CALIBRATOR_VERSION_UNSUPPORTED",)
    for failed_release in (version_failure, data_failure, window_failure, calendar_failure):
        assert len(failed_release.members) == len(original.candidates)
        assert failed_release.members[0].calibrated_probability is None
        assert failed_release.members[0].thesis == original.candidates[0].thesis
    with pytest.raises(ValueError, match="CALIBRATOR_VERSION_UNSUPPORTED"):
        candidate_module._fit_calibrator(unsupported)
    assert data_failure.disposition == "FAILED"
    assert data_failure.availability_failure == "CALIBRATION"
    assert data_failure.calibration is None
    assert window_failure.disposition == "FAILED"
    assert window_failure.availability_failure == "CALIBRATION"
    assert window_failure.calibration is None
    assert calendar_failure.disposition == "FAILED"
    assert calendar_failure.availability_failure == "DATA"
    assert calendar_failure.reasons == ("CANDIDATE_WINDOW_CALENDAR_INCOMPLETE",)


def test_host_availability_failure_preserves_calendar_failures() -> None:
    original = command()
    incomplete = candidate_release_availability_failure(
        original.model_copy(update={"market_sessions": original.market_sessions[:4]}),
        published_at=original.published_at,
        reason="CANDIDATE_QUALIFICATION_VERSION_MISMATCH",
        availability_failure="DATA",
    )
    invalid_sequence = candidate_release_availability_failure(
        original.model_copy(
            update={
                "market_sessions": (
                    original.market_sessions[0],
                    original.market_sessions[1].model_copy(update={"session_sequence": 105}),
                    *original.market_sessions[2:],
                )
            }
        ),
        published_at=original.published_at,
        reason="CANDIDATE_QUALIFICATION_VERSION_MISMATCH",
        availability_failure="DATA",
    )

    assert incomplete.availability_failure == "DATA"
    assert incomplete.reasons == ("CANDIDATE_WINDOW_CALENDAR_INCOMPLETE",)
    assert invalid_sequence.availability_failure == "DATA"
    assert invalid_sequence.reasons == ("CANDIDATE_WINDOW_CALENDAR_SEQUENCE_INVALID",)


def test_unvalidated_candidate_failure_omits_candidate_member_details() -> None:
    original = command()
    failed_command = original.model_copy(update={"market_sessions": original.market_sessions[:4]})

    unavailable = candidate_release_availability_failure(
        original,
        published_at=original.published_at,
        reason="CANDIDATE_RESEARCH_HANDOFF_UNAVAILABLE",
        availability_failure="DATA",
        include_member_details=False,
    )
    unavailable_with_calendar_failure = candidate_release_availability_failure(
        failed_command,
        published_at=original.published_at,
        reason="CANDIDATE_RESEARCH_HANDOFF_UNAVAILABLE",
        availability_failure="DATA",
        include_member_details=False,
    )

    assert unavailable.members == ()
    assert unavailable_with_calendar_failure.members == ()


def test_entry_invalid_record_is_a_mature_negative_calibration_label() -> None:
    original = command()
    records = tuple(
        record.model_copy(
            update={
                "entry_at": None,
                "terminal_success": False,
                "unified_maturity_at": six_month_terminal_evaluation_at(
                    record.entry_window_ends_at, record.market_calendar_version
                ),
                "label_available_at": six_month_terminal_evaluation_at(
                    record.entry_window_ends_at, record.market_calendar_version
                ),
            }
        )
        if record.record_id.endswith("-00")
        else record
        for record in original.training_records
    )

    release = freeze_candidate_release(original.model_copy(update={"training_records": records}))

    assert release.disposition == "CANDIDATES"
    assert release.calibration is not None
    assert release.calibration.training_record_count == len(records)
    assert release.calibration.negative_record_count == 300

    invalid_success = original.training_records[0].model_copy(
        update={"entry_at": None, "terminal_success": True}
    )
    failed = freeze_candidate_release(
        original.model_copy(
            update={"training_records": (invalid_success, *original.training_records[1:])}
        )
    )
    assert failed.availability_failure == "CALIBRATION"
    assert failed.reasons == ("CALIBRATION_ENTRY_INVALID_CANNOT_SUCCEED",)


def test_release_command_rejects_inconsistent_frozen_inputs() -> None:
    original = command()
    with pytest.raises(ValueError, match="cannot precede its authorization"):
        MarketStateQualification(
            market_state="BULL",
            status="AT_RISK",
            qualification_id="synthetic-bull-qualification",
            capability_version="candidate-v1",
            market_calendar_version="synthetic-calendar-v1",
            recorded_at=datetime(2046, 1, 2, tzinfo=UTC),
            current_status_recorded_at=datetime(2046, 1, 1, tzinfo=UTC),
            valid_through=datetime(2046, 12, 1, tzinfo=UTC),
        )

    payload = original.model_dump(mode="python")
    payload["label_watermark_at"] = datetime(2046, 7, 2, tzinfo=UTC)
    with pytest.raises(ValueError, match="label watermark"):
        CandidateReleaseCommand.model_validate(payload)

    payload = original.model_dump(mode="python")
    payload["training_window_months"] = (
        *original.training_window_months[:-1],
        original.training_window_months[-2],
    )
    with pytest.raises(ValueError, match="training months must be unique"):
        CandidateReleaseCommand.model_validate(payload)

    payload = original.model_dump(mode="python")
    payload["candidates"] = (*original.candidates, original.candidates[0])
    with pytest.raises(ValueError, match="unique securities"):
        CandidateReleaseCommand.model_validate(payload)

    payload = original.model_dump(mode="python")
    duplicate_research = original.candidates[0].model_copy(update={"security_id": "SYNTH-BETA"})
    payload["candidates"] = (original.candidates[0], duplicate_research)
    with pytest.raises(ValueError, match="unique research identities"):
        CandidateReleaseCommand.model_validate(payload)

    payload = original.model_dump(mode="python")
    payload["market_sessions"] = tuple(reversed(original.market_sessions))
    with pytest.raises(ValueError, match="chronological order"):
        CandidateReleaseCommand.model_validate(payload)

    payload = original.model_dump(mode="python")
    duplicate_date = original.market_sessions[-1].model_copy(
        update={
            "market_date": original.market_sessions[-2].market_date,
            "opens_at": original.market_sessions[-2].opens_at,
            "closes_at": original.market_sessions[-2].closes_at,
        }
    )
    payload["market_sessions"] = (*original.market_sessions[:-1], duplicate_date)
    with pytest.raises(ValueError, match="market session dates must be unique"):
        CandidateReleaseCommand.model_validate(payload)


def test_calibration_rejects_invalid_population_and_labels() -> None:
    original = command()

    window_without_future_entry = original.training_records[0].model_copy(
        update={
            "entry_window_ends_at": original.training_records[0].raw_score_frozen_at
            - timedelta(seconds=1)
        }
    )
    release = freeze_candidate_release(
        original.model_copy(
            update={
                "training_records": (window_without_future_entry, *original.training_records[1:])
            }
        )
    )
    assert "CALIBRATION_ENTRY_WINDOW_NOT_AFTER_PREDICTION" in release.reasons

    entry_outside_window = original.training_records[0].model_copy(
        update={
            "entry_at": original.training_records[0].entry_window_ends_at,
            "unified_maturity_at": six_month_terminal_evaluation_at(
                original.training_records[0].entry_window_ends_at,
                original.training_records[0].market_calendar_version,
            ),
            "label_available_at": six_month_terminal_evaluation_at(
                original.training_records[0].entry_window_ends_at,
                original.training_records[0].market_calendar_version,
            ),
        }
    )
    release = freeze_candidate_release(
        original.model_copy(
            update={"training_records": (entry_outside_window, *original.training_records[1:])}
        )
    )
    assert release.disposition == "CANDIDATES"

    intramonth_mature_entry = original.training_records[0].model_copy(
        update={
            "month": "2040-12",
            "raw_score_frozen_at": datetime(2040, 12, 14, 7, tzinfo=UTC),
            "raw_score_training_watermark_at": datetime(2040, 12, 13, 7, tzinfo=UTC),
            "entry_window_ends_at": datetime(2040, 12, 21, 8, tzinfo=UTC),
            "entry_at": datetime(2040, 12, 17, 7, tzinfo=UTC),
            "unified_maturity_at": six_month_terminal_evaluation_at(
                datetime(2040, 12, 17, 7, tzinfo=UTC), "synthetic-calendar-v1"
            ),
            "label_available_at": six_month_terminal_evaluation_at(
                datetime(2040, 12, 17, 7, tzinfo=UTC), "synthetic-calendar-v1"
            ),
        }
    )
    release = freeze_candidate_release(
        original.model_copy(
            update={
                "training_records": (
                    intramonth_mature_entry,
                    *original.training_records[1:],
                )
            }
        )
    )
    assert release.availability_failure is None
    assert release.calibration is not None

    outside_month = "2043-11"
    outside_month_records = tuple(
        record.model_copy(
            update={
                "record_id": f"synthetic-label-{outside_month}-{index:02d}",
                "month": outside_month,
                "source_research_event_id": f"synthetic-research-event-{outside_month}",
                "raw_score_frozen_at": datetime(2043, 11, 30, 7, tzinfo=UTC),
                "raw_score_training_watermark_at": datetime(2043, 11, 29, 7, tzinfo=UTC),
                "entry_window_ends_at": datetime(2043, 12, 6, 8, tzinfo=UTC),
                "entry_at": datetime(2043, 12, 5, 8, tzinfo=UTC),
                "unified_maturity_at": six_month_terminal_evaluation_at(
                    datetime(2043, 12, 5, 8, tzinfo=UTC), "synthetic-calendar-v1"
                ),
                "label_available_at": six_month_terminal_evaluation_at(
                    datetime(2043, 12, 5, 8, tzinfo=UTC), "synthetic-calendar-v1"
                ),
            }
        )
        for index, record in enumerate(original.training_records[:10])
    )
    release = freeze_candidate_release(
        original.model_copy(
            update={
                "training_window_months": (outside_month, *original.training_window_months[:-1]),
                "training_records": (*outside_month_records, *original.training_records),
            }
        ),
        initial_calibration=False,
    )
    assert release.disposition == "FAILED"
    assert release.availability_failure == "CALIBRATION"
    assert release.reasons == ("CALIBRATION_TRAINING_WINDOW_NOT_LATEST",)

    immature = original.training_records[0].model_copy(
        update={
            "label_available_at": original.training_records[0].unified_maturity_at
            - timedelta(seconds=1)
        }
    )
    release = freeze_candidate_release(
        original.model_copy(update={"training_records": (immature, *original.training_records[1:])})
    )
    assert "LABEL_BEFORE_UNIFIED_SIX_MONTH_MATURITY" in release.reasons

    utc_boundary = next(
        index for index, record in enumerate(original.training_records) if record.month == "2040-08"
    )
    timezone_maturity = original.training_records[utc_boundary].model_copy(
        update={
            "entry_at": datetime.fromisoformat("2045-09-01T06:00:00+00:00"),
            "entry_window_ends_at": datetime.fromisoformat("2045-08-31T23:00:00-08:00"),
            "unified_maturity_at": datetime.fromisoformat("2046-02-28T05:59:59+00:00"),
            "label_available_at": datetime.fromisoformat("2046-02-28T05:59:59+00:00"),
        }
    )
    records_with_timezone_maturity = (
        *original.training_records[:utc_boundary],
        timezone_maturity,
        *original.training_records[utc_boundary + 1 :],
    )
    release = freeze_candidate_release(
        original.model_copy(update={"training_records": records_with_timezone_maturity})
    )
    assert release.disposition == "FAILED"
    assert "LABEL_BEFORE_UNIFIED_SIX_MONTH_MATURITY" in release.reasons

    beyond_watermark = original.training_records[0].model_copy(
        update={
            "unified_maturity_at": datetime(2046, 7, 2, tzinfo=UTC),
            "label_available_at": datetime(2046, 7, 2, tzinfo=UTC),
        }
    )
    release = freeze_candidate_release(
        original.model_copy(
            update={"training_records": (beyond_watermark, *original.training_records[1:])}
        )
    )
    assert "LABEL_BEFORE_UNIFIED_SIX_MONTH_MATURITY" in release.reasons

    bad_entry_record = original.training_records[-1]
    assert bad_entry_record.entry_at is not None
    bad_entry_maturity_at = _six_month_anniversary(bad_entry_record.entry_at) - timedelta(days=1)
    bad_entry_maturity = bad_entry_record.model_copy(
        update={
            "unified_maturity_at": bad_entry_maturity_at,
            "label_available_at": bad_entry_maturity_at,
        }
    )
    release = freeze_candidate_release(
        original.model_copy(
            update={"training_records": (*original.training_records[:-1], bad_entry_maturity)}
        )
    )
    assert "LABEL_BEFORE_UNIFIED_SIX_MONTH_MATURITY" in release.reasons

    incomplete = tuple(record for record in original.training_records if record.month != "2041-01")
    release = freeze_candidate_release(original.model_copy(update={"training_records": incomplete}))
    assert "CALIBRATION_REQUIRES_60_MATURE_MONTHS" in release.reasons

    duplicate = original.training_records[0].model_copy(
        update={"record_id": original.training_records[1].record_id}
    )
    release = freeze_candidate_release(
        original.model_copy(
            update={"training_records": (duplicate, *original.training_records[1:])}
        )
    )
    assert "CALIBRATION_RECORD_IDENTITIES_NOT_UNIQUE" in release.reasons

    too_few = tuple(
        record for record in original.training_records if int(record.record_id[-2:]) < 8
    )
    release = freeze_candidate_release(original.model_copy(update={"training_records": too_few}))
    assert release.disposition == "FAILED"
    assert release.availability_failure == "CALIBRATION"
    assert release.reasons == ("CALIBRATION_REQUIRES_500_MATURE_RECORDS",)

    single_class = tuple(
        record.model_copy(update={"terminal_success": False})
        for record in original.training_records
    )
    release = freeze_candidate_release(
        original.model_copy(update={"training_records": single_class})
    )
    assert release.disposition == "FAILED"
    assert release.availability_failure == "CALIBRATION"
    assert release.reasons == ("CALIBRATION_CLASS_FLOOR_NOT_MET",)


def test_calibrator_requires_at_least_fifty_labels_per_class() -> None:
    original = command()
    records = tuple(
        record.model_copy(update={"terminal_success": index < 49})
        for index, record in enumerate(original.training_records)
    )

    release = freeze_candidate_release(original.model_copy(update={"training_records": records}))

    assert release.disposition == "FAILED"
    assert release.availability_failure == "CALIBRATION"
    assert release.reasons == ("CALIBRATION_CLASS_FLOOR_NOT_MET",)


def test_calibrator_accepts_exactly_fifty_labels_per_class() -> None:
    original = command()
    records = tuple(
        record.model_copy(update={"terminal_success": index < 50})
        for index, record in enumerate(original.training_records)
    )

    release = freeze_candidate_release(original.model_copy(update={"training_records": records}))

    assert release.calibration is not None
    assert release.calibration.positive_record_count == 50
    assert release.calibration.negative_record_count == 550


def test_calibrator_fails_closed_for_constant_or_singular_scores() -> None:
    original = command()
    constant = tuple(
        record.model_copy(update={"raw_success_score": Decimal("0.5")})
        for record in original.training_records
    )
    release = freeze_candidate_release(original.model_copy(update={"training_records": constant}))
    assert "CALIBRATION_FIT_FAILED" in release.reasons

    singular = tuple(
        record.model_copy(
            update={
                "raw_success_score": Decimal("0")
                if index < 300
                else Decimal(
                    "0.0000000000000000000000000000000000000000000000000000000000000000000001"
                )
            }
        )
        for index, record in enumerate(original.training_records)
    )
    release = freeze_candidate_release(original.model_copy(update={"training_records": singular}))
    assert "CALIBRATION_FIT_FAILED" in release.reasons


def test_failed_independent_risk_result_is_not_a_veto_pass() -> None:
    original = command()
    unavailable = original.candidates[0].model_copy(update={"risk_status": "FAILED"})
    release = freeze_candidate_release(original.model_copy(update={"candidates": (unavailable,)}))

    assert release.disposition == "FAILED"
    assert release.availability_failure == "SYSTEM"
    assert release.population is not None
    assert release.population.availability_failure == "SYSTEM"
    assert release.population.valid_monthly is False
    assert all(member.candidate is False for member in release.members)
    assert "INDEPENDENT_RISK_UNAVAILABLE" in release.members[0].reasons


def test_firth_solver_keeps_the_slope_nonnegative_for_inverse_training_signal() -> None:
    inverse = tuple(
        record.model_copy(update={"terminal_success": record.raw_success_score < Decimal("0.5")})
        for record in training_records()
    )

    _, slope = candidate_module._firth_logistic(inverse)

    assert slope >= 0


def test_firth_solver_fails_when_iteration_limit_is_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(candidate_module, "_sigmoid", lambda _: 0.5)
    monkeypatch.setattr(math, "log1p", lambda _: 0.0)

    with pytest.raises(ValueError, match="CALIBRATION_FIT_DID_NOT_CONVERGE"):
        candidate_module._firth_logistic(training_records())


def test_firth_solver_shrinks_rejected_steps_and_stops_at_the_scale_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_log1p = math.log1p

    def reject_nonzero_steps(value: float) -> float:
        if value == 1.0:
            return original_log1p(value)
        raise ValueError("synthetic invalid trial")

    monkeypatch.setattr(math, "log1p", reject_nonzero_steps)

    with pytest.raises(ValueError, match="CALIBRATION_FIT_DID_NOT_CONVERGE"):
        candidate_module._firth_logistic(training_records())


def test_firth_solver_accepts_a_stalled_step_at_the_numerical_tolerance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = tuple(
        record.model_copy(update={"terminal_success": (index // 10) % 2 == 0})
        for index, record in enumerate(training_records())
    )
    original_log1p = math.log1p
    calls = 0

    def degrade_trial_objective(value: float) -> float:
        nonlocal calls
        calls += 1
        return original_log1p(value) if calls <= len(records) else 1000.0

    monkeypatch.setattr(math, "log1p", degrade_trial_objective)

    intercept, slope = candidate_module._firth_logistic(records)

    assert intercept == 0
    assert slope == 0


def test_firth_solver_accepts_score_watermark_equal_to_freeze_time() -> None:
    original = command()
    first = original.training_records[0].model_copy(
        update={"raw_score_training_watermark_at": original.training_records[0].raw_score_frozen_at}
    )

    release = freeze_candidate_release(
        original.model_copy(update={"training_records": (first, *original.training_records[1:])})
    )

    assert release.availability_failure is None
    assert release.calibration is not None


def test_firth_solver_rejects_score_watermark_after_freeze_time() -> None:
    original = command()
    first = original.training_records[0].model_copy(
        update={
            "raw_score_training_watermark_at": original.training_records[0].raw_score_frozen_at
            + timedelta(microseconds=1)
        }
    )

    release = freeze_candidate_release(
        original.model_copy(update={"training_records": (first, *original.training_records[1:])})
    )

    assert release.availability_failure == "CALIBRATION"
    assert release.reasons == ("CALIBRATION_RAW_SCORE_NOT_OUT_OF_SAMPLE",)


def test_firth_solver_rejects_entry_before_prediction_time() -> None:
    original = command()
    first = original.training_records[0].model_copy(
        update={"entry_at": original.training_records[0].raw_score_frozen_at - timedelta(seconds=1)}
    )

    with pytest.raises(ValueError, match="CALIBRATION_EXECUTABLE_ENTRY_OUTSIDE_ENTRY_WINDOW"):
        before_prediction = original.model_copy(
            update={"training_records": (first, *original.training_records[1:])}
        )
        candidate_module._fit_calibrator(before_prediction)


def test_firth_solver_rejects_entry_after_frozen_window() -> None:
    original = command()
    first = original.training_records[0].model_copy(
        update={
            "entry_at": original.training_records[0].entry_window_ends_at + timedelta(seconds=1)
        }
    )

    with pytest.raises(ValueError, match="CALIBRATION_EXECUTABLE_ENTRY_OUTSIDE_ENTRY_WINDOW"):
        after_window = original.model_copy(
            update={"training_records": (first, *original.training_records[1:])}
        )
        candidate_module._fit_calibrator(after_window)


def test_firth_solver_rejects_raw_score_timestamp_from_a_different_month() -> None:
    original = command()
    first = original.training_records[0].model_copy(
        update={"raw_score_frozen_at": datetime(2037, 1, 30, 7, tzinfo=UTC)}
    )

    release = freeze_candidate_release(
        original.model_copy(update={"training_records": (first, *original.training_records[1:])})
    )

    assert release.availability_failure == "CALIBRATION"
    assert release.reasons == ("CALIBRATION_RAW_SCORE_MONTH_MISMATCH",)


def test_firth_solver_uses_shanghai_month_for_equivalent_raw_score_instants() -> None:
    original = command()
    source = original.training_records[0]
    year, month = (int(part) for part in source.month.split("-"))
    last_day = monthrange(year, month)[1]
    utc_month_end = datetime(year, month, last_day, 15, 59, tzinfo=UTC)
    equivalent_next_offset_month = utc_month_end.astimezone(timezone(timedelta(hours=9)))
    assert equivalent_next_offset_month.strftime("%Y-%m") != source.month
    adjusted_record = source.model_copy(
        update={
            "raw_score_frozen_at": equivalent_next_offset_month,
            "raw_score_training_watermark_at": utc_month_end - timedelta(days=1),
        }
    )

    release = freeze_candidate_release(
        original.model_copy(
            update={"training_records": (adjusted_record, *original.training_records[1:])}
        )
    )

    assert release.availability_failure is None
    assert release.calibration is not None


def test_firth_solver_uses_shanghai_month_for_frozen_raw_score_instants() -> None:
    original = command()
    source = original.training_records[0]
    year, month = (int(part) for part in source.month.split("-"))
    shanghai_month_start = datetime(
        year,
        month,
        1,
        0,
        30,
        tzinfo=timezone(timedelta(hours=8)),
    )
    raw_score_frozen_at = shanghai_month_start.astimezone(UTC)
    adjusted_record = source.model_copy(
        update={
            "raw_score_frozen_at": shanghai_month_start,
            "raw_score_training_watermark_at": raw_score_frozen_at - timedelta(days=1),
        }
    )

    release = freeze_candidate_release(
        original.model_copy(
            update={"training_records": (adjusted_record, *original.training_records[1:])}
        )
    )

    assert raw_score_frozen_at.strftime("%Y-%m") != source.month
    assert release.availability_failure is None
    assert release.calibration is not None


def test_firth_solver_rejects_nonfinite_final_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    original_isfinite = math.isfinite
    records = training_records()
    record_count = len(records)
    calls = 0

    def finite_scores_only(value: float) -> bool:
        nonlocal calls
        calls += 1
        if calls <= record_count:
            return original_isfinite(value)
        return False

    monkeypatch.setattr(math, "isfinite", finite_scores_only)
    with pytest.raises(ValueError, match="CALIBRATION_FIT_FAILED"):
        candidate_module._firth_logistic(records)


def test_unmatured_training_label_is_calibration_availability_failure() -> None:
    candidate = command()
    early = candidate.training_records[0].model_copy(
        update={"label_available_at": datetime(2046, 7, 2, tzinfo=UTC)}
    )
    candidate = candidate.model_copy(
        update={"training_records": (early, *candidate.training_records[1:])}
    )

    release = freeze_candidate_release(candidate)

    assert release.disposition == "FAILED"
    assert release.availability_failure == "CALIBRATION"
    assert "CALIBRATION_REQUIRES_60_MATURE_MONTHS" in release.reasons


def test_terminal_session_maturity_does_not_precede_the_label_watermark() -> None:
    original = command()
    target_month = original.training_window_months[-1]
    entry_window_ends_at = datetime(2045, 12, 5, 8, 0, 0, 500_000, tzinfo=UTC)
    maturity = six_month_terminal_evaluation_at(entry_window_ends_at, "synthetic-calendar-v1")
    records = tuple(
        record.model_copy(
            update={
                "entry_window_ends_at": entry_window_ends_at,
                "entry_at": None,
                "terminal_success": False,
                "unified_maturity_at": maturity,
                "label_available_at": maturity,
            }
        )
        if record.month == target_month
        else record
        for record in original.training_records
    )
    early_watermark = maturity - timedelta(milliseconds=250)
    early = freeze_candidate_release(
        original.model_copy(
            update={"training_records": records, "label_watermark_at": early_watermark}
        )
    )
    mature = freeze_candidate_release(
        original.model_copy(update={"training_records": records, "label_watermark_at": maturity})
    )

    assert early.disposition == "FAILED"
    assert "CALIBRATION_REQUIRES_60_MATURE_MONTHS" in early.reasons
    assert mature.disposition == "CANDIDATES"


def test_v1_market_calendar_keeps_its_frozen_session_map() -> None:
    calendar = synthetic_market_calendar("synthetic-market-calendar-v1")
    assert calendar is not None

    assert tuple(session.ordinal for session in calendar.sessions) == tuple(range(6101, 6122))
    assert calendar.sessions[3].closed_at == datetime(2042, 5, 25, 15, tzinfo=UTC)
    assert calendar.sessions[-1].closed_at == datetime(2042, 6, 17, 15, tzinfo=UTC)


def test_v2_market_calendar_extends_candidate_sessions_without_revising_v1() -> None:
    calendar = synthetic_market_calendar("synthetic-market-calendar-v2")
    assert calendar is not None

    assert calendar.sessions[-1].closed_at == datetime(2042, 8, 26, 15, tzinfo=UTC)
    assert calendar.sessions[3].closed_at == datetime(2042, 5, 23, 15, tzinfo=UTC)


def test_terminal_maturity_honors_frozen_v1_sessions_inside_their_coverage() -> None:
    calendar = synthetic_market_calendar("synthetic-market-calendar-v1")
    assert calendar is not None

    maturity = six_month_terminal_evaluation_at(
        datetime(2041, 11, 25, tzinfo=UTC), calendar.version_id
    )

    assert maturity == datetime(2042, 5, 25, 15, tzinfo=UTC)


def test_terminal_maturity_uses_the_frozen_candidate_market_schedule() -> None:
    calendar = synthetic_market_calendar("synthetic-market-calendar-v2")
    assert calendar is not None
    maturity = six_month_terminal_evaluation_at(
        datetime(2042, 1, 6, tzinfo=UTC), calendar.version_id
    )
    expected_maturity = datetime(2042, 7, 4, 15, tzinfo=UTC)

    assert maturity == expected_maturity
    assert any(session.closed_at == expected_maturity for session in calendar.sessions)
    assert any(session.closed_at == expected_maturity for session in calendar.terminal_sessions)


def test_terminal_maturity_does_not_invent_a_weekday_missing_from_candidate_calendar() -> None:
    calendar = synthetic_market_calendar("synthetic-market-calendar-v2")
    assert calendar is not None
    maturity = six_month_terminal_evaluation_at(
        datetime(2041, 11, 25, tzinfo=UTC), calendar.version_id
    )
    final_saved_session = max(
        (
            session
            for session in calendar.sessions
            if session.closed_at.date() <= datetime(2042, 5, 25).date()
        ),
        key=lambda session: session.closed_at,
    )

    assert maturity == final_saved_session.closed_at


@pytest.mark.parametrize(
    "entry_at",
    (
        datetime(2050, 10, 1, 12, tzinfo=UTC),
        datetime(2051, 1, 2, 12, tzinfo=UTC),
    ),
)
def test_terminal_maturity_rejects_targets_beyond_calendar_coverage(
    entry_at: datetime,
) -> None:
    with pytest.raises(ValueError, match="MARKET_CALENDAR_TERMINAL_SESSION_UNAVAILABLE"):
        raw_score_maturity_at(entry_at, "synthetic-calendar-v1")


def test_expired_late_publication_keeps_original_window_and_records_failure() -> None:
    release = freeze_candidate_release(command(published=datetime(2046, 7, 7, 9, tzinfo=UTC)))

    assert release.disposition == "FAILED"
    assert "PUBLICATION_AFTER_CANDIDATE_WINDOW" in release.reasons
    assert release.valid_market_dates[-1].isoformat() == "2046-07-06"
    assert release.population.valid_monthly is False
    assert release.calibration is not None
    assert release.members[0].candidate is False


def test_late_publication_does_not_hide_calibration_availability_failure() -> None:
    original = command(published=datetime(2046, 7, 7, 9, tzinfo=UTC))
    invalid_training = original.model_copy(update={"training_window_months": ("2045-01",)})

    release = freeze_candidate_release(invalid_training)

    assert release.disposition == "FAILED"
    assert release.availability_failure == "CALIBRATION"
    assert release.population.valid_monthly is False
    assert "CALIBRATION_REQUIRES_60_MATURE_MONTHS" in release.reasons


def test_publication_that_crosses_window_while_calibrating_is_failed() -> None:
    original = command()
    frozen = freeze_candidate_release(original, published_at=original.published_at)

    finalized = finalize_candidate_release_publication(
        original,
        frozen,
        published_at=datetime(2046, 7, 7, 9, tzinfo=UTC),
    )

    assert frozen.disposition == "CANDIDATES"
    assert finalized.disposition == "FAILED"
    assert finalized.published_at == datetime(2046, 7, 7, 9, tzinfo=UTC)
    assert finalized.population.valid_monthly is False
    assert finalized.members[0].candidate is False
    assert "PUBLICATION_AFTER_CANDIDATE_WINDOW" in finalized.reasons


def test_qualification_expiring_before_final_publication_removes_candidates() -> None:
    original = command()
    qualification = original.qualifications[0].model_copy(
        update={"valid_through": datetime(2046, 7, 2, 0, tzinfo=UTC)}
    )
    original = original.model_copy(update={"qualifications": (qualification,)})
    frozen = freeze_candidate_release(original, published_at=original.published_at)

    finalized = finalize_candidate_release_publication(
        original,
        frozen,
        published_at=datetime(2046, 7, 3, 0, tzinfo=UTC),
    )

    assert frozen.disposition == "CANDIDATES"
    assert finalized.disposition == "RECOMMENDATION_ABSTAINED"
    assert finalized.members[0].candidate is False
    assert finalized.members[0].market_state_qualified is False
    assert "MARKET_STATE_NOT_QUALIFIED" in finalized.members[0].reasons
    assert "MARKET_STATE_QUALIFICATION_EXPIRED" in finalized.members[0].reasons


def test_initial_publication_after_qualification_expiry_abstains() -> None:
    original = command(published=datetime(2046, 7, 3, tzinfo=UTC))
    qualification = original.qualifications[0].model_copy(
        update={"valid_through": datetime(2046, 7, 2, tzinfo=UTC)}
    )

    release = freeze_candidate_release(
        original.model_copy(update={"qualifications": (qualification,)})
    )

    assert release.disposition == "RECOMMENDATION_ABSTAINED"
    assert release.members[0].candidate is False
    assert "MARKET_STATE_NOT_QUALIFIED" in release.members[0].reasons


def test_training_window_requires_at_least_sixty_mature_months() -> None:
    candidate = command().model_copy(update={"training_window_months": ("2045-01",)})

    release = freeze_candidate_release(candidate)

    assert release.disposition == "FAILED"
    assert "CALIBRATION_REQUIRES_60_MATURE_MONTHS" in release.reasons


def test_declared_window_must_contain_only_mature_months() -> None:
    original = command()
    immature_month = original.training_window_months[-1]
    immature_records = tuple(
        record.model_copy(
            update={
                "unified_maturity_at": six_month_terminal_evaluation_at(
                    record.entry_at or record.entry_window_ends_at,
                    record.market_calendar_version,
                ),
                "label_available_at": six_month_terminal_evaluation_at(
                    record.entry_at or record.entry_window_ends_at,
                    record.market_calendar_version,
                ),
            }
        )
        if record.month == immature_month
        else record
        for record in original.training_records
    )
    extra_month_records = tuple(
        original.training_records[index].model_copy(
            update={
                "record_id": f"synthetic-label-2045-12-{index:02d}",
                "month": "2045-12",
                "source_research_event_id": "synthetic-research-event-2045-12",
                "raw_score_frozen_at": datetime(2045, 12, 31, 7, tzinfo=UTC),
                "raw_score_training_watermark_at": datetime(2045, 12, 30, 7, tzinfo=UTC),
                "entry_window_ends_at": datetime(2046, 1, 1, 8, tzinfo=UTC),
                "entry_at": datetime(2046, 1, 1, 7, tzinfo=UTC),
                "unified_maturity_at": six_month_terminal_evaluation_at(
                    datetime(2046, 1, 1, 7, tzinfo=UTC), "synthetic-calendar-v1"
                ),
                "label_available_at": six_month_terminal_evaluation_at(
                    datetime(2046, 1, 1, 7, tzinfo=UTC), "synthetic-calendar-v1"
                ),
            }
        )
        for index in range(10)
    )
    candidate = original.model_copy(
        update={"training_records": (*immature_records, *extra_month_records)}
    )

    release = freeze_candidate_release(candidate, initial_calibration=False)

    assert release.disposition == "FAILED"
    assert release.availability_failure == "CALIBRATION"
    assert release.reasons == ("CALIBRATION_TRAINING_WINDOW_NOT_LATEST",)


def test_declared_mature_window_cannot_omit_newer_mature_months() -> None:
    original = command()
    next_month_records = tuple(
        original.training_records[index].model_copy(
            update={
                "record_id": f"synthetic-label-2045-12-{index:02d}",
                "month": "2045-12",
                "source_research_event_id": "synthetic-research-event-2045-12",
                "raw_score_frozen_at": datetime(2045, 12, 31, 7, tzinfo=UTC),
                "raw_score_training_watermark_at": datetime(2045, 12, 30, 7, tzinfo=UTC),
                "entry_window_ends_at": datetime(2046, 1, 5, 8, tzinfo=UTC),
                "entry_at": datetime(2046, 1, 4, 8, tzinfo=UTC),
                "unified_maturity_at": six_month_terminal_evaluation_at(
                    datetime(2046, 1, 4, 8, tzinfo=UTC), "synthetic-calendar-v1"
                ),
                "label_available_at": six_month_terminal_evaluation_at(
                    datetime(2046, 1, 4, 8, tzinfo=UTC), "synthetic-calendar-v1"
                ),
            }
        )
        for index in range(10)
    )
    later_calendar = tuple(
        MarketSession(
            market_date=datetime(2046, 7, day, tzinfo=UTC).date(),
            opens_at=datetime(2046, 7, day, 1, tzinfo=UTC),
            closes_at=datetime(2046, 7, day, 8, tzinfo=UTC),
            session_sequence=sequence,
        )
        for day, sequence in ((11, 108), (12, 109), (13, 110), (16, 111), (17, 112))
    )
    candidate = original.model_copy(
        update={
            "knowledge_cutoff": datetime(2046, 7, 10, 8, tzinfo=UTC),
            "published_at": datetime(2046, 7, 11, 2, tzinfo=UTC),
            "last_completed_market_session_sequence": 107,
            "label_watermark_at": datetime(2046, 7, 10, 8, tzinfo=UTC),
            "training_records": (*original.training_records, *next_month_records),
            "market_sessions": later_calendar,
        }
    )

    release = freeze_candidate_release(candidate, initial_calibration=False)

    assert release.disposition == "FAILED"
    assert release.availability_failure == "CALIBRATION"
    assert release.reasons == ("CALIBRATION_TRAINING_WINDOW_NOT_LATEST",)


def test_declared_latest_mature_months_can_span_calendar_gaps() -> None:
    original = command()
    missing_month = "2042-01"
    earlier_month = "2043-11"
    earlier_records = tuple(
        record.model_copy(
            update={
                "record_id": f"synthetic-label-{earlier_month}-{index:02d}",
                "month": earlier_month,
                "source_research_event_id": f"synthetic-research-event-{earlier_month}",
                "raw_score_frozen_at": datetime(2043, 11, 30, 7, tzinfo=UTC),
                "raw_score_training_watermark_at": datetime(2043, 11, 29, 7, tzinfo=UTC),
                "entry_window_ends_at": datetime(2043, 12, 6, 8, tzinfo=UTC),
                "entry_at": datetime(2043, 12, 5, 8, tzinfo=UTC),
                "unified_maturity_at": six_month_terminal_evaluation_at(
                    datetime(2043, 12, 5, 8, tzinfo=UTC), "synthetic-calendar-v1"
                ),
                "label_available_at": six_month_terminal_evaluation_at(
                    datetime(2043, 12, 5, 8, tzinfo=UTC), "synthetic-calendar-v1"
                ),
            }
        )
        for index, record in enumerate(original.training_records[:10])
    )
    records = tuple(record for record in original.training_records if record.month != missing_month)
    records = (*records, *earlier_records)
    months = tuple(sorted({record.month for record in records}))

    release = freeze_candidate_release(
        original.model_copy(update={"training_window_months": months, "training_records": records})
    )

    assert release.disposition == "CANDIDATES"
    assert release.calibration is not None
    assert release.calibration.training_window_months == months


def test_previous_frozen_calibration_identities_remain_in_expected_cohort() -> None:
    original = command()
    frozen_only_record = original.training_records[1]
    frozen_only_identity = (
        frozen_only_record.month,
        frozen_only_record.security_id,
        frozen_only_record.research_id,
    )

    expected = decision_case_service._expected_calibration_identities(
        original.training_window_months,
        {},
        {frozen_only_identity: frozen_only_record},
    )

    assert expected == {frozen_only_identity}


def test_recent_diagnostics_preserve_frozen_source_event_attribution() -> None:
    source = frozen_raw_score_model_snapshot().training_records[0]
    identity = (source.month, source.security_id, source.research_id)
    source_record = decision_case_service._calibration_record_from_raw_score(
        source, "synthetic-source-event-current"
    )
    frozen_record = source_record.model_copy(
        update={"source_research_event_id": "synthetic-source-event-frozen"}
    )

    records = decision_case_service._recent_calibration_diagnostic_records(
        {identity: source},
        {identity: "synthetic-source-event-current"},
        {identity: frozen_record},
        {source.month},
        set(),
    )

    assert records[identity] == frozen_record


def test_candidate_release_freezes_member_probability_into_prediction_cohort() -> None:
    original = command()
    outcome = freeze_candidate_release(original)
    member = outcome.members[0]
    month = original.knowledge_cutoff.astimezone(UTC).strftime("%Y-%m")

    predictions = decision_case_service._frozen_candidate_prediction_rows(
        original,
        outcome,
        raw_score_frozen_at=original.knowledge_cutoff,
        raw_score_training_watermark_at=original.label_watermark_at,
    )

    prediction = predictions[(month, member.security_id, member.research_id)]
    assert prediction.raw_success_score == member.raw_success_score
    assert prediction.calibrated_probability == member.calibrated_probability
    assert prediction.matures_by == raw_score_maturity_at(
        original.market_sessions[-1].closes_at,
        original.market_calendar_version,
    )
    assert prediction.entry_sessions == tuple(
        (session.opens_at, session.closes_at) for session in original.market_sessions[:5]
    )


def test_missing_calendar_coverage_becomes_candidate_calibration_provenance_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = command()
    outcome = freeze_candidate_release(original)

    def missing_calendar_coverage(*_args: object) -> datetime:
        raise ValueError("MARKET_CALENDAR_TERMINAL_SESSION_UNAVAILABLE")

    monkeypatch.setattr(decision_case_service, "raw_score_maturity_at", missing_calendar_coverage)

    with pytest.raises(decision_case_service.CandidateCalibrationProvenanceInvalid):
        decision_case_service._frozen_candidate_prediction_rows(
            original,
            outcome,
            raw_score_frozen_at=original.knowledge_cutoff,
            raw_score_training_watermark_at=original.label_watermark_at,
        )


def test_preopen_cutoff_window_replays_its_same_day_entry() -> None:
    original = command()
    cutoff = datetime(2046, 7, 2, 0, tzinfo=UTC)
    preopen_command = original.model_copy(
        update={
            "knowledge_cutoff": cutoff,
            "published_at": cutoff + timedelta(hours=1),
            "label_watermark_at": cutoff,
        }
    )
    outcome = freeze_candidate_release(preopen_command)
    member = outcome.members[0]
    prediction_month = cutoff.strftime("%Y-%m")
    prediction = decision_case_service._frozen_candidate_prediction_rows(
        preopen_command,
        outcome,
        raw_score_frozen_at=cutoff,
        raw_score_training_watermark_at=cutoff,
    )[(prediction_month, member.security_id, member.research_id)]
    same_day_open = prediction.entry_sessions[0][0]
    maturity = raw_score_maturity_at(same_day_open, prediction.market_calendar_version)
    source = (
        frozen_raw_score_model_snapshot()
        .training_records[0]
        .model_copy(
            update={
                "month": prediction_month,
                "security_id": member.security_id,
                "research_id": member.research_id,
                "raw_score_frozen_at": prediction.raw_score_frozen_at,
                "raw_score_training_watermark_at": prediction.raw_score_training_watermark_at,
                "raw_success_score": prediction.raw_success_score,
                "historical_calibrated_probability": prediction.calibrated_probability,
                "entry_window_ends_at": prediction.entry_window_ends_at,
                "evaluation_entry_at": same_day_open,
                "unified_maturity_at": maturity,
                "market_calendar_version": prediction.market_calendar_version,
                "label_available_at": maturity,
            }
        )
    )

    assert outcome.disposition == "CANDIDATES"
    assert outcome.valid_market_dates[0] == same_day_open.date()
    decision_case_service._validate_frozen_candidate_prediction_source(prediction, source)


def test_frozen_prediction_accepts_executable_entry_at_session_close() -> None:
    original = command()
    outcome = freeze_candidate_release(original)
    member = outcome.members[0]
    month = original.knowledge_cutoff.strftime("%Y-%m")
    prediction = decision_case_service._frozen_candidate_prediction_rows(
        original,
        outcome,
        raw_score_frozen_at=original.knowledge_cutoff,
        raw_score_training_watermark_at=original.label_watermark_at,
    )[(month, member.security_id, member.research_id)]
    closing_at = prediction.entry_sessions[-1][1]
    maturity_at = raw_score_maturity_at(closing_at, prediction.market_calendar_version)
    source = (
        frozen_raw_score_model_snapshot()
        .training_records[0]
        .model_copy(
            update={
                "month": month,
                "security_id": member.security_id,
                "research_id": member.research_id,
                "raw_score_frozen_at": prediction.raw_score_frozen_at,
                "raw_score_training_watermark_at": prediction.raw_score_training_watermark_at,
                "raw_success_score": prediction.raw_success_score,
                "historical_calibrated_probability": prediction.calibrated_probability,
                "entry_window_ends_at": prediction.entry_window_ends_at,
                "evaluation_entry_at": closing_at,
                "unified_maturity_at": maturity_at,
                "market_calendar_version": prediction.market_calendar_version,
                "label_available_at": maturity_at,
            }
        )
    )

    decision_case_service._validate_frozen_candidate_prediction_source(prediction, source)


def test_calibrator_rejects_entry_before_next_session_window_open() -> None:
    original = command()
    source = original.training_records[0]
    year, month = (int(part) for part in source.month.split("-"))
    cutoff_day = next(day for day in range(10, 21) if datetime(year, month, day).weekday() < 5)
    cutoff = datetime(year, month, cutoff_day, 2, tzinfo=UTC)
    first_window_open = _raw_score_evaluation_entry_at(
        cutoff,
        source.market_calendar_version,
    )
    early_entry = cutoff + timedelta(hours=1)
    window_end = raw_score_entry_window_end(first_window_open, source.market_calendar_version)
    maturity = six_month_terminal_evaluation_at(early_entry, source.market_calendar_version)
    invalid_record = source.model_copy(
        update={
            "raw_score_frozen_at": cutoff,
            "raw_score_training_watermark_at": cutoff - timedelta(days=1),
            "entry_at": early_entry,
            "entry_window_ends_at": window_end,
            "unified_maturity_at": maturity,
            "label_available_at": maturity,
        }
    )
    invalid_command = original.model_copy(
        update={
            "training_records": (invalid_record, *original.training_records[1:]),
        }
    )

    with pytest.raises(ValueError, match="CALIBRATION_ENTRY_BEFORE_WINDOW_OPEN"):
        candidate_module._fit_calibrator(invalid_command)


def test_calibrator_rejects_a_declared_training_month_with_immature_labels() -> None:
    original = command()
    immature_month = "2045-12"
    cutoff = datetime(2045, 12, 31, 7, tzinfo=UTC)
    entry = datetime(2046, 1, 2, 8, tzinfo=UTC)
    entry_window_end = datetime(2046, 1, 9, 8, tzinfo=UTC)
    maturity = six_month_terminal_evaluation_at(entry, "synthetic-calendar-v1")
    immature_records = tuple(
        record.model_copy(
            update={
                "record_id": f"synthetic-immature-{index}",
                "month": immature_month,
                "source_research_event_id": "synthetic-immature-source",
                "raw_score_frozen_at": cutoff,
                "raw_score_training_watermark_at": cutoff - timedelta(days=1),
                "entry_at": entry,
                "entry_window_ends_at": entry_window_end,
                "unified_maturity_at": maturity,
                "label_available_at": maturity,
            }
        )
        for index, record in enumerate(original.training_records[:10])
    )
    candidate = original.model_copy(
        update={
            "training_window_months": (*original.training_window_months, immature_month),
            "training_records": (*original.training_records, *immature_records),
        }
    )

    with pytest.raises(ValueError, match="CALIBRATION_TRAINING_WINDOW_NOT_MATURE"):
        candidate_module._fit_calibrator(candidate)


def test_rolling_calibration_rejects_a_window_that_is_not_exactly_sixty_months() -> None:
    original = command()
    older_month = "2040-11"
    cutoff = datetime(2040, 11, 30, 7, tzinfo=UTC)
    entry = datetime(2040, 12, 3, 1, tzinfo=UTC)
    entry_window_end = datetime(2040, 12, 7, 8, tzinfo=UTC)
    maturity = six_month_terminal_evaluation_at(entry, "synthetic-calendar-v1")
    older_records = tuple(
        record.model_copy(
            update={
                "record_id": f"synthetic-older-rolling-{index}",
                "month": older_month,
                "source_research_event_id": "synthetic-older-rolling-source",
                "raw_score_frozen_at": cutoff,
                "raw_score_training_watermark_at": cutoff - timedelta(days=1),
                "entry_at": entry,
                "entry_window_ends_at": entry_window_end,
                "unified_maturity_at": maturity,
                "label_available_at": maturity,
            }
        )
        for index, record in enumerate(original.training_records[:10])
    )
    candidate = original.model_copy(
        update={
            "training_window_months": (older_month, *original.training_window_months),
            "training_records": (*older_records, *original.training_records),
        }
    )

    with pytest.raises(ValueError, match="CALIBRATION_ROLLING_WINDOW_MUST_BE_60_MONTHS"):
        candidate_module._fit_calibrator(candidate, initial_calibration=False)


def test_prior_candidate_predictions_use_the_referenced_research_event() -> None:
    original = command()
    outcome = freeze_candidate_release(original)
    research_watermark = original.knowledge_cutoff - timedelta(days=1)
    candidate_event = cast(
        DecisionEventFact,
        SimpleNamespace(
            validation_status="PASSED",
            committed_at=original.knowledge_cutoff.isoformat(),
            corrects_event_id=None,
            case=SimpleNamespace(
                candidate_release=original,
                knowledge_cutoff=original.knowledge_cutoff.isoformat(),
                research=None,
            ),
            result=SimpleNamespace(candidate_release=outcome, research=None),
        ),
    )
    research_event = cast(
        DecisionEventFact,
        SimpleNamespace(
            decision_event_id=original.research_event_id,
            business_object_id=original.research_object_id,
            case=SimpleNamespace(
                knowledge_cutoff=original.knowledge_cutoff.isoformat(),
                research=SimpleNamespace(
                    raw_score_model=SimpleNamespace(label_watermark_at=research_watermark)
                ),
            ),
            result=SimpleNamespace(
                research=SimpleNamespace(
                    raw_scores=(SimpleNamespace(label_watermark_at=research_watermark),)
                )
            ),
        ),
    )
    research_event.case.knowledge_cutoff = original.knowledge_cutoff.isoformat().replace(
        "+00:00", "Z"
    )

    predictions = decision_case_service._frozen_candidate_prediction_rows_from_events(
        candidate_event, research_event
    )

    member = outcome.members[0]
    identity = (
        original.knowledge_cutoff.strftime("%Y-%m"),
        member.security_id,
        member.research_id,
    )
    assert predictions[identity].raw_score_frozen_at == original.knowledge_cutoff
    assert predictions[identity].raw_score_training_watermark_at == research_watermark


def test_failed_candidate_member_without_probability_stays_out_of_calibration_history() -> None:
    original = command()
    missing_probability_member = SimpleNamespace(
        security_id="SYNTH-UNAVAILABLE",
        research_id="synthetic-unavailable-research",
        calibrated_probability=None,
    )
    failed_release_event = cast(
        DecisionEventFact,
        SimpleNamespace(
            validation_status="PASSED",
            committed_at=original.knowledge_cutoff.isoformat(),
            corrects_event_id=None,
            case=SimpleNamespace(
                candidate_release=original,
                knowledge_cutoff=original.knowledge_cutoff.isoformat(),
            ),
            result=SimpleNamespace(
                candidate_release=SimpleNamespace(members=(missing_probability_member,))
            ),
        ),
    )
    later_unavailable_event = cast(
        DecisionEventFact,
        SimpleNamespace(
            validation_status="PASSED",
            committed_at=original.knowledge_cutoff.isoformat(),
            corrects_event_id=None,
            case=SimpleNamespace(
                candidate_release=original,
                knowledge_cutoff=(original.knowledge_cutoff + timedelta(days=1)).isoformat(),
            ),
            result=SimpleNamespace(
                candidate_release=SimpleNamespace(members=(missing_probability_member,))
            ),
        ),
    )

    unavailable = decision_case_service._unavailable_candidate_probability_identities(
        (failed_release_event, later_unavailable_event),
        original.knowledge_cutoff,
    )

    assert unavailable == {
        (
            original.knowledge_cutoff.strftime("%Y-%m"),
            "SYNTH-UNAVAILABLE",
            "synthetic-unavailable-research",
        )
    }


def test_unavailable_probability_count_includes_committed_candidate_without_source_row() -> None:
    original = command()
    member = SimpleNamespace(
        security_id="SYNTH-UNAVAILABLE",
        research_id="synthetic-unavailable-research",
        calibrated_probability=None,
    )
    failed_release_event = cast(
        DecisionEventFact,
        SimpleNamespace(
            validation_status="PASSED",
            committed_at=original.knowledge_cutoff.isoformat(),
            corrects_event_id=None,
            case=SimpleNamespace(
                candidate_release=original,
                knowledge_cutoff=original.knowledge_cutoff.isoformat(),
            ),
            result=SimpleNamespace(candidate_release=SimpleNamespace(members=(member,))),
        ),
    )
    unavailable_identities = decision_case_service._unavailable_candidate_probability_identities(
        (failed_release_event,), original.knowledge_cutoff
    )
    count = decision_case_service._unavailable_calibration_probability_count(
        {},
        unavailable_identities,
        {},
        original.knowledge_cutoff,
    )

    assert count == 1


def test_suppressed_failed_release_members_are_not_trusted_for_identity_exclusion() -> None:
    original = command()
    failed_release_event = cast(
        DecisionEventFact,
        SimpleNamespace(
            validation_status="PASSED",
            committed_at=original.knowledge_cutoff.isoformat(),
            corrects_event_id=None,
            case=SimpleNamespace(
                candidate_release=original,
                knowledge_cutoff=original.knowledge_cutoff.isoformat(),
            ),
            result=SimpleNamespace(
                candidate_release=SimpleNamespace(
                    members=(),
                    availability_failure="DATA",
                    disposition="FAILED",
                    calibration=None,
                )
            ),
        ),
    )

    unavailable = decision_case_service._unavailable_candidate_probability_identities(
        (failed_release_event,),
        original.knowledge_cutoff,
    )

    assert unavailable == set()


def test_suppressed_failed_release_counts_only_its_validated_research_roster() -> None:
    original = command()
    source_members = tuple(
        SimpleNamespace(security_id=f"SOURCE-{member:02d}", research_id=f"SOURCE-R-{member:02d}")
        for member in range(10)
    )
    source_event = cast(
        DecisionEventFact,
        SimpleNamespace(
            decision_event_id=original.research_event_id,
            business_object_id=original.research_object_id,
            validation_status="PASSED",
            committed_at=original.knowledge_cutoff.isoformat(),
            corrects_event_id=None,
            case=SimpleNamespace(
                knowledge_cutoff=original.knowledge_cutoff.isoformat(),
                research=SimpleNamespace(members=source_members),
                access_scope=None,
            ),
            result=SimpleNamespace(
                research=SimpleNamespace(
                    disposition="FROZEN",
                    raw_scores=(object(),),
                    risk_veto=object(),
                )
            ),
        ),
    )
    failed_release = cast(
        DecisionEventFact,
        SimpleNamespace(
            validation_status="PASSED",
            committed_at=original.knowledge_cutoff.isoformat(),
            corrects_event_id=None,
            case=SimpleNamespace(
                candidate_release=original,
                knowledge_cutoff=original.knowledge_cutoff.isoformat(),
                access_scope=None,
            ),
            result=SimpleNamespace(
                candidate_release=SimpleNamespace(
                    members=(), disposition="FAILED", calibration=None
                )
            ),
        ),
    )

    unavailable = decision_case_service._unavailable_candidate_probability_identities(
        (failed_release,), original.knowledge_cutoff, (source_event,)
    )

    assert unavailable == {
        (original.knowledge_cutoff.strftime("%Y-%m"), member.security_id, member.research_id)
        for member in source_members
    }


@pytest.mark.parametrize("phase", ["FRAMEWORK_RUN", "HOST_VALIDATION", "BUSINESS_COMMIT"])
def test_uncommitted_candidate_failure_counts_only_its_validated_research_roster(
    phase: str,
) -> None:
    original = command()
    cutoff = original.knowledge_cutoff
    source_members = tuple(
        SimpleNamespace(security_id=f"SOURCE-{member:02d}", research_id=f"SOURCE-R-{member:02d}")
        for member in range(10)
    )
    source_event = cast(
        DecisionEventFact,
        SimpleNamespace(
            decision_event_id=original.research_event_id,
            business_object_id=original.research_object_id,
            validation_status="PASSED",
            committed_at=cutoff.isoformat(),
            corrects_event_id=None,
            case=SimpleNamespace(
                knowledge_cutoff=cutoff.isoformat(),
                research=SimpleNamespace(members=source_members),
                access_scope=None,
            ),
            result=SimpleNamespace(
                research=SimpleNamespace(
                    disposition="FROZEN",
                    raw_scores=(object(),),
                    risk_veto=object(),
                )
            ),
        ),
    )
    failed_candidate = cast(
        CandidateAvailabilityFailureFact,
        SimpleNamespace(
            case=SimpleNamespace(
                candidate_release=original,
                knowledge_cutoff=cutoff.isoformat(),
                access_scope=None,
            ),
            stage_result=SimpleNamespace(phase=phase, status="FAILED"),
            recorded_at=cutoff.isoformat(),
            committed_at=None,
        ),
    )
    late_candidate = cast(
        CandidateAvailabilityFailureFact,
        SimpleNamespace(
            case=failed_candidate.case,
            stage_result=failed_candidate.stage_result,
            recorded_at=(cutoff + timedelta(seconds=1)).isoformat(),
            committed_at=None,
        ),
    )

    unavailable = decision_case_service._unavailable_candidate_probability_identities(
        (),
        cutoff,
        (source_event,),
        (failed_candidate, late_candidate),
    )

    assert unavailable == {
        (cutoff.strftime("%Y-%m"), member.security_id, member.research_id)
        for member in source_members
    }


def test_candidate_stage_failure_counts_when_its_event_commits_after_cutoff() -> None:
    original = command()
    cutoff = original.knowledge_cutoff
    source_members = tuple(
        SimpleNamespace(security_id=f"SOURCE-{member:02d}", research_id=f"SOURCE-R-{member:02d}")
        for member in range(10)
    )
    source_event = cast(
        DecisionEventFact,
        SimpleNamespace(
            decision_event_id=original.research_event_id,
            business_object_id=original.research_object_id,
            validation_status="PASSED",
            committed_at=cutoff.isoformat(),
            corrects_event_id=None,
            case=SimpleNamespace(
                knowledge_cutoff=cutoff.isoformat(),
                research=SimpleNamespace(members=source_members),
                access_scope=None,
            ),
            result=SimpleNamespace(
                research=SimpleNamespace(
                    disposition="FROZEN",
                    raw_scores=(object(),),
                    risk_veto=object(),
                )
            ),
        ),
    )
    late_commit = cast(
        CandidateAvailabilityFailureFact,
        SimpleNamespace(
            case=SimpleNamespace(
                candidate_release=original,
                knowledge_cutoff=cutoff.isoformat(),
                access_scope=None,
            ),
            stage_result=SimpleNamespace(phase="FRAMEWORK_RUN", status="FAILED"),
            recorded_at=cutoff.isoformat(),
            committed_at=(cutoff + timedelta(seconds=1)).isoformat(),
        ),
    )

    unavailable = decision_case_service._unavailable_candidate_probability_identities(
        (), cutoff, (source_event,), (late_commit,)
    )

    assert unavailable == {
        (cutoff.strftime("%Y-%m"), member.security_id, member.research_id)
        for member in source_members
    }


def test_committed_candidate_outcome_supersedes_its_commit_failure_stage() -> None:
    original = command()
    cutoff = original.knowledge_cutoff
    framework_run_id = "synthetic-recovered-candidate-run"
    member = SimpleNamespace(
        security_id="RECOVERED-SECURITY",
        research_id="recovered-research",
        calibrated_probability=Decimal("0.91"),
    )
    committed_outcome = cast(
        DecisionEventFact,
        SimpleNamespace(
            framework_run_id=framework_run_id,
            validation_status="PASSED",
            committed_at=cutoff.isoformat(),
            corrects_event_id=None,
            case=SimpleNamespace(
                candidate_release=original,
                knowledge_cutoff=cutoff.isoformat(),
            ),
            result=SimpleNamespace(
                candidate_release=SimpleNamespace(
                    members=(member,), disposition="CANDIDATES", calibration=object()
                )
            ),
        ),
    )
    commit_failure = cast(
        CandidateAvailabilityFailureFact,
        SimpleNamespace(
            framework_run_id=framework_run_id,
            case=SimpleNamespace(
                candidate_release=original,
                knowledge_cutoff=cutoff.isoformat(),
                access_scope=None,
            ),
            stage_result=SimpleNamespace(phase="BUSINESS_COMMIT", status="FAILED"),
            recorded_at=cutoff.isoformat(),
            committed_at=cutoff.isoformat(),
        ),
    )

    unavailable = decision_case_service._unavailable_candidate_probability_identities(
        (committed_outcome,), cutoff, candidate_availability_failures=(commit_failure,)
    )

    assert unavailable == set()


def test_late_research_commit_cannot_validate_candidate_failure_roster() -> None:
    original = command()
    cutoff = original.knowledge_cutoff
    source_members = tuple(
        SimpleNamespace(security_id=f"SOURCE-{member:02d}", research_id=f"SOURCE-R-{member:02d}")
        for member in range(10)
    )
    late_source_event = cast(
        DecisionEventFact,
        SimpleNamespace(
            decision_event_id=original.research_event_id,
            business_object_id=original.research_object_id,
            validation_status="PASSED",
            committed_at=(cutoff + timedelta(seconds=1)).isoformat(),
            corrects_event_id=None,
            case=SimpleNamespace(
                knowledge_cutoff=cutoff.isoformat(),
                research=SimpleNamespace(members=source_members),
                access_scope=None,
            ),
            result=SimpleNamespace(
                research=SimpleNamespace(
                    disposition="FROZEN",
                    raw_scores=(object(),),
                    risk_veto=object(),
                )
            ),
        ),
    )
    failed_candidate = cast(
        DecisionEventFact,
        SimpleNamespace(
            validation_status="PASSED",
            committed_at=cutoff.isoformat(),
            corrects_event_id=None,
            case=SimpleNamespace(
                candidate_release=original,
                knowledge_cutoff=cutoff.isoformat(),
                access_scope=None,
            ),
            result=SimpleNamespace(
                candidate_release=SimpleNamespace(
                    members=(), disposition="FAILED", calibration=None
                )
            ),
        ),
    )

    unavailable = decision_case_service._unavailable_candidate_probability_identities(
        (failed_candidate,), cutoff, (late_source_event,)
    )

    assert unavailable == set()


def test_calibration_source_rejects_historical_event_committed_after_cutoff() -> None:
    cutoff = command().knowledge_cutoff
    late_event = cast(
        DecisionEventFact,
        SimpleNamespace(
            committed_at=(cutoff + timedelta(seconds=1)).isoformat(),
            decision_event_id="late-historical-event",
            case=SimpleNamespace(
                knowledge_cutoff=(cutoff - timedelta(days=1)).isoformat(),
            ),
        ),
    )

    with pytest.raises(decision_case_service.CandidateCalibrationProvenanceInvalid):
        decision_case_service._validate_calibration_source_event_availability(
            late_event,
            cutoff,
            current_research_event_id="current-research-event",
        )


def test_calibration_source_allows_current_cutoff_event_as_batch_snapshot() -> None:
    cutoff = command().knowledge_cutoff
    current_batch_event = cast(
        DecisionEventFact,
        SimpleNamespace(
            committed_at=(cutoff + timedelta(seconds=1)).isoformat(),
            decision_event_id="current-research-event",
            case=SimpleNamespace(knowledge_cutoff=cutoff.isoformat()),
        ),
    )

    decision_case_service._validate_calibration_source_event_availability(
        current_batch_event,
        cutoff,
        current_research_event_id="current-research-event",
    )


def test_frozen_candidate_probability_supersedes_research_failure_before_maturity() -> None:
    original = command()
    identity = (original.knowledge_cutoff.strftime("%Y-%m"), "SYNTH-RECOVERED", "research-a")

    count = decision_case_service._unavailable_calibration_probability_count(
        {},
        set(),
        {},
        original.knowledge_cutoff,
        {identity},
        frozen_candidate_prediction_identities={identity},
    )

    assert count == 0


def test_unvalidated_failed_release_members_cannot_exclude_authoritative_samples() -> None:
    original = command()
    missing_probability_member = SimpleNamespace(
        security_id="SYNTH-UNAVAILABLE",
        research_id="synthetic-unavailable-research",
        calibrated_probability=None,
    )
    failed_release_event = cast(
        DecisionEventFact,
        SimpleNamespace(
            validation_status="FAILED",
            committed_at=original.knowledge_cutoff.isoformat(),
            corrects_event_id=None,
            case=SimpleNamespace(
                candidate_release=original,
                knowledge_cutoff=original.knowledge_cutoff.isoformat(),
            ),
            result=SimpleNamespace(
                candidate_release=SimpleNamespace(members=(missing_probability_member,))
            ),
        ),
    )

    assert (
        decision_case_service._unavailable_candidate_probability_identities(
            (failed_release_event,), original.knowledge_cutoff
        )
        == set()
    )


@pytest.mark.parametrize("failed_phase", ("RESEARCH", "BUSINESS_COMMIT"))
def test_unavailable_probability_count_includes_committed_failed_research_roster(
    failed_phase: str,
) -> None:
    original = command()
    research_event = cast(
        DecisionEventFact,
        SimpleNamespace(
            corrects_event_id=None,
            validation_status="PASSED",
            committed_at=original.knowledge_cutoff.isoformat(),
            case=SimpleNamespace(
                research=SimpleNamespace(
                    members=tuple(
                        SimpleNamespace(
                            security_id=f"SYNTH-{member:02d}", research_id=f"R-{member:02d}"
                        )
                        for member in range(10)
                    )
                ),
                knowledge_cutoff=original.knowledge_cutoff.isoformat(),
            ),
            result=SimpleNamespace(research=None),
            stage_results=(SimpleNamespace(phase=failed_phase, status="FAILED"),),
        ),
    )

    unavailable = decision_case_service._unavailable_research_probability_identities(
        (research_event,), original.knowledge_cutoff
    )
    count = decision_case_service._unavailable_calibration_probability_count(
        {}, set(), {}, original.knowledge_cutoff, unavailable
    )

    assert count == 10


@pytest.mark.parametrize("failed_phase", ("RESEARCH", "RAW_SCORE", "RISK_VETO", "BUSINESS_COMMIT"))
def test_unavailable_probability_count_includes_uncommitted_failed_stage_roster(
    failed_phase: str,
) -> None:
    original = command()
    stage_failure = cast(
        ResearchAvailabilityFailureFact,
        SimpleNamespace(
            case=SimpleNamespace(
                research=SimpleNamespace(
                    members=tuple(
                        SimpleNamespace(
                            security_id=f"SYNTH-{member:02d}", research_id=f"R-{member:02d}"
                        )
                        for member in range(10)
                    )
                ),
                knowledge_cutoff=original.knowledge_cutoff.isoformat(),
            ),
            stage_result=SimpleNamespace(phase=failed_phase, status="FAILED"),
            recorded_at=original.knowledge_cutoff.isoformat(),
            framework_run_id="research-run-failed-then-recovered",
        ),
    )

    unavailable = decision_case_service._unavailable_research_probability_identities(
        (), original.knowledge_cutoff, (stage_failure,)
    )
    count = decision_case_service._unavailable_calibration_probability_count(
        {}, set(), {}, original.knowledge_cutoff, unavailable
    )

    assert count == 10


def test_recovered_research_commit_supersedes_same_run_commit_failure() -> None:
    original = command()
    cutoff = original.knowledge_cutoff
    members = tuple(
        SimpleNamespace(security_id=f"SYNTH-{member:02d}", research_id=f"R-{member:02d}")
        for member in range(10)
    )
    stage_failure = cast(
        ResearchAvailabilityFailureFact,
        SimpleNamespace(
            framework_run_id="research-run-failed-then-recovered",
            case=SimpleNamespace(
                research=SimpleNamespace(members=members),
                knowledge_cutoff=cutoff.isoformat(),
            ),
            stage_result=SimpleNamespace(phase="BUSINESS_COMMIT", status="FAILED"),
            recorded_at=cutoff.isoformat(),
        ),
    )
    committed_recovery = cast(
        DecisionEventFact,
        SimpleNamespace(
            framework_run_id="research-run-failed-then-recovered",
            corrects_event_id=None,
            validation_status="PASSED",
            committed_at=cutoff.isoformat(),
            case=SimpleNamespace(
                research=SimpleNamespace(members=members),
                knowledge_cutoff=cutoff.isoformat(),
            ),
            result=SimpleNamespace(research=SimpleNamespace(disposition="FROZEN")),
        ),
    )

    unavailable = decision_case_service._unavailable_research_probability_identities(
        (committed_recovery,), cutoff, (stage_failure,)
    )

    assert unavailable == set()


def test_recovered_research_probability_supersedes_failed_attempt_availability() -> None:
    source = frozen_raw_score_model_snapshot().training_records[0]
    failure = (
        source.month,
        source.security_id,
        "a-later-failed-research-retry",
    )

    count = decision_case_service._unavailable_calibration_probability_count(
        {
            (source.month, source.security_id, source.research_id): source,
        },
        set(),
        {},
        datetime(2050, 1, 1, tzinfo=UTC),
        {failure},
    )

    assert count == 0


def test_unavailable_probability_ignores_unvalidated_research_failure_roster() -> None:
    original = command()
    research_event = cast(
        DecisionEventFact,
        SimpleNamespace(
            corrects_event_id=None,
            validation_status="FAILED",
            case=SimpleNamespace(
                research=SimpleNamespace(
                    members=(SimpleNamespace(security_id="SYNTH-01", research_id="R-01"),)
                ),
                knowledge_cutoff=original.knowledge_cutoff.isoformat(),
            ),
            result=SimpleNamespace(research=None),
            stage_results=(SimpleNamespace(phase="RESEARCH", status="FAILED"),),
        ),
    )

    assert (
        decision_case_service._unavailable_research_probability_identities(
            (research_event,), original.knowledge_cutoff
        )
        == set()
    )


def test_historical_cohort_collection_rejects_two_pools_in_one_month() -> None:
    model = frozen_raw_score_model_snapshot()
    cohort = model.calibration_history_cohorts[0]
    conflicting = cohort.model_copy(
        update={
            "cohort_id": f"{cohort.cohort_id}-alternate",
            "member_security_ids": (*cohort.member_security_ids[:-1], "SYNTH-OTHER"),
            "completed_research_ids": {
                **cohort.completed_research_ids,
                "SYNTH-OTHER": "synthetic-other-research",
            },
        }
    )
    frozen = {cohort.cohort_id: cohort}

    with pytest.raises(decision_case_service.CandidateCalibrationProvenanceInvalid):
        decision_case_service._retain_immutable_cohort(frozen, conflicting)


def test_calibration_provenance_deduplicates_identical_source_rows() -> None:
    record = command().training_records[0]

    grouped = decision_case_service._calibration_records_by_source_event((record, record))

    assert grouped[record.source_research_event_id] == [record]


def test_calibration_provenance_rejects_conflicting_source_rows() -> None:
    record = command().training_records[0]
    changed = record.model_copy(
        update={"out_of_sample_probability": record.out_of_sample_probability + Decimal("0.01")}
    )

    with pytest.raises(decision_case_service.CandidateCalibrationProvenanceInvalid):
        decision_case_service._calibration_records_by_source_event((record, changed))


def test_unavailable_probability_count_includes_frozen_cohort_members_without_research() -> None:
    model = frozen_raw_score_model_snapshot()
    cohorts = (*model.training_cohorts, *model.calibration_history_cohorts)
    unavailable_security_ids = {
        (cohort.month, security_id)
        for cohort in cohorts
        for security_id in set(cohort.member_security_ids) - set(cohort.completed_research_ids)
    }

    count = decision_case_service._unavailable_calibration_probability_count(
        {},
        set(),
        {cohort.cohort_id: cohort for cohort in cohorts},
        datetime(2045, 1, 1, tzinfo=UTC),
    )

    assert count == len(unavailable_security_ids)


def test_unavailable_probability_count_includes_a_fully_failed_month_outside_fit_windows() -> None:
    model = frozen_raw_score_model_snapshot()
    cohort = next(
        cohort for cohort in model.training_cohorts if len(cohort.completed_research_ids) >= 8
    )
    rows_by_identity = {
        (record.month, record.security_id, record.research_id): record.model_copy(
            update={"historical_calibrated_probability": None}
        )
        for record in model.training_records
        if record.month == cohort.month
    }

    count = decision_case_service._unavailable_calibration_probability_count(
        rows_by_identity,
        set(),
        {cohort.cohort_id: cohort},
        datetime(2050, 1, 1, tzinfo=UTC),
    )

    assert count == len(cohort.member_security_ids)


def test_calibration_source_population_excludes_records_without_frozen_probability() -> None:
    source_records = frozen_raw_score_model_snapshot().training_records
    missing_probability = source_records[0].model_copy(
        update={"historical_calibrated_probability": None}
    )
    source_records = (missing_probability, *source_records[1:3])

    source_rows = decision_case_service._calibration_source_rows_by_identity(
        source_records, {missing_probability.month}
    )

    assert (
        missing_probability.month,
        missing_probability.security_id,
        missing_probability.research_id,
    ) not in source_rows
    assert len(source_rows) == 2


def test_calibration_source_population_unions_training_and_history_records() -> None:
    model = frozen_raw_score_model_snapshot()

    source_records = decision_case_service._raw_score_calibration_source_records(model)

    source_identities = {
        (record.month, record.security_id, record.research_id) for record in source_records
    }
    expected_identities = {
        (record.month, record.security_id, record.research_id)
        for record in (*model.training_records, *model.calibration_history_records)
    }
    assert source_identities == expected_identities
    assert len(source_records) == len(expected_identities)


def test_calibration_source_population_rejects_conflicting_duplicate_identity() -> None:
    model = frozen_raw_score_model_snapshot()
    training_record = model.training_records[0]
    conflicting_history_record = training_record.model_copy(
        update={"historical_calibrated_probability": Decimal("0.99")}
    )
    model_with_conflict = model.model_copy(
        update={"calibration_history_records": (conflicting_history_record,)}
    )

    with pytest.raises(decision_case_service.CandidateCalibrationProvenanceInvalid):
        decision_case_service._raw_score_calibration_source_records(model_with_conflict)


def test_calibration_provenance_rejects_rewritten_cohort_identity_across_events() -> None:
    cohort = frozen_raw_score_model_snapshot().training_cohorts[0]
    member_security_id = next(iter(cohort.completed_research_ids))
    rewritten_members = dict(cohort.completed_research_ids)
    rewritten_members[member_security_id] = "synthetic-rewritten-research"
    rewritten_cohort = cohort.model_copy(update={"completed_research_ids": rewritten_members})
    cohorts_by_id: dict[str, RawScoreTrainingCohort] = {}

    decision_case_service._retain_immutable_cohort(cohorts_by_id, cohort)

    with pytest.raises(decision_case_service.CandidateCalibrationProvenanceInvalid):
        decision_case_service._retain_immutable_cohort(cohorts_by_id, rewritten_cohort)


def test_unavailable_probability_identity_cannot_reenter_from_another_source_row() -> None:
    source_records = frozen_raw_score_model_snapshot().training_records
    unavailable = source_records[0].model_copy(update={"historical_calibrated_probability": None})
    duplicate_available = source_records[0]
    identity = (unavailable.month, unavailable.security_id, unavailable.research_id)

    source_rows = decision_case_service._calibration_source_rows_by_identity(
        (unavailable, duplicate_available), {unavailable.month}, {identity}
    )

    assert identity not in source_rows


def test_calibration_provenance_groups_allow_one_month_to_span_research_events() -> None:
    records = command().training_records[:2]
    first = records[0].model_copy(update={"source_research_event_id": "synthetic-source-event-a"})
    second = records[1].model_copy(update={"source_research_event_id": "synthetic-source-event-b"})

    grouped = decision_case_service._calibration_records_by_source_event((first, second))

    assert set(grouped) == {"synthetic-source-event-a", "synthetic-source-event-b"}
    assert {record.month for rows in grouped.values() for record in rows} == {first.month}


def test_candidate_prediction_month_uses_shanghai_for_equivalent_cutoff_instants() -> None:
    cutoffs = (
        datetime.fromisoformat("2042-06-30T23:59:59+08:00"),
        datetime.fromisoformat("2042-07-01T00:59:59+09:00"),
    )

    assert {decision_case_service._candidate_prediction_month(cutoff) for cutoff in cutoffs} == {
        "2042-06"
    }


def test_candidate_prediction_month_normalizes_non_shanghai_cutoff_offsets() -> None:
    cutoff = datetime.fromisoformat("2040-01-31T23:00:00-08:00")

    assert decision_case_service._candidate_prediction_month(cutoff) == "2040-02"


def test_unavailable_probability_ignores_committed_research_failure_after_cutoff() -> None:
    original = command()
    late_failure = cast(
        DecisionEventFact,
        SimpleNamespace(
            committed_at=(original.knowledge_cutoff + timedelta(seconds=1)).isoformat(),
            corrects_event_id=None,
            validation_status="PASSED",
            case=SimpleNamespace(
                research=SimpleNamespace(
                    members=tuple(
                        SimpleNamespace(
                            security_id=f"SYNTH-{member:02d}", research_id=f"R-{member:02d}"
                        )
                        for member in range(10)
                    )
                ),
                knowledge_cutoff=original.knowledge_cutoff.isoformat(),
            ),
            result=SimpleNamespace(research=None),
            stage_results=(SimpleNamespace(phase="RESEARCH", status="FAILED"),),
        ),
    )

    assert (
        decision_case_service._unavailable_research_probability_identities(
            (late_failure,), original.knowledge_cutoff
        )
        == set()
    )


def test_unavailable_probability_ignores_committed_candidate_failure_after_cutoff() -> None:
    original = command()
    late_failure = cast(
        DecisionEventFact,
        SimpleNamespace(
            committed_at=(original.knowledge_cutoff + timedelta(seconds=1)).isoformat(),
            validation_status="PASSED",
            corrects_event_id=None,
            case=SimpleNamespace(
                candidate_release=original,
                knowledge_cutoff=original.knowledge_cutoff.isoformat(),
            ),
            result=SimpleNamespace(
                candidate_release=SimpleNamespace(
                    members=(
                        SimpleNamespace(
                            security_id="SYNTH-UNAVAILABLE",
                            research_id="synthetic-unavailable-research",
                            calibrated_probability=None,
                        ),
                    ),
                    disposition="FAILED",
                    calibration=None,
                )
            ),
        ),
    )

    assert (
        decision_case_service._unavailable_candidate_probability_identities(
            (late_failure,), original.knowledge_cutoff
        )
        == set()
    )


def test_mature_calibration_source_rejects_duplicate_security_month_across_research_ids() -> None:
    first = frozen_raw_score_model_snapshot().calibration_history_records[0]
    second = first.model_copy(update={"research_id": f"{first.research_id}-renamed"})
    rows: dict[tuple[str, str, str], RawScoreTrainingRecord] = {}
    first_identity = (first.month, first.security_id, first.research_id)
    second_identity = (second.month, second.security_id, second.research_id)
    decision_case_service._retain_immutable_source_row(rows, first_identity, first)

    with pytest.raises(decision_case_service.CandidateCalibrationProvenanceInvalid):
        decision_case_service._retain_immutable_source_row(rows, second_identity, second)


def test_backdated_publication_request_does_not_hide_committed_calibration_history() -> None:
    frozen_command = command()
    prior_command = frozen_command.model_copy(
        update={
            "knowledge_cutoff": frozen_command.knowledge_cutoff - timedelta(days=60),
            "published_at": frozen_command.published_at - timedelta(days=60),
        }
    )
    prior_event = cast(
        DecisionEventFact,
        SimpleNamespace(
            case=SimpleNamespace(
                knowledge_cutoff=prior_command.knowledge_cutoff.isoformat(),
                candidate_release=prior_command,
            ),
            result=SimpleNamespace(
                candidate_release=SimpleNamespace(
                    calibration=SimpleNamespace(
                        calibrator_version=frozen_command.calibrator_version
                    )
                )
            ),
            committed_at=(prior_command.published_at + timedelta(days=1)).isoformat(),
        ),
    )

    assert decision_case_service._prior_calibration_snapshot_matches(prior_event, frozen_command)


def test_prior_calibration_snapshot_must_be_committed_by_current_knowledge_cutoff() -> None:
    current_command = command()
    prior_command = current_command.model_copy(
        update={
            "knowledge_cutoff": current_command.knowledge_cutoff - timedelta(days=60),
            "published_at": current_command.published_at - timedelta(days=60),
        }
    )
    prior_event = cast(
        DecisionEventFact,
        SimpleNamespace(
            case=SimpleNamespace(
                knowledge_cutoff=prior_command.knowledge_cutoff.isoformat(),
                candidate_release=prior_command,
            ),
            result=SimpleNamespace(
                candidate_release=SimpleNamespace(
                    calibration=SimpleNamespace(
                        calibrator_version=current_command.calibrator_version
                    )
                )
            ),
            committed_at=(current_command.knowledge_cutoff + timedelta(seconds=1)).isoformat(),
        ),
    )

    assert not decision_case_service._prior_calibration_snapshot_matches(
        prior_event,
        current_command,
    )


@pytest.mark.parametrize(
    "prior_updates",
    (
        {"capability_version": "candidate-v2"},
        {
            "training_records": tuple(
                record.model_copy(update={"raw_score_model_version": "raw-score-v2"})
                for record in command().training_records
            ),
        },
    ),
    ids=("capability-version", "raw-score-model-version"),
)
def test_prior_calibration_snapshot_does_not_cross_frozen_version(
    prior_updates: dict[str, object],
) -> None:
    current_command = command()
    prior_command = current_command.model_copy(
        update={
            **prior_updates,
            "knowledge_cutoff": current_command.knowledge_cutoff - timedelta(days=60),
            "published_at": current_command.published_at - timedelta(days=60),
        }
    )
    prior_event = cast(
        DecisionEventFact,
        SimpleNamespace(
            case=SimpleNamespace(
                knowledge_cutoff=prior_command.knowledge_cutoff.isoformat(),
                candidate_release=prior_command,
            ),
            result=SimpleNamespace(
                candidate_release=SimpleNamespace(
                    calibration=SimpleNamespace(
                        calibrator_version=current_command.calibrator_version
                    )
                )
            ),
            committed_at=(prior_command.published_at + timedelta(days=1)).isoformat(),
        ),
    )

    assert not decision_case_service._prior_calibration_snapshot_matches(
        prior_event, current_command
    )


def test_prior_calibration_snapshot_ignores_diagnostic_only_model_version_changes() -> None:
    current_command = command()
    prior_command = current_command.model_copy(
        update={
            "knowledge_cutoff": current_command.knowledge_cutoff - timedelta(days=60),
            "published_at": current_command.published_at - timedelta(days=60),
            "recent_diagnostic_records": (
                current_command.recent_diagnostic_records[0].model_copy(
                    update={"raw_score_model_version": "diagnostic-only-version"}
                ),
                *current_command.recent_diagnostic_records[1:],
            ),
        }
    )
    prior_event = cast(
        DecisionEventFact,
        SimpleNamespace(
            case=SimpleNamespace(
                knowledge_cutoff=prior_command.knowledge_cutoff.isoformat(),
                candidate_release=prior_command,
            ),
            result=SimpleNamespace(
                candidate_release=SimpleNamespace(
                    calibration=SimpleNamespace(
                        calibrator_version=current_command.calibrator_version
                    )
                )
            ),
            committed_at=(prior_command.published_at + timedelta(days=1)).isoformat(),
        ),
    )

    assert decision_case_service._prior_calibration_snapshot_matches(prior_event, current_command)


@pytest.mark.parametrize(
    "selection_updates",
    (
        {"calibrator_selection_window_months": command().calibrator_selection_window_months[:12]},
        {
            "calibrator_selection_records": command().calibrator_selection_records[:-1],
        },
        {
            "calibrator_selection_records": (
                command()
                .calibrator_selection_records[0]
                .model_copy(update={"out_of_sample_probability": Decimal("0.51")}),
                *command().calibrator_selection_records[1:],
            ),
        },
    ),
    ids=("window", "member-identity", "frozen-diagnostic-input"),
)
def test_calibrator_selection_evidence_is_immutable_across_refits(
    selection_updates: dict[str, object],
) -> None:
    frozen_command = command()
    changed_command = frozen_command.model_copy(update=selection_updates)

    assert not decision_case_service._same_calibrator_selection_evidence(
        frozen_command, changed_command
    )


def test_selection_evidence_comparison_ignores_regenerated_record_ids() -> None:
    frozen_command = command()
    changed_ids = tuple(
        record.model_copy(update={"record_id": f"regenerated-{index}"})
        for index, record in enumerate(frozen_command.calibrator_selection_records)
    )
    refit_command = frozen_command.model_copy(update={"calibrator_selection_records": changed_ids})

    assert decision_case_service._same_calibrator_selection_evidence(frozen_command, refit_command)


def test_candidate_calibration_source_rows_exclude_other_model_versions() -> None:
    model = frozen_raw_score_model_snapshot()
    previous_model = model.model_copy(update={"model_version": "raw-score-v0"})

    assert (
        decision_case_service._candidate_calibration_source_records(
            previous_model, model.model_version
        )
        == ()
    )
    assert decision_case_service._candidate_calibration_source_records(model, model.model_version)


def test_backdated_label_watermark_cannot_hide_newer_mature_cohort_months() -> None:
    authoritative_months = tuple(
        f"{2040 + index // 12:04}-{index % 12 + 1:02}" for index in range(63)
    )
    older_window = authoritative_months[:60]

    with pytest.raises(decision_case_service.CandidateCalibrationProvenanceInvalid):
        decision_case_service._validate_calibration_training_window(
            older_window, authoritative_months
        )

    decision_case_service._validate_calibration_training_window(
        authoritative_months[-60:], authoritative_months
    )


def test_latest_60_fitting_months_exclude_diagnostics() -> None:
    authoritative_months = tuple(
        f"{2040 + index // 12:04}-{index % 12 + 1:02}" for index in range(96)
    )
    selection_months = authoritative_months[:12]
    fit_months = authoritative_months[12:72]
    diagnostic_months = authoritative_months[-24:]

    decision_case_service._validate_calibration_training_window(
        fit_months,
        authoritative_months,
        selection_window_months=selection_months,
        recent_diagnostic_window_months=diagnostic_months,
    )

    with pytest.raises(decision_case_service.CandidateCalibrationProvenanceInvalid):
        decision_case_service._validate_calibration_training_window(
            authoritative_months[-60:],
            authoritative_months,
            selection_window_months=selection_months,
            recent_diagnostic_window_months=diagnostic_months,
        )


def test_initial_calibration_can_use_the_earliest_qualifying_mature_prefix() -> None:
    authoritative_months = tuple(
        f"{2040 + index // 12:04}-{index % 12 + 1:02}" for index in range(61)
    )

    decision_case_service._validate_calibration_training_window(
        authoritative_months[:60],
        authoritative_months,
        initial_calibration=True,
    )

    with pytest.raises(decision_case_service.CandidateCalibrationProvenanceInvalid):
        decision_case_service._validate_calibration_training_window(
            authoritative_months[-60:],
            authoritative_months,
            initial_calibration=True,
        )


def test_recent_diagnostic_window_uses_latest_cutoff_mature_months() -> None:
    cutoff_mature_months = tuple(
        f"{2037 + index // 12:04}-{index % 12 + 1:02}" for index in range(61)
    )
    earlier_watermark_months = cutoff_mature_months[:-1]

    recent_months = decision_case_service._recent_calibration_month_window(cutoff_mature_months)

    assert recent_months == cutoff_mature_months[-24:]
    assert recent_months != earlier_watermark_months[-24:]


def test_recent_diagnostics_use_cutoff_mature_labels_after_fit_watermark() -> None:
    original = command()
    diagnostic_month = "2045-12"
    raw_score_frozen_at = datetime(2045, 12, 31, 7, tzinfo=UTC)
    entry = _raw_score_evaluation_entry_at(
        raw_score_frozen_at,
        original.training_records[0].market_calendar_version,
    )
    entry_window_end = raw_score_entry_window_end(
        entry,
        original.training_records[0].market_calendar_version,
    )
    maturity = six_month_terminal_evaluation_at(
        entry,
        original.training_records[0].market_calendar_version,
    )
    newer_diagnostics = tuple(
        record.model_copy(
            update={
                "record_id": f"synthetic-new-diagnostic-{index:02d}",
                "month": diagnostic_month,
                "source_research_event_id": "synthetic-new-diagnostic-source",
                "raw_score_frozen_at": raw_score_frozen_at,
                "raw_score_training_watermark_at": raw_score_frozen_at - timedelta(days=1),
                "entry_at": entry,
                "entry_window_ends_at": entry_window_end,
                "unified_maturity_at": maturity,
                "label_available_at": maturity,
            }
        )
        for index, record in enumerate(original.training_records[:10])
    )
    diagnostic_window = (*original.recent_diagnostic_window_months[1:], diagnostic_month)
    command_with_earlier_fit_watermark = original.model_copy(
        update={
            "label_watermark_at": datetime(2046, 6, 4, 8, tzinfo=UTC),
            "recent_diagnostic_window_months": diagnostic_window,
            "recent_diagnostic_records": (
                *(
                    record
                    for record in original.recent_diagnostic_records
                    if record.month in diagnostic_window
                ),
                *newer_diagnostics,
            ),
        }
    )

    calibration = candidate_module._fit_calibrator(
        command_with_earlier_fit_watermark,
        recent_diagnostic_records=(*original.recent_diagnostic_records, *newer_diagnostics),
    )
    recent_records = tuple(
        record
        for record in (*original.recent_diagnostic_records, *newer_diagnostics)
        if record.month in calibration.recent_diagnostic_months
    )

    assert calibration.recent_diagnostic_months[-1] == diagnostic_month
    assert calibration.recent_diagnostic_sample_count == 240
    assert calibration.recent_diagnostics == candidate_module._calibration_diagnostics(
        recent_records,
        tuple(float(record.out_of_sample_probability) for record in recent_records),
    )


def test_closed_post_commit_diagnostic_alert_preserves_original_qualification() -> None:
    committed_at = datetime(2046, 7, 1, 8, tzinfo=UTC)

    class Scope:
        def same_scope_as(self, other: object) -> bool:
            return self is other

    scope = Scope()
    version = object()
    authorization = object()
    alert = SimpleNamespace(
        evidence_id="synthetic-diagnostic-alert",
        kind="DIAGNOSTIC_ALERT",
        available_at=committed_at + timedelta(seconds=1),
    )
    closure = SimpleNamespace(
        alert_evidence_id=alert.evidence_id,
        resolution="DISAPPEARED",
        terminates_alert=True,
        evidence=SimpleNamespace(available_at=committed_at + timedelta(seconds=2)),
    )
    frozen_record = SimpleNamespace(
        decision_id="qualification-base",
        previous_decision_id=None,
        recorded_at=committed_at - timedelta(days=1),
        scope=scope,
        version=version,
        authorization_id="authorization-1",
        authorization_evidence=authorization,
        authorization_terminated_at=None,
        formal_evidence=authorization,
        formal_passing_evidence=authorization,
        restrictions=(),
        restoration_evidence=(),
        state_activity_evidence=None,
        status="VALID",
        cause="QUALIFICATION_PASS",
        alerts=(),
        alert_closures=(),
        outstanding_alerts=(),
        market_state="BULL",
    )
    at_risk_record = SimpleNamespace(
        **{
            **vars(frozen_record),
            "decision_id": "qualification-at-risk",
            "previous_decision_id": frozen_record.decision_id,
            "recorded_at": committed_at + timedelta(seconds=1),
            "status": "AT_RISK",
            "cause": "DIAGNOSTIC_ALERT",
            "alerts": (alert,),
            "outstanding_alerts": (alert,),
        }
    )
    resolved_record = SimpleNamespace(
        **{
            **vars(at_risk_record),
            "decision_id": "qualification-alert-closed",
            "previous_decision_id": at_risk_record.decision_id,
            "recorded_at": committed_at + timedelta(seconds=2),
            "status": "VALID",
            "cause": "ALERT_CLOSED",
            "alert_closures": (closure,),
            "outstanding_alerts": (),
        }
    )
    qualification = SimpleNamespace(
        market_state="BULL",
        status="VALID",
        qualification_id=frozen_record.decision_id,
    )
    command = cast(
        CandidateReleaseCommand,
        SimpleNamespace(qualifications=(qualification,), market_state="BULL"),
    )
    outcome = SimpleNamespace(qualification=qualification, market_state="BULL")
    fact = cast(
        DecisionEventFact,
        SimpleNamespace(
            committed_at=committed_at.isoformat(),
            case=SimpleNamespace(access_scope=object()),
            result=SimpleNamespace(candidate_release=outcome),
        ),
    )
    history = cast(
        tuple[GovernanceOutcome, ...],
        tuple(
            SimpleNamespace(qualification=record)
            for record in (frozen_record, at_risk_record, resolved_record)
        ),
    )

    ignored_diagnostic_ids = decision_case_service._post_commit_diagnostic_alert_ids(
        fact,
        command,
        history,
    )

    assert ignored_diagnostic_ids == (at_risk_record.decision_id, resolved_record.decision_id)


def test_backdated_label_watermark_cannot_hide_cutoff_mature_rows_in_selected_months() -> None:
    months = tuple(f"2040-{month:02d}" for month in range(1, 13))[-2:]
    cutoff_rows = {
        (month, f"SEC-{member:03d}", f"research-{month}-{member:03d}"): object()
        for month in months
        for member in range(500)
    }
    watermark_rows = dict(cutoff_rows)
    watermark_rows.pop((months[-1], "SEC-499", f"research-{months[-1]}-499"))
    submitted = set(watermark_rows)

    with pytest.raises(decision_case_service.CandidateCalibrationProvenanceInvalid):
        decision_case_service._validate_cutoff_complete_calibration_population(
            months,
            cutoff_rows,
            {},
            set(),
            submitted,
        )


def test_partially_matured_candidate_month_does_not_advance_rolling_window() -> None:
    months = tuple(f"2040-{month:02d}" for month in range(1, 13))
    source_identities = {(month, "SOURCE", f"research-{month}") for month in months}
    predictions = {
        (months[-1], "PREDICTED-A", "candidate-a"),
        (months[-1], "PREDICTED-B", "candidate-b"),
    }
    mature_predictions = {(months[-1], "PREDICTED-A", "candidate-a")}

    mature_months = decision_case_service._fully_matured_calibration_months(
        source_identities,
        set(),
        mature_predictions,
        predictions,
        set(),
    )

    assert months[-1] not in mature_months
    assert mature_months == months[:-1]


def test_prediction_maturity_uses_frozen_window_not_trailing_calendar_sessions() -> None:
    original = command()
    final_session = original.market_sessions[-1]
    trailing_offset = timedelta(days=365)
    trailing_session = final_session.model_copy(
        update={
            "market_date": final_session.market_date + trailing_offset,
            "opens_at": final_session.opens_at + trailing_offset,
            "closes_at": final_session.closes_at + trailing_offset,
            "session_sequence": final_session.session_sequence + 1,
        }
    )
    extended = original.model_copy(
        update={"market_sessions": (*original.market_sessions, trailing_session)}
    )
    outcome = freeze_candidate_release(extended)
    member = outcome.members[0]
    identity = (
        extended.knowledge_cutoff.astimezone(UTC).strftime("%Y-%m"),
        member.security_id,
        member.research_id,
    )

    prediction = decision_case_service._frozen_candidate_prediction_rows(
        extended,
        outcome,
        raw_score_frozen_at=extended.knowledge_cutoff,
        raw_score_training_watermark_at=extended.label_watermark_at,
    )[identity]

    assert outcome.valid_market_dates[-1] == final_session.market_date
    assert prediction.matures_by == raw_score_maturity_at(
        final_session.closes_at,
        extended.market_calendar_version,
    )


def test_candidate_member_without_frozen_probability_is_not_added_to_history() -> None:
    original = command()
    outcome = freeze_candidate_release(original)
    failed_member = outcome.members[0].model_copy(update={"calibrated_probability": None})
    failed_outcome = outcome.model_copy(update={"members": (failed_member, *outcome.members[1:])})

    predictions = decision_case_service._frozen_candidate_prediction_rows(
        original,
        failed_outcome,
        raw_score_frozen_at=original.knowledge_cutoff,
        raw_score_training_watermark_at=original.label_watermark_at,
    )

    assert (
        original.knowledge_cutoff.strftime("%Y-%m"),
        failed_member.security_id,
        failed_member.research_id,
    ) not in predictions


def test_probability_bearing_abstained_or_vetoed_member_remains_in_history() -> None:
    original = command()
    outcome = freeze_candidate_release(original)
    member = outcome.members[0].model_copy(update={"candidate": False, "risk_status": "REJECTED"})
    gated_outcome = outcome.model_copy(
        update={"disposition": "RECOMMENDATION_ABSTAINED", "members": (member,)}
    )
    month = original.knowledge_cutoff.astimezone(UTC).strftime("%Y-%m")

    predictions = decision_case_service._frozen_candidate_prediction_rows(
        original,
        gated_outcome,
        raw_score_frozen_at=original.knowledge_cutoff,
        raw_score_training_watermark_at=original.label_watermark_at,
    )

    assert (month, member.security_id, member.research_id) in predictions


def test_previous_frozen_calibration_record_cannot_be_rewritten() -> None:
    record = command().training_records[0]
    retained: dict[tuple[str, str, str], CalibrationRecord] = {}

    decision_case_service._retain_frozen_calibration_record(retained, record)

    with pytest.raises(decision_case_service.CandidateCalibrationProvenanceInvalid):
        decision_case_service._retain_frozen_calibration_record(
            retained,
            record.model_copy(update={"out_of_sample_probability": Decimal("0.7")}),
        )


def test_initial_expanding_window_adds_months_until_record_floor_is_met() -> None:
    original = command()
    history = calibration_history_records()
    history_months = tuple(dict.fromkeys(record.month for record in history))
    months = history_months[24:87]
    records_by_month = {
        month: tuple(record for record in history if record.month == month) for month in months
    }
    # Sixty months at eight records give 480. The three newer fitting months
    # add 27 records, so the first qualifying initial prefix is 63 months.
    reduced_records = tuple(
        record
        for index, month in enumerate(months)
        for record in records_by_month[month][: (8 if index < 60 else 9)]
    )
    candidate = original.model_copy(
        update={
            "training_window_months": months,
            "training_records": reduced_records,
        }
    )

    release = freeze_candidate_release(candidate)

    assert release.disposition == "CANDIDATES", release.reasons
    assert release.calibration is not None
    assert release.calibration.training_window_months == candidate.training_window_months
    assert release.calibration.training_record_count >= 500

    overexpanded_month = history_months[23]
    overexpanded_records = tuple(
        record for record in history if record.month == overexpanded_month
    )[:8]
    overexpanded = freeze_candidate_release(
        candidate.model_copy(
            update={
                "training_window_months": (overexpanded_month, *candidate.training_window_months),
                "training_records": (*overexpanded_records, *candidate.training_records),
            }
        )
    )

    assert overexpanded.disposition == "FAILED"
    assert overexpanded.availability_failure == "CALIBRATION"
    assert overexpanded.reasons == ("CALIBRATION_INITIAL_WINDOW_EXCEEDS_MINIMUM",)


def test_initial_calibration_does_not_expand_past_first_qualifying_prefix() -> None:
    original = command()
    history_months = tuple(dict.fromkeys(record.month for record in calibration_history_records()))
    months = (*original.training_window_months, history_months[84])
    assert len(months) == 61
    counts = (9, *([8] * 32), *([7] * 4), *([9] * 23), 8)
    assert len(counts) == 61
    assert sum(counts[:60]) == 500
    assert sum(counts[1:]) == 499
    assert sum(counts) == 508
    records: list[CalibrationRecord] = []
    for month, count in zip(months, counts, strict=True):
        year, month_number = (int(part) for part in month.split("-"))
        cutoff = datetime(year, month_number, monthrange(year, month_number)[1], 7, tzinfo=UTC)
        entry = cutoff + timedelta(days=5)
        maturity = six_month_terminal_evaluation_at(entry, "synthetic-calendar-v1")
        records.extend(
            _calibration_record(
                month,
                index,
                record_prefix="synthetic-initial-prefix",
                research_prefix="synthetic-initial-prefix",
                security_prefix="SYNTHETIC-INITIAL-PREFIX",
                source_prefix="synthetic-initial-prefix",
                raw_score_frozen_at=cutoff,
                entry_window_ends_at=entry + timedelta(days=1),
                terminal_success=month == months[-1] or index % 2 == 0,
            ).model_copy(
                update={
                    "entry_at": entry,
                    "unified_maturity_at": maturity,
                    "label_available_at": maturity,
                }
            )
            for index in range(count)
        )
    candidate = original.model_copy(
        update={
            "training_window_months": months,
            "training_records": tuple(records),
        }
    )
    initial_prefix_records = tuple(record for record in records if record.month in months[:60])
    initial_prefix = candidate.model_copy(
        update={
            "training_window_months": months[:60],
            "training_records": initial_prefix_records,
        }
    )

    fit_window_release = freeze_candidate_release(initial_prefix)

    assert fit_window_release.disposition == "VALID_NO_CANDIDATES"
    assert fit_window_release.calibration is not None
    calibration = fit_window_release.calibration
    assert calibration.training_window_months == months[:60]
    assert calibration.recent_diagnostic_months == original.recent_diagnostic_window_months
    assert calibration.recent_diagnostic_sample_count == len(original.recent_diagnostic_records)

    expected_recent_records = original.recent_diagnostic_records
    latest_diagnostic_release = freeze_candidate_release(
        initial_prefix,
        published_at=initial_prefix.published_at,
        initial_calibration=True,
        recent_diagnostic_records=original.recent_diagnostic_records,
    )
    assert latest_diagnostic_release.disposition == "VALID_NO_CANDIDATES"
    assert latest_diagnostic_release.calibration is not None
    with_latest_diagnostics = latest_diagnostic_release.calibration
    assert with_latest_diagnostics.recent_diagnostic_months == (
        original.recent_diagnostic_window_months
    )
    assert with_latest_diagnostics.recent_diagnostic_sample_count == len(expected_recent_records)
    assert with_latest_diagnostics.recent_diagnostics == candidate_module._calibration_diagnostics(
        expected_recent_records,
        tuple(float(record.out_of_sample_probability) for record in expected_recent_records),
    )
    omitted_latest_mature_month = candidate.model_copy(
        update={"training_window_months": months[:60]}
    )
    rolling_release = freeze_candidate_release(
        omitted_latest_mature_month,
        initial_calibration=False,
    )
    assert rolling_release.disposition == "FAILED"
    assert rolling_release.availability_failure == "CALIBRATION"
    assert rolling_release.reasons == ("CALIBRATION_TRAINING_WINDOW_NOT_LATEST",)

    expanded_initial_release = freeze_candidate_release(candidate)
    assert expanded_initial_release.disposition == "FAILED"
    assert expanded_initial_release.availability_failure == "CALIBRATION"
    assert expanded_initial_release.reasons == ("CALIBRATION_INITIAL_WINDOW_EXCEEDS_MINIMUM",)
