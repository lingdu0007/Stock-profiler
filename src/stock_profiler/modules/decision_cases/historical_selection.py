"""Resolve preregistered monthly history through the frozen decision-case seam."""

from datetime import datetime
from zoneinfo import ZoneInfo

from stock_profiler.modules.decision_cases.cohort_month import resolve_cohort_month
from stock_profiler.modules.decision_cases.domain import (
    DecisionEventFact,
    FrozenDecisionCase,
)
from stock_profiler.modules.decision_cases.ports import DecisionLedger, Transaction
from stock_profiler.modules.evaluation.historical_contracts import (
    HistoricalCounts,
    HistoricalMonthInput,
    HistoricalMonthResult,
    HistoricalRegistration,
    HistoricalSelectionReport,
)
from stock_profiler.modules.evaluation.historical_inference import summarize_history
from stock_profiler.modules.evaluation.historical_revisions import merge_history


class InvalidHistoricalSelectionRequest(ValueError):
    """A rejected historical command requiring an audit outside the rolled-back transaction."""


def registered_months(start: str, end: str) -> tuple[str, ...]:
    year, month = map(int, start.split("-"))
    stop_year, stop_month = map(int, end.split("-"))
    return tuple(
        f"{index // 12:04}-{index % 12 + 1:02}"
        for index in range(year * 12 + month - 1, stop_year * 12 + stop_month)
    )


def assess_historical_selection(
    case: FrozenDecisionCase, ledger: DecisionLedger[Transaction], connection: Transaction
) -> HistoricalSelectionReport:
    command = case.historical_selection
    assert command is not None and case.access_scope is not None
    history = ledger.historical_selection_history(connection, case.access_scope)
    if command.operation == "REGISTER":
        assert command.registration is not None
        if command.cutoff_at.strftime("%Y-%m") > command.registration.start_month:
            raise ValueError("HISTORICAL_PREREGISTRATION_TOO_LATE")
        if any(
            event.result.selection is not None
            and command.registration.start_month
            <= event.result.selection.cutoff_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
                "%Y-%m"
            )
            <= command.registration.end_month
            and event.case.selection is not None
            and event.case.selection.strategy_version == command.registration.strategy_version
            for event in history
        ):
            raise ValueError("HISTORICAL_RESULTS_PRECEDE_REGISTRATION")
        if any(
            event.result.historical_selection is not None
            and event.result.historical_selection.registration.version_id
            == command.registration.version_id
            for event in history
        ):
            raise ValueError("HISTORICAL_REGISTRATION_IDENTITY_ALREADY_BOUND")
        return HistoricalSelectionReport(
            disposition="REGISTERED",
            registration=command.registration,
            registration_event_id=None,
            cutoff_at=command.cutoff_at,
        )
    source = ledger.get_decision_event(command.registration_event_id or "", connection)
    if (
        source is None
        or source.case.access_scope is None
        or not source.case.access_scope.same_scope_as(case.access_scope)
        or source.result.historical_selection is None
        or source.result.historical_selection.disposition != "REGISTERED"
        or source.corrects_event_id is not None
        or datetime.fromisoformat(source.committed_at) > command.cutoff_at
    ):
        raise ValueError("HISTORICAL_PREREGISTRATION_UNAVAILABLE")
    registration = source.result.historical_selection.registration
    previous = next(
        (
            event
            for event in reversed(history)
            if event.case.historical_selection is not None
            and event.case.historical_selection.registration_event_id == source.decision_event_id
        ),
        None,
    )
    if command.previous_event_id != (previous.decision_event_id if previous else None):
        raise ValueError("HISTORICAL_REPORT_LINEAGE_CONFLICT")
    if previous and command.cutoff_at < datetime.fromisoformat(previous.committed_at):
        raise ValueError("HISTORICAL_REPORT_CUTOFF_REWOUND")
    previous_report = (
        ledger.get_formal_report_for_event(previous.decision_event_id, connection)
        if previous
        else None
    )
    inputs = merge_history(
        tuple(
            event.case.historical_selection
            for event in history
            if event.case.historical_selection is not None
            and event.case.historical_selection.registration_event_id == source.decision_event_id
        ),
        command,
    )
    months = registered_months(registration.start_month, registration.end_month)
    if len({month.plan_month for month in command.months}) != len(command.months) or set(
        inputs
    ) - set(months):
        raise ValueError("HISTORICAL_TIMELINE_MEMBERSHIP_INVALID")
    results = tuple(
        resolve_month(inputs.get(month), month, registration, source, case, ledger, connection)
        for month in months
    )
    failures = ledger.selection_availability_failure_history(connection, case.access_scope)
    failure_months = {}
    for failed_attempt in failures:
        failed_command = failed_attempt.case.selection
        assert failed_command is not None
        month = failed_command.cutoff_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m")
        if (
            month in months
            and failed_attempt.case.version_bundle == registration.source_version_bundle
            and failed_command.strategy_version == registration.strategy_version
            and failed_command.policy == registration.selection_policy
            and datetime.fromisoformat(failed_attempt.recorded_at) <= command.cutoff_at
            and month not in failure_months
        ):
            failure_months[month] = failed_attempt
    adjusted_results = []
    for row in results:
        failure = failure_months.get(row.plan_month)
        committed = (
            ledger.get_decision_event(row.selection_event_id, connection)
            if row.selection_event_id
            else None
        )
        if failure is not None and (
            committed is None
            or datetime.fromisoformat(failure.recorded_at)
            <= datetime.fromisoformat(committed.committed_at)
        ):
            adjusted_results.append(
                HistoricalMonthResult(
                    plan_month=row.plan_month,
                    disposition="SYSTEM_FAILED",
                    selection_at=datetime.fromisoformat(failure.case.knowledge_cutoff),
                    reasons=failure.stage_result.reasons,
                )
            )
        else:
            adjusted_results.append(row)
    results = tuple(adjusted_results)
    counts = HistoricalCounts(
        planned=len(months),
        missing_months=sum(row.disposition == "MISSING" for row in results),
        mature_valid=sum(
            row.disposition in {"FROZEN", "ABSTAINED"}
            and row.matures_at is not None
            and row.matures_at <= command.cutoff_at
            for row in results
        ),
        passed_batches=sum(row.batch_pass is True for row in results),
        drawdown_evaluable=sum(row.drawdown_pass is not None for row in results),
        availability_failures=sum(
            row.disposition in {"DATA_FAILED", "SYSTEM_FAILED", "BLOCKED"}
            or row.availability_failure is not None
            for row in results
        ),
        due_missing=sum(
            bool(row.reasons)
            for row in results
            if row.disposition in {"FROZEN", "ABSTAINED", "UNKNOWN"}
        ),
        immature=sum(
            row.matures_at is not None and row.matures_at > command.cutoff_at for row in results
        ),
    )
    inference = summarize_history(results, registration, seed=source.case.seed)
    return HistoricalSelectionReport(
        inference=inference,
        disposition="INDETERMINATE"
        if counts.missing_months or counts.due_missing
        else inference.disposition,
        registration=registration,
        registration_event_id=source.decision_event_id,
        cutoff_at=command.cutoff_at,
        months=results,
        counts=counts,
        previous_event_id=command.previous_event_id,
        previous_report_id=previous_report.report_version_id if previous_report else None,
        report_version=(
            previous.result.historical_selection.report_version + 1
            if previous and previous.result.historical_selection
            else 1
        ),
    )


def resolve_month(
    month_input: HistoricalMonthInput | None,
    month: str,
    registration: HistoricalRegistration,
    registered: DecisionEventFact,
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
) -> HistoricalMonthResult:
    assert case.historical_selection is not None and case.access_scope is not None
    return resolve_cohort_month(
        month_input,
        month,
        registration,
        datetime.fromisoformat(registered.committed_at),
        case.access_scope,
        case.historical_selection.cutoff_at,
        ledger,
        connection,
    )
