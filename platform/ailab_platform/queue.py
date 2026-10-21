"""Job queues with at-least-once delivery.

A consumer ``dequeue``s a message, does the work, then ``ack``s it. A message
that is not acked within the visibility timeout (consumer crashed or hung) is
handed to the next ``dequeue`` call, with ``deliveries`` incremented so the
caller can give up on poison messages. Redis (or Valkey) Streams with a consumer
group in dev/prod; an in-memory twin with the same semantics for tests.

Locks live here too: they let a consumer detect that a run is already being
executed by someone else when a duplicate or reclaimed message shows up.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache

from .config import settings

DEFAULT_VISIBILITY_SECONDS = 300


@dataclass
class Message:
    id: str
    payload: dict
    deliveries: int = 1


def run_lock_key(run_id: str) -> str:
    return f"ailab:lock:run:{run_id}"


class JobQueue(ABC):
    @abstractmethod
    def enqueue(self, name: str, payload: dict) -> None: ...

    @abstractmethod
    def dequeue(
        self,
        name: str,
        consumer: str = "default",
        timeout: int = 2,
        visibility_seconds: int = DEFAULT_VISIBILITY_SECONDS,
    ) -> Message | None: ...

    @abstractmethod
    def ack(self, name: str, message: Message) -> None: ...

    @abstractmethod
    def outstanding(self, name: str) -> list[dict]:
        """Payloads of every message not yet acked (waiting or in flight)."""

    @abstractmethod
    def acquire_lock(self, key: str, ttl_seconds: int) -> str | None:
        """Take the lock; returns an ownership token, or None if someone else holds it."""

    @abstractmethod
    def release_lock(self, key: str, token: str) -> None:
        """Release only if ``token`` still owns the lock (it may have expired and been retaken)."""

    @abstractmethod
    def is_locked(self, key: str) -> bool: ...

    def length(self, name: str) -> int:
        return len(self.outstanding(name))

    def ping(self) -> bool:
        return True


class RedisQueue(JobQueue):
    GROUP = "workers"

    def __init__(self, url: str):
        import redis

        self.client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=15)
        self._groups: set[str] = set()

    def _ensure_group(self, name: str) -> None:
        if name in self._groups:
            return
        import redis

        try:
            self.client.xgroup_create(name, self.GROUP, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._groups.add(name)

    @staticmethod
    def _decode(entry) -> tuple[str, dict]:
        msg_id, fields = entry
        return msg_id, json.loads(fields["p"])

    def enqueue(self, name: str, payload: dict) -> None:
        self._ensure_group(name)
        self.client.xadd(name, {"p": json.dumps(payload, ensure_ascii=False)})

    def _deliveries(self, name: str, msg_id: str) -> int:
        rows = self.client.xpending_range(name, self.GROUP, min=msg_id, max=msg_id, count=1)
        return int(rows[0]["times_delivered"]) if rows else 1

    def dequeue(self, name, consumer="default", timeout=2, visibility_seconds=DEFAULT_VISIBILITY_SECONDS):
        import redis

        def attempt() -> Message | None:
            self._ensure_group(name)
            # 1) take over anything a dead/hung consumer left unacked
            claimed = self.client.xautoclaim(name, self.GROUP, consumer, min_idle_time=visibility_seconds * 1000, start_id="0-0", count=1)
            entries = claimed[1]
            if not entries:
                # 2) otherwise the next new message
                res = self.client.xreadgroup(self.GROUP, consumer, {name: ">"}, count=1, block=max(1, timeout) * 1000)
                entries = res[0][1] if res else []
            if not entries:
                return None
            msg_id, payload = self._decode(entries[0])
            return Message(id=msg_id, payload=payload, deliveries=self._deliveries(name, msg_id))

        try:
            return attempt()
        except redis.ResponseError as exc:
            if "NOGROUP" not in str(exc):
                raise
            # Redis lost the stream (restart without persistence) and a producer
            # re-created it without our group: forget the cache and make the group again.
            self._groups.discard(name)
            return attempt()

    # XACK and XDEL must be atomic: a crash between them leaves an entry that
    # is already acked but still returned by XRANGE (outstanding()/reaper).
    _ACK = "redis.call('XACK', KEYS[1], ARGV[1], ARGV[2]); return redis.call('XDEL', KEYS[1], ARGV[2])"

    def ack(self, name: str, message: Message) -> None:
        self.client.eval(self._ACK, 1, name, self.GROUP, message.id)

    def outstanding(self, name: str) -> list[dict]:
        return [self._decode(e)[1] for e in self.client.xrange(name, "-", "+")]

    _RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"

    def acquire_lock(self, key: str, ttl_seconds: int) -> str | None:
        token = uuid.uuid4().hex
        return token if self.client.set(key, token, nx=True, ex=ttl_seconds) else None

    def release_lock(self, key: str, token: str) -> None:
        self.client.eval(self._RELEASE, 1, key, token)

    def is_locked(self, key: str) -> bool:
        return bool(self.client.exists(key))

    def length(self, name: str) -> int:
        return int(self.client.xlen(name))

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except Exception:
            return False


class InMemoryQueue(JobQueue):
    """Same delivery semantics as RedisQueue; ``clock`` is injectable for tests."""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._seq = 0
        # per queue: id -> [payload, deliveries, delivered_at | None]
        self._entries: dict[str, OrderedDict[str, list]] = {}
        self._locks: dict[str, tuple[str, float]] = {}

    def enqueue(self, name: str, payload: dict) -> None:
        with self._lock:
            self._seq += 1
            self._entries.setdefault(name, OrderedDict())[str(self._seq)] = [payload, 0, None]

    def _take(self, name: str, visibility_seconds: int) -> Message | None:
        now = self._clock()
        with self._lock:
            entries = self._entries.get(name, OrderedDict())
            for msg_id, entry in entries.items():  # reclaimable first, then new, in order
                if entry[2] is not None and now - entry[2] >= visibility_seconds:
                    break
            else:
                for msg_id, entry in entries.items():
                    if entry[2] is None:
                        break
                else:
                    return None
            entry[1] += 1
            entry[2] = now
            return Message(id=msg_id, payload=entry[0], deliveries=entry[1])

    def dequeue(self, name, consumer="default", timeout=2, visibility_seconds=DEFAULT_VISIBILITY_SECONDS):
        deadline = time.monotonic() + timeout
        while True:
            msg = self._take(name, visibility_seconds)
            if msg or time.monotonic() >= deadline:
                return msg
            time.sleep(0.02)

    def ack(self, name: str, message: Message) -> None:
        with self._lock:
            self._entries.get(name, OrderedDict()).pop(message.id, None)

    def outstanding(self, name: str) -> list[dict]:
        with self._lock:
            return [e[0] for e in self._entries.get(name, OrderedDict()).values()]

    def acquire_lock(self, key: str, ttl_seconds: int) -> str | None:
        now = self._clock()
        with self._lock:
            held = self._locks.get(key)
            if held and held[1] > now:
                return None
            token = uuid.uuid4().hex
            self._locks[key] = (token, now + ttl_seconds)
            return token

    def release_lock(self, key: str, token: str) -> None:
        with self._lock:
            held = self._locks.get(key)
            if held and held[0] == token:
                del self._locks[key]

    def is_locked(self, key: str) -> bool:
        with self._lock:
            held = self._locks.get(key)
            return bool(held and held[1] > self._clock())


@lru_cache
def get_queue() -> JobQueue:
    if settings.queue_backend == "memory":
        return InMemoryQueue()
    return RedisQueue(settings.redis_url)


def reset_queue() -> None:
    get_queue.cache_clear()
