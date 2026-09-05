"""Frozen, public D0 decision-case contracts and stable identities."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.frozen_case import load_frozen_case_payload

FROZEN_CASE_CONTRACT_VERSION = "1.0.0"
FROZEN_HOST_CONTRACT_VERSION = "1.1.0"
FROZEN_AGENT_DEFINITION_ID = "synthetic-frozen-decision-case"
FROZEN_AGENT_DEFINITION_VERSION = "1.0.0"
FROZEN_OUTPUT_CONTRACT_VERSION = "1.0.0"
FROZEN_REPORT_PROJECTION_CONTRACT_VERSION = "1.1.0"
FROZEN_QUALIFICATION_SCOPE = "D0_SYNTHETIC_CONTRACT_ONLY"
FROZEN_CORRECTION_CONTRACT_VERSION = "1.0.0"


class FrozenContract(BaseModel):
    """Reject unversioned fields so a frozen case cannot silently expand."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class EvidenceClock(FrozenContract):
    """The immutable fact, publication, acquisition, and validation clocks."""

    fact_effective_at: str
    source_published_at: str
    acquired_at: str
    validated_at: str


class DecisionCaseVersionBundle(FrozenContract):
    """The complete version set that participates in replay identity."""

    case_contract_version: str
    host_contract_version: str
    host_application_version: str
    host_source_sha: str
    agent_definition_id: str
    agent_definition_version: str
    model_adapter_id: str
    routing_policy_version: str
    output_contract_version: str
    report_projection_contract_version: str
    m_agent_version: str
    m_agent_wheel_url: str
    m_agent_wheel_sha256: str
    m_agent_release_commit: str


class FrozenOutputContract(FrozenContract):
    """The versioned framework output contract frozen into one decision case."""

    contract_id: str
    version: str
    json_schema: dict[str, Any] = Field(alias="schema", serialization_alias="schema")


class FrozenAgentDefinition(FrozenContract):
    """The complete host-owned source for one frozen M-Agent definition."""

    definition_id: str
    version: str
    instructions: str
    model_adapter_id: str
    output_contract: FrozenOutputContract


class ExternalResult(FrozenContract):
    """The user-visible result expected from this original synthetic fixture."""

    outcome_code: str
    summary: str
    key_reasons: tuple[str, ...]


class GateResult(FrozenContract):
    """One deterministic gate evaluated during a saved decision stage."""

    gate_id: str
    status: Literal["PASSED", "FAILED"]


DecisionResultStatus = Literal[
    "SUCCEEDED",
    "REJECTED",
    "ABSTAINED",
    "FAILED",
    "PENDING",
    "EXPIRED",
    "EXECUTION_BLOCKED",
    "UNKNOWN",
]

FrameworkRunStatus = Literal[
    "CREATED",
    "RUNNING",
    "WAITING",
    "SUCCEEDED",
    "REJECTED",
    "FAILED",
    "CANCELLED",
]
StageStatus = Literal[
    "CREATED",
    "RUNNING",
    "WAITING",
    "SUCCEEDED",
    "REJECTED",
    "ABSTAINED",
    "FAILED",
    "PENDING",
    "EXPIRED",
    "EXECUTION_BLOCKED",
    "UNKNOWN",
    "CANCELLED",
]
BusinessCommitStatus = Literal["NOT_ATTEMPTED", "COMMITTED", "UNKNOWN"]


class StageResult(FrozenContract):
    """A phase-specific outcome; lifecycle states have no global terminal meaning."""

    phase: Literal[
        "FRAMEWORK_RUN",
        "HOST_VALIDATION",
        "BUSINESS_COMMIT",
        "PUBLICATION",
        "NOTIFICATION",
        "CORRECTION",
    ]
    status: StageStatus
    gate_results: tuple[GateResult, ...]
    reasons: tuple[str, ...]


class FormalReport(FrozenContract):
    """Read-only delivery projection derived from one committed host event."""

    report_version_id: str
    event_id: str
    business_object_id: str
    framework_run_id: str
    case_id: str
    synthetic: bool
    qualification_scope: str
    generated_at: str
    knowledge_cutoff: str
    evidence_clock: EvidenceClock
    version_bundle: DecisionCaseVersionBundle
    result: ExternalResult
    stage_results: tuple[StageResult, ...]
    corrects_event_id: str | None = None


class NotificationAttempt(FrozenContract):
    """One append-only attempt to point a user back to an existing formal report."""

    notification_attempt_id: str
    report_version_id: str
    event_id: str
    status: Literal["SUCCEEDED", "FAILED"]
    reasons: tuple[str, ...]


class DecisionCaseExecution(FrozenContract):
    """The three status boundaries the CLI and delivery surface must distinguish."""

    business_object_id: str
    framework_run_id: str
    decision_event_id: str
    report_version_id: str
    framework_run_status: FrameworkRunStatus
    business_result_status: DecisionResultStatus | None
    business_commit_status: BusinessCommitStatus
    publication_status: Literal["PUBLISHED", "CLOSED"]
    report: FormalReport | None
    stage_results: tuple[StageResult, ...]


class DecisionCaseCorrection(FrozenContract):
    """A replayable correction that preserves the original report as a separate fact."""

    original_event_id: str
    original_report_version_id: str
    report: FormalReport


class DecisionEventFact(FrozenContract):
    """The append-only business fact from which the report is rebuilt."""

    decision_event_id: str
    business_object_id: str
    framework_run_id: str
    case: FrozenDecisionCase
    result: ExternalResult
    validation_status: str
    committed_at: str
    stage_results: tuple[StageResult, ...]
    corrects_event_id: str | None = None

    def formal_report(self, report_version_id: str) -> FormalReport:
        """Project this complete append-only fact into its one read-only report."""
        return FormalReport(
            report_version_id=report_version_id,
            event_id=self.decision_event_id,
            business_object_id=self.business_object_id,
            framework_run_id=self.framework_run_id,
            case_id=self.case.case_id,
            synthetic=True,
            qualification_scope=self.case.qualification_scope,
            generated_at=self.case.report_generated_at,
            knowledge_cutoff=self.case.knowledge_cutoff,
            evidence_clock=self.case.evidence_clock,
            version_bundle=self.case.version_bundle,
            result=self.result,
            stage_results=(
                *self.stage_results,
                StageResult(
                    phase="PUBLICATION",
                    status="SUCCEEDED",
                    gate_results=(GateResult(gate_id="EVENT_COMMITTED", status="PASSED"),),
                    reasons=(),
                ),
            ),
            corrects_event_id=self.corrects_event_id,
        )


class FrozenDecisionCase(FrozenContract):
    """One replayable synthetic decision input with all clocks and contracts fixed."""

    synthetic: bool
    generator_version: str
    seed: int
    case_id: str
    business_identity: str
    knowledge_cutoff: str
    report_generated_at: str
    evidence_clock: EvidenceClock
    qualification_scope: str
    version_bundle: DecisionCaseVersionBundle
    agent_definition: FrozenAgentDefinition
    input: dict[str, Any]
    expected_external_result: ExternalResult

    @model_validator(mode="after")
    def validate_original_synthetic_contract(self) -> FrozenDecisionCase:
        """Make public fixtures fail closed unless they declare original D0 provenance."""
        if not self.synthetic:
            raise ValueError("frozen decision cases must declare synthetic: true")
        if not self.generator_version:
            raise ValueError("frozen decision cases require a generator version")
        if not self.case_id or not self.business_identity:
            raise ValueError("frozen decision cases require stable identities")
        if self.qualification_scope != FROZEN_QUALIFICATION_SCOPE:
            raise ValueError("frozen decision cases must use the sole D0 synthetic scope")
        if (
            self.version_bundle.case_contract_version != FROZEN_CASE_CONTRACT_VERSION
            or self.version_bundle.host_contract_version != FROZEN_HOST_CONTRACT_VERSION
            or self.version_bundle.report_projection_contract_version
            != FROZEN_REPORT_PROJECTION_CONTRACT_VERSION
            or self.version_bundle.agent_definition_id != FROZEN_AGENT_DEFINITION_ID
            or self.version_bundle.agent_definition_version != FROZEN_AGENT_DEFINITION_VERSION
            or self.version_bundle.output_contract_version != FROZEN_OUTPUT_CONTRACT_VERSION
        ):
            raise ValueError("frozen version bundle does not match the supported contract")
        if (
            self.agent_definition.definition_id != self.version_bundle.agent_definition_id
            or self.agent_definition.version != self.version_bundle.agent_definition_version
            or self.agent_definition.model_adapter_id != self.version_bundle.model_adapter_id
            or self.agent_definition.output_contract.version
            != self.version_bundle.output_contract_version
        ):
            raise ValueError("frozen AgentDefinition must match the version bundle")
        if (
            self.agent_definition.definition_id != FROZEN_AGENT_DEFINITION_ID
            or self.agent_definition.version != FROZEN_AGENT_DEFINITION_VERSION
            or self.agent_definition.output_contract.version != FROZEN_OUTPUT_CONTRACT_VERSION
        ):
            raise ValueError("frozen AgentDefinition does not match the supported contract")
        return self

    @property
    def frozen_input_fingerprint(self) -> str:
        """Hash the exact original input, clocks, scope, and version bundle."""
        return _fingerprint(self.model_dump(mode="json"))

    @property
    def business_object_id(self) -> str:
        """Identify the Stock Profiler business object independently from a framework Run."""
        return _stable_id(
            "business-object",
            {
                "business_identity": self.business_identity,
                "case_contract_version": self.version_bundle.case_contract_version,
            },
        )

    @property
    def framework_run_id(self) -> str:
        """Allocate a deterministic M-Agent identity for this complete frozen replay."""
        return _stable_id(
            "framework-run",
            {
                "business_object_id": self.business_object_id,
                "frozen_input_fingerprint": self.frozen_input_fingerprint,
                "definition_id": self.version_bundle.agent_definition_id,
                "definition_version": self.version_bundle.agent_definition_version,
            },
        )

    @property
    def decision_event_id(self) -> str:
        """Reserve a host-owned append-only event identity without reusing the Run ID."""
        return _stable_id(
            "decision-event",
            {
                "business_object_id": self.business_object_id,
                "framework_run_id": self.framework_run_id,
                "expected_external_result": self.expected_external_result.model_dump(mode="json"),
                "version_bundle": self.version_bundle.model_dump(mode="json"),
            },
        )

    @property
    def report_version_id(self) -> str:
        """Reserve the report identity separately from its source decision event."""
        return _stable_id(
            "report-version",
            {
                "decision_event_id": self.decision_event_id,
                "report_projection_contract_version": (
                    self.version_bundle.report_projection_contract_version
                ),
            },
        )

    def correction_event_id(self, original_event_id: str) -> str:
        """Allocate a separate append-only event identity for the sole D0 correction."""
        return _stable_id(
            "decision-correction",
            {
                "business_object_id": self.business_object_id,
                "original_event_id": original_event_id,
                "correction_contract_version": FROZEN_CORRECTION_CONTRACT_VERSION,
                "version_bundle": self.version_bundle.model_dump(mode="json"),
            },
        )

    def correction_report_version_id(self, original_event_id: str) -> str:
        """Keep a correction report identity distinct from both source identities."""
        return _stable_id(
            "report-correction",
            {
                "correction_event_id": self.correction_event_id(original_event_id),
                "report_projection_contract_version": (
                    self.version_bundle.report_projection_contract_version
                ),
            },
        )


def load_frozen_decision_case(settings: Settings) -> FrozenDecisionCase:
    """Bind the declared synthetic case to the exact configured host build."""
    payload = load_frozen_case_payload()
    version_bundle = payload.get("version_bundle")
    if not isinstance(version_bundle, dict):
        raise RuntimeError("frozen decision-case fixture has no version bundle")
    version_bundle["host_application_version"] = settings.configuration_version
    version_bundle["host_source_sha"] = settings.source_sha
    return FrozenDecisionCase.model_validate(payload)


_COMPLETE_SYNTHETIC_INPUT = {
    "security": {
        "issuer_id": "FICTIONAL-ORBITAL-MOSAIC",
        "symbol": "XQZ-4017",
        "display_name": "Orbital Mosaic Fabrication",
    },
    "account": {
        "account_id": "synthetic-account-4017",
        "account_kind": "SIMULATED_CASH",
    },
    "evidence": [
        {
            "evidence_id": "synthetic-evidence-001",
            "source": "fictional-ledger-alpha",
            "statement": "The fictional issuer completed the imaginary orbital-mosaic checklist.",
        },
        {
            "evidence_id": "synthetic-evidence-002",
            "source": "fictional-ledger-beta",
            "statement": "The simulated account has no execution capability.",
        },
    ],
}


def host_validates_external_result(case: FrozenDecisionCase, result: ExternalResult) -> bool:
    """Accept only the expected result from the complete frozen synthetic input."""
    return host_validation_result(case, result).status == "SUCCEEDED"


def host_validation_result(
    case: FrozenDecisionCase, result: ExternalResult
) -> StageResult:
    """Classify a typed framework result without collapsing host outcomes."""
    valid_frozen_input = (
        case.synthetic
        and case.qualification_scope == FROZEN_QUALIFICATION_SCOPE
        and has_complete_synthetic_input(case.input)
    )
    if valid_frozen_input and result == case.expected_external_result:
        return StageResult(
            phase="HOST_VALIDATION",
            status="SUCCEEDED",
            gate_results=(GateResult(gate_id="FROZEN_RESULT_MATCH", status="PASSED"),),
            reasons=(),
        )
    status = _SYNTHETIC_OUTCOME_STATUSES.get(result.outcome_code)
    if valid_frozen_input and status is not None:
        return StageResult(
            phase="HOST_VALIDATION",
            status=status,
            gate_results=(GateResult(gate_id="FROZEN_RESULT_MATCH", status="FAILED"),),
            reasons=(result.outcome_code,),
        )
    return StageResult(
        phase="HOST_VALIDATION",
        status="FAILED",
        gate_results=(GateResult(gate_id="FROZEN_RESULT_MATCH", status="FAILED"),),
        reasons=("UNSUPPORTED_SYNTHETIC_RESULT",),
    )


_SYNTHETIC_OUTCOME_STATUSES: dict[str, DecisionResultStatus] = {
    "SYNTHETIC_INPUT_REJECTED": "REJECTED",
    "SYNTHETIC_RESULT_ABSTAINED": "ABSTAINED",
    "SYNTHETIC_RESULT_FAILED": "FAILED",
    "SYNTHETIC_RESULT_PENDING": "PENDING",
    "SYNTHETIC_RESULT_EXPIRED": "EXPIRED",
    "SYNTHETIC_RESULT_EXECUTION_BLOCKED": "EXECUTION_BLOCKED",
}


def has_complete_synthetic_input(value: dict[str, Any]) -> bool:
    """Recognize the exact versioned input contract of this sole frozen case."""
    return value == _COMPLETE_SYNTHETIC_INPUT


def _fingerprint(value: object) -> str:
    return sha256(_canonical_json(value).encode()).hexdigest()


def _stable_id(kind: str, value: object) -> str:
    return f"{kind}-{_fingerprint(value)}"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
