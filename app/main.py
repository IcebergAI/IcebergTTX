import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import validate_settings
from app.database import run_migrations
from app.logging_config import configure_logging
from app.middleware import AuditContextMiddleware, CSRFOriginMiddleware
from app.models import (  # noqa: F401
    assessment,
    audit,
    audit_settings,
    communication,
    exercise,
    inject,
    inject_comment,
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
    responses,
    scenarios,
    settings,
    suggested_injects,
    ui,
    users,
    ws,
)
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    validate_settings()
    await run_migrations()
    await _load_siem_config()
    audit_service.emit("app.startup", severity="info")
    task = asyncio.create_task(heartbeat_task())
    yield
    task.cancel()
    audit_service.emit("app.shutdown", severity="info")


app = FastAPI(title="IcebergTTX", lifespan=lifespan)

# Outermost first: audit context must be set before CSRF (and everything else)
# runs so blocked requests are still attributable.
app.add_middleware(CSRFOriginMiddleware)
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
app.include_router(audit_router.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(scenarios.router, prefix="/api")
app.include_router(settings.router, prefix="/api")
app.include_router(exercises.router, prefix="/api")
app.include_router(injects.router, prefix="/api")
app.include_router(inject_comments.router, prefix="/api")
app.include_router(responses.router, prefix="/api")
app.include_router(suggested_injects.router, prefix="/api")
app.include_router(communications.router, prefix="/api")
app.include_router(ws.router)
app.include_router(health.router)
