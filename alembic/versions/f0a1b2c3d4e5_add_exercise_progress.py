"""add group-aware exercise progression

Revision ID: f0a1b2c3d4e5
Revises: b5c6d7e8f9a0
Create Date: 2026-07-12 01:55:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f0a1b2c3d4e5"
down_revision: str | None = "b5c6d7e8f9a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("inject", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("inject", sa.Column("resolved_by", sa.Integer(), nullable=True))
    op.add_column("inject", sa.Column("resolution_reason", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_inject_resolved_by_user",
        "inject",
        "user",
        ["resolved_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "exerciseprogress",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("exercise_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.String(), nullable=True),
        sa.Column("current_node_id", sa.String(), nullable=True),
        sa.Column("current_inject_id", sa.Integer(), nullable=True),
        sa.Column("advanced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("advanced_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["advanced_by"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["current_inject_id"], ["inject.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["exercise_id"], ["exercise.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "exercise_id",
            "group_id",
            name="uq_exercise_progress_group",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_exerciseprogress_exercise_group",
        "exerciseprogress",
        ["exercise_id", "group_id"],
    )
    op.create_table(
        "injectprogress",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("exercise_id", sa.Integer(), nullable=False),
        sa.Column("inject_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.String(), nullable=True),
        sa.Column(
            "state",
            sa.Enum("pending", "released", "resolved", name="injectstate"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Integer(), nullable=True),
        sa.Column("resolution_reason", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["exercise_id"], ["exercise.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["inject_id"], ["inject.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resolved_by"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "inject_id",
            "group_id",
            name="uq_inject_progress_group",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_injectprogress_inject_group", "injectprogress", ["inject_id", "group_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_injectprogress_inject_group", table_name="injectprogress")
    op.drop_table("injectprogress")
    op.drop_index("ix_exerciseprogress_exercise_group", table_name="exerciseprogress")
    op.drop_table("exerciseprogress")
    op.drop_constraint("fk_inject_resolved_by_user", "inject", type_="foreignkey")
    op.drop_column("inject", "resolution_reason")
    op.drop_column("inject", "resolved_by")
    op.drop_column("inject", "resolved_at")
