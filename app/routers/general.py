"""Admin API for runtime application and rate-limit settings."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.dependencies import require_admin
from app.models.general_settings import GeneralSettings
from app.models.user import User
from app.services import audit_service, general_settings_service

router = APIRouter(prefix="/general", tags=["general settings"])
AdminDep = Annotated[User, Depends(require_admin)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


class GeneralSettingsUpdate(BaseModel):
    """Strict allowlist: this surface intentionally contains no secrets."""

    model_config = ConfigDict(extra="forbid")

    registration_enabled: bool | None = None
    access_token_expire_minutes: int | None = Field(default=None, ge=1, le=43200)
    audit_persist: bool | None = None
    login_max_attempts: int | None = Field(default=None, ge=1, le=1000)
    login_lockout_seconds: int | None = Field(default=None, ge=1, le=86400)
    registration_max_attempts: int | None = Field(default=None, ge=1, le=1000)
    registration_lockout_seconds: int | None = Field(default=None, ge=1, le=86400)
    password_reset_max_attempts: int | None = Field(default=None, ge=1, le=1000)
    password_reset_lockout_seconds: int | None = Field(default=None, ge=1, le=86400)


@router.get("/settings")
async def get_general_settings(_: AdminDep, session: SessionDep) -> GeneralSettings:
    return await general_settings_service.get_settings(session)


@router.put("/settings")
async def update_general_settings(
    body: GeneralSettingsUpdate, current_user: AdminDep, session: SessionDep
) -> GeneralSettings:
    changes = body.model_dump(exclude_unset=True)
    row = await general_settings_service.update_settings(session, changes)
    audit_service.emit(
        "audit.settings_updated",
        actor=current_user,
        target_type="general_settings",
        target_id=row.id,
        reason="fields=" + ",".join(sorted(changes)),
        severity="warning",
    )
    return row
