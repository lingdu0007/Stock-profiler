from __future__ import annotations

from copy import deepcopy
from typing import Any

from governance_proofs import original_basis
from test_scoped_qualification import (
    GovernanceClock,
    case_payload,
    qualification_command,
)

from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import DecisionCaseExecution


def execute(
    settings: Settings,
    identity: str,
    command: dict[str, Any],
    *,
    observed_at: str = "2042-05-17T16:01:00Z",
) -> DecisionCaseExecution:
    return run_frozen_decision_case(
        settings,
        case_payload(settings, identity, command, contract_version="5.0.0"),
        clock=GovernanceClock(observed_at),
    )


def test_integrity_restoration_verifies_original_content_and_dependency_windows(
    migrated_settings: Settings,
) -> None:
    grant_command = qualification_command(migrated_settings)
    grant_command["evidence"]["basis"] = original_basis()
    grant_command["evidence"]["digest"] = (
        "8dfe431dfa9b6b7315567f2efa45f4033e309daefb62032cf04bb7930c8413af"
    )
    granted = execute(migrated_settings, "content-basis-grant", grant_command)
    assert granted.report is not None

    suspend_command = qualification_command(
        migrated_settings,
        action="SUSPEND",
        previous=granted.decision_event_id,
    )
    suspend_command["evidence"].update(
        kind="REQUIRED_PREMISE_UNVERIFIABLE",
        evidence_id="synthetic-basis-unavailable",
    )
    suspended = execute(migrated_settings, "content-basis-suspended", suspend_command)
    assert suspended.report is not None

    restore_command = qualification_command(
        migrated_settings,
        action="RESTORE",
        previous=suspended.decision_event_id,
    )
    restore_command["evidence"].update(
        kind="RESTORATION_DECISION",
        evidence_id="synthetic-content-restoration-decision",
    )
    proof = deepcopy(grant_command["evidence"])
    proof.update(
        kind="ORIGINAL_BASIS_RESTORED",
        evidence_id="synthetic-content-restoration-proof",
        resolves_evidence_id="synthetic-basis-unavailable",
        restored_authorization_digest=grant_command["evidence"]["digest"],
    )
    proof["basis"]["dependencies"][0]["valid_until"] = "2042-05-05T00:00:00Z"
    restore_command["restoration_evidence"] = [proof]
    rejected = execute(migrated_settings, "content-basis-rejected", restore_command)
    assert rejected.report is not None
    rejected_outcome = rejected.report.result.governance
    assert rejected_outcome is not None
    assert rejected_outcome.disposition == "DENIED"
    assert rejected_outcome.reasons == ("QUALIFICATION_RESTORATION_INCOMPLETE",)

    proof["basis"] = original_basis()
    restore_command["restoration_evidence"] = [proof]
    restored = execute(migrated_settings, "content-basis-restored", restore_command)
    assert restored.report is not None
    restored_outcome = restored.report.result.governance
    assert restored_outcome is not None
    assert restored_outcome.disposition == "APPROVED"
    assert restored_outcome.qualification is not None
    assert restored_outcome.qualification.status == "VALID"
    assert restored_outcome.qualification.authorization_id == granted.decision_event_id
    assert restored_outcome.qualification.authorization_evidence is not None
    assert restored_outcome.qualification.authorization_evidence.basis is not None
    assert (
        restored_outcome.qualification.authorization_evidence.basis.model_dump(mode="json")
        == original_basis()
    )
    assert granted.report.result.governance is not None
    assert granted.report.result.governance.qualification is not None
    assert granted.report.result.governance.qualification.status == "VALID"
