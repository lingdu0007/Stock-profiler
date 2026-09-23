"""Boundary adapter that converts M-Agent Runs into Stock Profiler values."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from importlib.metadata import version
from typing import Literal, cast

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
    PolicyIdentity,
    PolicyRequest,
    Runner,
    RunNotFoundError,
    RunPolicy,
    RunRecord,
    StaleRunVersionError,
    StepType,
    StructuredOutputMode,
    ToolCall,
    ToolCallingMode,
    ToolEffect,
    ToolOutcome,
    ToolRequest,
    deserialize_model_response,
    deserialize_tool_outcome,
    parse_stage_result,
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
    AuxiliaryRunReservationRecorder,
    ResearchMemberRunReservationRecorder,
    ResearchMemberRunResult,
)
from stock_profiler.modules.decision_cases.ports import FrameworkRunResult as FrameworkRunResult
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
from stock_profiler.modules.research.contracts import (
    RESEARCH_ANNOUNCEMENT_TOOL_VERSION,
    RESEARCH_CONTRACT_VERSION,
    RESEARCH_DEFINITION_ID,
    RESEARCH_DEFINITION_VERSION,
    RESEARCH_EVIDENCE_CONTRACT_VERSION,
    RESEARCH_LEGACY_ANNOUNCEMENT_TOOL_VERSION,
    RESEARCH_LEGACY_DEFINITION_VERSION,
    RESEARCH_LEGACY_EVIDENCE_CONTRACT_VERSION,
    RESEARCH_LEGACY_OUTPUT_CONTRACT_VERSION,
    RESEARCH_LEGACY_ROUTING_POLICY_VERSION,
    RESEARCH_MODEL_ADAPTER_ID,
    RESEARCH_OUTPUT_CONTRACT_ID,
    RESEARCH_OUTPUT_CONTRACT_VERSION,
    RESEARCH_PRIOR_DEFINITION_VERSION,
    RESEARCH_PRIOR_OUTPUT_CONTRACT_VERSION,
    RESEARCH_REQUIRED_DATA_TYPES,
    RESEARCH_ROUTING_POLICY_VERSION,
    RISK_DEFINITION_ID,
    RISK_DEFINITION_VERSION,
    ResearchCommand,
    ResearchDataManifest,
    ResearchDraft,
    ResearchDraftMember,
    ResearchMemberInput,
    ResearchRiskPlan,
    ResearchStageArtifact,
    ResearchToolEvidence,
    RiskGate,
    RiskMemberVeto,
    RiskVetoDraft,
    decode_historical_research_draft_member,
    decode_legacy_research_member_input,
    decode_legacy_research_stage_artifact,
    decode_legacy_research_tool_evidence,
    historical_research_draft_member_json_schema,
    legacy_research_draft_json_schema,
    research_draft_payload,
    research_evidence_payload,
    research_stage_artifact_payload,
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
    "Run one structured single-stock research definition for the assigned fixed-ten member. "
    "Use the required structured facts delivered by Context Provider before model steps. "
    "The explicit stages are collect, analyze, bull-bear, and draft. "
    "Use only the declared read-only announcement Tool for optional exploration. "
    "Return only the typed single-stock research draft. Do not output scores, probabilities, "
    "qualification, "
    "personal quantities, order, or trading conclusions."
)
LEGACY_RESEARCH_DEFINITION_INSTRUCTIONS = (
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
    "analyze": ContextScope.RUN_INPUT,
    "bull-bear": ContextScope.RUN_INPUT,
    "draft": ContextScope.RUN_INPUT,
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


class _RiskVerdictRunPolicy(RunPolicy):  # type: ignore[misc]
    """Make a typed risk rejection the durable Run terminal verdict."""

    @property
    def identity(self) -> PolicyIdentity:
        return PolicyIdentity(
            policy_id="synthetic-risk-veto",
            version="1",
            fingerprint="synthetic-risk-veto-verdict-v1",
        )

    def evaluate(self, request: PolicyRequest) -> PolicyDecision:
        if request.gate is not PolicyGate.FINAL_OUTPUT:
            return PolicyDecision(action=PolicyAction.ALLOW, reason_code="ALLOWED")
        output = request.payload.get("output")
        if isinstance(output, str):
            try:
                payload = json.loads(output)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and payload.get("disposition") == "REJECTED":
                return PolicyDecision(
                    action=PolicyAction.REJECT,
                    reason_code="SYNTHETIC_RISK_VETO",
                )
        return PolicyDecision(action=PolicyAction.ALLOW, reason_code="ALLOWED")


class _FrozenResearchContextProvider(ContextProvider):  # type: ignore[misc]
    """Deterministic required-fact Provider with an explicit data-failure mode."""

    deterministic = True

    def __init__(
        self,
        items: tuple[ContextItem, ...],
        *,
        runtime: RuntimeStorage | None = None,
        run_id: str | None = None,
        expected_members: tuple[ResearchMemberInput, ...] = (),
        fail: bool = False,
        legacy: bool = False,
        historical: bool = False,
        historical_with_debate_fields: bool = False,
    ) -> None:
        self._items = items
        self._runtime = runtime
        self._run_id = run_id
        self._expected_members = expected_members
        self._fail = fail
        self._legacy = legacy
        self._historical = historical
        self._historical_with_debate_fields = historical_with_debate_fields
        self._failure_code: str | None = None

    @property
    def failure_code(self) -> str | None:
        return self._failure_code

    async def provide(self, request: ContextRequest) -> tuple[ContextItem, ...]:
        del request
        if self._fail:
            self._failure_code = "RESEARCH_DATA_UNAVAILABLE"
            raise RuntimeError("RESEARCH_DATA_UNAVAILABLE")
        if self._runtime is None or self._run_id is None:
            try:
                _validate_research_context_items(
                    self._items,
                    self._expected_members,
                    legacy=self._legacy,
                )
            except ValueError as error:
                self._failure_code = "RESEARCH_REQUIRED_FACTS_INCOMPLETE"
                raise RuntimeError(str(error)) from error
            return self._items
        checkpoints = await self._runtime.run_store.get_checkpoints(self._run_id)
        stage_results = tuple(
            result
            for checkpoint in checkpoints
            if checkpoint.step_type is StepType.CONTEXT
            if (result := parse_stage_result(checkpoint.output)) is not None
            if result.stage_id in RESEARCH_STAGE_IDS
        )
        completed_stage_ids = {result.stage_id for result in stage_results}
        active_stage_index = next(
            (
                index
                for index, stage_id in enumerate(RESEARCH_STAGE_IDS)
                if stage_id not in completed_stage_ids
            ),
            len(RESEARCH_STAGE_IDS),
        )
        if active_stage_index == 0:
            try:
                _validate_research_context_items(
                    self._items,
                    self._expected_members,
                    legacy=self._legacy,
                )
            except ValueError as error:
                self._failure_code = "RESEARCH_REQUIRED_FACTS_INCOMPLETE"
                raise RuntimeError(str(error)) from error
            return self._items
        if active_stage_index >= len(RESEARCH_STAGE_IDS):
            return ()
        previous_stage_id = RESEARCH_STAGE_IDS[active_stage_index - 1]
        previous_results = tuple(
            result for result in stage_results if result.stage_id == previous_stage_id
        )
        if not previous_results:
            raise RuntimeError("RESEARCH_STAGE_INPUT_UNAVAILABLE")
        previous = max(previous_results, key=lambda result: result.boundary)
        if not previous.output_items:
            raise RuntimeError("RESEARCH_STAGE_INPUT_UNAVAILABLE")
        stage_id = RESEARCH_STAGE_IDS[active_stage_index]
        input_item_ids = tuple(item.item.item_id for item in previous.output_items)
        security_ids: list[str] = []
        evidence_ids: list[str] = []
        for output_item in previous.output_items:
            try:
                previous_payload = json.loads(output_item.item.content)
            except json.JSONDecodeError as error:
                raise RuntimeError("RESEARCH_STAGE_INPUT_INVALID") from error
            if "stage_artifact" in previous_payload:
                previous_artifact = (
                    decode_legacy_research_stage_artifact(previous_payload["stage_artifact"])
                    if self._legacy or self._historical
                    else ResearchStageArtifact.model_validate(previous_payload["stage_artifact"])
                )
                security_ids.extend(previous_artifact.security_ids)
                evidence_ids.extend(previous_artifact.evidence_ids)
                continue
            security_id = previous_payload.get("security_id")
            if isinstance(security_id, str):
                security_ids.append(security_id)
            for evidence in previous_payload.get("evidence", ()):
                evidence_id = evidence.get("evidence_id")
                if isinstance(evidence_id, str):
                    evidence_ids.append(evidence_id)
        if not security_ids or not evidence_ids:
            raise RuntimeError("RESEARCH_STAGE_INPUT_UNAVAILABLE")
        stage_artifact = ResearchStageArtifact(
            stage_id=cast(Literal["analyze", "bull-bear", "draft"], stage_id),
            source_stage_id=cast(
                Literal["collect", "analyze", "bull-bear"],
                previous.stage_id,
            ),
            input_item_ids=input_item_ids,
            security_ids=tuple(dict.fromkeys(security_ids)),
            evidence_ids=tuple(dict.fromkeys(evidence_ids)),
            summary=f"Synthetic {stage_id} stage consumed the prior frozen stage.",
            bull_case=(
                "The synthetic evidence supports a conditional upside case."
                if stage_id in {"bull-bear", "draft"}
                else None
            ),
            bear_case=(
                "The synthetic evidence preserves a conditional downside case."
                if stage_id in {"bull-bear", "draft"}
                else None
            ),
            catalysts=(
                ("A fictional catalyst remains conditional.",)
                if stage_id == "draft"
                else ()
            ),
            falsification_conditions=(
                ("A frozen downside fact would falsify the thesis.",)
                if stage_id == "draft"
                else ()
            ),
            unknowns=(
                ("Future external evidence remains unresolved.",)
                if stage_id == "draft"
                else ()
            ),
        )
        content = json.dumps(
            {
                "stage": stage_id,
                "source_stage": previous.stage_id,
                "input_item_ids": input_item_ids,
                "stage_artifact": research_stage_artifact_payload(
                    stage_artifact,
                    legacy=self._legacy
                    or (self._historical and not self._historical_with_debate_fields),
                ),
                "source_items": tuple(
                    item.item.model_dump(mode="json") for item in previous.output_items
                ),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return (
            ContextItem(
                item_id=f"research-stage-output:{stage_id}:0",
                source="synthetic-research-stage",
                content=content,
                metadata={
                    "stage": stage_id,
                    "source_stage": previous.stage_id,
                    "source_item_ids": tuple(item.item.item_id for item in previous.output_items),
                },
            ),
        )


def _validate_research_context_items(
    items: tuple[ContextItem, ...],
    expected_members: tuple[ResearchMemberInput, ...],
    *,
    legacy: bool = False,
) -> None:
    """Verify that the Provider will deliver every member's complete manifest."""
    expected_security_ids = tuple(member.security_id for member in expected_members)
    expected_members_by_security_id = {member.security_id: member for member in expected_members}
    if len(items) != len(expected_security_ids):
        raise ValueError("RESEARCH_REQUIRED_FACTS_INCOMPLETE: member coverage")
    actual_security_ids: list[str] = []
    for item in items:
        if item.metadata.get("availability") != "REQUIRED_BEFORE_MODEL":
            raise ValueError("RESEARCH_REQUIRED_FACTS_INCOMPLETE: availability")
        try:
            payload = json.loads(item.content)
            security_id = payload["security_id"]
            evidence_ids = tuple(evidence["evidence_id"] for evidence in payload["evidence"])
            manifest = ResearchDataManifest.model_validate(payload["data_manifest"])
            delivered_member = (
                decode_legacy_research_member_input(payload)
                if legacy
                else ResearchMemberInput.model_validate(payload)
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("RESEARCH_REQUIRED_FACTS_INCOMPLETE: malformed item") from error
        expected_member = expected_members_by_security_id.get(security_id)
        if expected_member is None or delivered_member != expected_member:
            raise ValueError("RESEARCH_REQUIRED_FACTS_INCOMPLETE: structured fact provenance")
        manifest_evidence_ids = tuple(
            evidence_id for entry in manifest.entries for evidence_id in entry.evidence_ids
        )
        if set(manifest_evidence_ids) != set(evidence_ids):
            raise ValueError("RESEARCH_REQUIRED_FACTS_INCOMPLETE: evidence coverage")
        incomplete_data_types = tuple(
            entry.data_type for entry in manifest.entries if entry.completeness != "COMPLETE"
        )
        if incomplete_data_types:
            raise ValueError(
                "RESEARCH_REQUIRED_FACTS_INCOMPLETE: " + ",".join(incomplete_data_types)
            )
        if all(entry.completeness == "COMPLETE" for entry in manifest.entries) and any(
            value is None for value in delivered_member.structured_signals.values()
        ):
            raise ValueError("RESEARCH_REQUIRED_FACTS_INCOMPLETE: structured signals")
        actual_security_ids.append(security_id)
    if tuple(actual_security_ids) != expected_security_ids:
        raise ValueError("RESEARCH_REQUIRED_FACTS_INCOMPLETE: security coverage")


def _recover_research_failure_code(
    command: ResearchCommand,
    research_run: FrameworkRunResult,
    context_provider: _FrozenResearchContextProvider,
    *,
    expected_members: tuple[ResearchMemberInput, ...] | None = None,
    context_items: tuple[ContextItem, ...] | None = None,
    legacy: bool = False,
) -> str | None:
    """Recover frozen data-failure identity after a terminal Run restart.

    M-Agent intentionally redacts unknown Provider exception codes in durable
    attempts. The immutable command is still sufficient to replay the
    required-facts validation without consulting transient Provider state.
    """
    known_codes = {"RESEARCH_DATA_UNAVAILABLE", "RESEARCH_REQUIRED_FACTS_INCOMPLETE"}
    for code in (context_provider.failure_code, research_run.error_code):
        if code in known_codes:
            return code
    if command.failure_mode == "DATA":
        return "RESEARCH_DATA_UNAVAILABLE"
    try:
        _validate_research_context_items(
            context_items if context_items is not None else _research_context_items(command),
            expected_members if expected_members is not None else command.members,
            legacy=legacy,
        )
    except ValueError as error:
        if str(error).startswith("RESEARCH_REQUIRED_FACTS_INCOMPLETE"):
            return "RESEARCH_REQUIRED_FACTS_INCOMPLETE"
    return None


def _research_member_run_id(
    case: FrozenDecisionCase,
    index: int,
    member: ResearchMemberInput,
) -> str:
    """Keep the host mapping as the first member Run and hash the others."""
    if index == 0:
        return case.framework_run_id
    digest = sha256(
        f"{case.framework_run_id}:{member.security_id}:{member.research_id}".encode()
    ).hexdigest()
    return f"research-member-run-{digest}"


def _research_member_input_payload(
    case: FrozenDecisionCase,
    member: ResearchMemberInput,
) -> str:
    """Bind each durable member Run to the complete frozen case and member identity."""
    return json.dumps(
        {
            "case_input": case.input,
            "member": member.model_dump(mode="json"),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _json_object(content: str) -> dict[str, object] | None:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


class _StagedResearchModelAdapter(DeterministicModelAdapter):  # type: ignore[misc]
    """Drive the frozen research draft with optional evidence exploration."""

    deterministic = True

    def __init__(
        self,
        draft_response: str,
        *,
        command: ResearchCommand | None = None,
        member: ResearchMemberInput | None = None,
        legacy: bool = False,
        historical: bool = False,
        historical_with_debate_fields: bool = False,
    ) -> None:
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
        self._command = command
        self._member = member
        self._legacy = legacy
        self._historical = historical
        self._historical_with_debate_fields = historical_with_debate_fields

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        if not request.tool_outcomes:
            for item in request.context_items:
                payload = _json_object(item.content)
                if not payload or not payload.get("risk_flags"):
                    continue
                security_id = payload.get("security_id")
                if isinstance(security_id, str) and security_id:
                    return ModelResponse(
                        tool_calls=(
                            ToolCall(
                                call_id="research-exploration-1",
                                tool_name=RESEARCH_TOOL_NAME,
                                arguments=json.dumps(
                                    {"security_id": security_id},
                                    ensure_ascii=True,
                                    separators=(",", ":"),
                                    sort_keys=True,
                                ),
                            ),
                        )
                    )
        if self._command is None:
            return ModelResponse(content=self._responses[0])
        draft_artifact: ResearchStageArtifact | None = None
        for item in request.context_items:
            payload = _json_object(item.content)
            if not payload or payload.get("stage") != "draft":
                continue
            try:
                candidate = (
                    decode_legacy_research_stage_artifact(payload["stage_artifact"])
                    if self._legacy or self._historical
                    else ResearchStageArtifact.model_validate(payload["stage_artifact"])
                )
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError("RESEARCH_STAGE_INPUT_INVALID") from error
            input_item_ids = payload.get("input_item_ids")
            context_item_ids = {context_item.item_id for context_item in request.context_items}
            if (
                candidate.stage_id != "draft"
                or item.metadata.get("source_stage") != "bull-bear"
                or not isinstance(input_item_ids, list)
                or tuple(input_item_ids) != candidate.input_item_ids
                or not set(candidate.input_item_ids).issubset(context_item_ids)
                or set(candidate.security_ids)
                != {
                    self._member.security_id
                    if self._member is not None
                    else member.security_id
                    for member in self._command.members
                }
                or set(candidate.evidence_ids)
                != {
                    evidence.evidence_id
                    for member in (
                        (self._member,)
                        if self._member is not None
                        else self._command.members
                    )
                    for evidence in member.evidence
                }
            ):
                raise RuntimeError("RESEARCH_STAGE_INPUT_INVALID")
            draft_artifact = candidate
            break
        if draft_artifact is None:
            raise RuntimeError("RESEARCH_STAGE_INPUT_UNAVAILABLE")
        return ModelResponse(
            content=_research_model_response(
                self._command,
                draft_artifact=draft_artifact,
                member=self._member,
                legacy=self._legacy,
                historical=self._historical,
                historical_with_debate_fields=self._historical_with_debate_fields,
            )
        )


def _read_announcement_tool(
    knowledge_cutoff: datetime | None = None,
    *,
    semantic_version: str = RESEARCH_ANNOUNCEMENT_TOOL_VERSION,
) -> DeterministicTool:
    default_cutoff = knowledge_cutoff or datetime(2042, 5, 31, 23, 59, 59, tzinfo=UTC)
    legacy = semantic_version == RESEARCH_LEGACY_ANNOUNCEMENT_TOOL_VERSION

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
        evidence_payload = {
            "evidence_id": f"announcement:{security_id}",
            "source": "synthetic-announcement-feed",
            "reference": f"synthetic://announcement/{security_id}",
            "statement": (
                f"Synthetic announcement evidence announcement:{security_id} "
                "is read-only and contains no trade instruction."
            ),
            "evidence_contract_version": (
                RESEARCH_LEGACY_EVIDENCE_CONTRACT_VERSION
                if legacy
                else RESEARCH_EVIDENCE_CONTRACT_VERSION
            ),
            "effective_at": None if legacy else default_cutoff,
            "source_published_at": None if legacy else default_cutoff,
            "acquired_at": default_cutoff,
            "validated_at": default_cutoff,
            "knowledge_cutoff": default_cutoff,
            "semantic_version": semantic_version,
            "validation_status": "VALIDATED",
        }
        evidence = (
            decode_legacy_research_tool_evidence(evidence_payload)
            if legacy
            else ResearchToolEvidence.model_validate(evidence_payload)
        )
        payload = research_evidence_payload(evidence, legacy=legacy)
        return ToolOutcome.success(
            request.call_id,
            request.tool_name,
            json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        )

    return DeterministicTool(
        name=RESEARCH_TOOL_NAME,
        description="Read one synthetic announcement without changing external state.",
        effect=ToolEffect.READ_ONLY,
        parameters=RESEARCH_TOOL_PARAMETERS,
        handler=handler,
    )


def _research_context_items(
    command: ResearchCommand,
    *,
    legacy: bool = False,
) -> tuple[ContextItem, ...]:
    return tuple(
        ContextItem(
            item_id=f"required-facts:{member.security_id}",
            source="synthetic-required-fact-provider",
            content=json.dumps(
                {
                    "security_id": member.security_id,
                    "research_id": member.research_id,
                    "knowledge_cutoff": member.knowledge_cutoff.isoformat(),
                    "evidence": tuple(
                        research_evidence_payload(evidence, legacy=legacy)
                        for evidence in member.evidence
                    ),
                    "data_manifest": member.data_manifest.model_dump(mode="json"),
                    "structured_facts": member.structured_facts.model_dump(mode="json"),
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
                "required_data_types": RESEARCH_REQUIRED_DATA_TYPES,
            },
        )
        for member in command.members
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


def _historical_research_draft_has_debate_fields(case: FrozenDecisionCase) -> bool:
    """Accept either persisted prior-current research member schema."""
    schema = case.agent_definition.output_contract.json_schema
    if schema == ResearchDraftMember.model_json_schema():
        return True
    if schema == historical_research_draft_member_json_schema():
        return False
    raise ValueError("historical research output schema is not supported by this runtime")


def _research_definition(
    case: FrozenDecisionCase,
    *,
    runtime: RuntimeStorage | None = None,
    run_id: str | None = None,
    member: ResearchMemberInput | None = None,
    context_items: tuple[ContextItem, ...] | None = None,
    historical: bool = False,
) -> AgentDefinition:
    """Register one staged research Definition with a narrow capability surface."""
    command = case.research
    assert command is not None
    historical_with_debate_fields = (
        historical and _historical_research_draft_has_debate_fields(case)
    )
    adapter = _StagedResearchModelAdapter(
        _research_model_response(
            command,
            member=member,
            historical=historical,
            historical_with_debate_fields=historical_with_debate_fields,
        ),
        command=command,
        member=member,
        legacy=False,
        historical=historical,
        historical_with_debate_fields=historical_with_debate_fields,
    )
    expected_members = (member,) if member is not None else command.members
    return AgentDefinition.for_adapter(
        definition_id=RESEARCH_DEFINITION_ID,
        version=(
            RESEARCH_PRIOR_DEFINITION_VERSION if historical else RESEARCH_DEFINITION_VERSION
        ),
        instructions=RESEARCH_DEFINITION_INSTRUCTIONS,
        model_adapter=adapter,
        context_provider=_FrozenResearchContextProvider(
            context_items if context_items is not None else _research_context_items(command),
            runtime=runtime,
            run_id=run_id,
            expected_members=expected_members,
            fail=command.failure_mode == "DATA",
            legacy=False,
            historical=historical,
            historical_with_debate_fields=historical_with_debate_fields,
        ),
        tools=(_read_announcement_tool(command.knowledge_cutoff),),
        output_contract=OutputContract(
            contract_id=RESEARCH_OUTPUT_CONTRACT_ID,
            version=(
                RESEARCH_PRIOR_OUTPUT_CONTRACT_VERSION
                if historical
                else RESEARCH_OUTPUT_CONTRACT_VERSION
            ),
            schema=(
                (
                    ResearchDraftMember.model_json_schema()
                    if historical_with_debate_fields
                    else historical_research_draft_member_json_schema()
                )
                if historical
                else ResearchDraftMember.model_json_schema()
            ),
            structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
        ),
        context_plan=_research_context_plan(),
    )


def _legacy_research_definition(
    case: FrozenDecisionCase,
    *,
    runtime: RuntimeStorage | None = None,
    run_id: str | None = None,
) -> AgentDefinition:
    """Reconstruct the pre-member-Run Definition for historical Run recovery."""
    command = case.research
    assert command is not None
    return AgentDefinition.for_adapter(
        definition_id=RESEARCH_DEFINITION_ID,
        version=RESEARCH_LEGACY_DEFINITION_VERSION,
        instructions=LEGACY_RESEARCH_DEFINITION_INSTRUCTIONS,
        model_adapter=_StagedResearchModelAdapter(
            _research_model_response(command, legacy=True),
            command=command,
            legacy=True,
        ),
        context_provider=_FrozenResearchContextProvider(
            _research_context_items(command, legacy=True),
            runtime=runtime,
            run_id=run_id,
            expected_members=command.members,
            fail=command.failure_mode == "DATA",
            legacy=True,
        ),
        tools=(
            _read_announcement_tool(
                command.knowledge_cutoff,
                semantic_version=RESEARCH_LEGACY_ANNOUNCEMENT_TOOL_VERSION,
            ),
        ),
        output_contract=OutputContract(
            contract_id=RESEARCH_OUTPUT_CONTRACT_ID,
            version=RESEARCH_LEGACY_OUTPUT_CONTRACT_VERSION,
            schema=legacy_research_draft_json_schema(),
            structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
        ),
        context_plan=_research_context_plan(),
    )


def _risk_definition(
    command: ResearchCommand,
    *,
    research_run_id: str,
    risk_plan: ResearchRiskPlan,
) -> AgentDefinition:
    """Register the independent risk Definition over an immutable handoff."""
    handoff = risk_plan.handoff_fingerprint
    rejected_member_ids = (
        {member.security_id for member in command.members}
        if command.risk_scenario == "REJECT"
        else set(command.risk_rejected_member_ids)
    )
    risk_rejected = bool(rejected_member_ids)
    risk_response = RiskVetoDraft(
        contract_version="1.0.0",
        handoff_fingerprint=handoff,
        disposition="REJECTED" if risk_rejected else "ACCEPTED",
        gates=(
            RiskGate(
                gate_id="SYNTHETIC_RISK_VETO",
                status="FAILED" if risk_rejected else "PASSED",
            ),
        ),
        reasons=("SYNTHETIC_RISK_VETO" if risk_rejected else "SYNTHETIC_RISK_ACCEPTED",),
        member_vetoes=tuple(
            RiskMemberVeto(
                security_id=member.security_id,
                research_id=member.research_id,
                disposition=(
                    "REJECTED" if member.security_id in rejected_member_ids else "ACCEPTED"
                ),
                gates=(
                    RiskGate(
                        gate_id="SYNTHETIC_RISK_VETO",
                        status=(
                            "FAILED" if member.security_id in rejected_member_ids else "PASSED"
                        ),
                    ),
                ),
                reasons=(
                    "SYNTHETIC_RISK_VETO"
                    if member.security_id in rejected_member_ids
                    else "SYNTHETIC_RISK_ACCEPTED",
                ),
            )
            for member in command.members
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
    risk_policy = _RiskVerdictRunPolicy()
    handoff_item = ContextItem(
        item_id=f"research-handoff:{research_run_id}",
        source="immutable-research-handoff",
        content=risk_plan.input_payload,
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


def _research_model_response(
    command: ResearchCommand,
    *,
    draft_artifact: ResearchStageArtifact | None = None,
    member: ResearchMemberInput | None = None,
    legacy: bool = False,
    historical: bool = False,
    historical_with_debate_fields: bool = False,
) -> str:
    draft_summary = (
        draft_artifact.summary
        if draft_artifact is not None
        else "The fictional thesis is bounded by the frozen evidence."
    )
    bull_case = (
        draft_artifact.bull_case
        if draft_artifact is not None and draft_artifact.bull_case is not None
        else "The fictional upside case remains conditional."
    )
    bear_case = (
        draft_artifact.bear_case
        if draft_artifact is not None and draft_artifact.bear_case is not None
        else "The fictional downside case remains explicit."
    )
    catalysts = (
        draft_artifact.catalysts
        if draft_artifact is not None and draft_artifact.catalysts
        else ("A fictional catalyst remains conditional.",)
    )
    falsification_conditions = (
        draft_artifact.falsification_conditions
        if draft_artifact is not None and draft_artifact.falsification_conditions
        else ("A frozen downside fact would falsify the thesis.",)
    )
    unknowns = (
        draft_artifact.unknowns
        if draft_artifact is not None and draft_artifact.unknowns
        else ("Future external evidence remains unresolved.",)
    )

    def member_payload(source: ResearchMemberInput) -> dict[str, object]:
        payload: dict[str, object] = {
            "security_id": source.security_id,
            "research_id": source.research_id,
            "evidence_refs": [evidence.evidence_id for evidence in source.evidence],
            "thesis": (
                f"Synthetic draft stage consumed: {draft_summary}"
                if draft_artifact is not None
                else draft_summary
            ),
            "bull_case": bull_case,
            "bear_case": bear_case,
            "knowledge_cutoff": source.knowledge_cutoff.isoformat(),
        }
        if not legacy and (not historical or historical_with_debate_fields):
            payload.update(
                {
                    "catalysts": catalysts,
                    "falsification_conditions": falsification_conditions,
                    "unknowns": unknowns,
                }
            )
        return payload

    if member is not None:
        response = member_payload(member)
    else:
        response = {
            "contract_version": "1.0.0",
            "members": [member_payload(source) for source in command.members],
        }
    if command.failure_mode in {"RESEARCH", "SYSTEM"}:
        response["probability"] = "0.99"
    return json.dumps(response, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


async def validate_frozen_recovery_case(case: FrozenDecisionCase, runtime: RuntimeStorage) -> None:
    """Require the supplied original snapshot to identify an already durable Run."""
    _assert_runtime_version_bundle(case)
    if _is_legacy_research_case(case):
        run = await runtime.run_store.get_run(case.framework_run_id)
        if run is None:
            raise MappedDurableRunMissingError("original durable research Run is missing")
        _assert_existing_run_matches_case(
            run,
            case,
            _legacy_research_definition(
                case,
                runtime=runtime,
                run_id=case.framework_run_id,
            ),
        )
        return
    if case.research is not None:
        context_items = _research_context_items(case.research)
        for index, member in enumerate(case.research.members):
            run_id = _research_member_run_id(case, index, member)
            run = await runtime.run_store.get_run(run_id)
            if run is None:
                raise MappedDurableRunMissingError(
                    "mapped research member Run is missing"
                )
            member_context_items = tuple(
                item
                for item in context_items
                if item.item_id == f"required-facts:{member.security_id}"
            )
            definition = _research_definition(
                case,
                runtime=runtime,
                run_id=run_id,
                member=member,
                context_items=member_context_items,
                historical=_is_historical_research_case(case),
            )
            _assert_existing_run_matches_definition_input(
                run,
                definition,
                _research_member_input_payload(case, member),
            )
        return
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
            (
                _legacy_research_definition(
                    legacy_case,
                    runtime=runtime,
                    run_id=legacy_case.framework_run_id,
                )
                if _is_legacy_research_case(legacy_case)
                else _frozen_definition(legacy_case)
            ),
        )
        candidates[run.run_id] = legacy_case
    if len(candidates) > 1:
        raise ValueError("multiple durable M-Agent Runs match the legacy frozen input")
    if case.research is not None and not _is_legacy_research_case(case):
        expected_member_run_ids = {
            _research_member_run_id(case, index, member)
            for index, member in enumerate(case.research.members)
        }
        member_runs = [
            await runtime.run_store.get_run(run_id) for run_id in expected_member_run_ids
        ]
        if all(run is not None for run in member_runs):
            try:
                uri = runtime.m_agent_run_store_path.resolve().as_uri() + "?mode=ro"
                with closing(sqlite3.connect(uri, uri=True)) as connection:
                    research_run_ids = {
                        row[0]
                        for row in connection.execute(
                            "SELECT run_id FROM runs WHERE definition_id = ?",
                            (case.agent_definition.definition_id,),
                        )
                    }
            except sqlite3.Error as error:
                raise ValueError("durable framework inventory cannot be verified") from error
            if research_run_ids == expected_member_run_ids:
                await validate_frozen_recovery_case(case, runtime)
                return case
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
    record_auxiliary_run_reservation: AuxiliaryRunReservationRecorder | None = None,
    clock: Clock | None = None,
) -> FrameworkRunResult:
    """Create or reuse the exact durable Run for one frozen host identity."""
    if case.research is not None:
        return await execute_research_decision_case(
            case,
            runtime,
            record_transition,
            record_auxiliary_run_reservation=record_auxiliary_run_reservation,
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


async def _execute_legacy_research_run(
    case: FrozenDecisionCase,
    runtime: RuntimeStorage,
    record_transition: FrameworkTransitionRecorder | None,
    *,
    clock: Clock | None,
) -> FrameworkRunResult:
    """Recover the historical aggregate research Run without rebinding its contract."""
    command = case.research
    assert command is not None
    existing_research_run = await runtime.run_store.get_run(case.framework_run_id)
    if existing_research_run is None:
        raise MappedDurableRunMissingError("original durable research Run is missing") from None
    research_definition = _legacy_research_definition(
        case,
        runtime=runtime,
        run_id=case.framework_run_id,
    )
    _assert_research_registered_capabilities(case, research_definition)
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
        context_provider = research_definition.context_provider
        if isinstance(context_provider, _FrozenResearchContextProvider):
            failure_code = _recover_research_failure_code(
                command,
                research_run,
                context_provider,
                legacy=True,
            )
            if failure_code is not None:
                return replace(research_run, error_code=failure_code)
        return research_run
    tool_evidence = await _research_tool_evidence(
        runtime,
        research_run.run_id,
        legacy=True,
    )
    return replace(
        research_run,
        run_existed_before=True,
        research_tool_evidence=tool_evidence,
    )


async def execute_research_run(
    case: FrozenDecisionCase,
    runtime: RuntimeStorage,
    record_transition: FrameworkTransitionRecorder | None = None,
    *,
    record_member_run_reservation: ResearchMemberRunReservationRecorder | None = None,
    clock: Clock | None = None,
) -> FrameworkRunResult:
    """Execute or recover one staged, durable research Run per cohort member."""
    command = case.research
    assert command is not None
    try:
        _assert_runtime_version_bundle(case)
    except ValueError:
        ResultDelivery(runtime.engine, clock=clock).record_capability_denial(case.framework_run_id)
        raise

    if _is_legacy_research_case(case):
        return await _execute_legacy_research_run(
            case,
            runtime,
            record_transition,
            clock=clock,
        )

    historical = _is_historical_research_case(case)
    context_items = _research_context_items(command)
    member_results: list[ResearchMemberRunResult] = []
    member_drafts: list[ResearchDraftMember] = []
    member_draft_indexes: list[int] = []
    member_run_ids: list[str] = []
    transitions: list[FrameworkRunTransition] = []
    primary_transitions: list[FrameworkRunTransition] = []
    tool_evidence: list[ResearchToolEvidence] = []
    all_runs_existed = True

    for index, member in enumerate(command.members):
        run_id = _research_member_run_id(case, index, member)
        member_run_ids.append(run_id)
        member_context_items = tuple(
            item for item in context_items if item.item_id == f"required-facts:{member.security_id}"
        )
        research_definition = _research_definition(
            case,
            runtime=runtime,
            run_id=run_id,
            member=member,
            context_items=member_context_items,
            historical=historical,
        )
        _assert_research_registered_capabilities(case, research_definition)
        existing_run = await runtime.run_store.get_run(run_id)
        all_runs_existed = all_runs_existed and existing_run is not None
        member_input = _research_member_input_payload(case, member)
        allow_create = (
            not historical
            and
            case.version_bundle.runtime_release == CURRENT_M_AGENT_RELEASE
            and (
                case.recovery_framework_run_id is None
                or index > 0
            )
        )
        if existing_run is None and allow_create and record_member_run_reservation is not None:
            allow_create = await record_member_run_reservation(run_id)
        result = await _execute_registered_run(
            runtime=runtime,
            run_id=run_id,
            definition=research_definition,
            input_payload=member_input,
            case=None,
            record_transition=record_transition,
            allow_create=allow_create,
            clock=clock,
        )
        if result.status != "SUCCEEDED" or result.output is None:
            context_provider = research_definition.context_provider
            if isinstance(context_provider, _FrozenResearchContextProvider):
                failure_code = _recover_research_failure_code(
                    command,
                    result,
                    context_provider,
                    expected_members=(member,),
                    context_items=member_context_items,
                )
                if failure_code is not None:
                    result = replace(result, error_code=failure_code)
        transitions.extend(result.transitions)
        if index == 0:
            primary_transitions.extend(result.transitions)
        member_results.append(
            ResearchMemberRunResult(
                security_id=member.security_id,
                research_id=member.research_id,
                run_id=run_id,
                status=result.status,
                error_code=result.error_code,
            )
        )
        if result.status != "SUCCEEDED" or result.output is None:
            continue
        try:
            member_drafts.append(
                decode_historical_research_draft_member(json.loads(result.output))
                if historical
                else ResearchDraftMember.model_validate_json(result.output)
            )
        except ValueError:
            member_results[-1] = replace(
                member_results[-1],
                status="FAILED",
                error_code="RESEARCH_OUTPUT_INVALID",
            )
            continue
        member_draft_indexes.append(index)
        for evidence in await _research_tool_evidence(runtime, run_id):
            if evidence.evidence_id not in {item.evidence_id for item in tool_evidence}:
                tool_evidence.append(evidence)

    member_run_result = tuple(member_results)
    failed_results = tuple(
        result for result in member_run_result if result.status in {"FAILED", "REJECTED"}
    )
    waiting_results = tuple(result for result in member_run_result if result.status == "WAITING")
    if failed_results:
        aggregate_status: FrameworkRunStatus = "FAILED"
    elif waiting_results:
        aggregate_status = "WAITING"
    else:
        aggregate_status = "SUCCEEDED"
    error_codes = tuple(result.error_code for result in member_run_result if result.error_code)
    if aggregate_status != "SUCCEEDED":
        aggregate_run_id = (
            failed_results[0].run_id
            if failed_results
            else waiting_results[0].run_id
            if waiting_results
            else case.framework_run_id
        )
        return FrameworkRunResult(
            run_id=aggregate_run_id,
            status=aggregate_status,
            output=None,
            run_existed_before=all_runs_existed,
            waiting_reason=(
                waiting_results[0].error_code
                if waiting_results and waiting_results[0].error_code is not None
                else "RESEARCH_MEMBER_RUN_WAITING"
                if waiting_results
                else None
            ),
            error_code=error_codes[0] if error_codes else "RESEARCH_MEMBER_RUN_FAILED",
            research_member_runs=member_run_result,
            transitions=tuple(primary_transitions),
            transitions_durably_recorded=record_transition is not None,
            research_tool_evidence=tuple(tool_evidence),
        )
    try:
        draft = ResearchDraft(contract_version="1.0.0", members=tuple(member_drafts))
    except ValueError:
        invalid_member_indexes = {
            index
            for index, member_draft in zip(
                member_draft_indexes,
                member_drafts,
                strict=True,
            )
            if (
                member_draft.security_id != command.members[index].security_id
                or member_draft.research_id != command.members[index].research_id
            )
        }
        member_run_result = tuple(
            replace(
                member_result,
                status="FAILED",
                error_code="RESEARCH_OUTPUT_INVALID",
            )
            if index in invalid_member_indexes
            else member_result
            for index, member_result in enumerate(member_run_result)
        )
        return FrameworkRunResult(
            run_id=case.framework_run_id,
            status="FAILED",
            output=None,
            run_existed_before=all_runs_existed,
            error_code="RESEARCH_OUTPUT_INVALID",
            research_member_runs=member_run_result,
            transitions=tuple(primary_transitions),
            transitions_durably_recorded=record_transition is not None,
            research_tool_evidence=tuple(tool_evidence),
        )
    return FrameworkRunResult(
        run_id=case.framework_run_id,
        status="SUCCEEDED",
        output=json.dumps(
            research_draft_payload(draft, historical=historical),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        run_existed_before=all_runs_existed,
        research_member_runs=member_run_result,
        transitions=tuple(primary_transitions),
        transitions_durably_recorded=record_transition is not None,
        research_tool_evidence=tuple(tool_evidence),
    )


async def execute_research_risk_run(
    case: FrozenDecisionCase,
    runtime: RuntimeStorage,
    research_run: FrameworkRunResult,
    risk_plan: ResearchRiskPlan,
    record_transition: FrameworkTransitionRecorder | None = None,
    *,
    record_auxiliary_run_reservation: AuxiliaryRunReservationRecorder | None = None,
    clock: Clock | None = None,
) -> FrameworkRunResult:
    """Execute or recover only the independent risk Run for an immutable plan."""
    command = case.research
    assert command is not None
    risk_definition = _risk_definition(
        command,
        research_run_id=research_run.run_id,
        risk_plan=risk_plan,
    )
    _assert_risk_definition(risk_definition)
    existing_risk_run = await runtime.run_store.get_run(risk_plan.risk_run_id)
    allow_create = True
    if (
        existing_risk_run is None
        and research_run.status == "SUCCEEDED"
        and research_run.run_existed_before
    ):
        allow_create = (
            record_auxiliary_run_reservation is not None
            and await record_auxiliary_run_reservation(risk_plan.risk_run_id)
        )
    result = await _execute_registered_run(
        runtime=runtime,
        run_id=risk_plan.risk_run_id,
        definition=risk_definition,
        input_payload=risk_plan.input_payload,
        case=None,
        record_transition=record_transition,
        allow_create=allow_create,
        clock=clock,
    )
    if result.status == "REJECTED" and result.output is None:
        rejected_output = await _last_model_checkpoint_output(runtime, result.run_id)
        if rejected_output is not None:
            return replace(result, output=rejected_output)
    return result


async def execute_research_decision_case(
    case: FrozenDecisionCase,
    runtime: RuntimeStorage,
    record_transition: FrameworkTransitionRecorder | None = None,
    *,
    record_auxiliary_run_reservation: AuxiliaryRunReservationRecorder | None = None,
    clock: Clock | None = None,
) -> FrameworkRunResult:
    """Compatibility entry point that delegates journey orchestration to the host module."""
    from stock_profiler.modules.decision_cases.service import execute_research_risk_journey

    return await execute_research_risk_journey(
        case,
        legacy=_is_legacy_research_case(case),
        historical=_is_historical_research_case(case),
        execute_research=lambda: execute_research_run(
            case,
            runtime,
            record_transition,
            clock=clock,
        ),
        execute_risk=lambda research_run, risk_plan: execute_research_risk_run(
            case,
            runtime,
            research_run,
            risk_plan,
            record_transition,
            record_auxiliary_run_reservation=record_auxiliary_run_reservation,
            clock=clock,
        ),
    )


async def _research_tool_evidence(
    runtime: RuntimeStorage,
    run_id: str,
    *,
    legacy: bool = False,
) -> tuple[ResearchToolEvidence, ...]:
    """Recover structured provenance from successful announcement checkpoints."""
    evidence_items: list[ResearchToolEvidence] = []
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
        try:
            payload = json.loads(outcome.result)
            evidence = (
                decode_legacy_research_tool_evidence(payload)
                if legacy
                else ResearchToolEvidence.model_validate(payload)
            )
        except ValueError as error:
            raise ValueError("research Tool evidence is not structured") from error
        if evidence.evidence_id not in {item.evidence_id for item in evidence_items}:
            evidence_items.append(evidence)
    return tuple(evidence_items)


async def _last_model_checkpoint_output(
    runtime: RuntimeStorage,
    run_id: str,
) -> str | None:
    """Recover a rejected Definition result without changing its Run terminal state."""
    checkpoints = await runtime.run_store.get_checkpoints(run_id)
    for checkpoint in reversed(checkpoints):
        if checkpoint.step_type is StepType.MODEL:
            try:
                return cast(str, deserialize_model_response(checkpoint.output).content)
            except ValueError:
                return None
    return None


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
        if transition.run_id != run_id:
            transition = replace(transition, run_id=run_id)
        transitions.append(transition)
        if record_transition is not None:
            await record_transition(transition)

    async def record_framework_statuses() -> None:
        nonlocal collected_status_count
        status_reasons = {
            "CREATED": "FRAMEWORK_RUN_CREATED",
            "RUNNING": "FRAMEWORK_RUN_STARTED",
            "WAITING": "FRAMEWORK_RUN_WAITING",
            "SUCCEEDED": "FRAMEWORK_RUN_SUCCEEDED",
            "REJECTED": "FRAMEWORK_RUN_REJECTED",
            "FAILED": "FRAMEWORK_RUN_FAILED",
            "CANCELLED": "FRAMEWORK_RUN_CANCELLED",
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
        _assert_existing_run_matches_definition_input(run, definition, input_payload)

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
            _assert_existing_run_matches_definition_input(run, definition, input_payload)
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
        or type(definition.run_policy) is not _RiskVerdictRunPolicy
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


def _is_legacy_research_case(case: FrozenDecisionCase) -> bool:
    return (
        case.research is not None
        and case.version_bundle.agent_definition_version == RESEARCH_LEGACY_DEFINITION_VERSION
        and case.version_bundle.output_contract_version
        == RESEARCH_LEGACY_OUTPUT_CONTRACT_VERSION
    )


def _is_historical_research_case(case: FrozenDecisionCase) -> bool:
    return (
        case.research is not None
        and case.version_bundle.agent_definition_version == RESEARCH_PRIOR_DEFINITION_VERSION
        and case.version_bundle.output_contract_version == RESEARCH_PRIOR_OUTPUT_CONTRACT_VERSION
    )


def _research_definition_for_case(case: FrozenDecisionCase) -> AgentDefinition:
    """Rebuild the exact research Definition family named by a frozen case."""
    if _is_legacy_research_case(case):
        return _legacy_research_definition(case)
    return _research_definition(case, historical=_is_historical_research_case(case))


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
        definition = _research_definition_for_case(case)
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
        legacy = _is_legacy_research_case(case)
        historical = _is_historical_research_case(case)
        expected_definition_version = (
            RESEARCH_LEGACY_DEFINITION_VERSION if legacy else RESEARCH_DEFINITION_VERSION
        )
        if historical:
            expected_definition_version = RESEARCH_PRIOR_DEFINITION_VERSION
        expected_output_contract_version = (
            RESEARCH_LEGACY_OUTPUT_CONTRACT_VERSION
            if legacy
            else RESEARCH_OUTPUT_CONTRACT_VERSION
        )
        if historical:
            expected_output_contract_version = RESEARCH_PRIOR_OUTPUT_CONTRACT_VERSION
        expected_routing_policy_version = (
            RESEARCH_LEGACY_ROUTING_POLICY_VERSION
            if legacy
            else RESEARCH_ROUTING_POLICY_VERSION
        )
        expected_instructions = (
            LEGACY_RESEARCH_DEFINITION_INSTRUCTIONS
            if legacy
            else RESEARCH_DEFINITION_INSTRUCTIONS
        )
        expected_output_schema = (
            legacy_research_draft_json_schema()
            if legacy
            else (
                (
                    ResearchDraftMember.model_json_schema()
                    if _historical_research_draft_has_debate_fields(case)
                    else historical_research_draft_member_json_schema()
                )
                if historical
                else ResearchDraftMember.model_json_schema()
            )
        )
        if (
            not supports_case_host_contract(
                bundle.case_contract_version,
                bundle.host_contract_version,
            )
            or not supports_report_projection_contract(bundle.report_projection_contract_version)
            or bundle.agent_definition_id != RESEARCH_DEFINITION_ID
            or bundle.agent_definition_version != expected_definition_version
            or bundle.output_contract_version != expected_output_contract_version
            or bundle.model_adapter_id != RESEARCH_MODEL_ADAPTER_ID
            or bundle.routing_policy_version != expected_routing_policy_version
            or case.agent_definition.definition_id != RESEARCH_DEFINITION_ID
            or case.agent_definition.version != expected_definition_version
            or case.agent_definition.model_adapter_id != RESEARCH_MODEL_ADAPTER_ID
            or case.agent_definition.instructions != expected_instructions
            or case.agent_definition.output_contract.contract_id != RESEARCH_OUTPUT_CONTRACT_ID
            or case.agent_definition.output_contract.version != expected_output_contract_version
            or case.agent_definition.output_contract.json_schema
            != expected_output_schema
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


def _assert_existing_run_matches_definition_input(
    run: RunRecord,
    definition: AgentDefinition,
    input_payload: str,
) -> None:
    """Bind a member Run to the same Definition snapshot and member input."""
    if (
        run.definition_id != definition.definition_id
        or run.definition_version != definition.version
        or run.input != input_payload
        or run.snapshot != definition.frozen_snapshot()
    ):
        raise ValueError("durable research member Run does not match its frozen input")


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
