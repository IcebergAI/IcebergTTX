"""Manage the singleton AuditSettings row + the in-memory SIEM config cache (#24).

The row (``id == 1``) is the admin-editable routing config; on first read it is
seeded from the ``SIEM_*`` env defaults. Every read/update pushes a
``SiemConfig`` snapshot into ``siem_service`` so the sync ``audit_service.emit``
path forwards without touching the DB.
"""

from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.audit_settings import AuditSettings
from app.services import siem_service

_SINGLETON_ID = 1

# Fields an admin may change (the HTTP token is intentionally not one of them —
# it is env-only). Used to validate/whitelist PUT payloads.
EDITABLE_FIELDS = (
    "enabled",
    "methods",
    "min_severity",
    "file_path",
    "syslog_host",
    "syslog_port",
    "syslog_protocol",
    "syslog_facility",
    "http_endpoint",
    "http_verify_tls",
)


def _to_config(row: AuditSettings) -> siem_service.SiemConfig:
    return siem_service.SiemConfig(
        enabled=row.enabled,
        methods=frozenset(m.lower() for m in (row.methods or [])),
        min_severity=row.min_severity,
        file_path=row.file_path,
        syslog_host=row.syslog_host,
        syslog_port=row.syslog_port,
        syslog_protocol=row.syslog_protocol,
        syslog_facility=row.syslog_facility,
        http_endpoint=row.http_endpoint,
        http_verify_tls=row.http_verify_tls,
    )


async def get_settings(session: AsyncSession) -> AuditSettings:
    """Return the singleton row, seeding it from env defaults on first read."""
    row = await session.get(AuditSettings, _SINGLETON_ID)
    if row is None:
        row = AuditSettings(
            id=_SINGLETON_ID,
            enabled=settings.siem_enabled,
            methods=settings.siem_default_methods,
            min_severity=settings.siem_min_severity,
            file_path=settings.siem_file_path,
            syslog_host=settings.siem_syslog_host,
            syslog_port=settings.siem_syslog_port,
            syslog_protocol=settings.siem_syslog_protocol,
            syslog_facility=settings.siem_syslog_facility,
            http_endpoint=settings.siem_http_endpoint,
            http_verify_tls=settings.siem_http_verify_tls,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def update_settings(session: AsyncSession, changes: dict[str, Any]) -> AuditSettings:
    """Apply a whitelisted patch to the singleton row and refresh the cache."""
    row = await get_settings(session)
    for key in EDITABLE_FIELDS:
        if key in changes and changes[key] is not None:
            setattr(row, key, changes[key])
    session.add(row)
    await session.commit()
    await session.refresh(row)
    siem_service.set_config(_to_config(row))
    return row


async def refresh_cache(session: AsyncSession) -> None:
    """Load the singleton row into the siem_service in-memory cache (startup)."""
    row = await get_settings(session)
    siem_service.set_config(_to_config(row))
