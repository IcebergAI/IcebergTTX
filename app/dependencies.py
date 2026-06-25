from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, status
from jose import JWTError
from sqlmodel import Session, select

from app.database import get_session
from app.models.user import User, UserRole
from app.services import audit_service
from app.services.auth_service import decode_access_token


def _extract_token(
    authorization: Annotated[str | None, Header()] = None,
    access_token: Annotated[str | None, Cookie()] = None,
) -> str:
    """Prefer Authorization header; fall back to cookie."""
    if authorization and authorization.startswith("Bearer "):
        return authorization.removeprefix("Bearer ")
    if access_token:
        return access_token
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


def _effective_user(user: User, view_role: str | None, view_team: str | None) -> User:
    object.__setattr__(user, "actual_role", user.role)
    object.__setattr__(user, "actual_team", user.team)
    object.__setattr__(user, "can_switch_roles", user.role == UserRole.facilitator)
    if user.role != UserRole.facilitator or view_role is None:
        return user
    try:
        effective_role = UserRole(view_role)
    except ValueError:
        return user
    effective = user.model_copy(
        update={
            "role": effective_role,
            "team": view_team.strip() if view_team and view_team.strip() else user.team,
        }
    )
    object.__setattr__(effective, "actual_role", user.role)
    object.__setattr__(effective, "actual_team", user.team)
    object.__setattr__(effective, "can_switch_roles", True)
    return effective


def get_current_user(
    token: Annotated[str, Depends(_extract_token)],
    session: Annotated[Session, Depends(get_session)],
    view_role: Annotated[str | None, Cookie(alias="dt_view_role")] = None,
    view_team: Annotated[str | None, Cookie(alias="dt_view_team")] = None,
) -> User:
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
    )
    try:
        payload = decode_access_token(token)
        email: str | None = payload.get("sub")
        if email is None:
            raise credentials_exc
    except JWTError:
        audit_service.emit(
            "auth.token_invalid", result="fail", reason="decode error", severity="warning"
        )
        raise credentials_exc

    user = session.exec(select(User).where(User.email == email)).first()
    if user is None or not user.is_active:
        audit_service.emit(
            "auth.token_invalid",
            result="fail",
            actor_email=email,
            reason="unknown or inactive user",
            severity="warning",
        )
        raise credentials_exc
    return _effective_user(user, view_role, view_team)


def get_current_actual_user(
    token: Annotated[str, Depends(_extract_token)],
    session: Annotated[Session, Depends(get_session)],
) -> User:
    user = get_current_user(token, session, view_role=None, view_team=None)
    object.__setattr__(user, "actual_role", user.role)
    object.__setattr__(user, "actual_team", user.team)
    object.__setattr__(user, "can_switch_roles", user.role == UserRole.facilitator)
    return user


def _deny(actor: User, required: tuple[UserRole, ...]) -> HTTPException:
    audit_service.emit(
        "authz.denied",
        result="deny",
        actor=actor,
        reason=f"requires one of {[r.value for r in required]}",
        severity="warning",
    )
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
    )


def require_role(*roles: UserRole):
    def _check(current_user: Annotated[User, Depends(get_current_user)]) -> User:
        if current_user.role not in roles:
            raise _deny(current_user, roles)
        return current_user

    return _check


def require_actual_role(*roles: UserRole):
    def _check(current_user: Annotated[User, Depends(get_current_actual_user)]) -> User:
        if current_user.role not in roles:
            raise _deny(current_user, roles)
        return current_user

    return _check
