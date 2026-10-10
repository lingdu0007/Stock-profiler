"""Synthetic saved-plan journeys through the authenticated delivery seam."""

import pytest
from test_candidate_confirmation import confirmation_payload
from test_scoped_qualification import GovernanceClock

from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.delivery.access import AccessPrincipal


def test_saved_allocation_is_visible_only_with_the_separate_allocation_grant(
    migrated_settings: Settings,
) -> None:
    payload = confirmation_payload(migrated_settings)
    delivery = ResultDelivery.from_settings(
        migrated_settings, clock=GovernanceClock("2042-05-19T16:01:00Z")
    )
    reader = AccessPrincipal(
        user_id=payload["access_scope"]["user_id"],
        account_ids=tuple(payload["access_scope"]["account_ids"]),
        permissions=("REPORT_READ",),
    )
    b1 = delivery.candidate_workspace(reader)
    assert b1 is not None and b1.allocations == ()
    b2 = delivery.candidate_workspace(
        reader.model_copy(update={"permissions": ("REPORT_READ", "CANDIDATE_ALLOCATION")})
    )
    assert b2 is not None and len(b2.allocations) == 1
    view = b2.allocations[0]
    assert view.plan_event_id == payload["candidate_confirmation"]["plan_event_id"]
    assert view.allocation.total_principal == 1400
    assert view.confirmations == () and view.executions == ()
    assert view.allocation.rows[0].legs[0].quantity == 70


def test_workspace_batch_command_replays_the_saved_policy_and_keeps_original_key(
    migrated_settings: Settings,
) -> None:
    from stock_profiler.bootstrap.candidate_workspace import submit_candidate_command

    payload = confirmation_payload(migrated_settings)
    principal = AccessPrincipal(
        user_id=payload["access_scope"]["user_id"],
        account_ids=tuple(payload["access_scope"]["account_ids"]),
        permissions=("REPORT_READ", "CANDIDATE_ALLOCATION", "CANDIDATE_COMMAND"),
    )
    delivery = ResultDelivery.from_settings(migrated_settings)
    command = payload["candidate_confirmation"]
    workspace = delivery.candidate_workspace(principal)
    assert workspace is not None
    request = {
        "operation": "CONFIRM",
        "plan_report_version_id": workspace.allocations[0].report_version_id,
        "input_report_version_id": workspace.allocations[0].report_version_id,
        "seen_confirmation_id": None,
        "seen_execution_id": None,
        "idempotency_key": "synthetic-workspace-confirm",
        "choices": command["choices"],
    }
    result = submit_candidate_command(
        migrated_settings, principal, request, clock=GovernanceClock("2042-05-19T16:01:00Z")
    )
    assert result.report is not None
    confirmation = result.report.result.candidate_confirmation
    assert confirmation is not None and confirmation.disposition == "CONFIRMED"
    assert [row.principal for row in confirmation.reservations] == [700, 700]
    replay = submit_candidate_command(
        migrated_settings, principal, request, clock=GovernanceClock("2042-05-19T16:02:00Z")
    )
    assert replay.report == result.report
    saved_workspace = delivery.candidate_workspace(principal)
    assert saved_workspace is not None
    assert saved_workspace.allocations[0].confirmations[0].event_id == result.report.event_id


def test_review_proves_equivalence_without_forming_a_choice_or_reservation(
    migrated_settings: Settings,
) -> None:
    from stock_profiler.bootstrap.candidate_workspace import submit_candidate_command

    payload = confirmation_payload(migrated_settings)
    principal = AccessPrincipal(
        user_id=payload["access_scope"]["user_id"],
        account_ids=tuple(payload["access_scope"]["account_ids"]),
        permissions=("REPORT_READ", "CANDIDATE_ALLOCATION", "CANDIDATE_COMMAND"),
    )
    delivery = ResultDelivery.from_settings(migrated_settings)
    workspace = delivery.candidate_workspace(principal)
    assert workspace is not None
    request = {
        "operation": "REVIEW",
        "plan_report_version_id": workspace.allocations[0].report_version_id,
        "input_report_version_id": workspace.allocations[0].report_version_id,
        "seen_confirmation_id": None,
        "seen_execution_id": None,
        "idempotency_key": "synthetic-workspace-review",
    }
    result = submit_candidate_command(
        migrated_settings, principal, request, clock=GovernanceClock("2042-05-19T16:01:00Z")
    )
    assert result.report is not None
    review = result.report.result.candidate_confirmation
    assert review is not None and review.disposition == "REVALIDATED"
    assert review.confirmation_id is None and review.choices == () and review.reservations == ()


def test_authenticated_command_requires_csrf_origin_and_recent_session(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_authenticated_report_delivery import _authenticated_client_with_csrf

    payload = confirmation_payload(migrated_settings)
    settings = migrated_settings.model_copy(
        update={
            "report_account_ids": tuple(payload["access_scope"]["account_ids"]),
            "report_permissions": ("REPORT_READ", "CANDIDATE_ALLOCATION", "CANDIDATE_COMMAND"),
        }
    )
    clock = GovernanceClock("2042-05-19T16:01:00Z")
    client, csrf = _authenticated_client_with_csrf(settings, monkeypatch, clock=clock)
    view = client.get("/api/v1/candidates").json()["allocations"][0]
    request = {
        "operation": "REVIEW",
        "plan_report_version_id": view["report_version_id"],
        "input_report_version_id": view["report_version_id"],
        "seen_confirmation_id": None,
        "seen_execution_id": None,
        "idempotency_key": "synthetic-http-review",
    }
    path = "/api/v1/candidates/commands"
    assert client.post(path, json=request).status_code == 403
    assert (
        client.post(
            path, json=request, headers={"Origin": "https://wrong.example", "X-CSRF-Token": csrf}
        ).status_code
        == 403
    )
    response = client.post(
        path, json=request, headers={"Origin": "https://localhost", "X-CSRF-Token": csrf}
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert (
        response.json()["report"]["result"]["candidate_confirmation"]["disposition"]
        == "REVALIDATED"
    )
    from datetime import timedelta

    clock.current += timedelta(minutes=11)
    assert (
        client.post(
            path, json=request, headers={"Origin": "https://localhost", "X-CSRF-Token": csrf}
        ).status_code
        == 403
    )


def test_declaration_is_pending_and_preserves_saved_broker_reservations(
    migrated_settings: Settings,
) -> None:
    from test_candidate_execution import execution_payload

    from stock_profiler.bootstrap.candidate_workspace import submit_candidate_command

    payload, accepted = execution_payload(migrated_settings)
    principal = AccessPrincipal(
        user_id=payload["access_scope"]["user_id"],
        account_ids=tuple(payload["access_scope"]["account_ids"]),
        permissions=("REPORT_READ", "CANDIDATE_ALLOCATION", "CANDIDATE_COMMAND"),
    )
    workspace = ResultDelivery.from_settings(migrated_settings).candidate_workspace(principal)
    assert workspace is not None
    request = {
        "operation": "DECLARE",
        "plan_report_version_id": workspace.allocations[0].report_version_id,
        "input_report_version_id": workspace.allocations[0].report_version_id,
        "seen_confirmation_id": accepted.event_id,
        "seen_execution_id": None,
        "idempotency_key": "synthetic-workspace-declaration",
        "declaration": payload["candidate_execution"]["declaration"],
    }
    execution = submit_candidate_command(
        migrated_settings, principal, request, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert execution.report is not None
    outcome = execution.report.result.candidate_execution
    assert outcome is not None and outcome.disposition == "PENDING_RECONCILIATION"
    assert outcome.reservations == accepted.result.candidate_confirmation.reservations
    assert outcome.fills == () and outcome.released_reservation_ids == ()


@pytest.mark.parametrize("change", ["price", "policy"])
def test_changed_policy_review_permanently_stops_new_actions_on_the_old_plan(
    migrated_settings: Settings,
    change: str,
) -> None:
    from copy import deepcopy

    from stock_profiler.bootstrap.candidate_workspace import submit_candidate_command
    from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case

    payload = confirmation_payload(migrated_settings)
    principal = AccessPrincipal(
        user_id=payload["access_scope"]["user_id"],
        account_ids=tuple(payload["access_scope"]["account_ids"]),
        permissions=("REPORT_READ", "CANDIDATE_ALLOCATION", "CANDIDATE_COMMAND"),
    )
    delivery = ResultDelivery.from_settings(migrated_settings)
    workspace = delivery.candidate_workspace(principal)
    assert workspace is not None
    original_id = workspace.allocations[0].report_version_id
    newer = deepcopy(payload)
    allocation = newer.pop("candidate_confirmation")["revalidation"]
    newer["input"].pop("candidate_confirmation")
    if change == "price":
        allocation["routes"][0]["price_cap"] = "11"
    else:
        allocation["policy"]["entry_target_ratio"] = "0.14"
    newer["candidate_allocation"] = allocation
    newer["input"]["candidate_allocation"] = deepcopy(allocation)
    newer["business_identity"] = "synthetic-workspace:changed-price"
    for field in (
        "case_contract_version",
        "host_contract_version",
        "report_projection_contract_version",
    ):
        newer["version_bundle"][field] = "candidate-allocation.1.0.0"
    inputs = run_frozen_decision_case(
        migrated_settings, newer, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert inputs is not None
    request = {
        "operation": "REVIEW",
        "plan_report_version_id": original_id,
        "input_report_version_id": inputs.report_version_id,
        "seen_confirmation_id": None,
        "seen_execution_id": None,
        "idempotency_key": "synthetic-changed-review",
    }
    review = submit_candidate_command(
        migrated_settings, principal, request, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert review is not None and review.result.candidate_confirmation is not None
    assert review.result.candidate_confirmation.reasons == ("PLAN_CHANGED",)
    request.update(
        operation="CONFIRM",
        input_report_version_id=original_id,
        idempotency_key="synthetic-old-plan-revival",
        choices=payload["candidate_confirmation"]["choices"],
    )
    stopped = submit_candidate_command(
        migrated_settings, principal, request, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert stopped is not None and stopped.result.candidate_confirmation is not None
    assert stopped.result.candidate_confirmation.reasons == ("PLAN_INVALIDATED",)
    assert stopped.result.candidate_confirmation.reservations == ()


@pytest.mark.parametrize("fault", ["before", "after"])
def test_workspace_submission_recovers_the_original_commit_without_a_new_reservation(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    from typing import Any

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.bootstrap.candidate_workspace import submit_candidate_command
    from stock_profiler.modules.decision_cases.ports import (
        DecisionEventCommitError,
        DecisionEventCommitUncertainError,
    )

    payload = confirmation_payload(migrated_settings)
    principal = AccessPrincipal(
        user_id=payload["access_scope"]["user_id"],
        account_ids=tuple(payload["access_scope"]["account_ids"]),
        permissions=("REPORT_READ", "CANDIDATE_ALLOCATION", "CANDIDATE_COMMAND"),
    )
    workspace = ResultDelivery.from_settings(migrated_settings).candidate_workspace(principal)
    assert workspace is not None
    request = {
        "operation": "CONFIRM",
        "plan_report_version_id": workspace.allocations[0].report_version_id,
        "input_report_version_id": workspace.allocations[0].report_version_id,
        "seen_confirmation_id": None,
        "seen_execution_id": None,
        "idempotency_key": "synthetic-workspace-uncertain-confirm",
        "choices": payload["candidate_confirmation"]["choices"],
    }
    original = DecisionLedger.commit_event

    def commit(self: DecisionLedger, *args: Any, **kwargs: Any) -> Any:
        if fault == "after":
            original(self, *args, **kwargs)
            raise DecisionEventCommitUncertainError("Synthetic acknowledgement loss")
        raise DecisionEventCommitError("Synthetic rollback")

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "commit_event", commit)
        attempt = submit_candidate_command(
            migrated_settings, principal, request, clock=GovernanceClock("2042-05-19T16:01:00Z")
        )
    recovered = submit_candidate_command(
        migrated_settings, principal, request, clock=GovernanceClock("2042-05-19T16:02:00Z")
    )
    assert recovered.framework_run_id == attempt.framework_run_id
    assert (
        recovered.report is not None and recovered.report.result.candidate_confirmation is not None
    )
    assert [
        row.principal for row in recovered.report.result.candidate_confirmation.reservations
    ] == [700, 700]
    final = ResultDelivery.from_settings(migrated_settings).candidate_workspace(principal)
    assert final is not None and len(final.allocations[0].confirmations) == 1
    changed = dict(
        request,
        choices=[
            {"security_id": row["security_id"], "choice": "DEFER"} for row in request["choices"]
        ],
    )
    with pytest.raises(ValueError, match="idempotency payload conflict"):
        submit_candidate_command(migrated_settings, principal, changed)


def test_execution_step_review_keeps_the_confirmed_vector_and_reservation_identities(
    migrated_settings: Settings,
) -> None:
    from test_candidate_execution import execution_payload

    from stock_profiler.bootstrap.candidate_workspace import submit_candidate_command

    payload, accepted = execution_payload(migrated_settings, review_step=False)
    principal = AccessPrincipal(
        user_id=payload["access_scope"]["user_id"],
        account_ids=tuple(payload["access_scope"]["account_ids"]),
        permissions=("REPORT_READ", "CANDIDATE_ALLOCATION", "CANDIDATE_COMMAND"),
    )
    workspace = ResultDelivery.from_settings(migrated_settings).candidate_workspace(principal)
    assert workspace is not None
    request = {
        "operation": "REVIEW_STEP",
        "plan_report_version_id": workspace.allocations[0].report_version_id,
        "input_report_version_id": workspace.allocations[0].report_version_id,
        "seen_confirmation_id": accepted.event_id,
        "seen_execution_id": None,
        "idempotency_key": "synthetic-workspace-step",
    }
    review = submit_candidate_command(
        migrated_settings, principal, request, clock=GovernanceClock("2042-05-19T16:01:10Z")
    ).report
    assert review is not None and review.result.candidate_confirmation is not None
    assert review.result.candidate_confirmation.disposition == "CONFIRMED"
    assert (
        review.result.candidate_confirmation.reservations
        == accepted.result.candidate_confirmation.reservations
    )
    assert (
        review.result.candidate_confirmation.choices
        == accepted.result.candidate_confirmation.choices
    )
    assert review.result.candidate_confirmation.supersedes_confirmation_id == accepted.event_id


def test_replanning_binds_original_plan_and_never_extends_its_window(
    migrated_settings: Settings,
) -> None:
    from stock_profiler.bootstrap.candidate_workspace import submit_candidate_command

    payload = confirmation_payload(migrated_settings)
    principal = AccessPrincipal(
        user_id=payload["access_scope"]["user_id"],
        account_ids=tuple(payload["access_scope"]["account_ids"]),
        permissions=("REPORT_READ", "CANDIDATE_ALLOCATION", "CANDIDATE_COMMAND"),
    )
    delivery = ResultDelivery.from_settings(migrated_settings)
    workspace = delivery.candidate_workspace(principal)
    assert workspace is not None
    original = workspace.allocations[0]
    request = {
        "operation": "REPLAN",
        "plan_report_version_id": original.report_version_id,
        "input_report_version_id": original.report_version_id,
        "seen_confirmation_id": None,
        "seen_execution_id": None,
        "idempotency_key": "synthetic-workspace-replan",
        "trigger_reason": "FACTS_CHANGED",
    }
    replanned = submit_candidate_command(
        migrated_settings, principal, request, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert replanned is not None and replanned.result.candidate_allocation is not None
    plan = replanned.result.candidate_allocation
    assert plan.replaces_plan_event_id == original.plan_event_id
    assert plan.replanning_reason == "FACTS_CHANGED"
    assert plan.plan_id != original.allocation.plan_id
    assert (
        plan.rows[0].candidate.valid_market_dates
        == original.allocation.rows[0].candidate.valid_market_dates
    )
    later = delivery.candidate_workspace(principal)
    assert later is not None and later.allocations[-1].confirmations == ()
    assert "PLAN_SUPERSEDED" in later.allocations[0].stop_reasons
    revival = dict(
        request,
        operation="CONFIRM",
        trigger_reason=None,
        idempotency_key="synthetic-superseded-confirm",
        choices=payload["candidate_confirmation"]["choices"],
    )
    stopped = submit_candidate_command(
        migrated_settings, principal, revival, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert stopped is not None and stopped.result.candidate_confirmation is not None
    assert stopped.result.candidate_confirmation.reasons == ("PLAN_SUPERSEDED",)
    request["idempotency_key"] = "synthetic-replan-after-expiry"
    expired = submit_candidate_command(
        migrated_settings, principal, request, clock=GovernanceClock("2042-05-24T16:01:00Z")
    ).report
    assert expired is not None and expired.result.candidate_allocation is not None
    assert expired.result.candidate_allocation.disposition == "BLOCKED"
    assert expired.result.candidate_allocation.reasons == ("REPLANNING_WINDOW_CLOSED",)


def test_b1_cannot_read_personal_plan_through_a_generic_report_deep_link(
    migrated_settings: Settings,
) -> None:
    payload = confirmation_payload(migrated_settings)
    delivery = ResultDelivery.from_settings(migrated_settings)
    reader = AccessPrincipal(
        user_id=payload["access_scope"]["user_id"],
        account_ids=tuple(payload["access_scope"]["account_ids"]),
        permissions=("REPORT_READ",),
    )
    b2 = reader.model_copy(update={"permissions": ("REPORT_READ", "CANDIDATE_ALLOCATION")})
    workspace = delivery.candidate_workspace(b2)
    assert workspace is not None
    assert delivery.read_report(workspace.allocations[0].report_version_id, reader) is None


@pytest.mark.parametrize("unknown", [False, True])
def test_workspace_withdrawal_requires_authoritative_saved_order_exclusion(
    migrated_settings: Settings, unknown: bool
) -> None:
    from test_candidate_confirmation import save_withdrawal_proof
    from test_candidate_execution import execution_payload

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.bootstrap.candidate_workspace import submit_candidate_command

    payload, accepted = execution_payload(migrated_settings, review_step=False)
    principal = AccessPrincipal(
        user_id=payload["access_scope"]["user_id"],
        account_ids=tuple(payload["access_scope"]["account_ids"]),
        permissions=("REPORT_READ", "CANDIDATE_ALLOCATION", "CANDIDATE_COMMAND"),
    )
    delivery = ResultDelivery.from_settings(migrated_settings)
    workspace = delivery.candidate_workspace(principal)
    assert workspace is not None
    plan = workspace.allocations[0]
    ledger = DecisionLedger.from_settings(migrated_settings)
    with ledger.serialize_case_execution() as connection:
        accepted_fact = ledger.get_decision_event(accepted.event_id, connection)
    assert accepted_fact is not None
    proof_id = save_withdrawal_proof(
        migrated_settings,
        accepted_fact.case.model_dump(mode="json"),
        open_order=unknown,
        unknown=unknown,
    )
    with ledger.serialize_case_execution() as connection:
        proof = ledger.get_formal_report_for_event(proof_id, connection)
    assert proof is not None and accepted.result.candidate_confirmation is not None
    request = {
        "operation": "WITHDRAW",
        "plan_report_version_id": plan.report_version_id,
        "input_report_version_id": plan.report_version_id,
        "seen_confirmation_id": accepted.event_id,
        "seen_execution_id": None,
        "idempotency_key": "synthetic-workspace-withdraw",
        "withdrawal_position_report_version_id": proof.report_version_id,
        "choices": [
            {"security_id": row.security_id, "choice": "DECLINE"}
            for row in accepted.result.candidate_confirmation.choices
        ],
    }
    report = submit_candidate_command(
        migrated_settings, principal, request, clock=GovernanceClock("2042-05-19T16:02:00Z")
    ).report
    assert report is not None and report.result.candidate_confirmation is not None
    outcome = report.result.candidate_confirmation
    if unknown:
        assert outcome.disposition == "BLOCKED" and outcome.released_reservation_ids == ()
    else:
        assert outcome.disposition == "CONFIRMED" and outcome.reservations == ()
        assert set(outcome.released_reservation_ids) == {
            row.commitment_id for row in accepted.result.candidate_confirmation.reservations
        }


def test_new_confirmation_cannot_ignore_an_unclosed_execution_version(
    migrated_settings: Settings,
) -> None:
    from test_candidate_execution import execution_payload

    from stock_profiler.bootstrap.candidate_workspace import submit_candidate_command

    payload, accepted = execution_payload(migrated_settings)
    principal = AccessPrincipal(
        user_id=payload["access_scope"]["user_id"],
        account_ids=tuple(payload["access_scope"]["account_ids"]),
        permissions=("REPORT_READ", "CANDIDATE_ALLOCATION", "CANDIDATE_COMMAND"),
    )
    workspace = ResultDelivery.from_settings(migrated_settings).candidate_workspace(principal)
    assert workspace is not None
    request = {
        "operation": "DECLARE",
        "plan_report_version_id": workspace.allocations[0].report_version_id,
        "input_report_version_id": workspace.allocations[0].report_version_id,
        "seen_confirmation_id": accepted.event_id,
        "seen_execution_id": None,
        "idempotency_key": "synthetic-unclosed-declaration",
        "declaration": payload["candidate_execution"]["declaration"],
    }
    pending = submit_candidate_command(
        migrated_settings, principal, request, clock=GovernanceClock("2042-05-19T16:02:00Z")
    ).report
    assert pending is not None
    request.update(
        operation="REVIEW_STEP",
        declaration=None,
        idempotency_key="synthetic-stale-execution-review",
    )
    stopped = submit_candidate_command(
        migrated_settings, principal, request, clock=GovernanceClock("2042-05-19T16:02:10Z")
    ).report
    assert stopped is not None and stopped.result.candidate_confirmation is not None
    assert stopped.result.candidate_confirmation.reasons == ("EXECUTION_VERSION_CONFLICT",)
    assert stopped.result.candidate_confirmation.reservations == ()


def test_replan_rechecks_the_original_window_at_the_commit_boundary(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import Any

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.bootstrap.candidate_workspace import submit_candidate_command

    payload = confirmation_payload(migrated_settings)
    principal = AccessPrincipal(
        user_id=payload["access_scope"]["user_id"],
        account_ids=tuple(payload["access_scope"]["account_ids"]),
        permissions=("REPORT_READ", "CANDIDATE_ALLOCATION", "CANDIDATE_COMMAND"),
    )
    workspace = ResultDelivery.from_settings(migrated_settings).candidate_workspace(principal)
    assert workspace is not None
    request = {
        "operation": "REPLAN",
        "plan_report_version_id": workspace.allocations[0].report_version_id,
        "input_report_version_id": workspace.allocations[0].report_version_id,
        "seen_confirmation_id": None,
        "seen_execution_id": None,
        "idempotency_key": "synthetic-replan-crossing-expiry",
        "trigger_reason": "FACTS_CHANGED",
    }
    clock = GovernanceClock("2042-05-19T16:01:00Z")
    original = DecisionLedger.record_stage_result

    def record(self: DecisionLedger, *args: Any, **kwargs: Any) -> Any:
        result = original(self, *args, **kwargs)
        if kwargs["stage_result"].phase == "CANDIDATE_ALLOCATION":
            clock.current = GovernanceClock("2042-05-24T16:01:00Z").current
        return result

    monkeypatch.setattr(DecisionLedger, "record_stage_result", record)
    result = submit_candidate_command(migrated_settings, principal, request, clock=clock).report
    assert result is not None and result.result.candidate_allocation is not None
    assert result.result.candidate_allocation.disposition == "BLOCKED"
    assert result.result.candidate_allocation.reasons == ("REPLANNING_WINDOW_CLOSED",)
