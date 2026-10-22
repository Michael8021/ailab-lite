"""Boundary and concurrency regressions found in the full repository review."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from ailab_platform.api.main import app
from ailab_platform.config import settings
from ailab_platform.db import SessionLocal, engine
from ailab_platform.llm import LLMError, LLMResponse, OpenAICompatibleClient, ToolCall
from ailab_platform.models import FileObject, User
from ailab_platform.quota import library_bytes
from ailab_platform.storage import S3Storage, get_storage

from .test_agent_loop import FakePlatform, ScriptedLLM, _run
from .test_artifacts import _commit, _running_run, _stage
from .test_skill_settings import _user

AUTH = {"Authorization": "Bearer test-token"}
SERVICE = {"X-Service-Token": "test-service"}


@pytest.fixture()
def api():
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("prompt", [" ", "\n\t", "\u3000"])
def test_blank_prompt_is_rejected_without_creating_a_run(api, prompt):
    conv = api.post("/v1/conversations", headers=AUTH, json={}).json()["id"]
    for path in ("/v1/runs", f"/v1/conversations/{conv}/messages"):
        response = api.post(path, headers=AUTH, json={"prompt": prompt})
        assert response.status_code == 422, response.text


def test_api_timestamps_are_unambiguous_utc(api):
    conv = api.post("/v1/conversations", headers=AUTH, json={}).json()
    run = api.post(f"/v1/conversations/{conv['id']}/messages", headers=AUTH, json={"prompt": "time"}).json()
    file = api.post("/v1/files", headers=AUTH, files={"upload": ("time.txt", b"x", "text/plain")}).json()
    events = api.get(f"/v1/runs/{run['id']}/events/history", headers=AUTH).json()
    for item in (conv, run, file, events[0]):
        assert item["created_at"].endswith(("Z", "+00:00")), item["created_at"]


def _presigned(api, monkeypatch, data=b"0123456789"):
    storage = get_storage()
    monkeypatch.setattr(storage, "presign_put", lambda key, expires=900: f"https://upload.test/{key}")
    response = api.post("/v1/files/presign", headers=AUTH, json={"filename": "input.txt"})
    assert response.status_code == 200, response.text
    file_id = response.json()["file_id"]
    with SessionLocal() as db:
        key = db.get(FileObject, file_id).storage_key
    storage.put_bytes(key, data)
    return file_id, key


def test_reusing_a_presigned_put_cannot_change_the_completed_file(api, monkeypatch):
    file_id, signed_key = _presigned(api, monkeypatch, b"scanned content")
    response = api.post(f"/v1/files/{file_id}/complete", headers=AUTH)
    assert response.status_code == 200, response.text
    storage = get_storage()
    storage.put_bytes(signed_key, b"unscanned replacement")  # the URL is valid for another 15 minutes
    api.patch(f"/internal/files/{file_id}", headers=SERVICE, json={"status": "ready"})
    assert api.get(f"/v1/files/{file_id}/content", headers=AUTH).content == b"scanned content"
    with SessionLocal() as db:
        assert db.get(FileObject, file_id).sha256 == hashlib.sha256(b"scanned content").hexdigest()


def test_empty_presigned_upload_is_rejected(api, monkeypatch):
    file_id, _ = _presigned(api, monkeypatch, b"")
    assert api.post(f"/v1/files/{file_id}/complete", headers=AUTH).status_code == 400


def test_presigned_completions_share_the_library_quota_lock(api, monkeypatch):
    from ailab_platform.api import routes_files

    files = [_presigned(api, monkeypatch)[0] for _ in range(6)]
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == settings.dev_user_email))
        used = library_bytes(db, user.id)
    monkeypatch.setattr(settings, "library_quota_bytes", used + 15)
    enforce = routes_files.enforce_library_quota

    def slow_check(*args):
        enforce(*args)
        time.sleep(0.04)  # open the check/write window that previously let every completion through

    monkeypatch.setattr(routes_files, "enforce_library_quota", slow_check)

    def complete(file_id):
        return TestClient(app).post(f"/v1/files/{file_id}/complete", headers=AUTH).status_code

    with ThreadPoolExecutor(max_workers=len(files)) as pool:
        codes = list(pool.map(complete, files))
    assert sorted(codes) == [200] + [413] * 5


def test_deleting_a_file_cleans_up_extracted_text(api):
    response = api.post("/v1/files", headers=AUTH, files={"upload": ("remove.txt", b"private text", "text/plain")})
    file_id = response.json()["id"]
    text_key = f"derived/{file_id}/text.txt"
    storage = get_storage()
    storage.put_bytes(text_key, b"private extracted text")
    api.patch(f"/internal/files/{file_id}", headers=SERVICE, json={"meta": {"text_key": text_key}})
    assert api.delete(f"/v1/files/{file_id}", headers=AUTH).status_code == 204
    assert not storage.exists(text_key)


def test_download_header_quotes_untrusted_filenames():
    from ailab_platform.api.routes_files import content_disposition

    header = content_disposition('report"\r\nX-Injected: yes.txt')
    assert "\r" not in header and "\n" not in header
    assert 'report\\"X-Injected: yes.txt";' in header
    assert "%0D%0A" in header  # the original filename is retained in the encoded parameter


@pytest.mark.parametrize("with_tool", [False, True])
def test_token_limit_applies_to_final_answers_and_precedes_tool_execution(monkeypatch, with_tool):
    monkeypatch.setattr(settings, "run_max_tokens", 100)
    calls = [ToolCall("c", "write_file", {"path": "/workspace/x", "content": "no"})] if with_tool else []
    llm = ScriptedLLM([LLMResponse(content="over budget", tool_calls=calls, usage={"prompt_tokens": 101})])
    platform, broker = _run(llm)
    assert platform.statuses[-1][0] == "failed"
    assert "token limit" in platform.statuses[-1][1]["error"]
    assert broker.leases == 0 and broker.put_calls == []


def test_recovered_run_keeps_the_tokens_consumed_by_earlier_attempts(monkeypatch):
    monkeypatch.setattr(settings, "run_max_tokens", 100)

    class RecoveredPlatform(FakePlatform):
        async def get_run(self, run_id):
            return {**await super().get_run(run_id), "usage": {"prompt_tokens": 80, "completion_tokens": 20}}

    llm = ScriptedLLM([])
    platform, _ = _run(llm, platform=RecoveredPlatform())
    assert "token limit" in platform.statuses[-1][1]["error"]
    assert llm.seen_messages == []


def test_chat_mode_refuses_unsolicited_tool_calls():
    class ChatPlatform(FakePlatform):
        async def get_run(self, run_id):
            return {**await super().get_run(run_id), "options": {"mode": "chat"}}

    call = ToolCall("c", "write_file", {"path": "/workspace/x", "content": "no"})
    platform, broker = _run(ScriptedLLM([LLMResponse(tool_calls=[call])]), platform=ChatPlatform())
    assert broker.leases == 0 and broker.put_calls == []
    assert platform.statuses[-1][0] == "failed"


def test_eof_without_a_completion_marker_is_not_a_success():
    chunk = {"choices": [{"delta": {"content": "partial answer"}}]}
    client = OpenAICompatibleClient(
        "https://llm.test",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\n")),
    )
    with pytest.raises(LLMError, match="incomplete"):
        asyncio.run(client.chat_stream([], [], "m"))


@pytest.mark.parametrize("early", [False, True])
def test_s3_stream_closes_the_response_body_even_when_stopped_early(early):
    class Body:
        closed = False

        def iter_chunks(self, _size):
            yield b"one"
            yield b"two"

        def close(self):
            self.closed = True

    body = Body()
    storage = object.__new__(S3Storage)
    storage.bucket = "test"
    storage.client = SimpleNamespace(get_object=lambda **_: {"Body": body})
    stream = storage.stream("x")
    if early:
        assert next(stream) == b"one"
        stream.close()
    else:
        assert list(stream) == [b"one", b"two"]
    assert body.closed


def test_conversation_list_has_a_constant_query_count(api):
    for _ in range(8):
        assert api.post("/v1/conversations", headers=AUTH, json={}).status_code == 201
    selects = []

    def count(_conn, _cursor, statement, *_):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        assert api.get("/v1/conversations", headers=AUTH).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(selects) <= 3, selects  # token, user, and the conversations with their last statuses


@pytest.mark.parametrize("service", ["agent", "worker", "broker"])
def test_readiness_returns_unavailable_when_a_dependency_or_consumer_is_down(monkeypatch, service):
    import importlib

    module = importlib.import_module(f"ailab_platform.{service}.main")
    if service == "broker":
        manager = SimpleNamespace(redis=SimpleNamespace(ping=lambda: False), provider=SimpleNamespace(name="mock", health=lambda: True))
        monkeypatch.setattr(module, "get_manager", lambda: manager)
    else:
        monkeypatch.setattr(module, "get_queue", lambda: SimpleNamespace(ping=lambda: True))
        monkeypatch.setattr(module, "_task" if service == "agent" else "_thread", None)
    response = TestClient(module.app).get("/readyz")
    assert response.status_code == 503
    assert response.json()["ok"] is False


def test_artifact_delivery_obeys_the_library_quota_and_retry_is_free(api, monkeypatch):
    run_id = _running_run(api)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == settings.dev_user_email))
        used = library_bytes(db, user.id)
    monkeypatch.setattr(settings, "library_quota_bytes", used + 4)
    body = _stage(b"fits")
    first = _commit(api, run_id, body)
    assert first.status_code == 200, first.text
    assert _commit(api, run_id, body).json()["file_id"] == first.json()["file_id"]
    rejected = _stage(b"exceeds")
    assert _commit(api, run_id, rejected).status_code == 413
    assert not get_storage().exists(rejected["staging_key"])


def test_simultaneous_skill_toggles_preserve_both_changes(api, monkeypatch):
    from ailab_platform.api import routes_skills

    auth = _user(api)
    disabled = routes_skills.disabled_skills

    def slow_read(user):
        result = disabled(user)
        time.sleep(0.05)
        return result

    monkeypatch.setattr(routes_skills, "disabled_skills", slow_read)

    def toggle(name):
        return TestClient(app).put(f"/v1/skills/{name}", headers=auth, json={"enabled": False}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(toggle, ["pptx", "xlsx"])) == [200, 200]
    assert all(not skill["enabled"] for skill in api.get("/v1/skills", headers=auth).json())
