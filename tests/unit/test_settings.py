from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from stock_profiler.bootstrap import settings as settings_module
from stock_profiler.bootstrap.settings import Settings, load_settings


def write_production_secrets(directory: Path) -> None:
    """Create non-synthetic local secret files for a production settings test."""
    directory.mkdir()
    (directory / "api_shared_secret").write_text("api-shared-secret", encoding="utf-8")
    (directory / "auth_bootstrap_token").write_text("bootstrap-token", encoding="utf-8")
    (directory / "auth_recovery_token").write_text("recovery-token", encoding="utf-8")


def production_settings_kwargs(tmp_path: Path) -> dict[str, object]:
    """Return the complete public production configuration contract."""
    return {
        "environment": "production",
        "source_sha": "a" * 40,
        "app_database_url": f"sqlite:///{tmp_path / 'stock-profiler.sqlite3'}",
        "m_agent_run_store_path": tmp_path / "m-agent-runs.sqlite3",
        "auth_rp_id": "profiler.private",
        "auth_origin": "https://profiler.private",
    }


def load_production_settings(secret_directory: Path, values: dict[str, object]) -> Settings:
    """Construct settings through pydantic-settings' test-only secret-dir override."""
    return Settings(_secrets_dir=secret_directory, **values)  # type: ignore[call-arg, arg-type]


def test_production_settings_fail_closed_without_a_real_secret(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="production configuration"):
        Settings(
            environment="production",
            source_sha="0" * 40,
            app_database_url=f"sqlite:///{tmp_path / 'stock-profiler.sqlite3'}",
            m_agent_run_store_path=tmp_path / "m-agent-runs.sqlite3",
        )


def test_production_settings_load_secrets_only_from_a_secret_directory(tmp_path: Path) -> None:
    secret_directory = tmp_path / "secrets"
    write_production_secrets(secret_directory)

    settings = load_production_settings(secret_directory, production_settings_kwargs(tmp_path))

    assert settings.api_shared_secret is not None
    assert settings.api_shared_secret.get_secret_value() == "api-shared-secret"
    assert settings.auth_bootstrap_token is not None
    assert settings.auth_recovery_token is not None


def test_production_settings_reject_secret_environment_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_directory = tmp_path / "secrets"
    write_production_secrets(secret_directory)
    monkeypatch.setenv("STOCK_PROFILER_API_SHARED_SECRET", "environment-secret")

    with pytest.raises(ValidationError, match="secret values must be supplied"):
        load_production_settings(secret_directory, production_settings_kwargs(tmp_path))


@pytest.mark.parametrize(
    ("override", "expected_message"),
    [
        ({"app_database_url": "sqlite:///relative.sqlite3"}, "production configuration"),
        ({"m_agent_run_store_path": Path("relative-m-agent.sqlite3")}, "production configuration"),
        ({"auth_rp_id": "not a relying-party id"}, "production configuration"),
        (
            {"auth_rp_id": "a..private", "auth_origin": "https://a..private"},
            "production configuration",
        ),
        (
            {"auth_rp_id": "a-.private", "auth_origin": "https://a-.private"},
            "production configuration",
        ),
        (
            {"auth_rp_id": "a.-private", "auth_origin": "https://a.-private"},
            "production configuration",
        ),
        ({"auth_origin": "https://"}, "production configuration"),
        ({"auth_origin": "https://unrelated.private"}, "production configuration"),
        (
            {
                "auth_rp_id": "profiler.example.invalid",
                "auth_origin": "https://profiler.example.invalid",
            },
            "production configuration",
        ),
        ({"auth_bootstrap_token_ttl_seconds": 599}, "production configuration"),
        ({"auth_recovery_token_ttl_seconds": 599}, "production configuration"),
    ],
)
def test_production_settings_reject_unsafe_paths_and_authentication_configuration(
    tmp_path: Path, override: dict[str, object], expected_message: str
) -> None:
    secret_directory = tmp_path / "secrets"
    write_production_secrets(secret_directory)
    values = production_settings_kwargs(tmp_path)
    values.update(override)

    with pytest.raises(ValidationError, match=expected_message):
        load_production_settings(secret_directory, values)


def test_runtime_store_must_not_share_the_application_database(tmp_path: Path) -> None:
    database = tmp_path / "shared.sqlite3"

    with pytest.raises(ValidationError, match="must be distinct"):
        Settings(
            environment="test",
            source_sha="a" * 40,
            app_database_url=f"sqlite:///{database}",
            m_agent_run_store_path=database,
        )


@pytest.mark.parametrize(
    "app_database_url",
    [
        "sqlite:///:memory:",
        "sqlite:///relative.sqlite3?timeout=5",
    ],
)
def test_application_database_url_must_identify_one_direct_sqlite_file(
    tmp_path: Path, app_database_url: str
) -> None:
    with pytest.raises(ValidationError, match="direct SQLite file URL"):
        Settings(
            environment="test",
            source_sha="a" * 40,
            app_database_url=app_database_url,
            m_agent_run_store_path=tmp_path / "m-agent-runs.sqlite3",
        )


def test_application_database_url_query_cannot_hide_the_m_agent_store(tmp_path: Path) -> None:
    run_store = tmp_path / "m-agent-runs.sqlite3"

    with pytest.raises(ValidationError, match="direct SQLite file URL"):
        Settings(
            environment="test",
            source_sha="a" * 40,
            app_database_url=f"sqlite:///{run_store}?timeout=5",
            m_agent_run_store_path=run_store,
        )


def test_source_sha_discovery_uses_the_complete_git_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_sha = "b" * 40
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"stdout": f"{expected_sha}\n"})(),
    )

    assert settings_module.discover_source_sha() == expected_sha


def test_authentication_configuration_contract_is_frozen_without_enabling_authentication(
    tmp_path: Path,
) -> None:
    settings = Settings(
        environment="test",
        source_sha="a" * 40,
        app_database_url=f"sqlite:///{tmp_path / 'stock-profiler.sqlite3'}",
        m_agent_run_store_path=tmp_path / "m-agent-runs.sqlite3",
    )

    assert settings.auth_rp_id == "localhost"
    assert settings.auth_origin == "http://localhost"
    assert settings.auth_session_cookie_name == "__Host-stock_profiler_session"
    assert settings.auth_challenge_ttl_seconds == 300
    assert settings.auth_session_idle_ttl_seconds == 7 * 24 * 60 * 60
    assert settings.auth_session_absolute_ttl_seconds == 30 * 24 * 60 * 60
    assert settings.auth_recent_reauthentication_ttl_seconds == 10 * 60
    assert settings.auth_bootstrap_token_ttl_seconds == 10 * 60
    assert settings.auth_recovery_token_ttl_seconds == 10 * 60


def test_settings_load_configures_privacy_safe_structlog(monkeypatch: pytest.MonkeyPatch) -> None:
    configured: list[bool] = []
    load_settings.cache_clear()
    monkeypatch.setattr(
        settings_module,
        "configure_logging",
        lambda *, development: configured.append(development),
    )

    load_settings()

    assert configured == [True]
    load_settings.cache_clear()
