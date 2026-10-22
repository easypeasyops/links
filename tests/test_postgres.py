"""Postgres-specific behaviour that the shared contract suite can't express."""

import socket
import time
import urllib.request
from datetime import timedelta

import pytest
from alembic import command
from fastapi.testclient import TestClient
from pydantic import HttpUrl
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from shortly.api import create_app, create_app_from_env
from shortly.db import make_engine, pool_in_use
from shortly.models import ShortLink
from shortly.pg_store import PostgresStore
from shortly.service import Shortener
from shortly.store import StoreBusyError
from tests.conftest import T0, alembic_config

pytestmark = pytest.mark.integration


def test_models_match_migrations(pg_url: str) -> None:
    # Fails if someone edits db.py without generating a migration — the classic
    # "works in tests (create_all) but prod schema is different" bug.
    command.check(alembic_config(pg_url))


def test_migrations_downgrade_and_reapply(pg_url: str) -> None:
    cfg = alembic_config(pg_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


def test_check_violation_is_not_treated_as_collision(pg_store: PostgresStore) -> None:
    # model_construct skips validation, so we can hand the store a link the DB must reject.
    # The store must re-raise it, NOT map it to CodeConflictError — otherwise the service
    # would "retry" a request that can never succeed.
    invalid = ShortLink.model_construct(
        code="bad0001",
        target=HttpUrl("https://example.com/"),
        created_at=T0,
        expires_at=T0 - timedelta(seconds=1),
    )
    with pytest.raises(IntegrityError, match="expiry_after_creation"):
        pg_store.add(invalid)


def test_ping_false_when_db_unreachable() -> None:
    dead = create_engine("postgresql+psycopg://u:p@127.0.0.1:1/none")
    assert PostgresStore(dead).ping() is False


@pytest.mark.usefixtures("restore_logging")  # create_app_from_env reconfigures logging
def test_production_wiring_end_to_end(
    pg_url: str, pg_store: PostgresStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercises the exact factory uvicorn runs: env -> Settings -> engine -> app.
    monkeypatch.setenv("SHORTLY_DATABASE_URL", pg_url)
    monkeypatch.setenv("SHORTLY_METRICS_PORT", "0")  # /metrics on the app port here
    monkeypatch.setenv("SHORTLY_BASE_URL", "https://sho.rt")
    with TestClient(create_app_from_env(), follow_redirects=False) as client:  # runs lifespan
        assert client.get("/readyz").status_code == 200
        code = client.post("/links", json={"url": "https://example.com/x"}).json()["code"]
        assert client.get(f"/{code}").headers["location"] == "https://example.com/x"
    assert pg_store.get(code) is not None  # really persisted, not in memory


def test_db_queries_are_child_spans_of_the_request(pg_url: str, pg_store: PostgresStore) -> None:
    # The question tracing answers: "this request was slow — which query?" That needs
    # SQL spans in the same trace as the HTTP span. This test exists because the first
    # attempt (SQLAlchemy instrumentation) produced *no* spans and raised no error.
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    PsycopgInstrumentor().instrument(tracer_provider=provider)
    engine = make_engine(pg_url)  # fresh engine, so its connections are instrumented
    app = create_app(
        Shortener(PostgresStore(engine)), base_url="https://sho.rt", tracer_provider=provider
    )
    try:
        TestClient(app, follow_redirects=False).get("/nope")
    finally:
        engine.dispose()
        PsycopgInstrumentor().uninstrument()  # global patch: undo it for other tests
        provider.shutdown()

    spans = exporter.get_finished_spans()
    (server,) = [s for s in spans if s.kind.name == "SERVER"]
    sql = [s for s in spans if s.kind.name == "CLIENT" and s.name.startswith("SELECT")]
    assert sql, f"no SQL spans; got {[s.name for s in spans]}"
    assert server.context is not None
    for s in sql:
        assert s.context is not None
        assert s.context.trace_id == server.context.trace_id  # same trace as the request


def test_app_engine_autocommits_each_statement(pg_url: str, pg_store: PostgresStore) -> None:
    # Autocommit is what saves two round trips per request (no BEGIN/ROLLBACK). Prove the
    # behaviour, not the flag: a write is visible to another connection with no commit().
    engine = make_engine(pg_url)
    try:
        with engine.connect() as conn:
            assert conn.connection.dbapi_connection.autocommit  # type: ignore[union-attr]
            conn.execute(
                text("INSERT INTO links (code, target, created_at) VALUES (:c, :t, now())"),
                {"c": "ac1", "t": "https://a.example/"},
            )
            assert pg_store.get("ac1") is not None  # seen from the store's own pool
    finally:
        engine.dispose()


def test_exhausted_pool_raises_busy_fast(pg_url: str) -> None:
    engine = make_engine(pg_url, pool_size=1, pool_timeout=0.1)
    store = PostgresStore(engine)
    try:
        with engine.connect():  # hold the only connection
            start = time.perf_counter()
            with pytest.raises(StoreBusyError):
                store.get("anything")
            assert time.perf_counter() - start < 1  # fails fast, not after 30 s
        assert pool_in_use(engine)() == 0
        assert store.get("anything") is None  # recovers once a connection is free
    finally:
        engine.dispose()


def test_pool_in_use_counts_checked_out_connections(pg_url: str) -> None:
    engine = make_engine(pg_url, pool_size=2)
    read = pool_in_use(engine)
    try:
        with engine.connect(), engine.connect():
            assert read() == 2
        assert read() == 0
        assert pool_in_use(make_engine(pg_url, pooled=False))() == 0
    finally:
        engine.dispose()


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize(("per_minute", "expected"), [("1", [201, 429]), ("0", [201, 201])])
def test_production_wiring_rate_limits_from_env(
    pg_url: str,
    pg_store: PostgresStore,
    monkeypatch: pytest.MonkeyPatch,
    per_minute: str,
    expected: list[int],
) -> None:
    monkeypatch.setenv("SHORTLY_DATABASE_URL", pg_url)
    monkeypatch.setenv("SHORTLY_METRICS_PORT", "0")  # /metrics on the app port here
    monkeypatch.setenv("SHORTLY_RATE_LIMIT_CREATES_PER_MINUTE", per_minute)  # 0 = off
    monkeypatch.setenv("SHORTLY_RATE_LIMIT_BURST", "1")
    with TestClient(create_app_from_env()) as client:
        statuses = [
            client.post("/links", json={"url": "https://example.com/"}).status_code
            for _ in range(2)
        ]
    assert statuses == expected


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.mark.usefixtures("restore_logging")
def test_metrics_on_their_own_port_and_not_on_the_app_port(
    pg_url: str, pg_store: PostgresStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _free_port()
    monkeypatch.setenv("SHORTLY_DATABASE_URL", pg_url)
    monkeypatch.setenv("SHORTLY_METRICS_PORT", str(port))
    with TestClient(create_app_from_env()) as client:
        client.post("/links", json={"url": "https://example.com/m"})
        # The public port no longer serves metrics: "/metrics" is just an unknown code.
        assert client.get("/metrics").status_code == 404
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
            body = resp.read().decode()
        assert "links_created_total 1.0" in body  # the same registry the app writes to
    # Stopped with the app: nothing listens on the port any more.
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
