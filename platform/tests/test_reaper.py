from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from ailab_platform.api.reaper import reap_once
from ailab_platform.config import settings
from ailab_platform.db import SessionLocal, init_db
from ailab_platform.models import (
    Artifact,
    FileObject,
    FileStatus,
    ModelUsage,
    Run,
    RunEvent,
    RunStatus,
    User,
    new_id,
)
from ailab_platform.queue import InMemoryQueue, run_lock_key


@pytest.fixture()
def db():
    init_db()
    with SessionLocal() as session:
        # other test modules leave runs/files behind in the shared test database
        for model in (RunEvent, ModelUsage, Artifact, Run, FileObject):
            session.execute(delete(model))
        session.commit()
        yield session


def _user(db) -> User:
    return db.scalar(select(User).where(User.email == settings.dev_user_email))


def _run(db, status, **kw) -> Run:
    run = Run(user_id=_user(db).id, prompt="p", status=status, **kw)
    db.add(run)
    db.commit()
    return run


LATER = datetime.now(UTC) + timedelta(seconds=10_000)


def test_queued_run_without_a_message_is_requeued(db) -> None:
    q = InMemoryQueue()
    run = _run(db, RunStatus.QUEUED)
    assert reap_once(db, q, now=LATER)["runs_requeued"] == 1
    assert q.outstanding(settings.run_queue) == [{"run_id": run.id}]
    assert reap_once(db, q, now=LATER)["runs_requeued"] == 0  # now it has a message


def test_young_queued_run_is_left_alone(db) -> None:
    q = InMemoryQueue()
    _run(db, RunStatus.QUEUED)
    assert reap_once(db, q)["runs_requeued"] == 0


def test_run_waiting_in_the_queue_is_not_duplicated(db) -> None:
    q = InMemoryQueue()
    run = _run(db, RunStatus.QUEUED)
    q.enqueue(settings.run_queue, {"run_id": run.id})
    q.dequeue(settings.run_queue, "c", timeout=1)  # in flight counts as outstanding too
    assert reap_once(db, q, now=LATER)["runs_requeued"] == 0


def test_orphaned_running_run_is_failed(db) -> None:
    q = InMemoryQueue()
    run = _run(db, RunStatus.RUNNING, started_at=datetime.now(UTC))
    assert reap_once(db, q, now=LATER)["runs_failed"] == 1
    db.refresh(run)
    assert run.status == RunStatus.FAILED and "agent lost" in run.error


def test_running_run_with_a_live_agent_is_left_alone(db) -> None:
    q = InMemoryQueue()
    run = _run(db, RunStatus.RUNNING, started_at=datetime.now(UTC))
    q.acquire_lock(run_lock_key(run.id), 60)
    assert reap_once(db, q, now=LATER)["runs_failed"] == 0
    db.refresh(run)
    assert run.status == RunStatus.RUNNING


def test_pending_file_without_a_message_is_requeued(db) -> None:
    q = InMemoryQueue()
    file = FileObject(
        id=new_id("fil"),
        user_id=_user(db).id,
        filename="a.txt",
        storage_key="uploads/x/a.txt",
        status=FileStatus.PENDING,
        meta={"processing": "queued"},
    )
    waiting = FileObject(
        id=new_id("fil"),
        user_id=_user(db).id,
        filename="b.txt",
        storage_key="uploads/x/b.txt",
        status=FileStatus.PENDING,
        meta={"processing": "awaiting_upload"},
    )
    db.add_all([file, waiting])
    db.commit()
    stats = reap_once(db, q, now=LATER)
    assert stats["files_requeued"] == 1
    queued = q.outstanding(settings.file_queue)
    assert {"file_id": file.id} in queued
    assert {"file_id": waiting.id} not in queued  # still waiting for the client's upload
