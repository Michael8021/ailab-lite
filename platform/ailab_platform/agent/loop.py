"""The Agent Service loop: model -> tools -> sandbox -> artifact.

One run is one turn of a conversation (or a standalone task). The answer text streams to the user as it is
generated; a sandbox is only leased when the model first uses a tool that needs one, so a plain chat turn never
waits for a sandbox. In a conversation the sandbox workspace is restored at lease time and saved at the end,
and office files written to /workspace/outputs get rendered previews while the turn is still running.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable
from typing import TypeVar

from .. import metrics
from ..catalog import get_catalog
from ..clients import AsyncBrokerClient, AsyncPlatformClient, ClientError
from ..config import settings
from ..llm import INVALID_ARGUMENTS_KEY, LLMClient, llm_for
from ..models import RunStatus
from ..paths import OUTPUTS
from .prompts import history_messages, system_prompt_for, user_prompt
from .tools import SANDBOX_TOOLS, ToolError, execute_tool, tool_result_message, tools_for_turn

log = logging.getLogger("ailab.agent")

T = TypeVar("T")
CANCEL_POLL_SECONDS = 2.0
SANDBOX_RETRY_SECONDS = 3.0
DELTA_FLUSH_SECONDS = 0.25  # how often streamed text is written as an agent.delta event
DELTA_FLUSH_CHARS = 120
PREVIEW_AFTER = {"exec", "write_file"}  # tools that can change files in /workspace/outputs
THINKING_KEEP_CHARS = 20_000  # of the reasoning stored with the run (the events keep all of it)
WRAP_UP_STEPS = 3  # this many steps before the limit the model is told to finish up
WRAP_UP_NOTE = "（平台提示：本轮只剩 {n} 次工具调用。请尽快收尾：生成并交付文件，然后用文字回复用户。）"
OUT_OF_STEPS_NOTE = "（平台提示：本轮的步数已用完。不要再调用工具，直接用文字告诉用户已经完成了什么、还有什么没完成、下一轮可以怎么继续。）"


class RunCanceled(Exception):
    pass


class _Turn:
    """Mutable state of one run while the loop works on it."""

    def __init__(self, run: dict) -> None:
        self.run = run
        self.lease_id: str | None = None
        self.artifacts: list[dict] = []
        self.delivered: dict[str, int] = {}  # sandbox path -> size at delivery (or at restore)
        self.previews: dict[str, dict] = {}  # filename -> latest preview manifest
        self.preview_task: asyncio.Task | None = None
        self.preview_again = False
        self.thinking: list[str] = []  # the model's reasoning across the turn's steps, kept for the conversation view
        self.thinking_seconds = 0.0


class _DeltaWriter:
    """Batches streamed text into events (one per ~quarter second, not one per token): agent.delta for the answer,
    agent.thinking for the model's reasoning (with how long it has been thinking)."""

    def __init__(self, loop: AgentLoop, run_id: str, step: int, kind: str = "agent.delta") -> None:
        self.loop, self.run_id, self.step, self.kind = loop, run_id, step, kind
        self.buffer: list[str] = []
        self.last = time.monotonic()
        self.started: float | None = None
        self.seconds = 0.0

    async def __call__(self, text: str) -> None:
        if self.started is None:
            self.started = time.monotonic()
        self.buffer.append(text)
        if time.monotonic() - self.last >= DELTA_FLUSH_SECONDS or sum(map(len, self.buffer)) >= DELTA_FLUSH_CHARS:
            await self.flush()

    async def flush(self) -> None:
        if self.buffer:
            text, self.buffer = "".join(self.buffer), []
            payload = {"step": self.step, "text": text}
            if self.kind == "agent.thinking":
                payload["seconds"] = self.seconds = round(time.monotonic() - (self.started or time.monotonic()), 1)
            await self.loop._event(self.run_id, self.kind, payload)
        self.last = time.monotonic()


def _arguments_json(arguments: dict) -> str:
    """Arguments as sent back to the model: the original text if it was not valid JSON."""
    if INVALID_ARGUMENTS_KEY in arguments:
        return str(arguments[INVALID_ARGUMENTS_KEY] or "{}")
    return json.dumps(arguments, ensure_ascii=False)


def _clip_arguments(arguments: dict, limit: int = 500) -> dict:
    """Keep run_events small: write_file content can be tens of KB."""
    return {k: (v[:limit] + f"...[+{len(v) - limit} chars]" if isinstance(v, str) and len(v) > limit else v) for k, v in arguments.items()}


class AgentLoop:
    def __init__(
        self,
        platform: AsyncPlatformClient | None = None,
        broker: AsyncBrokerClient | None = None,
        llm: LLMClient | None = None,
    ):
        self.platform = platform or AsyncPlatformClient()
        self.broker = broker or AsyncBrokerClient()
        self.llm = llm  # tests inject one; otherwise each run uses its catalog model's client

    async def _event(self, run_id: str, type_: str, payload: dict) -> None:
        try:
            await self.platform.append_event(run_id, type_, payload)
        except Exception:
            log.warning("event append failed run=%s type=%s", run_id, type_, exc_info=True)

    async def _is_canceled(self, run_id: str) -> bool:
        """True if the user canceled. A failed check counts as "not canceled": one API
        hiccup must not fail a run whose task is fine."""
        try:
            # the light /state endpoint, not the full run with artifacts and usage: this runs every couple of seconds
            return (await self.platform.get_run_state(run_id))["status"] == RunStatus.CANCELED
        except Exception:
            log.warning("cancel check failed for %s; assuming not canceled", run_id, exc_info=True)
            return False

    async def _interruptible(self, run_id: str, awaitable: Awaitable[T]) -> T:
        """Await a model call or tool, abandoning it if the user cancels the run.

        The sandbox command itself keeps running until the lease is released, which
        the caller does right after on RunCanceled.
        """
        task = asyncio.ensure_future(awaitable)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=CANCEL_POLL_SECONDS)
                if done:
                    return task.result()
                if await self._is_canceled(run_id):
                    raise RunCanceled()
        finally:
            if not task.done():
                task.cancel()

    async def _acquire_lease(self, run_id: str) -> dict:
        """Take a sandbox, waiting in line (like the design's waiting_sandbox) when at capacity."""
        waited = 0.0
        announced = False
        while True:
            try:
                return await self.broker.create_lease(run_id)
            except ClientError as exc:
                if exc.status != 429:
                    raise
            if waited >= settings.sandbox_wait_seconds:
                raise RuntimeError(f"no sandbox became available within {settings.sandbox_wait_seconds}s")
            if not announced:
                announced = True
                await self._event(run_id, "agent.waiting_sandbox", {"reason": "sandbox capacity reached"})
            if await self._is_canceled(run_id):
                raise RunCanceled()
            await asyncio.sleep(SANDBOX_RETRY_SECONDS)
            waited += SANDBOX_RETRY_SECONDS

    async def _deliver_leftovers(self, run_id: str, lease_id: str, delivered: dict[str, int], artifacts: list[dict]) -> None:
        """Archive files left in /workspace/outputs that the model never delivered.

        A run that ends without calling deliver_file would otherwise lose its work
        when the sandbox is destroyed. Files delivered earlier are skipped unless
        they changed afterwards; the API de-duplicates identical content anyway.
        """
        try:
            files = await self.broker.list_files(lease_id, OUTPUTS)
        except Exception:
            log.warning("could not list leftovers for %s", run_id, exc_info=True)
            return
        pending = [f for f in files if f["path"].startswith(OUTPUTS + "/") and delivered.get(f["path"]) != f.get("size")]
        for item in pending[: settings.max_auto_deliver_files]:
            try:
                result = await self.broker.commit(lease_id, item["path"])
            except Exception as exc:
                await self._event(run_id, "agent.warning", {"message": f"auto-deliver failed for {item['path']}: {exc}"})
                continue
            delivered[item["path"]] = item.get("size", 0)
            artifacts.append({**result, "auto": True})
            await self._event(run_id, "agent.autodeliver", {"path": item["path"], "filename": result.get("filename")})
        if len(pending) > settings.max_auto_deliver_files:
            await self._event(
                run_id,
                "agent.warning",
                {"message": f"{len(pending) - settings.max_auto_deliver_files} more files in outputs were not delivered"},
            )

    async def _ensure_lease(self, turn: _Turn, run_id: str) -> str:
        if turn.lease_id:
            return turn.lease_id
        lease = await self._acquire_lease(run_id)
        turn.lease_id = lease["lease_id"]
        await self._event(run_id, "agent.sandbox", {"lease_id": turn.lease_id, "provider": lease.get("provider")})
        if lease.get("restored") is False and lease.get("restore_error"):
            await self._event(run_id, "agent.warning", {"message": f"上一轮的工作区没能恢复：{lease['restore_error']}"})
        # Files already in outputs (restored from the last turn) are not this turn's work: never auto-deliver them unchanged.
        try:
            for f in await self.broker.list_files(turn.lease_id, OUTPUTS):
                turn.delivered.setdefault(f["path"], f.get("size", 0))
        except Exception:
            log.warning("could not list outputs after leasing for %s", run_id, exc_info=True)
        return turn.lease_id

    # ---- live previews: rendered by the Broker in the background while the model keeps working
    def _schedule_previews(self, turn: _Turn, run_id: str) -> None:
        if not turn.lease_id:
            return
        if turn.preview_task and not turn.preview_task.done():
            turn.preview_again = True  # the running render may have missed this change
            return
        turn.preview_task = asyncio.create_task(self._render_previews(turn, run_id))

    async def _render_previews(self, turn: _Turn, run_id: str) -> None:
        while True:
            turn.preview_again = False
            try:
                manifests = await self.broker.refresh_previews(turn.lease_id)
            except Exception as exc:
                log.warning("preview refresh failed for %s", run_id, exc_info=True)
                await self._event(run_id, "preview.failed", {"error": f"{type(exc).__name__}: {exc}"[:300]})
                return
            for m in manifests:
                if m.get("error"):
                    await self._event(run_id, "preview.failed", m)
                else:
                    turn.previews[m["filename"]] = m
                    await self._event(run_id, "preview.updated", m)
            if not turn.preview_again:
                return

    async def _finish_previews(self, turn: _Turn, run_id: str) -> None:
        if turn.preview_task:
            await asyncio.gather(turn.preview_task, return_exceptions=True)
        if turn.lease_id:
            await self._render_previews(turn, run_id)  # the last change may have come after the last render

    async def _save_workspace(self, turn: _Turn, run_id: str) -> bool:
        if not (turn.lease_id and turn.run.get("conversation_id")):
            return False
        try:
            await self.broker.snapshot(turn.lease_id)
            return True
        except Exception as exc:
            log.warning("workspace snapshot failed for %s", run_id, exc_info=True)
            await self._event(run_id, "agent.warning", {"message": f"工作区没能保存，下一轮需要重新生成文件：{exc}"[:300]})
            return False

    async def _call_model(self, llm: LLMClient, run_id: str, run: dict, model: str, messages: list[dict], step: int, tools=None):
        model_label = metrics.model_label(model)
        call_started = time.perf_counter()
        writer = _DeltaWriter(self, run_id, step)
        thinker = _DeltaWriter(self, run_id, step, "agent.thinking")

        async def on_text(text: str) -> None:
            await thinker.flush()  # thinking is over once the answer starts
            await writer(text)

        try:
            response = await self._interruptible(
                run_id,
                llm.chat_stream(
                    messages,
                    tools_for_turn() if tools is None else tools,
                    model,
                    user_id=run.get("user_id"),
                    on_text=on_text,
                    on_reasoning=thinker,
                ),
            )
        except RunCanceled:
            metrics.LLM_CALLS.labels(model_label, "canceled").inc()
            await thinker.flush()
            await writer.flush()
            raise
        except Exception:
            metrics.LLM_CALLS.labels(model_label, "error").inc()
            metrics.LLM_SECONDS.labels(model_label).observe(time.perf_counter() - call_started)
            raise
        await thinker.flush()
        await writer.flush()
        response.thinking_seconds = thinker.seconds
        metrics.LLM_CALLS.labels(model_label, "ok").inc()
        metrics.LLM_SECONDS.labels(model_label).observe(time.perf_counter() - call_started)
        return response

    async def _run_tool(self, turn: _Turn, run_id: str, call, step: int) -> dict:
        await self._event(run_id, "agent.tool_call", {"step": step, "name": call.name, "arguments": _clip_arguments(call.arguments)})
        tool = metrics.tool_label(call.name)
        tool_started = time.perf_counter()
        try:
            lease_id = await self._ensure_lease(turn, run_id) if call.name in SANDBOX_TOOLS else (turn.lease_id or "")
            result = await self._interruptible(run_id, execute_tool(call.name, call.arguments, lease_id, self.broker))
        except RunCanceled:
            raise
        except ToolError as exc:
            result = {"ok": False, "error": str(exc)}
        except Exception as exc:  # broker / sandbox failures stay in the loop
            log.exception("tool %s failed", call.name)
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        metrics.TOOL_SECONDS.labels(tool).observe(time.perf_counter() - tool_started)
        metrics.TOOL_CALLS.labels(tool, str(bool(result.get("ok"))).lower()).inc()
        shown = result.get("skill")
        await self._event(
            run_id,
            "agent.tool_result",
            {
                "step": step,
                "name": call.name,
                "ok": bool(result.get("ok")),
                "exit_code": result.get("exit_code"),
                "stdout": (result.get("stdout") or (f"已读取技能说明（{len(shown)} 字）" if shown else ""))[:2000],
                "stderr": (result.get("stderr") or "")[:2000],
                "error": result.get("error"),
                **({"images": [i["path"] for i in result.get("images", [])]} if call.name == "search_images" else {}),
            },
        )
        if call.name in PREVIEW_AFTER:
            self._schedule_previews(turn, run_id)
        if call.name == "deliver_file" and result.get("ok"):
            turn.artifacts.append(result)
            path = call.arguments.get("path")
            if isinstance(path, str):
                turn.delivered[path] = result.get("size", 0)
        return result

    async def process(self, run_id: str, attempt: int = 1) -> None:
        run = await self.platform.get_run(run_id)
        if run["status"] in RunStatus.TERMINAL:
            return

        await self.platform.set_run_status(run_id, RunStatus.RUNNING)
        await self._event(run_id, "agent.started", {"model": run.get("model"), "steps": 0})
        if attempt > 1:
            metrics.AGENT_RECOVERED.inc()
            await self._event(
                run_id,
                "agent.recovered",
                {"attempt": attempt, "note": "previous agent attempt did not finish; restarting the task"},
            )

        run_started = time.perf_counter()
        outcome = "error"  # replaced below once we know how the run ended
        turn = _Turn(run)
        final_summary = ""
        total_tokens = 0
        finished = False
        error: str | None = None
        try:
            model = run.get("model") or ""
            llm = self.llm or llm_for(model)  # fails the run early if the model is gone from the catalog
            options = run.get("options") or {}
            mode, skill = options.get("mode", "work"), options.get("skill")
            if skill:
                await self._event(run_id, "agent.skill", {"name": skill})
            attached = [f for f in run.get("input_files") or [] if f.get("text")]
            if attached:
                files = [{"filename": f["filename"], "chars": f.get("chars"), "truncated": f.get("truncated")} for f in attached]
                await self._event(run_id, "agent.attachments", {"files": files})
            messages: list[dict] = [
                {"role": "system", "content": system_prompt_for(mode, skill)},
                *history_messages(run.get("history")),
                {"role": "user", "content": user_prompt(run["prompt"], run.get("input_files"))},
            ]

            async def record(response) -> None:
                nonlocal total_tokens
                if not response.usage:
                    return
                total_tokens += response.usage.get("prompt_tokens", 0) + response.usage.get("completion_tokens", 0)
                try:
                    await self.platform.record_usage(
                        run_id,
                        model,
                        response.usage.get("prompt_tokens", 0),
                        response.usage.get("completion_tokens", 0),
                        response.usage.get("cached_tokens", 0),
                    )
                except Exception:
                    log.warning("usage record failed run=%s", run_id, exc_info=True)

            out_of_steps = False
            spec = get_catalog().get(model)
            # chat mode, or a model without tool calling: words only, so never a sandbox
            offered = [] if mode == "chat" or (spec is not None and not spec.tools) else None
            for step in range(settings.max_agent_steps):
                if await self._is_canceled(run_id):
                    raise RunCanceled()
                left = settings.max_agent_steps - step
                if left == WRAP_UP_STEPS and settings.max_agent_steps > 2 * WRAP_UP_STEPS:
                    messages.append({"role": "user", "content": WRAP_UP_NOTE.format(n=left)})

                response = await self._call_model(llm, run_id, run, model, messages, step, tools=offered)
                await record(response)
                if response.reasoning_content:
                    turn.thinking.append(response.reasoning_content)
                    turn.thinking_seconds += response.thinking_seconds

                if response.content:
                    final_summary = response.content
                    await self._event(run_id, "agent.message", {"step": step, "content": response.content})

                if not response.tool_calls:
                    finished = True
                    break

                assistant: dict = {
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": _arguments_json(call.arguments)},
                        }
                        for call in response.tool_calls
                    ],
                }
                if response.reasoning_content:  # the client decides whether the upstream gets it back
                    assistant["reasoning_content"] = response.reasoning_content
                messages.append(assistant)

                for call in response.tool_calls:
                    result = await self._run_tool(turn, run_id, call, step)
                    messages.append(tool_result_message(call.id, call.name, result))
                    if call.name == "finish" and result.get("ok"):
                        final_summary = result.get("summary") or final_summary
                        finished = True

                if finished:
                    break
                if total_tokens > settings.run_max_tokens:
                    error = f"token limit reached ({total_tokens} > {settings.run_max_tokens})"
                    break

            if not finished and error is None:
                # Out of steps: rather than ending on a bare error, let the model tell the user where things stand.
                out_of_steps = True
                messages.append({"role": "user", "content": OUT_OF_STEPS_NOTE})
                response = await self._call_model(llm, run_id, run, model, messages, settings.max_agent_steps, tools=[])
                await record(response)
                if response.content:
                    final_summary = response.content
                    await self._event(run_id, "agent.message", {"step": settings.max_agent_steps, "content": response.content})
                await self._event(run_id, "agent.warning", {"message": f"本轮用完了 {settings.max_agent_steps} 步"})

            result: dict = {}
            if turn.lease_id:
                # Whatever the outcome, don't lose finished work with the sandbox.
                await self._finish_previews(turn, run_id)
                await self._deliver_leftovers(run_id, turn.lease_id, turn.delivered, turn.artifacts)
                result["workspace_saved"] = await self._save_workspace(turn, run_id)
            result.update(artifacts=turn.artifacts, previews=list(turn.previews.values()))
            if turn.thinking:
                text = "\n\n".join(turn.thinking)
                result["thinking"] = {"text": text[-THINKING_KEEP_CHARS:], "seconds": round(turn.thinking_seconds, 1)}
            if out_of_steps and not turn.artifacts:
                error = f"agent exceeded {settings.max_agent_steps} steps"  # nothing to show for it: a failure

            if error is None:
                await self.platform.set_run_status(run_id, RunStatus.SUCCEEDED, summary=final_summary or "完成", result=result)
                outcome = "succeeded"
            else:
                await self.platform.set_run_status(run_id, RunStatus.FAILED, error=error, summary=final_summary, result=result)
                outcome = "failed"
        except RunCanceled:
            outcome = "canceled"
            await self._event(run_id, "agent.canceled", {})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("agent run %s failed", run_id)
            await self._event(run_id, "agent.error", {"error": f"{type(exc).__name__}: {exc}"})
            try:
                await self.platform.set_run_status(run_id, RunStatus.FAILED, error=f"{type(exc).__name__}: {exc}"[:2000])
            except Exception:
                log.exception("could not mark run %s failed", run_id)
        finally:
            metrics.AGENT_RUN_SECONDS.labels(outcome).observe(time.perf_counter() - run_started)
            if turn.preview_task and not turn.preview_task.done():
                turn.preview_task.cancel()
            if turn.lease_id:
                try:
                    await self.broker.release(turn.lease_id)
                except Exception:
                    log.warning("lease release failed %s", turn.lease_id, exc_info=True)
