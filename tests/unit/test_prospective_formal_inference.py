"""Prospective conjunction uses frozen labels, paired counts and due state gates."""

from decimal import Decimal
from fractions import Fraction

from test_historical_inference import history
from test_prospective_formal_nodes import formal_policy

from stock_profiler.modules.evaluation.contracts import EvaluationMember
from stock_profiler.modules.evaluation.historical_contracts import HistoricalMonthResult
from stock_profiler.modules.prospective.contracts import FormalMonth
from stock_profiler.modules.prospective.formal_inference import infer_prospective


def months(count: int) -> tuple[FormalMonth, ...]:
    result = []
    for index, cohort in enumerate(history(count)):
        assert cohort.matures_at is not None
        result.append(
            FormalMonth(
                cohort=cohort,
                valid_monthly=True,
                evaluation_period_mature=True,
                probabilities=tuple(
                    EvaluationMember(
                        evaluation_id=f"fictional-{index}-{security}",
                        population="PROBABILITY",
                        source_event_id=f"fictional-release-{index}",
                        security_id=f"fictional-{security}",
                        frozen_probability=Decimal(".85") if security < 2 else Decimal(".30"),
                        matures_at=cohort.matures_at,
                        state="NOT_ACHIEVED" if index % 6 == 0 else "ACHIEVED",
                    )
                    for security in range(3)
                ),
            )
        )
    return tuple(result)


def test_first_conjunction_does_not_invent_due_state_qualification() -> None:
    result = infer_prospective(
        months(24),
        formal_policy(),
        high_band_threshold=Decimal(".80"),
        alpha=Fraction(1, 40),
        seed=2291,
    )
    assert set(result.gates) == {
        "overall_pass",
        "overall_drawdown",
        "calibration_success",
        "overconfidence",
        "availability",
        "coverage",
        *(
            f"increment:{baseline}:{field}"
            for baseline in ("UNIVERSE", "RANDOM", "FOUR_FACTOR")
            for field in (
                "positive_rate",
                "target_rate",
                *(("batch_pass",) if baseline != "UNIVERSE" else ()),
            )
        ),
    }
    assert result.calibration_records == 72 and result.high_band_records == 48
    success, excess = result.gates["calibration_success"], result.gates["overconfidence"]
    assert success.estimate is not None and excess.estimate is not None
    assert abs(Fraction(success.estimate) - Fraction(5, 6)) < Fraction(1, 10**120)
    assert abs(Fraction(excess.estimate) - (Fraction(85, 100) - Fraction(5, 6))) < Fraction(
        1, 10**120
    )
    assert success.direction == "LOWER" and excess.direction == "UPPER"
    assert not any(
        item.stock_sufficient or item.probability_sufficient for item in result.regimes.values()
    )
    assert not result.qualified


def test_mature_states_join_the_same_formal_look_and_all_hard_gates() -> None:
    result = infer_prospective(
        months(180),
        formal_policy(),
        high_band_threshold=Decimal(".80"),
        alpha=Fraction(1, 40),
        seed=2291,
    )
    for state in ("BULL", "BEAR", "SIDEWAYS"):
        assert (
            result.regimes[state].stock_sufficient and result.regimes[state].probability_sufficient
        )
        assert {
            f"regime:{state}:batch_pass",
            f"regime:{state}:drawdown_pass",
            f"regime:{state}:calibration_success",
            f"regime:{state}:overconfidence",
        }.issubset(result.gates)
    assert set(result.sampling_digests) == {"6", "9", "12"}
    assert not result.qualified


def test_missing_exact_paired_counts_cannot_be_replaced_by_rounded_rates() -> None:
    original = months(24)
    first = original[0]
    baselines = {
        key: value.model_copy(update={"counts": None})
        for key, value in first.cohort.baselines.items()
    }
    altered = first.model_copy(
        update={"cohort": first.cohort.model_copy(update={"baselines": baselines})}
    )
    result = infer_prospective(
        (altered, *original[1:]),
        formal_policy(),
        high_band_threshold=Decimal(".80"),
        alpha=Fraction(1, 40),
        seed=2291,
    )
    assert result.disposition == "INDETERMINATE"
    assert result.gates["increment:RANDOM:positive_rate"].passed is None


def test_later_context_failure_cannot_drop_an_original_failed_pool() -> None:
    original = months(24)
    first = original[0].model_copy(
        update={
            "cohort": original[0].cohort.model_copy(
                update={
                    "batch_pass": False,
                    "availability_failure": "SYSTEM",
                }
            )
        }
    )
    result = infer_prospective(
        (first, *original[1:]),
        formal_policy(),
        high_band_threshold=Decimal(".80"),
        alpha=Fraction(1, 40),
        seed=2291,
    )
    estimate = result.gates["overall_pass"].estimate
    assert estimate is not None and abs(Fraction(estimate) - Fraction(23, 24)) < Fraction(
        1, 10**120
    )


def test_due_missing_original_probability_records_keep_the_formal_gate_indeterminate() -> None:
    original = months(24)
    missing = FormalMonth(
        cohort=original[0].cohort,
        probabilities=original[0].probabilities[:1],
        missing_high_band_records=1,
    )
    result = infer_prospective(
        (missing, *original[1:]),
        formal_policy(),
        high_band_threshold=Decimal(".80"),
        alpha=Fraction(1, 40),
        seed=2291,
    )
    assert result.gates["calibration_success"].passed is None
    assert result.gates["overconfidence"].passed is None


def test_formal_conjunction_keeps_original_missing_months_and_empty_recommendation_coverage() -> (
    None
):
    original = months(24)
    missing = tuple(
        FormalMonth(
            cohort=HistoricalMonthResult(
                plan_month=f"2049-{index + 1:02}", disposition="UNAVAILABLE"
            ),
            probabilities=(),
        )
        for index in range(12)
    )
    result = infer_prospective(
        (*original, *missing),
        formal_policy(),
        high_band_threshold=Decimal(".80"),
        alpha=Fraction(1, 40),
        seed=2291,
    )
    assert result.gates["availability"].estimate is not None
    assert abs(Fraction(result.gates["availability"].estimate) - Fraction(2, 3)) < Fraction(
        1, 10**120
    )
    assert result.gates["availability"].passed is False
    assert result.gates["coverage"].estimate == 0 and result.gates["coverage"].passed is False
    assert result.disposition != "PASSED"


def test_state_windows_retain_a_registered_terminal_boundary_later_than_source_outcomes() -> None:
    original = months(24)
    delayed = []
    for row in original:
        assert row.cohort.matures_at is not None
        delayed.append(
            row.model_copy(
                update={
                    "cohort": row.cohort.model_copy(update={"regime": "BULL"}),
                    "evaluation_end_at": row.cohort.matures_at.replace(
                        year=row.cohort.matures_at.year + 1
                    ),
                }
            )
        )
    result = infer_prospective(
        tuple(delayed),
        formal_policy(),
        high_band_threshold=Decimal(".80"),
        alpha=Fraction(1, 40),
        seed=2291,
    )
    assert result.regimes["BULL"].stock_windows == result.regimes["BULL"].probability_windows == 2


def test_known_individual_labels_cannot_count_a_still_pending_registered_result_window() -> None:
    pending = tuple(
        row.model_copy(
            update={
                "cohort": row.cohort.model_copy(
                    update={"regime": "BULL", "batch_pass": None, "drawdown_pass": None}
                ),
                "batch_due": False,
                "evaluation_period_mature": False,
            }
        )
        for row in months(24)
    )
    result = infer_prospective(
        pending,
        formal_policy(),
        high_band_threshold=Decimal(".80"),
        alpha=Fraction(1, 40),
        seed=2291,
    )
    assert result.regimes["BULL"].high_band_records == 48
    assert result.regimes["BULL"].probability_windows == 0
