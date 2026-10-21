from __future__ import annotations

import asyncio
import json
from datetime import UTC
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from ailab_platform import catalog as catalog_mod
from ailab_platform.api.main import app
from ailab_platform.catalog import CatalogError, get_catalog, load_catalog, parse_catalog
from ailab_platform.config import settings
from ailab_platform.llm import LLMError, MockLLM, OpenAICompatibleClient, llm_for, reset_llm_cache

AUTH = {"Authorization": "Bearer test-token"}
SERVICE = {"X-Service-Token": "test-service"}


def _entry(**kw) -> dict:
    return {"id": "m1", "provider": "mock", **kw}


# ---- parsing and validation -----------------------------------------------------------------------


def test_the_shipped_catalog_is_valid_and_keyless() -> None:
    shipped = Path(__file__).resolve().parents[1] / "models.toml"  # the file the repo and the image ship
    cat = load_catalog(shipped)
    assert cat.get("ailab-agent-pro").upstream_model == "deepseek-chat"
    text = shipped.read_text()
    assert "sk-" not in text, "a key must never be written into the catalog"


@pytest.mark.parametrize(
    "data,message",
    [
        ({}, "at least one"),
        ({"models": []}, "at least one"),
        ({"models": [_entry(id="bad id")]}, "bad id"),
        ({"models": [_entry(), _entry()]}, "duplicate"),
        ({"models": [_entry(provider="telepathy")]}, "provider must be"),
        ({"models": [_entry(provider="openai")]}, "base_url is required"),
        ({"models": [_entry(provider="openai", base_url="http://x", api_key_env="AILAB_SERVICE_TOKEN")]}, "must start with AILAB_KEY_"),
        ({"models": [_entry(weight=0)]}, "weight must be"),
        ({"models": [_entry(), _entry(id="m2", default=True), _entry(id="m3", default=True)]}, "more than one"),
        ({"models": [_entry(surprise=1)]}, "unknown keys"),
    ],
)
def test_bad_catalogs_are_refused_with_a_reason(data, message) -> None:
    with pytest.raises(CatalogError, match=message):
        parse_catalog(data)


def test_missing_or_broken_file_is_an_error_not_a_silent_default(tmp_path) -> None:
    with pytest.raises(CatalogError, match="AILAB_MODELS_FILE"):
        load_catalog(tmp_path / "nope.toml")
    (tmp_path / "bad.toml").write_text("[[models]\nid=")
    with pytest.raises(CatalogError):
        load_catalog(tmp_path / "bad.toml")


# ---- availability and defaults ----------------------------------------------------------------------


def test_a_model_without_its_key_is_hidden(monkeypatch) -> None:
    monkeypatch.delenv("AILAB_KEY_X", raising=False)
    cat = parse_catalog(
        {
            "models": [
                _entry(id="needs-key", provider="openai", base_url="http://gw", api_key_env="AILAB_KEY_X", default=True),
                _entry(id="free-gateway", provider="openai", base_url="http://internal-gw"),  # keyless upstream is fine
                _entry(id="demo"),
            ]
        }
    )
    assert [m.id for m in cat.available()] == ["free-gateway", "demo"]
    assert cat.default_id() == "free-gateway"  # the marked default is hidden: fall back to the first usable one
    monkeypatch.setenv("AILAB_KEY_X", "sk-123")
    assert cat.default_id() == "needs-key"


def test_key_is_read_from_the_env_file_too(monkeypatch, tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("AILAB_KEY_FROM_FILE=sk-from-dotenv\n")
    monkeypatch.setenv("AILAB_ENV_FILE", str(env))
    monkeypatch.delenv("AILAB_KEY_FROM_FILE", raising=False)
    assert catalog_mod.secret("AILAB_KEY_FROM_FILE") == "sk-from-dotenv"
    monkeypatch.setenv("AILAB_KEY_FROM_FILE", "sk-from-environment")
    assert catalog_mod.secret("AILAB_KEY_FROM_FILE") == "sk-from-environment"  # the real environment wins


def test_weights_and_unknown_models() -> None:
    cat = get_catalog()
    assert cat.weight_of("test-pro") == 3 and cat.weight_of("test-fast") == 1
    assert cat.weight_of("deleted-or-renamed-model") == 1


# ---- what users can see ------------------------------------------------------------------------------


@pytest.fixture()
def api():
    with TestClient(app) as c:
        yield c


def test_models_endpoint_lists_usable_models_and_leaks_nothing(api) -> None:
    assert api.get("/v1/models").status_code == 401
    body = api.get("/v1/models", headers=AUTH).json()
    assert body["default"] == "test-pro"
    assert [m["id"] for m in body["models"]] == ["test-pro", "test-fast"]  # not the keyless or the disabled one
    assert [m["default"] for m in body["models"]] == [True, False]
    text = json.dumps(body).lower()
    for secret in ("gateway.invalid", "upstream", "api_key", "ailab_key", "base_url", "deepseek"):
        assert secret not in text


def test_run_uses_the_chosen_model_or_the_default(api) -> None:
    chosen = api.post("/v1/runs", headers=AUTH, json={"prompt": "x", "model": "test-fast"}).json()
    default = api.post("/v1/runs", headers=AUTH, json={"prompt": "x"}).json()
    assert chosen["model"] == "test-fast" and default["model"] == "test-pro"


@pytest.mark.parametrize("model", ["test-no-key", "test-disabled", "gpt-whatever", "../etc"])
def test_unusable_models_are_refused(api, model) -> None:
    resp = api.post("/v1/runs", headers=AUTH, json={"prompt": "x", "model": model})
    assert resp.status_code == 400 and "not available" in resp.json()["detail"]


def test_quota_counts_weighted_tokens(api, monkeypatch) -> None:
    from datetime import datetime

    from sqlalchemy import select

    from ailab_platform.db import SessionLocal
    from ailab_platform.models import User
    from ailab_platform.quota import tokens_used_since

    day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == settings.dev_user_email))
        before = tokens_used_since(db, user.id, day)
    pro = api.post("/v1/runs", headers=AUTH, json={"prompt": "x", "model": "test-pro"}).json()["id"]
    fast = api.post("/v1/runs", headers=AUTH, json={"prompt": "x", "model": "test-fast"}).json()["id"]
    for run_id, model in ((pro, "test-pro"), (fast, "test-fast")):
        api.post(f"/internal/runs/{run_id}/usage", headers=SERVICE, json={"model": model, "prompt_tokens": 80, "completion_tokens": 20})
    with SessionLocal() as db:
        # 100 tokens at weight 3 plus 100 tokens at weight 1
        assert tokens_used_since(db, user.id, day) - before == 400


# ---- how a run reaches its model -----------------------------------------------------------------------


def test_each_model_gets_its_own_upstream(monkeypatch) -> None:
    reset_llm_cache()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization"), json.loads(request.content)["model"]))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    client = OpenAICompatibleClient("http://gw.test/v1", "sk-secret", "deepseek-chat", transport=httpx.MockTransport(handler))
    asyncio.run(client.chat([{"role": "user", "content": "hi"}], [], "ailab-agent-pro"))  # the id the user picked
    assert seen == [("http://gw.test/v1/chat/completions", "Bearer sk-secret", "deepseek-chat")]  # the upstream's name


def test_llm_for_builds_clients_from_the_catalog(monkeypatch) -> None:
    reset_llm_cache()
    assert isinstance(llm_for("test-fast"), MockLLM)
    with pytest.raises(LLMError, match="not available"):
        llm_for("test-no-key")
    with pytest.raises(LLMError):
        llm_for("not-in-the-catalog")

    cat = parse_catalog(
        {"models": [_entry(id="real", provider="openai", base_url="http://gw/v1/", api_key_env="AILAB_KEY_REAL", upstream_model="up-name")]}
    )
    monkeypatch.setattr(catalog_mod, "get_catalog", lambda: cat)
    monkeypatch.setattr("ailab_platform.llm.get_catalog", lambda: cat)
    monkeypatch.setenv("AILAB_KEY_REAL", "sk-real")
    reset_llm_cache()
    client = llm_for("real")
    assert (client.base_url, client.api_key, client.upstream_model) == ("http://gw/v1", "sk-real", "up-name")
    reset_llm_cache()


def test_usage_endpoint_reports_weighted_usage_against_the_limits(api, monkeypatch) -> None:
    assert api.get("/v1/usage").status_code == 401
    before = api.get("/v1/usage", headers=AUTH).json()
    run_id = api.post("/v1/runs", headers=AUTH, json={"prompt": "x", "model": "test-pro"}).json()["id"]
    api.post(f"/internal/runs/{run_id}/usage", headers=SERVICE, json={"model": "test-pro", "prompt_tokens": 10, "completion_tokens": 0})
    after = api.get("/v1/usage", headers=AUTH).json()
    assert after["daily_tokens"]["used"] - before["daily_tokens"]["used"] == 30  # 10 tokens x weight 3
    assert after["daily_tokens"]["limit"] == settings.daily_token_limit
    assert after["monthly_tokens"]["used"] >= after["daily_tokens"]["used"]
    assert after["library_bytes"]["limit"] == settings.library_quota_bytes and after["resets_at_utc"].endswith("+00:00")
