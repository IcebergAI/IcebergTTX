"""snapshot exercise member roles for stable attendance reports

Revision ID: a4b5c6d7e8f9
Revises: 9c4f2a7d1e30
Create Date: 2026-07-11 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4b5c6d7e8f9"
down_revision: str | None = "9c4f2a7d1e30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    user_role = postgresql.ENUM(
        "facilitator",
        "participant",
        "observer",
        name="userrole",
        create_type=False,
    )
    op.add_column(
        "exercisemember",
        sa.Column("role_at_enrolment", user_role, nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE exercisemember AS member
            SET role_at_enrolment = account.role
            FROM "user" AS account
            WHERE account.id = member.user_id
            """
        )
    )
    # The existing foreign key makes orphans impossible, but retain a deterministic
    # fallback for databases whose constraints were previously repaired or disabled.
    op.execute(
        sa.text(
            """
            UPDATE exercisemember
            SET role_at_enrolment = 'participant'::userrole
            WHERE role_at_enrolment IS NULL
            """
        )
    )
    op.alter_column("exercisemember", "role_at_enrolment", nullable=False)


def downgrade() -> None:
    op.drop_column("exercisemember", "role_at_enrolment")
