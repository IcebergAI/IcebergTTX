"""add authtoken (single-use email link tokens, #117)

Revision ID: a2b3c4d5e6f7
Revises: f0a1b2c3d4e5
Create Date: 2026-07-12 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a2b3c4d5e6f7'
down_revision: str | None = "f0a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Single-use, expiring, hashed link tokens for password reset (and, later, invites).
    # Only the SHA-256 hash is stored; the raw token lives only in the emailed link.
    op.create_table(
        'authtoken',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            'purpose',
            sa.Enum('password_reset', 'invite', name='authtokenpurpose'),
            nullable=False,
        ),
        sa.Column('email', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('team', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('exercise_id', sa.Integer(), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['exercise_id'], ['exercise.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_authtoken_token_hash'), 'authtoken', ['token_hash'], unique=True)
    op.create_index(op.f('ix_authtoken_email'), 'authtoken', ['email'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_authtoken_email'), table_name='authtoken')
    op.drop_index(op.f('ix_authtoken_token_hash'), table_name='authtoken')
    op.drop_table('authtoken')
    sa.Enum(name='authtokenpurpose').drop(op.get_bind())
