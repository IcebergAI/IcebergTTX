"""Read-only, admin-only effective configuration endpoint."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.dependencies import require_admin
from app.models.user import User
from app.services import effective_config_service

router = APIRouter(prefix="/config", tags=["effective config"])
AdminDep = Annotated[User, Depends(require_admin)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/effective")
async def get_effective_config(_: AdminDep, session: SessionDep) -> dict:
    return await effective_config_service.snapshot(session)
