"""Overload control: cap the requests a pod works on at once, and shed the rest fast.

Past its capacity, a pod without a limit doesn't fail: it queues. Every request waits behind
the ones before it, so latency climbs for *everyone* until clients time out (measured: at 800
req/s offered to a pod that serves ~525, p50 went from 2 ms to 1.6 s with zero errors, see
docs/DESIGN.md, Capacity). A 503 with Retry-After in a millisecond is better for the client
(it can retry another pod), and the requests that are admitted stay fast.

The limit counts requests in flight on this pod, in the event loop (one thread, so a plain
int is safe). Probes and /metrics are exempt: an overloaded pod is still alive, still knows
whether it can reach the database, and must still be scrapable while it sheds.
"""

from collections.abc import Callable, Sequence

from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Match
from starlette.types import ASGIApp, Receive, Scope, Send

EXEMPT_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})


class ConcurrencyLimitMiddleware:
    """Pure ASGI: admit at most ``limit`` requests at once; answer the rest 503 at once.

    ``limit=0`` turns the limit off (requests are still counted). ``on_shed`` is called for
    each refused request, ``on_count`` with the new in-flight count (a gauge's ``set``).

    ``routes`` (the app's router) lets a refused request carry its route template, as if
    the router had seen it. The SLOs select requests by route (``route="/{code}"``): a shed
    redirect labelled "unmatched" would fail the user without burning the error budget.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        limit: int,
        on_shed: Callable[[], None] = lambda: None,
        on_count: Callable[[int], None] = lambda _: None,
        routes: Sequence[BaseRoute] = (),
    ) -> None:
        self.app = app
        self.routes = routes
        self.limit = limit
        self.on_shed = on_shed
        self.on_count = on_count
        self.in_flight = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        if 0 < self.limit <= self.in_flight:
            self.on_shed()
            for route in self.routes:  # label the 503 with the route it was meant for
                match, _ = route.matches(scope)
                if match is Match.FULL:
                    scope["route"] = route
                    break
            response = JSONResponse(
                {"detail": "overloaded, retry shortly"},
                status_code=503,
                headers={"Retry-After": "1"},
            )
            await response(scope, receive, send)
            return
        self.in_flight += 1
        self.on_count(self.in_flight)
        try:
            await self.app(scope, receive, send)
        finally:
            self.in_flight -= 1
            self.on_count(self.in_flight)
