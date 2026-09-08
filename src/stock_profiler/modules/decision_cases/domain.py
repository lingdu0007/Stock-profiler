"""Frozen, public D0 decision-case contracts and stable identities."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any, Literal, Protocol, cast, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from stock_profiler.foundation.decision_versions import (
    CURRENT_M_AGENT_RELEASE,
    HISTORICAL_M_AGENT_RELEASE,
)
from stock_profiler.foundation.decision_versions import (
    DecisionCaseVersionBundle as DecisionCaseVersionBundle,
)
from stock_profiler.modules.decision_cases.frozen_case import load_frozen_case_payload
from stock_profiler.modules.portfolio.contracts import (
    PortfolioAuthorizationOutcome,
    PortfolioCommand,
    PortfolioConfirmationCommand,
    PortfolioUseCommand,
)
from stock_profiler.modules.portfolio.drawdown_contracts import DrawdownCommand, DrawdownOutcome
from stock_profiler.modules.portfolio.liquidity import LiquidityCommand, LiquidityOutcome
from stock_profiler.modules.portfolio.stress import PortfolioStressCommand, PortfolioStressOutcome
from stock_profiler.modules.position_management.contracts import (
    PositionReconciliationOutcome,
    PositionSnapshotCommand,
)
from stock_profiler.modules.qualification.contracts import (
    GovernanceCommand,
    GovernanceOutcome,
    QualificationCommand,
)

FROZEN_CASE_CONTRACT_VERSION = "2.0.0"
FROZEN_HOST_CONTRACT_VERSION = "2.0.0"
FROZEN_AGENT_DEFINITION_ID = "synthetic-frozen-decision-case"
FROZEN_AGENT_DEFINITION_VERSION = "1.0.0"
FROZEN_OUTPUT_CONTRACT_VERSION = "1.0.0"
FROZEN_REPORT_PROJECTION_CONTRACT_VERSION = "2.0.0"
_SCOPED_CASE_CONTRACT_VERSIONS = frozenset(
    {"3.0.0", "4.0.0", "5.0.0", "6.0.0", "7.0.0", "8.0.0", "8.2.0", "drawdown.1.0.0"}
)
_SUPPORTED_REPORT_PROJECTION_CONTRACT_VERSIONS = frozenset(
    {"1.0.0", FROZEN_REPORT_PROJECTION_CONTRACT_VERSION, *_SCOPED_CASE_CONTRACT_VERSIONS}
)
_SUPPORTED_CASE_HOST_CONTRACT_PAIRS = frozenset(
    {
        ("1.0.0", "1.0.0"),
        (FROZEN_CASE_CONTRACT_VERSION, FROZEN_HOST_CONTRACT_VERSION),
        ("3.0.0", "3.0.0"),
        ("4.0.0", "4.0.0"),
        ("5.0.0", "5.0.0"),
        ("6.0.0", "6.0.0"),
        ("7.0.0", "7.0.0"),
        ("drawdown.1.0.0", "drawdown.1.0.0"),
        ("8.0.0", "8.0.0"),
        ("8.2.0", "8.2.0"),
    }
)
FROZEN_QUALIFICATION_SCOPE = "D0_SYNTHETIC_CONTRACT_ONLY"
FROZEN_CORRECTION_CONTRACT_VERSION = "1.0.0"


class FrozenContract(BaseModel):
    """Reject unversioned fields so a frozen case cannot silently expand."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class ResultAccessScope(FrozenContract):
    """Immutable ownership and visibility bound before a scoped case is executed."""

    contract_version: Literal["1.0.0"]
    user_id: str = Field(min_length=1)
    account_ids: tuple[str, ...] = Field(min_length=1)
    visibility: Literal["USER", "SHADOW"]

    @model_validator(mode="after")
    def validate_accounts(self) -> ResultAccessScope:
        if any(not account for account in self.account_ids) or len(set(self.account_ids)) != len(
            self.account_ids
        ):
            raise ValueError("result account scope must be nonempty and unique")
        return self

    def same_scope_as(self, other: ResultAccessScope) -> bool:
        """Match historical ownership without normalizing its saved account order."""
        return set(self.account_ids) == set(other.account_ids) and self.model_dump(
            exclude={"account_ids"}
        ) == other.model_dump(exclude={"account_ids"})


class EvidenceClock(FrozenContract):
    """The immutable fact, publication, acquisition, and validation clocks."""

    fact_effective_at: str
    source_published_at: str
    acquired_at: str
    validated_at: str


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


class CorrectionEvidence(FrozenContract):
    """New authoritative synthetic facts, distinct from the original Run's evidence."""

    contract_version: Literal["2.0.0"]
    evidence_id: str
    corrects_evidence_id: str
    source: str
    reason: str
    original_statement: str
    corrected_statement: str
    evidence_clock: EvidenceClock
    knowledge_cutoff: str
    decision_formed_at: str


class ExternalResult(FrozenContract):
    """The user-visible result expected from this original synthetic fixture."""

    outcome_code: str
    summary: str
    key_reasons: tuple[str, ...]
    correction_evidence: CorrectionEvidence | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    governance: GovernanceOutcome | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    portfolio: PortfolioAuthorizationOutcome | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    position: PositionReconciliationOutcome | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    drawdown: DrawdownOutcome | None = Field(default=None, exclude_if=lambda value: value is None)
    liquidity: LiquidityOutcome | None = Field(default=None, exclude_if=lambda value: value is None)
    stress: PortfolioStressOutcome | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class GateResult(FrozenContract):
    """One deterministic gate evaluated during a saved decision stage."""

    gate_id: str
    status: Literal["PASSED", "FAILED", "UNKNOWN"]


BusinessResultStatus = Literal["SUCCEEDED", "REJECTED", "ABSTAINED", "FAILED"]
BusinessLifecycleStatus = Literal[
    "PENDING",
    "EXPIRED",
    "EXECUTION_BLOCKED",
    "UNKNOWN",
]
BusinessLifecycleOwner = Literal[
    "ADJUDICATION",
    "VALIDITY",
    "EXECUTION",
    "COMMIT_RECONCILIATION",
]
LifecycleStagePhase = Literal[
    "ADJUDICATION_LIFECYCLE",
    "VALIDITY_LIFECYCLE",
    "EXECUTION_LIFECYCLE",
    "COMMIT_RECONCILIATION",
]
StagePhase = Literal[
    "FRAMEWORK_RUN",
    "HOST_VALIDATION",
    "BUSINESS_DECISION",
    "QUALIFICATION",
    "PORTFOLIO_AUTHORIZATION",
    "POSITION_RECONCILIATION",
    "DRAWDOWN_PROTECTION",
    "LIQUIDITY_PROTECTION",
    "PORTFOLIO_STRESS",
    "ADJUDICATION_LIFECYCLE",
    "VALIDITY_LIFECYCLE",
    "EXECUTION_LIFECYCLE",
    "COMMIT_RECONCILIATION",
    "BUSINESS_COMMIT",
    "PUBLICATION",
    "NOTIFICATION",
    "CORRECTION",
]
NotificationAttemptStatus = Literal["SUCCEEDED", "FAILED"]

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
BusinessCommitStatus = Literal["NOT_ATTEMPTED", "FAILED", "COMMITTED", "UNKNOWN"]
_BUSINESS_RESULT_STATUSES = frozenset(get_args(BusinessResultStatus))
_BUSINESS_LIFECYCLE_STATUSES = frozenset(get_args(BusinessLifecycleStatus))
_FRAMEWORK_RUN_STATUSES = frozenset(get_args(FrameworkRunStatus))
_COMMITTABLE_HOST_OUTCOME_PHASES = frozenset(
    {
        "BUSINESS_DECISION",
        "ADJUDICATION_LIFECYCLE",
        "VALIDITY_LIFECYCLE",
        "EXECUTION_LIFECYCLE",
    }
)
_STAGE_STATUS_BY_PHASE: dict[str, frozenset[str]] = {
    "FRAMEWORK_RUN": _FRAMEWORK_RUN_STATUSES,
    "HOST_VALIDATION": frozenset({"SUCCEEDED", "FAILED"}),
    "BUSINESS_DECISION": _BUSINESS_RESULT_STATUSES,
    "QUALIFICATION": frozenset({"SUCCEEDED", "REJECTED"}),
    "PORTFOLIO_AUTHORIZATION": frozenset({"SUCCEEDED", "REJECTED"}),
    "POSITION_RECONCILIATION": frozenset({"SUCCEEDED", "REJECTED"}),
    "DRAWDOWN_PROTECTION": frozenset({"SUCCEEDED", "REJECTED", "UNKNOWN"}),
    "LIQUIDITY_PROTECTION": frozenset({"SUCCEEDED", "REJECTED"}),
    "PORTFOLIO_STRESS": frozenset({"SUCCEEDED", "REJECTED"}),
    "ADJUDICATION_LIFECYCLE": frozenset({"PENDING", "UNKNOWN"}),
    "VALIDITY_LIFECYCLE": frozenset({"EXPIRED", "UNKNOWN"}),
    "EXECUTION_LIFECYCLE": frozenset({"EXECUTION_BLOCKED", "UNKNOWN"}),
    "COMMIT_RECONCILIATION": frozenset({"UNKNOWN"}),
    "BUSINESS_COMMIT": frozenset({"SUCCEEDED", "FAILED"}),
    "PUBLICATION": frozenset({"SUCCEEDED", "FAILED", "UNKNOWN"}),
    "NOTIFICATION": frozenset({"SUCCEEDED", "FAILED"}),
    "CORRECTION": frozenset({"SUCCEEDED"}),
}
_LIFECYCLE_OWNER_BY_PHASE: dict[str, BusinessLifecycleOwner] = {
    "ADJUDICATION_LIFECYCLE": "ADJUDICATION",
    "VALIDITY_LIFECYCLE": "VALIDITY",
    "EXECUTION_LIFECYCLE": "EXECUTION",
    "COMMIT_RECONCILIATION": "COMMIT_RECONCILIATION",
}


class StageResult(FrozenContract):
    """A phase-specific outcome; lifecycle states have no global terminal meaning."""

    phase: StagePhase
    status: StageStatus
    gate_results: tuple[GateResult, ...]
    reasons: tuple[str, ...]

    @model_validator(mode="after")
    def validate_phase_status(self) -> StageResult:
        """Keep lifecycle states in the phase that owns their meaning."""
        if self.status not in _STAGE_STATUS_BY_PHASE[self.phase]:
            raise ValueError(f"{self.phase} cannot record status {self.status}")
        return self


class BusinessLifecycle(FrozenContract):
    """Expose a lifecycle state only together with the phase that owns it."""

    owner: BusinessLifecycleOwner
    phase: LifecycleStagePhase
    status: BusinessLifecycleStatus

    @model_validator(mode="after")
    def validate_owner_status(self) -> BusinessLifecycle:
        """Disallow a lifecycle phase from being presented under another owner."""
        expected_owner = _LIFECYCLE_OWNER_BY_PHASE[self.phase]
        if self.owner != expected_owner:
            raise ValueError(f"{self.phase} belongs to {expected_owner}")
        if self.status not in _STAGE_STATUS_BY_PHASE[self.phase]:
            raise ValueError(f"{self.phase} cannot record status {self.status}")
        return self


def _legacy_stage_result_payloads(
    result: object, *, include_publication: bool
) -> list[dict[str, object]]:
    """Project the pre-stage ledger shape without changing its stored payload."""
    result_reasons = result.get("key_reasons", []) if isinstance(result, dict) else []
    reasons = [reason for reason in result_reasons if isinstance(reason, str)]
    stage_results: list[dict[str, object]] = [
        {
            "phase": "FRAMEWORK_RUN",
            "status": "SUCCEEDED",
            "gate_results": [{"gate_id": "RUN_TERMINAL", "status": "PASSED"}],
            "reasons": [],
        },
        {
            "phase": "HOST_VALIDATION",
            "status": "SUCCEEDED",
            "gate_results": [{"gate_id": "FROZEN_RESULT_MATCH", "status": "PASSED"}],
            "reasons": reasons,
        },
    ]
    outcome_code = result.get("outcome_code") if isinstance(result, dict) else None
    outcome = _SYNTHETIC_OUTCOMES.get(outcome_code) if isinstance(outcome_code, str) else None
    if outcome is not None:
        stage_results.append(
            {
                "phase": outcome.phase,
                "status": outcome.status,
                "gate_results": [
                    {"gate_id": "OUTPUT_CONTRACT", "status": "PASSED"},
                    {"gate_id": "FROZEN_RESULT_MATCH", "status": "PASSED"},
                ],
                "reasons": reasons,
            }
        )
    stage_results.append(
        {
            "phase": "BUSINESS_COMMIT",
            "status": "SUCCEEDED",
            "gate_results": [{"gate_id": "HOST_RESULT_SAVED", "status": "PASSED"}],
            "reasons": [],
        }
    )
    if include_publication:
        stage_results.append(
            {
                "phase": "PUBLICATION",
                "status": "SUCCEEDED",
                "gate_results": [{"gate_id": "EVENT_COMMITTED", "status": "PASSED"}],
                "reasons": [],
            }
        )
    return stage_results


def _uses_legacy_report_projection(value: dict[str, Any]) -> bool:
    """Recognize an omitted stage ledger only for the historical projection contract."""
    version_bundle = value.get("version_bundle")
    if not isinstance(version_bundle, dict):
        case = value.get("case")
        version_bundle = case.get("version_bundle") if isinstance(case, dict) else None
    return (
        isinstance(version_bundle, dict)
        and version_bundle.get("report_projection_contract_version") == "1.0.0"
    )


def supports_report_projection_contract(version: str) -> bool:
    """Return whether a frozen report projection version can be read by D0."""
    return version in _SUPPORTED_REPORT_PROJECTION_CONTRACT_VERSIONS


def supports_case_host_contract(case_version: str, host_version: str) -> bool:
    """Return whether a frozen case and host contract pair can be recovered by D0."""
    return (case_version, host_version) in _SUPPORTED_CASE_HOST_CONTRACT_PAIRS


def definition_version_for_case_contract(case_version: str) -> str:
    """Keep host and framework boundary validation on the same compatibility rule."""
    return (
        "2.0.0"
        if case_version in _SCOPED_CASE_CONTRACT_VERSIONS
        else FROZEN_AGENT_DEFINITION_VERSION
    )


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
    access_scope: ResultAccessScope | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    def with_publication_history(self, event_stages: tuple[StageResult, ...]) -> FormalReport:
        """Retain closed gates and correction recovery without replacing saved facts."""
        final_stage = self.stage_results[-1]
        if final_stage.phase != "PUBLICATION" or final_stage.status != "SUCCEEDED":
            return self
        if self.corrects_event_id is None:
            publication_history = tuple(
                stage
                for stage in event_stages
                if stage.phase == "PUBLICATION" and stage.status != "SUCCEEDED"
            )
            if not publication_history:
                return self
            return self.model_copy(
                update={
                    "stage_results": (
                        *self.stage_results[:-1],
                        *publication_history,
                        final_stage,
                    )
                }
            )
        correction_phases = frozenset(
            {"CORRECTION", "BUSINESS_COMMIT", "COMMIT_RECONCILIATION", "PUBLICATION"}
        )
        correction_history = tuple(
            stage for stage in event_stages if stage.phase in correction_phases
        )
        if not correction_history:
            return self
        return self.model_copy(
            update={
                "stage_results": (
                    *(
                        stage
                        for stage in self.stage_results
                        if stage.phase not in correction_phases
                    ),
                    *correction_history,
                )
            }
        )

    @model_validator(mode="before")
    @classmethod
    def project_legacy_stage_results(cls, value: Any) -> Any:
        """Read pre-stage reports without rewriting their immutable JSON."""
        if (
            not isinstance(value, dict)
            or "stage_results" in value
            or not _uses_legacy_report_projection(value)
        ):
            return value
        payload = dict(value)
        payload["stage_results"] = _legacy_stage_result_payloads(
            payload.get("result"),
            include_publication=True,
        )
        return payload


class NotificationAttempt(FrozenContract):
    """One append-only attempt to point a user back to an existing formal report."""

    notification_attempt_id: str
    report_version_id: str
    event_id: str
    status: NotificationAttemptStatus
    reasons: tuple[str, ...]
    recorded_at: str


class DecisionCaseExecution(FrozenContract):
    """The three status boundaries the CLI and delivery surface must distinguish."""

    business_object_id: str
    framework_run_id: str
    decision_event_id: str
    report_version_id: str
    framework_run_status: FrameworkRunStatus
    business_result_status: BusinessResultStatus | None
    business_lifecycle: BusinessLifecycle | None
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
    generated_at: str | None = None

    @model_validator(mode="before")
    @classmethod
    def project_legacy_stage_results(cls, value: Any) -> Any:
        """Read pre-stage event facts without mutating their committed payload."""
        if (
            not isinstance(value, dict)
            or "stage_results" in value
            or not _uses_legacy_report_projection(value)
        ):
            return value
        payload = dict(value)
        payload["stage_results"] = _legacy_stage_result_payloads(
            payload.get("result"),
            include_publication=False,
        )
        return payload

    def formal_report(self, report_version_id: str) -> FormalReport:
        """Project this complete append-only fact into its one read-only report."""
        return FormalReport.model_validate(stored_report_payload(self, report_version_id))

    def formal_report_payload(self, report_version_id: str) -> dict[str, object]:
        """Serialize a report using the source event's immutable projection contract."""
        return stored_report_payload(self, report_version_id)


def stored_report_payload(event: DecisionEventFact, report_version_id: str) -> dict[str, object]:
    """Frozen v1/v2 storage contract, shared by publication and integrity validation."""
    payload: dict[str, object] = {
        "report_version_id": report_version_id,
        "event_id": event.decision_event_id,
        "business_object_id": event.business_object_id,
        "framework_run_id": event.framework_run_id,
        "case_id": event.case.case_id,
        "synthetic": True,
        "qualification_scope": event.case.qualification_scope,
        "generated_at": event.generated_at or event.case.report_generated_at,
        "knowledge_cutoff": event.case.knowledge_cutoff,
        "evidence_clock": event.case.evidence_clock.model_dump(mode="json"),
        "version_bundle": event.case.version_bundle.model_dump(mode="json"),
        "result": event.result.model_dump(mode="json"),
        "stage_results": [
            *(stage.model_dump(mode="json") for stage in event.stage_results),
            {
                "phase": "PUBLICATION",
                "status": "SUCCEEDED",
                "gate_results": [{"gate_id": "EVENT_COMMITTED", "status": "PASSED"}],
                "reasons": [],
            },
        ],
        "corrects_event_id": event.corrects_event_id,
    }
    if event.case.version_bundle.report_projection_contract_version == "1.0.0":
        payload.pop("stage_results")
        payload.pop("corrects_event_id")
    if event.case.access_scope is not None:
        payload["access_scope"] = event.case.access_scope.model_dump(mode="json")
    return payload


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
    recovery_framework_run_id: str | None = Field(default=None, exclude=True)
    access_scope: ResultAccessScope | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    governance: GovernanceCommand | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    portfolio: PortfolioCommand | None = Field(default=None, exclude_if=lambda value: value is None)
    position: PositionSnapshotCommand | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    drawdown: DrawdownCommand | None = Field(default=None, exclude_if=lambda value: value is None)
    liquidity: LiquidityCommand | None = Field(default=None, exclude_if=lambda value: value is None)
    stress: PortfolioStressCommand | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def validate_original_synthetic_contract(self) -> FrozenDecisionCase:
        """Make public fixtures fail closed unless they declare original D0 provenance."""
        scoped = self.version_bundle.case_contract_version in _SCOPED_CASE_CONTRACT_VERSIONS
        governed = self.version_bundle.case_contract_version in {"4.0.0", "5.0.0"}
        portfolio_governed = self.version_bundle.case_contract_version == "6.0.0"
        position_governed = self.version_bundle.case_contract_version == "7.0.0"
        drawdown_governed = self.version_bundle.case_contract_version == "drawdown.1.0.0"
        if drawdown_governed != (self.drawdown is not None):
            raise ValueError("drawdown requires its own frozen contract")
        if self.drawdown is not None and (
            self.drawdown.cutoff_at != datetime.fromisoformat(self.knowledge_cutoff)
        ):
            raise ValueError("drawdown and frozen cutoff must agree")
        liquidity_governed = self.version_bundle.case_contract_version == "8.2.0"
        stress_governed = self.version_bundle.case_contract_version == "8.0.0"
        host_command_case = (
            governed
            or portfolio_governed
            or position_governed
            or liquidity_governed
            or stress_governed
            or drawdown_governed
        )
        if stress_governed != (self.stress is not None):
            raise ValueError("portfolio stress requires the version 8 frozen contract")
        if governed != (self.governance is not None):
            raise ValueError("governance requires a governed frozen contract")
        if portfolio_governed != (self.portfolio is not None):
            raise ValueError("portfolio requires a portfolio frozen contract")
        if position_governed != (self.position is not None):
            raise ValueError("position reconciliation requires the version 7 frozen contract")
        if liquidity_governed != (self.liquidity is not None):
            raise ValueError("liquidity requires the version 8.2 frozen contract")
        if (
            sum(
                command is not None
                for command in (
                    self.governance,
                    self.portfolio,
                    self.position,
                    self.liquidity,
                    self.stress,
                    self.drawdown,
                )
            )
            > 1
        ):
            raise ValueError("a frozen case cannot combine host commands")
        if (
            self.version_bundle.case_contract_version == "4.0.0"
            and self.governance is not None
            and (
                not isinstance(self.governance, QualificationCommand)
                or self.governance.action not in {"GRANT", "ALERT"}
                or self.governance.evidence.kind not in {"QUALIFICATION_PASS", "DIAGNOSTIC_ALERT"}
            )
        ):
            raise ValueError("extended governance requires the version 5 frozen contract")
        if host_command_case and datetime.fromisoformat(self.knowledge_cutoff).tzinfo is None:
            raise ValueError("host-command cases require a timezone-aware knowledge cutoff")
        if (
            self.expected_external_result.governance is not None
            or self.expected_external_result.portfolio is not None
            or self.expected_external_result.position is not None
            or self.expected_external_result.drawdown is not None
            or self.expected_external_result.liquidity is not None
            or self.expected_external_result.stress is not None
        ):
            raise ValueError("host decisions are never framework output")
        if self.governance is not None and (
            self.access_scope is None
            or self.governance.scope.user_id != self.access_scope.user_id
            or set(self.governance.scope.account_ids) != set(self.access_scope.account_ids)
        ):
            raise ValueError("governance and frozen access scope must agree")
        if (
            self.portfolio is not None
            and not isinstance(self.portfolio, PortfolioUseCommand)
            and (
                self.access_scope is None
                or set(self.portfolio.proposal.snapshot.account_ids)
                != set(self.access_scope.account_ids)
            )
        ):
            raise ValueError("portfolio and frozen access scope must agree")
        if (
            isinstance(self.portfolio, PortfolioConfirmationCommand)
            and self.access_scope is not None
            and self.portfolio.confirmation.user_id != self.access_scope.user_id
        ):
            raise ValueError("portfolio confirmation and frozen access scope must agree")
        if self.position is not None and (
            self.access_scope is None
            or set(self.position.account_ids) != set(self.access_scope.account_ids)
            or self.position.cutoff_at != datetime.fromisoformat(self.knowledge_cutoff)
        ):
            raise ValueError("position snapshot and frozen access scope must agree")
        if self.liquidity is not None and (
            self.access_scope is None
            or set(self.liquidity.position_snapshot.account_ids)
            != set(self.access_scope.account_ids)
            or self.liquidity.position_snapshot.cutoff_at
            != datetime.fromisoformat(self.knowledge_cutoff)
        ):
            raise ValueError("liquidity snapshot and frozen access scope must agree")
        if self.stress is not None and (
            self.access_scope is None
            or not set(self.stress.position_snapshot.account_ids).issubset(
                self.access_scope.account_ids
            )
            or self.stress.position_snapshot.cutoff_at
            != datetime.fromisoformat(self.knowledge_cutoff)
        ):
            raise ValueError("stress snapshot and frozen access scope must agree")
        account = self.input.get("account")
        definition_version = definition_version_for_case_contract(
            self.version_bundle.case_contract_version
        )
        if scoped != (self.access_scope is not None):
            raise ValueError("scoped cases require a scoped contract and frozen access scope")
        if scoped and (
            self.version_bundle.host_contract_version != self.version_bundle.case_contract_version
            or self.version_bundle.report_projection_contract_version
            != self.version_bundle.case_contract_version
            or self.access_scope is None
            or not isinstance(account, dict)
            or account.get("account_id") not in self.access_scope.account_ids
        ):
            raise ValueError("scoped case versions and account facts must agree")
        if not self.synthetic:
            raise ValueError("frozen decision cases must declare synthetic: true")
        if not self.generator_version:
            raise ValueError("frozen decision cases require a generator version")
        if not self.case_id or not self.business_identity:
            raise ValueError("frozen decision cases require stable identities")
        if self.qualification_scope != FROZEN_QUALIFICATION_SCOPE:
            raise ValueError("frozen decision cases must use the sole D0 synthetic scope")
        if (
            not supports_case_host_contract(
                self.version_bundle.case_contract_version,
                self.version_bundle.host_contract_version,
            )
            or not supports_report_projection_contract(
                self.version_bundle.report_projection_contract_version
            )
            or self.version_bundle.agent_definition_id != FROZEN_AGENT_DEFINITION_ID
            or self.version_bundle.agent_definition_version != definition_version
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
            or self.agent_definition.version != definition_version
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
    def recovery_business_object_ids(self) -> tuple[str, ...]:
        """Locate the one durable business lineage across explicitly supported contracts."""
        if self.access_scope is not None:
            return (self.business_object_id,)
        supported_pairs = (
            (
                self.version_bundle.case_contract_version,
                self.version_bundle.host_contract_version,
            ),
            *_SUPPORTED_CASE_HOST_CONTRACT_PAIRS,
        )
        return tuple(
            dict.fromkeys(
                _stable_id(
                    "business-object",
                    {
                        "business_identity": self.business_identity,
                        "case_contract_version": case_contract_version,
                    },
                )
                for case_contract_version, _host_contract_version in supported_pairs
                if case_contract_version not in _SCOPED_CASE_CONTRACT_VERSIONS
            )
        )

    @property
    def legacy_contract_recovery_cases(self) -> tuple[FrozenDecisionCase, ...]:
        """Reconstruct only supported historical cases when an old host mapping is absent."""
        if self.access_scope is not None:
            return ()
        current_pair = (
            self.version_bundle.case_contract_version,
            self.version_bundle.host_contract_version,
        )
        return tuple(
            self.model_copy(
                update={
                    "version_bundle": self.version_bundle.model_copy(
                        update={
                            "case_contract_version": case_contract_version,
                            "host_contract_version": host_contract_version,
                            "report_projection_contract_version": "1.0.0",
                        }
                    )
                }
            )
            for case_contract_version, host_contract_version in sorted(
                _SUPPORTED_CASE_HOST_CONTRACT_PAIRS
            )
            if (case_contract_version, host_contract_version) != current_pair
            and case_contract_version not in _SCOPED_CASE_CONTRACT_VERSIONS
        )

    @property
    def framework_run_id(self) -> str:
        """Allocate a deterministic M-Agent identity for this complete frozen replay."""
        if self.recovery_framework_run_id is not None:
            return self.recovery_framework_run_id
        return _stable_id(
            "framework-run",
            {
                "business_object_id": self.business_object_id,
                "frozen_input_fingerprint": self.frozen_input_fingerprint,
                "definition_id": self.version_bundle.agent_definition_id,
                "definition_version": self.version_bundle.agent_definition_version,
            },
        )

    def matches_recovery_input(self, other: FrozenDecisionCase) -> bool:
        """Allow source identity updates while rejecting a changed frozen input."""
        return _recovery_input_payload(self) == _recovery_input_payload(other)

    def matches_runtime_upgrade_recovery_input(self, other: FrozenDecisionCase) -> bool:
        """Compare a supplied original snapshot without rebinding any durable identity."""
        if (
            self.version_bundle.runtime_release != HISTORICAL_M_AGENT_RELEASE
            or other.version_bundle.runtime_release != CURRENT_M_AGENT_RELEASE
        ):
            return False
        comparison = other.model_copy(
            update={
                "version_bundle": other.version_bundle.model_copy(
                    update=HISTORICAL_M_AGENT_RELEASE.model_dump()
                )
            }
        )
        return self.matches_legacy_recovery_input(comparison)

    def matches_legacy_recovery_input(self, other: FrozenDecisionCase | None = None) -> bool:
        """Compare legacy projections without relaxing the immutable D0 input."""
        comparison_case = other or FrozenDecisionCase.model_validate(load_frozen_case_payload())
        if other is None and supports_case_host_contract(
            self.version_bundle.case_contract_version,
            self.version_bundle.host_contract_version,
        ):
            comparison_case = comparison_case.model_copy(
                update={
                    "version_bundle": comparison_case.version_bundle.model_copy(
                        update={
                            "case_contract_version": self.version_bundle.case_contract_version,
                            "host_contract_version": self.version_bundle.host_contract_version,
                            **self.version_bundle.runtime_release.model_dump(),
                        }
                    )
                }
            )
        return _legacy_recovery_input_payload(self) == _legacy_recovery_input_payload(
            comparison_case
        )

    def legacy_projection_recovery_case(self, framework_run_id: str) -> FrozenDecisionCase:
        """Recover a v1 mapping with its original Run and event/report identities."""
        return self.model_copy(
            update={
                "version_bundle": self.version_bundle.model_copy(
                    update={"report_projection_contract_version": "1.0.0"}
                ),
                "recovery_framework_run_id": framework_run_id,
            }
        )

    @property
    def decision_event_id(self) -> str:
        """Reserve a host-owned append-only event identity without reusing the Run ID."""
        return self.decision_event_id_for_framework_run(self.framework_run_id)

    def decision_event_id_for_framework_run(self, framework_run_id: str) -> str:
        """Allocate an event identity for a recovered original M-Agent Run."""
        version_bundle = self.version_bundle.model_dump(mode="json")
        version_bundle.pop("host_application_version")
        version_bundle.pop("host_source_sha")
        return self._decision_event_id_for_version_bundle(framework_run_id, version_bundle)

    def legacy_decision_event_id_for_framework_run(self, framework_run_id: str) -> str:
        """Read a pre-migration event ID that bound full host provenance."""
        return self._decision_event_id_for_version_bundle(
            framework_run_id, self.version_bundle.model_dump(mode="json")
        )

    def _decision_event_id_for_version_bundle(
        self, framework_run_id: str, version_bundle: dict[str, Any]
    ) -> str:
        return _stable_id(
            "decision-event",
            {
                "business_object_id": self.business_object_id,
                "framework_run_id": framework_run_id,
                "expected_external_result": self.expected_external_result.model_dump(mode="json"),
                "version_bundle": version_bundle,
            },
        )

    @property
    def report_version_id(self) -> str:
        """Reserve the report identity separately from its source decision event."""
        return self.report_version_id_for_event(self.decision_event_id)

    def report_version_id_for_event(self, decision_event_id: str) -> str:
        """Allocate a projection identity for one retained decision event."""
        return _stable_id(
            "report-version",
            {
                "decision_event_id": decision_event_id,
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
            },
        )

    def correction_report_version_id(self, original_event_id: str) -> str:
        """Keep a correction report identity distinct from both source identities."""
        return _stable_id(
            "report-correction",
            {
                "correction_event_id": self.correction_event_id(original_event_id),
                "correction_contract_version": FROZEN_CORRECTION_CONTRACT_VERSION,
            },
        )


class FrozenBuildIdentity(Protocol):
    @property
    def configuration_version(self) -> str: ...

    @property
    def source_sha(self) -> str: ...


def load_frozen_decision_case(settings: FrozenBuildIdentity) -> FrozenDecisionCase:
    """Create a new synthetic input for this build; saved historical cases are never rebound."""
    payload = load_frozen_case_payload()
    version_bundle = payload.get("version_bundle")
    if not isinstance(version_bundle, dict):
        raise RuntimeError("frozen decision-case fixture has no version bundle")
    version_bundle["host_application_version"] = settings.configuration_version
    version_bundle["host_source_sha"] = settings.source_sha
    version_bundle.update(CURRENT_M_AGENT_RELEASE.model_dump(mode="json"))
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
_COMPLETE_SYNTHETIC_PORTFOLIO_ACCOUNT_IDS = frozenset(
    {"synthetic-account-4017", "synthetic-account-8029"}
)


def host_validation_result(case: FrozenDecisionCase, result: ExternalResult) -> StageResult:
    """Record the host-owned contract gate before classifying a business outcome."""
    recorded_reasons = result.key_reasons
    valid_frozen_input = (
        case.synthetic
        and case.qualification_scope == FROZEN_QUALIFICATION_SCOPE
        and has_complete_synthetic_input(case.input)
    )
    if not recorded_reasons:
        return StageResult(
            phase="HOST_VALIDATION",
            status="FAILED",
            gate_results=(GateResult(gate_id="KEY_REASONS_PRESENT", status="FAILED"),),
            reasons=("RESULT_REASONS_REQUIRED",),
        )
    outcome = _SYNTHETIC_OUTCOMES.get(result.outcome_code)
    if not valid_frozen_input or outcome is None or result != case.expected_external_result:
        return StageResult(
            phase="HOST_VALIDATION",
            status="FAILED",
            gate_results=(GateResult(gate_id="FROZEN_RESULT_MATCH", status="FAILED"),),
            reasons=("UNSUPPORTED_SYNTHETIC_RESULT",),
        )
    return StageResult(
        phase="HOST_VALIDATION",
        status="SUCCEEDED",
        gate_results=(
            GateResult(gate_id="OUTPUT_CONTRACT", status="PASSED"),
            GateResult(gate_id="FROZEN_RESULT_MATCH", status="PASSED"),
        ),
        reasons=recorded_reasons,
    )


def business_outcome_result(result: ExternalResult) -> StageResult:
    """Classify an already validated typed output into its owning business family."""
    outcome = _SYNTHETIC_OUTCOMES[result.outcome_code]
    return StageResult(
        phase=outcome.phase,
        status=outcome.status,
        gate_results=(
            GateResult(gate_id="OUTPUT_CONTRACT", status="PASSED"),
            outcome.gate,
        ),
        reasons=result.key_reasons,
    )


@dataclass(frozen=True)
class _SyntheticOutcomeDefinition:
    phase: StagePhase
    status: StageStatus
    gate: GateResult


_SYNTHETIC_OUTCOMES: dict[str, _SyntheticOutcomeDefinition] = {
    "SYNTHETIC_REVIEW_COMPLETE": _SyntheticOutcomeDefinition(
        "BUSINESS_DECISION",
        "SUCCEEDED",
        GateResult(gate_id="DECISION_ACCEPTED", status="PASSED"),
    ),
    "SYNTHETIC_INPUT_REJECTED": _SyntheticOutcomeDefinition(
        "BUSINESS_DECISION",
        "REJECTED",
        GateResult(gate_id="DECISION_ACCEPTED", status="FAILED"),
    ),
    "SYNTHETIC_RESULT_ABSTAINED": _SyntheticOutcomeDefinition(
        "BUSINESS_DECISION",
        "ABSTAINED",
        GateResult(gate_id="ABSTENTION_RECORDED", status="PASSED"),
    ),
    "SYNTHETIC_RESULT_FAILED": _SyntheticOutcomeDefinition(
        "BUSINESS_DECISION",
        "FAILED",
        GateResult(gate_id="DECISION_COMPLETED", status="FAILED"),
    ),
    "SYNTHETIC_RESULT_PENDING": _SyntheticOutcomeDefinition(
        "ADJUDICATION_LIFECYCLE",
        "PENDING",
        GateResult(gate_id="ADJUDICATION_COMPLETED", status="UNKNOWN"),
    ),
    "SYNTHETIC_RESULT_EXPIRED": _SyntheticOutcomeDefinition(
        "VALIDITY_LIFECYCLE",
        "EXPIRED",
        GateResult(gate_id="VALIDITY_WINDOW", status="FAILED"),
    ),
    "SYNTHETIC_RESULT_EXECUTION_BLOCKED": _SyntheticOutcomeDefinition(
        "EXECUTION_LIFECYCLE",
        "EXECUTION_BLOCKED",
        GateResult(gate_id="EXECUTION_AVAILABLE", status="FAILED"),
    ),
    "SYNTHETIC_RESULT_UNKNOWN": _SyntheticOutcomeDefinition(
        "ADJUDICATION_LIFECYCLE",
        "UNKNOWN",
        GateResult(gate_id="DECISION_DETERMINED", status="UNKNOWN"),
    ),
}


def is_committable_host_outcome(stage_result: StageResult) -> bool:
    """Permit validated business and lifecycle outcomes to become host facts."""
    return stage_result.phase in _COMMITTABLE_HOST_OUTCOME_PHASES


def business_result_status_from_stage(
    stage_result: StageResult,
) -> BusinessResultStatus | None:
    """Extract a host business outcome while preserving lifecycle-only states."""
    if (
        stage_result.phase == "BUSINESS_DECISION"
        and stage_result.status in _BUSINESS_RESULT_STATUSES
    ):
        return cast(BusinessResultStatus, stage_result.status)
    return None


def business_lifecycle_from_stage(
    stage_result: StageResult,
) -> BusinessLifecycle | None:
    """Extract a lifecycle state together with its owning phase."""
    owner = _LIFECYCLE_OWNER_BY_PHASE.get(stage_result.phase)
    if owner is not None and stage_result.status in _BUSINESS_LIFECYCLE_STATUSES:
        return BusinessLifecycle(
            owner=owner,
            phase=cast(LifecycleStagePhase, stage_result.phase),
            status=cast(BusinessLifecycleStatus, stage_result.status),
        )
    return None


def framework_run_status_from_stage(stage_result: StageResult) -> FrameworkRunStatus:
    """Read a saved framework state without treating host states as framework data."""
    if stage_result.phase == "FRAMEWORK_RUN" and stage_result.status in _FRAMEWORK_RUN_STATUSES:
        return cast(FrameworkRunStatus, stage_result.status)
    raise RuntimeError("stage result does not contain a framework run state")


def has_complete_synthetic_input(value: dict[str, Any]) -> bool:
    """Recognize one complete, versioned synthetic result-family input."""
    return synthetic_outcome_code_from_input(value) is not None


def synthetic_outcome_code_from_input(value: dict[str, Any]) -> str | None:
    """Read the explicit synthetic scenario without consulting expected output."""
    input_without_scenario = dict(value)
    outcome_code = input_without_scenario.pop("scenario", "SYNTHETIC_REVIEW_COMPLETE")
    account = input_without_scenario.get("account")
    if (
        isinstance(account, dict)
        and account.get("account_id") in _COMPLETE_SYNTHETIC_PORTFOLIO_ACCOUNT_IDS
    ):
        normalized_account = dict(account)
        normalized_account["account_id"] = "synthetic-account-4017"
        input_without_scenario["account"] = normalized_account
    if input_without_scenario != _COMPLETE_SYNTHETIC_INPUT:
        return None
    return outcome_code if outcome_code in _SYNTHETIC_OUTCOMES else None


def _fingerprint(value: object) -> str:
    return sha256(_canonical_json(value).encode()).hexdigest()


def _recovery_input_payload(case: FrozenDecisionCase) -> dict[str, Any]:
    """Normalize only host build provenance when comparing retained snapshots."""
    payload = case.model_dump(mode="json")
    version_bundle = payload["version_bundle"]
    assert isinstance(version_bundle, dict)
    for build_identity_field in ("host_application_version", "host_source_sha"):
        version_bundle.pop(build_identity_field)
    return payload


def _legacy_recovery_input_payload(case: FrozenDecisionCase) -> dict[str, Any]:
    """Compare snapshotless records to the canonical D0 input across old projections."""
    payload = _recovery_input_payload(case)
    version_bundle = payload["version_bundle"]
    assert isinstance(version_bundle, dict)
    version_bundle.pop("report_projection_contract_version")
    return payload


def _stable_id(kind: str, value: object) -> str:
    return f"{kind}-{_fingerprint(value)}"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
