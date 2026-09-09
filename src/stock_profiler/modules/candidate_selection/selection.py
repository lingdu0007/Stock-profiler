"""Frozen screening handoff and host-owned cohort selection."""

from __future__ import annotations

from datetime import date
from decimal import Context, Decimal, localcontext
from fractions import Fraction
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field

from stock_profiler.modules.candidate_selection.universe import (
    UniverseCommand,
    UniverseContract,
    UniverseOutcome,
)
from stock_profiler.modules.candidate_selection.universe_evidence import UniverseEvidence


class SelectionPolicy(UniverseContract):
    version_id: str = Field(min_length=1)
    cohort_size: int = Field(gt=0, strict=True)
    industry_limit: int = Field(gt=0, strict=True)
    capitalization_limit: int = Field(gt=0, strict=True)
    correlation_sessions: int = Field(gt=1, strict=True)
    maximum_correlation: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    positive_weight: int = Field(gt=0, strict=True)
    terminal_weight: int = Field(gt=0, strict=True)


class ScreeningRow(UniverseContract):
    security_id: str = Field(min_length=1)
    industry: str | None
    float_capitalization: Decimal | None
    positive_score: Decimal | None
    terminal_score: Decimal | None
    adjusted_returns: tuple[Decimal, ...]
    return_dates: tuple[date, ...]


class SelectionCommand(UniverseContract):
    contract_version: Literal["1.0.0"]
    synthetic: Literal[True]
    generator_version: str = Field(min_length=1)
    seed: int
    cutoff_at: AwareDatetime
    purpose: Literal["SYNTHETIC", "HISTORICAL_RECONSTRUCTED", "REAL_CANDIDATE"]
    universe_object_id: str = Field(min_length=1)
    universe_event_id: str = Field(min_length=1)
    policy: SelectionPolicy
    industry_version: str = Field(min_length=1)
    adjustment_version: str = Field(min_length=1)
    screening_snapshot_id: str = Field(min_length=1)
    strategy_version: str = Field(min_length=1)
    rows: tuple[ScreeningRow, ...]
    evidence: UniverseEvidence | None = None


class ScreeningRank(UniverseContract):
    security_id: str
    rank: int
    positive_percentile: Decimal
    terminal_percentile: Decimal
    composite_score: Decimal


class SelectionPopulation(UniverseContract):
    scheduled_monthly: Literal[True] = True
    valid_monthly: bool
    recommendation_coverage_denominator: bool
    selection_pass_denominator: bool
    selection_pass: Literal[False] | None = None
    availability_failure: Literal["DATA", "SYSTEM"] | None = None


class SelectionScan(UniverseContract):
    security_id: str
    capitalization_group: int
    included: bool
    reasons: tuple[str, ...]
    correlated_with: tuple[str, ...]


class SelectionOutcome(UniverseContract):
    disposition: Literal["FROZEN", "ABSTAINED", "DATA_FAILED", "SYSTEM_FAILED", "BLOCKED"]
    cutoff_at: AwareDatetime
    universe_event_id: str
    policy: SelectionPolicy
    members: tuple[str, ...]
    ranking: tuple[ScreeningRank, ...]
    scan: tuple[SelectionScan, ...]
    population: SelectionPopulation
    reasons: tuple[str, ...]
    qualification_scope: Literal["D0_SYNTHETIC_CONTRACT_ONLY"] = "D0_SYNTHETIC_CONTRACT_ONLY"
    actionable: Literal[False] = False


def freeze_selection(
    command: SelectionCommand,
    source: UniverseCommand | None,
    universe: UniverseOutcome | None,
    *,
    prerequisite: str = "SUCCEEDED",
) -> SelectionOutcome:
    if prerequisite != "SUCCEEDED":
        return SelectionOutcome(
            disposition="SYSTEM_FAILED" if prerequisite == "FAILED" else "BLOCKED",
            cutoff_at=command.cutoff_at,
            universe_event_id=command.universe_event_id,
            policy=command.policy,
            members=(),
            ranking=(),
            scan=(),
            population=SelectionPopulation(
                valid_monthly=False,
                recommendation_coverage_denominator=False,
                selection_pass_denominator=False,
                availability_failure="SYSTEM" if prerequisite == "FAILED" else None,
            ),
            reasons=("BUSINESS_PREREQUISITE_NOT_MET",),
        )
    failures: list[str] = []
    if (
        source is None
        or universe is None
        or universe.disposition != "FROZEN"
        or source.cutoff_at != command.cutoff_at
        or source.purpose != command.purpose
    ):
        failures.append("FROZEN_UNIVERSE_REQUIRED")
    else:
        keys = [row.security_id for row in command.rows]
        if len(set(keys)) != len(keys) or set(keys) != set(universe.members):
            failures.append("SCREENING_UNIVERSE_INCOMPLETE")
        sessions = (
            tuple(
                day.market_date
                for day in source.calendar.days
                if day.close_at is not None and day.close_at <= command.cutoff_at
            )[-command.policy.correlation_sessions :]
            if source.calendar is not None
            else ()
        )
        if len(sessions) != command.policy.correlation_sessions or any(
            row.return_dates != sessions
            or len(row.adjusted_returns) != command.policy.correlation_sessions
            for row in command.rows
        ):
            failures.append("CORRELATION_WINDOW_INCOMPLETE")
    for row in command.rows:
        values = (row.float_capitalization, row.positive_score, row.terminal_score)
        if (
            not row.industry
            or any(value is None or not value.is_finite() for value in values)
            or row.float_capitalization is None
            or row.float_capitalization <= 0
        ):
            failures.append("SCREENING_FACT_MISSING")
        if any(not value.is_finite() or value < -1 for value in row.adjusted_returns):
            failures.append("ADJUSTED_RETURN_INVALID")
        if len(set(row.adjusted_returns)) < 2:
            failures.append("CORRELATION_UNDEFINED")
    if command.evidence is None:
        failures.append("SELECTION_EVIDENCE_REQUIRED")
    else:
        if command.evidence.authority != "EXCHANGE":
            failures.append("SELECTION_SOURCE_NOT_AUTHORITATIVE")
        if command.evidence.fact_effective_at.astimezone(ZoneInfo("Asia/Shanghai")).date() != (
            command.cutoff_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        ):
            failures.append("SELECTION_FACT_STALE")
        failures.extend(
            command.evidence.failures(
                command.cutoff_at,
                command.model_dump(
                    mode="json",
                    include={
                        "rows",
                        "industry_version",
                        "adjustment_version",
                        "screening_snapshot_id",
                        "strategy_version",
                    },
                ),
                command.purpose,
            )
        )
    if failures:
        return SelectionOutcome(
            disposition="DATA_FAILED",
            cutoff_at=command.cutoff_at,
            universe_event_id=command.universe_event_id,
            policy=command.policy,
            members=(),
            ranking=(),
            scan=(),
            population=SelectionPopulation(
                valid_monthly=False,
                recommendation_coverage_denominator=False,
                selection_pass_denominator=False,
                availability_failure="DATA",
            ),
            reasons=tuple(dict.fromkeys(failures)),
        )
    with localcontext(Context(prec=38)):
        positive = _percentiles(command.rows, "positive_score")
        terminal = _percentiles(command.rows, "terminal_score")
        policy = command.policy
        scores = {
            row.security_id: (
                policy.positive_weight * positive[row.security_id]
                + policy.terminal_weight * terminal[row.security_id]
            )
            / (policy.positive_weight + policy.terminal_weight)
            for row in command.rows
        }
        ranking = tuple(
            ScreeningRank(
                security_id=security_id,
                rank=index + 1,
                positive_percentile=positive[security_id],
                terminal_percentile=terminal[security_id],
                composite_score=scores[security_id],
            )
            for index, security_id in enumerate(sorted(scores, key=lambda key: (-scores[key], key)))
        )
        capitalization = {
            row.security_id: index * 3 // len(command.rows)
            for index, row in enumerate(
                sorted(command.rows, key=lambda row: (row.float_capitalization, row.security_id))
            )
        }
        rows = {row.security_id: row for row in command.rows}
        members: list[str] = []
        scan: list[SelectionScan] = []
        for ranked in ranking:
            row = rows[ranked.security_id]
            group = capitalization[row.security_id]
            reasons: list[str] = []
            if sum(rows[key].industry == row.industry for key in members) >= policy.industry_limit:
                reasons.append("INDUSTRY_LIMIT")
            if sum(capitalization[key] == group for key in members) >= policy.capitalization_limit:
                reasons.append("CAPITALIZATION_LIMIT")
            conflicts = tuple(
                key
                for key in members
                if _correlation_exceeds(row.adjusted_returns, rows[key].adjusted_returns, policy)
            )
            if conflicts:
                reasons.append("CORRELATION_LIMIT")
            if not reasons:
                members.append(row.security_id)
            scan.append(
                SelectionScan(
                    security_id=row.security_id,
                    capitalization_group=group,
                    included=not reasons,
                    reasons=tuple(reasons),
                    correlated_with=conflicts,
                )
            )
            if len(members) == policy.cohort_size:
                break
        formed = len(members) == policy.cohort_size
        return SelectionOutcome(
            disposition="FROZEN" if formed else "ABSTAINED",
            cutoff_at=command.cutoff_at,
            universe_event_id=command.universe_event_id,
            policy=policy,
            members=tuple(members) if formed else (),
            ranking=ranking,
            scan=tuple(scan),
            population=SelectionPopulation(
                valid_monthly=True,
                recommendation_coverage_denominator=True,
                selection_pass_denominator=True,
                selection_pass=None if formed else False,
            ),
            reasons=() if formed else ("CONSTRAINTS_PREVENT_COMPLETE_COHORT",),
        )


def _percentiles(
    rows: tuple[ScreeningRow, ...], field: Literal["positive_score", "terminal_score"]
) -> dict[str, Decimal]:
    values = [getattr(row, field) for row in rows]
    return {
        row.security_id: Decimal(
            sum(value < getattr(row, field) for value in values)
            + (sum(value == getattr(row, field) for value in values) - 1) / 2
        )
        * 100
        / max(1, len(rows) - 1)
        for row in rows
    }


def _correlation_exceeds(
    left: tuple[Decimal, ...], right: tuple[Decimal, ...], policy: SelectionPolicy
) -> bool:
    x = tuple(Fraction(value) for value in left)
    y = tuple(Fraction(value) for value in right)
    n = len(x)
    covariance = n * sum(a * b for a, b in zip(x, y, strict=True)) - sum(x) * sum(y)
    variance_x = n * sum(a * a for a in x) - sum(x) ** 2
    variance_y = n * sum(b * b for b in y) - sum(y) ** 2
    # Compare squares exactly, without a floating-point epsilon at the policy boundary.
    return covariance > 0 and covariance**2 > (
        Fraction(policy.maximum_correlation) ** 2 * variance_x * variance_y
    )
