"""Boundary adapter that converts M-Agent Runs into Stock Profiler values."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib.metadata import version
from typing import cast

from m_agent.adapters import DeterministicModelAdapter
from m_agent.runtime import (
    DEFAULT_LEASE_TTL,
    AgentDefinition,
    DefinitionRegistry,
    DuplicateRunError,
    IllegalRunTransitionError,
    LeaseNotHeldError,
    ModelCapabilities,
    OutputContract,
    Runner,
    RunNotFoundError,
    RunRecord,
    StaleRunVersionError,
    StructuredOutputMode,
)

from stock_profiler.adapters.persistence.runtime_ownership import RuntimeStorage
from stock_profiler.foundation.versioning import (
    M_AGENT_DISTRIBUTION,
    M_AGENT_RELEASE_COMMIT,
    M_AGENT_WHEEL_SHA256,
    M_AGENT_WHEEL_URL,
)
from stock_profiler.modules.decision_cases.domain import (
    FROZEN_AGENT_DEFINITION_ID,
    FROZEN_AGENT_DEFINITION_VERSION,
    FROZEN_OUTPUT_CONTRACT_VERSION,
    FrameworkRunStatus,
    FrozenDecisionCase,
    supports_case_host_contract,
    supports_report_projection_contract,
    synthetic_outcome_code_from_input,
)

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


@dataclass(frozen=True)
class FrameworkRunResult:
    """Framework details expressed without leaking framework types to the host module."""

    run_id: str
    status: FrameworkRunStatus
    output: str | None
    waiting_reason: str | None = None
    error_code: str | None = None
    transitions: tuple[FrameworkRunTransition, ...] = ()
    transitions_durably_recorded: bool = False


@dataclass(frozen=True)
class FrameworkRunTransition:
    """One durable framework state observed before its terminal result."""

    status: FrameworkRunStatus
    reason: str


FrameworkTransitionRecorder = Callable[[FrameworkRunTransition], Awaitable[None]]
_PLAINTEXT_PAYLOAD_PREFIX = b"m-agent-plaintext:"
_RUN_INPUT_FIELD = "run:input"


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


def find_unmapped_legacy_frozen_decision_case(
    case: FrozenDecisionCase,
    runtime: RuntimeStorage,
) -> FrozenDecisionCase | None:
    """Find one historical Run by its frozen input without mutating the M-Agent store."""
    expected_input = json.dumps(
        case.input,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    candidates: list[FrozenDecisionCase] = []
    try:
        with sqlite3.connect(
            f"{runtime.m_agent_run_store_path.resolve().as_uri()}?mode=ro",
            uri=True,
        ) as connection:
            for legacy_case in case.legacy_contract_recovery_cases:
                rows = connection.execute(
                    """
                    SELECT runs.run_id
                    FROM runs
                    JOIN run_payloads
                      ON run_payloads.run_id = runs.run_id
                    WHERE runs.run_id = ?
                      AND runs.definition_id = ?
                      AND runs.definition_version = ?
                      AND run_payloads.field = ?
                      AND run_payloads.encoded = ?
                    ORDER BY runs.run_id
                    """,
                    (
                        legacy_case.framework_run_id,
                        legacy_case.agent_definition.definition_id,
                        legacy_case.agent_definition.version,
                        _RUN_INPUT_FIELD,
                        _PLAINTEXT_PAYLOAD_PREFIX + expected_input.encode(),
                    ),
                ).fetchall()
                candidates.extend(
                    legacy_case.model_copy(update={"recovery_framework_run_id": row[0]})
                    for row in rows
                )
    except sqlite3.OperationalError as error:
        if "no such table" in str(error).lower():
            return None
        raise ValueError("cannot inspect durable M-Agent Run history") from error
    if len(candidates) > 1:
        raise ValueError("multiple durable M-Agent Runs match the legacy frozen input")
    return candidates[0] if candidates else None


async def execute_frozen_decision_case(
    case: FrozenDecisionCase,
    runtime: RuntimeStorage,
    record_transition: FrameworkTransitionRecorder | None = None,
) -> FrameworkRunResult:
    """Create or reuse the exact durable Run for one frozen host identity."""
    _assert_runtime_version_bundle(case)
    adapter = DeterministicModelAdapter(
        responses=(_deterministic_model_response(case),),
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
            raise ValueError("mapped durable M-Agent Run is missing") from None
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
    return FrameworkRunResult(
        run_id=run.run_id,
        status=cast(FrameworkRunStatus, run.status.value),
        output=run.output,
        waiting_reason=run.waiting_reason,
        error_code=run.error_code,
        transitions=tuple(transitions),
        transitions_durably_recorded=record_transition is not None,
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
    if (
        not supports_case_host_contract(
            bundle.case_contract_version,
            bundle.host_contract_version,
        )
        or not supports_report_projection_contract(bundle.report_projection_contract_version)
        or bundle.agent_definition_id != FROZEN_AGENT_DEFINITION_ID
        or bundle.agent_definition_version != FROZEN_AGENT_DEFINITION_VERSION
        or bundle.output_contract_version != FROZEN_OUTPUT_CONTRACT_VERSION
    ):
        raise ValueError("full frozen version bundle is not supported by this runtime")
    if (
        bundle.model_adapter_id != DETERMINISTIC_MODEL_ADAPTER_ID
        or bundle.routing_policy_version != D0_ROUTING_POLICY_VERSION
        or case.agent_definition.definition_id != FROZEN_AGENT_DEFINITION_ID
        or case.agent_definition.version != FROZEN_AGENT_DEFINITION_VERSION
        or case.agent_definition.model_adapter_id != DETERMINISTIC_MODEL_ADAPTER_ID
        or case.agent_definition.instructions != FROZEN_DEFINITION_INSTRUCTIONS
        or case.agent_definition.output_contract.contract_id != FROZEN_OUTPUT_CONTRACT_ID
        or case.agent_definition.output_contract.version != FROZEN_OUTPUT_CONTRACT_VERSION
        or case.agent_definition.output_contract.json_schema != FROZEN_OUTPUT_SCHEMA
    ):
        raise ValueError("frozen AgentDefinition does not match the runtime adapter")
    if (
        bundle.m_agent_version != version(M_AGENT_DISTRIBUTION)
        or bundle.m_agent_wheel_url != M_AGENT_WHEEL_URL
        or bundle.m_agent_wheel_sha256 != M_AGENT_WHEEL_SHA256
        or bundle.m_agent_release_commit != M_AGENT_RELEASE_COMMIT
    ):
        raise ValueError("frozen M-Agent release bundle does not match the installed runtime")


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
