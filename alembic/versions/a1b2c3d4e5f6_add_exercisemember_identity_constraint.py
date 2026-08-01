"""add exercisemember identity constraint

Revision ID: a1b2c3d4e5f6
Revises: f9a1b3c5d7e9
Create Date: 2026-07-21 00:00:00.000000
"""
from collections.abc import Sequence

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "f9a1b3c5d7e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ExerciseMember was the last identity table still guarded only by an
    # application-level check-then-insert (#262), so concurrent enrolments could
    # commit duplicate (exercise_id, user_id) rows — possibly with divergent
    # group_ids. Collapse any pre-existing duplicates to the earliest row before the
    # database starts enforcing uniqueness (mirrors c8d9e0f1a2b3 for response).
    op.execute(
        """
        DELETE FROM exercisemember AS duplicate
        USING exercisemember AS canonical
        WHERE duplicate.exercise_id = canonical.exercise_id
          AND duplicate.user_id = canonical.user_id
          AND duplicate.id > canonical.id
        """
    )
    # The unique constraint's backing index serves the same (exercise_id, user_id)
    # lookup the plain index did, so the old index is redundant once it exists.
    op.drop_index("ix_exercisemember_exercise_user", table_name="exercisemember")
    op.create_unique_constraint(
        "uq_exercisemember_exercise_user",
        "exercisemember",
        ["exercise_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_exercisemember_exercise_user", "exercisemember", type_="unique"
    )
    op.create_index(
        "ix_exercisemember_exercise_user",
        "exercisemember",
        ["exercise_id", "user_id"],
    )
