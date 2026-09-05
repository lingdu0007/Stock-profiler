from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from webauthn.helpers import bytes_to_base64url

from stock_profiler.adapters.authentication.passkeys import (
    AUTH_SESSIONS,
    HostGrantPurpose,
    PasskeyAuthenticator,
)
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.entrypoints.http.app import create_app
from stock_profiler.modules.decision_cases.service import run_default_frozen_decision_case

ORIGIN = "https://localhost"
BOOTSTRAP_TOKEN = "bootstrap-token-for-synthetic-test"


def test_authenticated_api_reads_the_same_committed_report_and_never_caches_it(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = run_default_frozen_decision_case(migrated_settings).report
    client, csrf_token = _authenticated_client_with_csrf(migrated_settings, monkeypatch)
    storage = initialize_runtime_storage(migrated_settings)
    with storage.engine.connect() as connection:
        last_seen_before = connection.execute(
            AUTH_SESSIONS.select().with_only_columns(AUTH_SESSIONS.c.last_seen_at)
        ).scalar_one()

    response = client.get(f"/api/v1/reports/{report.report_version_id}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert response.json() == report.model_dump(mode="json")
    assert response.json()["event_id"] == report.event_id
    assert response.json()["framework_run_id"] == report.framework_run_id
    assert response.json()["evidence_clock"]["validated_at"] == "2042-05-17T15:18:00Z"
    with storage.engine.connect() as connection:
        assert (
            connection.execute(
                AUTH_SESSIONS.select().with_only_columns(AUTH_SESSIONS.c.last_seen_at)
            ).scalar_one()
            == last_seen_before
        )

    missing_csrf = client.post(
        "/api/v1/auth/session/refresh",
        headers={"Origin": ORIGIN},
    )
    wrong_origin = client.post(
        "/api/v1/auth/session/refresh",
        headers={"Origin": "https://wrong.example", "X-CSRF-Token": csrf_token},
    )
    refreshed = client.post(
        "/api/v1/auth/session/refresh",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf_token},
    )

    assert missing_csrf.status_code == 403
    assert wrong_origin.status_code == 403
    assert refreshed.status_code == 200
    assert refreshed.json() == {"status": "authenticated"}
    refresh_max_age = int(refreshed.headers["set-cookie"].split("Max-Age=")[1].split(";", 1)[0])
    assert 0 < refresh_max_age <= 2_592_000
    with storage.engine.connect() as connection:
        assert (
            connection.execute(
                AUTH_SESSIONS.select().with_only_columns(AUTH_SESSIONS.c.last_seen_at)
            ).scalar_one()
            != last_seen_before
        )


def test_reports_are_inaccessible_without_a_session_and_logout_requires_origin_and_csrf(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = run_default_frozen_decision_case(migrated_settings).report
    unauthenticated = TestClient(create_app(_auth_settings(migrated_settings)), base_url=ORIGIN)

    denied = unauthenticated.get(f"/api/v1/reports/{report.report_version_id}")

    assert denied.status_code == 401
    assert denied.headers["cache-control"] == "no-store"

    client, csrf_token = _authenticated_client_with_csrf(migrated_settings, monkeypatch)
    missing_csrf = client.delete("/api/v1/auth/session", headers={"Origin": ORIGIN})
    wrong_origin = client.delete(
        "/api/v1/auth/session",
        headers={"Origin": "https://wrong.example", "X-CSRF-Token": csrf_token},
    )
    logged_out = client.delete(
        "/api/v1/auth/session",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf_token},
    )
    after_logout = client.get(f"/api/v1/reports/{report.report_version_id}")

    assert missing_csrf.status_code == 403
    assert wrong_origin.status_code == 403
    assert logged_out.status_code == 204
    assert logged_out.headers["cache-control"] == "no-store"
    assert after_logout.status_code == 401


def test_passkey_ceremonies_require_the_configured_origin_and_the_session_cookie_is_host_only(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _auth_settings(migrated_settings)
    client = TestClient(create_app(settings), base_url=ORIGIN)

    rejected = client.post("/api/v1/auth/host-console/grants")

    assert rejected.status_code == 404

    _, csrf_token = _authenticated_client_with_csrf(migrated_settings, monkeypatch)
    assert csrf_token
    cookie = client.cookies.get("__Host-stock_profiler_session")
    assert cookie is None


def _authenticated_client(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    return _authenticated_client_with_csrf(migrated_settings, monkeypatch)[0]


def _authenticated_client_with_csrf(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, str]:
    settings = _auth_settings(migrated_settings)
    credential_id = bytes_to_base64url(b"synthetic-passkey-credential")
    monkeypatch.setattr(
        "stock_profiler.adapters.authentication.passkeys.verify_registration_response",
        lambda **_: SimpleNamespace(
            credential_id=b"synthetic-passkey-credential",
            credential_public_key=b"synthetic-passkey-public-key",
            sign_count=1,
        ),
    )
    monkeypatch.setattr(
        "stock_profiler.adapters.authentication.passkeys.verify_authentication_response",
        lambda **_: SimpleNamespace(new_sign_count=2),
    )
    client = TestClient(create_app(settings), base_url=ORIGIN)
    grant_id = PasskeyAuthenticator(
        initialize_runtime_storage(settings).engine, settings
    ).create_host_console_grant(HostGrantPurpose.BOOTSTRAP)
    registration = client.post(
        "/api/v1/auth/passkeys/registration/options",
        headers={"Origin": ORIGIN, "X-Host-Console-Grant": grant_id},
    )
    registered = client.post(
        "/api/v1/auth/passkeys/registration/verify",
        headers={"Origin": ORIGIN},
        json={
            "challenge_id": registration.json()["challenge_id"],
            "credential": {"id": credential_id},
        },
    )
    authentication = client.post(
        "/api/v1/auth/passkeys/authentication/options", headers={"Origin": ORIGIN}
    )
    authenticated = client.post(
        "/api/v1/auth/passkeys/authentication/verify",
        headers={"Origin": ORIGIN},
        json={
            "challenge_id": authentication.json()["challenge_id"],
            "credential": {"id": credential_id},
        },
    )

    assert registration.status_code == 200
    assert registered.status_code == 204
    assert authentication.status_code == 200
    assert authenticated.status_code == 200
    assert authenticated.headers["cache-control"] == "no-store"
    assert "__Host-stock_profiler_session=" in authenticated.headers["set-cookie"]
    assert "HttpOnly" in authenticated.headers["set-cookie"]
    assert "Secure" in authenticated.headers["set-cookie"]
    assert "SameSite=lax" in authenticated.headers["set-cookie"]
    assert "Max-Age=2592000" in authenticated.headers["set-cookie"]
    assert "__Host-stock_profiler_csrf=" in authenticated.headers["set-cookie"]
    return client, str(authenticated.json()["csrf_token"])


def _auth_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "auth_origin": ORIGIN,
            "auth_rp_id": "localhost",
            "auth_bootstrap_token": SecretStr(BOOTSTRAP_TOKEN),
            "auth_recovery_token": SecretStr("recovery-token-for-synthetic-test"),
        }
    )
