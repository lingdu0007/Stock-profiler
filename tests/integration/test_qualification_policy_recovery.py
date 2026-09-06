from __future__ import annotations

import asyncio
import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import select
from test_runtime_upgrade import OLD_RELEASE
from test_runtime_upgrade import historical_python as historical_python
from test_scoped_qualification import GovernanceClock, case_payload, qualification_command
from test_version_handoffs import task_node

from stock_profiler.adapters.m_agent.frozen_decision_case import execute_frozen_decision_case
from stock_profiler.adapters.persistence.decision_ledger import (
    DECISION_CASE_BUSINESS_OBJECTS,
    DECISION_EVENTS,
    FORMAL_REPORTS,
    DecisionLedger,
)
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.decision_cases import (
    get_formal_report,
    replay_default_frozen_decision_case,
    run_frozen_decision_case,
)
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases import service
from stock_profiler.modules.decision_cases.domain import (
    FrozenDecisionCase,
    GateResult,
    StagePhase,
    StageResult,
)
from stock_profiler.modules.delivery.access import AccessPrincipal
from stock_profiler.modules.qualification.contracts import (
    GovernanceOutcome,
    QualificationCommand,
    QualificationRecord,
)


@pytest.mark.parametrize("mode", ["completed", "checkpoint"])
@pytest.mark.parametrize("explicit_policy", [False, True])
@pytest.mark.parametrize("account_shape", ["original", "reversed", "duplicate"])
def test_original_governed_run_recovers_without_backfilling_or_replacing_policy(
    migrated_settings: Settings,
    historical_python: Path,
    tmp_path: Path,
    mode: str,
    explicit_policy: bool,
    account_shape: str,
) -> None:
    command = qualification_command(migrated_settings)
    accounts = command["scope"]["account_ids"].copy()
    if account_shape == "reversed":
        accounts.append("synthetic-account-orbit")
        command["scope"]["account_ids"] = list(reversed(accounts))
    elif account_shape == "duplicate":
        command["scope"]["account_ids"] = accounts * 2
    command["evidence"]["scope"] = deepcopy(command["scope"])
    if not explicit_policy:
        command["version"].pop("qualification_policy")
        command["evidence"]["version"].pop("qualification_policy")
    payload = case_payload(migrated_settings, "historical-policy", command)
    payload["access_scope"]["account_ids"] = accounts
    payload["version_bundle"].update(OLD_RELEASE)
    case = FrozenDecisionCase.model_validate(payload)
    assert case.model_dump(mode="json")["governance"] == payload["governance"]
    envelope = tmp_path / "original-synthetic-case.json"
    envelope.write_text(
        json.dumps(
            {
                "case": payload,
                "run_id": case.framework_run_id,
                "fingerprint": case.frozen_input_fingerprint,
            }
        )
    )
    generated = subprocess.run(
        [
            str(historical_python),
            "-I",
            str(Path(__file__).parents[1] / "support/legacy_runtime_case.py"),
            str(envelope),
            str(migrated_settings.resolved_m_agent_run_store_path),
            mode,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    original = json.loads(generated.stdout)
    assert original["run"]["status"] == ("RUNNING" if mode == "checkpoint" else "SUCCEEDED")
    assert len(original["checkpoints"]) == 2
    changed = deepcopy(payload)
    replacement = qualification_command(migrated_settings)["version"]["qualification_policy"]
    replacement["seed"] = 82003
    changed["governance"]["version"]["qualification_policy"] = replacement
    changed["governance"]["evidence"]["version"] = deepcopy(changed["governance"]["version"])
    changed_case = FrozenDecisionCase.model_validate(changed)
    upgraded = migrated_settings.model_copy(update={"source_sha": "b" * 40})
    with pytest.raises(ValueError, match="original"):
        replay_default_frozen_decision_case(
            upgraded,
            case.business_identity,
            recovery_case=changed_case,
            clock=GovernanceClock(),
        )
    ledger = DecisionLedger.from_settings(upgraded, clock=GovernanceClock())
    assert ledger.counts() == {"business_objects": 0, "decision_events": 0, "reports": 0}

    execution = replay_default_frozen_decision_case(
        upgraded,
        case.business_identity,
        recovery_case=case,
        clock=GovernanceClock(),
    )
    assert execution.framework_run_id == original["run"]["run_id"] == case.framework_run_id
    assert execution.framework_run_status == "SUCCEEDED"
    assert execution.report is not None
    assert execution.report.version_bundle == case.version_bundle
    outcome = execution.report.result.governance
    assert outcome is not None
    if explicit_policy:
        assert outcome.disposition == "APPROVED" and outcome.qualification is not None
        assert outcome.qualification.version.model_dump(mode="json") == command["version"]
    else:
        assert outcome.disposition == "DENIED" and outcome.qualification is None
        assert outcome.reasons == ("QUALIFICATION_POLICY_REQUIRED",)

    runtime = initialize_runtime_storage(upgraded)
    recovered = asyncio.run(runtime.run_store.get_run(case.framework_run_id))
    assert recovered is not None
    assert recovered.input == original["run"]["input"]
    assert recovered.model_dump(mode="json")["snapshot"] == original["run"]["snapshot"]
    assert [
        item.model_dump(mode="json")
        for item in asyncio.run(runtime.run_store.get_checkpoints(case.framework_run_id))
    ] == original["checkpoints"]
    assert asyncio.run(runtime.run_store.get_run(changed_case.framework_run_id)) is None
    with ledger.serialize_case_execution() as connection:
        fact_before = ledger.get_original_decision_event(execution.business_object_id, connection)
    assert fact_before is not None and fact_before.case == case
    assert (
        replay_default_frozen_decision_case(
            upgraded,
            case.business_identity,
            recovery_case=case,
            clock=GovernanceClock(),
        ).report
        == execution.report
    )
    assert (
        run_frozen_decision_case(
            migrated_settings,
            payload,
            clock=GovernanceClock(),
        ).report
        == execution.report
    )
    corrected = service.correct_default_frozen_decision_case(
        changed_case, ledger, case.business_identity
    )
    with ledger.serialize_case_execution() as connection:
        assert (
            ledger.get_original_decision_event(execution.business_object_id, connection)
            == fact_before
        )
        correction = ledger.get_correction_event(execution.decision_event_id, connection)
    assert correction is not None and correction.case.governance == case.governance
    assert corrected.report.version_bundle == case.version_bundle
    assert case.access_scope is not None
    assert (
        get_formal_report(
            execution.report_version_id,
            upgraded,
            principal=AccessPrincipal(
                user_id=case.access_scope.user_id,
                account_ids=case.access_scope.account_ids,
                permissions=("REPORT_READ",),
            ),
        )
        == execution.report
    )
    assert asyncio.run(runtime.run_store.get_run(case.framework_run_id)) == recovered
    assert json.loads(envelope.read_text())["case"] == payload
    if explicit_policy:
        canonical_command = deepcopy(command)
        canonical_command["scope"]["account_ids"] = accounts
        canonical_command["evidence"]["scope"] = deepcopy(canonical_command["scope"])
        canonical_command["version"]["qualification_policy"]["seed"] = 82003
        canonical_command["evidence"]["version"] = deepcopy(canonical_command["version"])
        attempted = case_payload(upgraded, "historical-account-policy-alias", canonical_command)
        attempted["access_scope"]["account_ids"] = accounts
        denied = run_frozen_decision_case(upgraded, attempted, clock=GovernanceClock())
        assert denied.report is not None and denied.report.result.governance is not None
        assert denied.report.result.governance.reasons == (
            "QUALIFICATION_POLICY_IDENTITY_CONFLICT",
        )
        canonical_command["version"] = deepcopy(command["version"])
        canonical_command["evidence"]["version"] = deepcopy(command["version"])
        attempted = case_payload(upgraded, "historical-account-revision-alias", canonical_command)
        attempted["access_scope"]["account_ids"] = accounts
        denied = run_frozen_decision_case(upgraded, attempted, clock=GovernanceClock())
        assert denied.report is not None and denied.report.result.governance is not None
        assert denied.report.result.governance.reasons == ("QUALIFICATION_REVISION_CONFLICT",)
        assert (
            get_formal_report(
                execution.report_version_id,
                upgraded,
                principal=AccessPrincipal(
                    user_id=case.access_scope.user_id,
                    account_ids=case.access_scope.account_ids,
                    permissions=("REPORT_READ",),
                ),
            )
            == execution.report
        )
    runtime.run_store.close()


def test_published_legacy_policyless_approval_remains_readable_without_new_authority(
    migrated_settings: Settings,
) -> None:
    """Native D0 legacy projection fixture, not a historical-policy qualification proof."""
    command = qualification_command(migrated_settings)
    command["version"].pop("qualification_policy")
    command["evidence"]["version"].pop("qualification_policy")
    payload = case_payload(migrated_settings, "native-legacy-published-policyless", command)
    case = FrozenDecisionCase.model_validate(payload)
    original_command = QualificationCommand.model_validate(command)
    runtime = initialize_runtime_storage(migrated_settings)
    original_run = asyncio.run(execute_frozen_decision_case(case, runtime, clock=GovernanceClock()))
    assert original_run.status == "SUCCEEDED"
    run_before = asyncio.run(runtime.run_store.get_run(case.framework_run_id))
    checkpoints_before = asyncio.run(runtime.run_store.get_checkpoints(case.framework_run_id))
    legacy_outcome = GovernanceOutcome(
        disposition="APPROVED",
        reasons=("QUALIFICATION_PASS",),
        qualification=QualificationRecord(
            decision_id=case.decision_event_id,
            authorization_id=case.decision_event_id,
            scope=original_command.scope,
            version=original_command.version,
            status="VALID",
            cause="QUALIFICATION_PASS",
            authorization_evidence=original_command.evidence,
            recorded_at=GovernanceClock().now(),
            evidence=original_command.evidence,
        ),
    )
    result = case.expected_external_result.model_copy(update={"governance": legacy_outcome})
    phases: tuple[StagePhase, ...] = (
        "FRAMEWORK_RUN",
        "HOST_VALIDATION",
        "BUSINESS_DECISION",
        "QUALIFICATION",
        "BUSINESS_COMMIT",
    )
    stages = tuple(
        StageResult(
            phase=phase,
            status="SUCCEEDED",
            gate_results=(GateResult(gate_id="NATIVE_SYNTHETIC_LEGACY_FIXTURE", status="PASSED"),),
            reasons=(),
        )
        for phase in phases
    )
    ledger = DecisionLedger.from_settings(migrated_settings, clock=GovernanceClock())
    with ledger.serialize_case_execution() as connection:
        ledger.ensure_business_object(connection, case)
        fact = ledger.commit_event(
            connection,
            case=case,
            framework_run_id=case.framework_run_id,
            result=result,
            stage_results=stages,
        )
        ledger.ensure_event_stage_results(connection, fact)
        published = ledger.publish_report(connection, fact)
        ledger.record_stage_result(
            connection,
            case=case,
            stage_result=published.stage_results[-1],
            decision_event_id=case.decision_event_id,
            framework_run_id=case.framework_run_id,
        )
        raw_case = connection.execute(
            select(DECISION_CASE_BUSINESS_OBJECTS.c.case_payload).where(
                DECISION_CASE_BUSINESS_OBJECTS.c.business_object_id == case.business_object_id
            )
        ).scalar_one()
        raw_event = connection.execute(
            select(DECISION_EVENTS.c.event_payload).where(
                DECISION_EVENTS.c.decision_event_id == case.decision_event_id
            )
        ).scalar_one()
        raw_report = connection.execute(
            select(FORMAL_REPORTS.c.report_payload).where(
                FORMAL_REPORTS.c.report_version_id == published.report_version_id
            )
        ).scalar_one()
    assert "qualification_policy" not in raw_case + raw_event + raw_report
    assert "policy_binding" not in raw_event + raw_report
    assert case.access_scope is not None
    principal = AccessPrincipal(
        user_id=case.access_scope.user_id,
        account_ids=case.access_scope.account_ids,
        permissions=("REPORT_READ",),
    )
    assert (
        get_formal_report(published.report_version_id, migrated_settings, principal=principal)
        == published
    )
    assert (
        run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
        == published
    )
    replacement = deepcopy(payload)
    replacement["governance"] = qualification_command(migrated_settings)
    changed_case = FrozenDecisionCase.model_validate(replacement)
    assert (
        run_frozen_decision_case(migrated_settings, replacement, clock=GovernanceClock()).report
        == published
    )
    correction = service.correct_default_frozen_decision_case(
        changed_case, ledger, case.business_identity
    )
    assert correction.report.version_bundle == case.version_bundle
    with ledger.serialize_case_execution() as connection:
        correction_fact = ledger.get_correction_event(case.decision_event_id, connection)
        assert correction_fact is not None and correction_fact.case.governance == case.governance
        assert ledger.get_original_decision_event(case.business_object_id, connection) == fact
        assert (
            connection.execute(
                select(DECISION_CASE_BUSINESS_OBJECTS.c.case_payload).where(
                    DECISION_CASE_BUSINESS_OBJECTS.c.business_object_id == case.business_object_id
                )
            ).scalar_one()
            == raw_case
        )
        assert (
            connection.execute(
                select(DECISION_EVENTS.c.event_payload).where(
                    DECISION_EVENTS.c.decision_event_id == case.decision_event_id
                )
            ).scalar_one()
            == raw_event
        )
        assert (
            connection.execute(
                select(FORMAL_REPORTS.c.report_payload).where(
                    FORMAL_REPORTS.c.report_version_id == published.report_version_id
                )
            ).scalar_one()
            == raw_report
        )
    assert (
        get_formal_report(published.report_version_id, migrated_settings, principal=principal)
        == published
    )
    assert asyncio.run(runtime.run_store.get_run(case.framework_run_id)) == run_before
    assert (
        asyncio.run(runtime.run_store.get_checkpoints(case.framework_run_id)) == checkpoints_before
    )
    assert asyncio.run(runtime.run_store.get_run(changed_case.framework_run_id)) is None
    node = task_node(18)
    registration = run_frozen_decision_case(
        migrated_settings,
        case_payload(
            migrated_settings,
            "legacy-policyless-next-node",
            {"operation": "REGISTER_TASK_NODE", "scope": command["scope"], "node": node},
            contract_version="5.0.0",
        ),
        clock=GovernanceClock(),
    )
    assert registration.report is not None and registration.report.result.governance is not None
    assert registration.report.result.governance.disposition == "APPROVED"
    request = case_payload(
        migrated_settings,
        "legacy-policyless-new-activation",
        {
            "operation": "ACTIVATE_VERSION",
            "scope": command["scope"],
            "version": command["version"],
            "previous_version": None,
            "previous_activation_id": None,
            "qualification_decision_id": case.decision_event_id,
            "first_node_id": node["node_id"],
        },
        contract_version="5.0.0",
    )
    denied = run_frozen_decision_case(migrated_settings, request, clock=GovernanceClock())
    assert denied.report is not None and denied.report.result.governance is not None
    assert denied.report.result.governance.disposition == "DENIED"
    assert denied.report.result.governance.reasons == ("VERSION_HANDOFF_PREREQUISITES_NOT_MET",)
    assert denied.report.result.governance.activation is None
    runtime.run_store.close()
