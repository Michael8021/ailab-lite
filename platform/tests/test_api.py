from __future__ import annotations

from fastapi.testclient import TestClient

from ailab_platform.api.main import app

AUTH = {"Authorization": "Bearer test-token"}
SERVICE = {"X-Service-Token": "test-service"}


def client() -> TestClient:
    return TestClient(app)


def test_healthz() -> None:
    with client() as c:
        body = c.get("/healthz").json()
        assert body["ok"] is True
        assert body["service"] == "platform-api"


def test_auth_required() -> None:
    with client() as c:
        assert c.get("/v1/runs").status_code == 401
        assert c.get("/v1/runs", headers=AUTH).status_code == 200


def test_run_lifecycle_and_events() -> None:
    with client() as c:
        resp = c.post("/v1/runs", headers=AUTH, json={"prompt": "帮我做一个 PPT"})
        assert resp.status_code == 201, resp.text
        run = resp.json()
        assert run["status"] == "queued"
        assert run["title"].startswith("帮我做一个")

        detail = c.get(f"/v1/runs/{run['id']}", headers=AUTH).json()
        assert detail["id"] == run["id"]

        history = c.get(f"/v1/runs/{run['id']}/events/history", headers=AUTH).json()
        assert next(e["type"] for e in history) == "run.created"

        internal = c.get(f"/internal/runs/{run['id']}", headers=SERVICE)
        assert internal.status_code == 200
        assert internal.json()["id"] == run["id"]

        canceled = c.post(f"/v1/runs/{run['id']}/cancel", headers=AUTH)
        assert canceled.status_code == 200
        assert canceled.json()["status"] == "canceled"
        assert c.post(f"/v1/runs/{run['id']}/cancel", headers=AUTH).status_code == 409


def test_internal_status_and_artifact_flow() -> None:
    with client() as c:
        run = c.post("/v1/runs", headers=AUTH, json={"prompt": "生成一个文件"}).json()
        r = c.post(f"/internal/runs/{run['id']}/status", headers=SERVICE, json={"status": "running"})
        assert r.status_code == 200
        assert r.json()["status"] == "running"

        upload = c.post(
            "/v1/files",
            headers=AUTH,
            files={"upload": ("note.txt", b"hello ailab", "text/plain")},
        )
        assert upload.status_code == 201, upload.text
        assert upload.json()["size"] == 11

        files = c.get("/v1/files", headers=AUTH).json()
        assert any(f["filename"] == "note.txt" for f in files)
        file_id = upload.json()["id"]
        assert upload.json()["status"] == "pending"  # quarantined until the Worker scans it
        assert c.get(f"/v1/files/{file_id}/content", headers=AUTH).status_code == 409

        c.patch(f"/internal/files/{file_id}", headers=SERVICE, json={"status": "ready"})
        downloaded = c.get(f"/v1/files/{file_id}/content", headers=AUTH)
        assert downloaded.content == b"hello ailab"


def test_service_token_required() -> None:
    with client() as c:
        assert c.get("/internal/runs/nope").status_code == 403
        assert c.get("/internal/runs/nope", headers=SERVICE).status_code == 404


def test_model_must_be_allowlisted() -> None:
    with client() as c:
        ok = c.post("/v1/runs", headers=AUTH, json={"prompt": "hi", "model": "test-fast"})
        assert ok.status_code == 201, ok.text
        assert ok.json()["user_id"] and ok.json()["model"] == "test-fast"
        bad = c.post("/v1/runs", headers=AUTH, json={"prompt": "hi", "model": "some-expensive-model"})
        assert bad.status_code == 400


def test_info_needs_auth_and_leaks_no_secrets() -> None:
    with client() as c:
        assert c.get("/v1/info").status_code == 401
        body = c.get("/v1/info", headers=AUTH).json()
        assert body["default_model"] == "test-pro" and body["sandbox_provider"] == "mock"
        text = str(body).lower()
        for secret in ("test-token", "test-service", "api_key", "password", "secret"):
            assert secret not in text
