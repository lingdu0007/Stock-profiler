from __future__ import annotations

from stock_profiler.bootstrap.settings import Settings
from stock_profiler.entrypoints.cli.service import (
    diagnostic_snapshot,
    process_health_snapshot,
    version_snapshot,
)
from stock_profiler.entrypoints.migrate.service import migration_snapshot
from stock_profiler.entrypoints.processes import process_snapshot
from stock_profiler.entrypoints.scheduler.service import scheduler_snapshot
from stock_profiler.entrypoints.worker.service import worker_snapshot


def test_all_entrypoints_report_the_same_build_identity(migrated_settings: Settings) -> None:
    expected = version_snapshot(migrated_settings)

    assert diagnostic_snapshot(migrated_settings)["version"] == expected
    assert scheduler_snapshot(migrated_settings)["version"] == expected
    assert worker_snapshot(migrated_settings)["version"] == expected
    assert migration_snapshot(migrated_settings)["version"] == expected


def test_doctor_and_process_health_report_missing_and_current_heartbeats(
    migrated_settings: Settings,
) -> None:
    assert process_health_snapshot("scheduler", migrated_settings)["status"] == "missing"
    assert diagnostic_snapshot(migrated_settings)["status"] == "not-ready"

    process_snapshot("api", migrated_settings)
    scheduler_snapshot(migrated_settings)
    worker_snapshot(migrated_settings)

    assert process_health_snapshot("scheduler", migrated_settings)["status"] == "ready"
    assert diagnostic_snapshot(migrated_settings)["status"] == "ready"
