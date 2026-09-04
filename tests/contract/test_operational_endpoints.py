from __future__ import annotations

from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from structlog.testing import capture_logs

from stock_profiler.bootstrap.settings import Settings
from stock_profiler.entrypoints.http.app import create_app
from stock_profiler.entrypoints.process_runner import run_heartbeat_loop
from stock_profiler.entrypoints.processes import ProcessSnapshot


def test_health_endpoints_are_operational_without_business_state(
    migrated_settings: Settings,
) -> None:
    client = TestClient(create_app(migrated_settings))

    assert client.get("/livez").json() == {"status": "live"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_readiness_fails_closed_when_the_application_database_is_not_migrated(
    settings: Settings,
) -> None:
    client = TestClient(create_app(settings), raise_server_exceptions=False)

    assert client.get("/readyz").status_code == 500


def test_heartbeat_runner_can_report_once_without_scheduling_work(
    settings: Settings, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "stock_profiler.entrypoints.process_runner.load_settings",
        lambda: settings,
    )

    def snapshot(_: Settings) -> ProcessSnapshot:
        return {
            "process": "scheduler",
            "status": "ready",
            "version": {
                "application_version": "0.1.0.dev0",
                "source_sha": "a" * 40,
            },
        }

    with capture_logs() as logs:
        run_heartbeat_loop(snapshot, once=True)

    assert logs == [
        {
            "event": "process.heartbeat",
            "component": "scheduler",
            "operation": "heartbeat",
            "status": "ready",
            "version": "0.1.0.dev0",
            "git_sha": "a" * 40,
            "log_level": "info",
        }
    ]
