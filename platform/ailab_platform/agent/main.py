"""Agent Service entrypoint: one Redis consumer, one sandbox at a time."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ..catalog import get_catalog
from ..clients import AsyncPlatformClient
from ..config import settings
from ..models import RunStatus
from ..queue import Message, get_queue, run_lock_key
from .loop import AgentLoop
from .prompts import system_prompt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("ailab.agent")
_task: asyncio.Task | None = None


CONSUMER = f"agent-{socket.gethostname()}-{os.getpid()}"


def visibility_seconds() -> int:
    """How long a run message may stay unacked before another consumer takes it over.

    Longer than the hard run timeout, so an unacked message always means the
    consumer died rather than that the run is merely slow.
    """
    return settings.run_timeout_seconds + 60


async def handle_message(loop: AgentLoop, queue, msg: Message) -> None:
    """Process one run message. On shutdown (CancelledError) the run is handed back to the queue."""
    run_id = msg.payload.get("run_id")
    if not run_id:
        log.warning("dropping malformed run message: %s", msg.payload)
        await asyncio.to_thread(queue.ack, settings.run_queue, msg)
        return

    # A message re-queued at shutdown starts a fresh delivery count, so it carries its own.
    attempt = max(msg.deliveries, int(msg.payload.get("attempt", 1)))
    platform = AsyncPlatformClient()
    if attempt > settings.run_max_attempts:
        log.error("run %s attempted %d times; giving up", run_id, attempt)
        try:
            await platform.set_run_status(
                run_id, RunStatus.FAILED, error=f"agent crashed or hung {attempt - 1} times; giving up"
            )
        except Exception:
            log.exception("could not mark run %s failed", run_id)
        await asyncio.to_thread(queue.ack, settings.run_queue, msg)
        return

    lock = run_lock_key(run_id)
    token = await asyncio.to_thread(queue.acquire_lock, lock, settings.run_timeout_seconds + 30)
    if token is None:
        log.warning("run %s is already being executed elsewhere; dropping duplicate message", run_id)
        await asyncio.to_thread(queue.ack, settings.run_queue, msg)
        return

    log.info("picked run %s (attempt %d)", run_id, attempt)
    try:
        try:
            await asyncio.wait_for(loop.process(run_id, attempt=attempt), timeout=settings.run_timeout_seconds)
        except TimeoutError:
            log.error("run %s hit the %ss timeout", run_id, settings.run_timeout_seconds)
            try:
                await platform.set_run_status(
                    run_id, RunStatus.FAILED, error=f"run timeout after {settings.run_timeout_seconds}s"
                )
            except Exception:
                log.exception("could not mark timed-out run %s failed", run_id)
        except asyncio.CancelledError:
            # Shutting down (rolling deploy, SIGTERM). Waiting for the visibility timeout would
            # leave the run stalled for ~16 minutes, so hand it to the next agent right away.
            try:
                await asyncio.to_thread(
                    queue.enqueue, settings.run_queue, {"run_id": run_id, "attempt": attempt + 1}
                )
                await asyncio.to_thread(queue.ack, settings.run_queue, msg)
                log.info("run %s handed back to the queue for another agent", run_id)
            except Exception:
                log.exception("could not hand run %s back; it will be redelivered after the visibility timeout", run_id)
            raise
        except Exception:
            log.exception("run %s crashed", run_id)
            try:
                await platform.set_run_status(run_id, RunStatus.FAILED, error="agent crashed unexpectedly")
            except Exception:
                log.exception("could not mark crashed run %s failed", run_id)
        await asyncio.to_thread(queue.ack, settings.run_queue, msg)
    finally:
        await asyncio.to_thread(queue.release_lock, lock, token)


async def consume_forever() -> None:
    loop = AgentLoop()
    while True:
        queue = get_queue()
        try:
            msg = await asyncio.to_thread(queue.dequeue, settings.run_queue, CONSUMER, 2, visibility_seconds())
        except Exception:
            log.exception("run queue unavailable; retrying")
            await asyncio.sleep(3)
            continue
        if msg is None:
            continue
        try:
            await handle_message(loop, queue, msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            # e.g. the queue went away between process and ack: the message will
            # be redelivered after the visibility timeout and the run lock guards it.
            log.exception("could not finish handling run message %s", msg.id)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _task
    settings.validate_secrets()
    settings.ensure_dirs()
    system_prompt()  # raises if the skill file is missing
    _task = asyncio.create_task(consume_forever(), name="agent-consumer")
    catalog = get_catalog()  # logs which models are hidden for want of a key
    log.info("agent service ready sandbox=%s models=%s", settings.sandbox_provider, [m.id for m in catalog.available()])
    yield
    if _task:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(title="AILab Agent Service", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "service": "agent", "models": [m.id for m in get_catalog().available()]}


@app.get("/readyz")
def readyz() -> dict:
    try:
        queue_ok = get_queue().ping()
    except Exception:
        queue_ok = False
    return {"ok": queue_ok, "queue": queue_ok, "consumer_running": bool(_task and not _task.done())}
