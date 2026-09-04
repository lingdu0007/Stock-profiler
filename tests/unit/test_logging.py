from __future__ import annotations

from pathlib import Path

import pytest
from structlog.testing import capture_logs

from stock_profiler.bootstrap.settings import Settings
from stock_profiler.entrypoints.process_runner import run_heartbeat_loop
from stock_profiler.foundation.logging import sanitize_log_event


def test_log_allowlist_removes_sensitive_and_unknown_values() -> None:
    event = sanitize_log_event(
        {
            "event": "startup.complete",
            "component": "api",
            "status": "ready",
            "credential": "do-not-log",
            "prompt": "do-not-log",
            "account_id": "do-not-log",
            "unexpected": "do-not-log",
        }
    )

    assert event == {
        "event": "startup.complete",
        "component": "api",
        "status": "ready",
    }


def test_heartbeat_loop_emits_only_allowlisted_operational_log_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(
        environment="test",
        source_sha="a" * 40,
        app_database_url=f"sqlite:///{tmp_path / 'application.sqlite3'}",
        m_agent_run_store_path=tmp_path / "m-agent.sqlite3",
    )
    monkeypatch.setattr(
        "stock_profiler.entrypoints.process_runner.load_settings",
        lambda expected_process_role: settings,
    )

    with capture_logs() as logs:
        run_heartbeat_loop(
            lambda _: {
                "process": "worker",
                "status": "ready",
                "version": {
                    "application_version": "0.1.0.dev0",
                    "source_sha": "a" * 40,
                    "credential": "not-logged",
                },
            },
            expected_process_role="worker",
            once=True,
        )

    assert logs == [
        {
            "event": "process.heartbeat",
            "component": "worker",
            "operation": "heartbeat",
            "status": "ready",
            "version": "0.1.0.dev0",
            "git_sha": "a" * 40,
            "log_level": "info",
        }
    ]
