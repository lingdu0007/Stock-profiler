"""Resolve saved monthly cohort, path and paired-baseline facts without qualification."""

from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from zoneinfo import ZoneInfo

from stock_profiler.modules.candidate_selection.selection import freeze_selection
from stock_profiler.modules.candidate_selection.universe import freeze_universe
from stock_profiler.modules.decision_cases.domain import (
    ResultAccessScope,
    business_outcome_result,
)
from stock_profiler.modules.decision_cases.ports import DecisionLedger, Transaction
from stock_profiler.modules.evaluation.cohort_metrics import (
    cohort_metrics,
    cohort_nav,
    decimal_fraction,
    exact_member_return,
    historical_sessions,
)
from stock_profiler.modules.evaluation.contracts import EvaluationMember
from stock_profiler.modules.evaluation.historical_baselines import market_regime, paired_baselines
from stock_profiler.modules.evaluation.historical_contracts import (
    CohortEvaluationPolicy,
    HistoricalMonthInput,
    HistoricalMonthResult,
)
from stock_profiler.modules.evaluation.service import evaluate_member
from stock_profiler.modules.portfolio.market_calendar import (
    six_month_terminal_evaluation_at,
    synthetic_market_calendar,
)


def resolve_cohort_month(
    month_input: HistoricalMonthInput | None,
    month: str,
    registration: CohortEvaluationPolicy,
    registered_at: datetime,
    access_scope: ResultAccessScope,
    cutoff_at: datetime,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
) -> HistoricalMonthResult:
    if month_input is None:
        calendar = synthetic_market_calendar(registration.market_calendar_version)
        if calendar is None:
            raise ValueError("HISTORICAL_CALENDAR_UNAVAILABLE")
        cutoffs = tuple(
            session.closed_at
            for session in historical_sessions(calendar)
            if session.closed_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m") == month
        )
        return HistoricalMonthResult(
            plan_month=month,
            disposition="FUTURE" if cutoffs and cutoff_at < cutoffs[-1] else "MISSING",
        )
    source = ledger.get_decision_event(month_input.selection_event_id, connection)
    if (
        source is None
        or source.case.access_scope is None
        or not source.case.access_scope.same_scope_as(access_scope)
        or source.result.selection is None
        or source.corrects_event_id is not None
        or datetime.fromisoformat(source.committed_at) > cutoff_at
    ):
        raise ValueError("HISTORICAL_SELECTION_SOURCE_UNAVAILABLE")
    selection = source.result.selection
    selection_command = source.case.selection
    if selection_command is None:
        return HistoricalMonthResult(
            plan_month=month,
            disposition="UNKNOWN",
            reasons=("STRICT_POINT_IN_TIME_SOURCE_REQUIRED",),
        )
    if (
        selection.cutoff_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m") != month
        or source.case.version_bundle != registration.source_version_bundle
        or selection.policy != registration.selection_policy
        or selection_command.strategy_version != registration.strategy_version
        or registered_at > selection.cutoff_at
    ):
        raise ValueError("HISTORICAL_SELECTION_CONTRACT_MISMATCH")
    universe_source = ledger.get_decision_event(selection.universe_event_id, connection)
    if (
        universe_source is None
        or universe_source.case.access_scope is None
        or not universe_source.case.access_scope.same_scope_as(access_scope)
    ):
        raise ValueError("HISTORICAL_UNIVERSE_SOURCE_UNAVAILABLE")
    if (
        universe_source.case.universe is None
        or universe_source.result.universe is None
        or universe_source.corrects_event_id is not None
        or datetime.fromisoformat(universe_source.committed_at)
        > datetime.fromisoformat(source.committed_at)
    ):
        raise ValueError("HISTORICAL_UNIVERSE_SOURCE_UNAVAILABLE")
    universe_replay = freeze_universe(
        universe_source.case.universe,
        business_prerequisite_met=business_outcome_result(
            universe_source.case.expected_external_result
        ).status
        == "SUCCEEDED",
    )
    if universe_replay != universe_source.result.universe:
        raise ValueError("HISTORICAL_UNIVERSE_WALK_FORWARD_REPLAY_FAILED")
    replay = freeze_selection(
        selection_command,
        universe_source.case.universe,
        universe_source.result.universe,
        prerequisite=business_outcome_result(source.case.expected_external_result).status,
    )
    if replay != selection:
        raise ValueError("HISTORICAL_SELECTION_WALK_FORWARD_REPLAY_FAILED")
    calendar = synthetic_market_calendar(registration.market_calendar_version)
    if calendar is None:
        raise ValueError("HISTORICAL_CALENDAR_UNAVAILABLE")
    sessions = calendar.sessions_after_open(selection.cutoff_at, 5)
    if len(sessions) != 5:
        raise ValueError("HISTORICAL_ENTRY_WINDOW_UNAVAILABLE")
    maturity = six_month_terminal_evaluation_at(sessions[-1].closed_at, calendar.version_id)
    result = HistoricalMonthResult(
        plan_month=month,
        disposition=selection.disposition,
        selection_event_id=source.decision_event_id,
        members=selection.members,
        selection_at=selection.cutoff_at,
        matures_at=maturity,
        batch_pass=False
        if selection.disposition == "ABSTAINED" and maturity <= cutoff_at
        else None,
        positive_count=0 if selection.disposition == "ABSTAINED" else None,
        target_count=0 if selection.disposition == "ABSTAINED" else None,
        positive_rate=Decimal(0) if selection.disposition == "ABSTAINED" else None,
        target_rate=Decimal(0) if selection.disposition == "ABSTAINED" else None,
    )
    if selection.disposition not in {"FROZEN", "ABSTAINED"} or maturity > cutoff_at:
        return result
    try:
        result = result.model_copy(
            update={"regime": market_regime(month_input, selection.cutoff_at, calendar.version_id)}
        )
    except ValueError as error:
        result = result.model_copy(
            update={"reasons": (str(error),), "availability_failure": "SYSTEM"}
        )
    if month_input.future_index is not None:
        future = month_input.future_index
        if (
            month_input.index is None
            or not month_input.index.prices
            or future.starting_at != month_input.index.prices[-1].closed_at
            or future.starting_total_return_price != month_input.index.prices[-1].total_return_price
            or future.effective_at != maturity
            or future.available_at > cutoff_at
        ):
            raise ValueError("HISTORICAL_EX_POST_INDEX_EVIDENCE_INVALID")
        result = result.model_copy(
            update={
                "future_index_return": decimal_fraction(
                    Fraction(future.terminal_total_return_price)
                    / Fraction(future.starting_total_return_price)
                    - 1
                )
            }
        )
    if month_input.factors is None or month_input.factors.available_at > selection.cutoff_at:
        result = result.model_copy(update={"availability_failure": "SYSTEM"})
    observations = {row.security_id: row for row in month_input.observations}
    if len(observations) != len(month_input.observations):
        raise ValueError("HISTORICAL_OUTCOME_MEMBERSHIP_DUPLICATED")
    universe_securities = {row.security_id for row in selection_command.rows}
    if (
        set(observations) - universe_securities
        or {row.security_id for row in month_input.paths} - universe_securities
    ):
        raise ValueError("HISTORICAL_OUTCOME_OUTSIDE_FROZEN_UNIVERSE")
    calendar_sessions = tuple(
        item.closed_at
        for item in historical_sessions(calendar)
        if sessions[0].closed_at <= item.closed_at <= maturity
    )
    if selection.disposition == "FROZEN":
        standard = ledger.get_decision_event(month_input.standard_event_id or "", connection)
        if standard is None or standard.result.standard_outcomes is None:
            return result.model_copy(update={"reasons": ("HISTORICAL_STANDARD_OUTCOMES_REQUIRED",)})
        report = standard.result.standard_outcomes
        latest_standard = next(
            (
                event
                for event in reversed(ledger.standard_evaluation_history(connection, access_scope))
                if event.result.standard_outcomes is not None
                and event.result.standard_outcomes.selection_event_id == source.decision_event_id
                and event.result.standard_outcomes.cutoff_at <= cutoff_at
                and datetime.fromisoformat(event.committed_at) <= cutoff_at
            ),
            None,
        )
        if (
            latest_standard is None
            or latest_standard.decision_event_id != standard.decision_event_id
            or standard.case.standard_outcomes is None
            or standard.case.standard_outcomes.standard_quantity != registration.standard_quantity
            or standard.case.standard_outcomes.market_calendar_version
            != registration.market_calendar_version
        ):
            raise ValueError("HISTORICAL_STANDARD_SOURCE_STALE_OR_INCOMPATIBLE")
        if (
            standard.case.access_scope is None
            or not standard.case.access_scope.same_scope_as(access_scope)
            or standard.corrects_event_id is not None
            or report.selection_event_id != source.decision_event_id
            or report.cutoff_at > cutoff_at
            or datetime.fromisoformat(standard.committed_at) > cutoff_at
        ):
            raise ValueError("HISTORICAL_STANDARD_SOURCE_UNAVAILABLE")
        observations = {row.security_id: row for row in month_input.observations}
        if len(observations) != len(month_input.observations):
            raise ValueError("HISTORICAL_OUTCOME_MEMBERSHIP_DUPLICATED")
        saved = tuple(row for row in report.members if row.population == "SELECTION")
        if set(row.security_id for row in saved) != set(selection.members):
            raise ValueError("HISTORICAL_STANDARD_MEMBERSHIP_MISMATCH")
        members = tuple(
            evaluate_member(
                EvaluationMember(
                    evaluation_id=row.evaluation_id,
                    population="SELECTION",
                    source_event_id=row.source_event_id,
                    security_id=row.security_id,
                    matures_at=maturity,
                    state="UNAVAILABLE",
                ),
                observations.get(row.security_id),
                tuple(item.closed_at for item in sessions),
                cutoff_at,
                registration.standard_quantity,
                calendar.version_id,
            )
            for row in saved
        )
        if any(row.state not in {"ACHIEVED", "NOT_ACHIEVED"} for row in members):
            return result.model_copy(update={"reasons": ("HISTORICAL_DUE_OUTCOMES_MISSING",)})
        if any(
            (row.observation, row.entry_at, row.matures_at, row.net_total_return, row.entry_expired)
            != (
                prior.observation,
                prior.entry_at,
                prior.matures_at,
                prior.net_total_return,
                prior.entry_expired,
            )
            for row, prior in zip(members, saved, strict=True)
        ):
            raise ValueError("HISTORICAL_STANDARD_OUTCOMES_REWRITTEN")
        result = result.model_copy(
            update={
                **cohort_metrics(members, registration),
                "member_returns": {
                    row.security_id: decimal_fraction(
                        exact_member_return(row, registration.standard_quantity)
                    )
                    for row in members
                },
            }
        )
        try:
            nav, drawdown = cohort_nav(
                members, month_input.paths, calendar_sessions, registration, cutoff_at
            )
        except ValueError as error:
            return result.model_copy(update={"reasons": (str(error),)})
        result = result.model_copy(
            update={
                "nav": nav,
                "maximum_drawdown": decimal_fraction(drawdown),
                "drawdown_pass": drawdown <= Fraction(registration.maximum_drawdown),
            }
        )
    try:
        universe_members = tuple(
            evaluate_member(
                EvaluationMember(
                    evaluation_id=f"baseline:{source.decision_event_id}:{security}",
                    population="SELECTION",
                    source_event_id=source.decision_event_id,
                    security_id=security,
                    matures_at=maturity,
                    state="UNAVAILABLE",
                ),
                observations.get(security),
                tuple(item.closed_at for item in sessions),
                cutoff_at,
                registration.standard_quantity,
                calendar.version_id,
            )
            for security in (row.security_id for row in selection_command.rows)
        )
        if any(member.state not in {"ACHIEVED", "NOT_ACHIEVED"} for member in universe_members):
            raise ValueError("HISTORICAL_BASELINE_DUE_OUTCOMES_MISSING")
        baselines = paired_baselines(
            month_input,
            selection_command,
            source.decision_event_id,
            universe_members,
            calendar_sessions,
            registration,
            cutoff_at,
        )
    except ValueError as error:
        return result.model_copy(
            update={
                "reasons": (*result.reasons, str(error)),
                "availability_failure": "SYSTEM"
                if str(error).startswith("HISTORICAL_FACTOR")
                or str(error) == "HISTORICAL_SELECTION_VISIBLE_FACTOR_SOURCE_REQUIRED"
                else result.availability_failure,
            }
        )
    return result.model_copy(update={"baselines": baselines})
