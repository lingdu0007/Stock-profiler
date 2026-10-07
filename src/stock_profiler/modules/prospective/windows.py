"""Count original result periods whose inclusive evaluation intervals do not overlap."""

from datetime import datetime


def disjoint_windows(periods: tuple[tuple[datetime, datetime], ...]) -> int:
    end = None
    count = 0
    for start, maturity in sorted(periods):
        if maturity < start:
            raise ValueError("PROSPECTIVE_EVALUATION_PERIOD_REVERSED")
        if end is None or start > end:
            count += 1
            end = maturity
    return count
