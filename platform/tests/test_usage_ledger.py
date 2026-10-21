"""Usage is recorded with the weight and cache hits of the moment, and quota reads exactly that."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ailab_platform import catalog as catalog_mod
from ailab_platform.agent.loop import AgentLoop
from ailab_platform.api.main import app
from ailab_platform.api.reaper import REAPER_LOCK, reap_guarded
from ailab_platform.config import settings
from ailab_platform.db import SessionLocal
from ailab_platform.llm import LLMResponse, OpenAICompatibleClient
from ailab_platform.models import ModelUsage, User
from ailab_platform.queue import InMemoryQueue
from ailab_platform.quota import tokens_used_since

AUTH = {"Authorization": "Bearer test-token"}
SERVICE = {"X-Service-Token": "test-service"}


@pytest.fixture()
def api():
    with TestClient(app) as c:
        yield c


def _used_today() -> int:
    day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == settings.dev_user_email))
        return tokens_used_since(db, user.id, day)


def _record(api, model: str, prompt: int, completion: int = 0, cached: int = 0) -> str:
    run_id = api.post("/v1/runs", headers=AUTH, json={"prompt": "x", "model": model}).json()["id"]
    body = {"model": model, "prompt_tokens": prompt, "completion_tokens": completion, "cached_tokens": cached}
    assert api.post(f"/internal/runs/{run_id}/usage", headers=SERVICE, json=body).status_code == 200
    return run_id


def test_changing_a_weight_later_does_not_rewrite_past_calls(api, monkeypatch) -> None:
    before = _used_today()
    _record(api, "test-pro", prompt=100)  # weight 3 when recorded
    assert _used_today() - before == 300

    monkeypatch.setattr(catalog_mod.Catalog, "weight_of", lambda self, model_id: 10.0)  # an admin edits models.toml
    assert _used_today() - before == 300, "history must keep the weight it was recorded with"
    _record(api, "test-pro", prompt=100)
    assert _used_today() - before == 300 + 1000, "new calls use the new weight"


def test_cached_prompt_tokens_are_discounted(api) -> None:
    before = _used_today()
    _record(api, "test-fast", prompt=1000, completion=50, cached=800)  # weight 1
    # 200 uncached + 800 x 0.1 cached + 50 completion
    assert _used_today() - before == 200 + 80 + 50


def test_cached_tokens_cannot_exceed_the_prompt(api) -> None:
    before = _used_today()
    _record(api, "test-fast", prompt=100, cached=5000)  # nonsense from a buggy upstream
    assert _used_today() - before == 10  # all 100 prompt tokens counted as cached, nothing negative


def test_run_detail_reports_cached_tokens(api) -> None:
    run_id = _record(api, "test-fast", prompt=100, completion=10, cached=60)
    usage = api.get(f"/v1/runs/{run_id}", headers=AUTH).json()["usage"]
    assert (usage["prompt_tokens"], usage["completion_tokens"], usage["cached_tokens"]) == (100, 10, 60)


def test_negative_numbers_are_refused(api) -> None:
    run_id = api.post("/v1/runs", headers=AUTH, json={"prompt": "x"}).json()["id"]
    bad = api.post(f"/internal/runs/{run_id}/usage", headers=SERVICE, json={"model": "m", "prompt_tokens": -5})
    assert bad.status_code == 422
    with SessionLocal() as db:
        assert db.scalar(select(ModelUsage).where(ModelUsage.run_id == run_id)) is None


@pytest.mark.parametrize(
    "usage,expected",
    [
        ({"prompt_tokens": 10, "prompt_cache_hit_tokens": 7}, 7),  # DeepSeek
        ({"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 4}}, 4),  # OpenAI / vLLM
        ({"prompt_tokens": 10}, 0),
        ({"prompt_tokens": 10, "prompt_cache_hit_tokens": None, "prompt_tokens_details": None}, 0),
        ({"prompt_tokens": 10, "prompt_cache_hit_tokens": "n/a"}, 0),
    ],
)
def test_llm_client_reads_cached_tokens_from_either_format(usage, expected) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": usage})

    client = OpenAICompatibleClient("http://gw.test/v1", transport=httpx.MockTransport(handler))
    resp = asyncio.run(client.chat([{"role": "user", "content": "hi"}], [], "m"))
    assert resp.usage["cached_tokens"] == expected


def test_agent_reports_cached_tokens_to_the_platform() -> None:
    from tests.test_agent_loop import FakePlatform, RichBroker, ScriptedLLM

    seen = []

    class Platform(FakePlatform):
        async def record_usage(self, run_id, model, prompt, completion, cached=0):
            seen.append((model, prompt, completion, cached))

    llm = ScriptedLLM([LLMResponse(content="done", usage={"prompt_tokens": 50, "completion_tokens": 5, "cached_tokens": 30})])
    asyncio.run(AgentLoop(platform=Platform(), broker=RichBroker(), llm=llm).process("run_1"))
    assert seen == [("m", 50, 5, 30)]


# ---- several API replicas share one reaper pass per interval -------------------------------------------


def test_only_one_replica_reaps_per_interval() -> None:
    now = [0.0]
    queue = InMemoryQueue(clock=lambda: now[0])
    first = reap_guarded(queue)
    second = reap_guarded(queue)  # another replica a moment later
    assert first is not None and second is None
    assert queue.is_locked(REAPER_LOCK)

    now[0] += settings.reaper_interval_seconds  # the next interval
    assert reap_guarded(queue) is not None
    assert json.dumps(first)  # a stats dict, not an exception
