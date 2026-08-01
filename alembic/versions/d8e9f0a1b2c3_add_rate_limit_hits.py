"""Move rate-limit counters into an unlogged table (#213).

UNLOGGED is the point of writing this by hand rather than autogenerating it: these rows
are expendable throttle state, so keeping them out of the WAL is what makes a write per
login attempt acceptable. Postgres truncates the table after an unclean shutdown, which
at worst forgives some in-progress lockouts.

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d8e9f0a1b2c3"
down_revision: str | None = "c7d8e9f0a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE UNLOGGED TABLE rate_limit_hits (
                id serial PRIMARY KEY,
                scope varchar(32) NOT NULL,
                key varchar(320) NOT NULL,
                at timestamptz NOT NULL
            )
            """
        )
    )
    op.create_index(
        "ix_rate_limit_hits_scope_key_at", "rate_limit_hits", ["scope", "key", "at"]
    )


def downgrade() -> None:
    op.drop_index("ix_rate_limit_hits_scope_key_at", table_name="rate_limit_hits")
    op.drop_table("rate_limit_hits")
