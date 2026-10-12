"""Current pointers and audit history derived from immutable published reports."""

from datetime import datetime

from stock_profiler.modules.decision_cases.domain import FormalReport
from stock_profiler.modules.delivery.monitoring_contracts import MonitoringCase, MonitoringContract
from stock_profiler.modules.delivery.user_facts import UserFact


class MonitoringWorkspace(MonitoringContract):
    observed_at: str | None = None
    inactive_statistical_case_ids: tuple[str, ...] = ()
    reports: tuple[FormalReport, ...]
    current_report_ids: tuple[str, ...]
    inbox: tuple[MonitoringCase, ...]
    user_facts: tuple[UserFact, ...]


def project_workspace(
    reports: tuple[FormalReport, ...],
    user_facts: tuple[UserFact, ...],
    now: datetime | None = None,
    invalid_statistical_report_ids: tuple[str, ...] = (),
) -> MonitoringWorkspace:
    current: dict[str, FormalReport] = {}
    for report in reports:
        monitoring = report.result.monitoring
        assert monitoring is not None
        if monitoring.kind in {"DAILY_CLOSE", "EVENT_REASSESS"}:
            previous = current.get(monitoring.portfolio_id)
            if previous is not None and (
                datetime.fromisoformat(report.knowledge_cutoff)
                < datetime.fromisoformat(previous.knowledge_cutoff)
                or report.access_scope is None
                or previous.access_scope is None
                or not report.access_scope.same_scope_as(previous.access_scope)
                or "MONITORING_HISTORY_SCOPE_INCOMPLETE" in monitoring.reasons
                or "MONITORING_SNAPSHOT_NOT_FORWARD" in monitoring.reasons
                or (
                    report.corrects_event_id is not None
                    and report.corrects_event_id != previous.event_id
                )
            ):
                continue
            current[monitoring.portfolio_id] = report
    active_ids = (
        {
            item.case_id
            for report in current.values()
            if report.result.monitoring is not None
            for item in report.result.monitoring.cases
            if item.statistical_window_valid(now)
            and (
                item.priority != "P2"
                or report.report_version_id not in invalid_statistical_report_ids
            )
        }
        if now is not None
        else {
            item.case_id
            for report in current.values()
            if report.result.monitoring is not None
            for item in report.result.monitoring.cases
            if item.priority != "P2"
        }
    )
    return MonitoringWorkspace(
        observed_at=now.isoformat() if now else None,
        inactive_statistical_case_ids=tuple(
            dict.fromkeys(
                item.case_id
                for report in reports
                if report.result.monitoring is not None
                for item in report.result.monitoring.cases
                if item.priority == "P2" and item.case_id not in active_ids
            )
        ),
        reports=reports,
        current_report_ids=tuple(report.report_version_id for report in current.values()),
        inbox=tuple(
            item
            for report in current.values()
            if report.result.monitoring is not None
            for item in report.result.monitoring.cases
            if item.priority != "P2" or item.case_id in active_ids
        ),
        user_facts=user_facts,
    )
