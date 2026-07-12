"""add OIDC tenant and role provenance

Revision ID: f8a9b0c1d2e3
Revises: a2b3c4d5e6f7
Create Date: 2026-07-11 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f8a9b0c1d2e3'
down_revision: str | None = 'a2b3c4d5e6f7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'user',
        sa.Column('auth_tenant', sa.String(), nullable=True),
    )
    op.add_column(
        'user',
        sa.Column(
            'role_managed_by_idp',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Existing passwordless, non-admin external accounts match the old JIT shape;
    # adopt them into role synchronization. Local/password-linked accounts and
    # admins are conservative operator-managed overrides.
    op.execute(
        sa.text(
            """
            UPDATE "user"
            SET role_managed_by_idp = TRUE
            WHERE auth_provider <> 'local'
              AND hashed_password IS NULL
              AND is_admin IS FALSE
            """
        )
    )
    op.alter_column('user', 'role_managed_by_idp', server_default=None)


def downgrade() -> None:
    op.drop_column('user', 'role_managed_by_idp')
    op.drop_column('user', 'auth_tenant')
