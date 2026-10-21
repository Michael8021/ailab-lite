"""The Agent Service loop: model -> tools -> sandbox -> artifact."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable
from typing import TypeVar

from .. import metrics
from ..clients import AsyncBrokerClient, AsyncPlatformClient, ClientError
from ..config import settings
from ..llm import INVALID_ARGUMENTS_KEY, LLMClient, llm_for
from ..models import RunStatus
from ..paths import OUTPUTS
from .prompts import system_prompt, user_prompt
from .tools import TOOLS, ToolError, execute_tool, tool_result_message

log = logging.getLogger("ailab.agent")

T = TypeVar("T")
CANCEL_POLL_SECONDS = 2.0
SANDBOX_RETRY_SECONDS = 3.0


class RunCanceled(Exception):
    pass


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
            return (await self.platform.get_run(run_id))["status"] == RunStatus.CANCELED
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

    async def process(self, run_id: str, attempt: int = 1) -> None:
        run = await self.platform.get_run(run_id)
        if run["status"] in RunStatus.TERMINAL:
            return

        await self.platform.set_run_status(run_id, RunStatus.RUNNING)
        await self._event(run_id, "agent.started", {"model": run.get("model"), "steps": 0})
        if attempt > 1:
            await self._event(
                run_id,
                "agent.recovered",
                {"attempt": attempt, "note": "previous agent attempt did not finish; restarting the task"},
            )

        run_started = time.perf_counter()
        outcome = "error"  # replaced below once we know how the run ended
        if attempt > 1:
            metrics.AGENT_RECOVERED.inc()
        lease_id: str | None = None
        final_summary = ""
        artifacts: list[dict] = []
        delivered: dict[str, int] = {}  # sandbox path -> size at delivery
        total_tokens = 0
        finished = False
        error: str | None = None
        try:
            model = run.get("model") or ""
            llm = self.llm or llm_for(model)  # fails the run early if the model is gone from the catalog
            lease = await self._acquire_lease(run_id)
            lease_id = lease["lease_id"]
            await self._event(run_id, "agent.sandbox", {"lease_id": lease_id, "provider": lease.get("provider")})

            messages: list[dict] = [
                {"role": "system", "content": system_prompt()},
                {"role": "user", "content": user_prompt(run["prompt"], lease.get("inputs"))},
            ]

            for step in range(settings.max_agent_steps):
                if await self._is_canceled(run_id):
                    raise RunCanceled()

                model_label = metrics.model_label(model)
                call_started = time.perf_counter()
                try:
                    response = await self._interruptible(run_id, llm.chat(messages, TOOLS, model, user_id=run.get("user_id")))
                except RunCanceled:
                    metrics.LLM_CALLS.labels(model_label, "canceled").inc()
                    raise
                except Exception:
                    metrics.LLM_CALLS.labels(model_label, "error").inc()
                    metrics.LLM_SECONDS.labels(model_label).observe(time.perf_counter() - call_started)
                    raise
                metrics.LLM_CALLS.labels(model_label, "ok").inc()
                metrics.LLM_SECONDS.labels(model_label).observe(time.perf_counter() - call_started)
                if response.usage:
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
                if settings.llm_echo_reasoning and response.reasoning_content:
                    assistant["reasoning_content"] = response.reasoning_content
                messages.append(assistant)

                for call in response.tool_calls:
                    await self._event(
                        run_id,
                        "agent.tool_call",
                        {"step": step, "name": call.name, "arguments": _clip_arguments(call.arguments)},
                    )
                    tool = metrics.tool_label(call.name)
                    tool_started = time.perf_counter()
                    try:
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
                    await self._event(
                        run_id,
                        "agent.tool_result",
                        {
                            "step": step,
                            "name": call.name,
                            "ok": bool(result.get("ok")),
                            "exit_code": result.get("exit_code"),
                            "stdout": (result.get("stdout") or "")[:2000],
                            "stderr": (result.get("stderr") or "")[:2000],
                            "error": result.get("error"),
                        },
                    )
                    messages.append(tool_result_message(call.id, call.name, result))

                    if call.name == "deliver_file" and result.get("ok"):
                        artifacts.append(result)
                        path = call.arguments.get("path")
                        if isinstance(path, str):
                            delivered[path] = result.get("size", 0)
                    if call.name == "finish" and result.get("ok"):
                        final_summary = result.get("summary") or final_summary
                        finished = True

                if finished:
                    break
                if total_tokens > settings.run_max_tokens:
                    error = f"token limit reached ({total_tokens} > {settings.run_max_tokens})"
                    break

            if not finished and error is None:
                error = f"agent exceeded {settings.max_agent_steps} steps"

            # Whatever the outcome, don't lose finished work with the sandbox.
            await self._deliver_leftovers(run_id, lease_id, delivered, artifacts)

            if error is None:
                await self.platform.set_run_status(
                    run_id,
                    RunStatus.SUCCEEDED,
                    summary=final_summary or "任务完成",
                    result={"artifacts": artifacts},
                )
                outcome = "succeeded"
            else:
                await self.platform.set_run_status(
                    run_id,
                    RunStatus.FAILED,
                    error=error,
                    summary=final_summary,
                    result={"artifacts": artifacts},
                )
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
            if lease_id:
                try:
                    await self.broker.release(lease_id)
                except Exception:
                    log.warning("lease release failed %s", lease_id, exc_info=True)
