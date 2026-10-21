"""HTTP layer. Thin by design: parse, call the service, map results to status codes.

No business logic lives here, so the service stays testable without HTTP.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated

import anyio
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from opentelemetry.sdk.trace import TracerProvider
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import AwareDatetime, BaseModel, Field, HttpUrl

from shortly.observability import Metrics, RequestContextMiddleware
from shortly.service import (
    CodeSpaceExhaustedError,
    LinkExpiredError,
    LinkNotFoundError,
    Shortener,
)
from shortly.store import StoreBusyError

MAX_TTL_SECONDS = 365 * 24 * 3600


class CreateLink(BaseModel):
    url: HttpUrl
    ttl_seconds: Annotated[int, Field(gt=0, le=MAX_TTL_SECONDS)] | None = None


class LinkOut(BaseModel):
    code: str
    short_url: str
    target: HttpUrl
    expires_at: AwareDatetime | None


def create_app(
    shortener: Shortener,
    *,
    base_url: str,
    metrics: Metrics | None = None,
    tracer_provider: TracerProvider | None = None,
    on_shutdown: Callable[[], None] = lambda: None,
) -> FastAPI:
    """Build the app around an already-wired ``Shortener``.

    Taking dependencies as arguments (instead of reading globals or env vars here) is
    what lets tests build the app with an in-memory store, a fake clock, a private
    metrics registry, and an in-memory span exporter.
    """
    metrics = metrics or Metrics()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        on_shutdown()  # runs after uvicorn has drained in-flight requests
        if tracer_provider is not None:
            tracer_provider.shutdown()  # flush buffered spans before the process exits

    app = FastAPI(title="shortly", lifespan=lifespan)
    probe_limiter = anyio.CapacityLimiter(1)  # see readyz
    base = base_url.rstrip("/")

    # Saturated backend: fail fast and tell well-behaved clients when to come back,
    # rather than holding the request until it times out somewhere upstream.
    @app.exception_handler(StoreBusyError)
    def shed(_: Request, __: StoreBusyError) -> JSONResponse:
        metrics.load_shed.inc()
        return JSONResponse(
            {"detail": "overloaded, retry shortly"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Retry-After": "1"},
        )

    # Probes are separate on purpose. Liveness: "is the process wedged?" — restarting
    # fixes that. Readiness: "can I take traffic?" — a DB blip should pull the pod out of
    # rotation, NOT restart it (restarting every pod during a DB outage makes it worse).
    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        # `async def`: answered on the event loop, never queued behind request work in the
        # threadpool. A sync handler here timed out under overload, kubelet restarted every
        # pod, and the outage fed itself (found by the CI autoscaling drill).
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> dict[str, str]:
        # The DB ping blocks, so it runs in a thread, but on its own one-thread limiter,
        # not the request threadpool: an overloaded pod still answers "can I reach the DB?"
        ready = await anyio.to_thread.run_sync(shortener.is_ready, limiter=probe_limiter)
        if not ready:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "not ready")
        return {"status": "ready"}

    # Prometheus scrapes this. Not authenticated here: in production it should only be
    # reachable on the cluster network (a separate port or a NetworkPolicy), not the edge.
    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        # On the event loop for the same reason as the probes: a scrape that queues behind
        # overloaded requests times out, leaving a gap in the metrics exactly when needed.
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.post("/links", status_code=status.HTTP_201_CREATED)
    def create_link(body: CreateLink) -> LinkOut:
        ttl = timedelta(seconds=body.ttl_seconds) if body.ttl_seconds else None
        try:
            link = shortener.shorten(str(body.url), ttl=ttl)
        except CodeSpaceExhaustedError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "try again") from exc
        metrics.links_created.inc()
        return LinkOut(
            code=link.code,
            short_url=f"{base}/{link.code}",
            target=link.target,
            expires_at=link.expires_at,
        )

    # Sync `def` endpoints run in FastAPI's threadpool, so blocking DB calls don't stall
    # the event loop. (An `async def` here with a sync driver would.)
    @app.get("/{code}")
    def follow(code: str) -> RedirectResponse:
        try:
            target = shortener.resolve(code)
        except LinkNotFoundError as exc:
            metrics.resolutions.labels("not_found").inc()
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown link") from exc
        except LinkExpiredError as exc:
            metrics.resolutions.labels("expired").inc()
            raise HTTPException(status.HTTP_410_GONE, "link expired") from exc
        metrics.resolutions.labels("hit").inc()
        # 307, not 301: 301 is cached by browsers forever, so expiry and analytics break.
        return RedirectResponse(target, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    if tracer_provider is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=tracer_provider,
            excluded_urls="healthz,readyz,metrics",  # probes would drown real traffic
            exclude_spans=["send", "receive"],  # per-ASGI-message spans: ~3x volume, no insight
        )
    # The OTel instrumentation wraps the entire middleware stack, so this middleware runs
    # *inside* the request span: its access log line carries the trace ID (verified in
    # tests/test_observability.py). It sees the final status, including unhandled 500s.
    app.add_middleware(RequestContextMiddleware, metrics=metrics)
    return app


def create_app_from_env() -> FastAPI:
    """Production wiring. Run with: uvicorn --factory shortly.api:create_app_from_env"""
    # Imported here so tests of the HTTP layer never need a database driver configured.
    from shortly.db import make_engine, pool_in_use
    from shortly.observability import configure_logging, tracer_provider_from_env
    from shortly.pg_store import PostgresStore
    from shortly.settings import Settings

    settings = Settings()  # fields come from the environment; fails fast if missing
    configure_logging(settings.log_level, json_output=settings.log_json)
    engine = make_engine(
        str(settings.database_url),
        pool_size=settings.db_pool_size,
        pool_timeout=settings.db_pool_timeout_seconds,
    )
    metrics = Metrics()
    metrics.db_pool_size.set(settings.db_pool_size)
    metrics.db_pool_in_use.set_function(pool_in_use(engine))
    provider = tracer_provider_from_env(settings.service_name)
    if provider is not None:
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

        # One span per query, as a child of the request span: "which query was slow?"
        # Instrumented at the *driver* (psycopg), not SQLAlchemy: the SQLAlchemy
        # instrumentation silently no-ops on SQLAlchemy 2.1, and the driver sees every
        # query regardless of what issued it. tests/test_postgres.py proves spans appear.
        PsycopgInstrumentor().instrument(tracer_provider=provider)

    return create_app(
        Shortener(PostgresStore(engine), on_collision=metrics.code_collisions.inc),
        base_url=settings.base_url,
        metrics=metrics,
        tracer_provider=provider,
        on_shutdown=engine.dispose,  # close pooled connections cleanly
    )
