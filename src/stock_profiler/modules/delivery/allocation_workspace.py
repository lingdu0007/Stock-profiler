"""Separate saved personal-plan identities for the authenticated candidate view."""

from dataclasses import dataclass
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict

from stock_profiler.modules.decision_cases.domain import FormalReport, FrozenDecisionCase
from stock_profiler.modules.portfolio.allocation_contracts import CandidateAllocationOutcome
from stock_profiler.modules.portfolio.allocation_inputs import is_allocation_input_source
from stock_profiler.modules.portfolio.allocation_window import allocation_entry_window


@dataclass(frozen=True)
class AllocationWorkspaceSource:
    report: FormalReport
    case: FrozenDecisionCase


class AllocationWorkspaceView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_event_id: str
    report_version_id: str
    plan_report: FormalReport
    allocation: CandidateAllocationOutcome
    latest_input_report_version_id: str
    valid_from: AwareDatetime | None
    valid_through: AwareDatetime | None
    new_actions_permitted: bool
    stop_reasons: tuple[str, ...] = ()
    reviews: tuple[FormalReport, ...] = ()
    confirmations: tuple[FormalReport, ...] = ()
    executions: tuple[FormalReport, ...] = ()


def project_allocations(
    sources: tuple[AllocationWorkspaceSource, ...],
    now: datetime,
    candidate_stops: dict[str, tuple[str, ...]] | None = None,
    plan_stops: dict[str, tuple[str, ...]] | None = None,
) -> tuple[AllocationWorkspaceView, ...]:
    views = []
    for source in sources:
        report, plan = source.report, source.report.result.candidate_allocation
        if plan is None:
            continue
        related = tuple(
            item
            for item in sources
            if (
                item.case.candidate_confirmation is not None
                and item.case.candidate_confirmation.plan_event_id == report.event_id
            )
            or (
                item.case.candidate_execution is not None
                and item.case.candidate_execution.plan_event_id == report.event_id
            )
        )
        reviews = tuple(
            item.report
            for item in related
            if item.case.candidate_confirmation is not None
            and item.case.candidate_confirmation.operation == "REVIEW"
        )
        confirmations = tuple(
            item.report
            for item in related
            if item.case.candidate_confirmation is not None
            and item.case.candidate_confirmation.operation != "REVIEW"
        )
        executions = tuple(
            item.report for item in related if item.case.candidate_execution is not None
        )
        inputs = tuple(
            item.report
            for item in sources
            if item.case.candidate_allocation is not None
            and item.case.candidate_allocation.candidate_event_id == plan.candidate_event_id
            and is_allocation_input_source(
                item.case.candidate_allocation, item.report.result.candidate_allocation
            )
        )
        start, end = allocation_entry_window(plan)
        reasons = tuple(
            dict.fromkeys(
                reason
                for item in (*reviews, *confirmations)
                if item.result.candidate_confirmation is not None
                for reason in item.result.candidate_confirmation.reasons
                if reason in {"PLAN_CHANGED", "PLAN_INVALIDATED", "PLAN_SUPERSEDED"}
            )
        )
        if start is None or end is None or not start <= now <= end:
            reasons += ("ENTRY_WINDOW_CLOSED",)
        if plan.disposition != "PLANNED":
            reasons += plan.reasons or ("PLAN_NOT_CONFIRMABLE",)
        if plan_stops is not None:
            reasons += plan_stops.get(report.event_id, ())
        if candidate_stops is not None:
            reasons += candidate_stops.get(
                plan.candidate_event_id or "", ("CANDIDATE_UNAVAILABLE",)
            )
        latest_execution = executions[-1].result.candidate_execution if executions else None
        if latest_execution is not None and (
            latest_execution.disposition != "RECONCILED" or latest_execution.unresolved_order_keys
        ):
            reasons += ("EXECUTION_PENDING_RECONCILIATION",)
        if any(
            item.report.result.candidate_allocation is not None
            and item.report.result.candidate_allocation.disposition == "PLANNED"
            and item.report.result.candidate_allocation.replaces_plan_event_id == report.event_id
            for item in sources
        ):
            reasons += ("PLAN_SUPERSEDED",)
        views.append(
            AllocationWorkspaceView(
                plan_event_id=report.event_id,
                report_version_id=report.report_version_id,
                plan_report=report,
                allocation=plan,
                latest_input_report_version_id=inputs[-1].report_version_id
                if inputs
                else report.report_version_id,
                valid_from=start,
                valid_through=end,
                new_actions_permitted=not reasons,
                stop_reasons=reasons,
                reviews=reviews,
                confirmations=confirmations,
                executions=executions,
            )
        )
    return tuple(views)
