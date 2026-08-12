"""Record explicit inject schedule overrides (#315)."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inject",
        sa.Column(
            "release_offset_explicit", sa.Boolean(), nullable=True, server_default=sa.false()
        ),
    )
    # Rows that predate this provenance field cannot distinguish a scenario default
    # from a facilitator override. NULL is an intentional safe-unknown state; the
    # synchronizer preserves those values until a facilitator makes a new choice.
    op.execute(
        sa.text("UPDATE inject SET release_offset_explicit = NULL WHERE scenario_seeded = TRUE")
    )


def downgrade() -> None:
    op.drop_column("inject", "release_offset_explicit")
