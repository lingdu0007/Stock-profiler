"""Monitoring consumes saved decisions, never framework or browser conclusions."""

from datetime import datetime
from hashlib import sha256

from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
from stock_profiler.modules.decision_cases.ports import DecisionLedger, Transaction
from stock_profiler.modules.delivery.monitoring_contracts import (
    MonitoringCase,
    MonitoringFreshness,
    MonitoringOutcome,
)
from stock_profiler.modules.delivery.monitoring_notifications import route_synthetic_notifications

_EVENT_AUTHORITIES = {
    "ACCOUNT_STATE": "BROKER",
    "EXECUTION_BLOCKED": "BROKER",
    "CAPITAL_PROTECTION": "RISK_POLICY",
    "RISK_BREACH": "RISK_POLICY",
    "TERMINATION": "EXCHANGE",
    "SECURITY_STATE": "EXCHANGE",
    "THESIS_FALSIFIED": "DISCLOSURE",
    "CORPORATE_ACTION": "DISCLOSURE",
    "DATA_CORRECTION": "FACT_AUTHORITY",
}


def assess_monitoring(
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
) -> MonitoringOutcome:
    command = case.monitoring
    scope = case.access_scope
    assert command is not None and scope is not None
    blocked = MonitoringOutcome(
        portfolio_id=command.portfolio_id,
        kind=command.kind,
        disposition="BLOCKED",
        reasons=("MONITORING_HANDOFF_UNAVAILABLE",),
    )
    history = ledger.monitoring_history(connection, scope, command.portfolio_id)
    retained: dict[str, MonitoringCase] = {}
    for prior in history:
        assert prior.case.access_scope is not None and prior.case.monitoring is not None
        if not scope.same_scope_as(prior.case.access_scope):
            return blocked.model_copy(update={"reasons": ("MONITORING_HISTORY_SCOPE_INCOMPLETE",)})
        if prior.case.monitoring.cutoff_at > command.cutoff_at:
            return blocked.model_copy(update={"reasons": ("MONITORING_SNAPSHOT_NOT_FORWARD",)})
    for prior in history:
        assert prior.result.monitoring is not None
        if prior.result.monitoring.kind not in {"DAILY_CLOSE", "EVENT_REASSESS"}:
            continue
        for item in prior.result.monitoring.cases:
            retained[item.case_id] = item
    blocked = blocked.model_copy(
        update={
            "cases": tuple(
                item.model_copy(update={"quantity_status": "UNKNOWN", "plan": None})
                for item in retained.values()
            )
        }
    )
    source = ledger.get_formal_report_for_event(command.source_event_id, connection)
    if (
        source is None
        or source.access_scope is None
        or not scope.same_scope_as(source.access_scope)
    ):
        return blocked
    if datetime.fromisoformat(source.knowledge_cutoff) > command.cutoff_at:
        return blocked.model_copy(update={"reasons": ("MONITORING_SOURCE_AFTER_CUTOFF",)})
    if command.kind == "EVENT_REASSESS":
        if not command.events or any(
            _EVENT_AUTHORITIES.get(event.kind) != event.authority
            or event.evidence.problem_codes(command.cutoff_at, require_current_completeness=False)
            for event in command.events
        ):
            return blocked.model_copy(update={"reasons": ("MONITORING_EVENT_UNQUALIFIED",)})
        protective = any(
            event.kind in {"TERMINATION", "CAPITAL_PROTECTION"} for event in command.events
        )
        if protective and retained:
            calendar = command.calendar
            if calendar is None or calendar.evidence.problem_codes(
                command.cutoff_at, require_current_completeness=False
            ):
                return blocked.model_copy(update={"reasons": ("MONITORING_CALENDAR_UNAVAILABLE",)})
            return MonitoringOutcome(
                portfolio_id=command.portfolio_id,
                kind=command.kind,
                disposition="ASSESSED",
                reasons=("MONITORING_PROTECTION_QUANTITY_UNAVAILABLE",),
                cases=tuple(
                    item.model_copy(
                        update={
                            "priority": "P0",
                            "quantity_status": "UNKNOWN",
                            "plan": None,
                            "last_reviewed_at": case.knowledge_cutoff,
                        }
                    )
                    for item in retained.values()
                ),
                source_report_ids=(source.report_version_id,),
                freshness=MonitoringFreshness(
                    evidence_cutoff=command.cutoff_at,
                    assessed_at=ledger.observed_at(),
                    market_status="CLOSED" if not calendar.is_trading_day else "WAITING_WINDOW",
                    next_window_start=calendar.next_window_start,
                    next_window_end=calendar.next_window_end,
                    account_evidence=(),
                    event_evidence=tuple(event.evidence for event in command.events),
                    calendar_evidence=calendar.evidence,
                ),
            )
    if command.kind in {"LIFECYCLE", "OPERATIONS"}:
        monitoring = source.result.monitoring
        if monitoring is None or monitoring.portfolio_id != command.portfolio_id:
            return blocked
        if command.kind == "LIFECYCLE":
            reconciliation = ledger.get_formal_report_for_event(
                command.reconciliation_event_id or "", connection
            )
            if (
                reconciliation is None
                or reconciliation.access_scope is None
                or not scope.same_scope_as(reconciliation.access_scope)
                or reconciliation.result.position is None
                or reconciliation.result.position.disposition != "RECONCILED"
                or reconciliation.result.position.snapshot.cutoff_at > command.cutoff_at
            ):
                return blocked.model_copy(
                    update={"reasons": ("MONITORING_RECONCILIATION_UNAVAILABLE",)}
                )
            return monitoring.model_copy(
                update={
                    "kind": command.kind,
                    "source_report_ids": (
                        source.report_version_id,
                        reconciliation.report_version_id,
                    ),
                    "reconciliation": reconciliation.result.position,
                }
            )
        references = tuple(
            report.report_version_id
            for prior in history
            if (report := ledger.get_formal_report_for_event(prior.decision_event_id, connection))
            is not None
        )
        return monitoring.model_copy(
            update={
                "kind": command.kind,
                "source_report_ids": references,
                "notifications": tuple(
                    attempt
                    for prior in history
                    if prior.result.monitoring is not None
                    for attempt in prior.result.monitoring.notifications
                ),
            }
        )
    if command.kind == "NOTIFICATION_RUN":
        monitoring = source.result.monitoring
        if monitoring is None or command.notification is None or not monitoring.cases:
            return blocked
        if any(
            prior.case.monitoring is not None
            and prior.case.monitoring.kind in {"DAILY_CLOSE", "EVENT_REASSESS"}
            and prior.case.monitoring.cutoff_at > datetime.fromisoformat(source.knowledge_cutoff)
            for prior in history
        ):
            return blocked.model_copy(update={"reasons": ("MONITORING_SOURCE_SUPERSEDED",)})
        return monitoring.model_copy(
            update={
                "kind": command.kind,
                "source_report_ids": (source.report_version_id,),
                "notifications": route_synthetic_notifications(
                    monitoring, command.notification, ledger.observed_at()
                ),
            }
        )
    fact = ledger.get_original_decision_event(source.business_object_id, connection)
    plan = source.result.execution_plan
    if (
        fact is None
        or fact.case.execution_plan is None
        or fact.case.execution_plan.portfolio_id != command.portfolio_id
        or fact.case.execution_plan.cutoff_at != command.cutoff_at
        or plan is None
    ):
        return blocked
    calendar = command.calendar
    if calendar is None or calendar.evidence.problem_codes(
        command.cutoff_at, require_current_completeness=True
    ):
        return blocked.model_copy(update={"reasons": ("MONITORING_CALENDAR_UNAVAILABLE",)})
    if command.kind == "DAILY_CLOSE" and (
        not calendar.is_trading_day or calendar.close_at > command.cutoff_at
    ):
        return blocked.model_copy(update={"reasons": ("MONITORING_CLOSE_NOT_COMPLETED",)})
    if command.kind == "EVENT_REASSESS" and (
        not command.events
        or any(
            _EVENT_AUTHORITIES.get(event.kind) != event.authority
            or event.evidence.problem_codes(command.cutoff_at, require_current_completeness=True)
            for event in command.events
        )
    ):
        return blocked.model_copy(update={"reasons": ("MONITORING_EVENT_UNQUALIFIED",)})
    position_report = ledger.get_formal_report_for_event(
        fact.case.execution_plan.concentration_event_id, connection
    )
    if position_report is None or position_report.result.position is None:
        return blocked
    position = position_report.result.position
    if position.disposition != "RECONCILED" or position.snapshot.snapshot_evidence.problem_codes(
        command.cutoff_at, require_current_completeness=True
    ):
        return blocked.model_copy(update={"reasons": ("MONITORING_EVIDENCE_INCOMPLETE",)})
    obligations = tuple(
        sorted({identity for target in plan.targets for identity in target.source_obligation_ids})
    )
    cases: tuple[MonitoringCase, ...] = tuple(retained.values())
    if obligations:
        identity = sha256(
            f"{scope.model_dump_json()}\n{command.portfolio_id}\n{obligations}".encode()
        ).hexdigest()
        previous = next(
            (
                item
                for item in retained.values()
                if set(item.obligation_ids).intersection(obligations)
            ),
            None,
        )
        current = MonitoringCase(
            case_id=f"monitoring-case-{identity}",
            source_event_id=source.event_id,
            obligation_ids=obligations,
            priority="P1",
            plan=plan,
            first_established_at=case.knowledge_cutoff,
            last_reviewed_at=case.knowledge_cutoff,
        )
        if previous is not None:
            current = current.model_copy(
                update={
                    "case_id": previous.case_id,
                    "first_established_at": previous.first_established_at,
                    "priority": previous.priority,
                }
            )
        retained[current.case_id] = current
        cases = tuple(retained.values())
    return MonitoringOutcome(
        portfolio_id=command.portfolio_id,
        kind=command.kind,
        disposition="ASSESSED",
        reasons=(),
        cases=cases,
        action_units=position.snapshot.action_units,
        freshness=MonitoringFreshness(
            evidence_cutoff=command.cutoff_at,
            assessed_at=ledger.observed_at(),
            market_status="CLOSED" if not calendar.is_trading_day else "WAITING_WINDOW",
            next_window_start=calendar.next_window_start,
            next_window_end=calendar.next_window_end,
            account_evidence=tuple(
                unit.position_evidence for unit in position.snapshot.action_units
            ),
            event_evidence=tuple(event.evidence for event in command.events),
            calendar_evidence=calendar.evidence,
        ),
    )
