from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlmodel import Field, SQLModel


class AuditEvent(SQLModel, table=True):
    """Append-only record of a security-relevant action (#23).

    Never store secrets or payload bodies here — only identifiers and metadata.
    """

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        index=True,
        sa_type=DateTime(timezone=True),
    )
    request_id: str | None = Field(default=None, index=True)
    source_ip: str | None = None
    method: str | None = None
    path: str | None = None
    action: str = Field(index=True)
    result: str = "success"  # success | fail | deny
    actor_id: int | None = Field(default=None, index=True)
    actor_email: str | None = None
    actor_role: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    reason: str | None = None
    severity: str = "info"  # info | warning | critical
    security_relevant: bool = True
