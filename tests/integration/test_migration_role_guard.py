from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from stock_profiler.bootstrap.settings import load_settings

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("override_database_url", (False, True))
def test_migration_rejects_non_migrate_role_for_all_database_url_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    override_database_url: bool,
) -> None:
    """Alembic must bind to its role before accepting any configured URL."""
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "worker")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    if override_database_url:
        config.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path / 'application.sqlite3'}")

    with pytest.raises(ValueError, match="expected migrate, got worker"):
        command.upgrade(config, "head", sql=True)

    load_settings.cache_clear()
