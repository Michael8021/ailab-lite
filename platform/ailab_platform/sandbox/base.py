"""Sandbox provider interface.

The Broker owns the only handle to a provider; the Agent never talks to the
container runtime directly. In production this maps to OpenSandbox's
ingress-gateway + per-sandbox token.
"""

from __future__ import annotations

import shlex
from abc import ABC, abstractmethod
from dataclasses import dataclass


class SandboxError(RuntimeError):
    pass


# The provider-side expiry outlives the Broker lease by this much, so the Broker
# reaper normally acts first and the provider is only the backstop.
SANDBOX_EXPIRY_GRACE_SECONDS = 300


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


class SandboxProvider(ABC):
    name = "base"

    @abstractmethod
    def acquire(self, lease_id: str, image: str, ttl_seconds: int | None = None) -> str:
        """Create a sandbox and return the provider-side id.

        ``ttl_seconds`` is the lease lifetime; the sandbox itself must expire
        ``SANDBOX_EXPIRY_GRACE_SECONDS`` later even if nobody releases it.
        """

    @abstractmethod
    def exec(self, sandbox_id: str, cmd: str, timeout: int) -> ExecResult: ...

    @abstractmethod
    def put_file(self, sandbox_id: str, path: str, data: bytes) -> None: ...

    @abstractmethod
    def get_file(self, sandbox_id: str, path: str) -> bytes: ...

    @abstractmethod
    def list_files(self, sandbox_id: str, path: str) -> list[dict]: ...

    @abstractmethod
    def release(self, sandbox_id: str) -> None: ...

    def file_size(self, sandbox_id: str, path: str) -> int:
        """Size of a regular file, without transferring it.

        ``[ -f ]`` and ``stat`` follow symlinks, so a link created by the agent
        could point outside ``/workspace``. Resolve the parent and reject the
        link before reading. (A determined agent could still race the check;
        the provider boundary is the real security boundary.)
        """
        q = shlex.quote(path)
        cmd = (
            f"parent=$(dirname {q}); "
            'rp=$(realpath -m "$parent") || exit 1; '
            'case "$rp" in /workspace|/workspace/*) ;; *) exit 1;; esac; '
            f"[ ! -L {q} ] || exit 1; "
            f"[ -f {q} ] || exit 1; "
            f"stat -c %s {q}"
        )
        res = self.exec(sandbox_id, cmd, timeout=15)
        if res.exit_code != 0 or not res.stdout.strip().isdigit():
            raise SandboxError(f"not a regular file in the sandbox: {path}")
        return int(res.stdout.strip())

    def reap_expired(self) -> int:
        """Remove sandboxes past their provider-side expiry; returns how many."""
        return 0

    def health(self) -> bool:
        return True
