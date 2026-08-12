"""Capture immutable, content-addressed exercise run definitions (#315).

Revision ID: a9c4e7f1b2d6
Revises: d8e9f0a1b2c3
"""

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a9c4e7f1b2d6"
down_revision: str | None = "d8e9f0a1b2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "exercise",
        sa.Column("cloned_from_exercise_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_exercise_cloned_from_exercise",
        "exercise",
        "exercise",
        ["cloned_from_exercise_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_exercise_cloned_from_exercise_id", "exercise", ["cloned_from_exercise_id"])
    op.create_table(
        "exerciserunsnapshot",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("exercise_id", sa.Integer(), nullable=False),
        sa.Column("scenario_id", sa.Integer(), nullable=False),
        sa.Column("scenario_version", sa.String(), nullable=False),
        sa.Column("scenario_title", sa.String(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("configuration_json", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["exercise_id"], ["exercise.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["scenario_id"], ["scenario.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("exercise_id", name="uq_exerciserunsnapshot_exercise"),
    )
    op.create_index("ix_exerciserunsnapshot_exercise_id", "exerciserunsnapshot", ["exercise_id"])
    op.create_index(
        "ix_exerciserunsnapshot_content_sha256", "exerciserunsnapshot", ["content_sha256"]
    )
    op.create_index(
        "ix_exerciserunsnapshot_scenario_captured",
        "exerciserunsnapshot",
        ["scenario_id", "captured_at"],
    )
    # Existing launched runs need the same historical protection as new ones.
    # Use deterministic JSON serialization here rather than asking a migration to
    # import application models; Alembic must remain runnable in a minimal image.
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT e.id AS exercise_id, e.llm_enabled, s.id AS scenario_id,
                   s.version AS scenario_version, s.title AS scenario_title,
                   s.definition
            FROM exercise AS e
            JOIN scenario AS s ON s.id = e.scenario_id
            WHERE e.state IN ('active', 'paused', 'completed')
            """
        )
    ).mappings()
    for row in rows:
        definition = json.dumps(
            json.loads(row["definition"]),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        configuration = {
            # There was no launch-time configuration history before this
            # revision. The current mutable value cannot prove what governed a
            # historical run, so preserve that uncertainty instead of fabricating
            # provenance from the post-launch row.
            "llm_enabled": None,
            "llm_enabled_source": "unknown_legacy",
            "scenario_id": row["scenario_id"],
            "scenario_version": row["scenario_version"],
        }
        payload = json.dumps(
            {
                "configuration": configuration,
                "definition": json.loads(definition),
                "schema_version": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        bind.execute(
            sa.text(
                """
                INSERT INTO exerciserunsnapshot
                    (exercise_id, scenario_id, scenario_version, scenario_title,
                     schema_version, definition, configuration_json, content_sha256, captured_at)
                VALUES
                    (:exercise_id, :scenario_id, :scenario_version, :scenario_title,
                     1, :definition, :configuration_json, :content_sha256, now())
                """
            ),
            {
                "exercise_id": row["exercise_id"],
                "scenario_id": row["scenario_id"],
                "scenario_version": row["scenario_version"],
                "scenario_title": row["scenario_title"],
                "definition": definition,
                "configuration_json": json.dumps(
                    configuration, sort_keys=True, separators=(",", ":")
                ),
                "content_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            },
        )


def downgrade() -> None:
    op.drop_index("ix_exerciserunsnapshot_scenario_captured", table_name="exerciserunsnapshot")
    op.drop_index("ix_exerciserunsnapshot_content_sha256", table_name="exerciserunsnapshot")
    op.drop_index("ix_exerciserunsnapshot_exercise_id", table_name="exerciserunsnapshot")
    op.drop_table("exerciserunsnapshot")
    op.drop_index("ix_exercise_cloned_from_exercise_id", table_name="exercise")
    op.drop_constraint("fk_exercise_cloned_from_exercise", "exercise", type_="foreignkey")
    op.drop_column("exercise", "cloned_from_exercise_id")
