"""Resolve committed origin facts before freezing an exit thesis."""

import json
from datetime import datetime
from decimal import Context, Decimal, localcontext
from hashlib import sha256

from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
from stock_profiler.modules.decision_cases.ports import DecisionLedger, Transaction
from stock_profiler.modules.position_management.exit_contracts import (
    ExitHardGate,
    ExitOutcome,
    ExitTarget,
    ExitThesis,
    GuardedExitProbability,
    SecurityAssessment,
    SecurityEvidence,
)
from stock_profiler.modules.qualification.service import (
    current_qualification,
    qualification_is_current,
)


def adjudicate_security_exit(
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    *,
    business_prerequisite_met: bool,
) -> ExitOutcome:
    command = case.exit_assessment
    scope = case.access_scope
    assert command is not None and scope is not None
    if not business_prerequisite_met:
        return ExitOutcome(disposition="BLOCKED", reasons=("BUSINESS_PREREQUISITE_FAILED",))
    if isinstance(command, SecurityAssessment):
        return assess_security(case, command, ledger, connection)
    thesis = command.thesis
    history = ledger.security_exit_history(connection, scope, thesis.lifecycle_id)
    if any(fact.result.exit_assessment and fact.result.exit_assessment.thesis for fact in history):
        return ExitOutcome(disposition="BLOCKED", reasons=("THESIS_ALREADY_FROZEN",))
    if (
        thesis.registered_at != command.cutoff_at
        or thesis.valid_until <= command.cutoff_at
        or command.cutoff_at != datetime.fromisoformat(ledger.observed_at())
    ):
        return ExitOutcome(disposition="BLOCKED", reasons=("THESIS_REGISTRATION_NOT_FORWARD",))
    report = ledger.get_formal_report_for_event(command.position_event_id, connection)
    if (
        report is None
        or report.access_scope is None
        or not scope.same_scope_as(report.access_scope)
        or report.result.position is None
        or report.result.position.disposition != "RECONCILED"
        or report.result.position.snapshot.cutoff_at != command.cutoff_at
    ):
        return ExitOutcome(disposition="BLOCKED", reasons=("POSITION_ORIGIN_UNAVAILABLE",))
    unit = next(
        (
            unit
            for unit in report.result.position.snapshot.action_units
            if unit.position_id == command.position_id
        ),
        None,
    )
    if (
        unit is None
        or unit.security_id != thesis.security_id
        or unit.lifecycle_id != thesis.lifecycle_id
        or unit.origin != thesis.origin
        or unit.total_quantity is None
        or unit.total_quantity <= 0
    ):
        return ExitOutcome(disposition="BLOCKED", reasons=("POSITION_ORIGIN_MISMATCH",))
    return ExitOutcome(
        disposition="THESIS_REGISTERED", thesis=thesis, thesis_event_id=case.decision_event_id
    )


def assess_security(
    case: FrozenDecisionCase,
    command: SecurityAssessment,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
) -> ExitOutcome:
    scope = case.access_scope
    assert scope is not None
    history = ledger.governance_history(connection, scope)
    thesis, thesis_event_id = resolve_thesis(command, case, ledger, connection)
    hard_gates: list[ExitHardGate] = []
    visible = tuple(e for e in command.evidence if evidence_usable(e, command))
    conflicting = {
        (e.family, e.source, e.metric)
        for e in visible
        if any(
            (other.family, other.source, other.metric) == (e.family, e.source, e.metric)
            and (other.value, other.legal_termination) != (e.value, e.legal_termination)
            for other in visible
        )
    }
    usable = tuple(e for e in visible if (e.family, e.source, e.metric) not in conflicting)
    for evidence in usable:
        if (
            evidence.family == "SECURITY"
            and evidence.authority == "EXCHANGE"
            and evidence.legal_termination
        ):
            hard_gates.append(
                ExitHardGate(
                    gate_id="LEGAL_TERMINATION",
                    evidence_id=evidence.evidence_id,
                    authority="EXCHANGE",
                    source=evidence.source,
                    thesis_event_id=None,
                    established_at=command.cutoff_at,
                )
            )
    if thesis is not None:
        for proposition in thesis.propositions:
            for evidence in usable:
                if (
                    evidence.authority != proposition.authority
                    or evidence.source != proposition.source
                    or evidence.metric != proposition.metric
                    or evidence.public_at < thesis.registered_at
                    or evidence.public_at > thesis.valid_until
                ):
                    continue
                falsified = {
                    "LE": evidence.value <= proposition.threshold,
                    "GE": evidence.value >= proposition.threshold,
                    "EQ": evidence.value == proposition.threshold,
                }[proposition.operator]
                if falsified:
                    hard_gates.append(
                        ExitHardGate(
                            gate_id=f"THESIS_FALSIFIED:{proposition.proposition_id}",
                            evidence_id=evidence.evidence_id,
                            authority=proposition.authority,
                            source=evidence.source,
                            thesis_event_id=thesis_event_id,
                            established_at=command.cutoff_at,
                        )
                    )
    probabilities = []
    reasons: list[str] = []
    if len(usable) != len(command.evidence) or {e.family for e in usable} != {
        "SECURITY",
        "MARKET",
        "RISK",
    }:
        reasons.append("SECURITY_EVIDENCE_INCOMPLETE")
    if not any(
        e.family == "MARKET" and e.metric == "standard_price" and e.value == command.standard_price
        for e in usable
    ):
        reasons.append("STANDARD_PRICE_UNAVAILABLE")
    if thesis is None or thesis.valid_until < command.cutoff_at:
        reasons.append("THESIS_UNAVAILABLE_OR_EXPIRED")
    global_reasons = tuple(reasons)
    downside = sorted(
        (p for p in command.predictions if p.calibration.target == "D20"),
        key=lambda p: p.calibration.loss_boundary or "",
    )
    incoherent = any(
        left.point < right.point for left, right in zip(downside, downside[1:], strict=False)
    )
    for prediction in command.predictions:
        calibration = prediction.calibration
        qualification_scope = prediction.qualification_scope
        failures = list(global_reasons)
        if prediction.calibration.target == "D20" and incoherent:
            failures.append("DOWNSIDE_GRID_INCOHERENT")
        if (
            calibration.version != command.version
            or calibration.market_state != command.market_state
            or calibration.calendar_version != command.calendar_version
        ):
            failures.append("PREDICTION_VERSION_OR_STATE_MISMATCH")
        if (
            qualification_scope.capability != "security-forward-exit"
            or qualification_scope.purpose != "security-forward-assessment"
            or qualification_scope.user_id != scope.user_id
            or set(qualification_scope.account_ids) != set(scope.account_ids)
            or qualification_scope.market_state != command.market_state
            or qualification_scope.board != command.board
            or qualification_scope.target != calibration.target
            or qualification_scope.probability_grid != (calibration.loss_boundary or "POSITIVE")
            or qualification_scope.holding_age_domain != "INITIAL"
            or thesis is None
            or qualification_scope.source != thesis.origin
        ):
            failures.append("QUALIFICATION_SCOPE_MISMATCH")
        if (
            prediction.produced_at != command.cutoff_at
            or prediction.valid_until < command.cutoff_at
        ):
            failures.append("PREDICTION_NOT_FRESH")
        if not calibration.band_lower <= prediction.point <= calibration.band_upper:
            failures.append("CALIBRATION_BAND_MISMATCH")
        record = current_qualification(history, qualification_scope, calibration.version)
        basis = (
            (record.formal_passing_evidence or record.authorization_evidence) if record else None
        )
        digest = sha256(
            json.dumps(
                calibration.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        if (
            record is None
            or record.decision_id != prediction.qualification_id
            or record.status not in {"VALID", "AT_RISK"}
            or record.recorded_at > command.cutoff_at
            or record.evidence.available_at > command.cutoff_at
            or not qualification_is_current(record, command.cutoff_at)
            or basis is None
            or basis.available_at > command.cutoff_at
            or basis.digest != digest
            or basis.market_calendar_version != command.calendar_version
        ):
            failures.append("TARGET_QUALIFICATION_UNAVAILABLE")
        with localcontext(Context(prec=34)):
            lower = max(Decimal(0), prediction.point - calibration.lower_error)
            upper = min(Decimal(1), prediction.point + calibration.upper_error)
        probabilities.append(
            GuardedExitProbability(
                target=calibration.target,
                loss_boundary=calibration.loss_boundary,
                point=prediction.point,
                guarded_lower=lower if not failures else None,
                guarded_upper=upper if not failures else None,
                qualification_status=(
                    "AT_RISK"
                    if record is not None and not failures and record.status == "AT_RISK"
                    else "VALID"
                    if record is not None and not failures
                    else "NOT_QUALIFIED"
                ),
                qualification_id=prediction.qualification_id,
                qualification_scope=qualification_scope,
                version=calibration.version,
                produced_at=prediction.produced_at,
                valid_until=prediction.valid_until,
                calibration=calibration,
                reasons=tuple(failures),
            )
        )
        reasons.extend(failures)
    keys = [(p.target, p.loss_boundary) for p in probabilities]
    expected = [("D20", loss) for loss in ("0.05", "0.10", "0.15", "0.20")] + [("V60", None)]
    if sorted(keys, key=str) != sorted(expected, key=str):
        reasons.append("MODEL_TARGETS_INCOMPLETE")
    return ExitOutcome(
        disposition="NON_ACTIONABLE" if reasons else "ASSESSED",
        reasons=tuple(dict.fromkeys(reasons)),
        security_id=command.security_id,
        standard_price=command.standard_price,
        evidence=command.evidence,
        targets=(
            ExitTarget(target="D20", market_sessions=20, aggregation="DAILY_MINIMUM"),
            ExitTarget(
                target="V60", market_sessions=60, aggregation="TERMINAL_INCREMENT_VS_EXIT_CASH"
            ),
        ),
        probabilities=tuple(probabilities),
        thesis=thesis,
        thesis_event_id=thesis_event_id,
        hard_gates=tuple(hard_gates),
    )


def resolve_thesis(
    command: SecurityAssessment,
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
) -> tuple[ExitThesis | None, str | None]:
    if command.thesis_event_id is None:
        return None, None
    report = ledger.get_formal_report_for_event(command.thesis_event_id, connection)
    if (
        report is None
        or report.access_scope is None
        or case.access_scope is None
        or not report.access_scope.same_scope_as(case.access_scope)
        or report.result.exit_assessment is None
        or report.result.exit_assessment.disposition != "THESIS_REGISTERED"
    ):
        return None, None
    thesis = report.result.exit_assessment.thesis
    if (
        thesis is None
        or thesis.security_id != command.security_id
        or thesis.lifecycle_id != command.lifecycle_id
        or thesis.registered_at > command.cutoff_at
    ):
        return None, None
    return thesis, command.thesis_event_id


def evidence_usable(evidence: SecurityEvidence, command: SecurityAssessment) -> bool:
    return (
        evidence.security_id == command.security_id
        and evidence.complete
        and (
            (evidence.family in {"SECURITY", "MARKET"} and evidence.authority == "EXCHANGE")
            or (evidence.family == "RISK" and evidence.authority in {"DISCLOSURE", "EXCHANGE"})
        )
        and evidence.public_at <= evidence.acquired_at <= evidence.validated_at <= command.cutoff_at
        and evidence.valid_until >= command.cutoff_at
    )
