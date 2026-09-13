from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

PREVIOUS_REVISION = "20260911_0003"


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def test_network_migration_downgrade_and_upgrade(network_database_url: str) -> None:
    config = _config(network_database_url)
    command.downgrade(config, PREVIOUS_REVISION)
    engine = create_engine(network_database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert "network_captures" not in tables
        assert "network_capture_idempotency_keys" not in tables
        assert "network_analysis_results" not in tables
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(network_database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert {
            "network_captures",
            "network_capture_idempotency_keys",
            "network_analysis_results",
        } <= tables
    finally:
        engine.dispose()
