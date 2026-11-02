"""Per-client rate limiting for link creation: GCRA, the "generic cell rate algorithm".

GCRA is a token bucket stored as ONE number per client: the theoretical arrival time
(``tat``), the moment the client's bucket would be full again. Each allowed request pushes
``tat`` forward by one emission interval ``T = 60 / per_minute``. A request is allowed if,
after that push, ``tat`` is at most ``burst * T`` ahead of now. So a quiet client may send
``burst`` requests at once, then one every ``T`` seconds. There is no window boundary
(unlike fixed-window counters, which let a client double its rate across the edge) and no
background refill job.

One number per client is what makes the Postgres version (``pg_store.PostgresRateLimiter``)
a single atomic upsert, so the limit is exact across every replica without Redis or another
moving part.
"""

import ipaddress
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Limit:
    per_minute: float  # sustained rate
    burst: int  # requests a quiet client may send at once

    def __post_init__(self) -> None:
        if self.per_minute <= 0 or self.burst < 1:
            raise ValueError("per_minute must be > 0 and burst >= 1")

    @property
    def interval(self) -> float:
        """Seconds of budget one request costs (GCRA's emission interval, T)."""
        return 60.0 / self.per_minute

    @property
    def tolerance(self) -> float:
        """How far ``tat`` may run ahead of now: ``burst`` requests' worth."""
        return self.interval * self.burst


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: float = 0.0  # seconds until one more request would be allowed

    @property
    def retry_after_header(self) -> str:
        """Retry-After takes whole seconds; round up so a client that obeys it succeeds."""
        return str(max(1, math.ceil(self.retry_after)))


def gcra(tat: float | None, now: float, limit: Limit) -> tuple[Decision, float | None]:
    """One GCRA step. Returns the decision and the new ``tat`` (None: leave it unchanged).

    The Postgres limiter runs the same arithmetic in SQL; the contract tests hold both
    implementations to these semantics.
    """
    new_tat = max(tat if tat is not None else now, now) + limit.interval
    if new_tat - now <= limit.tolerance:
        return Decision(allowed=True), new_tat
    # Denied (never for a fresh client: burst >= 1), so tat is set and stays. The next
    # request is allowed once now >= tat + T - tolerance.
    current = now if tat is None else tat
    return Decision(False, retry_after=current + limit.interval - limit.tolerance - now), None


class RateLimiter(Protocol):
    def hit(self, key: str) -> Decision:
        """Count one request for ``key`` and decide. May raise ``StoreBusyError``."""
        ...


def client_key(host: str | None) -> str:
    """The identity a limit applies to: the client IP, with IPv6 grouped by /64.

    One IPv6 subscriber usually owns a whole /64 (2^64 addresses), so a per-address limit
    would be no limit at all. ``host`` is the peer address after uvicorn's proxy-header
    handling: X-Forwarded-For is honoured only from FORWARDED_ALLOW_IPS (the ingress), so
    clients can't pick their own key.
    """
    if not host:
        return "unknown"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host[:64]  # not an IP (e.g. a unix socket or a test client): use as-is
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


class InMemoryRateLimiter:
    """Per-process limiter for tests and single-process runs. Thread-safe (the API runs
    sync handlers in a threadpool). With N replicas each keeps its own count, so a client
    gets N times the limit: production uses ``PostgresRateLimiter``."""

    def __init__(self, limit: Limit, *, clock: Callable[[], float] = time.time) -> None:
        self._limit = limit
        self._clock = clock
        self._tats: dict[str, float] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> Decision:
        with self._lock:
            now = self._clock()
            decision, new_tat = gcra(self._tats.get(key), now, self._limit)
            if new_tat is not None:
                self._tats[key] = new_tat
            # A tat in the past is the same as no row: drop it, so memory stays bounded by
            # the clients active in the last burst * T seconds.
            if len(self._tats) > 10_000:
                self._tats = {k: t for k, t in self._tats.items() if t > now}
            return decision
