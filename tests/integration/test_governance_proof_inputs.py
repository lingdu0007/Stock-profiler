from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any

import pytest
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


def diagnostic_alert(
    settings: Settings,
    identity: str,
    previous: str,
    *,
    rule_version: str,
    planned_nodes: list[str],
) -> DecisionCaseExecution:
    command = qualification_command(settings, action="ALERT", previous=previous)
    command["evidence"].update(
        kind="DIAGNOSTIC_ALERT",
        evidence_id=f"synthetic-{identity}",
        diagnostic_plan={
            "contract_version": "1.0.0",
            "rule_version": rule_version,
            "planned_nodes": planned_nodes,
        },
    )
    return execute(settings, identity, command)


def alert_closure_command(
    settings: Settings,
    previous: str,
    *,
    alert_evidence_id: str,
    rule_version: str,
    planned_nodes: list[str],
) -> dict[str, Any]:
    command = qualification_command(settings, action="CLOSE_ALERT", previous=previous)
    command["evidence"].update(
        kind="ALERT_CLOSURE",
        evidence_id=f"synthetic-closure-{alert_evidence_id}",
        evaluation_end=planned_nodes[-1],
        available_at="2042-06-01T08:00:00Z",
        expires_at="2043-03-01T00:00:00Z",
        resolves_evidence_id=alert_evidence_id,
    )
    observations = []
    for index, scheduled_at in enumerate(planned_nodes):
        observation = deepcopy(command["evidence"])
        observation.update(
            kind="DIAGNOSTIC_CLEAR",
            evidence_id=f"synthetic-clear-{alert_evidence_id}-{index}",
            evaluation_end=scheduled_at,
            available_at=("2042-05-24T08:00:00Z" if index == 0 else "2042-05-31T08:00:00Z"),
            resolves_evidence_id=alert_evidence_id,
        )
        observations.append(
            {
                "scheduled_at": scheduled_at,
                "status": "CLEAR",
                "rule_version": rule_version,
                "evidence": observation,
            }
        )
    command["alert_closure"] = {
        "contract_version": "1.0.0",
        "alert_evidence_id": alert_evidence_id,
        "resolution": "DISAPPEARED",
        "rule_version": rule_version,
        "observations": observations,
    }
    return command


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


def test_integrity_restoration_accepts_only_complete_certified_substitution(
    migrated_settings: Settings,
) -> None:
    grant_command = qualification_command(migrated_settings)
    grant_command["evidence"]["basis"] = original_basis()
    grant_command["evidence"]["digest"] = canonical_digest(original_basis())
    granted = execute(migrated_settings, "certified-substitution-grant", grant_command)

    suspend = qualification_command(
        migrated_settings,
        action="SUSPEND",
        previous=granted.decision_event_id,
    )
    suspend["evidence"].update(
        kind="REQUIRED_PREMISE_UNVERIFIABLE",
        evidence_id="synthetic-certified-basis-unavailable",
    )
    suspended = execute(migrated_settings, "certified-substitution-suspended", suspend)

    substitute = original_basis()
    substitute["root_artifact"].update(
        content="synthetic decision-equivalent replacement content",
        content_sha256=sha256(b"synthetic decision-equivalent replacement content").hexdigest(),
        version="synthetic-root-v2-certified",
    )
    substitute_digest = canonical_digest(substitute)
    restore = qualification_command(
        migrated_settings,
        action="RESTORE",
        previous=suspended.decision_event_id,
    )
    restore["evidence"].update(
        kind="RESTORATION_DECISION",
        evidence_id="synthetic-certified-restoration-decision",
    )
    proof = deepcopy(restore["evidence"])
    proof.update(
        kind="CERTIFIED_BASIS_SUBSTITUTION",
        evidence_id="synthetic-certified-substitution-proof",
        digest=substitute_digest,
        basis=substitute,
        resolves_evidence_id="synthetic-certified-basis-unavailable",
        restored_authorization_digest=grant_command["evidence"]["digest"],
        certified_substitution={
            "contract_version": "1.0.0",
            "original_basis_digest": grant_command["evidence"]["digest"],
            "substitute_basis_digest": substitute_digest,
            "checks": [
                {
                    "check_id": f"synthetic-certified-{kind.lower().replace('_', '-')}",
                    "kind": kind,
                    "original_basis_digest": grant_command["evidence"]["digest"],
                    "substitute_basis_digest": substitute_digest,
                    "passed": True,
                    "available_at": "2042-05-15T00:00:00Z",
                }
                for kind in ("EQUIVALENCE", "MIGRATION", "CROSS_VALIDATION", "REPLAY")
            ],
        },
    )
    incomplete = deepcopy(proof)
    incomplete["certified_substitution"]["checks"][-1]["kind"] = "EQUIVALENCE"
    restore["restoration_evidence"] = [incomplete]
    denied = execute(migrated_settings, "certified-substitution-incomplete", restore)
    assert denied.report is not None
    assert denied.report.result.governance is not None
    assert denied.report.result.governance.disposition == "DENIED"
    assert denied.report.result.governance.reasons == ("QUALIFICATION_RESTORATION_INCOMPLETE",)

    restore["restoration_evidence"] = [proof]
    restored = execute(migrated_settings, "certified-substitution-restored", restore)
    assert restored.report is not None
    outcome = restored.report.result.governance
    assert outcome is not None
    assert outcome.disposition == "APPROVED"
    record = outcome.qualification
    assert record is not None
    assert record.status == "VALID"
    assert record.authorization_id == granted.decision_event_id
    assert record.authorization_evidence is not None
    assert record.authorization_evidence.basis is not None
    assert record.authorization_evidence.basis.model_dump(mode="json") == original_basis()
    assert record.restoration_evidence[0].basis is not None
    assert record.restoration_evidence[0].basis.model_dump(mode="json") == substitute


@pytest.mark.parametrize("formal_node_due", [False, True])
def test_state_activity_only_expiry_restores_without_consuming_a_formal_node(
    migrated_settings: Settings,
    formal_node_due: bool,
) -> None:
    grant_command = qualification_command(migrated_settings)
    grant_command["evidence"]["state_activity_end"] = "2041-12-01T00:00:00Z"
    if formal_node_due:
        grant_command["evidence"]["formal_check"] = {
            "sequence_id": "synthetic-state-activity-formal-sequence",
            "index": 1,
            "registered_at": "2042-05-01T00:00:00Z",
            "scheduled_at": "2042-05-06T00:00:00Z",
            "planned_nodes": [
                "2042-05-06T00:00:00Z",
                "2042-10-01T00:00:00Z",
            ],
            "required_gates": ["synthetic-state-activity-formal-gate"],
        }
    suffix = "formal-due" if formal_node_due else "no-formal-due"
    granted = execute(migrated_settings, f"state-activity-grant-{suffix}", grant_command)

    alert = qualification_command(
        migrated_settings,
        action="ALERT",
        previous=granted.decision_event_id,
    )
    alert["evidence"].update(
        kind="DIAGNOSTIC_ALERT",
        evidence_id="synthetic-state-activity-trigger",
        state_activity_end="2042-10-10T00:00:00Z",
        evaluation_end="2042-10-10T00:00:00Z",
        available_at="2042-10-15T08:00:00Z",
        expires_at="2043-03-01T00:00:00Z",
    )
    restricted = execute(
        migrated_settings,
        f"state-activity-restricted-{suffix}",
        alert,
        observed_at="2042-10-15T10:00:00Z",
        knowledge_cutoff="2042-10-15T09:00:00Z",
    )
    assert restricted.report is not None
    restricted_outcome = restricted.report.result.governance
    assert restricted_outcome is not None
    restricted_record = restricted_outcome.qualification
    assert restricted_record is not None
    assert restricted_record.status == "SUSPENDED"
    assert [(item.cause, item.evidence.evidence_id) for item in restricted_record.restrictions] == [
        ("STATE_ACTIVITY_EXPIRED", "synthetic-qualification-proof-4519")
    ]

    restore = qualification_command(
        migrated_settings,
        action="RESTORE",
        previous=restricted.decision_event_id,
    )
    restore["evidence"].update(
        kind="RESTORATION_DECISION",
        evidence_id="synthetic-state-activity-restoration-decision",
        state_activity_end="2042-10-10T00:00:00Z",
        evaluation_end="2042-10-10T00:00:00Z",
        available_at="2042-10-15T08:30:00Z",
        expires_at="2043-03-01T00:00:00Z",
    )
    proof = deepcopy(restore["evidence"])
    proof.update(
        kind="STATE_ACTIVITY_RESTORED",
        evidence_id="synthetic-state-activity-restored",
        resolves_evidence_id="synthetic-qualification-proof-4519",
    )
    restore["restoration_evidence"] = [proof]
    restored = execute(
        migrated_settings,
        f"state-activity-restored-{suffix}",
        restore,
        observed_at="2042-10-15T10:30:00Z",
        knowledge_cutoff="2042-10-15T09:30:00Z",
    )
    assert restored.report is not None
    outcome = restored.report.result.governance
    assert outcome is not None
    if formal_node_due:
        assert outcome.disposition == "DENIED"
        assert outcome.reasons == ("QUALIFICATION_RESTORATION_INCOMPLETE",)
        assert outcome.qualification is None
        return
    assert outcome.disposition == "APPROVED"
    record = outcome.qualification
    assert record is not None
    assert record.status == "AT_RISK"
    assert record.authorization_id == granted.decision_event_id
    assert record.authorization_evidence is not None
    assert record.authorization_evidence.evidence_id == "synthetic-qualification-proof-4519"
    assert record.state_activity_evidence is not None
    assert record.state_activity_evidence.evidence_id == "synthetic-state-activity-restored"
    assert record.formal_node_dispositions == ()


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
    alert_nodes = ["2042-05-18T00:00:00Z", "2042-05-19T00:00:00Z"]
    alerted = diagnostic_alert(
        migrated_settings,
        "complete-requalification-alert",
        granted.decision_event_id,
        rule_version="synthetic-complete-requalification-alert-rule",
        planned_nodes=alert_nodes,
    )
    closure_command = alert_closure_command(
        migrated_settings,
        alerted.decision_event_id,
        alert_evidence_id="synthetic-complete-requalification-alert",
        rule_version="synthetic-complete-requalification-alert-rule",
        planned_nodes=alert_nodes,
    )
    closure_command["evidence"]["available_at"] = "2042-05-20T08:00:00Z"
    for index, observation in enumerate(closure_command["alert_closure"]["observations"]):
        observation["evidence"]["available_at"] = (
            "2042-05-18T08:00:00Z" if index == 0 else "2042-05-19T08:00:00Z"
        )
    closed = execute(
        migrated_settings,
        "complete-requalification-alert-closed",
        closure_command,
        observed_at="2042-05-20T10:00:00Z",
        knowledge_cutoff="2042-05-20T09:00:00Z",
    )
    revoke_command["previous_decision_id"] = closed.decision_event_id
    revoked = execute(
        migrated_settings,
        "complete-requalification-revoked",
        revoke_command,
        observed_at="2042-05-20T11:00:00Z",
        knowledge_cutoff="2042-05-20T10:30:00Z",
    )

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
            "prediction_frozen_at": "2042-05-23T00:00:00Z",
            "available_at": "2042-05-24T00:00:00Z",
            "matured_at": "2042-05-28T00:00:00Z",
            "outcome_digest": "3" * 64,
        },
        {
            "member_id": "synthetic-forward-member-b",
            "prediction_frozen_at": "2042-05-24T00:00:00Z",
            "available_at": "2042-05-25T00:00:00Z",
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
        requalification_application_id="synthetic-complete-requalification-application",
        requalification_population_id="synthetic-complete-historical-population",
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
        requalification_application_id="synthetic-complete-requalification-application",
        requalification_population_id="synthetic-complete-forward-population",
    )
    command["evidence"]["requalification_application_id"] = (
        "synthetic-complete-requalification-application"
    )
    historical_registration = {
        "application_id": "synthetic-complete-requalification-application",
        "population_kind": "HISTORICAL",
        "population_id": "synthetic-complete-historical-population",
        "registered_at": "2042-05-21T00:00:00Z",
        "locked_at": "2042-05-22T00:00:00Z",
        "frozen_version_digest": frozen_version_digest,
        "registered_member_ids": [item["member_id"] for item in historical_members],
        "required_gates": ["synthetic-history-gate"],
    }
    historical_registration["digest"] = canonical_digest(historical_registration)
    forward_registration = {
        "application_id": "synthetic-complete-requalification-application",
        "population_kind": "FORWARD",
        "population_id": "synthetic-complete-forward-population",
        "registered_at": "2042-05-21T00:00:00Z",
        "locked_at": "2042-05-22T00:00:00Z",
        "frozen_version_digest": frozen_version_digest,
        "registered_member_ids": [item["member_id"] for item in forward_members],
        "required_gates": ["synthetic-forward-gate"],
    }
    forward_registration["digest"] = canonical_digest(forward_registration)
    historical_evidence["requalification_registration_digest"] = historical_registration["digest"]
    forward_evidence["requalification_registration_digest"] = forward_registration["digest"]
    command["requalification"] = {
        "application_id": "synthetic-complete-requalification-application",
        "registered_at": "2042-05-21T00:00:00Z",
        "locked_at": "2042-05-22T00:00:00Z",
        "first_prediction_frozen_at": "2042-05-23T00:00:00Z",
        "frozen_version_digest": frozen_version_digest,
        "historical_evidence": historical_evidence,
        "forward_evidence": forward_evidence,
        "historical_registration": historical_registration,
        "forward_registration": forward_registration,
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

    silently_shrunk = deepcopy(command)
    silently_shrunk["requalification"]["forward_population"]["registered_member_ids"].pop()
    silently_shrunk["requalification"]["forward_population"]["members"].pop()
    silently_shrunk_execution = execute(
        migrated_settings,
        "complete-requalification-silently-shrunk",
        silently_shrunk,
        observed_at="2042-05-31T10:00:00Z",
        knowledge_cutoff="2042-05-31T09:00:00Z",
    )
    assert silently_shrunk_execution.report is not None
    silently_shrunk_outcome = silently_shrunk_execution.report.result.governance
    assert silently_shrunk_outcome is not None
    assert silently_shrunk_outcome.disposition == "DENIED"
    assert silently_shrunk_outcome.reasons == ("REQUALIFICATION_PROOF_INVALID",)

    exchanged_gates = deepcopy(command)
    historical_population = exchanged_gates["requalification"]["historical_population"]
    forward_population = exchanged_gates["requalification"]["forward_population"]
    historical_population["required_gates"], forward_population["required_gates"] = (
        forward_population["required_gates"],
        historical_population["required_gates"],
    )
    historical_population["gate_results"], forward_population["gate_results"] = (
        forward_population["gate_results"],
        historical_population["gate_results"],
    )
    exchanged_gates_execution = execute(
        migrated_settings,
        "complete-requalification-exchanged-gates",
        exchanged_gates,
        observed_at="2042-05-31T10:00:00Z",
        knowledge_cutoff="2042-05-31T09:00:00Z",
    )
    assert exchanged_gates_execution.report is not None
    exchanged_gates_outcome = exchanged_gates_execution.report.result.governance
    assert exchanged_gates_outcome is not None
    assert exchanged_gates_outcome.disposition == "DENIED"
    assert exchanged_gates_outcome.reasons == ("REQUALIFICATION_PROOF_INVALID",)

    relabeled_report = deepcopy(command)
    relabeled_report["requalification"]["historical_evidence"]["requalification_application_id"] = (
        "synthetic-old-application"
    )
    relabeled_report_execution = execute(
        migrated_settings,
        "complete-requalification-relabeled-report",
        relabeled_report,
        observed_at="2042-05-31T10:00:00Z",
        knowledge_cutoff="2042-05-31T09:00:00Z",
    )
    assert relabeled_report_execution.report is not None
    relabeled_report_outcome = relabeled_report_execution.report.result.governance
    assert relabeled_report_outcome is not None
    assert relabeled_report_outcome.disposition == "DENIED"
    assert relabeled_report_outcome.reasons == ("REQUALIFICATION_PROOF_INVALID",)

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
    assert len(completed_record.alert_closures) == 1
    assert completed_record.outstanding_alerts == ()
    assert revoked.report is not None
    assert revoked.report.result.governance is not None
    assert revoked.report.result.governance.qualification is not None
    assert revoked.report.result.governance.qualification.status == "REVOKED"


def test_revoked_substantive_version_cannot_grant_again_under_a_new_name(
    migrated_settings: Settings,
) -> None:
    granted = execute(
        migrated_settings,
        "renamed-revocation-grant",
        qualification_command(migrated_settings),
    )
    revoke = qualification_command(
        migrated_settings,
        action="REVOKE",
        previous=granted.decision_event_id,
    )
    revoke["evidence"].update(
        kind="ORIGINAL_BASIS_INVALID",
        evidence_id="synthetic-renamed-revocation",
    )
    revoked = execute(migrated_settings, "renamed-revocation-state", revoke)

    renamed = qualification_command(migrated_settings)
    renamed["version"]["version_id"] = "synthetic-renamed-version"
    renamed["evidence"]["version"] = deepcopy(renamed["version"])
    renamed["evidence"]["evidence_id"] = "synthetic-renamed-grant-attempt"
    repeated = execute(migrated_settings, "renamed-revocation-attempt", renamed)
    assert repeated.report is not None
    outcome = repeated.report.result.governance
    assert outcome is not None
    assert outcome.disposition == "DENIED"
    assert outcome.reasons == ("QUALIFICATION_REVISION_CONFLICT",)
    assert outcome.qualification is None
    assert revoked.report is not None
    assert revoked.report.result.governance is not None
    assert revoked.report.result.governance.qualification is not None
    assert revoked.report.result.governance.qualification.status == "REVOKED"


def test_formal_check_cannot_replace_the_registered_error_budget(
    migrated_settings: Settings,
) -> None:
    planned = ["2042-05-06T00:00:00Z", "2042-06-03T00:00:00Z"]
    original_check = {
        "sequence_id": "synthetic-budget-sequence",
        "index": 1,
        "registered_at": "2042-05-01T00:00:00Z",
        "scheduled_at": planned[0],
        "planned_nodes": planned,
        "required_gates": ["synthetic-budget-gate"],
        "error_budget_id": "synthetic-original-budget",
    }
    grant = qualification_command(migrated_settings)
    grant["evidence"]["formal_check"] = original_check
    granted = execute(migrated_settings, "formal-budget-grant", grant)

    check = qualification_command(
        migrated_settings,
        action="FORMAL_CHECK",
        previous=granted.decision_event_id,
    )
    check["evidence"].update(
        kind="FORMAL_CHECK",
        evidence_id="synthetic-replacement-budget-check",
        evaluation_end=planned[1],
        available_at="2042-06-04T08:00:00Z",
        expires_at="2043-03-01T00:00:00Z",
        formal_check={
            **original_check,
            "index": 2,
            "scheduled_at": planned[1],
            "error_budget_id": "synthetic-replacement-budget",
        },
        maturity_sufficient=True,
        gate_results=[{"gate_id": "synthetic-budget-gate", "passed": True}],
    )
    execution = execute(
        migrated_settings,
        "formal-budget-replacement",
        check,
        observed_at="2042-06-04T10:00:00Z",
        knowledge_cutoff="2042-06-04T09:00:00Z",
    )
    assert execution.report is not None
    outcome = execution.report.result.governance
    assert outcome is not None
    assert outcome.disposition == "DENIED"
    assert outcome.reasons == ("FORMAL_CHECK_NOT_ALLOWED",)


def test_alert_closure_is_independent_and_only_closes_the_target(
    migrated_settings: Settings,
) -> None:
    grant = execute(
        migrated_settings,
        "independent-alert-closure-grant",
        qualification_command(migrated_settings),
    )
    first_nodes = ["2042-05-24T00:00:00Z", "2042-05-31T00:00:00Z"]
    first = diagnostic_alert(
        migrated_settings,
        "independent-alert-one",
        grant.decision_event_id,
        rule_version="synthetic-diagnostic-rule-one",
        planned_nodes=first_nodes,
    )
    second = diagnostic_alert(
        migrated_settings,
        "independent-alert-two",
        first.decision_event_id,
        rule_version="synthetic-diagnostic-rule-two",
        planned_nodes=["2042-06-14T00:00:00Z", "2042-06-21T00:00:00Z"],
    )
    command = alert_closure_command(
        migrated_settings,
        second.decision_event_id,
        alert_evidence_id="synthetic-independent-alert-one",
        rule_version="synthetic-diagnostic-rule-one",
        planned_nodes=first_nodes,
    )

    for malformation in (
        "missing-node",
        "skipped-node",
        "recurrence",
        "insufficient",
        "unavailable",
        "late-evidence",
    ):
        malformed = deepcopy(command)
        observations = malformed["alert_closure"]["observations"]
        if malformation == "missing-node":
            observations.pop()
        elif malformation == "skipped-node":
            observations[0]["scheduled_at"] = first_nodes[1]
        elif malformation == "recurrence":
            observations[1]["status"] = "RECURRENT"
        elif malformation == "insufficient":
            observations[1]["status"] = "INSUFFICIENT"
        elif malformation == "unavailable":
            observations[1]["status"] = "UNAVAILABLE"
        else:
            observations[1]["evidence"]["available_at"] = "2042-06-01T09:01:00Z"
        malformed_execution = execute(
            migrated_settings,
            f"independent-alert-malformed-{malformation}",
            malformed,
            observed_at="2042-06-01T10:00:00Z",
            knowledge_cutoff="2042-06-01T09:00:00Z",
        )
        assert malformed_execution.report is not None
        malformed_outcome = malformed_execution.report.result.governance
        assert malformed_outcome is not None
        assert malformed_outcome.disposition == "DENIED"
        assert malformed_outcome.reasons == ("ALERT_CLOSURE_PROOF_INVALID",)

    closed = execute(
        migrated_settings,
        "independent-alert-closed",
        command,
        observed_at="2042-06-01T10:00:00Z",
        knowledge_cutoff="2042-06-01T09:00:00Z",
    )
    assert closed.report is not None
    outcome = closed.report.result.governance
    assert outcome is not None
    assert outcome.disposition == "APPROVED"
    record = outcome.qualification
    assert record is not None
    assert record.status == "AT_RISK"
    assert record.cause == "DIAGNOSTIC_ALERT"
    assert [item.evidence_id for item in record.alerts] == [
        "synthetic-independent-alert-one",
        "synthetic-independent-alert-two",
    ]
    assert [item.evidence_id for item in record.outstanding_alerts] == [
        "synthetic-independent-alert-two"
    ]
    assert len(record.alert_closures) == 1
    assert record.alert_closures[0].alert_evidence_id == ("synthetic-independent-alert-one")
    assert record.authorization_id == grant.decision_event_id
    assert record.authorization_evidence is not None
    assert record.authorization_evidence.evidence_id == "synthetic-qualification-proof-4519"
    assert record.formal_node_dispositions == ()


def test_alert_closure_never_restores_revoked_authorization(
    migrated_settings: Settings,
) -> None:
    grant = execute(
        migrated_settings,
        "revoked-alert-closure-grant",
        qualification_command(migrated_settings),
    )
    planned_nodes = ["2042-05-24T00:00:00Z", "2042-05-31T00:00:00Z"]
    alert = diagnostic_alert(
        migrated_settings,
        "revoked-alert",
        grant.decision_event_id,
        rule_version="synthetic-revoked-diagnostic-rule",
        planned_nodes=planned_nodes,
    )
    revoke = qualification_command(
        migrated_settings,
        action="REVOKE",
        previous=alert.decision_event_id,
    )
    revoke["evidence"].update(
        kind="ORIGINAL_BASIS_INVALID",
        evidence_id="synthetic-revoked-alert-basis-invalid",
    )
    revoked = execute(migrated_settings, "revoked-alert-state", revoke)
    assert revoked.report is not None
    revoked_record = revoked.report.result.governance
    assert revoked_record is not None
    assert revoked_record.qualification is not None
    terminated_at = revoked_record.qualification.authorization_terminated_at

    command = alert_closure_command(
        migrated_settings,
        revoked.decision_event_id,
        alert_evidence_id="synthetic-revoked-alert",
        rule_version="synthetic-revoked-diagnostic-rule",
        planned_nodes=planned_nodes,
    )
    closed = execute(
        migrated_settings,
        "revoked-alert-closed",
        command,
        observed_at="2042-06-01T10:00:00Z",
        knowledge_cutoff="2042-06-01T09:00:00Z",
    )
    assert closed.report is not None
    outcome = closed.report.result.governance
    assert outcome is not None
    assert outcome.disposition == "APPROVED"
    record = outcome.qualification
    assert record is not None
    assert record.status == "REVOKED"
    assert record.cause == "ORIGINAL_BASIS_INVALID"
    assert record.authorization_id == grant.decision_event_id
    assert record.authorization_terminated_at == terminated_at
    assert [item.evidence_id for item in record.alerts] == ["synthetic-revoked-alert"]
    assert record.outstanding_alerts == ()
    assert [item.evidence.evidence_id for item in record.restrictions] == [
        "synthetic-revoked-alert-basis-invalid"
    ]


def test_alert_disappearance_can_restart_after_an_interrupted_original_node(
    migrated_settings: Settings,
) -> None:
    grant = execute(
        migrated_settings,
        "restarted-alert-grant",
        qualification_command(migrated_settings),
    )
    planned_nodes = [
        "2042-05-18T00:00:00Z",
        "2042-05-25T00:00:00Z",
        "2042-06-01T00:00:00Z",
        "2042-06-08T00:00:00Z",
    ]
    alert = diagnostic_alert(
        migrated_settings,
        "restarted-alert",
        grant.decision_event_id,
        rule_version="synthetic-restarted-alert-rule",
        planned_nodes=planned_nodes,
    )
    command = alert_closure_command(
        migrated_settings,
        alert.decision_event_id,
        alert_evidence_id="synthetic-restarted-alert",
        rule_version="synthetic-restarted-alert-rule",
        planned_nodes=planned_nodes,
    )
    command["evidence"].update(
        evaluation_end=planned_nodes[-1],
        available_at="2042-06-09T08:00:00Z",
        expires_at="2043-03-01T00:00:00Z",
    )
    for index, observation in enumerate(command["alert_closure"]["observations"]):
        scheduled = observation["scheduled_at"]
        observation["evidence"].update(
            evaluation_end=scheduled,
            available_at=(datetime.fromisoformat(scheduled) + timedelta(hours=8)).isoformat(),
        )
        if index == 1:
            observation["status"] = "RECURRENT"
            observation["evidence"]["kind"] = "DIAGNOSTIC_RECURRENT"
    closed = execute(
        migrated_settings,
        "restarted-alert-closed",
        command,
        observed_at="2042-06-09T10:00:00Z",
        knowledge_cutoff="2042-06-09T09:00:00Z",
    )
    assert closed.report is not None
    outcome = closed.report.result.governance
    assert outcome is not None
    assert outcome.disposition == "APPROVED"
    record = outcome.qualification
    assert record is not None
    assert record.status == "VALID"
    assert [item.status for item in record.alert_closures[0].proof.observations] == [
        "CLEAR",
        "RECURRENT",
        "CLEAR",
        "CLEAR",
    ]


@pytest.mark.parametrize(
    ("resolution", "evidence_kind", "expected_status"),
    [
        ("PROVEN_ERRONEOUS", "ALERT_PROVEN_ERRONEOUS", "VALID"),
        ("TRANSFERRED", "ALERT_TRANSFERRED", "SUSPENDED"),
        ("ARCHIVED", "ALERT_ARCHIVED", "AT_RISK"),
    ],
)
def test_alert_non_disappearance_resolutions_require_independent_evidence(
    migrated_settings: Settings,
    resolution: str,
    evidence_kind: str,
    expected_status: str,
) -> None:
    grant = execute(
        migrated_settings,
        f"{resolution.lower()}-alert-grant",
        qualification_command(migrated_settings),
    )
    alert = diagnostic_alert(
        migrated_settings,
        f"{resolution.lower()}-alert",
        grant.decision_event_id,
        rule_version=f"synthetic-{resolution.lower()}-rule",
        planned_nodes=["2042-05-24T00:00:00Z", "2042-05-31T00:00:00Z"],
    )
    previous = alert.decision_event_id
    transferred_restriction_id = None
    if resolution == "TRANSFERRED":
        suspend = qualification_command(
            migrated_settings,
            action="SUSPEND",
            previous=previous,
        )
        transferred_restriction_id = "synthetic-transferred-integrity-restriction"
        suspend["evidence"].update(
            kind="REQUIRED_PREMISE_UNVERIFIABLE",
            evidence_id=transferred_restriction_id,
        )
        restricted = execute(migrated_settings, "transferred-alert-restricted", suspend)
        previous = restricted.decision_event_id

    command = qualification_command(
        migrated_settings,
        action="CLOSE_ALERT",
        previous=previous,
    )
    alert_evidence_id = f"synthetic-{resolution.lower()}-alert"
    command["evidence"].update(
        kind="ALERT_CLOSURE",
        evidence_id=f"synthetic-{resolution.lower()}-closure",
        evaluation_end="2042-05-18T00:00:00Z",
        available_at="2042-05-19T08:00:00Z",
        expires_at="2043-03-01T00:00:00Z",
        resolves_evidence_id=alert_evidence_id,
    )
    resolution_evidence = deepcopy(command["evidence"])
    resolution_evidence.update(
        kind=evidence_kind,
        evidence_id=f"synthetic-{resolution.lower()}-resolution-proof",
        reviewed_alert_digest="b" * 64,
    )
    command["alert_closure"] = {
        "contract_version": "1.0.0",
        "alert_evidence_id": alert_evidence_id,
        "resolution": resolution,
        "rule_version": f"synthetic-{resolution.lower()}-rule",
        "resolution_evidence": resolution_evidence,
    }
    if transferred_restriction_id is not None:
        command["alert_closure"]["transferred_restriction_evidence_id"] = transferred_restriction_id
    closed = execute(
        migrated_settings,
        f"{resolution.lower()}-alert-closed",
        command,
        observed_at="2042-05-19T10:00:00Z",
        knowledge_cutoff="2042-05-19T09:00:00Z",
    )
    assert closed.report is not None
    outcome = closed.report.result.governance
    assert outcome is not None
    assert outcome.disposition == "APPROVED"
    record = outcome.qualification
    assert record is not None
    assert record.status == expected_status
    assert record.alert_closures[-1].resolution == resolution
