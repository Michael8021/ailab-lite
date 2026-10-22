"""deploy.sh's catalog helper: a custom OpenAI-compatible model becomes a catalog the platform accepts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from ailab_platform.catalog import load_catalog

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "make_catalog.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


def test_a_custom_model_becomes_the_default_next_to_the_demo(tmp_path) -> None:
    out = tmp_path / "models.local.toml"
    r = _run(
        "custom",
        str(out),
        "--base-url",
        "https://api.example.com/v1/",
        "--name",
        "Qwen/Qwen3-32B",
        "--label",
        '通义 "Qwen"',
        "--key-env",
        "AILAB_KEY_MAIN",
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "qwen3-32b"
    cat = load_catalog(out)  # the platform's own parser accepts it
    spec = cat.get("qwen3-32b")
    assert spec.base_url == "https://api.example.com/v1" and spec.upstream_model == "Qwen/Qwen3-32B" and spec.name == '通义 "Qwen"'
    assert spec.api_key_env == "AILAB_KEY_MAIN" and spec.default and spec.tools
    assert cat.get("ailab-demo") is not None
    assert _run("key-env", str(out)).stdout.strip() == "AILAB_KEY_MAIN"
    assert _run("default", str(out)).stdout.strip() == "qwen3-32b"


def test_keyless_and_chat_only(tmp_path) -> None:
    out = tmp_path / "m.toml"
    assert _run("custom", str(out), "--base-url", "http://10.0.0.5:8000/v1", "--name", "llama3", "--no-tools").returncode == 0
    spec = load_catalog(out).get("llama3")
    assert spec.api_key_env == "" and spec.tools is False
    assert _run("key-env", str(out)).stdout.strip() == ""


@pytest.mark.parametrize(
    "args,message",
    [
        (["--base-url", "api.example.com", "--name", "x"], "http(s) URL"),
        (["--base-url", "https://a.b/v1/chat/completions", "--name", "x"], "before /chat/completions"),
        (["--base-url", "https://a.b/v1", "--name", "x", "--key-env", "SECRET"], "AILAB_KEY_"),
    ],
)
def test_mistakes_are_refused(tmp_path, args, message) -> None:
    r = _run("custom", str(tmp_path / "x.toml"), *args)
    assert r.returncode != 0 and message in r.stderr


def test_the_stock_catalog_passes_the_check() -> None:
    r = _run("check", str(SCRIPT.parents[1] / "models.toml"))
    assert r.returncode == 0 and "deepseek-v4.1-flash" in r.stdout
