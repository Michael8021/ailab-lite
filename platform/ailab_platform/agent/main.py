"""Agent Service entrypoint: one Redis consumer, one sandbox at a time."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Response

from ..catalog import get_catalog
from ..clients import AsyncPlatformClient
from ..config import settings
from ..deps import metrics_auth
from ..metrics import metrics_response
from ..models import RunStatus
from ..obs import NO_REQUEST, RequestContextMiddleware, request_id_var, setup_logging
from ..queue import Message, get_queue, run_lock_key
from .loop import AgentLoop
from .prompts import system_prompt

setup_logging("agent")
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
    platform = AsyncPlatformClient()  # holds a connection pool now: close it with the message
    try:
        await _handle_message(loop, queue, msg, platform)
    finally:
        await platform.aclose()


async def _handle_message(loop: AgentLoop, queue, msg: Message, platform) -> None:
    run_id = msg.payload.get("run_id")
    if not run_id:
        log.warning("dropping malformed run message: %s", msg.payload)
        await asyncio.to_thread(queue.ack, settings.run_queue, msg)
        return

    # A message re-queued at shutdown starts a fresh delivery count, so it carries its own.
    attempt = max(msg.deliveries, int(msg.payload.get("attempt", 1)))
    if attempt > settings.run_max_attempts:
        log.error("run %s attempted %d times; giving up", run_id, attempt)
        try:
            await platform.set_run_status(run_id, RunStatus.FAILED, error=f"agent crashed or hung {attempt - 1} times; giving up")
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
                await platform.set_run_status(run_id, RunStatus.FAILED, error=f"run timeout after {settings.run_timeout_seconds}s")
            except Exception:
                log.exception("could not mark timed-out run %s failed", run_id)
        except asyncio.CancelledError:
            # Shutting down (rolling deploy, SIGTERM). Waiting for the visibility timeout would
            # leave the run stalled for ~16 minutes, so hand it to the next agent right away.
            try:
                await asyncio.to_thread(queue.enqueue, settings.run_queue, {"run_id": run_id, "attempt": attempt + 1})
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
        rid = request_id_var.set(str(msg.payload.get("run_id") or NO_REQUEST))  # every log line about this run carries its id
        try:
            await handle_message(loop, queue, msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            # e.g. the queue went away between process and ack: the message will
            # be redelivered after the visibility timeout and the run lock guards it.
            log.exception("could not finish handling run message %s", msg.id)
        finally:
            request_id_var.reset(rid)


def begin_drain(loop: asyncio.AbstractEventLoop) -> None:
    """Stop taking runs and hand the current one back, now - not when the web server gets round to shutting down.

    On SIGTERM uvicorn first waits for open connections (up to the pod's grace period) and only then runs the
    lifespan shutdown. Until then the consumer would keep taking new runs, and the pod is killed in the middle of
    one, which then sits with a dead consumer until the visibility timeout (~16 minutes).
    """
    if _task and not _task.done():
        log.info("SIGTERM: handing the current run back and taking no new ones")
        loop.call_soon_threadsafe(_task.cancel)


def _chain_sigterm(loop: asyncio.AbstractEventLoop) -> None:
    """Run begin_drain on SIGTERM, then whatever handler was there (uvicorn's, which starts its own shutdown)."""
    previous = signal.getsignal(signal.SIGTERM)

    def handler(signum, frame) -> None:
        begin_drain(loop)
        if callable(previous):
            previous(signum, frame)

    try:
        signal.signal(signal.SIGTERM, handler)
    except ValueError:  # not the main thread (e.g. a test client): nothing to chain
        pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _task
    settings.validate_secrets()
    settings.ensure_dirs()
    system_prompt()  # raises if the skill file is missing
    _task = asyncio.create_task(consume_forever(), name="agent-consumer")
    _chain_sigterm(asyncio.get_running_loop())
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
app.add_middleware(RequestContextMiddleware, service="agent")


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


@app.get("/metrics", include_in_schema=False, dependencies=[Depends(metrics_auth)])
def metrics_endpoint() -> Response:
    return metrics_response()
