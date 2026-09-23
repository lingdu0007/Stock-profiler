from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, cast

import pytest
from m_agent.adapters import DeterministicModelAdapter, InMemoryRunStore, PlaintextPayloadCodec
from m_agent.runtime import (
    AgentDefinition,
    ContextItem,
    DefinitionRegistry,
    ModelRequest,
    PolicyAction,
    PolicyGate,
    Runner,
    StepType,
    StructuredOutputMode,
    ToolRequest,
    parse_stage_result,
)

import stock_profiler.adapters.m_agent.frozen_decision_case as frozen_decision_case
import stock_profiler.bootstrap.decision_cases as case_bootstrap
from stock_profiler.adapters.m_agent.frozen_decision_case import (
    RESEARCH_DEFINITION_INSTRUCTIONS,
    _read_announcement_tool,
    execute_research_decision_case,
)
from stock_profiler.adapters.persistence.decision_ledger import (
    DECISION_STAGE_EVENTS,
    DecisionLedger,
)
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.foundation.decision_versions import (
    CURRENT_M_AGENT_RELEASE,
    HISTORICAL_M_AGENT_RELEASE,
    DecisionCaseVersionBundle,
)
from stock_profiler.modules.candidate_selection.selection import (
    ScreeningRank,
    ScreeningRow,
    SelectionCommand,
    SelectionOutcome,
    SelectionPolicy,
    SelectionPopulation,
)
from stock_profiler.modules.decision_cases import service as decision_case_service
from stock_profiler.modules.decision_cases.domain import (
    FROZEN_AGENT_DEFINITION_ID,
    EvidenceClock,
    ExternalResult,
    FrozenAgentDefinition,
    FrozenDecisionCase,
    FrozenOutputContract,
    ResultAccessScope,
)
from stock_profiler.modules.decision_cases.ports import (
    DecisionEventCommitError,
    FrameworkRunResult,
    MappedDurableRunMissingError,
)
from stock_profiler.modules.decision_cases.service import execute_research_risk_journey
from stock_profiler.modules.research import service as research_service
from stock_profiler.modules.research.contracts import (
    RAW_SCORE_FEATURE_IDS,
    RESEARCH_ANNOUNCEMENT_TOOL_VERSION,
    RESEARCH_DEFINITION_ID,
    RESEARCH_MODEL_ADAPTER_ID,
    RESEARCH_OUTPUT_CONTRACT_ID,
    RESEARCH_OUTPUT_CONTRACT_VERSION,
    RESEARCH_REQUIRED_DATA_TYPES,
    RESEARCH_ROUTING_POLICY_VERSION,
    RISK_DEFINITION_ID,
    RISK_DEFINITION_VERSION,
    FrozenDualTargetScreening,
    RawScoreCalculationError,
    ResearchCommand,
    ResearchDataManifest,
    ResearchDataManifestEntry,
    ResearchDraft,
    ResearchDraftMember,
    ResearchEvidence,
    ResearchFrameworkOutput,
    ResearchMemberHandoff,
    ResearchMemberInput,
    ResearchRiskPlan,
    ResearchStageArtifact,
    ResearchToolEvidence,
    RiskGate,
    RiskMemberVeto,
    RiskVetoDraft,
    freeze_raw_score,
    frozen_raw_score_model_snapshot,
    handoff_fingerprint,
    risk_run_id_for,
    screening_output_sha256,
    selection_binding_sha256,
)
from stock_profiler.modules.research.service import freeze_research


def research_command(
    *,
    risk_scenario: Literal["ACCEPT", "REJECT"] = "REJECT",
    failure_mode: Literal["NONE", "DATA", "RESEARCH", "RAW_SCORE", "RISK", "SYSTEM"] = "NONE",
) -> ResearchCommand:
    cutoff = datetime(2042, 5, 31, 23, 59, 59, tzinfo=UTC)
    members = tuple(
        ResearchMemberInput(
            security_id=f"synthetic-security-{index:02}",
            research_id=f"research-{index:02}",
            knowledge_cutoff=cutoff,
            evidence=tuple(
                ResearchEvidence(
                    evidence_id=f"{data_type.lower()}-evidence-{index:02}",
                    source=f"fictional-{data_type.lower()}-feed",
                    reference=f"synthetic://evidence/{data_type.lower()}/{index:02}",
                    statement=(
                        "A fictional structured fact is available at the cutoff "
                        f"for {data_type.lower()}."
                    ),
                    acquired_at=cutoff,
                    validated_at=cutoff,
                    knowledge_cutoff=cutoff,
                    semantic_version=f"fictional-{data_type.lower()}-feed-v1",
                    validation_status="VALIDATED",
                )
                for data_type in RESEARCH_REQUIRED_DATA_TYPES
            ),
            data_manifest=ResearchDataManifest(
                version="synthetic-per-stock-research-manifest-v1",
                entries=tuple(
                    ResearchDataManifestEntry(
                        data_type=data_type,
                        provider_id="synthetic-required-fact-provider",
                        provider_version="synthetic-required-fact-provider-v1",
                        completeness="COMPLETE",
                        event_status="PRESENT",
                        evidence_ids=(f"{data_type.lower()}-evidence-{index:02}",),
                        knowledge_cutoff=cutoff,
                    )
                    for data_type in RESEARCH_REQUIRED_DATA_TYPES
                ),
            ),
            structured_signals={
                signal_id: Decimal(index + 1) / Decimal(100) for signal_id in RAW_SCORE_FEATURE_IDS
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
        failure_mode=failure_mode,
    )


def _selection_anchor_case(
    settings: Settings,
    command: ResearchCommand,
    scope: ResultAccessScope,
) -> tuple[FrozenDecisionCase, SelectionOutcome]:
    """Create a committed synthetic selection fact for the research handoff."""
    selection = SelectionCommand(
        contract_version="1.0.0",
        synthetic=True,
        generator_version="synthetic-selection-anchor-v1",
        seed=1515,
        cutoff_at=command.cutoff_at,
        purpose="SYNTHETIC",
        universe_object_id="synthetic-universe-object-1515",
        universe_event_id="synthetic-universe-event-1515",
        policy=SelectionPolicy(
            version_id="synthetic-selection-policy-v1",
            cohort_size=10,
            industry_limit=2,
            capitalization_limit=4,
            correlation_sessions=2,
            maximum_correlation=Decimal("0.8"),
            positive_weight=8,
            terminal_weight=5,
        ),
        industry_version="synthetic-industry-v1",
        adjustment_version="synthetic-adjustment-v1",
        screening_snapshot_id=command.screening.snapshot_id,
        strategy_version=command.screening.strategy_version,
        rows=tuple(
            ScreeningRow(
                security_id=security_id,
                industry=f"synthetic-industry-{index % 5}",
                float_capitalization=Decimal(1000 + index),
                positive_score=command.screening.positive_scores[security_id],
                terminal_score=command.screening.terminal_scores[security_id],
                adjusted_returns=(Decimal("0.01"), Decimal("0.02")),
                return_dates=(command.cutoff_at.date(),),
            )
            for index, security_id in enumerate(command.screening.universe_security_ids)
        ),
    )
    selection_outcome = SelectionOutcome(
        disposition="FROZEN",
        cutoff_at=command.cutoff_at,
        universe_event_id=selection.universe_event_id,
        policy=selection.policy,
        members=command.screening.selected_member_ids,
        ranking=tuple(
            ScreeningRank(
                security_id=security_id,
                rank=index + 1,
                positive_percentile=command.screening.positive_percentiles[security_id],
                terminal_percentile=command.screening.terminal_percentiles[security_id],
                composite_score=Decimal("63.8461538461538461538461538462"),
            )
            for index, security_id in enumerate(command.screening.selected_member_ids)
        ),
        scan=(),
        population=SelectionPopulation(
            valid_monthly=True,
            recommendation_coverage_denominator=True,
            selection_pass_denominator=True,
            selection_pass=False,
        ),
        reasons=("SELECTION_FROZEN",),
    )
    definition = FrozenAgentDefinition(
        definition_id=FROZEN_AGENT_DEFINITION_ID,
        version="2.0.0",
        instructions="Return only the frozen synthetic selection result as JSON.",
        model_adapter_id="m-agent-deterministic-model-adapter",
        output_contract=FrozenOutputContract(
            contract_id="synthetic-decision-case-output",
            version="1.0.0",
            schema={"type": "object"},
        ),
    )
    bundle = DecisionCaseVersionBundle(
        case_contract_version="selection.1.0.0",
        host_contract_version="selection.1.0.0",
        host_application_version=settings.configuration_version,
        host_source_sha=settings.source_sha,
        agent_definition_id=FROZEN_AGENT_DEFINITION_ID,
        agent_definition_version="2.0.0",
        model_adapter_id="m-agent-deterministic-model-adapter",
        routing_policy_version="d0-single-definition-route-v1",
        output_contract_version="1.0.0",
        report_projection_contract_version="selection.1.0.0",
        **CURRENT_M_AGENT_RELEASE.model_dump(),
    )
    anchor_case = FrozenDecisionCase(
        synthetic=True,
        generator_version="synthetic-selection-anchor-case-v1",
        seed=1515,
        case_id="synthetic-selection-anchor-case-1515",
        business_identity="synthetic-selection-anchor-1515",
        knowledge_cutoff=command.knowledge_cutoff.isoformat(),
        report_generated_at=command.cutoff_at.isoformat(),
        evidence_clock=EvidenceClock(
            fact_effective_at=command.cutoff_at.isoformat(),
            source_published_at=command.cutoff_at.isoformat(),
            acquired_at=command.cutoff_at.isoformat(),
            validated_at=command.cutoff_at.isoformat(),
        ),
        qualification_scope="D0_SYNTHETIC_CONTRACT_ONLY",
        version_bundle=bundle,
        agent_definition=definition,
        input={
            "account": {
                "account_id": scope.account_ids[0],
                "account_kind": "SIMULATED_CASH",
            },
            "selection": selection.model_dump(mode="json"),
        },
        expected_external_result=ExternalResult(
            outcome_code="SYNTHETIC_REVIEW_COMPLETE",
            summary="Synthetic selection anchor.",
            key_reasons=("SELECTION_FROZEN",),
        ),
        selection=selection,
        access_scope=scope,
    )
    return anchor_case, selection_outcome


def _seed_selection_event(
    settings: Settings,
    anchor_case: FrozenDecisionCase,
    selection_event_id: str,
    selection_outcome: SelectionOutcome,
) -> None:
    """Persist the upstream selection event before executing research."""
    runtime = initialize_runtime_storage(settings)
    ledger = DecisionLedger(runtime.engine)
    ledger.persist_business_mapping_before_framework(anchor_case)
    with ledger.serialize_case_execution() as connection:
        ledger.commit_event(
            connection,
            case=anchor_case,
            framework_run_id=anchor_case.framework_run_id,
            result=ExternalResult(
                outcome_code="SELECTION_FROZEN",
                summary="Synthetic selection anchor.",
                key_reasons=("SELECTION_FROZEN",),
                selection=selection_outcome,
            ),
            stage_results=(),
            decision_event_id=selection_event_id,
        )


def _draft(command: ResearchCommand) -> ResearchDraft:
    return ResearchDraft(
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


def _case(
    settings: Settings,
    *,
    risk_scenario: Literal["ACCEPT", "REJECT"] = "REJECT",
    failure_mode: Literal["NONE", "DATA", "RESEARCH", "RAW_SCORE", "RISK", "SYSTEM"] = "NONE",
    user_id: str = "synthetic-user-1616",
) -> FrozenDecisionCase:
    command = research_command(risk_scenario=risk_scenario, failure_mode=failure_mode)
    scope = ResultAccessScope(
        contract_version="1.0.0",
        user_id=user_id,
        account_ids=("synthetic-account-4017",),
        visibility="USER",
    )
    selection_anchor, selection_outcome = _selection_anchor_case(settings, command, scope)
    command = command.model_copy(
        update={
            "selection_object_id": selection_anchor.business_object_id,
            "selection_event_id": selection_anchor.decision_event_id,
            "selection_fingerprint": selection_binding_sha256(
                selection_anchor.business_object_id,
                selection_anchor.decision_event_id,
                command.cutoff_at,
                command.screening,
            ),
        }
    )
    _seed_selection_event(settings, selection_anchor, command.selection_event_id, selection_outcome)
    draft = _draft(command)
    tool_evidence_refs = ("announcement:synthetic-security-00",)
    tool_evidence = tuple(
        ResearchToolEvidence(
            evidence_id=evidence_id,
            source="synthetic-announcement-feed",
            reference=f"synthetic://announcement/{evidence_id.removeprefix('announcement:')}",
            statement=(
                f"Synthetic announcement evidence {evidence_id} is read-only "
                "and contains no trade instruction."
            ),
            acquired_at=command.knowledge_cutoff,
            validated_at=command.knowledge_cutoff,
            knowledge_cutoff=command.knowledge_cutoff,
            semantic_version=RESEARCH_ANNOUNCEMENT_TOOL_VERSION,
            validation_status="VALIDATED",
        )
        for evidence_id in tool_evidence_refs
    )
    raw_scores = tuple(freeze_raw_score(command, member) for member in command.members)
    provisional_risk = RiskVetoDraft(
        contract_version="1.0.0",
        handoff_fingerprint=handoff_fingerprint(
            command,
            draft,
            raw_scores=raw_scores,
            tool_evidence_refs=tool_evidence_refs,
            tool_evidence=tool_evidence,
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
        disposition="REJECTED" if risk_scenario == "REJECT" else "ACCEPTED",
        gates=(
            RiskGate(
                gate_id="SYNTHETIC_RISK_VETO",
                status="FAILED" if risk_scenario == "REJECT" else "PASSED",
            ),
        ),
        reasons=(
            "SYNTHETIC_RISK_VETO" if risk_scenario == "REJECT" else "SYNTHETIC_RISK_ACCEPTED",
        ),
        member_vetoes=tuple(
            RiskMemberVeto(
                security_id=member.security_id,
                research_id=member.research_id,
                disposition="REJECTED" if risk_scenario == "REJECT" else "ACCEPTED",
                gates=(
                    RiskGate(
                        gate_id="SYNTHETIC_RISK_VETO",
                        status="FAILED" if risk_scenario == "REJECT" else "PASSED",
                    ),
                ),
                reasons=(
                    "SYNTHETIC_RISK_VETO"
                    if risk_scenario == "REJECT"
                    else "SYNTHETIC_RISK_ACCEPTED",
                ),
            )
            for member in command.members
        ),
    )
    provisional = freeze_research(
        command,
        ResearchFrameworkOutput(
            research_run_id="provisional-research-run",
            risk_run_id="provisional-risk-run",
            draft=draft,
            risk_veto=provisional_risk,
            raw_scores=raw_scores,
            tool_evidence_refs=tool_evidence_refs,
            tool_evidence=tool_evidence,
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
    definition = FrozenAgentDefinition(
        definition_id=RESEARCH_DEFINITION_ID,
        version="2.0.0",
        instructions=RESEARCH_DEFINITION_INSTRUCTIONS,
        model_adapter_id=RESEARCH_MODEL_ADAPTER_ID,
        output_contract=FrozenOutputContract(
            contract_id=RESEARCH_OUTPUT_CONTRACT_ID,
            version=RESEARCH_OUTPUT_CONTRACT_VERSION,
            schema=ResearchDraft.model_json_schema(),
        ),
    )
    bundle = DecisionCaseVersionBundle(
        case_contract_version="research.1.0.0",
        host_contract_version="research.1.0.0",
        host_application_version=settings.configuration_version,
        host_source_sha=settings.source_sha,
        agent_definition_id=RESEARCH_DEFINITION_ID,
        agent_definition_version="2.0.0",
        model_adapter_id=RESEARCH_MODEL_ADAPTER_ID,
        routing_policy_version=RESEARCH_ROUTING_POLICY_VERSION,
        output_contract_version=RESEARCH_OUTPUT_CONTRACT_VERSION,
        report_projection_contract_version="research.1.0.0",
        **CURRENT_M_AGENT_RELEASE.model_dump(),
    )
    base = FrozenDecisionCase(
        synthetic=True,
        generator_version="synthetic-research-case-v1",
        seed=1616,
        case_id="research-case-1616",
        business_identity="synthetic-research-1616",
        knowledge_cutoff=command.knowledge_cutoff.isoformat(),
        report_generated_at="2042-06-01T00:04:00+00:00",
        evidence_clock=EvidenceClock(
            fact_effective_at=command.cutoff_at.isoformat(),
            source_published_at=command.cutoff_at.isoformat(),
            acquired_at=command.cutoff_at.isoformat(),
            validated_at=command.cutoff_at.isoformat(),
        ),
        qualification_scope="D0_SYNTHETIC_CONTRACT_ONLY",
        version_bundle=bundle,
        agent_definition=definition,
        input={
            "account": {
                "account_id": "synthetic-account-4017",
                "account_kind": "SIMULATED_CASH",
            },
            "research": command.model_dump(mode="json"),
        },
        expected_external_result=ExternalResult(
            outcome_code="RESEARCH_REJECTED" if risk_scenario == "REJECT" else "RESEARCH_FROZEN",
            summary="provisional",
            key_reasons=("PROVISIONAL",),
            research=provisional,
        ),
        research=command,
        access_scope=scope,
    )
    risk_run_id = risk_run_id_for(
        base.framework_run_id,
        draft,
        raw_scores=raw_scores,
        tool_evidence_refs=tool_evidence_refs,
        tool_evidence=tool_evidence,
        member_handoffs=tuple(
            ResearchMemberHandoff(
                security_id=member.security_id,
                research_id=member.research_id,
                evidence=member.evidence,
                risk_flags=member.risk_flags,
            )
            for member in command.members
        ),
    )
    assert provisional.risk_veto is not None
    risk_veto = provisional.risk_veto.model_copy(update={"run_id": risk_run_id})
    risk = provisional.model_copy(
        update={
            "handoff": provisional.handoff.model_copy(
                update={
                    "research_run_id": base.framework_run_id,
                    "risk_run_id": risk_run_id,
                    "risk_veto": risk_veto,
                }
            ),
            "risk_veto": risk_veto,
        }
    )
    expected = ExternalResult(
        outcome_code="RESEARCH_REJECTED" if risk_scenario == "REJECT" else "RESEARCH_FROZEN",
        summary=(
            "Synthetic fixed-ten research was rejected by the independent risk veto."
            if risk_scenario == "REJECT"
            else "Synthetic fixed-ten research and independent risk veto were frozen."
        ),
        key_reasons=risk.reasons,
        research=risk,
    )
    return base.model_copy(update={"expected_external_result": expected})


def test_research_run_and_risk_veto_are_durable_and_rejected_result_is_final(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings)

    execution = run_frozen_decision_case(
        migrated_settings,
        case.model_dump(mode="json"),
    )

    assert execution.report is not None
    assert execution.business_result_status == "REJECTED"
    assert execution.report.result.outcome_code == "RESEARCH_REJECTED"
    assert execution.report.result.research is not None
    assert execution.report.result.research.risk_veto is not None
    assert execution.report.result.research.disposition == "REJECTED"
    assert execution.report.result.research.raw_scores is not None
    assert len(execution.report.result.research.raw_scores) == 10
    assert len(execution.report.result.research.risk_veto.member_vetoes) == 10
    assert {
        member.security_id for member in execution.report.result.research.risk_veto.member_vetoes
    } == set(execution.report.result.research.handoff.security_ids)
    assert tuple(
        evidence.evidence_id for evidence in execution.report.result.research.tool_evidence
    ) == ("announcement:synthetic-security-00",)
    assert execution.framework_run_status == "SUCCEEDED"
    assert {
        stage.phase: stage.status
        for stage in execution.report.stage_results
        if stage.phase in {"RESEARCH", "RISK_VETO", "BUSINESS_DECISION"}
    } == {
        "RESEARCH": "SUCCEEDED",
        "RISK_VETO": "REJECTED",
        "BUSINESS_DECISION": "REJECTED",
    }

    runtime = initialize_runtime_storage(migrated_settings)
    research_run = asyncio.run(runtime.run_store.get_run(case.framework_run_id))
    assert research_run is not None
    assert research_run.definition_id == RESEARCH_DEFINITION_ID
    assert research_run.snapshot is not None
    assert tuple(
        stage.identity.stage_id for stage in research_run.snapshot.context_plan.stages
    ) == ("collect", "analyze", "bull-bear", "draft")
    assert tuple(
        declaration.effect.value for declaration in research_run.snapshot.tool_declarations
    ) == ("READ_ONLY",)
    assert len(asyncio.run(runtime.run_store.get_checkpoints(case.framework_run_id))) >= 4

    risk_run_id = execution.report.result.research.handoff.risk_run_id
    risk_run = asyncio.run(runtime.run_store.get_run(risk_run_id))
    assert risk_run is not None
    assert risk_run.status.value == "REJECTED"
    assert risk_run.definition_id == RISK_DEFINITION_ID
    assert risk_run.definition_version == RISK_DEFINITION_VERSION
    assert risk_run.snapshot is not None
    assert risk_run.snapshot.tool_declarations == ()
    assert risk_run.snapshot.has_context_provider is True
    assert risk_run.input is not None
    assert "fictional structured fact is available at the cutoff" in risk_run.input
    assert "LIQUIDITY_WARNING" in risk_run.input
    risk_policy_decisions = asyncio.run(runtime.run_store.get_policy_decisions(risk_run_id))
    assert any(
        decision.gate is PolicyGate.FINAL_OUTPUT
        and decision.action is PolicyAction.REJECT
        and decision.reason_code == "SYNTHETIC_RISK_VETO"
        for decision in risk_policy_decisions
    )
    checkpoints = asyncio.run(runtime.run_store.get_checkpoints(case.framework_run_id))
    assert sum(checkpoint.step_type is StepType.MODEL for checkpoint in checkpoints) == 2
    assert sum(checkpoint.step_type is StepType.TOOL for checkpoint in checkpoints) == 1
    context_results = tuple(
        result
        for checkpoint in checkpoints
        if checkpoint.step_type is StepType.CONTEXT
        if (result := parse_stage_result(checkpoint.output)) is not None
    )
    assert {
        (result.stage_id, result.scope.value, result.boundary): len(result.output_items)
        for result in context_results
        if result.output_items
    } == {
        ("collect", "RUN_INPUT", 0): 10,
        ("analyze", "RUN_INPUT", 0): 1,
        ("bull-bear", "RUN_INPUT", 0): 1,
        ("draft", "RUN_INPUT", 0): 1,
    }
    draft_context = next(
        result for result in context_results if result.stage_id == "draft" and result.output_items
    )
    assert draft_context.output_items[0].item.metadata["source_stage"] == "bull-bear"
    stage_artifacts = {
        result.stage_id: ResearchStageArtifact.model_validate(
            json.loads(result.output_items[0].item.content)["stage_artifact"]
        )
        for result in context_results
        if result.stage_id in {"analyze", "bull-bear", "draft"} and result.output_items
    }
    assert stage_artifacts["analyze"].source_stage_id == "collect"
    assert stage_artifacts["bull-bear"].source_stage_id == "analyze"
    assert stage_artifacts["draft"].source_stage_id == "bull-bear"
    assert stage_artifacts["bull-bear"].bull_case is not None
    assert stage_artifacts["bull-bear"].bear_case is not None
    assert stage_artifacts["draft"].bull_case is not None
    assert stage_artifacts["draft"].bear_case is not None
    assert execution.report.result.research.handoff.evidence_ids[-1:] == (
        "announcement:synthetic-security-00",
    )

    with runtime.engine.connect() as connection:
        recorded_run_ids = {
            row.framework_run_id
            for row in connection.execute(
                DECISION_STAGE_EVENTS.select().where(
                    DECISION_STAGE_EVENTS.c.business_object_id == case.business_object_id
                )
            )
        }
        risk_phases = {
            stage.phase
            for stage in DecisionLedger(runtime.engine).get_stage_results(
                case.business_object_id,
                connection,
                framework_run_id=risk_run_id,
            )
        }
    assert {case.framework_run_id, risk_run_id}.issubset(recorded_run_ids)
    assert "RISK_FRAMEWORK_RUN" in risk_phases


def test_allowlisted_announcement_tool_is_read_only_and_argument_bound() -> None:
    tool = _read_announcement_tool()
    outcome = asyncio.run(
        tool.invoke(
            ToolRequest(
                call_id="tool-call-1616",
                tool_name="read_announcement",
                arguments='{"security_id":"synthetic-security-00"}',
            )
        )
    )

    assert outcome.status.value == "SUCCESS"
    assert outcome.result is not None
    assert "announcement:synthetic-security-00" in outcome.result
    assert "trade instruction" in outcome.result


def test_research_model_can_skip_exploration_and_binds_tool_calls_to_context() -> None:
    adapter = frozen_decision_case._StagedResearchModelAdapter("{}")
    no_exploration = ModelRequest(
        input="{}",
        instructions="",
        context_items=(
            ContextItem(
                item_id="member-input",
                source="synthetic-test",
                content=json.dumps({"security_id": "renamed-security", "risk_flags": []}),
            ),
        ),
        structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
    )
    response = asyncio.run(adapter.generate(no_exploration))
    assert response.tool_calls == ()

    exploration = no_exploration.model_copy(
        update={
            "context_items": (
                ContextItem(
                    item_id="member-input",
                    source="synthetic-test",
                    content=json.dumps(
                        {
                            "security_id": "renamed-security",
                            "risk_flags": ["LIQUIDITY_WARNING"],
                        }
                    ),
                ),
            )
        }
    )
    response = asyncio.run(adapter.generate(exploration))
    assert len(response.tool_calls) == 1
    assert json.loads(response.tool_calls[0].arguments)["security_id"] == "renamed-security"


def test_risk_framework_rejection_is_driven_by_typed_veto_not_scenario() -> None:
    command = research_command(risk_scenario="ACCEPT")
    draft = _draft(command)
    risk_plan = research_service.prepare_research_risk_plan(
        command,
        "probe-research-run",
        draft,
        (),
    )
    original_definition = frozen_decision_case._risk_definition(
        command,
        research_run_id="probe-research-run",
        risk_plan=risk_plan,
    )
    payload = json.loads(original_definition.model_adapter._responses[0])
    payload["disposition"] = "REJECTED"
    payload["gates"][0]["status"] = "FAILED"
    for member_veto in payload["member_vetoes"]:
        member_veto["disposition"] = "REJECTED"
        member_veto["gates"][0]["status"] = "FAILED"
    adapter = DeterministicModelAdapter(
        responses=(json.dumps(payload),),
        capabilities=original_definition.model_adapter.capabilities,
    )
    definition = AgentDefinition.for_adapter(
        definition_id=original_definition.definition_id,
        version=original_definition.version,
        instructions=original_definition.instructions,
        model_adapter=adapter,
        context_provider=original_definition.context_provider,
        tools=(),
        output_contract=original_definition.output_contract,
        run_policy=original_definition.run_policy,
    )
    registry = DefinitionRegistry()
    registry.register(definition)
    runner = Runner(
        registry=registry,
        store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
    )

    async def run() -> str:
        created = await runner.create_run(
            definition.definition_id,
            definition.version,
            risk_plan.input_payload,
            run_id=risk_plan.risk_run_id,
        )
        result = await runner.start_run(created.run_id)
        return result.status.value

    assert asyncio.run(run()) == "REJECTED"


def test_risk_handoff_preserves_each_member_evidence_and_flags(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    execution = run_frozen_decision_case(migrated_settings, case.model_dump(mode="json"))

    assert execution.report is not None
    research = execution.report.result.research
    assert research is not None
    assert (
        research.handoff.member_handoffs[0]
        .evidence[0]
        .statement.startswith("A fictional structured fact")
    )
    assert research.handoff.member_handoffs[0].risk_flags == ("LIQUIDITY_WARNING",)


def test_invalid_research_provenance_stops_before_raw_score_and_risk(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    command = case.research
    assert command is not None
    invalid_draft = _draft(command).model_dump(mode="json")
    invalid_draft["members"][0]["evidence_refs"] = ["invented-evidence"]

    monkeypatch.setattr(
        frozen_decision_case,
        "_research_model_response",
        lambda _: json.dumps(invalid_draft, ensure_ascii=True, separators=(",", ":")),
    )
    execution = run_frozen_decision_case(migrated_settings, case.model_dump(mode="json"))

    assert execution.report is None
    assert any(
        stage.phase == "FRAMEWORK_RUN" and stage.status == "SUCCEEDED"
        for stage in execution.stage_results
    )
    assert any(
        stage.phase == "RESEARCH"
        and stage.status == "FAILED"
        and "RESEARCH_PROVENANCE_INVALID" in stage.reasons
        for stage in execution.stage_results
    )
    assert not any(stage.phase in {"RAW_SCORE", "RISK_VETO"} for stage in execution.stage_results)
    with initialize_runtime_storage(migrated_settings).engine.connect() as connection:
        recorded_run_ids = {
            row.framework_run_id
            for row in connection.execute(
                DECISION_STAGE_EVENTS.select().where(
                    DECISION_STAGE_EVENTS.c.business_object_id == case.business_object_id
                )
            )
        }
    assert recorded_run_ids == {case.framework_run_id}


def test_incomplete_provider_manifest_fails_research_before_downstream_stages(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    original_context_items = frozen_decision_case._research_context_items
    monkeypatch.setattr(
        frozen_decision_case,
        "_research_context_items",
        lambda command: original_context_items(command)[:-1],
    )

    execution = run_frozen_decision_case(migrated_settings, case.model_dump(mode="json"))

    assert execution.report is None
    assert any(
        stage.phase == "RESEARCH"
        and stage.status == "FAILED"
        and "RESEARCH_REQUIRED_FACTS_INCOMPLETE" in stage.reasons
        for stage in execution.stage_results
    ), [(stage.phase, stage.status, stage.reasons) for stage in execution.stage_results]
    assert not any(stage.phase in {"RAW_SCORE", "RISK_VETO"} for stage in execution.stage_results)


def test_incomplete_member_manifest_is_saved_as_research_data_failure(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    payload = case.model_dump(mode="json")
    research_payload = payload["research"]
    assert isinstance(research_payload, dict)
    research_payload["members"][0]["data_manifest"]["entries"][1]["completeness"] = "INCOMPLETE"
    research_payload["members"][0]["data_manifest"]["entries"][1]["event_status"] = "UNAVAILABLE"
    payload["input"]["research"] = research_payload

    execution = run_frozen_decision_case(migrated_settings, payload)

    assert execution.report is None
    assert any(
        stage.phase == "RESEARCH"
        and stage.status == "FAILED"
        and "RESEARCH_REQUIRED_FACTS_INCOMPLETE" in stage.reasons
        for stage in execution.stage_results
    )
    research_stage = next(stage for stage in execution.stage_results if stage.phase == "RESEARCH")
    data_gates = {
        gate.gate_id: gate.status
        for gate in research_stage.gate_results
        if gate.gate_id.startswith("RESEARCH_DATA:")
    }
    assert len(data_gates) == 10 * len(RESEARCH_REQUIRED_DATA_TYPES)
    assert data_gates["RESEARCH_DATA:synthetic-security-00:MONEY_FLOW"] == "FAILED"
    assert sum(status == "FAILED" for status in data_gates.values()) == 1
    assert sum(status == "PASSED" for status in data_gates.values()) == (len(data_gates) - 1)
    assert not any(stage.phase in {"RAW_SCORE", "RISK_VETO"} for stage in execution.stage_results)


def test_missing_structured_signal_is_saved_as_research_data_failure(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    payload = case.model_dump(mode="json")
    research_payload = payload["research"]
    assert isinstance(research_payload, dict)
    research_payload["members"][0]["data_manifest"]["entries"][3]["completeness"] = "INCOMPLETE"
    research_payload["members"][0]["data_manifest"]["entries"][3]["event_status"] = "UNAVAILABLE"
    research_payload["members"][0]["structured_signals"]["operating_cash_flow_return_on_assets"] = (
        None
    )
    payload["input"]["research"] = research_payload

    execution = run_frozen_decision_case(migrated_settings, payload)

    assert execution.report is None
    assert any(
        stage.phase == "RESEARCH"
        and stage.status == "FAILED"
        and "RESEARCH_REQUIRED_FACTS_INCOMPLETE" in stage.reasons
        for stage in execution.stage_results
    )
    assert not any(stage.phase in {"RAW_SCORE", "RISK_VETO"} for stage in execution.stage_results)


def test_invalid_risk_handoff_preserves_completed_research_and_raw_score(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    original_execute_risk = cast(
        Callable[..., Awaitable[FrameworkRunResult]],
        case_bootstrap.__dict__["execute_research_risk_run"],
    )

    async def execute_invalid_risk(*args: Any, **kwargs: Any) -> FrameworkRunResult:
        result = await original_execute_risk(*args, **kwargs)
        assert result.output is not None
        payload = json.loads(result.output)
        payload["handoff_fingerprint"] = "0" * 64
        return replace(
            result,
            output=json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        )

    monkeypatch.setattr(
        case_bootstrap,
        "execute_research_risk_run",
        execute_invalid_risk,
        raising=False,
    )
    execution = run_frozen_decision_case(migrated_settings, case.model_dump(mode="json"))

    assert execution.report is None
    assert any(
        stage.phase == "RESEARCH"
        and stage.status == "SUCCEEDED"
        and "RESEARCH_DRAFT_READY" in stage.reasons
        for stage in execution.stage_results
    )
    assert any(
        stage.phase == "RAW_SCORE"
        and stage.status == "SUCCEEDED"
        and "RAW_SCORE_FROZEN" in stage.reasons
        for stage in execution.stage_results
    )
    assert any(
        stage.phase == "RISK_VETO"
        and stage.status == "FAILED"
        and "RISK_HANDOFF_INVALID" in stage.reasons
        for stage in execution.stage_results
    )
    assert not any(stage.phase == "BUSINESS_DECISION" for stage in execution.stage_results)


def test_missing_upstream_selection_event_stops_before_framework_execution(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    payload = case.model_dump(mode="json")
    research = payload["research"]
    assert isinstance(research, dict)
    research["selection_event_id"] = "selection-event-not-committed"
    research["selection_fingerprint"] = selection_binding_sha256(
        research["selection_object_id"],
        research["selection_event_id"],
        datetime.fromisoformat(research["cutoff_at"]),
        FrozenDualTargetScreening.model_validate(research["screening"]),
    )
    payload["input"]["research"] = research
    execution = run_frozen_decision_case(migrated_settings, payload)

    assert execution.report is None
    assert execution.framework_run_status == "CREATED"
    assert any(
        stage.phase == "RESEARCH"
        and stage.status == "FAILED"
        and "RESEARCH_SELECTION_EVENT_MISSING" in stage.reasons
        for stage in execution.stage_results
    )
    assert not any(
        stage.phase in {"FRAMEWORK_RUN", "RAW_SCORE", "RISK_VETO"}
        for stage in execution.stage_results
    )


def test_upstream_selection_percentile_mismatch_stops_before_framework_execution(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    payload = case.model_dump(mode="json")
    research = payload["research"]
    assert isinstance(research, dict)
    screening = FrozenDualTargetScreening.model_validate(research["screening"])
    changed_screening = screening.model_copy(
        update={
            "positive_percentiles": {
                **screening.positive_percentiles,
                screening.selected_member_ids[0]: Decimal("61"),
            }
        }
    )
    changed_screening = FrozenDualTargetScreening.model_validate(
        changed_screening.model_copy(
            update={"output_sha256": screening_output_sha256(changed_screening)}
        ).model_dump(mode="python")
    )
    research["screening"] = changed_screening.model_dump(mode="json")
    research["selection_fingerprint"] = selection_binding_sha256(
        research["selection_object_id"],
        research["selection_event_id"],
        datetime.fromisoformat(research["cutoff_at"]),
        changed_screening,
    )
    payload["input"]["research"] = research
    execution = run_frozen_decision_case(migrated_settings, payload)

    assert execution.report is None
    assert execution.framework_run_status == "CREATED"
    assert any(
        stage.phase == "RESEARCH"
        and stage.status == "FAILED"
        and "RESEARCH_SELECTION_PERCENTILE_MISMATCH" in stage.reasons
        for stage in execution.stage_results
    )
    assert not any(
        stage.phase in {"FRAMEWORK_RUN", "RAW_SCORE", "RISK_VETO"}
        for stage in execution.stage_results
    )


def test_rejected_risk_run_does_not_fabricate_member_verdicts(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    command = case.research
    assert command is not None

    async def execute_research() -> FrameworkRunResult:
        return FrameworkRunResult(
            run_id=case.framework_run_id,
            status="SUCCEEDED",
            output=_draft(command).model_dump_json(),
        )

    async def execute_rejected_risk(
        _: FrameworkRunResult, plan: ResearchRiskPlan
    ) -> FrameworkRunResult:
        return FrameworkRunResult(
            run_id=plan.risk_run_id,
            status="REJECTED",
            output=None,
            error_code="RISK_RUN_REJECTED",
        )

    framework = asyncio.run(
        execute_research_risk_journey(
            case,
            execute_research=execute_research,
            execute_risk=execute_rejected_risk,
        )
    )
    assert framework.status == "SUCCEEDED"
    assert framework.risk_run_status == "REJECTED"
    envelope = ResearchFrameworkOutput.model_validate_json(framework.output or "")
    assert envelope.risk_veto is None


def test_research_waiting_is_saved_without_a_research_failure(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")

    async def waiting_research_run(*_: object, **__: object) -> FrameworkRunResult:
        return FrameworkRunResult(
            run_id=case.framework_run_id,
            status="WAITING",
            output=None,
            waiting_reason="RESEARCH_WAITING_FOR_RECOVERY",
        )

    monkeypatch.setattr(case_bootstrap, "execute_research_run", waiting_research_run)
    execution = run_frozen_decision_case(migrated_settings, case.model_dump(mode="json"))

    assert execution.report is None
    assert execution.framework_run_status == "WAITING"
    assert any(
        stage.phase == "FRAMEWORK_RUN"
        and stage.status == "WAITING"
        and "RESEARCH_WAITING_FOR_RECOVERY" in stage.reasons
        for stage in execution.stage_results
    )
    assert not any(stage.phase == "RESEARCH" for stage in execution.stage_results)


def test_risk_waiting_preserves_raw_score_without_a_risk_failure(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")

    async def waiting_risk_run(
        _: object,
        runtime: object,
        research_run: FrameworkRunResult,
        risk_plan: ResearchRiskPlan,
        *args: object,
        **kwargs: object,
    ) -> FrameworkRunResult:
        del runtime, research_run, args, kwargs
        return FrameworkRunResult(
            run_id=risk_plan.risk_run_id,
            status="WAITING",
            output=None,
            waiting_reason="RISK_WAITING_FOR_RECOVERY",
        )

    monkeypatch.setattr(case_bootstrap, "execute_research_risk_run", waiting_risk_run)
    execution = run_frozen_decision_case(migrated_settings, case.model_dump(mode="json"))

    assert execution.report is None
    assert any(
        stage.phase == "RISK_FRAMEWORK_RUN"
        and stage.status == "WAITING"
        and "RISK_WAITING_FOR_RECOVERY" in stage.reasons
        for stage in execution.stage_results
    ), [
        (stage.phase, stage.status, stage.reasons)
        for stage in execution.stage_results
        if stage.phase == "RISK_FRAMEWORK_RUN"
    ]
    assert any(
        stage.phase == "RESEARCH" and stage.status == "SUCCEEDED"
        for stage in execution.stage_results
    )
    assert any(
        stage.phase == "RAW_SCORE" and stage.status == "SUCCEEDED"
        for stage in execution.stage_results
    )
    assert not any(stage.phase == "RISK_VETO" for stage in execution.stage_results)
    assert not any(stage.phase == "HOST_VALIDATION" for stage in execution.stage_results)


def test_pending_risk_reservation_can_be_reused_after_creation_gap(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    runtime = initialize_runtime_storage(migrated_settings)
    ledger = DecisionLedger(runtime.engine)
    ledger.persist_business_mapping_before_framework(case)
    risk_run_id = "risk-run-pending-reservation"

    first = asyncio.run(
        decision_case_service._record_auxiliary_run_reservation(
            ledger,
            case,
            risk_run_id,
        )
    )
    second = asyncio.run(
        decision_case_service._record_auxiliary_run_reservation(
            ledger,
            case,
            risk_run_id,
        )
    )

    assert first is True
    assert second is True


def test_research_context_stages_have_distinct_frozen_roles(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings)
    execution = run_frozen_decision_case(migrated_settings, case.model_dump(mode="json"))
    assert execution.report is not None

    runtime = initialize_runtime_storage(migrated_settings)
    research_run = asyncio.run(runtime.run_store.get_run(case.framework_run_id))
    assert research_run is not None and research_run.snapshot is not None
    stages = research_run.snapshot.context_plan.stages
    assert tuple(stage.identity.stage_id for stage in stages) == (
        "collect",
        "analyze",
        "bull-bear",
        "draft",
    )
    assert tuple(stage.config.config["phase"] for stage in stages if stage.config) == (
        "collect",
        "analyze",
        "bull-bear",
        "draft",
    )
    assert tuple(stage.identity.scope.value for stage in stages) == (
        "RUN_INPUT",
        "RUN_INPUT",
        "RUN_INPUT",
        "RUN_INPUT",
    )
    assert tuple(stage.output_channels for stage in stages) == (
        ("RESEARCH_COLLECTION",),
        ("RESEARCH_ANALYSIS",),
        ("RESEARCH_BULL_BEAR",),
        ("RESEARCH_DRAFT",),
    )


def test_historical_research_case_without_original_run_fails_closed(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings)
    historical_case = case.model_copy(
        update={
            "version_bundle": case.version_bundle.model_copy(
                update=HISTORICAL_M_AGENT_RELEASE.model_dump()
            )
        }
    )

    with pytest.raises(DecisionEventCommitError, match="missing durable framework Run"):
        run_frozen_decision_case(
            migrated_settings,
            historical_case.model_dump(mode="json"),
        )


def test_research_provider_failure_closes_without_raw_score_or_risk_run(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT", failure_mode="DATA")

    execution = run_frozen_decision_case(
        migrated_settings,
        case.model_dump(mode="json"),
    )

    assert execution.report is None
    assert execution.business_commit_status == "NOT_ATTEMPTED"
    assert any(
        stage.phase == "RESEARCH"
        and stage.status == "FAILED"
        and "RESEARCH_DATA_UNAVAILABLE" in stage.reasons
        for stage in execution.stage_results
    )
    assert not any(stage.phase == "RISK_VETO" for stage in execution.stage_results)


def test_insufficient_raw_score_model_evidence_is_saved_as_raw_score_failure(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    assert case.research is not None
    model = case.research.raw_score_model.model_copy(
        update={
            "mature_months": 59,
            "training_record_count": 499,
            "positive_record_count": 249,
        }
    )
    case = case.model_copy(
        update={
            "input": {
                **case.input,
                "research": {
                    **case.input["research"],
                    "raw_score_model": model.model_dump(mode="json"),
                },
            },
            "research": case.research.model_copy(update={"raw_score_model": model}),
        }
    )

    execution = run_frozen_decision_case(
        migrated_settings,
        case.model_dump(mode="json"),
    )

    assert execution.report is None
    assert any(
        stage.phase == "RAW_SCORE"
        and stage.status == "FAILED"
        and "RAW_SCORE_MODEL_EVIDENCE_INSUFFICIENT" in stage.reasons
        for stage in execution.stage_results
    )
    assert not any(stage.phase == "RISK_VETO" for stage in execution.stage_results)


def test_research_output_contract_failure_is_saved_as_research_failure(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT", failure_mode="SYSTEM")

    execution = run_frozen_decision_case(
        migrated_settings,
        case.model_dump(mode="json"),
    )

    assert execution.report is None
    assert any(
        stage.phase == "RESEARCH" and stage.status == "FAILED" for stage in execution.stage_results
    )


def test_raw_score_failure_has_its_own_gate_and_does_not_start_risk(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT", failure_mode="RAW_SCORE")

    execution = run_frozen_decision_case(
        migrated_settings,
        case.model_dump(mode="json"),
    )

    assert execution.report is None
    assert any(
        stage.phase == "RAW_SCORE"
        and stage.status == "FAILED"
        and "RAW_SCORE_NOT_AVAILABLE" in stage.reasons
        for stage in execution.stage_results
    )
    assert not any(stage.phase == "RISK_VETO" for stage in execution.stage_results)


def test_actual_raw_score_arithmetic_failure_is_saved_at_raw_score_phase(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")

    def fail_raw_score(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RawScoreCalculationError("RAW_SCORE_CALCULATION_FAILED")

    monkeypatch.setattr(research_service, "freeze_raw_score", fail_raw_score)
    execution = run_frozen_decision_case(migrated_settings, case.model_dump(mode="json"))

    assert execution.report is None
    assert any(
        stage.phase == "RAW_SCORE"
        and stage.status == "FAILED"
        and "RAW_SCORE_CALCULATION_FAILED" in stage.reasons
        for stage in execution.stage_results
    )


def test_risk_run_failure_is_saved_without_fabricating_a_veto(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT", failure_mode="RISK")

    execution = run_frozen_decision_case(
        migrated_settings,
        case.model_dump(mode="json"),
    )

    assert execution.report is None
    assert any(
        stage.phase == "RAW_SCORE" and stage.status == "SUCCEEDED"
        for stage in execution.stage_results
    )
    assert any(
        stage.phase == "RISK_VETO" and stage.status == "FAILED" for stage in execution.stage_results
    )
    assert not any(stage.phase == "BUSINESS_DECISION" for stage in execution.stage_results)


def test_missing_existing_risk_run_fails_closed_without_replacement(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    runtime = initialize_runtime_storage(migrated_settings)
    first = asyncio.run(execute_research_decision_case(case, runtime))
    assert first.risk_run_id is not None

    original_get_run = runtime.run_store.get_run

    async def missing_risk_run(run_id: str):  # type: ignore[no-untyped-def]
        if run_id == first.risk_run_id:
            return None
        return await original_get_run(run_id)

    monkeypatch.setattr(runtime.run_store, "get_run", missing_risk_run)
    with pytest.raises(MappedDurableRunMissingError, match="auxiliary"):
        asyncio.run(execute_research_decision_case(case, runtime))


def test_research_success_can_recover_the_same_risk_identity_after_a_reserved_gap(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    runtime = initialize_runtime_storage(migrated_settings)
    reserved: list[str] = []

    async def reserve(risk_run_id: str) -> bool:
        reserved.append(risk_run_id)
        return True

    first = asyncio.run(
        execute_research_decision_case(
            case,
            runtime,
            record_auxiliary_run_reservation=reserve,
        )
    )
    assert first.risk_run_id is not None
    assert reserved == []

    original_get_run = runtime.run_store.get_run
    missing_once = True

    async def observe_reserved_gap(run_id: str):  # type: ignore[no-untyped-def]
        nonlocal missing_once
        if run_id == first.risk_run_id and missing_once:
            missing_once = False
            return None
        return await original_get_run(run_id)

    monkeypatch.setattr(runtime.run_store, "get_run", observe_reserved_gap)
    second = asyncio.run(
        execute_research_decision_case(
            case,
            runtime,
            record_auxiliary_run_reservation=reserve,
        )
    )

    assert second.risk_run_id == first.risk_run_id
    assert second.risk_run_status == "SUCCEEDED"
    assert reserved == [first.risk_run_id]


def test_research_commit_failure_restores_auxiliary_risk_stage_history(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")

    def fail_commit(*_: object, **__: object) -> object:
        raise DecisionEventCommitError("synthetic commit failure")

    monkeypatch.setattr(DecisionLedger, "commit_event_fact", fail_commit)
    execution = run_frozen_decision_case(
        migrated_settings,
        case.model_dump(mode="json"),
    )

    assert execution.report is None
    command = case.research
    assert command is not None
    tool_evidence_refs = ("announcement:synthetic-security-00",)
    risk_run_id = risk_run_id_for(
        case.framework_run_id,
        _draft(command),
        raw_scores=tuple(freeze_raw_score(command, member) for member in command.members),
        tool_evidence_refs=tool_evidence_refs,
        tool_evidence=tuple(
            ResearchToolEvidence(
                evidence_id=evidence_id,
                source="synthetic-announcement-feed",
                reference=f"synthetic://announcement/{evidence_id.removeprefix('announcement:')}",
                statement=(
                    f"Synthetic announcement evidence {evidence_id} is read-only "
                    "and contains no trade instruction."
                ),
                acquired_at=command.knowledge_cutoff,
                validated_at=command.knowledge_cutoff,
                knowledge_cutoff=command.knowledge_cutoff,
                semantic_version=RESEARCH_ANNOUNCEMENT_TOOL_VERSION,
                validation_status="VALIDATED",
            )
            for evidence_id in tool_evidence_refs
        ),
        member_handoffs=tuple(
            ResearchMemberHandoff(
                security_id=member.security_id,
                research_id=member.research_id,
                evidence=member.evidence,
                risk_flags=member.risk_flags,
            )
            for member in command.members
        ),
    )
    with initialize_runtime_storage(migrated_settings).engine.connect() as connection:
        recorded_run_ids = {
            row.framework_run_id
            for row in connection.execute(
                DECISION_STAGE_EVENTS.select().where(
                    DECISION_STAGE_EVENTS.c.business_object_id == case.business_object_id
                )
            )
        }
    assert risk_run_id in recorded_run_ids


def test_accepted_research_replays_the_same_report_without_new_downstream_outputs(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")

    first = run_frozen_decision_case(migrated_settings, case.model_dump(mode="json"))
    second = run_frozen_decision_case(migrated_settings, case.model_dump(mode="json"))

    assert first.report is not None
    assert second.report == first.report
    assert first.report.result.outcome_code == "RESEARCH_FROZEN"
    assert first.report.result.research is not None
    assert first.report.result.research.risk_veto is not None
    assert first.report.result.research.risk_veto.disposition == "ACCEPTED"
    assert sum(stage.phase == "BUSINESS_COMMIT" for stage in second.stage_results) == 1
