"""Deterministic D0 governance; synthetic evidence cannot authorize real use."""

from datetime import datetime

from stock_profiler.modules.qualification.contracts import (
    CapabilityVersion,
    GovernanceOutcome,
    QualificationCommand,
    QualificationRecord,
    QualificationScope,
)


def adjudicate(
    command: QualificationCommand,
    *,
    event_id: str,
    observed_at: str,
    knowledge_cutoff: str,
    history: tuple[GovernanceOutcome, ...],
) -> GovernanceOutcome:
    now = datetime.fromisoformat(observed_at)
    cutoff = datetime.fromisoformat(knowledge_cutoff)
    evidence = command.evidence
    if (
        evidence.scope != command.scope
        or evidence.version != command.version
        or evidence.evaluation_end > evidence.available_at
        or evidence.available_at > now
        or evidence.expires_at < now
    ):
        return GovernanceOutcome(disposition="DENIED", reasons=("QUALIFICATION_EVIDENCE_INVALID",))
    if evidence.available_at > cutoff:
        return GovernanceOutcome(
            disposition="DENIED", reasons=("QUALIFICATION_EVIDENCE_AFTER_CUTOFF",)
        )
    previous = current_qualification(history, command.scope, command.version)
    if command.previous_decision_id != (previous.decision_id if previous else None):
        return GovernanceOutcome(disposition="DENIED", reasons=("QUALIFICATION_REVISION_CONFLICT",))
    if command.action == "ALERT":
        if previous is None or evidence.kind != "DIAGNOSTIC_ALERT":
            return GovernanceOutcome(
                disposition="DENIED", reasons=("ORIGINAL_AUTHORIZATION_REQUIRED",)
            )
        if any(
            inherited.available_at > cutoff
            for inherited in (
                previous.authorization_evidence,
                previous.evidence,
                *previous.alerts,
            )
        ):
            return GovernanceOutcome(
                disposition="DENIED", reasons=("QUALIFICATION_HISTORY_AFTER_CUTOFF",)
            )
        return GovernanceOutcome(
            disposition="APPROVED",
            reasons=("DIAGNOSTIC_ALERT",),
            qualification=previous.model_copy(
                update={
                    "decision_id": event_id,
                    "previous_decision_id": previous.decision_id,
                    "status": "AT_RISK",
                    "cause": evidence.kind,
                    "evidence": evidence,
                    "alerts": (*previous.alerts, evidence),
                    "recorded_at": now,
                }
            ),
        )
    if previous is not None or evidence.kind != "QUALIFICATION_PASS":
        return GovernanceOutcome(disposition="DENIED", reasons=("QUALIFICATION_GRANT_NOT_ALLOWED",))
    return GovernanceOutcome(
        disposition="APPROVED",
        reasons=("QUALIFICATION_PASS",),
        qualification=QualificationRecord(
            decision_id=event_id,
            authorization_id=event_id,
            scope=command.scope,
            version=command.version,
            status="VALID",
            cause=evidence.kind,
            authorization_evidence=evidence,
            recorded_at=now,
            evidence=evidence,
        ),
    )


def current_qualification(
    history: tuple[GovernanceOutcome, ...],
    scope: QualificationScope,
    version: CapabilityVersion,
) -> QualificationRecord | None:
    records = [
        outcome.qualification
        for outcome in history
        if outcome.qualification is not None
        and outcome.qualification.scope == scope
        and outcome.qualification.version == version
    ]
    superseded = {record.previous_decision_id for record in records}
    heads = [record for record in records if record.decision_id not in superseded]
    if len(heads) > 1:
        raise ValueError("qualification history has conflicting revisions")
    return heads[0] if heads else None
