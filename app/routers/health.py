import asyncio
import logging

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.database import engine

logger = logging.getLogger("iceberg_ttx")

router = APIRouter()

# Bound the readiness DB probe so a hung/unreachable Postgres fails the check
# quickly rather than stacking up against the probe's own timeout.
READINESS_DB_TIMEOUT_SECONDS = 3.0


@router.get("/api/health")
def health() -> dict:
    """Liveness probe: DB-free and unconditional.

    Backs the k8s *liveness* probe — a DB outage must not restart app pods (that
    would just crash-loop through startup migration attempts). Readiness is
    handled separately by ``/api/health/ready`` (#71).
    """
    return {"status": "ok"}


@router.get("/api/health/ready")
async def readiness(response: Response) -> dict:
    """Readiness probe: verifies DB connectivity with a short-timeout ``SELECT 1``.

    Returns 503 when the database is down/unreachable so k8s pulls the pod from
    the endpoint set instead of routing traffic that would 500 (#71). No table
    access — a bare ``SELECT 1`` so it does not depend on migrations having run.
    """
    try:
        async with asyncio.timeout(READINESS_DB_TIMEOUT_SECONDS):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("Readiness check failed: database unreachable (%s)", exc)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": "unreachable"}
    return {"status": "ok", "database": "ok"}
