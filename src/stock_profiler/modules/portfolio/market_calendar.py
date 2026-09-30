"""Immutable synthetic market-calendar references for portfolio risk evidence."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value)


def six_month_terminal_evaluation_at(value: datetime, market_calendar_version: str) -> datetime:
    """Resolve the six-month target to the last saved session close on or before its date."""
    anchor = value.astimezone(UTC)
    month_index = anchor.year * 12 + anchor.month - 1 + 6
    year, month_zero = divmod(month_index, 12)
    target = anchor.replace(
        year=year,
        month=month_zero + 1,
        day=min(anchor.day, monthrange(year, month_zero + 1)[1]),
    )
    calendar = synthetic_market_calendar(market_calendar_version)
    if calendar is None:
        raise ValueError("MARKET_CALENDAR_VERSION_UNSUPPORTED")
    if target.date() > calendar.terminal_session_coverage_through:
        raise ValueError("MARKET_CALENDAR_TERMINAL_SESSION_UNAVAILABLE")
    session = calendar.last_terminal_session_on_or_before(target.date())
    if session is None:
        raise ValueError("MARKET_CALENDAR_TERMINAL_SESSION_UNAVAILABLE")
    return session.closed_at


def market_session_close_on(
    market_date: date,
    market_calendar_version: str,
) -> datetime | None:
    """Return the saved session close, preserving gaps inside active-calendar coverage."""
    calendar = synthetic_market_calendar(market_calendar_version)
    if calendar is None:
        return None
    if calendar.sessions and calendar.sessions[0].closed_at.date() <= market_date <= (
        calendar.sessions[-1].closed_at.date()
    ):
        sessions = calendar.sessions
    else:
        sessions = calendar.terminal_sessions or calendar.sessions
    session = next((item for item in sessions if item.closed_at.date() == market_date), None)
    return session.closed_at if session is not None else None


def next_market_session_open_after(
    value: datetime,
    market_calendar_version: str,
) -> datetime:
    """Return the first saved session open strictly after the frozen cutoff."""
    calendar = synthetic_market_calendar(market_calendar_version)
    if calendar is None:
        raise ValueError("MARKET_CALENDAR_VERSION_UNSUPPORTED")
    cutoff_date = value.astimezone(UTC).date()
    if calendar.sessions and calendar.sessions[0].closed_at.date() <= cutoff_date <= (
        calendar.sessions[-1].closed_at.date()
    ):
        sessions = calendar.sessions
    else:
        sessions = calendar.terminal_sessions or calendar.sessions
    session = next((item for item in sessions if item.closed_at - timedelta(hours=7) > value), None)
    if session is None and sessions is calendar.sessions and calendar.terminal_sessions:
        session = next(
            (
                item
                for item in calendar.terminal_sessions
                if item.closed_at - timedelta(hours=7) > value
            ),
            None,
        )
    if session is None:
        raise ValueError("MARKET_CALENDAR_ENTRY_WINDOW_UNAVAILABLE")
    return session.closed_at - timedelta(hours=7)


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
    terminal_session_coverage_through: date
    terminal_sessions: tuple[MarketSession, ...] = ()

    def session_for(self, ordinal: int) -> MarketSession | None:
        return next((session for session in self.sessions if session.ordinal == ordinal), None)

    def next_session_after(self, closed_at: datetime) -> MarketSession | None:
        return next(
            (session for session in self.sessions if session.closed_at > closed_at),
            None,
        )

    def sessions_after_open(self, value: datetime, count: int) -> tuple[MarketSession, ...]:
        """Return the next sessions across saved and extended calendar coverage."""
        if count <= 0:
            return ()
        if self.sessions:
            first_date = self.sessions[0].closed_at.date()
            last_date = self.sessions[-1].closed_at.date()
            extended_sessions = self.terminal_sessions or self.sessions
            sessions = (
                tuple(
                    session
                    for session in extended_sessions
                    if session.closed_at.date() < first_date
                )
                + self.sessions
                + tuple(
                    session for session in extended_sessions if session.closed_at.date() > last_date
                )
            )
        else:
            sessions = self.terminal_sessions
        return tuple(
            session for session in sessions if session.closed_at - timedelta(hours=7) > value
        )[:count]

    def session_at_or_before(self, value: datetime) -> MarketSession | None:
        """Resolve the latest completed session using the same coverage precedence."""
        value_date = value.astimezone(UTC).date()
        if self.sessions and self.sessions[0].closed_at.date() <= value_date <= (
            self.sessions[-1].closed_at.date()
        ):
            completed_sessions = tuple(
                session for session in self.sessions if session.closed_at <= value
            )
            if completed_sessions:
                return completed_sessions[-1]
            first_saved_date = self.sessions[0].closed_at.date()
            prior_sessions = tuple(
                session
                for session in self.terminal_sessions
                if session.closed_at.date() < first_saved_date and session.closed_at <= value
            )
            return prior_sessions[-1] if prior_sessions else None
        extended_sessions = self.terminal_sessions or self.sessions
        completed_sessions = tuple(
            session for session in extended_sessions if session.closed_at <= value
        )
        return completed_sessions[-1] if completed_sessions else None

    def next_monthly_selection_cutoff_after(self, closed_at: datetime) -> datetime | None:
        return next(
            (cutoff for cutoff in self.monthly_selection_cutoffs if cutoff > closed_at),
            None,
        )

    def last_terminal_session_on_or_before(self, target_date: date) -> MarketSession | None:
        sessions = self.terminal_sessions or self.sessions
        if self.sessions and self.sessions[0].closed_at.date() <= target_date <= (
            self.sessions[-1].closed_at.date()
        ):
            sessions = self.sessions
        return next(
            (session for session in reversed(sessions) if session.closed_at.date() <= target_date),
            None,
        )


def _weekday_sessions(
    *,
    start: date,
    end: date,
    close_at: time,
    first_ordinal: int,
    excluded_dates: frozenset[date] = frozenset(),
) -> tuple[MarketSession, ...]:
    """Build ordered synthetic sessions within the frozen calendar coverage."""
    sessions: list[MarketSession] = []
    current = start
    while current <= end:
        if current.weekday() < 5 and current not in excluded_dates:
            closed_at = datetime.combine(current, close_at, tzinfo=UTC)
            ordinal = first_ordinal + len(sessions)
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
        terminal_session_coverage_through=date(2050, 12, 31),
        terminal_sessions=_weekday_sessions(
            start=date(2037, 1, 1),
            end=date(2050, 12, 31),
            close_at=time(15),
            first_ordinal=1_000_000,
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
        ),
        monthly_selection_cutoffs=(_utc("2042-06-17T16:00:00+00:00"),),
        terminal_session_coverage_through=date(2050, 12, 31),
        terminal_sessions=_weekday_sessions(
            start=date(2037, 1, 1),
            end=date(2050, 12, 31),
            close_at=time(15),
            first_ordinal=1_000_000,
            excluded_dates=frozenset({date(2042, 6, 6)}),
        ),
    ),
    SyntheticMarketCalendar(
        version_id="synthetic-market-calendar-v2",
        sessions=_weekday_sessions(
            start=date(2042, 5, 20),
            end=date(2042, 8, 26),
            close_at=time(15),
            first_ordinal=6101,
            excluded_dates=frozenset({date(2042, 6, 6)}),
        ),
        monthly_selection_cutoffs=(_utc("2042-06-17T16:00:00+00:00"),),
        terminal_session_coverage_through=date(2050, 12, 31),
        terminal_sessions=_weekday_sessions(
            start=date(2037, 1, 1),
            end=date(2050, 12, 31),
            close_at=time(15),
            first_ordinal=1_000_000,
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
        terminal_session_coverage_through=date(2050, 12, 31),
        terminal_sessions=_weekday_sessions(
            start=date(2037, 1, 1),
            end=date(2050, 12, 31),
            close_at=time(8),
            first_ordinal=1_000_000,
        ),
    ),
)
