"""Schema definition and engine construction (SQLAlchemy Core, no ORM).

The table here is the source of truth for Alembic autogenerate; the migration files
are what actually runs against a database.
"""

from collections.abc import Callable

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Engine,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
)
from sqlalchemy.pool import NullPool, QueuePool

metadata = MetaData()

links = Table(
    "links",
    metadata,
    # PRIMARY KEY implies UNIQUE: this constraint is what makes CodeConflictError atomic.
    Column("code", String(32), primary_key=True),
    Column("target", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("expires_at IS NULL OR expires_at > created_at", name="expiry_after_creation"),
)


def make_engine(
    url: str, *, pool_size: int = 5, pool_timeout: float = 1.0, pooled: bool = True
) -> Engine:
    """Engine for the app (pooled, autocommit) or for one-shot processes (no pool).

    The pool is a fixed size: ``max_overflow=0`` makes each replica's connection count a
    known number, so the Postgres budget is ``max_replicas * pool_size`` (docs/DESIGN.md,
    Capacity). ``pool_timeout`` bounds how long a request waits for a connection before
    ``StoreBusyError`` -> 503; SQLAlchemy's default of 30 s turns overload into a pile-up.
    """
    # SQLAlchemy needs the driver in the scheme; accept plain postgresql:// from env.
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    if not pooled:  # one-shot processes (migrations, scripts): keep real transactions
        return create_engine(url, poolclass=NullPool)
    return create_engine(
        url,
        pool_size=pool_size,
        max_overflow=0,
        pool_timeout=pool_timeout,
        # pre_ping: test a pooled connection before use, so a DB restart doesn't surface as
        # one failed request per stale connection. Costs one round trip per checkout.
        pool_pre_ping=True,
        # Every query the app runs is ONE statement, which Postgres already executes
        # atomically. Without autocommit, psycopg wraps each in BEGIN ... COMMIT/ROLLBACK:
        # 2 extra round trips per request (load test: see docs/DESIGN.md, Capacity).
        # A future multi-statement unit of work must opt back in explicitly:
        #   conn.execution_options(isolation_level="READ COMMITTED")
        isolation_level="AUTOCOMMIT",
    )


def pool_in_use(engine: Engine) -> Callable[[], float]:
    """A zero-arg reader of how many pooled connections are checked out (for a Gauge)."""
    pool = engine.pool
    if not isinstance(pool, QueuePool):  # NullPool has nothing to report
        return lambda: 0.0
    return lambda: float(pool.checkedout())
