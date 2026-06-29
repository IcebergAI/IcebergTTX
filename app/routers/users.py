from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.dependencies import require_role
from app.models.user import User, UserRole
from app.schemas.api import UserPublic

router = APIRouter(prefix="/users", tags=["users"])

FacilitatorDep = Annotated[User, Depends(require_role(UserRole.facilitator))]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _user_out(u: User) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "display_name": u.display_name,
        "role": u.role,
        "team": u.team,
        "is_active": u.is_active,
    }


@router.get("", response_model=list[UserPublic])
async def list_users(_: FacilitatorDep, session: SessionDep):
    return [_user_out(u) for u in (await session.exec(select(User))).all()]
