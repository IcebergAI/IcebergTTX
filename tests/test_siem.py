"""SIEM audit-forwarding pipeline tests (#24)."""

import asyncio
import json

import pytest
from httpx import AsyncClient

from app.config import settings
from app.services import background, siem_service
from app.services.siem_service import SiemConfig


@pytest.fixture(autouse=True)
def _reset_siem():
    """Isolate the module-global OUTBOX + config cache between tests."""
    siem_service.OUTBOX.clear()
    prev = siem_service.get_config()
    yield
    siem_service.OUTBOX.clear()
    siem_service.set_config(prev)


def _event(severity: str = "info", action: str = "auth.login") -> dict:
    return {"action": action, "severity": severity, "actor_email": "a@example.com"}


async def _drain() -> None:
    pending = [t for t in list(background._tasks) if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _get_status(client: AsyncClient, path: str, token: str) -> int:
    resp = await client.get(path, headers=_headers(token))
    return resp.status_code


# --------------------------------------------------------------------------- #
# siem_service.emit — gating + isolation
# --------------------------------------------------------------------------- #


async def test_disabled_is_noop():
    await siem_service.emit(_event(), SiemConfig(enabled=False, methods=frozenset({"stdout"})))
    assert siem_service.OUTBOX == []


async def test_min_severity_gate():
    cfg = SiemConfig(enabled=True, methods=frozenset({"stdout"}), min_severity="warning")
    await siem_service.emit(_event("info"), cfg)
    assert siem_service.OUTBOX == []
    await siem_service.emit(_event("critical"), cfg)
    assert len(siem_service.OUTBOX) == 1


async def test_only_selected_methods_invoked(monkeypatch):
    called = []

    async def _rec_http(event, cfg):
        called.append("http")

    async def _rec_syslog(event, cfg):
        called.append("syslog")

    monkeypatch.setattr(siem_service, "_emit_http", _rec_http)
    monkeypatch.setattr(siem_service, "_emit_syslog", _rec_syslog)
    cfg = SiemConfig(enabled=True, methods=frozenset({"http"}), http_endpoint="https://x")
    await siem_service.emit(_event(), cfg)
    assert called == ["http"]


async def test_failing_sink_never_propagates(monkeypatch):
    async def _boom(event, cfg):
        raise RuntimeError("SIEM down")

    monkeypatch.setattr(siem_service, "_emit_http", _boom)
    cfg = SiemConfig(enabled=True, methods=frozenset({"http"}), http_endpoint="https://x")
    # Must not raise — auditing can never break the request that triggered it.
    await siem_service.emit(_event(), cfg)
    assert len(siem_service.OUTBOX) == 1


# --------------------------------------------------------------------------- #
# HTTP sink — env-only bearer token + TLS verification, no secret leakage
# --------------------------------------------------------------------------- #


async def test_http_sink_uses_env_token_and_verify(monkeypatch):
    captured = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content=None, headers=None):
            captured["url"] = url
            captured["content"] = content
            captured["headers"] = headers
            return _FakeResp()

    class _FakeResp:
        def raise_for_status(self):
            return None

    monkeypatch.setattr(siem_service.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(settings, "siem_http_token", "super-secret-token")

    cfg = SiemConfig(
        enabled=True,
        methods=frozenset({"http"}),
        http_endpoint="https://siem.example.com/collector",
        http_verify_tls=False,
    )
    event = _event()
    await siem_service.emit(event, cfg)

    assert captured["init"]["verify"] is False
    assert captured["headers"]["Authorization"] == "Bearer super-secret-token"
    # The secret token must never end up in the forwarded body or the test OUTBOX.
    assert "super-secret-token" not in captured["content"]
    assert "super-secret-token" not in json.dumps(siem_service.OUTBOX)


async def test_syslog_sink_rfc5424_framing(monkeypatch):
    sent = {}

    class _FakeSock:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, t):
            pass

        def connect(self, addr):
            sent["addr"] = addr

        def send(self, data):
            sent["data"] = data

        def sendall(self, data):
            sent["data"] = data

    monkeypatch.setattr(siem_service.socket, "socket", _FakeSock)
    cfg = SiemConfig(
        enabled=True,
        methods=frozenset({"syslog"}),
        syslog_host="collector",
        syslog_port=5514,
        syslog_facility=13,
    )
    await siem_service.emit(_event("info"), cfg)

    assert sent["addr"] == ("collector", 5514)
    # facility 13 * 8 + severity 6 (info) = 110
    assert sent["data"].startswith(b"<110>1 ")
    assert b"iceberg-ttx - audit -" in sent["data"]


# --------------------------------------------------------------------------- #
# audit_service.emit -> _ship wiring
# --------------------------------------------------------------------------- #


async def test_emit_ships_when_enabled():
    from app.services import audit_service

    siem_service.set_config(SiemConfig(enabled=True, methods=frozenset({"stdout"})))
    audit_service.emit("auth.login", actor_email="x@example.com")
    await _drain()
    assert any(e.get("action") == "auth.login" for e in siem_service.OUTBOX)


async def test_emit_does_not_ship_when_disabled():
    from app.services import audit_service

    siem_service.set_config(SiemConfig(enabled=False))
    audit_service.emit("auth.login", actor_email="x@example.com")
    await _drain()
    assert siem_service.OUTBOX == []


# --------------------------------------------------------------------------- #
# API — admin-gated settings / events / test
# --------------------------------------------------------------------------- #


async def test_settings_requires_admin(
    client: AsyncClient, participant_token: str, facilitator_token: str, admin_token: str
):
    assert await _get_status(client, "/api/audit/settings", participant_token) == 403
    # A non-admin facilitator is denied too — the gate is stricter than "facilitator".
    assert await _get_status(client, "/api/audit/settings", facilitator_token) == 403
    assert await _get_status(client, "/api/audit/settings", admin_token) == 200


async def test_settings_roundtrip_refreshes_cache(client: AsyncClient, admin_token: str):
    resp = await client.put(
        "/api/audit/settings",
        headers=_headers(admin_token),
        json={
            "enabled": True,
            "methods": ["stdout", "syslog"],
            "min_severity": "warning",
            "syslog_host": "collector",
            "syslog_port": 6514,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert set(body["methods"]) == {"stdout", "syslog"}
    assert body["min_severity"] == "warning"
    assert body["syslog_port"] == 6514

    # The in-memory cache the sync emit path reads is refreshed on save.
    cfg = siem_service.get_config()
    assert cfg.enabled is True
    assert cfg.min_severity == "warning"
    assert "syslog" in cfg.methods

    # And it is persisted / seeded back on the next read.
    again = await client.get("/api/audit/settings", headers=_headers(admin_token))
    assert again.json()["syslog_host"] == "collector"


async def test_invalid_method_is_dropped(client: AsyncClient, admin_token: str):
    resp = await client.put(
        "/api/audit/settings",
        headers=_headers(admin_token),
        json={"methods": ["stdout", "bogus"]},
    )
    assert resp.status_code == 200
    assert resp.json()["methods"] == ["stdout"]


async def test_test_event_emits_through_sinks(client: AsyncClient, admin_token: str):
    await client.put(
        "/api/audit/settings",
        headers=_headers(admin_token),
        json={"enabled": True, "methods": ["stdout"], "min_severity": "info"},
    )
    resp = await client.post("/api/audit/test", headers=_headers(admin_token))
    assert resp.status_code == 200
    await _drain()
    assert any(e.get("action") == "audit.test" for e in siem_service.OUTBOX)


async def test_events_requires_admin_and_returns(
    client: AsyncClient, admin_token: str, participant_token: str
):
    assert await _get_status(client, "/api/audit/events", participant_token) == 403
    # The settings PUT above/here emits an audit.settings_updated event; ensure the
    # trail is queryable and filterable by admins.
    await client.put(
        "/api/audit/settings", headers=_headers(admin_token), json={"enabled": False}
    )
    await _drain()
    resp = await client.get(
        "/api/audit/events?action=audit.settings_updated", headers=_headers(admin_token)
    )
    assert resp.status_code == 200
    events = resp.json()
    assert all(e["action"] == "audit.settings_updated" for e in events)


async def test_admin_audit_page_gated(
    client: AsyncClient, admin_token: str, participant_token: str
):
    # Admin sees the page; a non-admin is redirected away from the shell.
    ok = await client.get(
        "/admin/audit", headers={"Cookie": f"access_token={admin_token}"}, follow_redirects=False
    )
    assert ok.status_code == 200
    redirected = await client.get(
        "/admin/audit",
        headers={"Cookie": f"access_token={participant_token}"},
        follow_redirects=False,
    )
    assert redirected.status_code in (302, 307)
