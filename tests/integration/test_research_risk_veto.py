from __future__ import annotations

import asyncio
import json
from calendar import monthrange
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, cast

import pytest
from m_agent.adapters import (
    DeterministicContextProvider,
    DeterministicModelAdapter,
    InMemoryRunStore,
    PlaintextPayloadCodec,
)
from m_agent.runtime import (
    AgentDefinition,
    ContextItem,
    ContextRequest,
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
from sqlalchemy import select

import stock_profiler.adapters.m_agent.frozen_decision_case as frozen_decision_case
import stock_profiler.bootstrap.decision_cases as case_bootstrap
from stock_profiler.adapters.m_agent.frozen_decision_case import (
    FROZEN_DEFINITION_INSTRUCTIONS,
    FROZEN_OUTPUT_SCHEMA,
    RESEARCH_DEFINITION_INSTRUCTIONS,
    _read_announcement_tool,
    execute_research_decision_case,
)
from stock_profiler.adapters.persistence.decision_ledger import (
    DECISION_EVENTS,
    DECISION_STAGE_EVENTS,
    FORMAL_REPORTS,
    DecisionLedger,
    _decode_research_event_payload,
)
from stock_profiler.adapters.persistence.runtime_ownership import (
    RuntimeStorage,
    initialize_runtime_storage,
)
from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.foundation.clock import UtcClock
from stock_profiler.foundation.decision_versions import (
    CURRENT_M_AGENT_RELEASE,
    HISTORICAL_M_AGENT_RELEASE,
    DecisionCaseVersionBundle,
)
from stock_profiler.modules.candidate_selection.calibrated_candidates import (
    CalibrationRecord,
    CandidateEvidenceClock,
    CandidateInput,
    CandidateReleaseCommand,
    CandidateRiskGate,
    MarketSession,
    MarketStateQualification,
    candidate_release_availability_failure,
    candidate_release_blocked_by_business_prerequisite,
    finalize_candidate_release_publication,
    freeze_candidate_release,
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
    _COMPLETE_SYNTHETIC_INPUT,
    FROZEN_AGENT_DEFINITION_ID,
    DecisionEventFact,
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
    FrameworkRunTransition,
    MappedDurableRunMissingError,
    ResearchMemberRunResult,
)
from stock_profiler.modules.decision_cases.service import execute_research_risk_journey
from stock_profiler.modules.portfolio.market_calendar import synthetic_market_calendar
from stock_profiler.modules.qualification.contracts import (
    CapabilityVersion,
    GovernanceOutcome,
    QualificationEvidence,
    QualificationPolicy,
    QualificationRecord,
    QualificationScope,
)
from stock_profiler.modules.research import service as research_service
from stock_profiler.modules.research.contracts import (
    RESEARCH_ANNOUNCEMENT_TOOL_VERSION,
    RESEARCH_DEFINITION_ID,
    RESEARCH_DEFINITION_VERSION,
    RESEARCH_LEGACY_DEFINITION_VERSION,
    RESEARCH_LEGACY_OUTPUT_CONTRACT_VERSION,
    RESEARCH_LEGACY_ROUTING_POLICY_VERSION,
    RESEARCH_MODEL_ADAPTER_ID,
    RESEARCH_OUTPUT_CONTRACT_ID,
    RESEARCH_OUTPUT_CONTRACT_VERSION,
    RESEARCH_PRIOR_DEFINITION_VERSION,
    RESEARCH_PRIOR_OUTPUT_CONTRACT_VERSION,
    RESEARCH_REQUIRED_DATA_TYPES,
    RESEARCH_ROUTING_POLICY_VERSION,
    RISK_DEFINITION_ID,
    RISK_DEFINITION_VERSION,
    RISK_LEGACY_DEFINITION_VERSION,
    RISK_LEGACY_OUTPUT_CONTRACT_VERSION,
    FrozenDualTargetScreening,
    RawScoreCalculationError,
    RawScoreModelSnapshot,
    RawScoreTrainingRecord,
    ResearchCommand,
    ResearchDataManifest,
    ResearchDataManifestEntry,
    ResearchDraft,
    ResearchDraftMember,
    ResearchEvidence,
    ResearchFrameworkOutput,
    ResearchMemberHandoff,
    ResearchMemberInput,
    ResearchMoneyFlowFacts,
    ResearchRiskPlan,
    ResearchStageArtifact,
    ResearchStructuredFacts,
    ResearchToolEvidence,
    RiskGate,
    RiskMemberVeto,
    RiskVetoDraft,
    calculate_structured_signals,
    decode_historical_research_framework_output,
    decode_legacy_research_framework_output,
    decode_legacy_research_outcome,
    freeze_raw_score,
    frozen_raw_score_model_snapshot,
    handoff_fingerprint,
    risk_run_id_for,
    screening_output_sha256,
    selection_binding_sha256,
)
from stock_profiler.modules.research.service import freeze_research


def _synthetic_entry_window_end(month: str) -> datetime:
    year, month_number = (int(part) for part in month.split("-"))
    next_month = month_number % 12 + 1
    next_year = year + int(month_number == 12)
    return datetime(next_year, next_month, 5, 8, tzinfo=UTC)


def _synthetic_label_maturity(month: str) -> datetime:
    entry_at = _synthetic_entry_window_end(month) - timedelta(days=1)
    target_index = entry_at.year * 12 + entry_at.month - 1 + 6
    target_year, target_month_index = divmod(target_index, 12)
    return entry_at.replace(
        year=target_year,
        month=target_month_index + 1,
        day=min(entry_at.day, monthrange(target_year, target_month_index + 1)[1]),
    )


def _synthetic_raw_score_frozen_at(month: str) -> datetime:
    year, month_number = (int(part) for part in month.split("-"))
    return datetime(
        year,
        month_number,
        monthrange(year, month_number)[1],
        7,
        tzinfo=UTC,
    )


def _structured_facts(index: int) -> ResearchStructuredFacts:
    signal_value = Decimal(index + 1) / Decimal(100)
    return ResearchStructuredFacts(
        money_flow=ResearchMoneyFlowFacts(
            net_amount=signal_value,
            inflow_amount=signal_value + Decimal("1"),
            outflow_amount=Decimal("1"),
        ),
        revenue_growth_current=signal_value,
        revenue_growth_prior=Decimal("0"),
        quarter_profit_improvement=signal_value,
        average_total_assets=Decimal("1"),
        operating_cash_flow_ttm=signal_value,
        working_capital_pressure_current=signal_value,
        working_capital_pressure_prior=Decimal("0"),
        leverage_ratio_current=signal_value,
        leverage_ratio_prior=Decimal("0"),
        stock_return_20d=signal_value,
        industry_return_20d=Decimal("0"),
        downside_semivariance_60d=signal_value,
        max_drawdown_60d=signal_value,
        turnover_change=signal_value,
        institutional_net_buy_ratio=signal_value,
        institutional_listing_frequency=signal_value,
    )


def research_command(
    *,
    risk_scenario: Literal["ACCEPT", "REJECT"] = "REJECT",
    failure_mode: Literal["NONE", "DATA", "RESEARCH", "RAW_SCORE", "RISK", "SYSTEM"] = "NONE",
    risk_rejected_member_ids: tuple[str, ...] = (),
    raw_score_model: RawScoreModelSnapshot | None = None,
) -> ResearchCommand:
    cutoff = datetime(2042, 6, 30, 23, 59, 59, tzinfo=UTC)
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
                    effective_at=cutoff,
                    source_published_at=(
                        cutoff - timedelta(days=30)
                        if index == 0 and data_type == RESEARCH_REQUIRED_DATA_TYPES[0]
                        else cutoff
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
            structured_facts=_structured_facts(index),
            structured_signals=calculate_structured_signals(_structured_facts(index)),
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
        raw_score_model=raw_score_model or frozen_raw_score_model_snapshot(),
        risk_scenario=risk_scenario,
        risk_rejected_member_ids=risk_rejected_member_ids,
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
                thesis=(
                    "Synthetic draft stage consumed: "
                    "Synthetic draft stage consumed the prior frozen stage."
                ),
                bull_case="The synthetic evidence supports a conditional upside case.",
                bear_case="The synthetic evidence preserves a conditional downside case.",
                catalysts=("A fictional catalyst remains conditional.",),
                falsification_conditions=("A frozen downside fact would falsify the thesis.",),
                unknowns=("Future external evidence remains unresolved.",),
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
    no_candidate_calibration: bool = False,
) -> FrozenDecisionCase:
    raw_score_model = frozen_raw_score_model_snapshot()
    if no_candidate_calibration:

        def no_candidate_label(record: RawScoreTrainingRecord) -> bool:
            return (
                record.raw_success_score is not None
                and record.raw_success_score <= Decimal("0.5")
                and record.evaluation_entry_at is not None
            )

        records = tuple(
            record.model_copy(update={"terminal_label": no_candidate_label(record)})
            for record in raw_score_model.training_records
        )
        calibration_history_records = tuple(
            record.model_copy(update={"terminal_label": no_candidate_label(record)})
            for record in raw_score_model.calibration_history_records
        )
        positives = sum(record.terminal_label for record in records)
        raw_score_model = RawScoreModelSnapshot.model_validate(
            {
                **raw_score_model.model_dump(mode="python"),
                "training_records": records,
                "calibration_history_records": calibration_history_records,
                "positive_record_count": positives,
                "negative_record_count": len(records) - positives,
            }
        )
    command = research_command(
        risk_scenario=risk_scenario,
        failure_mode=failure_mode,
        raw_score_model=raw_score_model,
    )
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
            effective_at=command.knowledge_cutoff,
            source_published_at=command.knowledge_cutoff,
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
        version=RESEARCH_DEFINITION_VERSION,
        instructions=RESEARCH_DEFINITION_INSTRUCTIONS,
        model_adapter_id=RESEARCH_MODEL_ADAPTER_ID,
        output_contract=FrozenOutputContract(
            contract_id=RESEARCH_OUTPUT_CONTRACT_ID,
            version=RESEARCH_OUTPUT_CONTRACT_VERSION,
            schema=ResearchDraftMember.model_json_schema(),
        ),
    )
    bundle = DecisionCaseVersionBundle(
        case_contract_version="research.1.0.0",
        host_contract_version="research.1.0.0",
        host_application_version=settings.configuration_version,
        host_source_sha=settings.source_sha,
        agent_definition_id=RESEARCH_DEFINITION_ID,
        agent_definition_version=RESEARCH_DEFINITION_VERSION,
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
        report_generated_at="2042-07-01T00:04:00+00:00",
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
    member_run_ids = tuple(
        frozen_decision_case._research_member_run_id(base, index, member)
        for index, member in enumerate(command.members)
    )
    member_handoffs = tuple(
        ResearchMemberHandoff(
            security_id=member.security_id,
            research_id=member.research_id,
            evidence=member.evidence,
            risk_flags=member.risk_flags,
            research_run_id=member_run_ids[index],
        )
        for index, member in enumerate(command.members)
    )
    risk_run_id = risk_run_id_for(
        base.framework_run_id,
        draft,
        raw_scores=raw_scores,
        tool_evidence_refs=tool_evidence_refs,
        tool_evidence=tool_evidence,
        member_handoffs=member_handoffs,
    )
    provisional_risk = provisional_risk.model_copy(
        update={
            "handoff_fingerprint": handoff_fingerprint(
                command,
                draft,
                raw_scores=raw_scores,
                tool_evidence_refs=tool_evidence_refs,
                tool_evidence=tool_evidence,
                member_handoffs=member_handoffs,
            )
        }
    )
    provisional = freeze_research(
        command,
        ResearchFrameworkOutput(
            research_run_id=base.framework_run_id,
            research_run_ids=member_run_ids,
            risk_run_id=risk_run_id,
            draft=draft,
            risk_veto=provisional_risk,
            raw_scores=raw_scores,
            tool_evidence_refs=tool_evidence_refs,
            tool_evidence=tool_evidence,
            member_handoffs=member_handoffs,
        ),
    )
    expected = ExternalResult(
        outcome_code="RESEARCH_REJECTED" if risk_scenario == "REJECT" else "RESEARCH_FROZEN",
        summary=(
            "Synthetic fixed-ten research was rejected by the independent risk veto."
            if risk_scenario == "REJECT"
            else "Synthetic fixed-ten research and independent risk veto were frozen."
        ),
        key_reasons=provisional.reasons,
        research=provisional,
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
        ("collect", "RUN_INPUT", 0): 1,
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
    collect_context = next(result for result in context_results if result.stage_id == "collect")
    analyze_context = next(result for result in context_results if result.stage_id == "analyze")
    bull_bear_context = next(result for result in context_results if result.stage_id == "bull-bear")
    assert stage_artifacts["analyze"].input_item_ids == tuple(
        item.item.item_id for item in collect_context.output_items
    )
    assert stage_artifacts["bull-bear"].input_item_ids == (
        analyze_context.output_items[0].item.item_id,
    )
    assert stage_artifacts["draft"].input_item_ids == (
        bull_bear_context.output_items[0].item.item_id,
    )
    assert "Synthetic draft stage consumed" in execution.report.result.research.members[0].thesis
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


def test_each_fixed_ten_member_has_its_own_durable_research_run(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")

    execution = run_frozen_decision_case(
        migrated_settings,
        case.model_dump(mode="json"),
    )

    assert execution.report is not None
    research = execution.report.result.research
    assert research is not None
    member_run_ids = tuple(handoff.research_run_id for handoff in research.handoff.member_handoffs)
    assert len(member_run_ids) == 10
    assert len(set(member_run_ids)) == 10
    assert member_run_ids[0] == case.framework_run_id

    runtime = initialize_runtime_storage(migrated_settings)
    command = case.research
    assert command is not None
    for member, member_run_id in zip(command.members, member_run_ids, strict=True):
        run = asyncio.run(runtime.run_store.get_run(member_run_id))
        assert run is not None
        assert run.definition_id == RESEARCH_DEFINITION_ID
        assert run.input is not None
        checkpoints = asyncio.run(runtime.run_store.get_checkpoints(member_run_id))
        context_results = tuple(
            result
            for checkpoint in checkpoints
            if checkpoint.step_type is StepType.CONTEXT
            if (result := parse_stage_result(checkpoint.output)) is not None
        )
        collect = next(result for result in context_results if result.stage_id == "collect")
        assert len(collect.output_items) == 1
        assert json.loads(collect.output_items[0].item.content)["security_id"] == member.security_id
    with runtime.engine.connect() as connection:
        recorded_member_run_ids = {
            row.framework_run_id
            for row in connection.execute(
                DECISION_STAGE_EVENTS.select().where(
                    DECISION_STAGE_EVENTS.c.business_object_id == case.business_object_id
                )
            )
        }
        mapped_run_ids = DecisionLedger(runtime.engine).mapped_framework_run_ids(connection)
        member_terminal_statuses = {
            member_run_id: DecisionLedger(runtime.engine)
            .get_stage_results(
                case.business_object_id,
                connection,
                framework_run_id=member_run_id,
            )[-1]
            .status
            for member_run_id in member_run_ids
        }
    assert set(member_run_ids).issubset(recorded_member_run_ids)
    assert set(member_run_ids).issubset(mapped_run_ids)
    assert member_terminal_statuses == {
        member_run_id: "SUCCEEDED" for member_run_id in member_run_ids
    }


def test_failed_member_does_not_relabel_a_successful_member_run(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    payload = case.model_dump(mode="json")
    research_payload = payload["research"]
    assert isinstance(research_payload, dict)
    research_payload["members"][1]["data_manifest"]["entries"][0].update(
        completeness="INCOMPLETE",
        event_status="UNAVAILABLE",
    )
    payload["input"]["research"] = research_payload
    failed_case = FrozenDecisionCase.model_validate(payload)
    runtime = initialize_runtime_storage(migrated_settings)

    result = asyncio.run(frozen_decision_case.execute_research_run(failed_case, runtime))

    first_run = asyncio.run(runtime.run_store.get_run(failed_case.framework_run_id))
    failed_member_run_id = frozen_decision_case._research_member_run_id(
        failed_case,
        1,
        failed_case.research.members[1],  # type: ignore[union-attr]
    )
    failed_member_run = asyncio.run(runtime.run_store.get_run(failed_member_run_id))
    assert first_run is not None
    assert failed_member_run is not None
    assert first_run.status.value == "SUCCEEDED"
    assert failed_member_run.status.value == "FAILED"
    assert result.status == "FAILED"
    assert result.run_id == failed_member_run_id
    assert (
        next(
            member
            for member in result.research_member_runs
            if member.run_id == failed_member_run_id
        ).status
        == "FAILED"
    )


def test_partial_member_research_recovery_creates_missing_member_runs(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    command = case.research
    assert command is not None
    runtime = initialize_runtime_storage(migrated_settings)
    first_member = command.members[0]
    first_run_id = frozen_decision_case._research_member_run_id(case, 0, first_member)
    first_context = tuple(
        item
        for item in frozen_decision_case._research_context_items(command)
        if item.item_id == f"required-facts:{first_member.security_id}"
    )
    first_definition = frozen_decision_case._research_definition(
        case,
        runtime=runtime,
        run_id=first_run_id,
        member=first_member,
        context_items=first_context,
    )
    first_result = asyncio.run(
        frozen_decision_case._execute_registered_run(
            runtime=runtime,
            run_id=first_run_id,
            definition=first_definition,
            input_payload=frozen_decision_case._research_member_input_payload(
                case,
                first_member,
            ),
            case=None,
            record_transition=None,
            clock=None,
        )
    )
    assert first_result.status == "SUCCEEDED"

    recovery_case = case.model_copy(update={"recovery_framework_run_id": case.framework_run_id})
    recovered = asyncio.run(frozen_decision_case.execute_research_run(recovery_case, runtime))

    assert recovered.status == "SUCCEEDED"
    assert len(recovered.research_member_runs) == 10
    assert all(member.status == "SUCCEEDED" for member in recovered.research_member_runs)
    for member_run in recovered.research_member_runs:
        assert asyncio.run(runtime.run_store.get_run(member_run.run_id)) is not None


def test_later_missing_member_run_preserves_completed_member_results(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    command = case.research
    assert command is not None
    second_run_id = frozen_decision_case._research_member_run_id(case, 1, command.members[1])
    original_execute_registered_run = frozen_decision_case._execute_registered_run

    async def missing_second_member_run(**kwargs: Any) -> FrameworkRunResult:
        if kwargs["run_id"] == second_run_id:
            raise MappedDurableRunMissingError("mapped research member Run is missing")
        return await original_execute_registered_run(**kwargs)

    monkeypatch.setattr(
        frozen_decision_case,
        "_execute_registered_run",
        missing_second_member_run,
    )
    runtime = initialize_runtime_storage(migrated_settings)
    recovered = asyncio.run(frozen_decision_case.execute_research_run(case, runtime))

    assert recovered.status == "FAILED"
    assert recovered.run_id == second_run_id
    assert recovered.error_code == "RESEARCH_RUN_MISSING"
    member_results = {member.run_id: member for member in recovered.research_member_runs}
    assert len(member_results) == len(command.members)
    first_run_id = frozen_decision_case._research_member_run_id(case, 0, command.members[0])
    assert member_results[first_run_id].status == "SUCCEEDED"
    assert member_results[second_run_id].status == "FAILED"
    assert member_results[second_run_id].error_code == "RESEARCH_RUN_MISSING"
    assert any(
        transition.run_id == second_run_id
        and transition.status == "FAILED"
        and transition.reason == "RESEARCH_RUN_MISSING"
        for transition in recovered.transitions
    )


def test_historical_partial_member_recovery_creates_missing_member_runs(
    migrated_settings: Settings,
) -> None:
    current_case = _case(migrated_settings, risk_scenario="ACCEPT")
    payload = current_case.model_dump(mode="json")
    research_payload = payload["research"]
    assert isinstance(research_payload, dict)
    for cohort in research_payload["raw_score_model"]["training_cohorts"]:
        cohort["research_definition_version"] = RESEARCH_PRIOR_DEFINITION_VERSION
    payload["input"]["research"] = research_payload
    payload["agent_definition"]["version"] = RESEARCH_PRIOR_DEFINITION_VERSION
    payload["agent_definition"]["output_contract"]["version"] = (
        RESEARCH_PRIOR_OUTPUT_CONTRACT_VERSION
    )
    historical_output_schema = ResearchDraftMember.model_json_schema()
    for field_name in ("catalysts", "falsification_conditions", "unknowns"):
        historical_output_schema["properties"].pop(field_name, None)
    historical_output_schema["required"] = [
        "security_id",
        "research_id",
        "evidence_refs",
        "thesis",
        "bull_case",
        "bear_case",
        "knowledge_cutoff",
    ]
    payload["agent_definition"]["output_contract"]["json_schema"] = historical_output_schema
    payload["version_bundle"]["agent_definition_version"] = RESEARCH_PRIOR_DEFINITION_VERSION
    payload["version_bundle"]["output_contract_version"] = RESEARCH_PRIOR_OUTPUT_CONTRACT_VERSION
    for member in payload["expected_external_result"]["research"]["members"]:
        for field_name in ("catalysts", "falsification_conditions", "unknowns"):
            member.pop(field_name, None)
    payload["expected_external_result"]["research"]["handoff"]["research_definition_version"] = (
        RESEARCH_PRIOR_DEFINITION_VERSION
    )
    payload["expected_external_result"]["research"]["handoff"][
        "research_output_contract_version"
    ] = RESEARCH_PRIOR_OUTPUT_CONTRACT_VERSION
    historical_case = FrozenDecisionCase.model_validate(payload)
    runtime = initialize_runtime_storage(migrated_settings)
    command = historical_case.research
    assert command is not None
    first_member = command.members[0]
    first_run_id = frozen_decision_case._research_member_run_id(
        historical_case,
        0,
        first_member,
    )
    first_context = tuple(
        item
        for item in frozen_decision_case._research_context_items(command)
        if item.item_id == f"required-facts:{first_member.security_id}"
    )
    first_definition = frozen_decision_case._research_definition(
        historical_case,
        runtime=runtime,
        run_id=first_run_id,
        member=first_member,
        context_items=first_context,
        historical=True,
    )
    first_result = asyncio.run(
        frozen_decision_case._execute_registered_run(
            runtime=runtime,
            run_id=first_run_id,
            definition=first_definition,
            input_payload=frozen_decision_case._research_member_input_payload(
                historical_case,
                first_member,
            ),
            case=None,
            record_transition=None,
            clock=None,
        )
    )
    assert first_result.status == "SUCCEEDED"

    reserved_run_ids: list[str] = []

    async def reserve_member_run(run_id: str) -> bool:
        reserved_run_ids.append(run_id)
        return True

    recovery_case = historical_case.model_copy(
        update={"recovery_framework_run_id": historical_case.framework_run_id}
    )
    recovered = asyncio.run(
        frozen_decision_case.execute_research_run(
            recovery_case,
            runtime,
            record_member_run_reservation=reserve_member_run,
        )
    )

    assert recovered.status == "SUCCEEDED"
    assert len(reserved_run_ids) == 9
    assert len(recovered.research_member_runs) == 10
    assert all(member.status == "SUCCEEDED" for member in recovered.research_member_runs)


def test_reserved_missing_member_research_run_reuses_the_reserved_identity(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    command = case.research
    assert command is not None
    runtime = initialize_runtime_storage(migrated_settings)
    first_member = command.members[0]
    first_run_id = frozen_decision_case._research_member_run_id(case, 0, first_member)
    first_context = tuple(
        item
        for item in frozen_decision_case._research_context_items(command)
        if item.item_id == f"required-facts:{first_member.security_id}"
    )
    first_definition = frozen_decision_case._research_definition(
        case,
        runtime=runtime,
        run_id=first_run_id,
        member=first_member,
        context_items=first_context,
    )
    first_result = asyncio.run(
        frozen_decision_case._execute_registered_run(
            runtime=runtime,
            run_id=first_run_id,
            definition=first_definition,
            input_payload=frozen_decision_case._research_member_input_payload(case, first_member),
            case=None,
            record_transition=None,
            clock=None,
        )
    )
    assert first_result.status == "SUCCEEDED"
    reserved_run_id = frozen_decision_case._research_member_run_id(case, 1, command.members[1])
    ledger = DecisionLedger(runtime.engine)
    ledger.persist_business_mapping_before_framework(case)
    assert asyncio.run(
        decision_case_service._record_research_member_run_reservation(
            ledger,
            case,
            reserved_run_id,
        )
    )

    async def reserve_member_run(run_id: str) -> bool:
        return await decision_case_service._record_research_member_run_reservation(
            ledger,
            case,
            run_id,
        )

    recovery_case = case.model_copy(update={"recovery_framework_run_id": case.framework_run_id})
    recovered = asyncio.run(
        frozen_decision_case.execute_research_run(
            recovery_case,
            runtime,
            record_member_run_reservation=reserve_member_run,
        )
    )

    assert recovered.status == "SUCCEEDED"
    assert len(recovered.research_member_runs) == 10
    assert all(member.status == "SUCCEEDED" for member in recovered.research_member_runs)


def test_historical_aggregate_research_run_is_recovered_with_legacy_contracts(
    migrated_settings: Settings,
) -> None:
    current_case = _case(migrated_settings, risk_scenario="ACCEPT")
    payload = current_case.model_dump(mode="json")
    research_payload = payload["research"]
    assert isinstance(research_payload, dict)
    for member in research_payload["members"]:
        for evidence in member["evidence"]:
            evidence.pop("evidence_contract_version", None)
            evidence.pop("effective_at", None)
            evidence.pop("source_published_at", None)
    for cohort in research_payload["raw_score_model"]["training_cohorts"]:
        cohort["research_definition_version"] = RESEARCH_LEGACY_DEFINITION_VERSION
    payload["input"]["research"] = research_payload
    payload["agent_definition"]["version"] = RESEARCH_LEGACY_DEFINITION_VERSION
    payload["agent_definition"]["instructions"] = (
        frozen_decision_case.LEGACY_RESEARCH_DEFINITION_INSTRUCTIONS
    )
    payload["agent_definition"]["output_contract"]["version"] = (
        RESEARCH_LEGACY_OUTPUT_CONTRACT_VERSION
    )
    legacy_output_schema = ResearchDraft.model_json_schema()
    legacy_member_schema = legacy_output_schema["$defs"]["ResearchDraftMember"]
    for field_name in ("catalysts", "falsification_conditions", "unknowns"):
        legacy_member_schema["properties"].pop(field_name, None)
    legacy_member_schema["required"] = [
        "security_id",
        "research_id",
        "evidence_refs",
        "thesis",
        "bull_case",
        "bear_case",
        "knowledge_cutoff",
    ]
    payload["agent_definition"]["output_contract"]["json_schema"] = legacy_output_schema
    payload["version_bundle"]["agent_definition_version"] = RESEARCH_LEGACY_DEFINITION_VERSION
    payload["version_bundle"]["routing_policy_version"] = RESEARCH_LEGACY_ROUTING_POLICY_VERSION
    payload["version_bundle"]["output_contract_version"] = RESEARCH_LEGACY_OUTPUT_CONTRACT_VERSION
    for member in payload["expected_external_result"]["research"]["members"]:
        for field_name in ("catalysts", "falsification_conditions", "unknowns"):
            member.pop(field_name, None)
    original_legacy_input = json.dumps(
        payload["input"],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    legacy_case = FrozenDecisionCase.model_validate(payload)
    runtime = initialize_runtime_storage(migrated_settings)
    with pytest.raises(MappedDurableRunMissingError, match="original durable research Run"):
        asyncio.run(frozen_decision_case.execute_research_run(legacy_case, runtime))

    definition = frozen_decision_case._legacy_research_definition(
        legacy_case,
        runtime=runtime,
        run_id=legacy_case.framework_run_id,
    )
    assert type(definition.model_adapter) is DeterministicModelAdapter
    assert all(stage.config is None for stage in definition.context_plan.stages)
    assert all(
        stage.input_channels == ("REQUIRED_STRUCTURED_FACTS",)
        and stage.output_channels == ("REQUIRED_STRUCTURED_FACTS",)
        for stage in definition.context_plan.stages
    )
    created = asyncio.run(
        frozen_decision_case._execute_registered_run(
            runtime=runtime,
            run_id=legacy_case.framework_run_id,
            definition=definition,
            input_payload=json.dumps(
                legacy_case.input,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ),
            case=legacy_case,
            record_transition=None,
            clock=None,
        )
    )
    assert created.status == "SUCCEEDED"
    stored_legacy_run = asyncio.run(runtime.run_store.get_run(legacy_case.framework_run_id))
    assert stored_legacy_run is not None
    assert stored_legacy_run.input == original_legacy_input
    legacy_context_payloads = [
        json.loads(output_item.item.content)
        for checkpoint in asyncio.run(
            runtime.run_store.get_checkpoints(legacy_case.framework_run_id)
        )
        if checkpoint.step_type is StepType.CONTEXT
        if (stage_result := parse_stage_result(checkpoint.output)) is not None
        for output_item in stage_result.output_items
    ]
    assert legacy_context_payloads
    assert all("stage_artifact" not in payload for payload in legacy_context_payloads)

    recovered = asyncio.run(frozen_decision_case.execute_research_run(legacy_case, runtime))

    assert recovered.status == "SUCCEEDED"
    assert recovered.run_id == legacy_case.framework_run_id
    assert recovered.run_existed_before is True
    assert recovered.output is not None
    recovered_payload = json.loads(recovered.output)
    assert all(
        field_name not in member
        for member in recovered_payload["members"]
        for field_name in ("catalysts", "falsification_conditions", "unknowns")
    )
    assert frozen_decision_case.frozen_capability_inventory(legacy_case).definition_version == (
        RESEARCH_LEGACY_DEFINITION_VERSION
    )

    async def reserve_risk_run(_: str) -> bool:
        return True

    journey = asyncio.run(
        frozen_decision_case.execute_research_decision_case(
            legacy_case,
            runtime,
            record_auxiliary_run_reservation=reserve_risk_run,
        )
    )
    assert journey.status == "SUCCEEDED"
    assert journey.output is not None
    legacy_envelope = decode_legacy_research_framework_output(json.loads(journey.output))
    assert legacy_envelope.risk_run_id is not None
    risk_run = asyncio.run(runtime.run_store.get_run(legacy_envelope.risk_run_id))
    assert risk_run is not None
    assert risk_run.input is not None
    legacy_risk_input = json.loads(risk_run.input)
    assert "research_run_ids" not in legacy_risk_input
    assert "label_watermark_at" in legacy_risk_input["raw_scores"][0]
    assert all(
        field_name not in member
        for member in legacy_risk_input["draft"]["members"]
        for field_name in ("catalysts", "falsification_conditions", "unknowns")
    )
    assert "research_run_id" not in legacy_risk_input["member_handoffs"][0]
    assert all(
        field_name not in evidence
        for evidence in legacy_risk_input["member_handoffs"][0]["evidence"]
        for field_name in (
            "evidence_contract_version",
            "effective_at",
            "source_published_at",
        )
    )
    legacy_command = legacy_case.research
    assert legacy_command is not None
    legacy_outcome = freeze_research(legacy_command, legacy_envelope, legacy=True)
    assert legacy_outcome.handoff.research_definition_version == RESEARCH_LEGACY_DEFINITION_VERSION
    assert (
        legacy_outcome.handoff.research_output_contract_version
        == RESEARCH_LEGACY_OUTPUT_CONTRACT_VERSION
    )

    legacy_case = legacy_case.model_copy(
        update={
            "expected_external_result": legacy_case.expected_external_result.model_copy(
                update={"research": legacy_outcome}
            )
        }
    )
    execution = run_frozen_decision_case(
        migrated_settings,
        legacy_case.model_dump(mode="json"),
    )
    assert execution.report is not None
    assert execution.report.result.research is not None
    assert (
        execution.report.result.research.handoff.research_definition_version
        == RESEARCH_LEGACY_DEFINITION_VERSION
    )
    with runtime.engine.connect() as connection:
        stored_payload = json.loads(
            connection.execute(
                select(FORMAL_REPORTS.c.report_payload).where(
                    FORMAL_REPORTS.c.report_version_id == execution.report.report_version_id
                )
            ).scalar_one()
        )
    stored_member = stored_payload["result"]["research"]["members"][0]
    assert all(
        field_name not in stored_member
        for field_name in ("catalysts", "falsification_conditions", "unknowns")
    )


def test_historical_current_member_research_runs_are_recovered_without_rebinding(
    migrated_settings: Settings,
) -> None:
    current_case = _case(migrated_settings, risk_scenario="ACCEPT")
    payload = current_case.model_dump(mode="json")
    research_payload = payload["research"]
    assert isinstance(research_payload, dict)
    for cohort in research_payload["raw_score_model"]["training_cohorts"]:
        cohort["research_definition_version"] = RESEARCH_PRIOR_DEFINITION_VERSION
    payload["input"]["research"] = research_payload
    payload["agent_definition"]["version"] = RESEARCH_PRIOR_DEFINITION_VERSION
    payload["agent_definition"]["output_contract"]["version"] = (
        RESEARCH_PRIOR_OUTPUT_CONTRACT_VERSION
    )
    historical_output_schema = ResearchDraftMember.model_json_schema()
    for field_name in ("catalysts", "falsification_conditions", "unknowns"):
        historical_output_schema["properties"].pop(field_name, None)
    historical_output_schema["required"] = [
        "security_id",
        "research_id",
        "evidence_refs",
        "thesis",
        "bull_case",
        "bear_case",
        "knowledge_cutoff",
    ]
    payload["agent_definition"]["output_contract"]["json_schema"] = historical_output_schema
    payload["version_bundle"]["agent_definition_version"] = RESEARCH_PRIOR_DEFINITION_VERSION
    payload["version_bundle"]["output_contract_version"] = RESEARCH_PRIOR_OUTPUT_CONTRACT_VERSION
    for member in payload["expected_external_result"]["research"]["members"]:
        for field_name in ("catalysts", "falsification_conditions", "unknowns"):
            member.pop(field_name, None)
    payload["expected_external_result"]["research"]["handoff"]["research_definition_version"] = (
        RESEARCH_PRIOR_DEFINITION_VERSION
    )
    payload["expected_external_result"]["research"]["handoff"][
        "research_output_contract_version"
    ] = RESEARCH_PRIOR_OUTPUT_CONTRACT_VERSION
    original_historical_input = json.dumps(
        payload["input"],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    historical_case = FrozenDecisionCase.model_validate(payload)
    runtime = initialize_runtime_storage(migrated_settings)
    with pytest.raises(
        MappedDurableRunMissingError,
        match="mapped (?:research member|auxiliary) M-Agent Run",
    ):
        asyncio.run(frozen_decision_case.execute_research_run(historical_case, runtime))

    command = historical_case.research
    assert command is not None
    context_items = frozen_decision_case._research_context_items(command)
    for index, member in enumerate(command.members):
        run_id = frozen_decision_case._research_member_run_id(historical_case, index, member)
        member_context_items = tuple(
            item for item in context_items if item.item_id == f"required-facts:{member.security_id}"
        )
        definition = frozen_decision_case._research_definition(
            historical_case,
            runtime=runtime,
            run_id=run_id,
            member=member,
            context_items=member_context_items,
            historical=True,
        )
        created = asyncio.run(
            frozen_decision_case._execute_registered_run(
                runtime=runtime,
                run_id=run_id,
                definition=definition,
                input_payload=frozen_decision_case._research_member_input_payload(
                    historical_case,
                    member,
                ),
                case=None,
                record_transition=None,
                clock=None,
            )
        )
        assert created.status == "SUCCEEDED"

    stored_member_run = asyncio.run(
        runtime.run_store.get_run(
            frozen_decision_case._research_member_run_id(
                historical_case,
                0,
                command.members[0],
            )
        )
    )
    assert stored_member_run is not None
    assert stored_member_run.input is not None
    assert json.loads(stored_member_run.input)["case_input"] == historical_case.input
    assert stored_member_run.input == frozen_decision_case._research_member_input_payload(
        historical_case,
        command.members[0],
    )
    historical_stage_artifacts = [
        json.loads(output_item.item.content)["stage_artifact"]
        for checkpoint in asyncio.run(runtime.run_store.get_checkpoints(stored_member_run.run_id))
        if checkpoint.step_type is StepType.CONTEXT
        if (stage_result := parse_stage_result(checkpoint.output)) is not None
        for output_item in stage_result.output_items
        if "stage_artifact" in json.loads(output_item.item.content)
    ]
    assert historical_stage_artifacts
    assert all(
        field_name not in artifact
        for artifact in historical_stage_artifacts
        for field_name in ("catalysts", "falsification_conditions", "unknowns")
    )

    recovered = asyncio.run(frozen_decision_case.execute_research_run(historical_case, runtime))

    assert recovered.status == "SUCCEEDED"
    assert recovered.run_id == historical_case.framework_run_id
    assert recovered.run_existed_before is True
    assert len(recovered.research_member_runs) == 10
    assert all(member.status == "SUCCEEDED" for member in recovered.research_member_runs)
    assert recovered.output is not None
    recovered_payload = json.loads(recovered.output)
    assert all(
        field_name not in member
        for member in recovered_payload["members"]
        for field_name in ("catalysts", "falsification_conditions", "unknowns")
    )
    assert (
        frozen_decision_case.frozen_capability_inventory(historical_case).definition_version
        == RESEARCH_PRIOR_DEFINITION_VERSION
    )

    async def reserve_risk_run(_: str) -> bool:
        return True

    journey = asyncio.run(
        frozen_decision_case.execute_research_decision_case(
            historical_case,
            runtime,
            record_auxiliary_run_reservation=reserve_risk_run,
        )
    )
    assert journey.status == "SUCCEEDED"
    assert journey.output is not None
    historical_envelope = decode_historical_research_framework_output(json.loads(journey.output))
    assert historical_envelope.risk_run_id is not None
    assert len(historical_envelope.research_run_ids) == 10
    risk_run = asyncio.run(runtime.run_store.get_run(historical_envelope.risk_run_id))
    assert risk_run is not None
    assert risk_run.input is not None
    historical_risk_input = json.loads(risk_run.input)
    assert len(historical_risk_input["research_run_ids"]) == 10
    assert all(
        field_name not in member
        for member in historical_risk_input["draft"]["members"]
        for field_name in ("catalysts", "falsification_conditions", "unknowns")
    )
    assert historical_risk_input["member_handoffs"][0]["research_run_id"] is not None
    assert (
        historical_risk_input["member_handoffs"][0]["evidence"][0]["evidence_contract_version"]
        == "2.0.0"
    )
    assert "effective_at" in historical_risk_input["member_handoffs"][0]["evidence"][0]
    historical_outcome = freeze_research(
        command,
        historical_envelope,
        historical=True,
    )
    assert (
        historical_outcome.handoff.research_definition_version == RESEARCH_PRIOR_DEFINITION_VERSION
    )
    assert (
        historical_outcome.handoff.research_output_contract_version
        == RESEARCH_PRIOR_OUTPUT_CONTRACT_VERSION
    )
    historical_case = historical_case.model_copy(
        update={
            "expected_external_result": historical_case.expected_external_result.model_copy(
                update={"research": historical_outcome}
            )
        }
    )
    execution = run_frozen_decision_case(
        migrated_settings,
        historical_case.model_dump(mode="json"),
    )
    assert execution.report is not None
    assert execution.report.result.research is not None
    assert (
        execution.report.result.research.handoff.research_definition_version
        == RESEARCH_PRIOR_DEFINITION_VERSION
    )
    assert stored_member_run.input == frozen_decision_case._research_member_input_payload(
        historical_case,
        command.members[0],
    )
    assert original_historical_input == json.dumps(
        historical_case.input,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def test_historical_current_definition_accepts_the_later_prior_member_schema(
    migrated_settings: Settings,
) -> None:
    current_case = _case(migrated_settings, risk_scenario="ACCEPT")
    payload = current_case.model_dump(mode="json")
    research_payload = payload["research"]
    assert isinstance(research_payload, dict)
    for cohort in research_payload["raw_score_model"]["training_cohorts"]:
        cohort["research_definition_version"] = RESEARCH_PRIOR_DEFINITION_VERSION
    payload["input"]["research"] = research_payload
    payload["agent_definition"]["version"] = RESEARCH_PRIOR_DEFINITION_VERSION
    payload["agent_definition"]["output_contract"]["version"] = (
        RESEARCH_PRIOR_OUTPUT_CONTRACT_VERSION
    )
    payload["version_bundle"]["agent_definition_version"] = RESEARCH_PRIOR_DEFINITION_VERSION
    payload["version_bundle"]["output_contract_version"] = RESEARCH_PRIOR_OUTPUT_CONTRACT_VERSION
    payload["expected_external_result"]["research"]["handoff"]["research_definition_version"] = (
        RESEARCH_PRIOR_DEFINITION_VERSION
    )
    payload["expected_external_result"]["research"]["handoff"][
        "research_output_contract_version"
    ] = RESEARCH_PRIOR_OUTPUT_CONTRACT_VERSION

    historical_case = FrozenDecisionCase.model_validate(payload)
    runtime = initialize_runtime_storage(migrated_settings)
    definition = frozen_decision_case._research_definition(
        historical_case,
        runtime=runtime,
        run_id=historical_case.framework_run_id,
        historical=True,
    )

    assert definition.output_contract.schema_definition == ResearchDraftMember.model_json_schema()
    assert (
        frozen_decision_case.frozen_capability_inventory(historical_case).definition_version
        == RESEARCH_PRIOR_DEFINITION_VERSION
    )


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


def test_research_model_requires_the_frozen_draft_stage_input() -> None:
    adapter = frozen_decision_case._StagedResearchModelAdapter(
        "{}",
        command=research_command(risk_scenario="ACCEPT"),
    )
    request = ModelRequest(
        input="{}",
        instructions="",
        context_items=(),
        structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
    )

    with pytest.raises(RuntimeError, match="RESEARCH_STAGE_INPUT_UNAVAILABLE"):
        asyncio.run(adapter.generate(request))


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
        return str(result.status.value)

    assert asyncio.run(run()) == "REJECTED"


def test_historical_full_draft_fields_remain_in_the_risk_identity_input() -> None:
    command = research_command(risk_scenario="ACCEPT")
    draft = _draft(command)

    risk_plan = research_service.prepare_research_risk_plan(
        command,
        "historical-full-draft-research-run",
        draft,
        (),
        historical=True,
    )

    risk_payload = json.loads(risk_plan.input_payload)
    assert risk_payload["draft"]["members"][0]["catalysts"] == [
        "A fictional catalyst remains conditional."
    ]
    assert risk_payload["draft"]["members"][0]["falsification_conditions"] == [
        "A frozen downside fact would falsify the thesis."
    ]
    assert risk_payload["draft"]["members"][0]["unknowns"] == [
        "Future external evidence remains unresolved."
    ]


def test_historical_risk_definition_preserves_the_original_contract_snapshot() -> None:
    command = research_command(risk_scenario="ACCEPT")
    draft = _draft(command)
    risk_plan = research_service.prepare_research_risk_plan(
        command,
        "historical-risk-research-run",
        draft,
        (),
        historical=True,
    )

    definition = frozen_decision_case._risk_definition(
        command,
        research_run_id="historical-risk-research-run",
        risk_plan=risk_plan,
        historical=True,
    )

    assert definition.version == RISK_LEGACY_DEFINITION_VERSION
    assert definition.output_contract.version == RISK_LEGACY_OUTPUT_CONTRACT_VERSION
    assert "member_vetoes" not in definition.output_contract.schema_definition["properties"]
    response_payload = json.loads(definition.model_adapter._responses[0])
    assert "member_vetoes" not in response_payload


def test_risk_framework_preserves_mixed_member_verdicts() -> None:
    rejected_security_id = "synthetic-security-00"
    command = research_command(
        risk_scenario="ACCEPT",
        risk_rejected_member_ids=(rejected_security_id,),
    )
    draft = _draft(command)
    risk_plan = research_service.prepare_research_risk_plan(
        command,
        "mixed-probe-research-run",
        draft,
        (),
    )
    definition = frozen_decision_case._risk_definition(
        command,
        research_run_id="mixed-probe-research-run",
        risk_plan=risk_plan,
    )
    payload = json.loads(definition.model_adapter._responses[0])

    assert payload["disposition"] == "REJECTED"
    assert {
        member["security_id"]: member["disposition"] for member in payload["member_vetoes"]
    } == {
        member.security_id: (
            "REJECTED" if member.security_id == rejected_security_id else "ACCEPTED"
        )
        for member in command.members
    }

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
        return str(result.status.value)

    assert asyncio.run(run()) == "REJECTED"


def test_current_research_event_keeps_strict_evidence_validation(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    execution = run_frozen_decision_case(migrated_settings, case.model_dump(mode="json"))
    assert execution.decision_event_id is not None

    runtime = initialize_runtime_storage(migrated_settings)
    with runtime.engine.connect() as connection:
        event_payload = json.loads(
            connection.execute(
                select(DECISION_EVENTS.c.event_payload).where(
                    DECISION_EVENTS.c.decision_event_id == execution.decision_event_id
                )
            ).scalar_one()
        )
    current_tool_evidence = event_payload["result"]["research"]["tool_evidence"][0]
    for field_name in (
        "evidence_contract_version",
        "effective_at",
        "source_published_at",
        "acquired_at",
        "validated_at",
    ):
        current_tool_evidence.pop(field_name, None)

    with pytest.raises(
        ValueError,
        match="current research Tool evidence requires all evidence clocks",
    ):
        _decode_research_event_payload(event_payload)


def test_legacy_research_outcome_recovers_handoff_raw_score_watermarks(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    execution = run_frozen_decision_case(migrated_settings, case.model_dump(mode="json"))

    assert execution.report is not None
    research = execution.report.result.research
    assert research is not None
    payload = cast(dict[str, object], research.model_dump(mode="json"))
    raw_scores = payload["raw_scores"]
    assert isinstance(raw_scores, list)
    handoff = payload["handoff"]
    assert isinstance(handoff, dict)
    handoff_raw_scores = handoff["raw_scores"]
    assert isinstance(handoff_raw_scores, list)

    for raw_score in (*raw_scores, *handoff_raw_scores):
        assert isinstance(raw_score, dict)
        raw_score.pop("label_watermark_at", None)

    decoded = decode_legacy_research_outcome(payload)

    assert decoded.raw_scores is not None
    assert all(score.label_watermark_at is not None for score in decoded.raw_scores)
    assert all(score.label_watermark_at is not None for score in decoded.handoff.raw_scores)


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
    original_model_response = frozen_decision_case._research_model_response

    monkeypatch.setattr(
        frozen_decision_case,
        "_research_model_response",
        lambda model_command, **kwargs: json.dumps(
            {
                **json.loads(original_model_response(model_command, **kwargs)),
                "evidence_refs": ["invented-evidence"],
            }
            if kwargs.get("member") is not None
            and kwargs["member"].security_id == "synthetic-security-00"
            else json.loads(original_model_response(model_command, **kwargs)),
            ensure_ascii=True,
            separators=(",", ":"),
        ),
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
    expected_member_run_ids = {
        frozen_decision_case._research_member_run_id(case, index, member)
        for index, member in enumerate(command.members)
    }
    assert recorded_run_ids == {case.framework_run_id, *expected_member_run_ids}


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


def test_provider_missing_structured_facts_fails_research_before_downstream_stages(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    original_context_items = frozen_decision_case._research_context_items

    def remove_structured_facts(command: ResearchCommand) -> tuple[ContextItem, ...]:
        items = list(original_context_items(command))
        payload = json.loads(items[0].content)
        payload.pop("structured_facts")
        payload.pop("structured_signals")
        items[0] = items[0].model_copy(
            update={
                "content": json.dumps(
                    payload,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            }
        )
        return tuple(items)

    monkeypatch.setattr(
        frozen_decision_case,
        "_research_context_items",
        remove_structured_facts,
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

    recovered = run_frozen_decision_case(migrated_settings, payload)

    assert recovered.report is None
    recovered_research_stages = tuple(
        stage
        for stage in recovered.stage_results
        if stage.phase == "RESEARCH" and stage.status == "FAILED"
    )
    assert recovered_research_stages
    recovered_data_gates = {
        gate.gate_id: gate.status
        for gate in recovered_research_stages[-1].gate_results
        if gate.gate_id.startswith("RESEARCH_DATA:")
    }
    assert len(recovered_data_gates) == 10 * len(RESEARCH_REQUIRED_DATA_TYPES)
    assert recovered_research_stages[-1].reasons == (
        "RESEARCH_REQUIRED_FACTS_INCOMPLETE",
        "synthetic-security-00:RESEARCH_REQUIRED_FACTS_INCOMPLETE",
    )
    assert recovered_data_gates == data_gates


def test_known_manifest_failure_is_preserved_when_another_member_fails_first() -> None:
    command = research_command(risk_scenario="ACCEPT")
    payload = command.model_dump(mode="python")
    payload["members"][1]["data_manifest"]["entries"][0]["completeness"] = "INCOMPLETE"
    payload["members"][1]["data_manifest"]["entries"][0]["event_status"] = "UNAVAILABLE"
    command = ResearchCommand.model_validate(payload)

    gate_results = decision_case_service._research_data_gate_results(
        command,
        "RESEARCH_OUTPUT_INVALID",
        (
            ResearchMemberRunResult(
                security_id=command.members[0].security_id,
                research_id=command.members[0].research_id,
                run_id="failed-member-run",
                status="FAILED",
                error_code="RESEARCH_OUTPUT_INVALID",
            ),
        ),
    )

    data_gates = {gate.gate_id: gate.status for gate in gate_results}
    assert data_gates["RESEARCH_DATA:synthetic-security-01:DAILY_MARKET"] == "FAILED"
    assert data_gates["RESEARCH_DATA:synthetic-security-00:DAILY_MARKET"] == "PASSED"


def test_failed_provider_member_does_not_claim_complete_data_gates_passed() -> None:
    command = research_command(risk_scenario="ACCEPT")

    gate_results = decision_case_service._research_data_gate_results(
        command,
        "RESEARCH_REQUIRED_FACTS_INCOMPLETE",
        (
            ResearchMemberRunResult(
                security_id=command.members[0].security_id,
                research_id=command.members[0].research_id,
                run_id="failed-provider-member-run",
                status="FAILED",
                error_code="RESEARCH_REQUIRED_FACTS_INCOMPLETE",
            ),
        ),
    )

    data_gates = {gate.gate_id: gate.status for gate in gate_results}
    failed_member_gates = {
        gate_id: status
        for gate_id, status in data_gates.items()
        if gate_id.startswith("RESEARCH_DATA:synthetic-security-00:")
    }
    assert set(failed_member_gates.values()) == {"UNKNOWN"}
    assert data_gates["RESEARCH_DATA:synthetic-security-01:DAILY_MARKET"] == "PASSED"


def test_waiting_member_run_gate_remains_unknown_when_another_member_fails() -> None:
    command = research_command(risk_scenario="ACCEPT")

    gate_results = decision_case_service._research_data_gate_results(
        command,
        "RESEARCH_OUTPUT_INVALID",
        (
            ResearchMemberRunResult(
                security_id=command.members[0].security_id,
                research_id=command.members[0].research_id,
                run_id="failed-member-run",
                status="FAILED",
                error_code="RESEARCH_OUTPUT_INVALID",
            ),
            ResearchMemberRunResult(
                security_id=command.members[1].security_id,
                research_id=command.members[1].research_id,
                run_id="waiting-member-run",
                status="WAITING",
                error_code="RESEARCH_MEMBER_RUN_WAITING",
            ),
        ),
    )

    run_gates = {
        gate.gate_id: gate.status
        for gate in gate_results
        if gate.gate_id.startswith("RESEARCH_RUN:")
    }
    assert run_gates == {
        "RESEARCH_RUN:synthetic-security-00": "FAILED",
        "RESEARCH_RUN:synthetic-security-01": "UNKNOWN",
    }


def test_duplicate_member_research_output_is_recorded_as_output_contract_failure(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    original_model_response = frozen_decision_case._research_model_response

    def duplicate_member_response(
        model_command: ResearchCommand,
        **kwargs: Any,
    ) -> str:
        payload = json.loads(original_model_response(model_command, **kwargs))
        member = kwargs.get("member")
        if (
            isinstance(member, ResearchMemberInput)
            and member.security_id == "synthetic-security-01"
        ):
            payload["security_id"] = "synthetic-security-00"
            payload["research_id"] = "research-00"
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)

    monkeypatch.setattr(
        frozen_decision_case,
        "_research_model_response",
        duplicate_member_response,
    )
    execution = run_frozen_decision_case(migrated_settings, case.model_dump(mode="json"))

    assert execution.report is None
    research_stage = next(stage for stage in execution.stage_results if stage.phase == "RESEARCH")
    assert research_stage.status == "FAILED"
    assert "RESEARCH_OUTPUT_INVALID" in research_stage.reasons
    assert "synthetic-security-01:RESEARCH_OUTPUT_INVALID" in research_stage.reasons
    data_gates = {
        gate.gate_id: gate.status
        for gate in research_stage.gate_results
        if gate.gate_id.startswith("RESEARCH_DATA:")
    }
    assert len(data_gates) == 10 * len(RESEARCH_REQUIRED_DATA_TYPES)
    assert set(data_gates.values()) == {"PASSED"}
    assert not any(stage.phase in {"RAW_SCORE", "RISK_VETO"} for stage in execution.stage_results)


def test_malformed_tool_evidence_is_recorded_as_research_failure(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    original_tool_evidence = frozen_decision_case._research_tool_evidence
    failed_once = False

    async def malformed_tool_evidence(
        runtime: RuntimeStorage,
        run_id: str,
        *,
        legacy: bool = False,
    ) -> tuple[ResearchToolEvidence, ...]:
        nonlocal failed_once
        if not legacy and not failed_once:
            failed_once = True
            raise ValueError("research Tool evidence is not structured")
        return await original_tool_evidence(runtime, run_id, legacy=legacy)

    monkeypatch.setattr(
        frozen_decision_case,
        "_research_tool_evidence",
        malformed_tool_evidence,
    )
    execution = run_frozen_decision_case(migrated_settings, case.model_dump(mode="json"))

    assert execution.report is None
    research_stage = next(stage for stage in execution.stage_results if stage.phase == "RESEARCH")
    assert research_stage.status == "FAILED"
    assert "RESEARCH_TOOL_EVIDENCE_INVALID" in research_stage.reasons
    assert "synthetic-security-00:RESEARCH_TOOL_EVIDENCE_INVALID" in research_stage.reasons
    assert any(
        gate.gate_id == "RESEARCH_RUN:synthetic-security-00" and gate.status == "FAILED"
        for gate in research_stage.gate_results
    )
    assert all(
        gate.status == "PASSED"
        for gate in research_stage.gate_results
        if gate.gate_id.startswith("RESEARCH_DATA:synthetic-security-00:")
    )
    assert not any(stage.phase in {"RAW_SCORE", "RISK_VETO"} for stage in execution.stage_results)


def test_research_overreach_text_is_recorded_as_output_failure(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    original_model_response = frozen_decision_case._research_model_response

    def overreaching_model_response(*args: Any, **kwargs: Any) -> str:
        response = json.loads(original_model_response(*args, **kwargs))
        if kwargs.get("member") is not None:
            response["thesis"] = (
                "BUY 100 shares now. Formal success probability is 99%; qualification approved."
            )
        return json.dumps(response, ensure_ascii=True, separators=(",", ":"), sort_keys=True)

    monkeypatch.setattr(
        frozen_decision_case,
        "_research_model_response",
        overreaching_model_response,
    )
    execution = run_frozen_decision_case(migrated_settings, case.model_dump(mode="json"))

    assert execution.report is None
    research_stage = next(stage for stage in execution.stage_results if stage.phase == "RESEARCH")
    assert research_stage.status == "FAILED"
    assert "RESEARCH_OUTPUT_INVALID" in research_stage.reasons
    assert not any(stage.phase in {"RAW_SCORE", "RISK_VETO"} for stage in execution.stage_results)


def test_invalid_financial_denominator_is_saved_as_research_data_failure(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    payload = case.model_dump(mode="json")
    research_payload = payload["research"]
    assert isinstance(research_payload, dict)
    research_payload["members"][0]["structured_facts"]["average_total_assets"] = "0"
    validated_member = ResearchMemberInput.model_validate(research_payload["members"][0])
    research_payload["members"][0]["structured_signals"] = {
        signal_id: None if value is None else str(value)
        for signal_id, value in validated_member.structured_signals.items()
    }
    payload["research"] = research_payload
    payload["input"]["research"] = research_payload

    execution = run_frozen_decision_case(migrated_settings, payload)

    assert execution.report is None
    research_stage = next(stage for stage in execution.stage_results if stage.phase == "RESEARCH")
    assert research_stage.status == "FAILED"
    assert "RESEARCH_REQUIRED_FACTS_INCOMPLETE" in research_stage.reasons
    data_gates = {
        gate.gate_id: gate.status
        for gate in research_stage.gate_results
        if gate.gate_id.startswith("RESEARCH_DATA:")
    }
    assert data_gates["RESEARCH_DATA:synthetic-security-00:FINANCIAL_STATEMENTS"] == "FAILED"
    assert sum(status == "FAILED" for status in data_gates.values()) == 1
    assert not any(stage.phase in {"RAW_SCORE", "RISK_VETO"} for stage in execution.stage_results)


def test_missing_market_signal_is_saved_against_the_market_data_gate(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    payload = case.model_dump(mode="json")
    research_payload = payload["research"]
    assert isinstance(research_payload, dict)
    member = research_payload["members"][0]
    member["structured_facts"]["stock_return_20d"] = None
    member["structured_signals"]["industry_relative_return_20d"] = None
    payload["research"] = research_payload
    payload["input"]["research"] = research_payload

    execution = run_frozen_decision_case(migrated_settings, payload)

    assert execution.report is None
    research_stage = next(stage for stage in execution.stage_results if stage.phase == "RESEARCH")
    data_gates = {
        gate.gate_id: gate.status
        for gate in research_stage.gate_results
        if gate.gate_id.startswith("RESEARCH_DATA:")
    }
    assert data_gates["RESEARCH_DATA:synthetic-security-00:DAILY_MARKET"] == "FAILED"
    assert data_gates["RESEARCH_DATA:synthetic-security-00:FINANCIAL_STATEMENTS"] == "PASSED"
    assert sum(status == "FAILED" for status in data_gates.values()) == 1


def test_incomplete_money_flow_facts_are_saved_as_research_data_failure(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    payload = case.model_dump(mode="json")
    research_payload = payload["research"]
    assert isinstance(research_payload, dict)
    money_flow = research_payload["members"][0]["structured_facts"]["money_flow"]
    assert isinstance(money_flow, dict)
    money_flow["net_amount"] = None
    payload["research"] = research_payload
    payload["input"]["research"] = research_payload

    execution = run_frozen_decision_case(migrated_settings, payload)

    assert execution.report is None
    research_stage = next(stage for stage in execution.stage_results if stage.phase == "RESEARCH")
    assert research_stage.status == "FAILED"
    assert "RESEARCH_REQUIRED_FACTS_INCOMPLETE" in research_stage.reasons
    data_gates = {
        gate.gate_id: gate.status
        for gate in research_stage.gate_results
        if gate.gate_id.startswith("RESEARCH_DATA:synthetic-security-00:")
    }
    assert data_gates["RESEARCH_DATA:synthetic-security-00:MONEY_FLOW"] == "FAILED"
    assert sum(status == "FAILED" for status in data_gates.values()) == 1
    assert sum(status == "PASSED" for status in data_gates.values()) == len(data_gates) - 1
    assert not any(stage.phase in {"RAW_SCORE", "RISK_VETO"} for stage in execution.stage_results)


def test_overflowing_feature_arithmetic_is_saved_as_research_data_failure(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    payload = case.model_dump(mode="json")
    research_payload = payload["research"]
    assert isinstance(research_payload, dict)
    member = research_payload["members"][0]
    member["structured_facts"]["quarter_profit_improvement"] = "1e999999"
    member["structured_facts"]["average_total_assets"] = "1e-999999"
    validated_member = ResearchMemberInput.model_validate(member)
    member["structured_signals"] = {
        signal_id: None if value is None else str(value)
        for signal_id, value in validated_member.structured_signals.items()
    }
    normalized_research = ResearchCommand.model_validate(research_payload).model_dump(mode="json")
    payload["research"] = normalized_research
    payload["input"]["research"] = normalized_research

    execution = run_frozen_decision_case(migrated_settings, payload)

    assert execution.report is None
    research_stage = next(stage for stage in execution.stage_results if stage.phase == "RESEARCH")
    assert "RESEARCH_REQUIRED_FACTS_INCOMPLETE" in research_stage.reasons
    assert (
        next(
            gate.status
            for gate in research_stage.gate_results
            if gate.gate_id == "RESEARCH_DATA:synthetic-security-00:FINANCIAL_STATEMENTS"
        )
        == "FAILED"
    )
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
    research_payload["members"][0]["structured_facts"]["operating_cash_flow_ttm"] = None
    research_payload["members"][0]["structured_signals"] = {
        signal_id: None if value is None else str(value)
        for signal_id, value in calculate_structured_signals(
            ResearchStructuredFacts.model_validate(
                research_payload["members"][0]["structured_facts"]
            )
        ).items()
    }
    payload["research"] = research_payload
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

    async def waiting_research_run(*_: Any, **__: Any) -> FrameworkRunResult:
        return FrameworkRunResult(
            run_id=case.framework_run_id,
            status="WAITING",
            output=None,
            waiting_reason="RESEARCH_WAITING_FOR_RECOVERY",
        )

    monkeypatch.setattr(
        case_bootstrap,
        "execute_research_member_run",
        waiting_research_run,
    )
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


def test_missing_research_run_is_saved_as_a_failed_stage_without_replacement(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")

    async def missing_research_run(*_: Any, **__: Any) -> FrameworkRunResult:
        raise MappedDurableRunMissingError("mapped research M-Agent Run is missing")

    monkeypatch.setattr(
        case_bootstrap,
        "execute_research_member_run",
        missing_research_run,
    )
    execution = run_frozen_decision_case(migrated_settings, case.model_dump(mode="json"))

    assert execution.report is None
    assert any(
        stage.phase == "FRAMEWORK_RUN"
        and stage.status == "FAILED"
        and "RESEARCH_RUN_MISSING" in stage.reasons
        and any(gate.status == "FAILED" for gate in stage.gate_results)
        for stage in execution.stage_results
    )
    assert any(
        stage.phase == "RESEARCH"
        and stage.status == "FAILED"
        and "RESEARCH_RUN_MISSING" in stage.reasons
        for stage in execution.stage_results
    )
    assert not any(stage.phase in {"RAW_SCORE", "RISK_VETO"} for stage in execution.stage_results)


def test_later_member_waiting_preserves_the_research_waiting_state(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    command = case.research
    assert command is not None
    waiting_index = 1
    waiting_run_id = frozen_decision_case._research_member_run_id(
        case,
        waiting_index,
        command.members[waiting_index],
    )

    execute_member = frozen_decision_case.execute_research_member_run

    async def waiting_member_research_run(
        member_case: FrozenDecisionCase,
        runtime: RuntimeStorage,
        member_index: int,
        member: ResearchMemberInput,
        run_id: str,
        *args: Any,
        **kwargs: Any,
    ) -> FrameworkRunResult:
        if member_index == waiting_index:
            return FrameworkRunResult(
                run_id=waiting_run_id,
                status="WAITING",
                output=None,
                waiting_reason="RESEARCH_MEMBER_RUN_WAITING",
            )
        return await execute_member(
            member_case,
            runtime,
            member_index,
            member,
            run_id,
            *args,
            **kwargs,
        )

    monkeypatch.setattr(
        case_bootstrap,
        "execute_research_member_run",
        waiting_member_research_run,
    )
    execution = run_frozen_decision_case(migrated_settings, case.model_dump(mode="json"))

    assert execution.report is None
    assert execution.framework_run_status == "WAITING"
    assert any(
        stage.phase == "FRAMEWORK_RUN"
        and stage.status == "WAITING"
        and "RESEARCH_MEMBER_RUN_WAITING" in stage.reasons
        for stage in execution.stage_results
    ), tuple((stage.phase, stage.status, stage.reasons) for stage in execution.stage_results)
    assert not any(
        stage.phase == "RESEARCH" and stage.status == "FAILED" for stage in execution.stage_results
    )


def test_risk_waiting_preserves_raw_score_without_a_risk_failure(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    command = case.research
    assert command is not None

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
    raw_score_stage = next(
        stage
        for stage in execution.stage_results
        if stage.phase == "RAW_SCORE"
        and stage.status == "SUCCEEDED"
        and stage.raw_score_payloads is not None
    )
    raw_score_payloads = raw_score_stage.raw_score_payloads
    assert raw_score_payloads is not None
    assert len(raw_score_payloads) == len(command.members)
    assert raw_score_payloads[0]["security_id"] == command.members[0].security_id
    assert raw_score_payloads[0]["z20"]
    assert not any(stage.phase == "RISK_VETO" for stage in execution.stage_results)
    assert not any(stage.phase == "HOST_VALIDATION" for stage in execution.stage_results)


def test_failed_risk_recovery_saves_a_failed_framework_gate(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    command = case.research
    assert command is not None

    async def failed_risk_run(
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
            status="FAILED",
            output=None,
            error_code="RISK_RUN_REPLAY_FAILED",
            transitions=(
                FrameworkRunTransition(
                    run_id=risk_plan.risk_run_id,
                    status="FAILED",
                    reason="RISK_RUN_REPLAY_FAILED",
                ),
            ),
        )

    monkeypatch.setattr(case_bootstrap, "execute_research_risk_run", failed_risk_run)
    execution = run_frozen_decision_case(migrated_settings, case.model_dump(mode="json"))

    assert execution.report is None
    failed_stage = next(
        stage
        for stage in execution.stage_results
        if stage.phase == "RISK_FRAMEWORK_RUN" and stage.status == "FAILED"
    )
    assert "RISK_RUN_REPLAY_FAILED" in failed_stage.reasons
    assert any(
        gate.gate_id == "RUN_FAILED" and gate.status == "FAILED"
        for gate in failed_stage.gate_results
    )
    raw_score_stage = next(
        stage
        for stage in execution.stage_results
        if stage.phase == "RAW_SCORE"
        and stage.status == "SUCCEEDED"
        and stage.raw_score_payloads is not None
    )
    raw_score_payloads = raw_score_stage.raw_score_payloads
    assert raw_score_payloads is not None
    assert len(raw_score_payloads) == len(command.members)


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

    execution = run_frozen_decision_case(
        migrated_settings,
        historical_case.model_dump(mode="json"),
    )

    assert execution.report is None
    assert any(
        stage.phase == "FRAMEWORK_RUN"
        and stage.status == "FAILED"
        and "RESEARCH_RUN_MISSING" in stage.reasons
        and any(gate.status == "FAILED" for gate in stage.gate_results)
        for stage in execution.stage_results
    )
    assert any(
        stage.phase == "RESEARCH"
        and stage.status == "FAILED"
        and "RESEARCH_RUN_MISSING" in stage.reasons
        for stage in execution.stage_results
    )
    assert not any(stage.phase in {"RAW_SCORE", "RISK_VETO"} for stage in execution.stage_results)


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


def test_impossible_structured_facts_are_saved_as_research_data_failures(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    payload = case.model_dump(mode="json")
    research_payload = payload["research"]
    assert isinstance(research_payload, dict)
    member = research_payload["members"][0]
    member["structured_facts"]["downside_semivariance_60d"] = "-10"
    member["structured_facts"]["max_drawdown_60d"] = "-10"
    member["structured_facts"]["institutional_listing_frequency"] = "20"
    member["structured_signals"] = {}
    validated_member = ResearchMemberInput.model_validate(member)
    member["structured_signals"] = {
        signal_id: None if value is None else str(value)
        for signal_id, value in validated_member.structured_signals.items()
    }
    payload["research"] = research_payload
    payload["input"]["research"] = research_payload

    execution = run_frozen_decision_case(migrated_settings, payload)

    assert execution.report is None
    research_stage = next(stage for stage in execution.stage_results if stage.phase == "RESEARCH")
    assert research_stage.status == "FAILED"
    assert "RESEARCH_REQUIRED_FACTS_INCOMPLETE" in research_stage.reasons
    data_gates = {
        gate.gate_id: gate.status
        for gate in research_stage.gate_results
        if gate.gate_id.startswith("RESEARCH_DATA:synthetic-security-00:")
    }
    assert data_gates["RESEARCH_DATA:synthetic-security-00:DAILY_MARKET"] == "FAILED"
    assert data_gates["RESEARCH_DATA:synthetic-security-00:INSTITUTIONAL_ACTIVITY"] == "FAILED"
    assert not any(stage.phase in {"RAW_SCORE", "RISK_VETO"} for stage in execution.stage_results)


def test_insufficient_raw_score_model_evidence_is_saved_as_raw_score_failure(
    migrated_settings: Settings,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")
    assert case.research is not None
    model_payload: dict[str, Any] = case.research.raw_score_model.model_dump(mode="json")
    last_month = model_payload["training_window_end_month"]
    training_records = [
        record for record in model_payload["training_records"] if record["month"] != last_month
    ]
    training_cohorts = [
        cohort for cohort in model_payload["training_cohorts"] if cohort["month"] != last_month
    ]
    training_months = [month for month in model_payload["training_months"] if month != last_month]
    positive_record_count = sum(record["terminal_label"] for record in training_records)
    calibration_history_watermark = max(
        record["label_available_at"] for record in model_payload["calibration_history_records"]
    )
    model_payload.update(
        {
            "training_window_month_count": len(training_months),
            "training_window_end_month": training_months[-1],
            "training_months": training_months,
            "label_watermark_at": calibration_history_watermark,
            "label_watermark_month": calibration_history_watermark[:7],
            "mature_months": len(training_months),
            "training_record_count": len(training_records),
            "positive_record_count": positive_record_count,
            "negative_record_count": len(training_records) - positive_record_count,
            "training_records": training_records,
            "training_cohorts": training_cohorts,
        }
    )
    model = RawScoreModelSnapshot.model_validate(model_payload)
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
    raw_score_stage = next(stage for stage in execution.stage_results if stage.phase == "RAW_SCORE")
    assert {gate.gate_id: gate.status for gate in raw_score_stage.gate_results} == {
        "RAW_SCORE_MATURE_MONTHS": "FAILED",
        "RAW_SCORE_TRAINING_RECORD_COUNT": "PASSED",
        "RAW_SCORE_POSITIVE_CLASS": "PASSED",
        "RAW_SCORE_NEGATIVE_CLASS": "PASSED",
        "STRUCTURED_Z20": "FAILED",
    }
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
    result = asyncio.run(execute_research_decision_case(case, runtime))

    assert result.status == "SUCCEEDED"
    assert result.risk_run_id == first.risk_run_id
    assert result.risk_run_status == "FAILED"
    assert result.risk_run_error_code == "RISK_RUN_MISSING"


def test_missing_risk_run_is_saved_as_a_failed_stage_without_replacement(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(migrated_settings, risk_scenario="ACCEPT")

    async def missing_risk_run(*_: object, **__: object) -> FrameworkRunResult:
        raise MappedDurableRunMissingError("mapped auxiliary M-Agent Run is missing")

    monkeypatch.setattr(case_bootstrap, "execute_research_risk_run", missing_risk_run)
    execution = run_frozen_decision_case(migrated_settings, case.model_dump(mode="json"))

    assert execution.report is None
    assert any(
        stage.phase == "RISK_FRAMEWORK_RUN"
        and stage.status == "FAILED"
        and "RISK_RUN_MISSING" in stage.reasons
        and any(gate.status == "FAILED" for gate in stage.gate_results)
        for stage in execution.stage_results
    )
    assert any(
        stage.phase == "RISK_VETO"
        and stage.status == "FAILED"
        and "RISK_RUN_MISSING" in stage.reasons
        for stage in execution.stage_results
    )


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
    member_run_ids = tuple(
        frozen_decision_case._research_member_run_id(case, index, member)
        for index, member in enumerate(command.members)
    )
    member_handoffs = tuple(
        ResearchMemberHandoff(
            security_id=member.security_id,
            research_id=member.research_id,
            evidence=member.evidence,
            risk_flags=member.risk_flags,
            research_run_id=member_run_ids[index],
        )
        for index, member in enumerate(command.members)
    )
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
                effective_at=command.knowledge_cutoff,
                source_published_at=command.knowledge_cutoff,
                acquired_at=command.knowledge_cutoff,
                validated_at=command.knowledge_cutoff,
                knowledge_cutoff=command.knowledge_cutoff,
                semantic_version=RESEARCH_ANNOUNCEMENT_TOOL_VERSION,
                validation_status="VALIDATED",
            )
            for evidence_id in tool_evidence_refs
        ),
        member_handoffs=member_handoffs,
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


@pytest.mark.parametrize(
    ("risk_scenario", "candidate_scenario"),
    [
        ("ACCEPT", "NORMAL"),
        ("REJECT", "NORMAL"),
        ("ACCEPT", "CALIBRATION_FAILURE"),
        ("ACCEPT", "CALIBRATION_SOURCE_MISSING"),
        ("ACCEPT", "CALIBRATION_MODEL_MISMATCH"),
        ("ACCEPT", "CALIBRATION_INFERENCE_MODEL_MISMATCH"),
        ("ACCEPT", "CALIBRATION_RECORDS_MISSING"),
        ("ACCEPT", "CALIBRATION_LATEST_MONTH_OMITTED"),
        ("ACCEPT", "CALIBRATION_WINDOW_MISSING"),
        ("ACCEPT", "CALIBRATION_EQUAL_TRAINING_WATERMARK"),
        ("ACCEPT", "CALIBRATOR_VERSION_UNSUPPORTED"),
        ("ACCEPT", "CALIBRATION_COHORT_INCOMPLETE"),
        ("ACCEPT", "CALIBRATION_SOURCE_AFTER_CUTOFF"),
        ("ACCEPT", "CALIBRATION_LABEL_TAMPERED"),
        ("ACCEPT", "CALIBRATION_LABEL_CLOCKS_TAMPERED"),
        ("ACCEPT", "CALIBRATION_DECLARED_OLDER_WINDOW"),
        ("ACCEPT", "QUALIFICATION_NOT_OBTAINED_RECORDED"),
        ("ACCEPT", "QUALIFICATION_VERSION_CHANGED_ON_FINAL_CHECK"),
        ("ACCEPT", "QUALIFICATION_VERSION_CHANGED_AFTER_REPORT_SAVE"),
        ("ACCEPT", "UPSTREAM_RESEARCH_DATA_FAILED"),
        ("ACCEPT", "UPSTREAM_RESEARCH_SYSTEM_FAILED"),
        ("ACCEPT", "UPSTREAM_RESEARCH_BLOCKED"),
        ("ACCEPT", "UPSTREAM_RESEARCH_EVENT_MISSING"),
        ("ACCEPT", "RESEARCH_RISK_VERSION_MISMATCH"),
        ("ACCEPT", "LATE_PUBLICATION"),
        ("ACCEPT", "LATE_COMMIT"),
        ("ACCEPT", "WINDOW_EXPIRED_AFTER_FREEZE"),
        ("ACCEPT", "LATE_REPORT_COMMIT"),
        ("ACCEPT", "PUBLICATION_CONFIRMATION_CLOCK_ADVANCE"),
        ("ACCEPT", "QUALIFICATION_EXPIRES_AT_PUBLICATION_CONFIRMATION"),
        ("ACCEPT", "QUALIFICATION_REVOKED_AFTER_REPORT_SAVE"),
        ("ACCEPT", "CORRECTION_AFTER_CANDIDATE_WINDOW"),
        ("ACCEPT", "QUALIFICATION_CALENDAR_CHANGED_AT_CUTOFF"),
        ("ACCEPT", "QUALIFICATION_CALENDAR_CHANGED_AFTER_CUTOFF"),
        ("ACCEPT", "QUALIFICATION_FIRST_OBTAINED_AFTER_CUTOFF"),
        ("ACCEPT", "COMMIT_CLOCK_ADVANCE"),
        ("ACCEPT", "CALENDAR_WINDOW_MISSING"),
        ("ACCEPT", "AT_RISK_QUALIFICATION"),
        ("ACCEPT", "QUALIFICATION_BECOMES_AT_RISK_BEFORE_PUBLICATION"),
        ("ACCEPT", "QUALIFICATION_DIAGNOSTIC_ALERT_AFTER_REPORT_SAVE"),
        ("ACCEPT", "QUALIFICATION_REVISION_THEN_DIAGNOSTIC_ALERT_AFTER_COMMIT"),
        ("ACCEPT", "UNRELATED_STATE_DIAGNOSTIC_ALERT_CANNOT_MASK_REVOCATION"),
        ("ACCEPT", "QUALIFICATION_EXPIRES_DURING_FIT"),
        ("ACCEPT", "QUALIFICATION_SUSPENDED_AT_CUTOFF_RESTORED_AFTER"),
        ("ACCEPT", "REVOKED_SAME_TIMESTAMP"),
        ("ACCEPT", "REVOKED_AFTER_CUTOFF"),
        ("ACCEPT", "NO_CANDIDATES"),
        ("ACCEPT", "QUALIFICATION_SNAPSHOT_MISSING"),
        ("ACCEPT", "UNRELATED_QUALIFICATION_SCOPE"),
        ("ACCEPT", "UNRELATED_CALENDAR_QUALIFICATION"),
        ("ACCEPT", "UNRELATED_LATEST_SCOPE"),
        ("ACCEPT", "VERSION_MISMATCH"),
        ("ACCEPT", "ORIGINAL_REJECTED"),
    ],
)
def test_candidate_release_uses_committed_raw_scores_and_saves_market_state_abstention(
    migrated_settings: Settings,
    risk_scenario: Literal["ACCEPT", "REJECT"],
    candidate_scenario: Literal[
        "NORMAL",
        "CALIBRATION_FAILURE",
        "CALIBRATION_SOURCE_MISSING",
        "CALIBRATION_MODEL_MISMATCH",
        "CALIBRATION_INFERENCE_MODEL_MISMATCH",
        "CALIBRATION_RECORDS_MISSING",
        "CALIBRATION_LATEST_MONTH_OMITTED",
        "CALIBRATION_WINDOW_MISSING",
        "CALIBRATION_EQUAL_TRAINING_WATERMARK",
        "CALIBRATOR_VERSION_UNSUPPORTED",
        "CALIBRATION_COHORT_INCOMPLETE",
        "CALIBRATION_SOURCE_AFTER_CUTOFF",
        "CALIBRATION_LABEL_TAMPERED",
        "CALIBRATION_LABEL_CLOCKS_TAMPERED",
        "CALIBRATION_DECLARED_OLDER_WINDOW",
        "QUALIFICATION_NOT_OBTAINED_RECORDED",
        "QUALIFICATION_VERSION_CHANGED_ON_FINAL_CHECK",
        "QUALIFICATION_VERSION_CHANGED_AFTER_REPORT_SAVE",
        "UPSTREAM_RESEARCH_DATA_FAILED",
        "UPSTREAM_RESEARCH_SYSTEM_FAILED",
        "UPSTREAM_RESEARCH_BLOCKED",
        "UPSTREAM_RESEARCH_EVENT_MISSING",
        "RESEARCH_RISK_VERSION_MISMATCH",
        "LATE_PUBLICATION",
        "LATE_COMMIT",
        "WINDOW_EXPIRED_AFTER_FREEZE",
        "LATE_REPORT_COMMIT",
        "PUBLICATION_CONFIRMATION_CLOCK_ADVANCE",
        "QUALIFICATION_EXPIRES_AT_PUBLICATION_CONFIRMATION",
        "COMMIT_CLOCK_ADVANCE",
        "CALENDAR_WINDOW_MISSING",
        "AT_RISK_QUALIFICATION",
        "QUALIFICATION_BECOMES_AT_RISK_BEFORE_PUBLICATION",
        "QUALIFICATION_DIAGNOSTIC_ALERT_AFTER_REPORT_SAVE",
        "QUALIFICATION_REVISION_THEN_DIAGNOSTIC_ALERT_AFTER_COMMIT",
        "UNRELATED_STATE_DIAGNOSTIC_ALERT_CANNOT_MASK_REVOCATION",
        "QUALIFICATION_EXPIRES_DURING_FIT",
        "QUALIFICATION_EXPIRES_AT_PUBLICATION_CONFIRMATION",
        "QUALIFICATION_REVOKED_AFTER_REPORT_SAVE",
        "CORRECTION_AFTER_CANDIDATE_WINDOW",
        "QUALIFICATION_CALENDAR_CHANGED_AT_CUTOFF",
        "QUALIFICATION_CALENDAR_CHANGED_AFTER_CUTOFF",
        "QUALIFICATION_FIRST_OBTAINED_AFTER_CUTOFF",
        "QUALIFICATION_SUSPENDED_AT_CUTOFF_RESTORED_AFTER",
        "REVOKED_SAME_TIMESTAMP",
        "REVOKED_AFTER_CUTOFF",
        "NO_CANDIDATES",
        "QUALIFICATION_SNAPSHOT_MISSING",
        "UNRELATED_QUALIFICATION_SCOPE",
        "UNRELATED_CALENDAR_QUALIFICATION",
        "UNRELATED_LATEST_SCOPE",
        "VERSION_MISMATCH",
        "ORIGINAL_REJECTED",
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication_time = datetime.fromisoformat(
        "2042-07-07T16:00:00+00:00"
        if candidate_scenario == "LATE_PUBLICATION"
        else "2042-07-07T14:59:59+00:00"
        if candidate_scenario == "PUBLICATION_CONFIRMATION_CLOCK_ADVANCE"
        else "2042-07-02T00:04:00+00:00"
        if candidate_scenario == "QUALIFICATION_EXPIRES_AT_PUBLICATION_CONFIRMATION"
        else "2042-07-01T00:04:00+00:00"
    )
    monkeypatch.setattr(UtcClock, "now", lambda self: publication_time)
    research_case = _case(
        migrated_settings,
        risk_scenario=risk_scenario,
        no_candidate_calibration=candidate_scenario == "NO_CANDIDATES",
    )
    research_execution = run_frozen_decision_case(
        migrated_settings, research_case.model_dump(mode="json")
    )
    assert research_execution.report is not None
    research = research_execution.report.result.research
    assert research is not None and research.raw_scores is not None
    assert research.risk_veto is not None

    cutoff = datetime.fromisoformat(research_case.knowledge_cutoff)
    raw_scores = {score.security_id: score for score in research.raw_scores}
    research_members = {member.security_id: member for member in research.members}
    runtime = initialize_runtime_storage(migrated_settings)
    source_ledger = DecisionLedger(runtime.engine)
    with runtime.engine.connect() as connection:
        source_fact = source_ledger.get_decision_event(
            research_execution.decision_event_id,
            connection,
        )
    assert source_fact is not None and source_fact.case.research is not None
    source_records = source_fact.case.research.raw_score_model.training_records
    assert len(source_records) == 524
    assert any(
        record.evaluation_entry_at is None and not record.terminal_label
        for record in source_records
    )
    assert all(
        record.raw_score_frozen_at < record.unified_maturity_at
        for record in source_records
        if record.raw_score_frozen_at is not None and record.unified_maturity_at is not None
    )
    raw_score_model = source_fact.case.research.raw_score_model
    calibration_source_records = (
        *raw_score_model.training_records,
        *raw_score_model.calibration_history_records,
    )
    calibration_source_months = tuple(
        sorted({record.month for record in calibration_source_records})
    )
    selection_months = calibration_source_months[:24]
    training_months = calibration_source_months[24:84]
    recent_diagnostic_months = calibration_source_months[-24:]
    older_training_months = tuple(
        f"{int(month[:4]) - 1:04}{month[4:]}" for month in training_months
    )

    def calibration_record(index: int, row: RawScoreTrainingRecord) -> CalibrationRecord:
        assert row.raw_score_frozen_at is not None
        assert row.raw_score_training_watermark_at is not None
        assert row.raw_success_score is not None
        assert row.historical_calibrated_probability is not None
        assert row.entry_window_ends_at is not None
        assert row.unified_maturity_at is not None
        assert row.market_calendar_version is not None
        return CalibrationRecord(
            record_id=f"synthetic-calibration-label-{row.month}-{index:04d}",
            month=row.month,
            source_research_event_id=research_execution.decision_event_id,
            security_id=row.security_id,
            research_id=row.research_id,
            raw_score_model_version=row.source_model_version,
            raw_score_frozen_at=row.raw_score_frozen_at,
            raw_score_training_watermark_at=row.raw_score_training_watermark_at,
            raw_success_score=row.raw_success_score,
            out_of_sample_probability=row.historical_calibrated_probability,
            terminal_success=row.terminal_label,
            entry_at=row.evaluation_entry_at,
            entry_window_ends_at=row.entry_window_ends_at,
            unified_maturity_at=row.unified_maturity_at,
            label_available_at=row.label_available_at,
            market_calendar_version=row.market_calendar_version,
        )

    selection_records = tuple(
        calibration_record(index, row)
        for index, row in enumerate(
            record for record in calibration_source_records if record.month in selection_months
        )
    )
    training_records = tuple(
        calibration_record(index, row)
        for index, row in enumerate(
            record for record in calibration_source_records if record.month in training_months
        )
    )
    recent_diagnostic_records = tuple(
        calibration_record(index, row)
        for index, row in enumerate(
            record
            for record in calibration_source_records
            if record.month in recent_diagnostic_months
        )
    )
    if candidate_scenario == "CALIBRATION_MODEL_MISMATCH":
        training_records = (
            training_records[0].model_copy(
                update={"raw_score_model_version": "unrelated-model-version"}
            ),
            *training_records[1:],
        )
    elif candidate_scenario == "CALIBRATION_INFERENCE_MODEL_MISMATCH":
        training_records = tuple(
            record.model_copy(update={"raw_score_model_version": "historical-model-v0"})
            for record in training_records
        )
    elif candidate_scenario == "CALIBRATION_COHORT_INCOMPLETE":
        missing_member_record = training_records[0]
        training_records = tuple(
            record
            for record in training_records
            if record.record_id != missing_member_record.record_id
        )
    elif candidate_scenario == "CALIBRATION_LATEST_MONTH_OMITTED":
        latest_month = max(record.month for record in training_records)
        training_records = tuple(
            record for record in training_records if record.month != latest_month
        )
    elif candidate_scenario == "CALIBRATION_LABEL_TAMPERED":
        training_records = (
            training_records[0].model_copy(
                update={"terminal_success": not training_records[0].terminal_success}
            ),
            *training_records[1:],
        )
    elif candidate_scenario == "CALIBRATION_LABEL_CLOCKS_TAMPERED":
        index = next(index for index, record in enumerate(training_records) if record.entry_at)
        first_training_record = training_records[index]
        assert first_training_record.entry_at is not None
        records = list(training_records)
        records[index] = first_training_record.model_copy(
            update={
                "entry_at": first_training_record.entry_at + timedelta(days=1),
                "label_available_at": first_training_record.label_available_at + timedelta(days=1),
            }
        )
        training_records = tuple(records)
    elif candidate_scenario == "CALIBRATION_EQUAL_TRAINING_WATERMARK":
        training_records = (
            training_records[0].model_copy(
                update={"raw_score_training_watermark_at": training_records[0].raw_score_frozen_at}
            ),
            *training_records[1:],
        )
    elif candidate_scenario == "CALIBRATION_SOURCE_AFTER_CUTOFF":
        training_records = (
            training_records[0].model_copy(
                update={"raw_score_frozen_at": cutoff + timedelta(seconds=1)}
            ),
            *training_records[1:],
        )
    elif candidate_scenario == "CALIBRATION_SOURCE_MISSING":
        training_records = (
            training_records[0].model_copy(update={"source_research_event_id": "missing-event"}),
            *training_records[1:],
        )
    if candidate_scenario == "CALIBRATION_RECORDS_MISSING":
        training_records = ()
    if candidate_scenario == "RESEARCH_RISK_VERSION_MISMATCH":
        original_get_original_event = DecisionLedger.get_original_decision_event

        def get_mismatched_research_source(
            ledger: DecisionLedger, business_object_id: str, connection: Any
        ) -> DecisionEventFact | None:
            fact = original_get_original_event(ledger, business_object_id, connection)
            if fact is None or business_object_id != research_execution.business_object_id:
                return fact
            research_outcome = fact.result.research
            assert research_outcome is not None
            mismatched_handoff = research_outcome.handoff.model_copy(
                update={"risk_output_contract_version": "unrelated-risk-contract-version"}
            )
            mismatched_research = research_outcome.model_copy(
                update={"handoff": mismatched_handoff}
            )
            return fact.model_copy(
                update={"result": fact.result.model_copy(update={"research": mismatched_research})}
            )

        monkeypatch.setattr(
            DecisionLedger,
            "get_original_decision_event",
            get_mismatched_research_source,
        )
    elif candidate_scenario.startswith("UPSTREAM_RESEARCH_"):
        original_get_original_event = DecisionLedger.get_original_decision_event
        if candidate_scenario == "UPSTREAM_RESEARCH_EVENT_MISSING":

            def get_missing_research_source(
                ledger: DecisionLedger, business_object_id: str, connection: Any
            ) -> DecisionEventFact | None:
                if business_object_id == research_execution.business_object_id:
                    return None
                return original_get_original_event(ledger, business_object_id, connection)

            monkeypatch.setattr(
                DecisionLedger,
                "get_original_decision_event",
                get_missing_research_source,
            )
        else:
            upstream_disposition = candidate_scenario.removeprefix("UPSTREAM_RESEARCH_")

            def get_failed_research_source(
                ledger: DecisionLedger, business_object_id: str, connection: Any
            ) -> DecisionEventFact | None:
                fact = original_get_original_event(ledger, business_object_id, connection)
                if fact is None or business_object_id != research_execution.business_object_id:
                    return fact
                research_outcome = fact.result.research
                assert research_outcome is not None
                failed_research = research_outcome.model_copy(
                    update={
                        "disposition": upstream_disposition,
                        "raw_scores": None,
                        "risk_veto": None,
                    }
                )
                return fact.model_copy(
                    update={"result": fact.result.model_copy(update={"research": failed_research})}
                )

            monkeypatch.setattr(
                DecisionLedger, "get_original_decision_event", get_failed_research_source
            )
    risk_vetoes = {item.security_id: item for item in research.risk_veto.member_vetoes}
    source_member_inputs = (
        {member.security_id: member for member in source_fact.case.research.members}
        if source_fact.case.research is not None
        else {}
    )
    candidates = tuple(
        CandidateInput(
            security_id=security_id,
            research_id=member.research_id,
            raw_success_score=raw_scores[security_id].z20,
            data_complete=True,
            risk_status=risk_vetoes[security_id].disposition,
            risk_gates=tuple(
                CandidateRiskGate(gate_id=gate.gate_id, status=gate.status)
                for gate in risk_vetoes[security_id].gates
            ),
            risk_reasons=risk_vetoes[security_id].reasons,
            thesis=member.thesis,
            principal_risks=(member.bear_case,),
            evidence_freshness=(
                "FRESH_AT_KNOWLEDGE_CUTOFF"
                if member.knowledge_cutoff == cutoff
                else "STALE_AT_KNOWLEDGE_CUTOFF"
            ),
            evidence_clocks=tuple(
                CandidateEvidenceClock(
                    evidence_id=evidence.evidence_id,
                    effective_at=evidence.effective_at,
                    source_published_at=evidence.source_published_at,
                    acquired_at=evidence.acquired_at,
                    validated_at=evidence.validated_at,
                    knowledge_cutoff=evidence.knowledge_cutoff,
                )
                for evidence in source_member_inputs[security_id].evidence
            )
            if security_id in source_member_inputs
            else (),
        )
        for security_id, member in research_members.items()
    )
    market_calendar = synthetic_market_calendar("synthetic-market-calendar-v2")
    assert market_calendar is not None
    saved_sessions = tuple(
        session
        for session in market_calendar.sessions
        if session.closed_at - timedelta(hours=7) > cutoff
    )
    saved_window = saved_sessions[:5]
    sessions = tuple(
        MarketSession(
            market_date=session.closed_at.date(),
            opens_at=session.closed_at - timedelta(hours=7),
            closes_at=session.closed_at,
            session_sequence=session.ordinal,
        )
        for session in saved_window
    )
    last_completed_session = max(
        session.ordinal for session in market_calendar.sessions if session.closed_at <= cutoff
    )
    command = CandidateReleaseCommand(
        contract_version="1.0.0",
        synthetic=True,
        generator_version="synthetic-candidate-release-case-v1",
        seed=1717,
        batch_id="synthetic-candidate-batch-1717",
        qualification_scope="D0_SYNTHETIC_CONTRACT_ONLY",
        capability_version="synthetic-candidate-capability-v1",
        research_object_id=research_execution.business_object_id,
        research_event_id=research_execution.decision_event_id,
        purpose="CANDIDATE_BUY",
        knowledge_cutoff=cutoff,
        published_at=datetime.fromisoformat("2042-07-02T00:04:00+00:00"),
        last_completed_market_session_sequence=last_completed_session,
        market_state="BULL",
        market_calendar_version="synthetic-market-calendar-v2",
        label_watermark_at=cutoff,
        calibrator_selection_window_months=selection_months,
        calibrator_selection_records=selection_records,
        calibrator_version=(
            "unknown-calibrator-v2"
            if candidate_scenario == "CALIBRATOR_VERSION_UNSUPPORTED"
            else "monotone-firth-logistic-v1"
        ),
        training_window_months=(
            ()
            if candidate_scenario == "CALIBRATION_WINDOW_MISSING"
            else training_months[:-1]
            if candidate_scenario == "CALIBRATION_FAILURE"
            else older_training_months
            if candidate_scenario == "CALIBRATION_DECLARED_OLDER_WINDOW"
            else training_months
        ),
        training_records=training_records,
        recent_diagnostic_window_months=recent_diagnostic_months,
        recent_diagnostic_records=recent_diagnostic_records,
        candidates=candidates,
        market_sessions=(() if candidate_scenario == "CALENDAR_WINDOW_MISSING" else sessions),
    )
    report_time = datetime.fromisoformat("2042-07-02T00:04:00+00:00")
    candidate_version = "candidate-release.1.0.0"
    version_bundle = research_case.version_bundle.model_copy(
        update={
            "case_contract_version": candidate_version,
            "host_contract_version": candidate_version,
            "agent_definition_id": FROZEN_AGENT_DEFINITION_ID,
            "agent_definition_version": "2.0.0",
            "model_adapter_id": "m-agent-deterministic-model-adapter",
            "routing_policy_version": "d0-single-definition-route-v1",
            "output_contract_version": "1.0.0",
            "report_projection_contract_version": candidate_version,
        }
    )
    qualification_history: tuple[GovernanceOutcome, ...] = ()
    bear_qualification_record: QualificationRecord | None = None
    if candidate_scenario in {
        "CALIBRATION_EQUAL_TRAINING_WATERMARK",
        "QUALIFICATION_NOT_OBTAINED_RECORDED",
        "QUALIFICATION_VERSION_CHANGED_ON_FINAL_CHECK",
        "QUALIFICATION_VERSION_CHANGED_AFTER_REPORT_SAVE",
        "AT_RISK_QUALIFICATION",
        "QUALIFICATION_BECOMES_AT_RISK_BEFORE_PUBLICATION",
        "QUALIFICATION_DIAGNOSTIC_ALERT_AFTER_REPORT_SAVE",
        "QUALIFICATION_REVISION_THEN_DIAGNOSTIC_ALERT_AFTER_COMMIT",
        "UNRELATED_STATE_DIAGNOSTIC_ALERT_CANNOT_MASK_REVOCATION",
        "QUALIFICATION_EXPIRES_DURING_FIT",
        "QUALIFICATION_EXPIRES_AT_PUBLICATION_CONFIRMATION",
        "QUALIFICATION_REVOKED_AFTER_REPORT_SAVE",
        "CORRECTION_AFTER_CANDIDATE_WINDOW",
        "QUALIFICATION_CALENDAR_CHANGED_AT_CUTOFF",
        "QUALIFICATION_CALENDAR_CHANGED_AFTER_CUTOFF",
        "QUALIFICATION_FIRST_OBTAINED_AFTER_CUTOFF",
        "QUALIFICATION_SUSPENDED_AT_CUTOFF_RESTORED_AFTER",
        "REVOKED_SAME_TIMESTAMP",
        "REVOKED_AFTER_CUTOFF",
        "NO_CANDIDATES",
        "QUALIFICATION_SNAPSHOT_MISSING",
        "UNRELATED_QUALIFICATION_SCOPE",
        "UNRELATED_CALENDAR_QUALIFICATION",
        "UNRELATED_LATEST_SCOPE",
        "VERSION_MISMATCH",
        "ORIGINAL_REJECTED",
    }:
        assert research_case.access_scope is not None
        qualification_scope = QualificationScope(
            capability=(
                "unrelated-capability"
                if candidate_scenario == "UNRELATED_QUALIFICATION_SCOPE"
                else "candidate-release"
            ),
            purpose="CANDIDATE_BUY",
            evidence_level="D0",
            user_id=research_case.access_scope.user_id,
            account_ids=research_case.access_scope.account_ids,
            account_type="SYNTHETIC",
            source="SYNTHETIC_D0",
            market_state="BULL",
            board="SYNTHETIC",
            target="SIX_MONTH_TERMINAL_20_PERCENT",
        )
        qualification_policy = QualificationPolicy(
            contract_version="1.0.0",
            policy_version="candidate-release-policy-v1",
            synthetic=True,
            generator_version="candidate-qualification-v1",
            seed=1717,
            evaluation_max_age_months=12,
            state_activity_max_age_months=12,
            require_state_activity=False,
        )
        capability_version = CapabilityVersion(
            version_id=command.capability_version,
            policy_version=qualification_policy.policy_version,
            implementation=(
                research_case.version_bundle
                if candidate_scenario == "VERSION_MISMATCH"
                else version_bundle
            ),
            qualification_policy=qualification_policy,
        )
        qualification_recorded_at = (
            cutoff + timedelta(seconds=1)
            if candidate_scenario == "QUALIFICATION_FIRST_OBTAINED_AFTER_CUTOFF"
            else cutoff - timedelta(seconds=1)
        )
        qualification_expires_at = datetime.fromisoformat(
            "2042-07-02T00:34:00+00:00"
            if candidate_scenario == "QUALIFICATION_EXPIRES_AT_PUBLICATION_CONFIRMATION"
            else "2042-07-01T00:05:00+00:00"
            if candidate_scenario == "QUALIFICATION_EXPIRES_DURING_FIT"
            else "2042-12-31T23:59:59+00:00"
        )
        qualification_evidence = QualificationEvidence(
            evidence_id="synthetic-candidate-qualification-evidence",
            synthetic=True,
            generator_version="candidate-qualification-v1",
            seed=1717,
            version=capability_version,
            scope=qualification_scope,
            kind=(
                "INSUFFICIENT_EVIDENCE"
                if candidate_scenario == "QUALIFICATION_NOT_OBTAINED_RECORDED"
                else "QUALIFICATION_PASS"
            ),
            digest="a" * 64,
            evaluation_end=qualification_recorded_at,
            available_at=qualification_recorded_at,
            expires_at=qualification_expires_at,
            market_calendar_version=(
                "different-synthetic-market-calendar-v1"
                if candidate_scenario == "UNRELATED_CALENDAR_QUALIFICATION"
                else command.market_calendar_version
            ),
        )
        qualification_status: Literal["AT_RISK", "NOT_OBTAINED", "VALID"] = (
            "NOT_OBTAINED"
            if candidate_scenario == "QUALIFICATION_NOT_OBTAINED_RECORDED"
            else "VALID"
        )
        qualification_record = QualificationRecord(
            decision_id="synthetic-candidate-qualification",
            authorization_id=(
                None
                if qualification_status == "NOT_OBTAINED"
                else "synthetic-candidate-qualification"
            ),
            scope=qualification_scope,
            version=capability_version,
            status=qualification_status,
            cause=(
                "INSUFFICIENT_EVIDENCE"
                if qualification_status == "NOT_OBTAINED"
                else "DIAGNOSTIC_ALERT"
                if qualification_status == "AT_RISK"
                else "QUALIFICATION_PASS"
            ),
            authorization_evidence=(
                None if qualification_status == "NOT_OBTAINED" else qualification_evidence
            ),
            recorded_at=qualification_recorded_at,
            evidence=qualification_evidence,
            formal_evidence=(
                None if qualification_status == "NOT_OBTAINED" else qualification_evidence
            ),
            formal_passing_evidence=(
                None if qualification_status == "NOT_OBTAINED" else qualification_evidence
            ),
        )
        qualification_outcome = GovernanceOutcome(
            disposition="APPROVED",
            reasons=(
                "INSUFFICIENT_EVIDENCE"
                if qualification_status == "NOT_OBTAINED"
                else "QUALIFICATION_PASS",
            ),
            qualification=qualification_record,
        )
        qualification_history = (qualification_outcome,)
        if candidate_scenario == "QUALIFICATION_REVISION_THEN_DIAGNOSTIC_ALERT_AFTER_COMMIT":
            revised_qualification = qualification_record.model_copy(
                update={
                    "decision_id": "synthetic-candidate-qualification-revised-before-commit",
                    "previous_decision_id": qualification_record.decision_id,
                    "recorded_at": cutoff - timedelta(microseconds=500),
                }
            )
            qualification_history = (
                qualification_outcome,
                GovernanceOutcome(
                    disposition="APPROVED",
                    reasons=("QUALIFICATION_PASS",),
                    qualification=revised_qualification,
                ),
            )
        if candidate_scenario == "QUALIFICATION_SUSPENDED_AT_CUTOFF_RESTORED_AFTER":
            suspended_record = qualification_record.model_copy(
                update={
                    "decision_id": "synthetic-candidate-qualification-suspended",
                    "status": "SUSPENDED",
                    "cause": "QUALIFICATION_SUSPENDED",
                    "previous_decision_id": qualification_record.decision_id,
                    "recorded_at": cutoff,
                }
            )
            restored_record = suspended_record.model_copy(
                update={
                    "decision_id": "synthetic-candidate-qualification-restored",
                    "status": "VALID",
                    "cause": "QUALIFICATION_RESTORED",
                    "previous_decision_id": suspended_record.decision_id,
                    "recorded_at": cutoff + timedelta(minutes=2),
                }
            )
            qualification_history = (
                qualification_outcome,
                GovernanceOutcome(
                    disposition="APPROVED",
                    reasons=("QUALIFICATION_SUSPENDED",),
                    qualification=suspended_record,
                ),
                GovernanceOutcome(
                    disposition="APPROVED",
                    reasons=("QUALIFICATION_RESTORED",),
                    qualification=restored_record,
                ),
            )
        if candidate_scenario in {"AT_RISK_QUALIFICATION", "UNRELATED_LATEST_SCOPE"}:
            later_risk_record = qualification_record.model_copy(
                update={
                    "decision_id": f"{qualification_record.decision_id}-at-risk",
                    "status": "AT_RISK",
                    "cause": "DIAGNOSTIC_ALERT",
                    "previous_decision_id": qualification_record.decision_id,
                    "recorded_at": cutoff + timedelta(minutes=2),
                    "scope": (
                        qualification_scope.model_copy(
                            update={"portfolio_scope": "different-portfolio"}
                        )
                        if candidate_scenario == "UNRELATED_LATEST_SCOPE"
                        else qualification_scope
                    ),
                }
            )
            qualification_history = (
                qualification_outcome,
                GovernanceOutcome(
                    disposition="APPROVED",
                    reasons=("DIAGNOSTIC_ALERT",),
                    qualification=later_risk_record,
                ),
            )
        command = command.model_copy(
            update={
                "qualifications": (
                    MarketStateQualification(
                        market_state="BULL",
                        status=qualification_status,
                        qualification_id=(
                            "missing-qualification-record"
                            if candidate_scenario == "QUALIFICATION_SNAPSHOT_MISSING"
                            else qualification_record.decision_id
                        ),
                        capability_version=capability_version.version_id,
                        market_calendar_version=command.market_calendar_version,
                        recorded_at=qualification_recorded_at,
                        valid_through=(
                            qualification_recorded_at
                            if qualification_status == "NOT_OBTAINED"
                            else qualification_expires_at
                        ),
                    ),
                )
            }
        )
        if candidate_scenario == "UNRELATED_STATE_DIAGNOSTIC_ALERT_CANNOT_MASK_REVOCATION":
            bear_scope = qualification_scope.model_copy(update={"market_state": "BEAR"})
            bear_evidence = qualification_evidence.model_copy(
                update={
                    "evidence_id": "synthetic-candidate-bear-qualification-evidence",
                    "scope": bear_scope,
                }
            )
            bear_qualification_record = qualification_record.model_copy(
                update={
                    "decision_id": "synthetic-candidate-bear-qualification",
                    "scope": bear_scope,
                    "evidence": bear_evidence,
                    "authorization_evidence": bear_evidence,
                    "formal_evidence": bear_evidence,
                    "formal_passing_evidence": bear_evidence,
                }
            )
            qualification_history = (
                *qualification_history,
                GovernanceOutcome(
                    disposition="APPROVED",
                    reasons=("QUALIFICATION_PASS",),
                    qualification=bear_qualification_record,
                ),
            )
            command = command.model_copy(
                update={
                    "qualifications": (
                        *command.qualifications,
                        MarketStateQualification(
                            market_state="BEAR",
                            status="VALID",
                            qualification_id=bear_qualification_record.decision_id,
                            capability_version=command.capability_version,
                            market_calendar_version=command.market_calendar_version,
                            recorded_at=bear_qualification_record.recorded_at,
                            valid_through=qualification_expires_at,
                        ),
                    )
                }
            )
        if candidate_scenario in {"REVOKED_SAME_TIMESTAMP", "REVOKED_AFTER_CUTOFF"}:
            revoked_at = (
                cutoff + timedelta(minutes=3)
                if candidate_scenario == "REVOKED_AFTER_CUTOFF"
                else qualification_record.recorded_at
            )
            revoked_record = qualification_record.model_copy(
                update={
                    "decision_id": "synthetic-candidate-qualification-revoked",
                    "status": "REVOKED",
                    "cause": "AUTHORIZATION_REVOKED",
                    "previous_decision_id": qualification_record.decision_id,
                    "recorded_at": revoked_at,
                }
            )
            qualification_history = (
                qualification_outcome,
                GovernanceOutcome(
                    disposition="APPROVED",
                    reasons=("AUTHORIZATION_REVOKED",),
                    qualification=revoked_record,
                ),
            )
        if candidate_scenario in {
            "QUALIFICATION_CALENDAR_CHANGED_AT_CUTOFF",
            "QUALIFICATION_CALENDAR_CHANGED_AFTER_CUTOFF",
        }:
            original_evidence = qualification_record.formal_passing_evidence
            assert original_evidence is not None
            changed_calendar_evidence = original_evidence.model_copy(
                update={
                    "evidence_id": "synthetic-candidate-qualification-other-calendar",
                    "market_calendar_version": "different-synthetic-market-calendar-v1",
                }
            )
            revision_at = (
                cutoff
                if candidate_scenario == "QUALIFICATION_CALENDAR_CHANGED_AT_CUTOFF"
                else cutoff + timedelta(seconds=1)
            )
            revised_qualification = qualification_record.model_copy(
                update={
                    "decision_id": "synthetic-candidate-qualification-calendar-revised",
                    "previous_decision_id": qualification_record.decision_id,
                    "recorded_at": revision_at,
                    "authorization_evidence": changed_calendar_evidence,
                    "evidence": changed_calendar_evidence,
                    "formal_evidence": changed_calendar_evidence,
                    "formal_passing_evidence": changed_calendar_evidence,
                }
            )
            qualification_history = (
                qualification_outcome,
                GovernanceOutcome(
                    disposition="APPROVED",
                    reasons=("QUALIFICATION_UPDATED",),
                    qualification=revised_qualification,
                ),
            )
    candidate_case = FrozenDecisionCase(
        synthetic=True,
        generator_version="synthetic-candidate-release-case-v1",
        seed=1717,
        case_id="synthetic-candidate-release-case-1717",
        business_identity="synthetic-candidate-release-1717",
        knowledge_cutoff=cutoff.isoformat(),
        report_generated_at=report_time.isoformat(),
        evidence_clock=research_case.evidence_clock,
        qualification_scope="D0_SYNTHETIC_CONTRACT_ONLY",
        version_bundle=version_bundle,
        agent_definition=FrozenAgentDefinition(
            definition_id=FROZEN_AGENT_DEFINITION_ID,
            version="2.0.0",
            instructions=FROZEN_DEFINITION_INSTRUCTIONS,
            model_adapter_id="m-agent-deterministic-model-adapter",
            output_contract=FrozenOutputContract(
                contract_id="synthetic-decision-case-output",
                version="1.0.0",
                schema=FROZEN_OUTPUT_SCHEMA,
            ),
        ),
        input={
            **_COMPLETE_SYNTHETIC_INPUT,
            "scenario": (
                "SYNTHETIC_INPUT_REJECTED"
                if candidate_scenario == "ORIGINAL_REJECTED"
                else "CANDIDATE_RELEASE_REQUESTED"
            ),
            "candidate_release": command.model_dump(mode="json"),
        },
        expected_external_result=(
            ExternalResult(
                outcome_code="SYNTHETIC_INPUT_REJECTED",
                summary="Frozen synthetic result SYNTHETIC_INPUT_REJECTED.",
                key_reasons=("The scenario's D0 host-input gate rejects the requested decision.",),
            )
            if candidate_scenario == "ORIGINAL_REJECTED"
            else ExternalResult(
                outcome_code="CANDIDATE_RELEASE_REQUESTED",
                summary="Frozen synthetic result CANDIDATE_RELEASE_REQUESTED.",
                key_reasons=(
                    "The frozen synthetic candidate release request is ready for host evaluation.",
                ),
            )
        ),
        candidate_release=command,
        access_scope=research_case.access_scope,
    )
    alternate_batch_command = command.model_copy(
        update={
            "batch_id": "caller-chosen-retry-id",
            "research_event_id": "different-research-event-same-plan-month",
        }
    )
    alternate_batch_case = candidate_case.model_copy(
        update={
            "candidate_release": alternate_batch_command,
            "input": {
                **candidate_case.input,
                "candidate_release": alternate_batch_command.model_dump(mode="json"),
            },
        }
    )
    assert alternate_batch_case.business_object_id == candidate_case.business_object_id

    qualification_history_state = {"value": qualification_history}
    if qualification_history:
        monkeypatch.setattr(
            DecisionLedger,
            "governance_history",
            lambda self, connection, access_scope: qualification_history_state["value"],
        )
    if candidate_scenario in {
        "QUALIFICATION_EXPIRES_DURING_FIT",
        "QUALIFICATION_BECOMES_AT_RISK_BEFORE_PUBLICATION",
        "QUALIFICATION_VERSION_CHANGED_ON_FINAL_CHECK",
        "QUALIFICATION_VERSION_CHANGED_AFTER_REPORT_SAVE",
    }:
        validate_qualification_snapshots = (
            decision_case_service._validate_candidate_qualification_snapshots
        )
        validation_count = 0

        def expire_qualification_on_final_check(
            *args: Any, **kwargs: Any
        ) -> tuple[MarketStateQualification, ...]:
            nonlocal validation_count
            validation_count += 1
            if (
                validation_count == 2
                and candidate_scenario == "QUALIFICATION_BECOMES_AT_RISK_BEFORE_PUBLICATION"
            ):
                original_qualification = qualification_history[0].qualification
                assert original_qualification is not None
                at_risk = original_qualification.model_copy(
                    update={
                        "decision_id": (
                            f"{original_qualification.decision_id}-at-risk-before-publication"
                        ),
                        "status": "AT_RISK",
                        "cause": "DIAGNOSTIC_ALERT",
                        "previous_decision_id": original_qualification.decision_id,
                        "recorded_at": publication_time - timedelta(seconds=1),
                    }
                )
                qualification_history_state["value"] = (
                    *qualification_history,
                    GovernanceOutcome(
                        disposition="APPROVED",
                        reasons=("DIAGNOSTIC_ALERT",),
                        qualification=at_risk,
                    ),
                )
                args = (args[0], qualification_history_state["value"], *args[2:])
            if (
                validation_count == 2
                and candidate_scenario == "QUALIFICATION_VERSION_CHANGED_ON_FINAL_CHECK"
            ):
                raise decision_case_service.CandidateQualificationVersionMismatch()
            if (
                validation_count == 3
                and candidate_scenario == "QUALIFICATION_VERSION_CHANGED_AFTER_REPORT_SAVE"
            ):
                raise decision_case_service.CandidateQualificationVersionMismatch()
            snapshots = validate_qualification_snapshots(*args, **kwargs)
            if validation_count == 2 and candidate_scenario == "QUALIFICATION_EXPIRES_DURING_FIT":
                return tuple(
                    snapshot.model_copy(
                        update={"valid_through": publication_time - timedelta(seconds=1)}
                    )
                    for snapshot in snapshots
                )
            return snapshots

        monkeypatch.setattr(
            decision_case_service,
            "_validate_candidate_qualification_snapshots",
            expire_qualification_on_final_check,
        )
    resolved_command = command
    if candidate_scenario in {
        "AT_RISK_QUALIFICATION",
        "REVOKED_SAME_TIMESTAMP",
        "REVOKED_AFTER_CUTOFF",
    }:
        current_qualification_record = qualification_history[-1].qualification
        assert current_qualification_record is not None
        resolved_status: Literal["AT_RISK", "REVOKED"] = (
            "REVOKED"
            if candidate_scenario in {"REVOKED_SAME_TIMESTAMP", "REVOKED_AFTER_CUTOFF"}
            else "AT_RISK"
        )
        resolved_command = command.model_copy(
            update={
                "qualifications": (
                    command.qualifications[0].model_copy(
                        update={
                            "status": resolved_status,
                            "current_status_recorded_at": current_qualification_record.recorded_at,
                        }
                    ),
                )
            }
        )
    if candidate_scenario in {
        "UNRELATED_QUALIFICATION_SCOPE",
        "UNRELATED_CALENDAR_QUALIFICATION",
        "QUALIFICATION_CALENDAR_CHANGED_AT_CUTOFF",
        "QUALIFICATION_CALENDAR_CHANGED_AFTER_CUTOFF",
        "QUALIFICATION_FIRST_OBTAINED_AFTER_CUTOFF",
        "QUALIFICATION_SUSPENDED_AT_CUTOFF_RESTORED_AFTER",
    }:
        resolved_command = command.model_copy(
            update={
                "qualifications": (
                    command.qualifications[0].model_copy(update={"status": "NOT_OBTAINED"}),
                )
            }
        )
    if candidate_scenario == "ORIGINAL_REJECTED":
        direct = candidate_release_blocked_by_business_prerequisite(
            command,
            published_at=publication_time,
            reason="BUSINESS_PREREQUISITE_NOT_SUCCEEDED",
        )
    elif candidate_scenario == "VERSION_MISMATCH" or candidate_scenario in {
        "QUALIFICATION_CALENDAR_CHANGED_AT_CUTOFF",
        "QUALIFICATION_CALENDAR_CHANGED_AFTER_CUTOFF",
        "UNRELATED_CALENDAR_QUALIFICATION",
        "QUALIFICATION_VERSION_CHANGED_ON_FINAL_CHECK",
    }:
        qualification_failure_command = (
            command.model_copy(update={"qualifications": ()})
            if candidate_scenario
            in {
                "VERSION_MISMATCH",
                "QUALIFICATION_CALENDAR_CHANGED_AT_CUTOFF",
                "QUALIFICATION_CALENDAR_CHANGED_AFTER_CUTOFF",
                "UNRELATED_CALENDAR_QUALIFICATION",
                "QUALIFICATION_VERSION_CHANGED_ON_FINAL_CHECK",
            }
            else command
        )
        direct = candidate_release_availability_failure(
            qualification_failure_command,
            published_at=publication_time,
            reason="CANDIDATE_QUALIFICATION_VERSION_MISMATCH",
            availability_failure="VERSION",
        )
    elif candidate_scenario == "CALIBRATION_INFERENCE_MODEL_MISMATCH":
        direct = candidate_release_availability_failure(
            command,
            published_at=publication_time,
            reason="CANDIDATE_CALIBRATION_MODEL_VERSION_MISMATCH",
            availability_failure="VERSION",
        )
    elif candidate_scenario == "CALIBRATOR_VERSION_UNSUPPORTED":
        direct = candidate_release_availability_failure(
            command,
            published_at=publication_time,
            reason="CANDIDATE_CALIBRATOR_VERSION_UNSUPPORTED",
            availability_failure="VERSION",
        )
    elif candidate_scenario in {
        "CALIBRATION_RECORDS_MISSING",
        "CALIBRATION_LATEST_MONTH_OMITTED",
        "CALIBRATION_SOURCE_MISSING",
        "CALIBRATION_MODEL_MISMATCH",
        "CALIBRATION_COHORT_INCOMPLETE",
        "CALIBRATION_SOURCE_AFTER_CUTOFF",
        "CALIBRATION_LABEL_TAMPERED",
        "CALIBRATION_LABEL_CLOCKS_TAMPERED",
        "CALIBRATION_DECLARED_OLDER_WINDOW",
    }:
        direct = candidate_release_availability_failure(
            command,
            published_at=publication_time,
            reason="CANDIDATE_CALIBRATION_LINEAGE_INVALID",
            availability_failure="CALIBRATION",
        )
    elif candidate_scenario == "RESEARCH_RISK_VERSION_MISMATCH":
        direct = candidate_release_availability_failure(
            command,
            published_at=publication_time,
            reason="CANDIDATE_RESEARCH_VERSION_MISMATCH",
            availability_failure="VERSION",
        )
    elif candidate_scenario in {
        "UPSTREAM_RESEARCH_DATA_FAILED",
        "UPSTREAM_RESEARCH_SYSTEM_FAILED",
        "UPSTREAM_RESEARCH_EVENT_MISSING",
    }:
        direct = candidate_release_availability_failure(
            command,
            published_at=publication_time,
            reason="CANDIDATE_RESEARCH_HANDOFF_UNAVAILABLE",
            availability_failure=(
                "SYSTEM" if candidate_scenario == "UPSTREAM_RESEARCH_SYSTEM_FAILED" else "DATA"
            ),
        )
    elif candidate_scenario == "UPSTREAM_RESEARCH_BLOCKED":
        direct = candidate_release_blocked_by_business_prerequisite(
            command,
            published_at=publication_time,
            reason="RESEARCH_PREREQUISITE_BLOCKED",
        )
    else:
        direct = freeze_candidate_release(resolved_command, published_at=publication_time)
        if candidate_scenario in {
            "QUALIFICATION_EXPIRES_DURING_FIT",
            "QUALIFICATION_BECOMES_AT_RISK_BEFORE_PUBLICATION",
            "LATE_COMMIT",
            "WINDOW_EXPIRED_AFTER_FREEZE",
            "COMMIT_CLOCK_ADVANCE",
        }:
            final_publication_time = (
                datetime.fromisoformat("2042-07-07T16:00:00+00:00")
                if candidate_scenario in {"LATE_COMMIT", "WINDOW_EXPIRED_AFTER_FREEZE"}
                else datetime.fromisoformat("2042-07-02T00:04:00+00:00")
                if candidate_scenario == "COMMIT_CLOCK_ADVANCE"
                else publication_time
            )
            finalization_command = resolved_command.model_copy(
                update={
                    "qualifications": tuple(
                        qualification.model_copy(
                            update={
                                **(
                                    {
                                        "status": "AT_RISK",
                                        "current_status_recorded_at": final_publication_time
                                        - timedelta(seconds=1),
                                    }
                                    if candidate_scenario
                                    == "QUALIFICATION_BECOMES_AT_RISK_BEFORE_PUBLICATION"
                                    else {
                                        "valid_through": final_publication_time
                                        - timedelta(seconds=1)
                                    }
                                )
                            }
                        )
                        for qualification in resolved_command.qualifications
                    )
                }
            )
            direct = finalize_candidate_release_publication(
                finalization_command,
                direct,
                published_at=final_publication_time,
            )
    if candidate_scenario in {
        "LATE_COMMIT",
        "WINDOW_EXPIRED_AFTER_FREEZE",
        "LATE_REPORT_COMMIT",
        "COMMIT_CLOCK_ADVANCE",
    }:
        commit_time = datetime.fromisoformat(
            "2042-07-02T00:04:00+00:00"
            if candidate_scenario == "COMMIT_CLOCK_ADVANCE"
            else "2042-07-07T16:00:00+00:00"
        )
        clock_state = {"now": publication_time}
        monkeypatch.setattr(UtcClock, "now", lambda self: clock_state["now"])
        if candidate_scenario in {
            "LATE_COMMIT",
            "WINDOW_EXPIRED_AFTER_FREEZE",
            "COMMIT_CLOCK_ADVANCE",
        }:
            record_stage_result = DecisionLedger.record_stage_result

            def advance_clock_after_candidate_stage(
                ledger: DecisionLedger, connection: Any, **kwargs: Any
            ) -> Any:
                result = record_stage_result(ledger, connection, **kwargs)
                stage_result = kwargs.get("stage_result")
                if stage_result is not None and stage_result.phase == "CANDIDATE_RELEASE":
                    clock_state["now"] = commit_time
                return result

            monkeypatch.setattr(
                DecisionLedger, "record_stage_result", advance_clock_after_candidate_stage
            )
        else:
            publish_report = DecisionLedger.publish_report

            def advance_clock_after_report_save(
                ledger: DecisionLedger, connection: Any, fact: DecisionEventFact, *args: Any
            ) -> Any:
                report = publish_report(ledger, connection, fact, *args)
                clock_state["now"] = commit_time
                return report

            monkeypatch.setattr(DecisionLedger, "publish_report", advance_clock_after_report_save)
    if candidate_scenario == "QUALIFICATION_REVOKED_AFTER_REPORT_SAVE":
        publish_report = DecisionLedger.publish_report
        clock_state = {"now": publication_time}
        monkeypatch.setattr(UtcClock, "now", lambda self: clock_state["now"])

        def revoke_qualification_after_report_save(
            ledger: DecisionLedger, connection: Any, fact: DecisionEventFact, *args: Any
        ) -> Any:
            report = publish_report(ledger, connection, fact, *args)
            clock_state["now"] = publication_time + timedelta(seconds=2)
            original_qualification = qualification_history[0].qualification
            assert original_qualification is not None
            revoked = original_qualification.model_copy(
                update={
                    "decision_id": "synthetic-candidate-qualification-revoked-at-publication",
                    "status": "REVOKED",
                    "cause": "AUTHORIZATION_REVOKED",
                    "previous_decision_id": original_qualification.decision_id,
                    "recorded_at": publication_time + timedelta(seconds=1),
                }
            )
            qualification_history_state["value"] = (
                *qualification_history,
                GovernanceOutcome(
                    disposition="APPROVED",
                    reasons=("AUTHORIZATION_REVOKED",),
                    qualification=revoked,
                ),
            )
            return report

        monkeypatch.setattr(
            DecisionLedger,
            "publish_report",
            revoke_qualification_after_report_save,
        )
    if candidate_scenario in {
        "QUALIFICATION_DIAGNOSTIC_ALERT_AFTER_REPORT_SAVE",
        "QUALIFICATION_REVISION_THEN_DIAGNOSTIC_ALERT_AFTER_COMMIT",
    }:
        publish_report = DecisionLedger.publish_report
        clock_state = {"now": publication_time}
        monkeypatch.setattr(UtcClock, "now", lambda self: clock_state["now"])

        def record_diagnostic_alert_after_report_save(
            ledger: DecisionLedger, connection: Any, fact: DecisionEventFact, *args: Any
        ) -> Any:
            report = publish_report(ledger, connection, fact, *args)
            clock_state["now"] = publication_time + timedelta(seconds=3)
            original_qualification = (
                qualification_history[-1].qualification
                if candidate_scenario == "QUALIFICATION_REVISION_THEN_DIAGNOSTIC_ALERT_AFTER_COMMIT"
                else qualification_history[0].qualification
            )
            assert original_qualification is not None
            alert_available_at = publication_time + timedelta(seconds=1)
            alert_evidence = qualification_evidence.model_copy(
                update={
                    "evidence_id": "synthetic-candidate-diagnostic-alert-evidence",
                    "kind": "DIAGNOSTIC_ALERT",
                    "evaluation_end": alert_available_at,
                    "available_at": alert_available_at,
                }
            )
            first_alert = original_qualification.model_copy(
                update={
                    "decision_id": "synthetic-candidate-diagnostic-alert-after-publication-1",
                    "status": "AT_RISK",
                    "cause": "DIAGNOSTIC_ALERT",
                    "previous_decision_id": original_qualification.decision_id,
                    "recorded_at": publication_time + timedelta(seconds=1),
                    "alerts": (*original_qualification.alerts, alert_evidence),
                }
            )
            second_alert_evidence = alert_evidence.model_copy(
                update={
                    "evidence_id": "synthetic-candidate-diagnostic-alert-evidence-2",
                    "evaluation_end": publication_time + timedelta(seconds=2),
                    "available_at": publication_time + timedelta(seconds=2),
                }
            )
            second_alert = first_alert.model_copy(
                update={
                    "decision_id": "synthetic-candidate-diagnostic-alert-after-publication-2",
                    "previous_decision_id": first_alert.decision_id,
                    "recorded_at": publication_time + timedelta(seconds=2),
                    "alerts": (*first_alert.alerts, second_alert_evidence),
                }
            )
            qualification_history_state["value"] = (
                *qualification_history,
                GovernanceOutcome(
                    disposition="APPROVED",
                    reasons=("DIAGNOSTIC_ALERT",),
                    qualification=first_alert,
                ),
                GovernanceOutcome(
                    disposition="APPROVED",
                    reasons=("DIAGNOSTIC_ALERT",),
                    qualification=second_alert,
                ),
            )
            return report

        monkeypatch.setattr(
            DecisionLedger,
            "publish_report",
            record_diagnostic_alert_after_report_save,
        )
    if candidate_scenario == "UNRELATED_STATE_DIAGNOSTIC_ALERT_CANNOT_MASK_REVOCATION":
        publish_report = DecisionLedger.publish_report
        clock_state = {"now": publication_time}
        monkeypatch.setattr(UtcClock, "now", lambda self: clock_state["now"])

        def revoke_active_and_alert_unrelated_after_report_save(
            ledger: DecisionLedger, connection: Any, fact: DecisionEventFact, *args: Any
        ) -> Any:
            report = publish_report(ledger, connection, fact, *args)
            clock_state["now"] = publication_time + timedelta(seconds=2)
            original_bull = qualification_history[0].qualification
            assert original_bull is not None and bear_qualification_record is not None
            revoked_bull = original_bull.model_copy(
                update={
                    "decision_id": "synthetic-candidate-bull-revoked-after-save",
                    "status": "REVOKED",
                    "cause": "AUTHORIZATION_REVOKED",
                    "previous_decision_id": original_bull.decision_id,
                    "recorded_at": publication_time + timedelta(seconds=1),
                }
            )
            alerted_bear = bear_qualification_record.model_copy(
                update={
                    "decision_id": "synthetic-candidate-bear-alert-after-save",
                    "status": "AT_RISK",
                    "cause": "DIAGNOSTIC_ALERT",
                    "previous_decision_id": bear_qualification_record.decision_id,
                    "recorded_at": publication_time + timedelta(seconds=1),
                }
            )
            qualification_history_state["value"] = (
                *qualification_history,
                GovernanceOutcome(
                    disposition="APPROVED",
                    reasons=("AUTHORIZATION_REVOKED",),
                    qualification=revoked_bull,
                ),
                GovernanceOutcome(
                    disposition="APPROVED",
                    reasons=("DIAGNOSTIC_ALERT",),
                    qualification=alerted_bear,
                ),
            )
            return report

        monkeypatch.setattr(
            DecisionLedger,
            "publish_report",
            revoke_active_and_alert_unrelated_after_report_save,
        )
    if candidate_scenario in {
        "PUBLICATION_CONFIRMATION_CLOCK_ADVANCE",
        "QUALIFICATION_EXPIRES_AT_PUBLICATION_CONFIRMATION",
    }:
        clock_state = {"now": publication_time}
        monkeypatch.setattr(UtcClock, "now", lambda self: clock_state["now"])
        record_stage_result = DecisionLedger.record_stage_result

        def advance_clock_before_publication_confirmation(
            ledger: DecisionLedger, connection: Any, **kwargs: Any
        ) -> Any:
            stage_result = kwargs.get("stage_result")
            if stage_result is not None and stage_result.phase == "PUBLICATION":
                clock_state["now"] = publication_time + (
                    timedelta(hours=1)
                    if candidate_scenario == "QUALIFICATION_EXPIRES_AT_PUBLICATION_CONFIRMATION"
                    else timedelta(seconds=2)
                )
            return record_stage_result(ledger, connection, **kwargs)

        monkeypatch.setattr(
            DecisionLedger,
            "record_stage_result",
            advance_clock_before_publication_confirmation,
        )
    definition = frozen_decision_case._frozen_definition(candidate_case)
    context_provider = definition.context_provider
    assert isinstance(context_provider, DeterministicContextProvider)
    context_items = asyncio.run(context_provider.provide(ContextRequest(input="{}")))
    visible_input = json.loads(context_items[0].content)
    visible_candidate_release = visible_input["candidate_release"]
    assert "calibrator_selection_records" not in visible_candidate_release
    assert "training_records" not in visible_candidate_release
    assert "recent_diagnostic_records" not in visible_candidate_release

    execution = run_frozen_decision_case(migrated_settings, candidate_case.model_dump(mode="json"))

    expected_disposition = {
        "NORMAL": "RECOMMENDATION_ABSTAINED",
        "CALIBRATION_FAILURE": "FAILED",
        "CALIBRATION_SOURCE_MISSING": "FAILED",
        "CALIBRATION_MODEL_MISMATCH": "FAILED",
        "CALIBRATION_INFERENCE_MODEL_MISMATCH": "FAILED",
        "CALIBRATION_RECORDS_MISSING": "FAILED",
        "CALIBRATION_LATEST_MONTH_OMITTED": "FAILED",
        "CALIBRATION_WINDOW_MISSING": "FAILED",
        "CALIBRATION_EQUAL_TRAINING_WATERMARK": "CANDIDATES",
        "CALIBRATOR_VERSION_UNSUPPORTED": "FAILED",
        "CALIBRATION_COHORT_INCOMPLETE": "FAILED",
        "CALIBRATION_SOURCE_AFTER_CUTOFF": "FAILED",
        "CALIBRATION_LABEL_TAMPERED": "FAILED",
        "CALIBRATION_LABEL_CLOCKS_TAMPERED": "FAILED",
        "CALIBRATION_DECLARED_OLDER_WINDOW": "FAILED",
        "UPSTREAM_RESEARCH_DATA_FAILED": "FAILED",
        "UPSTREAM_RESEARCH_SYSTEM_FAILED": "FAILED",
        "UPSTREAM_RESEARCH_BLOCKED": "BLOCKED",
        "UPSTREAM_RESEARCH_EVENT_MISSING": "FAILED",
        "RESEARCH_RISK_VERSION_MISMATCH": "FAILED",
        "LATE_PUBLICATION": "FAILED",
        "LATE_COMMIT": "FAILED",
        "WINDOW_EXPIRED_AFTER_FREEZE": "FAILED",
        "LATE_REPORT_COMMIT": "RECOMMENDATION_ABSTAINED",
        "PUBLICATION_CONFIRMATION_CLOCK_ADVANCE": "RECOMMENDATION_ABSTAINED",
        "QUALIFICATION_EXPIRES_AT_PUBLICATION_CONFIRMATION": "CANDIDATES",
        "QUALIFICATION_REVOKED_AFTER_REPORT_SAVE": "CANDIDATES",
        "CORRECTION_AFTER_CANDIDATE_WINDOW": "CANDIDATES",
        "QUALIFICATION_CALENDAR_CHANGED_AT_CUTOFF": "FAILED",
        "QUALIFICATION_CALENDAR_CHANGED_AFTER_CUTOFF": "FAILED",
        "QUALIFICATION_FIRST_OBTAINED_AFTER_CUTOFF": "RECOMMENDATION_ABSTAINED",
        "COMMIT_CLOCK_ADVANCE": "RECOMMENDATION_ABSTAINED",
        "CALENDAR_WINDOW_MISSING": "FAILED",
        "AT_RISK_QUALIFICATION": direct.disposition,
        "QUALIFICATION_BECOMES_AT_RISK_BEFORE_PUBLICATION": "CANDIDATES",
        "QUALIFICATION_DIAGNOSTIC_ALERT_AFTER_REPORT_SAVE": "CANDIDATES",
        "QUALIFICATION_REVISION_THEN_DIAGNOSTIC_ALERT_AFTER_COMMIT": "CANDIDATES",
        "UNRELATED_STATE_DIAGNOSTIC_ALERT_CANNOT_MASK_REVOCATION": "CANDIDATES",
        "QUALIFICATION_EXPIRES_DURING_FIT": "RECOMMENDATION_ABSTAINED",
        "QUALIFICATION_SUSPENDED_AT_CUTOFF_RESTORED_AFTER": "RECOMMENDATION_ABSTAINED",
        "REVOKED_SAME_TIMESTAMP": "RECOMMENDATION_ABSTAINED",
        "REVOKED_AFTER_CUTOFF": "RECOMMENDATION_ABSTAINED",
        "NO_CANDIDATES": "VALID_NO_CANDIDATES",
        "QUALIFICATION_SNAPSHOT_MISSING": "CANDIDATES",
        "QUALIFICATION_NOT_OBTAINED_RECORDED": "RECOMMENDATION_ABSTAINED",
        "QUALIFICATION_VERSION_CHANGED_ON_FINAL_CHECK": "FAILED",
        "QUALIFICATION_VERSION_CHANGED_AFTER_REPORT_SAVE": "CANDIDATES",
        "UNRELATED_QUALIFICATION_SCOPE": "RECOMMENDATION_ABSTAINED",
        "UNRELATED_CALENDAR_QUALIFICATION": "FAILED",
        "UNRELATED_LATEST_SCOPE": "CANDIDATES",
        "VERSION_MISMATCH": "FAILED",
        "ORIGINAL_REJECTED": "BLOCKED",
    }[candidate_scenario]
    assert direct.disposition == expected_disposition, (
        direct.availability_failure,
        direct.reasons,
    )
    if candidate_scenario == "CALIBRATION_FAILURE" or candidate_scenario in {
        "CALIBRATION_SOURCE_MISSING",
        "CALIBRATION_MODEL_MISMATCH",
        "CALIBRATION_COHORT_INCOMPLETE",
        "CALIBRATION_RECORDS_MISSING",
        "CALIBRATION_LATEST_MONTH_OMITTED",
        "CALIBRATION_WINDOW_MISSING",
        "CALIBRATION_SOURCE_AFTER_CUTOFF",
        "CALIBRATION_LABEL_TAMPERED",
        "CALIBRATION_LABEL_CLOCKS_TAMPERED",
        "CALIBRATION_DECLARED_OLDER_WINDOW",
    }:
        assert direct.availability_failure == "CALIBRATION"
    elif candidate_scenario in {
        "CALENDAR_WINDOW_MISSING",
        "UPSTREAM_RESEARCH_EVENT_MISSING",
        "UPSTREAM_RESEARCH_DATA_FAILED",
    }:
        assert direct.availability_failure == "DATA"
    elif candidate_scenario == "UPSTREAM_RESEARCH_SYSTEM_FAILED":
        assert direct.availability_failure == "SYSTEM"
    elif candidate_scenario in {
        "VERSION_MISMATCH",
        "CALIBRATION_INFERENCE_MODEL_MISMATCH",
        "CALIBRATOR_VERSION_UNSUPPORTED",
        "QUALIFICATION_CALENDAR_CHANGED_AT_CUTOFF",
        "QUALIFICATION_CALENDAR_CHANGED_AFTER_CUTOFF",
        "UNRELATED_CALENDAR_QUALIFICATION",
        "QUALIFICATION_VERSION_CHANGED_ON_FINAL_CHECK",
    }:
        assert direct.availability_failure == "VERSION"
    elif candidate_scenario == "QUALIFICATION_EXPIRES_DURING_FIT":
        assert direct.members[0].market_state_qualified is False
    elif candidate_scenario in {
        "LATE_PUBLICATION",
        "LATE_COMMIT",
        "WINDOW_EXPIRED_AFTER_FREEZE",
    }:
        assert direct.population.valid_monthly is False
        assert "PUBLICATION_AFTER_CANDIDATE_WINDOW" in direct.reasons
    assert direct.population.recommendation_coverage_denominator == direct.population.valid_monthly
    if direct.calibration is not None:
        assert direct.calibration.slope >= 0
    if candidate_scenario == "CALIBRATION_DECLARED_OLDER_WINDOW":
        assert direct.availability_failure == "CALIBRATION"
    if candidate_scenario == "CALIBRATION_LABEL_TAMPERED":
        assert direct.availability_failure == "CALIBRATION"
    if candidate_scenario == "CALIBRATION_LABEL_CLOCKS_TAMPERED":
        assert direct.availability_failure == "CALIBRATION"
    if candidate_scenario in {
        "LATE_REPORT_COMMIT",
        "PUBLICATION_CONFIRMATION_CLOCK_ADVANCE",
        "QUALIFICATION_EXPIRES_AT_PUBLICATION_CONFIRMATION",
        "QUALIFICATION_REVOKED_AFTER_REPORT_SAVE",
    }:
        assert execution.report is not None
        assert execution.report.report_publication is not None
        assert execution.report.report_publication.status == "FAILED", (
            execution.report.result.candidate_release,
            execution.report.stage_results[-1],
        )
        assert execution.report.report_publication.failure_recorded_at is not None
        assert execution.report.report_publication.failure_reason == (
            "PUBLICATION_AFTER_CANDIDATE_WINDOW"
            if candidate_scenario
            in {"LATE_REPORT_COMMIT", "PUBLICATION_CONFIRMATION_CLOCK_ADVANCE"}
            else "CANDIDATE_QUALIFICATION_CHANGED_BEFORE_PUBLICATION"
        )
        if candidate_scenario in {
            "PUBLICATION_CONFIRMATION_CLOCK_ADVANCE",
            "QUALIFICATION_EXPIRES_AT_PUBLICATION_CONFIRMATION",
        }:
            assert execution.report.report_publication.failure_recorded_at == (
                publication_time
                + (
                    timedelta(hours=1)
                    if candidate_scenario == "QUALIFICATION_EXPIRES_AT_PUBLICATION_CONFIRMATION"
                    else timedelta(seconds=2)
                )
            ).isoformat().replace("+00:00", "Z")
        assert execution.report.result.candidate_release is not None
        assert execution.report.result.candidate_release.disposition == "FAILED"
        assert execution.publication_status == "FAILED"
        assert execution.report.stage_results[-1].phase == "PUBLICATION"
        assert execution.report.stage_results[-1].status == "FAILED"
        assert any(
            stage.phase == "PUBLICATION"
            and stage.status == "FAILED"
            and (
                "PUBLICATION_AFTER_CANDIDATE_WINDOW" in stage.reasons
                if candidate_scenario
                in {"LATE_REPORT_COMMIT", "PUBLICATION_CONFIRMATION_CLOCK_ADVANCE"}
                else "CANDIDATE_QUALIFICATION_CHANGED_BEFORE_PUBLICATION" in stage.reasons
            )
            for stage in execution.stage_results
        )
        correction_time = datetime.fromisoformat("2042-07-08T09:00:00+00:00")
        monkeypatch.setattr(UtcClock, "now", lambda self: correction_time)
        correction = decision_case_service.correct_default_frozen_decision_case(
            candidate_case,
            DecisionLedger.from_settings(migrated_settings),
            candidate_case.business_identity,
        )
        assert correction.report is not None
        assert correction.report.result.candidate_release is not None
        assert correction.report.result.candidate_release.disposition == "FAILED"
        assert all(
            member.candidate is False
            for member in correction.report.result.candidate_release.members
        )
        return
    if candidate_scenario == "CORRECTION_AFTER_CANDIDATE_WINDOW":
        assert execution.report is not None
        correction_time = datetime.fromisoformat("2042-07-08T09:00:00+00:00")
        monkeypatch.setattr(UtcClock, "now", lambda self: correction_time)
        correction = decision_case_service.correct_default_frozen_decision_case(
            candidate_case,
            DecisionLedger.from_settings(migrated_settings),
            candidate_case.business_identity,
        )
        assert correction.report is not None
        assert correction.report.corrects_event_id == execution.report.event_id
        assert (
            correction.report.result.candidate_release == execution.report.result.candidate_release
        )
        return
    assert execution.report is not None
    if candidate_scenario == "AT_RISK_QUALIFICATION":
        final_qualification = qualification_history[-1].qualification
        assert final_qualification is not None
        assert execution.report.result.candidate_release is not None
        assert execution.report.result.candidate_release.qualification is not None
        assert (
            execution.report.result.candidate_release.qualification.qualification_id
            == final_qualification.decision_id
        )
        direct = direct.model_copy(
            update={"qualification": execution.report.result.candidate_release.qualification}
        )
    assert direct is not None
    assert execution.report.result.candidate_release is not None
    saved_candidate_release = execution.report.result.candidate_release
    if candidate_scenario == "QUALIFICATION_VERSION_CHANGED_ON_FINAL_CHECK":
        assert saved_candidate_release.availability_failure == "VERSION"
        return
    if candidate_scenario == "QUALIFICATION_VERSION_CHANGED_AFTER_REPORT_SAVE":
        assert saved_candidate_release.availability_failure == "VERSION"
        return
    if candidate_scenario == "UNRELATED_STATE_DIAGNOSTIC_ALERT_CANNOT_MASK_REVOCATION":
        assert saved_candidate_release.disposition == "FAILED"
        assert execution.report.report_publication is not None
        assert execution.report.report_publication.status == "FAILED", (
            execution.report.result.candidate_release,
            execution.report.stage_results[-1],
        )
        assert execution.report.report_publication.failure_reason == (
            "CANDIDATE_QUALIFICATION_CHANGED_BEFORE_PUBLICATION"
        )
        return
    if candidate_scenario == "CALIBRATION_MODEL_MISMATCH":
        assert saved_candidate_release.disposition == "FAILED"
        assert saved_candidate_release.availability_failure == "VERSION"
        assert "CANDIDATE_CALIBRATION_MODEL_VERSION_MISMATCH" in saved_candidate_release.reasons
        return
    if candidate_scenario == "RESEARCH_RISK_VERSION_MISMATCH":
        assert saved_candidate_release.disposition == "FAILED"
        assert saved_candidate_release.availability_failure == "VERSION"
        assert "CANDIDATE_RESEARCH_VERSION_MISMATCH" in saved_candidate_release.reasons
        return
    if candidate_scenario == "CALIBRATION_EQUAL_TRAINING_WATERMARK":
        assert saved_candidate_release.disposition == "FAILED"
        assert saved_candidate_release.availability_failure == "CALIBRATION"
        assert "CANDIDATE_CALIBRATION_LINEAGE_INVALID" in saved_candidate_release.reasons
        return
    if candidate_scenario == "QUALIFICATION_SNAPSHOT_MISSING":
        assert saved_candidate_release.disposition == "FAILED"
        assert saved_candidate_release.availability_failure == "DATA"
        assert saved_candidate_release.reasons == ("CANDIDATE_QUALIFICATION_NOT_IN_LEDGER",)
        assert execution.report.report_publication is not None
        assert execution.report.report_publication.status == "PUBLISHED"
        assert execution.publication_status == "PUBLISHED"
        return
    direct = direct.model_copy(update={"qualification": saved_candidate_release.qualification})
    assert saved_candidate_release == direct
    if candidate_scenario == "NORMAL":
        assert execution.report.report_publication is not None
        assert execution.report.report_publication.status == "PUBLISHED"
        assert execution.report.report_publication.published_at is not None
        assert execution.publication_status == "PUBLISHED"
    assert execution.report.result.candidate_release.published_at == (
        datetime.fromisoformat("2042-07-07T16:00:00+00:00")
        if candidate_scenario in {"LATE_COMMIT", "WINDOW_EXPIRED_AFTER_FREEZE"}
        else datetime.fromisoformat("2042-07-02T00:04:00+00:00")
        if candidate_scenario == "COMMIT_CLOCK_ADVANCE"
        else publication_time
    )
    assert execution.report.result.candidate_release.members == direct.members
    source_risk_vetoes = {item.security_id: item for item in research.risk_veto.member_vetoes}
    for member in execution.report.result.candidate_release.members:
        source_veto = source_risk_vetoes[member.security_id]
        assert tuple((gate.gate_id, gate.status) for gate in member.risk_gates) == tuple(
            (gate.gate_id, gate.status) for gate in source_veto.gates
        )
        assert member.risk_reasons == source_veto.reasons
    if candidate_scenario == "NORMAL":
        assert all(
            member.evidence_freshness == "FRESH_AT_KNOWLEDGE_CUTOFF" for member in direct.members
        )
        assert direct.members[0].evidence_clocks == candidates[0].evidence_clocks
        assert direct.members[0].evidence_freshness == "FRESH_AT_KNOWLEDGE_CUTOFF"
        assert direct.members[0].evidence_clocks[0].source_published_at == (
            cutoff - timedelta(days=30)
        )
    if candidate_scenario == "AT_RISK_QUALIFICATION":
        assert direct.members[0].market_state_qualified is True
        assert direct.members[0].market_state_qualification_status == "AT_RISK"
    elif candidate_scenario in {
        "VERSION_MISMATCH",
        "QUALIFICATION_CALENDAR_CHANGED_AT_CUTOFF",
        "QUALIFICATION_CALENDAR_CHANGED_AFTER_CUTOFF",
        "UNRELATED_CALENDAR_QUALIFICATION",
        "QUALIFICATION_VERSION_CHANGED_ON_FINAL_CHECK",
    } or candidate_scenario in {
        "QUALIFICATION_NOT_OBTAINED_RECORDED",
    }:
        assert direct.members[0].market_state_qualified is False
        assert direct.members[0].market_state_qualification_status == "NOT_QUALIFIED"
    elif candidate_scenario == "QUALIFICATION_BECOMES_AT_RISK_BEFORE_PUBLICATION":
        assert direct.members[0].market_state_qualification_status == "AT_RISK"
        assert "MARKET_STATE_QUALIFICATION_AT_RISK" in direct.members[0].reasons
    elif candidate_scenario in {"REVOKED_SAME_TIMESTAMP", "REVOKED_AFTER_CUTOFF"}:
        assert direct.members[0].market_state_qualified is False
    elif candidate_scenario == "NO_CANDIDATES":
        assert direct.members[0].market_state_qualified is True
        assert all(member.candidate is False for member in direct.members)
        assert all(
            member.calibrated_probability is not None
            and member.calibrated_probability < Decimal("0.80")
            for member in direct.members
        )
    elif candidate_scenario == "UNRELATED_QUALIFICATION_SCOPE":
        assert direct.members[0].market_state_qualified is False
    elif candidate_scenario == "UNRELATED_LATEST_SCOPE":
        assert direct.members[0].market_state_qualified is True
        assert "MARKET_STATE_QUALIFICATION_AT_RISK" not in direct.members[0].reasons
    elif candidate_scenario == "ORIGINAL_REJECTED":
        assert execution.report.result.outcome_code == "SYNTHETIC_INPUT_REJECTED"
        assert (
            next(
                stage
                for stage in execution.report.stage_results
                if stage.phase == "BUSINESS_DECISION"
            ).status
            == "REJECTED"
        )
        assert (
            next(
                stage
                for stage in execution.report.stage_results
                if stage.phase == "CANDIDATE_RELEASE"
            ).status
            == "REJECTED"
        )
        assert len(direct.members) == len(candidates)
        assert direct.calibration is None
        assert direct.members[0].calibrated_probability is None
        assert direct.members[0].thesis == candidates[0].thesis
    if direct.members and direct.calibration is not None:
        assert direct.members[0].calibrated_probability is not None
    if direct.members:
        assert execution.report.result.candidate_release.members[0].risk_status == (
            "REJECTED" if risk_scenario == "REJECT" else "ACCEPTED"
        )
        if risk_scenario == "REJECT":
            assert (
                "INDEPENDENT_RISK_VETO"
                in execution.report.result.candidate_release.members[0].reasons
            )
    assert execution.report.stage_results[-1].phase == "PUBLICATION"
    assert execution.report.result.candidate_release.valid_market_dates == (
        ()
        if candidate_scenario == "CALENDAR_WINDOW_MISSING"
        else tuple(session.market_date for session in sessions)
    )
