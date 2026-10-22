"""Request ids, structured logs and metrics."""

from __future__ import annotations

import asyncio
import io
import json
import logging

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from ailab_platform import metrics, obs
from ailab_platform.api.main import app
from ailab_platform.clients import PlatformClient
from ailab_platform.config import settings
from ailab_platform.llm import LLMResponse, ToolCall

AUTH = {"Authorization": "Bearer test-token"}
SERVICE = {"X-Service-Token": "test-service"}


def sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture()
def api():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def clean_logging():
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in saved:
        root.addHandler(handler)
    root.setLevel(level)


# ------------------------------------------------------------------------------------- request ids


def test_every_response_carries_a_request_id_and_they_differ(api) -> None:
    a, b = api.get("/healthz"), api.get("/healthz")
    assert a.headers["x-request-id"] != b.headers["x-request-id"]
    assert obs._SAFE_ID.match(a.headers["x-request-id"])


def test_a_sane_incoming_id_is_kept_so_a_trace_can_span_services(api) -> None:
    assert api.get("/healthz", headers={"X-Request-ID": "run_abc-123.def"}).headers["x-request-id"] == "run_abc-123.def"


@pytest.mark.parametrize("bad", ["has spaces", "x" * 65, "semi;colon", "<script>", ""])
def test_an_unsafe_incoming_id_is_replaced_not_echoed(api, bad) -> None:
    got = api.get("/healthz", headers={"X-Request-ID": bad}).headers["x-request-id"]
    assert got != bad and obs._SAFE_ID.match(got)


@pytest.mark.parametrize("bad", ["ünicode", "line\nbreak", None])
def test_clean_request_id_rejects_what_a_client_could_not_even_send(bad) -> None:
    assert obs.clean_request_id(bad) != bad and obs._SAFE_ID.match(obs.clean_request_id(bad))


def test_internal_calls_forward_the_current_id() -> None:
    client = PlatformClient(base_url="http://x", token="t")
    assert "X-Request-ID" not in client._get_run("run_1").kwargs["headers"]
    token = obs.request_id_var.set("run_forwarded")
    try:
        assert client._get_run("run_1").kwargs["headers"]["X-Request-ID"] == "run_forwarded"
    finally:
        obs.request_id_var.reset(token)


# --------------------------------------------------------------------------------------------- logs


def _capture(service: str = "test") -> io.StringIO:
    obs.setup_logging(service)
    stream = io.StringIO()
    logging.getLogger().handlers[0].stream = stream
    return stream


def test_text_logs_show_service_and_request_id(clean_logging) -> None:
    stream = _capture("agent")
    token = obs.request_id_var.set("run_42")
    try:
        logging.getLogger("ailab.test").info("hello")
    finally:
        obs.request_id_var.reset(token)
    line = stream.getvalue().strip()
    assert " agent " in line and "[run_42]" in line and line.endswith("hello")


def test_json_logs_are_one_valid_object_per_line(clean_logging, monkeypatch) -> None:
    monkeypatch.setattr(settings, "log_format", "json")
    stream = _capture("broker")
    token = obs.request_id_var.set("req_1")
    try:
        log = logging.getLogger("ailab.test")
        log.warning("café ☕ %s", "x")
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("failed")
    finally:
        obs.request_id_var.reset(token)
    first, second = (json.loads(line) for line in stream.getvalue().strip().splitlines())
    assert first["msg"] == "café ☕ x" and first["level"] == "WARNING" and first["service"] == "broker" and first["request_id"] == "req_1"
    assert "ValueError: boom" in second["exc"]


def test_probe_endpoints_do_not_flood_the_log(api, clean_logging) -> None:
    stream = _capture("api")
    logging.getLogger().setLevel(logging.INFO)
    api.get("/healthz")
    api.get("/v1/runs", headers=AUTH)
    ours = [line for line in stream.getvalue().splitlines() if "ailab.api.http" in line]  # (the test client logs too)
    assert not [line for line in ours if "/healthz" in line]
    assert any("GET /v1/runs -> 200" in line for line in ours)


# ------------------------------------------------------------------------------------------ metrics


def test_http_metrics_use_the_route_template_not_the_concrete_path(api) -> None:
    run_id = api.post("/v1/runs", headers=AUTH, json={"prompt": "x", "model": "test-fast"}).json()["id"]
    before = sample("ailab_http_requests_total", service="api", method="GET", route="/v1/runs/{run_id}", status="200")
    api.get(f"/v1/runs/{run_id}", headers=AUTH)
    assert sample("ailab_http_requests_total", service="api", method="GET", route="/v1/runs/{run_id}", status="200") == before + 1
    body = api.get("/metrics", headers=SERVICE).text
    assert run_id not in body, "a run id as a label value would grow the series without bound"
    assert api.get("/definitely/not/a/route").status_code == 404
    assert 'route="unmatched"' in api.get("/metrics", headers=SERVICE).text


def test_metrics_endpoint_accepts_only_the_service_token(api) -> None:
    assert api.get("/metrics").status_code == 403
    assert api.get("/metrics", headers=AUTH).status_code == 403, "a user's token must not read operational data"
    assert api.get("/metrics", headers={"X-Service-Token": "wrong"}).status_code == 403
    assert api.get("/metrics", headers=SERVICE).status_code == 200
    assert api.get("/metrics", headers={"Authorization": "Bearer test-service"}).status_code == 200  # what a Prometheus scrape sends
    assert api.get("/metrics", params={"token": "test-service"}).status_code == 403


def test_metrics_expose_queue_depth_run_states_and_process_memory(api) -> None:
    import sys

    api.post("/v1/runs", headers=AUTH, json={"prompt": "x", "model": "test-fast"})
    text = api.get("/metrics", headers=SERVICE).text
    for series in (
        'ailab_runs_in_state{status="queued"}',
        'ailab_queue_depth{queue="runs"}',
        'ailab_queue_depth{queue="files"}',
    ):
        assert series in text, series
    if sys.platform.startswith("linux"):  # prometheus_client's default process collector reads /proc
        assert "process_resident_memory_bytes" in text
    assert api.get("/metrics", headers=SERVICE).headers["content-type"].startswith("text/plain")


def test_run_lifecycle_is_counted_and_timed(api) -> None:
    ok = sample("ailab_runs_total", status="succeeded")
    secs = sample("ailab_run_seconds_count", status="succeeded")
    run_id = api.post("/v1/runs", headers=AUTH, json={"prompt": "x", "model": "test-fast"}).json()["id"]
    for status in ("running", "succeeded"):
        api.post(f"/internal/runs/{run_id}/status", headers=SERVICE, json={"status": status})
    api.post(f"/internal/runs/{run_id}/status", headers=SERVICE, json={"status": "succeeded"})  # repeated: not counted twice
    assert sample("ailab_runs_total", status="succeeded") == ok + 1
    assert sample("ailab_run_seconds_count", status="succeeded") == secs + 1


def test_tokens_are_counted_per_model_and_kind(api) -> None:
    run_id = api.post("/v1/runs", headers=AUTH, json={"prompt": "x", "model": "test-fast"}).json()["id"]
    base = {k: sample("ailab_model_tokens_total", model="test-fast", kind=k) for k in ("prompt", "completion", "cached")}
    other = sample("ailab_model_tokens_total", model="other", kind="prompt")
    api.post(
        f"/internal/runs/{run_id}/usage",
        headers=SERVICE,
        json={"model": "test-fast", "prompt_tokens": 100, "completion_tokens": 7, "cached_tokens": 60},
    )
    api.post(f"/internal/runs/{run_id}/usage", headers=SERVICE, json={"model": "made-up-by-a-buggy-caller", "prompt_tokens": 5})
    assert sample("ailab_model_tokens_total", model="test-fast", kind="prompt") == base["prompt"] + 100
    assert sample("ailab_model_tokens_total", model="test-fast", kind="completion") == base["completion"] + 7
    assert sample("ailab_model_tokens_total", model="test-fast", kind="cached") == base["cached"] + 60
    assert sample("ailab_model_tokens_total", model="other", kind="prompt") == other + 5


def test_quota_rejections_are_counted(api, monkeypatch) -> None:
    before = sample("ailab_quota_rejections_total", kind="daily_tokens")
    monkeypatch.setattr(settings, "daily_token_limit", 0)
    assert api.post("/v1/runs", headers=AUTH, json={"prompt": "x", "model": "test-fast"}).status_code == 429
    assert sample("ailab_quota_rejections_total", kind="daily_tokens") == before + 1


def test_labels_that_come_from_outside_are_bounded() -> None:
    assert metrics.tool_label("exec") == "exec"
    assert metrics.tool_label("rm -rf / # injected by the model") == "other"
    assert metrics.model_label("test-pro") == "test-pro"
    assert metrics.model_label("whatever-the-run-says") == "other"


# -------------------------------------------------------------------------------------- broker/agent


def test_broker_metrics_cover_acquire_exec_capacity_and_leases(monkeypatch) -> None:
    from tests.test_broker_lease import _manager

    manager, _provider, _ = _manager()
    acquired = sample("ailab_sandbox_acquire_seconds_count")
    lease = manager.create("run_m", ttl_seconds=60)
    assert sample("ailab_sandbox_acquire_seconds_count") == acquired + 1

    execs = sample("ailab_sandbox_exec_seconds_count", provider="mock")
    manager.exec(lease["lease_id"], "echo hi")
    assert sample("ailab_sandbox_exec_seconds_count", provider="mock") == execs + 1

    collected = {m.name: m.samples[0].value for m in metrics.BrokerStateCollector(lambda: manager).collect()}
    assert collected == {"ailab_sandbox_leases_active": 1.0, "ailab_sandbox_capacity": float(settings.max_sandboxes)}

    monkeypatch.setattr(settings, "max_sandboxes", 1)
    rejected = sample("ailab_sandbox_capacity_rejections_total")
    with pytest.raises(Exception, match="capacity"):
        manager.create("run_other")
    assert sample("ailab_sandbox_capacity_rejections_total") == rejected + 1


def test_agent_metrics_cover_the_run_the_model_calls_and_the_tools() -> None:
    from ailab_platform.agent.loop import AgentLoop
    from tests.test_agent_loop import FakePlatform, RichBroker, ScriptedLLM

    runs = sample("ailab_agent_run_seconds_count", outcome="succeeded")
    calls = sample("ailab_llm_calls_total", model="other", outcome="ok")
    tools = sample("ailab_tool_calls_total", tool="other", ok="false")
    recovered = sample("ailab_agent_recovered_total")
    steps = [LLMResponse(tool_calls=[ToolCall(id="c", name="not-a-real-tool", arguments={})]), LLMResponse(content="done")]
    asyncio.run(AgentLoop(platform=FakePlatform(), broker=RichBroker(), llm=ScriptedLLM(steps)).process("run_1", attempt=2))
    assert sample("ailab_agent_run_seconds_count", outcome="succeeded") == runs + 1
    assert sample("ailab_llm_calls_total", model="other", outcome="ok") == calls + 2
    assert sample("ailab_tool_calls_total", tool="other", ok="false") == tools + 1  # an invented tool name is one label
    assert sample("ailab_agent_recovered_total") == recovered + 1


def test_agent_failure_and_cancel_outcomes_are_distinguished() -> None:
    from ailab_platform.agent.loop import AgentLoop
    from tests.test_agent_loop import FakePlatform, RichBroker, ScriptedLLM

    class Exploding(ScriptedLLM):
        async def chat(self, *a, **k):
            raise RuntimeError("upstream down")

    failed_calls = sample("ailab_llm_calls_total", model="other", outcome="error")
    errors = sample("ailab_agent_run_seconds_count", outcome="error")
    asyncio.run(AgentLoop(platform=FakePlatform(), broker=RichBroker(), llm=Exploding([])).process("run_1"))
    assert sample("ailab_llm_calls_total", model="other", outcome="error") == failed_calls + 1
    assert sample("ailab_agent_run_seconds_count", outcome="error") == errors + 1


def test_worker_and_reaper_counters(monkeypatch) -> None:
    from ailab_platform.queue import InMemoryQueue
    from ailab_platform.worker import main as worker_main

    class P:
        def patch_file(self, *a, **k):
            pass

    monkeypatch.setattr(worker_main, "PlatformClient", P)
    q = InMemoryQueue()
    ready, rejected = sample("ailab_files_processed_total", outcome="ready"), sample("ailab_files_processed_total", outcome="rejected")
    monkeypatch.setattr(worker_main, "process_file", lambda fid: {"file_id": fid, "kind": "text"})
    q.enqueue(settings.file_queue, {"file_id": "f1"})
    worker_main.handle_message(q, q.dequeue(settings.file_queue, "w", timeout=1))
    monkeypatch.setattr(worker_main, "process_file", lambda fid: {"file_id": fid, "rejected": {"ok": False}})
    q.enqueue(settings.file_queue, {"file_id": "f2"})
    worker_main.handle_message(q, q.dequeue(settings.file_queue, "w", timeout=1))
    assert sample("ailab_files_processed_total", outcome="ready") == ready + 1
    assert sample("ailab_files_processed_total", outcome="rejected") == rejected + 1
    assert obs.request_id_var.get() == obs.NO_REQUEST, "the worker must not leak a file id into the next message's logs"
