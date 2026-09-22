"""Host-owned deterministic research freeze and risk-veto handoff."""

from __future__ import annotations

from stock_profiler.modules.research.contracts import (
    RESEARCH_DEFINITION_ID,
    RESEARCH_DEFINITION_VERSION,
    RESEARCH_MODEL_ADAPTER_ID,
    RESEARCH_OUTPUT_CONTRACT_ID,
    RESEARCH_OUTPUT_CONTRACT_VERSION,
    RESEARCH_ROUTING_POLICY_VERSION,
    RESEARCH_SCOPE,
    RISK_DEFINITION_ID,
    RISK_DEFINITION_VERSION,
    RISK_MODEL_ADAPTER_ID,
    RISK_OUTPUT_CONTRACT_ID,
    RISK_OUTPUT_CONTRACT_VERSION,
    ResearchCommand,
    ResearchFrameworkOutput,
    ResearchHandoff,
    ResearchOutcome,
    RiskVetoOutcome,
    freeze_raw_score,
    handoff_fingerprint,
    research_member_results,
)


def freeze_research(
    command: ResearchCommand,
    framework: ResearchFrameworkOutput,
) -> ResearchOutcome:
    """Freeze research text, calculate structured z20 scores, and honor risk veto."""
    risk_veto_draft = framework.risk_veto
    risk_run_id = framework.risk_run_id
    if risk_veto_draft is None or risk_run_id is None:
        raise ValueError("independent risk Run did not produce a typed veto")
    _validate_draft_against_command(command, framework)
    expected_risk_disposition = "REJECTED" if command.risk_scenario == "REJECT" else "ACCEPTED"
    if risk_veto_draft.disposition != expected_risk_disposition:
        raise ValueError("independent risk Run disposition cannot be overridden")
    if risk_veto_draft.disposition == "REJECTED" and not any(
        gate.status == "FAILED" for gate in risk_veto_draft.gates
    ):
        raise ValueError("risk rejection requires a failed risk gate")
    if risk_veto_draft.disposition == "ACCEPTED" and any(
        gate.status == "FAILED" for gate in risk_veto_draft.gates
    ):
        raise ValueError("risk acceptance cannot contain a failed risk gate")

    raw_scores = tuple(freeze_raw_score(command, member) for member in command.members)
    risk_veto = RiskVetoOutcome(
        run_id=risk_run_id,
        definition_id=RISK_DEFINITION_ID,
        definition_version=RISK_DEFINITION_VERSION,
        disposition=risk_veto_draft.disposition,
        gates=risk_veto_draft.gates,
        reasons=risk_veto_draft.reasons,
    )
    evidence_ids = tuple(
        evidence.evidence_id for member in command.members for evidence in member.evidence
    )
    handoff = ResearchHandoff(
        contract_version="1.0.0",
        scope=RESEARCH_SCOPE,
        selection_object_id=command.selection_object_id,
        selection_event_id=command.selection_event_id,
        security_ids=tuple(member.security_id for member in command.members),
        targets=(
            command.screening.positive_target,
            command.screening.terminal_target,
        ),
        cutoff_at=command.cutoff_at,
        knowledge_cutoff=command.knowledge_cutoff,
        evidence_ids=evidence_ids,
        research_run_id=framework.research_run_id,
        risk_run_id=risk_run_id,
        research_definition_id=RESEARCH_DEFINITION_ID,
        research_definition_version=RESEARCH_DEFINITION_VERSION,
        risk_definition_id=RISK_DEFINITION_ID,
        risk_definition_version=RISK_DEFINITION_VERSION,
        research_model_adapter_id=RESEARCH_MODEL_ADAPTER_ID,
        risk_model_adapter_id=RISK_MODEL_ADAPTER_ID,
        research_routing_policy_version=RESEARCH_ROUTING_POLICY_VERSION,
        research_output_contract_id=RESEARCH_OUTPUT_CONTRACT_ID,
        research_output_contract_version=RESEARCH_OUTPUT_CONTRACT_VERSION,
        risk_output_contract_id=RISK_OUTPUT_CONTRACT_ID,
        risk_output_contract_version=RISK_OUTPUT_CONTRACT_VERSION,
        screening_strategy_version=command.screening.strategy_version,
        screening_snapshot_id=command.screening.snapshot_id,
        raw_scores=raw_scores,
        risk_veto=risk_veto,
    )
    return ResearchOutcome(
        disposition=(
            "REJECTED"
            if risk_veto_draft.disposition == "REJECTED"
            else "FROZEN"
        ),
        members=research_member_results(framework.draft),
        raw_scores=raw_scores,
        risk_veto=risk_veto,
        handoff=handoff,
        reasons=(
            "INDEPENDENT_RISK_VETO"
            if risk_veto_draft.disposition == "REJECTED"
            else "RESEARCH_AND_RISK_HANDOFF_FROZEN",
        ),
    )


def _validate_draft_against_command(
    command: ResearchCommand,
    framework: ResearchFrameworkOutput,
) -> None:
    """Reject a draft that loses evidence identity or invents a cohort member."""
    if framework.research_run_id == framework.risk_run_id:
        raise ValueError("research and risk Runs must have independent identities")
    if framework.risk_veto is None or framework.risk_run_id is None:
        raise ValueError("independent risk Run did not produce a typed veto")
    expected_fingerprint = handoff_fingerprint(command, framework.draft)
    if framework.risk_veto.handoff_fingerprint != expected_fingerprint:
        raise ValueError("risk Run must consume the immutable research handoff")
    expected = {member.security_id: member for member in command.members}
    actual = {member.security_id: member for member in framework.draft.members}
    if set(expected) != set(actual):
        raise ValueError("research draft must cover exactly the fixed-ten cohort")
    for security_id, source in expected.items():
        draft = actual[security_id]
        if (
            draft.research_id != source.research_id
            or draft.knowledge_cutoff != source.knowledge_cutoff
            or tuple(draft.evidence_refs) != tuple(
                evidence.evidence_id for evidence in source.evidence
            )
        ):
            raise ValueError("research draft lost immutable member provenance")
