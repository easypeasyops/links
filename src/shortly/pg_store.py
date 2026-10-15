"""Postgres adapter for ``LinkStore``."""

import logging

from psycopg.errors import UniqueViolation
from sqlalchemy import Engine, insert, select, text
from sqlalchemy.exc import IntegrityError

from shortly.db import links
from shortly.models import ShortLink
from shortly.store import CodeConflictError

log = logging.getLogger(__name__)


class PostgresStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def add(self, link: ShortLink) -> None:
        stmt = insert(links).values(
            code=link.code,
            target=str(link.target),
            created_at=link.created_at,
            expires_at=link.expires_at,
        )
        try:
            with self._engine.begin() as conn:
                conn.execute(stmt)
        except IntegrityError as exc:
            # Only a duplicate key is a collision. Any other integrity failure (e.g. the
            # CHECK constraint) is a real bug and must not be silently retried.
            if isinstance(exc.orig, UniqueViolation):
                raise CodeConflictError(link.code) from exc
            raise

    def get(self, code: str) -> ShortLink | None:
        with self._engine.connect() as conn:
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
