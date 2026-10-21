"""POST /internal/artifacts: validation, de-duplication and retry safety."""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from ailab_platform.api.main import app
from ailab_platform.config import settings
from ailab_platform.storage import get_storage

AUTH = {"Authorization": "Bearer test-token"}
SERVICE = {"X-Service-Token": "test-service"}


@pytest.fixture()
def api():
    with TestClient(app) as c:
        yield c


def _running_run(api) -> str:
    run = api.post("/v1/runs", headers=AUTH, json={"prompt": "make a file"}).json()
    api.post(f"/internal/runs/{run['id']}/status", headers=SERVICE, json={"status": "running"})
    return run["id"]


def _stage(data: bytes, name="out.md") -> dict:
    key = f"staging/lease_t/{hashlib.sha256(data).hexdigest()[:8]}/{name}"
    get_storage().put_bytes(key, data)
    return {
        "staging_key": key,
        "filename": name,
        "content_type": "text/markdown",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "role": "output",
    }


def _commit(api, run_id, body):
    return api.post("/internal/artifacts", headers=SERVICE, json={"run_id": run_id, **body})


def test_commit_creates_one_artifact_and_removes_staging(api) -> None:
    run_id = _running_run(api)
    body = _stage(b"hello")
    resp = _commit(api, run_id, body)
    assert resp.status_code == 200, resp.text
    assert not get_storage().exists(body["staging_key"])
    assert get_storage().get_bytes(f"runs/{run_id}/{resp.json()['file_id']}/out.md") == b"hello"
    detail = api.get(f"/v1/runs/{run_id}", headers=AUTH).json()
    assert [a["filename"] for a in detail["artifacts"]] == ["out.md"]


def test_same_content_twice_is_one_artifact(api) -> None:
    run_id = _running_run(api)
    first = _commit(api, run_id, _stage(b"same"))
    again = _commit(api, run_id, _stage(b"same"))  # e.g. a retry, or deliver_file called twice
    assert again.status_code == 200
    assert again.json()["artifact_id"] == first.json()["artifact_id"]
    assert len(api.get(f"/v1/runs/{run_id}", headers=AUTH).json()["artifacts"]) == 1


def test_retry_after_staging_was_already_consumed_is_idempotent(api) -> None:
    run_id = _running_run(api)
    body = _stage(b"payload")
    first = _commit(api, run_id, body)
    assert not get_storage().exists(body["staging_key"])
    retry = _commit(api, run_id, body)  # response was lost; staging is already gone
    assert retry.status_code == 200 and retry.json()["file_id"] == first.json()["file_id"]


def test_changed_content_is_a_new_artifact(api) -> None:
    run_id = _running_run(api)
    _commit(api, run_id, _stage(b"v1"))
    _commit(api, run_id, _stage(b"v2"))
    assert len(api.get(f"/v1/runs/{run_id}", headers=AUTH).json()["artifacts"]) == 2


def test_rejections_and_they_do_not_leak_staging(api, monkeypatch) -> None:
    run_id = _running_run(api)
    assert _commit(api, run_id, {**_stage(b"x"), "staging_key": "uploads/someone/else.txt"}).status_code == 400
    assert _commit(api, run_id, {**_stage(b"x"), "staging_key": "staging/nope/x"}).status_code == 409

    for change, code in (({"sha256": "0" * 64}, 400), ({"size": 999}, 400)):
        body = _stage(b"data")
        assert _commit(api, run_id, {**body, **change}).status_code == code
        assert not get_storage().exists(body["staging_key"])  # rejected -> staging removed

    monkeypatch.setattr(settings, "max_artifact_bytes", 2)
    body = _stage(b"data")
    assert _commit(api, run_id, body).status_code == 413
    assert not get_storage().exists(body["staging_key"])


def test_not_accepted_once_the_run_is_over(api) -> None:
    run_id = _running_run(api)
    api.post(f"/v1/runs/{run_id}/cancel", headers=AUTH)
    body = _stage(b"late")
    assert _commit(api, run_id, body).status_code == 409
    assert not get_storage().exists(body["staging_key"])


def test_hostile_filenames_are_flattened(api) -> None:
    run_id = _running_run(api)
    for name, expected in (("../../etc/passwd", "passwd"), ("..", "artifact"), ("a\\b\\c.txt", "c.txt")):
        resp = _commit(api, run_id, _stage(name.encode(), name="x.md") | {"filename": name})
        assert resp.status_code == 200, (name, resp.text)
        assert resp.json()["filename"] == expected
