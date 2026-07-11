"""add user.must_change_password

Revision ID: a8b9c0d1e2f3
Revises: f6a7b8c9d0e1
Create Date: 2026-07-11 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a8b9c0d1e2f3'
down_revision: str | None = 'f6a7b8c9d0e1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default backfills existing rows so the column can be NOT NULL.
    op.add_column(
        'user',
        sa.Column(
            'must_change_password',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # The app always supplies the value; drop the DB-side default going forward.
    op.alter_column('user', 'must_change_password', server_default=None)


def downgrade() -> None:
    op.drop_column('user', 'must_change_password')
