"""Current pointers and audit history derived from immutable published reports."""

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
