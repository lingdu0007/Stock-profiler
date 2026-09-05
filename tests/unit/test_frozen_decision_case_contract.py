from __future__ import annotations

import asyncio

import pytest

from stock_profiler.adapters.m_agent.frozen_decision_case import execute_frozen_decision_case
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import (
    host_validates_external_result,
    load_frozen_decision_case,
)


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
    assert first.version_bundle.report_projection_contract_version == "1.0.0"
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
                update={"report_projection_contract_version": "2.0.0"}
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


@pytest.mark.parametrize("field_name", ("definition_id", "version"))
def test_runtime_rejects_tampered_agent_definition_identity(
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

    assert not host_validates_external_result(contradictory_scope, case.expected_external_result)
