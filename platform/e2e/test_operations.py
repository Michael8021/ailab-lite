"""What an operator sees: request ids, metrics from every service, and one run traceable across the logs."""

from __future__ import annotations

import re
import sys
import time
import uuid

import httpx
import pytest

from .conftest import TOKEN

pytestmark = pytest.mark.e2e


def _scrape(url: str, service_token: str) -> str:
    resp = httpx.get(f"{url}/metrics", headers={"Authorization": f"Bearer {service_token}"}, timeout=10)  # as Prometheus sends it
    assert resp.status_code == 200
    return resp.text


def _value(text: str, series: str) -> float:
    match = re.search(rf"^{re.escape(series)} ([0-9.e+-]+)$", text, re.M)
    return float(match.group(1)) if match else 0.0


def _run_to_completion(stack, prompt: str, model: str = "test-fast") -> str:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    run_id = httpx.post(f"{stack.api}/v1/runs", headers=headers, json={"prompt": prompt, "model": model}, timeout=10).json()["id"]
    deadline = time.time() + 90
    while time.time() < deadline:
        status = httpx.get(f"{stack.api}/v1/runs/{run_id}", headers=headers, timeout=10).json()["status"]
        if status in ("succeeded", "failed", "canceled"):
            assert status == "succeeded"
            return run_id
        time.sleep(0.5)
    raise AssertionError(f"run {run_id} did not finish")


def test_every_service_reports_its_own_metrics_after_a_run(stack) -> None:
    before = {name: _scrape(getattr(stack, name), stack.service_token) for name in ("api", "broker", "agent")}
    run_id = _run_to_completion(stack, "你好，在沙箱里执行一条命令")
    after = {name: _scrape(getattr(stack, name), stack.service_token) for name in ("api", "broker", "agent")}

    def grew(service: str, series: str) -> bool:
        return _value(after[service], series) > _value(before[service], series)

    assert grew("api", 'ailab_runs_total{status="succeeded"}')
    assert grew("api", 'ailab_run_seconds_count{status="succeeded"}')
    assert grew("api", 'ailab_model_tokens_total{kind="prompt",model="test-fast"}')
    assert grew("agent", 'ailab_agent_run_seconds_count{outcome="succeeded"}')
    assert grew("agent", 'ailab_llm_calls_total{model="test-fast",outcome="ok"}')
    assert grew("agent", 'ailab_tool_calls_total{ok="true",tool="exec"}')
    assert grew("broker", "ailab_sandbox_acquire_seconds_count")
    assert grew("broker", 'ailab_sandbox_exec_seconds_count{provider="mock"}')
    assert run_id not in after["api"] + after["agent"] + after["broker"], "ids must never become label values"
    if sys.platform.startswith("linux"):  # the default process collector depends on /proc
        for service in ("api", "broker", "agent"):
            assert "process_resident_memory_bytes" in after[service]


def test_the_worker_counts_the_files_it_scans(stack, tmp_path) -> None:
    before = _scrape(stack.worker, stack.service_token)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    name = f"ops-{uuid.uuid4().hex[:6]}.txt"
    file_id = httpx.post(f"{stack.api}/v1/files", headers=headers, files={"upload": (name, b"scan me", "text/plain")}, timeout=10).json()[
        "id"
    ]
    for _ in range(60):
        if httpx.get(f"{stack.api}/v1/files/{file_id}", headers=headers, timeout=10).json()["status"] == "ready":
            break
        time.sleep(0.5)
    after = _scrape(stack.worker, stack.service_token)
    assert _value(after, 'ailab_files_processed_total{outcome="ready"}') > _value(before, 'ailab_files_processed_total{outcome="ready"}')
    httpx.delete(f"{stack.api}/v1/files/{file_id}", headers=headers, timeout=10)


def test_metrics_are_not_readable_with_a_user_token(stack) -> None:
    for service in (stack.api, stack.broker, stack.agent, stack.worker):
        assert httpx.get(f"{service}/metrics", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10).status_code == 403
        assert httpx.get(f"{service}/metrics", timeout=10).status_code == 403


def test_the_portal_passes_request_ids_through_to_the_api(logged_in, stack) -> None:
    mine = f"e2e-{uuid.uuid4().hex[:8]}"
    resp = logged_in.context.request.get(f"{stack.portal}/api/v1/runs", headers={"X-Request-ID": mine})
    assert resp.status == 200 and resp.headers["x-request-id"] == mine
    generated = logged_in.context.request.get(f"{stack.portal}/api/v1/runs")
    assert re.fullmatch(r"[0-9a-f]{16}", generated.headers["x-request-id"])


def test_one_run_can_be_followed_through_the_logs_of_three_services(stack) -> None:
    run_id = _run_to_completion(stack, "你好，在沙箱里执行一条命令")
    time.sleep(0.5)  # let the last log lines reach the files
    for service in ("api", "broker", "agent"):
        lines = [line for line in (stack.log_dir / f"{service}.log").read_text(errors="replace").splitlines() if f"[{run_id}]" in line]
        assert lines, f"no log line for {run_id} in the {service} log"
    api_lines = [line for line in (stack.log_dir / "api.log").read_text(errors="replace").splitlines() if f"[{run_id}]" in line]
    assert any("/internal/runs/" in line and "status" in line for line in api_lines), (
        "the Agent's status calls carry the run id into the API log"
    )
