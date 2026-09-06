"""Explicitly synthetic qualification commands and immutable audit projections."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from stock_profiler.foundation.decision_versions import DecisionCaseVersionBundle


class GovernanceContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QualificationScope(GovernanceContract):
    capability: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    evidence_level: Literal["D0"]
    user_id: str = Field(min_length=1)
    account_ids: tuple[str, ...] = Field(min_length=1)
    account_type: str = Field(min_length=1)
    source: str = Field(min_length=1)
    market_state: str = Field(min_length=1)
    board: str = Field(min_length=1)
    target: str = Field(min_length=1)


class CapabilityVersion(GovernanceContract):
    version_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    implementation: DecisionCaseVersionBundle


class QualificationEvidence(GovernanceContract):
    evidence_id: str = Field(min_length=1)
    synthetic: Literal[True]
    generator_version: str = Field(min_length=1)
    seed: int
    version: CapabilityVersion
    scope: QualificationScope
    kind: Literal["QUALIFICATION_PASS", "DIAGNOSTIC_ALERT"]
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_end: AwareDatetime
    available_at: AwareDatetime
    expires_at: AwareDatetime


class QualificationCommand(GovernanceContract):
    operation: Literal["QUALIFICATION"]
    action: Literal["GRANT", "ALERT"]
    scope: QualificationScope
    version: CapabilityVersion
    previous_decision_id: str | None
    evidence: QualificationEvidence


class QualificationRecord(GovernanceContract):
    decision_id: str
    authorization_id: str
    scope: QualificationScope
    version: CapabilityVersion
    status: Literal["VALID", "AT_RISK"]
    cause: str
    authorization_evidence: QualificationEvidence
    recorded_at: AwareDatetime
    previous_decision_id: str | None = None
    evidence: QualificationEvidence
    alerts: tuple[QualificationEvidence, ...] = ()


class GovernanceOutcome(GovernanceContract):
    disposition: Literal["APPROVED", "DENIED"]
    reasons: tuple[str, ...]
    qualification: QualificationRecord | None = None
