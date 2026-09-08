from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from test_execution_plans import execution_routes, risk_handoff_payload
from test_monitoring_workspace import monitoring_payload
from test_position_state_reconciliation import position_evidence
from test_scoped_qualification import GovernanceClock

from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
from stock_profiler.bootstrap.decision_cases import (
    get_formal_report,
    replay_default_frozen_decision_case,
    run_frozen_decision_case,
)
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
from stock_profiler.modules.delivery.access import AccessPrincipal
from stock_profiler.modules.delivery.user_facts import UserFactRequest


@pytest.mark.parametrize(
    "scenario,expected_priority,expected_target,expected_disposition",
    [
        ("normal", None, "200", "PLANNED"),
        ("buffer", None, "200", "PLANNED"),
        ("hard", "P1", "130", "PLANNED"),
        ("full-sale", "P1", "60", "RECONFIRMATION_REQUIRED"),
        ("cost-failure", "P1", "130", "BLOCKED"),
        ("p0-quiet", "P0", "0", "PLANNED"),
    ],
)
def test_frozen_protection_journey_rebuilds_without_releasing_risk(
    migrated_settings: Settings,
    tmp_path: Path,
    scenario: str,
    expected_priority: str | None,
    expected_target: str,
    expected_disposition: str,
) -> None:
    snapshots: list[dict[str, Any]] = []
    for repetition in range(2):
        settings = migrated_settings.model_copy(
            update={
                "app_database_url": f"sqlite:///{tmp_path / f'application-{repetition}.sqlite3'}",
                "m_agent_run_store_path": tmp_path / f"framework-{repetition}.sqlite3",
            }
        )
        configuration = Config("alembic.ini")
        configuration.set_main_option("sqlalchemy.url", settings.app_database_url)
        command.upgrade(configuration, "head")
        clock = GovernanceClock()
        payload = risk_handoff_payload(
            settings, normal=scenario == "normal", buffer=scenario == "buffer"
        )
        payload["execution_plan"]["routes"] = (
            [] if scenario == "cost-failure" else execution_routes()
        )
        if scenario == "full-sale":
            payload["execution_plan"]["established_targets"] = [
                {
                    "target_id": "synthetic-contract-reduction",
                    "security_id": "XQZ-4017",
                    "target_quantity": "60",
                    "direction": "REDUCE",
                    "qualified": True,
                    "policy_version": "synthetic-contract-target-v1",
                    "evidence": position_evidence("synthetic-contract-target"),
                }
            ]
            for route in payload["execution_plan"]["routes"]:
                route.update(minimum_quantity="100", quantity_increment="100")
        elif scenario == "p0-quiet":
            payload["execution_plan"]["established_targets"] = [
                {
                    "target_id": "synthetic-contract-termination",
                    "security_id": "XQZ-4017",
                    "target_quantity": "0",
                    "direction": "EXIT",
                    "qualified": True,
                    "priority": "P0",
                    "policy_version": "synthetic-contract-termination-v1",
                    "evidence": position_evidence("synthetic-contract-termination"),
                }
            ]
        execution = run_frozen_decision_case(settings, payload, clock=clock)
        plan_report = execution.report
        assert plan_report is not None and plan_report.result.execution_plan is not None
        plan = plan_report.result.execution_plan
        assert plan.disposition == expected_disposition
        assert plan.targets[0].target_quantity == Decimal(expected_target)
        assert plan.risk_restored is False
        assert plan.new_exposure_blocked is (scenario != "normal")
        assert run_frozen_decision_case(settings, payload, clock=clock) == execution

        monitor = monitoring_payload(settings, plan_report.event_id)
        monitor["monitoring"]["calendar"] = {
            "version_id": "synthetic-market-calendar-v1",
            "market_date": "2042-05-18",
            "is_trading_day": True,
            "close_at": monitor["knowledge_cutoff"],
            "next_window_start": "2042-05-18T16:00:00Z",
            "next_window_end": "2042-05-19T07:00:00Z",
            "evidence": position_evidence("synthetic-calendar"),
        }
        assessment = run_frozen_decision_case(settings, monitor, clock=clock)
        report = assessment.report
        assert report is not None and report.result.monitoring is not None
        assert report.synthetic
        assert report.qualification_scope == "D0_SYNTHETIC_CONTRACT_ONLY"
        assert report.monitoring_publication is not None
        cases = report.result.monitoring.cases
        assert [case.priority for case in cases] == (
            [] if expected_priority is None else [expected_priority]
        )
        principal = AccessPrincipal(
            user_id="stock-profiler-single-user",
            account_ids=("synthetic-account-4017", "synthetic-account-8029"),
            permissions=("REPORT_READ", "USER_FACT"),
        )
        assert get_formal_report(report.report_version_id, settings, principal=principal) == report
        assert (
            replay_default_frozen_decision_case(
                settings,
                monitor["business_identity"],
                clock=clock,
                recovery_case=FrozenDecisionCase.model_validate(monitor),
            ).report
            == report
        )
        delivery = ResultDelivery.from_settings(settings, clock=clock)
        if cases:
            for kind, extra in (
                ("VIEWED", {}),
                ("ACKNOWLEDGED", {}),
                ("EXECUTION_DECLARED", {"declaration": "REPORTED_FILLED"}),
            ):
                request = UserFactRequest.model_validate(
                    {"kind": kind, "idempotency_key": f"synthetic-journey-{kind}", **extra}
                )
                fact = delivery.record_user_fact(report.report_version_id, principal, request)
                assert fact is not None and not fact.authoritative_execution
                assert (
                    delivery.record_user_fact(report.report_version_id, principal, request) == fact
                )
            workspace = delivery.monitoring_workspace(principal)
            assert workspace is not None and workspace.inbox == cases

        notification = deepcopy(monitor)
        notification["monitoring"].update(
            kind="NOTIFICATION_RUN",
            source_event_id=report.event_id,
            notification={
                "identity": "synthetic-journey-notification",
                "routing_version": "synthetic-routing-v1",
                "quiet_until": "2042-05-18T17:00:00Z" if scenario == "p0-quiet" else None,
                "immediate_result": "ACCEPTED" if scenario == "p0-quiet" else "TIMEOUT",
                "persistent_result": "ACCEPTED",
            },
        )
        notified = run_frozen_decision_case(settings, notification, clock=clock).report
        assert notified is not None and notified.result.monitoring is not None
        assert notified.result.monitoring.cases == cases
        assert [attempt.role for attempt in notified.result.monitoring.notifications] == (
            ["IMMEDIATE", "PERSISTENT"] if cases else []
        )
        assert all(
            attempt.delivered is None
            and "XQZ" not in attempt.body
            and "synthetic-account" not in attempt.body
            for attempt in notified.result.monitoring.notifications
        )
        assert run_frozen_decision_case(settings, notification, clock=clock).report == notified
        audit_payload = deepcopy(monitor)
        audit_payload["monitoring"].update(
            kind="OPERATIONS",
            source_event_id=report.event_id,
            planned_trading_dates=[
                (date(2042, 5, 18) - timedelta(days=offset)).isoformat()
                for offset in reversed(range(20))
            ],
        )
        audit = run_frozen_decision_case(settings, audit_payload, clock=clock).report
        assert audit is not None and audit.result.monitoring is not None
        assert len(audit.result.monitoring.missing_daily_dates) == 19
        assert report.report_version_id in audit.result.monitoring.source_report_ids
        assert run_frozen_decision_case(settings, audit_payload, clock=clock).report == audit
        assert get_formal_report(report.report_version_id, settings, principal=principal) == report
        snapshots.append(
            {
                "plan": plan_report.model_dump(mode="json"),
                "assessment": report.model_dump(mode="json"),
                "notification": notified.model_dump(mode="json"),
                "audit": audit.model_dump(mode="json"),
            }
        )
    assert snapshots[0] == snapshots[1]
