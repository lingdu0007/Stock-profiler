"""CLI-facing operations that call host interfaces directly."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypedDict

from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.foundation.versioning import build_version_bundle

MONITORED_PROCESSES = ("api", "scheduler", "worker")


class DiagnosticSnapshot(TypedDict):
    status: str
    version: dict[str, str]
    processes: Mapping[str, str]


def version_snapshot(settings: Settings) -> dict[str, str]:
    """Return the exact identity also used by HTTP and the Web client."""
    return build_version_bundle(settings).to_dto()


def diagnostic_snapshot(settings: Settings) -> DiagnosticSnapshot:
    """Exercise configuration, persistence ownership, and heartbeat boundaries."""
    runtime = initialize_runtime_storage(settings)
    runtime.write_heartbeat("cli")
    processes = {
        process_name: runtime.heartbeat_status(process_name) for process_name in MONITORED_PROCESSES
    }
    status = "ready" if all(value == "ready" for value in processes.values()) else "not-ready"
    return {
        "status": status,
        "version": build_version_bundle(settings).to_dto(),
        "processes": processes,
    }


def process_health_snapshot(process_name: str, settings: Settings) -> dict[str, object]:
    """Report one long-running process heartbeat for Compose health checks."""
    runtime = initialize_runtime_storage(settings)
    return {
        "process": process_name,
        "status": runtime.heartbeat_status(process_name),
        "version": build_version_bundle(settings).to_dto(),
    }
