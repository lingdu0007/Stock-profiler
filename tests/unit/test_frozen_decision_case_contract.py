from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from threading import Thread
from time import sleep

import pytest
from m_agent.adapters import DeterministicModelAdapter
from m_agent.runtime import (
    AgentDefinition,
    CrashPoint,
    DefinitionRegistry,
    ModelCapabilities,
    OutputContract,
    Runner,
    StructuredOutputMode,
)

from stock_profiler.adapters.m_agent import frozen_decision_case as frozen_adapter
from stock_profiler.adapters.m_agent.frozen_decision_case import (
    FROZEN_OUTPUT_SCHEMA,
    _deterministic_model_response,
    execute_frozen_decision_case,
)
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases import service
from stock_profiler.modules.decision_cases.domain import (
    BusinessLifecycle,
    ExternalResult,
    FormalReport,
    GateResult,
    StageResult,
    business_lifecycle_from_stage,
    host_validation_result,
    load_frozen_decision_case,
)
from stock_profiler.modules.decision_cases.service import run_default_frozen_decision_case


def test_frozen_synthetic_case_has_stable_independent_identities(settings: Settings) -> None:
    first = load_frozen_decision_case(settings)
    second = load_frozen_decision_case(settings)

    assert first.synthetic is True
    assert first.generator_version == "frozen-decision-case-v1"
    assert first.seed == 4017
    assert first.qualification_scope == "D0_SYNTHETIC_CONTRACT_ONLY"
    assert first.knowledge_cutoff == "2042-05-17T16:00:00Z"
    assert first.evidence_clock.validated_at == "2042-05-17T15:18:00Z"
    assert first.version_bundle.m_agent_version == "0.5.0"
    assert first.version_bundle.host_application_version == settings.configuration_version
    assert first.version_bundle.host_source_sha == settings.source_sha
    assert first.version_bundle.model_adapter_id == "m-agent-deterministic-model-adapter"
    assert first.version_bundle.routing_policy_version == "d0-single-definition-route-v1"
    assert first.version_bundle.host_contract_version == "1.0.0"
    assert first.version_bundle.report_projection_contract_version == "2.0.0"
    assert first.agent_definition.instructions == (
        "Return only the frozen synthetic decision-case external result as JSON."
    )
    assert first.agent_definition.output_contract.contract_id == "synthetic-decision-case-output"
    assert first.report_generated_at == "2042-05-17T16:01:00Z"
    assert first.business_object_id == second.business_object_id
    assert first.framework_run_id == second.framework_run_id
    assert first.decision_event_id == second.decision_event_id
    assert first.report_version_id == second.report_version_id
    assert (
        len(
            {
                first.business_object_id,
                first.framework_run_id,
                first.decision_event_id,
                first.report_version_id,
            }
        )
        == 4
    )

    revised_report_contract = first.model_copy(
        update={
            "version_bundle": first.version_bundle.model_copy(
                update={"report_projection_contract_version": "3.0.0"}
            )
        }
    )
    assert revised_report_contract.report_version_id != first.report_version_id


def test_runtime_rejects_a_case_whose_declared_m_agent_release_is_not_installed(
    migrated_settings: Settings,
) -> None:
    case = load_frozen_decision_case(migrated_settings)
    tampered = case.model_copy(
        update={
            "version_bundle": case.version_bundle.model_copy(
                update={"m_agent_release_commit": "0" * 40}
            )
        }
    )

    with pytest.raises(ValueError, match="M-Agent release bundle"):
        asyncio.run(
            execute_frozen_decision_case(tampered, initialize_runtime_storage(migrated_settings))
        )


@pytest.mark.parametrize(
    "field_name",
    (
        "case_contract_version",
        "host_contract_version",
        "report_projection_contract_version",
    ),
)
def test_runtime_rejects_any_unsupported_host_contract_version(
    migrated_settings: Settings, field_name: str
) -> None:
    case = load_frozen_decision_case(migrated_settings)
    tampered = case.model_copy(
        update={"version_bundle": case.version_bundle.model_copy(update={field_name: "999.0.0"})}
    )

    with pytest.raises(ValueError, match="full frozen version bundle"):
        asyncio.run(
            execute_frozen_decision_case(tampered, initialize_runtime_storage(migrated_settings))
        )


@pytest.mark.parametrize("field_name", ("definition_id", "version", "instructions"))
def test_runtime_rejects_tampered_agent_definition_contract(
    migrated_settings: Settings, field_name: str
) -> None:
    case = load_frozen_decision_case(migrated_settings)
    tampered = case.model_copy(
        update={
            "agent_definition": case.agent_definition.model_copy(
                update={field_name: "unsupported-frozen-definition"}
            )
        }
    )

    with pytest.raises(ValueError, match="frozen AgentDefinition"):
        asyncio.run(
            execute_frozen_decision_case(tampered, initialize_runtime_storage(migrated_settings))
        )


def test_host_validation_rejects_any_scope_except_the_frozen_d0_scope(
    migrated_settings: Settings,
) -> None:
    case = load_frozen_decision_case(migrated_settings)
    contradictory_scope = case.model_copy(update={"qualification_scope": "D0_REAL_RECOMMENDATION"})

    assert (
        host_validation_result(
            contradictory_scope,
            case.expected_external_result,
        ).status
        == "FAILED"
    )


def test_host_validation_records_an_explicit_gate_when_result_reasons_are_empty(
    migrated_settings: Settings,
) -> None:
    case = load_frozen_decision_case(migrated_settings)
    empty_reason_result = ExternalResult(
        outcome_code=case.expected_external_result.outcome_code,
        summary=case.expected_external_result.summary,
        key_reasons=(),
    )

    stage = host_validation_result(case, empty_reason_result)

    assert stage.phase == "HOST_VALIDATION"
    assert stage.status == "FAILED"
    assert stage.gate_results[0].gate_id == "KEY_REASONS_PRESENT"
    assert stage.gate_results[0].status == "FAILED"
    assert stage.reasons == ("RESULT_REASONS_REQUIRED",)


def test_current_report_contract_refuses_a_missing_stage_history(
    migrated_settings: Settings,
) -> None:
    execution = run_default_frozen_decision_case(migrated_settings)
    assert execution.report is not None
    corrupted_current_payload = execution.report.model_dump(mode="json")
    corrupted_current_payload.pop("stage_results")

    with pytest.raises(ValueError, match="stage_results"):
        FormalReport.model_validate(corrupted_current_payload)


def test_lifecycle_statuses_keep_their_explicit_owner_and_phase() -> None:
    lifecycle = business_lifecycle_from_stage(
        StageResult(
            phase="ADJUDICATION_LIFECYCLE",
            status="UNKNOWN",
            gate_results=(GateResult(gate_id="ADJUDICATION_COMPLETE", status="UNKNOWN"),),
            reasons=("ADJUDICATION_INPUT_UNKNOWN",),
        )
    )

    assert lifecycle == BusinessLifecycle(
        owner="ADJUDICATION",
        phase="ADJUDICATION_LIFECYCLE",
        status="UNKNOWN",
    )
    with pytest.raises(ValueError, match="belongs to ADJUDICATION"):
        BusinessLifecycle(
            owner="VALIDITY",
            phase="ADJUDICATION_LIFECYCLE",
            status="PENDING",
        )
    with pytest.raises(ValueError, match="cannot record status EXPIRED"):
        BusinessLifecycle(
            owner="ADJUDICATION",
            phase="ADJUDICATION_LIFECYCLE",
            status="EXPIRED",
        )


def test_host_recovers_the_original_m_agent_model_checkpoint_into_the_original_identity_set(
    migrated_settings: Settings,
) -> None:
    case = load_frozen_decision_case(migrated_settings)
    runtime = initialize_runtime_storage(migrated_settings)

    async def create_checkpointed_run() -> None:
        definition = AgentDefinition.for_adapter(
            definition_id=case.agent_definition.definition_id,
            version=case.agent_definition.version,
            instructions=case.agent_definition.instructions,
            model_adapter=DeterministicModelAdapter(
                responses=(_deterministic_model_response(case),),
                capabilities=ModelCapabilities(
                    structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT
                ),
            ),
            output_contract=OutputContract(
                contract_id=case.agent_definition.output_contract.contract_id,
                version=case.agent_definition.output_contract.version,
                schema=FROZEN_OUTPUT_SCHEMA,
                structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
            ),
        )
        registry = DefinitionRegistry()
        registry.register(definition)

        def crash_after_model_checkpoint(point: CrashPoint, run_id: str) -> None:
            if point is CrashPoint.AFTER_MODEL_CHECKPOINT and run_id == case.framework_run_id:
                raise RuntimeError("synthetic worker interruption after model checkpoint")

        interrupted_runner = Runner(
            registry=registry,
            store=runtime.run_store,
            owner="test-interrupted-worker",
            crash_hook=crash_after_model_checkpoint,
        )
        created = await interrupted_runner.create_run(
            definition.definition_id,
            definition.version,
            json.dumps(case.input, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
            run_id=case.framework_run_id,
        )
        with pytest.raises(RuntimeError, match="model checkpoint"):
            await interrupted_runner.start_run(created.run_id)

        checkpointed = await interrupted_runner.get_run(case.framework_run_id)
        checkpoints = await runtime.run_store.get_checkpoints(case.framework_run_id)
        assert checkpointed.status.value == "RUNNING"
        assert len(checkpoints) == 1
        assert checkpoints[0].run_id == case.framework_run_id
        assert checkpoints[0].step_type.value == "MODEL"
        await runtime.run_store.release_lease(
            checkpointed.run_id,
            "test-interrupted-worker",
            expected_version=checkpointed.version,
        )

    asyncio.run(create_checkpointed_run())

    recovered = run_default_frozen_decision_case(migrated_settings)

    assert recovered.business_object_id == case.business_object_id
    assert recovered.framework_run_id == case.framework_run_id
    assert recovered.decision_event_id == case.decision_event_id
    assert recovered.report_version_id == case.report_version_id
    assert recovered.framework_run_status == "SUCCEEDED"
    assert recovered.business_commit_status == "COMMITTED"
    assert recovered.publication_status == "PUBLISHED"
    assert recovered.report is not None
    assert recovered.report.result == case.expected_external_result
    assert [
        stage.status for stage in recovered.stage_results if stage.phase == "FRAMEWORK_RUN"
    ] == ["RUNNING", "SUCCEEDED"]


def test_active_m_agent_lease_waits_for_the_original_run_to_resume(
    migrated_settings: Settings,
) -> None:
    case = load_frozen_decision_case(migrated_settings)
    runtime = initialize_runtime_storage(migrated_settings)

    async def create_leased_run() -> None:
        definition = AgentDefinition.for_adapter(
            definition_id=case.agent_definition.definition_id,
            version=case.agent_definition.version,
            instructions=case.agent_definition.instructions,
            model_adapter=DeterministicModelAdapter(
                responses=(_deterministic_model_response(case),),
                capabilities=ModelCapabilities(
                    structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT
                ),
            ),
            output_contract=OutputContract(
                contract_id=case.agent_definition.output_contract.contract_id,
                version=case.agent_definition.output_contract.version,
                schema=FROZEN_OUTPUT_SCHEMA,
                structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
            ),
        )
        registry = DefinitionRegistry()
        registry.register(definition)
        runner = Runner(registry=registry, store=runtime.run_store, owner="test-active-owner")
        created = await runner.create_run(
            definition.definition_id,
            definition.version,
            json.dumps(case.input, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
            run_id=case.framework_run_id,
        )
        await runtime.run_store.acquire_lease(
            created.run_id,
            "test-active-owner",
            timedelta(seconds=30),
            expected_version=created.version,
        )

    asyncio.run(create_leased_run())

    async def release_leased_run() -> None:
        release_runtime = initialize_runtime_storage(migrated_settings)
        try:
            run = await release_runtime.run_store.get_run(case.framework_run_id)
            assert run is not None
            await release_runtime.run_store.release_lease(
                run.run_id,
                "test-active-owner",
                expected_version=run.version,
            )
        finally:
            release_runtime.run_store.close()

    def release_after_observation_starts() -> None:
        sleep(0.05)
        asyncio.run(release_leased_run())

    release_worker = Thread(target=release_after_observation_starts)
    release_worker.start()
    recovered = run_default_frozen_decision_case(migrated_settings)
    release_worker.join(timeout=5)

    assert not release_worker.is_alive()
    assert recovered.framework_run_id == case.framework_run_id
    assert recovered.decision_event_id == case.decision_event_id
    assert recovered.publication_status == "PUBLISHED"
    assert recovered.report is not None


def test_framework_waiting_transition_preserves_the_observed_waiting_reason(
    migrated_settings: Settings,
) -> None:
    case = load_frozen_decision_case(migrated_settings)
    stages = service._framework_stage_results(
        case,
        frozen_adapter.FrameworkRunResult(
            run_id=case.framework_run_id,
            status="WAITING",
            output=None,
            waiting_reason="FRAMEWORK_AWAITING_RESOLUTION",
            transitions=(
                frozen_adapter.FrameworkRunTransition(
                    status="WAITING",
                    reason="FRAMEWORK_AWAITING_RESOLUTION",
                ),
            ),
        ),
    )

    assert [(stage.status, stage.reasons) for stage in stages] == [
        ("WAITING", ("FRAMEWORK_AWAITING_RESOLUTION",)),
        ("WAITING", ("FRAMEWORK_AWAITING_RESOLUTION",)),
    ]
