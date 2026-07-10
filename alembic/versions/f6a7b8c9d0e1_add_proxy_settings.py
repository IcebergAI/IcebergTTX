"""add proxysettings (outbound proxy config, #97)

Revision ID: f6a7b8c9d0e1
Revises: d4e5f6a7b8c9
Create Date: 2026-07-10 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f6a7b8c9d0e1'
down_revision: str | None = 'd4e5f6a7b8c9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No row is seeded here — the singleton (id=1) is created lazily from the
    # PROXY_* env defaults on first read (proxy_settings_service.get_settings).
    op.create_table(
        'proxysettings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('mode', sa.String(), nullable=False),
        sa.Column('proxy_url', sa.String(), nullable=False),
        sa.Column('no_proxy', sa.String(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('proxysettings')
