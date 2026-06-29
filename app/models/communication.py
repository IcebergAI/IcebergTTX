from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from app.models.exercise import Exercise
    from app.models.inject import Inject


class CommDirection(StrEnum):
    inbound = "inbound"
    outbound = "outbound"


class Communication(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    exercise_id: int = Field(foreign_key="exercise.id", ondelete="CASCADE")
    sender_id: int | None = Field(default=None, foreign_key="user.id", ondelete="SET NULL")
    sender_team: str | None = None
    direction: CommDirection
    external_entity: str | None = None      # e.g. "ICO", "NCSC", "CEO"
    subject: str
    body: str
    triggered_by_inject_id: int | None = Field(
        default=None, foreign_key="inject.id", ondelete="SET NULL"
    )
    visible_to_teams: str | None = None     # JSON list; None = all teams
    sent_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )
    read_by: str | None = None              # JSON list of user ids

    exercise: Optional["Exercise"] = Relationship(back_populates="communications")
    triggered_by_inject: Optional["Inject"] = Relationship(back_populates="communications")
