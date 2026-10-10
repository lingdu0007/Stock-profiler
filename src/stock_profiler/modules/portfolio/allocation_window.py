"""The immutable intersection of a saved plan's original market windows."""

from datetime import datetime, timedelta

from stock_profiler.modules.portfolio.allocation_contracts import CandidateAllocationOutcome
from stock_profiler.modules.portfolio.market_calendar import market_session_close_on


def allocation_entry_window(
    plan: CandidateAllocationOutcome,
) -> tuple[datetime | None, datetime | None]:
    if plan.correlations is None or not plan.rows:
        return None, None
    starts, ends = [], []
    for row in plan.rows:
        dates = row.candidate.valid_market_dates
        if not dates:
            return None, None
        start = market_session_close_on(dates[0], plan.correlations.market_calendar_version)
        end = market_session_close_on(dates[-1], plan.correlations.market_calendar_version)
        if start is None or end is None:
            return None, None
        starts.append(start - timedelta(hours=7))
        ends.append(end)
    return max(starts), min(ends)


def entry_window_is_open(plan: CandidateAllocationOutcome, now: datetime) -> bool:
    start, end = allocation_entry_window(plan)
    return start is not None and end is not None and start <= now <= end
