# pyright: reportArgumentType=false
# SQLModel's Field stub is narrower than its runtime SQLAlchemy type support.
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Column, DateTime, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from app.models.exercise import Exercise
    from app.models.response import Response


class SuggestedInjectStatus(StrEnum):
    pending_review = "pending_review"
    approved = "approved"
    rejected = "rejected"


class SuggestedInject(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint(
            "triggered_by_response_id",
            name="uq_suggested_inject_response",
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    exercise_id: int = Field(foreign_key="exercise.id", ondelete="CASCADE", index=True)
    triggered_by_response_id: int = Field(foreign_key="response.id", ondelete="CASCADE")
    title: str
    content: str
    target_teams: list[str] | None = Field(default=None, sa_column=Column(JSONB))
    llm_model: str
    status: SuggestedInjectStatus = Field(default=SuggestedInjectStatus.pending_review)
    reviewed_by: int | None = Field(default=None, foreign_key="user.id", ondelete="SET NULL")
    reviewed_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )

    exercise: Optional["Exercise"] = Relationship(back_populates="suggested_injects")
    triggered_by_response: Optional["Response"] = Relationship(
        back_populates="suggested_injects"
    )
