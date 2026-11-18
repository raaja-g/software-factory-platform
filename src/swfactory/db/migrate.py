"""Run Alembic programmatically (no alembic.ini). Only schema ``app``; never touches ``lg``."""

from typing import Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine


def include_name(name: str | None, type_: str, parent_names: Any) -> bool:
    """Alembic sees schema ``app`` only; ``lg`` belongs to the LangGraph checkpointer."""
    if type_ == "schema":
        return name == "app"
    return True


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    return getattr(obj, "schema", None) != "lg"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", "swfactory:migrations")
    cfg.attributes["url"] = url
    return cfg


def upgrade_database(url: str, revision: str = "head") -> str:
    """Upgrade to ``revision`` and return the revision the database is at afterwards."""
    command.upgrade(alembic_config(url), revision)
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"version_table_schema": "app"})
            return ctx.get_current_revision() or ""
    finally:
        engine.dispose()
