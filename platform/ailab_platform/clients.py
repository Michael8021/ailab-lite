"""HTTP clients used between platform services.

Services never share a database session or a filesystem handle: Platform API is
the only writer, and the Broker is the only sandbox owner. These clients make
that boundary explicit.
"""

from __future__ import annotations

from typing import Any, NamedTuple

import httpx

from .config import settings
from .obs import NO_REQUEST, REQUEST_ID_HEADER, request_id_var


class ClientError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: str):
        super().__init__(f"{method} {url} -> {status}: {body[:500]}")
        self.status = status
        self.body = body


def _raise(method: str, url: str, resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        raise ClientError(method, url, resp.status_code, resp.text)


class Request(NamedTuple):
    method: str
    url: str
    timeout: float
    kwargs: dict


class _Service:
    """Describes each call once; the sync and async clients only differ in how they send it."""

    default_base_url: str = ""

    def __init__(self, base_url: str | None = None, token: str | None = None):
        self.base_url = (base_url or self.default_base_url).rstrip("/")
        self.token = token or settings.service_token

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Service-Token": self.token}

    def _req(self, method: str, path: str, timeout: float = 30, **kwargs) -> Request:
        headers = dict(self.headers)
        rid = request_id_var.get()
        if rid != NO_REQUEST:  # carry the correlation id into the next service's logs
            headers[REQUEST_ID_HEADER] = rid
        return Request(method, f"{self.base_url}{path}", timeout, {"headers": headers, **kwargs})


class _PlatformApi(_Service):
    @property
    def default_base_url(self) -> str:  # type: ignore[override]
        return settings.api_base_url

    def _get_run(self, run_id: str) -> Request:
        return self._req("GET", f"/internal/runs/{run_id}")

    def _get_run_state(self, run_id: str) -> Request:
        return self._req("GET", f"/internal/runs/{run_id}/state", timeout=10)

    def _set_run_status(self, run_id, status, summary=None, error=None, result=None) -> Request:
        body = {"status": status, "summary": summary, "error": error, "result": result}
        return self._req("POST", f"/internal/runs/{run_id}/status", json=body)

    def _append_event(self, run_id, type_, payload=None) -> Request:
        return self._req("POST", f"/internal/runs/{run_id}/events", json={"type": type_, "payload": payload or {}})

    def _record_usage(self, run_id, model, prompt_tokens, completion_tokens, cached_tokens=0) -> Request:
        body = {
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cached_tokens": cached_tokens,
        }
        return self._req("POST", f"/internal/runs/{run_id}/usage", json=body)

    def _commit_artifact(self, payload) -> Request:
        return self._req("POST", "/internal/artifacts", timeout=60, json=payload)

    def _get_file(self, file_id) -> Request:
        return self._req("GET", f"/internal/files/{file_id}")

    def _get_file_bytes(self, file_id) -> Request:
        return self._req("GET", f"/internal/files/{file_id}/content", timeout=120)

    def _patch_file(self, file_id, meta, status) -> Request:
        return self._req("PATCH", f"/internal/files/{file_id}", json={"meta": meta, "status": status})


class PlatformClient(_PlatformApi):
    """Synchronous internal-API client (Broker / Worker)."""

    def _send(self, req: Request) -> httpx.Response:
        with httpx.Client(timeout=req.timeout) as c:
            r = c.request(req.method, req.url, **req.kwargs)
        _raise(req.method, req.url, r)
        return r

    def get_run(self, run_id: str) -> dict:
        return self._send(self._get_run(run_id)).json()

    def set_run_status(self, run_id, status, summary=None, error=None, result=None) -> dict:
        return self._send(self._set_run_status(run_id, status, summary, error, result)).json()

    def append_event(self, run_id: str, type_: str, payload: dict | None = None) -> dict:
        return self._send(self._append_event(run_id, type_, payload)).json()

    def record_usage(self, run_id: str, model: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> None:
        self._send(self._record_usage(run_id, model, prompt_tokens, completion_tokens, cached_tokens))

    def commit_artifact(self, payload: dict) -> dict:
        return self._send(self._commit_artifact(payload)).json()

    def get_file(self, file_id: str) -> dict:
        return self._send(self._get_file(file_id)).json()

    def get_file_bytes(self, file_id: str) -> bytes:
        return self._send(self._get_file_bytes(file_id)).content

    def patch_file(self, file_id: str, meta: dict | None = None, status: str | None = None) -> dict:
        return self._send(self._patch_file(file_id, meta, status)).json()


class AsyncPlatformClient(_PlatformApi):
    """Asynchronous internal-API client (Agent)."""

    async def _send(self, req: Request) -> httpx.Response:
        async with httpx.AsyncClient(timeout=req.timeout) as c:
            r = await c.request(req.method, req.url, **req.kwargs)
        _raise(req.method, req.url, r)
        return r

    async def get_run(self, run_id: str) -> dict:
        return (await self._send(self._get_run(run_id))).json()

    async def get_run_state(self, run_id: str) -> dict:
        return (await self._send(self._get_run_state(run_id))).json()

    async def set_run_status(self, run_id, status, summary=None, error=None, result=None) -> dict:
        return (await self._send(self._set_run_status(run_id, status, summary, error, result))).json()

    async def append_event(self, run_id: str, type_: str, payload: dict | None = None) -> dict:
        return (await self._send(self._append_event(run_id, type_, payload))).json()

    async def record_usage(self, run_id: str, model: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> None:
        await self._send(self._record_usage(run_id, model, prompt_tokens, completion_tokens, cached_tokens))


class AsyncBrokerClient(_Service):
    """Agent -> Broker: acquire a lease, run commands, move bytes, commit."""

    @property
    def default_base_url(self) -> str:  # type: ignore[override]
        return settings.broker_base_url

    async def _send(self, req: Request) -> httpx.Response:
        async with httpx.AsyncClient(timeout=req.timeout) as c:
            r = await c.request(req.method, req.url, **req.kwargs)
        _raise(req.method, req.url, r)
        return r

    async def create_lease(self, run_id: str, ttl_seconds: int | None = None, image: str | None = None) -> dict:
        # The Broker learns the run's input files from the API, not from us.
        body = {"run_id": run_id, "image": image, "ttl_seconds": ttl_seconds}
        return (await self._send(self._req("POST", "/internal/leases", timeout=180, json=body))).json()

    async def exec(self, lease_id: str, cmd: str, timeout_seconds: int | None = None) -> dict:
        req = self._req(
            "POST",
            f"/internal/leases/{lease_id}/exec",
            timeout=(timeout_seconds or 120) + 30,
            json={"cmd": cmd, "timeout_seconds": timeout_seconds},
        )
        return (await self._send(req)).json()

    async def put_file(self, lease_id: str, path: str, data: bytes) -> None:
        await self._send(self._req("PUT", f"/internal/leases/{lease_id}/files", timeout=120, params={"path": path}, content=data))

    async def get_file(self, lease_id: str, path: str) -> bytes:
        return (await self._send(self._req("GET", f"/internal/leases/{lease_id}/files", timeout=120, params={"path": path}))).content

    async def list_files(self, lease_id: str, path: str) -> list[dict]:
        r = await self._send(self._req("GET", f"/internal/leases/{lease_id}/files/list", timeout=60, params={"path": path}))
        return r.json().get("files", [])

    async def commit(self, lease_id: str, path: str, filename: str | None = None, role: str = "output") -> dict:
        body = {"path": path, "filename": filename, "role": role}
        return (await self._send(self._req("POST", f"/internal/leases/{lease_id}/commit", timeout=180, json=body))).json()

    async def refresh_previews(self, lease_id: str) -> list[dict]:
        r = await self._send(self._req("POST", f"/internal/leases/{lease_id}/previews", timeout=660))
        return r.json().get("previews", [])

    async def snapshot(self, lease_id: str) -> dict:
        return (await self._send(self._req("POST", f"/internal/leases/{lease_id}/snapshot", timeout=300))).json()

    async def release(self, lease_id: str) -> None:
        req = self._req("DELETE", f"/internal/leases/{lease_id}", timeout=60)
        async with httpx.AsyncClient(timeout=req.timeout) as c:
            r = await c.request(req.method, req.url, **req.kwargs)
        if r.status_code not in (200, 204, 404):
            _raise(req.method, req.url, r)


def run_summary(run: dict) -> str:
    return str(run.get("summary") or "")


def run_artifacts(run: dict) -> list[dict[str, Any]]:
    return list(run.get("artifacts") or [])
