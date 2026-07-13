from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.database import get_session
from app.dependencies import get_current_actual_user, get_current_user
from app.middleware import client_ip
from app.models.auth_token import AuthTokenPurpose
from app.models.exercise import Exercise
from app.models.user import LOCAL_AUTH_PROVIDER, User, UserRole
from app.schemas.auth import (
    InviteAccept,
    LoginRequest,
    PasswordResetComplete,
    PasswordResetRequest,
    RegisterRequest,
    TokenResponse,
    UpdateMeRequest,
    UserResponse,
)
from app.services import (
    audit_service,
    general_settings_service,
    mail_service,
    oidc_settings_service,
    token_service,
)
from app.services.auth_service import create_access_token, hash_password, verify_password
from app.services.background import spawn
from app.services.exercise_service import enrol_member
from app.services.rate_limit import (
    login_rate_limiter,
    password_reset_rate_limiter,
    registration_rate_limiter,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# Reset links are short-lived — long enough to receive the email and act, short
# enough to bound exposure of a leaked link (#117).
RESET_TOKEN_TTL = timedelta(hours=1)


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
    if not oidc_settings_service.get_config().local_auth_enabled:
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


def _require_registration_enabled() -> None:
    """Guard self-service registration when it is turned off (#67)."""
    if not general_settings_service.get_config().registration_enabled:
        audit_service.emit(
            "auth.register",
            result="deny",
            reason="registration disabled",
            severity="warning",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Self-registration is disabled.",
        )


def _require_smtp() -> None:
    """Guard the email-dependent endpoints when SMTP is not configured (#117).

    Returns 404 (not 403): with no mailer the feature does not exist, so the routes
    read as absent rather than forbidden. UI entry points are hidden in parallel.
    """
    if not mail_service.smtp_enabled():
        audit_service.emit(
            "auth.email_feature",
            result="deny",
            reason="smtp not configured",
            severity="warning",
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    _require_local_auth()
    _require_registration_enabled()
    # Per-IP flood cap (#67): every attempt counts (not just failures), so a host
    # cannot mass-create accounts. Keyed by IP alone — the email is the thing being
    # created, so it can't be part of the key.
    ip = client_ip(request) or "unknown"
    if registration_rate_limiter.is_limited(ip):
        retry_after = registration_rate_limiter.retry_after(ip)
        audit_service.emit(
            "auth.register",
            result="deny",
            actor_email=body.email,
            reason="rate limited",
            severity="warning",
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many registration attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    registration_rate_limiter.record_failure(ip)

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


@router.post("/password-reset/request")
async def password_reset_request(
    body: PasswordResetRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Start a self-service password reset (#117).

    Always returns the same 200 regardless of whether the account exists — no
    enumeration. Email sending is fired off the response path (spawn) so there is no
    timing tell. Only local-auth accounts get a reset link; SSO accounts get a
    "sign in via SSO" notice; unknown emails get nothing.
    """
    _require_smtp()
    ip = client_ip(request) or "unknown"
    if password_reset_rate_limiter.is_limited(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many reset requests. Try again later.",
            headers={"Retry-After": str(password_reset_rate_limiter.retry_after(ip))},
        )
    password_reset_rate_limiter.record_failure(ip)

    user = (await session.exec(select(User).where(User.email == body.email))).first()
    if user is not None and user.auth_provider == LOCAL_AUTH_PROVIDER:
        raw = await token_service.create(
            session,
            purpose=AuthTokenPurpose.password_reset,
            email=user.email,
            user_id=user.id,
            ttl=RESET_TOKEN_TTL,
        )
        link = mail_service.build_link(request, "/reset-password", raw)
        spawn(
            mail_service.send(
                user.email,
                "Reset your IcebergTTX password",
                "We received a request to reset your IcebergTTX password.\n\n"
                f"Use this link within the next hour to choose a new one:\n{link}\n\n"
                "If you didn't request this, you can ignore this email — your "
                "password will not change.",
            )
        )
    elif user is not None:
        # SSO account — no local password to reset.
        spawn(
            mail_service.send(
                user.email,
                "IcebergTTX password reset",
                "You (or someone) requested a password reset for your IcebergTTX "
                "account, but it signs in via single sign-on (SSO) and has no "
                "password to reset. Use your organisation's SSO to sign in.",
            )
        )
    # Never log the token; record only the attempt + the email that was requested.
    audit_service.emit(
        "auth.password_reset_request",
        actor_email=body.email,
        target_type="user",
        target_id=user.id if user else None,
        reason="reset email sent" if user else "no matching account",
    )
    return {"status": "ok"}


@router.post("/password-reset/complete", response_model=TokenResponse)
async def password_reset_complete(
    body: PasswordResetComplete,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Finish a reset with the emailed token + a new password (#117).

    On success: set the new password, revoke all existing sessions (token_valid_after,
    #14), clear any must_change_password flag, and log the caller in via a fresh cookie.
    """
    _require_smtp()
    token = await token_service.consume(
        session, raw=body.token, purpose=AuthTokenPurpose.password_reset
    )
    user = await session.get(User, token.user_id) if token and token.user_id else None
    if user is None or user.auth_provider != LOCAL_AUTH_PROVIDER:
        audit_service.emit(
            "auth.password_reset_complete",
            result="fail",
            reason="invalid or expired token",
            severity="warning",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired reset link."
        )

    user.hashed_password = hash_password(body.password)
    # Revoke previously-issued tokens (#14); whole-second truncation so the fresh
    # login token below (iat is second-precision) is not itself rejected.
    user.token_valid_after = datetime.now(UTC).replace(microsecond=0)
    user.must_change_password = False
    session.add(user)
    await session.commit()
    await session.refresh(user)

    new_token = create_access_token(
        subject=user.email, role=user.role.value, is_admin=user.is_admin
    )
    _set_session_cookie(response, new_token)
    audit_service.emit(
        "auth.password_reset_complete",
        actor_id=user.id,
        actor_email=user.email,
        actor_role=user.role.value,
        target_type="user",
        target_id=user.id,
        severity="warning",
    )
    return TokenResponse(access_token=new_token)


@router.post("/invite/accept", response_model=TokenResponse)
async def invite_accept(
    body: InviteAccept,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Redeem a participant invite (#117).

    The emailed token authorises account creation, so this works even while open
    self-registration is disabled. The email/team/exercise are taken from the token
    (never the client); a new participant is created, enrolled in the bound exercise
    if any, and logged in.
    """
    _require_smtp()
    token = await token_service.consume(
        session,
        raw=body.token,
        purpose=AuthTokenPurpose.invite,
        commit=False,
    )
    if token is None:
        audit_service.emit(
            "auth.invite_accept",
            result="fail",
            reason="invalid or expired token",
            severity="warning",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired invite link."
        )
    if (await session.exec(select(User).where(User.email == token.email))).first():
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This email already has an account."
        )

    # Email is authoritative from the token; role is always participant (like register).
    user = User(
        email=token.email,
        display_name=body.display_name,
        hashed_password=hash_password(body.password),
        role=UserRole.participant,
        team=token.team,
    )
    session.add(user)
    await session.flush()
    assert user.id is not None  # nosec B101 - narrow the PK for enrol_member
    # Auto-enrol in the invite's exercise (group defaults from the user's team).
    if token.exercise_id is not None:
        exercise = await session.get(Exercise, token.exercise_id)
        if exercise is not None:
            try:
                await enrol_member(
                    session,
                    exercise=exercise,
                    user_id=user.id,
                    commit=False,
                )
            except HTTPException:
                # Roll back both the token burn and account creation when release
                # won the shared roster lock before this acceptance.
                await session.rollback()
                raise
    await session.commit()
    await session.refresh(user)

    new_token = create_access_token(
        subject=user.email, role=user.role.value, is_admin=user.is_admin
    )
    _set_session_cookie(response, new_token)
    audit_service.emit(
        "auth.invite_accept",
        actor_id=user.id,
        actor_email=user.email,
        actor_role=user.role.value,
        target_type="user",
        target_id=user.id,
    )
    return TokenResponse(access_token=new_token)


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
        # Changing the password satisfies any admin-set temp-password requirement (#66).
        current_user.must_change_password = False
    current_user.sqlmodel_update(update_data)
    session.add(current_user)
    await session.commit()
    await session.refresh(current_user)
    if new_password is not None:
        # Re-issue so the caller's own session survives its own password change;
        # every earlier token is now revoked by the token_valid_after bump. The
        # fresh token rides ONLY in the httpOnly cookie — never the response body,
        # so page-context JS can't read/exfiltrate it (the client drops its stale
        # localStorage bearer and falls back to the cookie).
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
