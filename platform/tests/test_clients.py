"""Internal clients keep one transport per process instead of one per call."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ailab_platform.clients import AsyncPlatformClient, ClientError, PlatformClient


class _Response:
    status_code = 200
    text = ""

    def json(self) -> dict:
        return {"ok": True}


class _SyncClient:
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    def request(self, *args, **kwargs) -> _Response:
        self.calls += 1
        return _Response()

    def close(self) -> None:
        self.closed = True


class _AsyncClient:
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    async def request(self, *args, **kwargs) -> _Response:
        self.calls += 1
        return _Response()

    async def aclose(self) -> None:
        self.closed = True


def test_sync_client_reuses_one_transport(monkeypatch) -> None:
    fake = _SyncClient()
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: fake)

    client = PlatformClient(base_url="http://api", token="t")
    client.get_run("r1")
    client.get_run("r2")

    assert fake.calls == 2
    client.close()
    assert fake.closed is True


def test_async_client_reuses_one_transport(monkeypatch) -> None:
    fake = _AsyncClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake)
    client = AsyncPlatformClient(base_url="http://api", token="t")

    async def exercise() -> None:
        await client.get_run("r1")
        await client.get_run("r2")
        await client.aclose()

    asyncio.run(exercise())
    assert fake.calls == 2
    assert fake.closed is True


def test_client_rejects_error_responses(monkeypatch) -> None:
    class Bad(_Response):
        status_code = 503
        text = "upstream unavailable"

    class BadClient(_SyncClient):
        def request(self, *args, **kwargs) -> _Response:
            return Bad()

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: BadClient())
    with pytest.raises(ClientError, match="503"):
        PlatformClient(base_url="http://api", token="t").get_run("r")


def test_short_lived_clients_are_closed(monkeypatch) -> None:
    """A Worker file and an Agent message each make their own client; with a pool inside, it must be closed."""
    from ailab_platform.agent import main as agent_main
    from ailab_platform.queue import InMemoryQueue
    from ailab_platform.worker import pipeline

    closed: list[str] = []

    class Sync:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            closed.append("worker")

        def get_file(self, file_id):
            return {"status": "ready"}  # already processed: returns at once

    monkeypatch.setattr(pipeline, "PlatformClient", Sync)
    assert pipeline.process_file("f1") == {"file_id": "f1", "skipped": "ready"}

    class Async:
        async def aclose(self):
            closed.append("agent")

    monkeypatch.setattr(agent_main, "AsyncPlatformClient", Async)
    q = InMemoryQueue()
    from ailab_platform.config import settings

    q.enqueue(settings.run_queue, {})  # malformed: dropped right away
    asyncio.run(agent_main.handle_message(None, q, q.dequeue(settings.run_queue, "c", timeout=1)))
    assert closed == ["worker", "agent"]
