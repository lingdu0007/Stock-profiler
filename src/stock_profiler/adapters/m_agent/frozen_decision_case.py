"""Boundary adapter that converts M-Agent Runs into Stock Profiler values."""

from __future__ import annotations

import json
from dataclasses import dataclass

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
from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase


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
    adapter = DeterministicModelAdapter(
        responses=(case.expected_external_result.model_dump_json(),),
        capabilities=ModelCapabilities(structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT),
    )
    definition = AgentDefinition.for_adapter(
        definition_id=case.version_bundle.agent_definition_id,
        version=case.version_bundle.agent_definition_version,
        instructions="Return only the frozen synthetic decision-case external result as JSON.",
        model_adapter=adapter,
        output_contract=OutputContract(
            contract_id="synthetic-decision-case-output",
            version=case.version_bundle.output_contract_version,
            schema={
                "type": "object",
                "properties": {
                    "outcome_code": {"type": "string"},
                    "summary": {"type": "string"},
                    "key_reasons": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["outcome_code", "summary", "key_reasons"],
                "additionalProperties": False,
            },
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
