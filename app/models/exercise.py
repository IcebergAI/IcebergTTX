from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Optional

from sqlalchemy import CheckConstraint, Column, DateTime, Index
from sqlmodel import Field, Relationship, SQLModel

from app.models.user import UserRole

if TYPE_CHECKING:
    from app.models.communication import Communication
    from app.models.inject import Inject
    from app.models.inject_comment import InjectComment
    from app.models.report_summary import ExecutiveSummary
    from app.models.response import Response
    from app.models.scenario import Scenario
    from app.models.suggested_inject import SuggestedInject


class ExerciseState(StrEnum):
    draft = "draft"
    active = "active"
    paused = "paused"
    completed = "completed"


# Valid one-step transitions
VALID_TRANSITIONS: dict[ExerciseState, set[ExerciseState]] = {
    ExerciseState.draft: {ExerciseState.active},
    ExerciseState.active: {ExerciseState.paused, ExerciseState.completed},
    ExerciseState.paused: {ExerciseState.active, ExerciseState.completed},
    ExerciseState.completed: set(),
}

TRANSITION_ACTIONS: dict[tuple[ExerciseState, ExerciseState], str] = {
    (ExerciseState.draft, ExerciseState.active): "exercise.start",
    (ExerciseState.active, ExerciseState.paused): "exercise.pause",
    (ExerciseState.paused, ExerciseState.active): "exercise.resume",
    (ExerciseState.active, ExerciseState.completed): "exercise.complete",
    (ExerciseState.paused, ExerciseState.completed): "exercise.complete",
}


def transition_action(from_state: ExerciseState, to_state: ExerciseState) -> str:
    """Return the canonical audit/timeline action for a valid transition."""
    return TRANSITION_ACTIONS[(from_state, to_state)]


class Exercise(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    scenario_id: int = Field(foreign_key="scenario.id")
    title: str
    state: ExerciseState = Field(default=ExerciseState.draft)
    current_node_id: str | None = None  # tracks active inject in the scenario tree
    llm_enabled: bool = Field(default=False)
    # Facilitator's live/after-action observations (#112) — the raw material of the
    # after-action report. Owner-only; never exposed to participants/observers.
    debrief_notes: str | None = None
    started_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    ended_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    created_by: int = Field(foreign_key="user.id")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )

    scenario: Optional["Scenario"] = Relationship(back_populates="exercises")
    injects: list["Inject"] = Relationship(back_populates="exercise", cascade_delete=True)
    responses: list["Response"] = Relationship(back_populates="exercise", cascade_delete=True)
    members: list["ExerciseMember"] = Relationship(
        back_populates="exercise", cascade_delete=True
    )
    communications: list["Communication"] = Relationship(
        back_populates="exercise", cascade_delete=True
    )
    inject_comments: list["InjectComment"] = Relationship(
        back_populates="exercise", cascade_delete=True
    )
    suggested_injects: list["SuggestedInject"] = Relationship(
        back_populates="exercise", cascade_delete=True
    )
    executive_summary: Optional["ExecutiveSummary"] = Relationship(
        back_populates="exercise",
        cascade_delete=True,
        sa_relationship_kwargs={"uselist": False},
    )
    state_transitions: list["ExerciseStateTransition"] = Relationship(
        back_populates="exercise", cascade_delete=True
    )


class ExerciseMember(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    exercise_id: int = Field(foreign_key="exercise.id", ondelete="CASCADE")
    user_id: int = Field(foreign_key="user.id")
    group_id: str | None = None
    # Immutable attendance metadata: reporting must not be rewritten when an
    # administrator later changes the user's global role. Removing and re-enrolling
    # a user intentionally captures a new snapshot.
    role_at_enrolment: UserRole
    joined_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )

    exercise: Optional["Exercise"] = Relationship(back_populates="members")


class ExerciseStateTransition(SQLModel, table=True):
    """Append-only lifecycle history committed atomically with the Exercise row."""

    __table_args__ = (
        CheckConstraint("from_state <> to_state", name="ck_exercise_transition_changes_state"),
        Index(
            "ix_exercisestatetransition_exercise_time",
            "exercise_id",
            "transitioned_at",
            "id",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    exercise_id: int = Field(foreign_key="exercise.id", ondelete="CASCADE")
    from_state: ExerciseState
    to_state: ExerciseState
    actor_id: int | None = Field(default=None, foreign_key="user.id", ondelete="SET NULL")
    transitioned_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    exercise: Optional["Exercise"] = Relationship(back_populates="state_transitions")
