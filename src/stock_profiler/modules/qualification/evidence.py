"""Qualification age follows evaluation endpoints, never report or grant dates."""

from calendar import monthrange
from datetime import UTC, datetime

from stock_profiler.modules.qualification.contracts import QualificationEvidence


def qualification_deadline(evidence: QualificationEvidence) -> datetime:
    deadlines = [evidence.expires_at, _month_end_after(evidence.evaluation_end, 12)]
    if evidence.state_activity_end is not None:
        deadlines.append(_month_end_after(evidence.state_activity_end, 24))
    return min(deadlines)


def _month_end_after(endpoint: datetime, months: int) -> datetime:
    endpoint = endpoint.astimezone(UTC)
    year, zero_month = divmod(endpoint.year * 12 + endpoint.month - 1 + months, 12)
    month = zero_month + 1
    return datetime(year, month, monthrange(year, month)[1], 23, 59, 59, 999999, tzinfo=UTC)
