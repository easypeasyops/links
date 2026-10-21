import asyncio
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from shortly.api import create_app
from shortly.observability import Metrics
from shortly.service import Shortener
from shortly.store import InMemoryStore, StoreBusyError
from tests.conftest import FakeClock

BASE = "https://sho.rt"


@pytest.fixture
def client(shortener: Shortener) -> TestClient:
    return TestClient(create_app(shortener, base_url=BASE), follow_redirects=False)


def test_create_and_follow(client: TestClient) -> None:
    resp = client.post("/links", json={"url": "https://example.com/page"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["short_url"] == f"{BASE}/{body['code']}"
    assert body["expires_at"] is None

    hop = client.get(f"/{body['code']}")
    assert hop.status_code == 307  # temporary: never cached forever by browsers
    assert hop.headers["location"] == "https://example.com/page"


@pytest.mark.parametrize(
    "payload",
    [
        {"url": "not-a-url"},
        {"url": "javascript:alert(1)"},
        {"url": "https://example.com/", "ttl_seconds": 0},
        {"url": "https://example.com/", "ttl_seconds": 10**9},
        {},
    ],
)
def test_rejects_bad_input(client: TestClient, payload: dict[str, object]) -> None:
    assert client.post("/links", json=payload).status_code == 422


def test_unknown_code_is_404(client: TestClient) -> None:
    assert client.get("/nope").status_code == 404


def test_expired_link_is_410(client: TestClient, clock: FakeClock) -> None:
    code = client.post("/links", json={"url": "https://example.com/", "ttl_seconds": 60}).json()[
        "code"
    ]
    clock.advance(timedelta(seconds=60))
    assert client.get(f"/{code}").status_code == 410


def test_exhausted_code_space_is_503(clock: FakeClock) -> None:
    svc = Shortener(InMemoryStore(), code_gen=lambda: "same", clock=clock, max_attempts=2)
    c = TestClient(create_app(svc, base_url=BASE))
    assert c.post("/links", json={"url": "https://a.example/"}).status_code == 201
    assert c.post("/links", json={"url": "https://b.example/"}).status_code == 503


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_readyz_reflects_store(clock: FakeClock) -> None:
    class DownStore(InMemoryStore):
        def ping(self) -> bool:
            return False

    up = TestClient(create_app(Shortener(InMemoryStore(), clock=clock), base_url=BASE))
    down = TestClient(create_app(Shortener(DownStore(), clock=clock), base_url=BASE))
    assert up.get("/readyz").status_code == 200
    assert down.get("/readyz").status_code == 503


def test_saturated_store_is_shed_with_503_and_retry_after(clock: FakeClock) -> None:
    class BusyStore(InMemoryStore):
        def get(self, code: str) -> None:
            raise StoreBusyError("pool exhausted")

    metrics = Metrics()
    c = TestClient(
        create_app(Shortener(BusyStore(), clock=clock), base_url=BASE, metrics=metrics),
        follow_redirects=False,
    )
    resp = c.get("/abc1234")
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "1"
    assert metrics.registry.get_sample_value("load_shed_total") == 1


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_probes_and_metrics_answer_while_the_threadpool_is_saturated(
    clock: FakeClock,
) -> None:
    # Regression: sync probe handlers queued behind request work in the threadpool (40
    # threads); under overload they timed out and kubelet restarted every pod.
    import httpx2 as httpx

    class SlowStore(InMemoryStore):
        def get(self, code: str) -> None:
            time.sleep(1.0)  # a request stuck on a slow dependency, holding its thread

    app = create_app(Shortener(SlowStore(), clock=clock), base_url=BASE)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        busy = [asyncio.create_task(client.get(f"/c{i}")) for i in range(60)]  # > 40 threads
        await asyncio.sleep(0.2)  # let them take every thread
        timings = {}
        for path in ("/healthz", "/readyz", "/metrics"):
            start = time.perf_counter()
            assert (await client.get(path)).status_code == 200
            timings[path] = time.perf_counter() - start
        await asyncio.gather(*busy)
    assert all(t < 0.5 for t in timings.values()), timings  # queued, they'd wait about 1 s
