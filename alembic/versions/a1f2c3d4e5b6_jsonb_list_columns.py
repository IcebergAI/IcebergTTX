"""convert JSON-text list columns to JSONB

Retype the small list columns previously stored as JSON strings in VARCHAR
(target_teams / visible_to_teams / read_by / tags) to native ``jsonb`` so the
app can read/write Python lists directly instead of json.dumps/loads round-trips.
Existing rows already hold valid JSON text, so ``USING <col>::jsonb`` converts
them in place; NULLs stay NULL. ``scenario.definition`` is intentionally left as
validated text (it is parsed via the ScenarioDefinition model, not indexed).

Revision ID: a1f2c3d4e5b6
Revises: e5b99d4c11c0
Create Date: 2026-07-05 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1f2c3d4e5b6'
down_revision: str | None = 'e5b99d4c11c0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, column) pairs to convert.
_COLUMNS = [
    ('scenario', 'tags'),
    ('inject', 'target_teams'),
    ('communication', 'visible_to_teams'),
    ('communication', 'read_by'),
    ('suggestedinject', 'target_teams'),
]


def upgrade() -> None:
    for table, column in _COLUMNS:
        op.alter_column(
            table,
            column,
            type_=postgresql.JSONB(),
            postgresql_using=f'{column}::jsonb',
            existing_nullable=True,
        )


def downgrade() -> None:
    for table, column in _COLUMNS:
        op.alter_column(
            table,
            column,
            type_=sa.String(),
            postgresql_using=f'{column}::text',
            existing_nullable=True,
        )
