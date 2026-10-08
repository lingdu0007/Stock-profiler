"""Retained qualification eligibility shared by reading and new candidate use."""

from datetime import datetime
from typing import Literal

from stock_profiler.modules.candidate_selection.calibrated_candidates import CandidateReleaseOutcome
from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
from stock_profiler.modules.qualification.contracts import GovernanceOutcome
from stock_profiler.modules.qualification.service import (
    current_qualification,
    qualification_is_current,
)

CandidateCurrentQualificationStatus = Literal[
    "UNAVAILABLE", "NOT_OBTAINED", "VALID", "AT_RISK", "SUSPENDED", "REVOKED", "EXPIRED"
]


def candidate_qualification_eligibility(
    release: CandidateReleaseOutcome,
    case: FrozenDecisionCase,
    history: tuple[GovernanceOutcome, ...],
    now: datetime,
) -> tuple[tuple[str, ...], tuple[str, ...], CandidateCurrentQualificationStatus]:
    qualification_status: CandidateCurrentQualificationStatus = "UNAVAILABLE"
    snapshot = release.qualification
    scope = case.access_scope
    records = tuple(
        outcome
        for outcome in history
        if outcome.qualification is not None and outcome.qualification.recorded_at <= now
    )
    record = next(
        (
            outcome.qualification
            for outcome in records
            if outcome.qualification is not None
            and snapshot is not None
            and outcome.qualification.decision_id == snapshot.qualification_id
        ),
        None,
    )
    if record is None or scope is None:
        return (("CURRENT_QUALIFICATION_UNAVAILABLE",), (), qualification_status)
    basis = record.formal_passing_evidence or record.authorization_evidence
    if (
        record.scope.user_id != scope.user_id
        or set(record.scope.account_ids) != set(scope.account_ids)
        or record.scope.capability != "candidate-release"
        or record.scope.purpose != "CANDIDATE_BUY"
        or record.scope.market_state != release.market_state
        or record.version.version_id != release.capability_version
        or record.version.implementation != case.version_bundle
        or basis is None
        or basis.market_calendar_version != release.market_calendar_version
    ):
        return (
            ("CURRENT_QUALIFICATION_SCOPE_OR_VERSION_CHANGED",),
            (record.decision_id,),
            qualification_status,
        )
    try:
        latest = current_qualification(records, record.scope, record.version)
    except ValueError:
        return (("CURRENT_QUALIFICATION_AMBIGUOUS",), (), qualification_status)
    if latest is None:
        return (("CURRENT_QUALIFICATION_UNAVAILABLE",), (), qualification_status)
    qualification_status = latest.status
    if latest.status in {"VALID", "AT_RISK"} and not qualification_is_current(latest, now):
        qualification_status = "EXPIRED"
    by_id = {
        outcome.qualification.decision_id: outcome.qualification
        for outcome in records
        if outcome.qualification is not None
    }
    lineage = []
    seen: set[str] = set()
    cursor = latest
    while cursor.decision_id not in seen:
        seen.add(cursor.decision_id)
        lineage.append(cursor)
        if cursor.decision_id == record.decision_id:
            break
        previous = by_id.get(cursor.previous_decision_id or "")
        if previous is None:
            return (("CURRENT_QUALIFICATION_LINEAGE_UNAVAILABLE",), (), qualification_status)
        cursor = previous
    if lineage[-1].decision_id != record.decision_id:
        return (("CURRENT_QUALIFICATION_AMBIGUOUS",), (), qualification_status)
    for revision in reversed(lineage):
        if revision.status not in {"VALID", "AT_RISK"}:
            return (
                ("RELEASE_QUALIFICATION_INVALIDATED", revision.status, revision.cause),
                (revision.decision_id,),
                qualification_status,
            )
    for revision in by_id.values():
        if revision.previous_decision_id in seen and (
            revision.version != record.version or not revision.scope.same_scope_as(record.scope)
        ):
            return (
                ("CURRENT_QUALIFICATION_SCOPE_OR_VERSION_CHANGED",),
                (revision.decision_id,),
                qualification_status,
            )
    latest_basis = latest.formal_passing_evidence or latest.authorization_evidence
    if (
        latest_basis is None
        or latest_basis.market_calendar_version != release.market_calendar_version
    ):
        return (
            ("CURRENT_QUALIFICATION_SCOPE_OR_VERSION_CHANGED",),
            (latest.decision_id,),
            qualification_status,
        )
    if not qualification_is_current(record, now):
        return (("RELEASE_QUALIFICATION_EXPIRED",), (record.decision_id,), qualification_status)
    if not qualification_is_current(latest, now):
        return (("CURRENT_QUALIFICATION_EXPIRED",), (latest.decision_id,), qualification_status)
    return ((), (latest.decision_id,), qualification_status)
