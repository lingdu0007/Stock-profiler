"""Selection-visible market context and reproducible constrained paired baselines."""

from datetime import datetime
from fractions import Fraction
from hashlib import sha256
from random import Random
from typing import Literal

from stock_profiler.modules.candidate_selection.selection import (
    SelectionCommand,
    SelectionConstraints,
)
from stock_profiler.modules.evaluation.cohort_metrics import (
    cohort_metrics,
    cohort_nav,
    decimal_fraction,
    exact_member_return,
    historical_sessions,
)
from stock_profiler.modules.evaluation.contracts import EvaluationMember
from stock_profiler.modules.evaluation.historical_contracts import (
    BaselineCounts,
    BaselineResult,
    HistoricalMonthInput,
    HistoricalRegistration,
)
from stock_profiler.modules.portfolio.market_calendar import synthetic_market_calendar


def market_regime(
    month: HistoricalMonthInput, selection_cutoff: datetime, calendar_version: str
) -> Literal["BULL", "BEAR", "SIDEWAYS"]:
    calendar = synthetic_market_calendar(calendar_version)
    index = month.index
    if calendar is None or index is None or index.available_at > selection_cutoff:
        raise ValueError("HISTORICAL_SELECTION_VISIBLE_INDEX_REQUIRED")
    history = tuple(
        session.closed_at
        for session in historical_sessions(calendar)
        if session.closed_at <= selection_cutoff
    )
    if (
        tuple(price.closed_at for price in index.prices) != history[-120:]
        or len(index.prices) != 120
        or index.effective_at != history[-1]
    ):
        raise ValueError("HISTORICAL_INDEX_WINDOW_INCOMPLETE")
    prices = tuple(Fraction(price.total_return_price) for price in index.prices)
    average = sum(prices, Fraction(0)) / 120
    change = prices[-1] / prices[-61] - 1
    return (
        "BULL"
        if prices[-1] > average and change > 0
        else "BEAR"
        if prices[-1] < average and change < 0
        else "SIDEWAYS"
    )


def _factor_order(
    month: HistoricalMonthInput, command: SelectionCommand, calendar_version: str
) -> tuple[str, ...]:
    factors = month.factors
    calendar = synthetic_market_calendar(calendar_version)
    if factors is None or calendar is None or factors.available_at > command.cutoff_at:
        raise ValueError("HISTORICAL_SELECTION_VISIBLE_FACTOR_SOURCE_REQUIRED")
    history = tuple(
        session.closed_at
        for session in historical_sessions(calendar)
        if session.closed_at <= command.cutoff_at
    )
    values: dict[str, tuple[Fraction | None, ...]] = {
        row.security_id: (None,) * 4 for row in command.rows
    }
    seen: set[str] = set()
    for row in factors.rows:
        if row.security_id in seen or row.security_id not in values:
            raise ValueError("HISTORICAL_FACTOR_UNIVERSE_INCOMPLETE")
        seen.add(row.security_id)
        scalar_values = (
            row.net_income_ttm,
            row.total_capitalization,
            row.average_equity,
            row.momentum_start_price,
            row.momentum_end_price,
        )
        normalized = tuple(
            value if value is not None and value.is_finite() else None for value in scalar_values
        )
        income, capitalization, equity, start_price, end_price = normalized
        value = (
            Fraction(income) / Fraction(capitalization)
            if income is not None and capitalization is not None and capitalization > 0
            else None
        )
        quality = (
            Fraction(income) / Fraction(equity)
            if income is not None and equity is not None and equity > 0
            else None
        )
        momentum = (
            Fraction(end_price) / Fraction(start_price) - 1
            if start_price is not None
            and end_price is not None
            and min(start_price, end_price) > 0
            and len(history) >= 252
            and row.momentum_start_at == history[-252]
            and row.momentum_end_at == history[-21]
            else None
        )
        volatility = None
        if (
            len(row.daily_returns) == 120
            and row.return_dates == history[-120:]
            and all(value.is_finite() and value >= -1 for value in row.daily_returns)
        ):
            returns = tuple(Fraction(item) for item in row.daily_returns)
            mean = sum(returns, Fraction(0)) / 120
            volatility = -sum(((item - mean) ** 2 for item in returns), Fraction(0)) / 119
        values[row.security_id] = (value, quality, momentum, volatility)
    if not history or factors.effective_at != history[-1]:
        raise ValueError("HISTORICAL_FACTOR_WINDOW_INCOMPLETE")
    scores: dict[str, Fraction] = {}
    for security, inputs in values.items():
        score = Fraction(0)
        for dimension, value in enumerate(inputs):
            if value is None:
                continue
            peers = tuple(items[dimension] for items in values.values())
            below = sum(item is None or item < value for item in peers)
            tied = sum(item == value for item in peers)
            score += (below + Fraction(tied - 1, 2)) * 100 / max(1, len(peers) - 1) / 4
        scores[security] = score
    return tuple(sorted(scores, key=lambda security: (-scores[security], security)))


def paired_baselines(
    month: HistoricalMonthInput,
    command: SelectionCommand,
    selection_event_id: str,
    members: tuple[EvaluationMember, ...],
    sessions: tuple[datetime, ...],
    policy: HistoricalRegistration,
    cutoff: datetime,
) -> dict[str, BaselineResult]:
    factor_order = _factor_order(month, command, policy.market_calendar_version)
    constraints = SelectionConstraints(command)
    by_security = {member.security_id: member for member in members}
    if set(by_security) != {row.security_id for row in command.rows}:
        raise ValueError("HISTORICAL_BASELINE_OUTCOMES_INCOMPLETE")
    returns = tuple(exact_member_return(member, policy.standard_quantity) for member in members)
    universe = BaselineResult(
        positive_rate=decimal_fraction(Fraction(sum(value > 0 for value in returns), len(returns)))
        if returns
        else None,
        target_rate=decimal_fraction(
            Fraction(
                sum(value >= Fraction(policy.terminal_target) for value in returns), len(returns)
            )
        )
        if returns
        else None,
        batch_pass_rate=None,
        drawdown_pass_rate=None,
        trial_count=1,
        failed_trials=0,
        membership_digest=sha256("\n".join(sorted(by_security)).encode()).hexdigest(),
        counts=BaselineCounts(
            positive=sum(value > 0 for value in returns),
            target=sum(value >= Fraction(policy.terminal_target) for value in returns),
            member_slots=len(returns),
        ),
    )
    cache: dict[tuple[str, ...], tuple[Fraction, Fraction, Fraction, Fraction]] = {}

    def metrics(keys: tuple[str, ...]) -> tuple[Fraction, Fraction, Fraction, Fraction]:
        if not keys:
            return (Fraction(0),) * 4
        canonical = tuple(sorted(keys))
        if canonical not in cache:
            selected = tuple(by_security[key] for key in canonical)
            metrics = cohort_metrics(selected, policy)
            _, drawdown = cohort_nav(selected, month.paths, sessions, policy, cutoff)
            cache[canonical] = (
                Fraction(int(metrics["positive_count"]), len(keys)),
                Fraction(int(metrics["target_count"]), len(keys)),
                Fraction(bool(metrics["batch_pass"])),
                Fraction(drawdown <= policy.maximum_drawdown),
            )
        return cache[canonical]

    def aggregate(orders: tuple[tuple[str, ...], ...]) -> BaselineResult:
        selected = tuple(constraints.scan(order)[0] for order in orders)
        outcomes = tuple(metrics(keys) for keys in selected)
        rates = tuple(
            decimal_fraction(
                sum((values[index] for values in outcomes), Fraction(0)) / len(outcomes)
            )
            for index in range(4)
        )
        return BaselineResult(
            positive_rate=rates[0],
            target_rate=rates[1],
            batch_pass_rate=rates[2],
            drawdown_pass_rate=rates[3],
            trial_count=len(selected),
            failed_trials=sum(not keys for keys in selected),
            membership_digest=sha256(
                "\n".join(",".join(keys) for keys in selected).encode()
            ).hexdigest(),
            members=selected[0] if len(selected) == 1 else (),
            counts=BaselineCounts(
                positive=int(
                    sum((values[0] for values in outcomes), Fraction(0))
                    * policy.selection_policy.cohort_size
                ),
                target=int(
                    sum((values[1] for values in outcomes), Fraction(0))
                    * policy.selection_policy.cohort_size
                ),
                member_slots=len(selected) * policy.selection_policy.cohort_size,
                passed_trials=int(sum((values[2] for values in outcomes), Fraction(0))),
            ),
        )

    seed = int(sha256(f"{policy.version_id}:{selection_event_id}".encode()).hexdigest(), 16)
    random = Random(seed)
    securities = tuple(sorted(by_security))
    random_orders = tuple(
        tuple(random.sample(securities, len(securities))) for _ in range(policy.random_trials)
    )
    return {
        "UNIVERSE": universe,
        "RANDOM": aggregate(random_orders),
        "FOUR_FACTOR": aggregate((factor_order,)),
    }
