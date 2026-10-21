from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ailab_platform.api.main import app
from ailab_platform.db import SessionLocal
from ailab_platform.models import Run, RunEvent, RunStatus
from ailab_platform.run_state import InvalidTransition, transition_run

AUTH = {"Authorization": "Bearer test-token"}
SERVICE = {"X-Service-Token": "test-service"}


@pytest.fixture()
def api():
    with TestClient(app) as c:
        yield c


def _new_run(api) -> str:
    return api.post("/v1/runs", headers=AUTH, json={"prompt": "x"}).json()["id"]


def _types(run_id: str) -> list[str]:
    with SessionLocal() as db:
        return [e.type for e in db.scalars(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq))]


def test_happy_path_events(api) -> None:
    run_id = _new_run(api)
    api.post(f"/internal/runs/{run_id}/status", headers=SERVICE, json={"status": "running"})
    done = api.post(f"/internal/runs/{run_id}/status", headers=SERVICE, json={"status": "succeeded", "summary": "ok"})
    assert done.json()["status"] == "succeeded" and done.json()["finished_at"]
    assert _types(run_id) == ["run.created", "run.status", "run.status", "run.completed"]


def test_cannot_go_backwards_or_skip(api) -> None:
    run_id = _new_run(api)
    assert api.post(f"/internal/runs/{run_id}/status", headers=SERVICE, json={"status": "queued"}).status_code == 400
    assert api.post(f"/internal/runs/{run_id}/status", headers=SERVICE, json={"status": "succeeded"}).status_code == 409
    assert api.get(f"/v1/runs/{run_id}", headers=AUTH).json()["status"] == "queued"


def test_late_success_cannot_overwrite_a_cancel(api) -> None:
    run_id = _new_run(api)
    api.post(f"/internal/runs/{run_id}/status", headers=SERVICE, json={"status": "running"})
    api.post(f"/v1/runs/{run_id}/cancel", headers=AUTH)
    late = api.post(f"/internal/runs/{run_id}/status", headers=SERVICE, json={"status": "succeeded"})
    assert late.status_code == 200 and late.json()["status"] == "canceled"
    types = _types(run_id)
    assert "run.canceled" in types and "run.completed" not in types


def test_running_twice_is_a_noop(api) -> None:  # a redelivered message re-reports "running"
    run_id = _new_run(api)
    for _ in range(2):
        assert api.post(f"/internal/runs/{run_id}/status", headers=SERVICE, json={"status": "running"}).status_code == 200
    assert _types(run_id).count("run.status") == 1


def test_stale_writer_loses_the_race(api) -> None:
    run_id = _new_run(api)
    api.post(f"/internal/runs/{run_id}/status", headers=SERVICE, json={"status": "running"})
    with SessionLocal() as stale:
        assert stale.get(Run, run_id).status == RunStatus.RUNNING
        api.post(f"/v1/runs/{run_id}/cancel", headers=AUTH)  # another writer wins meanwhile
        run, changed = transition_run(stale, run_id, RunStatus.SUCCEEDED, summary="too late")
    assert changed is False and run.status == RunStatus.CANCELED and run.summary != "too late"


def test_invalid_transition_raises_in_the_service(api) -> None:
    run_id = _new_run(api)
    with SessionLocal() as db, pytest.raises(InvalidTransition):
        transition_run(db, run_id, RunStatus.SUCCEEDED)
