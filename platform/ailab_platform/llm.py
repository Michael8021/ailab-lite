"""Model gateway client (OpenAI-compatible) plus a deterministic mock.

The mock keeps the whole platform runnable without a DeepSeek key; switching
``AILAB_LLM_PROVIDER=openai`` points the same code at Higress/GPUStack.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
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


class LLMClient:
    async def chat(self, messages: list[dict], tools: list[dict], model: str, user_id: str | None = None) -> LLMResponse:
        raise NotImplementedError


RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
INVALID_ARGUMENTS_KEY = "_invalid_arguments"


class OpenAICompatibleClient(LLMClient):
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        upstream_model: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.upstream_model = upstream_model  # what the upstream calls the model; None = send the id as is
        self._transport = transport

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

    async def chat(self, messages: list[dict], tools: list[dict], model: str, user_id: str | None = None) -> LLMResponse:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if user_id:
            # The gateway's per-user token rate limit keys on this header; without
            # it requests are not limited at all.
            headers["x-user-id"] = user_id
        payload: dict[str, Any] = {
            "model": self.upstream_model or model,
            "messages": messages,
            "temperature": 0.2,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        data = await self._post(payload, headers)

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason") or "stop"
        if finish_reason == "length":
            raise LLMError("model output was truncated (finish_reason=length); raise max tokens or shorten the task")
        calls: list[ToolCall] = []
        for i, raw in enumerate(message.get("tool_calls") or []):
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
        usage = data.get("usage") or {}
        return LLMResponse(
            content=message.get("content") or "",
            tool_calls=calls,
            usage={
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
            },
            finish_reason=finish_reason,
            reasoning_content=message.get("reasoning_content") or "",
        )


def _assistant_steps(messages: list[dict]) -> int:
    return sum(1 for m in messages if m.get("role") == "assistant" and m.get("tool_calls"))


def _user_prompt(messages: list[dict]) -> str:
    return next((m.get("content") or "" for m in messages if m.get("role") == "user"), "")


def _wants_ppt(prompt: str) -> bool:
    text = prompt.lower()
    return any(k in text for k in ("ppt", "幻灯片", "演示文稿", "汇报材料", "做个演示"))


class MockLLM(LLMClient):
    """Deterministic tool-calling policy used by tests and the dev demo."""

    async def chat(self, messages: list[dict], tools: list[dict], model: str, user_id: str | None = None) -> LLMResponse:
        prompt = _user_prompt(messages)
        step = _assistant_steps(messages)
        usage = {"prompt_tokens": max(1, len(json.dumps(messages, ensure_ascii=False)) // 4), "completion_tokens": 20}

        if _wants_ppt(prompt):
            if step == 0:
                outline = {
                    "title": "AILab 平台演示",
                    "subtitle": "由 Agent Service 在沙箱内生成",
                    "slides": [
                        {"title": "平台链路", "bullets": ["Portal 创建 Run", "Agent 调用模型", "Broker 领取沙箱", "产出回写对象存储"]},
                        {"title": "隔离与权限", "bullets": ["沙箱默认拒绝全部网络", "无密钥、无 SA Token", "模型 Key 只在 Agent 侧"]},
                        {"title": "下一步", "bullets": ["接入 OpenSandbox", "切换 SeaweedFS", "对接 Keycloak"]},
                    ],
                }
                return LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id=f"call_mock_{step}_write",
                            name="write_file",
                            arguments={
                                "path": "/workspace/outline.json",
                                "content": json.dumps(outline, ensure_ascii=False, indent=2),
                            },
                        )
                    ],
                    usage=usage,
                )
            if step == 1:
                return LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id=f"call_mock_{step}_build",
                            name="exec",
                            arguments={
                                "cmd": "python /opt/skills/pptx-builder/scripts/build.py "
                                "/workspace/outline.json /workspace/outputs/demo.pptx"
                            },
                        )
                    ],
                    usage=usage,
                )
            if step == 2:
                return LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id=f"call_mock_{step}_preview",
                            name="exec",
                            arguments={
                                "cmd": "python /opt/skills/pptx-builder/scripts/preview.py /workspace/outputs/demo.pptx"
                            },
                        )
                    ],
                    usage=usage,
                )
            if step == 3:
                return LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id=f"call_mock_{step}_deliver",
                            name="deliver_file",
                            arguments={"path": "/workspace/outputs/demo.pptx", "filename": "ailab-demo.pptx"},
                        )
                    ],
                    usage=usage,
                )
            return LLMResponse(
                content="已完成：演示文稿已生成并交付到资料库。",
                usage=usage,
            )

        if "文件" in prompt or "总结" in prompt:
            if step == 0:
                content = f"# 处理结果\n\n原始请求：{prompt}\n\n这是 Agent Service 生成的文件。\n"
                return LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id=f"call_mock_{step}_write",
                            name="write_file",
                            arguments={"path": "/workspace/outputs/answer.md", "content": content},
                        )
                    ],
                    usage=usage,
                )
            if step == 1:
                return LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id=f"call_mock_{step}_deliver",
                            name="deliver_file",
                            arguments={"path": "/workspace/outputs/answer.md", "filename": "answer.md"},
                        )
                    ],
                    usage=usage,
                )
            return LLMResponse(content="文件已生成并交付。", usage=usage)

        if step == 0:
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id=f"call_mock_{step}_exec",
                        name="exec",
                        arguments={"cmd": "echo 'hello from ailab sandbox'; pwd; ls -la"},
                    )
                ],
                usage=usage,
            )
        return LLMResponse(content="沙箱命令已执行完成。", usage=usage)


@cache
def llm_for(model_id: str) -> LLMClient:
    """The client for a catalog model: its own upstream URL, key and upstream model name."""
    spec = get_catalog().get(model_id)
    if spec is None or not spec.available:
        raise LLMError(f"model {model_id!r} is not available")
    if spec.provider == "mock":
        return MockLLM()
    return OpenAICompatibleClient(spec.base_url, spec.api_key(), spec.upstream_model or spec.id)


def reset_llm_cache() -> None:
    llm_for.cache_clear()
