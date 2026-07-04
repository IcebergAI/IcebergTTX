"""add user.token_valid_after

Revision ID: e5b99d4c11c0
Revises: 27d0b1d4557c
Create Date: 2026-07-05 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5b99d4c11c0'
down_revision: str | None = '27d0b1d4557c'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'user',
        sa.Column('token_valid_after', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('user', 'token_valid_after')
