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
        sa.Column("scenario_seeded", sa.Boolean(), nullable=True, server_default=sa.false()),
    )
    op.add_column(
        "communication",
        sa.Column("audience_explicit", sa.Boolean(), nullable=True, server_default=sa.false()),
    )
    # Existing rows predate an explicit provenance bit.  Identity alone is not
    # evidence: the API has always allowed facilitator-authored injects to reuse a
    # scenario node id.  Preserve that uncertainty instead of classifying custom
    # content as seeded and deleting it on the next clone edit.
    op.execute(sa.text("UPDATE inject SET scenario_seeded = NULL"))
    # Existing rows predate this bit too.  An audience equal to every historical
    # team may still have been an explicit restriction, so false would permit a
    # later clone edit to expand it to newly added teams.  NULL means unknown and
    # is handled conservatively by reconciliation.
    op.execute(sa.text("UPDATE communication SET audience_explicit = NULL"))


def downgrade() -> None:
    op.drop_column("communication", "audience_explicit")
    op.drop_column("inject", "scenario_seeded")
