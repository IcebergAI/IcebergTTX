"""add durable exercise state transitions

Revision ID: 9c4f2a7d1e30
Revises: e7f8a9b0c1d2
Create Date: 2026-07-11 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9c4f2a7d1e30"
down_revision: str | None = "e7f8a9b0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Reuse the enum type created for exercise.state by the baseline migration.
    exercise_state = postgresql.ENUM(
        "draft",
        "active",
        "paused",
        "completed",
        name="exercisestate",
        create_type=False,
    )
    op.create_table(
        "exercisestatetransition",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("exercise_id", sa.Integer(), nullable=False),
        sa.Column("from_state", exercise_state, nullable=False),
        sa.Column("to_state", exercise_state, nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("transitioned_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "from_state <> to_state", name="ck_exercise_transition_changes_state"
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["exercise_id"], ["exercise.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_exercisestatetransition_exercise_time",
        "exercisestatetransition",
        ["exercise_id", "transitioned_at", "id"],
        unique=False,
    )

    # Recover exact historical events where persisted audit rows are available.
    # Audit actor IDs are not foreign-key constrained, so retain one only when the
    # referenced user still exists. For a completion, the immediately preceding
    # lifecycle action determines whether the prior state was active or paused.
    op.execute(
        sa.text(
            """
            INSERT INTO exercisestatetransition
                (exercise_id, from_state, to_state, actor_id, transitioned_at)
            SELECT
                e.id,
                CASE
                    WHEN a.action = 'exercise.start' THEN 'draft'::exercisestate
                    WHEN a.action = 'exercise.pause' THEN 'active'::exercisestate
                    WHEN a.action = 'exercise.resume' THEN 'paused'::exercisestate
                    WHEN (
                        SELECT prior.action
                        FROM auditevent AS prior
                        WHERE prior.target_type = 'exercise'
                          AND prior.target_id = a.target_id
                          AND prior.action IN (
                              'exercise.start', 'exercise.pause',
                              'exercise.resume', 'exercise.complete'
                          )
                          AND prior.result = 'success'
                          AND (prior.created_at, prior.id) < (a.created_at, a.id)
                        ORDER BY prior.created_at DESC, prior.id DESC
                        LIMIT 1
                    ) = 'exercise.pause' THEN 'paused'::exercisestate
                    ELSE 'active'::exercisestate
                END,
                CASE
                    WHEN a.action IN ('exercise.start', 'exercise.resume')
                        THEN 'active'::exercisestate
                    WHEN a.action = 'exercise.pause' THEN 'paused'::exercisestate
                    ELSE 'completed'::exercisestate
                END,
                CASE
                    WHEN EXISTS (SELECT 1 FROM "user" AS u WHERE u.id = a.actor_id)
                        THEN a.actor_id
                    ELSE NULL
                END,
                a.created_at
            FROM auditevent AS a
            JOIN exercise AS e ON a.target_id = e.id::text
            WHERE a.target_type = 'exercise'
              AND a.action IN (
                  'exercise.start', 'exercise.pause',
                  'exercise.resume', 'exercise.complete'
              )
              AND a.result = 'success'
            ORDER BY a.created_at, a.id
            """
        )
    )

    # Audit persistence is optional. Preserve the start/completion events that can
    # be recovered from the always-populated Exercise timestamps when no matching
    # durable audit event was available. The exact pre-completion state and actor
    # cannot always be known for legacy rows, so use the most recent recovered state
    # and the exercise owner as the same best-effort attribution used by the old
    # timeline implementation.
    op.execute(
        sa.text(
            """
            INSERT INTO exercisestatetransition
                (exercise_id, from_state, to_state, actor_id, transitioned_at)
            SELECT
                e.id,
                'draft'::exercisestate,
                'active'::exercisestate,
                e.created_by,
                e.started_at
            FROM exercise AS e
            WHERE e.started_at IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM exercisestatetransition AS t
                  WHERE t.exercise_id = e.id
                    AND t.from_state = 'draft'::exercisestate
                    AND t.to_state = 'active'::exercisestate
              )
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO exercisestatetransition
                (exercise_id, from_state, to_state, actor_id, transitioned_at)
            SELECT
                e.id,
                CASE
                    WHEN (
                        SELECT t.to_state::text
                        FROM exercisestatetransition AS t
                        WHERE t.exercise_id = e.id
                        ORDER BY t.transitioned_at DESC, t.id DESC
                        LIMIT 1
                    ) = 'paused' THEN 'paused'::exercisestate
                    ELSE 'active'::exercisestate
                END,
                'completed'::exercisestate,
                e.created_by,
                e.ended_at
            FROM exercise AS e
            WHERE e.ended_at IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM exercisestatetransition AS t
                  WHERE t.exercise_id = e.id
                    AND t.to_state = 'completed'::exercisestate
              )
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_exercisestatetransition_exercise_time",
        table_name="exercisestatetransition",
    )
    op.drop_table("exercisestatetransition")
