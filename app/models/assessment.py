# pyright: reportArgumentType=false
# SQLModel's Field stub is narrower than its runtime SQLAlchemy type support.
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from app.models.response import Response


class ResponseAssessment(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("response_id", name="uq_assessment_response"),)
    id: int | None = Field(default=None, primary_key=True)
    response_id: int = Field(foreign_key="response.id", ondelete="CASCADE")
    llm_model: str
    assessment_text: str
    decision_quality: str | None = None          # "good" | "adequate" | "poor"
    recommended_branch_option_id: str | None = None  # maps to a scenario option id
    assessed_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )

    response: Optional["Response"] = Relationship(back_populates="assessment")
