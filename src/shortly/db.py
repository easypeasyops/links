"""Schema definition and engine construction (SQLAlchemy Core, no ORM).

The table here is the source of truth for Alembic autogenerate; the migration files
are what actually runs against a database.
"""

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
from sqlalchemy.pool import NullPool

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


def make_engine(url: str, *, pool_size: int = 5, pooled: bool = True) -> Engine:
    # SQLAlchemy needs the driver in the scheme; accept plain postgresql:// from env.
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    if not pooled:  # one-shot processes (migrations, scripts)
        return create_engine(url, poolclass=NullPool)
    # pre_ping: test a pooled connection before use, so a DB restart doesn't surface as
    # one failed request per stale connection.
    return create_engine(url, pool_size=pool_size, pool_pre_ping=True)
