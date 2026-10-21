"""Browser tests against a real stack: four Python services, Redis, and the Portal's production build.

Everything is real except the two things that cost money or need privileges: the sandbox is the mock
provider (a temp directory) and the models are the mock ones from tests/models.test.toml.

    AILAB_E2E=1 pytest e2e                       (needs Redis, Node, and Chromium - see below)

Environment:
    AILAB_TEST_REDIS_URL               default redis://127.0.0.1:6379/15 (a scratch database: keys are removed after)
    PLAYWRIGHT_CHROMIUM_EXECUTABLE     use this browser binary instead of the one `playwright install` fetched
    AILAB_E2E_BASE_PORT / _PORTAL_PORT default 18080..18083 and 13000
    AILAB_E2E_PORTAL_DIR               test another checkout of the Portal (needs its node_modules), e.g. a dependency-upgrade branch
    AILAB_E2E_LOG_DIR                  where service logs are copied (default platform/e2e-logs)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

PLATFORM = Path(__file__).resolve().parents[1]
PORTAL_SRC = Path(os.environ.get("AILAB_E2E_PORTAL_DIR") or PLATFORM / "portal")  # point elsewhere to try an upgrade
TOKEN = "e2e-login-token-0123456789abcdef"
SERVICE_TOKEN = "e2e-service-token-0123456789abcdef0123"
BASE_PORT = int(os.environ.get("AILAB_E2E_BASE_PORT", "18080"))
PORTAL_PORT = int(os.environ.get("AILAB_E2E_PORTAL_PORT", "13000"))
REDIS_URL = os.environ.get("AILAB_TEST_REDIS_URL", "redis://127.0.0.1:6379/15")
LOG_DIR = Path(os.environ.get("AILAB_E2E_LOG_DIR", PLATFORM / "e2e-logs"))


def pytest_collection_modifyitems(config, items):
    if os.environ.get("AILAB_E2E") == "1":
        return
    skip = pytest.mark.skip(reason="browser E2E: set AILAB_E2E=1 (needs Redis, Node and Chromium)")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)


@dataclass
class Stack:
    portal: str
    api: str
    token: str = TOKEN


def _wait(url: str, what: str, seconds: int = 90) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{what} did not come up at {url}")


def _build_portal(workdir: Path) -> Path:
    """Build the Portal in a copy: the repo's tsconfig.json and any running Portal's .next stay untouched."""
    if not (PORTAL_SRC / "node_modules").is_dir():
        raise RuntimeError("platform/portal/node_modules is missing: run `npm ci` there first")
    dst = workdir / "portal"
    dst.mkdir()
    for name in ("app", "lib", "middleware.ts", "next.config.mjs", "tsconfig.json", "package.json", "package-lock.json", "next-env.d.ts"):
        src = PORTAL_SRC / name
        if src.is_dir():
            shutil.copytree(src, dst / name)
        else:
            shutil.copy2(src, dst / name)
    # Not a symlink: Turbopack (Next 16's default) refuses to resolve files outside the workspace root.
    # A hard-link copy is instant and stays inside it; fall back to a real copy across filesystems.
    linked = subprocess.run(["cp", "-al", str(PORTAL_SRC / "node_modules"), str(dst / "node_modules")], capture_output=True)
    if linked.returncode != 0:
        shutil.rmtree(dst / "node_modules", ignore_errors=True)
        shutil.copytree(PORTAL_SRC / "node_modules", dst / "node_modules", symlinks=True)
    subprocess.run(["npm", "run", "build"], cwd=dst, check=True, capture_output=True, text=True, timeout=600)
    return dst


@pytest.fixture(scope="session")
def stack(tmp_path_factory):
    import redis

    work = tmp_path_factory.mktemp("e2e")
    tag = uuid.uuid4().hex[:8]
    ports = {"api": BASE_PORT, "broker": BASE_PORT + 1, "agent": BASE_PORT + 2, "worker": BASE_PORT + 3}
    data = work / "data"
    env = {k: v for k, v in os.environ.items() if not k.startswith("AILAB_")}
    env.update(
        AILAB_ENV="test",
        AILAB_ENV_FILE=str(work / "no-such.env"),  # never read a developer's real .env (it may hold model keys)
        AILAB_DATA_DIR=str(data),
        AILAB_STORAGE_ROOT=str(data / "storage"),
        AILAB_DATABASE_URL=f"sqlite:///{data}/e2e.db",
        AILAB_REDIS_URL=REDIS_URL,
        AILAB_RUN_QUEUE=f"ailab:stream:e2e-runs-{tag}",
        AILAB_FILE_QUEUE=f"ailab:stream:e2e-files-{tag}",
        AILAB_SANDBOX_PROVIDER="mock",
        AILAB_MODELS_FILE=str(PLATFORM / "tests" / "models.test.toml"),
        AILAB_API_BASE_URL=f"http://127.0.0.1:{ports['api']}",
        AILAB_BROKER_BASE_URL=f"http://127.0.0.1:{ports['broker']}",
        AILAB_DEV_TOKEN=TOKEN,
        AILAB_SERVICE_TOKEN=SERVICE_TOKEN,
        AILAB_RUN_TIMEOUT_SECONDS="120",
        AILAB_REAPER_INTERVAL_SECONDS="3600",
    )
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    procs: list[subprocess.Popen] = []
    logs = []

    def start(name: str, cmd: list[str], cwd: Path, extra_env: dict | None = None) -> None:
        log = open(LOG_DIR / f"{name}.log", "wb")
        logs.append(log)
        procs.append(subprocess.Popen(cmd, cwd=cwd, env={**env, **(extra_env or {})}, stdout=log, stderr=subprocess.STDOUT))

    try:
        portal_dir = _build_portal(work)
        for name, port in ports.items():
            start(
                name,
                [sys.executable, "-m", "uvicorn", f"ailab_platform.{name}.main:app", "--host", "127.0.0.1", "--port", str(port)],
                PLATFORM,
            )
        for name, port in ports.items():
            _wait(f"http://127.0.0.1:{port}/healthz", name)
        start(
            "portal",
            ["node", str(portal_dir / "node_modules/next/dist/bin/next"), "start", "-H", "127.0.0.1", "-p", str(PORTAL_PORT)],
            portal_dir,
            {"AILAB_API_BASE": f"http://127.0.0.1:{ports['api']}"},
        )
        _wait(f"http://127.0.0.1:{PORTAL_PORT}/login", "portal")
        yield Stack(portal=f"http://127.0.0.1:{PORTAL_PORT}", api=f"http://127.0.0.1:{ports['api']}")
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        for log in logs:
            log.close()
        client = redis.Redis.from_url(REDIS_URL)
        for pattern in (f"ailab:stream:e2e-*-{tag}", "ailab:lease:*", "ailab:leases:active", "ailab:lock:*"):
            for key in client.scan_iter(pattern):
                client.delete(key)


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
        b = pw.chromium.launch(executable_path=exe, args=["--no-sandbox"])
        yield b
        b.close()


@pytest.fixture()
def context(browser):
    ctx = browser.new_context(accept_downloads=True, viewport={"width": 1280, "height": 900})
    yield ctx
    ctx.close()


@pytest.fixture()
def page(context):
    return context.new_page()


def login(page, stack: Stack, token: str = TOKEN, path: str = "/") -> None:
    page.goto(stack.portal + path)
    page.fill("input[type=password]", token)
    page.click("button[type=submit]")
    page.wait_for_selector("input[type=password]", state="detached", timeout=15000)


@pytest.fixture()
def logged_in(page, stack):
    login(page, stack)
    page.wait_for_selector("select option", state="attached", timeout=15000)
    return page
