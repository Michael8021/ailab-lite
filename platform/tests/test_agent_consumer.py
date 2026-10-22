"""Run-message handling: ack only after the work, poison messages, duplicates."""

from __future__ import annotations

import asyncio

import pytest

from ailab_platform.agent import main as agent_main
from ailab_platform.config import settings
from ailab_platform.queue import InMemoryQueue, run_lock_key


class FakePlatform:
    statuses: list[tuple[str, str, dict]] = []

    async def aclose(self):
        pass

    async def set_run_status(self, run_id, status, **kw):
        FakePlatform.statuses.append((run_id, status, kw))
        return {}


class FakeLoop:
    def __init__(self, behaviour="ok") -> None:
        self.behaviour = behaviour
        self.calls: list[tuple[str, int]] = []

    async def process(self, run_id, attempt=1):
        self.calls.append((run_id, attempt))
        if self.behaviour == "hang":
            await asyncio.sleep(60)
        elif self.behaviour == "boom":
            raise RuntimeError("boom")
        elif self.behaviour == "cancel":
            raise asyncio.CancelledError()


@pytest.fixture()
def env(monkeypatch):
    FakePlatform.statuses = []
    monkeypatch.setattr(agent_main, "AsyncPlatformClient", FakePlatform)
    monkeypatch.setattr(settings, "run_timeout_seconds", 1)
    q = InMemoryQueue()
    q.enqueue(settings.run_queue, {"run_id": "run_1"})
    return q, q.dequeue(settings.run_queue, "c", timeout=1)


def _handle(loop, q, msg):
    asyncio.run(agent_main.handle_message(loop, q, msg))


def test_success_acks_and_releases_the_lock(env) -> None:
    q, msg = env
    loop = FakeLoop()
    _handle(loop, q, msg)
    assert loop.calls == [("run_1", 1)]
    assert q.outstanding(settings.run_queue) == []
    assert not q.is_locked(run_lock_key("run_1"))


def test_shutdown_hands_the_run_straight_back(env) -> None:
    q, msg = env
    with pytest.raises(asyncio.CancelledError):
        _handle(FakeLoop("cancel"), q, msg)
    # one message, immediately available to another agent, with the attempt carried over
    assert q.outstanding(settings.run_queue) == [{"run_id": "run_1", "attempt": 2}]
    again = q.dequeue(settings.run_queue, "other-agent", timeout=1)
    assert again.payload["run_id"] == "run_1"
    assert not q.is_locked(run_lock_key("run_1"))


def test_attempt_count_survives_handbacks(env) -> None:
    q, msg = env
    q.ack(settings.run_queue, msg)
    q.enqueue(settings.run_queue, {"run_id": "run_1", "attempt": settings.run_max_attempts + 1})
    loop = FakeLoop()
    _handle(loop, q, q.dequeue(settings.run_queue, "c", timeout=1))
    assert loop.calls == []  # bounced between agents too many times: give up
    assert FakePlatform.statuses[-1][:2] == ("run_1", "failed")


def test_timeout_fails_the_run_and_acks(env) -> None:
    q, msg = env
    _handle(FakeLoop("hang"), q, msg)
    assert FakePlatform.statuses[-1][:2] == ("run_1", "failed")
    assert "timeout" in FakePlatform.statuses[-1][2]["error"]
    assert q.outstanding(settings.run_queue) == []


def test_unexpected_crash_fails_the_run_and_acks(env) -> None:
    q, msg = env
    _handle(FakeLoop("boom"), q, msg)
    assert FakePlatform.statuses[-1][:2] == ("run_1", "failed")
    assert q.outstanding(settings.run_queue) == []


def test_poison_message_is_failed_without_running(env) -> None:
    q, msg = env
    msg.deliveries = settings.run_max_attempts + 1
    loop = FakeLoop()
    _handle(loop, q, msg)
    assert loop.calls == []
    assert FakePlatform.statuses[-1][:2] == ("run_1", "failed")
    assert q.outstanding(settings.run_queue) == []


def test_redelivery_reports_the_attempt_number(env) -> None:
    q, msg = env
    msg.deliveries = 2
    loop = FakeLoop()
    _handle(loop, q, msg)
    assert loop.calls == [("run_1", 2)]


def test_duplicate_message_for_a_run_in_progress_is_dropped(env) -> None:
    q, msg = env
    assert q.acquire_lock(run_lock_key("run_1"), 60)  # another agent is on it
    loop = FakeLoop()
    _handle(loop, q, msg)
    assert loop.calls == []
    assert q.outstanding(settings.run_queue) == []
    assert q.is_locked(run_lock_key("run_1"))  # and we did not steal its lock


def test_sigterm_cancels_the_consumer_at_once_and_keeps_uvicorns_handler(monkeypatch) -> None:
    import signal as _signal

    from ailab_platform.agent import main as agent_main

    calls = []

    async def scenario():
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(asyncio.sleep(3600))
        monkeypatch.setattr(agent_main, "_task", task)
        previous = _signal.getsignal(_signal.SIGTERM)
        _signal.signal(_signal.SIGTERM, lambda s, f: calls.append("uvicorn"))
        try:
            agent_main._chain_sigterm(loop)
            _signal.getsignal(_signal.SIGTERM)(_signal.SIGTERM, None)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert task.cancelled()
        finally:
            _signal.signal(_signal.SIGTERM, previous)

    asyncio.run(scenario())
    assert calls == ["uvicorn"]
