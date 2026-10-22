"""index links.expires_at for cleanup

Built CONCURRENTLY: a plain CREATE INDEX blocks every write to `links` until it finishes,
which on a large table is an outage. CONCURRENTLY can't run inside a transaction, hence
the autocommit block. A concurrent build that fails leaves an INVALID index behind, which
IF NOT EXISTS would then skip, so a retry drops an invalid one first.

Expand-only: the previous release ignores the index.

Revision ID: 24d81034465b
Revises: 994f45e26523
Create Date: 2026-10-22 13:01:01.730244

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "24d81034465b"
down_revision: str | Sequence[str] | None = "994f45e26523"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


INDEX = "ix_links_expires_at"


def upgrade() -> None:
    """Upgrade schema."""
    with op.get_context().autocommit_block():
        invalid = op.get_bind().scalar(
            sa.text(
                "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
                " WHERE c.relname = :name AND NOT i.indisvalid"
            ),
            {"name": INDEX},
        )
        if invalid:  # left by an earlier build that failed halfway
            op.drop_index(INDEX, table_name="links", postgresql_concurrently=True)
        op.create_index(
            INDEX,
            "links",
            ["expires_at"],
            postgresql_where=sa.text("expires_at IS NOT NULL"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.get_context().autocommit_block():
        op.drop_index(INDEX, table_name="links", postgresql_concurrently=True, if_exists=True)
