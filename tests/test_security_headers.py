"""App-level security headers, incl. the strict Content-Security-Policy (#77)."""

import pytest
from httpx import AsyncClient

from app.config import settings
from app.middleware import CONTENT_SECURITY_POLICY

EXPECTED_HEADERS = {
    "content-security-policy": CONTENT_SECURITY_POLICY,
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "cross-origin-opener-policy": "same-origin",
}


@pytest.mark.parametrize("path", ["/api/health", "/login"])
async def test_security_headers_on_all_responses(client: AsyncClient, path: str):
    """Both API and server-rendered page responses carry the security header set."""
    resp = await client.get(path)
    assert resp.status_code == 200
    for name, value in EXPECTED_HEADERS.items():
        assert resp.headers.get(name) == value


async def test_csp_script_src_is_strict(client: AsyncClient):
    """script-src is 'self' only — no 'unsafe-inline' / 'unsafe-eval' (the whole point)."""
    resp = await client.get("/api/health")
    csp = resp.headers["content-security-policy"]
    directives = {
        part.strip().split(" ", 1)[0]: part.strip()
        for part in csp.split(";")
        if part.strip()
    }
    assert directives["script-src"] == "script-src 'self'"
    assert "unsafe-inline" not in directives["script-src"]
    assert "unsafe-eval" not in directives["script-src"]
    # style-src intentionally keeps 'unsafe-inline' for dynamic style= attributes.
    assert "unsafe-inline" in directives["style-src"]


async def test_hsts_absent_in_dev_mode(client: AsyncClient):
    """Conftest runs with DEV_MODE=true, so HSTS is not asserted over local HTTP."""
    resp = await client.get("/api/health")
    assert "strict-transport-security" not in resp.headers


async def test_hsts_present_outside_dev_mode(client: AsyncClient, monkeypatch):
    """With dev_mode off (behind a TLS proxy in prod), HSTS is emitted."""
    monkeypatch.setattr(settings, "dev_mode", False)
    resp = await client.get("/api/health")
    assert resp.headers.get("strict-transport-security") == (
        "max-age=31536000; includeSubDomains"
    )


async def test_csp_present_on_csrf_blocked_response(client: AsyncClient, facilitator):
    """Security headers wrap CSRF: even a 403 CSRF block carries the CSP."""
    await client.post(
        "/api/auth/login",
        json={"email": facilitator.email, "password": "password1234"},
    )
    # Cookie-authenticated mutation with no Origin → CSRF 403.
    resp = await client.post("/api/exercises", json={"scenario_id": 1, "title": "x"})
    assert resp.status_code == 403
    assert resp.headers.get("content-security-policy") == CONTENT_SECURITY_POLICY


async def test_attachment_download_nosniff_not_duplicated(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise,
):
    """The per-download nosniff (#16) survives — set once, not duplicated by the middleware."""
    r = await client.post(
        f"/api/exercises/{active_exercise.id}/injects",
        data={"title": "Doc", "content": "x", "sequence_order": "7"},
        files={"attachment": ("note.txt", b"hello", "text/plain")},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 201
    data = r.json()
    await client.post(
        f"/api/exercises/{active_exercise.id}/injects/{data['id']}/release",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    download = await client.get(
        data["attachment"]["url"],
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert download.status_code == 200
    assert download.headers.get_list("x-content-type-options") == ["nosniff"]
