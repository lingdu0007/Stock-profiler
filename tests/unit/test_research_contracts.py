from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

import pytest

from stock_profiler.modules.research.contracts import (
    RAW_SCORE_FEATURE_IDS,
    RESEARCH_ANNOUNCEMENT_TOOL_VERSION,
    RESEARCH_REQUIRED_DATA_TYPES,
    FrozenDualTargetScreening,
    RawScoreCalculationError,
    RawScoreFeatureTransform,
    ResearchCommand,
    ResearchDataManifest,
    ResearchDataManifestEntry,
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
    frozen_raw_score_model_snapshot,
    handoff_fingerprint,
    screening_output_sha256,
    selection_binding_sha256,
)
from stock_profiler.modules.research.service import freeze_research, validate_research_draft


def _member_input(index: int, cutoff: datetime) -> ResearchMemberInput:
    evidence = tuple(
        ResearchEvidence(
            evidence_id=f"{data_type.lower()}-evidence-{index:02}",
            source=f"fictional-{data_type.lower()}-feed",
            reference=f"synthetic://evidence/{data_type.lower()}/{index:02}",
            statement=(
                f"A fictional structured fact is available at the cutoff for {data_type.lower()}."
            ),
            acquired_at=cutoff,
            validated_at=cutoff,
            knowledge_cutoff=cutoff,
            semantic_version=f"fictional-{data_type.lower()}-feed-v1",
            validation_status="VALIDATED",
        )
        for data_type in RESEARCH_REQUIRED_DATA_TYPES
    )
    return ResearchMemberInput(
        security_id=f"synthetic-security-{index:02}",
        research_id=f"research-{index:02}",
        knowledge_cutoff=cutoff,
        evidence=evidence,
        data_manifest=ResearchDataManifest(
            version="synthetic-per-stock-research-manifest-v1",
            entries=tuple(
                ResearchDataManifestEntry(
                    data_type=data_type,
                    provider_id="synthetic-required-fact-provider",
                    provider_version="synthetic-required-fact-provider-v1",
                    completeness="COMPLETE",
                    event_status="PRESENT",
                    evidence_ids=(evidence[position].evidence_id,),
                    knowledge_cutoff=cutoff,
                )
                for position, data_type in enumerate(RESEARCH_REQUIRED_DATA_TYPES)
            ),
        ),
        structured_signals={
            signal_id: Decimal(index + 1) / Decimal(10) for signal_id in RAW_SCORE_FEATURE_IDS
        },
        risk_flags=("LIQUIDITY_WARNING",) if index == 0 else (),
    )


def _command(*, risk_scenario: Literal["ACCEPT", "REJECT"] = "ACCEPT") -> ResearchCommand:
    cutoff = datetime(2042, 5, 31, 23, 59, 59, tzinfo=UTC)
    members = tuple(_member_input(index, cutoff) for index in range(10))
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
        positive_percentiles={security_id: Decimal("60") for security_id in ids},
        terminal_percentiles={security_id: Decimal("70") for security_id in ids},
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
        raw_score_model=frozen_raw_score_model_snapshot(),
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
    assert raw_score.training_window_id == "synthetic-training-window-60m"
    assert raw_score.normalization_snapshot_id == "synthetic-normalization-v1"
    assert raw_score.interaction_terms == ()
    assert raw_score.z20 == Decimal("33.127")
    assert raw_score.transformed_inputs["working_capital_pressure_change"] == Decimal("-0.1")
    assert raw_score.feature_transformations["working_capital_pressure_change"].reverse is True


def test_raw_score_model_snapshot_requires_the_frozen_training_waterline() -> None:
    payload = _command().model_dump(mode="json")
    payload["raw_score_model"]["mature_months"] = 59

    command = ResearchCommand.model_validate(payload)
    with pytest.raises(RawScoreCalculationError, match="RAW_SCORE_MODEL_EVIDENCE_INSUFFICIENT"):
        freeze_raw_score(command, command.members[0])

    payload = _command().model_dump(mode="json")
    payload["raw_score_model"]["training_record_count"] = 499
    payload["raw_score_model"]["positive_record_count"] = 249

    command = ResearchCommand.model_validate(payload)
    with pytest.raises(RawScoreCalculationError, match="RAW_SCORE_MODEL_EVIDENCE_INSUFFICIENT"):
        freeze_raw_score(command, command.members[0])


def test_raw_score_snapshot_freezes_transformations_and_model_constraints() -> None:
    snapshot = frozen_raw_score_model_snapshot()

    assert set(snapshot.transformations) == set(RAW_SCORE_FEATURE_IDS)
    assert snapshot.transformations["working_capital_pressure_change"].reverse is True
    assert snapshot.transformations["turnover_change"].reverse is False
    assert snapshot.l1_ratio == Decimal("0.25")
    assert snapshot.l2_ratio == Decimal("0.75")

    payload = _command().model_dump(mode="json")
    payload["raw_score_model"]["l1_ratio"] = "1"
    payload["raw_score_model"]["l2_ratio"] = "0"
    with pytest.raises(ValueError, match="25% L1 and 75% L2"):
        ResearchCommand.model_validate(payload)

    payload = _command().model_dump(mode="json")
    payload["raw_score_model"]["coefficients"]["turnover_change"] = "-0.02"
    payload["raw_score_model"]["coefficients"]["institutional_listing_frequency"] = "-0.03"
    ResearchCommand.model_validate(payload)

    payload["raw_score_model"]["coefficients"]["institutional_net_buy_ratio"] = "-0.03"
    with pytest.raises(ValueError, match="non-negative"):
        ResearchCommand.model_validate(payload)


def test_raw_score_feature_transform_rejects_invalid_frozen_parameters() -> None:
    with pytest.raises(ValueError, match="clip bounds"):
        RawScoreFeatureTransform(
            lower_clip=Decimal("1"),
            upper_clip=Decimal("1"),
            median=Decimal("1"),
            iqr=Decimal("1"),
            reverse=False,
        )


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


def test_raw_score_uses_frozen_percentiles_not_screening_head_scores() -> None:
    command = _command()
    original = freeze_raw_score(command, command.members[0])
    changed_screening = command.screening.model_copy(
        update={
            "positive_scores": {
                security_id: Decimal("0.9")
                for security_id in command.screening.universe_security_ids
            },
            "terminal_scores": {
                security_id: Decimal("0.1")
                for security_id in command.screening.universe_security_ids
            },
        }
    )
    changed_screening = FrozenDualTargetScreening.model_validate(
        changed_screening.model_copy(
            update={"output_sha256": screening_output_sha256(changed_screening)}
        ).model_dump(mode="python")
    )
    changed_command = command.model_copy(
        update={
            "screening": changed_screening,
            "selection_fingerprint": selection_binding_sha256(
                command.selection_object_id,
                command.selection_event_id,
                command.cutoff_at,
                changed_screening,
            ),
        }
    )

    assert freeze_raw_score(changed_command, changed_command.members[0]) == original


def test_raw_score_changes_when_a_frozen_percentile_changes() -> None:
    command = _command()
    original = freeze_raw_score(command, command.members[0])
    changed_percentiles = command.screening.model_copy(
        update={
            "positive_percentiles": {
                **command.screening.positive_percentiles,
                command.members[0].security_id: Decimal("61"),
            }
        }
    )
    changed_percentiles = FrozenDualTargetScreening.model_validate(
        changed_percentiles.model_copy(
            update={"output_sha256": screening_output_sha256(changed_percentiles)}
        ).model_dump(mode="python")
    )
    changed_command = command.model_copy(
        update={
            "screening": changed_percentiles,
            "selection_fingerprint": selection_binding_sha256(
                command.selection_object_id,
                command.selection_event_id,
                command.cutoff_at,
                changed_percentiles,
            ),
        }
    )

    assert freeze_raw_score(changed_command, changed_command.members[0]).z20 == (
        original.z20 + Decimal("0.15")
    )


def test_screening_output_hash_rejects_a_changed_score() -> None:
    command = _command()
    payload = command.screening.model_dump(mode="json")
    payload["terminal_scores"]["synthetic-security-00"] = "0.8"

    with pytest.raises(ValueError, match="screening output hash"):
        FrozenDualTargetScreening.model_validate(payload)


def test_raw_score_arithmetic_failure_is_a_scoped_domain_failure() -> None:
    command = _command()
    coefficients = dict(command.raw_score_model.coefficients)
    coefficients["single_quarter_revenue_acceleration"] = Decimal("1e1000002")
    model = command.raw_score_model.model_copy(update={"coefficients": coefficients})
    command = command.model_copy(update={"raw_score_model": model})
    member = command.members[0].model_copy(
        update={
            "structured_signals": {signal_id: Decimal("0.1") for signal_id in RAW_SCORE_FEATURE_IDS}
        }
    )

    with pytest.raises(RawScoreCalculationError, match="RAW_SCORE_CALCULATION_FAILED"):
        freeze_raw_score(command, member)


def test_research_command_rejects_a_non_ten_or_incomplete_cohort() -> None:
    payload = _command().model_dump(mode="json")
    payload["members"] = payload["members"][:-1]

    with pytest.raises(ValueError, match="exactly ten"):
        ResearchCommand.model_validate(payload)


def test_research_command_preserves_unavailable_data_for_failure_handling() -> None:
    payload = _command().model_dump(mode="json")
    payload["members"][0]["data_manifest"]["entries"] = payload["members"][0]["data_manifest"][
        "entries"
    ][:-1]

    with pytest.raises(ValueError, match="required research data types"):
        ResearchCommand.model_validate(payload)

    payload = _command().model_dump(mode="json")
    payload["members"][0]["data_manifest"]["entries"][1]["completeness"] = "INCOMPLETE"
    payload["members"][0]["data_manifest"]["entries"][1]["event_status"] = "UNAVAILABLE"

    command = ResearchCommand.model_validate(payload)
    assert command.members[0].data_manifest.entries[1].completeness == "INCOMPLETE"


def test_research_command_allows_missing_signal_when_its_manifest_is_unavailable() -> None:
    payload = _command().model_dump(mode="json")
    payload["members"][0]["data_manifest"]["entries"][3]["completeness"] = "INCOMPLETE"
    payload["members"][0]["data_manifest"]["entries"][3]["event_status"] = "UNAVAILABLE"
    payload["members"][0]["structured_signals"]["operating_cash_flow_return_on_assets"] = None

    command = ResearchCommand.model_validate(payload)

    assert command.members[0].structured_signals["operating_cash_flow_return_on_assets"] is None
    with pytest.raises(RawScoreCalculationError, match="RESEARCH_DATA_UNAVAILABLE"):
        freeze_raw_score(command, command.members[0])


def test_verified_empty_institutional_activity_requires_zero_signals() -> None:
    payload = _command().model_dump(mode="json")
    payload["members"][0]["data_manifest"]["entries"][2]["event_status"] = "VERIFIED_EMPTY"

    with pytest.raises(ValueError, match="verified-empty institutional activity"):
        ResearchCommand.model_validate(payload)

    payload["members"][0]["structured_signals"]["institutional_net_buy_ratio"] = "0"
    payload["members"][0]["structured_signals"]["institutional_listing_frequency"] = "0"
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


def test_research_draft_rejects_provider_and_tool_evidence_identity_collision() -> None:
    payload = _command().model_dump(mode="json")
    colliding_id = "announcement:synthetic-security-00"
    payload["members"][0]["evidence"][0]["evidence_id"] = colliding_id
    payload["members"][0]["data_manifest"]["entries"][0]["evidence_ids"] = [colliding_id]
    command = ResearchCommand.model_validate(payload)
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
    tool_evidence = ResearchToolEvidence(
        evidence_id=colliding_id,
        source="fictional-announcement-feed",
        reference="synthetic://announcement/synthetic-security-00",
        statement="A fictional announcement is available at the cutoff.",
        acquired_at=command.knowledge_cutoff,
        validated_at=command.knowledge_cutoff,
        knowledge_cutoff=command.knowledge_cutoff,
        semantic_version=RESEARCH_ANNOUNCEMENT_TOOL_VERSION,
        validation_status="VALIDATED",
    )

    with pytest.raises(ValueError, match="collision between Provider and Tool"):
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

    contradictory_member = risk.member_vetoes[0].model_copy(update={"disposition": "ACCEPTED"})
    contradictory_risk = risk.model_copy(
        update={
            "member_vetoes": (contradictory_member, *risk.member_vetoes[1:]),
        }
    )
    with pytest.raises(ValueError, match="risk member disposition"):
        freeze_research(
            command,
            ResearchFrameworkOutput(
                research_run_id="research-run-1616",
                risk_run_id="risk-run-1616",
                draft=draft,
                risk_veto=contradictory_risk,
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
