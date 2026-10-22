"""Skills each user switches on and off: the list, the API, the prompt, load_skill and the composer's buttons."""

from __future__ import annotations

import asyncio
import uuid

from fastapi.testclient import TestClient

from ailab_platform import accounts
from ailab_platform.agent.loop import AgentLoop
from ailab_platform.agent.prompts import system_prompt_for
from ailab_platform.api.main import app
from ailab_platform.db import SessionLocal, init_db
from ailab_platform.llm import LLMResponse, ToolCall

from .test_agent_loop import FakeBroker, FakePlatform, ScriptedLLM

SERVICE = {"X-Service-Token": "test-service"}


def _user(c: TestClient) -> dict:
    """A fresh user, so switching skills off does not leak into other tests."""
    init_db()
    name = f"s{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        accounts.create_user(db, name, "skills-password")
        db.commit()
    return {"Authorization": "Bearer " + c.post("/v1/auth/login", json={"username": name, "password": "skills-password"}).json()["token"]}


def test_skills_are_listed_on_by_default_and_switch_per_user() -> None:
    with TestClient(app) as c:
        me, other = _user(c), _user(c)
        listed = c.get("/v1/skills", headers=me).json()
        assert [(s["name"], s["title"], s["enabled"]) for s in listed] == [
            ("pptx", "演示文稿（PPT）", True),
            ("xlsx", "表格（Excel）", True),
        ]
        off = c.put("/v1/skills/pptx", headers=me, json={"enabled": False})
        assert off.status_code == 200 and off.json()["enabled"] is False
        assert {s["name"]: s["enabled"] for s in c.get("/v1/skills", headers=me).json()} == {"pptx": False, "xlsx": True}
        assert all(s["enabled"] for s in c.get("/v1/skills", headers=other).json())  # someone else's settings are untouched
        assert c.put("/v1/skills/pptx", headers=me, json={"enabled": True}).json()["enabled"] is True
        assert c.put("/v1/skills/nope", headers=me, json={"enabled": False}).status_code == 404
        assert c.get("/v1/skills").status_code == 401


def test_a_switched_off_skill_is_refused_and_reaches_the_agent_as_off() -> None:
    with TestClient(app) as c:
        me = _user(c)
        c.put("/v1/skills/pptx", headers=me, json={"enabled": False})
        refused = c.post("/v1/runs", headers=me, json={"prompt": "PPT", "skill": "pptx"})
        assert refused.status_code == 400 and "已关闭" in refused.json()["detail"]
        run = c.post("/v1/runs", headers=me, json={"prompt": "做个表格", "skill": "xlsx"}).json()
        assert c.get(f"/internal/runs/{run['id']}", headers=SERVICE).json()["disabled_skills"] == ["pptx"]


def test_the_prompt_lists_only_the_skills_that_are_on() -> None:
    both = system_prompt_for("work")
    assert "- pptx：" in both and "- xlsx：" in both
    no_ppt = system_prompt_for("work", disabled=frozenset({"pptx"}))
    assert "- pptx：" not in no_ppt and "- xlsx：" in no_ppt
    none = system_prompt_for("work", disabled=frozenset({"pptx", "xlsx"}))
    assert "关闭了所有技能" in none
    assert "本轮已选技能" not in system_prompt_for("work", "pptx", frozenset({"pptx"}))  # never preloaded when off


def test_load_skill_is_refused_for_a_switched_off_skill() -> None:
    class Platform(FakePlatform):
        async def get_run(self, run_id):
            return {**(await super().get_run(run_id)), "disabled_skills": ["pptx"]}

    platform = Platform()
    read = ToolCall(id="c1", name="load_skill", arguments={"name": "pptx"})
    llm = ScriptedLLM([LLMResponse(tool_calls=[read]), LLMResponse(content="ok")])
    asyncio.run(AgentLoop(platform=platform, broker=FakeBroker(), llm=llm).process("run_1"))
    result = next(p for t, p in platform.events if t == "agent.tool_result")
    assert result["ok"] is False and "已被用户关闭" in result["error"]
