"""The one place a Run's status changes.

Valid moves: queued -> running | failed | canceled, running -> succeeded | failed |
canceled. A terminal run never changes again (asking for it is a no-op, so a late
"succeeded" from the Agent cannot overwrite the user's cancel). The write is a
compare-and-swap on the status we read, so two writers racing resolve to one winner.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from . import metrics
from .events import append_event
from .models import Run, RunStatus

ALLOWED: dict[str, set[str]] = {
    RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELED},
    RunStatus.RUNNING: {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED},
}


class InvalidTransition(ValueError):
    pass


def transition_run(
    db: Session,
    run_id: str,
    new_status: str,
    *,
    summary: str | None = None,
    error: str | None = None,
    result: dict | None = None,
    reason: str = "canceled",
    commit: bool = True,
) -> tuple[Run, bool]:
    """Move a run to ``new_status``. Returns (run, changed)."""
    run = db.get(Run, run_id)
    if run is None:
        raise LookupError(run_id)
    if run.status in RunStatus.TERMINAL or run.status == new_status:
        return run, False
    if new_status not in ALLOWED.get(run.status, set()):
        raise InvalidTransition(f"{run.status} -> {new_status}")

    now = datetime.now(UTC)
    values: dict = {"status": new_status, "updated_at": now}
    if new_status == RunStatus.RUNNING and run.started_at is None:
        values["started_at"] = now
    if new_status in RunStatus.TERMINAL:
        values["finished_at"] = now
    for column, value in (("summary", summary), ("error", error), ("result", result)):
        if value is not None:
            values[column] = value

    won = db.execute(
        update(Run).where(Run.id == run_id, Run.status == run.status).values(**values).execution_options(synchronize_session=False)
    ).rowcount
    if commit:
        db.commit()
    else:
        db.flush()
    db.refresh(run)
    if not won:
        return run, False  # someone else moved it first; theirs stands

    metrics.RUNS.labels(new_status).inc()
    if new_status in RunStatus.TERMINAL:
        started = run.started_at or run.created_at
        if started.tzinfo is None:  # SQLite drops tzinfo
            started = started.replace(tzinfo=UTC)
        metrics.RUN_SECONDS.labels(new_status).observe(max(0.0, (now - started).total_seconds()))
    append_event(db, run.id, "run.status", {"status": run.status}, commit=commit)
    if new_status == RunStatus.SUCCEEDED:
        append_event(db, run.id, "run.completed", {"summary": run.summary, "result": run.result}, commit=commit)
    elif new_status == RunStatus.FAILED:
        append_event(db, run.id, "run.failed", {"error": run.error}, commit=commit)
    elif new_status == RunStatus.CANCELED:
        append_event(db, run.id, "run.canceled", {"reason": reason}, commit=commit)
    return run, True
