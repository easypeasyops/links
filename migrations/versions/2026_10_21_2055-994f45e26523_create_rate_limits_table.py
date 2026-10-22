"""create rate_limits table

Expand-only: a new table that the previous release ignores, so this migration is safe to
run before the new pods start, and a rollback of the app needs no downgrade.

Revision ID: 994f45e26523
Revises: 9518f4b91d9a
Create Date: 2026-10-21 20:55:11.305722

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "994f45e26523"
down_revision: str | Sequence[str] | None = "9518f4b91d9a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "rate_limits",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("tat", sa.Double(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    """Downgrade schema. Loses only short-lived limiter state: clients start fresh."""
    op.drop_table("rate_limits")
