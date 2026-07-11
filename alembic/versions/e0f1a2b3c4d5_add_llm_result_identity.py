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
    # Preserve the assessment already linked by Response when possible, otherwise
    # retain the earliest row. Older replay races could leave more than one.
    op.execute(
        """
        WITH ranked AS (
            SELECT assessment.id,
                   row_number() OVER (
                       PARTITION BY assessment.response_id
                       ORDER BY CASE WHEN response.assessment_id = assessment.id THEN 0 ELSE 1 END,
                                assessment.id
                   ) AS position
            FROM responseassessment AS assessment
            LEFT JOIN response ON response.id = assessment.response_id
        )
        DELETE FROM responseassessment AS duplicate
        USING ranked
        WHERE duplicate.id = ranked.id
          AND ranked.position > 1
        """
    )
    op.create_unique_constraint("uq_assessment_response", "responseassessment", ["response_id"])
    # Suggestions have no downstream FK selecting a canonical row. Prefer an
    # already-reviewed result over pending duplicates, then preserve the oldest.
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY triggered_by_response_id
                       ORDER BY CASE status::text
                                    WHEN 'approved' THEN 0
                                    WHEN 'rejected' THEN 1
                                    ELSE 2
                                END,
                                id
                   ) AS position
            FROM suggestedinject
        )
        DELETE FROM suggestedinject AS duplicate
        USING ranked
        WHERE duplicate.id = ranked.id
          AND ranked.position > 1
        """
    )
    op.create_unique_constraint(
        "uq_suggested_inject_response",
        "suggestedinject",
        ["triggered_by_response_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_suggested_inject_response", "suggestedinject", type_="unique"
    )
    op.drop_constraint("uq_assessment_response", "responseassessment", type_="unique")
