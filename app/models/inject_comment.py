from datetime import UTC, datetime
from typing import TYPE_CHECKING, Optional

from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from app.models.exercise import Exercise
    from app.models.inject import Inject


class InjectComment(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    inject_id: int = Field(foreign_key="inject.id", ondelete="CASCADE", index=True)
    exercise_id: int = Field(foreign_key="exercise.id", ondelete="CASCADE", index=True)
    user_id: int = Field(foreign_key="user.id")
    group_id: str | None = None
    content: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    inject: Optional["Inject"] = Relationship(back_populates="comments")
    exercise: Optional["Exercise"] = Relationship(back_populates="inject_comments")
