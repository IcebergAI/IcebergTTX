"""add email settings

Revision ID: c6d8e0f2a4b6
Revises: b4d7e2f19a03
Create Date: 2026-07-13 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6d8e0f2a4b6"
down_revision: str | None = "b4d7e2f19a03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Seed no row: first read captures the deployment's SMTP_* defaults.
    op.create_table(
        "emailsettings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("smtp_host", sa.String(), nullable=False),
        sa.Column("smtp_port", sa.Integer(), nullable=False),
        sa.Column("smtp_from", sa.String(), nullable=False),
        sa.Column("smtp_username", sa.String(), nullable=False),
        sa.Column("smtp_starttls", sa.Boolean(), nullable=False),
        sa.Column("smtp_tls", sa.Boolean(), nullable=False),
        sa.Column("public_base_url", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("emailsettings")
