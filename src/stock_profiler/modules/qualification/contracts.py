"""Explicitly synthetic qualification commands and immutable audit projections."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

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
    holding_age_domain: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )
    renewal_ordinal: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )
    probability_grid: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )
    portfolio_scope: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )

    def same_scope_as(self, other: QualificationScope) -> bool:
        """Compare account membership without changing either frozen representation."""
        return set(self.account_ids) == set(other.account_ids) and self.model_dump(
            exclude={"account_ids"}
        ) == other.model_dump(exclude={"account_ids"})


class QualificationPolicy(GovernanceContract):
    """Explicit D0 policy input, never a default for personal authorization."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: Literal["1.0.0"]
    policy_version: str = Field(min_length=1, pattern=r"^\S+$")
    synthetic: Literal[True]
    generator_version: str = Field(min_length=1, pattern=r"^\S+$")
    seed: int
    evaluation_max_age_months: int = Field(gt=0)
    state_activity_max_age_months: int = Field(gt=0)
    require_state_activity: bool

    @field_validator("synthetic", mode="before")
    @classmethod
    def require_synthetic_boolean(cls, value: object) -> Literal[True]:
        if value is not True:
            raise ValueError("policy must explicitly declare synthetic: true")
        return True


class CapabilityVersion(GovernanceContract):
    version_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    implementation: DecisionCaseVersionBundle
    qualification_policy: QualificationPolicy | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @property
    def policy_error(self) -> str | None:
        if self.qualification_policy is None:
            return "QUALIFICATION_POLICY_REQUIRED"
        if self.qualification_policy.policy_version != self.policy_version:
            return "QUALIFICATION_POLICY_VERSION_MISMATCH"
        return None


class FormalCheckIdentity(GovernanceContract):
    sequence_id: str = Field(min_length=1)
    index: int = Field(ge=1)
    registered_at: AwareDatetime
    scheduled_at: AwareDatetime
    planned_nodes: tuple[AwareDatetime, ...] = Field(default=(), exclude_if=lambda value: not value)
    required_gates: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)


class QualificationGate(GovernanceContract):
    gate_id: str = Field(min_length=1)
    passed: bool


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
        "FORMAL_CHECK",
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
    maturity_sufficient: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    gate_results: tuple[QualificationGate, ...] = Field(
        default=(), exclude_if=lambda value: not value
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
        "GRANT",
        "ALERT",
        "RECORD_NOT_OBTAINED",
        "SUSPEND",
        "REVOKE",
        "RESTORE",
        "REQUALIFY",
        "FORMAL_CHECK",
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
    cause: Literal[
        "REQUIRED_PREMISE_UNVERIFIABLE",
        "ORIGINAL_BASIS_INVALID",
        "EVIDENCE_EXPIRED",
        "FORMAL_PERFORMANCE_FAILURE",
    ]
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

    formal_evidence: QualificationEvidence | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    formal_passing_evidence: QualificationEvidence | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    last_formal_node: AwareDatetime | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    def evidence_available_by(self, cutoff: datetime) -> bool:
        return all(
            item is None or item.available_at <= cutoff
            for item in (
                self.authorization_evidence,
                self.evidence,
                self.formal_evidence,
                self.formal_passing_evidence,
                *self.alerts,
                *(restriction.evidence for restriction in self.restrictions),
                *self.restoration_evidence,
            )
        )


class EstablishedObligation(GovernanceContract):
    obligation_id: str = Field(min_length=1)
    basis_reference: str = Field(min_length=1)
    direction: Literal["REDUCE", "EXIT"]
    quantity_status: Literal["UNKNOWN"]


class LegalTaskNode(GovernanceContract):
    node_id: str = Field(min_length=1)
    task_identity: str = Field(min_length=1)
    kind: Literal["MONTHLY", "DAILY", "RENEWAL"]
    scheduled_at: AwareDatetime
    knowledge_cutoff: AwareDatetime
    valid_until: AwareDatetime
    deterministic_obligations: tuple[EstablishedObligation, ...] = ()


class RegisterTaskNodeCommand(GovernanceContract):
    operation: Literal["REGISTER_TASK_NODE"]
    scope: QualificationScope
    node: LegalTaskNode


class ActivateVersionCommand(GovernanceContract):
    operation: Literal["ACTIVATE_VERSION"]
    scope: QualificationScope
    version: CapabilityVersion
    previous_version: CapabilityVersion | None
    previous_activation_id: str | None
    qualification_decision_id: str
    first_node_id: str


class FreezeTaskCommand(GovernanceContract):
    operation: Literal["FREEZE_TASK"]
    scope: QualificationScope
    version: CapabilityVersion
    node_id: str
    activation_id: str | None


class UseTaskCommand(GovernanceContract):
    operation: Literal["USE_TASK"]
    scope: QualificationScope
    version: CapabilityVersion
    task_identity: str


GovernanceCommand: TypeAlias = Annotated[
    QualificationCommand
    | RegisterTaskNodeCommand
    | ActivateVersionCommand
    | FreezeTaskCommand
    | UseTaskCommand,
    Field(discriminator="operation"),
]


class RegisteredTaskNode(GovernanceContract):
    decision_id: str
    scope: QualificationScope
    node: LegalTaskNode
    registered_at: AwareDatetime


class VersionActivation(GovernanceContract):
    decision_id: str
    scope: QualificationScope
    version: CapabilityVersion
    previous_version: CapabilityVersion | None
    previous_activation_id: str | None
    qualification_snapshot: QualificationRecord
    first_node: LegalTaskNode
    retained_task_ids: tuple[str, ...]
    recorded_at: AwareDatetime


class FrozenTask(GovernanceContract):
    decision_id: str
    scope: QualificationScope
    version: CapabilityVersion
    node: LegalTaskNode
    activation_id: str | None
    qualification_snapshot: QualificationRecord | None
    freeze_status: Literal["APPROVED", "DENIED"]
    reasons: tuple[str, ...]
    frozen_at: AwareDatetime


class TaskUsage(GovernanceContract):
    allowed: bool
    task_snapshot: FrozenTask
    qualification_snapshot: QualificationRecord | None
    reasons: tuple[str, ...]
    checked_at: AwareDatetime


class PolicyBinding(GovernanceContract):
    """Frozen scoped policy input, independent of whether authority was obtained."""

    scope: QualificationScope
    version: CapabilityVersion


class GovernanceOutcome(GovernanceContract):
    disposition: Literal["APPROVED", "DENIED"]
    reasons: tuple[str, ...]
    qualification: QualificationRecord | None = None
    task_node: RegisteredTaskNode | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    activation: VersionActivation | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    task: FrozenTask | None = Field(default=None, exclude_if=lambda value: value is None)
    usage: TaskUsage | None = Field(default=None, exclude_if=lambda value: value is None)
    policy_binding: PolicyBinding | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @property
    def bound_policy(self) -> PolicyBinding | None:
        if self.policy_binding is not None:
            return self.policy_binding
        if self.qualification is not None:
            return PolicyBinding(scope=self.qualification.scope, version=self.qualification.version)
        return None
