"""Fail closed when the exact saved monitoring plan cannot be revalidated."""

from datetime import datetime

from stock_profiler.modules.decision_cases.domain import FormalReport
from stock_profiler.modules.decision_cases.ports import DecisionLedger, Transaction


def confirmation_permitted(
    report: FormalReport, ledger: DecisionLedger[Transaction], connection: Transaction
) -> bool:
    outcome = report.result.monitoring
    if outcome is None:
        return True
    if (
        report.access_scope is None
        or outcome.disposition != "ASSESSED"
        or outcome.kind not in {"DAILY_CLOSE", "EVENT_REASSESS"}
        or not outcome.cases
        or outcome.freshness is None
        or outcome.freshness.next_window_end is None
        or datetime.fromisoformat(ledger.observed_at()) > outcome.freshness.next_window_end
        or any(item.plan is None or item.plan.disposition == "BLOCKED" for item in outcome.cases)
        or ledger.get_correction_event(report.event_id, connection) is not None
    ):
        return False
    history = ledger.monitoring_history(connection, report.access_scope, outcome.portfolio_id)
    assessments = tuple(
        fact
        for fact in history
        if fact.case.monitoring is not None
        and fact.case.monitoring.kind in {"DAILY_CLOSE", "EVENT_REASSESS"}
    )
    if not assessments or assessments[-1].decision_event_id != report.event_id:
        return False
    plans = ledger.execution_plan_history(connection, report.access_scope, outcome.portfolio_id)
    return (
        bool(plans)
        and all(item.source_event_id == plans[-1].decision_event_id for item in outcome.cases)
        and ledger.monitoring_inputs_unchanged(
            connection, report.access_scope, plans[-1].decision_event_id
        )
    )
