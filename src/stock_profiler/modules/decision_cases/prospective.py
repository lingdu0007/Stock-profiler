"""Persisted original-cycle accounting behind the complete decision-case seam."""

from datetime import datetime
from typing import TypeVar

from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
from stock_profiler.modules.decision_cases.ports import DecisionLedger
from stock_profiler.modules.prospective.accounting import (
    data_complete,
    pipeline_complete,
    summarize_operations,
)
from stock_profiler.modules.prospective.contracts import (
    CycleFormalLook,
    CycleReport,
    CycleWatermark,
)

Transaction = TypeVar("Transaction")


class InvalidProspectiveRequest(ValueError):
    """A prospective request cannot alter its frozen origin or observation history."""


def assess_prospective(
    case: FrozenDecisionCase, ledger: DecisionLedger[Transaction], connection: Transaction
) -> CycleReport:
    command = case.prospective
    assert command is not None and case.access_scope is not None
    if case.access_scope.visibility != "SHADOW":
        raise ValueError("PROSPECTIVE_REQUIRES_SHADOW_SCOPE")
    history = ledger.prospective_history(connection, case.access_scope)
    if command.operation == "REGISTER":
        registration = command.registration
        assert registration is not None
        if registration.source_version_bundle != case.version_bundle:
            raise ValueError("PROSPECTIVE_LOCK_BUNDLE_MISMATCH")
        if any(
            event.case.prospective is not None
            and event.case.prospective.registration is not None
            and event.case.prospective.registration.source_version_bundle
            == registration.source_version_bundle
            for event in history
        ):
            raise ValueError("PROSPECTIVE_LOCK_ALREADY_REGISTERED")
        return CycleReport(
            disposition="REGISTERED",
            registration_event_id=None,
            version_id=registration.version_id,
            cutoff_at=command.cutoff_at,
        )
    source = ledger.get_decision_event(command.registration_event_id or "", connection)
    if (
        source is None
        or source.case.access_scope is None
        or not source.case.access_scope.same_scope_as(case.access_scope)
        or source.case.prospective is None
        or source.case.prospective.registration is None
        or source.result.prospective is None
        or source.result.prospective.disposition != "REGISTERED"
        or source.corrects_event_id is not None
        or datetime.fromisoformat(source.committed_at) > command.cutoff_at
    ):
        raise ValueError("PROSPECTIVE_REGISTRATION_UNAVAILABLE")
    registration = source.case.prospective.registration
    prior = tuple(
        event
        for event in history
        if event.case.prospective is not None
        and event.case.prospective.registration_event_id == source.decision_event_id
    )
    previous = prior[-1] if prior else None
    if command.previous_event_id != (previous.decision_event_id if previous else None):
        raise ValueError("PROSPECTIVE_LINEAGE_CONFLICT")
    if previous and command.cutoff_at < datetime.fromisoformat(previous.committed_at):
        raise ValueError("PROSPECTIVE_CUTOFF_REWOUND")
    if case.version_bundle != registration.source_version_bundle:
        raise ValueError("PROSPECTIVE_LOCK_BUNDLE_MISMATCH")
    observations = {
        event.case.prospective.observation.plan_month: event.case.prospective.observation
        for event in prior
        if event.case.prospective is not None and event.case.prospective.observation is not None
    }
    if command.observation is not None:
        row = command.observation
        if row.plan_month in observations:
            raise ValueError("PROSPECTIVE_MONTH_ALREADY_RECORDED")
        node = next(
            (node for node in registration.plan_nodes if node.plan_month == row.plan_month), None
        )
        if node is None or row.completed_at < node.knowledge_cutoff:
            raise ValueError("PROSPECTIVE_PLAN_MONTH_UNAVAILABLE")
        if row.status in {"CANDIDATES", "ABSTAINED"} and command.cutoff_at > node.disclosure_at:
            raise ValueError("PROSPECTIVE_SUCCESS_RECORDED_LATE")
        if any(item.scope not in registration.source_scopes for item in row.sources):
            raise ValueError("PROSPECTIVE_SOURCE_SCOPE_MISMATCH")
        if any(item.knowledge_cutoff != node.knowledge_cutoff for item in row.sources):
            raise ValueError("PROSPECTIVE_SOURCE_CUTOFF_MISMATCH")
        if (
            row.first_delivery_at is not None
            and not node.knowledge_cutoff <= row.first_delivery_at <= node.disclosure_at
        ):
            raise ValueError("PROSPECTIVE_DELIVERY_OUTSIDE_ORIGINAL_WINDOW")
        for item in row.notifications:
            expected_due = {"INITIAL": node.first_entry_at, "FINAL": node.disclosure_at}.get(
                item.kind
            )
            if (
                expected_due is not None and item.due_at != expected_due
            ) or not node.knowledge_cutoff <= item.due_at <= node.disclosure_at:
                raise ValueError("PROSPECTIVE_NOTIFICATION_WINDOW_CHANGED")
        observations[row.plan_month] = row
    operations, sources = summarize_operations(registration, observations, command.cutoff_at)
    valid_nodes = tuple(
        node
        for node in registration.plan_nodes
        if data_complete(node, observations.get(node.plan_month), registration.source_scopes)
        and pipeline_complete(node, observations.get(node.plan_month))
    )
    return CycleReport(
        disposition="WAITING_FOR_EVIDENCE",
        registration_event_id=source.decision_event_id,
        version_id=registration.version_id,
        cutoff_at=command.cutoff_at,
        previous_event_id=command.previous_event_id,
        report_version=len(prior) + 1,
        operations=operations,
        source_watermarks=sources,
        watermark=CycleWatermark(
            mature_batches=0,
            nonoverlapping_windows=0,
            high_band_records=0,
            pending_batches=sum(node.matures_at > command.cutoff_at for node in valid_nodes),
            due_missing_batches=sum(node.matures_at <= command.cutoff_at for node in valid_nodes),
            observation_reached=False,
            formal_sufficient=False,
            waiting_for=("24_MATURE_BATCHES", "4_NONOVERLAPPING_WINDOWS", "100_HIGH_BAND_RECORDS"),
        ),
        formal_look=CycleFormalLook(disposition="WAITING_FOR_MATURITY"),
    )
