"""Admin runtime email configuration API (#186)."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.dependencies import require_admin
from app.models.email_settings import EmailSettings
from app.models.user import User
from app.services import audit_service, email_settings_service, mail_service, sink_pinning

logger = logging.getLogger("iceberg_ttx")
router = APIRouter(prefix="/email", tags=["email"])
AdminDep = Annotated[User, Depends(require_admin)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


class EmailSettingsUpdate(BaseModel):
    """Whitelisted non-secret settings; unknown/secret fields are rejected."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    smtp_host: str | None = None
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_from: str | None = None
    smtp_username: str | None = None
    smtp_starttls: bool | None = None
    smtp_tls: bool | None = None
    public_base_url: str | None = None

    @field_validator("smtp_host", "smtp_from", "smtp_username", "public_base_url")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def _tls_modes_are_exclusive(self):
        if self.smtp_starttls is True and self.smtp_tls is True:
            raise ValueError("STARTTLS and implicit TLS cannot both be enabled")
        return self


@router.get("/settings")
async def get_email_settings(_: AdminDep, session: SessionDep) -> EmailSettings:
    return await email_settings_service.get_settings(session)


@router.put("/settings")
async def update_email_settings(
    body: EmailSettingsUpdate, current_user: AdminDep, session: SessionDep
) -> EmailSettings:
    changes = body.model_dump(exclude_unset=True)
    current = await email_settings_service.get_settings(session)
    # Copy the value out: update_settings mutates this very row instance, so a
    # post-update read of `current` would compare the new host with itself.
    previous_host = current.smtp_host
    starttls = changes.get("smtp_starttls", current.smtp_starttls)
    tls = changes.get("smtp_tls", current.smtp_tls)
    if starttls and tls:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="STARTTLS and implicit TLS cannot both be enabled",
        )
    enabled = changes.get("enabled", current.enabled)
    base_url = changes.get("public_base_url", current.public_base_url)
    if enabled and not (base_url or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "Set a public base URL before enabling email — reset and invite "
                "links are never derived from the request host (#258)."
            ),
        )
    try:
        row = await email_settings_service.update_settings(session, changes)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    if sink_pinning.host_changed(previous_host, row.smtp_host):
        # SMTP AUTH offers the env-only password to whatever host is configured, so
        # a host change is security-relevant on its own (#259).
        audit_service.emit(
            "email.smtp_host_changed",
            actor=current_user,
            target_type="email_settings",
            target_id=row.id,
            reason=(
                f"from={sink_pinning.origin(previous_host) or 'unset'} "
                f"to={sink_pinning.origin(row.smtp_host) or 'unset'}"
            ),
            severity="critical",
        )
    audit_service.emit(
        "email.settings_updated",
        actor=current_user,
        target_type="email_settings",
        target_id=row.id,
        reason="fields=" + ",".join(sorted(changes)),
        severity="warning",
    )
    return row


@router.post("/test")
async def test_email(current_user: AdminDep, session: SessionDep) -> dict[str, str]:
    """Mail only the authenticated admin and disclose no exception message."""
    await email_settings_service.refresh_cache(session)
    try:
        await mail_service.send_test(current_user.email)
        result = "ok"
    except Exception as exc:  # noqa: BLE001 - convert to a safe class-only result
        logger.warning("email delivery test failed: %s", type(exc).__name__)
        result = f"error: {type(exc).__name__}"
    audit_service.emit(
        "email.test",
        actor=current_user,
        target_type="email_settings",
        reason=result,
        severity="warning" if result != "ok" else "info",
    )
    return {"result": result}
