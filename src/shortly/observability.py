"""Logs, metrics, and traces — the three signals, wired so they point at each other.

* Every log line is one JSON object carrying the request ID and (when tracing is on) the
  trace ID, so you can jump from a log line to its trace and to every sibling log line.
* Metrics use the *route template* (`/{code}`), never the raw path, as a label. Raw paths
  would create one time series per short code — unbounded cardinality that eventually
  takes down Prometheus.
* Tracing is opt-in via the standard OTEL_EXPORTER_OTLP_ENDPOINT variable, so local runs
  and tests don't need a collector.
"""

import json
import logging
import os
import re
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Final

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER: Final = "x-request-id"
# Accept a caller-supplied ID only if it's short and boring. Anything else could be used
# for log injection or to blow up log storage, so we replace it.
_VALID_REQUEST_ID: Final = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# Probe and scrape traffic is frequent and uninteresting; log it at DEBUG, not INFO.
_QUIET_PATHS: Final = frozenset({"/healthz", "/readyz", "/metrics"})

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

log = logging.getLogger("shortly.access")

# ------------------------------------------------------------------------------ logging

_STANDARD_ATTRS: Final = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    | {"message", "asctime", "color_message"}  # uvicorn adds an ANSI-colored duplicate of msg
)


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Fields passed via ``extra=`` become top-level keys."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if (rid := request_id_var.get()) is not None:
            entry["request_id"] = rid
        span_ctx = trace.get_current_span().get_span_context()
        if span_ctx.is_valid:
            entry["trace_id"] = format(span_ctx.trace_id, "032x")
            entry["span_id"] = format(span_ctx.span_id, "016x")
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Route everything — ours, uvicorn's, SQLAlchemy's — through one stdout handler.

    Containers log to stdout; the platform (Docker, Kubernetes, a log shipper) collects
    it. Writing files inside a container is an anti-pattern (and our FS is read-only).
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if json_output else logging.Formatter("%(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # uvicorn installs its own handlers; strip them so its logs become JSON too.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    # We emit our own access log with route templates and request IDs.
    logging.getLogger("uvicorn.access").disabled = True


# ------------------------------------------------------------------------------ metrics


class Metrics:
    """All metrics on one registry. A registry per app keeps tests isolated."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self.requests = Counter(
            "http_requests",
            "HTTP requests by route template and status.",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "http_request_duration_seconds",
            "HTTP request latency.",
            ["method", "route"],
            # Bucket edges chosen around the SLO: a redirect should take < 50ms.
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
            registry=self.registry,
        )
        self.links_created = Counter(
            "links_created", "Short links created.", registry=self.registry
        )
        self.resolutions = Counter(
            "link_resolutions",
            "Redirect lookups by outcome.",
            ["outcome"],  # hit | not_found | expired — a small, fixed set
            registry=self.registry,
        )
        self.code_collisions = Counter(
            "code_collisions",
            "Generated codes that were already taken. A rising rate means the code space "
            "is filling up — time to lengthen codes.",
            registry=self.registry,
        )
        # Saturation signals for the DB pool. in_use / size near 1 with rising
        # load_shed is the "add replicas" signal; see docs/RUNBOOK.md.
        self.db_pool_size = Gauge(
            "db_pool_size", "Connections in this process's DB pool.", registry=self.registry
        )
        self.db_pool_in_use = Gauge(
            "db_pool_in_use", "DB pool connections checked out now.", registry=self.registry
        )
        self.load_shed = Counter(
            "load_shed",
            "Requests rejected with 503 because the pod or the database was saturated.",
            # pool: no free DB connection in time; db_timeout: a statement or lock wait ran
            # past its timeout; concurrency: the pod was at its in-flight request limit.
            ["reason"],
            registry=self.registry,
        )
        for reason in ("pool", "db_timeout", "concurrency"):
            self.load_shed.labels(reason)  # export 0 for each, so rate() works from the start
        self.in_flight = Gauge(
            "requests_in_flight",
            "Requests this pod is working on now (probes and /metrics excluded).",
            registry=self.registry,
        )
        self.rate_limited = Counter(
            "rate_limited",
            "POST /links requests refused with 429 because the client was over its limit.",
            registry=self.registry,
        )


# ------------------------------------------------------------------------------ middleware


def _route_template(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


class RequestContextMiddleware:
    """Pure ASGI middleware: request ID, access log, and RED metrics for every request.

    (Pure ASGI rather than Starlette's BaseHTTPMiddleware: no extra task, so context
    variables and streaming responses behave normally.)
    """

    def __init__(self, app: ASGIApp, metrics: Metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(REQUEST_ID_HEADER.encode(), b"").decode("latin-1")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        status = 500  # if the app raises before sending a response, that's what the client sees
        start = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER.encode(), request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            route = _route_template(scope)
            method = scope["method"]
            self.metrics.requests.labels(method, route, str(status)).inc()
            self.metrics.latency.labels(method, route).observe(elapsed)
            log.log(
                logging.DEBUG if scope["path"] in _QUIET_PATHS else logging.INFO,
                "request",
                extra={
                    "method": method,
                    "route": route,
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 2),
                },
            )
            request_id_var.reset(token)


# ------------------------------------------------------------------------------ tracing


def tracer_provider_from_env(service_name: str) -> TracerProvider | None:
    """A provider exporting via OTLP/HTTP if OTEL_EXPORTER_OTLP_ENDPOINT is set, else None.

    The exporter reads endpoint, headers, and timeouts from the standard OTEL_* env vars,
    so pointing at Jaeger, Tempo, Honeycomb, or a collector is configuration, not code.
    Call ``provider.shutdown()`` on exit to flush buffered spans — the app's lifespan does.
    """
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return None

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    return provider
