"""Alembic environment.

Decoupled from the app Settings: the URL (sync, psycopg) is read
directly from MYCELIUM_DATABASE_URL_SYNC, so migrations do not require the
JWT secret. Migrations run as the owner role (`mycelium`), not as
`mycelium_app` (see docs/adr/0015).
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from mycelium_core.models import Base

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers=False: fileConfig defaults to True, which would
    # DISABLE every already-configured logger. Harmless for the standalone
    # `alembic` CLI, but when migrations run IN-PROCESS (a test harness, or an
    # app that migrates at startup) that silently kills the application's
    # loggers. Configure alembic/sqlalchemy logging without nuking the rest.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

_DEFAULT_URL = "postgresql+psycopg://mycelium:mycelium@localhost:5432/mycelium"


def _url() -> str:
    return os.environ.get("MYCELIUM_DATABASE_URL_SYNC", _DEFAULT_URL)


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool, future=True)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
