"""Alembic environment for application-owned SQLite tables only."""

from __future__ import annotations

from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

from stock_profiler.adapters.persistence.runtime_ownership import METADATA
from stock_profiler.bootstrap.settings import load_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = METADATA


def application_database_url() -> str:
    """Use an explicit Alembic URL for tests or the frozen runtime settings."""
    runtime_settings = load_settings(expected_process_role="migrate")
    configured_url = config.get_main_option("sqlalchemy.url")
    if configured_url and configured_url != "sqlite:///./.runtime/stock-profiler.sqlite3":
        return configured_url
    return runtime_settings.app_database_url


def ensure_parent_directory(database_url: str) -> None:
    """Create only the SQLite parent directory; Alembic creates the schema."""
    Path(database_url.removeprefix("sqlite:///")).resolve().parent.mkdir(
        parents=True, exist_ok=True
    )


def run_migrations_offline() -> None:
    """Emit SQL for the application database without accessing M-Agent storage."""
    database_url = application_database_url()
    ensure_parent_directory(database_url)
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations to the application database only."""
    database_url = application_database_url()
    ensure_parent_directory(database_url)
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = database_url
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
