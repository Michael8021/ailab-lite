"""对话 / 工作 mode, a skill picked in the composer, and attachments read without a sandbox."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from ailab_platform.agent.loop import AgentLoop
from ailab_platform.api.main import app
from ailab_platform.llm import LLMResponse, ToolCall
from ailab_platform.storage import get_storage
from ailab_platform.worker.pipeline import process_file

from .test_agent_loop import FakeBroker, FakePlatform, ScriptedLLM

AUTH = {"Authorization": "Bearer test-token"}
SERVICE = {"X-Service-Token": "test-service"}


# ---- API: the options are stored with the run ---------------------------------------------------


def test_mode_and_skill_are_stored_and_checked() -> None:
    with TestClient(app) as c:
        conv = c.post("/v1/conversations", headers=AUTH, json={}).json()
        chat = c.post(f"/v1/conversations/{conv['id']}/messages", headers=AUTH, json={"prompt": "hi", "mode": "chat"}).json()
        assert chat["options"] == {"mode": "chat"}
        c.post(f"/v1/runs/{chat['id']}/cancel", headers=AUTH)
        deck = c.post(
            f"/v1/conversations/{conv['id']}/messages", headers=AUTH, json={"prompt": "PPT", "mode": "chat", "skill": "pptx"}
        ).json()
        assert deck["options"] == {"mode": "work", "skill": "pptx", "skill_title": "演示文稿（PPT）"}  # a skill is work by definition
        c.post(f"/v1/runs/{deck['id']}/cancel", headers=AUTH)
        assert c.post(f"/v1/conversations/{conv['id']}/messages", headers=AUTH, json={"prompt": "x", "skill": "nope"}).status_code == 400
        assert c.post(f"/v1/conversations/{conv['id']}/messages", headers=AUTH, json={"prompt": "x", "mode": "other"}).status_code == 422
        default = c.post("/v1/runs", headers=AUTH, json={"prompt": "x"}).json()
        assert default["options"] == {"mode": "work"}


def test_the_agent_gets_each_attachments_text(monkeypatch) -> None:
    from ailab_platform.api import routes_internal

    monkeypatch.setattr(routes_internal, "ATTACHMENT_CHARS", 50)
    with TestClient(app) as c:
        ids = []
        for name, body in (("long.txt", "字" * 80), ("short.txt", "hello")):
            up = c.post("/v1/files", headers=AUTH, files={"upload": (name, body.encode(), "text/plain")}).json()
            process_file(up["id"], platform=_ApiClient(c), storage=get_storage())
            ids.append(up["id"])
        run = c.post("/v1/runs", headers=AUTH, json={"prompt": "总结附件", "file_ids": ids}).json()
        files = c.get(f"/internal/runs/{run['id']}", headers=SERVICE).json()["input_files"]
        assert [(f["filename"], f["chars"], len(f["text"]), f["truncated"]) for f in files] == [
            ("long.txt", 80, 50, True),
            ("short.txt", 5, 5, False),
        ]
        assert "text" not in c.get(f"/v1/runs/{run['id']}", headers=AUTH).json()["input_files"][0]  # only the agent gets it


class _ApiClient:
    """The Worker's PlatformClient, served by the test client."""

    def __init__(self, c: TestClient) -> None:
        self.c = c

    def get_file(self, file_id):
        return self.c.get(f"/internal/files/{file_id}", headers=SERVICE).json()

    def get_file_bytes(self, file_id):
        return self.c.get(f"/internal/files/{file_id}/content", headers=SERVICE).content

    def patch_file(self, file_id, meta=None, status=None):
        return self.c.patch(f"/internal/files/{file_id}", headers=SERVICE, json={"meta": meta, "status": status}).json()


# ---- agent ----------------------------------------------------------------------------------------


class _Platform(FakePlatform):
    def __init__(self, **run) -> None:
        super().__init__()
        self.extra = run

    async def get_run(self, run_id):
        return {**(await super().get_run(run_id)), **self.extra}


class _Recording(ScriptedLLM):
    def __init__(self, responses) -> None:
        super().__init__(responses)
        self.offered: list = []

    async def chat(self, messages, tools, model, user_id=None):
        self.offered.append(tools)
        return await super().chat(messages, tools, model, user_id)


def _run(llm, platform, broker=None):
    broker = broker or FakeBroker()
    asyncio.run(AgentLoop(platform=platform, broker=broker, llm=llm).process("run_1"))
    return broker


def test_chat_mode_offers_no_tools_and_never_starts_a_sandbox() -> None:
    llm = _Recording([LLMResponse(content="只用文字回答")])
    broker = _run(llm, _Platform(options={"mode": "chat"}))
    assert llm.offered == [[]] and broker.leases == 0
    assert "「对话」模式" in llm.seen_messages[0][0]["content"]


def test_a_picked_skill_comes_preloaded() -> None:
    llm = _Recording([LLMResponse(content="ok")])
    platform = _Platform(options={"mode": "work", "skill": "pptx"})
    _run(llm, platform)
    system = llm.seen_messages[0][0]["content"]
    assert "本轮已选技能：pptx" in system and "/opt/skills/pptx/scripts/build.py" in system
    assert llm.offered[0]  # work mode: tools offered
    assert ("agent.skill", {"name": "pptx", "title": "演示文稿（PPT）"}) in platform.events


def test_attachment_text_is_in_the_message() -> None:
    files = [
        {"file_id": "f1", "filename": "notes.txt", "chars": 1200, "text": "第一段内容", "truncated": True},
        {"file_id": "f2", "filename": "photo.jpg", "chars": 0, "text": "", "truncated": False},
    ]
    llm = _Recording([LLMResponse(content="ok")])
    platform = _Platform(input_files=files, options={"mode": "chat"})
    _run(llm, platform)
    user = llm.seen_messages[0][-1]["content"]
    assert "附件《notes.txt》：共 1200 字，以下是前 5 字" in user and "第一段内容" in user
    assert "附件《photo.jpg》：没有可直接读取的文字" in user
    event = next(p for t, p in platform.events if t == "agent.attachments")
    assert event["files"] == [{"filename": "notes.txt", "chars": 1200, "truncated": True}]


def test_work_mode_still_lets_the_model_decide() -> None:
    step = ToolCall(id="c1", name="exec", arguments={"cmd": "true"})
    llm = _Recording([LLMResponse(tool_calls=[step]), LLMResponse(content="done")])
    broker = _run(llm, _Platform(options={"mode": "work"}))
    assert broker.leases == 1 and all(llm.offered)
