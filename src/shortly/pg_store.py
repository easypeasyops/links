"""Postgres adapters: ``LinkStore`` and ``RateLimiter``."""

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime

from psycopg.errors import LockNotAvailable, QueryCanceled, UniqueViolation
from sqlalchemy import Connection, Engine, insert, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from shortly.db import links
from shortly.models import ShortLink
from shortly.ratelimit import Decision, Limit
from shortly.store import CodeConflictError, StoreBusyError

log = logging.getLogger(__name__)


@contextmanager
def connect(engine: Engine) -> Iterator[Connection]:
    """A pooled connection, or ``StoreBusyError`` if none frees up within pool_timeout, or
    if a statement on it runs past statement_timeout / lock_timeout (db.make_engine).

    The engine is AUTOCOMMIT (see db.make_engine): each statement commits on its own.
    """
    try:
        conn = engine.connect()
    except PoolTimeoutError as exc:  # every pooled connection busy for pool_timeout
        raise StoreBusyError("database pool exhausted", reason="pool") from exc
    with conn:
        try:
            yield conn
        except DBAPIError as exc:
            # Postgres cancelled the statement (57014) or gave up on a lock (55P03): the
            # database is slow or contended, not broken. Shed like a full pool: 503 + retry.
            if isinstance(exc.orig, QueryCanceled | LockNotAvailable):
                raise StoreBusyError("database timeout", reason="db_timeout") from exc
            raise


# One bounded batch, oldest first, through the partial index on expires_at. SKIP LOCKED: two
# cleanup runs at once (a manual run during the scheduled one) split the work instead of
# blocking each other. Autocommit: every batch commits on its own, so a run stopped halfway
# keeps what it did and holds no lock for long.
_DELETE_EXPIRED = text("""
    DELETE FROM links WHERE code IN (
        SELECT code FROM links
         WHERE expires_at < :before
         ORDER BY expires_at
         LIMIT :limit
           FOR UPDATE SKIP LOCKED)
""")


class PostgresStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _connect(self) -> AbstractContextManager[Connection]:
        return connect(self._engine)

    def add(self, link: ShortLink) -> None:
        stmt = insert(links).values(
            code=link.code,
            target=str(link.target),
            created_at=link.created_at,
            expires_at=link.expires_at,
        )
        try:
            with self._connect() as conn:
                conn.execute(stmt)
        except IntegrityError as exc:
            # Only a duplicate key is a collision. Any other integrity failure (e.g. the
            # CHECK constraint) is a real bug and must not be silently retried.
            if isinstance(exc.orig, UniqueViolation):
                raise CodeConflictError(link.code) from exc
            raise

    def get(self, code: str) -> ShortLink | None:
        with self._connect() as conn:
            row = conn.execute(select(links).where(links.c.code == code)).mappings().first()
        return None if row is None else ShortLink.model_validate(dict(row))

    def delete_expired(self, before: datetime, limit: int) -> int:
        with self._connect() as conn:
            return conn.execute(_DELETE_EXPIRED, {"before": before, "limit": limit}).rowcount

    def ping(self) -> bool:
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            log.warning("database ping failed", exc_info=True)
            return False
        return True


# Insert a fresh client, or push an existing one's tat forward, in ONE atomic statement.
# The WHERE clause makes the update conditional: when it fails (the client is over its
# limit) the row is left alone and nothing is returned. Row-level locking serialises
# concurrent requests from the same client, across every replica.
_HIT = text("""
    INSERT INTO rate_limits AS r (key, tat) VALUES (:key, :now + :interval)
    ON CONFLICT (key) DO UPDATE
       SET tat = GREATEST(r.tat, :now) + :interval
     WHERE GREATEST(r.tat, :now) + :interval - :now <= :tolerance
    RETURNING tat
""")
_TAT = text("SELECT tat FROM rate_limits WHERE key = :key")
# A row whose tat is in the past means exactly what a missing row means.
_CLEANUP = text("DELETE FROM rate_limits WHERE tat < :now")


class PostgresRateLimiter:
    """GCRA in Postgres: exact across replicas, one round trip per allowed request.

    Time comes from the app's clock, not the database's, so tests can control it. Pods
    are NTP-synced to milliseconds, which is noise against a limit counted in seconds.
    """

    def __init__(
        self,
        engine: Engine,
        limit: Limit,
        *,
        clock: Callable[[], float] = time.time,
        cleanup_every: float = 60.0,
    ) -> None:
        self._engine = engine
        self._limit = limit
        self._clock = clock
        self._cleanup_every = cleanup_every
        self._next_cleanup = clock() + cleanup_every
        self._cleanup_lock = threading.Lock()

    def hit(self, key: str) -> Decision:
        now = self._clock()
        params = {
            "key": key,
            "now": now,
            "interval": self._limit.interval,
            "tolerance": self._limit.tolerance,
        }
        with connect(self._engine) as conn:
            if conn.execute(_HIT, params).first() is not None:
                decision = Decision(allowed=True)
            else:
                tat = conn.execute(_TAT, {"key": key}).scalar_one_or_none()
                # tat None: a concurrent cleanup just removed the row; retry is safe now.
                limit = self._limit
                wait = 0.0 if tat is None else tat + limit.interval - limit.tolerance - now
                decision = Decision(allowed=False, retry_after=max(wait, 0.0))
            self._maybe_cleanup(conn, now)
        return decision

    def _maybe_cleanup(self, conn: Connection, now: float) -> None:
        # At most once per cleanup_every per process, and never two at once: the table
        # holds one row per client active in the last few seconds, so this stays small.
        if now < self._next_cleanup or not self._cleanup_lock.acquire(blocking=False):
            return
        try:
            self._next_cleanup = now + self._cleanup_every
            conn.execute(_CLEANUP, {"now": now})
        finally:
            self._cleanup_lock.release()
