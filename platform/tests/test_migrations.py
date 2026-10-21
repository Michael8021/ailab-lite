"""Schema migrations: fresh databases, databases that predate migrations, upgrade/downgrade, and drift.

Runs on SQLite always; on PostgreSQL too when AILAB_TEST_POSTGRES_URL points at a throwaway database.
"""

from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from ailab_platform.migrate import alembic_config, run_migrations
from ailab_platform.models import Base


@pytest.fixture(params=["sqlite", "postgres"])
def engine(request, tmp_path):
    if request.param == "sqlite":
        eng = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    else:
        url = os.environ.get("AILAB_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("AILAB_TEST_POSTGRES_URL not set")
        eng = create_engine(url)
        with eng.begin() as conn:  # a clean slate: this must be a throwaway database
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    yield eng
    eng.dispose()


def _seed_user_and_run(conn) -> None:
    """A user and a run, written with plain SQL so it works at any schema revision."""
    conn.execute(text("INSERT INTO users (id, email, display_name, created_at) VALUES ('u1', 'a@b.c', 'A', '2026-01-01 00:00:00')"))
    columns = "id, user_id, title, prompt, status, model, input_file_ids, result, summary, error, created_at, updated_at"
    values = "'r1', 'u1', 't', 'p', 'queued', 'm', '[]', '{}', '', '', '2026-01-01 00:00:00', '2026-01-01 00:00:00'"
    conn.execute(text(f"INSERT INTO runs ({columns}) VALUES ({values})"))


def _head() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def _version(engine) -> str | None:
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _upgrade_to(engine, revision: str) -> None:
    cfg = alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision)


def _downgrade_to(engine, revision: str) -> None:
    cfg = alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, revision)


def test_fresh_database_gets_the_whole_schema(engine) -> None:
    run_migrations(engine)
    assert _version(engine) == _head()
    assert set(inspect(engine).get_table_names()) >= set(Base.metadata.tables)


def test_migrations_and_models_do_not_drift(engine) -> None:
    """Fails when someone edits models.py without writing a migration (the old create_all never noticed)."""
    run_migrations(engine)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == [], f"models.py differs from the migrated schema; run alembic revision --autogenerate: {diff}"


def test_running_twice_is_harmless(engine) -> None:
    run_migrations(engine)
    run_migrations(engine)
    assert _version(engine) == _head()


def test_database_created_before_migrations_is_stamped_and_upgraded_keeping_its_data(engine) -> None:
    """The deployment that already exists: tables from create_all (revision 0001), no alembic_version."""
    _upgrade_to(engine, "0001")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))  # what create_all() left behind: no version table
        _seed_user_and_run(conn)
        conn.execute(
            text(
                "INSERT INTO model_usage (run_id, model, prompt_tokens, completion_tokens, created_at) "
                "VALUES ('r1', 'unknown-model', 10, 5, '2026-01-01 00:00:00')"
            )
        )

    run_migrations(engine)

    assert _version(engine) == _head()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT email FROM users")).scalar() == "a@b.c"
        row = conn.execute(text("SELECT prompt_tokens, completion_tokens, cached_tokens, weight FROM model_usage")).one()
    assert tuple(row) == (10, 5, 0, 1.0)


def test_existing_usage_rows_keep_the_weight_they_were_being_charged(engine) -> None:
    """Before revision 0002 the weight was looked up at read time, so history is frozen at those values."""
    _upgrade_to(engine, "0001")
    with engine.begin() as conn:
        _seed_user_and_run(conn)
        for model in ("test-pro", "test-fast", "renamed-away"):
            conn.execute(
                text(
                    "INSERT INTO model_usage (run_id, model, prompt_tokens, completion_tokens, created_at) "
                    f"VALUES ('r1', '{model}', 100, 0, '2026-01-01 00:00:00')"
                )
            )
    _upgrade_to(engine, "0002")
    with engine.connect() as conn:
        weights = dict(conn.execute(text("SELECT model, weight FROM model_usage")).all())
    assert weights == {"test-pro": 3.0, "test-fast": 1.0, "renamed-away": 1.0}  # tests/models.test.toml


def test_downgrade_removes_only_the_new_columns(engine) -> None:
    run_migrations(engine)
    _downgrade_to(engine, "0001")
    cols = {c["name"] for c in inspect(engine).get_columns("model_usage")}
    assert "weight" not in cols and "cached_tokens" not in cols and {"prompt_tokens", "completion_tokens"} <= cols
    run_migrations(engine)  # and it can go up again
    assert _version(engine) == _head()


def test_concurrent_looking_starts_do_not_collide(engine) -> None:
    """Two services starting together: the second finds the first's work done."""
    run_migrations(engine)
    other = create_engine(engine.url)
    try:
        run_migrations(other)
    finally:
        other.dispose()
    assert _version(engine) == _head()
