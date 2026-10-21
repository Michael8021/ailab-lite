"""Bring the database schema to the latest revision (the Platform API does this at startup).

* A fresh database: ``upgrade head`` creates everything.
* A database created before migrations existed (by ``Base.metadata.create_all``): it already has the
  tables of revision 0001 but no ``alembic_version``. It is stamped 0001 first, then upgraded - so the
  existing data is kept and no table is created twice.
* PostgreSQL: a session advisory lock makes several API replicas starting together migrate one at a time.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, text

log = logging.getLogger("ailab.migrate")

INITIAL_REVISION = "0001"  # the schema create_all() used to build
ADVISORY_LOCK_KEY = 7240012


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    return cfg


def run_migrations(engine: Engine) -> None:
    cfg = alembic_config()
    with engine.connect() as conn:
        postgres = conn.dialect.name == "postgresql"
        if postgres:
            conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
        try:
            tables = set(inspect(conn).get_table_names())
            cfg.attributes["connection"] = conn
            if "alembic_version" not in tables and "users" in tables:
                log.info("database predates migrations: stamping it as revision %s", INITIAL_REVISION)
                command.stamp(cfg, INITIAL_REVISION)
            command.upgrade(cfg, "head")
            conn.commit()
        finally:
            if postgres:
                conn.rollback()
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
                conn.commit()
