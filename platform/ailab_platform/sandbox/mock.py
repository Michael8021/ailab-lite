"""Local mock sandbox: a temp directory + host subprocess.

Dev/test only — no isolation. It exists so the API/Agent/Broker/Worker can be
exercised before T1/T2 images and OpenSandbox are ready.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..config import BASE_DIR, settings
from .base import ExecResult, SandboxError, SandboxProvider

SKILLS = BASE_DIR.parent / "images" / "sbx-office" / "skills"
TOOLS = BASE_DIR.parent / "images" / "sbx-office"


class MockSandbox(SandboxProvider):
    name = "mock"

    def __init__(self) -> None:
        self.root = settings.data_dir / "sandboxes"
        self.root.mkdir(parents=True, exist_ok=True)

    def _sandbox_root(self, sandbox_id: str) -> Path:
        path = (self.root / sandbox_id).resolve()
        if self.root.resolve() not in path.parents:
            raise SandboxError("invalid sandbox id")
        return path

    def _translate(self, sandbox_id: str, path: str) -> Path:
        root = self._sandbox_root(sandbox_id)
        if path == "/workspace":
            return root / "workspace"
        if path.startswith("/workspace/"):
            rel = path[len("/workspace/") :]
            target = (root / "workspace" / rel).resolve()
        elif path.startswith("/opt/skills/"):
            target = (SKILLS / path[len("/opt/skills/") :]).resolve()
        elif path.startswith("/opt/ailab-tools/"):
            target = (TOOLS / path[len("/opt/ailab-tools/") :]).resolve()
        else:
            raise SandboxError(f"mock sandbox only supports /workspace and /opt paths, got {path}")
        if root not in target.parents and target != root and SKILLS not in target.parents and TOOLS not in target.parents:
            raise SandboxError(f"path escapes sandbox: {path}")
        return target

    def acquire(self, lease_id: str, image: str, ttl_seconds: int | None = None) -> str:
        root = self._sandbox_root(lease_id)
        (root / "workspace" / "inputs").mkdir(parents=True, exist_ok=True)
        (root / "workspace" / "outputs").mkdir(parents=True, exist_ok=True)
        return lease_id

    def exec(self, sandbox_id: str, cmd: str, timeout: int) -> ExecResult:
        translated = cmd
        root = self._sandbox_root(sandbox_id)
        translated = translated.replace("/workspace", str(root / "workspace"))
        translated = translated.replace("/opt/skills", str(SKILLS))
        translated = translated.replace("/opt/ailab-tools", str(TOOLS))
        env = os.environ.copy()
        env["PATH"] = f"{Path(sys.executable).parent}:{env.get('PATH', '')}"
        started = time.monotonic()
        try:
            proc = subprocess.run(
                translated,
                shell=True,
                cwd=root / "workspace",
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            return ExecResult(
                exit_code=proc.returncode,
                stdout=proc.stdout[-8000:],
                stderr=proc.stderr[-8000:],
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except subprocess.TimeoutExpired as exc:
            return ExecResult(
                exit_code=124,
                stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
                stderr=f"timeout after {timeout}s",
                duration_ms=int((time.monotonic() - started) * 1000),
            )

    def put_file(self, sandbox_id: str, path: str, data: bytes) -> None:
        target = self._translate(sandbox_id, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def get_file(self, sandbox_id: str, path: str) -> bytes:
        target = self._translate(sandbox_id, path)
        if not target.is_file():
            raise SandboxError(f"file not found in sandbox: {path}")
        return target.read_bytes()

    def file_size(self, sandbox_id: str, path: str) -> int:
        target = self._translate(sandbox_id, path)
        if not target.is_file():
            raise SandboxError(f"not a regular file in the sandbox: {path}")
        return target.stat().st_size

    def list_files(self, sandbox_id: str, path: str) -> list[dict]:
        target = self._translate(sandbox_id, path)
        if not target.exists():
            return []
        out: list[dict] = []
        for item in sorted(target.rglob("*")):
            if item.is_file():
                out.append(
                    {
                        "path": "/workspace/" + str(item.relative_to(self._sandbox_root(sandbox_id) / "workspace")),
                        "size": item.stat().st_size,
                    }
                )
        return out[:500]

    def release(self, sandbox_id: str) -> None:
        shutil.rmtree(self._sandbox_root(sandbox_id), ignore_errors=True)
