"""Current pointers and audit history derived from immutable published reports."""

from datetime import datetime

from stock_profiler.modules.decision_cases.domain import FormalReport
from stock_profiler.modules.delivery.monitoring_contracts import MonitoringCase, MonitoringContract
from stock_profiler.modules.delivery.user_facts import UserFact


class MonitoringWorkspace(MonitoringContract):
    reports: tuple[FormalReport, ...]
    current_report_ids: tuple[str, ...]
    inbox: tuple[MonitoringCase, ...]
    user_facts: tuple[UserFact, ...]


def project_workspace(
    reports: tuple[FormalReport, ...], user_facts: tuple[UserFact, ...]
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
                or report.access_scope != previous.access_scope
                or "MONITORING_HISTORY_SCOPE_INCOMPLETE" in monitoring.reasons
                or "MONITORING_SNAPSHOT_NOT_FORWARD" in monitoring.reasons
                or (
                    report.corrects_event_id is not None
                    and report.corrects_event_id != previous.event_id
                )
            ):
                continue
            current[monitoring.portfolio_id] = report
    return MonitoringWorkspace(
        reports=reports,
        current_report_ids=tuple(report.report_version_id for report in current.values()),
        inbox=tuple(
            item
            for report in current.values()
            if report.result.monitoring is not None
            for item in report.result.monitoring.cases
        ),
        user_facts=user_facts,
    )
