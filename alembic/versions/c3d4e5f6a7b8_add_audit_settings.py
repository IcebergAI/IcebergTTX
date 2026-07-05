"""add auditsettings (SIEM forwarding config, #24)

Revision ID: c3d4e5f6a7b8
Revises: b7c8d9e0f1a2
Create Date: 2026-07-05 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: str | None = 'b7c8d9e0f1a2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'auditsettings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('methods', JSONB(), nullable=True),
        sa.Column('min_severity', sa.String(), nullable=False),
        sa.Column('file_path', sa.String(), nullable=False),
        sa.Column('syslog_host', sa.String(), nullable=False),
        sa.Column('syslog_port', sa.Integer(), nullable=False),
        sa.Column('syslog_protocol', sa.String(), nullable=False),
        sa.Column('syslog_facility', sa.Integer(), nullable=False),
        sa.Column('http_endpoint', sa.String(), nullable=False),
        sa.Column('http_verify_tls', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('auditsettings')
