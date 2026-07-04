from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime
from sqlmodel import Field, SQLModel


class UserRole(StrEnum):
    facilitator = "facilitator"
    participant = "participant"
    observer = "observer"


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    display_name: str
    hashed_password: str
    role: UserRole = Field(default=UserRole.participant)
    team: str | None = None
    is_active: bool = Field(default=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )
    # Token revocation cutoff (#14): tokens issued (iat) before this instant are
    # rejected in get_current_user. Bumped on password change to invalidate all
    # previously-issued tokens ("change password to kick out an attacker").
    token_valid_after: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
