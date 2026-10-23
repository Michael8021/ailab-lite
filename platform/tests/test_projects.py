"""Project ownership, conversation isolation and cascading deletion."""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from ailab_platform.api.main import app
from ailab_platform.db import SessionLocal
from ailab_platform.models import Artifact, Conversation, FileObject, Project, Run, RunEvent
from ailab_platform.storage import get_storage
from tests.test_conversations import _other_user

AUTH = {"Authorization": "Bearer test-token"}
SERVICE = {"X-Service-Token": "test-service"}


@pytest.fixture()
def api():
    with TestClient(app) as client:
        client.headers.update(_other_user(client))
        yield client


def project(api, name="项目") -> dict:
    response = api.post("/v1/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def upload(api, name="a.txt", project_id=None, parent_id=None) -> dict:
    params = {k: v for k, v in {"project_id": project_id, "parent_id": parent_id}.items() if v is not None}
    response = api.post("/v1/files", params=params, files={"upload": (name, b"data", "text/plain")})
    assert response.status_code == 201, response.text
    return response.json()


def artifact(api, run_id, data=b"output") -> dict:
    key = f"staging/{run_id}/out.txt"
    get_storage().put_bytes(key, data)
    response = api.post(
        "/internal/artifacts",
        headers=SERVICE,
        json={
            "run_id": run_id,
            "staging_key": key,
            "filename": "out.txt",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_project_crud_counts_and_utc(api):
    one = project(api, "  项目 A  ")
    assert one["name"] == "项目 A" and one["conversations"] == one["files"] == 0
    assert one["created_at"].endswith("Z") and one["updated_at"].endswith("Z")
    assert set(one) == {"id", "name", "created_at", "updated_at", "conversations", "files"}
    two = project(api, "项目 B")
    conv = api.post("/v1/conversations", json={"project_id": one["id"]}).json()
    upload(api, project_id=one["id"])
    detail = api.get(f"/v1/projects/{one['id']}").json()
    assert detail["conversations"] == detail["files"] == 1
    assert api.delete(f"/v1/conversations/{conv['id']}").status_code == 204
    assert api.get(f"/v1/projects/{one['id']}").json()["conversations"] == 0
    renamed = api.patch(f"/v1/projects/{one['id']}", json={"name": "  新名字  "}).json()
    assert renamed["name"] == "新名字" and renamed["updated_at"] > one["updated_at"]
    assert [p["id"] for p in api.get("/v1/projects").json()] == [one["id"], two["id"]]
    assert api.delete(f"/v1/projects/{two['id']}").status_code == 204
    assert api.get(f"/v1/projects/{two['id']}").status_code == 404


@pytest.mark.parametrize("name", ["", "   ", "a" * 121])
def test_project_name_validation(api, name):
    assert api.post("/v1/projects", json={"name": name}).status_code == 422
    p = project(api)
    assert api.patch(f"/v1/projects/{p['id']}", json={"name": name}).status_code == 422


def test_ownership_checks(api):
    p = project(api)
    outsider = {"project_id": p["id"]}
    assert api.get(f"/v1/projects/{p['id']}", headers=AUTH).status_code == 404
    assert api.patch(f"/v1/projects/{p['id']}", headers=AUTH, json={"name": "x"}).status_code == 404
    assert api.delete(f"/v1/projects/{p['id']}", headers=AUTH).status_code == 404
    assert p["id"] not in [x["id"] for x in api.get("/v1/projects", headers=AUTH).json()]
    for path in ("/v1/conversations", "/v1/files"):
        assert api.get(path, headers=AUTH, params=outsider).status_code == 404
    assert api.post("/v1/conversations", headers=AUTH, json=outsider).status_code == 404
    conv = api.post("/v1/conversations", headers=AUTH, json={}).json()
    assert api.patch(f"/v1/conversations/{conv['id']}", headers=AUTH, json=outsider).status_code == 404
    assert api.post("/v1/files", headers=AUTH, params=outsider, files={"upload": ("x.txt", b"x")}).status_code == 404
    assert api.post("/v1/files/presign", headers=AUTH, json={"filename": "x", **outsider}).status_code == 404


def test_conversation_isolation_move_and_activity(api):
    p = project(api)
    plain = api.post("/v1/conversations", json={}).json()
    grouped = api.post("/v1/conversations", json={"project_id": p["id"]}).json()
    assert plain["project_id"] is None and grouped["project_id"] == p["id"]
    assert [c["id"] for c in api.get("/v1/conversations").json()] == [plain["id"]]
    assert [c["id"] for c in api.get("/v1/conversations", params={"project_id": p["id"]}).json()] == [grouped["id"]]
    url = f"/v1/conversations/{plain['id']}"
    assert api.patch(url, json={}).status_code == 422
    assert api.patch(url, json={"title": None}).status_code == 422
    assert api.patch(url, json={"title": "x" * 201}).status_code == 422
    assert api.patch(url, json={"project_id": p["id"], "title": "改名"}).json()["title"] == "改名"
    assert api.patch(url, json={"project_id": None}).json()["project_id"] is None
    response = api.post(f"/v1/conversations/{grouped['id']}/messages", json={"prompt": "hello"})
    assert response.status_code == 201, response.text
    assert api.get(f"/v1/projects/{p['id']}").json()["updated_at"] > p["updated_at"]


def test_upload_and_artifact_inherit_project(api):
    p = project(api)
    assert upload(api, project_id=p["id"])["project_id"] == p["id"]
    conv = api.post("/v1/conversations", json={"project_id": p["id"]}).json()
    run = api.post(f"/v1/conversations/{conv['id']}/messages", json={"prompt": "create"}).json()
    api.post(f"/internal/runs/{run['id']}/status", headers=SERVICE, json={"status": "running"})
    out = artifact(api, run["id"])
    file = api.get(f"/v1/files/{out['file_id']}").json()
    assert file["project_id"] == p["id"] and not file["starred"]
    assert api.get(f"/v1/projects/{p['id']}").json()["files"] == 2


def test_project_delete_removes_ledger_storage_and_cancels_run(api):
    p = project(api)
    conv = api.post("/v1/conversations", json={"project_id": p["id"]}).json()
    run = api.post(f"/v1/conversations/{conv['id']}/messages", json={"prompt": "create"}).json()
    api.post(f"/internal/runs/{run['id']}/status", headers=SERVICE, json={"status": "running"})
    out = artifact(api, run["id"])
    parent = upload(api, project_id=p["id"])
    child = upload(api, parent_id=parent["id"])
    upload(api, project_id=p["id"], parent_id=parent["id"])
    with SessionLocal() as db:
        file = db.get(FileObject, parent["id"])
        file.meta = {"derived_key": f"derived/{file.id}", "text_key": f"text/{file.id}"}
        keys = [f.storage_key for f in db.scalars(select(FileObject).where(FileObject.project_id == p["id"]))]
        keys += list(file.meta.values())
        for key in file.meta.values():
            get_storage().put_bytes(key, b"derived")
        db.commit()
    response = api.delete(f"/v1/projects/{p['id']}")
    assert response.status_code == 204, response.text
    assert all(not get_storage().exists(key) for key in keys)
    with SessionLocal() as db:
        assert db.get(Project, p["id"]) is None
        assert db.get(Conversation, conv["id"]).deleted_at is not None
        assert db.get(Conversation, conv["id"]).project_id is None
        assert db.get(Run, run["id"]).status == "canceled"
        assert db.scalar(select(func.count()).select_from(Artifact).where(Artifact.file_id == out["file_id"])) == 0
        assert db.get(FileObject, parent["id"]) is None
        assert db.get(FileObject, out["file_id"]) is None
        assert db.get(FileObject, child["id"]).parent_id is None
        assert db.scalar(select(RunEvent).where(RunEvent.run_id == run["id"], RunEvent.type == "run.canceled")) is not None
    assert api.get(f"/v1/conversations/{conv['id']}").status_code == 404


def test_project_delete_rolls_back_cancellation_on_failure(api, monkeypatch):
    from ailab_platform.api import routes_projects

    p = project(api)
    conv = api.post("/v1/conversations", json={"project_id": p["id"]}).json()
    run = api.post(f"/v1/conversations/{conv['id']}/messages", json={"prompt": "create"}).json()

    def fail(*args, **kwargs):
        raise RuntimeError("deletion failed")

    monkeypatch.setattr(routes_projects, "delete_file_rows", fail)
    with pytest.raises(RuntimeError, match="deletion failed"):
        api.delete(f"/v1/projects/{p['id']}")
    with SessionLocal() as db:
        assert db.get(Project, p["id"]) is not None
        assert db.get(Conversation, conv["id"]).deleted_at is None
        assert db.get(Run, run["id"]).status == "queued"
        assert db.scalar(select(RunEvent).where(RunEvent.run_id == run["id"], RunEvent.type == "run.canceled")) is None
