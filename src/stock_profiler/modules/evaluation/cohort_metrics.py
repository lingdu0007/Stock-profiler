"""Exact terminal proportions and common-calendar cash-sleeve wealth paths."""

from datetime import datetime
from decimal import Context, Decimal, localcontext
from fractions import Fraction

from stock_profiler.modules.evaluation.contracts import EvaluationMember, TerminalEvidence
from stock_profiler.modules.evaluation.historical_contracts import (
    HistoricalRegistration,
    WealthPath,
)
from stock_profiler.modules.evaluation.service import exact_costs
from stock_profiler.modules.portfolio.market_calendar import MarketSession, SyntheticMarketCalendar


def historical_sessions(calendar: SyntheticMarketCalendar) -> tuple[MarketSession, ...]:
    """Preserve authoritative saved sessions and their gaps within active coverage."""
    if not calendar.sessions:
        return calendar.terminal_sessions
    first, last = calendar.sessions[0].closed_at, calendar.sessions[-1].closed_at
    return (
        tuple(session for session in calendar.terminal_sessions if session.closed_at < first)
        + calendar.sessions
        + tuple(session for session in calendar.terminal_sessions if session.closed_at > last)
    )


def decimal_fraction(value: Fraction) -> Decimal:
    with localcontext(Context(prec=128)):
        return Decimal(value.numerator) / Decimal(value.denominator)


def exact_member_return(member: EvaluationMember, quantity: Decimal) -> Fraction:
    if member.entry_expired:
        return Fraction(0)
    observation = member.observation
    assert (
        observation is not None
        and observation.entry is not None
        and observation.terminal is not None
    )
    session = next(item for item in observation.entry.sessions if item.buyable)
    assert session.costs is not None
    cost = Fraction(quantity) * Fraction(session.turnover) / Fraction(session.volume) + exact_costs(
        session.costs
    )
    return terminal_wealth(observation.terminal, quantity) / cost - 1


def terminal_wealth(mark: TerminalEvidence, quantity: Decimal) -> Fraction:
    return (
        Fraction(quantity) * Fraction(mark.quantity_multiplier) * Fraction(mark.price)
        + Fraction(mark.cash_distributions)
        - exact_costs(mark.costs)
    )


def cohort_metrics(
    members: tuple[EvaluationMember, ...], policy: HistoricalRegistration
) -> dict[str, Decimal | int | bool]:
    returns = tuple(exact_member_return(member, policy.standard_quantity) for member in members)
    positive = sum(value > 0 for value in returns)
    target = sum(value >= Fraction(policy.terminal_target) for value in returns)
    return {
        "positive_count": positive,
        "target_count": target,
        "positive_rate": decimal_fraction(Fraction(positive, len(members))),
        "target_rate": decimal_fraction(Fraction(target, len(members))),
        "batch_pass": positive >= policy.positive_members_required
        and target >= policy.target_members_required,
    }


def cohort_nav(
    members: tuple[EvaluationMember, ...],
    paths: tuple[WealthPath, ...],
    sessions: tuple[datetime, ...],
    policy: HistoricalRegistration,
    cutoff: datetime,
) -> tuple[tuple[Decimal, ...], Fraction]:
    by_security = {path.security_id: path for path in paths}
    if len(by_security) != len(paths):
        raise ValueError("HISTORICAL_PATH_MEMBERSHIP_DUPLICATED")
    sleeves: list[tuple[Fraction, ...]] = []
    for member in members:
        if member.entry_expired:
            sleeves.append(tuple(Fraction(1) for _ in sessions))
            continue
        observation = member.observation
        assert (
            observation is not None
            and observation.entry is not None
            and observation.terminal is not None
        )
        entry = next(item for item in observation.entry.sessions if item.buyable)
        assert entry.costs is not None
        entry_wealth = Fraction(policy.standard_quantity) * Fraction(entry.turnover) / Fraction(
            entry.volume
        ) + exact_costs(entry.costs)
        path = by_security.get(member.security_id)
        expected = tuple(at for at in sessions if entry.closed_at <= at <= member.matures_at)
        if (
            path is None
            or tuple(mark.effective_at for mark in path.marks) != expected
            or any(
                mark.available_at > cutoff or mark.actions_complete_through != mark.effective_at
                for mark in path.marks
            )
        ):
            raise ValueError("HISTORICAL_DAILY_WEALTH_PATH_INCOMPLETE")
        if path.marks[-1].model_dump(
            exclude={
                "evidence_id",
                "source_version",
                "published_at",
                "acquired_at",
                "validated_at",
                "corrects_evidence_id",
                "correction_reason",
            }
        ) != observation.terminal.model_dump(
            exclude={
                "evidence_id",
                "source_version",
                "published_at",
                "acquired_at",
                "validated_at",
                "corrects_evidence_id",
                "correction_reason",
            }
        ):
            raise ValueError("HISTORICAL_PATH_TERMINAL_WEALTH_MISMATCH")
        values = {
            mark.effective_at: terminal_wealth(mark, policy.standard_quantity) / entry_wealth
            for mark in path.marks
        }
        sleeves.append(
            tuple(
                Fraction(1)
                if at < entry.closed_at
                else values[at]
                if at <= member.matures_at
                else values[member.matures_at]
                for at in sessions
            )
        )
    nav = tuple(
        sum((sleeve[index] for sleeve in sleeves), Fraction(0))
        / policy.selection_policy.cohort_size
        for index in range(len(sessions))
    )
    peak = Fraction(1)
    drawdown = Fraction(0)
    for value in nav:
        peak = max(peak, value)
        drawdown = max(drawdown, 1 - value / peak)
    return tuple(decimal_fraction(value) for value in nav), drawdown
