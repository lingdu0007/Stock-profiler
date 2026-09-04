from __future__ import annotations

import subprocess
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from stock_profiler.bootstrap import settings as settings_module
from stock_profiler.bootstrap.settings import Settings, load_settings


def production_fixture_secret(name: str) -> str:
    """Create deterministic opaque test material that meets the production shape."""
    return sha256(f"public-fixture-{name}".encode()).hexdigest()


def write_production_secrets(directory: Path) -> None:
    """Create non-synthetic local secret files for a production settings test."""
    directory.mkdir()
    for name in ("api_shared_secret", "auth_bootstrap_token", "auth_recovery_token"):
        (directory / name).write_text(
            production_fixture_secret(name),
            encoding="utf-8",
        )


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
    assert settings.api_shared_secret.get_secret_value() == production_fixture_secret(
        "api_shared_secret"
    )
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
    ("secret_name", "unsafe_value"),
    [
        ("api_shared_secret", "too-short"),
        ("auth_bootstrap_token", "dev-value-that-is-long-enough-to-be-rejected"),
        ("api_shared_secret", "synthetic-value-that-is-long-enough-to-be-rejected"),
        ("auth_bootstrap_token", "test-value-that-is-long-enough-to-be-rejected"),
        ("auth_recovery_token", "development-value-that-is-long-enough-to-be-rejected"),
        ("auth_bootstrap_token", "example-value-that-is-long-enough-to-be-rejected"),
        ("auth_recovery_token", "placeholder-value-that-is-long-enough-to-be-rejected"),
        ("api_shared_secret", "invalid.value-that-is-long-enough-to-be-rejected"),
    ],
)
def test_production_settings_reject_short_placeholder_and_malformed_secrets(
    tmp_path: Path, secret_name: str, unsafe_value: str
) -> None:
    secret_directory = tmp_path / "secrets"
    write_production_secrets(secret_directory)
    (secret_directory / secret_name).write_text(unsafe_value, encoding="utf-8")

    with pytest.raises(ValidationError, match="production configuration"):
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
