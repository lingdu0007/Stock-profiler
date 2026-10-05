"""Conjunctive one-sided inference from jointly sampled complete calendar months."""

from collections import Counter
from collections.abc import Callable
from decimal import Decimal
from fractions import Fraction
from functools import partial
from hashlib import sha256
from math import exp, sqrt
from random import Random
from typing import Literal

from stock_profiler.modules.evaluation.cohort_metrics import decimal_fraction
from stock_profiler.modules.evaluation.historical_contracts import (
    HistoricalGate,
    HistoricalInference,
    HistoricalMonthResult,
    HistoricalRegistration,
    RegimeWatermark,
)

Metric = Callable[[tuple[HistoricalMonthResult, ...]], Fraction | None]


def _mean(values: tuple[Fraction, ...]) -> Fraction | None:
    return sum(values, Fraction(0)) / len(values) if values else None


def _rate(
    rows: tuple[HistoricalMonthResult, ...], field: str, regime: str | None = None
) -> Fraction | None:
    return _mean(
        tuple(
            Fraction(bool(getattr(row, field)))
            for row in rows
            if getattr(row, field) is not None and (regime is None or row.regime == regime)
        )
    )


def _increment(
    rows: tuple[HistoricalMonthResult, ...], baseline: str, field: str
) -> Fraction | None:
    values: list[Fraction] = []
    for row in rows:
        if getattr(row, field) is None:
            continue
        paired = row.baselines.get(baseline)
        if paired is None or paired.counts is None:
            return None
        if field == "batch_pass":
            if paired.counts.passed_trials is None or paired.trial_count <= 0:
                return None
            primary = Fraction(bool(row.batch_pass))
            comparison = Fraction(paired.counts.passed_trials, paired.trial_count)
        else:
            count = row.positive_count if field == "positive_rate" else row.target_count
            if count is None:
                return None
            primary = Fraction(count, len(row.members)) if row.members else Fraction(0)
            comparison = Fraction(
                paired.counts.positive if field == "positive_rate" else paired.counts.target,
                paired.counts.member_slots,
            )
        values.append(primary - comparison)
    return _mean(tuple(values))


def _watermark(
    rows: tuple[HistoricalMonthResult, ...], state: str, policy: HistoricalRegistration
) -> RegimeWatermark:
    eligible = tuple(row for row in rows if row.regime == state and row.batch_pass is not None)
    previous_end = None
    windows = 0
    for row in eligible:
        if (
            row.selection_at is not None
            and row.matures_at is not None
            and (previous_end is None or row.selection_at > previous_end)
        ):
            windows += 1
            previous_end = row.matures_at
    periods = 0
    previous_state = None
    for row in rows:
        if row.regime is not None:
            if row.regime == state and previous_state != state:
                periods += 1
            previous_state = row.regime
    formed = sum(row.drawdown_pass is not None for row in eligible)
    return RegimeWatermark(
        mature_months=len(eligible),
        formed_months=formed,
        nonoverlapping_windows=windows,
        periods=periods,
        sufficient=len(eligible) >= policy.regime_months
        and formed >= policy.regime_drawdown_months
        and windows >= policy.regime_nonoverlapping_windows
        and periods >= policy.regime_periods,
    )


def summarize_history(
    rows: tuple[HistoricalMonthResult, ...], policy: HistoricalRegistration, *, seed: int
) -> HistoricalInference:
    """Moving blocks sample all monthly facts together, including failed and immature months."""
    rows = tuple(row for row in rows if row.disposition != "FUTURE")
    metrics: dict[str, tuple[Metric, Decimal, bool]] = {
        "overall_pass": (
            lambda sample: _rate(sample, "batch_pass"),
            policy.overall_pass_floor,
            False,
        ),
        "overall_drawdown": (
            lambda sample: _rate(sample, "drawdown_pass"),
            policy.overall_drawdown_floor,
            False,
        ),
    }
    for baseline in ("UNIVERSE", "RANDOM", "FOUR_FACTOR"):
        for field in (
            "positive_rate",
            "target_rate",
            *(("batch_pass",) if baseline != "UNIVERSE" else ()),
        ):
            metrics[f"increment:{baseline}:{field}"] = (
                partial(_increment, baseline=baseline, field=field),
                Decimal(0),
                True,
            )
    regimes = {state: _watermark(rows, state, policy) for state in ("BULL", "BEAR", "SIDEWAYS")}
    for state in regimes:
        for field, floor in (
            ("batch_pass", policy.regime_pass_floor),
            ("drawdown_pass", policy.regime_drawdown_floor),
        ):
            metrics[f"regime:{state}:{field}"] = (
                partial(_rate, field=field, regime=state),
                floor,
                False,
            )
    distributions: dict[str, dict[str, list[Fraction]]] = {key: {} for key in metrics}
    undefined: dict[str, dict[str, int]] = {key: {} for key in metrics}
    resampling: dict[str, dict[str, int | str]] = {}
    for length in policy.block_lengths:
        label = str(length)
        block_seed = int(sha256(f"{policy.version_id}:{seed}:{length}".encode()).hexdigest(), 16)
        random = Random(block_seed)
        digest = sha256()
        for key in metrics:
            distributions[key][label] = []
            undefined[key][label] = 0
        effective_length = min(length, len(rows))
        for _ in range(policy.bootstrap_repetitions):
            indices: list[int] = []
            while len(indices) < len(rows):
                start = random.randrange(len(rows) - effective_length + 1)
                indices.extend(range(start, start + effective_length))
            indices = indices[: len(rows)]
            digest.update((",".join(map(str, indices)) + ";").encode())
            sample = tuple(rows[index] for index in indices)
            for key, (metric, _, _) in metrics.items():
                value = metric(sample)
                if value is None:
                    undefined[key][label] += 1
                else:
                    distributions[key][label].append(value)
        resampling[label] = {
            "repetitions": policy.bootstrap_repetitions,
            "seed_digest": sha256(str(block_seed).encode()).hexdigest(),
            "sampling_digest": digest.hexdigest(),
            "calendar_months": len(rows),
        }
    gates: dict[str, HistoricalGate] = {}
    for key, (metric, floor, strict) in metrics.items():
        bounds = {
            label: (
                sorted(values)[int((1 - Fraction(policy.confidence)) * (len(values) - 1))]
                if values and not undefined[key][label]
                else None
            )
            for label, values in distributions[key].items()
        }
        lower = (
            min(value for value in bounds.values() if value is not None)
            if all(value is not None for value in bounds.values())
            else None
        )
        estimate = metric(rows)
        gates[key] = HistoricalGate(
            estimate=decimal_fraction(estimate) if estimate is not None else None,
            lower_bound=decimal_fraction(lower) if lower is not None else None,
            threshold=floor,
            strict=strict,
            passed=(lower > Fraction(floor) if strict else lower >= Fraction(floor))
            if lower is not None
            else None,
            block_bounds={
                label: decimal_fraction(value) if value is not None else None
                for label, value in bounds.items()
            },
            undefined_resamples=undefined[key],
        )
    availability = (
        Fraction(
            sum(
                row.disposition in {"FROZEN", "ABSTAINED"} and row.availability_failure is None
                for row in rows
            ),
            len(rows),
        )
        if rows
        else None
    )
    gates["availability"] = HistoricalGate(
        estimate=decimal_fraction(availability) if availability is not None else None,
        lower_bound=decimal_fraction(availability) if availability is not None else None,
        threshold=policy.minimum_availability,
        strict=False,
        passed=availability >= Fraction(policy.minimum_availability)
        if availability is not None
        else None,
        block_bounds={},
        undefined_resamples={},
    )
    mature = sum(row.batch_pass is not None for row in rows)
    disposition: Literal["INSUFFICIENT", "EXPLORATORY", "PASSED", "FAILED", "INDETERMINATE"] = (
        "INSUFFICIENT"
        if mature < policy.exploratory_months
        else "EXPLORATORY"
        if mature < policy.formal_months
        else "INDETERMINATE"
        if not all(watermark.sufficient for watermark in regimes.values())
        or any(gate.passed is None for gate in gates.values())
        else "PASSED"
        if all(gate.passed for gate in gates.values())
        else "FAILED"
    )
    return HistoricalInference(
        disposition=disposition,
        gates=gates,
        regimes=regimes,
        diagnostics=_diagnostics(rows, seed, policy),
        resampling=resampling,
    )


def _diagnostics(
    rows: tuple[HistoricalMonthResult, ...], seed: int, policy: HistoricalRegistration
) -> dict[str, int | str | None]:
    frequencies = Counter(
        security for row in rows if row.batch_pass is not None for security in row.members
    )
    slots = sum(frequencies.values())
    effective = (
        Fraction(slots**2, sum(count**2 for count in frequencies.values())) if slots else None
    )
    formed = tuple(row for row in rows if row.batch_pass is not None and row.members)
    turnover = _mean(
        tuple(
            Fraction(
                len(set(right.members) - set(left.members)), policy.selection_policy.cohort_size
            )
            for left, right in zip(formed, formed[1:], strict=False)
        )
    )
    result: dict[str, int | str | None] = {
        "different_securities": len(frequencies),
        "highest_repetition": max(frequencies.values(), default=0),
        "top_ten_slot_share": str(
            decimal_fraction(
                Fraction(sum(count for _, count in frequencies.most_common(10)), slots)
            )
        )
        if slots
        else None,
        "effective_securities": str(decimal_fraction(effective)) if effective is not None else None,
        "composition_turnover": str(decimal_fraction(turnover)) if turnover is not None else None,
        "inference_unit": (
            "complete calendar month; common moving blocks; all metrics and baselines share samples"
        ),
    }
    # Calendar-pair autocorrelation is diagnostic only; gaps are never compressed.
    for lag in (1, 6, 9, 12):
        pairs = tuple(
            (float(left.batch_pass), float(right.batch_pass))
            for left, right in zip(rows, rows[lag:], strict=False)
            if left.batch_pass is not None and right.batch_pass is not None
        )
        if len(pairs) < 2:
            result[f"pass_autocorrelation_lag_{lag}"] = None
            continue
        x, y = zip(*pairs, strict=True)
        mx, my = sum(x) / len(x), sum(y) / len(y)
        vx, vy = sum((value - mx) ** 2 for value in x), sum((value - my) ** 2 for value in y)
        result[f"pass_autocorrelation_lag_{lag}"] = (
            str(sum((a - mx) * (b - my) for a, b in pairs) / sqrt(vx * vy))
            if vx * vy
            else "constant"
        )
    for scenario in ("UP", "DOWN", "FLAT"):
        stress = tuple(
            row
            for row in rows
            if row.future_index_return is not None
            and (
                "UP"
                if row.future_index_return >= Decimal("0.1")
                else "DOWN"
                if row.future_index_return <= Decimal("-0.1")
                else "FLAT"
            )
            == scenario
        )
        rate = _rate(stress, "batch_pass")
        result[f"ex_post_{scenario.lower()}_months"] = len(stress)
        result[f"ex_post_{scenario.lower()}_pass_rate"] = (
            str(decimal_fraction(rate)) if rate is not None else None
        )
    result.update(_two_way_diagnostics(rows, frequencies, seed, policy))
    return result


def _two_way_diagnostics(
    rows: tuple[HistoricalMonthResult, ...],
    frequencies: Counter[str],
    seed: int,
    policy: HistoricalRegistration,
) -> dict[str, int | str | None]:
    """Stock-cluster Poisson weights multiply common month-block multiplicities."""
    if not rows or not frequencies or not any(row.member_returns for row in rows):
        return {"stock_month_two_way_status": "unavailable"}
    worst: dict[str, Fraction | None] = {"positive": None, "target": None}
    for length in policy.block_lengths:
        random = Random(int(sha256(f"two-way:{seed}:{length}".encode()).hexdigest(), 16))
        samples: dict[str, list[Fraction]] = {"positive": [], "target": []}
        for _ in range(policy.bootstrap_repetitions):
            weights: dict[str, int] = {}
            for security in sorted(frequencies):
                product, count = 1.0, -1
                while product > exp(-1):
                    product *= random.random()
                    count += 1
                weights[security] = count
            indices: list[int] = []
            block = min(length, len(rows))
            while len(indices) < len(rows):
                start = random.randrange(len(rows) - block + 1)
                indices.extend(range(start, start + block))
            weighted_returns = tuple(
                (value, weights[security])
                for index in indices[: len(rows)]
                for security, value in rows[index].member_returns.items()
            )
            denominator = sum(weight for _, weight in weighted_returns)
            if not denominator:
                continue
            samples["positive"].append(
                Fraction(
                    sum(weight for value, weight in weighted_returns if value > 0), denominator
                )
            )
            samples["target"].append(
                Fraction(
                    sum(
                        weight
                        for value, weight in weighted_returns
                        if value >= policy.terminal_target
                    ),
                    denominator,
                )
            )
        for key, values in samples.items():
            if values:
                bound = sorted(values)[int((1 - Fraction(policy.confidence)) * (len(values) - 1))]
                prior_bound = worst[key]
                worst[key] = min(prior_bound, bound) if prior_bound is not None else bound
    return {
        "stock_month_two_way_status": "diagnostic only",
        **{
            f"stock_month_two_way_{key}_lower_bound": str(decimal_fraction(value))
            if value is not None
            else None
            for key, value in worst.items()
        },
    }
