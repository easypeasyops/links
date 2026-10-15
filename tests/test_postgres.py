"""Postgres-specific behaviour that the shared contract suite can't express."""

from datetime import timedelta

import pytest
from alembic import command
from fastapi.testclient import TestClient
from pydantic import HttpUrl
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError

from shortly.api import create_app_from_env
from shortly.models import ShortLink
from shortly.pg_store import PostgresStore
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
