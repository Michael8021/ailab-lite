"""Model gateway client (OpenAI-compatible) plus a deterministic mock.

The mock keeps the whole platform runnable without a DeepSeek key; switching
``AILAB_LLM_PROVIDER=openai`` points the same code at Higress/GPUStack.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import cache
from typing import Any

import httpx

from .catalog import get_catalog
from .config import settings

log = logging.getLogger("ailab.llm")


class LLMError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = "stop"
    reasoning_content: str = ""
    thinking_seconds: float = 0.0  # filled in by the agent loop from when the reasoning streamed


TextSink = Callable[[str], Awaitable[None]]


class LLMClient:
    async def chat(self, messages: list[dict], tools: list[dict], model: str, user_id: str | None = None) -> LLMResponse:
        raise NotImplementedError

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict],
        model: str,
        user_id: str | None = None,
        on_text: TextSink | None = None,
        on_reasoning: TextSink | None = None,
    ) -> LLMResponse:
        """Like chat(), but hands the model's thinking (on_reasoning) and then the answer text (on_text) over as they
        are produced. Default: in pieces once the whole response is there."""
        response = await self.chat(messages, tools, model, user_id=user_id)
        if on_reasoning and response.reasoning_content:
            for i in range(0, len(response.reasoning_content), 24):
                await on_reasoning(response.reasoning_content[i : i + 24])
        if on_text and response.content:
            step = 24
            for i in range(0, len(response.content), step):
                await on_text(response.content[i : i + step])
        return response


RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
INVALID_ARGUMENTS_KEY = "_invalid_arguments"


def _parse_tool_calls(raw_calls: list[dict]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for i, raw in enumerate(raw_calls):
        fn = raw.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            # Never run a tool on guessed arguments (write_file would write an empty file).
            args = {INVALID_ARGUMENTS_KEY: fn.get("arguments")}
        calls.append(
            ToolCall(
                id=raw.get("id") or f"call_{int(time.time())}_{i}",
                name=fn.get("name") or "",
                arguments=args if isinstance(args, dict) else {INVALID_ARGUMENTS_KEY: fn.get("arguments")},
            )
        )
    return calls


def _usage(usage: dict) -> dict[str, int]:
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "cached_tokens": _cached_tokens(usage),
    }


class _StreamState:
    """Accumulates an OpenAI-style chat completion stream into one LLMResponse."""

    def __init__(self) -> None:
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.calls: dict[int, dict] = {}
        self.usage: dict = {}
        self.finish = "stop"
        self.completed = False
        self.emitted = False  # some text already reached the user

    def feed(self, chunk: dict) -> tuple[str, str]:
        """Take one chunk; return the (answer text, thinking) it adds."""
        if chunk.get("error"):  # vLLM reports a failure mid-stream as an error chunk with HTTP 200
            raise LLMError(f"model stream failed: {str(chunk['error'])[:300]}")
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        text = thinking = ""
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("reasoning_content"):
                self.reasoning.append(delta["reasoning_content"])
                thinking += delta["reasoning_content"]
            if delta.get("content"):
                self.content.append(delta["content"])
                text += delta["content"]
            for tc in delta.get("tool_calls") or []:
                acc = self.calls.setdefault(int(tc.get("index", 0)), {"id": "", "function": {"name": "", "arguments": ""}})
                acc["id"] = tc.get("id") or acc["id"]
                fn = tc.get("function") or {}
                acc["function"]["name"] += fn.get("name") or ""
                acc["function"]["arguments"] += fn.get("arguments") or ""
            if choice.get("finish_reason"):
                self.finish = choice["finish_reason"]
                self.completed = True
        return text, thinking

    def response(self) -> LLMResponse:
        if self.finish == "length":
            raise LLMError("model output was truncated (finish_reason=length); raise max tokens or shorten the task")
        return LLMResponse(
            content="".join(self.content),
            tool_calls=_parse_tool_calls([self.calls[i] for i in sorted(self.calls)]),
            usage=_usage(self.usage),
            finish_reason=self.finish,
            reasoning_content="".join(self.reasoning),
        )


class OpenAICompatibleClient(LLMClient):
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        upstream_model: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        echo_reasoning: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.upstream_model = upstream_model  # what the upstream calls the model; None = send the id as is
        self._transport = transport
        # Thinking models (DeepSeek V4.x) reject a tool conversation unless each assistant turn carries its
        # reasoning_content back; other upstreams may reject the extra field. Off by default, and switched on
        # for good the first time the upstream asks for it (see _asks_for_reasoning).
        self.echo_reasoning = echo_reasoning

    def _outgoing(self, messages: list[dict]) -> list[dict]:
        if self.echo_reasoning:
            return messages
        return [{k: v for k, v in m.items() if k != "reasoning_content"} if "reasoning_content" in m else m for m in messages]

    def _asks_for_reasoning(self, exc: LLMError) -> bool:
        text = str(exc)
        if self.echo_reasoning or "reasoning_content" not in text or " 400" not in text:
            return False
        log.warning("upstream %s wants reasoning_content passed back; echoing it from now on", self.base_url)
        self.echo_reasoning = True
        return True

    async def _post(self, payload: dict, headers: dict) -> dict:
        """POST with bounded retries on transient failures (429 / 5xx / network)."""
        attempts = settings.llm_max_retries + 1
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds, transport=self._transport) as client:
                    resp = await client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
            except httpx.TransportError as exc:  # timeouts, connection resets, DNS
                if last:
                    raise LLMError(f"model gateway unreachable after {attempts} attempts: {exc!r}") from exc
                reason = repr(exc)
            else:
                if resp.status_code < 400:
                    return resp.json()
                body = resp.text[:300]
                if resp.status_code not in RETRYABLE_STATUS or last:
                    raise LLMError(f"model gateway returned {resp.status_code}: {body}")
                reason = f"{resp.status_code} {body}"
            delay = settings.llm_retry_backoff_seconds * (2**attempt)
            log.warning("model call failed (%s); retry %d/%d in %.1fs", reason, attempt + 1, attempts - 1, delay)
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _request(self, messages: list[dict], tools: list[dict], model: str, user_id: str | None, stream: bool) -> tuple[dict, dict]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if user_id:
            # The gateway's per-user token rate limit keys on this header; without
            # it requests are not limited at all.
            headers["x-user-id"] = user_id
        payload: dict[str, Any] = {
            "model": self.upstream_model or model,
            "messages": self._outgoing(messages),
            "temperature": 0.2,
            "stream": stream,
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload, headers

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict],
        model: str,
        user_id: str | None = None,
        on_text: TextSink | None = None,
        on_reasoning: TextSink | None = None,
    ) -> LLMResponse:
        try:
            return await self._stream_once(messages, tools, model, user_id, on_text, on_reasoning)
        except LLMError as exc:
            if self._asks_for_reasoning(exc):
                return await self._stream_once(messages, tools, model, user_id, on_text, on_reasoning)
            raise

    async def _stream_once(self, messages, tools, model, user_id, on_text, on_reasoning) -> LLMResponse:
        """Server-sent events. Retried like chat() on transient failures, but only until the first text was passed on:
        after that a retry would show the user the start of the answer twice."""
        payload, headers = self._request(messages, tools, model, user_id, stream=True)
        attempts = settings.llm_max_retries + 1
        for attempt in range(attempts):
            last = attempt == attempts - 1
            state = _StreamState()
            try:
                async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds, transport=self._transport) as client:
                    async with client.stream("POST", f"{self.base_url}/chat/completions", json=payload, headers=headers) as resp:
                        if resp.status_code < 400:
                            async for line in resp.aiter_lines():
                                if not line.startswith("data:") or line[5:].strip() == "":
                                    continue
                                if line[5:].strip() == "[DONE]":
                                    state.completed = True
                                    break
                                text, thinking = state.feed(json.loads(line[5:]))
                                if thinking and on_reasoning:
                                    state.emitted = True
                                    await on_reasoning(thinking)
                                if text and on_text:
                                    state.emitted = True
                                    await on_text(text)
                            if not state.completed:
                                raise LLMError("model stream is incomplete: ended without a completion marker")
                            return state.response()
                        body = (await resp.aread()).decode("utf-8", "replace")[:300]
                        if resp.status_code not in RETRYABLE_STATUS or last:
                            raise LLMError(f"model gateway returned {resp.status_code}: {body}")
                        reason = f"{resp.status_code} {body}"
            except (httpx.TransportError, json.JSONDecodeError) as exc:
                if last or state.emitted:
                    raise LLMError(f"model stream broke: {exc!r}") from exc
                reason = repr(exc)
            delay = settings.llm_retry_backoff_seconds * (2**attempt)
            log.warning("model stream failed (%s); retry %d/%d in %.1fs", reason, attempt + 1, attempts - 1, delay)
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    async def chat(self, messages: list[dict], tools: list[dict], model: str, user_id: str | None = None) -> LLMResponse:
        try:
            data = await self._post(*self._request(messages, tools, model, user_id, stream=False))
        except LLMError as exc:
            if not self._asks_for_reasoning(exc):
                raise
            data = await self._post(*self._request(messages, tools, model, user_id, stream=False))

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason") or "stop"
        if finish_reason == "length":
            raise LLMError("model output was truncated (finish_reason=length); raise max tokens or shorten the task")
        return LLMResponse(
            content=message.get("content") or "",
            tool_calls=_parse_tool_calls(message.get("tool_calls") or []),
            usage=_usage(data.get("usage") or {}),
            finish_reason=finish_reason,
            reasoning_content=message.get("reasoning_content") or "",
        )


def _cached_tokens(usage: dict) -> int:
    """Prompt tokens served from the upstream's cache, whichever way it reports them."""
    cached = usage.get("prompt_cache_hit_tokens")
    if cached is None:
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    try:
        return max(0, int(cached or 0))
    except (TypeError, ValueError):
        return 0


def _assistant_steps(messages: list[dict]) -> int:
    """Tool rounds since the user's latest message (earlier turns in the history do not count)."""
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=0)
    return sum(1 for m in messages[last_user:] if m.get("role") == "assistant" and m.get("tool_calls"))


def _user_prompt(messages: list[dict]) -> str:
    return next((m.get("content") or "" for m in reversed(messages) if m.get("role") == "user"), "")


def _wants_ppt(prompt: str) -> bool:
    text = prompt.lower()
    return any(k in text for k in ("ppt", "幻灯片", "演示文稿", "汇报材料", "做个演示"))


def _wants_xlsx(prompt: str) -> bool:
    text = prompt.lower()
    return any(k in text for k in ("excel", "xlsx", "表格", "报表"))


def _call(step: int, tool: str, arguments: dict) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(id=f"call_mock_{step}_{tool}", name=tool, arguments=dict(arguments))])


# Each scripted task: the tool calls in order, then the closing message.
_PPT = [
    ("load_skill", {"name": "pptx"}),
    ("exec", {"cmd": "mkdir -p /workspace/work && python /opt/skills/pptx/scripts/build.py --example > /workspace/work/deck.json"}),
    ("exec", {"cmd": "python /opt/skills/pptx/scripts/build.py /workspace/work/deck.json /workspace/outputs/ailab-demo.pptx"}),
    ("deliver_file", {"path": "/workspace/outputs/ailab-demo.pptx", "filename": "ailab-demo.pptx"}),
]
_XLSX = [
    ("load_skill", {"name": "xlsx"}),
    ("exec", {"cmd": "mkdir -p /workspace/work && python /opt/skills/xlsx/scripts/build_xlsx.py --example > /workspace/work/book.json"}),
    ("exec", {"cmd": "python /opt/skills/xlsx/scripts/build_xlsx.py /workspace/work/book.json /workspace/outputs/sales.xlsx"}),
    ("deliver_file", {"path": "/workspace/outputs/sales.xlsx", "filename": "sales.xlsx"}),
]


class MockLLM(LLMClient):
    """Deterministic tool-calling policy used by tests and the dev demo."""

    async def chat(self, messages: list[dict], tools: list[dict], model: str, user_id: str | None = None) -> LLMResponse:
        prompt = _user_prompt(messages)
        step = _assistant_steps(messages)
        usage = {"prompt_tokens": max(1, len(json.dumps(messages, ensure_ascii=False)) // 4), "completion_tokens": 20}

        script, closing = None, ""
        system = str(messages[0].get("content") or "") if messages else ""
        picked = next((k for k in ("pptx", "xlsx") if f"本轮已选技能：{k}" in system), "")
        if not tools:  # chat mode: words only
            pass
        elif picked == "pptx" or _wants_ppt(prompt):
            script, closing = _PPT, "已完成：演示文稿已生成并交付到资料库，右侧可以预览。想调整哪一页、换什么配色，直接告诉我。"
        elif picked == "xlsx" or _wants_xlsx(prompt):
            script, closing = _XLSX, "表格已生成并交付：包含三个区域的月度数据、合计行和柱状图。"
        elif "文件" in prompt or "总结" in prompt:
            content = f"# 处理结果\n\n原始请求：{prompt}\n\n这是 Agent Service 生成的文件。\n"
            script = [
                ("write_file", {"path": "/workspace/outputs/answer.md", "content": content}),
                ("deliver_file", {"path": "/workspace/outputs/answer.md", "filename": "answer.md"}),
            ]
            closing = "文件已生成并交付。"
        elif "沙箱" in prompt or "命令" in prompt:
            script = [("exec", {"cmd": "echo 'hello from ailab sandbox'; pwd; ls -la"})]
            closing = "沙箱命令已执行完成。"

        if script is None:  # plain chat: answer directly, no tools
            turns = sum(1 for m in messages if m.get("role") == "user" and not m.get("content", "").startswith("（平台提示"))
            attached = prompt.count("（附件《")
            text = (
                f"你好！这是**离线演示模型**的回答（本对话第 {turns} 轮）。\n\n"
                "- 它不调用真实大模型，只用来演示完整链路\n"
                "- 说「做一个 PPT」或「做一个 Excel 表格」可以看到沙箱生成文件和实时预览\n\n"
                f"> 你刚才说：{prompt.split(chr(10))[0][:80]}" + (f"\n\n我读到了 {attached} 个附件的内容。" if attached else "")
            )
            thinking = f"用户说：「{prompt[:40]}」。这是离线演示模型，不需要工具，按固定模板直接回答，并提示可以试试做 PPT 或表格。"
            return LLMResponse(content=text, usage=usage, reasoning_content=thinking)
        if step < len(script):
            name, arguments = script[step]
            response = _call(step, name, arguments)
            response.usage = usage
            return response
        return LLMResponse(content=closing, usage=usage)


@cache
def llm_for(model_id: str) -> LLMClient:
    """The client for a catalog model: its own upstream URL, key and upstream model name."""
    spec = get_catalog().get(model_id)
    if spec is None or not spec.available:
        raise LLMError(f"model {model_id!r} is not available")
    if spec.provider == "mock":
        return MockLLM()
    return OpenAICompatibleClient(
        spec.base_url, spec.api_key(), spec.upstream_model or spec.id, echo_reasoning=spec.echo_reasoning or settings.llm_echo_reasoning
    )


def reset_llm_cache() -> None:
    llm_for.cache_clear()
