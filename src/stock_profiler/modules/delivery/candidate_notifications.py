"""Bounded candidate reminders for already saved authenticated results."""

from datetime import datetime
from hashlib import sha256
from typing import Literal

from stock_profiler.modules.delivery.candidate_reminder_contracts import (
    CandidateReminderBody,
    CandidateReminderChannel,
    CandidateReminderRecord,
)
from stock_profiler.modules.delivery.candidate_reminder_contracts import (
    CandidateReminderRequest as CandidateReminderRequest,
)
from stock_profiler.modules.delivery.candidate_workspace import CandidateReleaseView


def prepare_candidate_reminder(
    release: CandidateReleaseView,
    request: CandidateReminderRequest,
    observed_at: datetime,
    scope_identity: str,
    history: tuple[CandidateReminderRecord, ...],
) -> CandidateReminderRecord | None:
    if request.kind == "PLAN_READY":
        return None
    eligible = release.status in {
        "CURRENT",
        "WAITING_MARKET",
    }
    if request.kind != "CORRECTION" and (
        release.superseded_by_report_id is not None
        or (release.release.disposition == "CANDIDATES" and not eligible)
    ):
        return None
    prior_alerts = tuple(
        item
        for item in history
        if item.report_version_id == release.report_version_id
        and item.path_completed
        and item.kind != "CORRECTION"
        and any(channel.result != "REJECTED" for channel in item.channels)
    )
    if request.kind == "FINAL" and (
        not eligible
        or not any(member.candidate for member in release.release.members)
        or release.final_reminder_before is None
        or observed_at >= release.final_reminder_before
    ):
        return None
    if request.kind == "CORRECTION" and (
        release.status not in {"SUPERSEDED", "INVALIDATED", "EXPIRED"}
        or not any(item.body.candidate_count > 0 for item in prior_alerts)
    ):
        return None
    digest = sha256(request.model_dump_json().encode()).hexdigest()
    material_identity = (
        f"{release.report_version_id}\n{release.superseded_by_report_id}\n{release.status}\n"
        f"{release.status_reasons}\n{release.status_evidence_ids}"
        if request.kind == "CORRECTION"
        else ""
    )
    intent = (
        "candidate-reminder-"
        + sha256(
            f"{scope_identity}\n{release.plan_month}\n{request.kind}\n{material_identity}".encode()
        ).hexdigest()
    )
    existing = next((item for item in history if item.intent_id == intent), None)
    if request.retry_of is None and existing is not None:
        return (
            existing
            if existing.request_digest == digest
            and existing.report_version_id == release.report_version_id
            else None
        )
    if request.retry_of is not None:
        previous = next((item for item in history if item.attempt_id == request.retry_of), None)
        if (
            previous is None
            or previous.intent_id != intent
            or previous.report_version_id != release.report_version_id
        ):
            return None
        repeated = next((item for item in history if item.retry_of == request.retry_of), None)
        if repeated is not None:
            return repeated if repeated.request_digest == digest else None
        deferred_due = (
            previous.status == "DEFERRED"
            and previous.deferred_until is not None
            and observed_at >= previous.deferred_until
            and request.quiet_until == previous.deferred_until
        )
        failed_attempt = previous.status == "ATTEMPTED" and all(
            channel.result != "ACCEPTED" for channel in previous.channels
        )
        if not (deferred_due or failed_attempt):
            return None
    harmful_quiet_delay = (
        request.kind == "CORRECTION"
        and request.quiet_until is not None
        and observed_at < request.quiet_until
        and release.valid_through is not None
        and observed_at < release.valid_through
        and any(item.body.candidate_count > 0 for item in prior_alerts)
    )
    status: Literal["ATTEMPTED", "DEFERRED", "DISABLED"] = "ATTEMPTED"
    if not request.reminders_enabled and request.kind != "CORRECTION":
        status = "DISABLED"
    elif (
        request.quiet_until is not None
        and observed_at < request.quiet_until
        and not harmful_quiet_delay
    ):
        status = "DEFERRED"
    channels = [CandidateReminderChannel(role="PRIMARY", result=request.primary_result)]
    if request.primary_result != "ACCEPTED" or harmful_quiet_delay:
        channels.append(CandidateReminderChannel(role="PERSISTENT", result=request.fallback_result))
    if status != "ATTEMPTED":
        channels = []
    return CandidateReminderRecord(
        intent_id=intent,
        attempt_id="candidate-attempt-" + sha256(f"{intent}\n{digest}".encode()).hexdigest(),
        report_version_id=release.report_version_id,
        plan_month=release.plan_month,
        kind=request.kind,
        request_digest=digest,
        recorded_at=observed_at,
        body=CandidateReminderBody(
            result_type="WITHDRAWN_DO_NOT_RELY_ON_ORIGINAL"
            if request.kind == "CORRECTION"
            else release.release.disposition,
            candidate_count=0
            if request.kind == "CORRECTION"
            else sum(member.candidate for member in release.release.members),
            window_ends_at=release.valid_through,
            entry_path=f"/candidates/releases/{release.report_version_id}",
        ),
        channels=tuple(channels),
        status=status,
        deferred_until=request.quiet_until if status == "DEFERRED" else None,
        retry_of=request.retry_of,
        path_completed=status == "ATTEMPTED",
    )
