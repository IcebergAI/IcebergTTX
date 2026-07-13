"""add llm settings

Revision ID: e8f0a2b4c6d8
Revises: d7e9f1a3b5c7
Create Date: 2026-07-13 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8f0a2b4c6d8"
down_revision: str | None = "d7e9f1a3b5c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llmsettings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("llm_provider", sa.String(), nullable=False),
        sa.Column("llm_max_tokens", sa.Integer(), nullable=False),
        sa.Column("anthropic_model", sa.String(), nullable=False),
        sa.Column("bedrock_model", sa.String(), nullable=False),
        sa.Column("bedrock_aws_region", sa.String(), nullable=False),
        sa.Column("openai_model", sa.String(), nullable=False),
        sa.Column("openai_base_url", sa.String(), nullable=False),
        sa.Column("ollama_model", sa.String(), nullable=False),
        sa.Column("ollama_base_url", sa.String(), nullable=False),
        sa.Column("gemini_model", sa.String(), nullable=False),
        sa.Column("gemini_base_url", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("llmsettings")
