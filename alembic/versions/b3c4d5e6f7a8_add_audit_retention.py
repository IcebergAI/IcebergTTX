"""add audit retention days + authtoken expiry index (#251)

Revision ID: b3c4d5e6f7a8
Revises: a1b2c3d4e5f6
Create Date: 2026-07-24 00:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3c4d5e6f7a8"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The auditsettings singleton is seeded lazily on first read, so an upgrade may
    # find the row already present — the column needs a server_default to backfill it.
    # 0 means "keep forever": an upgrade must never silently destroy security records,
    # so pruning stays off until an operator opts in at /admin/audit (#251).
    op.add_column(
        "auditsettings",
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="0"),
    )
    # Drop the DDL default once the backfill is done; the model default carries it from here.
    op.alter_column("auditsettings", "retention_days", server_default=None)
    # The retention sweep filters dead tokens on expires_at, which drives the branch
    # responsible for unbounded growth (every password-reset request nobody clicked).
    # used_at is mostly NULL and its branch is a small subset, so it stays unindexed.
    op.create_index("ix_authtoken_expires_at", "authtoken", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_authtoken_expires_at", table_name="authtoken")
    op.drop_column("auditsettings", "retention_days")
