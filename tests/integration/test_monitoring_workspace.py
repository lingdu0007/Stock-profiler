from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
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
from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.entrypoints.http.app import create_app
from stock_profiler.modules.delivery.access import AccessPrincipal
from stock_profiler.modules.delivery.monitoring_workspace import project_workspace
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
        "evidence_families": [
            {"family": family, "evidence": position_evidence(f"synthetic-{family.lower()}")}
            for family in (
                "MARKET_SECURITY",
                "COMPANY_EVENTS",
                "BROKER_ACCOUNT",
                "COST_RULES",
                "QUALIFICATION_VERSION",
            )
        ],
    }
    return payload


def ready_monitoring_payload(
    settings: Settings,
    *,
    protective_target: bool = False,
    normal: bool = False,
) -> dict[str, Any]:
    plan_payload = risk_handoff_payload(settings, normal=normal)
    plan_payload["execution_plan"]["routes"] = execution_routes()
    if protective_target:
        plan_payload["execution_plan"]["established_targets"] = [
            {
                "target_id": "synthetic-owned-termination-target",
                "security_id": "XQZ-4017",
                "target_quantity": "0",
                "direction": "EXIT",
                "qualified": True,
                "policy_version": "synthetic-termination-rule-v1",
                "priority": "P0",
                "evidence": position_evidence("synthetic-owned-termination"),
            }
        ]
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


def test_daily_close_projects_owned_protective_priority(migrated_settings: Settings) -> None:
    payload = ready_monitoring_payload(migrated_settings, protective_target=True)
    report = committed(migrated_settings, payload)
    assert report.result.monitoring is not None
    assert report.result.monitoring.cases[0].priority == "P0"


def test_daily_close_cannot_omit_a_required_evidence_family(migrated_settings: Settings) -> None:
    payload = ready_monitoring_payload(migrated_settings)
    payload["monitoring"]["evidence_families"].pop()
    report = committed(migrated_settings, payload)
    assert report.result.monitoring is not None
    assert report.result.monitoring.disposition == "BLOCKED"
    assert "MONITORING_EVIDENCE_FAMILY_INCOMPLETE" in report.result.monitoring.reasons
    assert report.result.monitoring.freshness is not None
    assert report.result.monitoring.freshness.evidence_families[-1].status == "UNKNOWN"


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
    assert report.monitoring_publication is not None
    assert report.monitoring_publication.committed_at
    assert report.monitoring_publication.published_at
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
    payload = ready_monitoring_payload(migrated_settings, protective_target=True)
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
                "security_ids": ["XQZ-4017"],
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
    assert monitoring.cases[0].required_targets
    assert all(
        target.direction == "EXIT" and target.target_quantity == 0
        for target in monitoring.cases[0].required_targets
    )
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


def test_initial_authoritative_termination_creates_a_zero_target_case(
    migrated_settings: Settings,
) -> None:
    payload = ready_monitoring_payload(migrated_settings, protective_target=True)
    payload["monitoring"].update(
        kind="EVENT_REASSESS",
        events=[
            {
                "event_id": "synthetic-initial-termination",
                "kind": "TERMINATION",
                "security_ids": ["XQZ-4017"],
                "authority": "EXCHANGE",
                "evidence": position_evidence("synthetic-exchange"),
            }
        ],
    )
    report = committed(migrated_settings, payload)
    assert report.result.monitoring is not None
    assert report.result.monitoring.cases[0].priority == "P0"
    assert report.result.monitoring.cases[0].required_targets
    assert all(
        target.target_quantity == 0 for target in report.result.monitoring.cases[0].required_targets
    )


@pytest.mark.parametrize("normal", [False, True])
def test_first_authoritative_risk_breach_preserves_owned_direction_without_complete_costs(
    migrated_settings: Settings,
    normal: bool,
) -> None:
    payload = ready_monitoring_payload(migrated_settings, normal=normal)
    payload["monitoring"].update(
        kind="EVENT_REASSESS",
        evidence_families=[],
        events=[
            {
                "event_id": "synthetic-first-risk-breach",
                "kind": "ACCOUNT_STATE" if normal else "RISK_BREACH",
                "security_ids": ["XQZ-4017"],
                "authority": "BROKER" if normal else "RISK_POLICY",
                "evidence": position_evidence("synthetic-risk-policy"),
            }
        ],
    )
    report = committed(migrated_settings, payload)
    assert report.result.monitoring is not None
    outcome = report.result.monitoring
    if normal:
        assert outcome.cases == ()
        return
    assert outcome.disposition == "ASSESSED"
    assert len(outcome.cases) == 1
    assert outcome.cases[0].priority == "P1"
    assert outcome.cases[0].required_targets[0].direction == "REDUCE"
    assert outcome.cases[0].quantity_status == "UNKNOWN"
    assert outcome.cases[0].plan is None
    assert outcome.action_units == ()


@pytest.mark.parametrize("normal_companion", [False, True])
def test_first_mixed_events_preserve_both_saved_targets_with_independent_priority(
    migrated_settings: Settings,
    normal_companion: bool,
) -> None:
    from decimal import Decimal
    from unittest.mock import Mock

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
    from stock_profiler.modules.decision_cases.monitoring import assess_monitoring

    payload = ready_monitoring_payload(migrated_settings)
    ledger = DecisionLedger.from_settings(migrated_settings, clock=GovernanceClock())
    with ledger.serialize_case_execution() as connection:
        source = ledger.get_formal_report_for_event(
            payload["monitoring"]["source_event_id"], connection
        )
        assert source is not None and source.result.execution_plan is not None
        owner = ledger.get_original_decision_event(source.business_object_id, connection)
        first = source.result.execution_plan.targets[0].model_copy(
            update={"target_quantity": Decimal(50), "source_obligation_ids": ("synthetic-risk-A",)}
        )
        second = first.model_copy(
            update={
                "security_id": "XQZ-B",
                "direction": "EXIT",
                "target_quantity": Decimal(0),
                "source_obligation_ids": ("synthetic-protection-B",),
            }
        )
        if normal_companion:
            first = first.model_copy(update={"direction": "HOLD", "source_obligation_ids": ()})
        source = source.model_copy(
            update={
                "result": source.result.model_copy(
                    update={
                        "execution_plan": source.result.execution_plan.model_copy(
                            update={"targets": (first, second)}
                        )
                    }
                )
            }
        )
        saved = Mock(spec=ledger)
        saved.monitoring_history.return_value = ()
        saved.get_formal_report_for_event.return_value = source
        saved.get_original_decision_event.return_value = owner
        saved.get_correction_event.return_value = None
        saved.observed_at.return_value = "2042-05-17T16:01:00Z"
        payload["monitoring"].update(
            kind="EVENT_REASSESS",
            events=[
                {
                    "event_id": "synthetic-risk-A",
                    "kind": "ACCOUNT_STATE" if normal_companion else "RISK_BREACH",
                    "security_ids": [first.security_id],
                    "authority": "BROKER" if normal_companion else "RISK_POLICY",
                    "evidence": position_evidence("synthetic-policy"),
                },
                {
                    "event_id": "synthetic-protection-B",
                    "kind": "TERMINATION",
                    "security_ids": [second.security_id],
                    "authority": "EXCHANGE",
                    "evidence": position_evidence("synthetic-exchange"),
                },
            ],
        )
        outcome = assess_monitoring(FrozenDecisionCase.model_validate(payload), saved, connection)
        split_sources = {}
        split_cases = []
        for item in outcome.cases:
            identity = f"synthetic-plan-{item.required_targets[0].security_id}"
            split_cases.append(
                item.model_copy(
                    update={"source_event_id": identity, "source_event_ids": (identity,)}
                )
            )
            assert source.result.execution_plan is not None
            split_sources[identity] = source.model_copy(
                update={
                    "event_id": identity,
                    "result": source.result.model_copy(
                        update={
                            "execution_plan": source.result.execution_plan.model_copy(
                                update={"targets": item.required_targets}
                            )
                        }
                    ),
                }
            )
        monitoring_source = source.model_copy(
            update={
                "event_id": "synthetic-multiple-owned-lineages",
                "result": source.result.model_copy(
                    update={
                        "execution_plan": None,
                        "monitoring": outcome.model_copy(update={"cases": tuple(split_cases)}),
                    }
                ),
            }
        )
        split_sources[monitoring_source.event_id] = monitoring_source
        saved.get_formal_report_for_event.side_effect = lambda identity, _: split_sources.get(
            identity
        )
        payload["monitoring"]["source_event_id"] = monitoring_source.event_id
        repeated = assess_monitoring(FrozenDecisionCase.model_validate(payload), saved, connection)
    assert outcome.disposition == "ASSESSED"
    expected = {("XQZ-B", "P0", Decimal(0))}
    if not normal_companion:
        expected.add((first.security_id, "P1", Decimal(50)))
    assert {
        (target.security_id, item.priority, target.target_quantity)
        for item in outcome.cases
        for target in item.required_targets
    } == expected
    assert repeated.disposition == "ASSESSED"
    assert {
        (target.security_id, item.priority, target.target_quantity)
        for item in repeated.cases
        for target in item.required_targets
    } == expected


@pytest.mark.parametrize("new_security", [True, False])
def test_protection_adds_a_new_owned_target_without_dropping_existing_targets(
    migrated_settings: Settings,
    new_security: bool,
) -> None:
    from decimal import Decimal
    from unittest.mock import Mock

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
    from stock_profiler.modules.decision_cases.monitoring import assess_monitoring
    from stock_profiler.modules.position_management.execution_contracts import (
        EstablishedQuantityTarget,
    )

    payload = ready_monitoring_payload(migrated_settings)
    original = committed(migrated_settings, payload)
    ledger = DecisionLedger.from_settings(migrated_settings, clock=GovernanceClock())
    with ledger.serialize_case_execution() as connection:
        prior = ledger.get_decision_event(original.event_id, connection)
        owner = ledger.get_decision_event(payload["monitoring"]["source_event_id"], connection)
        source = ledger.get_formal_report_for_event(
            payload["monitoring"]["source_event_id"], connection
        )
        assert prior is not None and owner is not None and source is not None
        assert source.result.execution_plan is not None
        old_target = source.result.execution_plan.targets[0]
        new_target = old_target.model_copy(
            update={
                "security_id": "XQZ-NEW" if new_security else old_target.security_id,
                "target_quantity": Decimal(0),
                "direction": "EXIT",
                "source_obligation_ids": ("synthetic-new-owned-obligation",),
            }
        )
        new_plan = source.result.execution_plan.model_copy(
            update={"targets": (old_target, new_target) if new_security else (new_target,)}
        )
        source = source.model_copy(
            update={
                "result": source.result.model_copy(update={"execution_plan": new_plan}),
                "event_id": "synthetic-new-owned-plan-event",
            }
        )
        payload["monitoring"].update(
            kind="EVENT_REASSESS",
            source_event_id=source.event_id,
            events=[
                {
                    "event_id": "synthetic-new-security-termination",
                    "kind": "TERMINATION",
                    "authority": "EXCHANGE",
                    "security_ids": [new_target.security_id],
                    "evidence": position_evidence("synthetic-exchange"),
                }
            ],
        )
        saved_reports = Mock(spec=ledger)
        saved_reports.monitoring_history.return_value = (prior,)
        saved_reports.get_formal_report_for_event.return_value = source
        saved_reports.get_original_decision_event.return_value = owner
        saved_reports.get_correction_event.return_value = None
        saved_reports.observed_at.return_value = "2042-05-17T16:01:00Z"
        outcome = assess_monitoring(
            FrozenDecisionCase.model_validate(payload), saved_reports, connection
        )
        assert owner.case.execution_plan is not None
        owner = owner.model_copy(
            update={
                "case": owner.case.model_copy(
                    update={
                        "execution_plan": owner.case.execution_plan.model_copy(
                            update={
                                "established_targets": (
                                    EstablishedQuantityTarget.model_validate(
                                        {
                                            "target_id": "synthetic-new-owned-obligation",
                                            "security_id": new_target.security_id,
                                            "target_quantity": "0",
                                            "direction": "EXIT",
                                            "qualified": True,
                                            "priority": "P0",
                                            "policy_version": "synthetic-exit-v1",
                                            "evidence": position_evidence("synthetic-exit"),
                                        }
                                    ),
                                )
                            }
                        )
                    }
                )
            }
        )
        saved_reports.get_original_decision_event.return_value = owner
        assert owner.case.execution_plan is not None
        position_id = owner.case.execution_plan.concentration_event_id
        position_source = ledger.get_formal_report_for_event(position_id, connection)
        assert position_source is not None
        saved_reports.monitoring_history.return_value = (
            prior.model_copy(
                update={"result": prior.result.model_copy(update={"monitoring": outcome})}
            ),
        )
        saved_reports.get_formal_report_for_event.side_effect = lambda identity, _: (
            position_source if identity == position_id else source
        )
        payload["monitoring"]["events"] = [
            {
                "event_id": "synthetic-following-review",
                "kind": "ACCOUNT_STATE",
                "authority": "BROKER",
                "evidence": position_evidence("synthetic-broker"),
            }
        ]
        following = assess_monitoring(
            FrozenDecisionCase.model_validate(payload), saved_reports, connection
        )
        multi_source = original.model_copy(
            update={"result": original.result.model_copy(update={"monitoring": following})}
        )
        saved_reports.get_formal_report_for_event.side_effect = lambda identity, _: (
            multi_source if identity == original.event_id else source
        )
        payload["monitoring"].update(
            source_event_id=original.event_id,
            events=[
                {
                    "event_id": "synthetic-following-termination",
                    "kind": "TERMINATION",
                    "authority": "EXCHANGE",
                    "security_ids": [new_target.security_id],
                    "evidence": position_evidence("synthetic-exchange"),
                }
            ],
        )
        repeated = assess_monitoring(
            FrozenDecisionCase.model_validate(payload), saved_reports, connection
        )
    assert outcome.disposition == "ASSESSED"
    targets = {
        target.security_id: target for item in outcome.cases for target in item.required_targets
    }
    if new_security:
        assert targets[old_target.security_id] == old_target
    assert targets[new_target.security_id] == new_target
    assert any(item.priority == "P0" for item in outcome.cases)
    assert any("synthetic-new-owned-obligation" in item.obligation_ids for item in outcome.cases)
    assert all(
        item.source_event_id == source.event_id
        for item in outcome.cases
        if any(target.security_id == new_target.security_id for target in item.required_targets)
    )
    assert following.disposition == "ASSESSED"
    assert (
        sum("synthetic-new-owned-obligation" in item.obligation_ids for item in following.cases)
        == 1
    )
    assert {item.case_id for item in following.cases} == {item.case_id for item in outcome.cases}
    if new_security:
        assert (
            next(
                item.priority
                for item in following.cases
                if "synthetic-new-owned-obligation" not in item.obligation_ids
            )
            == "P1"
        )
    assert repeated.disposition == "ASSESSED"
    assert {item.case_id for item in repeated.cases} == {item.case_id for item in outcome.cases}


def test_termination_cannot_invent_an_exit_from_a_saved_reduction(
    migrated_settings: Settings,
) -> None:
    payload = ready_monitoring_payload(migrated_settings)
    payload["monitoring"].update(
        kind="EVENT_REASSESS",
        events=[
            {
                "event_id": "synthetic-termination-without-owned-target",
                "kind": "TERMINATION",
                "authority": "EXCHANGE",
                "evidence": position_evidence("synthetic-exchange"),
            }
        ],
    )
    report = committed(migrated_settings, payload)
    assert report.result.monitoring is not None
    assert report.result.monitoring.disposition == "BLOCKED"
    assert report.result.monitoring.cases == ()


def test_rejected_backdated_report_is_archived_without_erasing_current_cases(
    migrated_settings: Settings,
) -> None:
    payload = ready_monitoring_payload(migrated_settings)
    current = committed(migrated_settings, payload)
    payload["knowledge_cutoff"] = "2042-05-16T16:00:00Z"
    payload["monitoring"]["cutoff_at"] = payload["knowledge_cutoff"]
    rejected = committed(migrated_settings, payload)
    workspace = project_workspace((current, rejected), ())
    assert workspace.current_report_ids == (current.report_version_id,)
    assert current.result.monitoring is not None
    assert workspace.inbox == current.result.monitoring.cases
    assert workspace.reports == (current, rejected)


@pytest.mark.parametrize("variant", ["reordered", "rejected-scope"])
def test_valid_event_progresses_after_equivalent_or_rejected_scope(
    migrated_settings: Settings,
    variant: str,
) -> None:
    payload = ready_monitoring_payload(migrated_settings)
    original = committed(migrated_settings, payload)
    event = deepcopy(payload)
    event["monitoring"].update(
        kind="EVENT_REASSESS",
        events=[
            {
                "event_id": "synthetic-valid-scope-event",
                "kind": "ACCOUNT_STATE",
                "authority": "BROKER",
                "evidence": position_evidence("synthetic-broker"),
            }
        ],
    )
    if variant == "reordered":
        event["access_scope"]["account_ids"].reverse()
    else:
        invalid = deepcopy(event)
        invalid["monitoring"]["events"][0]["event_id"] = "synthetic-rejected-scope-event"
        invalid["access_scope"]["account_ids"].pop()
        denied = committed(migrated_settings, invalid)
        assert denied.result.monitoring is not None
        assert denied.result.monitoring.disposition == "BLOCKED"
    current = committed(migrated_settings, event)
    assert current.result.monitoring is not None
    assert current.result.monitoring.disposition == "ASSESSED"
    assert project_workspace((original, current), ()).current_report_ids == (
        current.report_version_id,
    )


def test_notification_cannot_route_another_portfolios_saved_cases(
    migrated_settings: Settings,
) -> None:
    payload = ready_monitoring_payload(migrated_settings)
    original = committed(migrated_settings, payload)
    payload["monitoring"].update(
        kind="NOTIFICATION_RUN",
        portfolio_id="synthetic-other-portfolio",
        source_event_id=original.event_id,
        notification={
            "identity": "synthetic-cross-portfolio",
            "routing_version": "synthetic-routing-v1",
            "quiet_until": None,
            "immediate_result": "ACCEPTED",
            "persistent_result": "ACCEPTED",
        },
    )
    denied = committed(migrated_settings, payload)
    assert denied.result.monitoring is not None
    assert denied.result.monitoring.disposition == "BLOCKED"
    assert denied.result.monitoring.notifications == ()
    assert denied.result.monitoring.cases == ()


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


def test_successful_protective_routes_do_not_drop_quiet_risk_work(
    migrated_settings: Settings,
) -> None:
    from unittest.mock import Mock

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
    from stock_profiler.modules.decision_cases.monitoring import assess_monitoring

    payload = ready_monitoring_payload(migrated_settings)
    source = committed(migrated_settings, payload)
    assert source.result.monitoring is not None
    regular = source.result.monitoring.cases[0]
    protective = regular.model_copy(
        update={
            "case_id": "synthetic-protective-case",
            "priority": "P0",
            "obligation_ids": ("synthetic-protective-obligation",),
        }
    )
    mixed = source.result.monitoring.model_copy(update={"cases": (regular, protective)})
    source = source.model_copy(
        update={"result": source.result.model_copy(update={"monitoring": mixed})}
    )
    ledger = DecisionLedger.from_settings(migrated_settings, clock=GovernanceClock())
    with ledger.serialize_case_execution() as connection:
        fact = ledger.get_decision_event(source.event_id, connection)
        assert fact is not None
        saved = Mock(spec=ledger)
        saved.monitoring_history.return_value = (
            fact.model_copy(
                update={"result": fact.result.model_copy(update={"monitoring": mixed})}
            ),
        )
        saved.get_formal_report_for_event.return_value = source
        saved.get_correction_event.return_value = None
        saved.observed_at.return_value = "2042-05-17T16:01:00Z"
        payload["monitoring"].update(
            kind="NOTIFICATION_RUN",
            source_event_id=source.event_id,
            notification={
                "identity": "synthetic-mixed-priorities",
                "routing_version": "synthetic-routing-v1",
                "quiet_until": "2042-05-17T17:00:00Z",
                "immediate_result": "ACCEPTED",
                "persistent_result": "ACCEPTED",
            },
        )
        outcome = assess_monitoring(FrozenDecisionCase.model_validate(payload), saved, connection)
    assert len(outcome.notifications) == 2
    assert all(attempt.case_id == protective.case_id for attempt in outcome.notifications)
    assert outcome.notification_due_at is not None


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


@pytest.mark.parametrize("quiet,premature", [(True, False), (False, False), (True, True)])
def test_notification_retains_due_work_and_resumes_without_a_new_case(
    migrated_settings: Settings,
    quiet: bool,
    premature: bool,
) -> None:
    payload = ready_monitoring_payload(migrated_settings)
    original = committed(migrated_settings, payload)
    payload["monitoring"].update(
        kind="NOTIFICATION_RUN",
        source_event_id=original.event_id,
        notification={
            "identity": "synthetic-deferred-notification",
            "routing_version": "synthetic-routing-v1",
            "quiet_until": "2042-05-17T17:00:00Z" if quiet else None,
            "immediate_result": "ACCEPTED" if quiet else "UNKNOWN",
            "persistent_result": "ACCEPTED" if quiet else "TIMEOUT",
        },
    )
    deferred = committed(migrated_settings, payload)
    assert deferred.result.monitoring is not None
    assert bool(deferred.result.monitoring.notifications) is not quiet
    assert deferred.result.monitoring.notification_due_at is not None
    payload["monitoring"]["notification"].update(
        attempt_number=2,
        previous_event_id=deferred.event_id,
    )
    if premature:
        early = committed(migrated_settings, payload)
        assert early.result.monitoring is not None
        assert (
            early.result.monitoring.notification_due_at
            == deferred.result.monitoring.notification_due_at
        )
        payload["monitoring"]["notification"].update(
            attempt_number=3,
            previous_event_id=early.event_id,
        )
    resumed = run_frozen_decision_case(
        migrated_settings,
        payload,
        clock=GovernanceClock("2042-05-17T17:00:00Z"),
    ).report
    assert resumed is not None and resumed.result.monitoring is not None
    assert resumed.result.monitoring.notifications
    assert resumed.result.monitoring.cases == deferred.result.monitoring.cases
    replay = run_frozen_decision_case(
        migrated_settings,
        payload,
        clock=GovernanceClock("2042-05-17T17:01:00Z"),
    ).report
    assert replay == resumed


@pytest.mark.parametrize("as_of_trading_day", [True, False])
def test_lifecycle_and_operations_reports_bind_saved_reconciliation_and_original_reports(
    migrated_settings: Settings,
    as_of_trading_day: bool,
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
    payload["monitoring"]["planned_trading_dates"] = [
        (date(2042, 5, 18 if as_of_trading_day else 17) - timedelta(days=offset)).isoformat()
        for offset in reversed(range(20))
    ]
    principal = AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017", "synthetic-account-8029"),
        permissions=("REPORT_READ", "USER_FACT"),
    )
    delivery = ResultDelivery.from_settings(migrated_settings, clock=GovernanceClock())
    acknowledged = delivery.record_user_fact(
        original.report_version_id,
        principal,
        UserFactRequest(kind="ACKNOWLEDGED", idempotency_key="synthetic-audit-ack"),
    )
    assert acknowledged is not None
    audit = committed(migrated_settings, payload)
    assert audit.result.monitoring is not None
    assert original.report_version_id in audit.result.monitoring.source_report_ids
    assert lifecycle.report_version_id in audit.result.monitoring.source_report_ids
    assert len(audit.result.monitoring.operations_dates) == 20
    assert len(audit.result.monitoring.missing_daily_dates) == (19 if as_of_trading_day else 20)
    assert audit.result.monitoring.user_facts == (acknowledged,)
    assert (
        delivery.record_user_fact(
            original.report_version_id,
            principal,
            UserFactRequest(
                kind="EXECUTION_DECLARED",
                declaration="PREPARING",
                idempotency_key="synthetic-after-audit",
            ),
        )
        is not None
    )
    assert committed(migrated_settings, payload) == audit


def test_operations_freezes_recent_interactions_with_reports_older_than_the_window(
    migrated_settings: Settings,
) -> None:
    payload = ready_monitoring_payload(migrated_settings)
    original = committed(migrated_settings, payload)
    principal = AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017", "synthetic-account-8029"),
        permissions=("REPORT_READ", "USER_FACT"),
    )
    clock = GovernanceClock("2042-06-30T08:01:00Z")
    delivery = ResultDelivery.from_settings(migrated_settings, clock=clock)
    fact = delivery.record_user_fact(
        original.report_version_id,
        principal,
        UserFactRequest(kind="ACKNOWLEDGED", idempotency_key="synthetic-old-report-current-ack"),
    )
    assert fact is not None
    notification_payload = deepcopy(payload)
    notification_payload["monitoring"].update(
        kind="NOTIFICATION_RUN",
        source_event_id=original.event_id,
        notification={
            "identity": "synthetic-recent-notification-old-evidence",
            "routing_version": "synthetic-routing-v1",
            "quiet_until": None,
            "immediate_result": "ACCEPTED",
            "persistent_result": "ACCEPTED",
        },
    )
    notification = run_frozen_decision_case(
        migrated_settings, notification_payload, clock=clock
    ).report
    assert notification is not None and notification.result.monitoring is not None
    assert notification.result.monitoring.notifications
    payload["knowledge_cutoff"] = "2042-06-30T08:00:00Z"
    payload["monitoring"].update(
        kind="OPERATIONS",
        cutoff_at=payload["knowledge_cutoff"],
        source_event_id=original.event_id,
        monthly_freeze=True,
        planned_trading_dates=[
            (date(2042, 6, 30) - timedelta(days=offset)).isoformat()
            for offset in reversed(range(20))
        ],
    )
    audit = run_frozen_decision_case(migrated_settings, payload, clock=clock).report
    assert audit is not None and audit.result.monitoring is not None
    assert audit.result.monitoring.user_facts == (fact,)
    assert original.report_version_id in audit.result.monitoring.source_report_ids
    assert audit.result.monitoring.notifications == notification.result.monitoring.notifications
    assert notification.report_version_id in audit.result.monitoring.source_report_ids


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


@pytest.mark.parametrize("corrected", ["upstream", "monitoring", "historical-monitoring"])
def test_corrections_participate_in_confirmation_and_notification_lineage(
    migrated_settings: Settings,
    corrected: str,
) -> None:
    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.modules.decision_cases.service import correct_default_frozen_decision_case

    payload = ready_monitoring_payload(migrated_settings)
    original = committed(migrated_settings, payload)
    current = original
    if corrected == "historical-monitoring":
        reassessment = deepcopy(payload)
        reassessment["monitoring"].update(
            kind="EVENT_REASSESS",
            events=[
                {
                    "event_id": "synthetic-newer-assessment",
                    "kind": "ACCOUNT_STATE",
                    "authority": "BROKER",
                    "evidence": position_evidence("synthetic-broker"),
                }
            ],
        )
        current = committed(migrated_settings, reassessment)
    ledger = DecisionLedger.from_settings(migrated_settings, clock=GovernanceClock())
    with ledger.serialize_case_execution() as connection:
        target_id = original.event_id
        if corrected == "upstream":
            plan = ledger.get_decision_event(payload["monitoring"]["source_event_id"], connection)
            assert plan is not None and plan.case.execution_plan is not None
            target_id = plan.case.execution_plan.concentration_event_id
        target = ledger.get_decision_event(target_id, connection)
        assert target is not None
    replacement = correct_default_frozen_decision_case(
        target.case,
        ledger,
        target.case.business_identity,
    ).report
    if corrected == "upstream":
        delivery = ResultDelivery.from_settings(migrated_settings, clock=GovernanceClock())
        principal = AccessPrincipal(
            user_id="stock-profiler-single-user",
            account_ids=("synthetic-account-4017", "synthetic-account-8029"),
            permissions=("REPORT_READ", "USER_FACT"),
        )
        assert (
            delivery.record_user_fact(
                original.report_version_id,
                principal,
                UserFactRequest(
                    kind="CONFIRMED", choice="ACCEPT", idempotency_key="synthetic-corrected-input"
                ),
            )
            is None
        )
    else:
        notification_source = replacement if corrected == "monitoring" else current
        payload["monitoring"].update(
            kind="NOTIFICATION_RUN",
            source_event_id=notification_source.event_id,
            notification={
                "identity": "synthetic-corrected-notification",
                "routing_version": "synthetic-routing-v1",
                "quiet_until": None,
                "immediate_result": "ACCEPTED",
                "persistent_result": "ACCEPTED",
            },
        )
        report = committed(migrated_settings, payload)
        assert report.result.monitoring is not None
        assert report.result.monitoring.disposition == "ASSESSED"
        assert report.result.monitoring.notifications
        assert report.result.monitoring.source_report_ids == (
            notification_source.report_version_id,
        )


def test_evidence_expiration_invalidates_confirmation_without_new_facts(
    migrated_settings: Settings,
) -> None:
    payload = ready_monitoring_payload(migrated_settings)
    payload["monitoring"]["evidence_families"][0]["evidence"]["expires_at"] = "2042-05-17T16:00:30Z"
    original = committed(migrated_settings, payload)
    delivery = ResultDelivery.from_settings(migrated_settings, clock=GovernanceClock())
    principal = AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017", "synthetic-account-8029"),
        permissions=("REPORT_READ", "USER_FACT"),
    )
    assert (
        delivery.record_user_fact(
            original.report_version_id,
            principal,
            UserFactRequest(kind="CONFIRMED", choice="ACCEPT", idempotency_key="synthetic-expired"),
        )
        is None
    )


def test_new_authoritative_position_facts_invalidate_old_plan_confirmation(
    migrated_settings: Settings,
) -> None:
    from test_issuer_concentration import concentration_payload
    from test_position_state_reconciliation import position_case_payload

    original = committed(migrated_settings, ready_monitoring_payload(migrated_settings))
    snapshot = concentration_payload(
        migrated_settings,
        "synthetic-unused-authorization",
        quantity="100",
    )["concentration"]["position_snapshot"]
    committed(
        migrated_settings,
        position_case_payload(migrated_settings, "synthetic-new-position-facts", snapshot),
    )
    delivery = ResultDelivery.from_settings(migrated_settings, clock=GovernanceClock())
    principal = AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017", "synthetic-account-8029"),
        permissions=("REPORT_READ", "USER_FACT"),
    )
    assert (
        delivery.record_user_fact(
            original.report_version_id,
            principal,
            UserFactRequest(
                kind="CONFIRMED", choice="ACCEPT", idempotency_key="synthetic-new-facts"
            ),
        )
        is None
    )
