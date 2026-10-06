"""Original fictional temporal calibration fixture with a fixed generator and seed."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from synthetic_calibration import calibration_history_records, training_records

from stock_profiler.modules.candidate_selection.calibrated_candidates import (
    CandidateInput,
    CandidateReleaseCommand,
    CandidateRiskGate,
    MarketSession,
    MarketStateQualification,
)
from stock_profiler.modules.portfolio.market_calendar import (
    synthetic_market_calendar,
)


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
