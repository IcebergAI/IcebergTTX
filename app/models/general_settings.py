# pyright: reportArgumentType=false
from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlmodel import Field, SQLModel


class GeneralSettings(SQLModel, table=True):
    """Admin-editable, non-secret application and throttle settings."""

    id: int | None = Field(default=None, primary_key=True)
    registration_enabled: bool = True
    access_token_expire_minutes: int = 480
    audit_persist: bool = True
    login_max_attempts: int = 5
    login_lockout_seconds: int = 300
    registration_max_attempts: int = 5
    registration_lockout_seconds: int = 3600
    password_reset_max_attempts: int = 5
    password_reset_lockout_seconds: int = 3600
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )
