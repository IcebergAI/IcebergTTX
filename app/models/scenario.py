from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


class Scenario(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    description: str | None = None
    version: str = "1.0"
    tags: str | None = None  # JSON-serialised list
    definition: str  # Full ScenarioDefinition JSON blob
    created_by: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
