"""Active-provider resolution for the pluggable AI backend.

Force-imports the adapter family modules so they self-register (mirrors
``app/services/oidc/service.py``), then resolves the single active provider from
``settings.active_llm_provider()`` (selected by the global ``LLM_PROVIDER`` setting,
the AI analog of ``AUTH_MODE``). The built provider is cached for the process;
``reset_provider_cache()`` clears it (used by tests, same discipline as the OIDC
lazy registration).
"""

from __future__ import annotations

from app.config import settings
from app.services.llm import anthropic_provider as _anthropic  # noqa: F401 - registers adapter
from app.services.llm import openai_provider as _openai  # noqa: F401 - registers adapter
from app.services.llm.base import LLMProvider, build_provider

_provider: LLMProvider | None = None
_provider_built = False


def build_active_provider() -> LLMProvider | None:
    """Build the active provider from config, or None when the LLM is disabled or
    the selected family has no adapter."""
    cfg = settings.active_llm_provider()
    if cfg is None:
        return None
    return build_provider(cfg)


def active_provider() -> LLMProvider | None:
    """The process-wide active provider (built once, then cached)."""
    global _provider, _provider_built
    if not _provider_built:
        _provider = build_active_provider()
        _provider_built = True
    return _provider


def reset_provider_cache() -> None:
    """Drop the cached provider so the next ``active_provider()`` rebuilds it."""
    global _provider, _provider_built
    _provider = None
    _provider_built = False
