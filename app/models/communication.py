from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Column, DateTime, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from app.models.exercise import Exercise
    from app.models.inject import Inject


class CommDirection(StrEnum):
    inbound = "inbound"
    outbound = "outbound"


class Communication(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint(
            "exercise_id",
            "trigger_key",
            name="uq_communication_exercise_trigger_key",
        ),
    )
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
    # Durable idempotency key for node-level scenario-triggered communications (#140).
    trigger_key: str | None = None
    visible_to_teams: list[str] | None = Field(  # None = all teams
        default=None, sa_column=Column(JSONB)
    )
    sent_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )
    exercise: Optional["Exercise"] = Relationship(back_populates="communications")
    triggered_by_inject: Optional["Inject"] = Relationship(back_populates="communications")
    reads: list["CommunicationRead"] = Relationship(
        back_populates="communication", cascade_delete=True
    )


class CommunicationRead(SQLModel, table=True):
    """One immutable first-read receipt per communication and user."""

    __table_args__ = (
        Index(
            "ix_communicationread_user_communication",
            "user_id",
            "communication_id",
        ),
    )

    communication_id: int = Field(
        foreign_key="communication.id",
        ondelete="CASCADE",
        primary_key=True,
    )
    user_id: int = Field(
        foreign_key="user.id",
        ondelete="CASCADE",
        primary_key=True,
    )
    read_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    communication: Optional["Communication"] = Relationship(back_populates="reads")
