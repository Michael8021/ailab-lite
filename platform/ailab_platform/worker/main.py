"""File processing Worker: parse, preview, scan; no OCR in the lite profile."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Response
from fastapi.responses import JSONResponse

from .. import metrics
from ..clients import PlatformClient
from ..config import settings
from ..deps import metrics_auth
from ..metrics import metrics_response
from ..obs import NO_REQUEST, RequestContextMiddleware, request_id_var, setup_logging
from ..queue import get_queue
from .pipeline import process_file

setup_logging("worker")
log = logging.getLogger("ailab.worker")
_thread: threading.Thread | None = None
_stop = threading.Event()


CONSUMER = f"worker-{socket.gethostname()}-{os.getpid()}"
FILE_VISIBILITY_SECONDS = 600


def handle_message(queue, msg) -> None:
    file_id = msg.payload.get("file_id")
    rid = request_id_var.set(str(file_id or NO_REQUEST))  # log lines about this file carry its id
    try:
        _handle(queue, msg, file_id)
    finally:
        request_id_var.reset(rid)


def _patch(file_id: str, **kw) -> None:
    client = PlatformClient()
    try:
        client.patch_file(file_id, **kw)
    finally:
        client.close()


def _handle(queue, msg, file_id) -> None:
    if file_id and msg.deliveries > settings.file_max_attempts:
        log.error("file %s could not be scanned after %d attempts; rejecting it", file_id, msg.deliveries - 1)
        try:
            _patch(
                file_id,
                meta={"processing": "error", "scan": {"ok": False, "reason": "scan failed repeatedly"}},
                status="rejected",
            )
        except Exception:
            log.warning("could not reject file %s", file_id, exc_info=True)
            return  # leave the message unacked and try again later
        metrics.FILES.labels("rejected").inc()
        queue.ack(settings.file_queue, msg)
        return
    if file_id:
        started = time.perf_counter()
        try:
            result = process_file(file_id)
            log.info("processed file %s: %s", file_id, result)
            outcome = "rejected" if "rejected" in result else "skipped" if "skipped" in result else "ready"
            metrics.FILES.labels(outcome).inc()
            metrics.FILE_SECONDS.observe(time.perf_counter() - started)
        except Exception:
            # Stay quarantined (pending) and un-acked: redelivered after the visibility timeout.
            log.exception("file %s processing failed", file_id)
            metrics.FILES.labels("error").inc()
            try:
                _patch(file_id, meta={"processing": "error"})
            except Exception:
                log.warning("could not flag file %s as errored", file_id, exc_info=True)
            return
    queue.ack(settings.file_queue, msg)


def _consume() -> None:
    while not _stop.is_set():
        queue = get_queue()
        try:
            msg = queue.dequeue(settings.file_queue, CONSUMER, 2, FILE_VISIBILITY_SECONDS)
        except Exception:
            log.exception("file queue unavailable; retrying")
            _stop.wait(3)
            continue
        if msg is None:
            continue
        try:
            handle_message(queue, msg)
        except Exception:
            log.exception("could not finish handling file message %s", msg.id)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _thread
    settings.validate_secrets()
    settings.ensure_dirs()
    _stop.clear()
    _thread = threading.Thread(target=_consume, name="file-worker", daemon=True)
    _thread.start()
    log.info("file worker ready")
    yield
    _stop.set()


app = FastAPI(title="AILab File Worker", version="0.1.0", lifespan=lifespan)
app.add_middleware(RequestContextMiddleware, service="worker")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "service": "file-worker"}


@app.get("/readyz")
def readyz() -> Response:
    try:
        queue_ok = get_queue().ping()
    except Exception:
        queue_ok = False
    running = bool(_thread and _thread.is_alive())
    ok = queue_ok and running
    return JSONResponse(status_code=200 if ok else 503, content={"ok": ok, "queue": queue_ok, "worker_running": running})


@app.get("/metrics", include_in_schema=False, dependencies=[Depends(metrics_auth)])
def metrics_endpoint() -> Response:
    return metrics_response()
