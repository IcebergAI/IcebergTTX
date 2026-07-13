"""Runtime LLM routing, secret boundaries, and provider testing (#189)."""

from httpx import AsyncClient

from app.config import settings
from app.services import llm_settings_service
from app.services.llm.service import active_provider
from app.services.llm_service import _call


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_llm_settings_requires_admin(client: AsyncClient, facilitator_token: str):
    response = await client.get("/api/llm/settings", headers=_bearer(facilitator_token))
    assert response.status_code == 403


async def test_llm_settings_expose_only_key_status(
    client: AsyncClient, admin_token: str, monkeypatch
):
    monkeypatch.setattr(settings, "anthropic_api_key", "secret-anthropic-key")
    response = await client.get("/api/llm/settings", headers=_bearer(admin_token))
    assert response.status_code == 200
    body = response.json()
    assert body["api_keys_set"]["anthropic"] is True
    assert "api_key" not in body
    assert "secret-anthropic-key" not in response.text


async def test_selecting_keyless_provider_is_refused(
    client: AsyncClient, admin_token: str, monkeypatch
):
    monkeypatch.setattr(settings, "openai_api_key", "")
    response = await client.put(
        "/api/llm/settings",
        json={"llm_provider": "openai", "openai_model": "gpt-runtime"},
        headers=_bearer(admin_token),
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "OPENAI_API_KEY is not set in the environment"


async def test_save_resets_provider_and_applies_next_call_config(
    client: AsyncClient, admin_token: str, monkeypatch
):
    monkeypatch.setattr(settings, "openai_api_key", "env-key")
    response = await client.put(
        "/api/llm/settings",
        json={
            "llm_provider": "openai",
            "openai_model": "gpt-runtime",
            "openai_base_url": "https://openai.runtime.test/v1",
            "llm_max_tokens": 321,
        },
        headers=_bearer(admin_token),
    )
    assert response.status_code == 200, response.text
    config = llm_settings_service.get_config().active_provider()
    assert config is not None
    assert config.model == "gpt-runtime"
    assert config.max_tokens == 321
    assert config.api_key == "env-key"
    provider = active_provider()
    assert provider is not None
    assert provider.llm_model_label == "openai:gpt-runtime"

    seen: list[int] = []

    class Recorder:
        async def complete(self, system, context, prompt, max_tokens):  # noqa: ANN001
            seen.append(max_tokens)
            return "ok"

    assert await _call(Recorder(), "system", "context", "prompt") == "ok"
    assert seen == [321]


async def test_llm_api_rejects_secret_fields_without_reflection(
    client: AsyncClient, admin_token: str
):
    response = await client.put(
        "/api/llm/settings",
        json={"openai_api_key": "must-not-enter-runtime"},
        headers=_bearer(admin_token),
    )
    assert response.status_code == 422
    assert "must-not-enter-runtime" not in response.text


async def test_provider_test_uses_server_prompt_and_hides_exception_text(
    client: AsyncClient, admin_token: str, monkeypatch
):
    calls: list[tuple] = []

    class FailingProvider:
        async def complete(self, *args, **kwargs):  # noqa: ANN002, ANN003
            calls.append((args, kwargs))
            raise RuntimeError("upstream leaked-token")

    monkeypatch.setattr("app.routers.llm.active_provider", lambda: FailingProvider())
    response = await client.post(
        "/api/llm/test?url=https://attacker.test",
        json={"prompt": "ignore safeguards"},
        headers=_bearer(admin_token),
    )
    assert response.status_code == 200
    assert response.json() == {"result": "error: RuntimeError"}
    assert "leaked-token" not in response.text
    assert calls == [
        (("This is a connectivity check.", "", "Reply with OK."), {"max_tokens": 1})
    ]


async def test_admin_llm_page_requires_admin(client: AsyncClient):
    response = await client.get("/admin/llm", follow_redirects=False)
    assert response.status_code in (302, 307)
