from datetime import UTC, datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from app.models.assessment import ResponseAssessment
    from app.models.exercise import Exercise
    from app.models.inject import Inject
    from app.models.suggested_inject import SuggestedInject


class Response(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint(
            "exercise_id",
            "inject_id",
            "user_id",
            name="uq_response_exercise_inject_user",
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    inject_id: int = Field(foreign_key="inject.id", ondelete="CASCADE")
    exercise_id: int = Field(foreign_key="exercise.id", ondelete="CASCADE")
    user_id: int = Field(foreign_key="user.id")
    group_id: str | None = None
    content: str
    selected_option: str | None = None  # option id from scenario definition
    submitted_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )
    # No FK constraint here — ResponseAssessment.response_id carries the relationship (avoids cycle)
    assessment_id: int | None = None

    exercise: Optional["Exercise"] = Relationship(back_populates="responses")
    inject: Optional["Inject"] = Relationship(back_populates="responses")
    assessment: Optional["ResponseAssessment"] = Relationship(
        back_populates="response",
        cascade_delete=True,
        sa_relationship_kwargs={"uselist": False},
    )
    suggested_injects: list["SuggestedInject"] = Relationship(
        back_populates="triggered_by_response", cascade_delete=True
    )
