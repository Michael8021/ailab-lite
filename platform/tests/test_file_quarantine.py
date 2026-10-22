"""Uploads stay quarantined (pending) until the Worker's scan marks them ready."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from ailab_platform import clients
from ailab_platform.api.main import app
from ailab_platform.worker.pipeline import process_file

AUTH = {"Authorization": "Bearer test-token"}
EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


class _Bridge:
    """Stands in for httpx.Client so PlatformClient talks to the in-process API."""

    def __init__(self, test_client: TestClient):
        self._c = test_client

    def __call__(self, **_kwargs):
        return self

    def request(self, method, url, **_kwargs):
        parsed = urlsplit(url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        return self._c.request(
            method,
            path,
            headers=_kwargs.get("headers"),
            content=_kwargs.get("content"),
            json=_kwargs.get("json"),
        )

    def close(self):
        pass

    def __enter__(self):
        return self._c

    def __exit__(self, *_exc):
        return False


@pytest.fixture()
def api(monkeypatch):
    with TestClient(app) as c:
        monkeypatch.setattr(clients.httpx, "Client", _Bridge(c))
        yield c


def _upload(api: TestClient, name: str, data: bytes) -> dict:
    resp = api.post("/v1/files", headers=AUTH, files={"upload": (name, data, "text/plain")})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_upload_is_pending_and_unusable_until_scanned(api) -> None:
    f = _upload(api, "ok.txt", b"hello quarantine")
    assert f["status"] == "pending"

    assert api.get(f"/v1/files/{f['id']}/content", headers=AUTH).status_code == 409
    run = api.post("/v1/runs", headers=AUTH, json={"prompt": "总结文件", "file_ids": [f["id"]]})
    assert run.status_code == 409

    result = process_file(f["id"])
    assert result.get("kind") == "text"

    after = api.get(f"/v1/files/{f['id']}", headers=AUTH).json()
    assert after["status"] == "ready"
    assert api.get(f"/v1/files/{f['id']}/content", headers=AUTH).content == b"hello quarantine"
    run = api.post("/v1/runs", headers=AUTH, json={"prompt": "总结文件", "file_ids": [f["id"]]})
    assert run.status_code == 201, run.text


def test_infected_file_is_rejected_and_stays_unusable(api) -> None:
    f = _upload(api, "eicar.txt", EICAR)

    result = process_file(f["id"])
    assert "rejected" in result

    after = api.get(f"/v1/files/{f['id']}", headers=AUTH).json()
    assert after["status"] == "rejected"
    assert after["meta"]["processing"] == "rejected"
    assert api.get(f"/v1/files/{f['id']}/content", headers=AUTH).status_code == 409
    run = api.post("/v1/runs", headers=AUTH, json={"prompt": "总结文件", "file_ids": [f["id"]]})
    assert run.status_code == 409


def test_worker_skips_files_that_are_not_pending(api) -> None:
    f = _upload(api, "twice.txt", b"abc")
    process_file(f["id"])
    assert process_file(f["id"]) == {"file_id": f["id"], "skipped": "ready"}
