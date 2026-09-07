"""Capital facts are derived from committed authorization and position evidence."""

from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Literal

from stock_profiler.modules.portfolio.contracts import PortfolioAuthorizationOutcome
from stock_profiler.modules.portfolio.drawdown_contracts import (
    DrawdownCommand,
    DrawdownOutcome,
    DrawdownState,
    ExactRatio,
)
from stock_profiler.modules.portfolio.drawdown_flows import (
    adjusted_units,
    decimal_value,
    transfer_keys,
)
from stock_profiler.modules.portfolio.market_calendar import synthetic_market_calendar
from stock_profiler.modules.position_management.contracts import PositionReconciliationOutcome


def adjudicate(
    command: DrawdownCommand,
    *,
    event_id: str,
    account_ids: tuple[str, ...],
    authorizations: tuple[PortfolioAuthorizationOutcome, ...],
    position: PositionReconciliationOutcome | None,
    history: tuple[DrawdownOutcome, ...],
    business_prerequisite_met: bool,
    before_positions: dict[str, PositionReconciliationOutcome | None],
) -> DrawdownOutcome:
    capital = next(
        (
            item.authorization
            for item in reversed(authorizations)
            if item.authorization is not None
            and item.authorization.authorization_id == command.authorization_id
        ),
        None,
    )
    if capital is None or not business_prerequisite_met:
        return DrawdownOutcome(disposition="DENIED", reasons=("CAPITAL_AUTHORIZATION_REQUIRED",))
    budget = capital.proposal.risk_budget
    if (
        capital.proposal.portfolio_id != command.portfolio_id
        or set(capital.proposal.snapshot.selected_account_ids) != set(account_ids)
        or not capital.evidence_available_by(command.cutoff_at)
        or budget.effective_at > command.cutoff_at
        or (command.operation in {"OPEN", "REAUTHORIZE"} and command.cutoff_at >= budget.expires_at)
    ):
        return DrawdownOutcome(disposition="DENIED", reasons=("CAPITAL_AUTHORIZATION_SCOPE",))
    prior = next((item.state for item in reversed(history) if item.state is not None), None)
    if command.operation == "OPEN" and (
        prior is not None or command.previous_decision_id is not None
    ):
        return DrawdownOutcome(disposition="DENIED", reasons=("CAPITAL_EPOCH_ALREADY_EXISTS",))
    if command.operation != "OPEN" and (
        prior is None
        or command.previous_decision_id != prior.decision_id
        or (command.operation != "REAUTHORIZE" and command.epoch_id != prior.epoch_id)
        or command.portfolio_id != prior.portfolio_id
        or set(account_ids) != set(prior.account_ids)
        or command.cutoff_at <= prior.cutoff_at
        or command.policy != prior.policy
    ):
        return DrawdownOutcome(disposition="DENIED", reasons=("CAPITAL_EPOCH_REVISION_CONFLICT",))
    predecessor_epoch_id = prior.previous_epoch_id if prior is not None else None
    if command.operation == "REAUTHORIZE":
        confirmation = command.reauthorization
        if (
            prior is None
            or prior.epoch_status != "CLOSED"
            or prior.closed_at is None
            or prior.cooling_sessions < prior.policy.cooling_sessions
            or prior.authorization_id == command.authorization_id
            or confirmation is None
            or confirmation.authorization_id != command.authorization_id
            or confirmation.previous_epoch_id != prior.epoch_id
            or confirmation.epoch_id != command.epoch_id
            or not prior.cutoff_at < confirmation.confirmed_at <= command.cutoff_at
            or capital.confirmation.confirmed_at != confirmation.confirmed_at
            or any(
                item.state is not None and item.state.epoch_id == command.epoch_id
                for item in history
            )
            or command.capital_flows
        ):
            return DrawdownOutcome(
                disposition="DENIED", reasons=("CAPITAL_REAUTHORIZATION_REQUIRED",)
            )
        predecessor_epoch_id = prior.epoch_id
    if (
        synthetic_market_calendar(command.policy.market_calendar_version_id) is None
        or command.policy.caution_recovery_ratio >= budget.drawdown.caution_ratio
        or command.policy.defensive_recovery_ratio >= budget.drawdown.defensive_ratio
    ):
        return DrawdownOutcome(disposition="DENIED", reasons=("DRAWDOWN_POLICY_INVALID",))
    if (
        position is None
        or position.disposition != "RECONCILED"
        or position.snapshot.cutoff_at != command.cutoff_at
        or set(item.account_id for item in position.snapshot.cash_states) != set(account_ids)
        or position.snapshot.total_account_equity is None
        or command.valuation.liquidation_cost is None
        or command.valuation.evidence.problem_codes(
            command.cutoff_at, require_current_completeness=True
        )
    ):
        retained = (
            prior.model_copy(
                update={
                    "decision_id": event_id,
                    "cutoff_at": command.cutoff_at,
                    "valuation": command.valuation,
                    "net_liquidation_equity": None,
                    "unit_nav": None,
                    "current_drawdown": None,
                    "current_stock_exposure": None,
                    "new_exposure_blocked": True,
                    "recovery_sessions": 0,
                    "last_recovery_session": None,
                    "cooling_sessions": 0,
                    "execution_blocked": True,
                    "stock_exposure_target_value": None,
                }
            )
            if prior is not None
            else None
        )
        return DrawdownOutcome(
            disposition="UNKNOWN", reasons=("DRAWDOWN_EVIDENCE_UNKNOWN",), state=retained
        )
    with localcontext() as context:
        context.prec = 50
        equity = position.snapshot.total_account_equity - command.valuation.liquidation_cost
    if equity <= 0 and command.operation in {"OPEN", "REAUTHORIZE"}:
        return DrawdownOutcome(disposition="DENIED", reasons=("POSITIVE_INITIAL_CAPITAL_REQUIRED",))
    fully_reconciled_zero = (
        all(unit.total_quantity == 0 for unit in position.snapshot.action_units)
        and not position.snapshot.unfinished_orders
        and not any(item.active for item in position.snapshot.execution_restrictions)
        and all(
            cash.frozen_cash == 0 and cash.receivable_cash == 0 and cash.payable_cash == 0
            for cash in position.snapshot.cash_states
        )
    )
    if command.operation in {"CLOSE", "REAUTHORIZE"} and not fully_reconciled_zero:
        return DrawdownOutcome(
            disposition="DENIED", reasons=("CAPITAL_EXECUTION_RECONCILIATION_REQUIRED",)
        )
    if command.operation == "CLOSE" and (prior is None or prior.epoch_status == "CLOSED"):
        return DrawdownOutcome(disposition="DENIED", reasons=("OPEN_CAPITAL_EPOCH_REQUIRED",))
    if command.operation == "REAUTHORIZE":
        prior = None
    units_fraction = prior.exact_units.fraction if prior is not None else Fraction(equity)
    peak_fraction = prior.exact_peak.fraction if prior is not None else Fraction(1)
    flow_ids = prior.processed_flow_ids if prior is not None else ()
    interval_drawdown = Fraction(0)
    if prior is not None:
        try:
            units_fraction, peak_fraction, flow_ids, interval_drawdown = adjusted_units(
                command, prior, position, before_positions
            )
        except ValueError as error:
            return DrawdownOutcome(
                disposition="UNKNOWN",
                reasons=(str(error),),
                state=prior.model_copy(
                    update={
                        "decision_id": event_id,
                        "cutoff_at": command.cutoff_at,
                        "unit_nav": None,
                        "current_drawdown": None,
                        "net_liquidation_equity": None,
                        "current_stock_exposure": None,
                        "new_exposure_blocked": True,
                        "recovery_sessions": 0,
                        "last_recovery_session": None,
                        "cooling_sessions": 0,
                        "execution_blocked": True,
                        "stock_exposure_target_value": None,
                    }
                ),
            )
    nav_fraction = Fraction(equity) / units_fraction
    peak_fraction = max(peak_fraction, nav_fraction)
    drawdown_fraction = 1 - nav_fraction / peak_fraction
    interval_drawdown = max(interval_drawdown, drawdown_fraction)
    with localcontext() as context:
        context.prec = 50
        units = decimal_value(units_fraction)
        nav = decimal_value(nav_fraction)
        peak = decimal_value(peak_fraction)
        drawdown = decimal_value(drawdown_fraction)
        maximum = max(
            prior.maximum_drawdown if prior is not None else Decimal(0),
            decimal_value(interval_drawdown),
        )
        exposure = sum(
            (
                item.current_market_exposure or Decimal(0)
                for item in position.snapshot.issuer_exposures
            ),
            Decimal(0),
        )
    risk = prior.risk_state if prior is not None else "NORMAL"
    if interval_drawdown >= budget.drawdown.preservation_ratio:
        risk = "PRESERVATION"
    elif risk != "PRESERVATION" and interval_drawdown >= budget.drawdown.defensive_ratio:
        risk = "DEFENSIVE"
    elif risk == "NORMAL" and interval_drawdown >= budget.drawdown.caution_ratio:
        risk = "CAUTION"
    count = 0
    last_session = None
    calendar = synthetic_market_calendar(command.policy.market_calendar_version_id)
    session = (
        calendar.session_for(command.market_session_ordinal)
        if calendar is not None and command.market_session_ordinal is not None
        else None
    )
    next_session = calendar.next_session_after(session.closed_at) if calendar and session else None
    recovery_gate = command.other_risk_gate
    valid_close = (
        session is not None
        and position.snapshot.evidence_clock.business_effective_at == session.closed_at
        and session.closed_at <= command.cutoff_at
        and (next_session is None or command.cutoff_at < next_session.closed_at)
        and recovery_gate is not None
        and not recovery_gate.hard_gate_active
        and not recovery_gate.evidence.problem_codes(
            command.cutoff_at, require_current_completeness=True
        )
    )
    if prior is not None and risk == prior.risk_state and valid_close and session is not None:
        eligible = (risk == "CAUTION" and drawdown < command.policy.caution_recovery_ratio) or (
            risk == "DEFENSIVE"
            and drawdown < command.policy.defensive_recovery_ratio
            and exposure <= equity * command.policy.defensive_exposure_ratio
        )
        if eligible:
            count = (
                prior.recovery_sessions + 1
                if prior.last_recovery_session == session.ordinal - 1
                else 1
            )
            last_session = session.ordinal
            required = (
                command.policy.caution_recovery_sessions
                if risk == "CAUTION"
                else command.policy.defensive_recovery_sessions
            )
            if count >= required:
                risk = "NORMAL" if risk == "CAUTION" else "CAUTION"
                count = 0
                last_session = None
    epoch_status = prior.epoch_status if prior is not None else "OPEN"
    closed_at = prior.closed_at if prior is not None else None
    cooling = 0
    if command.operation == "CLOSE":
        epoch_status = "CLOSED"
        closed_at = command.cutoff_at
        count = 0
        last_session = None
    elif epoch_status == "CLOSED" and prior is not None:
        count = 0
        if fully_reconciled_zero and valid_close and session is not None:
            cooling = (
                prior.cooling_sessions + 1
                if prior.last_recovery_session == session.ordinal - 1
                else 1
            )
            last_session = session.ordinal
    limit = (
        Decimal(0)
        if risk == "PRESERVATION"
        else command.policy.defensive_exposure_ratio
        if risk == "DEFENSIVE"
        else None
    )
    target = (
        decimal_value(Fraction(max(equity, Decimal(0))) * Fraction(limit))
        if limit is not None
        else None
    )
    direction: Literal["REDUCE", "EXIT"] | None = (
        "EXIT"
        if risk == "PRESERVATION"
        else "REDUCE"
        if target is not None and exposure > target
        else None
    )
    sellable_exposure = sum(
        (
            Fraction(unit.exact_statistical_action_quantity or 0) * Fraction(unit.market_price or 0)
            for unit in position.snapshot.action_units
        ),
        Fraction(0),
    )
    blocked = target is not None and Fraction(exposure) - sellable_exposure > Fraction(target)
    state = DrawdownState(
        decision_id=event_id,
        epoch_id=command.epoch_id,
        portfolio_id=command.portfolio_id,
        authorization_id=command.authorization_id,
        account_ids=account_ids,
        cutoff_at=command.cutoff_at,
        policy=command.policy,
        thresholds=budget.drawdown,
        net_liquidation_equity=equity,
        units=units,
        unit_nav=nav,
        high_water_nav=peak,
        maximum_drawdown=maximum,
        current_drawdown=drawdown,
        risk_state=risk,
        new_exposure_blocked=(
            risk != "NORMAL" or epoch_status == "CLOSED" or command.cutoff_at >= budget.expires_at
        ),
        valuation=command.valuation,
        current_stock_exposure=exposure,
        stock_exposure_limit=limit,
        risk_direction=direction,
        execution_blocked=blocked,
        stock_exposure_target_value=target,
        recovery_sessions=count,
        last_recovery_session=last_session,
        exact_units=ExactRatio.from_fraction(units_fraction),
        exact_peak=ExactRatio.from_fraction(peak_fraction),
        processed_transfers=transfer_keys(position),
        processed_flow_ids=flow_ids,
        epoch_status=epoch_status,
        closed_at=closed_at,
        cooling_sessions=cooling,
        previous_epoch_id=predecessor_epoch_id,
        reauthorization=command.reauthorization if prior is None else prior.reauthorization,
    )
    return DrawdownOutcome(
        disposition="ACCEPTED",
        reasons=("CAPITAL_EPOCH_OPENED" if prior is None else "DRAWDOWN_OBSERVED",),
        state=state,
    )
