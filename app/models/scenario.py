from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from app.models.exercise import Exercise


class Scenario(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    description: str | None = None
    version: str = "1.0"
    tags: str | None = None  # JSON-serialised list
    definition: str  # Full ScenarioDefinition JSON blob
    created_by: int = Field(foreign_key="user.id")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )

    # Navigation only — deleting a scenario in use is blocked by a route guard,
    # never cascaded (it would destroy live exercise data).
    exercises: list["Exercise"] = Relationship(back_populates="scenario")
