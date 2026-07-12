from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Column, DateTime, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from app.models.communication import Communication
    from app.models.exercise import Exercise
    from app.models.inject_comment import InjectComment
    from app.models.response import Response


class InjectState(StrEnum):
    pending = "pending"
    released = "released"
    resolved = "resolved"


class Inject(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    exercise_id: int = Field(foreign_key="exercise.id", ondelete="CASCADE")
    scenario_node_id: str | None = None   # links back to the ScenarioDefinition inject id
    title: str
    content: str
    target_teams: list[str] | None = Field(  # team IDs; None = all teams
        default=None, sa_column=Column(JSONB)
    )
    group_id: str | None = None           # exercise-scoped group; None = shared/all groups
    sequence_order: int = Field(default=0)
    # Scheduled release (#116): minutes after exercise start at which this inject
    # auto-releases. None = manual-only ("pull not push" default). Seeded from the
    # scenario node's `release_at_minutes`, but runtime-mutable (set/cancel per inject).
    release_offset_minutes: int | None = Field(default=None)
    state: InjectState = Field(default=InjectState.pending)
    released_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    released_by: int | None = Field(default=None, foreign_key="user.id", ondelete="SET NULL")
    resolved_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    resolved_by: int | None = Field(default=None, foreign_key="user.id", ondelete="SET NULL")
    resolution_reason: str | None = None
    attachment_filename: str | None = None
    attachment_content_type: str | None = None
    attachment_path: str | None = None
    attachment_size: int | None = None

    exercise: Optional["Exercise"] = Relationship(back_populates="injects")
    responses: list["Response"] = Relationship(back_populates="inject", cascade_delete=True)
    comments: list["InjectComment"] = Relationship(back_populates="inject", cascade_delete=True)
    # Communications are records in their own right — deleting an inject nulls the
    # back-reference (SET NULL) rather than deleting the communication.
    communications: list["Communication"] = Relationship(
        back_populates="triggered_by_inject",
        sa_relationship_kwargs={"passive_deletes": True},
    )


class InjectProgress(SQLModel, table=True):
    """Resolution state for one group moving through a released inject."""

    __table_args__ = (
        UniqueConstraint(
            "inject_id",
            "group_id",
            name="uq_inject_progress_group",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_injectprogress_inject_group", "inject_id", "group_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    exercise_id: int = Field(foreign_key="exercise.id", ondelete="CASCADE")
    inject_id: int = Field(foreign_key="inject.id", ondelete="CASCADE")
    group_id: str | None = None
    state: InjectState = Field(default=InjectState.released)
    resolved_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    resolved_by: int | None = Field(default=None, foreign_key="user.id", ondelete="SET NULL")
    resolution_reason: str | None = None
