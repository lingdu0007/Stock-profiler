"""Credential-free candidate delivery through the shared authenticated host boundary."""

import pytest
from synthetic_candidate_workspace import failed_candidate_case
from test_authenticated_report_delivery import _authenticated_client_with_csrf

from stock_profiler.bootstrap.settings import Settings


def test_authenticated_workspace_has_explicit_empty_history(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _authenticated_client_with_csrf(migrated_settings, monkeypatch)
    response = client.get("/api/v1/candidates")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["releases"] == []
    assert response.json()["current_report_ids"] == []
    assert response.json()["details"] == []
    assert client.post("/api/v1/candidates", json={"kind": "CONFIRM"}).status_code == 405


def test_failed_month_is_visible_with_original_report_identity(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case

    execution = run_frozen_decision_case(
        migrated_settings, failed_candidate_case(migrated_settings)
    )
    assert execution.report is not None
    client, _ = _authenticated_client_with_csrf(migrated_settings, monkeypatch)
    response = client.get("/api/v1/candidates")
    assert response.status_code == 200
    release = response.json()["releases"][0]
    assert release["report_version_id"] == execution.report.report_version_id
    assert release["event_id"] == execution.report.event_id
    assert release["release"]["disposition"] == "FAILED"
    assert release["knowledge_cutoff"] == execution.report.knowledge_cutoff
    assert response.json()["current_report_ids"] == [execution.report.report_version_id]
    assert "synthetic-account-4017" not in response.text


def test_initial_reminder_uses_only_minimal_body_and_stable_monthly_identity(
    migrated_settings: Settings,
) -> None:
    from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
    from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
    from stock_profiler.modules.delivery.access import AccessPrincipal
    from stock_profiler.modules.delivery.candidate_notifications import CandidateReminderRequest

    execution = run_frozen_decision_case(
        migrated_settings, failed_candidate_case(migrated_settings)
    )
    assert execution.report is not None
    principal = AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017",),
        permissions=("REPORT_READ",),
    )
    delivery = ResultDelivery.from_settings(migrated_settings)
    request = CandidateReminderRequest(
        contract_version="1.0.0",
        synthetic=True,
        generator_version="candidate-reminder-v1",
        seed=1818,
        kind="INITIAL",
        primary_result="ACCEPTED",
        fallback_result="ACCEPTED",
    )
    reminder = delivery.candidate_reminder(execution.report.report_version_id, principal, request)
    assert reminder is not None
    assert set(reminder.body.model_dump()) == {
        "result_type",
        "candidate_count",
        "window_ends_at",
        "entry_path",
    }
    assert reminder.body.result_type == "FAILED"
    assert reminder.body.candidate_count == 0
    assert reminder.body.entry_path == f"/candidates/releases/{execution.report.report_version_id}"
    workspace = delivery.candidate_workspace(principal)
    assert workspace is not None
    assert workspace.releases[0].reminders == (reminder,)
    assert reminder == delivery.candidate_reminder(
        execution.report.report_version_id, principal, request
    )
    assert (
        len(delivery.candidate_reminder_history(execution.report.report_version_id, principal)) == 1
    )


def test_ordinary_reminder_defers_until_quiet_period_ends_without_new_intent(
    migrated_settings: Settings,
) -> None:
    from datetime import datetime

    from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
    from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
    from stock_profiler.modules.delivery.access import AccessPrincipal
    from stock_profiler.modules.delivery.candidate_notifications import CandidateReminderRequest

    class FixedClock:
        instant = datetime.fromisoformat("2042-05-17T16:01:00+00:00")

        def now(self) -> datetime:
            return self.instant

    clock = FixedClock()
    execution = run_frozen_decision_case(
        migrated_settings, failed_candidate_case(migrated_settings), clock=clock
    )
    assert execution.report is not None
    principal = AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017",),
        permissions=("REPORT_READ",),
    )
    delivery = ResultDelivery.from_settings(migrated_settings, clock=clock)
    request = CandidateReminderRequest(
        contract_version="1.0.0",
        synthetic=True,
        generator_version="candidate-reminder-v1",
        seed=1818,
        kind="INITIAL",
        primary_result="REJECTED",
        fallback_result="ACCEPTED",
        quiet_until=clock.instant.replace(year=clock.instant.year + 1),
    )
    deferred = delivery.candidate_reminder(execution.report.report_version_id, principal, request)
    assert deferred is not None and deferred.status == "DEFERRED"
    assert deferred.channels == ()
    retry = request.model_copy(update={"retry_of": deferred.attempt_id})
    assert delivery.candidate_reminder(execution.report.report_version_id, principal, retry) is None
    assert request.quiet_until is not None
    clock.instant = request.quiet_until
    completed = delivery.candidate_reminder(execution.report.report_version_id, principal, retry)
    assert completed is not None and completed.status == "ATTEMPTED"
    assert completed.intent_id == deferred.intent_id
    assert [channel.role for channel in completed.channels] == ["PRIMARY", "PERSISTENT"]
    assert completed.body == deferred.body
    assert (
        delivery.candidate_reminder(execution.report.report_version_id, principal, retry)
        == completed
    )


def test_candidate_detail_identity_and_current_qualification_are_independent(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timedelta
    from functools import partial

    import test_research_risk_veto as research_cases

    monkeypatch.setattr(
        research_cases, "_case", partial(research_cases._case, user_id="stock-profiler-single-user")
    )

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.foundation.clock import UtcClock
    from stock_profiler.modules.qualification.contracts import GovernanceOutcome

    research_cases.test_candidate_release_uses_committed_raw_scores_and_saves_market_state_abstention(
        migrated_settings, "ACCEPT", "QUALIFICATION_DIAGNOSTIC_ALERT_AFTER_REPORT_SAVE", monkeypatch
    )
    client, _ = _authenticated_client_with_csrf(migrated_settings, monkeypatch)
    workspace = client.get("/api/v1/candidates").json()
    release = workspace["releases"][0]
    assert release["status"] in {"CURRENT", "WAITING_MARKET"}
    assert len(workspace["details"]) == 10
    assert release["frozen_pool_count"] == 10
    assert release["research_completed_count"] == 10
    assert len(set(release["detail_ids"])) == 10
    assert all(item["event_id"] == release["event_id"] for item in workspace["details"])
    frozen_cutoff = release["knowledge_cutoff"]
    frozen_expiry = release["valid_through"]
    from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
    from stock_profiler.modules.delivery.access import AccessPrincipal
    from stock_profiler.modules.delivery.candidate_reminder_contracts import (
        CandidateReminderRequest,
    )

    principal = AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017",),
        permissions=("REPORT_READ",),
    )
    delivery = ResultDelivery.from_settings(migrated_settings)
    initial_request = CandidateReminderRequest(
        contract_version="1.0.0",
        synthetic=True,
        generator_version="candidate-reminder-v1",
        seed=1818,
        kind="INITIAL",
        primary_result="ACCEPTED",
        fallback_result="ACCEPTED",
    )
    initial = delivery.candidate_reminder(release["report_version_id"], principal, initial_request)
    assert initial is not None and initial.body.candidate_count > 0

    ledger = DecisionLedger.from_settings(migrated_settings)
    with ledger.serialize_case_execution() as connection:
        event = ledger.get_decision_event(release["event_id"], connection)
        assert event is not None and event.case.access_scope is not None
        history = ledger.governance_history(connection, event.case.access_scope)
    previous = history[-1].qualification
    assert previous is not None
    changed_at = UtcClock().now() + timedelta(seconds=2)
    revoked = previous.model_copy(
        update={
            "decision_id": "synthetic-workspace-revocation",
            "status": "REVOKED",
            "previous_decision_id": previous.decision_id,
            "recorded_at": changed_at,
            "cause": "AUTHORIZATION_REVOKED",
        }
    )
    monkeypatch.setattr(
        DecisionLedger,
        "governance_history",
        lambda *args: (
            *history,
            GovernanceOutcome(
                disposition="APPROVED", reasons=("AUTHORIZATION_REVOKED",), qualification=revoked
            ),
        ),
    )
    monkeypatch.setattr(UtcClock, "now", lambda self: changed_at)
    invalidated = client.get("/api/v1/candidates").json()["releases"][0]
    assert invalidated["status"] == "INVALIDATED"
    assert invalidated["knowledge_cutoff"] == frozen_cutoff
    assert invalidated["valid_through"] == frozen_expiry
    assert (
        delivery.candidate_reminder(release["report_version_id"], principal, initial_request)
        is None
    )
    correction_request = initial_request.model_copy(
        update={"kind": "CORRECTION", "quiet_until": changed_at + timedelta(hours=1)}
    )
    withdrawal = delivery.candidate_reminder(
        release["report_version_id"], principal, correction_request
    )
    assert withdrawal is not None and withdrawal.body.candidate_count == 0
    assert withdrawal.body.result_type == "WITHDRAWN_DO_NOT_RELY_ON_ORIGINAL"
    assert [channel.role for channel in withdrawal.channels] == ["PRIMARY", "PERSISTENT"]
    assert (
        delivery.candidate_reminder(release["report_version_id"], principal, correction_request)
        == withdrawal
    )

    monkeypatch.setattr(
        UtcClock, "now", lambda self: datetime.fromisoformat("2042-07-08T16:00:00+00:00")
    )
    assert client.get("/api/v1/candidates").status_code == 401
    expired_workspace = delivery.candidate_workspace(principal)
    assert expired_workspace is not None
    expired = expired_workspace.model_dump(mode="json")["releases"][0]
    assert expired["status"] == "EXPIRED"
    assert expired["valid_through"] == frozen_expiry
    assert (
        delivery.candidate_reminder(release["report_version_id"], principal, initial_request)
        is None
    )
    assert (
        delivery.candidate_reminder(
            release["report_version_id"],
            principal,
            initial_request.model_copy(update={"kind": "FINAL"}),
        )
        is None
    )


@pytest.mark.parametrize(
    "kind, fields",
    [
        ("ACKNOWLEDGED", {}),
        ("CONFIRMED", {"choice": "ACCEPT"}),
        ("EXECUTION_DECLARED", {"declaration": "REPORTED_FILLED"}),
    ],
)
def test_candidate_reports_reject_action_facts(
    migrated_settings: Settings, kind: str, fields: dict[str, str]
) -> None:
    from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
    from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
    from stock_profiler.modules.delivery.access import AccessPrincipal
    from stock_profiler.modules.delivery.user_facts import UserFactRequest

    execution = run_frozen_decision_case(
        migrated_settings, failed_candidate_case(migrated_settings)
    )
    assert execution.report is not None
    principal = AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017",),
        permissions=("REPORT_READ", "USER_FACT"),
    )
    delivery = ResultDelivery.from_settings(migrated_settings)
    request = UserFactRequest.model_validate(
        {"kind": kind, "idempotency_key": "synthetic-action-probe", **fields}
    )
    assert delivery.record_user_fact(execution.report.report_version_id, principal, request) is None
    assert delivery.user_facts(execution.report.report_version_id, principal) == ()
    assert delivery.audit_history()[-1].reason == "CANDIDATE_READ_ONLY"


def test_correction_keeps_original_month_and_both_report_identities(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime

    from stock_profiler.bootstrap.decision_cases import (
        correct_default_frozen_decision_case,
        run_frozen_decision_case,
    )
    from stock_profiler.foundation.clock import UtcClock

    payload = failed_candidate_case(migrated_settings)
    original = run_frozen_decision_case(migrated_settings, payload)
    assert original.report is not None
    monkeypatch.setattr(
        UtcClock, "now", lambda self: datetime.fromisoformat("2042-08-01T09:00:00+00:00")
    )
    correction = correct_default_frozen_decision_case(
        migrated_settings, str(payload["business_identity"])
    )
    assert correction.report is not None
    client, _ = _authenticated_client_with_csrf(migrated_settings, monkeypatch)
    data = client.get("/api/v1/candidates").json()
    old, new = data["releases"]
    assert old["report_version_id"] == original.report.report_version_id
    assert old["status"] == "SUPERSEDED"
    assert old["superseded_by_report_id"] == new["report_version_id"]
    assert new["corrects_report_id"] == old["report_version_id"]
    assert new["plan_month"] == old["plan_month"]
    assert data["current_report_ids"] == [new["report_version_id"]]
    assert old["release"]["knowledge_cutoff"] == new["release"]["knowledge_cutoff"]


@pytest.mark.parametrize(
    "mode",
    [
        "SHADOW",
        "UNSAVED",
        "UNCOMMITTED",
        "WRONG_USER",
        "WRONG_ACCOUNT",
        "NO_PERMISSION",
        "FORGED_REQUEST",
    ],
)
def test_delivery_qualification_rejects_isolation_and_storage_failures(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from stock_profiler.adapters.persistence.decision_ledger import (
        DecisionEventCommitError,
        DecisionLedger,
    )
    from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
    from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
    from stock_profiler.modules.delivery.access import AccessPrincipal
    from stock_profiler.modules.delivery.candidate_reminder_contracts import (
        CandidateReminderRequest,
    )

    payload = failed_candidate_case(migrated_settings)
    if mode == "SHADOW":
        scope = payload["access_scope"]
        assert isinstance(scope, dict)
        scope["visibility"] = "SHADOW"
    if mode in {"UNSAVED", "UNCOMMITTED"}:

        def storage_failure(*args: object, **kwargs: object) -> None:
            raise DecisionEventCommitError("synthetic storage failure")

        monkeypatch.setattr(
            DecisionLedger,
            "publish_report" if mode == "UNSAVED" else "commit_event",
            storage_failure,
        )
    execution = run_frozen_decision_case(migrated_settings, payload)
    principal = AccessPrincipal(
        user_id="other-user" if mode == "WRONG_USER" else "stock-profiler-single-user",
        account_ids=("other-account",) if mode == "WRONG_ACCOUNT" else ("synthetic-account-4017",),
        permissions=() if mode == "NO_PERMISSION" else ("REPORT_READ",),
    )
    request = CandidateReminderRequest(
        contract_version="1.0.0",
        synthetic=True,
        generator_version="candidate-reminder-v1",
        seed=1818,
        kind="INITIAL",
        primary_result="ACCEPTED",
        fallback_result="ACCEPTED",
    )
    if mode == "FORGED_REQUEST":
        request = request.model_copy(update={"kind": "CONFIRM"})
    delivery = ResultDelivery.from_settings(migrated_settings)
    assert delivery.candidate_reminder(execution.report_version_id, principal, request) is None
    assert delivery.candidate_reminder_history(execution.report_version_id, principal) == ()
    if mode in {"SHADOW", "UNSAVED", "UNCOMMITTED"}:
        assert execution.report is None
        workspace = delivery.candidate_workspace(principal)
        assert workspace is not None and workspace.releases == ()
    assert delivery.audit_history()


def test_monthly_reminder_budget_survives_account_scope_revision(
    migrated_settings: Settings,
) -> None:
    from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
    from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
    from stock_profiler.modules.delivery.access import AccessPrincipal
    from stock_profiler.modules.delivery.candidate_reminder_contracts import (
        CandidateReminderRequest,
    )

    original = failed_candidate_case(migrated_settings)
    revised = failed_candidate_case(migrated_settings)
    scope = revised["access_scope"]
    assert isinstance(scope, dict)
    scope["account_ids"] = ["synthetic-account-4017", "synthetic-account-4018"]
    revised["business_identity"] = "synthetic-workspace-revised-scope"
    first = run_frozen_decision_case(migrated_settings, original)
    second = run_frozen_decision_case(migrated_settings, revised)
    assert first.report is not None and second.report is not None
    principal = AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017", "synthetic-account-4018"),
        permissions=("REPORT_READ",),
    )
    request = CandidateReminderRequest(
        contract_version="1.0.0",
        synthetic=True,
        generator_version="candidate-reminder-v1",
        seed=1818,
        kind="INITIAL",
        primary_result="ACCEPTED",
        fallback_result="ACCEPTED",
    )
    delivery = ResultDelivery.from_settings(migrated_settings)
    assert (
        delivery.candidate_reminder(first.report.report_version_id, principal, request) is not None
    )
    assert delivery.candidate_reminder(second.report.report_version_id, principal, request) is None
    assert delivery.candidate_reminder_history(second.report.report_version_id, principal) == ()
