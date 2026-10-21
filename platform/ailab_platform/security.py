"""Token authentication primitives (session tokens from a password login, and API tokens)."""

from __future__ import annotations

import hmac

from fastapi import Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .config import settings
from .db import hash_token
from .models import ApiToken, User, utcnow


def bearer_token(request: Request) -> str | None:
    """The Bearer token from the Authorization header, and nowhere else.

    Query-string tokens are not accepted: URLs end up in browser history, proxy and access
    logs. The Portal keeps the token in an httpOnly cookie and adds the header server-side.
    """
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def authenticate(db: Session, token: str | None) -> User | None:
    if not token:
        return None
    row = db.scalar(
        select(ApiToken).where(
            ApiToken.token_hash == hash_token(token),
            ApiToken.revoked_at.is_(None),
            or_(ApiToken.expires_at.is_(None), ApiToken.expires_at > utcnow()),
        )
    )
    if row is None:
        return None
    user = db.get(User, row.user_id)
    return None if user is None or user.disabled_at is not None else user


def service_token_ok(token: str | None) -> bool:
    if not token:
        return False
    return hmac.compare_digest(token, settings.service_token)
