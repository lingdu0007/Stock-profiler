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
