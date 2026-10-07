"""Conjunctive prospective cohort and probability inference on one calendar axis."""

from collections.abc import Callable
from decimal import Decimal
from fractions import Fraction
from functools import partial
from typing import Literal

from stock_profiler.modules.evaluation.cohort_metrics import decimal_fraction
from stock_profiler.modules.prospective.contracts import (
    FormalGate,
    FormalInference,
    FormalMonth,
    FormalPolicy,
    FormalRegimeWatermark,
)
from stock_profiler.modules.prospective.studentized import MetricDefinition, studentized_bounds

Metric = Callable[[tuple[FormalMonth, ...]], Fraction | None]


def _mean(values: tuple[Fraction, ...]) -> Fraction | None:
    return sum(values, Fraction(0)) / len(values) if values else None


def _stock_eligible(row: FormalMonth) -> bool:
    return row.cohort.batch_pass is not None


def _stock_rate(
    rows: tuple[FormalMonth, ...], field: str, state: str | None = None
) -> Fraction | None:
    if any(
        row.batch_due
        and row.cohort.disposition in {"FROZEN", "ABSTAINED"}
        and row.cohort.batch_pass is None
        for row in rows
    ):
        return None
    if field == "drawdown_pass" and any(
        row.batch_due and row.cohort.members and row.cohort.drawdown_pass is None for row in rows
    ):
        return None
    return _mean(
        tuple(
            Fraction(bool(getattr(row.cohort, field)))
            for row in rows
            if _stock_eligible(row)
            and getattr(row.cohort, field) is not None
            and (state is None or row.cohort.regime == state)
        )
    )


def _increment(rows: tuple[FormalMonth, ...], baseline: str, field: str) -> Fraction | None:
    values: list[Fraction] = []
    for row in rows:
        cohort = row.cohort
        if (
            row.batch_due
            and cohort.disposition in {"FROZEN", "ABSTAINED"}
            and cohort.batch_pass is None
        ):
            return None
        if not _stock_eligible(row):
            continue
        paired = cohort.baselines.get(baseline)
        if paired is None or paired.counts is None:
            return None
        if field == "batch_pass":
            if paired.counts.passed_trials is None or paired.trial_count <= 0:
                return None
            primary = Fraction(bool(cohort.batch_pass))
            comparison = Fraction(paired.counts.passed_trials, paired.trial_count)
        else:
            count = cohort.positive_count if field == "positive_rate" else cohort.target_count
            if count is None or paired.counts.member_slots <= 0:
                return None
            primary = Fraction(count, len(cohort.members)) if cohort.members else Fraction(0)
            comparison = Fraction(
                paired.counts.positive if field == "positive_rate" else paired.counts.target,
                paired.counts.member_slots,
            )
        values.append(primary - comparison)
    return _mean(tuple(values))


def _high_members(
    rows: tuple[FormalMonth, ...], threshold: Decimal, state: str | None = None
) -> tuple[tuple[Fraction, bool], ...]:
    return tuple(
        (Fraction(member.frozen_probability), member.state == "ACHIEVED")
        for row in rows
        if state is None or row.cohort.regime == state
        for member in row.probabilities
        if member.frozen_probability is not None and member.frozen_probability >= threshold
    )


def _probability(
    rows: tuple[FormalMonth, ...], threshold: Decimal, excess: bool, state: str | None = None
) -> Fraction | None:
    if any(
        row.missing_high_band_records for row in rows if state is None or row.cohort.regime == state
    ):
        return None
    return _mean(
        tuple(
            probability - success if excess else Fraction(success)
            for probability, success in _high_members(rows, threshold, state)
        )
    )


def _windows(rows: tuple[FormalMonth, ...]) -> int:
    end = None
    count = 0
    for row in rows:
        start, maturity = row.cohort.selection_at, row.cohort.matures_at
        if start is not None and maturity is not None and (end is None or start > end):
            count += 1
            end = max((maturity, *(member.matures_at for member in row.probabilities)))
    return count


def _periods(
    rows: tuple[FormalMonth, ...], state: str, eligible: Callable[[FormalMonth], bool]
) -> int:
    previous_state = None
    counted = False
    periods = 0
    for row in rows:
        current = row.cohort.regime
        if current is None:
            continue
        if current != previous_state:
            counted = False
            previous_state = current
        if current == state and eligible(row) and not counted:
            periods += 1
            counted = True
    return periods


def _regime(
    rows: tuple[FormalMonth, ...], state: str, policy: FormalPolicy, threshold: Decimal
) -> FormalRegimeWatermark:
    stock = tuple(row for row in rows if row.cohort.regime == state and _stock_eligible(row))
    probability = tuple(row for row in rows if row.cohort.regime == state and row.probabilities)
    formed = sum(row.cohort.drawdown_pass is not None for row in stock)
    stock_windows, probability_windows = _windows(stock), _windows(probability)
    stock_periods = _periods(rows, state, _stock_eligible)
    probability_periods = _periods(rows, state, lambda row: bool(row.probabilities))
    high = len(_high_members(probability, threshold))
    return FormalRegimeWatermark(
        mature_batches=len(stock),
        formed_batches=formed,
        stock_windows=stock_windows,
        stock_periods=stock_periods,
        probability_months=len(probability),
        high_band_records=high,
        probability_windows=probability_windows,
        probability_periods=probability_periods,
        stock_sufficient=(
            len(stock) >= policy.regime_batches
            and formed >= policy.regime_formed
            and stock_windows >= policy.regime_windows
            and stock_periods >= policy.regime_periods
        ),
        probability_sufficient=(
            len(probability) >= policy.regime_batches
            and high >= policy.regime_high_band_records
            and probability_windows >= policy.regime_windows
            and probability_periods >= policy.regime_periods
        ),
    )


def infer_prospective(
    rows: tuple[FormalMonth, ...],
    policy: FormalPolicy,
    *,
    high_band_threshold: Decimal,
    alpha: Fraction,
    seed: int,
) -> FormalInference:
    definitions: dict[str, tuple[Metric, Literal["LOWER", "UPPER"], Decimal, bool]] = {
        "overall_pass": (
            partial(_stock_rate, field="batch_pass"),
            "LOWER",
            policy.overall_pass_floor,
            False,
        ),
        "overall_drawdown": (
            partial(_stock_rate, field="drawdown_pass"),
            "LOWER",
            policy.overall_drawdown_floor,
            False,
        ),
        "calibration_success": (
            partial(_probability, threshold=high_band_threshold, excess=False),
            "LOWER",
            policy.calibration_success_floor,
            False,
        ),
        "overconfidence": (
            partial(_probability, threshold=high_band_threshold, excess=True),
            "UPPER",
            policy.overconfidence_ceiling,
            False,
        ),
    }
    for baseline in ("UNIVERSE", "RANDOM", "FOUR_FACTOR"):
        for field in (
            "positive_rate",
            "target_rate",
            *(("batch_pass",) if baseline != "UNIVERSE" else ()),
        ):
            definitions[f"increment:{baseline}:{field}"] = (
                partial(_increment, baseline=baseline, field=field),
                "LOWER",
                Decimal(0),
                True,
            )
    regimes = {
        state: _regime(rows, state, policy, high_band_threshold)
        for state in ("BULL", "BEAR", "SIDEWAYS")
    }
    for state, watermark in regimes.items():
        if watermark.stock_sufficient:
            for field, floor in (
                ("batch_pass", policy.regime_pass_floor),
                ("drawdown_pass", policy.regime_drawdown_floor),
            ):
                definitions[f"regime:{state}:{field}"] = (
                    partial(_stock_rate, field=field, state=state),
                    "LOWER",
                    floor,
                    False,
                )
        if watermark.probability_sufficient:
            for key, excess, floor in (
                ("calibration_success", False, policy.calibration_success_floor),
                ("overconfidence", True, policy.overconfidence_ceiling),
            ):
                calibration_direction: Literal["LOWER", "UPPER"] = "UPPER" if excess else "LOWER"
                definitions[f"regime:{state}:{key}"] = (
                    partial(
                        _probability, threshold=high_band_threshold, excess=excess, state=state
                    ),
                    calibration_direction,
                    floor,
                    False,
                )
    results = studentized_bounds(
        rows,
        {
            key: MetricDefinition(metric, direction)
            for key, (metric, direction, _, _) in definitions.items()
        },
        block_lengths=policy.block_lengths,
        repetitions=policy.bootstrap_repetitions,
        inner_repetitions=policy.inner_repetitions,
        alpha=alpha,
        seed=seed,
    )
    gates = {}
    for key, result in results.items():
        _, direction, floor, strict = definitions[key]
        bound = result.bound
        passed = (
            None
            if bound is None
            else (bound > Fraction(floor) if strict else bound >= Fraction(floor))
            if direction == "LOWER"
            else bound <= Fraction(floor)
        )
        gates[key] = FormalGate(
            estimate=decimal_fraction(result.estimate) if result.estimate is not None else None,
            bound=decimal_fraction(bound) if bound is not None else None,
            direction=direction,
            threshold=floor,
            strict=strict,
            passed=passed,
            block_bounds={
                label: decimal_fraction(value) if value is not None else None
                for label, value in result.block_bounds.items()
            },
            undefined_resamples=result.undefined_resamples,
        )
    return FormalInference(
        disposition="INDETERMINATE"
        if any(gate.passed is None for gate in gates.values())
        else "PASSED"
        if all(gate.passed for gate in gates.values())
        else "FAILED",
        gates=gates,
        regimes=regimes,
        sampling_digests=next(iter(results.values())).sampling_digests,
        unreliable_tail=any(result.unreliable_tail for result in results.values()),
        calibration_records=sum(len(row.probabilities) for row in rows),
        high_band_records=len(_high_members(rows, high_band_threshold)),
        due_missing_high_band_records=sum(row.missing_high_band_records for row in rows),
    )
