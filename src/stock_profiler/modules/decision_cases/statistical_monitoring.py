"""Deliver saved position conclusions without recalculating their decisions."""

from datetime import datetime
from hashlib import sha256

from pydantic import BaseModel

from stock_profiler.modules.decision_cases.domain import (
    FormalReport,
    FrozenDecisionCase,
    ResultAccessScope,
)
from stock_profiler.modules.decision_cases.ports import DecisionLedger, Transaction
from stock_profiler.modules.delivery.monitoring_contracts import MonitoringCase, MonitoringOutcome
from stock_profiler.modules.position_management.contracts import PositionEvidence
from stock_profiler.modules.position_management.exit_contracts import (
    GuardedExitProbability,
    SecurityAssessment,
)
from stock_profiler.modules.qualification.service import (
    current_qualification,
    qualification_is_current,
)


def project_position_action(
    case: FrozenDecisionCase,
    source: FormalReport,
    blocked: MonitoringOutcome,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    *,
    prerequisite_met: bool,
) -> MonitoringOutcome:
    command, scope = case.monitoring, case.access_scope
    assert command is not None and scope is not None
    fact = ledger.get_original_decision_event(source.business_object_id, connection)
    action = source.result.position_action
    if fact is None or fact.case.position_action is None or action is None:
        return blocked
    original = fact.case.position_action
    if (
        command.contract_version != "1.1.0"
        or original.execution.portfolio_id != command.portfolio_id
        or original.cutoff_at != command.cutoff_at
        or ledger.get_correction_event(source.event_id, connection) is not None
        or not ledger.monitoring_inputs_unchanged(connection, scope, source.event_id)
    ):
        return blocked.model_copy(update={"reasons": ("MONITORING_CONCLUSION_NOT_CURRENT",)})
    calendar = command.calendar
    position = ledger.get_formal_report_for_event(
        original.execution.concentration_event_id, connection
    )
    if position is None or position.result.position is None:
        return blocked
    items = list(blocked.cases)
    deterministic = action.deterministic_execution
    security = ledger.get_formal_report_for_event(original.security_event_id, connection)
    capital = ledger.get_formal_report_for_event(original.execution.drawdown_event_id, connection)
    capital_protection = (
        capital is not None
        and capital.result.drawdown is not None
        and capital.result.drawdown.state is not None
        and capital.result.drawdown.state.risk_state == "PRESERVATION"
    )
    protective = (
        security is not None
        and security.result.exit_assessment is not None
        and bool(security.result.exit_assessment.hard_gates)
        or capital is not None
        and capital.result.drawdown is not None
        and capital.result.drawdown.state is not None
        and capital.result.drawdown.state.risk_state == "PRESERVATION"
    )
    protective_sources = (
        {
            identity
            for target in deterministic.targets
            if deterministic is not None
            for identity in target.source_obligation_ids
            if (obligation := ledger.get_formal_report_for_event(identity, connection)) is not None
            and obligation.result.exit_assessment is not None
            and obligation.result.exit_assessment.hard_gates
        }
        if deterministic is not None
        else set()
    )
    if deterministic is not None:
        for target in deterministic.targets:
            if not target.source_obligation_ids:
                continue
            previous = next(
                (
                    item
                    for item in items
                    if set(item.obligation_ids).intersection(target.source_obligation_ids)
                ),
                None,
            )
            identity = sha256(
                f"{scope.model_dump_json()}\n{command.portfolio_id}\n{target.source_obligation_ids}".encode()
            ).hexdigest()
            item = MonitoringCase(
                case_id=previous.case_id if previous else f"monitoring-case-{identity}",
                source_event_id=source.event_id,
                obligation_ids=target.source_obligation_ids,
                priority="P0"
                if capital_protection
                or protective_sources.intersection(target.source_obligation_ids)
                or target.security_id == original.security_id
                and protective
                or previous is not None
                and previous.priority == "P0"
                else "P1",
                plan=deterministic,
                required_targets=(target,),
                quantity_status="VERIFIED" if action.current_quantity is not None else "UNKNOWN",
                first_established_at=previous.first_established_at
                if previous
                else source.knowledge_cutoff,
                last_reviewed_at=source.knowledge_cutoff,
                conclusion_report_id=source.report_version_id,
                direction=target.direction,
                target_quantity=target.target_quantity,
                lifecycle_id=action.lifecycle_id,
            )
            if previous:
                items.remove(previous)
            items.append(item)

    projected = blocked.model_copy(
        update={
            "cases": tuple(items),
            "action_units": position.result.position.snapshot.action_units,
            "source_report_ids": (source.report_version_id,),
            "position_action": action,
            "conclusion_report_id": source.report_version_id,
            "freshness": blocked.freshness.model_copy(
                update={
                    "account_evidence": tuple(
                        unit.position_evidence
                        for unit in position.result.position.snapshot.action_units
                    )
                }
            )
            if blocked.freshness
            else None,
        }
    )
    if not prerequisite_met:
        return projected
    security_fact = ledger.get_decision_event(original.security_event_id, connection)
    assessment = security_fact.case.exit_assessment if security_fact else None
    previous_deliveries = tuple(
        prior
        for prior in ledger.monitoring_history(connection, scope, command.portfolio_id)
        if prior.case.monitoring is not None
        and prior.case.monitoring.kind in {"DAILY_CLOSE", "EVENT_REASSESS"}
        and prior.case.monitoring.source_event_id == source.event_id
    )
    if (
        calendar is None
        or not isinstance(assessment, SecurityAssessment)
        or calendar.version_id != assessment.calendar_version
        or calendar.next_window_start is None
        or calendar.next_window_end is None
        or not command.cutoff_at < calendar.next_window_start < calendar.next_window_end
        or any(
            calendar.next_window_end > prediction.valid_until
            for prediction in assessment.predictions
        )
        or previous_deliveries
    ):
        return projected.model_copy(
            update={"reasons": ("MONITORING_STATISTICAL_WINDOW_UNAVAILABLE",)}
        )
    if (
        action.model_direction in {"REDUCE", "EXIT"}
        and action.direction is not None
        and not protective
        and not any(reason != "STRICTEST_TARGET_CONJUNCTION" for reason in action.reasons)
    ):
        identity = sha256(f"{source.event_id}\n{source.report_version_id}".encode()).hexdigest()
        items.append(
            MonitoringCase(
                case_id=f"monitoring-statistical-{identity}",
                source_event_id=source.event_id,
                obligation_ids=(),
                priority="P2",
                plan=action.execution,
                required_targets=action.execution.targets if action.execution else (),
                quantity_status="VERIFIED" if action.current_quantity is not None else "UNKNOWN",
                first_established_at=source.knowledge_cutoff,
                last_reviewed_at=source.knowledge_cutoff,
                conclusion_report_id=source.report_version_id,
                direction=action.direction,
                target_quantity=action.target_quantity,
                lifecycle_id=action.lifecycle_id,
                qualification_id=original.qualification_id,
                window_start=calendar.next_window_start,
                window_end=calendar.next_window_end,
            )
        )
    return projected.model_copy(
        update={
            "disposition": "ASSESSED",
            "reasons": action.reasons,
            "cases": tuple(items),
            "action_units": position.result.position.snapshot.action_units,
            "source_report_ids": (source.report_version_id,),
            "position_action": action,
            "conclusion_report_id": source.report_version_id,
        }
    )


def statistical_conclusion_current(
    report: FormalReport,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
) -> bool:
    """Revalidate authority and original evidence without renewing a saved conclusion."""
    outcome, scope = report.result.monitoring, report.access_scope
    if outcome is None or scope is None or outcome.conclusion_report_id is None:
        return False
    source = ledger.get_formal_report(outcome.conclusion_report_id)
    if source is None or ledger.get_correction_event(source.event_id, connection) is not None:
        return False
    fact = ledger.get_original_decision_event(source.business_object_id, connection)
    original = fact.case.position_action if fact else None
    if original is None or not ledger.monitoring_inputs_unchanged(
        connection, scope, source.event_id
    ):
        return False
    if not execution_conclusion_current(report, ledger, connection):
        return False
    now = datetime.fromisoformat(ledger.observed_at())
    record = current_qualification(
        ledger.governance_history(connection, scope),
        original.qualification_scope,
        original.policy.version,
    )
    security = ledger.get_formal_report_for_event(original.security_event_id, connection)
    authorizations = ledger.portfolio_authorization_history(
        connection, scope, original.execution.portfolio_id, now.isoformat()
    )
    authorization = next(
        (item.authorization for item in reversed(authorizations) if item.authorization is not None),
        None,
    )
    return not (
        authorization is None
        or authorization.authorization_id != original.execution.authorization_id
        or authorization.proposal.risk_budget.expires_at <= now
        or any(
            route.security_id == original.security_id
            and (
                route.cost_curve is None or not _evidence_unexpired(route.cost_curve.evidence, now)
            )
            for route in original.execution.routes
        )
        or record is None
        or record.decision_id != original.qualification_id
        or record.status not in {"VALID", "AT_RISK"}
        or not qualification_is_current(record, now)
        or security is None
        or security.result.exit_assessment is None
        or security.result.exit_assessment.thesis is None
        or security.result.exit_assessment.thesis.valid_until <= now
        or any(evidence.valid_until <= now for evidence in security.result.exit_assessment.evidence)
        or any(
            prediction.valid_until <= now
            or not _prediction_qualification_current(prediction, scope, ledger, connection, now)
            for prediction in security.result.exit_assessment.probabilities
        )
        or outcome.freshness is None
        or any(
            family.evidence is None
            or family.status != "VALIDATED"
            or family.evidence.expires_at is not None
            and family.evidence.expires_at <= now
            for family in outcome.freshness.evidence_families
        )
        or any(
            evidence.expires_at is not None and evidence.expires_at <= now
            for evidence in (
                *outcome.freshness.account_evidence,
                outcome.freshness.calendar_evidence,
            )
        )
    )


def execution_conclusion_current(
    report: FormalReport, ledger: DecisionLedger[Transaction], connection: Transaction
) -> bool:
    """Verify a deterministic plan's saved execution facts independently of statistics."""
    outcome, scope = report.result.monitoring, report.access_scope
    if outcome is None or scope is None or outcome.conclusion_report_id is None:
        return False
    source = ledger.get_formal_report(outcome.conclusion_report_id)
    if (
        source is None
        or ledger.get_correction_event(source.event_id, connection) is not None
        or not ledger.monitoring_inputs_unchanged(connection, scope, source.event_id)
    ):
        return False
    fact = ledger.get_original_decision_event(source.business_object_id, connection)
    command = fact.case.position_action if fact else None
    if command is None:
        return False
    now = datetime.fromisoformat(ledger.observed_at())
    for route in command.execution.routes:
        if not _evidence_unexpired(route.rules_evidence, now):
            return False
        curves = (route.cost_curve, route.conservative_cost_curve)
        if not any(
            curve is not None and _evidence_unexpired(curve.evidence, now) for curve in curves
        ):
            return False
    for event_id in (
        command.execution.concentration_event_id,
        command.execution.stress_event_id,
        command.execution.liquidity_event_id,
        command.execution.drawdown_event_id,
    ):
        dependency = ledger.get_formal_report_for_event(event_id, connection)
        if (
            dependency is None
            or dependency.access_scope is None
            or not scope.same_scope_as(dependency.access_scope)
            or ledger.get_correction_event(event_id, connection) is not None
            or not _evidence_unexpired(dependency.result, now)
        ):
            return False
    return True


def _evidence_unexpired(value: object, now: datetime) -> bool:
    if isinstance(value, PositionEvidence):
        return value.expires_at is None or now < value.expires_at
    if isinstance(value, BaseModel):
        return all(
            _evidence_unexpired(getattr(value, name), now) for name in type(value).model_fields
        )
    if isinstance(value, tuple):
        return all(_evidence_unexpired(item, now) for item in value)
    return True


def _prediction_qualification_current(
    prediction: GuardedExitProbability,
    scope: ResultAccessScope,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    now: datetime,
) -> bool:
    record = current_qualification(
        ledger.governance_history(connection, scope),
        prediction.qualification_scope,
        prediction.version,
    )
    return (
        record is not None
        and record.decision_id == prediction.qualification_id
        and record.status in {"VALID", "AT_RISK"}
        and qualification_is_current(record, now)
    )
