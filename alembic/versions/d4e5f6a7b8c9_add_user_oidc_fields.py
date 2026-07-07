"""add user OIDC identity fields (auth_provider/subject, nullable password, #25)

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-07 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: str | None = 'c3d4e5f6a7b8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # New external-identity columns. auth_provider backfills to "local" for every
    # existing row (server_default), then the default is dropped so the app-side
    # default governs going forward.
    op.add_column(
        'user',
        sa.Column(
            'auth_provider',
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
            server_default='local',
        ),
    )
    op.alter_column('user', 'auth_provider', server_default=None)
    op.create_index(op.f('ix_user_auth_provider'), 'user', ['auth_provider'], unique=False)
    op.add_column(
        'user',
        sa.Column('subject', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )
    op.create_unique_constraint('uq_user_provider_subject', 'user', ['auth_provider', 'subject'])
    # OIDC-provisioned accounts have no local password.
    op.alter_column('user', 'hashed_password', existing_type=sqlmodel.sql.sqltypes.AutoString(),
                    nullable=True)


def downgrade() -> None:
    op.alter_column('user', 'hashed_password', existing_type=sqlmodel.sql.sqltypes.AutoString(),
                    nullable=False)
    op.drop_constraint('uq_user_provider_subject', 'user', type_='unique')
    op.drop_column('user', 'subject')
    op.drop_index(op.f('ix_user_auth_provider'), table_name='user')
    op.drop_column('user', 'auth_provider')
