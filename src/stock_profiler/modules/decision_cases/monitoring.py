"""Monitoring consumes saved decisions, never framework or browser conclusions."""

from calendar import monthrange
from datetime import datetime
from hashlib import sha256
from typing import get_args
from zoneinfo import ZoneInfo

from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
from stock_profiler.modules.decision_cases.ports import DecisionLedger, Transaction
from stock_profiler.modules.delivery.monitoring_contracts import (
    EvidenceFamily,
    MonitoringCase,
    MonitoringCommand,
    MonitoringEvidenceStatus,
    MonitoringFreshness,
    MonitoringOutcome,
)
from stock_profiler.modules.delivery.monitoring_notifications import route_synthetic_notifications
from stock_profiler.modules.position_management.contracts import PositionEvidence

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


def _evidence_status(command: MonitoringCommand) -> tuple[MonitoringEvidenceStatus, ...]:
    facts = {item.family: item.evidence for item in command.evidence_families}
    result = []
    for family in get_args(EvidenceFamily):
        evidence = facts.get(family)
        reasons = (
            evidence.problem_codes(command.cutoff_at, require_current_completeness=True)
            if evidence is not None
            else ("EVIDENCE_FAMILY_MISSING",)
        )
        result.append(
            MonitoringEvidenceStatus(
                family=family,
                evidence=evidence,
                reasons=reasons,
                status="UNKNOWN" if evidence is None else "BLOCKED" if reasons else "VALIDATED",
            )
        )
    return tuple(result)


def _freshness(
    command: MonitoringCommand,
    observed_at: str,
    account_evidence: tuple[PositionEvidence, ...] = (),
) -> MonitoringFreshness:
    calendar = command.calendar
    assert calendar is not None
    return MonitoringFreshness(
        evidence_cutoff=command.cutoff_at,
        assessed_at=observed_at,
        market_status="CLOSED" if not calendar.is_trading_day else "WAITING_WINDOW",
        next_window_start=calendar.next_window_start,
        next_window_end=calendar.next_window_end,
        account_evidence=account_evidence,
        event_evidence=tuple(event.evidence for event in command.events),
        calendar_evidence=calendar.evidence,
        evidence_families=_evidence_status(command),
    )


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
        freshness=_freshness(command, ledger.observed_at()) if command.calendar else None,
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
        if protective or any(item.status != "VALIDATED" for item in _evidence_status(command)):
            affected = {
                security
                for event in command.events
                if not protective or event.kind in {"TERMINATION", "CAPITAL_PROTECTION"}
                for security in event.security_ids
            }
            plan_source = source
            if source.result.monitoring is not None:
                source_ids = {
                    item.source_event_id
                    for item in source.result.monitoring.cases
                    if any(target.security_id in affected for target in item.required_targets)
                }
                if len(source_ids) != 1:
                    return blocked
                resolved = ledger.get_formal_report_for_event(next(iter(source_ids)), connection)
                if resolved is None:
                    return blocked
                plan_source = resolved
            owned_plan = plan_source.result.execution_plan
            owned_fact = ledger.get_original_decision_event(
                plan_source.business_object_id, connection
            )
            if (
                not affected
                or owned_plan is None
                or owned_fact is None
                or owned_fact.case.execution_plan is None
                or owned_fact.case.execution_plan.portfolio_id != command.portfolio_id
                or plan_source.access_scope is None
                or not scope.same_scope_as(plan_source.access_scope)
                or ledger.get_correction_event(plan_source.event_id, connection) is not None
            ):
                return blocked
            owned_targets = {
                target.security_id: target
                for target in owned_plan.targets
                if target.security_id in affected
                and (not protective or target.direction == "EXIT" and target.target_quantity == 0)
            }
            if set(owned_targets) != affected:
                return blocked.model_copy(
                    update={"reasons": ("MONITORING_PROTECTION_TARGET_UNAVAILABLE",)}
                )
            if any(
                target.target_quantity < owned_targets[target.security_id].target_quantity
                for item in retained.values()
                for target in item.required_targets
                if target.security_id in owned_targets
            ):
                return blocked.model_copy(
                    update={"reasons": ("MONITORING_TARGET_WEAKENING_REJECTED",)}
                )
            calendar = command.calendar
            if calendar is None or calendar.evidence.problem_codes(
                command.cutoff_at, require_current_completeness=False
            ):
                return blocked.model_copy(update={"reasons": ("MONITORING_CALENDAR_UNAVAILABLE",)})
            if not retained:
                source_fact = ledger.get_original_decision_event(
                    plan_source.business_object_id, connection
                )
                if (
                    not owned_plan.targets
                    or source_fact is None
                    or source_fact.case.execution_plan is None
                    or source_fact.case.execution_plan.portfolio_id != command.portfolio_id
                ):
                    return blocked
                obligations = tuple(
                    sorted(
                        {
                            identity
                            for target in owned_targets.values()
                            for identity in target.source_obligation_ids
                        }
                    )
                )
                identity = sha256(
                    f"{scope.model_dump_json()}\n{command.portfolio_id}\n{obligations}".encode()
                ).hexdigest()
                initial = MonitoringCase(
                    case_id=f"monitoring-case-{identity}",
                    source_event_id=plan_source.event_id,
                    obligation_ids=obligations,
                    priority="P0" if protective else "P1",
                    plan=None,
                    required_targets=tuple(owned_targets.values()),
                    quantity_status="UNKNOWN",
                    first_established_at=case.knowledge_cutoff,
                    last_reviewed_at=case.knowledge_cutoff,
                )
                retained[initial.case_id] = initial
            covered_securities = {
                target.security_id for item in retained.values() for target in item.required_targets
            }
            for security in sorted(affected - covered_securities):
                target = owned_targets[security]
                matching = next(
                    (
                        item
                        for item in retained.values()
                        if set(item.obligation_ids).intersection(target.source_obligation_ids)
                    ),
                    None,
                )
                if matching is not None:
                    retained[matching.case_id] = matching.model_copy(
                        update={
                            "required_targets": (*matching.required_targets, target),
                            "obligation_ids": tuple(
                                sorted(
                                    set((*matching.obligation_ids, *target.source_obligation_ids))
                                )
                            ),
                            "source_event_id": plan_source.event_id,
                        }
                    )
                else:
                    identity = sha256(
                        f"{scope.model_dump_json()}\n{command.portfolio_id}\n"
                        f"{security}\n{target.source_obligation_ids}".encode()
                    ).hexdigest()
                    added = MonitoringCase(
                        case_id=f"monitoring-case-{identity}",
                        source_event_id=plan_source.event_id,
                        obligation_ids=target.source_obligation_ids,
                        priority="P0" if protective else "P1",
                        plan=None,
                        required_targets=(target,),
                        quantity_status="UNKNOWN",
                        first_established_at=case.knowledge_cutoff,
                        last_reviewed_at=case.knowledge_cutoff,
                    )
                    retained[added.case_id] = added
            return MonitoringOutcome(
                portfolio_id=command.portfolio_id,
                kind=command.kind,
                disposition="ASSESSED",
                reasons=(
                    "MONITORING_PROTECTION_QUANTITY_UNAVAILABLE"
                    if protective
                    else "MONITORING_RISK_QUANTITY_UNAVAILABLE",
                ),
                cases=tuple(
                    item.model_copy(
                        update={
                            "priority": "P0"
                            if protective
                            and any(
                                target.security_id in affected for target in item.required_targets
                            )
                            else item.priority,
                            "quantity_status": "UNKNOWN",
                            "plan": None,
                            "source_event_id": plan_source.event_id
                            if any(
                                target.security_id in affected for target in item.required_targets
                            )
                            else item.source_event_id,
                            "obligation_ids": tuple(
                                sorted(
                                    set(item.obligation_ids).union(
                                        identity
                                        for target in item.required_targets
                                        if target.security_id in owned_targets
                                        for identity in owned_targets[
                                            target.security_id
                                        ].source_obligation_ids
                                    )
                                )
                            ),
                            "required_targets": tuple(
                                owned_targets.get(target.security_id, target)
                                for target in item.required_targets
                            ),
                            "last_reviewed_at": case.knowledge_cutoff,
                        }
                    )
                    for item in retained.values()
                ),
                source_report_ids=(source.report_version_id,),
                freshness=_freshness(command, ledger.observed_at()),
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
        as_of = command.cutoff_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        dates = command.planned_trading_dates
        if (
            len(dates) != 20
            or tuple(sorted(set(dates))) != dates
            or dates[-1] > as_of
            or command.monthly_freeze
            and as_of.day != monthrange(as_of.year, as_of.month)[1]
        ):
            return blocked.model_copy(update={"reasons": ("MONITORING_OPERATIONS_WINDOW_INVALID",)})
        interaction_cutoff = ledger.observed_at()
        window_start = datetime.combine(dates[0], datetime.min.time(), ZoneInfo("Asia/Shanghai"))
        recorded_until = datetime.fromisoformat(interaction_cutoff)
        window_history = tuple(
            prior
            for prior in history
            if prior.case.monitoring is not None
            and prior.case.monitoring.kind != "OPERATIONS"
            and (
                dates[0]
                <= prior.case.monitoring.cutoff_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
                <= as_of
                or prior.case.monitoring.kind == "NOTIFICATION_RUN"
                and prior.result.monitoring is not None
                and any(
                    window_start <= datetime.fromisoformat(attempt.attempted_at) <= recorded_until
                    for attempt in prior.result.monitoring.notifications
                )
            )
        )
        covered = {
            prior.case.monitoring.cutoff_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
            for prior in window_history
            if prior.case.monitoring is not None and prior.case.monitoring.kind == "DAILY_CLOSE"
        }
        references = tuple(
            report.report_version_id
            for prior in window_history
            if (report := ledger.get_formal_report_for_event(prior.decision_event_id, connection))
            is not None
        )
        user_facts = ledger.monitoring_user_facts(
            connection,
            scope,
            command.portfolio_id,
            window_start,
            recorded_until,
        )
        references = tuple(
            dict.fromkeys((*references, *(fact.report_version_id for fact in user_facts)))
        )
        return monitoring.model_copy(
            update={
                "kind": command.kind,
                "source_report_ids": references,
                "user_facts": user_facts,
                "interaction_cutoff_at": interaction_cutoff,
                "operations_dates": dates,
                "missing_daily_dates": tuple(day for day in dates if day not in covered),
                "notifications": tuple(
                    attempt
                    for prior in window_history
                    if prior.result.monitoring is not None
                    and prior.result.monitoring.kind == "NOTIFICATION_RUN"
                    for attempt in prior.result.monitoring.notifications
                    if window_start
                    <= datetime.fromisoformat(attempt.attempted_at)
                    <= recorded_until
                ),
            }
        )
    if command.kind == "NOTIFICATION_RUN":
        monitoring = source.result.monitoring
        if (
            monitoring is None
            or monitoring.portfolio_id != command.portfolio_id
            or command.notification is None
            or not monitoring.cases
        ):
            return blocked
        assessments = tuple(
            prior
            for prior in history
            if prior.case.monitoring is not None
            and prior.case.monitoring.kind in {"DAILY_CLOSE", "EVENT_REASSESS"}
            and prior.result.monitoring is not None
            and "MONITORING_SNAPSHOT_NOT_FORWARD" not in prior.result.monitoring.reasons
            and "MONITORING_HISTORY_SCOPE_INCOMPLETE" not in prior.result.monitoring.reasons
        )
        if (
            ledger.get_correction_event(source.event_id, connection) is not None
            or not assessments
            or assessments[-1].decision_event_id != source.event_id
        ):
            return blocked.model_copy(update={"reasons": ("MONITORING_SOURCE_SUPERSEDED",)})
        request = command.notification
        if request.attempt_number > 1:
            previous_run = next(
                (
                    prior
                    for prior in history
                    if prior.decision_event_id == request.previous_event_id
                ),
                None,
            )
            if (
                previous_run is None
                or previous_run.case.monitoring is None
                or previous_run.case.monitoring.notification is None
                or previous_run.result.monitoring is None
                or previous_run.case.monitoring.source_event_id != source.event_id
                or previous_run.case.monitoring.notification.identity != request.identity
                or previous_run.case.monitoring.notification.routing_version
                != request.routing_version
                or previous_run.case.monitoring.notification.attempt_number + 1
                != request.attempt_number
                or previous_run.result.monitoring.notification_due_at is None
            ):
                return blocked.model_copy(update={"reasons": ("MONITORING_RETRY_NOT_DUE",)})
            if (
                datetime.fromisoformat(ledger.observed_at())
                < previous_run.result.monitoring.notification_due_at
            ):
                return monitoring.model_copy(
                    update={
                        "kind": command.kind,
                        "source_report_ids": (source.report_version_id,),
                        "notifications": (),
                        "notification_due_at": previous_run.result.monitoring.notification_due_at,
                        "reasons": ("MONITORING_RETRY_DEFERRED",),
                    }
                )
        attempts = route_synthetic_notifications(monitoring, request, ledger.observed_at())
        pending_due: list[datetime] = []
        attempted_cases = {attempt.case_id for attempt in attempts}
        if request.quiet_until is not None and any(
            item.case_id not in attempted_cases for item in monitoring.cases
        ):
            pending_due.append(request.quiet_until)
        if any(attempt.result != "ACCEPTED" for attempt in attempts):
            pending_due.append(datetime.fromisoformat(ledger.observed_at()))
        return monitoring.model_copy(
            update={
                "kind": command.kind,
                "source_report_ids": (source.report_version_id,),
                "notifications": attempts,
                "notification_due_at": min(pending_due, default=None),
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
    if any(item.status != "VALIDATED" for item in _evidence_status(command)):
        return blocked.model_copy(update={"reasons": ("MONITORING_EVIDENCE_FAMILY_INCOMPLETE",)})
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
    capital_report = ledger.get_formal_report_for_event(
        fact.case.execution_plan.drawdown_event_id, connection
    )
    capital_state = (
        capital_report.result.drawdown.state
        if capital_report is not None and capital_report.result.drawdown is not None
        else None
    )
    capital_protection = capital_state is not None and capital_state.risk_state == "PRESERVATION"
    protective_obligations = {
        target.target_id
        for target in fact.case.execution_plan.established_targets
        if target.priority == "P0" and target.qualified
    }
    cases: tuple[MonitoringCase, ...] = tuple(retained.values())
    if any(
        target.target_quantity < candidate.target_quantity
        for item in retained.values()
        for target in item.required_targets
        for candidate in plan.targets
        if candidate.security_id == target.security_id
    ):
        return blocked.model_copy(update={"reasons": ("MONITORING_TARGET_WEAKENING_REJECTED",)})
    if obligations:
        assigned: set[str] = set()
        for previous in sorted(
            retained.values(),
            key=lambda item: (item.priority != "P0", item.first_established_at, item.case_id),
        ):
            identities = tuple(
                identity for identity in previous.obligation_ids if identity not in assigned
            )
            assigned.update(identities)
            if not identities:
                del retained[previous.case_id]
                continue
            current_ids = set(identities).intersection(obligations)
            if not current_ids:
                retained[previous.case_id] = previous.model_copy(
                    update={"obligation_ids": identities}
                )
                continue
            complete = set(identities).issubset(obligations)
            retained[previous.case_id] = previous.model_copy(
                update={
                    "obligation_ids": identities,
                    "source_event_id": source.event_id,
                    "plan": plan if complete else None,
                    "required_targets": tuple(
                        target
                        for target in plan.targets
                        if set(target.source_obligation_ids).intersection(current_ids)
                    )
                    if complete
                    else previous.required_targets,
                    "quantity_status": "VERIFIED" if complete else "UNKNOWN",
                    "last_reviewed_at": case.knowledge_cutoff
                    if complete
                    else previous.last_reviewed_at,
                    "priority": "P0"
                    if capital_protection
                    or protective_obligations.intersection(identities)
                    or previous.priority == "P0"
                    else "P1",
                }
            )
        new_obligations = tuple(identity for identity in obligations if identity not in assigned)
        identity = sha256(
            f"{scope.model_dump_json()}\n{command.portfolio_id}\n{new_obligations}".encode()
        ).hexdigest()
        current = MonitoringCase(
            case_id=f"monitoring-case-{identity}",
            source_event_id=source.event_id,
            obligation_ids=new_obligations,
            priority="P0"
            if capital_protection or protective_obligations.intersection(new_obligations)
            else "P1",
            plan=plan,
            required_targets=tuple(
                target
                for target in plan.targets
                if set(target.source_obligation_ids).intersection(new_obligations)
            ),
            first_established_at=case.knowledge_cutoff,
            last_reviewed_at=case.knowledge_cutoff,
        )
        if new_obligations:
            retained[current.case_id] = current
        cases = tuple(retained.values())
    return MonitoringOutcome(
        portfolio_id=command.portfolio_id,
        kind=command.kind,
        disposition="ASSESSED",
        reasons=(),
        cases=cases,
        action_units=position.snapshot.action_units,
        freshness=_freshness(
            command,
            ledger.observed_at(),
            tuple(unit.position_evidence for unit in position.snapshot.action_units),
        ),
    )
