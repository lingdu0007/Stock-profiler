"""Boundary adapter that converts M-Agent Runs into Stock Profiler values."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from importlib.metadata import version
from typing import cast

from m_agent.adapters import (
    DeterministicContextProvider,
    DeterministicModelAdapter,
    DeterministicTool,
)
from m_agent.runtime import (
    DEFAULT_LEASE_TTL,
    AgentDefinition,
    AllowAllRunPolicy,
    ContextItem,
    ContextPlan,
    ContextProvider,
    ContextRequest,
    ContextScope,
    ContextStage,
    ContextStageConfig,
    ContextStageIdentity,
    ContextTransformType,
    DefinitionRegistry,
    DuplicateRunError,
    IllegalRunTransitionError,
    LeaseNotHeldError,
    ModelCapabilities,
    ModelCapabilityCombination,
    ModelContractViolationError,
    ModelRequest,
    ModelResponse,
    OutputContract,
    PolicyAction,
    PolicyDecision,
    PolicyGate,
    Runner,
    RunNotFoundError,
    RunRecord,
    StaleRunVersionError,
    StaticRunPolicy,
    StepType,
    StructuredOutputMode,
    ToolCall,
    ToolCallingMode,
    ToolEffect,
    ToolOutcome,
    ToolRequest,
    deserialize_tool_outcome,
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
from stock_profiler.modules.research import service as research_service
from stock_profiler.modules.research.contracts import (
    RESEARCH_CONTRACT_VERSION,
    RESEARCH_DEFINITION_ID,
    RESEARCH_DEFINITION_VERSION,
    RESEARCH_MODEL_ADAPTER_ID,
    RESEARCH_OUTPUT_CONTRACT_ID,
    RESEARCH_OUTPUT_CONTRACT_VERSION,
    RESEARCH_ROUTING_POLICY_VERSION,
    RISK_DEFINITION_ID,
    RISK_DEFINITION_VERSION,
    RawScore,
    RawScoreCalculationError,
    ResearchCommand,
    ResearchDraft,
    ResearchFrameworkOutput,
    RiskGate,
    RiskVetoDraft,
    handoff_fingerprint,
    risk_run_id_for,
)

READ_ONLY_TOOL_ALLOWLIST: frozenset[str] = frozenset()
RESEARCH_READ_ONLY_TOOL_ALLOWLIST: frozenset[str] = frozenset({"read_announcement"})

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
RESEARCH_DEFINITION_INSTRUCTIONS = (
    "Run one structured single-stock research definition across the fixed ten cohort. "
    "Use the required structured facts delivered by Context Provider before model steps. "
    "The explicit stages are collect, analyze, bull-bear, and draft. "
    "Use only the declared read-only announcement Tool for optional exploration. "
    "Return only the typed research draft. Do not output scores, probabilities, qualification, "
    "personal quantities, order, or trading conclusions."
)
RISK_DEFINITION_INSTRUCTIONS = (
    "Evaluate the immutable research handoff with an independent risk-veto definition. "
    "Return only the typed risk gate result. A REJECTED result is authoritative and cannot "
    "be changed by research or orchestration."
)
RESEARCH_STAGE_IDS = ("collect", "analyze", "bull-bear", "draft")
RESEARCH_STAGE_CONTRACTS = (
    ("collect", ("RUN_INPUT",), ("RESEARCH_COLLECTION",)),
    ("analyze", ("RESEARCH_COLLECTION",), ("RESEARCH_ANALYSIS",)),
    ("bull-bear", ("RESEARCH_ANALYSIS",), ("RESEARCH_BULL_BEAR",)),
    ("draft", ("RESEARCH_BULL_BEAR",), ("RESEARCH_DRAFT",)),
)
RESEARCH_STAGE_SCOPES = {
    "collect": ContextScope.RUN_INPUT,
    "analyze": ContextScope.MODEL_STEP,
    "bull-bear": ContextScope.MODEL_STEP,
    "draft": ContextScope.MODEL_STEP,
}
RESEARCH_TOOL_NAME = "read_announcement"
RESEARCH_TOOL_PARAMETERS = {
    "type": "object",
    "properties": {"security_id": {"type": "string"}},
    "required": ["security_id"],
    "additionalProperties": False,
}
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


class _FrozenResearchContextProvider(ContextProvider):  # type: ignore[misc]
    """Deterministic required-fact Provider with an explicit data-failure mode."""

    deterministic = True

    def __init__(self, items: tuple[ContextItem, ...], *, fail: bool = False) -> None:
        self._items = items
        self._fail = fail

    async def provide(self, request: ContextRequest) -> tuple[ContextItem, ...]:
        del request
        if self._fail:
            raise RuntimeError("RESEARCH_DATA_UNAVAILABLE")
        return self._items


class _StagedResearchModelAdapter(DeterministicModelAdapter):  # type: ignore[misc]
    """Drive the four frozen research phases through durable model/tool steps."""

    deterministic = True

    def __init__(self, draft_response: str) -> None:
        capabilities = ModelCapabilities(
            tool_calling=ToolCallingMode.NATIVE,
            structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
            supported_combinations=(
                ModelCapabilityCombination(
                    tool_calling=ToolCallingMode.NATIVE,
                    structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
                ),
            ),
        )
        super().__init__(responses=(draft_response,), capabilities=capabilities)
        self._stage_tool_calls = tuple(
            ToolCall(
                call_id=f"research-stage-{stage_id}",
                tool_name=RESEARCH_TOOL_NAME,
                arguments=json.dumps(
                    {"security_id": f"synthetic-security-{index:02}"},
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
            for stage_id, index in (
                ("collect", 0),
                ("analyze", 1),
                ("bull-bear", 2),
            )
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        stage_index = len(request.tool_outcomes)
        if stage_index < len(self._stage_tool_calls):
            return ModelResponse(tool_calls=(self._stage_tool_calls[stage_index],))
        return ModelResponse(content=self._responses[0])


def _read_announcement_tool() -> DeterministicTool:
    def handler(request: ToolRequest) -> ToolOutcome:
        try:
            arguments = json.loads(request.arguments)
        except json.JSONDecodeError:
            return ToolOutcome.rejected(
                request.call_id,
                request.tool_name,
                "INVALID_ARGUMENTS",
                "announcement lookup arguments must be JSON",
            )
        security_id = arguments.get("security_id")
        if not isinstance(security_id, str) or not security_id:
            return ToolOutcome.rejected(
                request.call_id,
                request.tool_name,
                "INVALID_SECURITY_ID",
                "announcement lookup requires one security_id",
            )
        return ToolOutcome.success(
            request.call_id,
            request.tool_name,
            (
                f"Synthetic announcement evidence announcement:{security_id} "
                "is read-only and contains no trade instruction."
            ),
        )

    return DeterministicTool(
        name=RESEARCH_TOOL_NAME,
        description="Read one synthetic announcement without changing external state.",
        effect=ToolEffect.READ_ONLY,
        parameters=RESEARCH_TOOL_PARAMETERS,
        handler=handler,
    )


def _research_context_items(command: ResearchCommand) -> tuple[ContextItem, ...]:
    return tuple(
        ContextItem(
            item_id=f"required-fact:{member.security_id}:{evidence.evidence_id}",
            source="synthetic-required-fact-provider",
            content=json.dumps(
                {
                    "security_id": member.security_id,
                    "research_id": member.research_id,
                    "evidence": evidence.model_dump(mode="json"),
                    "structured_signals": member.model_dump(mode="json")["structured_signals"],
                    "risk_flags": member.risk_flags,
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ),
            metadata={
                "availability": "REQUIRED_BEFORE_MODEL",
                "knowledge_cutoff": member.knowledge_cutoff.isoformat(),
            },
        )
        for member in command.members
        for evidence in member.evidence
    )


def _research_context_plan() -> ContextPlan:
    return ContextPlan(
        plan_version=RESEARCH_CONTRACT_VERSION,
        stages=tuple(
            ContextStage(
                identity=ContextStageIdentity(
                    stage_id=stage_id,
                    scope=RESEARCH_STAGE_SCOPES[stage_id],
                    transform_type=ContextTransformType.PROVIDE,
                    config_version=RESEARCH_CONTRACT_VERSION,
                ),
                config=ContextStageConfig(
                    stage_id=stage_id,
                    transform_type=ContextTransformType.PROVIDE,
                    config_version=RESEARCH_CONTRACT_VERSION,
                    config={
                        "phase": stage_id,
                        "stage_order": RESEARCH_STAGE_IDS.index(stage_id),
                        "input_channels": list(input_channels),
                        "output_channels": list(output_channels),
                    },
                ),
                input_channels=input_channels,
                output_channels=output_channels,
            )
            for stage_id, input_channels, output_channels in RESEARCH_STAGE_CONTRACTS
        ),
    )


def _research_definition(case: FrozenDecisionCase) -> AgentDefinition:
    """Register one staged research Definition with a narrow capability surface."""
    command = case.research
    assert command is not None
    adapter = _StagedResearchModelAdapter(_research_model_response(command))
    return AgentDefinition.for_adapter(
        definition_id=RESEARCH_DEFINITION_ID,
        version=RESEARCH_DEFINITION_VERSION,
        instructions=RESEARCH_DEFINITION_INSTRUCTIONS,
        model_adapter=adapter,
        context_provider=_FrozenResearchContextProvider(
            _research_context_items(command),
            fail=command.failure_mode == "DATA",
        ),
        tools=(_read_announcement_tool(),),
        output_contract=OutputContract(
            contract_id=RESEARCH_OUTPUT_CONTRACT_ID,
            version=RESEARCH_OUTPUT_CONTRACT_VERSION,
            schema=ResearchDraft.model_json_schema(),
            structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
        ),
        context_plan=_research_context_plan(),
    )


def _risk_definition(
    command: ResearchCommand,
    draft: ResearchDraft,
    *,
    research_run_id: str,
    raw_scores: tuple[RawScore, ...],
    tool_evidence_refs: tuple[str, ...],
) -> AgentDefinition:
    """Register the independent risk Definition over an immutable handoff."""
    handoff = handoff_fingerprint(
        command,
        draft,
        raw_scores=raw_scores,
        tool_evidence_refs=tool_evidence_refs,
    )
    risk_response = RiskVetoDraft(
        contract_version="1.0.0",
        handoff_fingerprint=handoff,
        disposition="REJECTED" if command.risk_scenario == "REJECT" else "ACCEPTED",
        gates=(
            RiskGate(
                gate_id="SYNTHETIC_RISK_VETO",
                status="FAILED" if command.risk_scenario == "REJECT" else "PASSED",
            ),
        ),
        reasons=(
            "SYNTHETIC_RISK_VETO"
            if command.risk_scenario == "REJECT"
            else "SYNTHETIC_RISK_ACCEPTED",
        ),
    )
    if command.failure_mode == "RISK":
        risk_response_payload = risk_response.model_dump(mode="json")
        risk_response_payload["probability"] = "0.01"
        risk_response_json = json.dumps(
            risk_response_payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    else:
        risk_response_json = risk_response.model_dump_json()
    adapter = DeterministicModelAdapter(
        responses=(risk_response_json,),
        capabilities=ModelCapabilities(structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT),
    )
    risk_policy = StaticRunPolicy(
        policy_id="synthetic-risk-veto",
        version="1",
        decisions=(
            {
                PolicyGate.FINAL_OUTPUT: PolicyDecision(
                    action=PolicyAction.REJECT,
                    reason_code="SYNTHETIC_RISK_VETO",
                )
            }
            if command.risk_scenario == "REJECT" and command.failure_mode != "RISK"
            else {}
        ),
    )
    handoff_item = ContextItem(
        item_id=f"research-handoff:{research_run_id}",
        source="immutable-research-handoff",
        content=json.dumps(
            {
                "handoff_fingerprint": handoff,
                "research_run_id": research_run_id,
                "draft": draft.model_dump(mode="json"),
                "raw_scores": tuple(score.model_dump(mode="json") for score in raw_scores),
                "tool_evidence_refs": tool_evidence_refs,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        metadata={"availability": "IMMUTABLE_RESEARCH_HANDOFF"},
    )
    return AgentDefinition.for_adapter(
        definition_id=RISK_DEFINITION_ID,
        version=RISK_DEFINITION_VERSION,
        instructions=RISK_DEFINITION_INSTRUCTIONS,
        model_adapter=adapter,
        context_provider=DeterministicContextProvider(items=(handoff_item,)),
        tools=(),
        output_contract=OutputContract(
            contract_id="synthetic-independent-risk-veto",
            version="1.0.0",
            schema=RiskVetoDraft.model_json_schema(),
            structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
        ),
        run_policy=risk_policy,
    )


def _research_model_response(command: ResearchCommand) -> str:
    members = []
    for member in command.members:
        members.append(
            {
                "security_id": member.security_id,
                "research_id": member.research_id,
                "evidence_refs": [evidence.evidence_id for evidence in member.evidence],
                "thesis": "The fictional thesis is bounded by the frozen evidence.",
                "bull_case": "The fictional upside case remains conditional.",
                "bear_case": "The fictional downside case remains explicit.",
                "knowledge_cutoff": member.knowledge_cutoff.isoformat(),
            }
        )
    response: dict[str, object] = {"contract_version": "1.0.0", "members": members}
    if command.failure_mode in {"RESEARCH", "SYSTEM"}:
        response["probability"] = "0.99"
    return json.dumps(response, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


async def validate_frozen_recovery_case(case: FrozenDecisionCase, runtime: RuntimeStorage) -> None:
    """Require the supplied original snapshot to identify an already durable Run."""
    _assert_runtime_version_bundle(case)
    run = await runtime.run_store.get_run(case.framework_run_id)
    if run is None:
        raise MappedDurableRunMissingError("original durable M-Agent Run is missing")
    definition = (
        _research_definition(case) if case.research is not None else _frozen_definition(case)
    )
    _assert_existing_run_matches_case(run, case, definition)


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
    if case.research is not None:
        return await execute_research_decision_case(
            case,
            runtime,
            record_transition,
            clock=clock,
        )
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
        _assert_missing_run_can_be_created(case)
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


async def execute_research_decision_case(
    case: FrozenDecisionCase,
    runtime: RuntimeStorage,
    record_transition: FrameworkTransitionRecorder | None = None,
    *,
    clock: Clock | None = None,
) -> FrameworkRunResult:
    """Execute one staged research Run followed by an independent risk Run."""
    command = case.research
    assert command is not None
    existing_research_run = await runtime.run_store.get_run(case.framework_run_id)
    research_run_was_succeeded = (
        existing_research_run is not None and existing_research_run.status.value == "SUCCEEDED"
    )
    try:
        _assert_runtime_version_bundle(case)
        research_definition = _research_definition(case)
        _assert_research_registered_capabilities(case, research_definition)
    except ValueError:
        ResultDelivery(runtime.engine, clock=clock).record_capability_denial(case.framework_run_id)
        raise

    research_input = json.dumps(
        case.input,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    research_run = await _execute_registered_run(
        runtime=runtime,
        run_id=case.framework_run_id,
        definition=research_definition,
        input_payload=research_input,
        case=case,
        record_transition=record_transition,
        clock=clock,
    )
    if research_run.status != "SUCCEEDED" or research_run.output is None:
        if command.failure_mode == "DATA":
            return replace(research_run, error_code="RESEARCH_DATA_UNAVAILABLE")
        return research_run
    try:
        draft = ResearchDraft.model_validate_json(research_run.output)
    except ValueError:
        return replace(
            research_run,
            status="FAILED",
            error_code="RESEARCH_OUTPUT_INVALID",
        )

    tool_evidence_refs = await _research_tool_evidence_refs(runtime, research_run.run_id)
    try:
        raw_scores = tuple(
            research_service.freeze_raw_score(command, member) for member in command.members
        )
    except RawScoreCalculationError as error:
        envelope = ResearchFrameworkOutput(
            research_run_id=research_run.run_id,
            risk_run_id=None,
            draft=draft,
            risk_veto=None,
            tool_evidence_refs=tool_evidence_refs,
        )
        return replace(
            research_run,
            output=envelope.model_dump_json(),
            raw_score_error_code=str(error),
        )

    if command.failure_mode == "RAW_SCORE":
        envelope = ResearchFrameworkOutput(
            research_run_id=research_run.run_id,
            risk_run_id=None,
            draft=draft,
            risk_veto=None,
            tool_evidence_refs=tool_evidence_refs,
        )
        return replace(research_run, output=envelope.model_dump_json())

    risk_run_id = risk_run_id_for(
        research_run.run_id,
        draft,
        raw_scores=raw_scores,
        tool_evidence_refs=tool_evidence_refs,
    )
    risk_definition = _risk_definition(
        command,
        draft,
        research_run_id=research_run.run_id,
        raw_scores=raw_scores,
        tool_evidence_refs=tool_evidence_refs,
    )
    _assert_risk_definition(risk_definition)
    risk_input = json.dumps(
        {
            "handoff_fingerprint": handoff_fingerprint(
                command,
                draft,
                raw_scores=raw_scores,
                tool_evidence_refs=tool_evidence_refs,
            ),
            "research_run_id": research_run.run_id,
            "draft": draft.model_dump(mode="json"),
            "raw_scores": tuple(score.model_dump(mode="json") for score in raw_scores),
            "tool_evidence_refs": tool_evidence_refs,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    risk_run = await _execute_registered_run(
        runtime=runtime,
        run_id=risk_run_id,
        definition=risk_definition,
        input_payload=risk_input,
        case=None,
        record_transition=None,
        allow_create=not research_run_was_succeeded,
        clock=clock,
    )
    if risk_run.status == "SUCCEEDED" and risk_run.output is not None:
        try:
            risk_veto = RiskVetoDraft.model_validate_json(risk_run.output)
        except ValueError:
            risk_veto = None
    elif risk_run.status == "REJECTED":
        risk_veto = RiskVetoDraft(
            contract_version="1.0.0",
            handoff_fingerprint=handoff_fingerprint(
                command,
                draft,
                raw_scores=raw_scores,
                tool_evidence_refs=tool_evidence_refs,
            ),
            disposition="REJECTED",
            gates=(
                RiskGate(
                    gate_id="SYNTHETIC_RISK_VETO",
                    status="FAILED",
                ),
            ),
            reasons=(risk_run.error_code or "SYNTHETIC_RISK_VETO",),
        )
    else:
        risk_veto = None
    envelope = ResearchFrameworkOutput(
        research_run_id=research_run.run_id,
        risk_run_id=risk_run.run_id,
        draft=draft,
        risk_veto=risk_veto,
        raw_scores=raw_scores,
        tool_evidence_refs=tool_evidence_refs,
    )
    return replace(
        research_run,
        output=envelope.model_dump_json(),
        risk_run_id=risk_run.run_id,
        risk_run_status=risk_run.status,
        risk_run_error_code=risk_run.error_code,
        risk_transitions=risk_run.transitions,
        risk_transitions_durably_recorded=risk_run.transitions_durably_recorded,
    )


async def _research_tool_evidence_refs(
    runtime: RuntimeStorage,
    run_id: str,
) -> tuple[str, ...]:
    """Recover only successful announcement identities from durable Tool checkpoints."""
    references: list[str] = []
    for checkpoint in await runtime.run_store.get_checkpoints(run_id):
        if checkpoint.step_type is not StepType.TOOL:
            continue
        try:
            outcome = deserialize_tool_outcome(checkpoint.output)
        except ValueError as error:
            raise ValueError("research Tool checkpoint is invalid") from error
        if (
            outcome.tool_name != RESEARCH_TOOL_NAME
            or outcome.result is None
            or outcome.status.value != "SUCCESS"
        ):
            continue
        reference = next(
            (
                token
                for token in outcome.result.split()
                if token.startswith("announcement:") and len(token) > len("announcement:")
            ),
            None,
        )
        if reference is not None and reference not in references:
            references.append(reference)
    return tuple(references)


async def _execute_registered_run(
    *,
    runtime: RuntimeStorage,
    run_id: str,
    definition: AgentDefinition,
    input_payload: str,
    case: FrozenDecisionCase | None,
    record_transition: FrameworkTransitionRecorder | None,
    clock: Clock | None,
    allow_create: bool = True,
) -> FrameworkRunResult:
    """Create/recover one durable Run while preserving the definition snapshot."""
    registry = DefinitionRegistry()
    registry.register(definition)
    status_collector = _RunStatusCollector(run_id, [])
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

    def assert_run_matches(run: RunRecord) -> None:
        if case is not None:
            _assert_existing_run_matches_case(run, case, definition)
            return
        if (
            run.definition_id != definition.definition_id
            or run.definition_version != definition.version
            or run.input != input_payload
            or run.snapshot != definition.frozen_snapshot()
        ):
            raise ValueError("durable auxiliary Run does not match its frozen input")

    try:
        run = await runner.get_run(run_id)
    except RunNotFoundError:
        if case is not None:
            _assert_missing_run_can_be_created(case)
        elif not allow_create:
            raise MappedDurableRunMissingError("mapped auxiliary M-Agent Run is missing") from None
        try:
            created = await runner.create_run(
                definition.definition_id,
                definition.version,
                input_payload,
                run_id=run_id,
            )
            await record_framework_statuses()
            run = await runner.start_run(created.run_id)
            await record_framework_statuses()
        except _CONCURRENT_RUN_RECOVERY_ERRORS:
            run = await _recover_registered_run(
                runner,
                run_id,
                definition,
                input_payload,
                case,
                observe,
            )
            await record_framework_statuses()
        except sqlite3.Error as error:
            if not _is_concurrent_creation_error(error):
                raise
            run = await _recover_registered_run(
                runner,
                run_id,
                definition,
                input_payload,
                case,
                observe,
            )
            await record_framework_statuses()
    else:
        assert_run_matches(run)
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
                run = await _recover_registered_run(
                    runner,
                    run_id,
                    definition,
                    input_payload,
                    case,
                    observe,
                )
            await record_framework_statuses()
    if run.error_code == ModelContractViolationError.code:
        ResultDelivery(runtime.engine, clock=clock).record_capability_denial(run_id)
    return FrameworkRunResult(
        run_id=run.run_id,
        status=cast(FrameworkRunStatus, run.status.value),
        output=run.output,
        waiting_reason=run.waiting_reason,
        error_code=run.error_code,
        transitions=tuple(transitions),
        transitions_durably_recorded=record_transition is not None,
    )


async def _recover_registered_run(
    runner: Runner,
    run_id: str,
    definition: AgentDefinition,
    input_payload: str,
    case: FrozenDecisionCase | None,
    observe: FrameworkTransitionRecorder,
) -> RunRecord:
    """Converge on a concurrently-created auxiliary or primary Run."""
    deadline = asyncio.get_running_loop().time() + DEFAULT_LEASE_TTL.total_seconds()
    while asyncio.get_running_loop().time() < deadline:
        try:
            run = await runner.get_run(run_id)
        except RunNotFoundError:
            await asyncio.sleep(_CONCURRENT_RUN_OBSERVATION_DELAY_SECONDS)
            continue
        if case is not None:
            _assert_existing_run_matches_case(run, case, definition)
        else:
            if (
                run.definition_id != definition.definition_id
                or run.definition_version != definition.version
                or run.input != input_payload
                or run.snapshot != definition.frozen_snapshot()
            ):
                raise ValueError("durable auxiliary Run does not match its frozen input")
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
    raise ValueError("concurrent durable auxiliary Run did not become recoverable")


def _assert_risk_definition(definition: AgentDefinition) -> None:
    if (
        definition.tools
        or definition.context_plan.stages
        or not isinstance(definition.context_provider, DeterministicContextProvider)
        or type(definition.model_adapter) is not DeterministicModelAdapter
        or definition.model_adapters
        or type(definition.run_policy) is not StaticRunPolicy
        or definition.run_policy.identity.policy_id != "synthetic-risk-veto"
        or definition.run_policy.identity.version != "1"
        or definition.compression_contract is not None
    ):
        raise ValueError("undeclared risk framework capability")


def _assert_research_registered_capabilities(
    case: FrozenDecisionCase,
    definition: AgentDefinition,
) -> None:
    """Keep research extensions deterministic and read-only at registration time."""
    if (
        tuple(tool.name for tool in definition.tools)
        != tuple(sorted(RESEARCH_READ_ONLY_TOOL_ALLOWLIST))
        or any(tool.effect is not ToolEffect.READ_ONLY for tool in definition.tools)
        or any(not getattr(tool, "deterministic", False) for tool in definition.tools)
        or type(definition.model_adapter) is not _StagedResearchModelAdapter
        or definition.model_adapters
        or type(definition.run_policy) is not AllowAllRunPolicy
        or definition.compression_contract is not None
        or not definition.context_plan.stages
        or tuple(stage.identity.stage_id for stage in definition.context_plan.stages)
        != RESEARCH_STAGE_IDS
        or tuple(
            (
                stage.identity.stage_id,
                stage.identity.scope,
                stage.input_channels,
                stage.output_channels,
            )
            for stage in definition.context_plan.stages
        )
        != tuple(
            (
                stage_id,
                RESEARCH_STAGE_SCOPES[stage_id],
                input_channels,
                output_channels,
            )
            for stage_id, input_channels, output_channels in RESEARCH_STAGE_CONTRACTS
        )
        or any(
            stage.config is None
            or stage.config.config.get("phase") != stage.identity.stage_id
            or stage.config.config.get("stage_order")
            != RESEARCH_STAGE_IDS.index(stage.identity.stage_id)
            for stage in definition.context_plan.stages
        )
        or not isinstance(definition.context_provider, _FrozenResearchContextProvider)
        or not definition.context_provider.deterministic
        or case.research is None
    ):
        raise ValueError("undeclared research framework capability")


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
    if case.research is not None:
        definition = _research_definition(case)
        _assert_research_registered_capabilities(case, definition)
    else:
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


def _assert_missing_run_can_be_created(case: FrozenDecisionCase) -> None:
    """Reject replacement creation for mapped or historical frozen identities."""
    if case.recovery_framework_run_id is not None:
        raise MappedDurableRunMissingError("mapped durable M-Agent Run is missing") from None
    if case.version_bundle.runtime_release != CURRENT_M_AGENT_RELEASE:
        raise MappedDurableRunMissingError(
            "historical runtime identity requires the original durable Run"
        ) from None


def _assert_runtime_version_bundle(case: FrozenDecisionCase) -> None:
    """Reject frozen metadata that does not describe the installed deterministic route."""
    bundle = case.version_bundle
    definition_version = definition_version_for_case_contract(bundle.case_contract_version)
    if case.research is not None:
        if (
            not supports_case_host_contract(
                bundle.case_contract_version,
                bundle.host_contract_version,
            )
            or not supports_report_projection_contract(bundle.report_projection_contract_version)
            or bundle.agent_definition_id != RESEARCH_DEFINITION_ID
            or bundle.agent_definition_version != definition_version
            or bundle.output_contract_version != RESEARCH_OUTPUT_CONTRACT_VERSION
            or bundle.model_adapter_id != RESEARCH_MODEL_ADAPTER_ID
            or bundle.routing_policy_version != RESEARCH_ROUTING_POLICY_VERSION
            or case.agent_definition.definition_id != RESEARCH_DEFINITION_ID
            or case.agent_definition.version != definition_version
            or case.agent_definition.model_adapter_id != RESEARCH_MODEL_ADAPTER_ID
            or case.agent_definition.instructions != RESEARCH_DEFINITION_INSTRUCTIONS
            or case.agent_definition.output_contract.contract_id != RESEARCH_OUTPUT_CONTRACT_ID
            or case.agent_definition.output_contract.version != RESEARCH_OUTPUT_CONTRACT_VERSION
            or case.agent_definition.output_contract.json_schema
            != ResearchDraft.model_json_schema()
        ):
            raise ValueError("research version bundle is not supported by this runtime")
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
            raise ValueError("full research version bundle or scoped case is invalid") from error
        return
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
