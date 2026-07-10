"""Outbound-proxy resolution + wiring (#97).

The central contract is that an *unloaded* proxy cache passes no httpx kwargs at
all, so every call site behaves byte-for-byte as it did before the feature (httpx
defaults ``trust_env=True``, honouring any ambient HTTPS_PROXY). Each wiring test
therefore has a paired "unset" case.
"""

import pytest
from httpx import AsyncClient

from app.config import Settings, settings, validate_settings
from app.services import proxy, siem_service
from app.services.llm.anthropic_provider import AnthropicFamilyAdapter
from app.services.proxy import ProxyConfig
from app.services.siem_service import SiemConfig


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _cfg(**over) -> ProxyConfig:
    base = {"mode": "SYSTEM", "proxy_url": "", "no_proxy": ""}
    base.update(over)
    return ProxyConfig(**base)


@pytest.fixture(autouse=True)
def _reset_proxy_cache():
    """The cache is a module global; keep tests isolated (mirrors the SIEM/LLM fixtures)."""
    proxy.set_config(None)
    yield
    proxy.set_config(None)


# ── resolve(): the mode rules ──────────────────────────────────────────────────


def test_resolve_system_honours_env():
    # Only trust_env — a `proxy: None` here would override the env proxy this mode exists for.
    assert proxy.resolve(_cfg(mode="SYSTEM"), "https://api.anthropic.com") == {"trust_env": True}


def test_resolve_none_is_direct():
    assert proxy.resolve(_cfg(mode="NONE"), "https://api.anthropic.com") == {
        "trust_env": False,
        "proxy": None,
    }


def test_resolve_explicit_uses_proxy():
    out = proxy.resolve(_cfg(mode="EXPLICIT", proxy_url="http://p:3128"), "https://x.example")
    assert out == {"trust_env": False, "proxy": "http://p:3128"}


def test_resolve_explicit_without_url_is_direct():
    assert proxy.resolve(_cfg(mode="EXPLICIT"), "https://x.example") == {
        "trust_env": False,
        "proxy": None,
    }


def test_resolve_unknown_mode_falls_back_to_system():
    assert proxy.resolve(_cfg(mode="garbage"), "https://x.example") == {"trust_env": True}


# ── _should_bypass(): standard NO_PROXY semantics ─────────────────────────────


@pytest.mark.parametrize(
    "host,entries,expected",
    [
        ("api.example.com", ["example.com"], True),  # subdomain suffix
        ("example.com", ["example.com"], True),  # exact domain
        ("api.example.com", [".example.com"], True),  # leading dot
        ("notexample.com", ["example.com"], False),  # no match (not a suffix)
        ("10.1.2.3", ["10.0.0.0/8"], True),  # CIDR hit
        ("11.1.2.3", ["10.0.0.0/8"], False),  # CIDR miss
        ("127.0.0.1", ["127.0.0.1"], True),  # exact IP
        ("localhost", ["localhost"], True),  # localhost
        ("anything.com", ["*"], True),  # wildcard
        ("other.com", ["a.com", "b.com"], False),  # multi-entry, no match
        (None, ["example.com"], True),  # unknown host → direct
    ],
)
def test_should_bypass(host, entries, expected):
    assert proxy._should_bypass(host, entries) is expected


def test_resolve_explicit_bypasses_excluded_host():
    cfg = _cfg(mode="EXPLICIT", proxy_url="http://p:3128", no_proxy="siem.internal")
    assert proxy.resolve(cfg, "https://siem.internal/collector") == {
        "trust_env": False,
        "proxy": None,
    }


# ── credential injection (env-only, never persisted) ──────────────────────────


def test_credentials_injected_from_env(monkeypatch):
    monkeypatch.setattr(proxy.settings, "proxy_username", "bob")
    monkeypatch.setattr(proxy.settings, "proxy_password", "p@ss word")
    out = proxy.resolve(_cfg(mode="EXPLICIT", proxy_url="http://proxy.corp:3128"), "https://x.com")
    assert out["proxy"] == "http://bob:p%40ss%20word@proxy.corp:3128"


def test_no_credentials_when_unset(monkeypatch):
    monkeypatch.setattr(proxy.settings, "proxy_username", "")
    out = proxy.resolve(_cfg(mode="EXPLICIT", proxy_url="http://proxy.corp:3128"), "https://x.com")
    assert out["proxy"] == "http://proxy.corp:3128"


# ── SIEM HTTP sink wiring ─────────────────────────────────────────────────────


def _fake_httpx_client(captured: dict):
    class _FakeResp:
        status_code = 200

        def raise_for_status(self):
            return None

    class _FakeClient:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content=None, headers=None):
            return _FakeResp()

        async def get(self, url):
            return _FakeResp()

    return _FakeClient


async def _emit_http_once() -> None:
    cfg = SiemConfig(
        enabled=True,
        methods=frozenset({"http"}),
        http_endpoint="https://siem.example.com/collector",
    )
    await siem_service.emit({"action": "t", "severity": "info"}, cfg)


async def test_siem_http_uses_proxy(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(siem_service.httpx, "AsyncClient", _fake_httpx_client(captured))
    proxy.set_config(_cfg(mode="EXPLICIT", proxy_url="http://p:3128"))

    await _emit_http_once()

    assert captured["init"]["proxy"] == "http://p:3128"
    assert captured["init"]["trust_env"] is False


async def test_siem_http_bypasses_internal_collector(monkeypatch):
    """The classic corporate shape: proxy the LLM, go direct to the internal SIEM."""
    captured: dict = {}
    monkeypatch.setattr(siem_service.httpx, "AsyncClient", _fake_httpx_client(captured))
    proxy.set_config(_cfg(mode="EXPLICIT", proxy_url="http://p:3128", no_proxy="siem.example.com"))

    await _emit_http_once()

    assert captured["init"]["proxy"] is None


async def test_siem_http_unchanged_when_cache_unloaded(monkeypatch):
    """The backwards-compatibility contract: no cache → no kwargs at all."""
    captured: dict = {}
    monkeypatch.setattr(siem_service.httpx, "AsyncClient", _fake_httpx_client(captured))
    proxy.set_config(None)

    await _emit_http_once()

    assert "proxy" not in captured["init"]
    assert "trust_env" not in captured["init"]


# ── LLM adapter wiring ────────────────────────────────────────────────────────


def _llm_cfg(**over):
    from app.config import LLMProviderConfig

    base = {"key": "anthropic", "display_name": "A", "adapter": "anthropic", "model": "m"}
    base.update(over)
    return LLMProviderConfig(**base)


def test_anthropic_api_base_direct():
    adapter = AnthropicFamilyAdapter(_llm_cfg())
    assert adapter.api_base() == "https://api.anthropic.com"


def test_bedrock_api_base_is_the_regional_aws_endpoint():
    """Bedrock dials AWS, not the Anthropic API — the no-proxy list must match that host."""
    adapter = AnthropicFamilyAdapter(_llm_cfg(backend="bedrock", aws_region="eu-west-2"))
    assert adapter.api_base() == "https://bedrock-runtime.eu-west-2.amazonaws.com"


def test_bedrock_bypass_keys_off_the_aws_host():
    """Excluding the AWS host must make Bedrock go direct — proves which URL is resolved.

    Note a bypassed host still gets a *client*, carrying ``trust_env=False``: passing
    ``http_client=None`` would let the SDK build its own (``trust_env=True``) client and
    silently re-enable the env proxy for a host the admin excluded.
    """
    bedrock = AnthropicFamilyAdapter(_llm_cfg(backend="bedrock", aws_region="eu-west-2"))
    proxy.set_config(
        _cfg(
            mode="EXPLICIT",
            proxy_url="http://p:3128",
            no_proxy="bedrock-runtime.eu-west-2.amazonaws.com",
        )
    )
    bedrock_kwargs = proxy.resolve_kwargs(bedrock.api_base())
    assert bedrock_kwargs == {"trust_env": False, "proxy": None}
    assert bedrock._http_client() is not None  # direct, but env proxy stays disabled

    # ...while the Anthropic host is still proxied under the very same config.
    direct = AnthropicFamilyAdapter(_llm_cfg())
    assert proxy.resolve_kwargs(direct.api_base())["proxy"] == "http://p:3128"


def test_anthropic_client_gets_proxied_http_client(monkeypatch):
    captured: dict = {}

    class _FakeAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeAnthropic)
    proxy.set_config(_cfg(mode="EXPLICIT", proxy_url="http://p:3128"))

    AnthropicFamilyAdapter(_llm_cfg())._get_client()

    assert captured["http_client"] is not None


async def test_anthropic_client_unchanged_when_cache_unloaded(monkeypatch):
    captured: dict = {}

    class _FakeAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeAnthropic)
    proxy.set_config(None)

    AnthropicFamilyAdapter(_llm_cfg())._get_client()

    # None → the SDK builds its own default client, exactly as before the feature.
    assert captured["http_client"] is None


# ── OIDC wiring ───────────────────────────────────────────────────────────────


def _enable_entra(monkeypatch):
    monkeypatch.setattr(settings, "oidc_entra_enabled", True)
    monkeypatch.setattr(settings, "oidc_entra_client_id", "cid")
    monkeypatch.setattr(settings, "oidc_entra_client_secret", "sec")
    monkeypatch.setattr(settings, "oidc_entra_tenant_id", "tenant-guid")


def test_oidc_register_passes_proxy_into_client_kwargs(monkeypatch):
    from app.services.oidc import service as oidc_service

    oidc_service.reset_registration()
    _enable_entra(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr(oidc_service.oauth, "register", lambda **kw: captured.update(kw))
    proxy.set_config(_cfg(mode="EXPLICIT", proxy_url="http://p:3128"))

    oidc_service.register_providers()
    oidc_service.reset_registration()

    assert captured["client_kwargs"]["proxy"] == "http://p:3128"
    assert captured["client_kwargs"]["trust_env"] is False
    assert captured["client_kwargs"]["code_challenge_method"] == "S256"


def test_oidc_register_unchanged_when_cache_unloaded(monkeypatch):
    from app.services.oidc import service as oidc_service

    oidc_service.reset_registration()
    _enable_entra(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr(oidc_service.oauth, "register", lambda **kw: captured.update(kw))
    proxy.set_config(None)

    oidc_service.register_providers()
    oidc_service.reset_registration()

    assert "proxy" not in captured["client_kwargs"]
    assert "trust_env" not in captured["client_kwargs"]


def test_reset_registration_rebuilds_the_authlib_client(monkeypatch):
    """Authlib's create_client() caches per name — register() alone keeps the stale
    client (and its old proxy). reset_registration() must produce a fresh registry."""
    from app.services.oidc import service as oidc_service

    oidc_service.reset_registration()
    _enable_entra(monkeypatch)

    oidc_service.register_providers()
    first_registry = oidc_service.oauth
    first_client = oidc_service.oauth.create_client("entra")

    oidc_service.reset_registration()
    assert oidc_service.oauth is not first_registry  # fresh registry object
    assert oidc_service._registration_done is False

    oidc_service.register_providers()
    assert oidc_service.oauth.create_client("entra") is not first_client
    oidc_service.reset_registration()


# ── validate_settings ─────────────────────────────────────────────────────────


def test_validate_rejects_unknown_mode():
    with pytest.raises(RuntimeError, match="PROXY_MODE"):
        validate_settings(Settings(dev_mode=True, proxy_mode="sideways"))


def test_validate_rejects_explicit_without_url():
    with pytest.raises(RuntimeError, match="requires PROXY_URL"):
        validate_settings(Settings(dev_mode=True, proxy_mode="explicit", proxy_url=""))


def test_validate_rejects_bad_proxy_url_scheme():
    with pytest.raises(RuntimeError, match="PROXY_URL must be"):
        validate_settings(
            Settings(dev_mode=True, proxy_mode="explicit", proxy_url="proxy.corp:3128")
        )


def test_validate_accepts_defaults():
    validate_settings(Settings(dev_mode=True))  # SYSTEM, no URL → fine


# ── admin API ─────────────────────────────────────────────────────────────────


async def test_proxy_settings_requires_admin(client: AsyncClient, facilitator_token: str):
    r = await client.get("/api/proxy/settings", headers=_bearer(facilitator_token))
    assert r.status_code == 403


async def test_proxy_settings_seeded_from_env(client: AsyncClient, admin_token: str):
    r = await client.get("/api/proxy/settings", headers=_bearer(admin_token))
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "SYSTEM"  # backwards-compatible default
    assert "127.0.0.0/8" in body["no_proxy"]


async def test_proxy_settings_round_trip_and_cache_refresh(client: AsyncClient, admin_token: str):
    r = await client.put(
        "/api/proxy/settings",
        json={
            "mode": "explicit",
            "proxy_url": "http://proxy.corp:3128",
            "no_proxy": "siem.internal",
        },
        headers=_bearer(admin_token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "EXPLICIT"  # normalised

    # The in-memory cache the sync emit path reads must reflect the save.
    cached = proxy.get_config()
    assert cached is not None
    assert cached.proxy_url == "http://proxy.corp:3128"


async def test_proxy_settings_never_accepts_credentials(client: AsyncClient, admin_token: str):
    r = await client.put(
        "/api/proxy/settings",
        json={"mode": "EXPLICIT", "proxy_url": "http://p:3128", "proxy_username": "bob"},
        headers=_bearer(admin_token),
    )
    assert r.status_code == 200
    assert "proxy_username" not in r.json()


async def test_proxy_save_invalidates_llm_and_oidc_caches(
    client: AsyncClient, admin_token: str, monkeypatch
):
    from app.services.llm import service as llm_service
    from app.services.oidc import service as oidc_service

    calls = []
    monkeypatch.setattr(llm_service, "reset_provider_cache", lambda: calls.append("llm"))
    monkeypatch.setattr(oidc_service, "reset_registration", lambda: calls.append("oidc"))

    r = await client.put("/api/proxy/settings", json={"mode": "NONE"}, headers=_bearer(admin_token))
    assert r.status_code == 200
    assert calls == ["llm", "oidc"]


def _stub_targets(monkeypatch, targets: dict[str, str]):
    import app.routers.proxy as proxy_router

    monkeypatch.setattr(proxy_router, "egress_targets", lambda: targets)


async def test_proxy_targets_lists_labels_not_urls(
    client: AsyncClient, admin_token: str, monkeypatch
):
    _stub_targets(monkeypatch, {"LLM (anthropic)": "https://api.anthropic.com"})
    r = await client.get("/api/proxy/targets", headers=_bearer(admin_token))
    assert r.status_code == 200
    body = r.json()
    assert body["targets"] == ["LLM (anthropic)"]
    # The URL itself is server-side only.
    assert "api.anthropic.com" not in r.text


async def test_proxy_test_route_reports_status(client: AsyncClient, admin_token: str, monkeypatch):
    import app.routers.proxy as proxy_router

    captured: dict = {}
    monkeypatch.setattr(proxy_router.httpx, "AsyncClient", _fake_httpx_client(captured))
    _stub_targets(monkeypatch, {"LLM (anthropic)": "https://api.anthropic.com"})

    r = await client.post(
        "/api/proxy/test", json={"target": "LLM (anthropic)"}, headers=_bearer(admin_token)
    )
    assert r.status_code == 200
    assert r.json()["result"] == "ok: HTTP 200"


async def test_proxy_test_rejects_arbitrary_url(client: AsyncClient, admin_token: str, monkeypatch):
    """SSRF guard: the dialled URL is chosen server-side, never taken from the body."""
    import app.routers.proxy as proxy_router

    captured: dict = {}
    monkeypatch.setattr(proxy_router.httpx, "AsyncClient", _fake_httpx_client(captured))
    _stub_targets(monkeypatch, {"LLM (anthropic)": "https://api.anthropic.com"})

    r = await client.post(
        "/api/proxy/test",
        json={"target": "http://169.254.169.254/latest/meta-data/"},
        headers=_bearer(admin_token),
    )
    assert r.status_code == 400
    assert captured == {}  # nothing was dialled


async def test_proxy_test_route_never_leaks_credentials_or_exception_text(
    client: AsyncClient, admin_token: str, monkeypatch
):
    """An httpx error can embed the proxy URL (with injected creds) and internal hosts.
    Only the exception *class* is returned; the message goes to the server log."""
    monkeypatch.setattr(settings, "proxy_username", "bob")
    monkeypatch.setattr(settings, "proxy_password", "hunter2")

    await client.put(
        "/api/proxy/settings",
        json={"mode": "EXPLICIT", "proxy_url": "http://proxy.corp:3128"},
        headers=_bearer(admin_token),
    )

    class _Boom:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            raise RuntimeError("connect failed via http://bob:hunter2@proxy.corp:3128")

        async def __aexit__(self, *a):
            return False

    import app.routers.proxy as proxy_router

    monkeypatch.setattr(proxy_router.httpx, "AsyncClient", _Boom)
    _stub_targets(monkeypatch, {"LLM (anthropic)": "https://api.anthropic.com"})

    r = await client.post(
        "/api/proxy/test", json={"target": "LLM (anthropic)"}, headers=_bearer(admin_token)
    )
    assert r.status_code == 200
    result = r.json()["result"]
    assert result == "error: RuntimeError"  # class only, no message
    assert "hunter2" not in result
    assert "proxy.corp" not in result


def test_scrub_redacts_credentials_from_log_text(monkeypatch):
    """The server-side log line is scrubbed even though it keeps the message."""
    import app.routers.proxy as proxy_router

    monkeypatch.setattr(settings, "proxy_username", "bob")
    monkeypatch.setattr(settings, "proxy_password", "p@ss")
    out = proxy_router._scrub("failed via http://bob:p%40ss@proxy.corp:3128")
    assert "p@ss" not in out
    assert "p%40ss" not in out
    assert "bob" not in out


async def test_proxy_targets_requires_admin(client: AsyncClient, facilitator_token: str):
    r = await client.get("/api/proxy/targets", headers=_bearer(facilitator_token))
    assert r.status_code == 403


async def test_admin_proxy_page_requires_admin(client: AsyncClient, participant_token: str):
    r = await client.get("/admin/proxy", follow_redirects=False)
    assert r.status_code in (302, 307)
