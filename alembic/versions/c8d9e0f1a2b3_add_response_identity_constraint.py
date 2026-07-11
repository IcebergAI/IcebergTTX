"""add response identity constraint

Revision ID: c8d9e0f1a2b3
Revises: b5c6d7e8f9a0
Create Date: 2026-07-12 00:00:00.000000
"""
from collections.abc import Sequence

from alembic import op

revision: str = "c8d9e0f1a2b3"
down_revision: str | None = "b5c6d7e8f9a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Older deployments could contain duplicate rows from concurrent submission
    # races. Preserve the earliest response for each participant/inject identity
    # before the database starts enforcing the invariant.
    op.execute(
        """
        DELETE FROM response AS duplicate
        USING response AS canonical
        WHERE duplicate.exercise_id = canonical.exercise_id
          AND duplicate.inject_id = canonical.inject_id
          AND duplicate.user_id = canonical.user_id
          AND duplicate.id > canonical.id
        """
    )
    op.create_unique_constraint(
        "uq_response_exercise_inject_user",
        "response",
        ["exercise_id", "inject_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_response_exercise_inject_user", "response", type_="unique")
