"""Background repair of work the queue lost track of.

The queue redelivers messages whose consumer died. This covers what the queue
itself cannot: a row committed but never enqueued (API crashed in between, or
the enqueue failed), and a message wiped from the queue (Redis restarted without
persistence) while its run was still marked running.

Single-instance: with several API replicas it needs leader election (or a
lock), otherwise replicas would race to requeue the same rows.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..events import append_event
from ..models import FileObject, FileStatus, Run, RunStatus
from ..queue import JobQueue, get_queue, run_lock_key
from ..run_state import transition_run

log = logging.getLogger("ailab.api.reaper")


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:  # SQLite drops tzinfo
        return dt.replace(tzinfo=UTC)
    return dt


def reap_once(db: Session, queue: JobQueue, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=settings.reaper_grace_seconds)
    stats = {"runs_requeued": 0, "runs_failed": 0, "files_requeued": 0}

    queued_runs = {p.get("run_id") for p in queue.outstanding(settings.run_queue)}
    for run in db.scalars(select(Run).where(Run.status.in_(RunStatus.ACTIVE))):
        if run.id in queued_runs:
            continue
        if run.status == RunStatus.QUEUED:
            if _aware(run.created_at) > cutoff:
                continue
            queue.enqueue(settings.run_queue, {"run_id": run.id})
            append_event(db, run.id, "run.requeued", {"reason": "no queue message found"})
            stats["runs_requeued"] += 1
            log.warning("requeued run %s: it was queued but had no queue message", run.id)
        else:  # running
            if queue.is_locked(run_lock_key(run.id)):
                continue  # an agent is actively executing it
            started = _aware(run.started_at) or _aware(run.created_at)
            if started > cutoff:
                continue
            _, changed = transition_run(
                db,
                run.id,
                RunStatus.FAILED,
                error="agent lost: the run was executing but no agent or queue message remains",
            )
            stats["runs_failed"] += int(changed)
            log.warning("failed run %s: running with no agent and no queue message", run.id)

    queued_files = {p.get("file_id") for p in queue.outstanding(settings.file_queue)}
    for file in db.scalars(select(FileObject).where(FileObject.status == FileStatus.PENDING)):
        if (file.meta or {}).get("processing") != "queued" or file.id in queued_files:
            continue
        if _aware(file.created_at) > cutoff:
            continue
        queue.enqueue(settings.file_queue, {"file_id": file.id})
        stats["files_requeued"] += 1
        log.warning("requeued file %s: pending scan had no queue message", file.id)
    return stats


REAPER_LOCK = "ailab:lock:reaper"


def reap_guarded(queue: JobQueue, session_factory=SessionLocal) -> dict[str, int] | None:
    """One reaper pass, but only if no other API replica ran one within the last interval.

    The lock is deliberately not released: it expires on its own, so a replica that wakes up a moment
    later skips this round instead of repeating it. Several replicas thus share one pass per interval.
    """
    if queue.acquire_lock(REAPER_LOCK, max(1, int(settings.reaper_interval_seconds * 0.8))) is None:
        return None
    with session_factory() as db:
        return reap_once(db, queue)


def _tick() -> dict[str, int] | None:
    return reap_guarded(get_queue())


async def reap_forever() -> None:
    while True:
        await asyncio.sleep(settings.reaper_interval_seconds)
        try:
            await asyncio.to_thread(_tick)
        except Exception:
            log.exception("reaper pass failed")
