"""Provider-adapter contract + registry for the pluggable AI backend.

An adapter turns a provider-neutral completion request (system prompt, a cacheable
context prefix, and a per-response user prompt) into a plain text response, hiding
the provider SDK's request shape and response parsing. Everything else about the
LLM pipeline (prompt assembly, JSON parsing, DB writes, WebSocket broadcasts) is
provider-agnostic and lives in ``app/services/llm_service.py``.

Adapters are keyed by *family* — the SDK surface they speak — not by provider key:
the ``anthropic`` family covers direct Anthropic and Amazon Bedrock (same
``messages.create`` surface), and the ``openai`` family covers OpenAI, Ollama, and
Gemini (all via the OpenAI-compatible Chat Completions surface, differing only by
base URL). Mirrors the OIDC ``register_adapter`` / ``get_adapter`` registry (#25).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from app.services import proxy

if TYPE_CHECKING:
    import httpx2

    from app.config import LLMProviderConfig


def sdk_http_client(api_base: str) -> httpx2.AsyncClient | None:
    """A proxied ``httpx2`` client for an SDK to dial ``api_base`` with, or None to
    let the SDK build its own default.

    ``httpx2`` is imported here rather than at module scope because it arrives only
    with a provider SDK (both ship it in place of ``httpx``), and no LLM SDK is a
    core dependency — ``service.py`` imports every adapter module unconditionally to
    self-register, so a base install with no ``llm-*`` extra must still import them.
    Same reason the adapters import their SDK inside ``_get_client()``.

    The client is built against the provider's base URL and resolved once, because
    the SDK client is long-lived; a proxy change invalidates it via
    ``reset_provider_cache()``.
    """
    proxy_kwargs = proxy.resolve_kwargs(api_base)
    if not proxy_kwargs:
        return None
    import httpx2

    return httpx2.AsyncClient(**proxy_kwargs)


class LLMProvider(Protocol):
    """A configured, ready-to-call AI backend.

    ``llm_model_label`` is what gets stamped into ``ResponseAssessment.llm_model`` /
    ``SuggestedInject.llm_model`` (e.g. ``"anthropic:claude-opus-4-8"``), so the
    persisted record identifies both the provider and the model that produced it.
    """

    key: str
    model: str
    llm_model_label: str

    async def complete(
        self, system: str, cached_context: str, user_prompt: str, max_tokens: int
    ) -> str:
        """Run one completion and return the model's text (``""`` if none)."""
        ...


# Adapter classes register here at import time, keyed by family.
_REGISTRY: dict[str, type[LLMProvider]] = {}


def register_adapter(family: str, adapter_cls: type[LLMProvider]) -> None:
    _REGISTRY[family] = adapter_cls


def get_adapter(family: str) -> type[LLMProvider] | None:
    return _REGISTRY.get(family)


def build_provider(cfg: LLMProviderConfig) -> LLMProvider | None:
    """Instantiate the adapter for a resolved provider config, or None if the
    family has no registered adapter."""
    adapter_cls = get_adapter(cfg.adapter)
    if adapter_cls is None:
        return None
    return adapter_cls(cfg)  # type: ignore[call-arg]
