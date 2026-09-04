"""Migration diagnostics only; schema revisions are initialized separately."""

from __future__ import annotations

from stock_profiler.bootstrap.settings import Settings
from stock_profiler.entrypoints.processes import ProcessSnapshot, process_snapshot


def migration_snapshot(settings: Settings) -> ProcessSnapshot:
    """Prove migration uses the same source identity and configuration boundary."""
    return process_snapshot("migrate", settings)
