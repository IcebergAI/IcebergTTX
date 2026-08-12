"""Track whether an inject row came from scenario materialization (#315)."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "a9c4e7f1b2d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inject",
        sa.Column("scenario_seeded", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "communication",
        sa.Column("audience_explicit", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Recover provenance for rows that clearly match the current scenario node
    # identity. Arbitrary facilitator-created node ids remain false by default.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE inject AS i
            SET scenario_seeded = TRUE
            FROM exercise AS e, scenario AS s
            WHERE i.exercise_id = e.id
              AND e.scenario_id = s.id
              AND i.scenario_node_id IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements((s.definition::jsonb)->'injects') AS node
                  WHERE node->>'id' = i.scenario_node_id
              )
            """
        )
    )


def downgrade() -> None:
    op.drop_column("communication", "audience_explicit")
    op.drop_column("inject", "scenario_seeded")
