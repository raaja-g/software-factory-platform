"""Alembic environment: schema ``app`` only; ``lg`` belongs to the LangGraph checkpointer."""

from alembic import context
from sqlalchemy import create_engine, text

from swfactory.db.migrate import include_name, include_object
from swfactory.db.tables import Base

config = context.config


def _url() -> str:
    url = config.attributes.get("url")
    if url:
        return url
    from swfactory.settings import get_settings

    return get_settings().sqlalchemy_url()


def run_migrations() -> None:
    engine = create_engine(_url())
    try:
        with engine.connect() as connection:
            connection.execute(text("CREATE SCHEMA IF NOT EXISTS app"))
            connection.commit()
            context.configure(
                connection=connection,
                target_metadata=Base.metadata,
                version_table_schema="app",
                include_schemas=True,
                include_name=include_name,
                include_object=include_object,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations()
