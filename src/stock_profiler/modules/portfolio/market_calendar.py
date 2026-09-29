"""Immutable synthetic market-calendar references for portfolio risk evidence."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value)


def six_month_terminal_evaluation_at(value: datetime, market_calendar_version: str) -> datetime:
    """Resolve the six-month target to the last saved session close on or before its date."""
    month_index = value.year * 12 + value.month - 1 + 6
    year, month_zero = divmod(month_index, 12)
    target = value.astimezone(UTC).replace(
        year=year,
        month=month_zero + 1,
        day=min(value.day, monthrange(year, month_zero + 1)[1]),
    )
    calendar = synthetic_market_calendar(market_calendar_version)
    if calendar is None:
        raise ValueError("MARKET_CALENDAR_VERSION_UNSUPPORTED")
    session = calendar.last_terminal_session_on_or_before(target.date())
    if session is None:
        raise ValueError("MARKET_CALENDAR_TERMINAL_SESSION_UNAVAILABLE")
    return session.closed_at


@dataclass(frozen=True)
class MarketSession:
    """One known normal session in a versioned synthetic market calendar."""

    ordinal: int
    closed_at: datetime


@dataclass(frozen=True)
class SyntheticMarketCalendar:
    """Synthetic reference data used to verify immutable relaxation evidence."""

    version_id: str
    sessions: tuple[MarketSession, ...]
    monthly_selection_cutoffs: tuple[datetime, ...]
    terminal_sessions: tuple[MarketSession, ...] = ()

    def session_for(self, ordinal: int) -> MarketSession | None:
        return next((session for session in self.sessions if session.ordinal == ordinal), None)

    def next_session_after(self, closed_at: datetime) -> MarketSession | None:
        return next(
            (session for session in self.sessions if session.closed_at > closed_at),
            None,
        )

    def next_monthly_selection_cutoff_after(self, closed_at: datetime) -> datetime | None:
        return next(
            (cutoff for cutoff in self.monthly_selection_cutoffs if cutoff > closed_at),
            None,
        )

    def last_terminal_session_on_or_before(self, target_date: date) -> MarketSession | None:
        return next(
            (
                session
                for session in reversed(self.terminal_sessions or self.sessions)
                if session.closed_at.date() <= target_date
            ),
            None,
        )


def _terminal_sessions(
    *,
    start: date,
    end: date,
    close_at: time,
    excluded_dates: frozenset[date] = frozenset(),
) -> tuple[MarketSession, ...]:
    """Build immutable synthetic terminal sessions, including explicitly missing dates."""
    sessions: list[MarketSession] = []
    current = start
    while current <= end:
        if current.weekday() < 5 and current not in excluded_dates:
            closed_at = datetime.combine(current, close_at, tzinfo=UTC)
            ordinal = 1_000_000 + (current - start).days
            sessions.append(MarketSession(ordinal, closed_at))
        current += timedelta(days=1)
    return tuple(sessions)


def synthetic_market_calendar(version_id: str) -> SyntheticMarketCalendar | None:
    """Return one known immutable synthetic calendar version, if it exists."""
    return next(
        (calendar for calendar in _SYNTHETIC_MARKET_CALENDARS if calendar.version_id == version_id),
        None,
    )


_SYNTHETIC_MARKET_CALENDARS = (
    SyntheticMarketCalendar(
        version_id="synthetic-capital-calendar-v1",
        sessions=tuple(
            MarketSession(
                7101 + index,
                _utc("2042-05-20T15:00:00+00:00") + timedelta(days=offset),
            )
            for index, offset in enumerate(
                (0, 1, 2, *(5 + week * 7 + day for week in range(12) for day in range(5)))
            )
        ),
        monthly_selection_cutoffs=(),
        terminal_sessions=_terminal_sessions(
            start=date(2037, 1, 1),
            end=date(2050, 12, 31),
            close_at=time(15),
        ),
    ),
    SyntheticMarketCalendar(
        version_id="synthetic-market-calendar-v1",
        sessions=(
            MarketSession(6101, _utc("2042-05-20T15:00:00+00:00")),
            MarketSession(6102, _utc("2042-05-21T15:00:00+00:00")),
            MarketSession(6103, _utc("2042-05-22T15:00:00+00:00")),
            MarketSession(6104, _utc("2042-05-25T15:00:00+00:00")),
            MarketSession(6105, _utc("2042-05-26T15:00:00+00:00")),
            MarketSession(6106, _utc("2042-05-27T15:00:00+00:00")),
            MarketSession(6107, _utc("2042-05-28T15:00:00+00:00")),
            MarketSession(6108, _utc("2042-05-29T15:00:00+00:00")),
            MarketSession(6109, _utc("2042-06-01T15:00:00+00:00")),
            MarketSession(6110, _utc("2042-06-02T15:00:00+00:00")),
            MarketSession(6111, _utc("2042-06-03T15:00:00+00:00")),
            MarketSession(6112, _utc("2042-06-04T15:00:00+00:00")),
            MarketSession(6113, _utc("2042-06-05T15:00:00+00:00")),
            MarketSession(6114, _utc("2042-06-08T15:00:00+00:00")),
            MarketSession(6115, _utc("2042-06-09T15:00:00+00:00")),
            MarketSession(6116, _utc("2042-06-10T15:00:00+00:00")),
            MarketSession(6117, _utc("2042-06-11T15:00:00+00:00")),
            MarketSession(6118, _utc("2042-06-12T15:00:00+00:00")),
            MarketSession(6119, _utc("2042-06-15T15:00:00+00:00")),
            MarketSession(6120, _utc("2042-06-16T15:00:00+00:00")),
            MarketSession(6121, _utc("2042-06-17T15:00:00+00:00")),
            *(
                MarketSession(
                    6122 + index,
                    _utc("2042-06-18T15:00:00+00:00") + timedelta(days=offset),
                )
                for index, offset in enumerate(
                    (0, 1, *(4 + week * 7 + day for week in range(10) for day in range(5)))
                )
            ),
        ),
        monthly_selection_cutoffs=(_utc("2042-06-17T16:00:00+00:00"),),
        terminal_sessions=_terminal_sessions(
            start=date(2037, 1, 1),
            end=date(2050, 12, 31),
            close_at=time(15),
            excluded_dates=frozenset({date(2042, 6, 6)}),
        ),
    ),
    SyntheticMarketCalendar(
        version_id="synthetic-calendar-v1",
        sessions=tuple(
            MarketSession(
                101 + index,
                _utc("2046-07-02T08:00:00+00:00") + timedelta(days=offset),
            )
            for index, offset in enumerate(
                offset
                for offset in range(30)
                if date(2046, 7, 2).weekday() < 5
                and (date(2046, 7, 2) + timedelta(days=offset)).weekday() < 5
            )
        ),
        monthly_selection_cutoffs=(),
        terminal_sessions=_terminal_sessions(
            start=date(2037, 1, 1),
            end=date(2050, 12, 31),
            close_at=time(8),
        ),
    ),
)
