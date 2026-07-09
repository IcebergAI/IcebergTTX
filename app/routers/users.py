from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.dependencies import require_admin, require_role
from app.models.user import User, UserRole
from app.schemas.api import UserPublic
from app.schemas.auth import AdminCreateUserRequest
from app.services import audit_service
from app.services.auth_service import hash_password

router = APIRouter(prefix="/users", tags=["users"])

FacilitatorDep = Annotated[User, Depends(require_role(UserRole.facilitator))]
AdminDep = Annotated[User, Depends(require_admin)]
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


@router.post("", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
async def create_user(body: AdminCreateUserRequest, admin: AdminDep, session: SessionDep):
    """Admin-provisioned account (#67) — the invite path when self-registration
    is disabled. Not gated by REGISTRATION_ENABLED or the register rate limit."""
    if (await session.exec(select(User).where(User.email == body.email))).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    user = User(
        email=body.email,
        display_name=body.display_name,
        hashed_password=hash_password(body.password),
        role=body.role,
        team=body.team,
        is_admin=body.is_admin,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    audit_service.emit(
        "admin.user_create",
        actor=admin,
        target_type="user",
        target_id=user.id,
        reason=f"role={user.role.value} is_admin={user.is_admin}",
    )
    return _user_out(user)
