from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from m_agent.adapters import DeterministicModelAdapter
from m_agent.runtime import (
    DefinitionRegistry,
    ModelRequest,
    ModelResponse,
    Runner,
    ToolCall,
)

from stock_profiler.adapters.m_agent import frozen_decision_case as adapter
from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase


@pytest.mark.parametrize(
    "tool_name",
    [
        "unknown_read",
        "generate_order",
        "prefill_order",
        "submit_order",
        "modify_order",
        "cancel_order",
    ],
)
def test_model_cannot_invoke_an_unregistered_tool(
    migrated_settings: Settings,
    scoped_payload: dict[str, Any],
    security_cases: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
) -> None:
    async def malicious_response(
        self: DeterministicModelAdapter, request: ModelRequest
    ) -> ModelResponse:
        return ModelResponse(
            tool_calls=(
                ToolCall(
                    call_id="synthetic-forged-call",
                    tool_name=tool_name,
                    arguments=json.dumps(security_cases["forbidden_payload"]),
                ),
            )
        )

    monkeypatch.setattr(DeterministicModelAdapter, "generate", malicious_response)
    execution = run_frozen_decision_case(migrated_settings, scoped_payload)
    assert execution.framework_run_status == "FAILED"
    assert execution.report is None
    assert execution.publication_status == "CLOSED"
    audit = ResultDelivery.from_settings(migrated_settings).audit_history()
    assert audit[-1].reason == "UNDECLARED_CAPABILITY"
    assert "XQZ-4017" not in audit[-1].model_dump_json()


def test_actual_context_checkpoint_contains_only_the_frozen_case(
    migrated_settings: Settings, scoped_payload: dict[str, Any]
) -> None:
    execution = run_frozen_decision_case(migrated_settings, scoped_payload)
    case = FrozenDecisionCase.model_validate(scoped_payload)
    runtime = initialize_runtime_storage(migrated_settings)
    registry = DefinitionRegistry()
    registry.register(adapter._frozen_definition(case))
    inspection = asyncio.run(
        Runner(registry=registry, store=runtime.run_store).inspect_run(execution.framework_run_id)
    )
    assert inspection.run.snapshot.has_context_provider is True
    assert inspection.run.snapshot.tool_declarations == ()
    serialized = inspection.model_dump_json()
    assert "frozen-synthetic-case" in serialized
    assert case.frozen_input_fingerprint in serialized
    assert adapter.frozen_capability_inventory(case).context_provider is True
