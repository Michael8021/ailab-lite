"""Password login, logout and "who am I"."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..accounts import AccountError, address_throttle, check_credentials, issue_session, normalize_username, set_password, user_throttle
from ..db import hash_token
from ..deps import current_user, get_db
from ..models import ApiToken, User, utcnow
from ..passwords import MAX_PASSWORD_LENGTH
from ..schemas import LoginRequest, PasswordChange
from ..security import bearer_token

router = APIRouter(prefix="/v1/auth", tags=["auth"])
log = logging.getLogger("ailab.api.auth")


def _user_out(user: User) -> dict:
    return {"id": user.id, "username": user.username, "display_name": user.display_name, "email": user.email}


def _client_address(request: Request) -> str:
    # The Portal is the only caller of this endpoint and passes the visitor's address along.
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "unknown")


@router.post("/login")
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    username = normalize_username(body.username)
    address = _client_address(request)
    wait = max(user_throttle.retry_after(username), address_throttle.retry_after(address))
    if wait:
        log.warning("login throttled user=%s from=%s retry_after=%ss", username, address, wait)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"too many failed attempts, try again in {wait} seconds",
            headers={"Retry-After": str(wait)},
        )
    user = check_credentials(db, username, body.password)
    if user is None:
        user_throttle.fail(username)
        address_throttle.fail(address)
        log.warning("login failed user=%s from=%s", username, address)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid username or password",  # the same words whether the name exists or not
        )
    user_throttle.succeed(username)
    token, lifetime = issue_session(db, user)
    db.commit()
    log.info("login ok user=%s from=%s", username, address)
    return {"token": token, "expires_in": lifetime, "user": _user_out(user)}


@router.get("/me")
def me(user: User = Depends(current_user)) -> dict:
    return _user_out(user)


@router.post("/logout")
def logout(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """End *this* session. (Tokens that are not sessions - the static API token - are left alone.)"""
    row = db.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(bearer_token(request) or ""), ApiToken.user_id == user.id))
    if row is not None and row.name == "session" and row.revoked_at is None:
        row.revoked_at = utcnow()
        db.commit()
    return {"ok": True}


@router.post("/password")
def change_password(body: PasswordChange, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    if not user.username:
        raise HTTPException(status_code=400, detail="this account has no password login")
    if len(body.current_password) > MAX_PASSWORD_LENGTH or check_credentials(db, user.username, body.current_password) is None:
        raise HTTPException(status_code=403, detail="current password is wrong")
    try:
        set_password(db, user, body.new_password)  # also ends every session, this one included
    except AccountError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return {"ok": True}
