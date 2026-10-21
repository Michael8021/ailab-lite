"""Test doubles shared by the integration tests."""

from __future__ import annotations

import fnmatch


class FakeRedis:
    """The subset of redis-py the Broker uses, with an advanceable clock."""

    def __init__(self) -> None:
        self.now = 1000.0
        self._data: dict[str, tuple[str, float | None]] = {}
        self._sets: dict[str, set[str]] = {}

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def _live(self, key: str) -> str | None:
        item = self._data.get(key)
        if item is None:
            return None
        value, expires = item
        if expires is not None and expires <= self.now:
            del self._data[key]
            return None
        return value

    def set(self, key, value, ex=None):
        self._data[key] = (value, self.now + ex if ex else None)

    def get(self, key):
        return self._live(key)

    def delete(self, key):
        self._data.pop(key, None)

    def sadd(self, key, member):
        self._sets.setdefault(key, set()).add(member)

    def srem(self, key, member):
        self._sets.get(key, set()).discard(member)

    def smembers(self, key):
        return set(self._sets.get(key, set()))

    def scan_iter(self, pattern):
        return [k for k in list(self._data) if fnmatch.fnmatch(k, pattern) and self._live(k) is not None]

    def ping(self):
        return True
