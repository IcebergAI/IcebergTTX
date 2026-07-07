import pytest
from httpx import AsyncClient

from app.routers import health


async def test_liveness_is_unconditional(client: AsyncClient):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readiness_ok_when_db_reachable(client: AsyncClient):
    resp = await client.get("/api/health/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}


async def test_readiness_503_when_db_unreachable(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    class DeadEngine:
        def connect(self):
            raise ConnectionError("connection refused")

    # Simulate Postgres being down: connecting raises instead of returning.
    monkeypatch.setattr(health, "engine", DeadEngine())

    resp = await client.get("/api/health/ready")
    assert resp.status_code == 503
    assert resp.json() == {"status": "unavailable", "database": "unreachable"}
