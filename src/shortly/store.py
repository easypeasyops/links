"""Storage port and an in-memory adapter.

The service depends on the ``LinkStore`` Protocol, not on any database. Postgres
arrives in a later milestone as a second adapter; the service and its tests won't change.
"""

from datetime import datetime
from typing import Protocol

from shortly.models import ShortLink


class CodeConflictError(Exception):
    """The code is already taken. Raised atomically by ``add``, never by a separate check."""


class StoreBusyError(Exception):
    """No backend capacity within the configured wait (e.g. the DB pool is exhausted).

    Overload, not failure: the API sheds the request with 503 + Retry-After instead of
    queueing it until the client gives up.
    """


class LinkStore(Protocol):
    def add(self, link: ShortLink) -> None:
        """Insert ``link``; raise ``CodeConflictError`` if its code exists.

        Any method may raise ``StoreBusyError`` when the backend is saturated.
        """
        ...

    def get(self, code: str) -> ShortLink | None: ...

    def ping(self) -> bool:
        """True if the backend can serve requests. Used by the readiness probe."""
        ...

    def delete_expired(self, before: datetime, limit: int) -> int:
        """Delete up to ``limit`` links that expired before ``before``, oldest first.

        Returns how many were deleted. Bounded so that one call is one short statement:
        the cleanup job (shortly.cleanup) calls it in a loop.
        """
        ...


class InMemoryStore:
    """Dict-backed store for tests and local runs. Not thread-safe; single process only."""

    def __init__(self) -> None:
        self._links: dict[str, ShortLink] = {}

    def add(self, link: ShortLink) -> None:
        if link.code in self._links:
            raise CodeConflictError(link.code)
        self._links[link.code] = link

    def get(self, code: str) -> ShortLink | None:
        return self._links.get(code)

    def ping(self) -> bool:
        return True

    def delete_expired(self, before: datetime, limit: int) -> int:
        expired = sorted(
            (link for link in self._links.values() if link.expires_at and link.expires_at < before),
            key=lambda link: link.expires_at or before,
        )[:limit]
        for link in expired:
            del self._links[link.code]
        return len(expired)
