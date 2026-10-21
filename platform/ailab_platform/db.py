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

engine = create_engine(
    settings.database_url,
    future=True,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - driver hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
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
