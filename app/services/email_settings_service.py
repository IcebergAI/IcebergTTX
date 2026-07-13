"""Manage the singleton EmailSettings row and mailer's runtime cache (#186)."""

from datetime import UTC, datetime
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.email_settings import EmailSettings
from app.services import mail_service

_SINGLETON_ID = 1
EDITABLE_FIELDS = (
    "enabled",
    "smtp_host",
    "smtp_port",
    "smtp_from",
    "smtp_username",
    "smtp_starttls",
    "smtp_tls",
    "public_base_url",
)


def _to_config(row: EmailSettings) -> mail_service.MailConfig:
    return mail_service.MailConfig(
        enabled=row.enabled,
        smtp_host=row.smtp_host,
        smtp_port=row.smtp_port,
        smtp_from=row.smtp_from,
        smtp_username=row.smtp_username,
        smtp_starttls=row.smtp_starttls,
        smtp_tls=row.smtp_tls,
        public_base_url=row.public_base_url,
    )


async def get_settings(session: AsyncSession) -> EmailSettings:
    """Return the singleton, lazily seeding non-secret values from the environment."""
    row = await session.get(EmailSettings, _SINGLETON_ID)
    if row is None:
        row = EmailSettings(
            id=_SINGLETON_ID,
            enabled=settings.smtp_enabled,
            smtp_host=settings.smtp_host,
            smtp_port=settings.smtp_port,
            smtp_from=settings.smtp_from,
            smtp_username=settings.smtp_username,
            smtp_starttls=settings.smtp_starttls,
            smtp_tls=settings.smtp_tls,
            public_base_url=settings.public_base_url,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def update_settings(session: AsyncSession, changes: dict[str, Any]) -> EmailSettings:
    row = await get_settings(session)
    for key in EDITABLE_FIELDS:
        if key in changes and changes[key] is not None:
            setattr(row, key, changes[key])
    row.updated_at = datetime.now(UTC)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    mail_service.set_config(_to_config(row))
    return row


async def refresh_cache(session: AsyncSession) -> None:
    mail_service.set_config(_to_config(await get_settings(session)))
