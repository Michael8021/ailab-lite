"""Run event helpers shared by the API and tests."""

from __future__ import annotations

import time

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .models import RunEvent


def append_event(db: Session, run_id: str, type_: str, payload: dict | None = None) -> RunEvent:
    """Append the next sequenced event for a run.

    SQLite has a single writer but internal API calls can still race on
    ``max(seq)``; the unique constraint plus retry keeps the sequence dense.
    """
    last_error: Exception | None = None
    for attempt in range(6):
        seq = db.scalar(select(func.coalesce(func.max(RunEvent.seq), 0)).where(RunEvent.run_id == run_id)) + 1
        event = RunEvent(run_id=run_id, seq=seq, type=type_, payload=payload or {})
        db.add(event)
        try:
            db.commit()
            db.refresh(event)
            return event
        except (IntegrityError, OperationalError) as exc:  # pragma: no cover - race path
            last_error = exc
            db.rollback()
            time.sleep(0.05 * (attempt + 1))
    raise RuntimeError(f"could not append event for {run_id}: {last_error}")


def list_events(db: Session, run_id: str, after: int = 0) -> list[RunEvent]:
    return list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.seq > after).order_by(RunEvent.seq)))
