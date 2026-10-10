"""Conjunctive synthetic envelope checks; no real activation path exists here."""

from datetime import datetime
from typing import Literal

from stock_profiler.modules.qualification.beta_contracts import (
    BETA_REQUIRED_ROLES,
    BetaEnvelopeCommand,
    BetaOperations,
)
from stock_profiler.modules.qualification.contracts import GovernanceOutcome
from stock_profiler.modules.qualification.service import (
    current_qualification,
    qualification_is_current,
)


def qualification_reasons(
    command: BetaEnvelopeCommand,
    history: tuple[GovernanceOutcome, ...],
    now: datetime,
    *,
    evidence_cutoff: datetime | None = None,
) -> tuple[str, ...]:
    if command.scope.capability != "B2" or command.scope.purpose != "BETA_NEW_EXPOSURE":
        return ("BETA_SCOPE_MISMATCH",)
    bindings = command.qualification_bindings
    if len(bindings) != len(BETA_REQUIRED_ROLES) or {item.role for item in bindings} != set(
        BETA_REQUIRED_ROLES
    ):
        return ("BETA_QUALIFICATION_REQUIRED",)
    for binding in bindings:
        updates: dict[str, object] = {"capability": binding.role}
        if binding.role == "T4":
            updates.update(
                holding_age_domain=command.policy.statistical_age_domain,
                probability_grid=command.policy.statistical_probability_grid,
                source=command.policy.statistical_source,
                target=command.policy.statistical_target,
                purpose=command.policy.statistical_purpose,
            )
        expected_scope = command.scope.model_copy(update=updates)
        try:
            record = current_qualification(history, expected_scope, binding.version)
        except ValueError:
            return ("BETA_QUALIFICATION_AMBIGUOUS",)
        if record is None:
            return ("BETA_QUALIFICATION_UNAVAILABLE",)
        if (
            record.decision_id != binding.decision_id
            or record.status != "VALID"
            or record.authorization_id is None
            or record.authorization_evidence is None
            or any(
                not proof.scope.same_scope_as(record.scope) or proof.version != record.version
                for proof in (
                    record.evidence,
                    record.authorization_evidence,
                    record.formal_evidence,
                    record.formal_passing_evidence,
                )
                if proof is not None
            )
            or record.authorization_terminated_at is not None
            or record.recorded_at > min(now, evidence_cutoff or now)
            or not record.evidence_available_by(min(now, evidence_cutoff or now))
            or not qualification_is_current(record, now)
            or record.outstanding_alerts
            or record.restrictions
            or binding.version.policy_error is not None
        ):
            return ("BETA_QUALIFICATION_NOT_CURRENT",)
    return ()


def planned_months(now: datetime, count: int) -> tuple[str, ...]:
    """Keep every elapsed planned month, including absent or failed deliveries."""
    index = now.year * 12 + now.month - 1
    return tuple(f"{value // 12:04d}-{value % 12 + 1:02d}" for value in range(index - count, index))


def observation_history_reasons(
    command: BetaEnvelopeCommand,
    history: tuple[BetaEnvelopeCommand, ...],
    now: datetime,
) -> tuple[str, ...]:
    """Keep failed obligations when a later frozen case omits or relabels them."""
    window = set(planned_months(now, command.policy.planned_month_count))
    current = (
        {item.plan_month: item for item in command.observations.months}
        if command.observations
        else {}
    )
    for prior in history:
        if not prior.scope.same_scope_as(command.scope):
            continue
        if prior.policy.version_id == command.policy.version_id and prior.policy != command.policy:
            return ("BETA_POLICY_VERSION_CONFLICT",)
        if prior.policy != command.policy and any(
            next((item for item in prior.qualification_bindings if item.role == role), None)
            == next((item for item in command.qualification_bindings if item.role == role), None)
            for role in ("G4", "G5")
        ):
            return ("BETA_POLICY_QUALIFICATION_REQUIRED",)
        if prior.observations is None:
            continue
        days = (
            {item.closed_at: item for item in command.observations.days}
            if command.observations
            else {}
        )
        from stock_profiler.modules.portfolio.market_calendar import synthetic_market_calendar

        calendar = synthetic_market_calendar(
            command.observations.market_calendar_version
            if command.observations
            else prior.observations.market_calendar_version
        )
        daily_window = (
            {
                session.closed_at
                for session in calendar.recent_completed_sessions(
                    now, command.policy.position_day_count
                )
            }
            if calendar
            else None
        )
        for day in prior.observations.days:
            hard_failure = day.p0p1_missing or day.safety_failures
            if not hard_failure and daily_window is not None and day.closed_at not in daily_window:
                continue
            if not (
                day.p0p1_missing
                or day.safety_failures
                or not day.data_completed
                or not day.pipeline_completed
            ):
                continue
            if command.observations is None:
                return ("BETA_OPERATIONAL_HISTORY_REQUIRED",)
            if days.get(day.closed_at) != day:
                return ("BETA_OPERATIONAL_HISTORY_CONFLICT",)
        for month in prior.observations.months:
            failed = (
                month.safety_failures
                or not month.data_completed
                or not month.pipeline_completed
                or any(
                    getattr(month, f"{name}_completed") < getattr(month, f"{name}_required")
                    for name in (
                        "batch",
                        "timely",
                        "plan",
                        "notification",
                        "report",
                        "confirmation",
                        "reconciliation",
                    )
                )
            )
            if month.plan_month not in window or not failed:
                continue
            if command.observations is None:
                return ("BETA_OPERATIONAL_HISTORY_REQUIRED",)
            if current.get(month.plan_month) != month:
                return ("BETA_OPERATIONAL_HISTORY_CONFLICT",)
    return ()


def operational_audit(command: BetaEnvelopeCommand, now: datetime) -> "BetaOperations":
    from decimal import Decimal

    from stock_profiler.modules.portfolio.market_calendar import synthetic_market_calendar
    from stock_profiler.modules.qualification.beta_contracts import BetaMetric, BetaOperations

    observation = command.observations
    assert observation is not None
    window = planned_months(now, command.policy.planned_month_count)
    months = {item.plan_month: item for item in observation.months}
    missing = tuple(month for month in window if month not in months)
    complete = (
        observation.plan_months == window
        and len(months) == len(observation.months)
        and not missing
        and set(months) == set(window)
    )
    metrics = {}
    for name in (
        "data",
        "pipeline",
        "batch",
        "timely",
        "plan",
        "notification",
        "report",
        "confirmation",
        "reconciliation",
    ):
        if name in {"data", "pipeline"}:
            required = len(window)
            completed = sum(
                bool(getattr(months[month], f"{name}_completed"))
                for month in window
                if month in months
            )
        else:
            required = sum(getattr(item, f"{name}_required") for item in months.values())
            completed = sum(getattr(item, f"{name}_completed") for item in months.values())
        rate = Decimal(completed) / required if required else None
        metrics[name] = BetaMetric(
            required=required,
            completed=completed,
            rate=rate,
            status="NOT_APPLICABLE"
            if rate is None
            else "PASSED"
            if rate >= command.policy.minimum_completion
            else "FAILED",
        )
    applicable_daily = any(day.system_position_ids for day in observation.days)
    calendar = synthetic_market_calendar(observation.market_calendar_version)
    sessions = (
        calendar.recent_completed_sessions(now, command.policy.position_day_count)
        if calendar
        else ()
    )
    expected_days = tuple(session.closed_at for session in sessions)
    days = {day.closed_at: day for day in observation.days}
    daily_complete = (
        len(expected_days) == command.policy.position_day_count
        and observation.plan_days == expected_days
        and len(days) == len(observation.days) == len(expected_days)
        and set(days) == set(expected_days)
    )
    for name in ("data", "pipeline"):
        required = command.policy.position_day_count if applicable_daily else 0
        completed = (
            sum(
                bool(getattr(days[day], f"{name}_completed"))
                for day in expected_days
                if day in days
            )
            if applicable_daily
            else 0
        )
        rate = Decimal(completed) / required if required else None
        metrics[f"daily_{name}"] = BetaMetric(
            required=required,
            completed=completed,
            rate=rate,
            status="NOT_APPLICABLE"
            if rate is None
            else "PASSED"
            if rate >= command.policy.minimum_completion
            else "FAILED",
        )
    core_failure = [
        month not in months
        or not months[month].data_completed
        or not months[month].pipeline_completed
        or any(
            getattr(months[month], f"{name}_completed") < getattr(months[month], f"{name}_required")
            for name in ("batch", "timely", "plan", "notification", "report")
        )
        for month in window
    ]
    consecutive = any(a and b for a, b in zip(core_failure, core_failure[1:], strict=False))
    failures = tuple(
        dict.fromkeys(reason for item in observation.months for reason in item.safety_failures)
    )
    return BetaOperations(
        plan_months=window,
        missing_months=missing,
        window_complete=complete,
        daily_window_complete=daily_complete,
        metrics=metrics,
        consecutive_core_failure=consecutive,
        safety_failures=failures,
        passed=complete
        and (not applicable_daily or daily_complete)
        and not consecutive
        and not failures
        and all(item.status != "FAILED" for item in metrics.values()),
    )


def envelope_decision(
    command: BetaEnvelopeCommand,
    now: datetime,
    history: tuple[GovernanceOutcome, ...],
    *,
    evidence_cutoff: datetime | None = None,
) -> tuple[Literal["INITIAL", "EXPANDED"], tuple[str, ...], BetaOperations | None, bool]:
    """Adjudicate fictional observation inputs without granting real permissions."""
    from stock_profiler.modules.portfolio.market_calendar import synthetic_market_calendar

    now = min(now, evidence_cutoff or now)
    observation = command.observations
    requested = command.requested_envelope == "EXPANDED"
    if observation is None:
        return "INITIAL", ("BETA_EXPANSION_OBSERVATION_REQUIRED",) if requested else (), None, True
    audit = operational_audit(command, now)
    day_failures = [
        not day.data_completed or not day.pipeline_completed
        for day in sorted(observation.days, key=lambda item: item.closed_at)
    ]
    daily_failed = bool(observation.days) and (
        any(
            sum(getattr(day, f"{name}_completed") for day in observation.days)
            < len(observation.days) * command.policy.minimum_completion
            for name in ("data", "pipeline")
        )
        or any(a and b for a, b in zip(day_failures, day_failures[1:], strict=False))
    )
    if (
        not audit.passed
        or daily_failed
        or any(day.safety_failures or day.p0p1_missing for day in observation.days)
    ):
        return "INITIAL", ("BETA_OPERATIONAL_GATE_FAILED",), audit, False
    if (
        not observation.scope.same_scope_as(command.scope)
        or observation.activation_id != command.activation_id
        or observation.policy_version != command.policy.version_id
        or observation.qualification_bindings != command.qualification_bindings
        or observation.simulated_origin != "REAL_TIME"
        or not observation.registered_at
        < observation.activated_at
        <= observation.available_at
        <= now
        or observation.effective_at > now
        or observation.effective_at < observation.available_at
    ):
        return "INITIAL", ("BETA_OBSERVATION_BASIS_MISMATCH",), None, True
    if not requested:
        return "INITIAL", (), audit, True
    clean_months = planned_months(now, command.policy.clean_month_count)
    months = {item.plan_month: item for item in observation.months}
    calendar = synthetic_market_calendar(observation.market_calendar_version)
    month_closes = {}
    if calendar is not None:
        for month in months:
            year, number = map(int, month.split("-"))
            boundary = now.replace(
                year=year + number // 12,
                month=number % 12 + 1,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            closes = calendar.recent_completed_sessions(boundary, 1)
            month_closes[month] = closes[0].closed_at if closes else None
    clean = all(
        month in months
        and months[month].closed_at == month_closes.get(month)
        and observation.activated_at <= months[month].closed_at <= observation.available_at
        and months[month].data_completed
        and months[month].pipeline_completed
        and all(
            getattr(months[month], f"{name}_completed")
            == getattr(months[month], f"{name}_required")
            for name in (
                "batch",
                "timely",
                "plan",
                "notification",
                "report",
                "confirmation",
                "reconciliation",
            )
        )
        for month in clean_months
    )
    calendar = synthetic_market_calendar(observation.market_calendar_version)
    sessions = (
        calendar.recent_completed_sessions(now, command.policy.position_day_count)
        if calendar
        else ()
    )
    expected_days = tuple(session.closed_at for session in sessions)
    days = {day.closed_at: day for day in observation.days}
    day_window = (
        len(expected_days) == command.policy.position_day_count
        and observation.plan_days == expected_days
        and len(days) == len(observation.days) == len(expected_days)
        and set(days) == set(expected_days)
        and all(
            day.closed_at >= observation.activated_at
            and day.closed_at <= observation.available_at
            and bool(day.system_position_ids)
            and all(day.system_position_ids)
            and not day.p0p1_missing
            for day in observation.days
        )
    )
    plans = observation.terminal_plans
    terminal = (
        bool(plans)
        and any(
            plan.terminal_outcome in {"PARTIALLY_FILLED", "FULLY_FILLED"}
            and plan.accepted_intents > 0
            and plan.fill_ids
            and plan.funds_reconciliation_id
            and plan.position_reconciliation_id
            for plan in plans
        )
        and len({plan.plan_id for plan in plans}) == len(plans)
        and all(
            plan.authoritative
            and observation.activated_at <= plan.closed_at <= observation.available_at
            and plan.remaining_reservation == 0
            and not plan.unknown_orders
            and plan.execution_deviations_reconciled
            and (
                plan.accepted_intents == 0
                and not plan.broker_order_ids
                and not plan.fill_ids
                or plan.accepted_intents > 0
                and bool(plan.funds_reconciliation_id)
                and bool(plan.position_reconciliation_id)
            )
            and (
                plan.terminal_outcome in {"NO_TRADE", "ALL_DECLINED", "DEFERRED_EXPIRED"}
                and not plan.broker_order_ids
                and not plan.fill_ids
                or plan.terminal_outcome in {"PARTIALLY_FILLED", "FULLY_FILLED"}
                and bool(plan.broker_order_ids)
                and bool(plan.fill_ids)
                and plan.accepted_intents > 0
            )
            for plan in plans
        )
    )
    day_completion = all(
        sum(getattr(day, f"{name}_completed") for day in observation.days)
        >= command.policy.position_day_count * command.policy.minimum_completion
        for name in ("data", "pipeline")
    )
    day_failures = [
        not day.data_completed or not day.pipeline_completed for day in observation.days
    ]
    day_completion = day_completion and not any(
        a and b for a, b in zip(day_failures, day_failures[1:], strict=False)
    )
    if qualification_reasons(command, history, now, evidence_cutoff=observation.activated_at):
        return "INITIAL", ("BETA_OBSERVATION_QUALIFICATION_UNAVAILABLE",), audit, True
    if not (
        clean and day_window and day_completion and terminal and observation.expansion_confirmed
    ):
        return "INITIAL", ("BETA_EXPANSION_OBSERVATION_INCOMPLETE",), audit, True
    return "EXPANDED", (), audit, True
