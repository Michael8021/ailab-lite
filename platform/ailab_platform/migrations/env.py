"""Alembic environment.

Two ways in: the alembic command line (builds its own engine from AILAB_DATABASE_URL), and the services
(ailab_platform.migrate passes the application's own connection in ``config.attributes["connection"]``).
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from ailab_platform.config import settings
from ailab_platform.models import Base

config = context.config
target_metadata = Base.metadata


def _configure(**kw) -> None:
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        # SQLite cannot ALTER most things in place; batch mode recreates the table there and is a no-op elsewhere.
        render_as_batch=True,
        **kw,
    )


def run_migrations_offline() -> None:
    _configure(url=settings.database_url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        _configure(connection=conn)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
