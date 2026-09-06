"""Deterministic D0 governance; synthetic evidence cannot authorize real use."""

from datetime import datetime
from typing import Literal

from stock_profiler.modules.qualification.contracts import (
    AlertClosure,
    AlertClosureProof,
    CapabilityVersion,
    FormalCheckIdentity,
    FormalNodeDisposition,
    GovernanceOutcome,
    QualificationCommand,
    QualificationEvidence,
    QualificationRecord,
    QualificationRestriction,
    QualificationScope,
    RequalificationPopulation,
    RequalificationPopulationRegistration,
)
from stock_profiler.modules.qualification.evidence import (
    capability_version_digest,
    evidence_basis_is_valid,
    evidence_is_current,
    formal_check_passed,
    overall_qualification_deadline,
    requalification_registration_digest,
    state_activity_deadline,
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
        or not evidence_basis_is_valid(evidence)
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
    previous = current_substantive_qualification(history, command.scope, command.version)
    if command.previous_decision_id != (previous.decision_id if previous else None):
        return GovernanceOutcome(disposition="DENIED", reasons=("QUALIFICATION_REVISION_CONFLICT",))
    if previous is not None and not previous.evidence_available_by(cutoff):
        return GovernanceOutcome(
            disposition="DENIED", reasons=("QUALIFICATION_HISTORY_AFTER_CUTOFF",)
        )
    if command.action == "FORMAL_CHECK":
        return _formal_check(command, previous, event_id, now)
    if command.action == "RECORD_FORMAL_NODE":
        return _record_formal_node_disposition(command, previous, event_id, now)
    if command.action == "CLOSE_ALERT":
        return _close_alert(command, previous, event_id, now, cutoff)
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
        historical_population = proof.historical_population
        forward_population = proof.forward_population
        historical_registration = proof.historical_registration
        forward_registration = proof.forward_registration
        next_node = (
            _next_formal_node(previous, original_check) if original_check is not None else None
        )
        if (
            not (
                previous.authorization_terminated_at
                < proof.registered_at
                <= proof.locked_at
                <= proof.first_prediction_frozen_at
                <= proof.forward_evidence.evaluation_end
            )
            or proof.frozen_version_digest is None
            or proof.frozen_version_digest != capability_version_digest(command.version)
            or historical_population is None
            or forward_population is None
            or historical_registration is None
            or forward_registration is None
            or historical_population.population_id == forward_population.population_id
            or proof.historical_evidence.evidence_id == proof.forward_evidence.evidence_id
            or evidence.requalification_application_id != proof.application_id
            or proof.historical_evidence.kind != "HISTORICAL_OOS_PASS"
            or proof.forward_evidence.kind != "LOCKED_FORWARD_PASS"
            or not _requalification_registration_is_valid(
                historical_registration,
                proof.application_id,
                "HISTORICAL",
                proof.registered_at,
                proof.locked_at,
                proof.frozen_version_digest,
            )
            or not _requalification_registration_is_valid(
                forward_registration,
                proof.application_id,
                "FORWARD",
                proof.registered_at,
                proof.locked_at,
                proof.frozen_version_digest,
            )
            or any(
                item.scope != command.scope
                or item.version != command.version
                or not evidence_basis_is_valid(item)
                or item.evaluation_end > item.available_at
                or item.available_at > evidence.available_at
                or not evidence_is_current(item, now)
                for item in (proof.historical_evidence, proof.forward_evidence)
            )
            or not _requalification_population_is_complete(
                historical_population,
                proof.historical_evidence,
                proof.frozen_version_digest,
                historical_registration,
            )
            or not _requalification_population_is_complete(
                forward_population,
                proof.forward_evidence,
                proof.frozen_version_digest,
                forward_registration,
            )
            or any(
                member.matured_at > proof.registered_at for member in historical_population.members
            )
            or any(
                member.prediction_frozen_at < proof.locked_at
                for member in forward_population.members
            )
            or min(member.prediction_frozen_at for member in forward_population.members)
            != proof.first_prediction_frozen_at
            or original_check is None
            or check is None
            or not original_check.planned_nodes
            or original_check.error_budget_id is None
            or not _same_formal_sequence(check, original_check)
            or check.index != original_check.index + 1
            or check.scheduled_at != next_node
            or formal_check_passed(evidence) is not True
            or (set(historical_population.required_gates) | set(forward_population.required_gates))
            != set(check.required_gates)
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
        disposition = FormalNodeDisposition(
            scheduled_at=check.scheduled_at,
            status="EXECUTED_PASS",
            check_index=check.index,
            evidence=evidence,
            recorded_at=now,
        )
        return GovernanceOutcome(
            disposition="APPROVED",
            reasons=("REQUALIFICATION_PASS",),
            qualification=QualificationRecord(
                decision_id=event_id,
                authorization_id=event_id,
                scope=command.scope,
                version=command.version,
                status="AT_RISK" if previous.outstanding_alerts else "VALID",
                cause=(
                    "DIAGNOSTIC_ALERT" if previous.outstanding_alerts else "REQUALIFICATION_PASS"
                ),
                authorization_evidence=evidence,
                recorded_at=now,
                previous_decision_id=previous.decision_id,
                evidence=evidence,
                alerts=previous.alerts,
                alert_closures=previous.alert_closures,
                requalification=proof,
                formal_evidence=evidence,
                formal_passing_evidence=evidence,
                last_formal_node=check.scheduled_at,
                formal_node_dispositions=(
                    *previous.formal_node_dispositions,
                    disposition,
                ),
            ),
        )
    if command.action == "RESTORE":
        if (
            previous is None
            or previous.status != "SUSPENDED"
            or previous.authorization_evidence is None
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
                or not evidence_basis_is_valid(proof)
                or proof.evaluation_end > proof.available_at
                or proof.available_at > min(now, cutoff)
                or proof.expires_at < now
                for proof in proofs
            )
            or any(
                not _restores_restriction(
                    by_restriction[restriction.evidence.evidence_id],
                    restriction,
                    previous,
                    now,
                )
                for restriction in previous.restrictions
            )
        ):
            return GovernanceOutcome(
                disposition="DENIED", reasons=("QUALIFICATION_RESTORATION_INCOMPLETE",)
            )
        state_activity_proofs = tuple(
            by_restriction[restriction.evidence.evidence_id]
            for restriction in previous.restrictions
            if restriction.cause == "STATE_ACTIVITY_EXPIRED"
        )
        return GovernanceOutcome(
            disposition="APPROVED",
            reasons=("QUALIFICATION_RESTORED",),
            qualification=previous.model_copy(
                update={
                    "decision_id": event_id,
                    "previous_decision_id": previous.decision_id,
                    "status": "AT_RISK" if previous.outstanding_alerts else "VALID",
                    "cause": (
                        "DIAGNOSTIC_ALERT"
                        if previous.outstanding_alerts
                        else "QUALIFICATION_RESTORED"
                    ),
                    "evidence": evidence,
                    "restrictions": (),
                    "restoration_evidence": proofs,
                    "state_activity_evidence": (
                        state_activity_proofs[-1]
                        if state_activity_proofs
                        else previous.state_activity_evidence
                    ),
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
            or not _diagnostic_plan_is_valid(evidence)
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
    cause = _qualification_expiry_cause(record, now)
    if (
        record.status not in {"VALID", "AT_RISK", "SUSPENDED"}
        or basis is None
        or cause is None
        or any(item.cause == cause for item in record.restrictions)
    ):
        return record
    return record.model_copy(
        update={
            "status": "SUSPENDED",
            "cause": record.cause if record.status == "SUSPENDED" else cause,
            "restrictions": (
                *record.restrictions,
                QualificationRestriction(cause=cause, evidence=basis),
            ),
        }
    )


def qualification_is_current(record: QualificationRecord, now: datetime) -> bool:
    return _qualification_expiry_cause(record, now) is None


def _qualification_expiry_cause(
    record: QualificationRecord,
    now: datetime,
) -> Literal["EVIDENCE_EXPIRED", "STATE_ACTIVITY_EXPIRED"] | None:
    basis = record.formal_passing_evidence or record.authorization_evidence
    if basis is None:
        return "EVIDENCE_EXPIRED"
    overall = overall_qualification_deadline(basis)
    if overall is None or overall < now:
        return "EVIDENCE_EXPIRED"
    state_source = record.state_activity_evidence or basis
    state = state_activity_deadline(state_source)
    policy = basis.version.qualification_policy
    if (policy is not None and policy.require_state_activity and state is None) or (
        state is not None and state < now
    ):
        return "STATE_ACTIVITY_EXPIRED"
    return None


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
    next_node = (
        _next_formal_node(previous, original_check)
        if previous is not None and original_check is not None
        else None
    )
    if (
        previous is None
        or check is None
        or original_check is None
        or evidence.kind != "FORMAL_CHECK"
        or (passed is None and evidence.maturity_sufficient is not False)
        or not original_check.planned_nodes
        or not _same_formal_sequence(check, original_check)
        or check.index != original_check.index + (0 if passed is None else 1)
        or check.scheduled_at not in check.planned_nodes
        or check.scheduled_at != next_node
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
        disposition = FormalNodeDisposition(
            scheduled_at=check.scheduled_at,
            status="INSUFFICIENT",
            check_index=check.index,
            evidence=evidence,
            recorded_at=now,
        )
        return GovernanceOutcome(
            disposition="APPROVED",
            reasons=("FORMAL_CHECK_WATERMARK_INSUFFICIENT",),
            qualification=previous.model_copy(
                update={
                    "decision_id": event_id,
                    "previous_decision_id": previous.decision_id,
                    "evidence": evidence,
                    "last_formal_node": check.scheduled_at,
                    "formal_node_dispositions": (
                        *previous.formal_node_dispositions,
                        disposition,
                    ),
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
    disposition = FormalNodeDisposition(
        scheduled_at=check.scheduled_at,
        status="EXECUTED_PASS" if passed else "EXECUTED_FAIL",
        check_index=check.index,
        evidence=evidence,
        recorded_at=now,
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
                "formal_node_dispositions": (
                    *previous.formal_node_dispositions,
                    disposition,
                ),
                "restrictions": restrictions,
                "recorded_at": now,
            }
        ),
    )


def _record_formal_node_disposition(
    command: QualificationCommand,
    previous: QualificationRecord | None,
    event_id: str,
    now: datetime,
) -> GovernanceOutcome:
    evidence = command.evidence
    check = evidence.formal_check
    original = (previous.formal_evidence or previous.authorization_evidence) if previous else None
    original_check = original.formal_check if original else None
    next_node = (
        _next_formal_node(previous, original_check)
        if previous is not None and original_check is not None
        else None
    )
    if (
        previous is None
        or check is None
        or original_check is None
        or evidence.kind != "FORMAL_NODE_NOT_EXECUTED"
        or not original_check.planned_nodes
        or not _same_formal_sequence(check, original_check)
        or check.index != original_check.index
        or check.scheduled_at != next_node
        or not (
            check.registered_at
            <= evidence.evaluation_end
            <= check.scheduled_at
            <= evidence.available_at
        )
    ):
        return GovernanceOutcome(disposition="DENIED", reasons=("FORMAL_NODE_NOT_ALLOWED",))
    disposition = FormalNodeDisposition(
        scheduled_at=check.scheduled_at,
        status="NOT_EXECUTED",
        check_index=check.index,
        evidence=evidence,
        recorded_at=now,
    )
    return GovernanceOutcome(
        disposition="APPROVED",
        reasons=("FORMAL_NODE_NOT_EXECUTED",),
        qualification=previous.model_copy(
            update={
                "decision_id": event_id,
                "previous_decision_id": previous.decision_id,
                "evidence": evidence,
                "last_formal_node": check.scheduled_at,
                "formal_node_dispositions": (
                    *previous.formal_node_dispositions,
                    disposition,
                ),
                "recorded_at": now,
            }
        ),
    )


def _next_formal_node(
    record: QualificationRecord,
    original_check: FormalCheckIdentity,
) -> datetime | None:
    anchor = original_check.scheduled_at
    if record.formal_node_dispositions:
        anchor = record.formal_node_dispositions[-1].scheduled_at
    elif record.last_formal_node is not None:
        if record.last_formal_node != original_check.scheduled_at:
            return None
        anchor = record.last_formal_node
    return next((node for node in original_check.planned_nodes if node > anchor), None)


def _same_formal_sequence(
    proposed: FormalCheckIdentity,
    original: FormalCheckIdentity,
) -> bool:
    return (
        proposed.sequence_id == original.sequence_id
        and proposed.registered_at == original.registered_at
        and proposed.planned_nodes == original.planned_nodes
        and proposed.required_gates == original.required_gates
        and proposed.error_budget_id == original.error_budget_id
    )


def _close_alert(
    command: QualificationCommand,
    previous: QualificationRecord | None,
    event_id: str,
    now: datetime,
    cutoff: datetime,
) -> GovernanceOutcome:
    evidence = command.evidence
    proof = command.alert_closure
    policy = command.version.qualification_policy
    if (
        previous is None
        or previous.authorization_evidence is None
        or evidence.kind != "ALERT_CLOSURE"
        or proof is None
        or policy is None
        or policy.diagnostic_clear_node_count is None
    ):
        return GovernanceOutcome(disposition="DENIED", reasons=("ALERT_CLOSURE_NOT_ALLOWED",))
    target = next(
        (item for item in previous.alerts if item.evidence_id == proof.alert_evidence_id),
        None,
    )
    clear_count = policy.diagnostic_clear_node_count
    plan = target.diagnostic_plan if target is not None else None
    disappearance = proof.resolution == "DISAPPEARED"
    if (
        target is None
        or plan is None
        or proof.alert_evidence_id != evidence.resolves_evidence_id
        or any(
            closure.alert_evidence_id == proof.alert_evidence_id
            for closure in previous.alert_closures
        )
        or not target.scope.same_scope_as(command.scope)
        or target.version != command.version
        or len(plan.planned_nodes) < clear_count
        or len(set(plan.planned_nodes)) != len(plan.planned_nodes)
        or tuple(sorted(plan.planned_nodes)) != plan.planned_nodes
        or any(node <= target.evaluation_end for node in plan.planned_nodes)
        or proof.rule_version != plan.rule_version
        or (
            disappearance
            and (
                proof.resolution_evidence is not None
                or proof.transferred_restriction_evidence_id is not None
                or len(proof.observations) < clear_count
                or not _diagnostic_observation_nodes_are_consecutive(proof, plan.planned_nodes)
                or not _diagnostic_observations_support_disappearance(
                    proof,
                    command,
                    target,
                    evidence,
                    clear_count,
                    now,
                    cutoff,
                )
            )
        )
        or (
            not disappearance
            and not _direct_alert_resolution_is_valid(
                proof,
                command,
                previous,
                target,
                evidence,
                now,
                cutoff,
            )
        )
    ):
        return GovernanceOutcome(disposition="DENIED", reasons=("ALERT_CLOSURE_PROOF_INVALID",))
    closure = AlertClosure(
        alert_evidence_id=target.evidence_id,
        resolution=proof.resolution,
        proof=proof,
        evidence=evidence,
        recorded_at=now,
    )
    closures = (*previous.alert_closures, closure)
    closed_ids = {item.alert_evidence_id for item in closures}
    outstanding = tuple(item for item in previous.alerts if item.evidence_id not in closed_ids)
    restricted = previous.status in {"SUSPENDED", "REVOKED"}
    archived = proof.resolution == "ARCHIVED"
    return GovernanceOutcome(
        disposition="APPROVED",
        reasons=("ALERT_CLOSED",),
        qualification=previous.model_copy(
            update={
                "decision_id": event_id,
                "previous_decision_id": previous.decision_id,
                "status": (
                    previous.status
                    if restricted or archived
                    else ("AT_RISK" if outstanding else "VALID")
                ),
                "cause": (
                    previous.cause
                    if restricted or archived
                    else ("DIAGNOSTIC_ALERT" if outstanding else "ALERT_CLOSED")
                ),
                "evidence": evidence,
                "alert_closures": closures,
                "recorded_at": now,
            }
        ),
    )


def _diagnostic_plan_is_valid(evidence: QualificationEvidence) -> bool:
    plan = evidence.diagnostic_plan
    if plan is None:
        return True
    return (
        len(set(plan.planned_nodes)) == len(plan.planned_nodes)
        and tuple(sorted(plan.planned_nodes)) == plan.planned_nodes
        and all(node > evidence.evaluation_end for node in plan.planned_nodes)
    )


def _diagnostic_observation_nodes_are_consecutive(
    proof: AlertClosureProof,
    planned_nodes: tuple[datetime, ...],
) -> bool:
    try:
        indices = tuple(
            planned_nodes.index(observation.scheduled_at) for observation in proof.observations
        )
    except ValueError:
        return False
    return bool(indices) and all(
        current == previous + 1 for previous, current in zip(indices, indices[1:], strict=False)
    )


def _diagnostic_observations_support_disappearance(
    proof: AlertClosureProof,
    command: QualificationCommand,
    target: QualificationEvidence,
    closure_evidence: QualificationEvidence,
    clear_count: int,
    now: datetime,
    cutoff: datetime,
) -> bool:
    observations = proof.observations
    if any(item.status != "CLEAR" for item in observations[-clear_count:]):
        return False
    evidence_ids = tuple(item.evidence.evidence_id for item in observations)
    evaluation_ends = tuple(item.evidence.evaluation_end for item in observations)
    available_times = tuple(item.evidence.available_at for item in observations)
    expected_kinds = {
        "CLEAR": "DIAGNOSTIC_CLEAR",
        "RECURRENT": "DIAGNOSTIC_RECURRENT",
        "INSUFFICIENT": "DIAGNOSTIC_INSUFFICIENT",
        "UNAVAILABLE": "DIAGNOSTIC_UNAVAILABLE",
    }
    return (
        len(set(evidence_ids)) == len(evidence_ids)
        and tuple(sorted(evaluation_ends)) == evaluation_ends
        and len(set(evaluation_ends)) == len(evaluation_ends)
        and tuple(sorted(available_times)) == available_times
        and len(set(available_times)) == len(available_times)
        and evaluation_ends[-1] <= closure_evidence.evaluation_end
        and available_times[-1] <= closure_evidence.available_at
        and all(
            item.rule_version == proof.rule_version
            and item.evidence.kind == expected_kinds[item.status]
            and item.evidence.diagnostic_plan is None
            and item.evidence.resolves_evidence_id == target.evidence_id
            and item.evidence.scope.same_scope_as(command.scope)
            and item.evidence.version == command.version
            and evidence_basis_is_valid(item.evidence)
            and target.evaluation_end < item.evidence.evaluation_end
            and item.evidence.evaluation_end
            <= item.scheduled_at
            <= item.evidence.available_at
            <= min(closure_evidence.available_at, cutoff)
            and evidence_is_current(item.evidence, now)
            for item in observations
        )
    )


def _direct_alert_resolution_is_valid(
    proof: AlertClosureProof,
    command: QualificationCommand,
    previous: QualificationRecord,
    target: QualificationEvidence,
    closure_evidence: QualificationEvidence,
    now: datetime,
    cutoff: datetime,
) -> bool:
    resolution_evidence = proof.resolution_evidence
    expected_kind = {
        "PROVEN_ERRONEOUS": "ALERT_PROVEN_ERRONEOUS",
        "TRANSFERRED": "ALERT_TRANSFERRED",
        "ARCHIVED": "ALERT_ARCHIVED",
    }.get(proof.resolution)
    if (
        expected_kind is None
        or resolution_evidence is None
        or proof.observations
        or resolution_evidence.kind != expected_kind
        or resolution_evidence.resolves_evidence_id != target.evidence_id
        or resolution_evidence.reviewed_alert_digest != target.digest
        or not resolution_evidence.scope.same_scope_as(command.scope)
        or resolution_evidence.version != command.version
        or not evidence_basis_is_valid(resolution_evidence)
        or target.evaluation_end > resolution_evidence.evaluation_end
        or resolution_evidence.evaluation_end > resolution_evidence.available_at
        or resolution_evidence.available_at > min(closure_evidence.available_at, cutoff)
        or not evidence_is_current(resolution_evidence, now)
    ):
        return False
    if proof.resolution == "TRANSFERRED":
        transferred_id = proof.transferred_restriction_evidence_id
        return transferred_id is not None and any(
            restriction.evidence.evidence_id == transferred_id
            for restriction in previous.restrictions
        )
    return proof.transferred_restriction_evidence_id is None


def _requalification_population_is_complete(
    population: RequalificationPopulation,
    evidence: QualificationEvidence,
    frozen_version_digest: str,
    registration: RequalificationPopulationRegistration,
) -> bool:
    registered_ids = population.registered_member_ids
    member_ids = tuple(member.member_id for member in population.members)
    required_gates = population.required_gates
    gate_ids = tuple(gate.gate_id for gate in population.gate_results)
    return (
        population.evidence_id == evidence.evidence_id
        and population.frozen_version_digest == frozen_version_digest
        and population.population_id == registration.population_id
        and registered_ids == registration.registered_member_ids
        and required_gates == registration.required_gates
        and evidence.requalification_application_id == registration.application_id
        and evidence.requalification_population_id == registration.population_id
        and evidence.requalification_registration_digest == registration.digest
        and len(set(registered_ids)) == len(registered_ids)
        and len(set(member_ids)) == len(member_ids)
        and set(member_ids) == set(registered_ids)
        and len(set(required_gates)) == len(required_gates)
        and len(set(gate_ids)) == len(gate_ids)
        and set(gate_ids) == set(required_gates)
        and all(gate.passed for gate in population.gate_results)
        and all(
            member.prediction_frozen_at
            <= member.available_at
            <= member.matured_at
            <= evidence.evaluation_end
            and member.available_at <= evidence.available_at
            for member in population.members
        )
    )


def _requalification_registration_is_valid(
    registration: RequalificationPopulationRegistration,
    application_id: str,
    population_kind: str,
    registered_at: datetime,
    locked_at: datetime,
    frozen_version_digest: str,
) -> bool:
    return (
        registration.application_id == application_id
        and registration.population_kind == population_kind
        and registration.registered_at == registered_at
        and registration.locked_at == locked_at
        and registration.frozen_version_digest == frozen_version_digest
        and len(set(registration.registered_member_ids)) == len(registration.registered_member_ids)
        and len(set(registration.required_gates)) == len(registration.required_gates)
        and registration.digest == requalification_registration_digest(registration)
    )


def _restores_restriction(
    proof: QualificationEvidence,
    restriction: QualificationRestriction,
    previous: QualificationRecord,
    now: datetime,
) -> bool:
    if restriction.cause == "REQUIRED_PREMISE_UNVERIFIABLE":
        original = previous.authorization_evidence
        if (
            original is None
            or original.basis is None
            or not evidence_basis_is_valid(original)
            or proof.restored_authorization_digest != original.digest
        ):
            return False
        if proof.kind == "ORIGINAL_BASIS_RESTORED":
            return proof.basis == original.basis
        return proof.kind == "CERTIFIED_BASIS_SUBSTITUTION" and (
            _certified_substitution_is_valid(proof, original)
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
    if restriction.cause == "STATE_ACTIVITY_EXPIRED":
        basis = previous.formal_passing_evidence or previous.authorization_evidence
        original_check = basis.formal_check if basis is not None else None
        overall_deadline = overall_qualification_deadline(basis) if basis is not None else None
        restored_state_deadline = state_activity_deadline(proof)
        next_node = (
            _next_formal_node(previous, original_check) if original_check is not None else None
        )
        return (
            proof.kind == "STATE_ACTIVITY_RESTORED"
            and basis is not None
            and overall_deadline is not None
            and overall_deadline >= now
            and proof.state_activity_end is not None
            and proof.evaluation_end == proof.state_activity_end
            and restored_state_deadline is not None
            and restored_state_deadline >= now
            and (
                restriction.evidence.state_activity_end is None
                or proof.state_activity_end > restriction.evidence.state_activity_end
            )
            and (next_node is None or next_node > now)
        )
    return False


def _certified_substitution_is_valid(
    proof: QualificationEvidence,
    original: QualificationEvidence,
) -> bool:
    certification = proof.certified_substitution
    if certification is None or proof.basis is None:
        return False
    checks = certification.checks
    required = {"EQUIVALENCE", "MIGRATION", "CROSS_VALIDATION", "REPLAY"}
    return (
        certification.original_basis_digest == original.digest
        and certification.substitute_basis_digest == proof.digest
        and len(checks) == len(required)
        and {check.kind for check in checks} == required
        and len({check.check_id for check in checks}) == len(checks)
        and all(
            check.original_basis_digest == original.digest
            and check.substitute_basis_digest == proof.digest
            and check.available_at <= proof.available_at
            for check in checks
        )
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
        and outcome.qualification.scope.same_scope_as(scope)
        and outcome.qualification.version == version
    ]
    superseded = {record.previous_decision_id for record in records}
    heads = [record for record in records if record.decision_id not in superseded]
    if len(heads) > 1:
        raise ValueError("qualification history has conflicting revisions")
    return heads[0] if heads else None


def current_substantive_qualification(
    history: tuple[GovernanceOutcome, ...],
    scope: QualificationScope,
    version: CapabilityVersion,
) -> QualificationRecord | None:
    records = [
        outcome.qualification
        for outcome in history
        if outcome.qualification is not None
        and outcome.qualification.scope.same_scope_as(scope)
        and outcome.qualification.version.same_substantive_version_as(version)
    ]
    superseded = {record.previous_decision_id for record in records}
    heads = [record for record in records if record.decision_id not in superseded]
    if len(heads) > 1:
        raise ValueError("qualification history has conflicting substantive revisions")
    return heads[0] if heads else None
