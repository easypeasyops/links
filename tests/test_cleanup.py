"""The expired-link cleanup job: batching, bounds, and the real thing against Postgres."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text

from shortly.cleanup import CleanupResult, delete_expired, main
from shortly.models import ShortLink
from shortly.pg_store import PostgresStore
from shortly.store import InMemoryStore
from tests.conftest import T0


def expired(code: str, days_ago: float) -> ShortLink:
    return ShortLink.model_validate(
        {
            "code": code,
            "target": "https://example.com/",
            "created_at": T0 - timedelta(days=days_ago + 1),
            "expires_at": T0 - timedelta(days=days_ago),
        }
    )


def test_deletes_in_batches_until_a_short_batch() -> None:
    store = InMemoryStore()
    for i in range(25):
        store.add(expired(f"c{i:06d}", days_ago=10))
    pauses: list[None] = []
    result = delete_expired(
        store,
        grace=timedelta(days=7),
        batch_size=10,
        max_batches=100,
        clock=lambda: T0,
        pause=lambda: pauses.append(None),
    )
    assert result == CleanupResult(deleted=25, batches=3, complete=True)
    assert len(pauses) == 2  # between batches, not after the last


def test_grace_period_keeps_recently_expired_links() -> None:
    store = InMemoryStore()
    store.add(expired("recent1", days_ago=3))  # still answers 410 Gone
    store.add(expired("ancient", days_ago=30))
    result = delete_expired(
        store, grace=timedelta(days=7), batch_size=10, max_batches=5, clock=lambda: T0
    )
    assert result.deleted == 1
    assert store.get("recent1") is not None


def test_stops_at_max_batches_and_says_so() -> None:
    store = InMemoryStore()
    for i in range(50):
        store.add(expired(f"c{i:06d}", days_ago=10))
    result = delete_expired(
        store,
        grace=timedelta(0),
        batch_size=10,
        max_batches=2,
        clock=lambda: T0,
        pause=lambda: None,
    )
    assert result == CleanupResult(deleted=20, batches=2, complete=False)  # the next run goes on


# ------------------------------------------------------------------------- Postgres


@pytest.mark.integration
def test_delete_can_use_the_partial_index(pg_store: PostgresStore, pg_engine: Engine) -> None:
    # A tiny test table always gets a sequential scan, so forbid that and see whether the
    # planner still finds an index for the cleanup query. If the index or the query drift
    # apart, it can't, and every cleanup batch on a big table would scan all of it.
    with pg_engine.connect() as conn:
        conn.execute(text("SET enable_seqscan = off"))
        plan = "\n".join(
            conn.execute(
                text(
                    "EXPLAIN SELECT code FROM links WHERE expires_at < now()"
                    " ORDER BY expires_at LIMIT 1000"
                )
            ).scalars()
        )
    assert "ix_links_expires_at" in plan, plan


@pytest.mark.integration
@pytest.mark.usefixtures("restore_logging")
def test_main_against_postgres(
    pg_url: str,
    pg_store: PostgresStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for i in range(3):
        pg_store.add(expired(f"old{i:04d}", days_ago=400))  # long past any grace period
    # main() uses the real clock: "recent" must be recent relative to now, not to T0.
    now = datetime.now(UTC)
    pg_store.add(
        ShortLink.model_validate(
            {"code": "recent1", "target": "https://example.com/",
             "created_at": now - timedelta(days=2), "expires_at": now - timedelta(days=1)}
        )
    )  # fmt: skip
    monkeypatch.setenv("SHORTLY_DATABASE_URL", pg_url)
    monkeypatch.setenv("SHORTLY_CLEANUP_BATCH_SIZE", "2")
    assert main() == 0
    assert all(pg_store.get(f"old{i:04d}") is None for i in range(3))
    assert pg_store.get("recent1") is not None  # inside the 7-day grace period
    finished = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if "cleanup" in line
    ]
    assert finished[-1]["msg"] == "cleanup finished"
    assert finished[-1]["deleted"] == 3
    assert finished[-1]["batches"] == 2
    assert finished[-1]["complete"] is True


@pytest.mark.integration
@pytest.mark.usefixtures("restore_logging")
def test_main_fails_loudly_without_a_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SHORTLY_DATABASE_URL", "postgresql://u:p@127.0.0.1:1/none")
    assert main() == 1  # the Job fails: the CronJob's last success goes stale, and alerts
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(e["msg"] == "cleanup failed" and e["level"] == "error" for e in lines)
