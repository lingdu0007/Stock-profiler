from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
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
    knowledge_cutoff: str | None = None,
) -> DecisionCaseExecution:
    payload = case_payload(settings, identity, command, contract_version="5.0.0")
    if knowledge_cutoff is not None:
        payload["knowledge_cutoff"] = knowledge_cutoff
    return run_frozen_decision_case(
        settings,
        payload,
        clock=GovernanceClock(observed_at),
    )


def canonical_digest(payload: dict[str, Any]) -> str:
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


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


def test_requalification_requires_complete_populations_and_original_sequence(
    migrated_settings: Settings,
) -> None:
    planned = [
        "2042-05-06T00:00:00Z",
        "2042-05-31T00:00:00Z",
        "2042-06-30T00:00:00Z",
    ]
    original_check = {
        "sequence_id": "synthetic-requalification-sequence",
        "index": 1,
        "registered_at": "2042-05-01T00:00:00Z",
        "scheduled_at": planned[0],
        "planned_nodes": planned,
        "required_gates": ["synthetic-history-gate", "synthetic-forward-gate"],
        "error_budget_id": "synthetic-requalification-error-budget",
    }
    grant_command = qualification_command(migrated_settings)
    grant_command["evidence"]["formal_check"] = original_check
    granted = execute(migrated_settings, "complete-requalification-grant", grant_command)

    revoke_command = qualification_command(
        migrated_settings,
        action="REVOKE",
        previous=granted.decision_event_id,
    )
    revoke_command["evidence"].update(
        kind="ORIGINAL_BASIS_INVALID",
        evidence_id="synthetic-complete-requalification-revocation",
    )
    revoked = execute(migrated_settings, "complete-requalification-revoked", revoke_command)

    command = qualification_command(
        migrated_settings,
        action="REQUALIFY",
        previous=revoked.decision_event_id,
    )
    command["evidence"].update(
        kind="REQUALIFICATION_PASS",
        evidence_id="synthetic-complete-requalification-pass",
        evaluation_end="2042-05-30T00:00:00Z",
        available_at="2042-05-31T08:00:00Z",
        expires_at="2043-03-01T00:00:00Z",
        formal_check={
            **original_check,
            "index": 2,
            "scheduled_at": planned[1],
        },
        maturity_sufficient=True,
        gate_results=[
            {"gate_id": "synthetic-history-gate", "passed": True},
            {"gate_id": "synthetic-forward-gate", "passed": True},
        ],
    )
    frozen_version_digest = canonical_digest(command["version"])
    historical_members = [
        {
            "member_id": "synthetic-history-member-a",
            "prediction_frozen_at": "2042-03-01T00:00:00Z",
            "available_at": "2042-03-02T00:00:00Z",
            "matured_at": "2042-05-17T00:00:00Z",
            "outcome_digest": "1" * 64,
        },
        {
            "member_id": "synthetic-history-member-b",
            "prediction_frozen_at": "2042-03-03T00:00:00Z",
            "available_at": "2042-03-04T00:00:00Z",
            "matured_at": "2042-05-18T00:00:00Z",
            "outcome_digest": "2" * 64,
        },
    ]
    forward_members = [
        {
            "member_id": "synthetic-forward-member-a",
            "prediction_frozen_at": "2042-05-20T00:00:00Z",
            "available_at": "2042-05-21T00:00:00Z",
            "matured_at": "2042-05-28T00:00:00Z",
            "outcome_digest": "3" * 64,
        },
        {
            "member_id": "synthetic-forward-member-b",
            "prediction_frozen_at": "2042-05-22T00:00:00Z",
            "available_at": "2042-05-23T00:00:00Z",
            "matured_at": "2042-05-29T00:00:00Z",
            "outcome_digest": "4" * 64,
        },
    ]
    historical_evidence = deepcopy(command["evidence"])
    historical_evidence.update(
        kind="HISTORICAL_OOS_PASS",
        evidence_id="synthetic-complete-history",
        evaluation_end="2042-05-18T00:00:00Z",
        available_at="2042-05-19T00:00:00Z",
        formal_check=None,
        maturity_sufficient=None,
        gate_results=[],
    )
    forward_evidence = deepcopy(command["evidence"])
    forward_evidence.update(
        kind="LOCKED_FORWARD_PASS",
        evidence_id="synthetic-complete-forward",
        evaluation_end="2042-05-29T00:00:00Z",
        available_at="2042-05-30T00:00:00Z",
        formal_check=None,
        maturity_sufficient=None,
        gate_results=[],
    )
    command["requalification"] = {
        "application_id": "synthetic-complete-requalification-application",
        "registered_at": "2042-05-18T00:00:00Z",
        "locked_at": "2042-05-19T00:00:00Z",
        "first_prediction_frozen_at": "2042-05-20T00:00:00Z",
        "frozen_version_digest": frozen_version_digest,
        "historical_evidence": historical_evidence,
        "forward_evidence": forward_evidence,
        "historical_population": {
            "population_id": "synthetic-complete-historical-population",
            "evidence_id": historical_evidence["evidence_id"],
            "frozen_version_digest": frozen_version_digest,
            "registered_member_ids": [item["member_id"] for item in historical_members],
            "members": historical_members,
            "required_gates": ["synthetic-history-gate"],
            "gate_results": [{"gate_id": "synthetic-history-gate", "passed": True}],
        },
        "forward_population": {
            "population_id": "synthetic-complete-forward-population",
            "evidence_id": forward_evidence["evidence_id"],
            "frozen_version_digest": frozen_version_digest,
            "registered_member_ids": [item["member_id"] for item in forward_members],
            "members": forward_members,
            "required_gates": ["synthetic-forward-gate"],
            "gate_results": [{"gate_id": "synthetic-forward-gate", "passed": True}],
        },
    }
    incomplete = deepcopy(command)
    incomplete["requalification"]["forward_population"]["members"].pop()
    incomplete_execution = execute(
        migrated_settings,
        "complete-requalification-incomplete",
        incomplete,
        observed_at="2042-05-31T10:00:00Z",
        knowledge_cutoff="2042-05-31T09:00:00Z",
    )
    assert incomplete_execution.report is not None
    incomplete_outcome = incomplete_execution.report.result.governance
    assert incomplete_outcome is not None
    assert incomplete_outcome.disposition == "DENIED"
    assert incomplete_outcome.reasons == ("REQUALIFICATION_PROOF_INVALID",)

    changed_sequence = deepcopy(command)
    changed_sequence["evidence"]["formal_check"]["planned_nodes"][-1] = "2042-07-31T00:00:00Z"
    changed_sequence_execution = execute(
        migrated_settings,
        "complete-requalification-changed-sequence",
        changed_sequence,
        observed_at="2042-05-31T10:00:00Z",
        knowledge_cutoff="2042-05-31T09:00:00Z",
    )
    assert changed_sequence_execution.report is not None
    changed_sequence_outcome = changed_sequence_execution.report.result.governance
    assert changed_sequence_outcome is not None
    assert changed_sequence_outcome.disposition == "DENIED"
    assert changed_sequence_outcome.reasons == ("REQUALIFICATION_PROOF_INVALID",)

    completed = execute(
        migrated_settings,
        "complete-requalification-approved",
        command,
        observed_at="2042-05-31T10:00:00Z",
        knowledge_cutoff="2042-05-31T09:00:00Z",
    )
    assert completed.report is not None
    completed_outcome = completed.report.result.governance
    assert completed_outcome is not None
    assert completed_outcome.disposition == "APPROVED"
    completed_record = completed_outcome.qualification
    assert completed_record is not None
    assert completed_record.status == "VALID"
    assert completed_record.authorization_id == completed.decision_event_id
    assert completed_record.previous_decision_id == revoked.decision_event_id
    assert completed_record.formal_evidence is not None
    assert completed_record.formal_evidence.formal_check is not None
    assert completed_record.formal_evidence.formal_check.index == 2
    assert completed_record.formal_node_dispositions[-1].status == "EXECUTED_PASS"
    assert revoked.report is not None
    assert revoked.report.result.governance is not None
    assert revoked.report.result.governance.qualification is not None
    assert revoked.report.result.governance.qualification.status == "REVOKED"
