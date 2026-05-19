from datetime import UTC, datetime
from enum import StrEnum

from sqlmodel import Field, SQLModel


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


class Exercise(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    scenario_id: int = Field(foreign_key="scenario.id")
    title: str
    state: ExerciseState = Field(default=ExerciseState.draft)
    current_node_id: str | None = None  # tracks active inject in the scenario tree
    llm_enabled: bool = Field(default=False)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    created_by: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ExerciseMember(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    exercise_id: int = Field(foreign_key="exercise.id")
    user_id: int = Field(foreign_key="user.id")
    group_id: str | None = None
    joined_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
