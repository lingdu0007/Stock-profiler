from __future__ import annotations

import asyncio

import pytest
from m_agent.adapters import DeterministicTool
from m_agent.runtime import AllowAllRunPolicy, ToolEffect

from stock_profiler.adapters.m_agent import frozen_decision_case as adapter
from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.decision_cases import run_default_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import load_frozen_decision_case


@pytest.mark.parametrize("effect", [ToolEffect.READ_ONLY, ToolEffect.NON_IDEMPOTENT])
def test_forged_tools_are_denied_before_direct_adapter_execution(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch, effect: ToolEffect
) -> None:
    case = load_frozen_decision_case(migrated_settings)
    runtime = initialize_runtime_storage(migrated_settings)
    side_effects: list[str] = []
    forged = DeterministicTool(
        name="submit_order",
        effect=effect,
        handler=lambda _: side_effects.append("forged order executed"),
    )
    original = adapter._frozen_definition(case)
    monkeypatch.setattr(
        adapter, "_frozen_definition", lambda _: original.model_copy(update={"tools": (forged,)})
    )

    with pytest.raises(ValueError, match="capability"):
        asyncio.run(adapter.execute_frozen_decision_case(case, runtime))

    assert side_effects == []
    assert asyncio.run(runtime.run_store.get_run(case.framework_run_id)) is None
    audit = ResultDelivery.from_settings(migrated_settings).audit_history()
    assert len(audit) == 1
    assert audit[0].reason == "UNDECLARED_CAPABILITY"


def test_capability_inventory_describes_the_actual_durable_definition(
    migrated_settings: Settings,
) -> None:
    execution = run_default_frozen_decision_case(migrated_settings)
    inventory = adapter.frozen_capability_inventory(load_frozen_decision_case(migrated_settings))
    runtime = initialize_runtime_storage(migrated_settings)
    run = asyncio.run(runtime.run_store.get_run(execution.framework_run_id))
    assert run is not None
    assert inventory.definition_id == run.definition_id
    assert inventory.definition_version == run.definition_version
    assert inventory.tool_names == ()
    assert run.snapshot.tool_declarations == ()
    assert inventory.session_enabled is False
    assert inventory.order_credentials is False
    assert inventory.model_routes == ("m-agent-deterministic-model-adapter",)


def test_forged_policy_cannot_hide_a_write_behind_a_read_only_definition(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    effects: list[str] = []

    class ForgedPolicy:
        identity = AllowAllRunPolicy().identity

        def evaluate(self, *args: object, **kwargs: object) -> object:
            effects.append("policy invoked")
            raise RuntimeError("forged write")

    case = load_frozen_decision_case(migrated_settings)
    runtime = initialize_runtime_storage(migrated_settings)
    original = adapter._frozen_definition(case)
    monkeypatch.setattr(
        adapter,
        "_frozen_definition",
        lambda _: original.model_copy(update={"run_policy": ForgedPolicy()}),
    )
    with pytest.raises(ValueError, match="capability"):
        asyncio.run(adapter.execute_frozen_decision_case(case, runtime))
    assert effects == []
    assert ResultDelivery.from_settings(migrated_settings).audit_history()
