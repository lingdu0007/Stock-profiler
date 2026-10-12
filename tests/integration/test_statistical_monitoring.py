from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from test_monitoring_workspace import monitoring_payload
from test_position_action_conclusion import action_payload
from test_position_state_reconciliation import position_evidence
from test_scoped_qualification import GovernanceClock

from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import FormalReport
from stock_profiler.modules.delivery.access import AccessPrincipal


def saved(settings: Settings, payload: dict[str, Any], at: str | None = None) -> FormalReport:
    result = run_frozen_decision_case(
        settings, payload, clock=GovernanceClock(at or payload["knowledge_cutoff"])
    )
    assert result.report is not None
    return result.report


def statistical_payload(
    settings: Settings, source: FormalReport, *, kind: str = "DAILY_CLOSE"
) -> dict[str, Any]:
    payload = monitoring_payload(settings, source.event_id)
    payload["version_bundle"].update(
        case_contract_version="monitoring.1.1.0",
        host_contract_version="monitoring.1.1.0",
        report_projection_contract_version="monitoring.1.1.0",
    )
    payload["monitoring"]["contract_version"] = "1.1.0"
    cutoff = source.knowledge_cutoff
    end = (datetime.fromisoformat(cutoff) + timedelta(days=1)).isoformat()
    start = (datetime.fromisoformat(cutoff) + timedelta(hours=16)).isoformat()
    payload["knowledge_cutoff"] = cutoff
    command = payload["monitoring"]
    command.update(cutoff_at=cutoff, kind=kind)
    command["calendar"] = {
        "version_id": "synthetic-session-calendar-v1",
        "market_date": cutoff[:10],
        "is_trading_day": True,
        "close_at": cutoff,
        "next_window_start": start,
        "next_window_end": end,
        "evidence": position_evidence("synthetic-calendar", cutoff_at=cutoff),
    }
    for family in command["evidence_families"]:
        family["evidence"] = position_evidence("synthetic-monitoring-family", cutoff_at=cutoff)
    return payload


def test_saved_reduction_delivers_version_bound_p2_with_original_quantity_and_window(
    migrated_settings: Settings,
) -> None:
    source = saved(
        migrated_settings,
        action_payload(migrated_settings, points=("0.65", "0.55", "0.45", "0.35", "0.50")),
    )
    report = saved(migrated_settings, statistical_payload(migrated_settings, source))
    assert report.result.monitoring is not None
    monitoring = report.result.monitoring
    assert monitoring is not None and monitoring.disposition == "ASSESSED"
    assert len(monitoring.cases) == 1
    item = monitoring.cases[0]
    assert item.priority == "P2"
    assert item.source_event_id == source.event_id
    assert item.conclusion_report_id == source.report_version_id
    assert item.direction == "REDUCE"
    assert item.target_quantity == Decimal("100")
    assert item.window_end is not None
    assert item.qualification_id
    assert monitoring.position_action == source.result.position_action
    assert source.report_version_id in monitoring.source_report_ids


def notification_payload(settings: Settings, source: FormalReport) -> dict[str, Any]:
    payload = statistical_payload(settings, source, kind="NOTIFICATION_RUN")
    payload["monitoring"].pop("calendar")
    payload["monitoring"]["notification"] = {
        "identity": "synthetic-statistical-message",
        "routing_version": "synthetic-routing-alpha",
        "quiet_until": None,
        "immediate_result": "REJECTED",
        "persistent_result": "REJECTED",
    }
    return payload


def principal() -> AccessPrincipal:
    return AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017", "synthetic-account-8029"),
        permissions=("REPORT_READ",),
    )


def test_expiry_stops_inbox_and_notification_retries_without_rewriting_history(
    migrated_settings: Settings,
) -> None:
    source = saved(
        migrated_settings,
        action_payload(migrated_settings, points=("0.65", "0.55", "0.45", "0.35", "0.50")),
    )
    report = saved(migrated_settings, statistical_payload(migrated_settings, source))
    assert report.result.monitoring is not None
    assert report.result.monitoring is not None
    request = notification_payload(migrated_settings, report)
    first = saved(migrated_settings, request)
    assert first.result.monitoring is not None and first.result.monitoring.notifications
    end = "2042-05-19T16:00:00Z"
    delivery = ResultDelivery.from_settings(migrated_settings, clock=GovernanceClock(end))
    workspace = delivery.monitoring_workspace(principal())
    assert workspace is not None
    assert workspace.inbox == ()
    assert delivery.read_report(report.report_version_id, principal()) == report
    request["monitoring"]["notification"].update(attempt_number=2, previous_event_id=first.event_id)
    retry = saved(migrated_settings, request, end)
    assert retry.result.monitoring is not None
    assert retry.result.monitoring.notifications == ()
    assert retry.result.monitoring.notification_due_at is None


def test_notification_retries_reuse_message_and_case_and_never_repeat_an_accepted_reminder(
    migrated_settings: Settings,
) -> None:
    source = saved(
        migrated_settings,
        action_payload(migrated_settings, points=("0.65", "0.55", "0.45", "0.35", "0.50")),
    )
    report = saved(migrated_settings, statistical_payload(migrated_settings, source))
    assert report.result.monitoring is not None
    request = notification_payload(migrated_settings, report)
    first = saved(migrated_settings, request)
    assert first.result.monitoring is not None
    request["monitoring"]["notification"].update(
        attempt_number=2, previous_event_id=first.event_id, immediate_result="ACCEPTED"
    )
    second = saved(migrated_settings, request, "2042-05-18T16:01:00Z")
    assert second.result.monitoring is not None
    assert (
        len(first.result.monitoring.notifications)
        == len(second.result.monitoring.notifications)
        == 1
    )
    a, b = first.result.monitoring.notifications[0], second.result.monitoring.notifications[0]
    assert a.case_id == b.case_id
    assert a.notification_id == b.notification_id
    assert a.attempt_id != b.attempt_id
    request["monitoring"]["notification"].update(
        identity="synthetic-rekey", attempt_number=1, previous_event_id=None
    )
    third = saved(migrated_settings, request)
    assert third.result.monitoring is not None and third.result.monitoring.notifications == ()


@pytest.mark.parametrize("mode", ["hold", "abstain", "observation"])
def test_non_interrupting_conclusions_remain_in_normal_view(
    migrated_settings: Settings, mode: str
) -> None:
    points = (
        ("0.20", "0.15", "0.10", "0.05", "0.75")
        if mode == "hold"
        else ("0.40", "0.30", "0.25", "0.20", "0.50")
    )
    payload = action_payload(
        migrated_settings, points=points, missing_downside=mode == "observation"
    )
    source = saved(migrated_settings, payload)
    report = saved(migrated_settings, statistical_payload(migrated_settings, source))
    assert report.result.monitoring is not None
    monitoring = report.result.monitoring
    assert monitoring is not None and monitoring.position_action == source.result.position_action
    assert monitoring.cases == ()


@pytest.mark.parametrize("gate", ["legal", "risk"])
def test_deterministic_protection_keeps_its_priority_after_statistical_expiry(
    migrated_settings: Settings, gate: str
) -> None:
    source = saved(
        migrated_settings,
        action_payload(
            migrated_settings,
            normal=gate == "legal",
            gate="legal" if gate == "legal" else None,
            points=("0.65", "0.55", "0.45", "0.35", "0.50"),
        ),
    )
    report = saved(migrated_settings, statistical_payload(migrated_settings, source))
    assert report.result.monitoring is not None
    assert report.result.monitoring is not None
    assert any(
        item.priority == ("P0" if gate == "legal" else "P1")
        for item in report.result.monitoring.cases
    )
    delivery = ResultDelivery.from_settings(
        migrated_settings, clock=GovernanceClock("2042-05-20T16:00:00Z")
    )
    workspace = delivery.monitoring_workspace(principal())
    assert workspace is not None
    assert workspace.inbox and all(item.priority != "P2" for item in workspace.inbox)


def test_new_event_assessment_replaces_p2_without_rehalving_or_rebinding_old_window(
    migrated_settings: Settings,
) -> None:
    original_payload = action_payload(
        migrated_settings, points=("0.65", "0.55", "0.45", "0.35", "0.50")
    )
    source = saved(migrated_settings, original_payload)
    report = saved(migrated_settings, statistical_payload(migrated_settings, source))
    assert report.result.monitoring is not None
    later_payload = action_payload(
        migrated_settings,
        cutoff_at="2042-05-19T16:00:00Z",
        previous=saved_assessment(migrated_settings, original_payload),
        points=("0.65", "0.55", "0.45", "0.35", "0.50"),
    )
    later_source = saved(migrated_settings, later_payload)
    request = statistical_payload(migrated_settings, later_source, kind="EVENT_REASSESS")
    request["monitoring"]["events"] = [
        {
            "event_id": "synthetic-authoritative-account-update",
            "kind": "ACCOUNT_STATE",
            "authority": "BROKER",
            "security_ids": ["XQZ-4017"],
            "evidence": position_evidence(
                "synthetic-broker", cutoff_at=later_source.knowledge_cutoff
            ),
        }
    ]
    later = saved(migrated_settings, request)
    assert report.result.monitoring is not None and later.result.monitoring is not None
    a, b = report.result.monitoring.cases[-1], later.result.monitoring.cases[-1]
    assert a.case_id != b.case_id
    assert a.target_quantity == b.target_quantity == Decimal("100")
    assert a.conclusion_report_id != b.conclusion_report_id
    delivery = ResultDelivery.from_settings(
        migrated_settings, clock=GovernanceClock(later.knowledge_cutoff)
    )
    workspace = delivery.monitoring_workspace(principal())
    assert workspace is not None and a.case_id in workspace.inactive_statistical_case_ids
    assert delivery.read_report(report.report_version_id, principal()) == report
    stale = statistical_payload(migrated_settings, source, kind="EVENT_REASSESS")
    stale["monitoring"]["events"] = request["monitoring"]["events"]
    stale["monitoring"]["events"][0]["event_id"] = "synthetic-stale-rebinding"
    stale["monitoring"]["calendar"]["next_window_end"] = "2042-05-25T16:00:00Z"
    blocked = saved(migrated_settings, stale)
    assert blocked.result.monitoring is not None
    assert blocked.result.monitoring.disposition == "BLOCKED"
    assert all(item.priority != "P2" for item in blocked.result.monitoring.cases)


def saved_assessment(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    delivery = ResultDelivery.from_settings(settings)
    source = delivery.report_input_case(payload["position_action"]["security_event_id"])
    assert source is not None and source.exit_assessment is not None
    return source.exit_assessment.model_dump(mode="json")


@pytest.mark.parametrize("defect", ["extend", "calendar", "missing", "reuse"])
def test_statistical_delivery_cannot_invent_a_new_window_from_an_old_conclusion(
    migrated_settings: Settings, defect: str
) -> None:
    source = saved(
        migrated_settings,
        action_payload(migrated_settings, points=("0.65", "0.55", "0.45", "0.35", "0.50")),
    )
    payload = statistical_payload(migrated_settings, source)
    if defect == "reuse":
        saved(migrated_settings, payload)
        payload["monitoring"].update(
            kind="EVENT_REASSESS",
            events=[
                {
                    "event_id": "synthetic-reused-old-conclusion",
                    "kind": "ACCOUNT_STATE",
                    "authority": "BROKER",
                    "evidence": position_evidence(
                        "synthetic-broker", cutoff_at=source.knowledge_cutoff
                    ),
                }
            ],
        )
    if defect in {"extend", "reuse"}:
        payload["monitoring"]["calendar"]["next_window_end"] = "2042-05-24T16:00:00Z"
    elif defect == "calendar":
        payload["monitoring"]["calendar"]["version_id"] = "synthetic-unrelated-calendar"
    else:
        payload["monitoring"]["calendar"].update(next_window_start=None, next_window_end=None)
    report = saved(migrated_settings, payload)
    assert report.result.monitoring is not None
    assert report.result.monitoring.disposition == "BLOCKED"
    assert all(item.priority != "P2" for item in report.result.monitoring.cases)


def test_confirmation_and_declaration_have_independent_clocks_and_do_not_extend_p2(
    migrated_settings: Settings,
) -> None:
    from stock_profiler.modules.delivery.user_facts import UserFactRequest

    source = saved(
        migrated_settings,
        action_payload(migrated_settings, points=("0.65", "0.55", "0.45", "0.35", "0.50")),
    )
    report = saved(
        migrated_settings, statistical_payload(migrated_settings, source), "2042-05-18T16:05:00Z"
    )
    actor = principal().model_copy(update={"permissions": ("REPORT_READ", "USER_FACT")})
    delivery = ResultDelivery.from_settings(
        migrated_settings, clock=GovernanceClock("2042-05-19T08:00:00Z")
    )
    for choice in ("ACCEPT", "DECLINE", "DEFER"):
        fact = delivery.record_user_fact(
            report.report_version_id,
            actor,
            UserFactRequest(kind="CONFIRMED", choice=choice, idempotency_key=f"synthetic-{choice}"),
        )
        assert fact is not None and fact.authoritative_execution is False
    declaration = delivery.record_user_fact(
        report.report_version_id,
        actor,
        UserFactRequest(
            kind="EXECUTION_DECLARED", declaration="UNABLE", idempotency_key="synthetic-unable"
        ),
    )
    assert declaration is not None and declaration.reconciliation_status == "PENDING"
    assert report.result.monitoring is not None and report.result.monitoring.freshness is not None
    assert (
        report.result.monitoring.freshness.evidence_cutoff.isoformat()
        == "2042-05-18T16:00:00+00:00"
    )
    assert report.result.monitoring.freshness.assessed_at == "2042-05-18T16:05:00Z"
    assert declaration.recorded_at == "2042-05-19T08:00:00Z"
    expired = ResultDelivery.from_settings(
        migrated_settings, clock=GovernanceClock("2042-05-19T16:00:00Z")
    )
    assert (
        expired.record_user_fact(
            report.report_version_id,
            actor,
            UserFactRequest(kind="CONFIRMED", choice="ACCEPT", idempotency_key="synthetic-expired"),
        )
        is None
    )
    assert expired.read_report(report.report_version_id, actor) == report
    workspace = expired.monitoring_workspace(actor)
    assert workspace is not None and workspace.inbox == () and len(workspace.user_facts) == 4


@pytest.mark.parametrize("quiet_until", ["2042-05-19T09:00:00Z", "2042-05-19T16:00:00Z"])
def test_p2_respects_quiet_hours_without_extending_the_window(
    migrated_settings: Settings,
    quiet_until: str,
) -> None:
    source = saved(
        migrated_settings,
        action_payload(migrated_settings, points=("0.65", "0.55", "0.45", "0.35", "0.50")),
    )
    report = saved(migrated_settings, statistical_payload(migrated_settings, source))
    assert report.result.monitoring is not None
    request = notification_payload(migrated_settings, report)
    request["monitoring"]["notification"].update(
        quiet_until=quiet_until, immediate_result="ACCEPTED"
    )
    deferred = saved(migrated_settings, request)
    assert deferred.result.monitoring is not None and deferred.result.monitoring.notifications == ()
    if quiet_until.endswith("09:00:00Z"):
        assert deferred.result.monitoring.notification_due_at is not None
        request["monitoring"]["notification"].update(
            attempt_number=2, previous_event_id=deferred.event_id
        )
        delivered = saved(migrated_settings, request, quiet_until)
        assert (
            delivered.result.monitoring is not None
            and len(delivered.result.monitoring.notifications) == 1
        )
        assert delivered.result.monitoring.cases == report.result.monitoring.cases
    else:
        assert deferred.result.monitoring.notification_due_at is None


def test_lifecycle_and_operations_keep_the_exact_statistical_conclusion_version(
    migrated_settings: Settings,
) -> None:
    source_payload = action_payload(
        migrated_settings, points=("0.65", "0.55", "0.45", "0.35", "0.50")
    )
    source = saved(migrated_settings, source_payload)
    report = saved(migrated_settings, statistical_payload(migrated_settings, source))
    assert report.result.monitoring is not None
    notified = saved(migrated_settings, notification_payload(migrated_settings, report))
    request = statistical_payload(migrated_settings, report, kind="LIFECYCLE")
    request["monitoring"]["reconciliation_event_id"] = source_payload["position_action"][
        "execution"
    ]["concentration_event_id"]
    lifecycle = saved(migrated_settings, request)
    request["monitoring"].update(
        kind="OPERATIONS",
        reconciliation_event_id=None,
        planned_trading_dates=[
            (datetime(2042, 5, 19) - timedelta(days=offset)).date().isoformat()
            for offset in reversed(range(20))
        ],
    )
    operations = saved(migrated_settings, request)
    for projection in (report, notified, lifecycle, operations):
        assert projection.result.monitoring is not None
        assert projection.result.monitoring.conclusion_report_id == source.report_version_id
        assert projection.result.monitoring.position_action == source.result.position_action
        assert projection.result.monitoring.cases == report.result.monitoring.cases
    assert operations.result.monitoring is not None and notified.result.monitoring is not None
    assert operations.result.monitoring.notifications == notified.result.monitoring.notifications


@pytest.mark.parametrize("gate", ["legal", "risk"])
@pytest.mark.parametrize("missing", ["window", "family", "calendar"])
def test_unknown_statistical_window_does_not_remove_deterministic_obligations(
    migrated_settings: Settings,
    gate: str,
    missing: str,
) -> None:
    source = saved(
        migrated_settings,
        action_payload(
            migrated_settings,
            normal=gate == "legal",
            gate="legal" if gate == "legal" else None,
            points=("0.65", "0.55", "0.45", "0.35", "0.50"),
        ),
    )
    request = statistical_payload(migrated_settings, source)
    if missing == "window":
        request["monitoring"]["calendar"].update(next_window_start=None, next_window_end=None)
    elif missing == "family":
        request["monitoring"]["evidence_families"] = []
    else:
        request["monitoring"].pop("calendar")
    report = saved(migrated_settings, request)
    assert report.result.monitoring is not None
    assert report.result.monitoring.disposition == "BLOCKED"
    assert report.result.monitoring.cases and all(
        item.priority != "P2" for item in report.result.monitoring.cases
    )


def test_shadow_statistical_reports_cannot_enter_user_delivery_or_notification(
    migrated_settings: Settings,
) -> None:
    source = saved(
        migrated_settings,
        action_payload(migrated_settings, points=("0.65", "0.55", "0.45", "0.35", "0.50")),
    )
    request = statistical_payload(migrated_settings, source)
    request["access_scope"]["visibility"] = "SHADOW"
    shadow = run_frozen_decision_case(
        migrated_settings, request, clock=GovernanceClock(source.knowledge_cutoff)
    )
    assert shadow.publication_status == "CLOSED" and shadow.report is None
    delivery = ResultDelivery.from_settings(
        migrated_settings, clock=GovernanceClock(source.knowledge_cutoff)
    )
    assert delivery.read_report(shadow.report_version_id, principal()) is None
    workspace = delivery.monitoring_workspace(principal())
    assert workspace is not None and workspace.reports == () and workspace.inbox == ()
    notification = notification_payload(migrated_settings, source)
    notification["monitoring"]["source_event_id"] = shadow.decision_event_id
    leak = saved(migrated_settings, notification)
    assert leak.result.monitoring is not None and leak.result.monitoring.notifications == ()
    assert leak.result.monitoring.position_action is None


def test_suspended_qualification_removes_p2_from_every_action_surface(
    migrated_settings: Settings,
) -> None:
    from test_scoped_qualification import case_payload, qualification_command

    from stock_profiler.modules.delivery.user_facts import UserFactRequest

    original = action_payload(migrated_settings, points=("0.65", "0.55", "0.45", "0.35", "0.50"))
    source = saved(migrated_settings, original)
    report = saved(migrated_settings, statistical_payload(migrated_settings, source))
    assert report.result.monitoring is not None
    action = original["position_action"]
    command = qualification_command(
        migrated_settings, action="SUSPEND", previous=action["qualification_id"]
    )
    command.update(scope=action["qualification_scope"], version=action["policy"]["version"])
    command["evidence"].update(
        scope=command["scope"],
        version=command["version"],
        kind="REQUIRED_PREMISE_UNVERIFIABLE",
        evidence_id="synthetic-action-qualification-suspension",
    )
    restriction = case_payload(
        migrated_settings, "statistical-suspension", command, contract_version="5.0.0"
    )
    restriction["access_scope"] = original["access_scope"]
    suspension = saved(migrated_settings, restriction, "2042-05-18T16:06:00Z")
    assert (
        suspension.result.governance is not None
        and suspension.result.governance.disposition == "APPROVED"
    )
    actor = principal().model_copy(update={"permissions": ("REPORT_READ", "USER_FACT")})
    delivery = ResultDelivery.from_settings(
        migrated_settings, clock=GovernanceClock("2042-05-18T16:07:00Z")
    )
    workspace = delivery.monitoring_workspace(actor)
    assert workspace is not None and workspace.inbox == ()
    assert (
        delivery.record_user_fact(
            report.report_version_id,
            actor,
            UserFactRequest(
                kind="CONFIRMED", choice="ACCEPT", idempotency_key="synthetic-suspended"
            ),
        )
        is None
    )
    notified = saved(
        migrated_settings, notification_payload(migrated_settings, report), "2042-05-18T16:07:00Z"
    )
    assert notified.result.monitoring is not None and notified.result.monitoring.notifications == ()
    assert delivery.read_report(report.report_version_id, actor) == report


def test_statistical_exit_has_zero_target_and_late_publication_does_not_revive_it(
    migrated_settings: Settings,
) -> None:
    source = saved(
        migrated_settings,
        action_payload(migrated_settings, points=("0.20", "0.15", "0.10", "0.05", "0.26")),
    )
    report = saved(
        migrated_settings, statistical_payload(migrated_settings, source), "2042-05-20T08:00:00Z"
    )
    assert report.result.monitoring is not None
    item = report.result.monitoring.cases[0]
    assert item.priority == "P2" and item.direction == "EXIT" and item.target_quantity == 0
    notified = saved(
        migrated_settings, notification_payload(migrated_settings, report), "2042-05-20T08:01:00Z"
    )
    assert notified.result.monitoring is not None and notified.result.monitoring.notifications == ()
    delivery = ResultDelivery.from_settings(
        migrated_settings, clock=GovernanceClock("2042-05-20T08:01:00Z")
    )
    workspace = delivery.monitoring_workspace(principal())
    assert workspace is not None and workspace.inbox == ()
    assert delivery.read_report(report.report_version_id, principal()) == report


@pytest.mark.parametrize("dependency", ["cost", "rules"])
def test_original_required_execution_evidence_expiry_closes_p2_even_with_fresh_envelopes(
    migrated_settings: Settings,
    dependency: str,
) -> None:
    from stock_profiler.modules.delivery.user_facts import UserFactRequest

    payload = action_payload(migrated_settings, points=("0.65", "0.55", "0.45", "0.35", "0.50"))
    for route in payload["position_action"]["execution"]["routes"]:
        evidence = (
            route["cost_curve"]["evidence"] if dependency == "cost" else route["rules_evidence"]
        )
        evidence["expires_at"] = "2042-05-19T09:00:00Z"
    source = saved(migrated_settings, payload)
    report = saved(migrated_settings, statistical_payload(migrated_settings, source))
    actor = principal().model_copy(update={"permissions": ("REPORT_READ", "USER_FACT")})
    delivery = ResultDelivery.from_settings(
        migrated_settings, clock=GovernanceClock("2042-05-19T09:00:00Z")
    )
    workspace = delivery.monitoring_workspace(actor)
    assert workspace is not None and workspace.inbox == ()
    assert (
        delivery.record_user_fact(
            report.report_version_id,
            actor,
            UserFactRequest(
                kind="CONFIRMED", choice="ACCEPT", idempotency_key="synthetic-expired-dependency"
            ),
        )
        is None
    )
    notification = saved(
        migrated_settings, notification_payload(migrated_settings, report), "2042-05-19T09:00:00Z"
    )
    assert (
        notification.result.monitoring is not None
        and notification.result.monitoring.notifications == ()
    )


def test_historical_hard_liquidation_is_delivered_on_first_later_monitoring(
    migrated_settings: Settings,
) -> None:
    payload = action_payload(migrated_settings, gate="legal")
    first = saved(migrated_settings, payload)
    assessment = saved_assessment(migrated_settings, payload)
    assessment["evidence"][0]["legal_termination"] = False
    later_payload = action_payload(
        migrated_settings, cutoff_at="2042-05-19T16:00:00Z", previous=assessment
    )
    later_source = saved(migrated_settings, later_payload)
    assert (
        first.result.position_action is not None and later_source.result.position_action is not None
    )
    assert later_source.result.position_action.direction == "EXIT"
    report = saved(migrated_settings, statistical_payload(migrated_settings, later_source))
    assert report.result.monitoring is not None
    assert any(
        item.priority == "P0" and item.target_quantity == 0
        for item in report.result.monitoring.cases
    )


def test_deterministic_confirmation_does_not_need_statistical_qualification(
    migrated_settings: Settings,
) -> None:
    from stock_profiler.modules.delivery.user_facts import UserFactRequest

    payload = action_payload(migrated_settings, gate="legal")
    payload["position_action"]["qualification_id"] = (
        "synthetic-unavailable-statistical-qualification"
    )
    source = saved(migrated_settings, payload)
    report = saved(migrated_settings, statistical_payload(migrated_settings, source))
    actor = principal().model_copy(update={"permissions": ("REPORT_READ", "USER_FACT")})
    delivery = ResultDelivery.from_settings(
        migrated_settings, clock=GovernanceClock("2042-05-19T08:00:00Z")
    )
    assert report.result.monitoring is not None and all(
        item.priority != "P2" for item in report.result.monitoring.cases
    )
    assert (
        delivery.record_user_fact(
            report.report_version_id,
            actor,
            UserFactRequest(
                kind="CONFIRMED", choice="ACCEPT", idempotency_key="synthetic-protection-confirmed"
            ),
        )
        is not None
    )
