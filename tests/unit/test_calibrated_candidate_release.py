import math
import random
from calendar import monthrange
from datetime import UTC, datetime, timedelta
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
    candidate_release_availability_failure,
    finalize_candidate_release_publication,
    freeze_candidate_release,
)


def training_records() -> tuple[CalibrationRecord, ...]:
    records: list[CalibrationRecord] = []
    months = tuple(f"{year}-{month:02d}" for year in range(2040, 2046) for month in range(1, 13))[
        :-1
    ][-60:]
    for month in months:
        year, month_number = (int(part) for part in month.split("-"))
        month_end = datetime(year, month_number, monthrange(year, month_number)[1], 8, tzinfo=UTC)
        raw_score_frozen_at = month_end - timedelta(hours=1)
        entry_window_ends_at = month_end + timedelta(days=5)
        maturity = _six_month_anniversary(entry_window_ends_at)
        for member in range(10):
            records.append(
                CalibrationRecord(
                    record_id=f"synthetic-label-{month}-{member:02d}",
                    month=month,
                    source_research_event_id=f"synthetic-research-event-{month}",
                    security_id=f"SYNTH-SECURITY-{member:02d}",
                    research_id=f"synthetic-research-{month}-{member:02d}",
                    raw_score_model_version="synthetic-elastic-net-v1",
                    raw_score_frozen_at=raw_score_frozen_at,
                    raw_score_training_watermark_at=raw_score_frozen_at - timedelta(days=1),
                    raw_success_score=Decimal(member) / Decimal("10"),
                    terminal_success=member >= 5,
                    entry_window_ends_at=entry_window_ends_at,
                    entry_at=entry_window_ends_at - timedelta(days=1),
                    unified_maturity_at=maturity,
                    label_available_at=maturity,
                )
            )
    return tuple(records)


def weak_positive_signal_records() -> tuple[CalibrationRecord, ...]:
    generator = random.Random(10417)
    threshold = generator.random()
    strength = generator.uniform(-3, 3)
    records: list[CalibrationRecord] = []
    months = tuple(f"{year}-{month:02d}" for year in range(2040, 2046) for month in range(1, 13))[
        :-1
    ][-60:]
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
        maturity = _six_month_anniversary(entry_window_ends_at)
        for member in range(10):
            score = Decimal(member) / Decimal("10")
            probability = 1 / (1 + math.exp(-strength * (float(score) - threshold)))
            records.append(
                CalibrationRecord(
                    record_id=f"positive-label-{month}-{member:02d}",
                    month=month,
                    source_research_event_id=f"synthetic-positive-research-event-{month}",
                    security_id=f"SYNTH-POSITIVE-{member:02d}",
                    research_id=f"synthetic-positive-research-{month}-{member:02d}",
                    raw_score_model_version="synthetic-elastic-net-v1",
                    raw_score_frozen_at=raw_score_frozen_at,
                    raw_score_training_watermark_at=raw_score_frozen_at - timedelta(days=1),
                    raw_success_score=score,
                    terminal_success=generator.random() < probability,
                    entry_window_ends_at=entry_window_ends_at,
                    entry_at=entry_window_ends_at - timedelta(days=1),
                    unified_maturity_at=maturity,
                    label_available_at=maturity,
                )
            )
    return tuple(records)


def _six_month_anniversary(value: datetime) -> datetime:
    month_index = value.year * 12 + value.month - 1 + 6
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    return value.replace(
        year=year,
        month=month,
        day=min(value.day, monthrange(year, month)[1]),
    )


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
            session_sequence=99 + day,
        )
        for day in range(2, 7)
    )
    records = training_records()
    months = tuple(dict.fromkeys(record.month for record in records))
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
        last_completed_market_session_sequence=100,
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
        training_window_months=months,
        training_records=records,
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
    assert "MARKET_STATE_QUALIFICATION_AT_RISK" in release.members[0].reasons


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
    assert "MARKET_STATE_NOT_QUALIFIED" in finalized.reasons
    assert "MARKET_STATE_QUALIFICATION_EXPIRED" not in finalized.reasons


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


def test_host_availability_failure_preserves_calendar_failures() -> None:
    original = command()
    incomplete = candidate_release_availability_failure(
        original.model_copy(update={"market_sessions": original.market_sessions[:4]}),
        published_at=original.published_at,
        reason="CANDIDATE_QUALIFICATION_VERSION_MISMATCH",
        availability_failure="VERSION",
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
        availability_failure="VERSION",
    )

    assert incomplete.availability_failure == "DATA"
    assert incomplete.reasons == ("CANDIDATE_WINDOW_CALENDAR_INCOMPLETE",)
    assert invalid_sequence.availability_failure == "DATA"
    assert invalid_sequence.reasons == ("CANDIDATE_WINDOW_CALENDAR_SEQUENCE_INVALID",)


def test_entry_invalid_record_is_a_mature_negative_calibration_label() -> None:
    original = command()
    records = tuple(
        record.model_copy(update={"entry_at": None, "terminal_success": False})
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
        update={"entry_window_ends_at": datetime(2040, 12, 31, 8, tzinfo=UTC)}
    )
    release = freeze_candidate_release(
        original.model_copy(
            update={
                "training_records": (window_without_future_entry, *original.training_records[1:])
            }
        )
    )
    assert "CALIBRATION_ENTRY_WINDOW_PRECEDES_PREDICTION_MONTH" in release.reasons

    entry_outside_window = original.training_records[0].model_copy(
        update={"entry_at": original.training_records[0].entry_window_ends_at}
    )
    release = freeze_candidate_release(
        original.model_copy(
            update={"training_records": (entry_outside_window, *original.training_records[1:])}
        )
    )
    assert "CALIBRATION_EXECUTABLE_ENTRY_OUTSIDE_ENTRY_WINDOW" in release.reasons

    outside_month = "2040-11"
    outside_month_records = tuple(
        record.model_copy(
            update={
                "record_id": f"synthetic-label-{outside_month}-{index:02d}",
                "month": outside_month,
                "source_research_event_id": f"synthetic-research-event-{outside_month}",
                "raw_score_frozen_at": datetime(2040, 11, 30, 7, tzinfo=UTC),
                "raw_score_training_watermark_at": datetime(2040, 11, 29, 7, tzinfo=UTC),
                "entry_window_ends_at": datetime(2040, 12, 6, 8, tzinfo=UTC),
                "entry_at": datetime(2040, 12, 5, 8, tzinfo=UTC),
                "unified_maturity_at": datetime(2041, 6, 6, 8, tzinfo=UTC),
                "label_available_at": datetime(2041, 6, 6, 8, tzinfo=UTC),
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
        )
    )
    assert "CALIBRATION_TRAINING_WINDOW_NOT_LATEST_MATURE_MONTHS" in release.reasons

    immature = original.training_records[0].model_copy(
        update={"label_available_at": datetime(2041, 1, 31, 23, 59, 59, tzinfo=UTC)}
    )
    release = freeze_candidate_release(
        original.model_copy(update={"training_records": (immature, *original.training_records[1:])})
    )
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
    assert "CALIBRATION_REQUIRES_60_MATURE_MONTHS" in release.reasons

    bad_entry_maturity = original.training_records[-1].model_copy(
        update={
            "unified_maturity_at": original.training_records[-1].unified_maturity_at
            - timedelta(days=1),
            "label_available_at": original.training_records[-1].label_available_at
            - timedelta(days=1),
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


def test_firth_solver_rejects_training_scores_without_temporal_provenance() -> None:
    original = command()
    first = original.training_records[0].model_copy(
        update={"raw_score_training_watermark_at": original.training_records[0].raw_score_frozen_at}
    )

    release = freeze_candidate_release(
        original.model_copy(update={"training_records": (first, *original.training_records[1:])})
    )

    assert release.availability_failure == "CALIBRATION"
    assert release.reasons == ("CALIBRATION_RAW_SCORE_NOT_OUT_OF_SAMPLE",)


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
    assert "CALIBRATION_REQUIRES_60_MATURE_MONTHS" in release.reasons


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


def test_training_window_must_be_exactly_sixty_consecutive_mature_months() -> None:
    candidate = command().model_copy(update={"training_window_months": ("2045-01",)})

    release = freeze_candidate_release(candidate)

    assert release.disposition == "FAILED"
    assert "CALIBRATION_REQUIRES_60_MATURE_MONTHS" in release.reasons


def test_training_window_must_use_latest_mature_months() -> None:
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
                "unified_maturity_at": datetime(2046, 7, 5, 8, tzinfo=UTC),
                "label_available_at": datetime(2046, 7, 5, 8, tzinfo=UTC),
            }
        )
        for index in range(10)
    )
    later_calendar = tuple(
        MarketSession(
            market_date=datetime(2046, 7, day, tzinfo=UTC).date(),
            opens_at=datetime(2046, 7, day, 1, tzinfo=UTC),
            closes_at=datetime(2046, 7, day, 8, tzinfo=UTC),
            session_sequence=day + 100,
        )
        for day in range(11, 16)
    )
    candidate = original.model_copy(
        update={
            "knowledge_cutoff": datetime(2046, 7, 10, 8, tzinfo=UTC),
            "published_at": datetime(2046, 7, 11, 2, tzinfo=UTC),
            "last_completed_market_session_sequence": 110,
            "label_watermark_at": datetime(2046, 7, 10, 8, tzinfo=UTC),
            "training_records": (*original.training_records, *next_month_records),
            "market_sessions": later_calendar,
        }
    )

    release = freeze_candidate_release(candidate)

    assert release.disposition == "FAILED"
    assert release.availability_failure == "CALIBRATION"
    assert "CALIBRATION_TRAINING_WINDOW_NOT_LATEST_MATURE_MONTHS" in release.reasons


def test_latest_sixty_mature_months_reject_calendar_gaps() -> None:
    original = command()
    missing_month = "2042-01"
    earlier_month = "2040-11"
    earlier_records = tuple(
        record.model_copy(
            update={
                "record_id": f"synthetic-label-{earlier_month}-{index:02d}",
                "month": earlier_month,
                "source_research_event_id": f"synthetic-research-event-{earlier_month}",
                "raw_score_frozen_at": datetime(2040, 11, 30, 7, tzinfo=UTC),
                "raw_score_training_watermark_at": datetime(2040, 11, 29, 7, tzinfo=UTC),
                "entry_window_ends_at": datetime(2040, 12, 6, 8, tzinfo=UTC),
                "entry_at": datetime(2040, 12, 5, 8, tzinfo=UTC),
                "unified_maturity_at": datetime(2041, 6, 6, 8, tzinfo=UTC),
                "label_available_at": datetime(2041, 6, 6, 8, tzinfo=UTC),
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

    assert release.disposition == "FAILED"
    assert release.availability_failure == "CALIBRATION"
    assert "CALIBRATION_TRAINING_WINDOW_NOT_LATEST_MATURE_MONTHS" in release.reasons
