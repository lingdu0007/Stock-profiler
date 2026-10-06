"""Preregistered evaluation of complete immutable monthly probability evidence."""

from datetime import UTC, datetime
from decimal import Decimal

from stock_profiler.foundation.decision_versions import (
    CURRENT_M_AGENT_RELEASE,
    DecisionCaseVersionBundle,
)
from stock_profiler.modules.evaluation.probability_contracts import (
    ProbabilityMember,
    ProbabilityMonth,
    ProbabilityRegistration,
)
from stock_profiler.modules.evaluation.probability_inference import summarize_probability_history


def registration() -> ProbabilityRegistration:
    return ProbabilityRegistration(
        version_id="fictional-probability-inference-v1",
        start_month="2034-01",
        end_month="2048-12",
        source_version_bundle=DecisionCaseVersionBundle(
            case_contract_version="candidate-release.1.0.0",
            host_contract_version="candidate-release.1.0.0",
            host_application_version="0.1.0.dev0",
            host_source_sha="a" * 40,
            agent_definition_id="fictional",
            agent_definition_version="2.0.0",
            model_adapter_id="fictional",
            routing_policy_version="fictional",
            output_contract_version="1.0.0",
            report_projection_contract_version="candidate-release.1.0.0",
            **CURRENT_M_AGENT_RELEASE.model_dump(),
        ),
        capability_version="fictional-probability-v1",
        raw_score_model_version="synthetic-elastic-net-v1",
        selection_strategy_version="synthetic-dual-head-strategy-v1",
        minimum_availability=Decimal(".95"),
        market_calendar_version="synthetic-calendar-v1",
        calibrator_version="monotone-firth-logistic-v1",
        high_band_threshold=Decimal(".8"),
        success_floor=Decimal(".8"),
        overconfidence_ceiling=Decimal(".05"),
        coverage_floor=Decimal(".5"),
        overall_months=120,
        overall_high_band_records=500,
        overall_nonoverlapping_windows=20,
        regime_months=30,
        regime_high_band_records=100,
        regime_nonoverlapping_windows=5,
        regime_periods=2,
        block_lengths=(6, 9, 12),
        confidence=Decimal(".95"),
        bootstrap_repetitions=199,
    )


def history(
    *, probability: str = ".85", failures: int = 0, candidates: bool = True
) -> tuple[ProbabilityMonth, ...]:
    rows = []
    for i in range(180):
        year, month = 2034 + i // 12, i % 12 + 1
        end_index = year * 12 + month - 1 + 6
        maturity = datetime(end_index // 12, end_index % 12 + 1, 15, tzinfo=UTC)
        members = tuple(
            ProbabilityMember(
                evaluation_id=f"fictional:{i}:{j}",
                source_event_id=f"fictional-month-{i}",
                security_id=f"FICTIONAL-{j}",
                raw_success_score=Decimal(j),
                frozen_probability=Decimal(probability),
                matures_at=maturity,
                state="NOT_ACHIEVED" if j < failures else "ACHIEVED",
                entry_expired=False,
            )
            for j in range(10)
        )
        rows.append(
            ProbabilityMonth(
                plan_month=f"{year}-{month:02}",
                disposition="FROZEN",
                source_event_id=f"fictional-month-{i}",
                cutoff_at=datetime(year, month, 1, tzinfo=UTC),
                matures_at=maturity,
                regime=("BULL", "BEAR", "SIDEWAYS")[i % 3],
                label_mature=True,
                valid_monthly=True,
                has_candidates=candidates,
                members=members,
            )
        )
    return tuple(rows)


def test_high_band_requires_both_one_sided_gates_on_common_calendar_blocks() -> None:
    result = summarize_probability_history(
        history(probability=".99", failures=1), registration(), seed=2121
    )
    assert result.overall.sufficient
    assert result.gates["overall:success"].passed is True
    assert result.gates["overall:overconfidence"].passed is False
    assert result.gates["overall:overconfidence"].direction == "UPPER"
    assert result.gates["overall:overconfidence"].bound == Decimal(".09")
    assert result.overall.disposition == "FAILED"
    assert set(result.resampling) == {"6", "9", "12"}
    assert all(r["calendar_months"] == 180 for r in result.resampling.values())


def test_conservative_predictions_pass_without_symmetric_penalty_but_require_coverage() -> None:
    result = summarize_probability_history(history(), registration(), seed=2121)
    assert result.overall.disposition == "PASSED"
    assert result.gates["overall:overconfidence"].bound == Decimal("-.15")
    assert all(state.disposition == "PASSED" for state in result.regimes.values())
    uncovered = summarize_probability_history(history(candidates=False), registration(), seed=2121)
    assert uncovered.gates["coverage"].estimate == 0
    assert uncovered.overall.disposition == "FAILED"


def test_regimes_keep_independent_watermarks_without_shrinking_sampling_timeline() -> None:
    rows = tuple(row.model_copy(update={"regime": "BULL"}) for row in history())
    result = summarize_probability_history(rows, registration(), seed=2121)
    assert result.overall.disposition == "PASSED"
    assert result.regimes["BULL"].disposition == "INSUFFICIENT"  # only one contiguous period
    assert result.regimes["BEAR"].high_band_records == 0
    assert result.regimes["BEAR"].disposition == "INSUFFICIENT"
    assert result.gates["BEAR:success"].bound is None
    assert all(block["calendar_months"] == 180 for block in result.resampling.values())


def test_record_counts_do_not_replace_mature_months_or_nonoverlapping_windows() -> None:
    short = summarize_probability_history(history()[:119], registration(), seed=2121)
    assert short.overall.high_band_records == 1190
    assert short.overall.disposition == "INSUFFICIENT"
    overlapping = tuple(
        row.model_copy(update={"matures_at": datetime(2050, 1, 1, tzinfo=UTC)}) for row in history()
    )
    insufficient = summarize_probability_history(overlapping, registration(), seed=2121)
    assert insufficient.overall.mature_months == 180
    assert insufficient.overall.nonoverlapping_windows == 1
    assert insufficient.overall.disposition == "INSUFFICIENT"


def test_missing_outcomes_are_indeterminate_and_failed_months_do_not_count_as_abstentions() -> None:
    rows = history()
    missing = rows[0].model_copy(
        update={
            "members": (
                rows[0].members[0].model_copy(update={"state": "UNAVAILABLE"}),
                *rows[0].members[1:],
            )
        }
    )
    result = summarize_probability_history((missing, *rows[1:]), registration(), seed=2121)
    assert result.overall.disposition == "INDETERMINATE"
    failure = rows[0].model_copy(
        update={
            "disposition": "FAILED",
            "valid_monthly": False,
            "has_candidates": False,
            "members": (),
            "availability_failure": "CALIBRATION",
        }
    )
    result = summarize_probability_history((failure, *rows[1:]), registration(), seed=2121)
    assert result.gates["coverage"].estimate == 1


def test_full_population_log_loss_and_recent_diagnostics_do_not_fit_production() -> None:
    result = summarize_probability_history(history(), registration(), seed=2121)
    assert result.diagnostics is not None
    assert result.diagnostics.log_loss == Decimal("0.16251893")
    assert result.diagnostics.brier_score == Decimal("0.02250000")
    assert result.recent_diagnostic_months == tuple(row.plan_month for row in history()[-24:])
    assert result.recent_diagnostic_sample_count == 240
    assert result.recent_diagnostics is not None


def test_recent_diagnostic_window_never_reaches_back_around_missing_labels() -> None:
    rows = history()
    changed = rows[-1].model_copy(
        update={
            "members": (
                rows[-1].members[0].model_copy(update={"state": "UNAVAILABLE"}),
                *rows[-1].members[1:],
            )
        }
    )
    result = summarize_probability_history((*rows[:-1], changed), registration(), seed=2121)
    assert result.recent_diagnostic_months == tuple(row.plan_month for row in rows[-24:])
    assert result.recent_diagnostics is None


def test_preregistration_cannot_lower_the_fixed_qualification_watermarks() -> None:
    import pytest
    from pydantic import ValidationError

    for field, value in {
        "high_band_threshold": ".9",
        "success_floor": ".79",
        "overconfidence_ceiling": ".06",
        "coverage_floor": ".49",
        "minimum_availability": ".94",
        "overall_months": 119,
        "overall_high_band_records": 499,
        "overall_nonoverlapping_windows": 19,
        "regime_months": 29,
        "regime_high_band_records": 99,
        "regime_nonoverlapping_windows": 4,
        "regime_periods": 1,
        "confidence": ".9",
    }.items():
        with pytest.raises(ValidationError):
            ProbabilityRegistration.model_validate(registration().model_dump() | {field: value})


def test_unknown_selection_state_cannot_be_omitted_from_state_qualification() -> None:
    rows = history()
    unknown = rows[0].model_copy(update={"regime": None})
    result = summarize_probability_history((unknown, *rows[1:]), registration(), seed=2121)
    assert result.overall.disposition == "PASSED"
    assert all(state.disposition == "INDETERMINATE" for state in result.regimes.values())


def test_pending_second_state_episode_cannot_supply_historical_evidence() -> None:
    rows = history()
    changed = tuple(row.model_copy(update={"regime": "BULL"}) for row in rows[:120])
    separation = tuple(row.model_copy(update={"regime": "BEAR"}) for row in rows[120:150])
    pending = tuple(
        row.model_copy(
            update={
                "regime": "BULL",
                "label_mature": False,
                "members": tuple(
                    member.model_copy(update={"state": "PENDING"}) for member in row.members
                ),
            }
        )
        for row in rows[150:]
    )
    result = summarize_probability_history(
        (*changed, *separation, *pending), registration(), seed=2121
    )
    assert result.regimes["BULL"].periods == 1
    assert result.regimes["BULL"].disposition == "INSUFFICIENT"


def test_month_watermark_waits_for_the_complete_registered_entry_window() -> None:
    rows = history()
    early = rows[-1].model_copy(update={"label_mature": False})
    result = summarize_probability_history((*rows[:-1], early), registration(), seed=2121)
    assert result.overall.mature_months == 179
