"""Evaluate explicit frozen policy inputs without supplying authorization defaults."""

import json
from calendar import month_name, monthrange
from datetime import UTC, datetime
from hashlib import sha256

from stock_profiler.modules.qualification.contracts import (
    CapabilityVersion,
    EvidenceBasis,
    QualificationEvidence,
    RequalificationPopulationRegistration,
)


def overall_qualification_deadline(evidence: QualificationEvidence) -> datetime | None:
    policy = evidence.version.qualification_policy
    if policy is None or evidence.version.policy_error is not None:
        return None
    try:
        return min(
            evidence.expires_at,
            _month_end_after(evidence.evaluation_end, policy.evaluation_max_age_months),
        )
    except (ValueError, OverflowError):
        return None


def state_activity_deadline(evidence: QualificationEvidence) -> datetime | None:
    policy = evidence.version.qualification_policy
    if (
        policy is None
        or evidence.version.policy_error is not None
        or evidence.state_activity_end is None
    ):
        return None
    try:
        return _month_end_after(
            evidence.state_activity_end,
            policy.state_activity_max_age_months,
        )
    except (ValueError, OverflowError):
        return None


def qualification_deadline(
    evidence: QualificationEvidence,
    state_activity_evidence: QualificationEvidence | None = None,
) -> datetime | None:
    policy = evidence.version.qualification_policy
    overall = overall_qualification_deadline(evidence)
    state_source = state_activity_evidence or evidence
    state = state_activity_deadline(state_source)
    if policy is None or overall is None or (policy.require_state_activity and state is None):
        return None
    return min(overall, state) if state is not None else overall


def evidence_is_current(evidence: QualificationEvidence, now: datetime) -> bool:
    deadline = qualification_deadline(evidence)
    return deadline is not None and now <= deadline


def evidence_basis_is_valid(evidence: QualificationEvidence) -> bool:
    basis = evidence.basis
    if basis is None:
        return True
    artifacts = (basis.root_artifact, *basis.dependencies)
    artifact_ids = [item.artifact_id for item in artifacts]
    dependency_ids = {item.artifact_id for item in basis.dependencies}
    windows = {item.dependency_artifact_id: item for item in basis.dependency_windows}
    return (
        len(set(artifact_ids)) == len(artifact_ids)
        and len(windows) == len(basis.dependency_windows)
        and set(windows) == dependency_ids
        and all(
            sha256(item.content.encode()).hexdigest() == item.content_sha256
            and item.valid_from <= item.valid_until
            and item.available_at <= evidence.available_at
            for item in artifacts
        )
        and basis.root_artifact.valid_from
        <= evidence.evaluation_end
        <= basis.root_artifact.valid_until
        and all(
            window.parent_artifact_id == basis.root_artifact.artifact_id
            and artifact.valid_from <= window.required_from <= window.required_until
            and window.required_until <= artifact.valid_until
            and window.required_from <= evidence.evaluation_end <= window.required_until
            for artifact in basis.dependencies
            for window in (windows[artifact.artifact_id],)
        )
        and evidence.digest == evidence_basis_digest(basis)
    )


def evidence_basis_digest(basis: EvidenceBasis) -> str:
    payload = json.dumps(
        basis.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return sha256(payload).hexdigest()


def capability_version_digest(version: CapabilityVersion) -> str:
    payload = json.dumps(
        version.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return sha256(payload).hexdigest()


def requalification_registration_digest(
    registration: RequalificationPopulationRegistration,
) -> str:
    payload = json.dumps(
        registration.model_dump(mode="json", exclude={"digest"}),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return sha256(payload).hexdigest()


def _month_end_after(endpoint: datetime, months: int) -> datetime:
    endpoint = endpoint.astimezone(UTC)
    months_per_year = len(month_name) - 1
    year, zero_month = divmod(
        endpoint.year * months_per_year + endpoint.month - 1 + months, months_per_year
    )
    month = zero_month + 1
    return datetime(year, month, monthrange(year, month)[1], 23, 59, 59, 999999, tzinfo=UTC)


def formal_check_passed(evidence: QualificationEvidence) -> bool | None:
    check = evidence.formal_check
    if (
        check is None
        or not check.required_gates
        or evidence.maturity_sufficient is not True
        or len(set(check.required_gates)) != len(check.required_gates)
        or len(evidence.gate_results) != len(check.required_gates)
        or {gate.gate_id for gate in evidence.gate_results} != set(check.required_gates)
    ):
        return None
    return all(gate.passed for gate in evidence.gate_results)
