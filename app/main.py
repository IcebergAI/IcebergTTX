import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.database import create_db_and_tables
from app.models import (  # noqa: F401
    assessment,
    communication,
    exercise,
    inject,
    response,
    scenario,
    suggested_inject,
    user,
)
from app.routers import (
    auth,
    communications,
    exercises,
    health,
    injects,
    responses,
    scenarios,
    settings,
    suggested_injects,
    ui,
    users,
    ws,
)
from app.services.ws_manager import heartbeat_task


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    task = asyncio.create_task(heartbeat_task())
    yield
    task.cancel()


app = FastAPI(title="Deep Thought", lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
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
app.include_router(users.router, prefix="/api")
app.include_router(scenarios.router, prefix="/api")
app.include_router(settings.router, prefix="/api")
app.include_router(exercises.router, prefix="/api")
app.include_router(injects.router, prefix="/api")
app.include_router(responses.router, prefix="/api")
app.include_router(suggested_injects.router, prefix="/api")
app.include_router(communications.router, prefix="/api")
app.include_router(ws.router)
app.include_router(health.router)
