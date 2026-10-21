"""Per-user critical sections for "check a limit, then write" sequences.

A limit that is checked in one transaction and enforced by a later insert can be overshot by requests that
arrive together: each sees room, each inserts. Two layers close that:

* an in-process lock per user (covers a single API process: SQLite deployments, and threads of one replica);
* on PostgreSQL, a row lock on the user (``SELECT ... FOR UPDATE``), held until the surrounding transaction
  commits or rolls back - this is what serializes several API replicas.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import User

_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def _lock_for(user_id: str) -> threading.Lock:
    with _guard:
        return _locks.setdefault(user_id, threading.Lock())


@contextmanager
def user_critical_section(db: Session, user_id: str) -> Iterator[None]:
    """Run the check and the write that depends on it inside this, and commit before leaving."""
    with _lock_for(user_id):
        if db.get_bind().dialect.name == "postgresql":
            db.execute(select(User.id).where(User.id == user_id).with_for_update())
        yield
