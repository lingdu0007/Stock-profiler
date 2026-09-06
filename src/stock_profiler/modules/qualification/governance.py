"""Dispatch frozen host governance without exposing transport or persistence details."""

from stock_profiler.modules.qualification import service, versions
from stock_profiler.modules.qualification.contracts import (
    GovernanceCommand,
    GovernanceOutcome,
    PolicyBinding,
    QualificationCommand,
    RegisterTaskNodeCommand,
)


def adjudicate(
    command: GovernanceCommand,
    *,
    event_id: str,
    observed_at: str,
    knowledge_cutoff: str,
    history: tuple[GovernanceOutcome, ...],
    business_prerequisite_met: bool = True,
) -> GovernanceOutcome:
    conflict = not isinstance(
        command, RegisterTaskNodeCommand
    ) and service.policy_identity_conflicts(command.scope, command.version, history)
    binding = (
        PolicyBinding(scope=command.scope, version=command.version)
        if not isinstance(command, RegisterTaskNodeCommand)
        and command.version.policy_error is None
        and not conflict
        else None
    )
    if not business_prerequisite_met:
        return GovernanceOutcome(
            disposition="DENIED",
            reasons=("BUSINESS_PREREQUISITE_NOT_MET",),
            policy_binding=binding,
        )
    if conflict and isinstance(command, QualificationCommand):
        return GovernanceOutcome(
            disposition="DENIED", reasons=("QUALIFICATION_POLICY_IDENTITY_CONFLICT",)
        )
    if isinstance(command, QualificationCommand):
        outcome = service.adjudicate(
            command,
            event_id=event_id,
            observed_at=observed_at,
            knowledge_cutoff=knowledge_cutoff,
            history=history,
        )
    else:
        outcome = versions.adjudicate(
            command,
            event_id=event_id,
            observed_at=observed_at,
            knowledge_cutoff=knowledge_cutoff,
            history=history,
        )
    return outcome.model_copy(update={"policy_binding": binding})
