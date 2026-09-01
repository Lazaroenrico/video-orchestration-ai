"""Execução programática das migrações Alembic."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _sqlalchemy_url(database_url: str) -> str:
    if database_url.startswith("postgresql+psycopg://"):
        return database_url
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    raise ValueError("DATABASE_URL deve usar postgresql://")


def upgrade_database(database_url: str, revision: str = "head") -> None:
    """Aplica migrações até ``revision``; a operação é idempotente."""
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.attributes["configure_logger"] = False
    config.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", _sqlalchemy_url(database_url).replace("%", "%%"))
    command.upgrade(config, revision)
