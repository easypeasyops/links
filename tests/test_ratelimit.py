"""Rate limiting: GCRA semantics (contract suite for every limiter), keys, and the API.

The contract tests run against the in-memory and the Postgres limiter with the same fake
clock, so "a burst, then one per interval" means exactly the same in both.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from shortly.api import create_app
from shortly.observability import Metrics
from shortly.pg_store import PostgresRateLimiter
from shortly.ratelimit import Decision, InMemoryRateLimiter, Limit, RateLimiter, client_key
from shortly.service import Shortener
from shortly.store import InMemoryStore, StoreBusyError
from tests.conftest import FakeTime

LIMIT = Limit(per_minute=60, burst=3)  # one per second, three at once


@pytest.fixture
def now() -> FakeTime:
    return FakeTime()


@pytest.fixture(
    params=[pytest.param("memory"), pytest.param("postgres", marks=pytest.mark.integration)]
)
def limiter(request: pytest.FixtureRequest, now: FakeTime) -> RateLimiter:
    if request.param == "postgres":
        engine: Engine = request.getfixturevalue("clean_rate_limits")
        return PostgresRateLimiter(engine, LIMIT, clock=now)
    return InMemoryRateLimiter(LIMIT, clock=now)


def hits(limiter: RateLimiter, n: int, key: str = "a") -> list[bool]:
    return [limiter.hit(key).allowed for _ in range(n)]


# ---------------------------------------------------------------------------- contract


def test_a_quiet_client_gets_its_burst_then_is_refused(limiter: RateLimiter) -> None:
    assert hits(limiter, 4) == [True, True, True, False]


def test_refusal_says_when_to_retry_and_retrying_then_succeeds(
    limiter: RateLimiter, now: FakeTime
) -> None:
    hits(limiter, 3)
    refused = limiter.hit("a")
    assert not refused.allowed
    assert refused.retry_after == pytest.approx(1.0)  # one interval: 60 / 60 per minute
    now.advance(refused.retry_after - 0.01)
    assert not limiter.hit("a").allowed  # a refused request costs nothing...
    now.advance(0.01)
    assert limiter.hit("a").allowed  # ...so the promised time holds exactly


def test_sustained_rate_is_one_per_interval(limiter: RateLimiter, now: FakeTime) -> None:
    hits(limiter, 3)
    allowed = 0
    for _ in range(20):  # 10 s, a request every 0.5 s: only every other one fits
        now.advance(0.5)
        allowed += limiter.hit("a").allowed
    assert allowed == 10


def test_an_idle_client_gets_a_full_burst_again(limiter: RateLimiter, now: FakeTime) -> None:
    hits(limiter, 4)
    now.advance(3600)
    assert hits(limiter, 4) == [True, True, True, False]  # not more: the bucket caps at burst


def test_clients_are_limited_independently(limiter: RateLimiter) -> None:
    hits(limiter, 4, key="a")
    assert hits(limiter, 3, key="b") == [True, True, True]


# ------------------------------------------------------------------------ Postgres only


@pytest.mark.integration
def test_postgres_limit_is_exact_under_concurrency(
    clean_rate_limits: Engine, now: FakeTime
) -> None:
    # The point of doing it in one upsert: concurrent requests from one client, on
    # different connections (= different pods), can't both take the last token.
    limiter = PostgresRateLimiter(clean_rate_limits, Limit(per_minute=60, burst=10), clock=now)
    with ThreadPoolExecutor(max_workers=5) as pool:  # = the engine's pool size
        results = list(pool.map(lambda _: limiter.hit("same").allowed, range(40)))
    assert sum(results) == 10


@pytest.mark.integration
def test_postgres_cleanup_drops_only_expired_rows(clean_rate_limits: Engine, now: FakeTime) -> None:
    limiter = PostgresRateLimiter(clean_rate_limits, LIMIT, clock=now, cleanup_every=60)
    limiter.hit("old")
    now.advance(61)  # "old" has long refilled; the cleanup is due
    limiter.hit("new")
    with clean_rate_limits.connect() as conn:
        keys = conn.execute(text("SELECT key FROM rate_limits")).scalars().all()
    assert keys == ["new"]


@pytest.mark.integration
def test_postgres_limiter_sheds_when_the_pool_is_exhausted(pg_url: str, now: FakeTime) -> None:
    from shortly.db import make_engine

    engine = make_engine(pg_url, pool_size=1, pool_timeout=0.1)
    try:
        limiter = PostgresRateLimiter(engine, LIMIT, clock=now)
        with engine.connect(), pytest.raises(StoreBusyError):  # hold the only connection
            limiter.hit("a")
    finally:
        engine.dispose()


# -------------------------------------------------------------------------- unit pieces


@pytest.mark.parametrize(
    ("host", "key"),
    [
        ("203.0.113.7", "203.0.113.7"),
        ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),
        ("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64"),  # same subscriber
        ("::ffff:203.0.113.7", "203.0.113.7"),  # IPv4 seen through a dual-stack socket
        (None, "unknown"),
        ("testclient", "testclient"),
    ],
)
def test_client_key(host: str | None, key: str) -> None:
    assert client_key(host) == key


@pytest.mark.parametrize(("wait", "header"), [(0.0, "1"), (0.2, "1"), (1.0, "1"), (1.01, "2")])
def test_retry_after_header_rounds_up(wait: float, header: str) -> None:
    assert Decision(allowed=False, retry_after=wait).retry_after_header == header


@pytest.mark.parametrize(("per_minute", "burst"), [(0, 1), (-1, 1), (60, 0)])
def test_limit_rejects_nonsense(per_minute: float, burst: int) -> None:
    with pytest.raises(ValueError, match="per_minute"):
        Limit(per_minute, burst)


def test_in_memory_limiter_forgets_idle_clients(now: FakeTime) -> None:
    limiter = InMemoryRateLimiter(LIMIT, clock=now)
    for i in range(10_001):
        limiter.hit(f"c{i}")
    now.advance(10)
    limiter.hit("trigger")
    assert len(limiter._tats) == 1  # everyone else had refilled: dropped


# ---------------------------------------------------------------------------------- API


def _app(limiter: RateLimiter | None, metrics: Metrics | None = None) -> TestClient:
    app = create_app(
        Shortener(InMemoryStore()), base_url="https://sho.rt", metrics=metrics, rate_limiter=limiter
    )
    return TestClient(app, client=("203.0.113.7", 40000))


def test_api_logs_the_refused_client(now: FakeTime, caplog: pytest.LogCaptureFixture) -> None:
    client = _app(InMemoryRateLimiter(Limit(per_minute=1, burst=1), clock=now))
    with caplog.at_level(logging.INFO, logger="shortly.ratelimit"):
        client.post("/links", json={"url": "https://example.com/"})
        assert not caplog.records  # allowed requests log no address
        client.post("/links", json={"url": "https://example.com/"})
    assert [(r.getMessage(), r.__dict__["client"]) for r in caplog.records] == [
        ("rate limited", "203.0.113.7")
    ]


def test_api_refuses_over_limit_with_429_and_retry_after(now: FakeTime) -> None:
    metrics = Metrics()
    client = _app(InMemoryRateLimiter(LIMIT, clock=now), metrics)
    statuses = [
        client.post("/links", json={"url": "https://example.com/"}).status_code for _ in range(4)
    ]
    assert statuses == [201, 201, 201, 429]
    refused = client.post("/links", json={"url": "https://example.com/"})
    assert refused.headers["retry-after"] == "1"
    assert refused.json() == {"detail": "rate limit exceeded, retry later"}
    assert metrics.registry.get_sample_value("rate_limited_total") == 2
    # Visible as a 4xx in the RED metrics, so it never burns the availability SLO.
    assert (
        metrics.registry.get_sample_value(
            "http_requests_total", {"method": "POST", "route": "/links", "status": "429"}
        )
        == 2
    )


def test_api_limits_creates_only(now: FakeTime) -> None:
    client = _app(InMemoryRateLimiter(Limit(per_minute=1, burst=1), clock=now))
    code = client.post("/links", json={"url": "https://example.com/"}).json()["code"]
    assert client.post("/links", json={"url": "https://example.com/"}).status_code == 429
    assert all(client.get(f"/{code}", follow_redirects=False).status_code == 307 for _ in range(20))


def test_api_keys_on_the_client_address(now: FakeTime) -> None:
    limiter = InMemoryRateLimiter(Limit(per_minute=1, burst=1), clock=now)
    app = create_app(Shortener(InMemoryStore()), base_url="https://sho.rt", rate_limiter=limiter)
    first = TestClient(app, client=("203.0.113.7", 1))
    second = TestClient(app, client=("198.51.100.9", 1))
    assert first.post("/links", json={"url": "https://example.com/"}).status_code == 201
    assert first.post("/links", json={"url": "https://example.com/"}).status_code == 429
    assert second.post("/links", json={"url": "https://example.com/"}).status_code == 201


def test_no_limiter_means_no_limit() -> None:
    client = _app(None)
    assert all(
        client.post("/links", json={"url": "https://example.com/"}).status_code == 201
        for _ in range(50)
    )
