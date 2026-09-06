from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.decision_cases import (
    get_formal_report,
    run_frozen_decision_case,
)
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases import service
from stock_profiler.modules.decision_cases.domain import (
    FrozenDecisionCase,
    load_frozen_decision_case,
)
from stock_profiler.modules.delivery.access import AccessPrincipal


class GovernanceClock:
    def __init__(self, value: str = "2042-05-17T16:01:00Z") -> None:
        self.current = datetime.fromisoformat(value).astimezone(UTC)

    def now(self) -> datetime:
        return self.current


def scope() -> dict[str, Any]:
    return {
        "capability": "synthetic-review",
        "purpose": "synthetic-statistical-use",
        "evidence_level": "D0",
        "user_id": "stock-profiler-single-user",
        "account_ids": ["synthetic-account-4017"],
        "account_type": "SIMULATED_CASH",
        "source": "fictional-ledger-alpha",
        "market_state": "synthetic-steady",
        "board": "synthetic-board",
        "target": "synthetic-review-target",
    }


def version(settings: Settings, name: str = "synthetic-version-one") -> dict[str, Any]:
    return {
        "version_id": name,
        "policy_version": name,
        "implementation": load_frozen_decision_case(settings).version_bundle.model_dump(
            mode="json"
        ),
    }


def qualification_command(
    settings: Settings, *, action: str = "GRANT", previous: str | None = None
) -> dict[str, Any]:
    return {
        "operation": "QUALIFICATION",
        "action": action,
        "scope": scope(),
        "version": version(settings),
        "previous_decision_id": previous,
        "evidence": {
            "evidence_id": "synthetic-qualification-proof-4519",
            "synthetic": True,
            "generator_version": "scoped-governance/1",
            "seed": 4519,
            "version": version(settings),
            "scope": scope(),
            "kind": "QUALIFICATION_PASS",
            "digest": "b" * 64,
            "evaluation_end": "2042-05-01T00:00:00Z",
            "available_at": "2042-05-17T15:30:00Z",
            "expires_at": "2043-05-31T23:59:59Z",
        },
    }


def case_payload(settings: Settings, identity: str, command: dict[str, Any]) -> dict[str, Any]:
    payload = load_frozen_decision_case(settings).model_dump(mode="json")
    payload["business_identity"] = f"synthetic:governance:{identity}"
    payload["case_id"] = f"d0-governance-{identity}"
    payload["version_bundle"].update(
        case_contract_version="4.0.0",
        host_contract_version="4.0.0",
        report_projection_contract_version="4.0.0",
        agent_definition_version="2.0.0",
    )
    payload["agent_definition"]["version"] = "2.0.0"
    payload["access_scope"] = {
        "contract_version": "1.0.0",
        "user_id": "stock-profiler-single-user",
        "account_ids": ["synthetic-account-4017"],
        "visibility": "USER",
    }
    payload["governance"] = command
    return payload


def test_qualification_is_saved_with_exact_scope_and_authorization_evidence(
    migrated_settings: Settings,
) -> None:
    payload = case_payload(migrated_settings, "grant", qualification_command(migrated_settings))
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    result = execution.report.result.model_dump(mode="json")["governance"]
    assert result["disposition"] == "APPROVED"
    assert result["qualification"]["status"] == "VALID"
    assert result["qualification"]["scope"] == scope()
    assert result["qualification"]["version"] == version(migrated_settings)
    assert result["qualification"]["authorization_id"] == execution.decision_event_id
    assert result["qualification"]["authorization_evidence"]["evidence_id"] == (
        "synthetic-qualification-proof-4519"
    )
    assert result["qualification"]["cause"] == "QUALIFICATION_PASS"
    assert (
        run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
        == execution.report
    )
    assert (
        get_formal_report(
            execution.report_version_id,
            migrated_settings,
            principal=AccessPrincipal(
                user_id="stock-profiler-single-user",
                account_ids=("synthetic-account-4017",),
                permissions=("REPORT_READ",),
            ),
        )
        == execution.report
    )


def test_repeated_diagnostic_alerts_retain_the_original_authorization(
    migrated_settings: Settings,
) -> None:
    grant = run_frozen_decision_case(
        migrated_settings,
        case_payload(migrated_settings, "grant", qualification_command(migrated_settings)),
        clock=GovernanceClock(),
    )
    previous = grant.decision_event_id
    for identity in ("alert-one", "alert-two"):
        command = qualification_command(migrated_settings, action="ALERT", previous=previous)
        command["evidence"]["kind"] = "DIAGNOSTIC_ALERT"
        command["evidence"]["evidence_id"] = f"synthetic-{identity}"
        alert = run_frozen_decision_case(
            migrated_settings,
            case_payload(migrated_settings, identity, command),
            clock=GovernanceClock(),
        )
        assert alert.report is not None
        record = alert.report.result.model_dump(mode="json")["governance"]["qualification"]
        assert record["status"] == "AT_RISK"
        assert record["authorization_id"] == grant.decision_event_id
        assert record["authorization_evidence"]["kind"] == "QUALIFICATION_PASS"
        assert record["version"] == version(migrated_settings)
        previous = alert.decision_event_id
    assert [item["evidence_id"] for item in record["alerts"]] == [
        "synthetic-alert-one",
        "synthetic-alert-two",
    ]
    assert record["previous_decision_id"] != grant.decision_event_id
    assert grant.report is not None
    assert grant.report.result.model_dump(mode="json")["governance"]["qualification"]["status"] == (
        "VALID"
    )


@pytest.mark.parametrize(
    ("fixture", "phase", "status", "gate", "gate_status"),
    [
        ("input-rejected", "BUSINESS_DECISION", "REJECTED", "DECISION_ACCEPTED", "FAILED"),
        ("result-abstained", "BUSINESS_DECISION", "ABSTAINED", "ABSTENTION_RECORDED", "PASSED"),
        ("result-failed", "BUSINESS_DECISION", "FAILED", "DECISION_COMPLETED", "FAILED"),
        (
            "result-pending",
            "ADJUDICATION_LIFECYCLE",
            "PENDING",
            "ADJUDICATION_COMPLETED",
            "UNKNOWN",
        ),
        ("result-expired", "VALIDITY_LIFECYCLE", "EXPIRED", "VALIDITY_WINDOW", "FAILED"),
        (
            "result-execution-blocked",
            "EXECUTION_LIFECYCLE",
            "EXECUTION_BLOCKED",
            "EXECUTION_AVAILABLE",
            "FAILED",
        ),
        ("result-unknown", "ADJUDICATION_LIFECYCLE", "UNKNOWN", "DECISION_DETERMINED", "UNKNOWN"),
    ],
)
def test_qualification_cannot_replace_a_non_success_business_outcome(
    migrated_settings: Settings, fixture: str, phase: str, status: str, gate: str, gate_status: str
) -> None:
    source = json.loads(
        (
            Path(__file__).parents[1]
            / "fixtures"
            / "synthetic"
            / "result-families"
            / f"{fixture}.json"
        ).read_text(encoding="utf-8")
    )
    payload = case_payload(migrated_settings, fixture, qualification_command(migrated_settings))
    payload["input"] = source["input"]
    payload["expected_external_result"] = source["expected_external_result"]
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    outcome = next(stage for stage in execution.stage_results if stage.phase == phase)
    assert outcome.status == status
    assert execution.business_result_status == (status if phase == "BUSINESS_DECISION" else None)
    if phase != "BUSINESS_DECISION":
        assert execution.business_lifecycle is not None
        assert execution.business_lifecycle.phase == phase
        assert execution.business_lifecycle.status == status
    assert [(item.gate_id, item.status) for item in outcome.gate_results] == [
        ("OUTPUT_CONTRACT", "PASSED"),
        (gate, gate_status),
    ]
    assert outcome.reasons == tuple(source["expected_external_result"]["key_reasons"])
    assert execution.business_commit_status == "COMMITTED"
    assert execution.publication_status == "PUBLISHED"
    assert execution.report is not None
    assert (
        execution.report.result.outcome_code == source["expected_external_result"]["outcome_code"]
    )
    governance = execution.report.result.governance
    assert governance is not None
    assert governance.disposition == "DENIED"
    assert governance.qualification is None
    assert governance.reasons == ("BUSINESS_PREREQUISITE_NOT_MET",)
    qualification_stage = next(
        stage for stage in execution.stage_results if stage.phase == "QUALIFICATION"
    )
    assert qualification_stage.status == "REJECTED"
    assert qualification_stage.reasons == governance.reasons
    assert (
        run_frozen_decision_case(
            migrated_settings, payload, clock=GovernanceClock("2042-05-18T16:01:00Z")
        ).report
        == execution.report
    )


@pytest.mark.parametrize(
    ("available_at", "disposition"),
    [
        ("2042-05-17T15:59:59Z", "APPROVED"),
        ("2042-05-17T16:00:00Z", "APPROVED"),
        ("2042-05-17T16:00:00.000001Z", "DENIED"),
        ("2042-05-17T19:00:00.000001+03:00", "DENIED"),
    ],
)
def test_qualification_uses_the_frozen_cutoff_even_when_execution_is_delayed(
    migrated_settings: Settings, available_at: str, disposition: str
) -> None:
    command = qualification_command(migrated_settings)
    command["evidence"]["available_at"] = available_at
    payload = case_payload(migrated_settings, "cutoff", command)
    assert payload["knowledge_cutoff"] == "2042-05-17T16:00:00Z"
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-18T16:01:00Z")
    )
    assert execution.report is not None
    assert execution.business_result_status == "SUCCEEDED"
    governance = execution.report.result.governance
    assert governance is not None
    assert governance.disposition == disposition
    qualification_stage = next(
        stage for stage in execution.stage_results if stage.phase == "QUALIFICATION"
    )
    if disposition == "DENIED":
        assert governance.qualification is None
        assert governance.reasons == ("QUALIFICATION_EVIDENCE_AFTER_CUTOFF",)
        assert qualification_stage.status == "REJECTED"
    else:
        assert governance.qualification is not None
        assert governance.qualification.authorization_id == execution.decision_event_id
        assert qualification_stage.status == "SUCCEEDED"
    assert (
        run_frozen_decision_case(
            migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
        ).report
        == execution.report
    )


@pytest.mark.parametrize("visibility", ["USER", "SHADOW"])
def test_governance_history_filters_frozen_visibility_before_adjudication(
    migrated_settings: Settings, visibility: str
) -> None:
    """Auxiliary persistence check after one real Run, not a cross-Run integration proof."""
    payload = case_payload(migrated_settings, "history", qualification_command(migrated_settings))
    payload["access_scope"]["visibility"] = visibility
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    case = FrozenDecisionCase.model_validate(payload)
    assert case.access_scope is not None
    ledger = DecisionLedger.from_settings(migrated_settings)
    with ledger.serialize_case_execution() as connection:
        fact = ledger.get_original_decision_event(execution.business_object_id, connection)
        assert fact is not None
        assert fact.case.access_scope == case.access_scope
        assert fact.result.governance is not None
        assert fact.result.governance.disposition == "APPROVED"
        assert ledger.governance_history(connection, case.access_scope) == (fact.result.governance,)
        other_scope = case.access_scope.model_copy(
            update={"visibility": "SHADOW" if visibility == "USER" else "USER"}
        )
        assert ledger.governance_history(connection, other_scope) == ()
    assert (execution.report is None) == (visibility == "SHADOW")


@pytest.mark.parametrize("visibility", ["USER", "SHADOW"])
@pytest.mark.parametrize("action", ["GRANT", "ALERT"])
def test_cross_visibility_governance_journey_never_borrows_authority(
    migrated_settings: Settings, visibility: str, action: str
) -> None:
    """Real integration gate; the pinned multi-Run Context collision still blocks this journey."""
    source_payload = case_payload(
        migrated_settings, "source", qualification_command(migrated_settings)
    )
    source_payload["access_scope"]["visibility"] = visibility
    source = run_frozen_decision_case(migrated_settings, source_payload, clock=GovernanceClock())
    command = qualification_command(
        migrated_settings,
        action=action,
        previous=source.decision_event_id if action == "ALERT" else None,
    )
    if action == "ALERT":
        command["evidence"]["kind"] = "DIAGNOSTIC_ALERT"
    target_payload = case_payload(migrated_settings, "target", command)
    target_payload["access_scope"]["visibility"] = "SHADOW" if visibility == "USER" else "USER"
    target = run_frozen_decision_case(migrated_settings, target_payload, clock=GovernanceClock())
    ledger = DecisionLedger.from_settings(migrated_settings)
    with ledger.serialize_case_execution() as connection:
        fact = ledger.get_original_decision_event(target.business_object_id, connection)
        assert fact is not None
        governance = fact.result.governance
        assert governance is not None
        if action == "ALERT":
            assert governance.disposition == "DENIED"
            assert governance.qualification is None
        else:
            assert governance.disposition == "APPROVED"
            assert governance.qualification is not None
            assert governance.qualification.authorization_id == target.decision_event_id
            assert governance.qualification.authorization_id != source.decision_event_id
    assert (
        run_frozen_decision_case(migrated_settings, source_payload, clock=GovernanceClock()).report
        == source.report
    )


@pytest.mark.parametrize("correction_source_sha", ["a" * 40, "b" * 40])
@pytest.mark.parametrize(
    ("available_at", "disposition"),
    [
        ("2042-05-17T15:30:00Z", "APPROVED"),
        ("2042-05-17T16:00:30Z", "DENIED"),
    ],
)
def test_governed_correction_preserves_original_contract_authority_and_run(
    migrated_settings: Settings, correction_source_sha: str, available_at: str, disposition: str
) -> None:
    command = qualification_command(migrated_settings)
    command["evidence"]["available_at"] = available_at
    payload = case_payload(migrated_settings, "corrected", command)
    original = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert original.report is not None
    assert original.report.result.governance is not None
    assert original.report.result.governance.disposition == disposition
    original_json = original.report.model_dump_json()
    case = FrozenDecisionCase.model_validate(payload)
    assert case.access_scope is not None
    runtime = initialize_runtime_storage(migrated_settings)
    run_before = asyncio.run(runtime.run_store.get_run(original.framework_run_id))
    assert run_before is not None
    ledger = DecisionLedger.from_settings(
        migrated_settings, clock=GovernanceClock("2042-05-17T16:02:00Z")
    )
    with ledger.serialize_case_execution() as connection:
        event_before = ledger.get_original_decision_event(original.business_object_id, connection)
        history_before = ledger.governance_history(connection, case.access_scope)
    current_payload = case.model_dump(mode="json")
    current_payload["version_bundle"]["host_source_sha"] = correction_source_sha
    current_case = FrozenDecisionCase.model_validate(current_payload)
    correction = service.correct_default_frozen_decision_case(
        current_case, ledger, case.business_identity
    )
    assert correction.report.version_bundle.report_projection_contract_version == "4.0.0"
    assert correction.report.version_bundle.case_contract_version == "4.0.0"
    assert correction.report.version_bundle.host_source_sha == correction_source_sha
    assert correction.report.framework_run_id == original.framework_run_id
    assert correction.report.corrects_event_id == original.decision_event_id
    assert correction.report.event_id != original.decision_event_id
    assert correction.report.result.governance == original.report.result.governance
    assert correction.report.result.correction_evidence is not None
    assert correction.report.knowledge_cutoff == original.report.knowledge_cutoff
    assert correction.report.result.correction_evidence.knowledge_cutoff == ("2042-05-17T16:01:50Z")
    principal = AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017",),
        permissions=("REPORT_READ",),
    )
    assert (
        get_formal_report(
            correction.report.report_version_id, migrated_settings, principal=principal
        )
        == correction.report
    )
    saved_original = get_formal_report(
        original.report_version_id, migrated_settings, principal=principal
    )
    assert saved_original is not None
    assert saved_original.model_dump_json() == original_json
    assert (
        service.correct_default_frozen_decision_case(current_case, ledger, case.business_identity)
        == correction
    )
    assert (
        run_frozen_decision_case(
            migrated_settings.model_copy(update={"source_sha": correction_source_sha}),
            current_payload,
            clock=GovernanceClock("2042-05-18T16:02:00Z"),
        ).report
        == original.report
    )
    with ledger.serialize_case_execution() as connection:
        assert (
            ledger.get_original_decision_event(original.business_object_id, connection)
            == event_before
        )
        assert ledger.governance_history(connection, case.access_scope) == history_before
    assert asyncio.run(runtime.run_store.get_run(original.framework_run_id)) == run_before
