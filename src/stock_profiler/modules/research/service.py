"""Host-owned deterministic research freeze and risk-veto handoff."""

from __future__ import annotations

import json

from stock_profiler.modules.research.contracts import (
    RESEARCH_ANNOUNCEMENT_TOOL_VERSION,
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
    RawScoreCalculationError,
    ResearchCommand,
    ResearchDraft,
    ResearchFrameworkOutput,
    ResearchHandoff,
    ResearchMemberHandoff,
    ResearchOutcome,
    ResearchRiskPlan,
    ResearchToolEvidence,
    RiskMemberVeto,
    RiskVetoOutcome,
    freeze_raw_score,
    handoff_fingerprint,
    research_member_results,
    risk_run_id_for,
)

__all__ = (
    "ResearchRiskPlan",
    "freeze_raw_score",
    "freeze_research",
    "prepare_research_risk_plan",
    "validate_research_draft",
)


def prepare_research_risk_plan(
    command: ResearchCommand,
    research_run_id: str,
    draft: ResearchDraft,
    tool_evidence: tuple[ResearchToolEvidence, ...],
) -> ResearchRiskPlan:
    """Calculate and bind the immutable inputs consumed by the risk Run."""
    tool_evidence_refs = tuple(evidence.evidence_id for evidence in tool_evidence)
    validate_research_draft(command, draft, tool_evidence)
    raw_scores = tuple(freeze_raw_score(command, member) for member in command.members)
    member_handoffs = _member_handoffs_for_command(command)
    fingerprint = handoff_fingerprint(
        command,
        draft,
        raw_scores=raw_scores,
        tool_evidence_refs=tool_evidence_refs,
        tool_evidence=tool_evidence,
        member_handoffs=member_handoffs,
    )
    risk_run_id = risk_run_id_for(
        research_run_id,
        draft,
        raw_scores=raw_scores,
        tool_evidence_refs=tool_evidence_refs,
        tool_evidence=tool_evidence,
        member_handoffs=member_handoffs,
    )
    input_payload = json.dumps(
        {
            "handoff_fingerprint": fingerprint,
            "research_run_id": research_run_id,
            "draft": draft.model_dump(mode="json"),
            "raw_scores": tuple(score.model_dump(mode="json") for score in raw_scores),
            "tool_evidence_refs": tool_evidence_refs,
            "tool_evidence": tuple(evidence.model_dump(mode="json") for evidence in tool_evidence),
            "member_handoffs": tuple(member.model_dump(mode="json") for member in member_handoffs),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return ResearchRiskPlan(
        raw_scores=raw_scores,
        tool_evidence_refs=tool_evidence_refs,
        tool_evidence=tool_evidence,
        member_handoffs=member_handoffs,
        risk_run_id=risk_run_id,
        handoff_fingerprint=fingerprint,
        input_payload=input_payload,
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
    if framework.raw_scores is None:
        raise RawScoreCalculationError("RAW_SCORE_HANDOFF_MISSING")
    expected_raw_scores = tuple(freeze_raw_score(command, member) for member in command.members)
    if framework.raw_scores != expected_raw_scores:
        raise RawScoreCalculationError("RAW_SCORE_INPUT_MISMATCH")
    tool_evidence = _validate_tool_evidence(
        command,
        framework.tool_evidence_refs,
        framework.tool_evidence,
    )
    member_handoffs = _validate_member_handoffs(
        command,
        framework.member_handoffs,
    )
    _validate_draft_against_command(command, framework)
    if risk_veto_draft.disposition == "REJECTED" and not any(
        gate.status == "FAILED" for gate in risk_veto_draft.gates
    ):
        raise ValueError("risk rejection requires a failed risk gate")
    if risk_veto_draft.disposition == "ACCEPTED" and any(
        gate.status == "FAILED" for gate in risk_veto_draft.gates
    ):
        raise ValueError("risk acceptance cannot contain a failed risk gate")
    member_vetoes = _validate_risk_member_vetoes(command, risk_veto_draft.member_vetoes)

    raw_scores = framework.raw_scores
    risk_veto = RiskVetoOutcome(
        run_id=risk_run_id,
        definition_id=RISK_DEFINITION_ID,
        definition_version=RISK_DEFINITION_VERSION,
        disposition=risk_veto_draft.disposition,
        gates=risk_veto_draft.gates,
        reasons=risk_veto_draft.reasons,
        member_vetoes=member_vetoes,
    )
    base_evidence_ids = tuple(
        evidence.evidence_id for member in command.members for evidence in member.evidence
    )
    tool_evidence_refs = tuple(evidence.evidence_id for evidence in tool_evidence)
    evidence_ids = (*base_evidence_ids, *tool_evidence_refs)
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
        selection_fingerprint=command.selection_fingerprint,
        raw_scores=raw_scores,
        tool_evidence=tool_evidence,
        member_handoffs=member_handoffs,
        risk_veto=risk_veto,
    )
    return ResearchOutcome(
        disposition=("REJECTED" if risk_veto_draft.disposition == "REJECTED" else "FROZEN"),
        members=research_member_results(framework.draft),
        raw_scores=raw_scores,
        risk_veto=risk_veto,
        tool_evidence=tool_evidence,
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
    if framework.raw_scores is None:
        raise RawScoreCalculationError("RAW_SCORE_HANDOFF_MISSING")
    tool_evidence = _validate_tool_evidence(
        command,
        framework.tool_evidence_refs,
        framework.tool_evidence,
    )
    validate_research_draft(command, framework.draft, tool_evidence)
    member_handoffs = _validate_member_handoffs(command, framework.member_handoffs)
    expected_fingerprint = handoff_fingerprint(
        command,
        framework.draft,
        raw_scores=framework.raw_scores,
        tool_evidence_refs=framework.tool_evidence_refs,
        tool_evidence=tool_evidence,
        member_handoffs=member_handoffs,
    )
    if framework.risk_veto.handoff_fingerprint != expected_fingerprint:
        raise ValueError("risk Run must consume the immutable research handoff")
    return None


def validate_research_draft(
    command: ResearchCommand,
    draft: ResearchDraft,
    tool_evidence: tuple[ResearchToolEvidence, ...],
) -> None:
    """Validate research provenance before raw-score or risk work can begin."""
    tool_evidence_refs = tuple(evidence.evidence_id for evidence in tool_evidence)
    _validate_tool_evidence(command, tool_evidence_refs, tool_evidence)
    expected = tuple((member.security_id, member.research_id) for member in command.members)
    actual = tuple((member.security_id, member.research_id) for member in draft.members)
    if actual != expected:
        raise ValueError(
            "research draft must preserve the frozen selected cohort identities and order"
        )
    tool_refs = set(tool_evidence_refs)
    for source, candidate in zip(command.members, draft.members, strict=True):
        required_refs = tuple(evidence.evidence_id for evidence in source.evidence)
        candidate_refs = tuple(candidate.evidence_refs)
        if len(set(candidate_refs)) != len(candidate_refs):
            raise ValueError("research draft evidence references must be unique")
        if tuple(ref for ref in candidate_refs if ref in set(required_refs)) != required_refs:
            raise ValueError("research draft lost immutable member provenance")
        if not set(candidate_refs).issubset(set(required_refs) | tool_refs):
            raise ValueError("research draft cited unavailable evidence")
        if candidate.knowledge_cutoff != source.knowledge_cutoff:
            raise ValueError("research draft changed the frozen knowledge cutoff")


def _validate_tool_evidence(
    command: ResearchCommand,
    tool_evidence_refs: tuple[str, ...],
    tool_evidence: tuple[ResearchToolEvidence, ...],
) -> tuple[ResearchToolEvidence, ...]:
    """Bind every exploratory Tool claim to its cutoff and semantic version."""
    evidence = tuple(tool_evidence)
    evidence_ids = tuple(item.evidence_id for item in evidence)
    if evidence_ids != tuple(tool_evidence_refs):
        raise ValueError("research Tool evidence references do not match structured evidence")
    if len(set(evidence_ids)) != len(evidence_ids):
        raise ValueError("research Tool evidence identities must be unique")
    provider_evidence_ids = {
        item.evidence_id for member in command.members for item in member.evidence
    }
    if provider_evidence_ids.intersection(evidence_ids):
        raise ValueError("research evidence identity collision between Provider and Tool")
    for item in evidence:
        if (
            item.semantic_version != RESEARCH_ANNOUNCEMENT_TOOL_VERSION
            or item.acquired_at != command.knowledge_cutoff
            or item.validated_at != command.knowledge_cutoff
            or item.knowledge_cutoff != command.knowledge_cutoff
            or item.validation_status != "VALIDATED"
        ):
            raise ValueError("research Tool evidence is not bound to the frozen cutoff")
    return evidence


def _member_handoffs_for_command(command: ResearchCommand) -> tuple[ResearchMemberHandoff, ...]:
    return tuple(
        ResearchMemberHandoff(
            security_id=member.security_id,
            research_id=member.research_id,
            evidence=member.evidence,
            risk_flags=member.risk_flags,
        )
        for member in command.members
    )


def _validate_member_handoffs(
    command: ResearchCommand,
    member_handoffs: tuple[ResearchMemberHandoff, ...],
) -> tuple[ResearchMemberHandoff, ...]:
    handoffs = tuple(member_handoffs)
    if len(handoffs) != len(command.members):
        raise ValueError("research risk handoff must contain every member's evidence")
    expected = tuple(
        (member.security_id, member.research_id, member.evidence, member.risk_flags)
        for member in command.members
    )
    actual = tuple(
        (member.security_id, member.research_id, member.evidence, member.risk_flags)
        for member in handoffs
    )
    if actual != expected:
        raise ValueError("research risk handoff changed member order, evidence, or risk flags")
    return handoffs


def _validate_risk_member_vetoes(
    command: ResearchCommand,
    member_vetoes: tuple[RiskMemberVeto, ...],
) -> tuple[RiskMemberVeto, ...]:
    vetoes = tuple(member_vetoes)
    if len(vetoes) != len(command.members):
        raise ValueError("risk veto must contain a decision for every research member")
    expected = tuple((member.security_id, member.research_id) for member in command.members)
    actual = tuple((member.security_id, member.research_id) for member in vetoes)
    if actual != expected:
        raise ValueError("risk veto member identities or order do not match the research cohort")
    for veto in vetoes:
        expected_disposition = (
            "REJECTED" if any(gate.status == "FAILED" for gate in veto.gates) else "ACCEPTED"
        )
        if veto.disposition != expected_disposition:
            raise ValueError("risk member disposition must match its gates")
    return vetoes
