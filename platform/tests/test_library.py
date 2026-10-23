"""Library filters, batch operations and bounded-memory archives."""

from __future__ import annotations

import asyncio
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from ailab_platform.api.main import app
from ailab_platform.api.routes_files import _archive_stream
from ailab_platform.config import settings
from ailab_platform.db import SessionLocal
from ailab_platform.models import FileObject
from ailab_platform.storage import get_storage
from tests.test_conversations import _other_user
from tests.test_projects import AUTH, SERVICE, project, upload


@pytest.fixture()
def api():
    with TestClient(app) as client:
        client.headers.update(_other_user(client))
        yield client


def ready(api, name="a.txt", data=b"data", content_type="text/plain") -> dict:
    response = api.post("/v1/files", files={"upload": (name, data, content_type)})
    assert response.status_code == 201, response.text
    file = response.json()
    assert api.patch(f"/internal/files/{file['id']}", headers=SERVICE, json={"status": "ready"}).status_code == 200
    return file


def listed(api, **params):
    response = api.get("/v1/files", params=params)
    assert response.status_code == 200, response.text
    return [file["id"] for file in response.json()]


def batch(api, ids, action, **body):
    return api.post("/v1/files/batch", json={"ids": ids, "action": action, **body})


def test_ids_filter_ownership_combination_and_limit(api):
    p = project(api)
    image = ready(api, "photo.png", content_type="image/png")
    other = upload(api)
    api.patch(f"/v1/files/{image['id']}", json={"starred": True, "project_id": p["id"]})
    ids = [("ids", f["id"]) for f in (image, other)] + [("ids", "missing")]
    assert {f["id"] for f in api.get("/v1/files", params=ids).json()} == {image["id"], other["id"]}
    assert api.get("/v1/files", params=ids, headers=AUTH).json() == []
    for params in ({"category": "starred"}, {"category": "images"}, {"project_id": p["id"]}, {"q": "photo"}):
        result = api.get("/v1/files", params=ids + list(params.items()))
        assert [f["id"] for f in result.json()] == [image["id"]]
    assert len(api.get("/v1/files", params=[("ids", image["id"])] * 200).json()) == 1
    assert api.get("/v1/files", params=[("ids", image["id"])] * 201).status_code == 422


def test_thumbnail_response_caching_ownership_and_cleanup(api, monkeypatch):
    from ailab_platform.api import routes_files

    file = ready(api, "photo.png", content_type="image/png")
    url = f"/v1/files/{file['id']}/thumbnail"
    assert api.get(f"/v1/files/{file['id']}").json()["thumbnail_url"] is None
    assert api.get(url).status_code == 404
    key = f"derived/{file['id']}/thumb.webp"
    get_storage().put_bytes(key, b"webp")
    api.patch(f"/internal/files/{file['id']}", headers=SERVICE, json={"meta": {"thumb_key": key}})
    assert api.get(f"/v1/files/{file['id']}").json()["thumbnail_url"] == url
    assert api.get("/v1/files", params={"ids": file["id"]}).json()[0]["thumbnail_url"] == url
    original_stream = get_storage().stream
    original_owned = routes_files._owned_file
    sessions = []

    def owned(db, *args):
        sessions.append(db)
        return original_owned(db, *args)

    monkeypatch.setattr(routes_files, "_owned_file", owned)

    def stream(*args, **kwargs):
        # Auth/list sessions must be released before the first storage read.
        assert sessions and not sessions[-1].in_transaction()
        yield from original_stream(*args, **kwargs)

    monkeypatch.setattr(get_storage(), "stream", stream)
    response = api.get(url)
    assert response.status_code == 200 and response.content == b"webp"
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["cache-control"] == "private, max-age=31536000, immutable"
    etag = response.headers["etag"]
    for value in (etag, f'"other", W/{etag}', "*"):
        cached = api.get(url, headers={"If-None-Match": value})
        assert cached.status_code == 304 and cached.content == b""
        assert cached.headers["etag"] == etag
    monkeypatch.setattr(routes_files, "THUMBNAIL_VERSION", "webp-next")
    upgraded = api.get(url, headers={"If-None-Match": etag})
    assert upgraded.status_code == 200 and upgraded.headers["etag"] != etag
    assert api.get(url, headers={"If-None-Match": '"different"'}).status_code == 200
    assert api.get(url, headers={**AUTH, "If-None-Match": etag}).status_code == 404
    assert api.get("/v1/files/missing/thumbnail").status_code == 404
    for status in ("pending", "rejected"):
        api.patch(f"/internal/files/{file['id']}", headers=SERVICE, json={"status": status})
        assert api.get(url, headers={"If-None-Match": etag}).status_code == 404
    assert api.delete(f"/v1/files/{file['id']}").status_code == 204
    assert not get_storage().exists(key)


def test_image_artifact_enqueues_thumbnail_job_and_worker_processes_it(api, monkeypatch):
    import hashlib

    from ailab_platform.api import routes_internal
    from ailab_platform.queue import InMemoryQueue
    from ailab_platform.worker.pipeline import process_file
    from tests.test_worker import image_bytes

    q = InMemoryQueue()
    monkeypatch.setattr(routes_internal, "get_queue", lambda: q)
    run = api.post("/v1/runs", json={"prompt": "image"}).json()
    assert api.post(f"/internal/runs/{run['id']}/status", headers=SERVICE, json={"status": "running"}).status_code == 200
    data = image_bytes()
    key = f"staging/{run['id']}/image.png"
    body = {
        "run_id": run["id"],
        "staging_key": key,
        "filename": "image.png",
        "content_type": "image/png",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    get_storage().put_bytes(key, data)
    response = api.post("/internal/artifacts", headers=SERVICE, json=body)
    assert response.status_code == 200, response.text
    fid = response.json()["file_id"]
    msg = q.dequeue(settings.file_queue, "w", timeout=1)
    assert msg.payload == {"file_id": fid, "thumbnail_only": True}

    class Platform:
        def get_file(self, file_id):
            return api.get(f"/internal/files/{file_id}", headers=SERVICE).json()

        def get_file_bytes(self, file_id, **kwargs):
            return api.get(f"/internal/files/{file_id}/content", headers=SERVICE).content

        def patch_file(self, file_id, **kwargs):
            assert api.patch(f"/internal/files/{file_id}", headers=SERVICE, json=kwargs).status_code == 200

    process_file(fid, platform=Platform(), thumbnail_only=True)
    file = api.get(f"/v1/files/{fid}").json()
    assert file["status"] == "ready" and file["meta"]["source"] == "sandbox" and file["thumbnail_url"]
    assert api.get(file["thumbnail_url"]).status_code == 200
    q.ack(settings.file_queue, msg)
    get_storage().put_bytes(key, data)
    assert api.post("/internal/artifacts", headers=SERVICE, json=body).json()["file_id"] == fid
    assert q.outstanding(settings.file_queue) == []
    assert api.delete(f"/v1/files/{fid}").status_code == 204

    def fail_queue():
        raise RuntimeError("offline")

    monkeypatch.setattr(routes_internal, "get_queue", fail_queue)
    get_storage().put_bytes(key, data)
    response = api.post("/internal/artifacts", headers=SERVICE, json=body)
    assert response.status_code == 200  # queue outage must not fail an artifact commit


def test_backfill_only_enqueues_ready_missing_images_in_batches(api, monkeypatch):
    from ailab_platform.queue import InMemoryQueue
    from ailab_platform.worker import backfill_thumbnails as backfill

    p = project(api)
    # Isolate the backfill selection from the rest of this suite's shared database.
    candidates = [
        ready(api, "missing.png", content_type="image/png"),
        ready(api, "generic.JPG", content_type="application/octet-stream"),
        ready(api, "done.png", content_type="image/png"),
        ready(api, "vector.svg", content_type="image/svg+xml"),
        ready(api, "text.txt"),
        upload(api, "pending.png"),
        ready(api, "rejected.png", content_type="image/png"),
    ]
    for file in candidates:
        api.patch(f"/v1/files/{file['id']}", json={"project_id": p["id"]})
    api.patch(f"/internal/files/{candidates[2]['id']}", headers=SERVICE, json={"meta": {"thumb_key": "existing"}})
    api.patch(f"/internal/files/{candidates[6]['id']}", headers=SERVICE, json={"status": "rejected"})
    q = InMemoryQueue()
    monkeypatch.setattr(backfill, "get_queue", lambda: q)
    before = [api.get(f"/v1/files/{f['id']}").json() for f in candidates]
    backfill.backfill_thumbnails(batch_size=1)
    assert before == [api.get(f"/v1/files/{f['id']}").json() for f in candidates]
    ids = {msg["file_id"] for msg in q.outstanding(settings.file_queue)}
    expected = {f["id"] for f in candidates[:2]}
    assert ids & {f["id"] for f in candidates} == expected
    for file in candidates[:2]:
        api.patch(f"/internal/files/{file['id']}", headers=SERVICE, json={"meta": {"thumb_key": "created"}})
    q2 = InMemoryQueue()
    monkeypatch.setattr(backfill, "get_queue", lambda: q2)
    backfill.backfill_thumbnails(batch_size=2)
    assert not expected & {msg["file_id"] for msg in q2.outstanding(settings.file_queue)}


def test_filters_order_search_and_pagination(api):
    p = project(api)
    one = ready(api, "100%_Report.txt")
    two = ready(api, "100X_Report.txt")
    image = ready(api, "pic.PNG", content_type="image/png")
    grouped = upload(api, project_id=p["id"])
    assert api.patch(f"/v1/files/{one['id']}", json={"starred": True}).status_code == 200
    assert api.patch(f"/v1/files/{two['id']}", json={"starred": True}).status_code == 200
    assert listed(api, category="starred") == [two["id"], one["id"]]
    assert listed(api, category="images") == [image["id"]]
    assert listed(api, project_id=p["id"]) == [grouped["id"]]
    assert listed(api, q="%") == [one["id"]]
    assert set(listed(api, q="_")) == {one["id"], two["id"]}
    assert listed(api, q="report", limit=1, offset=1) == [one["id"]]
    assert listed(api, q="\\") == []
    assert listed(api, limit=1) == [grouped["id"]]
    assert api.get("/v1/files", params={"category": "wrong"}).status_code == 422
    assert api.get("/v1/files", params={"limit": 201}).status_code == 422


def test_patch_file_move_star_and_validation(api):
    file = upload(api)
    p = project(api)
    url = f"/v1/files/{file['id']}"
    assert api.patch(url, json={}).status_code == 422
    assert api.patch(url, json={"starred": None}).status_code == 422
    result = api.patch(url, json={"starred": True, "project_id": p["id"]}).json()
    assert result["starred"] and result["project_id"] == p["id"]
    with SessionLocal() as db:
        starred_at = db.get(FileObject, file["id"]).starred_at
    assert api.patch(url, json={"starred": True}).status_code == 200
    with SessionLocal() as db:
        assert db.get(FileObject, file["id"]).starred_at == starred_at
    result = api.patch(url, json={"starred": False, "project_id": None}).json()
    assert not result["starred"] and result["project_id"] is None
    assert api.patch(url, json={"project_id": "missing"}).status_code == 404
    assert api.patch(url, headers=AUTH, json={"starred": True}).status_code == 404


def test_batch_star_unstar_and_move(api):
    one, two = upload(api), upload(api)
    ids = [one["id"], two["id"]]
    p = project(api)
    assert batch(api, [ids[0]], "star").json() == {"count": 1}
    with SessionLocal() as db:
        original = db.get(FileObject, ids[0]).starred_at
    assert batch(api, [*ids, ids[0]], "star").json() == {"count": 2}
    with SessionLocal() as db:
        assert db.get(FileObject, ids[0]).starred_at == original
    assert set(listed(api, category="starred")) == set(ids)
    assert batch(api, ids, "unstar").json() == {"count": 2}
    assert listed(api, category="starred") == []
    assert batch(api, ids, "move", project_id=p["id"]).json() == {"count": 2}
    assert set(listed(api, project_id=p["id"])) == set(ids)
    assert batch(api, ids, "move", project_id=None).status_code == 200
    assert listed(api, project_id=p["id"]) == []
    assert batch(api, ids, "move", project_id="missing").status_code == 404
    assert batch(api, [], "star").status_code == 422
    assert batch(api, ["x"] * 201, "star").status_code == 422


@pytest.mark.parametrize("action", ["star", "unstar", "move", "delete"])
def test_batch_is_all_or_nothing_for_unknown_or_foreign_ids(api, action):
    own = upload(api)
    foreign = api.post("/v1/files", headers=AUTH, files={"upload": ("foreign.txt", b"data")}).json()
    api.patch(f"/v1/files/{own['id']}", json={"starred": True})
    with SessionLocal() as db:
        key = db.get(FileObject, own["id"]).storage_key
        starred_at = db.get(FileObject, own["id"]).starred_at
    for bad_id in (foreign["id"], "missing"):
        assert batch(api, [own["id"], bad_id], action).status_code == 404
        with SessionLocal() as db:
            file = db.get(FileObject, own["id"])
            assert file is not None and file.starred_at == starred_at and file.project_id is None
        assert get_storage().exists(key)


def test_batch_delete_parent_rule_and_storage_cleanup(api):
    parent = upload(api, "parent.txt")
    child = upload(api, "child.txt", parent_id=parent["id"])
    other = upload(api, "other.txt")
    response = batch(api, [parent["id"], other["id"]], "delete")
    assert response.status_code == 409 and "child.txt" in response.json()["detail"]
    assert api.get(f"/v1/files/{other['id']}").status_code == 200
    assert api.delete(f"/v1/files/{parent['id']}").status_code == 409
    ids = [parent["id"], child["id"], other["id"]]
    with SessionLocal() as db:
        file = db.get(FileObject, child["id"])
        file.meta = {"derived_key": f"derived/{file.id}", "text_key": f"text/{file.id}"}
        keys = [db.get(FileObject, file_id).storage_key for file_id in ids] + list(file.meta.values())
        for key in file.meta.values():
            get_storage().put_bytes(key, b"derived")
        db.commit()
    assert batch(api, ids, "delete").json() == {"count": 3}
    assert all(not get_storage().exists(key) for key in keys)
    assert all(api.get(f"/v1/files/{file_id}").status_code == 404 for file_id in ids)


def test_presign_project_and_completion(api, monkeypatch):
    storage = get_storage()
    monkeypatch.setattr(storage, "presign_put", lambda *args, **kwargs: "https://upload.example.test")
    p = project(api)
    response = api.post("/v1/files/presign", json={"filename": "signed.txt", "project_id": p["id"]})
    assert response.status_code == 200, response.text
    file_id = response.json()["file_id"]
    with SessionLocal() as db:
        file = db.get(FileObject, file_id)
        assert file.project_id == p["id"]
        storage.put_bytes(file.storage_key, b"signed upload")
    completed = api.post(f"/v1/files/{file_id}/complete")
    assert completed.status_code == 200 and completed.json()["project_id"] == p["id"]


def test_archive_contents_names_and_headers(api):
    one = ready(api, "a.pdf", b"first")
    two = ready(api, "a.pdf", b"second")
    three = ready(api, "a (2).pdf", b"third")
    response = api.get("/v1/files/archive", params=[("ids", f["id"]) for f in (one, two, three, one)])
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert "content-length" not in response.headers
    assert "ailab-files-" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == ["a.pdf", "a (2).pdf", "a (2) (2).pdf"]
        assert [archive.read(name) for name in archive.namelist()] == [b"first", b"second", b"third"]
        assert all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist())


def test_archive_rejections(api, monkeypatch):
    file = ready(api)
    pending = upload(api, "pending.txt")
    response = api.get("/v1/files/archive", params=[("ids", file["id"]), ("ids", pending["id"])])
    assert response.status_code == 409 and "pending.txt" in response.json()["detail"]
    monkeypatch.setattr(settings, "max_archive_bytes", 3)
    assert api.get("/v1/files/archive", params={"ids": file["id"]}).status_code == 413
    assert api.get("/v1/files/archive", headers=AUTH, params={"ids": file["id"]}).status_code == 404
    assert api.get("/v1/files/archive", params={"ids": "missing"}).status_code == 404
    assert api.get("/v1/files/archive").status_code == 422
    assert api.get("/v1/files/archive", params=[("ids", file["id"])] * 201).status_code == 422


def test_archive_streams_chunks_and_closes_on_early_exit(monkeypatch):
    storage = get_storage()
    read = []
    closed = []

    def chunks(key):
        try:
            for i in range(100):
                read.append(i)
                yield b"x" * 65536
        finally:
            closed.append(key)

    monkeypatch.setattr(storage, "stream", chunks)
    archive = _archive_stream([("large.pdf", "large")])
    assert next(archive).startswith(b"PK")
    assert not read
    assert len(next(archive)) == 65536 and read == [0]
    archive.close()
    assert closed == ["large"]


@pytest.mark.parametrize("version", ["2.3", "2.4"])
def test_archive_disconnect_closes_storage(api, monkeypatch, version):
    from ailab_platform.api.routes_files import download_archive
    from ailab_platform.models import User

    file = ready(api)
    closed = []

    def chunks(key):
        try:
            while True:
                yield b"x" * 65536
        finally:
            closed.append(key)

    monkeypatch.setattr(get_storage(), "stream", chunks)
    with SessionLocal() as db:
        user = db.get(User, db.get(FileObject, file["id"]).user_id)
        response = download_archive([file["id"]], user, db)

    async def run():
        sent = 0

        async def send(message):
            nonlocal sent
            if message["type"] == "http.response.body":
                sent += 1
                if sent == 2:
                    raise OSError("client disconnected")

        async def receive():
            await asyncio.Event().wait()

        try:
            await response({"type": "http", "asgi": {"spec_version": version}}, receive, send)
        except (OSError, ExceptionGroup):
            pass
        except Exception as exc:
            assert type(exc).__name__ == "ClientDisconnect"
        assert len(closed) == 1  # release before the event loop shuts down

    asyncio.run(run())
    assert len(closed) == 1
