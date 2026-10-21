from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ailab_platform.api.main import app
from ailab_platform.config import Settings, settings
from ailab_platform.db import SessionLocal
from ailab_platform.models import FileObject, User
from ailab_platform.quota import tokens_used_since

AUTH = {"Authorization": "Bearer test-token"}
SERVICE = {"X-Service-Token": "test-service"}


@pytest.fixture()
def api():
    with TestClient(app) as c:
        yield c


def _day_start() -> datetime:
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


# ---- quotas -----------------------------------------------------------------------------------


def test_daily_token_quota_blocks_new_runs(api, monkeypatch) -> None:
    first = api.post("/v1/runs", headers=AUTH, json={"prompt": "x"}).json()["id"]
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == settings.dev_user_email))
        used = tokens_used_since(db, user.id, _day_start())
    monkeypatch.setattr(settings, "daily_token_limit", used + 10)
    assert api.post("/v1/runs", headers=AUTH, json={"prompt": "x"}).status_code == 201

    api.post(f"/internal/runs/{first}/usage", headers=SERVICE, json={"model": "m", "prompt_tokens": 8, "completion_tokens": 2})
    blocked = api.post("/v1/runs", headers=AUTH, json={"prompt": "x"})
    assert blocked.status_code == 429 and "daily" in blocked.json()["detail"]


def test_library_quota_blocks_uploads_and_ignores_rejected_files(api, monkeypatch) -> None:
    from ailab_platform.quota import library_bytes

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == settings.dev_user_email))
        used = library_bytes(db, user.id)
    monkeypatch.setattr(settings, "library_quota_bytes", used + 10)
    ok = api.post("/v1/files", headers=AUTH, files={"upload": ("a.txt", b"12345", "text/plain")})
    assert ok.status_code == 201
    over = api.post("/v1/files", headers=AUTH, files={"upload": ("b.txt", b"123456", "text/plain")})
    assert over.status_code == 413

    api.patch(f"/internal/files/{ok.json()['id']}", headers=SERVICE, json={"status": "rejected"})
    assert api.post("/v1/files", headers=AUTH, files={"upload": ("c.txt", b"1234567890", "text/plain")}).status_code == 201


def test_upload_size_limit_is_enforced_while_reading(api, monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_upload_bytes", 10)
    assert api.post("/v1/files", headers=AUTH, files={"upload": ("a", b"x" * 11, "text/plain")}).status_code == 413
    assert api.post("/v1/files", headers=AUTH, files={"upload": ("a", b"x" * 10, "text/plain")}).status_code == 201


# ---- tokens only travel in the Authorization header ---------------------------------------------


def test_query_string_tokens_are_not_accepted_anywhere(api) -> None:
    run_id = api.post("/v1/runs", headers=AUTH, json={"prompt": "x"}).json()["id"]
    api.post(f"/v1/runs/{run_id}/cancel", headers=AUTH)
    f = api.post("/v1/files", headers=AUTH, files={"upload": ("n.txt", b"hi", "text/plain")}).json()["id"]
    api.patch(f"/internal/files/{f}", headers=SERVICE, json={"status": "ready"})
    q = {"token": "test-token"}
    for url in ("/v1/runs", f"/v1/runs/{run_id}", f"/v1/runs/{run_id}/events", f"/v1/files/{f}", f"/v1/files/{f}/content", "/v1/info"):
        assert api.get(url, params=q).status_code == 401, url
    assert api.post(f"/v1/runs/{run_id}/cancel", params=q).status_code == 401
    # the header still works for the very same endpoints
    assert api.get(f"/v1/files/{f}/content", headers=AUTH).content == b"hi"
    assert api.get(f"/v1/runs/{run_id}/events", headers=AUTH).status_code == 200


def test_cors_is_off_by_default(api) -> None:
    resp = api.get("/healthz", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


# ---- refusing to run with published default secrets ---------------------------------------------

GOOD = dict(dev_token="a-real-operator-token", service_token="x" * 32, osb_api_key="k")


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


@pytest.mark.parametrize("env", ["dev", "test", "k3s", "smoke"])
def test_lite_environments_tolerate_defaults(env) -> None:
    _settings(env=env, dev_token="dev-demo-token", service_token="dev-service-token").validate_secrets()


def test_production_refuses_defaults() -> None:
    with pytest.raises(RuntimeError, match=r"DEV_TOKEN.*SERVICE_TOKEN"):
        _settings(env="prod", dev_token="dev-demo-token", service_token="dev-service-token").validate_secrets()


def test_production_refuses_short_service_token() -> None:
    with pytest.raises(RuntimeError, match="shorter"):
        _settings(env="prod", **{**GOOD, "service_token": "short"}).validate_secrets()


def test_production_osb_key_only_matters_with_opensandbox() -> None:
    base = {**GOOD, "osb_api_key": "dev-osb-key"}
    _settings(env="prod", sandbox_provider="docker", **base).validate_secrets()
    with pytest.raises(RuntimeError, match="OSB_API_KEY"):
        _settings(env="prod", sandbox_provider="opensandbox", **base).validate_secrets()


def test_production_with_real_secrets_starts() -> None:
    _settings(env="prod", **GOOD).validate_secrets()


# ---- deleting files / refusing oversized uploads early ----------------------------------------------


def test_delete_frees_library_space_and_unblocks_a_stuck_file(api, monkeypatch) -> None:
    from ailab_platform.quota import library_bytes
    from ailab_platform.storage import get_storage

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == settings.dev_user_email))
        before = library_bytes(db, user.id)
    f = api.post("/v1/files", headers=AUTH, files={"upload": ("stuck.txt", b"12345", "text/plain")}).json()
    with SessionLocal() as db:
        assert library_bytes(db, user.id) == before + 5
        key = db.get(FileObject, f["id"]).storage_key
    assert get_storage().exists(key)

    assert api.delete(f"/v1/files/{f['id']}", headers=AUTH).status_code == 204
    assert api.get(f"/v1/files/{f['id']}", headers=AUTH).status_code == 404
    assert not get_storage().exists(key)
    with SessionLocal() as db:
        assert library_bytes(db, user.id) == before


def test_delete_is_owner_only_and_keeps_version_chains(api) -> None:
    v1 = api.post("/v1/files", headers=AUTH, files={"upload": ("d.txt", b"v1", "text/plain")}).json()
    v2 = api.post(f"/v1/files?parent_id={v1['id']}", headers=AUTH, files={"upload": ("d.txt", b"v2", "text/plain")}).json()
    assert api.delete(f"/v1/files/{v1['id']}", headers=AUTH).status_code == 409  # v2 builds on it
    assert api.delete(f"/v1/files/{v2['id']}", headers=AUTH).status_code == 204
    assert api.delete(f"/v1/files/{v1['id']}", headers=AUTH).status_code == 204
    assert api.delete(f"/v1/files/{v1['id']}", headers=AUTH).status_code == 404
    assert api.delete("/v1/files/fil_x", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_huge_upload_is_refused_before_the_body_is_parsed(api, monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_upload_bytes", 1000)
    resp = api.post(
        "/v1/files",
        headers={**AUTH, "content-type": "multipart/form-data; boundary=x"},
        content=b"x" * (1000 + 64 * 1024 + 1),
    )
    assert resp.status_code == 413 and "exceeds" in resp.json()["detail"]


# ---- rotating the static dev token ---------------------------------------------------------------


def test_rotating_the_dev_token_revokes_the_old_one(monkeypatch) -> None:
    from ailab_platform.db import hash_token, init_db
    from ailab_platform.security import authenticate

    def can_log_in(token: str) -> bool:
        with SessionLocal() as db:
            return authenticate(db, token) is not None

    monkeypatch.setattr(settings, "dev_token", "old-token-before-rotation")
    init_db()
    assert can_log_in("old-token-before-rotation")

    monkeypatch.setattr(settings, "dev_token", "new-token-after-rotation")
    init_db()  # the service restarts with the rotated secret; the database is the same
    assert can_log_in("new-token-after-rotation")
    assert not can_log_in("old-token-before-rotation")

    monkeypatch.setattr(settings, "dev_token", "old-token-before-rotation")
    init_db()  # rolling back to the previous value works too
    assert can_log_in("old-token-before-rotation") and not can_log_in("new-token-after-rotation")

    monkeypatch.setattr(settings, "dev_token", "test-token")  # leave the shared test DB as the other tests expect
    init_db()
    assert can_log_in("test-token") and hash_token("x")
