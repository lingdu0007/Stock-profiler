"""Scheduler diagnostics only; business scheduling is intentionally deferred."""

from __future__ import annotations

from stock_profiler.bootstrap.settings import Settings
from stock_profiler.entrypoints.processes import ProcessSnapshot, process_snapshot


def scheduler_snapshot(settings: Settings) -> ProcessSnapshot:
    """Prove common composition without scheduling any business work."""
    return process_snapshot("scheduler", settings)
