"""Run event helpers shared by the API and tests."""

from __future__ import annotations

import time

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .models import RunEvent, utcnow


def append_event(db: Session, run_id: str, type_: str, payload: dict | None = None) -> RunEvent:
    """Append the next sequenced event for a run in one INSERT.

    The sequence is computed by the INSERT's own subquery, so this is one round
    trip instead of SELECT max(seq) + INSERT + COMMIT + REFRESH. SQLite has a
    single writer but internal API calls can still race; the unique constraint
    plus retry keeps the sequence dense.
    """
    last_error: Exception | None = None
    for attempt in range(6):
        created_at = utcnow()
        data = payload or {}
        next_seq = select(func.coalesce(func.max(RunEvent.seq), 0) + 1).where(RunEvent.run_id == run_id).scalar_subquery()
        stmt = (
            insert(RunEvent)
            .values(run_id=run_id, seq=next_seq, type=type_, payload=data, created_at=created_at)
            .returning(RunEvent.id, RunEvent.seq)
        )
        try:
            row = db.execute(stmt).one()
            db.commit()
            return RunEvent(
                id=row.id,
                run_id=run_id,
                seq=row.seq,
                type=type_,
                payload=data,
                created_at=created_at,
            )
        except (IntegrityError, OperationalError) as exc:  # pragma: no cover - race path
            last_error = exc
            db.rollback()
            time.sleep(0.05 * (attempt + 1))
    raise RuntimeError(f"could not append event for {run_id}: {last_error}")


def list_events(db: Session, run_id: str, after: int = 0) -> list[RunEvent]:
    return list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.seq > after).order_by(RunEvent.seq)))
