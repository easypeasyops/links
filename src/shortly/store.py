"""Storage port and an in-memory adapter.

The service depends on the ``LinkStore`` Protocol, not on any database. Postgres
arrives in a later milestone as a second adapter; the service and its tests won't change.
"""

from typing import Protocol

from shortly.models import ShortLink


class CodeConflictError(Exception):
    """The code is already taken. Raised atomically by ``add``, never by a separate check."""


class LinkStore(Protocol):
    def add(self, link: ShortLink) -> None:
        """Insert ``link``; raise ``CodeConflictError`` if its code exists."""
        ...

    def get(self, code: str) -> ShortLink | None: ...

    def ping(self) -> bool:
        """True if the backend can serve requests. Used by the readiness probe."""
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
