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


class FormalCheckIdentity(GovernanceContract):
    sequence_id: str = Field(min_length=1)
    index: int = Field(ge=1)
    registered_at: AwareDatetime
    scheduled_at: AwareDatetime


class QualificationEvidence(GovernanceContract):
    evidence_id: str = Field(min_length=1)
    synthetic: Literal[True]
    generator_version: str = Field(min_length=1)
    seed: int
    version: CapabilityVersion
    scope: QualificationScope
    kind: Literal[
        "QUALIFICATION_PASS",
        "DIAGNOSTIC_ALERT",
        "INSUFFICIENT_EVIDENCE",
        "REQUIRED_PREMISE_UNVERIFIABLE",
        "ORIGINAL_BASIS_INVALID",
        "RESTORATION_DECISION",
        "ORIGINAL_BASIS_RESTORED",
        "REQUALIFICATION_PASS",
        "HISTORICAL_OOS_PASS",
        "LOCKED_FORWARD_PASS",
    ]
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_end: AwareDatetime
    available_at: AwareDatetime
    expires_at: AwareDatetime
    state_activity_end: AwareDatetime | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    resolves_evidence_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    restored_authorization_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", exclude_if=lambda value: value is None
    )
    formal_check: FormalCheckIdentity | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class RequalificationProof(GovernanceContract):
    application_id: str = Field(min_length=1)
    registered_at: AwareDatetime
    locked_at: AwareDatetime
    first_prediction_frozen_at: AwareDatetime
    historical_evidence: QualificationEvidence
    forward_evidence: QualificationEvidence


class QualificationCommand(GovernanceContract):
    operation: Literal["QUALIFICATION"]
    action: Literal[
        "GRANT", "ALERT", "RECORD_NOT_OBTAINED", "SUSPEND", "REVOKE", "RESTORE", "REQUALIFY"
    ]
    scope: QualificationScope
    version: CapabilityVersion
    previous_decision_id: str | None
    evidence: QualificationEvidence
    restoration_evidence: tuple[QualificationEvidence, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    requalification: RequalificationProof | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class QualificationRestriction(GovernanceContract):
    cause: Literal["REQUIRED_PREMISE_UNVERIFIABLE", "ORIGINAL_BASIS_INVALID", "EVIDENCE_EXPIRED"]
    evidence: QualificationEvidence


class QualificationRecord(GovernanceContract):
    decision_id: str
    authorization_id: str | None
    scope: QualificationScope
    version: CapabilityVersion
    status: Literal["NOT_OBTAINED", "VALID", "AT_RISK", "SUSPENDED", "REVOKED"]
    cause: str
    authorization_evidence: QualificationEvidence | None
    authorization_terminated_at: AwareDatetime | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    recorded_at: AwareDatetime
    previous_decision_id: str | None = None
    evidence: QualificationEvidence
    alerts: tuple[QualificationEvidence, ...] = ()
    restrictions: tuple[QualificationRestriction, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    restoration_evidence: tuple[QualificationEvidence, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    requalification: RequalificationProof | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class GovernanceOutcome(GovernanceContract):
    disposition: Literal["APPROVED", "DENIED"]
    reasons: tuple[str, ...]
    qualification: QualificationRecord | None = None
