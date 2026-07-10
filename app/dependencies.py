from datetime import UTC, datetime
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from jwt import PyJWTError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.models.user import User, UserRole
from app.services import audit_service
from app.services.auth_service import decode_access_token
from app.services.role_preview import apply_role_preview


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


async def resolve_user_from_token(
    token: str,
    session: AsyncSession,
    view_role: str | None = None,
    view_team: str | None = None,
) -> User | None:
    """Decode a JWT and return the active, non-revoked user (role preview applied).

    Returns ``None`` on any failure — bad/expired token, missing ``sub``, unknown
    or inactive user, or a token revoked by ``token_valid_after`` (#14). This lets
    non-HTTP callers (the WebSocket handshake) close with an appropriate code;
    ``get_current_user`` wraps it and raises 401 for HTTP requests instead.
    """
    try:
        payload = decode_access_token(token)
    except PyJWTError:
        audit_service.emit(
            "auth.token_invalid", result="fail", reason="decode error", severity="warning"
        )
        return None

    email: str | None = payload.get("sub")
    if email is None:
        # Not a decode failure — the token verified but lacks a subject claim.
        audit_service.emit(
            "auth.token_invalid", result="fail", reason="missing sub claim", severity="warning"
        )
        return None

    user = (await session.exec(select(User).where(User.email == email))).first()
    if user is None or not user.is_active:
        audit_service.emit(
            "auth.token_invalid",
            result="fail",
            actor_email=email,
            reason="unknown or inactive user",
            severity="warning",
        )
        return None

    # Token revocation (#14): reject tokens issued before the user's cutoff. A
    # missing `iat` on a token is treated as revoked when a cutoff is set.
    if user.token_valid_after is not None:
        iat = payload.get("iat")
        # A non-numeric/out-of-range iat falls through to the revoked branch
        # rather than raising from fromtimestamp() and surfacing as a 500.
        issued_at = datetime.fromtimestamp(iat, UTC) if isinstance(iat, int | float) else None
        if issued_at is None or issued_at < user.token_valid_after:
            audit_service.emit(
                "auth.token_invalid",
                result="fail",
                actor_email=email,
                reason="revoked",
                severity="warning",
            )
            return None

    return apply_role_preview(user, view_role, view_team)


# Safe (non-mutating) HTTP methods stay reachable while a password change is
# pending, so the frontend shell can still read /auth/me and load enough to
# funnel the user to /settings (#66).
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _enforce_password_change(user: User, request: Request) -> None:
    """Server-side backstop for the admin-set temporary-password flag (#66).

    While ``must_change_password`` is set, block state-changing requests to
    everything except the auth namespace (``/api/auth/*`` — so the user can view
    and change their own account and log out). Reads are left alone; the UI
    redirect to /settings is the primary funnel, this just stops a user from
    ignoring it and driving the app via the API on a password the admin knows.
    """
    if not getattr(user, "must_change_password", False):
        return
    if request.method in _SAFE_METHODS or request.url.path.startswith("/api/auth/"):
        return
    audit_service.emit(
        "authz.denied",
        result="deny",
        actor=user,
        reason="password change required",
        severity="warning",
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail="Password change required"
    )


async def get_current_user(
    request: Request,
    token: Annotated[str, Depends(_extract_token)],
    session: Annotated[AsyncSession, Depends(get_session)],
    view_role: Annotated[str | None, Cookie(alias="dt_view_role")] = None,
    view_team: Annotated[str | None, Cookie(alias="dt_view_team")] = None,
) -> User:
    user = await resolve_user_from_token(token, session, view_role, view_team)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        )
    _enforce_password_change(user, request)
    return user


async def get_current_actual_user(
    request: Request,
    token: Annotated[str, Depends(_extract_token)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    user = await get_current_user(request, token, session, view_role=None, view_team=None)
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


def require_admin(current_user: Annotated[User, Depends(get_current_user)]) -> User:
    """Gate on the real ``User.is_admin`` column (#24 audit admin, #12 ownership).

    A real column, so it survives role-preview ``model_copy`` and is unspoofable —
    a forged JWT claim buys nothing here.
    """
    if not getattr(current_user, "is_admin", False):
        audit_service.emit(
            "authz.denied",
            result="deny",
            actor=current_user,
            reason="requires admin",
            severity="warning",
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return current_user
