"""Per-user limits the Platform API enforces before accepting work."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .catalog import get_catalog
from .config import settings
from .models import FileObject, FileStatus, ModelUsage, Run


def tokens_used_since(db: Session, user_id: str, since: datetime) -> int:
    """Weighted tokens: a call to a model with weight 3 costs three times its raw token count,
    so the limits roughly track compute rather than tokens."""
    rows = db.execute(
        select(ModelUsage.model, func.sum(ModelUsage.prompt_tokens + ModelUsage.completion_tokens))
        .join(Run, Run.id == ModelUsage.run_id)
        .where(Run.user_id == user_id, ModelUsage.created_at >= since)
        .group_by(ModelUsage.model)
    ).all()
    catalog = get_catalog()
    return int(sum((tokens or 0) * catalog.weight_of(model) for model, tokens in rows))


def usage_summary(db: Session, user_id: str) -> dict:
    """What the user has used against each limit (tokens are weighted, see tokens_used_since)."""
    now = datetime.now(UTC)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    return {
        "daily_tokens": {"used": tokens_used_since(db, user_id, day), "limit": settings.daily_token_limit},
        "monthly_tokens": {"used": tokens_used_since(db, user_id, month), "limit": settings.monthly_token_limit},
        "library_bytes": {"used": library_bytes(db, user_id), "limit": settings.library_quota_bytes},
        "resets_at_utc": (day.replace(hour=0) + timedelta(days=1)).isoformat(),
    }


def enforce_token_quota(db: Session, user_id: str) -> None:
    now = datetime.now(UTC)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    if tokens_used_since(db, user_id, day) >= settings.daily_token_limit:
        raise HTTPException(status_code=429, detail="daily token quota used up; it resets at 00:00 UTC")
    if tokens_used_since(db, user_id, month) >= settings.monthly_token_limit:
        raise HTTPException(status_code=429, detail="monthly token quota used up")


def library_bytes(db: Session, user_id: str) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.sum(FileObject.size), 0)).where(
                FileObject.user_id == user_id, FileObject.status != FileStatus.REJECTED
            )
        )
        or 0
    )


def enforce_library_quota(db: Session, user_id: str, incoming: int) -> None:
    if library_bytes(db, user_id) + incoming > settings.library_quota_bytes:
        raise HTTPException(status_code=413, detail="library quota exceeded; delete files or ask for more space")
