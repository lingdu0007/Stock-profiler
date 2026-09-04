from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from stock_profiler.adapters.persistence.runtime_ownership import (
    HEARTBEATS,
    initialize_runtime_storage,
)
from stock_profiler.bootstrap.settings import Settings


def test_runtime_storage_uses_two_distinct_sqlite_files(migrated_settings: Settings) -> None:
    runtime = initialize_runtime_storage(migrated_settings)

    assert runtime.application_database_path != runtime.m_agent_run_store_path
    assert runtime.application_database_path.exists()
    assert runtime.m_agent_run_store_path.exists()


def test_runtime_storage_reports_missing_current_and_stale_process_heartbeats(
    migrated_settings: Settings,
) -> None:
    runtime = initialize_runtime_storage(migrated_settings)

    assert runtime.heartbeat_status("scheduler") == "missing"
    runtime.write_heartbeat("scheduler")
    assert runtime.heartbeat_status("scheduler") == "ready"

    stale_at = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    with runtime.engine.begin() as connection:
        connection.execute(
            update(HEARTBEATS)
            .where(HEARTBEATS.c.process_name == "scheduler")
            .values(observed_at=stale_at)
        )

    assert runtime.heartbeat_status("scheduler") == "stale"
