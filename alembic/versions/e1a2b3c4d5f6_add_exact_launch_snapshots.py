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
        # No database default is deliberate. New code supplies an origin. The trigger
        # below classifies rows omitted by a previous-version replica against a locked
        # parent state, so a child insert cannot race across the launch boundary.

    op.execute(
        sa.text(
            """
            CREATE FUNCTION classify_exercise_child_origin()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                parent_state exercisestate;
            BEGIN
                IF NEW.created_during_run IS NOT NULL THEN
                    RETURN NEW;
                END IF;

                SELECT state
                INTO parent_state
                FROM exercise
                WHERE id = NEW.exercise_id
                FOR UPDATE;

                IF FOUND THEN
                    NEW.created_during_run := parent_state <> 'draft'::exercisestate;
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )
    for table_name in ("inject", "communication", "exercisemember"):
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {table_name}_classify_origin
                BEFORE INSERT ON {table_name}
                FOR EACH ROW EXECUTE FUNCTION classify_exercise_child_origin()
                """
            )
        )

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

    # The previous release remains live briefly during a rolling update. It knows
    # neither the snapshot columns nor the freeze operation, so database enforcement
    # must reject its draft -> active write rather than silently creating a modern run
    # with pending provenance. Deferral permits the new application to flush the
    # snapshot link before its compare-and-swap changes the lifecycle state.
    op.execute(
        sa.text(
            """
            CREATE FUNCTION enforce_exercise_launch_state_consistency()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                final_state exercisestate;
                final_provenance snapshotprovenance;
                final_digest varchar(64);
                final_title text;
                final_llm_enabled boolean;
                frozen_title text;
                frozen_llm_enabled boolean;
            BEGIN
                SELECT state, launch_provenance, launch_snapshot_digest,
                       title, llm_enabled
                INTO final_state, final_provenance, final_digest,
                     final_title, final_llm_enabled
                FROM exercise
                WHERE id = NEW.id;

                IF NOT FOUND THEN
                    RETURN NULL;
                END IF;

                IF (
                    final_state = 'draft'::exercisestate
                    AND final_provenance IS DISTINCT FROM 'pending'::snapshotprovenance
                ) OR (
                    final_state <> 'draft'::exercisestate
                    AND final_provenance = 'pending'::snapshotprovenance
                ) THEN
                    RAISE EXCEPTION
                        'exercise launch provenance is inconsistent with lifecycle state';
                END IF;

                IF final_provenance = 'exact'::snapshotprovenance THEN
                    SELECT
                        content #>> '{exercise,title}',
                        (content #>> '{exercise,llm_enabled}')::boolean
                    INTO frozen_title, frozen_llm_enabled
                    FROM exercise_launch_snapshot
                    WHERE digest = final_digest;

                    IF frozen_title IS DISTINCT FROM final_title
                       OR frozen_llm_enabled IS DISTINCT FROM final_llm_enabled THEN
                        RAISE EXCEPTION
                            'exercise launch settings diverge from the exact snapshot';
                    END IF;
                END IF;
                RETURN NULL;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE CONSTRAINT TRIGGER exercise_launch_state_consistent
            AFTER INSERT OR UPDATE ON exercise
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION enforce_exercise_launch_state_consistency()
            """
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION reject_exact_exercise_launch_setting_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF OLD.launch_provenance = 'exact'::snapshotprovenance AND (
                    NEW.scenario_id IS DISTINCT FROM OLD.scenario_id
                    OR NEW.title IS DISTINCT FROM OLD.title
                    OR NEW.llm_enabled IS DISTINCT FROM OLD.llm_enabled
                    OR NEW.launch_provenance IS DISTINCT FROM OLD.launch_provenance
                    OR NEW.launch_snapshot_digest IS DISTINCT FROM OLD.launch_snapshot_digest
                ) THEN
                    RAISE EXCEPTION 'exact exercise launch settings are immutable';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER exact_exercise_launch_settings_immutable
            BEFORE UPDATE ON exercise
            FOR EACH ROW EXECUTE FUNCTION reject_exact_exercise_launch_setting_mutation()
            """
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION reject_exact_launch_inject_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                parent_provenance snapshotprovenance;
            BEGIN
                SELECT launch_provenance
                INTO parent_provenance
                FROM exercise
                WHERE id = OLD.exercise_id;

                IF parent_provenance = 'exact'::snapshotprovenance
                   AND OLD.created_during_run IS FALSE THEN
                    IF TG_OP = 'DELETE' OR (
                        NEW.exercise_id IS DISTINCT FROM OLD.exercise_id
                        OR NEW.scenario_node_id IS DISTINCT FROM OLD.scenario_node_id
                        OR NEW.title IS DISTINCT FROM OLD.title
                        OR NEW.content IS DISTINCT FROM OLD.content
                        OR NEW.target_teams IS DISTINCT FROM OLD.target_teams
                        OR NEW.group_id IS DISTINCT FROM OLD.group_id
                        OR NEW.sequence_order IS DISTINCT FROM OLD.sequence_order
                        OR NEW.release_offset_minutes IS DISTINCT FROM OLD.release_offset_minutes
                        OR NEW.attachment_filename IS DISTINCT FROM OLD.attachment_filename
                        OR NEW.attachment_content_type IS DISTINCT FROM OLD.attachment_content_type
                        OR NEW.attachment_path IS DISTINCT FROM OLD.attachment_path
                        OR NEW.attachment_size IS DISTINCT FROM OLD.attachment_size
                        OR NEW.attachment_sha256 IS DISTINCT FROM OLD.attachment_sha256
                        OR NEW.created_during_run IS DISTINCT FROM OLD.created_during_run
                    ) THEN
                        RAISE EXCEPTION 'launch-configured inject fields are immutable';
                    END IF;
                END IF;
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER exact_launch_inject_immutable
            BEFORE UPDATE OR DELETE ON inject
            FOR EACH ROW EXECUTE FUNCTION reject_exact_launch_inject_mutation()
            """
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION reject_exact_prepared_communication_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                parent_provenance snapshotprovenance;
            BEGIN
                SELECT launch_provenance
                INTO parent_provenance
                FROM exercise
                WHERE id = OLD.exercise_id;

                IF parent_provenance = 'exact'::snapshotprovenance
                   AND OLD.created_during_run IS FALSE THEN
                    RAISE EXCEPTION 'launch-prepared communications are immutable';
                END IF;
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER exact_prepared_communication_immutable
            BEFORE UPDATE OR DELETE ON communication
            FOR EACH ROW EXECUTE FUNCTION reject_exact_prepared_communication_mutation()
            """
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION reject_exact_member_identity_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                parent_provenance snapshotprovenance;
            BEGIN
                SELECT launch_provenance
                INTO parent_provenance
                FROM exercise
                WHERE id = OLD.exercise_id;

                IF parent_provenance = 'exact'::snapshotprovenance
                   AND OLD.created_during_run IS FALSE AND (
                        NEW.exercise_id IS DISTINCT FROM OLD.exercise_id
                        OR NEW.user_id IS DISTINCT FROM OLD.user_id
                        OR NEW.role_at_enrolment IS DISTINCT FROM OLD.role_at_enrolment
                        OR NEW.created_during_run IS DISTINCT FROM OLD.created_during_run
                   ) THEN
                    RAISE EXCEPTION 'launch member identity and role are immutable';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER exact_member_identity_immutable
            BEFORE UPDATE ON exercisemember
            FOR EACH ROW EXECUTE FUNCTION reject_exact_member_identity_mutation()
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
    op.execute(sa.text("DROP TRIGGER exact_member_identity_immutable ON exercisemember"))
    op.execute(sa.text("DROP FUNCTION reject_exact_member_identity_mutation()"))
    op.execute(
        sa.text("DROP TRIGGER exact_prepared_communication_immutable ON communication")
    )
    op.execute(sa.text("DROP FUNCTION reject_exact_prepared_communication_mutation()"))
    op.execute(sa.text("DROP TRIGGER exact_launch_inject_immutable ON inject"))
    op.execute(sa.text("DROP FUNCTION reject_exact_launch_inject_mutation()"))
    op.execute(
        sa.text("DROP TRIGGER exact_exercise_launch_settings_immutable ON exercise")
    )
    op.execute(sa.text("DROP FUNCTION reject_exact_exercise_launch_setting_mutation()"))
    op.execute(sa.text("DROP TRIGGER exercise_launch_state_consistent ON exercise"))
    op.execute(sa.text("DROP FUNCTION enforce_exercise_launch_state_consistency()"))
    op.execute(
        sa.text(
            "DROP TRIGGER exercise_launch_snapshot_immutable ON exercise_launch_snapshot"
        )
    )
    op.execute(sa.text("DROP FUNCTION reject_exercise_launch_snapshot_mutation()"))
    op.drop_column("inject", "attachment_sha256")
    for table_name in ("exercisemember", "communication", "inject"):
        op.execute(sa.text(f"DROP TRIGGER {table_name}_classify_origin ON {table_name}"))
        op.drop_column(table_name, "created_during_run")
    op.execute(sa.text("DROP FUNCTION classify_exercise_child_origin()"))
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
