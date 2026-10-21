"""OpenSandbox provider (T2 dev server, Docker runtime).

The Broker API stays the same; switching ``AILAB_SANDBOX_PROVIDER=opensandbox``
routes lifecycle/exec/file operations through the OpenSandbox server instead of
talking to Docker directly. On k8s this becomes the controller + ingress-gateway
path, and the per-sandbox token replaces this server-side API key.
"""

from __future__ import annotations

import shlex
import threading
from datetime import timedelta

import httpx

from ..config import settings
from ..paths import PathError, normalize_path
from .base import SANDBOX_EXPIRY_GRACE_SECONDS, ExecResult, SandboxError, SandboxProvider


def _checked(path: str) -> str:
    try:
        return normalize_path(path)
    except PathError as exc:
        raise SandboxError(str(exc)) from exc


def _join(messages) -> str:
    return "\n".join((m.text or "") for m in messages).strip("\n")


class OpenSandboxProvider(SandboxProvider):
    name = "opensandbox"

    def __init__(self) -> None:
        try:
            from opensandbox import SandboxSync
            from opensandbox.config import ConnectionConfigSync
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise SandboxError(
                "opensandbox SDK is not installed; run `pip install opensandbox==1.1.1` or use AILAB_SANDBOX_PROVIDER=docker"
            ) from exc

        self._SandboxSync = SandboxSync
        self._config = ConnectionConfigSync(
            domain=settings.osb_domain,
            protocol=settings.osb_protocol,
            api_key=settings.osb_api_key,
            request_timeout=timedelta(seconds=settings.osb_request_timeout_seconds),
        )
        self._cache: dict[str, object] = {}
        self._lock = threading.Lock()

    def _remember(self, sandbox) -> str:
        with self._lock:
            self._cache[sandbox.id] = sandbox
        return sandbox.id

    def _sandbox(self, sandbox_id: str):
        with self._lock:
            cached = self._cache.get(sandbox_id)
        if cached is not None:
            return cached
        try:
            sandbox = self._SandboxSync.connect(sandbox_id, connection_config=self._config)
        except Exception as exc:
            raise SandboxError(f"could not connect to sandbox {sandbox_id}: {exc}") from exc
        with self._lock:
            self._cache[sandbox_id] = sandbox
        return sandbox

    def acquire(self, lease_id: str, image: str, ttl_seconds: int | None = None) -> str:
        from opensandbox.models import NetworkPolicy

        # OpenSandbox's timeout is an absolute expiry (extended only via renew()),
        # not an idle timeout, so it must cover the whole lease.
        lifetime = (ttl_seconds or settings.sandbox_ttl_seconds) + SANDBOX_EXPIRY_GRACE_SECONDS

        try:
            sandbox = self._SandboxSync.create(
                image or settings.sandbox_image,
                connection_config=self._config,
                timeout=timedelta(seconds=lifetime),
                ready_timeout=timedelta(seconds=90),
                network_policy=NetworkPolicy(default_action="deny"),
                resource={"cpu": "1", "memory": "1536M"},
            )
        except Exception as exc:
            raise SandboxError(f"OpenSandbox create failed: {exc}") from exc
        return self._remember(sandbox)

    def exec(self, sandbox_id: str, cmd: str, timeout: int) -> ExecResult:
        from opensandbox.models.execd import RunCommandOpts

        if isinstance(cmd, (list, tuple)):
            cmd = " ".join(shlex.quote(str(part)) for part in cmd)
        sandbox = self._sandbox(sandbox_id)
        try:
            execution = sandbox.commands.run(
                cmd,
                opts=RunCommandOpts(working_directory="/workspace", timeout=timedelta(seconds=timeout)),
            )
        except Exception as exc:
            raise SandboxError(f"OpenSandbox exec failed: {exc}") from exc
        exit_code = execution.exit_code
        if exit_code is None:
            exit_code = 1 if execution.error else 0
        duration = 0
        if execution.complete is not None:
            duration = int(execution.complete.execution_time_in_millis)
        return ExecResult(
            exit_code=int(exit_code),
            stdout=_join(execution.logs.stdout)[-8000:],
            stderr=_join(execution.logs.stderr)[-8000:],
            duration_ms=duration,
        )

    def put_file(self, sandbox_id: str, path: str, data: bytes) -> None:
        path = _checked(path)
        q = shlex.quote(path)
        check = self.exec(
            sandbox_id,
            f'p={q}; parent=$(dirname "$p"); '
            'rp=$(realpath -m "$parent") || exit 1; '
            'case "$rp" in /workspace|/workspace/*) ;; *) exit 1;; esac; '
            '[ ! -L "$p" ]',
            timeout=15,
        )
        if check.exit_code != 0:
            raise SandboxError(f"refusing to write through a symlink: {path}")
        sandbox = self._sandbox(sandbox_id)
        try:
            sandbox.files.write_file(path, data, mode=0o644)
        except Exception as exc:
            raise SandboxError(f"OpenSandbox write_file failed: {exc}") from exc

    def get_file(self, sandbox_id: str, path: str) -> bytes:
        path = _checked(path)
        sandbox = self._sandbox(sandbox_id)
        try:
            return bytes(sandbox.files.read_bytes(path))
        except Exception as exc:
            raise SandboxError(f"OpenSandbox read_bytes failed: {exc}") from exc

    def list_files(self, sandbox_id: str, path: str) -> list[dict]:
        path = _checked(path)
        result = self.exec(
            sandbox_id,
            f"find {shlex.quote(path)} -maxdepth 2 -type f -printf '%p\\t%s\\n'",
            timeout=30,
        )
        if result.exit_code != 0:
            return []
        out: list[dict] = []
        for line in result.stdout.splitlines():
            if "\t" not in line:
                continue
            item_path, size = line.split("\t", 1)
            try:
                out.append({"path": item_path, "size": int(size)})
            except ValueError:
                continue
        return out[:500]

    def release(self, sandbox_id: str) -> None:
        with self._lock:
            sandbox = self._cache.pop(sandbox_id, None)
        if sandbox is None:
            try:
                sandbox = self._SandboxSync.connect(sandbox_id, connection_config=self._config)
            except Exception:
                return
        try:
            sandbox.destroy()
        except Exception as exc:
            raise SandboxError(f"OpenSandbox destroy failed: {exc}") from exc

    def health(self) -> bool:
        try:
            resp = httpx.get(
                f"{settings.osb_protocol}://{settings.osb_domain}/health",
                headers={"OPEN-SANDBOX-API-KEY": settings.osb_api_key},
                timeout=5,
            )
            return resp.status_code == 200
        except Exception:
            return False
