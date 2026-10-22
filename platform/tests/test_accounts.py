"""Password login: accounts, sessions, throttling, the command line."""

from __future__ import annotations

import io
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ailab_platform import accounts, users
from ailab_platform.accounts import AccountError, LoginThrottle
from ailab_platform.api.main import app
from ailab_platform.config import settings
from ailab_platform.db import SessionLocal, hash_token, init_db
from ailab_platform.models import ApiToken, User, utcnow
from ailab_platform.passwords import hash_password, verify_password

PASSWORD = "correct horse battery"


def make_user(password: str = PASSWORD) -> str:
    init_db()  # the API creates the schema at startup; these tests touch the database first
    name = f"u{uuid.uuid4().hex[:10]}"
    with SessionLocal() as db:
        accounts.create_user(db, name, password)
        db.commit()
    return name


def login(c: TestClient, name: str, password: str = PASSWORD, **headers):
    return c.post("/v1/auth/login", json={"username": name, "password": password}, headers=headers)


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_password_hashes_verify_and_are_salted() -> None:
    a, b = hash_password("s3cret-pass"), hash_password("s3cret-pass")
    assert a != b and a.startswith("scrypt$")
    assert verify_password("s3cret-pass", a) and not verify_password("s3cret-pasS", a)
    assert not verify_password("x", None) and not verify_password("x", "garbage") and not verify_password("x", "scrypt$1$2$3$zz$zz")


def test_login_gives_a_session_that_works_and_logout_ends_it() -> None:
    name = make_user()
    with TestClient(app) as c:
        resp = login(c, name)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["expires_in"] == settings.session_ttl_seconds and body["user"]["username"] == name
        headers = bearer(body["token"])
        assert c.get("/v1/auth/me", headers=headers).json()["username"] == name
        assert c.get("/v1/runs", headers=headers).status_code == 200
        assert c.post("/v1/auth/logout", headers=headers).status_code == 200
        assert c.get("/v1/runs", headers=headers).status_code == 401


def test_the_token_is_stored_only_as_a_hash() -> None:
    name = make_user()
    with TestClient(app) as c:
        token = login(c, name).json()["token"]
    with SessionLocal() as db:
        rows = list(db.scalars(select(ApiToken).where(ApiToken.token_hash == hash_token(token))))
        assert len(rows) == 1 and rows[0].name == "session" and token not in (rows[0].token_hash, rows[0].id)


def test_wrong_password_and_unknown_user_are_indistinguishable() -> None:
    name = make_user()
    with TestClient(app) as c:
        wrong = login(c, name, "not the password")
        unknown = login(c, "nobody-" + uuid.uuid4().hex[:8])
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json() == unknown.json()


def test_usernames_are_case_insensitive() -> None:
    name = make_user()
    with TestClient(app) as c:
        assert login(c, name.upper()).status_code == 200


def test_an_expired_session_stops_working() -> None:
    name = make_user()
    with TestClient(app) as c:
        token = login(c, name).json()["token"]
        with SessionLocal() as db:
            row = db.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(token)))
            row.expires_at = utcnow() - timedelta(seconds=1)
            db.commit()
        assert c.get("/v1/runs", headers=bearer(token)).status_code == 401


def test_a_disabled_account_cannot_log_in_and_its_sessions_die() -> None:
    name = make_user()
    with TestClient(app) as c:
        token = login(c, name).json()["token"]
        assert users.main(["disable", name]) == 0
        assert login(c, name).status_code == 401
        assert c.get("/v1/runs", headers=bearer(token)).status_code == 401
        assert users.main(["enable", name]) == 0
        assert login(c, name).status_code == 200


def test_repeated_failures_are_throttled_then_cleared_by_a_success_elsewhere() -> None:
    name = make_user()
    with TestClient(app) as c:
        for _ in range(settings.login_max_failures):
            assert login(c, name, "wrong", **{"X-Forwarded-For": "203.0.113.7"}).status_code == 401
        blocked = login(c, name, PASSWORD, **{"X-Forwarded-For": "203.0.113.7"})  # even the right password waits
        assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) > 0
        assert "try again" in blocked.json()["detail"]


def test_the_throttle_window_slides() -> None:
    t = LoginThrottle(max_failures=2, window_seconds=1)
    t.fail("a")
    t.fail("a")
    assert t.retry_after("a") > 0 and t.retry_after("b") == 0
    t.succeed("a")
    assert t.retry_after("a") == 0


def test_the_static_api_token_still_works_and_is_not_ended_by_logout() -> None:
    with TestClient(app) as c:
        headers = bearer("test-token")
        assert c.post("/v1/auth/logout", headers=headers).status_code == 200
        assert c.get("/v1/runs", headers=headers).status_code == 200


def test_changing_the_password_ends_all_sessions_and_needs_the_current_one() -> None:
    name = make_user()
    with TestClient(app) as c:
        one, two = login(c, name).json()["token"], login(c, name).json()["token"]

        def change(current: str, new: str) -> int:
            body = {"current_password": current, "new_password": new}
            return c.post("/v1/auth/password", headers=bearer(one), json=body).status_code

        assert change("nope", "a brand new one") == 403
        assert change(PASSWORD, "short") == 422
        assert change(PASSWORD, "a brand new one") == 200
        assert c.get("/v1/runs", headers=bearer(one)).status_code == 401 and c.get("/v1/runs", headers=bearer(two)).status_code == 401
        assert login(c, name).status_code == 401
        assert login(c, name, "a brand new one").status_code == 200


def test_account_rules() -> None:
    with SessionLocal() as db:
        with pytest.raises(AccountError, match="username"):
            accounts.create_user(db, "a b", PASSWORD)
        with pytest.raises(AccountError, match="at least"):
            accounts.create_user(db, "someone", "short")
        name = make_user()
        with pytest.raises(AccountError, match="already exists"):
            accounts.create_user(db, name.upper(), PASSWORD)


def test_bootstrap_admin_is_created_once_and_never_overwritten(monkeypatch) -> None:
    name = f"adm{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "admin_username", name)
    monkeypatch.setattr(settings, "admin_password", "first password")
    with SessionLocal() as db:
        accounts.ensure_bootstrap_admin(db)
        db.commit()
    monkeypatch.setattr(settings, "admin_password", "second password")
    with SessionLocal() as db:
        accounts.ensure_bootstrap_admin(db)
        db.commit()
    with TestClient(app) as c:
        assert login(c, name, "second password").status_code == 401
        assert login(c, name, "first password").status_code == 200


def test_command_line_add_passwd_list(monkeypatch, capsys) -> None:
    name = f"cli{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr("sys.stdin", io.StringIO("first-password\n"))
    assert users.main(["add", name, "--password-stdin"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("second-password\n"))
    assert users.main(["passwd", name, "--password-stdin"]) == 0
    assert users.main(["list"]) == 0
    assert name in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO("whatever-it-is\n"))
    assert users.main(["passwd", "no-such-user", "--password-stdin"]) == 1
    with TestClient(app) as c:
        assert login(c, name, "first-password").status_code == 401
        assert login(c, name, "second-password").status_code == 200


def test_users_without_a_password_cannot_log_in() -> None:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == settings.dev_user_email))
        assert user is not None and user.password_hash is None
    with TestClient(app) as c:
        assert c.post("/v1/auth/login", json={"username": "demo", "password": "anything"}).status_code == 401


# ---- sign-up ---------------------------------------------------------------------------------------

from ailab_platform.api import routes_auth  # noqa: E402


def _register(c: TestClient, name: str, password: str = PASSWORD, **extra):
    return c.post("/v1/auth/register", json={"username": name, "password": password, **extra})


@pytest.fixture()
def _fresh_signup_throttle(monkeypatch):
    monkeypatch.setattr(routes_auth, "signup_throttle", LoginThrottle(3, 3600))


def test_open_registration_creates_a_logged_in_account(_fresh_signup_throttle) -> None:
    name = f"new{uuid.uuid4().hex[:8]}"
    with TestClient(app) as c:
        assert c.get("/v1/auth/options").json() == {"registration": "open"}
        r = _register(c, name.upper(), display_name="小明")
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["user"]["username"] == name and body["user"]["display_name"] == "小明"
        assert c.get("/v1/auth/me", headers=bearer(body["token"])).json()["username"] == name
        assert login(c, name).status_code == 200
        again = _register(c, name)
        assert again.status_code == 409 and "已经被注册" in again.json()["detail"]


def test_registration_rules_come_back_in_chinese(_fresh_signup_throttle) -> None:
    with TestClient(app) as c:
        assert "用户名" in _register(c, "a b").json()["detail"]
        assert "8 个字符" in _register(c, f"ok{uuid.uuid4().hex[:6]}", "short").json()["detail"]
        demo = _register(c, "demo")  # demo@ailab.local belongs to the token user: still "taken"
        assert demo.status_code == 409


def test_invite_and_closed_modes(monkeypatch, _fresh_signup_throttle) -> None:
    with TestClient(app) as c:
        monkeypatch.setattr(settings, "registration", "invite")
        monkeypatch.setattr(settings, "invite_code", "")
        assert c.get("/v1/auth/options").json()["registration"] == "closed"  # invite with no code: nobody can join
        monkeypatch.setattr(settings, "invite_code", "lab-2026")
        assert c.get("/v1/auth/options").json()["registration"] == "invite"
        assert _register(c, f"i{uuid.uuid4().hex[:8]}", invite_code="wrong").status_code == 403
        assert _register(c, f"i{uuid.uuid4().hex[:8]}", invite_code=" lab-2026 ").status_code == 201
        monkeypatch.setattr(settings, "registration", "closed")
        r = _register(c, f"c{uuid.uuid4().hex[:8]}")
        assert r.status_code == 403 and "未开放注册" in r.json()["detail"]


def test_sign_ups_are_rate_limited_per_address(_fresh_signup_throttle) -> None:
    with TestClient(app) as c:
        codes = [_register(c, f"r{uuid.uuid4().hex[:8]}", **{}).status_code for _ in range(4)]
        assert codes == [201, 201, 201, 429]
