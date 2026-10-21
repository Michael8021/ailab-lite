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
    db.commit()
    db.refresh(run)
    if not won:
        return run, False  # someone else moved it first; theirs stands

    append_event(db, run.id, "run.status", {"status": run.status})
    if new_status == RunStatus.SUCCEEDED:
        append_event(db, run.id, "run.completed", {"summary": run.summary, "result": run.result})
    elif new_status == RunStatus.FAILED:
        append_event(db, run.id, "run.failed", {"error": run.error})
    elif new_status == RunStatus.CANCELED:
        append_event(db, run.id, "run.canceled", {"reason": reason})
    return run, True
