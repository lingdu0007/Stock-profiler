"""Shared health and identity output for non-HTTP entrypoints."""

from __future__ import annotations

from typing import TypedDict

from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.foundation.versioning import build_version_bundle


class ProcessSnapshot(TypedDict):
    process: str
    status: str
    version: dict[str, str]


def process_snapshot(process_name: str, settings: Settings) -> ProcessSnapshot:
    """Initialize only operational boundaries and report the common build identity."""
    runtime = initialize_runtime_storage(settings)
    heartbeat = runtime.write_heartbeat(process_name)
    return {
        "process": heartbeat["process"],
        "status": heartbeat["status"],
        "version": build_version_bundle(settings).to_dto(),
    }
