"""Boundary adapter that converts M-Agent Runs into Stock Profiler values."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from importlib.metadata import version
from typing import cast

from m_agent.adapters import DeterministicContextProvider, DeterministicModelAdapter
from m_agent.runtime import (
    DEFAULT_LEASE_TTL,
    AgentDefinition,
    AllowAllRunPolicy,
    ContextItem,
    DefinitionRegistry,
    DuplicateRunError,
    IllegalRunTransitionError,
    LeaseNotHeldError,
    ModelCapabilities,
    ModelContractViolationError,
    OutputContract,
    Runner,
    RunNotFoundError,
    RunRecord,
    StaleRunVersionError,
    StructuredOutputMode,
)

from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
from stock_profiler.adapters.persistence.runtime_ownership import RuntimeStorage
from stock_profiler.foundation.clock import Clock
from stock_profiler.foundation.decision_versions import (
    CURRENT_M_AGENT_RELEASE,
    HISTORICAL_M_AGENT_RELEASE,
)
from stock_profiler.foundation.versioning import M_AGENT_DISTRIBUTION
from stock_profiler.modules.decision_cases.domain import (
    FROZEN_AGENT_DEFINITION_ID,
    FROZEN_OUTPUT_CONTRACT_VERSION,
    FrameworkRunStatus,
    FrozenDecisionCase,
    definition_version_for_case_contract,
    supports_case_host_contract,
    supports_report_projection_contract,
    synthetic_outcome_code_from_input,
)
from stock_profiler.modules.decision_cases.ports import (
    FrameworkRunResult as FrameworkRunResult,
)
from stock_profiler.modules.decision_cases.ports import (
    FrameworkRunTransition as FrameworkRunTransition,
)
from stock_profiler.modules.decision_cases.ports import (
    FrameworkTransitionRecorder as FrameworkTransitionRecorder,
)
from stock_profiler.modules.decision_cases.ports import (
    MappedDurableRunMissingError as MappedDurableRunMissingError,
)
from stock_profiler.modules.delivery.capabilities import CapabilityInventory

READ_ONLY_TOOL_ALLOWLIST: frozenset[str] = frozenset()

DETERMINISTIC_MODEL_ADAPTER_ID = "m-agent-deterministic-model-adapter"
D0_ROUTING_POLICY_VERSION = "d0-single-definition-route-v1"
FROZEN_DEFINITION_INSTRUCTIONS = (
    "Return only the frozen synthetic decision-case external result as JSON."
)
FROZEN_OUTPUT_CONTRACT_ID = "synthetic-decision-case-output"
FROZEN_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome_code": {"type": "string"},
        "summary": {"type": "string"},
        "key_reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["outcome_code", "summary", "key_reasons"],
    "additionalProperties": False,
}
DEFAULT_SYNTHETIC_MODEL_RESPONSE = (
    '{"key_reasons":['
    '"All required fictional evidence records are present.",'
    '"The output is D0 synthetic evidence and is not a recommendation."'
    '],"outcome_code":"SYNTHETIC_REVIEW_COMPLETE",'
    '"summary":"Synthetic D0 decision case completed under the frozen contract."}'
)
_CONCURRENT_RUN_OBSERVATION_DELAY_SECONDS = 0.01
_CONCURRENT_RUN_RECOVERY_ERRORS = (
    DuplicateRunError,
    IllegalRunTransitionError,
    LeaseNotHeldError,
    StaleRunVersionError,
)


@dataclass
class _RunStatusCollector:
    """Collect framework-emitted, already-durable statuses without changing a Run."""

    run_id: str
    statuses: list[FrameworkRunStatus]

    def emit(self, event: object) -> None:
        if getattr(event, "run_id", None) != self.run_id:
            return
        status = getattr(event, "run_status", None)
        value = getattr(status, "value", None)
        if value in {
            "CREATED",
            "RUNNING",
            "WAITING",
            "SUCCEEDED",
            "REJECTED",
            "FAILED",
            "CANCELLED",
        }:
            self.statuses.append(cast(FrameworkRunStatus, value))


async def validate_frozen_recovery_case(case: FrozenDecisionCase, runtime: RuntimeStorage) -> None:
    """Require the supplied original snapshot to identify an already durable Run."""
    _assert_runtime_version_bundle(case)
    run = await runtime.run_store.get_run(case.framework_run_id)
    if run is None:
        raise MappedDurableRunMissingError("original durable M-Agent Run is missing")
    _assert_existing_run_matches_case(run, case, _frozen_definition(case))


async def find_unmapped_legacy_frozen_decision_case(
    case: FrozenDecisionCase,
    runtime: RuntimeStorage,
    known_run_ids: frozenset[str] = frozenset(),
) -> FrozenDecisionCase | None:
    """Find a deterministic historical Run through the public M-Agent store."""
    candidates: dict[str, FrozenDecisionCase] = {}
    for legacy_case in case.legacy_contract_recovery_cases:
        _assert_runtime_version_bundle(legacy_case)
        run = await runtime.run_store.get_run(legacy_case.framework_run_id)
        if run is None:
            continue
        _assert_existing_run_matches_case(
            run,
            legacy_case,
            _frozen_definition(legacy_case),
        )
        candidates[run.run_id] = legacy_case
    if len(candidates) > 1:
        raise ValueError("multiple durable M-Agent Runs match the legacy frozen input")
    # v0.5.0 has no Run inventory API. Read metadata only to veto unsafe creation;
    # never adopt a Run or reconstruct its frozen provenance from this inventory.
    try:
        uri = runtime.m_agent_run_store_path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            run_ids = {
                row[0]
                for row in connection.execute(
                    "SELECT run_id FROM runs WHERE definition_id = ?",
                    (case.agent_definition.definition_id,),
                )
            }
    except sqlite3.Error as error:
        raise ValueError("durable framework inventory cannot be verified") from error
    unmapped_run_ids = run_ids - known_run_ids
    if len(unmapped_run_ids) > 1 or unmapped_run_ids - {case.framework_run_id, *candidates}:
        raise ValueError("unmapped durable Run requires original frozen build provenance")
    if not candidates:
        return None
    recovery_framework_run_id, legacy_case = next(iter(candidates.items()))
    return legacy_case.model_copy(update={"recovery_framework_run_id": recovery_framework_run_id})


def _frozen_definition(case: FrozenDecisionCase) -> AgentDefinition:
    """Build the exact public M-Agent definition that owns a frozen Run."""
    adapter = DeterministicModelAdapter(
        responses=(_deterministic_model_response(case),),
        capabilities=ModelCapabilities(structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT),
    )
    return AgentDefinition.for_adapter(
        definition_id=case.agent_definition.definition_id,
        version=case.agent_definition.version,
        instructions=case.agent_definition.instructions,
        model_adapter=adapter,
        context_provider=(
            DeterministicContextProvider(
                items=(
                    ContextItem(
                        item_id=case.frozen_input_fingerprint,
                        source="frozen-synthetic-case",
                        content=json.dumps(
                            case.input, ensure_ascii=True, separators=(",", ":"), sort_keys=True
                        ),
                    ),
                )
            )
            if case.access_scope is not None
            else None
        ),
        tools=(),
        output_contract=OutputContract(
            contract_id=case.agent_definition.output_contract.contract_id,
            version=case.agent_definition.output_contract.version,
            schema=case.agent_definition.output_contract.json_schema,
            structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
        ),
    )


async def execute_frozen_decision_case(
    case: FrozenDecisionCase,
    runtime: RuntimeStorage,
    record_transition: FrameworkTransitionRecorder | None = None,
    *,
    clock: Clock | None = None,
) -> FrameworkRunResult:
    """Create or reuse the exact durable Run for one frozen host identity."""
    try:
        _assert_runtime_version_bundle(case)
        definition = _frozen_definition(case)
        _assert_registered_capabilities(case, definition)
    except ValueError:
        ResultDelivery(runtime.engine, clock=clock).record_capability_denial(case.framework_run_id)
        raise
    registry = DefinitionRegistry()
    registry.register(definition)
    status_collector = _RunStatusCollector(case.framework_run_id, [])
    runner = Runner(
        registry=registry,
        store=runtime.run_store,
        telemetry_sink=status_collector,
    )
    transitions: list[FrameworkRunTransition] = []
    collected_status_count = 0

    async def observe(transition: FrameworkRunTransition) -> None:
        transitions.append(transition)
        if record_transition is not None:
            await record_transition(transition)

    async def record_framework_statuses() -> None:
        nonlocal collected_status_count
        status_reasons = {
            "CREATED": "FRAMEWORK_RUN_CREATED",
            "RUNNING": "FRAMEWORK_RUN_STARTED",
            "WAITING": "FRAMEWORK_RUN_WAITING",
        }
        for status in status_collector.statuses[collected_status_count:]:
            reason = status_reasons.get(status)
            if reason is not None:
                await observe(FrameworkRunTransition(status=status, reason=reason))
        collected_status_count = len(status_collector.statuses)

    try:
        run = await runner.get_run(case.framework_run_id)
    except RunNotFoundError:
        if case.recovery_framework_run_id is not None:
            raise MappedDurableRunMissingError("mapped durable M-Agent Run is missing") from None
        if case.version_bundle.runtime_release != CURRENT_M_AGENT_RELEASE:
            raise MappedDurableRunMissingError(
                "historical runtime identity requires the original durable Run"
            ) from None
        try:
            created = await runner.create_run(
                definition.definition_id,
                definition.version,
                json.dumps(case.input, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
                run_id=case.framework_run_id,
            )
            await record_framework_statuses()
            run = await runner.start_run(created.run_id)
            await record_framework_statuses()
        except _CONCURRENT_RUN_RECOVERY_ERRORS:
            run = await _recover_concurrent_run(
                runner,
                case,
                definition,
                observe,
            )
            await record_framework_statuses()
        except sqlite3.Error as error:
            if not _is_concurrent_creation_error(error):
                raise
            run = await _recover_concurrent_run(
                runner,
                case,
                definition,
                observe,
            )
            await record_framework_statuses()
    else:
        _assert_existing_run_matches_case(run, case, definition)
        await observe(FrameworkRunTransition(status="CREATED", reason="FRAMEWORK_RUN_CREATED"))
        if run.status.is_terminal:
            await observe(
                FrameworkRunTransition(
                    status=cast(FrameworkRunStatus, run.status.value),
                    reason="FRAMEWORK_RUN_RECOVERED_TERMINAL",
                )
            )
        else:
            await observe(
                FrameworkRunTransition(
                    status=cast(FrameworkRunStatus, run.status.value),
                    reason=(
                        run.waiting_reason
                        if run.status.value == "WAITING" and run.waiting_reason is not None
                        else "FRAMEWORK_RUN_RECOVERED"
                    ),
                )
            )
            try:
                run = await runner.resume_run(run.run_id)
            except _CONCURRENT_RUN_RECOVERY_ERRORS:
                run = await _recover_concurrent_run(
                    runner,
                    case,
                    definition,
                    observe,
                )
            await record_framework_statuses()
    if run.error_code == ModelContractViolationError.code:
        ResultDelivery(runtime.engine, clock=clock).record_capability_denial(case.framework_run_id)
    return FrameworkRunResult(
        run_id=run.run_id,
        status=cast(FrameworkRunStatus, run.status.value),
        output=run.output,
        waiting_reason=run.waiting_reason,
        error_code=run.error_code,
        transitions=tuple(transitions),
        transitions_durably_recorded=record_transition is not None,
    )


def _assert_registered_capabilities(case: FrozenDecisionCase, definition: AgentDefinition) -> None:
    """No request, session, or definition metadata can install an executable extension."""
    if (
        definition.tools
        or READ_ONLY_TOOL_ALLOWLIST
        or type(definition.model_adapter) is not DeterministicModelAdapter
        or definition.model_adapters
        or type(definition.run_policy) is not AllowAllRunPolicy
        or definition.context_plan.stages
        or definition.compression_contract is not None
        or (case.access_scope is None and definition.context_provider is not None)
        or (
            case.access_scope is not None
            and type(definition.context_provider) is not DeterministicContextProvider
        )
    ):
        raise ValueError("undeclared framework capability")


def frozen_capability_inventory(case: FrozenDecisionCase) -> CapabilityInventory:
    _assert_runtime_version_bundle(case)
    definition = _frozen_definition(case)
    _assert_registered_capabilities(case, definition)
    snapshot = definition.frozen_snapshot()
    return CapabilityInventory(
        definition_id=snapshot.definition_id,
        definition_version=snapshot.version,
        context_provider=snapshot.has_context_provider,
        tool_names=tuple(tool.name for tool in snapshot.tool_declarations),
        model_routes=(case.agent_definition.model_adapter_id,),
        session_enabled=False,
        order_credentials=False,
    )


async def _recover_concurrent_run(
    runner: Runner,
    case: FrozenDecisionCase,
    definition: AgentDefinition,
    observe: FrameworkTransitionRecorder,
) -> RunRecord:
    """Converge on a competing owner without creating a replacement Run."""
    deadline = asyncio.get_running_loop().time() + DEFAULT_LEASE_TTL.total_seconds()
    while asyncio.get_running_loop().time() < deadline:
        try:
            run = await runner.get_run(case.framework_run_id)
        except RunNotFoundError:
            await asyncio.sleep(_CONCURRENT_RUN_OBSERVATION_DELAY_SECONDS)
            continue
        _assert_existing_run_matches_case(run, case, definition)
        if run.status.is_terminal or run.status.value == "WAITING":
            await observe(
                FrameworkRunTransition(
                    status=cast(FrameworkRunStatus, run.status.value),
                    reason="FRAMEWORK_RUN_CONCURRENT_RECOVERY",
                )
            )
            return run
        try:
            return await runner.resume_run(run.run_id)
        except LeaseNotHeldError:
            await asyncio.sleep(_CONCURRENT_RUN_OBSERVATION_DELAY_SECONDS)
        except _CONCURRENT_RUN_RECOVERY_ERRORS:
            await asyncio.sleep(_CONCURRENT_RUN_OBSERVATION_DELAY_SECONDS)
    raise ValueError("concurrent durable M-Agent Run did not become recoverable")


def _is_concurrent_creation_error(error: sqlite3.Error) -> bool:
    """Recognize SQLite's unwrapped duplicate or transient writer-conflict errors."""
    return isinstance(error, sqlite3.IntegrityError) or (
        isinstance(error, sqlite3.OperationalError)
        and any(
            marker in str(error).lower() for marker in ("database is locked", "database is busy")
        )
    )


def _assert_runtime_version_bundle(case: FrozenDecisionCase) -> None:
    """Reject frozen metadata that does not describe the installed deterministic route."""
    bundle = case.version_bundle
    definition_version = definition_version_for_case_contract(bundle.case_contract_version)
    if (
        not supports_case_host_contract(
            bundle.case_contract_version,
            bundle.host_contract_version,
        )
        or not supports_report_projection_contract(bundle.report_projection_contract_version)
        or bundle.agent_definition_id != FROZEN_AGENT_DEFINITION_ID
        or bundle.agent_definition_version != definition_version
        or bundle.output_contract_version != FROZEN_OUTPUT_CONTRACT_VERSION
    ):
        raise ValueError("full frozen version bundle is not supported by this runtime")
    if (
        bundle.model_adapter_id != DETERMINISTIC_MODEL_ADAPTER_ID
        or bundle.routing_policy_version != D0_ROUTING_POLICY_VERSION
        or case.agent_definition.definition_id != FROZEN_AGENT_DEFINITION_ID
        or case.agent_definition.version != definition_version
        or case.agent_definition.model_adapter_id != DETERMINISTIC_MODEL_ADAPTER_ID
        or case.agent_definition.instructions != FROZEN_DEFINITION_INSTRUCTIONS
        or case.agent_definition.output_contract.contract_id != FROZEN_OUTPUT_CONTRACT_ID
        or case.agent_definition.output_contract.version != FROZEN_OUTPUT_CONTRACT_VERSION
        or case.agent_definition.output_contract.json_schema != FROZEN_OUTPUT_SCHEMA
    ):
        raise ValueError("frozen AgentDefinition does not match the runtime adapter")
    if version(
        M_AGENT_DISTRIBUTION
    ) != CURRENT_M_AGENT_RELEASE.m_agent_version or bundle.runtime_release not in (
        CURRENT_M_AGENT_RELEASE,
        HISTORICAL_M_AGENT_RELEASE,
    ):
        raise ValueError("frozen M-Agent release bundle does not match the installed runtime")
    try:
        FrozenDecisionCase.model_validate(case.model_dump(mode="python"))
    except ValueError as error:
        raise ValueError("full frozen version bundle or scoped case is invalid") from error


def _assert_existing_run_matches_case(
    run: RunRecord,
    case: FrozenDecisionCase,
    definition: AgentDefinition,
) -> None:
    """Bind a recovered run to the same frozen definition and input before reuse."""
    expected_input = json.dumps(
        case.input,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if (
        run.definition_id != case.agent_definition.definition_id
        or run.definition_version != case.agent_definition.version
        or run.input != expected_input
    ):
        raise ValueError("durable M-Agent Run does not match the frozen recovery input")
    if run.snapshot != definition.frozen_snapshot():
        raise ValueError("durable M-Agent Run does not match the frozen definition snapshot")


def _deterministic_model_response(case: FrozenDecisionCase) -> str:
    """Make the test model respond to the frozen input, never its expected output field."""
    outcome_code = synthetic_outcome_code_from_input(case.input)
    if outcome_code == "SYNTHETIC_REVIEW_COMPLETE":
        return DEFAULT_SYNTHETIC_MODEL_RESPONSE
    if outcome_code is not None:
        return json.dumps(
            {
                "key_reasons": [_SYNTHETIC_OUTCOME_REASONS[outcome_code]],
                "outcome_code": outcome_code,
                "summary": f"Frozen synthetic result {outcome_code}.",
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    return _incomplete_synthetic_response()


_SYNTHETIC_OUTCOME_REASONS: dict[str, str] = {
    "SYNTHETIC_INPUT_REJECTED": (
        "The scenario's D0 host-input gate rejects the requested decision."
    ),
    "SYNTHETIC_RESULT_ABSTAINED": ("The scenario has no eligible synthetic action to record."),
    "SYNTHETIC_RESULT_FAILED": "The scenario injects a deterministic business-stage fault.",
    "SYNTHETIC_RESULT_PENDING": "The scenario leaves the adjudication gate unresolved.",
    "SYNTHETIC_RESULT_EXPIRED": "The scenario's synthetic validity deadline has passed.",
    "SYNTHETIC_RESULT_EXECUTION_BLOCKED": (
        "The scenario's synthetic host execution gate is unavailable."
    ),
    "SYNTHETIC_RESULT_UNKNOWN": "The scenario withholds a resolved adjudication outcome.",
}


def _incomplete_synthetic_response() -> str:
    """Return an invalid-input response, not a synthetic business rejection."""
    return (
        '{"key_reasons":["The frozen synthetic evidence records are incomplete."],'
        '"outcome_code":"SYNTHETIC_INPUT_INVALID",'
        '"summary":"Frozen synthetic input did not satisfy the deterministic contract."}'
    )
