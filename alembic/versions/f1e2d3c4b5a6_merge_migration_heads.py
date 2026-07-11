"""merge existing migration heads

Revision ID: f1e2d3c4b5a6
Revises: a4b5c6d7e8f9, b5c6d7e8f9a0, f7a8b9c0d1e2
Create Date: 2026-07-12 10:00:00.000000
"""

from collections.abc import Sequence

revision: str = "f1e2d3c4b5a6"
down_revision: tuple[str, str, str] = (
    "a4b5c6d7e8f9",
    "b5c6d7e8f9a0",
    "f7a8b9c0d1e2",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
