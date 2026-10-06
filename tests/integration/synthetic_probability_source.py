"""Original fictional temporal calibration fixture with a fixed generator and seed."""

from calendar import monthrange
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from stock_profiler.modules.candidate_selection.calibrated_candidates import (
    CalibrationRecord,
    CandidateInput,
    CandidateReleaseCommand,
    CandidateRiskGate,
    MarketSession,
    MarketStateQualification,
)
from stock_profiler.modules.portfolio.market_calendar import (
    six_month_terminal_evaluation_at,
    synthetic_market_calendar,
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
