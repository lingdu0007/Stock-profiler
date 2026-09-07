"""Immutable synthetic market-calendar references for portfolio risk evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value)


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


def synthetic_market_calendar(version_id: str) -> SyntheticMarketCalendar | None:
    """Return one known immutable synthetic calendar version, if it exists."""
    return next(
        (calendar for calendar in _SYNTHETIC_MARKET_CALENDARS if calendar.version_id == version_id),
        None,
    )


_SYNTHETIC_MARKET_CALENDARS = (
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
    ),
)
