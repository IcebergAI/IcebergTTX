"""pacing: pause timing + scheduled inject release

Revision ID: f7a8b9c0d1e2
Revises: e7f8a9b0c1d2
Create Date: 2026-07-11 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f7a8b9c0d1e2'
down_revision: str | None = 'e7f8a9b0c1d2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Pause-aware exercise clock (#116). paused_at is set while paused (else NULL);
    # accumulated_pause_seconds totals all completed pause spans.
    op.add_column(
        'exercise',
        sa.Column('paused_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'exercise',
        sa.Column(
            'accumulated_pause_seconds',
            sa.Float(),
            nullable=False,
            server_default='0',
        ),
    )
    # Scheduled inject release (#116). Minutes after exercise start; NULL = manual-only.
    op.add_column(
        'inject',
        sa.Column('release_offset_minutes', sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('inject', 'release_offset_minutes')
    op.drop_column('exercise', 'accumulated_pause_seconds')
    op.drop_column('exercise', 'paused_at')
