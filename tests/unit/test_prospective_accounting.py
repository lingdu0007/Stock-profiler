"""Original synthetic calendars exercise rolling denominators and retained failures."""

from calendar import monthrange
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import load_frozen_decision_case
from stock_profiler.modules.prospective.accounting import summarize_operations
from stock_profiler.modules.prospective.contracts import (
    CycleRegistration,
    EvidenceFloor,
    MonthlyObservation,
    NotificationObligation,
    OperationsPolicy,
    PlanNode,
    ShadowIncident,
    SourceSnapshot,
)


def node(index: int) -> PlanNode:
    current = 2042 * 12 + index
    following = current + 1
    terminal = following + 6
    year, month = current // 12, current % 12 + 1
    return PlanNode(
        plan_month=f"{year}-{month:02}",
        knowledge_cutoff=datetime(year, month, monthrange(year, month)[1], 15, 59, 59, tzinfo=UTC),
        first_entry_at=datetime(following // 12, following % 12 + 1, 1, 1, 30, tzinfo=UTC),
        disclosure_at=datetime(following // 12, following % 12 + 1, 7, 7, tzinfo=UTC),
        matures_at=datetime(terminal // 12, terminal % 12 + 1, 7, 7, tzinfo=UTC),
    )


def registration(settings: Settings, months: int = 24) -> CycleRegistration:
    return CycleRegistration(
        version_id="fictional-accounting-v1",
        capability_version="fictional-monthly-v1",
        source_version_bundle=load_frozen_decision_case(settings).version_bundle,
        calendar_version="fictional-calendar-v1",
        source_scopes=("fictional-source/fields/board/use",),
        plan_nodes=tuple(node(index) for index in range(months)),
        operations_policy=OperationsPolicy(
            window_months=24, completion_floor=Decimal(".95"), coverage_floor=Decimal(".50")
        ),
        source_months_required=3,
        formal_floor=EvidenceFloor(
            mature_batches=24, nonoverlapping_windows=4, high_band_records=100
        ),
    )


def observation(plan: PlanNode, *, candidates: bool = True) -> MonthlyObservation:
    return MonthlyObservation(
        plan_month=plan.plan_month,
        completed_at=plan.disclosure_at,
        status="CANDIDATES" if candidates else "ABSTAINED",
        reason="NONE",
        sources=(
            SourceSnapshot(
                scope="fictional-source/fields/board/use",
                snapshot_digest="c" * 64,
                knowledge_cutoff=plan.knowledge_cutoff,
                published_at=plan.knowledge_cutoff - timedelta(minutes=3),
                acquired_at=plan.knowledge_cutoff - timedelta(minutes=2),
                validated_at=plan.knowledge_cutoff - timedelta(minutes=1),
                watermark_complete=True,
                critical_error=False,
            ),
        ),
        batch_saved_at=plan.disclosure_at,
        report_saved_at=plan.disclosure_at,
        first_delivery_at=plan.first_entry_at - timedelta(minutes=1) if candidates else None,
        notifications=(
            NotificationObligation(
                kind="INITIAL",
                due_at=plan.first_entry_at,
                delivered_at=plan.first_entry_at - timedelta(minutes=1),
            ),
            NotificationObligation(
                kind="FINAL", due_at=plan.disclosure_at, delivered_at=plan.disclosure_at
            ),
        )
        if candidates
        else (),
        incidents=(),
    )


def test_complete_24_month_window_passes_mechanical_gates_without_granting_qualification(
    settings: Settings,
) -> None:
    policy = registration(settings)
    rows = {plan.plan_month: observation(plan) for plan in policy.plan_nodes}
    report, sources = summarize_operations(policy, rows, datetime(2045, 1, 1, tzinfo=UTC))
    assert report.planned_months == 24
    assert report.full_window and report.gates_passed
    assert report.pipeline.rate == 1 and report.coverage.rate == 1
    assert report.notifications.denominator == 48 and report.notifications.numerator == 48
    assert report.qualified is False
    assert sources[0].watermark_reached and sources[0].qualification_granted is False


def test_rolling_window_keeps_missing_months_and_never_selects_last_successes(
    settings: Settings,
) -> None:
    policy = registration(settings, 27)
    rows = {plan.plan_month: observation(plan) for plan in policy.plan_nodes[:-2]}
    report, sources = summarize_operations(policy, rows, datetime(2045, 1, 1, tzinfo=UTC))
    assert report.window_months == tuple(plan.plan_month for plan in policy.plan_nodes[3:])
    assert report.pipeline.denominator == 24 and report.pipeline.numerator == 22
    assert report.data.numerator == 22
    assert report.consecutive_failure and report.consecutive_obligation_failure
    assert not report.gates_passed
    assert sources[0].consecutive_months == 0
    assert report.notifications.disposition == "WITHHELD"
    assert report.coverage.disposition == "WITHHELD"


def test_adjacent_data_and_system_failures_are_combined(settings: Settings) -> None:
    policy = registration(settings)
    rows = {plan.plan_month: observation(plan) for plan in policy.plan_nodes}
    earlier, later = policy.plan_nodes[-2:]
    rows[earlier.plan_month] = observation(earlier).model_copy(update={"sources": ()})
    rows[later.plan_month] = observation(later, candidates=False).model_copy(
        update={"status": "FAILED", "reason": "SYSTEM", "batch_saved_at": None}
    )
    report, _ = summarize_operations(policy, rows, datetime(2045, 1, 1, tzinfo=UTC))
    assert report.pipeline.numerator == 22 and report.consecutive_failure
    assert tuple(failure.reason for failure in report.failures) == ("DATA", "SYSTEM")
    assert report.qualified is False


def test_abstention_uses_pipeline_denominator_and_notification_na(settings: Settings) -> None:
    policy = registration(settings)
    rows = {plan.plan_month: observation(plan, candidates=False) for plan in policy.plan_nodes}
    report, _ = summarize_operations(policy, rows, datetime(2045, 1, 1, tzinfo=UTC))
    assert report.pipeline.rate == 1
    assert (
        report.notifications.rate is None and report.notifications.disposition == "NOT_APPLICABLE"
    )
    assert report.timeliness.rate is None
    assert report.coverage.denominator == 24 and report.coverage.rate == 0
    assert not report.gates_passed


def test_current_window_withholds_every_opportunity_sensitive_metric(settings: Settings) -> None:
    policy = registration(settings, 1)
    cutoff = policy.plan_nodes[0].knowledge_cutoff
    report, _ = summarize_operations(policy, {}, cutoff)
    for metric in (report.notifications, report.timeliness, report.coverage):
        assert metric.disposition == "WITHHELD"
        assert metric.rate is None and metric.numerator == metric.denominator == 0


def test_source_streak_resets_for_a_missing_or_critical_month(settings: Settings) -> None:
    policy = registration(settings, 4)
    rows = {plan.plan_month: observation(plan) for plan in policy.plan_nodes[:3]}
    _, sources = summarize_operations(policy, rows, policy.plan_nodes[2].disclosure_at)
    assert sources[0].consecutive_months == 3 and sources[0].watermark_reached
    _, sources = summarize_operations(policy, rows, policy.plan_nodes[3].disclosure_at)
    assert sources[0].consecutive_months == 0 and not sources[0].watermark_reached
    row = observation(policy.plan_nodes[3])
    rows[row.plan_month] = row.model_copy(
        update={"sources": (row.sources[0].model_copy(update={"critical_error": True}),)}
    )
    _, sources = summarize_operations(policy, rows, policy.plan_nodes[3].disclosure_at)
    assert sources[0].consecutive_months == 0


def test_early_window_is_not_a_24_month_pass(settings: Settings) -> None:
    policy = registration(settings, 3)
    rows = {plan.plan_month: observation(plan) for plan in policy.plan_nodes}
    report, _ = summarize_operations(policy, rows, policy.plan_nodes[-1].disclosure_at)
    assert report.data.rate == 1 and not report.full_window and not report.gates_passed


def test_timely_boundary_requires_delivery_before_first_open(settings: Settings) -> None:
    policy = registration(settings)
    rows = {plan.plan_month: observation(plan) for plan in policy.plan_nodes}
    plan = policy.plan_nodes[-1]
    rows[plan.plan_month] = rows[plan.plan_month].model_copy(
        update={"first_delivery_at": plan.first_entry_at}
    )
    report, _ = summarize_operations(policy, rows, datetime(2045, 1, 1, tzinfo=UTC))
    assert report.timeliness.numerator == 23
    assert report.timeliness.rate == Decimal(23) / 24


def test_closed_safety_incident_and_old_unclosed_incident_remain_retained(
    settings: Settings,
) -> None:
    policy = registration(settings, 27)
    rows = {plan.plan_month: observation(plan) for plan in policy.plan_nodes}
    plan = policy.plan_nodes[0]
    rows[plan.plan_month] = rows[plan.plan_month].model_copy(
        update={
            "incidents": (
                ShadowIncident(
                    incident_id="fictional-privacy",
                    kind="PRIVACY",
                    occurred_at=plan.disclosure_at,
                    closed_at=plan.disclosure_at,
                ),
                ShadowIncident(
                    incident_id="fictional-operations",
                    kind="OPERATIONS",
                    occurred_at=plan.disclosure_at,
                    closed_at=None,
                ),
            )
        }
    )
    report, _ = summarize_operations(policy, rows, datetime(2045, 1, 1, tzinfo=UTC))
    assert report.incident_count == 0
    assert report.unclosed_incidents == 1 and not report.safety_clear and not report.gates_passed


@pytest.mark.parametrize(
    "changes",
    [
        {"knowledge_cutoff": "2042-01-27T15:59:59Z"},
        {"matures_at": "2042-07-07T07:00:00Z"},
        {"knowledge_cutoff": "2042-01-31T15:59:59.1Z"},
    ],
)
def test_plan_rejects_shortened_or_invented_monthly_clocks(changes: dict[str, str]) -> None:
    payload = node(0).model_dump(mode="json")
    payload.update(changes)
    with pytest.raises(ValidationError):
        PlanNode.model_validate(payload)


def test_notification_consecutive_failure_uses_plan_months_not_reminder_sequence(
    settings: Settings,
) -> None:
    policy = registration(settings)
    rows = {plan.plan_month: observation(plan) for plan in policy.plan_nodes}
    for plan in policy.plan_nodes[-2:]:
        row = rows[plan.plan_month]
        rows[plan.plan_month] = row.model_copy(
            update={
                "notifications": (
                    row.notifications[0].model_copy(update={"delivered_at": None}),
                    row.notifications[1],
                )
            }
        )
    report, _ = summarize_operations(policy, rows, datetime(2045, 1, 1, tzinfo=UTC))
    assert report.notifications.numerator == 46
    assert report.consecutive_obligation_failure and not report.gates_passed


def test_two_failed_reminders_in_one_month_do_not_create_two_failed_months(
    settings: Settings,
) -> None:
    policy = registration(settings)
    rows = {plan.plan_month: observation(plan) for plan in policy.plan_nodes}
    row = rows[policy.plan_nodes[-1].plan_month]
    rows[row.plan_month] = row.model_copy(
        update={
            "notifications": tuple(
                item.model_copy(update={"delivered_at": None}) for item in row.notifications
            )
        }
    )
    report, _ = summarize_operations(policy, rows, datetime(2045, 1, 1, tzinfo=UTC))
    assert report.notifications.numerator == 46
    assert not report.consecutive_obligation_failure and report.gates_passed


def test_failure_report_counts_independently_of_pipeline_failure(settings: Settings) -> None:
    policy = registration(settings, 1)
    plan = policy.plan_nodes[0]
    row = observation(plan, candidates=False).model_copy(
        update={"status": "FAILED", "reason": "SYSTEM", "batch_saved_at": None}
    )
    report, _ = summarize_operations(policy, {plan.plan_month: row}, plan.disclosure_at)
    assert report.pipeline.numerator == 0 and report.batches.numerator == 0
    assert report.reports.numerator == 1


def test_notification_from_before_original_window_cannot_count_as_delivery(
    settings: Settings,
) -> None:
    policy = registration(settings, 1)
    plan = policy.plan_nodes[0]
    row = observation(plan)
    row = row.model_copy(
        update={
            "notifications": tuple(
                item.model_copy(
                    update={"delivered_at": plan.knowledge_cutoff - timedelta(seconds=1)}
                )
                for item in row.notifications
            )
        }
    )
    report, _ = summarize_operations(policy, {plan.plan_month: row}, plan.disclosure_at)
    assert report.notifications.numerator == 0


def test_batch_saved_before_knowledge_cutoff_cannot_complete_the_pipeline(
    settings: Settings,
) -> None:
    policy = registration(settings, 1)
    plan = policy.plan_nodes[0]
    row = observation(plan, candidates=False).model_copy(
        update={"batch_saved_at": plan.knowledge_cutoff - timedelta(seconds=1)}
    )
    report, _ = summarize_operations(policy, {plan.plan_month: row}, plan.disclosure_at)
    assert report.pipeline.numerator == 0


def test_preregistered_completion_floor_can_tighten_operations_gate(settings: Settings) -> None:
    policy = registration(settings)
    rows = {plan.plan_month: observation(plan) for plan in policy.plan_nodes}
    row = rows[policy.plan_nodes[-1].plan_month]
    rows[row.plan_month] = row.model_copy(update={"report_saved_at": None})
    report, _ = summarize_operations(policy, rows, datetime(2045, 1, 1, tzinfo=UTC))
    assert report.gates_passed
    payload = policy.model_dump(mode="json")
    payload["operations_policy"] = {
        "window_months": 24,
        "completion_floor": "1",
        "coverage_floor": "0.50",
    }
    strict = CycleRegistration.model_validate(payload)
    report, _ = summarize_operations(strict, rows, datetime(2045, 1, 1, tzinfo=UTC))
    assert not report.gates_passed


def test_six_month_floor_uses_exchange_civil_dates_across_timezone_month_boundaries() -> None:
    payload = node(7).model_dump(mode="json")
    payload.update(disclosure_at="2042-09-30T16:30:00Z", matures_at="2043-03-30T16:30:00Z")
    with pytest.raises(ValidationError, match="six-calendar-month"):
        PlanNode.model_validate(payload)
