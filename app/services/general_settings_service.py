"""Manage the singleton GeneralSettings row and frozen runtime snapshot."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.general_settings import GeneralSettings

_SINGLETON_ID = 1
EDITABLE_FIELDS = (
    "registration_enabled",
    "access_token_expire_minutes",
    "audit_persist",
    "login_max_attempts",
    "login_lockout_seconds",
    "registration_max_attempts",
    "registration_lockout_seconds",
    "password_reset_max_attempts",
    "password_reset_lockout_seconds",
)


@dataclass(frozen=True)
class GeneralConfig:
    registration_enabled: bool
    access_token_expire_minutes: int
    audit_persist: bool
    login_max_attempts: int
    login_lockout_seconds: int
    registration_max_attempts: int
    registration_lockout_seconds: int
    password_reset_max_attempts: int
    password_reset_lockout_seconds: int


def _env_config() -> GeneralConfig:
    return GeneralConfig(**{field: getattr(settings, field) for field in EDITABLE_FIELDS})


def _to_config(row: GeneralSettings) -> GeneralConfig:
    return GeneralConfig(**{field: getattr(row, field) for field in EDITABLE_FIELDS})


_config: GeneralConfig | None = None


def get_config() -> GeneralConfig:
    """Return the cached DB snapshot, falling back to environment before startup load."""
    return _config or _env_config()


def set_config(config: GeneralConfig | None) -> None:
    global _config
    _config = config
    from app.services import rate_limit

    current = get_config()
    rate_limit.apply_config(
        login_max_attempts=current.login_max_attempts,
        login_lockout_seconds=current.login_lockout_seconds,
        registration_max_attempts=current.registration_max_attempts,
        registration_lockout_seconds=current.registration_lockout_seconds,
        password_reset_max_attempts=current.password_reset_max_attempts,
        password_reset_lockout_seconds=current.password_reset_lockout_seconds,
    )


async def get_settings(session: AsyncSession) -> GeneralSettings:
    """Return the singleton, lazily seeding it from deployment environment values."""
    row = await session.get(GeneralSettings, _SINGLETON_ID)
    if row is None:
        row = GeneralSettings(
            id=_SINGLETON_ID,
            **{field: getattr(settings, field) for field in EDITABLE_FIELDS},
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def update_settings(session: AsyncSession, changes: dict[str, Any]) -> GeneralSettings:
    row = await get_settings(session)
    for key in EDITABLE_FIELDS:
        if key in changes and changes[key] is not None:
            setattr(row, key, changes[key])
    row.updated_at = datetime.now(UTC)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    set_config(_to_config(row))
    return row


async def refresh_cache(session: AsyncSession) -> None:
    set_config(_to_config(await get_settings(session)))
