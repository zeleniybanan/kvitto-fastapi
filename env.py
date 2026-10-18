"""Migrations share a connection with startup, or use DATABASE_URL from the CLI."""

import os

from alembic import context
from sqlalchemy import create_engine

from app.models import Base

config = context.config


def migrate(connection):
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=os.getenv("DATABASE_URL", config.get_main_option("sqlalchemy.url")),
        target_metadata=Base.metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    migrate(config.attributes["connection"])
else:
    engine = create_engine(os.getenv("DATABASE_URL", config.get_main_option("sqlalchemy.url")))
    try:
        with engine.connect() as connection:
            migrate(connection)
    finally:
        engine.dispose()
