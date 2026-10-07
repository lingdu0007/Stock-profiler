"""Original synthetic prospective operations at the complete frozen-case seam."""

from typing import Any

import pytest
from test_scoped_qualification import GovernanceClock, case_payload

from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import DecisionCaseExecution, DecisionEventFact


def cycle_case(settings: Settings, identity: str = "register") -> dict[str, Any]:
    payload = case_payload(
        settings, f"prospective-{identity}", {}, contract_version="prospective.1.0.0"
    )
    payload.pop("governance")
    payload["access_scope"]["visibility"] = "SHADOW"
    cutoff = "2042-05-01T00:00:00Z"
    payload.update(knowledge_cutoff=cutoff, report_generated_at=cutoff)
    payload["prospective"] = {
        "operation": "REGISTER",
        "synthetic": True,
        "generator_version": "fictional-shadow-cycle/1",
        "seed": 2207,
        "cutoff_at": cutoff,
        "registration_event_id": None,
        "previous_event_id": None,
        "observation": None,
        "maturity": None,
        "registration": {
            "version_id": "fictional-locked-cycle-v1",
            "capability_version": "fictional-t1-v1",
            "source_version_bundle": dict(payload["version_bundle"]),
            "calendar_version": "fictional-exchange-calendar-v1",
            "source_scopes": ["fictional-market/mainboard/shadow"],
            "operations_policy": {
                "window_months": 24,
                "completion_floor": "0.95",
                "coverage_floor": "0.50",
            },
            "source_months_required": 3,
            "formal_floor": {
                "mature_batches": 24,
                "nonoverlapping_windows": 4,
                "high_band_records": 100,
            },
            "plan_nodes": [
                {
                    "plan_month": "2042-06",
                    "knowledge_cutoff": "2042-06-30T15:59:59Z",
                    "first_entry_at": "2042-07-01T01:30:00Z",
                    "disclosure_at": "2042-07-07T07:00:00Z",
                    "matures_at": "2043-01-07T07:00:00Z",
                }
            ],
        },
    }
    payload["input"]["prospective"] = payload["prospective"]
    return payload


def run_case(settings: Settings, payload: dict[str, Any]) -> DecisionCaseExecution:
    return run_frozen_decision_case(
        settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )


def review_case(
    settings: Settings,
    registration: str,
    previous: str | None = None,
    *,
    cutoff: str = "2043-02-01T00:00:00Z",
    identity: str = "review",
) -> dict[str, Any]:
    payload = cycle_case(settings, identity)
    payload.update(knowledge_cutoff=cutoff, report_generated_at=cutoff)
    payload["prospective"].update(
        operation="REVIEW",
        registration=None,
        registration_event_id=registration,
        previous_event_id=previous,
        cutoff_at=cutoff,
    )
    return payload


def saved(settings: Settings, execution: DecisionCaseExecution) -> DecisionEventFact:
    assert execution.business_commit_status == "COMMITTED"
    assert execution.publication_status == "CLOSED"
    assert execution.report is None
    ledger = DecisionLedger.from_settings(settings)
    with ledger.serialize_case_execution() as connection:
        fact = ledger.get_decision_event(execution.decision_event_id, connection)
    assert fact is not None
    return fact


def test_preregistered_missing_month_remains_in_all_denominators(
    migrated_settings: Settings,
) -> None:
    registered = saved(
        migrated_settings, run_case(migrated_settings, cycle_case(migrated_settings))
    )
    assert registered.result.prospective is not None
    assert registered.result.prospective.disposition == "REGISTERED"
    payload = review_case(migrated_settings, registered.decision_event_id)
    reviewed = saved(migrated_settings, run_case(migrated_settings, payload))
    evidence = reviewed.result.model_dump(mode="json")["prospective"]
    assert evidence["operations"]["planned_months"] == 1
    assert evidence["operations"]["data"]["denominator"] == 1
    assert evidence["operations"]["data"]["numerator"] == 0
    assert evidence["operations"]["notifications"]["disposition"] == "WITHHELD"
    assert evidence["watermark"]["mature_batches"] == 0
    assert evidence["formal_look"]["actual_ordinal"] == 0
    assert evidence["formal_look"]["alpha_spent"] == "0"
    assert evidence["authorization_granted"] is False
    assert saved(migrated_settings, run_case(migrated_settings, payload)) == reviewed


def test_cycle_cannot_use_a_user_publication_scope(migrated_settings: Settings) -> None:
    payload = cycle_case(migrated_settings)
    payload["access_scope"]["visibility"] = "USER"
    with pytest.raises(ValueError, match="PROSPECTIVE_REQUIRES_SHADOW_SCOPE"):
        run_case(migrated_settings, payload)


def observation_case(
    settings: Settings,
    registration: str,
    previous: str | None = None,
    *,
    status: str = "ABSTAINED",
    identity: str = "observe",
) -> dict[str, Any]:
    payload = review_case(
        settings, registration, previous, cutoff="2042-07-07T07:00:00Z", identity=identity
    )
    payload["prospective"].update(
        operation="OBSERVE",
        observation={
            "plan_month": "2042-06",
            "completed_at": "2042-07-07T07:00:00Z",
            "status": status,
            "reason": "NONE" if status in {"CANDIDATES", "ABSTAINED"} else "SYSTEM",
            "sources": [
                {
                    "scope": "fictional-market/mainboard/shadow",
                    "snapshot_digest": "c" * 64,
                    "knowledge_cutoff": "2042-06-30T15:59:59Z",
                    "published_at": "2042-06-30T15:00:00Z",
                    "acquired_at": "2042-06-30T15:05:00Z",
                    "validated_at": "2042-06-30T15:10:00Z",
                    "watermark_complete": True,
                    "critical_error": False,
                }
            ],
            "batch_saved_at": "2042-07-07T07:00:00Z" if status == "ABSTAINED" else None,
            "report_saved_at": "2042-07-07T07:00:00Z",
            "first_delivery_at": None,
            "notifications": [],
            "incidents": [],
        },
    )
    return payload


def test_abstention_is_a_valid_month_with_no_notification_obligation(
    migrated_settings: Settings,
) -> None:
    registered = saved(
        migrated_settings, run_case(migrated_settings, cycle_case(migrated_settings))
    )
    observed = saved(
        migrated_settings,
        run_case(
            migrated_settings, observation_case(migrated_settings, registered.decision_event_id)
        ),
    )
    assert observed.result.prospective is not None
    operations = observed.result.prospective.operations
    assert operations is not None
    assert operations.pipeline.numerator == 1
    assert operations.data.numerator == 1
    assert operations.batches.numerator == 1
    assert operations.notifications.rate is None
    assert operations.notifications.disposition == "NOT_APPLICABLE"
    assert operations.coverage.numerator == 0
    assert operations.coverage.denominator == 1
    assert operations.full_window is False
    assert operations.qualified is False


def test_failed_original_month_cannot_be_replaced_by_a_successful_retry(
    migrated_settings: Settings,
) -> None:
    registered = saved(
        migrated_settings, run_case(migrated_settings, cycle_case(migrated_settings))
    )
    failed = saved(
        migrated_settings,
        run_case(
            migrated_settings,
            observation_case(migrated_settings, registered.decision_event_id, status="FAILED"),
        ),
    )
    retry = observation_case(
        migrated_settings, registered.decision_event_id, failed.decision_event_id, identity="retry"
    )
    with pytest.raises(ValueError, match="PROSPECTIVE_MONTH_ALREADY_RECORDED"):
        run_case(migrated_settings, retry)
    reviewed = saved(
        migrated_settings,
        run_case(
            migrated_settings,
            review_case(migrated_settings, registered.decision_event_id, failed.decision_event_id),
        ),
    )
    assert reviewed.result.prospective is not None
    operations = reviewed.result.prospective.operations
    assert operations is not None
    assert operations.planned_months == 1
    assert operations.pipeline.numerator == 0
    assert operations.reports.numerator == 1
    assert operations.failures[0].plan_month == "2042-06"
    assert operations.failures[0].reason == "SYSTEM"


def test_late_observation_cannot_backfill_a_missing_plan_as_success(
    migrated_settings: Settings,
) -> None:
    registered = saved(
        migrated_settings, run_case(migrated_settings, cycle_case(migrated_settings))
    )
    payload = observation_case(migrated_settings, registered.decision_event_id)
    payload.update(
        knowledge_cutoff="2042-08-01T00:00:00Z", report_generated_at="2042-08-01T00:00:00Z"
    )
    payload["prospective"]["cutoff_at"] = payload["knowledge_cutoff"]
    with pytest.raises(ValueError, match="PROSPECTIVE_SUCCESS_RECORDED_LATE"):
        run_case(migrated_settings, payload)


def test_valid_month_waits_for_outcomes_then_retains_due_missing_labels(
    migrated_settings: Settings,
) -> None:
    registered = saved(
        migrated_settings, run_case(migrated_settings, cycle_case(migrated_settings))
    )
    observed = saved(
        migrated_settings,
        run_case(
            migrated_settings, observation_case(migrated_settings, registered.decision_event_id)
        ),
    )
    assert observed.result.prospective is not None
    assert observed.result.prospective.watermark is not None
    assert observed.result.prospective.watermark.pending_batches == 1
    assert observed.result.prospective.watermark.due_missing_batches == 0
    reviewed = saved(
        migrated_settings,
        run_case(
            migrated_settings,
            review_case(
                migrated_settings, registered.decision_event_id, observed.decision_event_id
            ),
        ),
    )
    assert reviewed.result.prospective is not None
    assert reviewed.result.prospective.watermark is not None
    assert reviewed.result.prospective.watermark.pending_batches == 0
    assert reviewed.result.prospective.watermark.due_missing_batches == 1
    assert reviewed.result.prospective.watermark.mature_batches == 0
    assert reviewed.result.prospective.formal_look is not None
    assert reviewed.result.prospective.formal_look.alpha_spent == 0
    assert reviewed.result.prospective.formal_look.actual_ordinal == 0


def test_registration_rename_cannot_reset_the_locked_population(
    migrated_settings: Settings,
) -> None:
    saved(migrated_settings, run_case(migrated_settings, cycle_case(migrated_settings)))
    renamed = cycle_case(migrated_settings, "renamed")
    renamed["prospective"]["registration"]["version_id"] = "fictional-renamed-cycle-v1"
    renamed["prospective"]["registration"]["capability_version"] = "fictional-renamed-capability-v1"
    with pytest.raises(ValueError, match="PROSPECTIVE_LOCK_ALREADY_REGISTERED"):
        run_case(migrated_settings, renamed)


def test_review_cannot_fork_or_rewind_its_saved_lineage(migrated_settings: Settings) -> None:
    registered = saved(
        migrated_settings, run_case(migrated_settings, cycle_case(migrated_settings))
    )
    first = saved(
        migrated_settings,
        run_case(migrated_settings, review_case(migrated_settings, registered.decision_event_id)),
    )
    with pytest.raises(ValueError, match="PROSPECTIVE_LINEAGE_CONFLICT"):
        run_case(
            migrated_settings,
            review_case(migrated_settings, registered.decision_event_id, identity="fork"),
        )
    with pytest.raises(ValueError, match="PROSPECTIVE_CUTOFF_REWOUND"):
        run_case(
            migrated_settings,
            review_case(
                migrated_settings,
                registered.decision_event_id,
                first.decision_event_id,
                cutoff="2043-01-01T00:00:00Z",
                identity="rewind",
            ),
        )
    second = saved(
        migrated_settings,
        run_case(
            migrated_settings,
            review_case(
                migrated_settings,
                registered.decision_event_id,
                first.decision_event_id,
                cutoff="2043-03-01T00:00:00Z",
                identity="successor",
            ),
        ),
    )
    assert second.result.prospective is not None
    assert second.result.prospective.previous_event_id == first.decision_event_id
    assert second.result.prospective.report_version == 2
    assert first.result.prospective is not None and first.result.prospective.report_version == 1


def test_shadow_cycle_is_unreadable_to_its_authorized_user(migrated_settings: Settings) -> None:
    from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
    from stock_profiler.bootstrap.decision_cases import get_formal_report
    from stock_profiler.modules.delivery.access import AccessPrincipal

    execution = run_case(migrated_settings, cycle_case(migrated_settings))
    assert execution.report is None and execution.publication_status == "CLOSED"
    principal = AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017",),
        permissions=("REPORT_READ", "USER_FACT"),
    )
    assert (
        get_formal_report(execution.report_version_id, migrated_settings, principal=principal)
        is None
    )
    audit = ResultDelivery.from_settings(migrated_settings).audit_history()
    assert audit[-1].outcome == "DENIED"
    serialized = audit[-1].model_dump_json()
    assert "fictional-locked-cycle" not in serialized
    assert "FICTIONAL-ORBITAL" not in serialized


def test_other_shadow_scope_cannot_reference_an_existing_registration(
    migrated_settings: Settings,
) -> None:
    registered = saved(
        migrated_settings, run_case(migrated_settings, cycle_case(migrated_settings))
    )
    payload = review_case(migrated_settings, registered.decision_event_id)
    payload["access_scope"]["user_id"] = "fictional-other-user"
    with pytest.raises(ValueError, match="PROSPECTIVE_REGISTRATION_UNAVAILABLE"):
        run_case(migrated_settings, payload)


def test_saved_logical_cutoff_cannot_rewind_when_host_clock_differs(
    migrated_settings: Settings,
) -> None:
    registered = saved(
        migrated_settings, run_case(migrated_settings, cycle_case(migrated_settings))
    )
    payload = review_case(migrated_settings, registered.decision_event_id)
    first = saved(migrated_settings, run_frozen_decision_case(migrated_settings, payload))
    assert first.result.prospective is not None
    assert first.committed_at < payload["knowledge_cutoff"]
    with pytest.raises(ValueError, match="PROSPECTIVE_CUTOFF_REWOUND"):
        run_case(
            migrated_settings,
            review_case(
                migrated_settings,
                registered.decision_event_id,
                first.decision_event_id,
                cutoff="2043-01-01T00:00:00Z",
                identity="logical-rewind",
            ),
        )


def test_incident_facts_append_to_original_month_without_replacing_its_outcome(
    migrated_settings: Settings,
) -> None:
    registered = saved(
        migrated_settings, run_case(migrated_settings, cycle_case(migrated_settings))
    )
    observed = saved(
        migrated_settings,
        run_case(
            migrated_settings, observation_case(migrated_settings, registered.decision_event_id)
        ),
    )
    payload = review_case(
        migrated_settings,
        registered.decision_event_id,
        observed.decision_event_id,
        cutoff="2042-08-01T00:00:00Z",
        identity="late-incident",
    )
    payload["prospective"].update(
        operation="INCIDENT",
        incident_fact={
            "operation": "RECORD",
            "plan_month": "2042-06",
            "incident_id": "fictional-incident-1",
            "kind": "PRIVACY",
            "occurred_at": "2042-07-06T07:00:00Z",
            "closed_at": None,
        },
    )
    incident = saved(migrated_settings, run_case(migrated_settings, payload))
    assert (
        incident.result.prospective is not None
        and incident.result.prospective.operations is not None
    )
    assert incident.result.prospective.operations.incident_count == 1
    assert incident.result.prospective.operations.unclosed_incidents == 1
    assert incident.result.prospective.operations.pipeline.numerator == 1
    payload = review_case(
        migrated_settings,
        registered.decision_event_id,
        incident.decision_event_id,
        cutoff="2042-09-01T00:00:00Z",
        identity="close-incident",
    )
    payload["prospective"].update(
        operation="INCIDENT",
        incident_fact={
            "operation": "CLOSE",
            "plan_month": "2042-06",
            "incident_id": "fictional-incident-1",
            "kind": "PRIVACY",
            "occurred_at": "2042-07-06T07:00:00Z",
            "closed_at": "2042-08-31T07:00:00Z",
        },
    )
    closed = saved(migrated_settings, run_case(migrated_settings, payload))
    assert (
        closed.result.prospective is not None and closed.result.prospective.operations is not None
    )
    assert closed.result.prospective.operations.unclosed_incidents == 0
    assert not closed.result.prospective.operations.safety_clear
    assert closed.result.prospective.previous_event_id == incident.decision_event_id
    assert (
        observed.result.prospective is not None
        and observed.result.prospective.operations is not None
    )
    assert observed.result.prospective.operations.incident_count == 0
