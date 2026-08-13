"""add exact immutable launch snapshots without inventing legacy history

Revision ID: e1a2b3c4d5f6
Revises: d8e9f0a1b2c3
Create Date: 2026-08-13 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e1a2b3c4d5f6"
down_revision: str | None = "d8e9f0a1b2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    provenance = postgresql.ENUM(
        "pending",
        "exact",
        "unknown",
        name="snapshotprovenance",
    )
    provenance.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "exercise_launch_snapshot",
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=20), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "digest ~ '^[0-9a-f]{64}$'",
            name="ck_exercise_launch_snapshot_digest",
        ),
        sa.PrimaryKeyConstraint("digest"),
    )

    op.add_column(
        "exercise",
        sa.Column(
            "launch_provenance",
            postgresql.ENUM(
                "pending",
                "exact",
                "unknown",
                name="snapshotprovenance",
                create_type=False,
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "exercise",
        sa.Column("launch_snapshot_digest", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_exercise_launch_snapshot_digest",
        "exercise",
        "exercise_launch_snapshot",
        ["launch_snapshot_digest"],
        ["digest"],
    )
    op.create_index(
        "ix_exercise_launch_snapshot_digest",
        "exercise",
        ["launch_snapshot_digest"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_exercise_launch_provenance_digest",
        "exercise",
        "(launch_provenance = 'exact' AND launch_snapshot_digest IS NOT NULL) "
        "OR (launch_provenance IN ('pending', 'unknown') "
        "AND launch_snapshot_digest IS NULL)",
    )

    # Draft state is current, authoritative configuration and can still be frozen.
    # Every run that crossed the launch boundary before this migration remains unknown:
    # current rows cannot prove what was present when that historical launch happened.
    op.execute(
        sa.text(
            """
            UPDATE exercise
            SET launch_provenance = CASE
                WHEN state = 'draft'::exercisestate
                    THEN 'pending'::snapshotprovenance
                ELSE 'unknown'::snapshotprovenance
            END
            """
        )
    )
    op.alter_column(
        "exercise",
        "launch_provenance",
        nullable=False,
        server_default="pending",
    )

    for table_name in ("inject", "communication", "exercisemember"):
        op.add_column(
            table_name,
            sa.Column("created_during_run", sa.Boolean(), nullable=True),
        )
        op.execute(
            sa.text(
                f"""
                UPDATE {table_name} AS child
                SET created_during_run = false
                FROM exercise AS parent
                WHERE child.exercise_id = parent.id
                  AND parent.state = 'draft'::exercisestate
                """
            )
        )
        # No database default is deliberate. The new application always supplies an
        # origin. A previous-version replica serving during rollout omits this new
        # column and therefore records NULL (unknown), never a false launch claim.

    op.add_column(
        "inject",
        sa.Column("attachment_sha256", sa.String(length=64), nullable=True),
    )

    # Snapshots have no update/delete API, and this trigger makes that invariant true
    # even for accidental ORM or operator writes. A schema migration can explicitly
    # replace the trigger if the versioned representation ever needs to evolve.
    op.execute(
        sa.text(
            """
            CREATE FUNCTION reject_exercise_launch_snapshot_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'exercise launch snapshots are immutable';
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER exercise_launch_snapshot_immutable
            BEFORE UPDATE OR DELETE ON exercise_launch_snapshot
            FOR EACH ROW EXECUTE FUNCTION reject_exercise_launch_snapshot_mutation()
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER exercise_launch_snapshot_immutable ON exercise_launch_snapshot"
        )
    )
    op.execute(sa.text("DROP FUNCTION reject_exercise_launch_snapshot_mutation()"))
    op.drop_column("inject", "attachment_sha256")
    for table_name in ("exercisemember", "communication", "inject"):
        op.drop_column(table_name, "created_during_run")
    op.drop_index("ix_exercise_launch_snapshot_digest", table_name="exercise")
    op.drop_constraint(
        "ck_exercise_launch_provenance_digest",
        "exercise",
        type_="check",
    )
    op.drop_constraint(
        "fk_exercise_launch_snapshot_digest",
        "exercise",
        type_="foreignkey",
    )
    op.drop_column("exercise", "launch_snapshot_digest")
    op.drop_column("exercise", "launch_provenance")
    op.drop_table("exercise_launch_snapshot")
    postgresql.ENUM(name="snapshotprovenance").drop(op.get_bind(), checkfirst=True)
