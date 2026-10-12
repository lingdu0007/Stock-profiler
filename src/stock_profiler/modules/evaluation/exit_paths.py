"""Executable paired exit paths from frozen forecasts and complete market sessions."""

from datetime import datetime
from decimal import Context, Decimal, localcontext
from typing import Literal

from stock_profiler.modules.evaluation.contracts import StandardExitPosition
from stock_profiler.modules.evaluation.exit_contracts import (
    ExitDecisionLabel,
    ExitForecast,
    ExitObservation,
    ExitPolicy,
    ExitPolicyPath,
    ExitPositionResult,
    StandardExitTrade,
)
from stock_profiler.modules.portfolio.market_calendar import synthetic_market_calendar

BASELINES = ("HOLD_END", "HARD_ONLY", "FIXED_LOSS", "TRAILING_DRAWDOWN")


def statistical_direction(
    forecast: ExitForecast, policy: ExitPolicy
) -> Literal["HOLD", "REDUCE", "EXIT", "ABSTAIN", "DATA_FAILED"]:
    if not forecast.data_complete:
        return "DATA_FAILED"
    if not forecast.qualified_scope or forecast.state not in policy.states:
        return "ABSTAIN"
    value = forecast.value
    downside = forecast.downside.get(str(policy.selected_grid))
    if value is not None and value.upper <= policy.denied_value:
        return "EXIT"
    if value is None or downside is None:
        return "ABSTAIN"
    if downside.lower >= policy.risky_downside:
        return "REDUCE"
    if downside.upper <= policy.safe_downside and value.lower >= policy.established_value:
        return "HOLD"
    return "ABSTAIN"


def unlabeled_forecast(forecast: ExitForecast, policy: ExitPolicy) -> ExitDecisionLabel:
    direction = statistical_direction(forecast, policy)
    return ExitDecisionLabel(
        prediction_id=forecast.prediction_id,
        cutoff_at=forecast.cutoff_at,
        state=forecast.state,
        direction=direction,
        statistical=direction in {"HOLD", "REDUCE", "EXIT"} and not forecast.hard_exit,
        forecast=forecast,
    )


def evaluate_exit_position(
    position: StandardExitPosition,
    observation: ExitObservation | None,
    policy: ExitPolicy,
    quantity: Decimal,
    cutoff: datetime,
    frozen_forecasts: tuple[ExitForecast, ...] = (),
) -> ExitPositionResult:
    with localcontext(Context(prec=128)):
        result = _evaluate(position, observation, policy, quantity, cutoff, frozen_forecasts)
        replay = _evaluate(position, observation, policy, quantity, cutoff, frozen_forecasts)
        return result.model_copy(update={"replay_consistent": result == replay})


def _evaluate(
    position: StandardExitPosition,
    observation: ExitObservation | None,
    policy: ExitPolicy,
    quantity: Decimal,
    cutoff: datetime,
    frozen_forecasts: tuple[ExitForecast, ...] = (),
) -> ExitPositionResult:
    base = position.model_dump()
    base["initial_quantity"] = quantity
    if cutoff >= position.matures_at:
        base["state"] = "UNAVAILABLE"
    if observation is None:
        return ExitPositionResult(
            **base,
            observation=None,
            policies={},
            decisions=tuple(unlabeled_forecast(forecast, policy) for forecast in frozen_forecasts),
            missing_reasons=("EXIT_PATH_MISSING",),
        )
    calendar = synthetic_market_calendar(policy.market_calendar_version)
    if calendar is None:
        raise ValueError("EXIT_CALENDAR_UNAVAILABLE")
    if observation.available_at > cutoff or observation.effective_at > cutoff:
        raise ValueError("EXIT_EVIDENCE_NOT_AVAILABLE_AT_CUTOFF")
    rows = observation.sessions
    if any(row.costs != policy.cost_curve for row in rows):
        raise ValueError("EXIT_COST_CURVE_CHANGED")
    if not rows:
        raise ValueError("EXIT_PATH_EMPTY")
    expected = calendar.recent_completed_sessions(rows[-1].closed_at, 10000)
    expected_dates = tuple(
        session.closed_at for session in expected if session.closed_at >= position.entry_at
    )
    if (
        tuple(row.closed_at for row in rows) != expected_dates
        or rows[-1].closed_at > observation.effective_at
    ):
        raise ValueError("EXIT_PATH_CALENDAR_INCOMPLETE")
    retained_forecasts = frozen_forecasts or observation.forecasts
    forecasts = {item.cutoff_at: item for item in retained_forecasts}
    if len({item.cutoff_at for item in observation.forecasts}) != len(observation.forecasts) or len(
        {item.prediction_id for item in observation.forecasts}
    ) != len(observation.forecasts):
        raise ValueError("EXIT_FORECAST_IDENTITY_CONFLICT")
    for prediction in observation.forecasts:
        if (
            prediction.cutoff_at not in expected_dates
            or prediction.cutoff_at > position.matures_at
            or prediction.model_version != policy.model_version
            or prediction.frozen_at > cutoff
        ):
            raise ValueError("EXIT_FORECAST_CONTRACT_MISMATCH")
        values = [prediction.downside.get(str(grid)) for grid in policy.loss_grids]
        if prediction.data_complete and (
            any(value is None for value in values) or prediction.value is None
        ):
            raise ValueError("EXIT_COMPLETE_FORECAST_TARGET_MISSING")
        points = [value.point for value in values if value is not None]
        if any(left < right for left, right in zip(points, points[1:], strict=False)):
            raise ValueError("EXIT_DOWNSIDE_GRID_NOT_MONOTONE")
    policy_rows = tuple(row for row in rows if row.closed_at <= min(cutoff, position.matures_at))
    initial_wealth = quantity * position.entry_price
    paths: dict[str, ExitPolicyPath] = {}
    labels: list[ExitDecisionLabel] = []
    for name in ("PRIMARY", *BASELINES):
        exposure = Decimal(1)
        target = Decimal(1)
        cash = Decimal(0)
        paid = Decimal(0)
        dividends = Decimal(0)
        peak = initial_wealth
        drawdown = Decimal(0)
        market_peak = initial_wealth
        trade_signal = position.entry_at
        trades: list[StandardExitTrade] = []
        wealths: list[Decimal] = []
        previous_direction = None
        previous_evidence = None
        previous_index = None
        for index, row in enumerate(policy_rows):
            # Cash actions apply to exposure held at the start of the session.
            distribution = quantity * row.cash_per_initial_share * exposure
            cash += distribution
            dividends += quantity * row.cash_per_initial_share
            if index and exposure > target and row.sellable:
                sold = exposure - target
                gross = quantity * row.quantity_multiplier * sold * row.vwap
                cost = row.costs.charge(gross)
                cash += gross - cost
                paid += cost
                trades.append(
                    StandardExitTrade(
                        signal_at=trade_signal,
                        executed_at=row.closed_at,
                        exposure_sold=sold,
                        price=row.vwap,
                        costs=cost,
                        net_proceeds=gross - cost,
                    )
                )
                exposure = target
            remaining = quantity * row.quantity_multiplier * exposure * row.price
            # A blocked terminal is marked at residual value; it does not invent a sale.
            liquidation_cost = row.costs.charge(remaining) if row.sellable else Decimal(0)
            wealth = cash + remaining - liquidation_cost
            wealths.append(wealth)
            peak = max(peak, wealth)
            if peak > 0:
                drawdown = max(drawdown, 1 - wealth / peak)
            full_value = quantity * row.quantity_multiplier * row.price + dividends
            full_net = full_value - row.costs.charge(quantity * row.quantity_multiplier * row.price)
            market_peak = max(market_peak, full_net)
            forecast = forecasts.get(row.closed_at)
            proposed = Decimal(1)
            direction = statistical_direction(forecast, policy) if forecast else "DATA_FAILED"
            if name == "PRIMARY":
                if direction == "EXIT":
                    proposed = Decimal(0)
                elif direction == "REDUCE":
                    proposed = Decimal(".5")
                if forecast:
                    labels.append(
                        ExitDecisionLabel(
                            prediction_id=forecast.prediction_id,
                            cutoff_at=forecast.cutoff_at,
                            state=forecast.state,
                            direction=direction,
                            statistical=direction in {"HOLD", "REDUCE", "EXIT"}
                            and not forecast.hard_exit,
                            reversal_without_evidence=previous_direction is not None
                            and direction in {"HOLD", "REDUCE", "EXIT"}
                            and direction != previous_direction
                            and previous_evidence == forecast.new_evidence_id
                            and previous_index is not None
                            and index - previous_index <= 20,
                            forecast=forecast,
                        )
                    )
                    if direction in {"HOLD", "REDUCE", "EXIT"}:
                        previous_direction, previous_evidence, previous_index = (
                            direction,
                            forecast.new_evidence_id,
                            index,
                        )
            elif name == "FIXED_LOSS":
                loss = 1 - full_net / initial_wealth
                proposed = (
                    Decimal(0)
                    if loss >= policy.fixed_exit_loss
                    else Decimal(".5")
                    if loss >= policy.fixed_reduce_loss
                    else Decimal(1)
                )
            elif name == "TRAILING_DRAWDOWN":
                loss = 1 - full_net / market_peak if market_peak > 0 else Decimal(0)
                proposed = (
                    Decimal(0)
                    if loss >= policy.trailing_exit_loss
                    else Decimal(".5")
                    if loss >= policy.trailing_reduce_loss
                    else Decimal(1)
                )
            if name != "HOLD_END" and forecast and forecast.hard_exit:
                proposed = Decimal(0)
            if proposed < target:
                target, trade_signal = proposed, row.closed_at
        terminal_known = (
            bool(policy_rows)
            and policy_rows[-1].closed_at == position.matures_at
            and cutoff >= position.matures_at
        )
        terminal_cost = (
            policy_rows[-1].costs.charge(
                quantity * policy_rows[-1].quantity_multiplier * exposure * policy_rows[-1].price
            )
            if policy_rows and policy_rows[-1].sellable
            else Decimal(0)
        )
        paths[name] = ExitPolicyPath(
            current_exposure=exposure,
            pending_target=target,
            trades=tuple(trades),
            daily_wealth=tuple(wealths),
            dates=tuple(row.closed_at for row in policy_rows),
            net_total_return=wealths[-1] / initial_wealth - 1 if terminal_known else None,
            gross_total_return=(wealths[-1] + paid + terminal_cost) / initial_wealth - 1
            if terminal_known
            else None,
            maximum_drawdown=drawdown,
            exit_cost=paid + terminal_cost if terminal_known else paid,
            turnover=sum((trade.exposure_sold for trade in trades), Decimal(0)),
        )
    primary = paths["PRIMARY"]
    missing = tuple(
        "FORECAST_MISSING:" + row.closed_at.isoformat()
        for row in policy_rows
        if row.closed_at not in forecasts
    )
    labeled: list[ExitDecisionLabel] = []
    for label in labels:
        origin_index = expected_dates.index(label.cutoff_at)
        forward = calendar.sessions_after_open(label.cutoff_at, 60)
        actual_forward = rows[origin_index + 1 : origin_index + 61]
        # Each target waits for its complete separate market-session horizon.
        downside = None
        value = None
        capture = None
        origin = rows[origin_index]
        exit_cash = quantity * origin.quantity_multiplier * origin.price
        exit_cash -= origin.costs.charge(exit_cash)
        if exit_cash > 0:
            forward_wealth: list[Decimal] = []
            cash_actions = Decimal(0)
            for row in actual_forward:
                cash_actions += quantity * row.cash_per_initial_share
                market = quantity * row.quantity_multiplier * row.price
                forward_wealth.append(market + cash_actions - row.costs.charge(market))
            if (
                len(forward) >= 20
                and len(actual_forward) >= 20
                and actual_forward[19].closed_at == forward[19].closed_at
            ):
                downside = min(forward_wealth[:20]) / exit_cash - 1
            if (
                len(forward) == 60
                and len(actual_forward) == 60
                and actual_forward[59].closed_at == forward[59].closed_at
            ):
                value = forward_wealth[59] / exit_cash - 1
                if value > 0:
                    end_index = min(origin_index + 60, len(primary.daily_wealth) - 1)
                    policy_gain = (
                        primary.daily_wealth[end_index] - primary.daily_wealth[origin_index]
                    )
                    capture = max(Decimal(0), policy_gain) / (forward_wealth[59] - exit_cash)
        labeled.append(
            label.model_copy(
                update={
                    "downside_return": downside,
                    "value_return": value,
                    "upside_capture": capture,
                }
            )
        )
    labeled.extend(
        unlabeled_forecast(forecast, policy)
        for forecast in retained_forecasts
        if forecast.cutoff_at not in expected_dates
    )
    labeled.sort(key=lambda label: label.cutoff_at)
    due = cutoff >= position.matures_at
    base["current_exposure"] = primary.current_exposure
    base["state"] = (
        "EVALUABLE"
        if due and primary.net_total_return is not None and not missing
        else "UNAVAILABLE"
        if due
        else "PENDING"
    )
    return ExitPositionResult(
        **base,
        observation=observation,
        policies=paths,
        decisions=tuple(labeled),
        missing_reasons=missing
        + (("TERMINAL_PATH_MISSING",) if due and primary.net_total_return is None else ()),
    )
