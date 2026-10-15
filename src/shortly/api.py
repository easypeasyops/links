"""HTTP layer. Thin by design: parse, call the service, map results to status codes.

No business logic lives here, so the service stays testable without HTTP.
"""

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager, nullcontext
from datetime import timedelta
from typing import Annotated

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import AwareDatetime, BaseModel, Field, HttpUrl

from shortly.service import (
    CodeSpaceExhaustedError,
    LinkExpiredError,
    LinkNotFoundError,
    Shortener,
)

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
    lifespan_resource: Callable[[], AbstractContextManager[object]] = nullcontext,
) -> FastAPI:
    """Build the app around an already-wired ``Shortener``.

    Taking dependencies as arguments (instead of reading globals or env vars here) is
    what lets tests build the app with an in-memory store and a fake clock.
    """

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        with lifespan_resource():  # e.g. dispose the DB pool on shutdown
            yield

    app = FastAPI(title="shortly", lifespan=lifespan)
    base = base_url.rstrip("/")

    # Probes are separate on purpose. Liveness: "is the process wedged?" — restarting
    # fixes that. Readiness: "can I take traffic?" — a DB blip should pull the pod out of
    # rotation, NOT restart it (restarting every pod during a DB outage makes it worse).
    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> dict[str, str]:
        if not shortener.is_ready():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "not ready")
        return {"status": "ready"}

    @app.post("/links", status_code=status.HTTP_201_CREATED)
    def create_link(body: CreateLink) -> LinkOut:
        ttl = timedelta(seconds=body.ttl_seconds) if body.ttl_seconds else None
        try:
            link = shortener.shorten(str(body.url), ttl=ttl)
        except CodeSpaceExhaustedError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "try again") from exc
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
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown link") from exc
        except LinkExpiredError as exc:
            raise HTTPException(status.HTTP_410_GONE, "link expired") from exc
        # 307, not 301: 301 is cached by browsers forever, so expiry and analytics break.
        return RedirectResponse(target, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    return app


def create_app_from_env() -> FastAPI:
    """Production wiring. Run with: uvicorn --factory shortly.api:create_app_from_env"""
    # Imported here so tests of the HTTP layer never need a database driver configured.
    from shortly.db import make_engine
    from shortly.pg_store import PostgresStore
    from shortly.settings import Settings

    settings = Settings()  # fields come from the environment; fails fast if missing
    engine = make_engine(str(settings.database_url), pool_size=settings.db_pool_size)

    @contextmanager
    def dispose_on_exit() -> Iterator[None]:
        try:
            yield
        finally:
            engine.dispose()  # close pooled connections cleanly on graceful shutdown

    return create_app(
        Shortener(PostgresStore(engine)),
        base_url=settings.base_url,
        lifespan_resource=dispose_on_exit,
    )
