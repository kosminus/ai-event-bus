"""Prometheus metrics for the AI Event Bus."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import contextmanager
from threading import Lock

from fastapi import Request, Response
try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
except ModuleNotFoundError:  # pragma: no cover - exercised indirectly in tests
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

    _FALLBACK_METRICS: list["_MetricBase"] = []
    _FALLBACK_LOCK = Lock()

    class _MetricChild:
        def __init__(self, metric: "_MetricBase", label_values: tuple[str, ...]):
            self._metric = metric
            self._label_values = label_values

        def inc(self, amount: float = 1.0) -> None:
            self._metric._update(self._label_values, amount)

        def observe(self, amount: float) -> None:
            self._metric._update(self._label_values, amount)

    class _MetricBase:
        metric_type = "untyped"

        def __init__(self, name: str, documentation: str, labelnames: list[str] | tuple[str, ...]):
            self.name = name
            self.documentation = documentation
            self.labelnames = tuple(labelnames)
            self.samples: dict[tuple[str, ...], float] = {}
            _FALLBACK_METRICS.append(self)

        def labels(self, **labels: str) -> _MetricChild:
            values = tuple(str(labels.get(label, "")) for label in self.labelnames)
            return _MetricChild(self, values)

        def _update(self, label_values: tuple[str, ...], amount: float) -> None:
            with _FALLBACK_LOCK:
                self.samples[label_values] = self.samples.get(label_values, 0.0) + amount

        def render(self) -> str:
            lines = [
                f"# HELP {self.name} {self.documentation}",
                f"# TYPE {self.name} {self.metric_type}",
            ]
            for label_values, value in self.samples.items():
                if self.labelnames:
                    rendered = ",".join(
                        f'{key}="{val}"' for key, val in zip(self.labelnames, label_values, strict=False)
                    )
                    lines.append(f"{self.name}{{{rendered}}} {value}")
                else:
                    lines.append(f"{self.name} {value}")
            return "\n".join(lines)

    class Counter(_MetricBase):
        metric_type = "counter"

    class Histogram(_MetricBase):
        metric_type = "histogram"

    def generate_latest() -> bytes:
        body = "\n".join(metric.render() for metric in _FALLBACK_METRICS if metric.samples)
        return (body + ("\n" if body else "")).encode("utf-8")

HTTP_REQUESTS_TOTAL = Counter(
    "aiventbus_http_requests_total",
    "Total HTTP requests handled by the app.",
    ["method", "path", "status"],
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "aiventbus_http_request_duration_seconds",
    "Latency of HTTP requests handled by the app.",
    ["method", "path"],
)

EVENTS_PUBLISHED_TOTAL = Counter(
    "aiventbus_events_published_total",
    "Total events published onto the bus.",
    ["topic", "source"],
)
EVENTS_DEDUPED_TOTAL = Counter(
    "aiventbus_events_deduped_total",
    "Total events marked as deduped.",
    ["topic"],
)
EVENTS_CHAIN_LIMIT_TOTAL = Counter(
    "aiventbus_events_chain_limit_total",
    "Total events rejected by chain guardrails.",
    ["reason"],
)
EVENT_PUBLISH_DURATION_SECONDS = Histogram(
    "aiventbus_event_publish_duration_seconds",
    "Latency of EventBus.publish.",
    ["outcome"],
)

ROUTING_DECISIONS_TOTAL = Counter(
    "aiventbus_routing_decisions_total",
    "Routing outcomes for published events.",
    ["result"],
)
ROUTING_DURATION_SECONDS = Histogram(
    "aiventbus_routing_duration_seconds",
    "Latency of AssignmentManager.route_event.",
    ["result"],
)
ASSIGNMENTS_CREATED_TOTAL = Counter(
    "aiventbus_assignments_created_total",
    "Assignments created for agents.",
    ["agent_id", "lane"],
)

AGENT_RUNS_TOTAL = Counter(
    "aiventbus_agent_runs_total",
    "Assignment processing outcomes per agent/model.",
    ["agent_id", "model", "result"],
)
AGENT_RUN_DURATION_SECONDS = Histogram(
    "aiventbus_agent_run_duration_seconds",
    "Latency of LLMAgentConsumer assignment processing.",
    ["agent_id", "model", "result"],
)
LLM_REQUESTS_TOTAL = Counter(
    "aiventbus_llm_requests_total",
    "LLM request outcomes.",
    ["agent_id", "model", "result"],
)
LLM_REQUEST_DURATION_SECONDS = Histogram(
    "aiventbus_llm_request_duration_seconds",
    "Latency of LLM requests.",
    ["agent_id", "model"],
)
LLM_PARSE_FAILURES_TOTAL = Counter(
    "aiventbus_llm_parse_failures_total",
    "Failed parses of structured LLM output.",
    ["agent_id", "model"],
)

ACTION_EXECUTIONS_TOTAL = Counter(
    "aiventbus_action_executions_total",
    "Action execution outcomes.",
    ["agent_id", "action_type", "result"],
)
ACTION_EXECUTION_DURATION_SECONDS = Histogram(
    "aiventbus_action_execution_duration_seconds",
    "Latency of action execution attempts.",
    ["action_type", "result"],
)


def _normalize_label(value: str | None, default: str = "unknown") -> str:
    if not value:
        return default
    return str(value)


def _route_path(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path or request.url.path or "unknown"


async def http_metrics_middleware(request: Request, call_next: Callable) -> Response:
    """Record request count and latency for every HTTP request."""
    path = _route_path(request)
    method = request.method
    start = time.monotonic()
    status = "500"
    try:
        response = await call_next(request)
        status = str(response.status_code)
        return response
    finally:
        duration = time.monotonic() - start
        HTTP_REQUESTS_TOTAL.labels(method=method, path=path, status=status).inc()
        HTTP_REQUEST_DURATION_SECONDS.labels(method=method, path=path).observe(duration)


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@contextmanager
def observe_duration(histogram: Histogram, **labels: str):
    start = time.monotonic()
    try:
        yield
    finally:
        histogram.labels(**labels).observe(time.monotonic() - start)


def record_event_published(topic: str, source: str | None) -> None:
    EVENTS_PUBLISHED_TOTAL.labels(
        topic=_normalize_label(topic),
        source=_normalize_label(source),
    ).inc()


def record_event_deduped(topic: str) -> None:
    EVENTS_DEDUPED_TOTAL.labels(topic=_normalize_label(topic)).inc()


def record_chain_limit(reason: str) -> None:
    EVENTS_CHAIN_LIMIT_TOTAL.labels(reason=_normalize_label(reason)).inc()


def record_routing_result(result: str) -> None:
    ROUTING_DECISIONS_TOTAL.labels(result=_normalize_label(result)).inc()


def record_assignment_created(agent_id: str, lane: str) -> None:
    ASSIGNMENTS_CREATED_TOTAL.labels(
        agent_id=_normalize_label(agent_id),
        lane=_normalize_label(lane),
    ).inc()


def record_llm_request(agent_id: str, model: str, duration_seconds: float, result: str) -> None:
    labels = {
        "agent_id": _normalize_label(agent_id),
        "model": _normalize_label(model),
    }
    LLM_REQUESTS_TOTAL.labels(result=_normalize_label(result), **labels).inc()
    if result == "success":
        LLM_REQUEST_DURATION_SECONDS.labels(**labels).observe(duration_seconds)


def record_llm_parse_failure(agent_id: str, model: str) -> None:
    LLM_PARSE_FAILURES_TOTAL.labels(
        agent_id=_normalize_label(agent_id),
        model=_normalize_label(model),
    ).inc()


def record_agent_run(agent_id: str, model: str, result: str, duration_seconds: float) -> None:
    labels = {
        "agent_id": _normalize_label(agent_id),
        "model": _normalize_label(model),
        "result": _normalize_label(result),
    }
    AGENT_RUNS_TOTAL.labels(**labels).inc()
    AGENT_RUN_DURATION_SECONDS.labels(**labels).observe(duration_seconds)


def record_action_execution(agent_id: str, action_type: str, result: str, duration_seconds: float) -> None:
    total_labels = {
        "agent_id": _normalize_label(agent_id),
        "action_type": _normalize_label(action_type),
        "result": _normalize_label(result),
    }
    ACTION_EXECUTIONS_TOTAL.labels(**total_labels).inc()
    ACTION_EXECUTION_DURATION_SECONDS.labels(
        action_type=_normalize_label(action_type),
        result=_normalize_label(result),
    ).observe(duration_seconds)
