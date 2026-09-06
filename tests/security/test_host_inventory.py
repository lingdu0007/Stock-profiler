from __future__ import annotations

import sys
from typing import Any, cast

import pytest
from starlette.routing import Route

from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
from stock_profiler.bootstrap.settings import Settings, load_settings
from stock_profiler.entrypoints import cli
from stock_profiler.entrypoints.http.app import create_app


@pytest.mark.parametrize("operation", ["generate", "prefill", "submit", "modify", "cancel"])
def test_actual_cli_rejects_and_audits_order_commands(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    monkeypatch.setenv("STOCK_PROFILER_APP_DATABASE_URL", migrated_settings.app_database_url)
    monkeypatch.setenv(
        "STOCK_PROFILER_M_AGENT_RUN_STORE_PATH", str(migrated_settings.m_agent_run_store_path)
    )
    load_settings.cache_clear()
    monkeypatch.setattr(sys, "argv", ["stock-profiler", operation + "-order"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    audit = ResultDelivery.from_settings(migrated_settings).audit_history()
    assert len(audit) == 1
    assert audit[0].surface == "CLI"
    assert audit[0].reason == "UNDECLARED_CAPABILITY"


def test_inventory_is_the_exact_registered_http_and_cli_surface(
    migrated_settings: Settings, security_cases: dict[str, Any]
) -> None:
    app = create_app(migrated_settings)
    assert all(isinstance(route, Route) for route in app.routes)
    assert sorted(cast(Route, route).path for route in app.routes) == sorted(
        [
            "/openapi.json",
            "/livez",
            "/readyz",
            "/api/v1/diagnostics/version",
            "/api/v1/diagnostics/safety-capabilities",
            "/api/v1/auth/passkeys/registration/options",
            "/api/v1/auth/passkeys/registration/verify",
            "/api/v1/auth/passkeys/authentication/options",
            "/api/v1/auth/passkeys/authentication/verify",
            "/api/v1/auth/passkeys/reauthentication/options",
            "/api/v1/auth/passkeys/reauthentication/verify",
            "/api/v1/auth/session/refresh",
            "/api/v1/auth/session",
            "/api/v1/reports/{report_version_id}",
            "/api/v1/reports/{report_version_id}/facts",
            "/api/v1/reports/{report_version_id}/facts",
        ]
    )
    assert cli.CLI_COMMANDS == (
        "version",
        "doctor",
        "process-health",
        "decision-case-run",
        "decision-case-replay",
        "decision-case-correct",
        "host-console-grant",
    )
    expected_methods = {
        "/openapi.json": {"GET", "HEAD"},
        "/livez": {"GET"},
        "/readyz": {"GET"},
        "/api/v1/diagnostics/version": {"GET"},
        "/api/v1/diagnostics/safety-capabilities": {"GET"},
        "/api/v1/auth/passkeys/registration/options": {"POST"},
        "/api/v1/auth/passkeys/registration/verify": {"POST"},
        "/api/v1/auth/passkeys/authentication/options": {"POST"},
        "/api/v1/auth/passkeys/authentication/verify": {"POST"},
        "/api/v1/auth/passkeys/reauthentication/options": {"POST"},
        "/api/v1/auth/passkeys/reauthentication/verify": {"POST"},
        "/api/v1/auth/session/refresh": {"POST"},
        "/api/v1/auth/session": {"DELETE"},
        "/api/v1/reports/{report_version_id}": {"GET"},
        "/api/v1/reports/{report_version_id}/facts": {"GET", "POST"},
    }
    assert {
        (cast(Route, route).path, method)
        for route in app.routes
        for method in (cast(Route, route).methods or set())
    } == {(path, method) for path, methods in expected_methods.items() for method in methods}
    assert security_cases["order_operations"] == [
        "generate",
        "prefill",
        "submit",
        "modify",
        "cancel",
    ]
