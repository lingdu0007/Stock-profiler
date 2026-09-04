"""Worker diagnostics only; durable product work is intentionally deferred."""

from __future__ import annotations

from stock_profiler.bootstrap.settings import Settings
from stock_profiler.entrypoints.processes import ProcessSnapshot, process_snapshot


def worker_snapshot(settings: Settings) -> ProcessSnapshot:
    """Prove common composition without creating agent or business work."""
    return process_snapshot("worker", settings)
