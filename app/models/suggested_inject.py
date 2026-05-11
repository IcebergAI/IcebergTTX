from datetime import UTC, datetime
from enum import StrEnum

from sqlmodel import Field, SQLModel


class SuggestedInjectStatus(StrEnum):
    pending_review = "pending_review"
    approved = "approved"
    rejected = "rejected"


class SuggestedInject(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    exercise_id: int = Field(foreign_key="exercise.id")
    triggered_by_response_id: int = Field(foreign_key="response.id")
    title: str
    content: str
    target_teams: str | None = None  # JSON list
    llm_model: str
    status: SuggestedInjectStatus = Field(default=SuggestedInjectStatus.pending_review)
    reviewed_by: int | None = Field(default=None, foreign_key="user.id")
    reviewed_at: datetime | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
