# pyright: reportArgumentType=false
from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlmodel import Field, SQLModel


class EmailSettings(SQLModel, table=True):
    """Admin-editable, non-secret SMTP configuration (#186)."""

    id: int | None = Field(default=None, primary_key=True)
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_from: str = ""
    smtp_username: str = ""
    smtp_starttls: bool = True
    smtp_tls: bool = False
    public_base_url: str = ""
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )
