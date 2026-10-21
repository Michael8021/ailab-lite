"""Sandbox provider factory."""

from __future__ import annotations

from functools import lru_cache

from ..config import settings
from .base import ExecResult, SandboxError, SandboxProvider
from .docker import DockerSandbox
from .mock import MockSandbox
from .opensandbox import OpenSandboxProvider

__all__ = [
    "DockerSandbox",
    "ExecResult",
    "MockSandbox",
    "OpenSandboxProvider",
    "SandboxError",
    "SandboxProvider",
    "get_provider",
    "reset_provider",
]


@lru_cache
def get_provider() -> SandboxProvider:
    kind = settings.sandbox_provider
    if kind == "docker":
        return DockerSandbox()
    if kind == "opensandbox":
        return OpenSandboxProvider()
    return MockSandbox()


def reset_provider() -> None:
    get_provider.cache_clear()
