# pyright: reportArgumentType=false
# SQLModel's Field stub is narrower than its runtime SQLAlchemy type support.
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Optional

from sqlalchemy import CheckConstraint, Column, DateTime, Index, Text, UniqueConstraint
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
    scenario_id: int = Field(foreign_key="scenario.id", index=True)
    title: str
    state: ExerciseState = Field(default=ExerciseState.draft)
    current_node_id: str | None = None  # tracks active inject in the scenario tree
    llm_enabled: bool = Field(default=False)
    # Facilitator's live/after-action observations (#112) — the raw material of the
    # after-action report. Owner-only; never exposed to participants/observers.
    debrief_notes: str | None = None
    started_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    ended_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    # Pause-aware clock (#116). `paused_at` is set while the exercise is paused (else
    # None); `accumulated_pause_seconds` is the total of all completed pause spans.
    # Effective elapsed = (now|paused_at|ended_at - started_at) - accumulated_pause_seconds.
    paused_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    accumulated_pause_seconds: float = Field(default=0.0)
    created_by: int = Field(foreign_key="user.id")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )
    # A draft deliberately has no immutable evidence yet. The first draft -> active
    # transition creates the one-to-one RunSnapshot in the *same* transaction as
    # the lifecycle event; every active/completed definition resolver then uses it.
    cloned_from_exercise_id: int | None = Field(
        default=None, foreign_key="exercise.id", ondelete="SET NULL", index=True
    )

    scenario: Optional["Scenario"] = Relationship(back_populates="exercises")
    injects: list["Inject"] = Relationship(back_populates="exercise", cascade_delete=True)
    responses: list["Response"] = Relationship(back_populates="exercise", cascade_delete=True)
    members: list["ExerciseMember"] = Relationship(back_populates="exercise", cascade_delete=True)
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
    progression: list["ExerciseProgress"] = Relationship(
        back_populates="exercise", cascade_delete=True
    )
    run_snapshot: Optional["ExerciseRunSnapshot"] = Relationship(
        back_populates="exercise",
        cascade_delete=True,
        sa_relationship_kwargs={"uselist": False},
    )


class ExerciseRunSnapshot(SQLModel, table=True):
    """Immutable, content-addressed scenario/configuration record for a launched run.

    The source Scenario remains a reusable library asset. This row is the evidence
    record used to interpret a live or historical Exercise, so it stores canonical
    JSON text rather than a foreign-key-only reference to mutable source content.
    """

    __table_args__ = (
        UniqueConstraint("exercise_id", name="uq_exerciserunsnapshot_exercise"),
        Index("ix_exerciserunsnapshot_content_sha256", "content_sha256"),
        Index("ix_exerciserunsnapshot_scenario_captured", "scenario_id", "captured_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    exercise_id: int = Field(foreign_key="exercise.id", ondelete="CASCADE", index=True)
    # Source identity is retained for provenance, but never dereferenced for a run.
    scenario_id: int = Field(foreign_key="scenario.id", ondelete="RESTRICT", index=True)
    scenario_version: str
    scenario_title: str
    schema_version: int = Field(default=1)
    definition: str = Field(sa_column=Column(Text, nullable=False))
    configuration_json: str = Field(sa_column=Column(Text, nullable=False))
    content_sha256: str = Field(index=True, min_length=64, max_length=64)
    captured_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )

    exercise: Optional["Exercise"] = Relationship(back_populates="run_snapshot")


class ExerciseMember(SQLModel, table=True):
    # The (exercise_id, user_id) pair is the app's hottest lookup — every
    # exercise_group_for_user() and sender-team resolution goes through it. Its leading
    # column also serves the exercise_id-only roster scan, so exercise_id needs no
    # index of its own. It is also an identity pair: enrolment must be unique per
    # (exercise, user), so a unique constraint (which backs the same lookup) replaces
    # the plain index and closes the concurrent-enrolment duplicate race (#262) at the
    # DB level, matching Response/ExerciseProgress/InjectProgress and friends.
    __table_args__ = (
        UniqueConstraint("exercise_id", "user_id", name="uq_exercisemember_exercise_user"),
    )

    id: int | None = Field(default=None, primary_key=True)
    exercise_id: int = Field(foreign_key="exercise.id", ondelete="CASCADE")
    user_id: int = Field(foreign_key="user.id", index=True)
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


class ExerciseProgress(SQLModel, table=True):
    """Authoritative cursor for one exercise-wide or team-specific progression path."""

    __table_args__ = (
        UniqueConstraint(
            "exercise_id",
            "group_id",
            name="uq_exercise_progress_group",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_exerciseprogress_exercise_group", "exercise_id", "group_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    exercise_id: int = Field(foreign_key="exercise.id", ondelete="CASCADE")
    # Null denotes the shared/all-teams path. Team-specific paths may diverge.
    group_id: str | None = None
    current_node_id: str | None = None
    # Indexed for the SET NULL fan-out when an inject is deleted, not for a query.
    current_inject_id: int | None = Field(
        default=None, foreign_key="inject.id", ondelete="SET NULL", index=True
    )
    advanced_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )
    advanced_by: int | None = Field(default=None, foreign_key="user.id", ondelete="SET NULL")

    exercise: Optional["Exercise"] = Relationship(back_populates="progression")
