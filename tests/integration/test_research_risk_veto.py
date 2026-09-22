from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

import pytest
from m_agent.runtime import StepType, ToolRequest

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
from stock_profiler.modules.decision_cases.domain import (
    EvidenceClock,
    ExternalResult,
    FrozenAgentDefinition,
    FrozenDecisionCase,
    FrozenOutputContract,
    ResultAccessScope,
)
from stock_profiler.modules.decision_cases.ports import (
    DecisionEventCommitError,
    MappedDurableRunMissingError,
)
from stock_profiler.modules.research import service as research_service
from stock_profiler.modules.research.contracts import (
    RAW_SCORE_FEATURE_IDS,
    RESEARCH_DEFINITION_ID,
    RESEARCH_MODEL_ADAPTER_ID,
    RESEARCH_OUTPUT_CONTRACT_ID,
    RESEARCH_OUTPUT_CONTRACT_VERSION,
    RESEARCH_ROUTING_POLICY_VERSION,
    RISK_DEFINITION_ID,
    RISK_DEFINITION_VERSION,
    FrozenDualTargetScreening,
    RawScoreCalculationError,
    ResearchCommand,
    ResearchDraft,
    ResearchDraftMember,
    ResearchEvidence,
    ResearchFrameworkOutput,
    ResearchMemberInput,
    RiskGate,
    RiskVetoDraft,
    freeze_raw_score,
    handoff_fingerprint,
    risk_run_id_for,
    screening_output_sha256,
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
            evidence=(
                ResearchEvidence(
                    evidence_id=f"evidence-{index:02}",
                    source="fictional-certified-feed",
                    reference=f"synthetic://evidence/{index:02}",
                    statement="A fictional structured fact is available at the cutoff.",
                    knowledge_cutoff=cutoff,
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
        cutoff_at=cutoff,
        knowledge_cutoff=cutoff,
        purpose="SYNTHETIC",
        screening=screening,
        members=members,
        risk_scenario=risk_scenario,
        failure_mode=failure_mode,
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
) -> FrozenDecisionCase:
    command = research_command(risk_scenario=risk_scenario, failure_mode=failure_mode)
    scope = ResultAccessScope(
        contract_version="1.0.0",
        user_id="synthetic-user-1616",
        account_ids=("synthetic-account-4017",),
        visibility="USER",
    )
    draft = _draft(command)
    tool_evidence_refs = tuple(f"announcement:synthetic-security-{index:02}" for index in range(3))
    raw_scores = tuple(freeze_raw_score(command, member) for member in command.members)
    provisional_risk = RiskVetoDraft(
        contract_version="1.0.0",
        handoff_fingerprint=handoff_fingerprint(
            command,
            draft,
            raw_scores=raw_scores,
            tool_evidence_refs=tool_evidence_refs,
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
    assert execution.report.result.research.disposition == "REJECTED"
    assert execution.report.result.research.raw_scores is not None
    assert len(execution.report.result.research.raw_scores) == 10
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
    checkpoints = asyncio.run(runtime.run_store.get_checkpoints(case.framework_run_id))
    assert sum(checkpoint.step_type is StepType.MODEL for checkpoint in checkpoints) == 4
    assert sum(checkpoint.step_type is StepType.TOOL for checkpoint in checkpoints) == 3
    assert execution.report.result.research.handoff.evidence_ids[-3:] == (
        "announcement:synthetic-security-00",
        "announcement:synthetic-security-01",
        "announcement:synthetic-security-02",
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
    assert {case.framework_run_id, risk_run_id}.issubset(recorded_run_ids)


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
        "MODEL_STEP",
        "MODEL_STEP",
        "MODEL_STEP",
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
    tool_evidence_refs = tuple(f"announcement:synthetic-security-{index:02}" for index in range(3))
    risk_run_id = risk_run_id_for(
        case.framework_run_id,
        _draft(command),
        raw_scores=tuple(freeze_raw_score(command, member) for member in command.members),
        tool_evidence_refs=tool_evidence_refs,
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
