"""Deterministic D0 governance; synthetic evidence cannot authorize real use."""

from datetime import datetime

from stock_profiler.modules.qualification.contracts import (
    CapabilityVersion,
    GovernanceOutcome,
    QualificationCommand,
    QualificationEvidence,
    QualificationRecord,
    QualificationRestriction,
    QualificationScope,
)
from stock_profiler.modules.qualification.evidence import (
    evidence_is_current,
    formal_check_passed,
    qualification_deadline,
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
    if command.version.policy_error is not None:
        return GovernanceOutcome(disposition="DENIED", reasons=(command.version.policy_error,))
    if (
        evidence.scope != command.scope
        or evidence.version != command.version
        or evidence.evaluation_end > evidence.available_at
        or evidence.available_at > now
        or evidence.expires_at < now
        or (
            evidence.state_activity_end is not None
            and evidence.state_activity_end > evidence.available_at
        )
    ):
        return GovernanceOutcome(disposition="DENIED", reasons=("QUALIFICATION_EVIDENCE_INVALID",))
    if evidence.available_at > cutoff:
        return GovernanceOutcome(
            disposition="DENIED", reasons=("QUALIFICATION_EVIDENCE_AFTER_CUTOFF",)
        )
    if policy_identity_conflicts(command.scope, command.version, history):
        return GovernanceOutcome(
            disposition="DENIED", reasons=("QUALIFICATION_POLICY_IDENTITY_CONFLICT",)
        )
    previous = current_qualification(history, command.scope, command.version)
    if command.previous_decision_id != (previous.decision_id if previous else None):
        return GovernanceOutcome(disposition="DENIED", reasons=("QUALIFICATION_REVISION_CONFLICT",))
    if previous is not None and not previous.evidence_available_by(cutoff):
        return GovernanceOutcome(
            disposition="DENIED", reasons=("QUALIFICATION_HISTORY_AFTER_CUTOFF",)
        )
    if command.action == "FORMAL_CHECK":
        return _formal_check(command, previous, event_id, now)
    if command.action == "REQUALIFY":
        proof = command.requalification
        if (
            previous is None
            or previous.status != "REVOKED"
            or previous.authorization_evidence is None
            or previous.authorization_terminated_at is None
            or proof is None
            or evidence.kind != "REQUALIFICATION_PASS"
        ):
            return GovernanceOutcome(disposition="DENIED", reasons=("REQUALIFICATION_NOT_ALLOWED",))
        original_check = (previous.formal_evidence or previous.authorization_evidence).formal_check
        check = evidence.formal_check
        if (
            not (
                previous.authorization_terminated_at
                < proof.registered_at
                <= proof.locked_at
                <= proof.first_prediction_frozen_at
                <= proof.forward_evidence.evaluation_end
            )
            or proof.historical_evidence.kind != "HISTORICAL_OOS_PASS"
            or proof.forward_evidence.kind != "LOCKED_FORWARD_PASS"
            or any(
                item.scope != command.scope
                or item.version != command.version
                or item.evaluation_end > item.available_at
                or item.available_at > evidence.available_at
                or not evidence_is_current(item, now)
                for item in (proof.historical_evidence, proof.forward_evidence)
            )
            or original_check is None
            or check is None
            or check.sequence_id != original_check.sequence_id
            or check.index != original_check.index + 1
            or check.registered_at != original_check.registered_at
            or check.scheduled_at <= original_check.scheduled_at
            or not (evidence.evaluation_end <= check.scheduled_at <= evidence.available_at)
            or not evidence_is_current(evidence, now)
            or any(
                item.qualification is not None
                and item.qualification.requalification is not None
                and item.qualification.requalification.application_id == proof.application_id
                for item in history
            )
        ):
            return GovernanceOutcome(
                disposition="DENIED", reasons=("REQUALIFICATION_PROOF_INVALID",)
            )
        return GovernanceOutcome(
            disposition="APPROVED",
            reasons=("REQUALIFICATION_PASS",),
            qualification=QualificationRecord(
                decision_id=event_id,
                authorization_id=event_id,
                scope=command.scope,
                version=command.version,
                status="AT_RISK" if previous.alerts else "VALID",
                cause="DIAGNOSTIC_ALERT" if previous.alerts else "REQUALIFICATION_PASS",
                authorization_evidence=evidence,
                recorded_at=now,
                previous_decision_id=previous.decision_id,
                evidence=evidence,
                alerts=previous.alerts,
                requalification=proof,
            ),
        )
    if command.action == "RESTORE":
        if (
            previous is None
            or previous.status != "SUSPENDED"
            or previous.authorization_evidence is None
            or not evidence_is_current(
                previous.formal_passing_evidence or previous.authorization_evidence, now
            )
            or evidence.kind != "RESTORATION_DECISION"
        ):
            return GovernanceOutcome(
                disposition="DENIED", reasons=("QUALIFICATION_RESTORATION_NOT_ALLOWED",)
            )
        proofs = command.restoration_evidence
        by_restriction = {proof.resolves_evidence_id: proof for proof in proofs}
        if (
            not previous.restrictions
            or len(by_restriction) != len(proofs)
            or set(by_restriction) != {item.evidence.evidence_id for item in previous.restrictions}
            or any(
                proof.scope != command.scope
                or proof.version != command.version
                or proof.evaluation_end > proof.available_at
                or proof.available_at > min(now, cutoff)
                or proof.expires_at < now
                for proof in proofs
            )
            or any(
                not _restores_restriction(
                    by_restriction[restriction.evidence.evidence_id], restriction, previous
                )
                for restriction in previous.restrictions
            )
        ):
            return GovernanceOutcome(
                disposition="DENIED", reasons=("QUALIFICATION_RESTORATION_INCOMPLETE",)
            )
        return GovernanceOutcome(
            disposition="APPROVED",
            reasons=("QUALIFICATION_RESTORED",),
            qualification=previous.model_copy(
                update={
                    "decision_id": event_id,
                    "previous_decision_id": previous.decision_id,
                    "status": "AT_RISK" if previous.alerts else "VALID",
                    "cause": "DIAGNOSTIC_ALERT" if previous.alerts else "QUALIFICATION_RESTORED",
                    "evidence": evidence,
                    "restrictions": (),
                    "restoration_evidence": proofs,
                    "recorded_at": now,
                }
            ),
        )
    if command.action in {"SUSPEND", "REVOKE"}:
        required_kind = (
            "REQUIRED_PREMISE_UNVERIFIABLE"
            if command.action == "SUSPEND"
            else "ORIGINAL_BASIS_INVALID"
        )
        if (
            previous is None
            or previous.authorization_id is None
            or previous.status == "REVOKED"
            or evidence.kind != required_kind
        ):
            return GovernanceOutcome(
                disposition="DENIED", reasons=("QUALIFICATION_RESTRICTION_NOT_SUPPORTED",)
            )
        return GovernanceOutcome(
            disposition="APPROVED",
            reasons=(evidence.kind,),
            qualification=previous.model_copy(
                update={
                    "decision_id": event_id,
                    "previous_decision_id": previous.decision_id,
                    "status": "SUSPENDED" if command.action == "SUSPEND" else "REVOKED",
                    "authorization_terminated_at": (
                        now if command.action == "REVOKE" else previous.authorization_terminated_at
                    ),
                    "cause": evidence.kind,
                    "evidence": evidence,
                    "restrictions": (
                        *previous.restrictions,
                        QualificationRestriction(
                            cause=(
                                "REQUIRED_PREMISE_UNVERIFIABLE"
                                if command.action == "SUSPEND"
                                else "ORIGINAL_BASIS_INVALID"
                            ),
                            evidence=evidence,
                        ),
                    ),
                    "recorded_at": now,
                }
            ),
        )
    if command.action == "RECORD_NOT_OBTAINED":
        if previous is not None or evidence.kind != "INSUFFICIENT_EVIDENCE":
            return GovernanceOutcome(
                disposition="DENIED", reasons=("QUALIFICATION_REGISTRATION_NOT_ALLOWED",)
            )
        return GovernanceOutcome(
            disposition="APPROVED",
            reasons=("INSUFFICIENT_EVIDENCE",),
            qualification=QualificationRecord(
                decision_id=event_id,
                authorization_id=None,
                scope=command.scope,
                version=command.version,
                status="NOT_OBTAINED",
                cause=evidence.kind,
                authorization_evidence=None,
                recorded_at=now,
                evidence=evidence,
            ),
        )
    if command.action == "ALERT":
        if (
            previous is None
            or previous.authorization_evidence is None
            or evidence.kind != "DIAGNOSTIC_ALERT"
        ):
            return GovernanceOutcome(
                disposition="DENIED", reasons=("ORIGINAL_AUTHORIZATION_REQUIRED",)
            )
        previous = restrict_expired_qualification(previous, now)
        return GovernanceOutcome(
            disposition="APPROVED",
            reasons=("DIAGNOSTIC_ALERT",),
            qualification=previous.model_copy(
                update={
                    "decision_id": event_id,
                    "previous_decision_id": previous.decision_id,
                    "status": (
                        previous.status
                        if previous.status in {"SUSPENDED", "REVOKED"}
                        else "AT_RISK"
                    ),
                    "cause": (
                        previous.cause
                        if previous.status in {"SUSPENDED", "REVOKED"}
                        else evidence.kind
                    ),
                    "evidence": evidence,
                    "alerts": (*previous.alerts, evidence),
                    "recorded_at": now,
                }
            ),
        )
    if (
        previous is not None and previous.status != "NOT_OBTAINED"
    ) or evidence.kind != "QUALIFICATION_PASS":
        return GovernanceOutcome(disposition="DENIED", reasons=("QUALIFICATION_GRANT_NOT_ALLOWED",))
    if not evidence_is_current(evidence, now):
        return GovernanceOutcome(disposition="DENIED", reasons=("QUALIFICATION_EVIDENCE_EXPIRED",))
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
            previous_decision_id=previous.decision_id if previous else None,
            recorded_at=now,
            evidence=evidence,
        ),
    )


def policy_identity_conflicts(
    scope: QualificationScope, version: CapabilityVersion, history: tuple[GovernanceOutcome, ...]
) -> bool:
    return any(
        binding is not None
        and binding.scope.same_scope_as(scope)
        and binding.version.policy_version == version.policy_version
        and binding.version.qualification_policy != version.qualification_policy
        for binding in (item.bound_policy for item in history)
    )


def restrict_expired_qualification(
    record: QualificationRecord, now: datetime
) -> QualificationRecord:
    basis = record.formal_passing_evidence or record.authorization_evidence
    deadline = qualification_deadline(basis) if basis is not None else None
    if (
        record.status not in {"VALID", "AT_RISK", "SUSPENDED"}
        or basis is None
        or deadline is None
        or deadline >= now
        or any(item.cause == "EVIDENCE_EXPIRED" for item in record.restrictions)
    ):
        return record
    return record.model_copy(
        update={
            "status": "SUSPENDED",
            "cause": record.cause if record.status == "SUSPENDED" else "EVIDENCE_EXPIRED",
            "restrictions": (
                *record.restrictions,
                QualificationRestriction(cause="EVIDENCE_EXPIRED", evidence=basis),
            ),
        }
    )


def _formal_check(
    command: QualificationCommand,
    previous: QualificationRecord | None,
    event_id: str,
    now: datetime,
) -> GovernanceOutcome:
    evidence = command.evidence
    check = evidence.formal_check
    original = (previous.formal_evidence or previous.authorization_evidence) if previous else None
    original_check = original.formal_check if original else None
    passed = formal_check_passed(evidence)
    if (
        previous is None
        or check is None
        or original_check is None
        or evidence.kind != "FORMAL_CHECK"
        or (passed is None and evidence.maturity_sufficient is not False)
        or not original_check.planned_nodes
        or check.planned_nodes != original_check.planned_nodes
        or check.required_gates != original_check.required_gates
        or check.sequence_id != original_check.sequence_id
        or check.registered_at != original_check.registered_at
        or check.index != original_check.index + (0 if passed is None else 1)
        or check.scheduled_at not in check.planned_nodes
        or check.scheduled_at <= original_check.scheduled_at
        or (
            previous.last_formal_node is not None
            and check.scheduled_at <= previous.last_formal_node
        )
        or not (
            check.registered_at
            <= evidence.evaluation_end
            <= check.scheduled_at
            <= evidence.available_at
        )
    ):
        return GovernanceOutcome(disposition="DENIED", reasons=("FORMAL_CHECK_NOT_ALLOWED",))
    previous = restrict_expired_qualification(previous, now)
    if passed is None:
        return GovernanceOutcome(
            disposition="APPROVED",
            reasons=("FORMAL_CHECK_WATERMARK_INSUFFICIENT",),
            qualification=previous.model_copy(
                update={
                    "decision_id": event_id,
                    "previous_decision_id": previous.decision_id,
                    "evidence": evidence,
                    "last_formal_node": check.scheduled_at,
                    "recorded_at": now,
                }
            ),
        )
    restricted = not passed and previous.status in {"VALID", "AT_RISK", "SUSPENDED"}
    restrictions = previous.restrictions
    if restricted:
        restrictions = (
            *restrictions,
            QualificationRestriction(cause="FORMAL_PERFORMANCE_FAILURE", evidence=evidence),
        )
    return GovernanceOutcome(
        disposition="APPROVED",
        reasons=("FORMAL_CHECK_PASSED" if passed else "FORMAL_CHECK_FAILED",),
        qualification=previous.model_copy(
            update={
                "decision_id": event_id,
                "previous_decision_id": previous.decision_id,
                "status": "SUSPENDED" if restricted else previous.status,
                "cause": "FORMAL_PERFORMANCE_FAILURE" if restricted else previous.cause,
                "evidence": evidence,
                "formal_evidence": evidence,
                "last_formal_node": check.scheduled_at,
                "formal_passing_evidence": (
                    evidence if passed else previous.formal_passing_evidence
                ),
                "restrictions": restrictions,
                "recorded_at": now,
            }
        ),
    )


def _restores_restriction(
    proof: QualificationEvidence,
    restriction: QualificationRestriction,
    previous: QualificationRecord,
) -> bool:
    if restriction.cause == "REQUIRED_PREMISE_UNVERIFIABLE":
        return (
            proof.kind == "ORIGINAL_BASIS_RESTORED"
            and previous.authorization_evidence is not None
            and proof.restored_authorization_digest == previous.authorization_evidence.digest
        )
    if restriction.cause in {"FORMAL_PERFORMANCE_FAILURE", "EVIDENCE_EXPIRED"}:
        return (
            proof.kind == "FORMAL_CHECK"
            and formal_check_passed(proof) is True
            and previous.formal_evidence is not None
            and proof.scope.same_scope_as(previous.formal_evidence.scope)
            and proof.model_copy(
                update={
                    "resolves_evidence_id": None,
                    "scope": previous.formal_evidence.scope,
                }
            )
            == previous.formal_evidence
            and proof.evaluation_end > restriction.evidence.evaluation_end
        )
    return False


def current_qualification(
    history: tuple[GovernanceOutcome, ...],
    scope: QualificationScope,
    version: CapabilityVersion,
) -> QualificationRecord | None:
    records = [
        outcome.qualification
        for outcome in history
        if outcome.qualification is not None
        and outcome.qualification.scope.same_scope_as(scope)
        and outcome.qualification.version == version
    ]
    superseded = {record.previous_decision_id for record in records}
    heads = [record for record in records if record.decision_id not in superseded]
    if len(heads) > 1:
        raise ValueError("qualification history has conflicting revisions")
    return heads[0] if heads else None
