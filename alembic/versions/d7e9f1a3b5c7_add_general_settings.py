"""add general settings

Revision ID: d7e9f1a3b5c7
Revises: c6d8e0f2a4b6
Create Date: 2026-07-13 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7e9f1a3b5c7"
down_revision: str | None = "c6d8e0f2a4b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # First read seeds the deployment's current non-secret environment defaults.
    op.create_table(
        "generalsettings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("registration_enabled", sa.Boolean(), nullable=False),
        sa.Column("access_token_expire_minutes", sa.Integer(), nullable=False),
        sa.Column("audit_persist", sa.Boolean(), nullable=False),
        sa.Column("login_max_attempts", sa.Integer(), nullable=False),
        sa.Column("login_lockout_seconds", sa.Integer(), nullable=False),
        sa.Column("registration_max_attempts", sa.Integer(), nullable=False),
        sa.Column("registration_lockout_seconds", sa.Integer(), nullable=False),
        sa.Column("password_reset_max_attempts", sa.Integer(), nullable=False),
        sa.Column("password_reset_lockout_seconds", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("generalsettings")
