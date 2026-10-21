"""FastAPI dependencies: DB session, user auth and service auth."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import User
from .security import authenticate, bearer_token, service_token_ok


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = authenticate(db, bearer_token(request))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def metrics_auth(request: Request, x_service_token: str | None = Header(default=None)) -> None:
    """/metrics shows traffic and queue sizes: guard it with the service token, in either of the two forms."""
    bearer = bearer_token(request)
    if not (service_token_ok(x_service_token) or service_token_ok(bearer)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="service token required")


def service_auth(x_service_token: str | None = Header(default=None)) -> None:
    if not service_token_ok(x_service_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="service token required")
