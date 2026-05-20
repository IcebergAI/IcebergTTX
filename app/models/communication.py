from datetime import UTC, datetime
from enum import StrEnum

from sqlmodel import Field, SQLModel


class CommDirection(StrEnum):
    inbound = "inbound"
    outbound = "outbound"


class Communication(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    exercise_id: int = Field(foreign_key="exercise.id")
    sender_id: int | None = Field(default=None, foreign_key="user.id")
    sender_team: str | None = None
    direction: CommDirection
    external_entity: str | None = None      # e.g. "ICO", "NCSC", "CEO"
    subject: str
    body: str
    triggered_by_inject_id: int | None = Field(default=None, foreign_key="inject.id")
    visible_to_teams: str | None = None     # JSON list; None = all teams
    sent_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    read_by: str | None = None              # JSON list of user ids
