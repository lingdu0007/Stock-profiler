from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

import pytest

from stock_profiler.modules.research.contracts import (
    RAW_SCORE_FEATURE_IDS,
    RESEARCH_ANNOUNCEMENT_TOOL_VERSION,
    FrozenDualTargetScreening,
    RawScoreCalculationError,
    ResearchCommand,
    ResearchDraft,
    ResearchDraftMember,
    ResearchEvidence,
    ResearchFrameworkOutput,
    ResearchMemberHandoff,
    ResearchMemberInput,
    ResearchToolEvidence,
    RiskGate,
    RiskMemberVeto,
    RiskVetoDraft,
    freeze_raw_score,
    handoff_fingerprint,
    screening_output_sha256,
    selection_binding_sha256,
)
from stock_profiler.modules.research.service import freeze_research, validate_research_draft


def _command(*, risk_scenario: Literal["ACCEPT", "REJECT"] = "ACCEPT") -> ResearchCommand:
    cutoff = datetime(2042, 5, 31, 23, 59, 59, tzinfo=UTC)
    members = tuple(
        ResearchMemberInput(
            security_id=f"synthetic-security-{index:02}",
            research_id=f"research-{index:02}",
            knowledge_cutoff=cutoff,
            evidence=(
                ResearchEvidence(
                    evidence_id=f"evidence-{index:02}",
                    source="fictional-certified-feed",
                    reference=f"synthetic://evidence/{index:02}",
                    statement="A fictional structured fact is available at the cutoff.",
                    acquired_at=cutoff,
                    validated_at=cutoff,
                    knowledge_cutoff=cutoff,
                    semantic_version="fictional-certified-feed-v1",
                    validation_status="VALIDATED",
                ),
            ),
            structured_signals={
                signal_id: Decimal(index + 1) / Decimal(10) for signal_id in RAW_SCORE_FEATURE_IDS
            },
            risk_flags=("LIQUIDITY_WARNING",) if index == 0 else (),
        )
        for index in range(10)
    )
    ids = tuple(member.security_id for member in members)
    screening = FrozenDualTargetScreening.model_construct(
        positive_target="SIX_MONTH_POSITIVE_RETURN",
        terminal_target="SIX_MONTH_TERMINAL_20_PERCENT",
        strategy_version="synthetic-dual-head-strategy-v1",
        snapshot_id="synthetic-dual-head-snapshot-v1",
        universe_security_ids=ids,
        selected_member_ids=ids,
        positive_scores={security_id: Decimal("0.6") for security_id in ids},
        terminal_scores={security_id: Decimal("0.7") for security_id in ids},
        positive_head_version="synthetic-positive-head-v1",
        terminal_head_version="synthetic-terminal-head-v1",
        output_sha256="",
    )
    screening = FrozenDualTargetScreening.model_validate(
        screening.model_copy(
            update={"output_sha256": screening_output_sha256(screening)}
        ).model_dump(mode="python")
    )
    return ResearchCommand(
        contract_version="1.0.0",
        synthetic=True,
        generator_version="synthetic-research-v1",
        seed=1616,
        selection_object_id="selection-object-1616",
        selection_event_id="selection-event-1616",
        selection_fingerprint=selection_binding_sha256(
            "selection-object-1616",
            "selection-event-1616",
            cutoff,
            screening,
        ),
        cutoff_at=cutoff,
        knowledge_cutoff=cutoff,
        purpose="SYNTHETIC",
        screening=screening,
        members=members,
        risk_scenario=risk_scenario,
    )


def test_research_command_freezes_ten_independent_member_identities() -> None:
    command = _command()

    assert len(command.members) == 10
    assert len({member.research_id for member in command.members}) == 10
    assert command.screening.selected_member_ids == tuple(
        member.security_id for member in command.members
    )
    assert all(
        member.evidence[0].knowledge_cutoff == command.knowledge_cutoff
        for member in command.members
    )


def test_raw_score_is_a_structured_uncalibrated_z20_with_no_text_input() -> None:
    command = _command()
    raw_score = freeze_raw_score(command, command.members[0])

    assert raw_score.target == "SIX_MONTH_TERMINAL_20_PERCENT"
    assert raw_score.probability is None
    assert raw_score.model_version == "elastic-net-logistic-z20-v1"
    assert raw_score.interaction_terms == ()
    assert raw_score.z20 == Decimal("-0.038")


def test_raw_score_does_not_change_when_research_text_changes() -> None:
    command = _command()
    original = freeze_raw_score(command, command.members[0])
    changed_member = command.members[0].model_copy(
        update={
            "structured_signals": command.members[0].structured_signals,
            "risk_flags": (),
        }
    )

    assert freeze_raw_score(command, changed_member) == original


def test_screening_output_hash_rejects_a_changed_score() -> None:
    command = _command()
    payload = command.screening.model_dump(mode="json")
    payload["terminal_scores"]["synthetic-security-00"] = "0.8"

    with pytest.raises(ValueError, match="screening output hash"):
        FrozenDualTargetScreening.model_validate(payload)


def test_raw_score_arithmetic_failure_is_a_scoped_domain_failure() -> None:
    command = _command()
    member = command.members[0].model_copy(
        update={
            "structured_signals": {
                signal_id: Decimal("1e1000002") for signal_id in RAW_SCORE_FEATURE_IDS
            }
        }
    )

    with pytest.raises(RawScoreCalculationError, match="RAW_SCORE_CALCULATION_FAILED"):
        freeze_raw_score(command, member)


def test_research_command_rejects_a_non_ten_or_incomplete_cohort() -> None:
    payload = _command().model_dump(mode="json")
    payload["members"] = payload["members"][:-1]

    with pytest.raises(ValueError, match="exactly ten"):
        ResearchCommand.model_validate(payload)


def test_research_command_rejects_a_reordered_frozen_cohort() -> None:
    payload = _command().model_dump(mode="json")
    payload["members"] = list(reversed(payload["members"]))

    with pytest.raises(ValueError, match="preserve"):
        ResearchCommand.model_validate(payload)


def test_research_command_rejects_a_changed_selection_binding() -> None:
    payload = _command().model_dump(mode="json")
    payload["selection_event_id"] = "substituted-selection-event"

    with pytest.raises(ValueError, match="selection binding"):
        ResearchCommand.model_validate(payload)


def test_research_draft_can_cite_validated_tool_evidence() -> None:
    command = _command()
    tool_evidence = ResearchToolEvidence(
        evidence_id="announcement:synthetic-security-00",
        source="fictional-announcement-feed",
        reference="synthetic://announcement/00",
        statement="A fictional announcement is available at the cutoff.",
        acquired_at=command.knowledge_cutoff,
        validated_at=command.knowledge_cutoff,
        knowledge_cutoff=command.knowledge_cutoff,
        semantic_version=RESEARCH_ANNOUNCEMENT_TOOL_VERSION,
        validation_status="VALIDATED",
    )
    draft = ResearchDraft(
        contract_version="1.0.0",
        members=tuple(
            ResearchDraftMember(
                security_id=member.security_id,
                research_id=member.research_id,
                evidence_refs=(
                    tuple(evidence.evidence_id for evidence in member.evidence)
                    + (tool_evidence.evidence_id,)
                    if member is command.members[0]
                    else tuple(evidence.evidence_id for evidence in member.evidence)
                ),
                thesis="The fictional thesis is bounded by the frozen evidence.",
                bull_case="The fictional upside case remains conditional.",
                bear_case="The fictional downside case remains explicit.",
                knowledge_cutoff=member.knowledge_cutoff,
            )
            for member in command.members
        ),
    )

    validate_research_draft(command, draft, (tool_evidence,))


def test_research_freeze_binds_typed_draft_raw_scores_and_independent_risk() -> None:
    command = _command(risk_scenario="REJECT")
    raw_scores = tuple(freeze_raw_score(command, member) for member in command.members)
    draft = ResearchDraft(
        contract_version="1.0.0",
        members=tuple(
            ResearchDraftMember(
                security_id=member.security_id,
                research_id=member.research_id,
                evidence_refs=tuple(evidence.evidence_id for evidence in member.evidence),
                thesis="The fictional thesis is bounded by the frozen evidence.",
                bull_case="The fictional upside case remains conditional.",
                bear_case="The fictional downside case remains explicit.",
                knowledge_cutoff=member.knowledge_cutoff,
            )
            for member in command.members
        ),
    )
    risk = RiskVetoDraft(
        contract_version="1.0.0",
        handoff_fingerprint=handoff_fingerprint(
            command,
            draft,
            raw_scores=raw_scores,
            member_handoffs=tuple(
                ResearchMemberHandoff(
                    security_id=member.security_id,
                    research_id=member.research_id,
                    evidence=member.evidence,
                    risk_flags=member.risk_flags,
                )
                for member in command.members
            ),
        ),
        disposition="REJECTED",
        gates=(RiskGate(gate_id="SYNTHETIC_RISK_GATE", status="FAILED"),),
        reasons=("SYNTHETIC_RISK_VETO",),
        member_vetoes=tuple(
            RiskMemberVeto(
                security_id=member.security_id,
                research_id=member.research_id,
                disposition="REJECTED",
                gates=(RiskGate(gate_id="SYNTHETIC_RISK_GATE", status="FAILED"),),
                reasons=("SYNTHETIC_RISK_VETO",),
            )
            for member in command.members
        ),
    )

    outcome = freeze_research(
        command,
        ResearchFrameworkOutput(
            research_run_id="research-run-1616",
            risk_run_id="risk-run-1616",
            draft=draft,
            risk_veto=risk,
            raw_scores=raw_scores,
            member_handoffs=tuple(
                ResearchMemberHandoff(
                    security_id=member.security_id,
                    research_id=member.research_id,
                    evidence=member.evidence,
                    risk_flags=member.risk_flags,
                )
                for member in command.members
            ),
        ),
    )

    assert outcome.disposition == "REJECTED"
    assert outcome.risk_veto is not None
    assert outcome.risk_veto.disposition == "REJECTED"
    assert outcome.raw_scores is not None
    assert len(outcome.raw_scores) == 10
    assert outcome.handoff.research_run_id == "research-run-1616"
    assert outcome.handoff.risk_run_id == "risk-run-1616"
    assert outcome.handoff.actionable is False

    with pytest.raises(ValueError, match="order"):
        freeze_research(
            command,
            ResearchFrameworkOutput(
                research_run_id="research-run-1616",
                risk_run_id="risk-run-1616",
                draft=draft,
                risk_veto=risk,
                raw_scores=raw_scores,
                member_handoffs=tuple(
                    ResearchMemberHandoff(
                        security_id=member.security_id,
                        research_id=member.research_id,
                        evidence=member.evidence,
                        risk_flags=member.risk_flags,
                    )
                    for member in reversed(command.members)
                ),
            ),
        )


def test_research_freeze_honors_an_independent_rejection_without_command_override() -> None:
    command = _command(risk_scenario="ACCEPT")
    raw_scores = tuple(freeze_raw_score(command, member) for member in command.members)
    draft = ResearchDraft(
        contract_version="1.0.0",
        members=tuple(
            ResearchDraftMember(
                security_id=member.security_id,
                research_id=member.research_id,
                evidence_refs=tuple(evidence.evidence_id for evidence in member.evidence),
                thesis="The fictional thesis is bounded by the frozen evidence.",
                bull_case="The fictional upside case remains conditional.",
                bear_case="The fictional downside case remains explicit.",
                knowledge_cutoff=member.knowledge_cutoff,
            )
            for member in command.members
        ),
    )
    risk = RiskVetoDraft(
        contract_version="1.0.0",
        handoff_fingerprint=handoff_fingerprint(
            command,
            draft,
            raw_scores=raw_scores,
            member_handoffs=tuple(
                ResearchMemberHandoff(
                    security_id=member.security_id,
                    research_id=member.research_id,
                    evidence=member.evidence,
                    risk_flags=member.risk_flags,
                )
                for member in command.members
            ),
        ),
        disposition="REJECTED",
        gates=(RiskGate(gate_id="SYNTHETIC_RISK_GATE", status="FAILED"),),
        reasons=("INDEPENDENT_RISK_VETO",),
        member_vetoes=tuple(
            RiskMemberVeto(
                security_id=member.security_id,
                research_id=member.research_id,
                disposition="REJECTED",
                gates=(RiskGate(gate_id="SYNTHETIC_RISK_GATE", status="FAILED"),),
                reasons=("INDEPENDENT_RISK_VETO",),
            )
            for member in command.members
        ),
    )

    outcome = freeze_research(
        command,
        ResearchFrameworkOutput(
            research_run_id="research-run-independent-reject",
            risk_run_id="risk-run-independent-reject",
            draft=draft,
            risk_veto=risk,
            raw_scores=raw_scores,
            member_handoffs=tuple(
                ResearchMemberHandoff(
                    security_id=member.security_id,
                    research_id=member.research_id,
                    evidence=member.evidence,
                    risk_flags=member.risk_flags,
                )
                for member in command.members
            ),
        ),
    )

    assert outcome.disposition == "REJECTED"


def test_research_freeze_requires_the_raw_scores_seen_by_risk() -> None:
    command = _command()
    draft = ResearchDraft(
        contract_version="1.0.0",
        members=tuple(
            ResearchDraftMember(
                security_id=member.security_id,
                research_id=member.research_id,
                evidence_refs=tuple(evidence.evidence_id for evidence in member.evidence),
                thesis="The fictional thesis is bounded by the frozen evidence.",
                bull_case="The fictional upside case remains conditional.",
                bear_case="The fictional downside case remains explicit.",
                knowledge_cutoff=member.knowledge_cutoff,
            )
            for member in command.members
        ),
    )
    risk = RiskVetoDraft(
        contract_version="1.0.0",
        handoff_fingerprint=handoff_fingerprint(command, draft),
        disposition="ACCEPTED",
        gates=(RiskGate(gate_id="SYNTHETIC_RISK_GATE", status="PASSED"),),
        reasons=("SYNTHETIC_RISK_ACCEPTED",),
    )

    with pytest.raises(RawScoreCalculationError, match="RAW_SCORE_HANDOFF_MISSING"):
        freeze_research(
            command,
            ResearchFrameworkOutput(
                research_run_id="research-run-no-score",
                risk_run_id="risk-run-no-score",
                draft=draft,
                risk_veto=risk,
            ),
        )
