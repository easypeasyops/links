"""Application logic: shorten and resolve.

Every source of nondeterminism (code generation, the clock) is injected, so tests
control them directly instead of patching module globals.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final

from pydantic import HttpUrl

from shortly.codes import generate_code
from shortly.models import ShortLink
from shortly.store import CodeConflictError, LinkStore

MAX_ATTEMPTS: Final = 5


class LinkNotFoundError(Exception):
    pass


class LinkExpiredError(Exception):
    pass


class CodeSpaceExhaustedError(Exception):
    """Every attempt collided. At sane fill levels this signals a bug, not bad luck."""


def utc_now() -> datetime:
    return datetime.now(UTC)


class Shortener:
    def __init__(
        self,
        store: LinkStore,
        *,
        code_gen: Callable[[], str] = generate_code,
        clock: Callable[[], datetime] = utc_now,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._store = store
        self._code_gen = code_gen
        self._clock = clock
        self._max_attempts = max_attempts

    def shorten(self, target: str, *, ttl: timedelta | None = None) -> ShortLink:
        url = HttpUrl(target)  # validate once, before any retries; raises ValidationError
        now = self._clock()
        expires_at = now + ttl if ttl is not None else None
        for _ in range(self._max_attempts):
            # Only collisions are retried; validation errors (e.g. non-positive ttl) propagate.
            link = ShortLink(
                code=self._code_gen(),
                target=url,
                created_at=now,
                expires_at=expires_at,
            )
            try:
                # Insert-and-catch, not check-then-insert: a get() followed by add() races
                # under concurrency. The store's add() is the single atomic decision point.
                self._store.add(link)
            except CodeConflictError:
                continue
            return link
        raise CodeSpaceExhaustedError(f"no free code after {self._max_attempts} attempts")

    def is_ready(self) -> bool:
        return self._store.ping()

    def resolve(self, code: str) -> str:
        link = self._store.get(code)
        if link is None:
            raise LinkNotFoundError(code)
        if link.is_expired(self._clock()):
            raise LinkExpiredError(code)
        return str(link.target)
