from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.dependencies import require_admin, require_role
from app.models.user import LOCAL_AUTH_PROVIDER, User, UserRole
from app.schemas.api import UserPublic
from app.schemas.auth import AdminCreateUserRequest, AdminResetPasswordRequest
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
        "must_change_password": u.must_change_password,
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


@router.post("/{user_id}/reset-password", response_model=UserPublic)
async def reset_password(
    user_id: int,
    body: AdminResetPasswordRequest,
    admin: AdminDep,
    session: SessionDep,
):
    """Admin-driven password reset (#66). Sets a temporary password on another
    user, revokes their existing sessions, and flags must_change_password so they
    are prompted to set their own on next login. SSO accounts have no local
    password and are rejected."""
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if target.auth_provider != LOCAL_AUTH_PROVIDER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot reset the password of an SSO account.",
        )
    target.hashed_password = hash_password(body.password)
    # Revoke all previously-issued tokens (#14). Truncated to whole seconds so a
    # token minted at this instant (iat is second-precision) isn't self-rejected.
    target.token_valid_after = datetime.now(UTC).replace(microsecond=0)
    target.must_change_password = body.must_change_password
    session.add(target)
    await session.commit()
    await session.refresh(target)
    audit_service.emit(
        "admin.password_reset",
        actor=admin,
        target_type="user",
        target_id=target.id,
        reason=f"must_change={body.must_change_password}",
        severity="warning",
    )
    return _user_out(target)
