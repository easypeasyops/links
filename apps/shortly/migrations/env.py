"""Alembic environment.

The database URL is resolved, in order, from:
  1. ``sqlalchemy.url`` set programmatically (the test suite does this), then
  2. ``SHORTLY_DATABASE_URL`` via the app's own Settings — one config path for app and migrations.
"""

from logging.config import fileConfig

from alembic import context

from shortly.db import make_engine, metadata
from shortly.settings import Settings

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or str(Settings().database_url)


def run_migrations_offline() -> None:
    """Print SQL instead of running it (`alembic upgrade head --sql`), e.g. for review."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(_url(), pooled=False)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
