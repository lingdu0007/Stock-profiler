"""Small injectable clock boundary for deterministic time-sensitive behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Provide the current UTC instant without binding policy to wall-clock access."""

    def now(self) -> datetime:
        """Return a timezone-aware current instant."""


class UtcClock:
    """Production clock backed by the system UTC wall clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)
