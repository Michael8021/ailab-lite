"""Request correlation and logging, shared by all four services.

* Every request carries an id: taken from an incoming ``X-Request-ID`` (if it is sane) or generated,
  returned in the response header, stamped on every log line, and forwarded on internal calls, so one
  user action can be followed from the Portal through the API, Broker and back.
* The Agent uses the run id as the id while it works on a run, so everything about one run is one search.
* ``AILAB_LOG_FORMAT=json`` switches the logs to one JSON object per line.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from contextvars import ContextVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import settings

NO_REQUEST = "-"
request_id_var: ContextVar[str] = ContextVar("request_id", default=NO_REQUEST)

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
REQUEST_ID_HEADER = "X-Request-ID"
QUIET_PATHS = {"/healthz", "/readyz", "/metrics"}  # probes and scrapes would drown the log


def clean_request_id(value: str | None) -> str:
    """The caller's id if it is short and plain, otherwise a fresh one (it ends up in logs and headers)."""
    return value if value and _SAFE_ID.match(value) else uuid.uuid4().hex[:16]


class _ContextFilter(logging.Filter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.service = self.service
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": getattr(record, "service", ""),
            "logger": record.name,
            "request_id": getattr(record, "request_id", NO_REQUEST),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


TEXT_FORMAT = "%(asctime)s %(levelname)s %(service)s %(name)s [%(request_id)s] %(message)s"


def setup_logging(service: str) -> None:
    """Configure the root logger once per process."""
    handler = logging.StreamHandler()
    handler.addFilter(_ContextFilter(service))
    handler.setFormatter(JsonFormatter() if settings.log_format == "json" else logging.Formatter(TEXT_FORMAT))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # We log requests ourselves (with the id and the route); uvicorn's access line would be a duplicate.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


class RequestContextMiddleware:
    """Pure ASGI (so streaming responses such as SSE are not buffered): id in/out, access log, HTTP metrics."""

    def __init__(self, app: ASGIApp, service: str):
        self.app = app
        self.service = service
        self.log = logging.getLogger(f"ailab.{service}.http")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from . import metrics  # late: metrics imports nothing from here, but keeps import order simple

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        rid = clean_request_id(headers.get(REQUEST_ID_HEADER.lower()))
        token = request_id_var.set(rid)
        started = time.perf_counter()
        status = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], (REQUEST_ID_HEADER.lower().encode(), rid.encode())]
                # Time to first byte: a response that streams for minutes (SSE) must not look like a slow request.
                route = scope.get("route")
                template = getattr(route, "path", None) or "unmatched"
                elapsed = time.perf_counter() - started
                metrics.observe_http(self.service, scope["method"], template, status, elapsed)
                if scope["path"] in QUIET_PATHS:
                    self.log.debug("%s %s -> %s %.0fms", scope["method"], scope["path"], status, elapsed * 1000)
                else:
                    self.log.info("%s %s -> %s %.0fms", scope["method"], scope["path"], status, elapsed * 1000)
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)
