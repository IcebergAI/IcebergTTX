import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings, validate_settings
from app.database import run_migrations
from app.logging_config import configure_logging
from app.middleware import (
    AuditContextMiddleware,
    CSRFOriginMiddleware,
    SecurityHeadersMiddleware,
)
from app.models import (  # noqa: F401
    assessment,
    audit,
    audit_settings,
    communication,
    exercise,
    inject,
    inject_comment,
    proxy_settings,
    report_summary,
    response,
    scenario,
    suggested_inject,
    user,
)
from app.routers import audit as audit_router
from app.routers import (
    auth,
    communications,
    exercises,
    health,
    inject_comments,
    injects,
    oidc,
    responses,
    scenarios,
    suggested_injects,
    ui,
    users,
    ws,
)
from app.routers import proxy as proxy_router
from app.routers import settings as settings_router
from app.routers.ui import UIRedirect
from app.services import audit_service
from app.services.ws_manager import heartbeat_task

logger = logging.getLogger("iceberg_ttx")


async def _load_siem_config() -> None:
    """Load the AuditSettings singleton into the SIEM in-memory cache (#24).

    Best-effort: a failure here must not block startup — the cache defaults to
    disabled (no forwarding) until an admin enables it.
    """
    try:
        from sqlmodel.ext.asyncio.session import AsyncSession

        from app.database import engine
        from app.services import audit_settings_service

        async with AsyncSession(engine) as session:
            await audit_settings_service.refresh_cache(session)
    except Exception:
        logger.exception("failed to load SIEM audit config; forwarding stays disabled")


async def _load_proxy_config() -> None:
    """Load the ProxySettings singleton into the outbound-proxy cache (#97).

    Best-effort: a failure here must not block startup — the cache stays unloaded,
    and every call site then passes no proxy kwargs, i.e. httpx's pre-feature
    default (``trust_env=True``, honouring any ambient HTTPS_PROXY).
    """
    try:
        from sqlmodel.ext.asyncio.session import AsyncSession

        from app.database import engine
        from app.services import proxy_settings_service

        async with AsyncSession(engine) as session:
            await proxy_settings_service.refresh_cache(session)
    except Exception:
        logger.exception("failed to load outbound proxy config; using httpx defaults")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    validate_settings()
    await run_migrations()
    # Must precede OIDC registration and any outbound call: the OIDC providers bake
    # the resolved proxy into their Authlib client at registration time (#97). Move
    # this below register_providers() and OIDC silently loses proxying.
    await _load_proxy_config()
    await _load_siem_config()
    # Register enabled OIDC providers with Authlib (#25). Idempotent; the routes
    # also register lazily so this is a no-op fast path under the test transport.
    from app.services.oidc import service as oidc_service

    oidc_service.register_providers()
    audit_service.emit("app.startup", severity="info")
    task = asyncio.create_task(heartbeat_task())
    yield
    task.cancel()
    audit_service.emit("app.shutdown", severity="info")


app = FastAPI(title="IcebergTTX", lifespan=lifespan)

# add_middleware wraps outermost-last. Order (outer → inner):
#   AuditContext → SecurityHeaders → CSRF → Session.
# Audit context must be outermost so blocked requests are still attributable;
# SecurityHeaders wraps CSRF so even CSRF-blocked 403s carry the security headers.
# SessionMiddleware (#25) is innermost — it backs Authlib's transient OIDC handshake
# state (state/nonce/PKCE verifier). It only sets a cookie during a login handshake;
# same_site=lax so the cookie survives the top-level redirect back from the IdP.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="dt_oidc_session",
    same_site="lax",
    https_only=settings.cookies_secure,
    max_age=600,
)
app.add_middleware(CSRFOriginMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(AuditContextMiddleware)


@app.exception_handler(UIRedirect)
async def ui_redirect_handler(request: Request, exc: UIRedirect) -> RedirectResponse:
    return RedirectResponse(exc.url)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    audit_service.emit(
        "app.error",
        result="fail",
        reason=type(exc).__name__,
        severity="critical",
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "An unexpected error occurred"},
    )

app.mount("/static", StaticFiles(directory="static"), name="static")

# UI routes first so literal paths like /scenarios/new are matched before
# parameterised API routes like /api/scenarios/{id}
app.include_router(ui.router)

# All JSON API routes prefixed with /api to avoid path conflicts with UI routes
app.include_router(auth.router, prefix="/api")
app.include_router(oidc.router, prefix="/api")
app.include_router(audit_router.router, prefix="/api")
app.include_router(proxy_router.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(scenarios.router, prefix="/api")
app.include_router(settings_router.router, prefix="/api")
app.include_router(exercises.router, prefix="/api")
app.include_router(injects.router, prefix="/api")
app.include_router(inject_comments.router, prefix="/api")
app.include_router(responses.router, prefix="/api")
app.include_router(suggested_injects.router, prefix="/api")
app.include_router(communications.router, prefix="/api")
app.include_router(ws.router)
app.include_router(health.router)
