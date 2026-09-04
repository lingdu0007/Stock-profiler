from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from stock_profiler.bootstrap.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        source_sha="a" * 40,
        app_database_url=f"sqlite:///{tmp_path / 'stock-profiler.sqlite3'}",
        m_agent_run_store_path=tmp_path / "m-agent-runs.sqlite3",
    )


@pytest.fixture
def migrated_settings(settings: Settings) -> Settings:
    """Apply the application-owned Alembic revision before readiness tests."""
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "head")
    return settings
