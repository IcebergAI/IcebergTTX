"""add triggered communication idempotency

Revision ID: d9e0f1a2b3c4
Revises: b5c6d7e8f9a0
Create Date: 2026-07-12 00:00:00.000000
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "d9e0f1a2b3c4"
down_revision: str | None = "b5c6d7e8f9a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("communication", sa.Column("trigger_key", sa.String(), nullable=True))
    op.create_unique_constraint(
        "uq_communication_exercise_trigger_key",
        "communication",
        ["exercise_id", "trigger_key"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_communication_exercise_trigger_key", "communication", type_="unique")
    op.drop_column("communication", "trigger_key")
