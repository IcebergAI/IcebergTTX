"""OpenAI-compatible adapter: OpenAI, Ollama, and Gemini.

All three speak the OpenAI Chat Completions surface, so one adapter covers them —
they differ only in ``base_url`` (OpenAI's default, Ollama's local endpoint, or
Gemini's OpenAI-compatible endpoint), the model ID, and the API key (Ollama needs
none). This adapter does not do prompt caching: it concatenates the cacheable
context into the user message (these providers have no ``cache_control`` concept).

The ``openai`` SDK is the optional ``llm-openai`` extra, imported lazily so a
deployment that only uses the Anthropic family never needs it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.services import proxy
from app.services.llm.base import register_adapter

if TYPE_CHECKING:
    import httpx2

    from app.config import LLMProviderConfig

OPENAI_API_BASE = "https://api.openai.com"


class OpenAICompatAdapter:
    """Adapter for any OpenAI-compatible Chat Completions endpoint."""

    def __init__(self, cfg: LLMProviderConfig) -> None:
        self.cfg = cfg
        self.key = cfg.key
        self.model = cfg.model
        self.llm_model_label = f"{cfg.key}:{cfg.model}"
        self._client = None

    def api_base(self) -> str:
        """The URL the SDK actually dials — what the no-proxy list is matched against.
        Ollama's local endpoint is covered by the default no-proxy list."""
        return self.cfg.base_url or OPENAI_API_BASE

    def _http_client(self) -> httpx2.AsyncClient | None:
        """A proxied httpx2 client, or None to let the SDK build its own default.
        Resolved once, against the base URL — the SDK client is long-lived.

        ``httpx2`` (not ``httpx``) because openai>=3 is built on it; it ships with
        the SDK, so the import is lazy like the SDK's own — core installs without
        the ``llm-openai`` extra have neither, and this module imports eagerly.
        The proxy kwargs (``proxy`` / ``trust_env``) are spelled the same in both.
        """
        proxy_kwargs = proxy.resolve_kwargs(self.api_base())
        if not proxy_kwargs:
            return None
        import httpx2

        return httpx2.AsyncClient(**proxy_kwargs)

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError(
                f"LLM_PROVIDER={self.key} requires the OpenAI extra: "
                "`pip install '.[llm-openai]'`."
            ) from exc
        # Ollama accepts any non-empty key; pass a placeholder when none is set.
        self._client = AsyncOpenAI(
            api_key=self.cfg.api_key or "not-needed",
            base_url=self.cfg.base_url or None,
            http_client=self._http_client(),
        )
        return self._client

    async def aclose(self) -> None:
        """Close the built SDK client and its HTTP pool; a no-op when unbuilt (#269)."""
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    async def complete(
        self, system: str, cached_context: str, user_prompt: str, max_tokens: int
    ) -> str:
        client = self._get_client()
        resp = await client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"{cached_context}\n\n{user_prompt}"},
            ],
        )
        return resp.choices[0].message.content or ""


register_adapter("openai", OpenAICompatAdapter)
