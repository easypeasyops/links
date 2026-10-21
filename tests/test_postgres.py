"""Postgres-specific behaviour that the shared contract suite can't express."""

from datetime import timedelta

import pytest
from alembic import command
from fastapi.testclient import TestClient
from pydantic import HttpUrl
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError

from shortly.api import create_app, create_app_from_env
from shortly.db import make_engine
from shortly.models import ShortLink
from shortly.pg_store import PostgresStore
from shortly.service import Shortener
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
