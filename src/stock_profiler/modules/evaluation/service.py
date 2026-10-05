"""Pure standard entry, terminal wealth and evidence lineage rules."""

from collections.abc import Mapping
from datetime import datetime
from decimal import Context, Decimal, localcontext
from fractions import Fraction

from stock_profiler.modules.evaluation.contracts import (
    EvaluationMember,
    OutcomeEvidence,
    PopulationCounts,
    StandardObservation,
    TradingCosts,
)
from stock_profiler.modules.portfolio.market_calendar import six_month_terminal_evaluation_at


def population_counts(members: tuple[EvaluationMember, ...], cutoff: datetime) -> PopulationCounts:
    due = sum(member.matures_at <= cutoff for member in members)
    missing = sum(member.state == "UNAVAILABLE" for member in members)
    return PopulationCounts(
        registered=len(members),
        due=due,
        evaluable=sum(member.state in {"ACHIEVED", "NOT_ACHIEVED"} for member in members),
        missing=missing,
        immature=len(members) - due,
        achieved=sum(member.state == "ACHIEVED" for member in members),
        not_achieved=sum(member.state == "NOT_ACHIEVED" for member in members),
        formal_adjudication="INDETERMINATE" if missing else "EVIDENCE_COMPLETE",
    )


def evaluate_member(
    member: EvaluationMember,
    observation: StandardObservation | None,
    sessions: tuple[datetime, ...],
    cutoff: datetime,
    quantity: Decimal,
    calendar_version: str,
) -> EvaluationMember:
    """Compute terminal standard wealth only from complete, cutoff-visible market facts."""
    if observation is None or observation.entry is None:
        return member.model_copy(update={"observation": observation})
    entry = observation.entry
    if entry.available_at > cutoff:
        return member
    if tuple(item.closed_at for item in entry.sessions) != sessions[: len(entry.sessions)]:
        raise ValueError("STANDARD_FIRST_BUYABLE_SESSION_UNPROVEN")
    if entry.effective_at < entry.sessions[-1].closed_at:
        raise ValueError("STANDARD_ENTRY_EVIDENCE_INCOMPLETE")
    executable = next((item for item in entry.sessions if item.buyable), None)
    if executable is None:
        if len(entry.sessions) != 5:
            return member.model_copy(update={"observation": observation})
        return member.model_copy(
            update={
                "entry_expired": True,
                "observation": observation,
                "state": "NOT_ACHIEVED" if member.matures_at <= cutoff else "PENDING",
            }
        )
    assert executable.costs is not None
    maturity = six_month_terminal_evaluation_at(executable.closed_at, calendar_version)
    entry_wealth = Fraction(quantity) * Fraction(executable.turnover) / Fraction(
        executable.volume
    ) + exact_costs(executable.costs)
    with localcontext(Context(prec=128)):
        price = entry_wealth / Fraction(quantity)
        entry_price = Decimal(price.numerator) / Decimal(price.denominator)
        terminal = observation.terminal
        net_return = None
        state = "PENDING" if maturity > cutoff else "UNAVAILABLE"
        if terminal is not None and maturity <= cutoff and terminal.available_at <= cutoff:
            if terminal.effective_at != maturity or terminal.actions_complete_through != maturity:
                raise ValueError("STANDARD_TERMINAL_HORIZON_INCOMPLETE")
            terminal_wealth = (
                Fraction(quantity)
                * Fraction(terminal.quantity_multiplier)
                * Fraction(terminal.price)
                + Fraction(terminal.cash_distributions)
                - exact_costs(terminal.costs)
            )
            exact_return = terminal_wealth / entry_wealth - 1
            net_return = Decimal(exact_return.numerator) / Decimal(exact_return.denominator)
            state = (
                "ACHIEVED" if terminal_wealth >= entry_wealth * Fraction(6, 5) else "NOT_ACHIEVED"
            )
        return member.model_copy(
            update={
                "entry_at": executable.closed_at,
                "entry_price": entry_price,
                "matures_at": maturity,
                "state": state,
                "net_total_return": net_return,
                "observation": observation,
            }
        )


def validate_evidence_revision(
    incoming: OutcomeEvidence,
    prior: OutcomeEvidence | None,
    evidence_by_id: Mapping[str, OutcomeEvidence],
    cutoff: datetime,
) -> None:
    if incoming.available_at > cutoff:
        raise ValueError("STANDARD_EVIDENCE_NOT_AVAILABLE_AT_CUTOFF")
    retained = evidence_by_id.get(incoming.evidence_id)
    if retained is not None and retained != incoming:
        raise ValueError("STANDARD_EVIDENCE_IDENTITY_REDEFINED")
    if prior is None:
        if incoming.corrects_evidence_id is not None:
            raise ValueError("STANDARD_CORRECTION_SOURCE_UNAVAILABLE")
    elif incoming != prior and (
        incoming.corrects_evidence_id != prior.evidence_id
        or incoming.evidence_id == prior.evidence_id
        or incoming.available_at < prior.available_at
    ):
        raise ValueError("STANDARD_AUTHORITATIVE_CORRECTION_REQUIRED")


def exact_costs(costs: TradingCosts) -> Fraction:
    return sum(
        (Fraction(value) for value in (costs.commission, costs.fees, costs.taxes, costs.slippage)),
        start=Fraction(0),
    )
