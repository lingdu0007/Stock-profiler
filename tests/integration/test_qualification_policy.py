from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from test_scoped_qualification import GovernanceClock, case_payload, qualification_command

from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases import service
from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase


def test_missing_policy_fails_closed_without_synthesizing_authority(
    migrated_settings: Settings,
) -> None:
    command = qualification_command(migrated_settings)
    command["version"].pop("qualification_policy", None)
    command["evidence"]["version"].pop("qualification_policy", None)
    payload = case_payload(
        migrated_settings, "missing-explicit-policy", command, contract_version="5.0.0"
    )
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    outcome = execution.report.result.governance
    assert outcome is not None
    assert outcome.disposition == "DENIED"
    assert outcome.reasons == ("QUALIFICATION_POLICY_REQUIRED",)
    assert outcome.qualification is None
    assert (
        run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
        == execution.report
    )


@pytest.mark.parametrize(
    ("evaluation_end", "state_end", "expected"),
    [
        ("2063-06-01T00:00:00Z", "2063-02-01T00:00:00Z", "APPROVED"),
        ("2063-05-31T00:00:00Z", "2063-02-01T00:00:00Z", "DENIED"),
        ("2063-06-01T00:00:00Z", "2063-01-31T00:00:00Z", "DENIED"),
    ],
)
def test_explicit_synthetic_policy_controls_both_evidence_clocks(
    migrated_settings: Settings, evaluation_end: str, state_end: str, expected: str
) -> None:
    command = qualification_command(migrated_settings)
    command["evidence"].update(
        evaluation_end=evaluation_end,
        state_activity_end=state_end,
        available_at="2063-11-08T00:00:00Z",
        expires_at="2068-01-01T00:00:00Z",
    )
    payload = case_payload(
        migrated_settings, "explicit-policy-clocks", command, contract_version="5.0.0"
    )
    payload["knowledge_cutoff"] = "2063-11-09T09:00:00Z"
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2063-11-09T10:00:00Z")
    )
    assert execution.report is not None
    outcome = execution.report.result.governance
    assert outcome is not None and outcome.disposition == expected
    if expected == "APPROVED":
        assert outcome.qualification is not None
        assert outcome.qualification.version.model_dump(mode="json") == command["version"]
    else:
        assert outcome.qualification is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("evaluation_max_age_months", 0),
        ("evaluation_max_age_months", -1),
        ("evaluation_max_age_months", True),
        ("evaluation_max_age_months", "5"),
        ("state_activity_max_age_months", 0),
        ("contract_version", "unsupported"),
        ("generator_version", ""),
        ("generator_version", " "),
        ("policy_version", ""),
        ("seed", None),
        ("synthetic", False),
        ("synthetic", 1),
        ("require_state_activity", "false"),
    ],
)
def test_invalid_policy_is_rejected_at_the_frozen_journey_entry(
    migrated_settings: Settings, field: str, value: Any
) -> None:
    command = qualification_command(migrated_settings)
    command["version"]["qualification_policy"][field] = value
    command["evidence"]["version"] = deepcopy(command["version"])
    payload = case_payload(migrated_settings, "invalid-policy", command, contract_version="5.0.0")
    with pytest.raises(ValueError):
        run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 0,
        "decision_events": 0,
        "reports": 0,
    }
    assert len(ResultDelivery.from_settings(migrated_settings).audit_history()) == 1


@pytest.mark.parametrize("change", ["version", "evidence", "missing-state", "overflow"])
def test_policy_mismatch_or_unusable_clock_never_grants_authority(
    migrated_settings: Settings, change: str
) -> None:
    command = qualification_command(migrated_settings)
    if change == "version":
        command["version"]["qualification_policy"]["policy_version"] = "synthetic-other-policy"
        command["evidence"]["version"] = deepcopy(command["version"])
    elif change == "evidence":
        command["evidence"]["version"]["qualification_policy"]["seed"] = 82003
    elif change == "missing-state":
        command["evidence"].pop("state_activity_end")
    else:
        command["version"]["qualification_policy"]["evaluation_max_age_months"] = 10**20
        command["evidence"]["version"] = deepcopy(command["version"])
    execution = run_frozen_decision_case(
        migrated_settings,
        case_payload(migrated_settings, "unusable-policy", command, contract_version="5.0.0"),
        clock=GovernanceClock(),
    )
    assert execution.report is not None and execution.report.result.governance is not None
    outcome = execution.report.result.governance
    assert outcome.disposition == "DENIED"
    assert outcome.qualification is None


@pytest.mark.parametrize("field", ["synthetic", "qualification_scope"])
def test_synthetic_policy_cannot_authorize_a_personal_case(
    migrated_settings: Settings, field: str
) -> None:
    payload = case_payload(
        migrated_settings,
        "personal-policy-refused",
        qualification_command(migrated_settings),
        contract_version="5.0.0",
    )
    payload[field] = False if field == "synthetic" else "PERSONAL_ACTION"
    with pytest.raises(ValueError):
        run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 0,
        "decision_events": 0,
        "reports": 0,
    }
    assert len(ResultDelivery.from_settings(migrated_settings).audit_history()) == 1


@pytest.mark.parametrize("action", ["GRANT", "ALERT", "RESTORE", "REQUALIFY"])
def test_policy_identity_cannot_be_redefined_to_inherit_or_replace_authority(
    migrated_settings: Settings, action: str
) -> None:
    original_payload = case_payload(
        migrated_settings,
        "policy-original",
        qualification_command(migrated_settings),
        contract_version="5.0.0",
    )
    original = run_frozen_decision_case(
        migrated_settings, original_payload, clock=GovernanceClock()
    )
    command = qualification_command(migrated_settings)
    command["action"] = action
    command["previous_decision_id"] = None if action == "GRANT" else original.decision_event_id
    command["evidence"]["kind"] = {
        "GRANT": "QUALIFICATION_PASS",
        "ALERT": "DIAGNOSTIC_ALERT",
        "RESTORE": "RESTORATION_DECISION",
        "REQUALIFY": "REQUALIFICATION_PASS",
    }[action]
    command["version"]["qualification_policy"]["evaluation_max_age_months"] = 7
    command["evidence"]["version"] = deepcopy(command["version"])
    attempted = run_frozen_decision_case(
        migrated_settings,
        case_payload(migrated_settings, "policy-redefined", command, contract_version="5.0.0"),
        clock=GovernanceClock(),
    )
    assert attempted.report is not None and attempted.report.result.governance is not None
    assert attempted.report.result.governance.disposition == "DENIED"
    assert attempted.report.result.governance.qualification is None
    assert (
        run_frozen_decision_case(
            migrated_settings, original_payload, clock=GovernanceClock()
        ).report
        == original.report
    )


def test_replay_and_correction_never_replace_the_original_policy(
    migrated_settings: Settings,
) -> None:
    payload = case_payload(
        migrated_settings,
        "policy-immutable",
        qualification_command(migrated_settings),
        contract_version="5.0.0",
    )
    original = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    runtime = initialize_runtime_storage(migrated_settings)
    original_run = asyncio.run(runtime.run_store.get_run(original.framework_run_id))
    original_checkpoints = asyncio.run(runtime.run_store.get_checkpoints(original.framework_run_id))
    changed = deepcopy(payload)
    changed["governance"]["version"]["qualification_policy"]["seed"] = 82003
    changed["governance"]["evidence"]["version"] = deepcopy(changed["governance"]["version"])
    replay = run_frozen_decision_case(migrated_settings, changed, clock=GovernanceClock())
    assert replay.report == original.report
    assert replay.framework_run_id == original.framework_run_id
    ledger = DecisionLedger.from_settings(migrated_settings, clock=GovernanceClock())
    corrected = service.correct_default_frozen_decision_case(
        FrozenDecisionCase.model_validate(changed), ledger, payload["business_identity"]
    )
    with ledger.serialize_case_execution() as connection:
        original_fact = ledger.get_original_decision_event(original.business_object_id, connection)
        correction = ledger.get_correction_event(original.decision_event_id, connection)
    assert original_fact is not None and correction is not None
    assert original_fact.case.governance == correction.case.governance
    assert correction.case.governance == FrozenDecisionCase.model_validate(payload).governance
    assert correction.corrects_event_id == original.decision_event_id
    assert corrected.report is not None
    assert asyncio.run(runtime.run_store.get_run(original.framework_run_id)) == original_run
    assert (
        asyncio.run(runtime.run_store.get_checkpoints(original.framework_run_id))
        == original_checkpoints
    )
    changed_case = FrozenDecisionCase.model_validate(changed)
    assert asyncio.run(runtime.run_store.get_run(changed_case.framework_run_id)) is None


@pytest.mark.parametrize("initial_denial", ["expired-evidence", "business-rejected"])
def test_denied_frozen_policy_cannot_be_redefined_but_original_policy_remains_usable(
    migrated_settings: Settings, initial_denial: str
) -> None:
    command = qualification_command(migrated_settings)
    command["evidence"].update(
        evaluation_end="2063-05-31T00:00:00Z",
        state_activity_end="2063-02-01T00:00:00Z",
        available_at="2063-11-08T00:00:00Z",
        expires_at="2068-01-01T00:00:00Z",
    )
    payload = case_payload(
        migrated_settings, "policy-denial-origin", command, contract_version="5.0.0"
    )
    payload["knowledge_cutoff"] = "2063-11-09T09:00:00Z"
    if initial_denial == "business-rejected":
        rejected = json.loads(
            (
                Path(__file__).parents[1] / "fixtures/synthetic/result-families/input-rejected.json"
            ).read_text()
        )
        payload.update(
            input=rejected["input"], expected_external_result=rejected["expected_external_result"]
        )
    original = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2063-11-09T10:00:00Z")
    )
    assert original.report is not None and original.report.result.governance is not None
    assert original.report.result.governance.disposition == "DENIED"
    assert original.report.result.governance.qualification is None
    changed = deepcopy(command)
    changed["version"]["qualification_policy"]["evaluation_max_age_months"] = 7
    changed["evidence"]["version"] = deepcopy(changed["version"])
    attempted = case_payload(
        migrated_settings, "policy-denial-redefined", changed, contract_version="5.0.0"
    )
    attempted["knowledge_cutoff"] = payload["knowledge_cutoff"]
    refused = run_frozen_decision_case(
        migrated_settings, attempted, clock=GovernanceClock("2063-11-09T10:01:00Z")
    )
    assert refused.report is not None and refused.report.result.governance is not None
    assert refused.report.result.governance.disposition == "DENIED"
    assert refused.report.result.governance.reasons == ("QUALIFICATION_POLICY_IDENTITY_CONFLICT",)
    command["evidence"]["evaluation_end"] = "2063-06-01T00:00:00Z"
    refreshed = case_payload(
        migrated_settings, "policy-original-fresh-evidence", command, contract_version="5.0.0"
    )
    refreshed["knowledge_cutoff"] = payload["knowledge_cutoff"]
    granted = run_frozen_decision_case(
        migrated_settings, refreshed, clock=GovernanceClock("2063-11-09T10:02:00Z")
    )
    assert granted.report is not None and granted.report.result.governance is not None
    assert granted.report.result.governance.disposition == "APPROVED"
    assert (
        run_frozen_decision_case(
            migrated_settings, payload, clock=GovernanceClock("2063-11-09T10:03:00Z")
        ).report
        == original.report
    )


@pytest.mark.parametrize("visibility", ["USER", "SHADOW"])
def test_denied_policy_bindings_do_not_cross_visibility(
    migrated_settings: Settings, visibility: str
) -> None:
    command = qualification_command(migrated_settings)
    command["evidence"].update(
        evaluation_end="2063-05-31T00:00:00Z",
        state_activity_end="2063-02-01T00:00:00Z",
        available_at="2063-11-08T00:00:00Z",
        expires_at="2068-01-01T00:00:00Z",
    )
    source_payload = case_payload(
        migrated_settings, "isolated-policy-binding", command, contract_version="5.0.0"
    )
    source_payload["access_scope"]["visibility"] = visibility
    source_payload["knowledge_cutoff"] = "2063-11-09T09:00:00Z"
    source = run_frozen_decision_case(
        migrated_settings, source_payload, clock=GovernanceClock("2063-11-09T10:00:00Z")
    )
    command["version"]["qualification_policy"]["evaluation_max_age_months"] = 7
    command["evidence"]["version"] = deepcopy(command["version"])
    target_payload = case_payload(
        migrated_settings, "other-visibility-policy", command, contract_version="5.0.0"
    )
    target_payload["access_scope"]["visibility"] = "SHADOW" if visibility == "USER" else "USER"
    target_payload["knowledge_cutoff"] = source_payload["knowledge_cutoff"]
    target = run_frozen_decision_case(
        migrated_settings, target_payload, clock=GovernanceClock("2063-11-09T10:01:00Z")
    )
    ledger = DecisionLedger.from_settings(migrated_settings)
    with ledger.serialize_case_execution() as connection:
        first = ledger.get_original_decision_event(source.business_object_id, connection)
        second = ledger.get_original_decision_event(target.business_object_id, connection)
    assert first is not None and first.result.governance is not None
    assert first.result.governance.disposition == "DENIED"
    assert first.result.governance.policy_binding is not None
    assert first.result.governance.qualification is None
    assert second is not None and second.result.governance is not None
    assert second.result.governance.disposition == "APPROVED"
    assert second.result.governance.qualification is not None
    assert second.result.governance.qualification.authorization_id == target.decision_event_id
    assert (source.report is None) == (visibility == "SHADOW")
    assert (target.report is None) == (visibility == "USER")
