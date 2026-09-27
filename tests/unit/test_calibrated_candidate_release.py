import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

import pytest

import stock_profiler.modules.candidate_selection.calibrated_candidates as candidate_module
from stock_profiler.modules.candidate_selection.calibrated_candidates import (
    CalibrationRecord,
    CandidateInput,
    CandidateReleaseCommand,
    MarketSession,
    MarketStateQualification,
    freeze_candidate_release,
)


def training_records() -> tuple[CalibrationRecord, ...]:
    records: list[CalibrationRecord] = []
    months = tuple(f"{year}-{month:02d}" for year in range(2041, 2046) for month in range(1, 13))
    for month in months:
        for member in range(10):
            records.append(
                CalibrationRecord(
                    record_id=f"synthetic-label-{month}-{member:02d}",
                    month=month,
                    raw_success_score=Decimal(member) / Decimal("10"),
                    terminal_success=member >= 5,
                    label_available_at=datetime(2046, 6, 30, 23, 59, 59, tzinfo=UTC),
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
    sessions = tuple(
        MarketSession(
            market_date=datetime(2046, 7, day, tzinfo=UTC).date(),
            opens_at=datetime(2046, 7, day, 1, tzinfo=UTC),
            closes_at=datetime(2046, 7, day, 8, tzinfo=UTC),
        )
        for day in range(2, 7)
    )
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
        market_state="BULL",
        market_calendar_version="synthetic-calendar-v1",
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
        training_window_months=tuple(
            f"{year}-{month:02d}" for year in range(2041, 2046) for month in range(1, 13)
        ),
        training_records=training_records(),
        candidates=(
            CandidateInput(
                security_id="SYNTH-ALPHA",
                research_id="synthetic-research-alpha",
                raw_success_score=Decimal(score),
                data_complete=True,
                risk_status="ACCEPTED",
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
    release = freeze_candidate_release(command())

    assert release.disposition == "CANDIDATES"
    assert release.calibration is not None
    assert release.calibration.label_watermark_at == datetime(2046, 7, 1, 8, tzinfo=UTC)
    assert release.calibration.training_record_count == 600
    assert release.members[0].calibrated_probability is not None
    assert release.members[0].valid_market_dates == (
        datetime(2046, 7, 2, 1, tzinfo=UTC).date(),
        datetime(2046, 7, 3, 1, tzinfo=UTC).date(),
        datetime(2046, 7, 4, 1, tzinfo=UTC).date(),
        datetime(2046, 7, 5, 1, tzinfo=UTC).date(),
        datetime(2046, 7, 6, 1, tzinfo=UTC).date(),
    )


def test_probability_below_threshold_forms_valid_no_candidate() -> None:
    release = freeze_candidate_release(command(score="0.1"))

    assert release.disposition == "VALID_NO_CANDIDATES"
    assert release.members[0].candidate is False
    assert "PROBABILITY_BELOW_THRESHOLD" in release.members[0].reasons


def test_unqualified_market_state_abstains_without_lowering_probability_gate() -> None:
    release = freeze_candidate_release(command(state_status="NOT_OBTAINED"))

    assert release.disposition == "RECOMMENDATION_ABSTAINED"
    assert release.members[0].candidate is False
    assert "MARKET_STATE_NOT_QUALIFIED" in release.members[0].reasons


def test_risk_veto_remains_independent_of_research_probability() -> None:
    candidate = command().model_copy(
        update={
            "candidates": (command().candidates[0].model_copy(update={"risk_status": "REJECTED"}),)
        }
    )

    release = freeze_candidate_release(candidate)

    assert release.disposition == "VALID_NO_CANDIDATES"
    assert "INDEPENDENT_RISK_VETO" in release.members[0].reasons


def test_incomplete_candidate_data_fails_the_whole_release() -> None:
    candidate = command()
    incomplete = candidate.candidates[0].model_copy(update={"data_complete": False})
    candidate = candidate.model_copy(update={"candidates": (incomplete,)})

    release = freeze_candidate_release(candidate)

    assert release.disposition == "FAILED"
    assert release.availability_failure == "DATA"
    assert release.members == ()


def test_fewer_than_five_post_cutoff_sessions_fail_without_moving_window() -> None:
    candidate = command()
    before_cutoff_sessions = tuple(
        MarketSession(
            market_date=datetime(2046, 6, day, tzinfo=UTC).date(),
            opens_at=datetime(2046, 6, day, 1, tzinfo=UTC),
            closes_at=datetime(2046, 6, day, 8, tzinfo=UTC),
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
        )
    with pytest.raises(ValueError, match="must close after it opens"):
        MarketSession(
            market_date=datetime(2046, 7, 2, tzinfo=UTC).date(),
            opens_at=datetime(2046, 7, 2, 8, tzinfo=UTC),
            closes_at=datetime(2046, 7, 2, 8, tzinfo=UTC),
        )


def test_release_command_rejects_inconsistent_frozen_inputs() -> None:
    original = command()
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

    outside = original.training_records[0].model_copy(update={"month": "2040-12"})
    release = freeze_candidate_release(
        original.model_copy(update={"training_records": (outside, *original.training_records[1:])})
    )
    assert "TRAINING_RECORD_OUTSIDE_WINDOW" in release.reasons

    immature = original.training_records[0].model_copy(
        update={"label_available_at": datetime(2041, 1, 31, 23, 59, 59, tzinfo=UTC)}
    )
    release = freeze_candidate_release(
        original.model_copy(update={"training_records": (immature, *original.training_records[1:])})
    )
    assert "LABEL_BEFORE_UNIFIED_SIX_MONTH_MATURITY" in release.reasons

    incomplete = tuple(record for record in original.training_records if record.month != "2041-01")
    release = freeze_candidate_release(original.model_copy(update={"training_records": incomplete}))
    assert "CALIBRATION_TRAINING_MONTHS_INCOMPLETE" in release.reasons

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
    assert "CALIBRATION_TRAINING_WATERMARK_NOT_MET" in release.reasons

    single_class = tuple(
        record.model_copy(update={"terminal_success": False})
        for record in original.training_records
    )
    release = freeze_candidate_release(
        original.model_copy(update={"training_records": single_class})
    )
    assert "CALIBRATION_TRAINING_WATERMARK_NOT_MET" in release.reasons


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

    assert release.disposition == "VALID_NO_CANDIDATES"
    assert "INDEPENDENT_RISK_UNAVAILABLE" in release.members[0].reasons


def test_firth_solver_keeps_the_slope_nonnegative_for_inverse_training_signal() -> None:
    inverse = tuple(
        record.model_copy(update={"terminal_success": record.raw_success_score < Decimal("0.5")})
        for record in training_records()
    )

    _, slope = candidate_module._firth_logistic(inverse)

    assert slope >= 0


def test_firth_solver_bounds_its_iteration_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(candidate_module, "_sigmoid", lambda _: 0.5)
    monkeypatch.setattr(math, "log1p", lambda _: 0.0)

    intercept, slope = candidate_module._firth_logistic(training_records())

    assert intercept < 0
    assert slope > 0


def test_firth_solver_shrinks_rejected_steps_and_stops_at_the_scale_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_log1p = math.log1p

    def reject_nonzero_steps(value: float) -> float:
        if value == 1.0:
            return original_log1p(value)
        raise ValueError("synthetic invalid trial")

    monkeypatch.setattr(math, "log1p", reject_nonzero_steps)

    intercept, slope = candidate_module._firth_logistic(training_records())

    assert intercept == 0
    assert slope == 0


def test_firth_solver_rejects_nonfinite_final_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    original_isfinite = math.isfinite
    calls = 0

    def finite_scores_only(value: float) -> bool:
        nonlocal calls
        calls += 1
        if calls <= len(training_records()):
            return original_isfinite(value)
        return False

    monkeypatch.setattr(math, "isfinite", finite_scores_only)
    with pytest.raises(ValueError, match="CALIBRATION_FIT_FAILED"):
        candidate_module._firth_logistic(training_records())


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
    assert "LABEL_NOT_MATURE_AT_WATERMARK" in release.reasons


def test_expired_late_publication_keeps_original_window_and_records_failure() -> None:
    release = freeze_candidate_release(command(published=datetime(2046, 7, 7, 9, tzinfo=UTC)))

    assert release.disposition == "FAILED"
    assert "PUBLICATION_AFTER_CANDIDATE_WINDOW" in release.reasons
    assert release.valid_market_dates[-1].isoformat() == "2046-07-06"


def test_training_window_must_be_exactly_sixty_consecutive_mature_months() -> None:
    candidate = command().model_copy(update={"training_window_months": ("2045-01",)})

    release = freeze_candidate_release(candidate)

    assert release.disposition == "FAILED"
    assert "CALIBRATION_REQUIRES_60_CONSECUTIVE_MATURE_MONTHS" in release.reasons
