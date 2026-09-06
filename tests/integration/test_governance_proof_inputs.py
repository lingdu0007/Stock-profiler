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


def test_formal_sequence_requires_a_disposition_for_every_planned_node(
    migrated_settings: Settings,
) -> None:
    planned = [
        "2042-05-06T00:00:00Z",
        "2042-06-03T00:00:00Z",
        "2042-07-08T00:00:00Z",
    ]
    original_check = {
        "sequence_id": "synthetic-disposition-sequence",
        "index": 1,
        "registered_at": "2042-05-01T00:00:00Z",
        "scheduled_at": planned[0],
        "planned_nodes": planned,
        "required_gates": ["synthetic-complete-gate"],
    }
    grant_command = qualification_command(migrated_settings)
    grant_command["evidence"]["formal_check"] = original_check
    granted = execute(migrated_settings, "disposition-grant", grant_command)

    check_command = qualification_command(
        migrated_settings,
        action="FORMAL_CHECK",
        previous=granted.decision_event_id,
    )
    check_command["evidence"].update(
        kind="FORMAL_CHECK",
        evidence_id="synthetic-disposition-check",
        evaluation_end=planned[2],
        available_at="2042-07-09T08:00:00Z",
        expires_at="2043-03-01T00:00:00Z",
        formal_check={**original_check, "index": 2, "scheduled_at": planned[2]},
        maturity_sufficient=True,
        gate_results=[{"gate_id": "synthetic-complete-gate", "passed": True}],
    )
    skipped_payload = case_payload(
        migrated_settings,
        "disposition-skipped-node",
        check_command,
        contract_version="5.0.0",
    )
    skipped_payload["knowledge_cutoff"] = "2042-07-09T09:00:00Z"
    skipped = run_frozen_decision_case(
        migrated_settings,
        skipped_payload,
        clock=GovernanceClock("2042-07-09T10:00:00Z"),
    )
    assert skipped.report is not None
    skipped_outcome = skipped.report.result.governance
    assert skipped_outcome is not None
    assert skipped_outcome.disposition == "DENIED"
    assert skipped_outcome.reasons == ("FORMAL_CHECK_NOT_ALLOWED",)

    disposition_command = qualification_command(
        migrated_settings,
        action="RECORD_FORMAL_NODE",
        previous=granted.decision_event_id,
    )
    disposition_command["evidence"].update(
        kind="FORMAL_NODE_NOT_EXECUTED",
        evidence_id="synthetic-disposition-missed-node",
        evaluation_end=planned[1],
        available_at="2042-06-04T08:00:00Z",
        expires_at="2043-03-01T00:00:00Z",
        formal_check={**original_check, "scheduled_at": planned[1]},
    )
    disposition_payload = case_payload(
        migrated_settings,
        "disposition-recorded-node",
        disposition_command,
        contract_version="5.0.0",
    )
    disposition_payload["knowledge_cutoff"] = "2042-06-04T09:00:00Z"
    disposition = run_frozen_decision_case(
        migrated_settings,
        disposition_payload,
        clock=GovernanceClock("2042-06-04T10:00:00Z"),
    )
    assert disposition.report is not None
    disposition_outcome = disposition.report.result.governance
    assert disposition_outcome is not None
    assert disposition_outcome.disposition == "APPROVED"
    assert disposition_outcome.reasons == ("FORMAL_NODE_NOT_EXECUTED",)
    disposition_record = disposition_outcome.qualification
    assert disposition_record is not None
    assert disposition_record.formal_evidence is None
    assert disposition_record.formal_node_dispositions[0].status == "NOT_EXECUTED"

    check_command["previous_decision_id"] = disposition.decision_event_id
    completed_payload = case_payload(
        migrated_settings,
        "disposition-completed-check",
        check_command,
        contract_version="5.0.0",
    )
    completed_payload["knowledge_cutoff"] = skipped_payload["knowledge_cutoff"]
    completed = run_frozen_decision_case(
        migrated_settings,
        completed_payload,
        clock=GovernanceClock("2042-07-09T10:00:00Z"),
    )
    assert completed.report is not None
    completed_outcome = completed.report.result.governance
    assert completed_outcome is not None
    assert completed_outcome.disposition == "APPROVED"
    completed_record = completed_outcome.qualification
    assert completed_record is not None
    assert completed_record.formal_evidence is not None
    assert completed_record.formal_evidence.formal_check is not None
    assert completed_record.formal_evidence.formal_check.index == 2
    assert [item.status for item in completed_record.formal_node_dispositions] == [
        "NOT_EXECUTED",
        "EXECUTED_PASS",
    ]
