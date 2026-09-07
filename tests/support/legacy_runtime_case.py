"""Generate original D0 history using the pinned, installed historical runtime only."""

from __future__ import annotations

import asyncio
import json
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any

from m_agent.adapters import (
    DeterministicContextProvider,
    DeterministicModelAdapter,
    PlaintextPayloadCodec,
    SQLiteRunStore,
)
from m_agent.runtime import (
    AgentDefinition,
    ContextItem,
    CrashPoint,
    DefinitionRegistry,
    ModelCapabilities,
    OutputContract,
    Runner,
    StructuredOutputMode,
)


async def generate(envelope: dict[str, Any], path: Path, mode: str) -> None:
    assert version("m-agent") == "0.5.0"
    case = envelope["case"]
    declared = case["agent_definition"]
    contract = declared["output_contract"]
    definition = AgentDefinition.for_adapter(
        definition_id=declared["definition_id"],
        version=declared["version"],
        instructions=declared["instructions"],
        model_adapter=DeterministicModelAdapter(
            responses=(
                json.dumps(case["expected_external_result"], separators=(",", ":"), sort_keys=True),
            ),
            capabilities=ModelCapabilities(
                structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT
            ),
        ),
        context_provider=DeterministicContextProvider(
            items=(
                ContextItem(
                    item_id=envelope["fingerprint"],
                    source="frozen-synthetic-case",
                    content=json.dumps(case["input"], separators=(",", ":"), sort_keys=True),
                ),
            )
        )
        if case.get("access_scope")
        else None,
        tools=(),
        output_contract=OutputContract(
            contract_id=contract["contract_id"],
            version=contract["version"],
            schema=contract["json_schema"],
            structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
        ),
    )
    registry = DefinitionRegistry()
    registry.register(definition)
    store = SQLiteRunStore(path, PlaintextPayloadCodec())

    def crash(point: CrashPoint, run_id: str) -> None:
        if mode == "checkpoint" and point is CrashPoint.AFTER_MODEL_CHECKPOINT:
            raise RuntimeError("synthetic historical worker interruption")

    runner = Runner(registry=registry, store=store, owner="historical-writer", crash_hook=crash)
    try:
        run = await runner.create_run(
            definition.definition_id,
            definition.version,
            json.dumps(case["input"], separators=(",", ":"), sort_keys=True),
            run_id=envelope["run_id"],
        )
        try:
            await runner.start_run(run.run_id)
        except RuntimeError:
            if mode != "checkpoint":
                raise
            interrupted = await runner.get_run(run.run_id)
            await store.release_lease(
                run.run_id, "historical-writer", expected_version=interrupted.version
            )
        original = await runner.get_run(run.run_id)
        checkpoints = await store.get_checkpoints(run.run_id)
        inspection = {
            "run": original.model_dump(mode="json"),
            "checkpoints": [item.model_dump(mode="json") for item in checkpoints],
        }
        if mode == "damaged":
            other = await runner.create_run(
                definition.definition_id,
                definition.version,
                "independent synthetic collision input",
                run_id="synthetic-historical-collision",
            )
            try:
                await runner.start_run(other.run_id)
            except Exception as error:
                if "UNIQUE constraint failed: step_checkpoints.step_id" not in str(error):
                    raise
            else:
                raise AssertionError("the historical collision was not reproduced")
        print(json.dumps(inspection, sort_keys=True))
    finally:
        store.close()


if __name__ == "__main__":
    asyncio.run(generate(json.loads(Path(sys.argv[1]).read_text()), Path(sys.argv[2]), sys.argv[3]))
