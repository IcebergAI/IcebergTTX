"""add LLM result identity constraints

Revision ID: e0f1a2b3c4d5
Revises: b5c6d7e8f9a0
Create Date: 2026-07-12 00:00:00.000000
"""
from collections.abc import Sequence

from alembic import op

revision: str = "e0f1a2b3c4d5"
down_revision: str | None = "b5c6d7e8f9a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint("uq_assessment_response", "responseassessment", ["response_id"])
    op.create_unique_constraint(
        "uq_suggested_inject_response", "suggestedinject", ["triggered_by_response_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_suggested_inject_response", "suggestedinject", type_="unique")
    op.drop_constraint("uq_assessment_response", "responseassessment", type_="unique")
