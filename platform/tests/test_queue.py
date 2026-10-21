"""Delivery semantics shared by InMemoryQueue and RedisQueue.

The Redis variant runs only when AILAB_TEST_REDIS_URL points at a throwaway db.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from ailab_platform.queue import InMemoryQueue, RedisQueue


class _Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def _memory():
    clock = _Clock()

    def wait(seconds: float) -> None:
        clock.now += seconds

    return InMemoryQueue(clock=clock), wait, "q"


def _redis():
    q = RedisQueue(os.environ["AILAB_TEST_REDIS_URL"])
    prefix = f"ailab:test:{uuid.uuid4().hex[:8]}"
    q._test_keys = prefix
    return q, time.sleep, f"{prefix}:q"


@pytest.fixture(params=["memory", "redis"])
def env(request):
    if request.param == "redis":
        if not os.environ.get("AILAB_TEST_REDIS_URL"):
            pytest.skip("AILAB_TEST_REDIS_URL not set")
        q, wait, name = _redis()
        yield q, wait, name
        q.client.delete(name, f"{name}:lock")
        return
    yield _memory()


def test_message_is_delivered_once_and_gone_after_ack(env) -> None:
    q, _, name = env
    q.enqueue(name, {"run_id": "r1"})
    msg = q.dequeue(name, "c1", timeout=1)
    assert msg.payload == {"run_id": "r1"} and msg.deliveries == 1
    assert q.dequeue(name, "c2", timeout=1) is None  # in flight: nobody else gets it
    assert q.outstanding(name) == [{"run_id": "r1"}]  # but it still counts as outstanding
    q.ack(name, msg)
    assert q.outstanding(name) == []
    assert q.dequeue(name, "c1", timeout=1) is None


def test_unacked_message_is_redelivered_after_visibility_timeout(env) -> None:
    q, wait, name = env
    q.enqueue(name, {"run_id": "r1"})
    first = q.dequeue(name, "dead-consumer", timeout=1, visibility_seconds=1)
    assert q.dequeue(name, "c2", timeout=1, visibility_seconds=1) is None  # too early
    wait(1.2)
    again = q.dequeue(name, "c2", timeout=1, visibility_seconds=1)
    assert again.payload == first.payload
    assert again.deliveries == 2
    q.ack(name, again)
    assert q.outstanding(name) == []


def test_fifo_order(env) -> None:
    q, _, name = env
    for i in range(3):
        q.enqueue(name, {"n": i})
    got = [q.dequeue(name, "c", timeout=1) for _ in range(3)]
    assert [m.payload["n"] for m in got] == [0, 1, 2]


def test_locks_are_exclusive_and_expire(env) -> None:
    q, wait, name = env
    key = f"{name}:lock"
    token = q.acquire_lock(key, 1)
    assert token
    assert q.acquire_lock(key, 1) is None
    assert q.is_locked(key)
    wait(1.2)
    assert not q.is_locked(key)
    token2 = q.acquire_lock(key, 5)
    assert token2 and token2 != token
    q.release_lock(key, token2)
    assert not q.is_locked(key)


def test_a_stale_owner_cannot_release_someone_elses_lock(env) -> None:
    q, wait, name = env
    key = f"{name}:lock"
    old = q.acquire_lock(key, 1)
    wait(1.2)  # old owner's lock expired ...
    new = q.acquire_lock(key, 30)  # ... and another consumer took over
    q.release_lock(key, old)  # the old owner's cleanup runs late
    assert q.is_locked(key), "stale release must not free the new owner's lock"
    q.release_lock(key, new)
    assert not q.is_locked(key)


def test_consumers_recover_when_the_stream_disappears(env) -> None:
    """Redis without persistence loses the stream and its consumer group on restart."""
    q, _, name = env
    q.enqueue(name, {"n": 1})
    assert q.dequeue(name, "c", timeout=1) is not None
    if hasattr(q, "client"):
        q.client.delete(name)  # simulate the restart/flush
    else:
        q._entries.clear()
    q.enqueue(name, {"n": 2})  # a producer recreates the stream, without our group
    msg = q.dequeue(name, "c", timeout=1)
    assert msg is not None and msg.payload == {"n": 2}
