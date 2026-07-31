"""Secret-bearing egress destinations stay pinned to the environment (#259).

The SIEM token, SMTP password, and proxy credentials are env-only *because* admins
are not trusted to read them. Each rides on a connection whose host was runtime
editable, so re-pointing the host and pressing "test" handed the secret to any server
an admin named. These tests pin that boundary shut and check the audit trail records
a destination move.
"""

import pytest
from httpx import AsyncClient

from app.config import settings
from app.services import audit_service, sink_pinning


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(name="audit_events")
def audit_events_fixture(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(audit_service, "emit", lambda action, **kw: events.append((action, kw)))
    return events


# ── origin normalisation ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("https://siem.test/ingest", "https://siem.test/other?x=1", True),
        ("https://SIEM.test/ingest", "https://siem.test/ingest", True),
        ("https://siem.test/ingest", "https://siem.test:8443/ingest", False),
        ("https://siem.test/ingest", "http://siem.test/ingest", False),
        ("https://siem.test/ingest", "https://attacker.example/ingest", False),
        ("smtp.test", "SMTP.test", True),
        ("smtp.test", "smtp.attacker.example", False),
    ],
)
def test_origin_compares_scheme_host_port_only(a: str, b: str, same: bool):
    assert (sink_pinning.origin(a) == sink_pinning.origin(b)) is same


# ── SIEM HTTP sink ────────────────────────────────────────────────────────────


async def test_siem_endpoint_pinned_while_token_is_set(
    client: AsyncClient, admin_token: str, monkeypatch
):
    monkeypatch.setattr(settings, "siem_http_token", "s3cret-bearer")
    monkeypatch.setattr(settings, "siem_http_endpoint", "https://siem.internal/ingest")
    resp = await client.put(
        "/api/audit/settings",
        headers=_headers(admin_token),
        json={"http_endpoint": "https://attacker.example/collect"},
    )
    assert resp.status_code == 422
    assert "SIEM_HTTP_TOKEN" in resp.text


async def test_siem_endpoint_path_stays_editable_on_the_pinned_host(
    client: AsyncClient, admin_token: str, monkeypatch
):
    monkeypatch.setattr(settings, "siem_http_token", "s3cret-bearer")
    monkeypatch.setattr(settings, "siem_http_endpoint", "https://siem.internal/ingest")
    resp = await client.put(
        "/api/audit/settings",
        headers=_headers(admin_token),
        json={"http_endpoint": "https://siem.internal/v2/events"},
    )
    assert resp.status_code == 200
    assert resp.json()["http_endpoint"] == "https://siem.internal/v2/events"


async def test_siem_endpoint_can_always_be_cleared(
    client: AsyncClient, admin_token: str, monkeypatch
):
    """With no destination the token goes nowhere — an operator must keep the off switch."""
    monkeypatch.setattr(settings, "siem_http_token", "s3cret-bearer")
    monkeypatch.setattr(settings, "siem_http_endpoint", "https://siem.internal/ingest")
    resp = await client.put(
        "/api/audit/settings", headers=_headers(admin_token), json={"http_endpoint": ""}
    )
    assert resp.status_code == 200
    assert resp.json()["http_endpoint"] == ""


async def test_siem_endpoint_free_to_move_without_a_token(
    client: AsyncClient, admin_token: str, monkeypatch
):
    monkeypatch.setattr(settings, "siem_http_token", "")
    resp = await client.put(
        "/api/audit/settings",
        headers=_headers(admin_token),
        json={"http_endpoint": "https://collector.example/ingest"},
    )
    assert resp.status_code == 200


async def test_siem_endpoint_change_is_audited_critical(
    client: AsyncClient, admin_token: str, monkeypatch, audit_events
):
    monkeypatch.setattr(settings, "siem_http_token", "")
    await client.put(
        "/api/audit/settings",
        headers=_headers(admin_token),
        json={"http_endpoint": "https://first.example/ingest"},
    )
    audit_events.clear()
    await client.put(
        "/api/audit/settings",
        headers=_headers(admin_token),
        json={"http_endpoint": "https://second.example/ingest"},
    )
    moved = [kw for action, kw in audit_events if action == "audit.sink_destination_changed"]
    assert len(moved) == 1
    assert moved[0]["severity"] == "critical"
    assert "first.example" in moved[0]["reason"]
    assert "second.example" in moved[0]["reason"]


async def test_siem_settings_save_without_host_move_is_not_flagged(
    client: AsyncClient, admin_token: str, monkeypatch, audit_events
):
    monkeypatch.setattr(settings, "siem_http_token", "")
    await client.put(
        "/api/audit/settings",
        headers=_headers(admin_token),
        json={"http_endpoint": "https://same.example/ingest"},
    )
    audit_events.clear()
    await client.put(
        "/api/audit/settings",
        headers=_headers(admin_token),
        json={"http_endpoint": "https://same.example/ingest", "min_severity": "warning"},
    )
    assert not [a for a, _ in audit_events if a == "audit.sink_destination_changed"]


# ── SMTP host ─────────────────────────────────────────────────────────────────


async def test_smtp_host_pinned_while_password_is_set(
    client: AsyncClient, admin_token: str, monkeypatch
):
    monkeypatch.setattr(settings, "smtp_password", "relay-password")
    monkeypatch.setattr(settings, "smtp_host", "smtp.internal")
    resp = await client.put(
        "/api/email/settings",
        headers=_headers(admin_token),
        json={"smtp_host": "smtp.attacker.example"},
    )
    assert resp.status_code == 422
    assert "SMTP_PASSWORD" in resp.text


async def test_smtp_host_change_is_audited_critical(
    client: AsyncClient, admin_token: str, monkeypatch, audit_events
):
    monkeypatch.setattr(settings, "smtp_password", "")
    await client.put(
        "/api/email/settings", headers=_headers(admin_token), json={"smtp_host": "smtp.one.test"}
    )
    audit_events.clear()
    resp = await client.put(
        "/api/email/settings", headers=_headers(admin_token), json={"smtp_host": "smtp.two.test"}
    )
    assert resp.status_code == 200
    moved = [kw for action, kw in audit_events if action == "email.smtp_host_changed"]
    assert len(moved) == 1
    assert moved[0]["severity"] == "critical"
    assert "smtp.one.test" in moved[0]["reason"]
    assert "smtp.two.test" in moved[0]["reason"]


# ── Proxy URL ─────────────────────────────────────────────────────────────────


async def test_proxy_url_pinned_while_credentials_are_set(
    client: AsyncClient, admin_token: str, monkeypatch
):
    monkeypatch.setattr(settings, "proxy_username", "svc")
    monkeypatch.setattr(settings, "proxy_password", "svc-password")
    monkeypatch.setattr(settings, "proxy_url", "http://proxy.internal:3128")
    resp = await client.put(
        "/api/proxy/settings",
        headers=_headers(admin_token),
        json={"mode": "EXPLICIT", "proxy_url": "http://attacker.example:3128"},
    )
    assert resp.status_code == 422
    assert "PROXY_USERNAME" in resp.text


async def test_proxy_url_free_to_move_without_credentials(
    client: AsyncClient, admin_token: str, monkeypatch, audit_events
):
    monkeypatch.setattr(settings, "proxy_username", "")
    monkeypatch.setattr(settings, "proxy_password", "")
    resp = await client.put(
        "/api/proxy/settings",
        headers=_headers(admin_token),
        json={"mode": "EXPLICIT", "proxy_url": "http://proxy.example:3128"},
    )
    assert resp.status_code == 200
    moved = [kw for action, kw in audit_events if action == "proxy.destination_changed"]
    assert len(moved) == 1
    assert moved[0]["severity"] == "critical"
