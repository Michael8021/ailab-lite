from __future__ import annotations

import asyncio

import httpx
import pytest

from ailab_platform import llm as llm_mod
from ailab_platform.config import settings
from ailab_platform.llm import INVALID_ARGUMENTS_KEY, LLMError, OpenAICompatibleClient


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(llm_mod.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(settings, "llm_max_retries", 2)
    monkeypatch.setattr(settings, "llm_retry_backoff_seconds", 1.0)
    return sleeps


def _ok(content="hi", **message_extra) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content, **message_extra}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        },
    )


def _client(handler) -> tuple[OpenAICompatibleClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(len(seen), request)

    return OpenAICompatibleClient("http://gateway.test/v1", "k", transport=httpx.MockTransport(wrapped)), seen


def _chat(client, **kw):
    return asyncio.run(client.chat([{"role": "user", "content": "x"}], [], "m", **kw))


def test_retries_transient_errors_with_backoff(_fast_retries) -> None:
    client, seen = _client(lambda n, r: httpx.Response(503, text="busy") if n < 3 else _ok())
    assert _chat(client).content == "hi"
    assert len(seen) == 3
    assert _fast_retries == [1.0, 2.0]


def test_gives_up_after_max_retries() -> None:
    client, seen = _client(lambda n, r: httpx.Response(429, text="slow down"))
    with pytest.raises(LLMError, match="429"):
        _chat(client)
    assert len(seen) == 3


def test_client_errors_are_not_retried() -> None:
    client, seen = _client(lambda n, r: httpx.Response(400, text="bad model"))
    with pytest.raises(LLMError, match="bad model"):
        _chat(client)
    assert len(seen) == 1


def test_network_errors_are_retried() -> None:
    def handler(n, request):
        if n == 1:
            raise httpx.ConnectError("boom")
        return _ok()

    client, seen = _client(handler)
    assert _chat(client).content == "hi"
    assert len(seen) == 2


def test_user_id_header_is_forwarded_when_given() -> None:
    client, seen = _client(lambda n, r: _ok())
    _chat(client, user_id="usr_1")
    _chat(client)
    assert seen[0].headers["x-user-id"] == "usr_1"
    assert "x-user-id" not in seen[1].headers


def test_truncated_output_is_an_error() -> None:
    client, _ = _client(lambda n, r: httpx.Response(200, json={"choices": [{"message": {"content": "par"}, "finish_reason": "length"}]}))
    with pytest.raises(LLMError, match="truncated"):
        _chat(client)


def test_malformed_tool_arguments_are_flagged_not_guessed() -> None:
    call = {"id": "c1", "function": {"name": "write_file", "arguments": '{"path": "/workspace/a", "content": "unterminated'}}
    client, _ = _client(lambda n, r: _ok(content="", tool_calls=[call]))
    resp = _chat(client)
    assert INVALID_ARGUMENTS_KEY in resp.tool_calls[0].arguments
    assert "path" not in resp.tool_calls[0].arguments


def test_reasoning_content_is_captured() -> None:
    client, _ = _client(lambda n, r: _ok(reasoning_content="thinking..."))
    assert _chat(client).reasoning_content == "thinking..."


def _sse(*chunks: dict) -> httpx.Response:
    import json as _json

    body = "".join(f"data: {_json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def test_streaming_hands_over_thinking_then_text_and_assembles_tool_calls() -> None:
    client, _ = _client(
        lambda n, r: _sse(
            {"choices": [{"delta": {"reasoning_content": "let me "}}]},
            {"choices": [{"delta": {"reasoning_content": "think"}}]},
            {"choices": [{"delta": {"content": "Hel"}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "content": "lo",
                            "tool_calls": [{"index": 0, "id": "c1", "function": {"name": "exec", "arguments": '{"cmd"'}}],
                        }
                    }
                ]
            },
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ': "ls"}'}}]}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 7, "prompt_cache_hit_tokens": 2}},
        )
    )
    seen: list[tuple[str, str]] = []

    async def text(t):
        seen.append(("text", t))

    async def thinking(t):
        seen.append(("think", t))

    r = asyncio.run(client.chat_stream([{"role": "user", "content": "x"}], [], "m", on_text=text, on_reasoning=thinking))
    assert seen == [("think", "let me "), ("think", "think"), ("text", "Hel"), ("text", "lo")]
    assert r.content == "Hello" and r.reasoning_content == "let me think"
    assert r.tool_calls[0].name == "exec" and r.tool_calls[0].arguments == {"cmd": "ls"}
    assert r.usage == {"prompt_tokens": 5, "completion_tokens": 7, "cached_tokens": 2}


def test_reasoning_is_stripped_unless_the_upstream_wants_it_back() -> None:
    import json as _json

    history = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "", "reasoning_content": "why", "tool_calls": []},
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
    ]
    bodies: list[dict] = []

    def handler(n, request):
        body = _json.loads(request.content)
        bodies.append(body)
        if "reasoning_content" not in body["messages"][1]:
            return httpx.Response(
                400, json={"error": {"message": "The `reasoning_content` in the thinking mode must be passed back to the API."}}
            )
        return _sse({"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}) if body.get("stream") else _ok()

    client, _ = _client(handler)
    assert asyncio.run(client.chat(history, [], "m")).content == "hi"
    assert ["reasoning_content" in b["messages"][1] for b in bodies] == [False, True]
    assert client.echo_reasoning is True  # remembered: the next call sends it straight away
    asyncio.run(client.chat_stream(history, [], "m"))
    assert "reasoning_content" in bodies[-1]["messages"][1]


def test_an_unrelated_400_is_not_retried_with_reasoning() -> None:
    client, seen = _client(lambda n, r: httpx.Response(400, text="bad request: unknown model"))
    with pytest.raises(LLMError, match="400"):
        _chat(client)
    assert len(seen) == 1 and client.echo_reasoning is False
