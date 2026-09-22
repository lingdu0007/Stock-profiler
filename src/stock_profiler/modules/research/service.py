"""Host-owned deterministic research freeze and risk-veto handoff."""

from __future__ import annotations

import json
from dataclasses import dataclass

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
    RawScore,
    RawScoreCalculationError,
    ResearchCommand,
    ResearchDraft,
    ResearchFrameworkOutput,
    ResearchHandoff,
    ResearchOutcome,
    ResearchToolEvidence,
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
)


@dataclass(frozen=True)
class ResearchRiskPlan:
    """Pure, immutable input plan for the independent risk Definition."""

    raw_scores: tuple[RawScore, ...]
    tool_evidence_refs: tuple[str, ...]
    tool_evidence: tuple[ResearchToolEvidence, ...]
    risk_run_id: str
    handoff_fingerprint: str
    input_payload: str


def prepare_research_risk_plan(
    command: ResearchCommand,
    research_run_id: str,
    draft: ResearchDraft,
    tool_evidence: tuple[ResearchToolEvidence, ...],
) -> ResearchRiskPlan:
    """Calculate and bind the immutable inputs consumed by the risk Run."""
    raw_scores = tuple(freeze_raw_score(command, member) for member in command.members)
    tool_evidence_refs = tuple(evidence.evidence_id for evidence in tool_evidence)
    _validate_tool_evidence(command, tool_evidence_refs, tool_evidence)
    fingerprint = handoff_fingerprint(
        command,
        draft,
        raw_scores=raw_scores,
        tool_evidence_refs=tool_evidence_refs,
        tool_evidence=tool_evidence,
    )
    risk_run_id = risk_run_id_for(
        research_run_id,
        draft,
        raw_scores=raw_scores,
        tool_evidence_refs=tool_evidence_refs,
        tool_evidence=tool_evidence,
    )
    input_payload = json.dumps(
        {
            "handoff_fingerprint": fingerprint,
            "research_run_id": research_run_id,
            "draft": draft.model_dump(mode="json"),
            "raw_scores": tuple(score.model_dump(mode="json") for score in raw_scores),
            "tool_evidence_refs": tool_evidence_refs,
            "tool_evidence": tuple(evidence.model_dump(mode="json") for evidence in tool_evidence),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return ResearchRiskPlan(
        raw_scores=raw_scores,
        tool_evidence_refs=tool_evidence_refs,
        tool_evidence=tool_evidence,
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
    _validate_draft_against_command(command, framework)
    if risk_veto_draft.disposition == "REJECTED" and not any(
        gate.status == "FAILED" for gate in risk_veto_draft.gates
    ):
        raise ValueError("risk rejection requires a failed risk gate")
    if risk_veto_draft.disposition == "ACCEPTED" and any(
        gate.status == "FAILED" for gate in risk_veto_draft.gates
    ):
        raise ValueError("risk acceptance cannot contain a failed risk gate")

    raw_scores = framework.raw_scores
    risk_veto = RiskVetoOutcome(
        run_id=risk_run_id,
        definition_id=RISK_DEFINITION_ID,
        definition_version=RISK_DEFINITION_VERSION,
        disposition=risk_veto_draft.disposition,
        gates=risk_veto_draft.gates,
        reasons=risk_veto_draft.reasons,
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
        raw_scores=raw_scores,
        tool_evidence=tool_evidence,
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
    expected_fingerprint = handoff_fingerprint(
        command,
        framework.draft,
        raw_scores=framework.raw_scores,
        tool_evidence_refs=framework.tool_evidence_refs,
        tool_evidence=tool_evidence,
    )
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
            or tuple(draft.evidence_refs)
            != tuple(evidence.evidence_id for evidence in source.evidence)
        ):
            raise ValueError("research draft lost immutable member provenance")


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
