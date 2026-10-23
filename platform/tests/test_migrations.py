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


def _sqlite_like_production(path):
    """The API's engine: SQLite with foreign keys enforced (db.py turns them on for every connection)."""
    from sqlalchemy import event

    eng = create_engine(f"sqlite:///{path}")

    @event.listens_for(eng, "connect")
    def _fk(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return eng


def test_adding_conversations_keeps_runs_that_other_tables_reference(tmp_path) -> None:
    """0004 must not rebuild `runs`: with foreign keys on, SQLite refuses to drop a table whose rows are referenced."""
    eng = _sqlite_like_production(tmp_path / "fk.db")
    _upgrade_to(eng, "0003")
    with eng.begin() as conn:
        _seed_user_and_run(conn)
        conn.execute(
            text("INSERT INTO run_events (run_id, seq, type, payload, created_at) VALUES ('r1', 1, 'run.created', '{}', '2026-01-01')")
        )
    run_migrations(eng)
    assert _version(eng) == _head()
    with eng.connect() as conn:
        assert conn.execute(text("SELECT id, conversation_id FROM runs")).one() == ("r1", None)
        assert conn.execute(text("SELECT count(*) FROM run_events")).scalar() == 1
    eng.dispose()


def test_a_half_applied_0004_is_completed(tmp_path) -> None:
    """What the first version of 0004 left behind on a live database: conversations and a stray temp table."""
    eng = _sqlite_like_production(tmp_path / "half.db")
    _upgrade_to(eng, "0003")
    with eng.begin() as conn:
        _seed_user_and_run(conn)
        conn.execute(
            text(
                "CREATE TABLE conversations (id VARCHAR(40) PRIMARY KEY, user_id VARCHAR(40) NOT NULL, title VARCHAR(200) NOT NULL, "
                "model VARCHAR(120) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, deleted_at DATETIME)"
            )
        )
        conn.execute(text("CREATE TABLE _alembic_tmp_runs (id VARCHAR(40))"))
    run_migrations(eng)
    assert _version(eng) == _head()
    tables = set(inspect(eng).get_table_names())
    assert "_alembic_tmp_runs" not in tables and "conversations" in tables
    assert "conversation_id" in {c["name"] for c in inspect(eng).get_columns("runs")}
    eng.dispose()


def test_a_database_that_ran_user_settings_as_0006_is_repaired(tmp_path) -> None:
    """A database that applied an unreleased numbering: users.settings exists, version says 0006, no 0006 indexes."""
    eng = _sqlite_like_production(tmp_path / "renumbered.db")
    _upgrade_to(eng, "0005")
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE users ADD COLUMN settings JSON"))
        conn.execute(text("UPDATE alembic_version SET version_num = '0006'"))
    run_migrations(eng)
    assert _version(eng) == _head()
    assert "ix_runs_user_created" in {i["name"] for i in inspect(eng).get_indexes("runs")}
    eng.dispose()


def test_projects_upgrade_and_downgrade_preserve_referenced_rows(tmp_path) -> None:
    eng = _sqlite_like_production(tmp_path / "projects.db")
    _upgrade_to(eng, "0007")
    with eng.begin() as conn:
        _seed_user_and_run(conn)
        conn.execute(
            text(
                "INSERT INTO conversations (id, user_id, title, model, created_at, updated_at) "
                "VALUES ('c1', 'u1', 'thread', 'm', '2026-01-01', '2026-01-01')"
            )
        )
        conn.execute(text("UPDATE runs SET conversation_id = 'c1' WHERE id = 'r1'"))
        conn.execute(
            text(
                "INSERT INTO files (id, user_id, filename, content_type, size, sha256, storage_key, status, version, meta, created_at) "
                "VALUES ('f1', 'u1', 'a.txt', 'text/plain', 1, '', 'a.txt', 'ready', 1, '{}', '2026-01-01')"
            )
        )
        conn.execute(
            text("INSERT INTO artifacts (id, run_id, file_id, role, created_at) VALUES ('a1', 'r1', 'f1', 'output', '2026-01-01')")
        )
    run_migrations(eng)
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO projects (id, user_id, name, created_at, updated_at) "
                "VALUES ('p1', 'u1', 'project', '2026-01-01', '2026-01-01')"
            )
        )
        conn.execute(text("UPDATE files SET project_id = 'p1', starred_at = '2026-01-01'"))
        conn.execute(text("UPDATE conversations SET project_id = 'p1'"))
        assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
    _downgrade_to(eng, "0007")
    assert "projects" not in inspect(eng).get_table_names()
    assert not {"project_id", "starred_at"} & {c["name"] for c in inspect(eng).get_columns("files")}
    with eng.connect() as conn:
        assert conn.execute(text("SELECT conversation_id FROM runs WHERE id = 'r1'")).scalar() == "c1"
        assert conn.execute(text("SELECT file_id FROM artifacts WHERE id = 'a1'")).scalar() == "f1"
        assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
    run_migrations(eng)
    with eng.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata) == []
    eng.dispose()
