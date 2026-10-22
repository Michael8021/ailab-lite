"""Database engine, session factory and seed data."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .migrate import run_migrations
from .models import ApiToken, User, new_id, utcnow

_is_sqlite = settings.database_url.startswith("sqlite")

_engine_kwargs: dict = {"future": True}
if _is_sqlite:
    # SSE polling and ordinary reads share this pool; the old 5+10 default was
    # exhausted by ~15 concurrent streams. Writes still serialize in SQLite.
    _engine_kwargs.update(
        pool_pre_ping=False,  # local file: a dead connection is not a realistic failure mode
        pool_size=20,
        max_overflow=10,
        pool_timeout=5,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
else:
    _engine_kwargs["pool_pre_ping"] = True

engine = create_engine(settings.database_url, **_engine_kwargs)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - driver hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        # WAL + NORMAL: crash-safe against process death; a power loss can lose
        # the last transactions, which is the right trade-off for the lite profile.
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        # SQLite has one writer at a time: under load, waiting for the lock beats failing the write (a status
        # update or usage record lost as "database is locked"). Keep the generous wait.
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def init_db() -> None:
    settings.ensure_dirs()
    run_migrations(engine)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == settings.dev_user_email))
        if user is None:
            user = User(
                id=new_id("usr"),
                email=settings.dev_user_email,
                display_name="Demo User",
            )
            db.add(user)
            db.flush()
        token_hash = hash_token(settings.dev_token)
        current = db.scalar(select(ApiToken).where(ApiToken.token_hash == token_hash))
        if current is None:
            db.add(ApiToken(id=new_id("tok"), user_id=user.id, name="dev", token_hash=token_hash))
        elif current.revoked_at is not None:
            current.revoked_at = None  # configured again after having been rotated away
        # AILAB_DEV_TOKEN is the one static token. After rotating it, the old value must
        # stop working - the database outlives the configuration (persistent volume).
        for stale in db.scalars(
            select(ApiToken).where(ApiToken.name == "dev", ApiToken.token_hash != token_hash, ApiToken.revoked_at.is_(None))
        ):
            stale.revoked_at = utcnow()
        from .accounts import ensure_bootstrap_admin  # accounts imports this module for hash_token

        ensure_bootstrap_admin(db)
        db.commit()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
