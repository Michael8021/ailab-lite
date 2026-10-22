"""Conversations: turns, history for the model, the workspace handed from turn to turn, previews."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from ailab_platform import accounts
from ailab_platform.api.main import app
from ailab_platform.db import SessionLocal, init_db
from ailab_platform.storage import get_storage

AUTH = {"Authorization": "Bearer test-token"}
SERVICE = {"X-Service-Token": "test-service"}


def _status(c, run_id, status, **body):
    r = c.post(f"/internal/runs/{run_id}/status", headers=SERVICE, json={"status": status, **body})
    assert r.status_code == 200, r.text
    return r.json()


def _finish(c, run_id, summary, **result):
    _status(c, run_id, "running")
    return _status(c, run_id, "succeeded", summary=summary, result=result)


def _other_user(c) -> dict:
    init_db()
    name = f"o{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        accounts.create_user(db, name, "other-password")
        db.commit()
    token = c.post("/v1/auth/login", json={"username": name, "password": "other-password"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_a_conversation_collects_turns_and_gets_a_title() -> None:
    with TestClient(app) as c:
        conv = c.post("/v1/conversations", headers=AUTH, json={}).json()
        assert conv["title"] == ""
        first = c.post(f"/v1/conversations/{conv['id']}/messages", headers=AUTH, json={"prompt": "帮我做一个关于城市更新的 PPT\n细节…"})
        assert first.status_code == 201, first.text
        assert first.json()["conversation_id"] == conv["id"]

        # one answer at a time
        busy = c.post(f"/v1/conversations/{conv['id']}/messages", headers=AUTH, json={"prompt": "再改一下"})
        assert busy.status_code == 409

        _finish(
            c, first.json()["id"], "做好了", previews=[{"filename": "deck.pptx", "kind": "pages", "pages": ["1/p-01.jpg"], "version": 1}]
        )
        second = c.post(f"/v1/conversations/{conv['id']}/messages", headers=AUTH, json={"prompt": "第三页换成图表"}).json()

        detail = c.get(f"/v1/conversations/{conv['id']}", headers=AUTH).json()
        assert detail["title"] == "帮我做一个关于城市更新的 PPT"
        assert [t["prompt"] for t in detail["turns"]] == ["帮我做一个关于城市更新的 PPT\n细节…", "第三页换成图表"]
        assert detail["previews"][0]["filename"] == "deck.pptx" and detail["previews"][0]["run_id"] == first.json()["id"]
        assert detail["last_status"] == "queued"

        listed = c.get("/v1/conversations", headers=AUTH).json()
        assert listed[0]["id"] == conv["id"]  # the most recently active first

        internal = c.get(f"/internal/runs/{second['id']}", headers=SERVICE).json()
        assert internal["history"] == [
            {"prompt": "帮我做一个关于城市更新的 PPT\n细节…", "reply": "做好了", "status": "succeeded", "files": []}
        ]
        assert internal["workspace_restore_key"] is None  # the first turn saved none
        public = c.get(f"/v1/runs/{second['id']}", headers=AUTH).json()
        assert "history" not in public and "workspace_restore_key" not in public


def test_the_newest_workspace_is_restored_and_older_ones_are_dropped() -> None:
    storage = get_storage()
    with TestClient(app) as c:
        conv = c.post("/v1/conversations", headers=AUTH, json={}).json()
        send = lambda p: c.post(f"/v1/conversations/{conv['id']}/messages", headers=AUTH, json={"prompt": p}).json()  # noqa: E731
        one = send("做个表格")
        storage.put_bytes(f"workspaces/{one['id']}.tgz", b"tar-1")
        _finish(c, one["id"], "ok", workspace_saved=True)
        two = send("加一列")
        assert c.get(f"/internal/runs/{two['id']}", headers=SERVICE).json()["workspace_restore_key"] == f"workspaces/{one['id']}.tgz"

        storage.put_bytes(f"workspaces/{two['id']}.tgz", b"tar-2")
        _finish(c, two["id"], "ok", workspace_saved=True)
        assert not storage.exists(f"workspaces/{one['id']}.tgz")
        three = send("再加一列")
        assert c.get(f"/internal/runs/{three['id']}", headers=SERVICE).json()["workspace_restore_key"] == f"workspaces/{two['id']}.tgz"


def test_failed_and_canceled_turns_are_described_in_the_history() -> None:
    with TestClient(app) as c:
        conv = c.post("/v1/conversations", headers=AUTH, json={}).json()
        one = c.post(f"/v1/conversations/{conv['id']}/messages", headers=AUTH, json={"prompt": "a"}).json()
        _status(c, one["id"], "running")
        _status(c, one["id"], "failed", error="boom")
        two = c.post(f"/v1/conversations/{conv['id']}/messages", headers=AUTH, json={"prompt": "b"}).json()
        c.post(f"/v1/runs/{two['id']}/cancel", headers=AUTH)
        three = c.post(f"/v1/conversations/{conv['id']}/messages", headers=AUTH, json={"prompt": "c"}).json()
        history = c.get(f"/internal/runs/{three['id']}", headers=SERVICE).json()["history"]
        assert "boom" in history[0]["reply"] and "中止" in history[1]["reply"]


def test_rename_delete_and_privacy() -> None:
    with TestClient(app) as c:
        conv = c.post("/v1/conversations", headers=AUTH, json={"title": "草稿"}).json()
        run = c.post(f"/v1/conversations/{conv['id']}/messages", headers=AUTH, json={"prompt": "hi"}).json()
        assert c.patch(f"/v1/conversations/{conv['id']}", headers=AUTH, json={"title": "新名字"}).json()["title"] == "新名字"

        other = _other_user(c)
        assert c.get(f"/v1/conversations/{conv['id']}", headers=other).status_code == 404
        assert c.post(f"/v1/conversations/{conv['id']}/messages", headers=other, json={"prompt": "x"}).status_code == 404
        assert conv["id"] not in [x["id"] for x in c.get("/v1/conversations", headers=other).json()]

        assert c.delete(f"/v1/conversations/{conv['id']}", headers=AUTH).status_code == 204
        assert c.get(f"/v1/conversations/{conv['id']}", headers=AUTH).status_code == 404
        assert c.get(f"/v1/runs/{run['id']}", headers=AUTH).json()["status"] == "canceled"  # the answer in progress stops


def test_previews_are_served_to_the_owner_only() -> None:
    storage = get_storage()
    with TestClient(app) as c:
        run = c.post("/v1/runs", headers=AUTH, json={"prompt": "做个 PPT"}).json()
        storage.put_bytes(f"previews/{run['id']}/1/p-01.jpg", b"\xff\xd8jpeg")
        storage.put_bytes(f"previews/{run['id']}/1/sheets.json", b'{"sheets": []}')
        page = c.get(f"/v1/runs/{run['id']}/previews/1/p-01.jpg", headers=AUTH)
        assert page.status_code == 200 and page.headers["content-type"] == "image/jpeg" and page.content == b"\xff\xd8jpeg"
        assert c.get(f"/v1/runs/{run['id']}/previews/1/sheets.json", headers=AUTH).json() == {"sheets": []}
        for bad in ("1/../../workspaces/x.tgz", "1/p-01.png", "x/p-01.jpg", "1/secret.json"):
            assert c.get(f"/v1/runs/{run['id']}/previews/{bad}", headers=AUTH).status_code == 404, bad
        assert c.get(f"/v1/runs/{run['id']}/previews/2/p-01.jpg", headers=AUTH).status_code == 404
        assert c.get(f"/v1/runs/{run['id']}/previews/1/p-01.jpg", headers=_other_user(c)).status_code == 404
