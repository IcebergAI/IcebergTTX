"""add user.is_admin

Revision ID: b7c8d9e0f1a2
Revises: a1f2c3d4e5b6
Create Date: 2026-07-05 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7c8d9e0f1a2'
down_revision: str | None = 'a1f2c3d4e5b6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default backfills existing rows so the column can be NOT NULL.
    op.add_column(
        'user',
        sa.Column(
            'is_admin',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # The app always supplies the value; drop the DB-side default going forward.
    op.alter_column('user', 'is_admin', server_default=None)


def downgrade() -> None:
    op.drop_column('user', 'is_admin')
