from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_authenticated_report_delivery import ORIGIN, _authenticated_client_with_csrf
from test_execution_plans import (
    committed,
    execution_payload,
    execution_routes,
    risk_handoff_payload,
)
from test_position_state_reconciliation import position_evidence
from test_scoped_qualification import GovernanceClock

from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.entrypoints.http.app import create_app
from stock_profiler.modules.delivery.access import AccessPrincipal
from stock_profiler.modules.delivery.user_facts import UserFactRequest


def monitoring_payload(settings: Settings, source_event_id: str) -> dict[str, Any]:
    payload = execution_payload(settings)
    del payload["execution_plan"]
    payload["case_id"] = "synthetic-monitoring-close"
    payload["business_identity"] = "synthetic-monitoring-close"
    payload["version_bundle"].update(
        case_contract_version="monitoring.1.0.0",
        host_contract_version="monitoring.1.0.0",
        report_projection_contract_version="monitoring.1.0.0",
    )
    payload["monitoring"] = {
        "contract_version": "1.0.0",
        "operation": "MONITOR_ASSESS",
        "portfolio_id": "synthetic-decision-portfolio-alpha",
        "cutoff_at": payload["knowledge_cutoff"],
        "kind": "DAILY_CLOSE",
        "source_event_id": source_event_id,
    }
    return payload


def ready_monitoring_payload(settings: Settings) -> dict[str, Any]:
    plan_payload = risk_handoff_payload(settings)
    plan_payload["execution_plan"]["routes"] = execution_routes()
    source = committed(settings, plan_payload)
    payload = monitoring_payload(settings, source.event_id)
    payload["monitoring"]["calendar"] = {
        "version_id": "synthetic-market-calendar-v1",
        "market_date": "2042-05-18",
        "is_trading_day": True,
        "close_at": payload["knowledge_cutoff"],
        "next_window_start": "2042-05-18T16:00:00Z",
        "next_window_end": "2042-05-19T07:00:00Z",
        "evidence": position_evidence("synthetic-calendar"),
    }
    return payload


def test_monitoring_requires_a_saved_execution_handoff(migrated_settings: Settings) -> None:
    report = committed(
        migrated_settings, monitoring_payload(migrated_settings, "synthetic-missing-event")
    )
    monitoring = report.result.monitoring
    assert monitoring is not None
    assert monitoring.disposition == "BLOCKED"
    assert monitoring.reasons == ("MONITORING_HANDOFF_UNAVAILABLE",)
    assert monitoring.cases == ()


def test_daily_close_projects_saved_targets_and_has_one_identity_per_market_day(
    migrated_settings: Settings,
) -> None:
    plan_payload = risk_handoff_payload(migrated_settings)
    plan_payload["execution_plan"]["routes"] = execution_routes()
    source = committed(migrated_settings, plan_payload)
    payload = monitoring_payload(migrated_settings, source.event_id)
    payload["monitoring"]["calendar"] = {
        "version_id": "synthetic-market-calendar-v1",
        "market_date": "2042-05-18",
        "is_trading_day": True,
        "close_at": payload["knowledge_cutoff"],
        "next_window_start": "2042-05-18T16:00:00Z",
        "next_window_end": "2042-05-19T07:00:00Z",
        "evidence": position_evidence("synthetic-calendar"),
    }
    report = committed(migrated_settings, payload)
    monitoring = report.result.monitoring
    assert monitoring is not None and monitoring.disposition == "ASSESSED"
    assert len(monitoring.cases) == 1
    case = monitoring.cases[0]
    assert case.priority == "P1"
    assert case.plan == source.result.execution_plan
    assert case.source_event_id == source.event_id
    assert case.first_established_at == "2042-05-17T16:00:00Z"
    assert len(monitoring.action_units) == 2
    retry = deepcopy(payload)
    retry["business_identity"] += ":another-scheduler-attempt"
    retry["case_id"] += "-retry"
    assert committed(migrated_settings, retry).report_version_id == report.report_version_id


@pytest.mark.parametrize("variation", ["scope-order", "incidental-fields", "missing-calendar"])
def test_daily_retry_cannot_change_identity_through_incidental_input(
    migrated_settings: Settings,
    variation: str,
) -> None:
    payload = ready_monitoring_payload(migrated_settings)
    original = committed(migrated_settings, payload)
    retry = deepcopy(payload)
    retry["business_identity"] += ":retry"
    retry["case_id"] += "-retry"
    if variation == "scope-order":
        retry["access_scope"]["account_ids"].reverse()
    elif variation == "incidental-fields":
        retry["monitoring"]["reconciliation_event_id"] = "synthetic-unrelated-event"
    else:
        del retry["monitoring"]["calendar"]
    assert committed(migrated_settings, retry).report_version_id == original.report_version_id


def test_authoritative_event_reassessment_keeps_case_identity_during_market_closure(
    migrated_settings: Settings,
) -> None:
    payload = ready_monitoring_payload(migrated_settings)
    original = committed(migrated_settings, payload)
    payload["monitoring"].update(
        kind="EVENT_REASSESS",
        events=[
            {
                "event_id": "synthetic-sellability-event",
                "kind": "ACCOUNT_STATE",
                "authority": "BROKER",
                "evidence": position_evidence("synthetic-broker-event"),
            }
        ],
    )
    payload["monitoring"]["calendar"]["is_trading_day"] = False
    event = committed(migrated_settings, payload)
    assert event.event_id != original.event_id
    assert event.result.monitoring is not None and original.result.monitoring is not None
    assert event.result.monitoring.disposition == "ASSESSED"
    assert event.result.monitoring.cases[0].case_id == original.result.monitoring.cases[0].case_id
    assert event.result.monitoring.freshness is not None
    assert event.result.monitoring.freshness.next_window_start is not None
    assert event.result.monitoring.cases[0].plan is not None
    assert event.result.monitoring.freshness.market_status == "CLOSED"
    assert event.result.monitoring.freshness.next_window_start.isoformat().startswith("2042-05-18")
    assert event.result.monitoring.cases[0].plan.risk_restored is False


@pytest.mark.parametrize("defect", ["ordinary-news", "late-validation", "closed-day"])
def test_invalid_event_or_daily_calendar_cannot_publish_new_monitoring_action(
    migrated_settings: Settings,
    defect: str,
) -> None:
    payload = ready_monitoring_payload(migrated_settings)
    if defect == "closed-day":
        payload["monitoring"]["calendar"]["is_trading_day"] = False
    else:
        payload["monitoring"].update(
            kind="EVENT_REASSESS",
            events=[
                {
                    "event_id": "synthetic-unqualified-event",
                    "kind": "ACCOUNT_STATE",
                    "authority": "NEWS" if defect == "ordinary-news" else "BROKER",
                    "evidence": position_evidence("synthetic-event"),
                }
            ],
        )
        if defect == "late-validation":
            payload["monitoring"]["events"][0]["evidence"]["validated_at"] = "2042-05-18T17:00:00Z"
    report = committed(migrated_settings, payload)
    assert report.result.monitoring is not None
    assert report.result.monitoring.disposition == "BLOCKED"
    assert report.result.monitoring.cases == ()


def test_later_missing_evidence_keeps_original_obligation_without_reusing_old_quantities(
    migrated_settings: Settings,
) -> None:
    payload = ready_monitoring_payload(migrated_settings)
    original = committed(migrated_settings, payload)
    payload["knowledge_cutoff"] = "2042-05-18T16:00:00Z"
    payload["monitoring"]["cutoff_at"] = payload["knowledge_cutoff"]
    payload["monitoring"]["calendar"]["market_date"] = "2042-05-19"
    payload["monitoring"]["source_event_id"] = "synthetic-missing-new-handoff"
    later = committed(migrated_settings, payload)
    assert later.result.monitoring is not None and original.result.monitoring is not None
    assert later.result.monitoring.disposition == "BLOCKED"
    old = original.result.monitoring.cases[0]
    retained = later.result.monitoring.cases[0]
    assert retained.case_id == old.case_id
    assert retained.first_established_at == old.first_established_at
    assert retained.last_reviewed_at == old.last_reviewed_at
    assert retained.quantity_status == "UNKNOWN"
    assert retained.plan is None
    assert retained.obligation_ids == old.obligation_ids


def test_authoritative_termination_protects_without_a_fresh_quantity_plan(
    migrated_settings: Settings,
) -> None:
    payload = ready_monitoring_payload(migrated_settings)
    original = committed(migrated_settings, payload)
    payload["knowledge_cutoff"] = "2042-05-18T16:00:00Z"
    payload["monitoring"].update(
        cutoff_at=payload["knowledge_cutoff"],
        kind="EVENT_REASSESS",
        source_event_id=original.event_id,
        events=[
            {
                "event_id": "synthetic-termination-event",
                "kind": "TERMINATION",
                "authority": "EXCHANGE",
                "evidence": position_evidence("synthetic-exchange-event"),
            }
        ],
    )
    payload["monitoring"]["calendar"]["is_trading_day"] = False
    payload["monitoring"]["calendar"]["evidence"]["complete_through_at"] = None
    report = committed(migrated_settings, payload)
    monitoring = report.result.monitoring
    assert monitoring is not None and monitoring.disposition == "ASSESSED"
    assert len(monitoring.cases) == 1
    assert original.result.monitoring is not None
    assert monitoring.cases[0].case_id == original.result.monitoring.cases[0].case_id
    assert monitoring.cases[0].priority == "P0"
    assert monitoring.cases[0].quantity_status == "UNKNOWN"
    assert monitoring.cases[0].plan is None
    assert monitoring.action_units == ()
    assert monitoring.freshness is not None
    assert monitoring.freshness.market_status == "CLOSED"
    payload["monitoring"].update(
        kind="NOTIFICATION_RUN",
        source_event_id=report.event_id,
        notification={
            "identity": "synthetic-p0-notification",
            "routing_version": "synthetic-routing-v1",
            "quiet_until": "2042-05-20T16:00:00Z",
            "immediate_result": "ACCEPTED",
            "persistent_result": "REJECTED",
        },
    )
    notified = committed(migrated_settings, payload)
    assert notified.result.monitoring is not None
    assert [attempt.role for attempt in notified.result.monitoring.notifications] == [
        "IMMEDIATE",
        "PERSISTENT",
    ]


def test_workspace_reads_only_published_authorized_reports_and_keeps_independent_facts(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = ready_monitoring_payload(migrated_settings)
    report = committed(migrated_settings, payload)
    assert TestClient(create_app(migrated_settings)).get("/api/v1/monitoring").status_code == 401
    settings = migrated_settings.model_copy(
        update={
            "report_account_ids": ("synthetic-account-4017", "synthetic-account-8029"),
            "report_permissions": ("REPORT_READ", "USER_FACT"),
        }
    )
    client, csrf = _authenticated_client_with_csrf(settings, monkeypatch)
    response = client.get("/api/v1/monitoring")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["reports"] == [report.model_dump(mode="json")]
    assert response.json()["current_report_ids"] == [report.report_version_id]
    assert len(response.json()["inbox"]) == 1
    for kind, extra in (
        ("VIEWED", {}),
        ("ACKNOWLEDGED", {}),
        ("EXECUTION_DECLARED", {"declaration": "REPORTED_FILLED"}),
    ):
        saved = client.post(
            f"/api/v1/reports/{report.report_version_id}/facts",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
            json={"kind": kind, "idempotency_key": f"synthetic-{kind}", **extra},
        )
        assert saved.status_code == 200
    after = client.get("/api/v1/monitoring").json()
    assert after["reports"] == response.json()["reports"]
    assert after["inbox"] == response.json()["inbox"]
    assert len(after["user_facts"]) == 3
    assert all(not fact["authoritative_execution"] for fact in after["user_facts"])


def test_notification_fallback_preserves_action_and_minimizes_external_content(
    migrated_settings: Settings,
) -> None:
    payload = ready_monitoring_payload(migrated_settings)
    original = committed(migrated_settings, payload)
    payload["monitoring"].update(
        kind="NOTIFICATION_RUN",
        source_event_id=original.event_id,
        notification={
            "identity": "synthetic-notification-1",
            "routing_version": "synthetic-routing-v1",
            "quiet_until": None,
            "immediate_result": "UNKNOWN",
            "persistent_result": "ACCEPTED",
        },
    )
    report = committed(migrated_settings, payload)
    monitoring = report.result.monitoring
    assert monitoring is not None and monitoring.disposition == "ASSESSED"
    assert original.result.monitoring is not None
    assert monitoring.cases == original.result.monitoring.cases
    assert tuple(attempt.role for attempt in monitoring.notifications) == (
        "IMMEDIATE",
        "PERSISTENT",
    )
    assert tuple(attempt.result for attempt in monitoring.notifications) == ("UNKNOWN", "ACCEPTED")
    body = monitoring.notifications[0].body
    assert "P1" in body and "/monitoring" in body
    assert "synthetic-account" not in body and "130" not in body
    assert all(attempt.delivered is None for attempt in monitoring.notifications)
    assert committed(migrated_settings, payload).report_version_id == report.report_version_id


def test_lifecycle_and_operations_reports_bind_saved_reconciliation_and_original_reports(
    migrated_settings: Settings,
) -> None:
    payload = ready_monitoring_payload(migrated_settings)
    original = committed(migrated_settings, payload)
    from test_issuer_concentration import concentration_payload
    from test_position_state_reconciliation import position_case_payload

    snapshot = concentration_payload(
        migrated_settings,
        "synthetic-unused-authorization",
        quantity="100",
    )["concentration"]["position_snapshot"]
    position = committed(
        migrated_settings,
        position_case_payload(migrated_settings, "synthetic-reconciliation", snapshot),
    )
    payload["monitoring"].update(
        kind="LIFECYCLE",
        source_event_id=original.event_id,
        reconciliation_event_id=position.event_id,
    )
    lifecycle = committed(migrated_settings, payload)
    assert lifecycle.result.monitoring is not None
    assert lifecycle.result.monitoring.reconciliation == position.result.position
    assert lifecycle.result.monitoring.source_report_ids == (
        original.report_version_id,
        position.report_version_id,
    )
    assert original.result.monitoring is not None
    assert lifecycle.result.monitoring.cases == original.result.monitoring.cases
    payload["monitoring"].update(kind="OPERATIONS", reconciliation_event_id=None)
    audit = committed(migrated_settings, payload)
    assert audit.result.monitoring is not None
    assert original.report_version_id in audit.result.monitoring.source_report_ids
    assert lifecycle.report_version_id in audit.result.monitoring.source_report_ids


def test_confirmation_requires_a_current_plan_but_never_discharges_the_obligation(
    migrated_settings: Settings,
) -> None:
    payload = ready_monitoring_payload(migrated_settings)
    original = committed(migrated_settings, payload)
    principal = AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017", "synthetic-account-8029"),
        permissions=("REPORT_READ", "USER_FACT"),
    )
    delivery = ResultDelivery.from_settings(migrated_settings, clock=GovernanceClock())
    request = UserFactRequest(kind="CONFIRMED", choice="ACCEPT", idempotency_key="synthetic-accept")
    saved = delivery.record_user_fact(original.report_version_id, principal, request)
    assert saved is not None
    assert delivery.record_user_fact(original.report_version_id, principal, request) == saved
    workspace = delivery.monitoring_workspace(principal)
    assert workspace is not None and original.result.monitoring is not None
    assert workspace.inbox == original.result.monitoring.cases
    payload["knowledge_cutoff"] = "2042-05-18T16:00:00Z"
    payload["monitoring"]["cutoff_at"] = payload["knowledge_cutoff"]
    payload["monitoring"]["calendar"]["market_date"] = "2042-05-19"
    payload["monitoring"]["source_event_id"] = "synthetic-missing-new-handoff"
    committed(migrated_settings, payload)
    assert delivery.record_user_fact(original.report_version_id, principal, request) == saved
    assert (
        delivery.record_user_fact(
            original.report_version_id,
            principal,
            UserFactRequest(
                kind="CONFIRMED", choice="ACCEPT", idempotency_key="synthetic-stale-accept"
            ),
        )
        is None
    )
