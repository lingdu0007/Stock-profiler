"""Directional high-band gates from common complete-calendar moving blocks."""

from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256
from random import Random
from typing import Literal

from stock_profiler.modules.candidate_selection.calibrated_candidates import (
    CalibrationDiagnosticObservation,
    CalibrationDiagnostics,
    summarize_probability_calibration,
)
from stock_profiler.modules.evaluation.cohort_metrics import decimal_fraction
from stock_profiler.modules.evaluation.probability_contracts import (
    ProbabilityBound,
    ProbabilityInference,
    ProbabilityMonth,
    ProbabilityRegistration,
    ProbabilityWatermark,
)


@dataclass(frozen=True)
class _MonthTotals:
    regime: str | None
    high_count: int
    successes: int
    predicted: Fraction


def _totals(row: ProbabilityMonth, policy: ProbabilityRegistration) -> _MonthTotals:
    members = tuple(
        member
        for member in row.members
        if member.state in {"ACHIEVED", "NOT_ACHIEVED"}
        and member.frozen_probability >= policy.high_band_threshold
    )
    return _MonthTotals(
        row.regime,
        len(members),
        sum(member.state == "ACHIEVED" for member in members),
        sum((Fraction(member.frozen_probability) for member in members), Fraction(0)),
    )


def _metrics(
    rows: tuple[_MonthTotals, ...], state: str | None
) -> tuple[Fraction | None, Fraction | None]:
    eligible = tuple(row for row in rows if state is None or row.regime == state)
    count = sum(row.high_count for row in eligible)
    if not count:
        return None, None
    successes = sum(row.successes for row in eligible)
    predicted = sum((row.predicted for row in eligible), Fraction(0))
    return Fraction(successes, count), (predicted - successes) / count


def _watermark(
    rows: tuple[ProbabilityMonth, ...], policy: ProbabilityRegistration, state: str | None
) -> ProbabilityWatermark:
    mature = tuple(
        row
        for row in rows
        if row.members
        and all(member.state in {"ACHIEVED", "NOT_ACHIEVED"} for member in row.members)
        and (state is None or row.regime == state)
    )
    high_count = sum(_totals(row, policy).high_count for row in mature)
    end = None
    windows = 0
    for row in mature:
        if (
            row.cutoff_at is not None
            and row.matures_at is not None
            and (end is None or row.cutoff_at > end)
        ):
            windows += 1
            end = row.matures_at
    periods = 0
    previous_state = None
    for row in rows:
        if row.regime is not None:
            if row.regime == state and previous_state != state:
                periods += 1
            previous_state = row.regime
    sufficient = (
        len(mature) >= (policy.overall_months if state is None else policy.regime_months)
        and high_count
        >= (policy.overall_high_band_records if state is None else policy.regime_high_band_records)
        and windows
        >= (
            policy.overall_nonoverlapping_windows
            if state is None
            else policy.regime_nonoverlapping_windows
        )
        and (state is None or periods >= policy.regime_periods)
    )
    return ProbabilityWatermark(
        mature_months=len(mature),
        high_band_records=high_count,
        nonoverlapping_windows=windows,
        periods=periods,
        sufficient=sufficient,
        disposition="INSUFFICIENT",
    )


def summarize_probability_history(
    rows: tuple[ProbabilityMonth, ...], policy: ProbabilityRegistration, *, seed: int
) -> ProbabilityInference:
    """Sample all records and state labels together, then take the worst directional bound."""
    rows = tuple(row for row in rows if row.disposition != "FUTURE")
    totals = tuple(_totals(row, policy) for row in rows)
    scopes: dict[str, str | None] = {
        "overall": None,
        "BULL": "BULL",
        "BEAR": "BEAR",
        "SIDEWAYS": "SIDEWAYS",
    }
    distributions: dict[str, dict[str, list[Fraction]]] = {
        f"{scope}:{metric}": {} for scope in scopes for metric in ("success", "overconfidence")
    }
    undefined: dict[str, dict[str, int]] = {key: {} for key in distributions}
    resampling: dict[str, dict[str, str | int]] = {}
    for length in policy.block_lengths:
        label = str(length)
        random = Random(
            int(sha256(f"{policy.version_id}:{seed}:{length}".encode()).hexdigest(), 16)
        )
        digest = sha256()
        for key in distributions:
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
            sample = tuple(totals[i] for i in indices)
            for scope, state in scopes.items():
                for metric, value in zip(
                    ("success", "overconfidence"), _metrics(sample, state), strict=True
                ):
                    key = f"{scope}:{metric}"
                    if value is None:
                        undefined[key][label] += 1
                    else:
                        distributions[key][label].append(value)
        resampling[label] = {
            "calendar_months": len(rows),
            "repetitions": policy.bootstrap_repetitions,
            "sampling_digest": digest.hexdigest(),
        }
    gates: dict[str, ProbabilityBound] = {}
    for scope, state in scopes.items():
        for metric, estimate in zip(
            ("success", "overconfidence"), _metrics(totals, state), strict=True
        ):
            key = f"{scope}:{metric}"
            lower = metric == "success"
            bounds = {}
            for block, values in distributions[key].items():
                tail = 1 - Fraction(policy.confidence) if lower else Fraction(policy.confidence)
                # Ceiling for upper tails, floor for lower tails, compared before rounding.
                position = tail * (len(values) - 1)
                index = (
                    position.numerator // position.denominator
                    if lower
                    else -(-position.numerator // position.denominator)
                )
                bounds[block] = (
                    sorted(values)[index] if values and not undefined[key][block] else None
                )
            bound = (
                (
                    min(value for value in bounds.values() if value is not None)
                    if lower
                    else max(value for value in bounds.values() if value is not None)
                )
                if bounds and all(value is not None for value in bounds.values())
                else None
            )
            threshold = policy.success_floor if lower else policy.overconfidence_ceiling
            gates[key] = ProbabilityBound(
                estimate=decimal_fraction(estimate) if estimate is not None else None,
                bound=decimal_fraction(bound) if bound is not None else None,
                direction="LOWER" if lower else "UPPER",
                threshold=threshold,
                passed=(bound >= Fraction(threshold) if lower else bound <= Fraction(threshold))
                if bound is not None
                else None,
                block_bounds={
                    block: decimal_fraction(value) if value is not None else None
                    for block, value in bounds.items()
                },
                undefined_resamples=undefined[key],
            )
    valid = tuple(row for row in rows if row.valid_monthly)
    coverage = Fraction(sum(row.has_candidates for row in valid), len(valid)) if valid else None
    gates["coverage"] = ProbabilityBound(
        estimate=decimal_fraction(coverage) if coverage is not None else None,
        bound=decimal_fraction(coverage) if coverage is not None else None,
        direction="LOWER",
        threshold=policy.coverage_floor,
        passed=coverage >= Fraction(policy.coverage_floor) if coverage is not None else None,
        block_bounds={},
        undefined_resamples={},
    )
    availability = Fraction(len(valid), len(rows)) if rows else None
    gates["availability"] = ProbabilityBound(
        estimate=decimal_fraction(availability) if availability is not None else None,
        bound=decimal_fraction(availability) if availability is not None else None,
        direction="LOWER",
        threshold=policy.minimum_availability,
        passed=availability >= Fraction(policy.minimum_availability)
        if availability is not None
        else None,
        block_bounds={},
        undefined_resamples={},
    )
    watermarks = {}
    for scope, state in scopes.items():
        watermark = _watermark(rows, policy, state)
        missing = any(
            row.disposition == "MISSING"
            or any(member.state == "UNAVAILABLE" for member in row.members)
            for row in rows
            if state is None or row.regime == state
        )
        if state is not None:
            missing = missing or any(row.members and row.regime is None for row in rows)
        required = (
            gates[f"{scope}:success"].passed,
            gates[f"{scope}:overconfidence"].passed,
            *((gates["coverage"].passed, gates["availability"].passed) if state is None else ()),
        )
        disposition: Literal["INSUFFICIENT", "INDETERMINATE", "PASSED", "FAILED"] = (
            "INDETERMINATE"
            if missing
            else "INSUFFICIENT"
            if not watermark.sufficient
            else "INDETERMINATE"
            if any(value is None for value in required)
            else "PASSED"
            if all(required)
            else "FAILED"
        )
        watermarks[scope] = watermark.model_copy(update={"disposition": disposition})
    diagnostics = probability_diagnostics(rows)
    mature_months = tuple(row for row in rows if row.label_mature)
    recent = mature_months[-24:]
    recent_count = sum(len(row.members) for row in recent)
    return ProbabilityInference(
        overall=watermarks.pop("overall"),
        regimes=watermarks,
        gates=gates,
        resampling=resampling,
        diagnostics=diagnostics,
        recent_diagnostic_months=tuple(row.plan_month for row in recent),
        recent_diagnostic_sample_count=recent_count,
        recent_diagnostics=probability_diagnostics(recent)
        if len(recent) == 24
        and recent_count >= 200
        and not any(member.state == "UNAVAILABLE" for row in recent for member in row.members)
        else None,
    )


def probability_diagnostics(rows: tuple[ProbabilityMonth, ...]) -> CalibrationDiagnostics | None:
    members = tuple(
        member
        for row in rows
        for member in row.members
        if member.state in {"ACHIEVED", "NOT_ACHIEVED"}
    )
    if not members:
        return None
    return summarize_probability_calibration(
        tuple(
            CalibrationDiagnosticObservation(
                raw_success_score=member.raw_success_score,
                terminal_success=member.state == "ACHIEVED",
            )
            for member in members
        ),
        tuple(member.frozen_probability for member in members),
    )
