from datetime import datetime
from enum import StrEnum

from sqlmodel import Field, SQLModel


class InjectState(StrEnum):
    pending = "pending"
    released = "released"
    resolved = "resolved"


class Inject(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    exercise_id: int = Field(foreign_key="exercise.id")
    scenario_node_id: str | None = None   # links back to the ScenarioDefinition inject id
    title: str
    content: str
    target_teams: str | None = None       # JSON list of team IDs; None = all teams
    group_id: str | None = None           # exercise-scoped group; None = shared/all groups
    sequence_order: int = Field(default=0)
    state: InjectState = Field(default=InjectState.pending)
    released_at: datetime | None = None
    released_by: int | None = Field(default=None, foreign_key="user.id")
