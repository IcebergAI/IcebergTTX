"""Admin audit-log + SIEM-forwarding API (#24).

All routes are admin-only (``require_admin`` — the real ``User.is_admin`` column).
Reads the persisted ``AuditEvent`` trail and edits the runtime SIEM routing
config; the HTTP bearer token is env-only and never exposed here.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.dependencies import require_admin
from app.models.audit import AuditEvent
from app.models.audit_settings import AuditSettings
from app.models.user import User
from app.services import audit_service, audit_settings_service

router = APIRouter(prefix="/audit", tags=["audit"])

AdminDep = Annotated[User, Depends(require_admin)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]

_EVENT_LIMIT = 200
_ALLOWED_METHODS = {"stdout", "file", "syslog", "http"}
_ALLOWED_SEVERITY = {"info", "warning", "critical"}


class AuditSettingsUpdate(BaseModel):
    """Whitelisted, validated patch to the routing config (no secret token)."""

    enabled: bool | None = None
    methods: list[str] | None = None
    min_severity: str | None = None
    file_path: str | None = None
    syslog_host: str | None = None
    syslog_port: int | None = None
    syslog_protocol: str | None = None
    syslog_facility: int | None = None
    http_endpoint: str | None = None
    http_verify_tls: bool | None = None

    @field_validator("methods")
    @classmethod
    def _valid_methods(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return [m for m in (s.strip().lower() for s in v) if m in _ALLOWED_METHODS]

    @field_validator("min_severity")
    @classmethod
    def _valid_severity(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        return v if v in _ALLOWED_SEVERITY else "info"

    @field_validator("syslog_protocol")
    @classmethod
    def _valid_protocol(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return "TCP" if v.strip().upper() == "TCP" else "UDP"


@router.get("/events")
async def list_events(
    _: AdminDep,
    session: SessionDep,
    action: str | None = None,
    severity: str | None = None,
    result: str | None = None,
    actor: str | None = None,
) -> list[AuditEvent]:
    """Most-recent audit events (capped), with optional filters."""
    stmt = select(AuditEvent).order_by(col(AuditEvent.created_at).desc()).limit(_EVENT_LIMIT)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if severity:
        stmt = stmt.where(AuditEvent.severity == severity)
    if result:
        stmt = stmt.where(AuditEvent.result == result)
    if actor:
        stmt = stmt.where(col(AuditEvent.actor_email).ilike(f"%{actor}%"))
    return list((await session.exec(stmt)).all())


@router.get("/settings")
async def get_audit_settings(_: AdminDep, session: SessionDep) -> AuditSettings:
    return await audit_settings_service.get_settings(session)


@router.put("/settings")
async def update_audit_settings(
    body: AuditSettingsUpdate,
    current_user: AdminDep,
    session: SessionDep,
) -> AuditSettings:
    row = await audit_settings_service.update_settings(
        session, body.model_dump(exclude_unset=True)
    )
    audit_service.emit(
        "audit.settings_updated",
        actor=current_user,
        target_type="audit_settings",
        target_id=row.id,
        severity="warning",
    )
    return row


@router.post("/test")
async def send_test_event(current_user: AdminDep, session: SessionDep) -> dict:
    """Emit a synthetic event through the enabled sinks to verify connectivity."""
    # Ensure the cache reflects the persisted row before emitting the test.
    await audit_settings_service.refresh_cache(session)
    audit_service.emit(
        "audit.test",
        actor=current_user,
        target_type="audit_settings",
        reason="admin-triggered SIEM connectivity test",
        severity="info",
    )
    return {"ok": True}
