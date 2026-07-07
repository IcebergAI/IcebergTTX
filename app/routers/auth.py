from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.database import get_session
from app.dependencies import get_current_actual_user, get_current_user
from app.middleware import client_ip
from app.models.user import User, UserRole
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UpdateMeRequest,
    UserResponse,
)
from app.services import audit_service
from app.services.auth_service import create_access_token, hash_password, verify_password
from app.services.rate_limit import login_rate_limiter

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=settings.cookies_secure,
        samesite="lax",
    )


def _require_local_auth() -> None:
    """Guard the local email/password endpoints when AUTH_MODE=oidc (#25)."""
    if not settings.local_auth_enabled:
        audit_service.emit(
            "auth.login",
            result="deny",
            reason="local auth disabled",
            severity="warning",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Local authentication is disabled; sign in via SSO.",
        )


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, session: Annotated[AsyncSession, Depends(get_session)]):
    _require_local_auth()
    if (await session.exec(select(User).where(User.email == body.email))).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    # Role is never taken from the request — self-registration is always a
    # participant (#8). Promotion is a privileged operation done out-of-band.
    user = User(
        email=body.email,
        display_name=body.display_name,
        hashed_password=hash_password(body.password),
        role=UserRole.participant,
        team=body.team,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    audit_service.emit(
        "auth.register",
        actor_id=user.id,
        actor_email=user.email,
        actor_role=user.role.value,
        target_type="user",
        target_id=user.id,
    )
    return user


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    _require_local_auth()
    ip = client_ip(request) or "unknown"
    rate_key = f"{ip}:{body.email}"
    if login_rate_limiter.is_limited(rate_key):
        retry_after = login_rate_limiter.retry_after(rate_key)
        audit_service.emit(
            "auth.login",
            result="deny",
            actor_email=body.email,
            reason="rate limited",
            severity="warning",
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    user = (await session.exec(select(User).where(User.email == body.email))).first()
    # An OIDC-provisioned account has no local password (hashed_password is NULL) and
    # must not authenticate here — it signs in via SSO (#25).
    if not user or user.hashed_password is None or not verify_password(
        body.password, user.hashed_password
    ):
        login_rate_limiter.record_failure(rate_key)
        audit_service.emit(
            "auth.login",
            result="fail",
            actor_email=body.email,
            reason="invalid credentials",
            severity="warning",
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not user.is_active:
        audit_service.emit(
            "auth.login",
            result="deny",
            actor_id=user.id,
            actor_email=user.email,
            actor_role=user.role.value,
            reason="account disabled",
            severity="warning",
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account disabled")

    login_rate_limiter.reset(rate_key)
    token = create_access_token(subject=user.email, role=user.role.value, is_admin=user.is_admin)
    _set_session_cookie(response, token)
    audit_service.emit(
        "auth.login",
        actor_id=user.id,
        actor_email=user.email,
        actor_role=user.role.value,
    )
    return TokenResponse(access_token=token)


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(key="access_token", path="/", samesite="lax")
    audit_service.emit("auth.logout")
    return {"ok": True}


@router.get("/me", response_model=UserResponse)
def get_me(current_user: Annotated[User, Depends(get_current_user)]):
    return current_user


@router.put("/me", response_model=UserResponse)
async def update_me(
    body: UpdateMeRequest,
    response: Response,
    current_user: Annotated[User, Depends(get_current_actual_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    update_data = body.model_dump(exclude_unset=True)
    new_password = update_data.pop("password", None)
    if new_password is not None:
        current_user.hashed_password = hash_password(new_password)
        # Revoke all previously-issued tokens (#14). Truncated to whole seconds so
        # a freshly-minted token (iat is second-precision) is not itself rejected.
        current_user.token_valid_after = datetime.now(UTC).replace(microsecond=0)
    current_user.sqlmodel_update(update_data)
    session.add(current_user)
    await session.commit()
    await session.refresh(current_user)
    if new_password is not None:
        # Re-issue so the caller's own session survives its own password change;
        # every earlier token is now revoked by the token_valid_after bump.
        token = create_access_token(
            subject=current_user.email, role=current_user.role.value, is_admin=current_user.is_admin
        )
        _set_session_cookie(response, token)
        audit_service.emit(
            "auth.password_change",
            actor=current_user,
            target_type="user",
            target_id=current_user.id,
            severity="warning",
        )
    return current_user
