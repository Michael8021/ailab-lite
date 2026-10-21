"""Sandbox path rules, shared by the Agent (early feedback) and the Broker (enforcement)."""

from __future__ import annotations

import posixpath

WORKSPACE = "/workspace"
OUTPUTS = "/workspace/outputs"


class PathError(ValueError):
    pass


def normalize_path(path: str, under: str = WORKSPACE) -> str:
    """Return the canonical absolute path, which must be ``under`` itself or inside it.

    ``startswith("/workspace")`` is not enough: it accepts ``/workspace-evil`` and
    only ``normpath`` resolves ``a/../..`` tricks.
    """
    if not path or "\x00" in path:
        raise PathError("empty or invalid path")
    norm = posixpath.normpath(path)
    if not norm.startswith("/") or norm.startswith("//"):
        raise PathError(f"path must be absolute: {path}")
    if norm != under and not norm.startswith(under + "/"):
        raise PathError(f"path must stay under {under}: {path}")
    return norm
