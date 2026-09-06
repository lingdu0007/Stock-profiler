"""Explicit forward-only version bindings for original synthetic task nodes."""

from datetime import datetime

from stock_profiler.modules.qualification.contracts import (
    ActivateVersionCommand,
    FreezeTaskCommand,
    FrozenTask,
    GovernanceOutcome,
    QualificationRecord,
    RegisteredTaskNode,
    RegisterTaskNodeCommand,
    TaskUsage,
    UseTaskCommand,
    VersionActivation,
)
from stock_profiler.modules.qualification.evidence import evidence_is_current
from stock_profiler.modules.qualification.service import (
    current_qualification,
    restrict_expired_qualification,
)


def adjudicate(
    command: RegisterTaskNodeCommand | ActivateVersionCommand | FreezeTaskCommand | UseTaskCommand,
    *,
    event_id: str,
    observed_at: str,
    knowledge_cutoff: str,
    history: tuple[GovernanceOutcome, ...],
) -> GovernanceOutcome:
    now = datetime.fromisoformat(observed_at)
    cutoff = datetime.fromisoformat(knowledge_cutoff)
    nodes = [
        item.task_node
        for item in history
        if item.task_node is not None and item.task_node.scope.same_scope_as(command.scope)
    ]
    tasks = [
        item.task
        for item in history
        if item.task is not None and item.task.scope.same_scope_as(command.scope)
    ]
    if isinstance(command, RegisterTaskNodeCommand):
        proposed = command.node
        if (
            proposed.scheduled_at <= now
            or not (proposed.knowledge_cutoff <= proposed.scheduled_at < proposed.valid_until)
            or any(
                item.node.node_id == proposed.node_id
                or item.node.task_identity == proposed.task_identity
                for item in nodes
            )
        ):
            return GovernanceOutcome(
                disposition="DENIED", reasons=("TASK_NODE_REGISTRATION_INVALID",)
            )
        return GovernanceOutcome(
            disposition="APPROVED",
            reasons=("TASK_NODE_REGISTERED",),
            task_node=RegisteredTaskNode(
                decision_id=event_id,
                scope=command.scope,
                node=proposed,
                registered_at=now,
            ),
        )
    qualification = current_qualification(history, command.scope, command.version)
    if isinstance(command, UseTaskCommand):
        task = next(
            (item for item in tasks if item.node.task_identity == command.task_identity), None
        )
        if task is None:
            return GovernanceOutcome(disposition="DENIED", reasons=("FROZEN_TASK_REQUIRED",))
        reasons: tuple[str, ...] = ()
        changed_qualification = None
        if task.version != command.version:
            reasons = ("ORIGINAL_TASK_VERSION_REQUIRED",)
        elif task.freeze_status != "APPROVED":
            reasons = ("ORIGINAL_TASK_WAS_NOT_APPROVED",)
        elif not (task.node.knowledge_cutoff <= cutoff <= now <= task.node.valid_until):
            reasons = ("ORIGINAL_TASK_WINDOW_CLOSED",)
        else:
            if qualification is not None:
                restricted = restrict_expired_qualification(qualification, now)
                if restricted != qualification:
                    changed_qualification = restricted.model_copy(
                        update={
                            "decision_id": event_id,
                            "previous_decision_id": qualification.decision_id,
                            "recorded_at": now,
                        }
                    )
                    qualification = changed_qualification
            if not _qualified(qualification, cutoff, now):
                reasons = ("CURRENT_SCOPED_QUALIFICATION_REQUIRED",)
        return GovernanceOutcome(
            disposition="DENIED" if reasons else "APPROVED",
            reasons=reasons or ("ORIGINAL_TASK_STATISTICAL_USE_ALLOWED",),
            qualification=changed_qualification,
            usage=TaskUsage(
                allowed=not reasons,
                task_snapshot=task,
                qualification_snapshot=qualification,
                reasons=reasons,
                checked_at=now,
            ),
        )
    activations = [
        item.activation
        for item in history
        if item.activation is not None and item.activation.scope.same_scope_as(command.scope)
    ]
    if isinstance(command, ActivateVersionCommand):
        current = _current_activation(activations)
        if (
            command.previous_activation_id != (current.decision_id if current else None)
            or command.previous_version != (current.version if current else None)
            or not _qualified(qualification, cutoff, now)
            or qualification is None
            or command.qualification_decision_id != qualification.decision_id
        ):
            return GovernanceOutcome(
                disposition="DENIED", reasons=("VERSION_HANDOFF_PREREQUISITES_NOT_MET",)
            )
        requested = next(
            (item.node for item in nodes if item.node.node_id == command.first_node_id), None
        )
        if requested is None:
            return GovernanceOutcome(
                disposition="DENIED", reasons=("REGISTERED_TASK_NODE_REQUIRED",)
            )
        frozen_nodes = {item.node.node_id for item in tasks}
        candidates = sorted(
            (
                item.node
                for item in nodes
                if item.node.kind == requested.kind
                and item.node.scheduled_at >= requested.scheduled_at
                and item.node.scheduled_at > now
                and item.node.node_id not in frozen_nodes
            ),
            key=lambda item: item.scheduled_at,
        )
        if not candidates or (
            current is not None
            and (
                candidates[0].scheduled_at <= current.first_node.scheduled_at
                or command.version == current.version
            )
        ):
            return GovernanceOutcome(
                disposition="DENIED", reasons=("NEXT_LEGAL_TASK_NODE_REQUIRED",)
            )
        return GovernanceOutcome(
            disposition="APPROVED",
            reasons=("VERSION_HANDOFF_RECORDED",),
            activation=VersionActivation(
                decision_id=event_id,
                scope=command.scope,
                version=command.version,
                previous_version=command.previous_version,
                previous_activation_id=command.previous_activation_id,
                qualification_snapshot=qualification,
                first_node=candidates[0],
                retained_task_ids=tuple(sorted(item.node.task_identity for item in tasks)),
                recorded_at=now,
            ),
        )
    node = next((item.node for item in nodes if item.node.node_id == command.node_id), None)
    if (
        node is None
        or any(item.node.node_id == command.node_id for item in tasks)
        or node.knowledge_cutoff != cutoff
        or not (node.scheduled_at <= now <= node.valid_until)
    ):
        return GovernanceOutcome(disposition="DENIED", reasons=("TASK_FREEZE_NOT_ALLOWED",))
    applicable = [
        item
        for item in activations
        if item.first_node.kind == node.kind and item.first_node.scheduled_at <= node.scheduled_at
    ]
    active = max(applicable, key=lambda item: item.first_node.scheduled_at) if applicable else None
    reasons = ()
    if (
        active is None
        or active.decision_id != command.activation_id
        or active.version != command.version
    ):
        reasons = ("EXPLICIT_VERSION_ACTIVATION_REQUIRED",)
    elif not _qualified(qualification, cutoff, now):
        reasons = ("CURRENT_SCOPED_QUALIFICATION_REQUIRED",)
    return GovernanceOutcome(
        disposition="DENIED" if reasons else "APPROVED",
        reasons=reasons or ("TASK_VERSION_FROZEN",),
        task=FrozenTask(
            decision_id=event_id,
            scope=command.scope,
            version=command.version,
            node=node,
            activation_id=command.activation_id,
            qualification_snapshot=qualification,
            freeze_status="DENIED" if reasons else "APPROVED",
            reasons=reasons,
            frozen_at=now,
        ),
    )


def _qualified(record: QualificationRecord | None, cutoff: datetime, now: datetime) -> bool:
    if record is None or record.status not in {"VALID", "AT_RISK"}:
        return False
    basis = record.formal_passing_evidence or record.authorization_evidence
    return (
        record.authorization_id is not None
        and basis is not None
        and evidence_is_current(basis, now)
        and record.evidence_available_by(min(cutoff, now))
    )


def _current_activation(activations: list[VersionActivation]) -> VersionActivation | None:
    superseded = {item.previous_activation_id for item in activations}
    heads = [item for item in activations if item.decision_id not in superseded]
    if len(heads) > 1:
        raise ValueError("version handoff history has conflicting revisions")
    return heads[0] if heads else None
