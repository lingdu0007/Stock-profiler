"""Joint calendar moving-block bootstrap-t with nested variance estimates."""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Context, Decimal, localcontext
from fractions import Fraction
from hashlib import sha256
from math import ceil, floor
from random import Random
from typing import Generic, Literal, TypeVar

from stock_profiler.modules.evaluation.cohort_metrics import decimal_fraction

Row = TypeVar("Row")


@dataclass(frozen=True)
class MetricDefinition(Generic[Row]):
    measure: Callable[[tuple[Row, ...]], Fraction | None]
    direction: Literal["LOWER", "UPPER"]


@dataclass(frozen=True)
class StudentizedBound:
    estimate: Fraction | None
    bound: Fraction | None
    block_bounds: dict[str, Fraction | None]
    undefined_resamples: dict[str, int]
    unreliable_tail: bool
    sampling_digests: dict[str, str]


def _indices(size: int, length: int, random: Random) -> tuple[int, ...]:
    result: list[int] = []
    while len(result) < size:
        start = random.randrange(size - length + 1)
        result.extend(range(start, start + length))
    return tuple(result[:size])


def _variance(values: tuple[Fraction | None, ...]) -> Fraction | None:
    if any(value is None for value in values):
        return None
    known = tuple(value for value in values if value is not None)
    mean = sum(known, Fraction(0)) / len(known)
    return sum(((value - mean) ** 2 for value in known), Fraction(0)) / (len(known) - 1)


def _variances(
    rows: tuple[Row, ...],
    metrics: dict[str, MetricDefinition[Row]],
    length: int,
    repetitions: int,
    random: Random,
    digest_update: Callable[[bytes], None],
) -> dict[str, Fraction | None]:
    values: dict[str, list[Fraction | None]] = {key: [] for key in metrics}
    for _ in range(repetitions):
        indices = _indices(len(rows), length, random)
        # The digest includes the shared inner samples as well as outer samples.
        digest_update((",".join(map(str, indices)) + ";").encode())
        sample = tuple(rows[index] for index in indices)
        for key, metric in metrics.items():
            values[key].append(metric.measure(sample))
    return {key: _variance(tuple(items)) for key, items in values.items()}


def studentized_bounds(
    rows: tuple[Row, ...],
    metrics: dict[str, MetricDefinition[Row]],
    *,
    block_lengths: tuple[int, ...],
    repetitions: int,
    inner_repetitions: int,
    alpha: Fraction,
    seed: int,
) -> dict[str, StudentizedBound]:
    """All gates share whole-month outer/inner samples; no undefined sample is discarded."""
    if not 0 < alpha < 1 or repetitions < 2 or inner_repetitions < 2:
        raise ValueError("PROSPECTIVE_STUDENTIZATION_PARAMETERS_INVALID")
    if not block_lengths or any(length < 1 for length in block_lengths):
        raise ValueError("PROSPECTIVE_BLOCK_LENGTHS_INVALID")
    estimates = {key: metric.measure(rows) for key, metric in metrics.items()}
    bounds: dict[str, dict[str, Fraction | None]] = {key: {} for key in metrics}
    undefined: dict[str, dict[str, int]] = {key: {} for key in metrics}
    digests: dict[str, str] = {}
    positions = {
        "LOWER": ceil((repetitions + 1) * (1 - alpha)) - 1,
        "UPPER": floor((repetitions + 1) * alpha) - 1,
    }
    unreliable = any(
        position <= 0 or position >= repetitions - 1 for position in positions.values()
    )
    for length in block_lengths:
        label = str(length)
        random = Random(int(sha256(f"{seed}:{length}".encode()).hexdigest(), 16))
        digest = sha256()
        for key in metrics:
            bounds[key][label] = None
            undefined[key][label] = 0
        if len(rows) < length:
            for key in metrics:
                undefined[key][label] = repetitions
            digests[label] = digest.hexdigest()
            continue
        original_variances = _variances(
            rows, metrics, length, inner_repetitions, random, digest.update
        )
        active = {
            key: metric
            for key, metric in metrics.items()
            if estimates[key] is not None
            and (original_variance := original_variances[key]) is not None
            and original_variance > 0
        }
        for key in set(metrics) - set(active):
            undefined[key][label] = repetitions
        pivots: dict[str, list[Decimal]] = {key: [] for key in metrics}
        for _ in range(repetitions):
            indices = _indices(len(rows), length, random)
            digest.update((",".join(map(str, indices)) + "|").encode())
            sample = tuple(rows[index] for index in indices)
            sampled_values = {key: metric.measure(sample) for key, metric in active.items()}
            variances = _variances(sample, active, length, inner_repetitions, random, digest.update)
            for key in active:
                original, value, variance = estimates[key], sampled_values[key], variances[key]
                if original is None or value is None or variance is None or variance <= 0:
                    undefined[key][label] += 1
                    continue
                with localcontext(Context(prec=128)):
                    pivots[key].append(
                        decimal_fraction(value - original) / decimal_fraction(variance).sqrt()
                    )
        digests[label] = digest.hexdigest()
        for key, metric in metrics.items():
            original, variance = estimates[key], original_variances[key]
            if (
                unreliable
                or undefined[key][label]
                or original is None
                or variance is None
                or variance <= 0
            ):
                continue
            pivot = sorted(pivots[key])[positions[metric.direction]]
            with localcontext(Context(prec=128)):
                bounds[key][label] = original - Fraction(pivot * decimal_fraction(variance).sqrt())
    return {
        key: StudentizedBound(
            estimate=estimates[key],
            bound=(min if metrics[key].direction == "LOWER" else max)(
                value for value in bounds[key].values() if value is not None
            )
            if all(value is not None for value in bounds[key].values())
            else None,
            block_bounds=bounds[key],
            undefined_resamples=undefined[key],
            unreliable_tail=unreliable,
            sampling_digests=digests,
        )
        for key in metrics
    }
