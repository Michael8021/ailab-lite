"""Prometheus metrics: a deliberately small set that answers operational questions.

* Is work piling up?             ailab_queue_depth, ailab_runs_in_state
* How long do things take?       ailab_run_seconds, ailab_sandbox_acquire_seconds, ailab_llm_call_seconds, ailab_tool_seconds
* What fails?                    ailab_runs_total{status="failed"}, ailab_llm_calls_total{outcome}, ailab_tool_calls_total{ok}
* What does it cost?             ailab_model_tokens_total{model,kind}, ailab_artifact_bytes_total
* Is a service healthy/sized?    process_resident_memory_bytes (prometheus_client's process collector), ailab_sandbox_leases_active

Every service is a single process and serves its own /metrics (guarded by the service token), so the default
registry is the right one. Label values that come from outside (tool names the model invents, model ids)
are mapped onto a fixed set: an unbounded label would grow Prometheus without limit.
"""

from __future__ import annotations

import logging

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector
from starlette.responses import Response

log = logging.getLogger("ailab.metrics")

_FAST = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
_SLOW = (1, 2.5, 5, 10, 20, 40, 60, 120, 300, 600, 900)

HTTP_REQUESTS = Counter("ailab_http_requests_total", "HTTP requests", ["service", "method", "route", "status"])
HTTP_SECONDS = Histogram("ailab_http_request_seconds", "Time to first response byte", ["service", "route"], buckets=_FAST)

RUNS = Counter("ailab_runs_total", "Runs that entered a status", ["status"])
RUN_SECONDS = Histogram("ailab_run_seconds", "Start-to-finish time of a run", ["status"], buckets=_SLOW)
MODEL_TOKENS = Counter("ailab_model_tokens_total", "Model tokens as reported by the upstream", ["model", "kind"])
QUOTA_REJECTIONS = Counter("ailab_quota_rejections_total", "Requests refused by a quota", ["kind"])
REAPER_ACTIONS = Counter("ailab_reaper_actions_total", "Things the reaper repaired", ["action"])

SANDBOX_ACQUIRE_SECONDS = Histogram(
    "ailab_sandbox_acquire_seconds", "Time to create a lease: sandbox start + input injection", buckets=_SLOW
)
SANDBOX_EXEC_SECONDS = Histogram("ailab_sandbox_exec_seconds", "Command execution in a sandbox", ["provider"], buckets=_SLOW)
SANDBOX_CAPACITY_REJECTIONS = Counter("ailab_sandbox_capacity_rejections_total", "Lease requests turned away because all slots were taken")
ARTIFACT_BYTES = Counter("ailab_artifact_bytes_total", "Bytes delivered from sandboxes")

AGENT_RUN_SECONDS = Histogram("ailab_agent_run_seconds", "Time the Agent spent on one run", ["outcome"], buckets=_SLOW)
AGENT_RECOVERED = Counter("ailab_agent_recovered_total", "Runs restarted because an earlier attempt did not finish")
LLM_CALLS = Counter("ailab_llm_calls_total", "Model calls", ["model", "outcome"])
LLM_SECONDS = Histogram("ailab_llm_call_seconds", "Model call latency", ["model"], buckets=_SLOW)
TOOL_CALLS = Counter("ailab_tool_calls_total", "Agent tool calls", ["tool", "ok"])
TOOL_SECONDS = Histogram("ailab_tool_seconds", "Agent tool latency", ["tool"], buckets=_SLOW)

FILES = Counter("ailab_files_processed_total", "Uploaded files the Worker finished with", ["outcome"])
FILE_SECONDS = Histogram("ailab_file_processing_seconds", "Worker time per uploaded file", buckets=_SLOW)

KNOWN_TOOLS = frozenset({"list_files", "read_file", "write_file", "exec", "deliver_file", "finish"})


def tool_label(name: str) -> str:
    """Tool names come from the model; anything outside our own set is one label."""
    return name if name in KNOWN_TOOLS else "other"


def model_label(model_id: str) -> str:
    from .catalog import get_catalog

    return model_id if get_catalog().get(model_id) is not None else "other"


def observe_http(service: str, method: str, route: str, status: int, seconds: float) -> None:
    HTTP_REQUESTS.labels(service, method, route, str(status)).inc()
    HTTP_SECONDS.labels(service, route).observe(seconds)


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ----------------------------------------------------------------------------- scrape-time gauges
class ApiStateCollector(Collector):
    """Values that live in the database / queue are read when Prometheus scrapes, not tracked incrementally."""

    def collect(self):
        from sqlalchemy import func, select

        from .config import settings
        from .db import SessionLocal
        from .models import Run, RunStatus
        from .queue import get_queue

        runs = GaugeMetricFamily("ailab_runs_in_state", "Runs per status right now", labels=["status"])
        try:
            with SessionLocal() as db:
                counts = dict(db.execute(select(Run.status, func.count()).group_by(Run.status)).all())
            for status in (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED):
                runs.add_metric([status], counts.get(status, 0))
        except Exception:
            log.warning("could not read run counts for /metrics", exc_info=True)
        yield runs

        depth = GaugeMetricFamily("ailab_queue_depth", "Messages not yet acknowledged (waiting or in flight)", labels=["queue"])
        try:
            queue = get_queue()
            depth.add_metric(["runs"], queue.length(settings.run_queue))
            depth.add_metric(["files"], queue.length(settings.file_queue))
        except Exception:
            log.warning("could not read queue depth for /metrics", exc_info=True)
        yield depth


class BrokerStateCollector(Collector):
    def __init__(self, manager_getter):
        self._manager = manager_getter

    def collect(self):
        from .config import settings

        active = GaugeMetricFamily("ailab_sandbox_leases_active", "Sandbox leases currently held")
        capacity = GaugeMetricFamily("ailab_sandbox_capacity", "Sandbox slots this Broker will hand out")
        try:
            active.add_metric([], len(self._manager().active_leases()))
        except Exception:
            log.warning("could not count leases for /metrics", exc_info=True)
        capacity.add_metric([], settings.max_sandboxes)
        yield active
        yield capacity


_registered: set[str] = set()


def register_once(key: str, collector: Collector) -> None:
    if key not in _registered:
        REGISTRY.register(collector)
        _registered.add(key)
