"""Postgres adapter for ``LinkStore``."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from psycopg.errors import UniqueViolation
from sqlalchemy import Connection, Engine, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from shortly.db import links
from shortly.models import ShortLink
from shortly.store import CodeConflictError, StoreBusyError

log = logging.getLogger(__name__)


class PostgresStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @contextmanager
    def _connect(self) -> Iterator[Connection]:
        # The engine is AUTOCOMMIT (see db.make_engine): each statement commits on its own.
        try:
            conn = self._engine.connect()
        except PoolTimeoutError as exc:  # every pooled connection busy for pool_timeout
            raise StoreBusyError("database pool exhausted") from exc
        with conn:
            yield conn

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

    def ping(self) -> bool:
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            log.warning("database ping failed", exc_info=True)
            return False
        return True
