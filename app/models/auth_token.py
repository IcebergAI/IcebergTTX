from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import Column, DateTime
from sqlmodel import Field, SQLModel


class AuthTokenPurpose(StrEnum):
    password_reset = "password_reset"  # nosec B105 - enum value, not a credential
    invite = "invite"


class AuthToken(SQLModel, table=True):
    """Single-use, expiring, hashed link token for email flows (#117).

    Only the SHA-256 hash of the random token is stored (`token_hash`) — the raw
    token exists only in the emailed link. `used_at` makes it single-use; `expires_at`
    bounds its lifetime. Serves both password reset (bound to `user_id`) and, later,
    invites (no user yet — `email` + optional `team`/`exercise_id` are pre-bound).
    """

    id: int | None = Field(default=None, primary_key=True)
    token_hash: str = Field(unique=True, index=True)
    purpose: AuthTokenPurpose
    email: str = Field(index=True)
    # Set for password reset (the account being reset); NULL for invites (no account yet).
    user_id: int | None = Field(default=None, foreign_key="user.id", ondelete="CASCADE")
    # Invite pre-binding (unused by reset).
    team: str | None = Field(default=None)
    exercise_id: int | None = Field(default=None, foreign_key="exercise.id", ondelete="SET NULL")
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    used_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
