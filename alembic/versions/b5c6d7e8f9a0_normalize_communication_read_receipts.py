"""normalize communication read receipts

Revision ID: b5c6d7e8f9a0
Revises: 9c4f2a7d1e30
Create Date: 2026-07-11 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5c6d7e8f9a0"
down_revision: str | None = "9c4f2a7d1e30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "communicationread",
        sa.Column("communication_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["communication_id"],
            ["communication.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("communication_id", "user_id"),
    )
    op.create_index(
        "ix_communicationread_user_communication",
        "communicationread",
        ["user_id", "communication_id"],
        unique=False,
    )

    # Legacy JSONB stored user IDs but no timestamp. Preserve every valid user
    # reference and use migration time as the first timestamp we can know exactly.
    op.execute(
        sa.text(
            """
            INSERT INTO communicationread (communication_id, user_id, read_at)
            SELECT DISTINCT communication.id, account.id, CURRENT_TIMESTAMP
            FROM communication
            CROSS JOIN LATERAL jsonb_array_elements_text(
                COALESCE(communication.read_by, '[]'::jsonb)
            ) AS legacy(user_id)
            JOIN "user" AS account
              ON legacy.user_id ~ '^[0-9]+$'
             AND account.id = legacy.user_id::integer
            ON CONFLICT (communication_id, user_id) DO NOTHING
            """
        )
    )
    op.drop_column("communication", "read_by")


def downgrade() -> None:
    op.add_column(
        "communication",
        sa.Column("read_by", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE communication
            SET read_by = receipts.user_ids
            FROM (
                SELECT
                    communication_id,
                    to_jsonb(array_agg(user_id ORDER BY user_id)) AS user_ids
                FROM communicationread
                GROUP BY communication_id
            ) AS receipts
            WHERE communication.id = receipts.communication_id
            """
        )
    )
    op.drop_index(
        "ix_communicationread_user_communication",
        table_name="communicationread",
    )
    op.drop_table("communicationread")
