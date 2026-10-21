import io
import json
import logging
import sys

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from shortly.api import create_app
from shortly.observability import (
    JsonFormatter,
    Metrics,
    configure_logging,
    request_id_var,
    tracer_provider_from_env,
)
from shortly.service import Shortener
from shortly.store import InMemoryStore
from tests.conftest import FakeClock, scripted_codes

BASE = "https://sho.rt"


@pytest.fixture
def metrics() -> Metrics:
    return Metrics()  # private registry per test: no cross-test leakage


@pytest.fixture
def client(shortener: Shortener, metrics: Metrics) -> TestClient:
    return TestClient(create_app(shortener, base_url=BASE, metrics=metrics), follow_redirects=False)


def sample(metrics: Metrics, name: str, **labels: str) -> float:
    value = metrics.registry.get_sample_value(name, labels)
    return 0.0 if value is None else value


# --- request IDs ----------------------------------------------------------------------


def test_generates_request_id(client: TestClient) -> None:
    rid = client.get("/healthz").headers["x-request-id"]
    assert len(rid) == 32  # uuid4 hex


def test_propagates_valid_caller_request_id(client: TestClient) -> None:
    resp = client.get("/healthz", headers={"x-request-id": "edge-abc.123"})
    assert resp.headers["x-request-id"] == "edge-abc.123"


@pytest.mark.parametrize("bad", ["has space", "x" * 65, "new\nline", "<script>"])
def test_replaces_unsafe_caller_request_id(client: TestClient, bad: str) -> None:
    rid = client.get("/healthz", headers={"x-request-id": bad}).headers["x-request-id"]
    assert rid != bad
    assert len(rid) == 32


def test_request_id_does_not_leak_between_requests(client: TestClient) -> None:
    client.get("/healthz", headers={"x-request-id": "first"})
    assert request_id_var.get() is None  # reset after the request, not left in context


# --- metrics --------------------------------------------------------------------------


def test_metrics_endpoint_is_prometheus_text(client: TestClient) -> None:
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "# TYPE http_requests_total counter" in resp.text


def test_route_label_is_template_not_raw_path(client: TestClient, metrics: Metrics) -> None:
    # 50 different codes must produce ONE time series, not 50. This is the cardinality
    # guard: raw paths as labels would grow Prometheus memory with every link ever made.
    for i in range(50):
        client.get(f"/missing{i}")
    assert sample(metrics, "http_requests_total", method="GET", route="/{code}", status="404") == 50
    series = [
        s
        for m in metrics.registry.collect()
        if m.name == "http_requests"
        for s in m.samples
        if s.name == "http_requests_total"
    ]
    assert len(series) == 1


def test_unmatched_paths_share_one_label(client: TestClient, metrics: Metrics) -> None:
    client.get("/a/b/c")
    client.get("/x/y")
    assert (
        sample(metrics, "http_requests_total", method="GET", route="unmatched", status="404") == 2
    )


def test_domain_counters(client: TestClient, metrics: Metrics) -> None:
    code = client.post("/links", json={"url": "https://example.com/"}).json()["code"]
    client.get(f"/{code}")
    client.get("/nope")
    assert sample(metrics, "links_created_total") == 1
    assert sample(metrics, "link_resolutions_total", outcome="hit") == 1
    assert sample(metrics, "link_resolutions_total", outcome="not_found") == 1


def test_latency_histogram_observed(client: TestClient, metrics: Metrics) -> None:
    client.get("/healthz")
    count = sample(metrics, "http_request_duration_seconds_count", method="GET", route="/healthz")
    assert count == 1


def test_collision_counter(clock: FakeClock, metrics: Metrics) -> None:
    svc = Shortener(
        InMemoryStore(),
        code_gen=scripted_codes("dup", "dup", "dup", "ok"),
        clock=clock,
        on_collision=metrics.code_collisions.inc,
    )
    c = TestClient(create_app(svc, base_url=BASE, metrics=metrics))
    c.post("/links", json={"url": "https://a.example/"})
    c.post("/links", json={"url": "https://b.example/"})
    assert sample(metrics, "code_collisions_total") == 2


def test_unhandled_error_counted_as_500(clock: FakeClock, metrics: Metrics) -> None:
    class BrokenStore(InMemoryStore):
        def get(self, code: str) -> None:
            raise RuntimeError("boom")

    app = create_app(Shortener(BrokenStore(), clock=clock), base_url=BASE, metrics=metrics)
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/anything").status_code == 500
    assert sample(metrics, "http_requests_total", method="GET", route="/{code}", status="500") == 1


# --- logging --------------------------------------------------------------------------


def test_json_formatter_shape() -> None:
    record = logging.LogRecord("shortly.x", logging.INFO, __file__, 1, "hello %s", ("w",), None)
    record.route = "/{code}"
    token = request_id_var.set("rid-1")
    try:
        entry = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert entry["msg"] == "hello w"
    assert entry["level"] == "info"
    assert entry["request_id"] == "rid-1"
    assert entry["route"] == "/{code}"  # extra= fields become top-level keys
    assert entry["ts"].endswith("+00:00")  # UTC, explicit offset


def test_access_log_line(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="shortly.access"):
        client.get("/nope", headers={"x-request-id": "trace-me"})
    (record,) = [r for r in caplog.records if r.name == "shortly.access"]
    assert record.__dict__["route"] == "/{code}"
    assert record.__dict__["status"] == 404
    assert record.__dict__["duration_ms"] >= 0


def test_probe_requests_log_at_debug(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="shortly.access"):
        client.get("/healthz")
        client.get("/metrics")
    assert not [r for r in caplog.records if r.name == "shortly.access"]


@pytest.mark.usefixtures("restore_logging")
def test_configure_logging_emits_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    logging.getLogger("uvicorn.error").info("from uvicorn", extra={"color_message": "\x1b[36mx"})
    entry = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert entry["logger"] == "uvicorn.error"  # third-party logs are JSON too
    assert "color_message" not in entry  # no ANSI escape codes in structured logs


# --- tracing --------------------------------------------------------------------------


def test_request_produces_span_and_log_correlation(shortener: Shortener, metrics: Metrics) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    app = create_app(shortener, base_url=BASE, metrics=metrics, tracer_provider=provider)
    c = TestClient(app, follow_redirects=False)

    # A real handler that formats at emit time, like production's stdout handler.
    # (caplog formats later, after the span has ended, so it can't test this.)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    access = logging.getLogger("shortly.access")
    access.addHandler(handler)
    access.setLevel(logging.INFO)
    try:
        c.get("/nope")
        c.get("/healthz")  # excluded from tracing: probes would drown real traffic
    finally:
        access.removeHandler(handler)
        access.setLevel(logging.NOTSET)
        provider.shutdown()

    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["GET /{code}"]  # one span: no send/receive noise
    server_spans = [s for s in spans if s.kind.name == "SERVER"]
    span = server_spans[0]
    assert span.attributes is not None
    assert span.attributes["http.route"] == "/{code}"
    assert span.context is not None

    # The access log line for that request carries the same trace ID: logs <-> traces.
    entry = json.loads(buf.getvalue().splitlines()[0])
    assert entry["route"] == "/{code}"
    assert entry["trace_id"] == format(span.context.trace_id, "032x")


def test_tracing_is_off_without_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert tracer_provider_from_env("shortly") is None


def test_tracing_on_with_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    provider = tracer_provider_from_env("shortly-test")
    assert provider is not None
    assert provider.resource.attributes["service.name"] == "shortly-test"
    provider.shutdown()  # nothing buffered, so no export attempt


def test_json_formatter_includes_exception() -> None:
    try:
        raise ValueError("bad")
    except ValueError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    entry = json.loads(JsonFormatter().format(record))
    assert "ValueError: bad" in entry["exc"]
