"""Regression coverage for the SSE auth path.

FastAPI keeps ``yield`` dependencies alive until a StreamingResponse finishes.
Using the regular ``get_db``-backed dependency for SSE therefore holds one pool
connection per viewer; a 5+10 QueuePool dies at ~15 streams. The streaming auth
dependency must close its session before returning.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from ailab_platform.db import engine, init_db
from ailab_platform.deps import current_user_short


@pytest.fixture(scope="module", autouse=True)
def _db() -> None:
    init_db()


def _request(token: str = "test-token") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/runs/x/events",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
    )


def test_stream_auth_does_not_hold_a_pool_connection() -> None:
    before = engine.pool.checkedout()
    user = current_user_short(_request())  # type: ignore[arg-type]
    assert user.email == "demo@ailab.local"
    assert engine.pool.checkedout() == before


def test_stream_auth_rejects_a_bad_token() -> None:
    with pytest.raises(HTTPException) as exc:
        current_user_short(_request("not-a-token"))  # type: ignore[arg-type]
    assert exc.value.status_code == 401


def test_streaming_keeps_the_fast_poll_through_short_gaps() -> None:
    from ailab_platform.api.routes_runs import SSE_FAST_POLLS, sse_poll_delay

    assert all(sse_poll_delay(i) < 0.31 for i in range(SSE_FAST_POLLS))  # a gap between two answer batches
    assert sse_poll_delay(SSE_FAST_POLLS) >= 1.0  # really idle: back off
