"""Boundary adapter that converts M-Agent Runs into Stock Profiler values."""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.metadata import version

from m_agent.adapters import DeterministicModelAdapter
from m_agent.runtime import (
    AgentDefinition,
    DefinitionRegistry,
    ModelCapabilities,
    OutputContract,
    Runner,
    RunNotFoundError,
    StructuredOutputMode,
)

from stock_profiler.adapters.persistence.runtime_ownership import RuntimeStorage
from stock_profiler.foundation.versioning import (
    M_AGENT_DISTRIBUTION,
    M_AGENT_RELEASE_COMMIT,
    M_AGENT_WHEEL_SHA256,
    M_AGENT_WHEEL_URL,
)
from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase

DETERMINISTIC_MODEL_ADAPTER_ID = "m-agent-deterministic-model-adapter"
D0_ROUTING_POLICY_VERSION = "d0-single-definition-route-v1"


@dataclass(frozen=True)
class FrameworkRunResult:
    """Framework details expressed without leaking framework types to the host module."""

    run_id: str
    status: str
    output: str | None


async def execute_frozen_decision_case(
    case: FrozenDecisionCase, runtime: RuntimeStorage
) -> FrameworkRunResult:
    """Create or reuse the exact durable Run for one frozen host identity."""
    _assert_runtime_version_bundle(case)
    adapter = DeterministicModelAdapter(
        responses=(case.expected_external_result.model_dump_json(),),
        capabilities=ModelCapabilities(structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT),
    )
    definition = AgentDefinition.for_adapter(
        definition_id=case.agent_definition.definition_id,
        version=case.agent_definition.version,
        instructions=case.agent_definition.instructions,
        model_adapter=adapter,
        output_contract=OutputContract(
            contract_id=case.agent_definition.output_contract.contract_id,
            version=case.agent_definition.output_contract.version,
            schema=case.agent_definition.output_contract.json_schema,
            structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
        ),
    )
    registry = DefinitionRegistry()
    registry.register(definition)
    runner = Runner(registry=registry, store=runtime.run_store, owner="stock-profiler-d0")
    try:
        run = await runner.get_run(case.framework_run_id)
    except RunNotFoundError:
        created = await runner.create_run(
            definition.definition_id,
            definition.version,
            json.dumps(case.input, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
            run_id=case.framework_run_id,
        )
        run = await runner.start_run(created.run_id)
    return FrameworkRunResult(run_id=run.run_id, status=run.status.value, output=run.output)


def _assert_runtime_version_bundle(case: FrozenDecisionCase) -> None:
    """Reject frozen metadata that does not describe the installed deterministic route."""
    bundle = case.version_bundle
    if (
        bundle.model_adapter_id != DETERMINISTIC_MODEL_ADAPTER_ID
        or bundle.routing_policy_version != D0_ROUTING_POLICY_VERSION
        or case.agent_definition.model_adapter_id != DETERMINISTIC_MODEL_ADAPTER_ID
    ):
        raise ValueError("frozen deterministic route does not match the runtime adapter")
    if (
        bundle.m_agent_version != version(M_AGENT_DISTRIBUTION)
        or bundle.m_agent_wheel_url != M_AGENT_WHEEL_URL
        or bundle.m_agent_wheel_sha256 != M_AGENT_WHEEL_SHA256
        or bundle.m_agent_release_commit != M_AGENT_RELEASE_COMMIT
    ):
        raise ValueError("frozen M-Agent release bundle does not match the installed runtime")
