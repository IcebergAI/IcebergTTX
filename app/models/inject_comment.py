from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


class InjectComment(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    inject_id: int = Field(foreign_key="inject.id")
    exercise_id: int = Field(foreign_key="exercise.id")
    user_id: int = Field(foreign_key="user.id")
    group_id: str | None = None
    content: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
