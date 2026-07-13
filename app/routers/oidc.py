"""OIDC / SSO routes (#25).

Authorization-Code + PKCE against a configured IdP. The login route redirects to
the provider; the callback validates the response (state/nonce/ID-token signature,
issuer, audience, expiry — all by Authlib), provisions/looks up the local user, and
then issues the *existing* local session token so every downstream authz check is
unchanged.
"""

from typing import Annotated

from authlib.integrations.starlette_client import OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.routers.auth import _set_session_cookie
from app.services import audit_service, oidc_settings_service
from app.services.auth_service import create_access_token
from app.services.oidc import service as oidc_service
from app.services.oidc.base import get_adapter

router = APIRouter(prefix="/auth/oidc", tags=["auth"])


def _callback_url(request: Request, provider: str) -> str:
    """Absolute redirect_uri the IdP will call back. Honours a configured base
    (for proxies that rewrite host/scheme) else derives it from the request."""
    redirect_base = oidc_settings_service.get_config().oidc_redirect_base_url
    if redirect_base:
        base = redirect_base.rstrip("/")
        return f"{base}/api/auth/oidc/{provider}/callback"
    return str(request.url_for("oidc_callback", provider=provider))


@router.get("/{provider}/login", name="oidc_login")
async def oidc_login(provider: str, request: Request):
    oidc_service.ensure_registered()
    cfg = oidc_service.get_provider(provider)
    client = oidc_service.oauth.create_client(provider)
    if cfg is None or client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown SSO provider")
    redirect_uri = _callback_url(request, provider)
    # Authlib generates + stores state (and, for openid scope, nonce) + the PKCE
    # verifier in request.session; they're validated on callback.
    return await client.authorize_redirect(request, redirect_uri)


@router.get("/{provider}/callback", name="oidc_callback")
async def oidc_callback(
    provider: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    oidc_service.ensure_registered()
    cfg = oidc_service.get_provider(provider)
    client = oidc_service.oauth.create_client(provider)
    adapter = get_adapter(provider)
    if cfg is None or client is None or adapter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown SSO provider")

    # Validate state/nonce, exchange the code, validate the ID token (signature via
    # JWKS, iss/aud/exp/iat/nonce). Any failure here is an auth failure — never log
    # the code or token.
    try:
        token = await client.authorize_access_token(request)
    except OAuthError as exc:
        audit_service.emit(
            "auth.oidc_login",
            result="fail",
            reason=f"provider={provider} id-token validation ({exc.error})",
            severity="warning",
        )
        return RedirectResponse("/login?error=sso", status_code=status.HTTP_303_SEE_OTHER)

    claims = token.get("userinfo")
    if not claims:
        try:
            claims = await client.userinfo(token=token)
        except Exception:
            claims = None
    if not claims:
        audit_service.emit(
            "auth.oidc_login",
            result="fail",
            reason=f"provider={provider} no id-token claims",
            severity="warning",
        )
        return RedirectResponse("/login?error=sso", status_code=status.HTTP_303_SEE_OTHER)

    try:
        identity = adapter.extract_identity(dict(claims), cfg.role_claim)
    except ValueError as exc:
        audit_service.emit(
            "auth.oidc_login",
            result="fail",
            reason=f"provider={provider} {exc}",
            severity="warning",
        )
        return RedirectResponse("/login?error=sso", status_code=status.HTTP_303_SEE_OTHER)

    try:
        user, _created = await oidc_service.provision_oidc_user(
            session, cfg=cfg, identity=identity
        )
    except oidc_service.OIDCProvisionError as exc:
        audit_service.emit(
            "auth.oidc_login",
            result="deny",
            actor_email=identity.email,
            reason=f"provider={provider} {exc.reason}",
            severity="warning",
        )
        return RedirectResponse("/login?error=sso", status_code=status.HTTP_303_SEE_OTHER)

    session_token = create_access_token(
        subject=user.email, role=user.role.value, is_admin=user.is_admin
    )
    response = RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(response, session_token)
    audit_service.emit(
        "auth.oidc_login",
        actor_id=user.id,
        actor_email=user.email,
        actor_role=user.role.value,
        reason=f"provider={provider}",
    )
    return response
