"""Independent preregistered statistical examples, never investment evidence."""

from datetime import UTC, datetime
from decimal import Decimal

from stock_profiler.modules.evaluation.historical_contracts import (
    BaselineCounts,
    BaselineResult,
    HistoricalMonthResult,
    HistoricalRegistration,
)
from stock_profiler.modules.evaluation.historical_inference import summarize_history


def policy() -> HistoricalRegistration:
    from stock_profiler.modules.decision_cases.frozen_case import load_frozen_case_payload

    return HistoricalRegistration.model_validate(
        {
            "version_id": "fictional-inference/1",
            "start_month": "2033-01",
            "end_month": "2047-12",
            "source_version_bundle": load_frozen_case_payload()["version_bundle"],
            "strategy_version": "fictional-screening/1",
            "market_calendar_version": "synthetic-market-calendar-v1",
            "standard_quantity": "100",
            "selection_policy": {
                "version_id": "fictional-selection/1",
                "cohort_size": 4,
                "industry_limit": 2,
                "capitalization_limit": 2,
                "correlation_sessions": 8,
                "maximum_correlation": "0.8",
                "positive_weight": 3,
                "terminal_weight": 2,
            },
            "positive_members_required": 3,
            "target_members_required": 2,
            "terminal_target": "0.25",
            "maximum_drawdown": "0.15",
            "exploratory_months": 60,
            "formal_months": 120,
            "overall_pass_floor": "0.8",
            "overall_drawdown_floor": "0.8",
            "regime_pass_floor": "0.5",
            "regime_drawdown_floor": "0.6",
            "regime_months": 30,
            "regime_drawdown_months": 24,
            "regime_nonoverlapping_windows": 5,
            "regime_periods": 2,
            "block_lengths": [6, 9, 12],
            "confidence": "0.95",
            "bootstrap_repetitions": 199,
            "random_trials": 37,
            "minimum_availability": "0.95",
        }
    )


def history(count: int) -> tuple[HistoricalMonthResult, ...]:
    rows = []
    for offset in range(count):
        year, month = divmod(2033 * 12 + offset, 12)
        terminal_year, terminal_month = divmod(2033 * 12 + offset + 7, 12)
        baseline = BaselineResult(
            positive_rate=Decimal("0.25"),
            target_rate=Decimal(0),
            batch_pass_rate=Decimal(0),
            drawdown_pass_rate=Decimal(1),
            trial_count=37,
            failed_trials=0,
            membership_digest="0" * 64,
            counts=BaselineCounts(positive=37, target=0, member_slots=148, passed_trials=0),
        )
        rows.append(
            HistoricalMonthResult(
                plan_month=f"{year}-{month + 1:02}",
                disposition="FROZEN",
                selection_event_id=f"fictional-selection-{offset}",
                selection_at=datetime(year, month + 1, 1, tzinfo=UTC),
                matures_at=datetime(terminal_year, terminal_month + 1, 1, tzinfo=UTC),
                members=tuple(f"fictional-{index}" for index in range(4)),
                positive_count=4,
                target_count=4,
                positive_rate=Decimal(1),
                target_rate=Decimal(1),
                batch_pass=True,
                drawdown_pass=True,
                maximum_drawdown=Decimal(0),
                regime=("BULL", "BEAR", "SIDEWAYS")[(offset // 30) % 3],
                baselines={
                    "UNIVERSE": baseline.model_copy(update={"batch_pass_rate": None}),
                    "RANDOM": baseline,
                    "FOUR_FACTOR": baseline,
                },
            )
        )
    return tuple(rows)


def test_sixty_to_one_hundred_nineteen_mature_months_remain_exploratory() -> None:
    result = summarize_history(history(119), policy(), seed=20)
    assert result.disposition == "EXPLORATORY"
    assert result.gates["overall_pass"].lower_bound == Decimal(1)
    assert set(result.gates["overall_pass"].block_bounds) == {"6", "9", "12"}


def test_formal_evidence_needs_all_state_watermarks_and_all_gates() -> None:
    result = summarize_history(history(180), policy(), seed=20)
    assert result.disposition == "PASSED"
    assert all(gate.passed is True for gate in result.gates.values())
    assert result.diagnostics["different_securities"] == 4
    assert result.diagnostics["highest_repetition"] == 180
    assert result.diagnostics["effective_securities"] == "4"


def test_below_exploration_floor_has_no_formal_verdict() -> None:
    assert summarize_history(history(59), policy(), seed=20).disposition == "INSUFFICIENT"


def test_one_hundred_twenty_months_with_one_state_period_are_indeterminate() -> None:
    result = summarize_history(history(120), policy(), seed=20)
    assert result.disposition == "INDETERMINATE"
    assert result.regimes["BEAR"].periods == 1
    assert result.regimes["BEAR"].sufficient is False


def test_failed_hard_gate_cannot_be_replaced_by_strong_member_returns() -> None:
    rows = tuple(row.model_copy(update={"drawdown_pass": False}) for row in history(180))
    result = summarize_history(rows, policy(), seed=20)
    assert result.disposition == "FAILED"
    assert result.gates["overall_pass"].passed is True
    assert result.gates["overall_drawdown"].passed is False


def test_zero_paired_increment_fails_the_strict_gate() -> None:
    rows = tuple(
        row.model_copy(
            update={
                "baselines": {
                    key: value.model_copy(
                        update={
                            "positive_rate": Decimal(1),
                            "counts": BaselineCounts(
                                positive=148, target=0, member_slots=148, passed_trials=0
                            ),
                        }
                    )
                    for key, value in row.baselines.items()
                }
            }
        )
        for row in history(180)
    )
    result = summarize_history(rows, policy(), seed=20)
    assert result.disposition == "FAILED"
    assert result.gates["increment:UNIVERSE:positive_rate"].lower_bound == 0
    assert result.gates["increment:UNIVERSE:positive_rate"].passed is False


def test_calendar_gaps_are_sampled_jointly_and_worst_block_bound_is_used() -> None:
    rows = list(history(180))
    for offset in range(0, 180, 11):
        rows[offset] = rows[offset].model_copy(update={"batch_pass": False})
    rows[17] = HistoricalMonthResult(plan_month=rows[17].plan_month, disposition="SYSTEM_FAILED")
    result = summarize_history(tuple(rows), policy(), seed=20)
    gate = result.gates["overall_pass"]
    assert gate.lower_bound == min(
        value for value in gate.block_bounds.values() if value is not None
    )
    assert all(sample["calendar_months"] == 180 for sample in result.resampling.values())
    assert summarize_history(tuple(rows), policy(), seed=20) == result
    assert summarize_history(tuple(rows), policy(), seed=21).resampling != result.resampling
    assert all(
        len(gate.block_bounds) == 3 for key, gate in result.gates.items() if key != "availability"
    )


def test_nonterminating_baseline_ratio_cannot_create_positive_increment() -> None:
    from fractions import Fraction

    from stock_profiler.modules.evaluation.cohort_metrics import decimal_fraction

    rows = []
    for offset, row in enumerate(history(180)):
        count = (3, 3, 4)[offset % 3]
        universe = row.baselines["UNIVERSE"].model_copy(
            update={
                "positive_rate": Decimal(1) if count == 3 else decimal_fraction(Fraction(5, 6)),
                "target_rate": decimal_fraction(Fraction(5, 6)),
                "counts": BaselineCounts(positive=6 if count == 3 else 5, target=5, member_slots=6),
            }
        )
        rows.append(
            row.model_copy(
                update={
                    "target_count": count,
                    "target_rate": Decimal(count) / 4,
                    "baselines": {**row.baselines, "UNIVERSE": universe},
                }
            )
        )
    result = summarize_history(tuple(rows), policy(), seed=20)
    gate = result.gates["increment:UNIVERSE:target_rate"]
    assert gate.estimate == 0
    assert gate.lower_bound == 0
    assert gate.passed is False
    assert result.disposition == "FAILED"


def test_missing_exact_baseline_counts_remain_indeterminate() -> None:
    rows = list(history(180))
    row = rows[90]
    rows[90] = row.model_copy(
        update={
            "baselines": {
                **row.baselines,
                "UNIVERSE": row.baselines["UNIVERSE"].model_copy(update={"counts": None}),
            }
        }
    )
    result = summarize_history(tuple(rows), policy(), seed=20)
    assert result.gates["increment:UNIVERSE:positive_rate"].passed is None
    assert result.disposition == "INDETERMINATE"
