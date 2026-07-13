"""Tests for the pluggable AI provider registry, config, and adapters (#26).

No provider SDK is exercised for real: the Anthropic and OpenAI clients are mocked
at each adapter's ``_get_client`` seam. Config resolution and startup validation are
exercised against ``Settings`` instances built with explicit env overrides.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import (
    GEMINI_OPENAI_BASE_URL,
    OLLAMA_DEFAULT_BASE_URL,
    Settings,
    validate_settings,
)
from app.services.llm.anthropic_provider import AnthropicFamilyAdapter
from app.services.llm.base import build_provider, get_adapter
from app.services.llm.openai_provider import OpenAICompatAdapter


def _settings(**overrides) -> Settings:
    # dev_mode relaxes the credential checks so we can build configs without keys.
    base = {"dev_mode": True}
    base.update(overrides)
    return Settings(**base)


# ── Config resolution ─────────────────────────────────────────────────────────

def test_disabled_provider_returns_none():
    for value in ("", "none", "disabled"):
        assert _settings(llm_provider=value).active_llm_provider() is None


def test_anthropic_config():
    cfg = _settings(
        llm_provider="anthropic", anthropic_api_key="sk-x", anthropic_model="claude-x"
    ).active_llm_provider()
    assert cfg.adapter == "anthropic"
    assert cfg.backend == "api"
    assert cfg.model == "claude-x"
    assert cfg.api_key == "sk-x"


def test_bedrock_config_uses_anthropic_family_and_region():
    cfg = _settings(
        llm_provider="bedrock",
        bedrock_model="anthropic.claude-x",
        bedrock_aws_region="eu-west-2",
    ).active_llm_provider()
    assert cfg.adapter == "anthropic"
    assert cfg.backend == "bedrock"
    assert cfg.model == "anthropic.claude-x"
    assert cfg.aws_region == "eu-west-2"


@pytest.mark.parametrize(
    "provider,expected_base",
    [
        ("openai", ""),  # "" ⇒ SDK default (api.openai.com)
        ("ollama", OLLAMA_DEFAULT_BASE_URL),
        ("gemini", GEMINI_OPENAI_BASE_URL),
    ],
)
def test_openai_family_base_url_resolution(provider, expected_base):
    cfg = _settings(llm_provider=provider).active_llm_provider()
    assert cfg.adapter == "openai"
    assert cfg.base_url == expected_base


def test_max_tokens_flows_from_settings():
    cfg = _settings(llm_provider="anthropic", llm_max_tokens=1234).active_llm_provider()
    assert cfg.max_tokens == 1234


# ── validate_settings ─────────────────────────────────────────────────────────

def test_unknown_provider_key_rejected():
    with pytest.raises(RuntimeError, match="LLM_PROVIDER must be one of"):
        validate_settings(_settings(dev_mode=False, llm_provider="bogus"))


def test_unknown_provider_key_rejected_even_in_dev_mode():
    # The key-name check runs regardless of dev_mode (like AUTH_MODE).
    with pytest.raises(RuntimeError, match="LLM_PROVIDER must be one of"):
        validate_settings(_settings(llm_provider="bogus"))


def test_missing_api_key_is_deferred_to_runtime_save_validation():
    s = Settings(
        dev_mode=False,
        secret_key="x" * 40,
        auth_mode="local",
        llm_provider="openai",
        openai_api_key="",
    )
    validate_settings(s)


def test_missing_bedrock_region_is_deferred_to_runtime_save_validation():
    s = Settings(
        dev_mode=False,
        secret_key="x" * 40,
        auth_mode="local",
        llm_provider="bedrock",
        bedrock_aws_region="",
    )
    validate_settings(s)


def test_ollama_needs_no_credentials_in_production():
    s = Settings(
        dev_mode=False, secret_key="x" * 40, auth_mode="local", llm_provider="ollama"
    )
    validate_settings(s)  # no raise


# ── Registry ──────────────────────────────────────────────────────────────────

def test_registry_maps_families_to_adapters():
    assert get_adapter("anthropic") is AnthropicFamilyAdapter
    assert get_adapter("openai") is OpenAICompatAdapter
    assert get_adapter("nonexistent") is None


def test_build_provider_instantiates_the_family_adapter():
    cfg = _settings(llm_provider="ollama").active_llm_provider()
    provider = build_provider(cfg)
    assert isinstance(provider, OpenAICompatAdapter)
    assert provider.llm_model_label == f"ollama:{cfg.model}"


# ── Anthropic-family adapter ──────────────────────────────────────────────────

def _anthropic_message(text: str):
    block = MagicMock()
    block.text = text
    msg = MagicMock()
    msg.content = [block]
    return msg


@pytest.mark.asyncio
async def test_anthropic_adapter_parses_content_text(monkeypatch):
    cfg = _settings(
        llm_provider="anthropic", anthropic_api_key="sk-x"
    ).active_llm_provider()
    adapter = AnthropicFamilyAdapter(cfg)
    client = MagicMock()
    client.messages = MagicMock(
        create=AsyncMock(return_value=_anthropic_message("hello"))
    )
    monkeypatch.setattr(adapter, "_get_client", lambda: client)

    out = await adapter.complete("sys", "context", "prompt", 600)

    assert out == "hello"
    kwargs = client.messages.create.call_args.kwargs
    # Direct API path carries the prompt-caching header + cache_control block.
    assert kwargs["extra_headers"]["anthropic-beta"] == "prompt-caching-2024-07-31"
    assert kwargs["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_bedrock_adapter_omits_cache_control(monkeypatch):
    cfg = _settings(
        llm_provider="bedrock", bedrock_aws_region="us-east-1"
    ).active_llm_provider()
    adapter = AnthropicFamilyAdapter(cfg)
    client = MagicMock()
    client.messages = MagicMock(return_value=None)
    client.messages.create = AsyncMock(return_value=_anthropic_message("hi"))
    monkeypatch.setattr(adapter, "_get_client", lambda: client)

    out = await adapter.complete("sys", "context", "prompt", 600)

    assert out == "hi"
    kwargs = client.messages.create.call_args.kwargs
    assert "extra_headers" not in kwargs
    assert "cache_control" not in kwargs["messages"][0]["content"][0]


# ── OpenAI-compatible adapter (OpenAI / Ollama / Gemini) ──────────────────────

def _openai_completion(text: str):
    choice = MagicMock()
    choice.message = MagicMock(content=text)
    resp = MagicMock()
    resp.choices = [choice]
    return resp


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "ollama", "gemini"])
async def test_openai_family_shares_adapter_and_parses_choice(provider, monkeypatch):
    cfg = _settings(
        llm_provider=provider,
        openai_api_key="sk-o",
        gemini_api_key="sk-g",
    ).active_llm_provider()
    adapter = OpenAICompatAdapter(cfg)
    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock(
        create=AsyncMock(return_value=_openai_completion("answer"))
    )
    monkeypatch.setattr(adapter, "_get_client", lambda: client)

    out = await adapter.complete("sys", "context", "prompt", 600)

    assert out == "answer"
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == cfg.model
    assert kwargs["messages"][0]["role"] == "system"
    assert "context" in kwargs["messages"][1]["content"]
