"""Immutable startup configuration with production fail-closed validation."""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path
from re import fullmatch
from typing import Literal
from urllib.parse import urlparse, urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from stock_profiler.foundation.logging import configure_logging

ZERO_SHA = "0" * 40
ProcessRole = Literal["api", "scheduler", "worker", "migrate", "cli"]
SECRET_SETTING_FIELDS = frozenset(
    {"api_shared_secret", "auth_bootstrap_token", "auth_recovery_token"}
)
SECRET_ENVIRONMENT_VARIABLES = tuple(
    f"STOCK_PROFILER_{field_name.upper()}" for field_name in SECRET_SETTING_FIELDS
)
MINIMUM_PRODUCTION_SECRET_LENGTH = 32
PUBLIC_SECRET_MARKERS = frozenset(
    {
        "dev",
        "development",
        "example",
        "placeholder",
        "synthetic",
        "test",
    }
)
RESERVED_AUTH_HOSTNAMES = frozenset({"localhost", "example", "invalid", "test"})
RESERVED_AUTH_HOST_SUFFIXES = (".example", ".invalid", ".localhost", ".test")
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def is_valid_rp_id(value: str) -> bool:
    """Accept only DNS-shaped relying-party identifiers."""
    if len(value) > 253:
        return False
    labels = value.split(".")
    return bool(labels) and all(
        fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is not None for label in labels
    )


def uses_reserved_auth_hostname(value: str) -> bool:
    """Identify documentation-only hostnames that cannot be production origins."""
    return value in RESERVED_AUTH_HOSTNAMES or value.endswith(RESERVED_AUTH_HOST_SUFFIXES)


def is_valid_production_secret(value: str) -> bool:
    """Accept opaque, non-placeholder secret material from a mounted secret file."""
    return (
        len(value) >= MINIMUM_PRODUCTION_SECRET_LENGTH
        and fullmatch(r"[A-Za-z0-9_-]+", value) is not None
        and not any(marker in value.casefold() for marker in PUBLIC_SECRET_MARKERS)
    )


def discover_source_sha() -> str:
    """Use the checked-out revision for local diagnostics when no image SHA is injected."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ZERO_SHA
    source_sha = completed.stdout.strip()
    return source_sha if fullmatch(r"[0-9a-f]{40}", source_sha) else ZERO_SHA


class EnvironmentWithoutSecretValues(PydanticBaseSettingsSource):
    """Delegate non-secret configuration to the environment source only."""

    def __init__(self, source: PydanticBaseSettingsSource) -> None:
        super().__init__(source.settings_cls)
        self.source = source

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[object, str, bool]:
        if field_name in SECRET_SETTING_FIELDS:
            return None, field_name, False
        return self.source.get_field_value(field, field_name)

    def __call__(self) -> dict[str, object]:
        return {
            field_name: value
            for field_name, value in self.source().items()
            if field_name not in SECRET_SETTING_FIELDS
        }


class SecretFilesOnlySource(PydanticBaseSettingsSource):
    """Read only declared sensitive fields from the mounted secret directory."""

    def __init__(self, source: PydanticBaseSettingsSource) -> None:
        super().__init__(source.settings_cls)
        self.secrets_dir = Path(str(source.secrets_dir))  # type: ignore[attr-defined]

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[object, str, bool]:
        if field_name not in SECRET_SETTING_FIELDS:
            return None, field_name, False
        path = self.secrets_dir / field_name
        if not path.is_file():
            return None, field_name, False
        return path.read_text(encoding="utf-8").strip(), field_name, False

    def __call__(self) -> dict[str, object]:
        return {
            field_name: value
            for field_name, field in self.settings_cls.model_fields.items()
            if (value := self.get_field_value(field, field_name)[0]) is not None
        }


class Settings(BaseSettings):
    """Settings are loaded once and may not be mutated after startup."""

    model_config = SettingsConfigDict(
        env_prefix="STOCK_PROFILER_",
        env_file=None,
        secrets_dir="/run/secrets",
        frozen=True,
        extra="forbid",
    )

    environment: Literal["development", "test", "production"] = "development"
    process_role: ProcessRole = "api"
    source_sha: str = Field(default_factory=discover_source_sha)
    configuration_version: str = "0.1.0.dev0"
    app_database_url: str = "sqlite:///./.runtime/stock-profiler.sqlite3"
    m_agent_run_store_path: Path = Path("./.runtime/m-agent-runs.sqlite3")
    api_shared_secret: SecretStr | None = None
    auth_rp_id: str = "localhost"
    auth_origin: str = "http://localhost"
    auth_session_cookie_name: str = "__Host-stock_profiler_session"
    auth_challenge_ttl_seconds: int = 5 * 60
    auth_session_idle_ttl_seconds: int = 7 * 24 * 60 * 60
    auth_session_absolute_ttl_seconds: int = 30 * 24 * 60 * 60
    auth_recent_reauthentication_ttl_seconds: int = 10 * 60
    auth_bootstrap_token: SecretStr | None = None
    auth_recovery_token: SecretStr | None = None
    auth_bootstrap_token_ttl_seconds: int = 10 * 60
    auth_recovery_token_ttl_seconds: int = 10 * 60

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Keep sensitive values out of environment-variable settings sources."""
        del settings_cls, dotenv_settings
        return (
            init_settings,
            EnvironmentWithoutSecretValues(env_settings),
            SecretFilesOnlySource(file_secret_settings),
        )

    @property
    def application_database_file_path(self) -> Path:
        """Return one direct SQLite file path and reject URL indirection."""
        parsed_url = urlsplit(self.app_database_url)
        if parsed_url.query or parsed_url.fragment:
            raise ValueError("application database must use a direct SQLite file URL")
        try:
            database_url = make_url(self.app_database_url)
        except ArgumentError as error:
            raise ValueError("application database must use a direct SQLite file URL") from error
        database_path = database_url.database
        if (
            database_url.drivername != "sqlite"
            or database_path in (None, "", ":memory:")
            or database_url.query
            or database_url.host is not None
            or database_url.username is not None
            or database_url.password is not None
        ):
            raise ValueError("application database must use a direct SQLite file URL")
        assert database_path is not None
        return Path(database_path)

    @property
    def application_database_path(self) -> Path:
        """Return the local file represented by the restricted SQLite URL."""
        return self.application_database_file_path.resolve()

    @property
    def resolved_m_agent_run_store_path(self) -> Path:
        """Return the independent M-Agent SQLite path."""
        return self.m_agent_run_store_path.resolve()

    @model_validator(mode="after")
    def validate_runtime_boundary(self) -> Settings:
        application_database_file_path = self.application_database_file_path
        if not fullmatch(r"[0-9a-f]{40}", self.source_sha):
            raise ValueError("source_sha must be a complete lowercase Git SHA")
        if self.application_database_path == self.resolved_m_agent_run_store_path:
            raise ValueError("application and M-Agent SQLite paths must be distinct")
        if any(variable in os.environ for variable in SECRET_ENVIRONMENT_VARIABLES):
            raise ValueError("secret values must be supplied from /run/secrets files")
        if self.environment == "production":
            secret = self.api_shared_secret.get_secret_value() if self.api_shared_secret else ""
            bootstrap_token = (
                self.auth_bootstrap_token.get_secret_value() if self.auth_bootstrap_token else ""
            )
            recovery_token = (
                self.auth_recovery_token.get_secret_value() if self.auth_recovery_token else ""
            )
            origin = urlparse(self.auth_origin)
            try:
                origin_port = origin.port
                has_valid_port = True
            except ValueError:
                origin_port = None
                has_valid_port = False
            has_valid_origin = (
                origin.scheme == "https"
                and origin.hostname is not None
                and origin.username is None
                and origin.password is None
                and origin.path in ("", "/")
                and not origin.params
                and not origin.query
                and not origin.fragment
                and has_valid_port
                and (origin_port is None or 0 <= origin_port <= 65535)
            )
            has_valid_rp_id = is_valid_rp_id(self.auth_rp_id)
            origin_matches_rp_id = bool(
                origin.hostname
                and (
                    origin.hostname == self.auth_rp_id
                    or origin.hostname.endswith(f".{self.auth_rp_id}")
                )
            )
            has_reserved_auth_hostname = bool(
                origin.hostname
                and (
                    uses_reserved_auth_hostname(self.auth_rp_id)
                    or uses_reserved_auth_hostname(origin.hostname)
                )
            )
            production_invalid = (
                self.source_sha == ZERO_SHA
                or self.configuration_version != "0.1.0.dev0"
                or not application_database_file_path.is_absolute()
                or not self.m_agent_run_store_path.is_absolute()
            )
            authentication_invalid = (
                not is_valid_production_secret(secret)
                or not has_valid_rp_id
                or not has_valid_origin
                or not origin_matches_rp_id
                or has_reserved_auth_hostname
                or self.auth_session_cookie_name != "__Host-stock_profiler_session"
                or self.auth_challenge_ttl_seconds != 5 * 60
                or self.auth_session_idle_ttl_seconds != 7 * 24 * 60 * 60
                or self.auth_session_absolute_ttl_seconds != 30 * 24 * 60 * 60
                or self.auth_recent_reauthentication_ttl_seconds != 10 * 60
                or self.auth_bootstrap_token_ttl_seconds != 10 * 60
                or self.auth_recovery_token_ttl_seconds != 10 * 60
                or not is_valid_production_secret(bootstrap_token)
                or not is_valid_production_secret(recovery_token)
            )
            production_invalid = production_invalid or (
                self.process_role == "api" and authentication_invalid
            )
            if production_invalid:
                raise ValueError("production configuration is incomplete or unsafe")
        return self


@lru_cache(maxsize=5)
def load_settings(expected_process_role: ProcessRole | None = None) -> Settings:
    """Freeze settings and reject a process that does not match its declared entrypoint role."""
    settings = Settings()
    if expected_process_role is not None and settings.process_role != expected_process_role:
        raise ValueError(
            f"process role mismatch: expected {expected_process_role}, got {settings.process_role}"
        )
    configure_logging(development=settings.environment == "development")
    return settings
