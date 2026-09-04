"""Heartbeat-only process loop shared by scheduler and worker entrypoints."""

from __future__ import annotations

from collections.abc import Callable
from time import sleep

from stock_profiler.bootstrap.settings import Settings, load_settings
from stock_profiler.entrypoints.processes import ProcessSnapshot
from stock_profiler.foundation.logging import log_operational_event


def run_heartbeat_loop(
    snapshotter: Callable[[Settings], ProcessSnapshot], *, once: bool = False
) -> None:
    """Keep an operational process alive without creating business work."""
    settings = load_settings()
    while True:
        snapshot = snapshotter(settings)
        version = snapshot["version"]
        log_operational_event(
            event="process.heartbeat",
            component=snapshot["process"],
            operation="heartbeat",
            status=snapshot["status"],
            version=version["application_version"],
            git_sha=version["source_sha"],
        )
        if once:
            return
        sleep(30)
