"""Frozen, public D0 decision-case contracts and stable identities."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_SYNTHETIC_CASE_PATH = (
    REPOSITORY_ROOT / "tests" / "fixtures" / "synthetic" / "replayable_frozen_decision_case.json"
)


class FrozenContract(BaseModel):
    """Reject unversioned fields so a frozen case cannot silently expand."""

    model_config = ConfigDict(extra="forbid", frozen=True)


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
    agent_definition_id: str
    agent_definition_version: str
    output_contract_version: str
    m_agent_version: str
    m_agent_release_commit: str


class ExternalResult(FrozenContract):
    """The user-visible result expected from this original synthetic fixture."""

    outcome_code: str
    summary: str
    key_reasons: tuple[str, ...]


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


class DecisionCaseExecution(FrozenContract):
    """The three status boundaries the CLI and delivery surface must distinguish."""

    business_object_id: str
    framework_run_id: str
    decision_event_id: str
    report_version_id: str
    framework_run_status: str
    business_commit_status: str
    publication_status: str
    report: FormalReport


class FrozenDecisionCase(FrozenContract):
    """One replayable synthetic decision input with all clocks and contracts fixed."""

    synthetic: bool
    generator_version: str
    seed: int
    case_id: str
    business_identity: str
    knowledge_cutoff: str
    evidence_clock: EvidenceClock
    qualification_scope: str
    version_bundle: DecisionCaseVersionBundle
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
        if not self.qualification_scope.startswith("D0_"):
            raise ValueError("frozen decision cases are limited to the D0 synthetic scope")
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
                "report_contract_version": self.version_bundle.output_contract_version,
            },
        )


def load_frozen_decision_case(path: Path = DEFAULT_SYNTHETIC_CASE_PATH) -> FrozenDecisionCase:
    """Load an explicitly declared original synthetic case from its versioned fixture."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return FrozenDecisionCase.model_validate(payload)


def _fingerprint(value: object) -> str:
    return sha256(_canonical_json(value).encode()).hexdigest()


def _stable_id(kind: str, value: object) -> str:
    return f"{kind}-{_fingerprint(value)}"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
