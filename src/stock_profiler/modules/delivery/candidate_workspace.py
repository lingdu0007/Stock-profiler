"""Read-only candidate identities projected from the shared decision ledger."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict

from stock_profiler.modules.candidate_selection.calibrated_candidates import (
    CalibratedMember,
    CandidateReleaseOutcome,
)
from stock_profiler.modules.candidate_selection.current_eligibility import (
    CandidateCurrentQualificationStatus,
    candidate_qualification_eligibility,
)
from stock_profiler.modules.decision_cases.domain import FormalReport, FrozenDecisionCase
from stock_profiler.modules.delivery.candidate_reminder_contracts import CandidateReminderRecord
from stock_profiler.modules.portfolio.market_calendar import (
    synthetic_market_calendar,
)
from stock_profiler.modules.qualification.contracts import GovernanceOutcome


@dataclass(frozen=True)
class CandidateWorkspaceSource:
    report: FormalReport
    case: FrozenDecisionCase
    qualification_history: tuple[GovernanceOutcome, ...] = ()
    research_correction_event_id: str | None = None
    research_correction_committed_at: datetime | None = None
    reminders: tuple[CandidateReminderRecord, ...] = ()
    frozen_pool_count: int | None = None
    research_completed_count: int | None = None


class CandidateDeliveryContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CandidateReleaseView(CandidateDeliveryContract):
    report_version_id: str
    event_id: str
    business_object_id: str
    plan_month: str
    knowledge_cutoff: str
    generated_at: str
    committed_at: str | None
    published_at: str | None
    valid_from: AwareDatetime | None
    valid_through: AwareDatetime | None
    final_reminder_before: AwareDatetime | None
    status: Literal["CURRENT", "RESULT", "WAITING_MARKET", "EXPIRED", "SUPERSEDED", "INVALIDATED"]
    status_reasons: tuple[str, ...]
    status_evidence_ids: tuple[str, ...] = ()
    withdrawal_evidence_ids: tuple[str, ...] = ()
    frozen_pool_count: int | None = None
    research_completed_count: int | None = None
    current_qualification_status: CandidateCurrentQualificationStatus = "UNAVAILABLE"
    corrects_report_id: str | None
    superseded_by_report_id: str | None
    detail_ids: tuple[str, ...]
    reminders: tuple[CandidateReminderRecord, ...] = ()
    release: CandidateReleaseOutcome


class CandidateDetailView(CandidateDeliveryContract):
    detail_id: str
    report_version_id: str
    event_id: str
    member: CalibratedMember
    status: Literal[
        "CURRENT", "NOT_CANDIDATE", "WAITING_MARKET", "EXPIRED", "SUPERSEDED", "INVALIDATED"
    ] = "NOT_CANDIDATE"
    status_reasons: tuple[str, ...] = ()


class CandidateWorkspace(CandidateDeliveryContract):
    observed_at: AwareDatetime
    releases: tuple[CandidateReleaseView, ...] = ()
    current_report_ids: tuple[str, ...] = ()
    details: tuple[CandidateDetailView, ...] = ()


def candidate_detail_id(report_id: str, research_id: str) -> str:
    return "candidate-detail-" + sha256(f"{report_id}\n{research_id}".encode()).hexdigest()


def project_candidate_workspace(
    sources: tuple[CandidateWorkspaceSource, ...], observed_at: datetime
) -> CandidateWorkspace:
    reports = tuple(source.report for source in sources)
    reports_by_event = {report.event_id: report for report in reports}
    corrections = {
        report.corrects_event_id: report for report in reports if report.corrects_event_id
    }
    current: dict[tuple[str, tuple[str, ...], str], FormalReport] = {}
    for report in reports:
        assert report.result.candidate_release is not None
        month = report.result.candidate_release.knowledge_cutoff.astimezone(
            ZoneInfo("Asia/Shanghai")
        ).strftime("%Y-%m")
        scope = report.access_scope
        key = (
            scope.user_id if scope else "",
            tuple(sorted(scope.account_ids)) if scope else (),
            month,
        )
        previous = current.get(key)
        if previous is None or report.corrects_event_id == previous.event_id:
            current[key] = report
    releases: list[CandidateReleaseView] = []
    details: list[CandidateDetailView] = []
    for source in sources:
        report, case = source.report, source.case
        release = report.result.candidate_release
        assert release is not None
        valid_from, valid_through, final_reminder_before = _saved_window(release, case)
        qualification_reasons, qualification_ids, qualification_status = _check_qualification(
            source, observed_at
        )
        successor = corrections.get(report.event_id)
        predecessor = reports_by_event.get(report.corrects_event_id or "")
        status: Literal[
            "CURRENT", "RESULT", "WAITING_MARKET", "EXPIRED", "SUPERSEDED", "INVALIDATED"
        ] = "RESULT"
        reasons = release.reasons
        evidence_ids = qualification_ids
        if successor is not None:
            status = "SUPERSEDED"
            evidence = successor.result.correction_evidence
            reasons = (evidence.reason if evidence else "REPORT_CORRECTED",)
            evidence_ids = (successor.event_id,)
        elif release.disposition == "CANDIDATES":
            if valid_through is None or valid_from is None:
                status, reasons = "INVALIDATED", ("WINDOW_EVIDENCE_UNAVAILABLE",)
                evidence_ids = (report.event_id,)
            elif observed_at > valid_through:
                status, reasons = "EXPIRED", ("CANDIDATE_WINDOW_ENDED",)
                evidence_ids = (report.event_id,)
            elif release.qualification is None or observed_at > release.qualification.valid_through:
                status, reasons = "INVALIDATED", ("QUALIFICATION_EXPIRED",)
                evidence_ids = (
                    release.qualification.qualification_id
                    if release.qualification
                    else report.event_id,
                )
            elif source.research_correction_event_id is not None:
                status, reasons = "INVALIDATED", ("RESEARCH_EVIDENCE_CORRECTED",)
                evidence_ids = (source.research_correction_event_id,)
            elif qualification_reasons:
                status, reasons = "INVALIDATED", qualification_reasons
            elif observed_at < valid_from:
                status = "WAITING_MARKET"
            else:
                status = "CURRENT"
        # Keep the original invalidating fact separate from the clock-dependent display state.
        withdrawal_ids: tuple[str, ...] = ()
        withdrawal_at = min(observed_at, valid_through) if valid_through else observed_at
        withdrawal_reasons, withdrawal_qualification_ids, _ = _check_qualification(
            source, withdrawal_at
        )
        if (
            successor is not None
            and datetime.fromisoformat(
                successor.report_publication.committed_at
                if successor.report_publication is not None
                else successor.generated_at
            )
            <= withdrawal_at
        ):
            withdrawal_ids = (successor.event_id,)
        elif release.disposition == "CANDIDATES":
            if source.research_correction_event_id is not None and (
                source.research_correction_committed_at is None
                or source.research_correction_committed_at <= withdrawal_at
            ):
                withdrawal_ids = (source.research_correction_event_id,)
            elif withdrawal_reasons:
                withdrawal_ids = withdrawal_qualification_ids or (report.event_id,)
            elif status in {"INVALIDATED", "EXPIRED", "SUPERSEDED"}:
                withdrawal_ids = (report.event_id,)
        member_details = tuple(
            CandidateDetailView(
                detail_id=candidate_detail_id(report.report_version_id, member.research_id),
                report_version_id=report.report_version_id,
                event_id=report.event_id,
                member=member,
                status=status if member.candidate and status != "RESULT" else "NOT_CANDIDATE",
                status_reasons=reasons if member.candidate else member.reasons,
            )
            for member in release.members
        )
        details.extend(member_details)
        publication = report.report_publication
        releases.append(
            CandidateReleaseView(
                report_version_id=report.report_version_id,
                event_id=report.event_id,
                business_object_id=report.business_object_id,
                plan_month=release.knowledge_cutoff.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
                    "%Y-%m"
                ),
                knowledge_cutoff=report.knowledge_cutoff,
                generated_at=report.generated_at,
                committed_at=publication.committed_at if publication else None,
                published_at=publication.published_at if publication else None,
                valid_from=valid_from,
                valid_through=valid_through,
                final_reminder_before=final_reminder_before,
                status=status,
                status_reasons=reasons,
                status_evidence_ids=evidence_ids,
                withdrawal_evidence_ids=withdrawal_ids,
                frozen_pool_count=source.frozen_pool_count,
                research_completed_count=source.research_completed_count,
                current_qualification_status=qualification_status,
                corrects_report_id=predecessor.report_version_id if predecessor else None,
                superseded_by_report_id=successor.report_version_id if successor else None,
                detail_ids=tuple(item.detail_id for item in member_details),
                reminders=source.reminders,
                release=release,
            )
        )
    return CandidateWorkspace(
        observed_at=observed_at,
        releases=tuple(releases),
        details=tuple(details),
        current_report_ids=tuple(report.report_version_id for report in current.values()),
    )


def _saved_window(
    release: CandidateReleaseOutcome, case: FrozenDecisionCase
) -> tuple[datetime | None, datetime | None, datetime | None]:
    if len(release.valid_market_dates) != 5:
        return None, None, None
    command = case.candidate_release
    if command is not None:
        sessions = {session.market_date: session for session in command.market_sessions}
        first, fifth = (
            sessions.get(release.valid_market_dates[0]),
            sessions.get(release.valid_market_dates[-1]),
        )
        if first is not None and fifth is not None:
            return first.opens_at, fifth.closes_at, fifth.opens_at
    calendar = synthetic_market_calendar(release.market_calendar_version)
    if calendar is None:
        return None, None, None
    saved = calendar.sessions_after_open(release.knowledge_cutoff, 5)
    if tuple(session.closed_at.date() for session in saved) != release.valid_market_dates:
        return None, None, None
    return (
        saved[0].closed_at - timedelta(hours=7),
        saved[-1].closed_at,
        saved[-1].closed_at - timedelta(hours=7),
    )


def _check_qualification(
    source: CandidateWorkspaceSource, now: datetime
) -> tuple[tuple[str, ...], tuple[str, ...], CandidateCurrentQualificationStatus]:
    release = source.report.result.candidate_release
    assert release is not None
    return candidate_qualification_eligibility(
        release, source.case, source.qualification_history, now
    )
