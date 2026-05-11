from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


class Response(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    inject_id: int = Field(foreign_key="inject.id")
    exercise_id: int = Field(foreign_key="exercise.id")
    user_id: int = Field(foreign_key="user.id")
    content: str
    selected_option: str | None = None  # option id from scenario definition
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # No FK constraint here — ResponseAssessment.response_id carries the relationship (avoids cycle)
    assessment_id: int | None = None
