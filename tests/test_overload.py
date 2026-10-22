"""The concurrency limit: shed past the limit, fast, and still count against the SLO."""

import threading

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

from shortly.api import create_app
from shortly.models import ShortLink
from shortly.observability import Metrics
from shortly.overload import ConcurrencyLimitMiddleware
from shortly.service import Shortener
from shortly.store import InMemoryStore
from tests.conftest import FakeClock

BASE = "http://sho.rt"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def call(app: ConcurrencyLimitMiddleware, path: str) -> tuple[int, dict[str, str]]:
    """One GET through a bare ASGI app; returns the status and the response headers."""
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
    }
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    headers = {k.decode(): v.decode() for k, v in start.get("headers", [])}
    return start["status"], headers


@pytest.mark.anyio
async def test_sheds_past_the_limit_and_recovers() -> None:
    release, both_in = anyio.Event(), anyio.Event()
    started = 0

    async def slow_app(scope, receive, send):  # type: ignore[no-untyped-def]
        nonlocal started
        started += 1
        if started == 2:
            both_in.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    shed: list[None] = []
    counts: list[int] = []
    app = ConcurrencyLimitMiddleware(
        slow_app, limit=2, on_shed=lambda: shed.append(None), on_count=counts.append
    )
    async with anyio.create_task_group() as tg:
        for _ in range(2):
            tg.start_soon(call, app, "/a")
        await both_in.wait()  # both admitted and in flight
        status, headers = await call(app, "/a")  # third: over the limit
        assert status == 503
        assert headers["retry-after"] == "1"
        assert len(shed) == 1
        release.set()
    assert (await call(app, "/a"))[0] == 200  # room again
    assert max(counts) == 2
    assert counts[-1] == 0


@pytest.mark.anyio
async def test_probes_and_metrics_are_never_shed() -> None:
    async def ok(scope, receive, send):  # type: ignore[no-untyped-def]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = ConcurrencyLimitMiddleware(ok, limit=1)
    app.in_flight = 1  # as if one request were running: the pod is full
    for path in ("/healthz", "/readyz", "/metrics"):
        assert (await call(app, path))[0] == 200
    assert (await call(app, "/abc1234"))[0] == 503


def test_shed_requests_count_against_the_slo_route(clock: FakeClock) -> None:
    # A shed redirect must be a 503 on route="/{code}" (what the SLO selects), not on
    # "unmatched": otherwise shedding would fail users without burning the error budget.
    entered, release = threading.Event(), threading.Event()

    class SlowStore(InMemoryStore):
        def get(self, code: str) -> ShortLink | None:
            entered.set()
            release.wait(5)
            return None

    metrics = Metrics()
    app = create_app(
        Shortener(SlowStore(), clock=clock),
        base_url=BASE,
        metrics=metrics,
        max_concurrent_requests=1,
    )
    with TestClient(app) as c:
        first = threading.Thread(target=c.get, args=("/abc1234",))
        first.start()
        assert entered.wait(5)  # the first request holds the only slot
        resp = c.get("/xyz9876")
        release.set()
        first.join(5)
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "1"
    assert resp.headers["x-request-id"]  # still went through the request middleware
    sample = metrics.registry.get_sample_value
    labels = {"method": "GET", "route": "/{code}", "status": "503"}
    assert sample("http_requests_total", labels) == 1
    assert sample("load_shed_total", {"reason": "concurrency"}) == 1
    assert sample("requests_in_flight") == 0


def test_limit_off_by_default_in_create_app(clock: FakeClock) -> None:
    c = TestClient(create_app(Shortener(InMemoryStore(), clock=clock), base_url=BASE))
    assert c.get("/healthz").status_code == 200
