# pyright: reportArgumentType=false
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import CheckConstraint, Column, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class ExerciseLaunchSnapshot(SQLModel, table=True):
    """Content-addressed, immutable launch configuration shared by equal runs."""

    __tablename__ = "exercise_launch_snapshot"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        CheckConstraint(
            "digest ~ '^[0-9a-f]{64}$'",
            name="ck_exercise_launch_snapshot_digest",
        ),
    )

    digest: str = Field(primary_key=True, max_length=64)
    schema_version: str = Field(default="1.0", max_length=20)
    content: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
