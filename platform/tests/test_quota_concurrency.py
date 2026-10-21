"""The per-user limits hold when requests arrive at the same moment (check-then-insert used to race)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from ailab_platform.api.main import app
from ailab_platform.config import settings
from ailab_platform.db import SessionLocal
from ailab_platform.models import Run, RunStatus, User

AUTH = {"Authorization": "Bearer test-token"}


def _active_runs() -> int:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == settings.dev_user_email))
        return db.scalar(select(func.count()).select_from(Run).where(Run.user_id == user.id, Run.status.in_(RunStatus.ACTIVE)))


def test_simultaneous_run_creation_cannot_exceed_the_active_run_limit(monkeypatch) -> None:
    with TestClient(app):  # lifespan: migrations + seed
        room = 2
        monkeypatch.setattr(settings, "max_active_runs_per_user", _active_runs() + room)

        def create(_):
            return TestClient(app).post("/v1/runs", headers=AUTH, json={"prompt": "x", "model": "test-fast"}).status_code

        with ThreadPoolExecutor(max_workers=10) as pool:
            codes = list(pool.map(create, range(10)))

        assert sorted(codes) == [201] * room + [429] * (10 - room), codes


def test_simultaneous_uploads_cannot_exceed_the_library_quota(monkeypatch) -> None:
    from ailab_platform.quota import library_bytes

    with TestClient(app):
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == settings.dev_user_email))
            used = library_bytes(db, user.id)
        monkeypatch.setattr(settings, "library_quota_bytes", used + 25)  # room for two 10-byte files, not three

        def upload(i):
            files = {"upload": (f"race-{i}.txt", b"0123456789", "text/plain")}
            return TestClient(app).post("/v1/files", headers=AUTH, files=files).status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(upload, range(8)))

        assert sorted(codes) == [201, 201] + [413] * 6, codes


def test_the_row_lock_alone_holds_the_limit_on_postgres(monkeypatch) -> None:
    """What protects several API replicas: no shared in-process lock, only SELECT ... FOR UPDATE."""
    import threading

    import pytest

    from ailab_platform import locks
    from ailab_platform.db import engine

    if engine.dialect.name != "postgresql":
        pytest.skip("needs the suite to run on PostgreSQL (AILAB_TEST_DATABASE_URL)")
    monkeypatch.setattr(locks, "_lock_for", lambda user_id: threading.Lock())  # every request gets its own lock: as if in another process

    with TestClient(app):
        room = 2
        monkeypatch.setattr(settings, "max_active_runs_per_user", _active_runs() + room)

        def create(_):
            return TestClient(app).post("/v1/runs", headers=AUTH, json={"prompt": "x", "model": "test-fast"}).status_code

        with ThreadPoolExecutor(max_workers=10) as pool:
            codes = list(pool.map(create, range(10)))
        assert sorted(codes) == [201] * room + [429] * (10 - room), codes
