"""Path safety at the provider boundary.

The Broker normalizes paths lexically; a sandbox can still create symlinks.
Providers must refuse to read/write through them before the kernel follows the
link. Docker/OpenSandbox cover this with ``realpath`` + ``[ ! -L ]``; the mock
provider resolves the final path and rejects anything outside its root.
"""

from __future__ import annotations

import pytest

from ailab_platform.queue import Message, RedisQueue
from ailab_platform.sandbox.base import ExecResult, SandboxError, SandboxProvider
from ailab_platform.sandbox.mock import MockSandbox


class _RecordingProvider(SandboxProvider):
    def __init__(self) -> None:
        self.cmd = ""

    def acquire(self, lease_id: str, image: str, ttl_seconds: int | None = None) -> str:
        return lease_id

    def exec(self, sandbox_id: str, cmd: str, timeout: int) -> ExecResult:
        self.cmd = cmd
        return ExecResult(exit_code=0, stdout="12\n", stderr="", duration_ms=1)

    def put_file(self, sandbox_id: str, path: str, data: bytes) -> None:
        raise NotImplementedError

    def get_file(self, sandbox_id: str, path: str) -> bytes:
        raise NotImplementedError

    def list_files(self, sandbox_id: str, path: str) -> list[dict]:
        raise NotImplementedError

    def release(self, sandbox_id: str) -> None:
        raise NotImplementedError


def test_file_size_command_checks_parent_and_link() -> None:
    provider = _RecordingProvider()
    assert provider.file_size("sandbox", "/workspace/outputs/a b.txt") == 12
    assert "realpath -m" in provider.cmd
    assert "[ ! -L" in provider.cmd
    assert "/workspace/outputs/a b.txt" in provider.cmd


def test_mock_sandbox_rejects_symlink_escape() -> None:
    provider = MockSandbox()
    sandbox_id = provider.acquire("lease-symlink", "unused")
    root = provider._sandbox_root(sandbox_id)
    link = root / "workspace" / "outputs" / "passwd"
    link.symlink_to("/etc/passwd")

    with pytest.raises(SandboxError, match="escapes"):
        provider.file_size(sandbox_id, "/workspace/outputs/passwd")
    with pytest.raises(SandboxError, match="escapes"):
        provider.get_file(sandbox_id, "/workspace/outputs/passwd")
    with pytest.raises(SandboxError, match="escapes"):
        provider.put_file(sandbox_id, "/workspace/outputs/passwd", b"overwrite")


def test_redis_ack_is_one_atomic_script() -> None:
    class _FakeRedis:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def eval(self, script: str, numkeys: int, *args: str) -> int:
            self.calls.append((script, numkeys, args))
            return 1

    q = object.__new__(RedisQueue)
    q.client = _FakeRedis()  # type: ignore[assignment]
    q.ack("ailab:stream:runs", Message(id="1-0", payload={}))

    script, numkeys, args = q.client.calls[0]
    assert "XACK" in script and "XDEL" in script
    assert numkeys == 1
    assert args == ("ailab:stream:runs", "workers", "1-0")
