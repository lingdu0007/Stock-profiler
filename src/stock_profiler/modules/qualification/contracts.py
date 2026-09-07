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
    diagnostic_clear_node_count: int | None = Field(
        default=None, gt=0, exclude_if=lambda value: value is None
    )

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

    def same_substantive_version_as(self, other: CapabilityVersion) -> bool:
        """Compare the rules that govern qualification, not release aliases."""
        return self._substantive_identity() == other._substantive_identity()

    def _substantive_identity(self) -> dict[str, object]:
        identity = self.model_dump()
        identity.pop("version_id")
        identity.pop("policy_version")
        policy = identity.get("qualification_policy")
        if isinstance(policy, dict):
            policy.pop("policy_version")
            policy.pop("generator_version")
            policy.pop("seed")
        implementation = identity["implementation"]
        if isinstance(implementation, dict):
            implementation.pop("host_source_sha")
        return identity


class FormalCheckIdentity(GovernanceContract):
    sequence_id: str = Field(min_length=1)
    index: int = Field(ge=1)
    registered_at: AwareDatetime
    scheduled_at: AwareDatetime
    planned_nodes: tuple[AwareDatetime, ...] = Field(default=(), exclude_if=lambda value: not value)
    required_gates: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)
    error_budget_id: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )


class QualificationGate(GovernanceContract):
    gate_id: str = Field(min_length=1)
    passed: bool


class EvidenceArtifact(GovernanceContract):
    artifact_id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    version: str = Field(min_length=1)
    license_id: str = Field(min_length=1)
    available_at: AwareDatetime
    valid_from: AwareDatetime
    valid_until: AwareDatetime


class EvidenceDependencyWindow(GovernanceContract):
    parent_artifact_id: str = Field(min_length=1)
    dependency_artifact_id: str = Field(min_length=1)
    required_from: AwareDatetime
    required_until: AwareDatetime


class EvidenceBasis(GovernanceContract):
    contract_version: Literal["1.0.0"]
    root_artifact: EvidenceArtifact
    dependencies: tuple[EvidenceArtifact, ...] = Field(min_length=1)
    dependency_windows: tuple[EvidenceDependencyWindow, ...] = Field(min_length=1)


class BasisSubstitutionCheck(GovernanceContract):
    check_id: str = Field(min_length=1)
    kind: Literal["EQUIVALENCE", "MIGRATION", "CROSS_VALIDATION", "REPLAY"]
    original_basis_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    substitute_basis_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    passed: Literal[True]
    available_at: AwareDatetime


class CertifiedDependencyWindow(GovernanceContract):
    original_dependency_artifact_id: str = Field(min_length=1)
    substitute_dependency_artifact_id: str = Field(min_length=1)
    required_from: AwareDatetime
    required_until: AwareDatetime


class CertifiedBasisSubstitution(GovernanceContract):
    contract_version: Literal["1.0.0"]
    original_basis_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    substitute_basis_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependency_windows: tuple[CertifiedDependencyWindow, ...] = Field(min_length=1)
    checks: tuple[BasisSubstitutionCheck, ...] = Field(min_length=4)


class DiagnosticPlan(GovernanceContract):
    contract_version: Literal["1.0.0"]
    rule_version: str = Field(min_length=1)
    planned_nodes: tuple[AwareDatetime, ...] = Field(min_length=1)


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
        "FORMAL_NODE_NOT_EXECUTED",
        "DIAGNOSTIC_CLEAR",
        "DIAGNOSTIC_RECURRENT",
        "DIAGNOSTIC_INSUFFICIENT",
        "DIAGNOSTIC_UNAVAILABLE",
        "ALERT_CLOSURE",
        "ALERT_PROVEN_ERRONEOUS",
        "ALERT_TRANSFERRED",
        "ALERT_ARCHIVED",
        "CERTIFIED_BASIS_SUBSTITUTION",
        "STATE_ACTIVITY_RESTORED",
        "REQUALIFICATION_APPLICATION_REGISTERED",
        "ALERT_REPLAY_RECORDED",
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
    requalification_application_id: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )
    requalification_population_id: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )
    requalification_registration_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    reviewed_alert_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    formal_check: FormalCheckIdentity | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    maturity_sufficient: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    gate_results: tuple[QualificationGate, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    basis: EvidenceBasis | None = Field(default=None, exclude_if=lambda value: value is None)
    certified_substitution: CertifiedBasisSubstitution | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    diagnostic_plan: DiagnosticPlan | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class RequalificationMember(GovernanceContract):
    member_id: str = Field(min_length=1)
    prediction_frozen_at: AwareDatetime
    available_at: AwareDatetime
    matured_at: AwareDatetime
    outcome_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class RequalificationPopulation(GovernanceContract):
    population_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    frozen_version_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    registered_member_ids: tuple[str, ...] = Field(min_length=1)
    members: tuple[RequalificationMember, ...] = Field(min_length=1)
    required_gates: tuple[str, ...] = Field(min_length=1)
    gate_results: tuple[QualificationGate, ...] = Field(min_length=1)


class RequalificationPopulationRegistration(GovernanceContract):
    application_id: str = Field(min_length=1)
    population_kind: Literal["HISTORICAL", "FORWARD"]
    population_id: str = Field(min_length=1)
    registered_at: AwareDatetime
    locked_at: AwareDatetime
    frozen_version_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    registered_member_ids: tuple[str, ...] = Field(min_length=1)
    required_gates: tuple[str, ...] = Field(min_length=1)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class RequalificationApplication(GovernanceContract):
    application_id: str = Field(min_length=1)
    registered_at: AwareDatetime
    locked_at: AwareDatetime
    frozen_version_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    historical_registration: RequalificationPopulationRegistration
    forward_registration: RequalificationPopulationRegistration


class RequalificationProof(GovernanceContract):
    application_id: str = Field(min_length=1)
    registered_at: AwareDatetime
    locked_at: AwareDatetime
    first_prediction_frozen_at: AwareDatetime
    frozen_version_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", exclude_if=lambda value: value is None
    )
    historical_evidence: QualificationEvidence
    forward_evidence: QualificationEvidence
    historical_population: RequalificationPopulation | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    forward_population: RequalificationPopulation | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    historical_registration: RequalificationPopulationRegistration | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    forward_registration: RequalificationPopulationRegistration | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class DiagnosticObservation(GovernanceContract):
    scheduled_at: AwareDatetime
    status: Literal["CLEAR", "RECURRENT", "INSUFFICIENT", "UNAVAILABLE"]
    rule_version: str = Field(min_length=1)
    evidence: QualificationEvidence


class ErroneousAlertReplay(GovernanceContract):
    contract_version: Literal["1.0.0"]
    replay_id: str = Field(min_length=1)
    alert_evidence_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    original_information: QualificationEvidence
    original_information_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    replay_result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    alert_reproduced: Literal[False]
    available_at: AwareDatetime


class AlertClosureProof(GovernanceContract):
    contract_version: Literal["1.0.0"]
    alert_evidence_id: str = Field(min_length=1)
    resolution: Literal["DISAPPEARED", "PROVEN_ERRONEOUS", "TRANSFERRED", "ARCHIVED"]
    rule_version: str = Field(min_length=1)
    observations: tuple[DiagnosticObservation, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    resolution_evidence: QualificationEvidence | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    erroneous_replay_id: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )
    transferred_restriction_evidence_id: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )


class FormalNodeDisposition(GovernanceContract):
    scheduled_at: AwareDatetime
    status: Literal["EXECUTED_PASS", "EXECUTED_FAIL", "INSUFFICIENT", "NOT_EXECUTED"]
    check_index: int = Field(ge=1)
    evidence: QualificationEvidence
    recorded_at: AwareDatetime


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
        "REGISTER_REQUALIFICATION",
        "RECORD_ALERT_REPLAY",
        "FORMAL_CHECK",
        "RECORD_FORMAL_NODE",
        "CLOSE_ALERT",
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
    requalification_application: RequalificationApplication | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    alert_replay: ErroneousAlertReplay | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    alert_closure: AlertClosureProof | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class QualificationRestriction(GovernanceContract):
    cause: Literal[
        "REQUIRED_PREMISE_UNVERIFIABLE",
        "ORIGINAL_BASIS_INVALID",
        "EVIDENCE_EXPIRED",
        "STATE_ACTIVITY_EXPIRED",
        "FORMAL_PERFORMANCE_FAILURE",
    ]
    evidence: QualificationEvidence


class RecordedAlertReplay(GovernanceContract):
    decision_id: str
    scope: QualificationScope
    version: CapabilityVersion
    previous_qualification_decision_id: str
    replay: ErroneousAlertReplay
    evidence: QualificationEvidence
    recorded_at: AwareDatetime


class AlertClosure(GovernanceContract):
    alert_evidence_id: str = Field(min_length=1)
    resolution: Literal["DISAPPEARED", "PROVEN_ERRONEOUS", "TRANSFERRED", "ARCHIVED"]
    proof: AlertClosureProof
    evidence: QualificationEvidence
    recorded_replay: RecordedAlertReplay | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    recorded_at: AwareDatetime

    @property
    def terminates_alert(self) -> bool:
        return self.resolution in {"DISAPPEARED", "PROVEN_ERRONEOUS"}


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
    alert_closures: tuple[AlertClosure, ...] = Field(default=(), exclude_if=lambda value: not value)
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
    state_activity_evidence: QualificationEvidence | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    last_formal_node: AwareDatetime | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    formal_node_dispositions: tuple[FormalNodeDisposition, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )

    @property
    def outstanding_alerts(self) -> tuple[QualificationEvidence, ...]:
        closed = {item.alert_evidence_id for item in self.alert_closures if item.terminates_alert}
        return tuple(item for item in self.alerts if item.evidence_id not in closed)

    def evidence_available_by(self, cutoff: datetime) -> bool:
        return all(
            item is None or item.available_at <= cutoff
            for item in (
                self.authorization_evidence,
                self.evidence,
                self.formal_evidence,
                self.formal_passing_evidence,
                self.state_activity_evidence,
                *self.alerts,
                *(closure.evidence for closure in self.alert_closures),
                *(
                    observation.evidence
                    for closure in self.alert_closures
                    for observation in closure.proof.observations
                ),
                *(closure.proof.resolution_evidence for closure in self.alert_closures),
                *(
                    closure.recorded_replay.evidence
                    for closure in self.alert_closures
                    if closure.recorded_replay is not None
                ),
                *(
                    closure.recorded_replay.replay.original_information
                    for closure in self.alert_closures
                    if closure.recorded_replay is not None
                ),
                *(restriction.evidence for restriction in self.restrictions),
                *self.restoration_evidence,
                *(disposition.evidence for disposition in self.formal_node_dispositions),
            )
        )


class EstablishedObligation(GovernanceContract):
    obligation_id: str = Field(min_length=1)
    basis_reference: str = Field(min_length=1)
    direction: Literal["REDUCE", "EXIT"]
    quantity_status: Literal["UNKNOWN"]


class RetainedObjectReference(GovernanceContract):
    kind: Literal["CONCLUSION", "CONTRACT", "EVIDENCE", "EVALUATION"]
    object_id: str = Field(min_length=1)


class LegalTaskNode(GovernanceContract):
    node_id: str = Field(min_length=1)
    task_identity: str = Field(min_length=1)
    kind: Literal["MONTHLY", "DAILY", "RENEWAL"]
    scheduled_at: AwareDatetime
    knowledge_cutoff: AwareDatetime
    valid_until: AwareDatetime
    deterministic_obligations: tuple[EstablishedObligation, ...] = ()
    retained_objects: tuple[RetainedObjectReference, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )


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


class RegisteredRequalificationApplication(GovernanceContract):
    decision_id: str
    scope: QualificationScope
    version: CapabilityVersion
    previous_qualification_decision_id: str
    application: RequalificationApplication
    evidence: QualificationEvidence
    recorded_at: AwareDatetime


class RetainedTaskRelation(GovernanceContract):
    task_identity: str = Field(min_length=1)
    task_decision_id: str = Field(min_length=1)
    original_version: CapabilityVersion
    node_id: str = Field(min_length=1)
    activation_id: str | None
    qualification_decision_id: str | None
    knowledge_cutoff: AwareDatetime
    scheduled_at: AwareDatetime
    valid_until: AwareDatetime
    retained_objects: tuple[RetainedObjectReference, ...] = ()
    deterministic_obligations: tuple[EstablishedObligation, ...] = ()


class VersionActivation(GovernanceContract):
    decision_id: str
    scope: QualificationScope
    version: CapabilityVersion
    previous_version: CapabilityVersion | None
    previous_activation_id: str | None
    qualification_snapshot: QualificationRecord
    first_node: LegalTaskNode
    retained_task_ids: tuple[str, ...]
    retained_tasks: tuple[RetainedTaskRelation, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
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
    registered_requalification: RegisteredRequalificationApplication | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    recorded_alert_replay: RecordedAlertReplay | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
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
