"""Anthropic-family adapter: direct Anthropic API and Amazon Bedrock.

Both backends share the ``client.messages.create`` surface, so they differ only in
client construction (``AsyncAnthropic`` vs ``AsyncAnthropicBedrock``) and the model
ID (Bedrock requires an ``anthropic.``-prefixed ID, direct Anthropic must not carry
it). Prompt caching (a ``cache_control`` block on the shared context) is applied
only on the direct API path; the retired beta header is no longer required.

The ``anthropic`` SDK is a core dependency; ``anthropic[bedrock]`` (boto3) is the
optional ``llm-bedrock`` extra, imported lazily so a non-Bedrock deployment never
needs it.
"""
# pyright: reportPrivateImportUsage=false, reportCallIssue=false, reportArgumentType=false
# The optional Bedrock class and documented message payload shape are selected at
# runtime, but are not fully represented by the installed Anthropic SDK stubs.

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from app.services import proxy
from app.services.llm.base import register_adapter

if TYPE_CHECKING:
    from app.config import LLMProviderConfig

ANTHROPIC_API_BASE = "https://api.anthropic.com"
_BEDROCK_HOST = "https://bedrock-runtime.{region}.amazonaws.com"


class AnthropicFamilyAdapter:
    """Adapter for Anthropic-hosted Claude models (direct API or Bedrock)."""

    def __init__(self, cfg: LLMProviderConfig) -> None:
        self.cfg = cfg
        self.key = cfg.key
        self.model = cfg.model
        self.llm_model_label = f"{cfg.key}:{cfg.model}"
        self._client = None

    def api_base(self) -> str:
        """The URL the SDK actually dials — what the no-proxy list is matched against.

        Bedrock talks to a regional AWS endpoint, not the Anthropic API, so the
        bypass decision must key off that host.
        """
        if self.cfg.backend == "bedrock" and self.cfg.aws_region:
            return _BEDROCK_HOST.format(region=self.cfg.aws_region)
        return ANTHROPIC_API_BASE

    def _http_client(self) -> httpx.AsyncClient | None:
        """A proxied httpx client, or None to let the SDK build its own default.

        Resolved once here, against the provider's base URL, because the SDK client
        is long-lived; a proxy change invalidates it via ``reset_provider_cache()``.
        """
        proxy_kwargs = proxy.resolve_kwargs(self.api_base())
        return httpx.AsyncClient(**proxy_kwargs) if proxy_kwargs else None

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as exc:
            extra = "llm-bedrock" if self.cfg.backend == "bedrock" else "llm-anthropic"
            raise RuntimeError(
                f"LLM_PROVIDER={self.key} requires the Anthropic extra: "
                f"`pip install '.[{extra}]'`."
            ) from exc
        http_client = self._http_client()
        if self.cfg.backend == "bedrock":
            try:
                self._client = anthropic.AsyncAnthropicBedrock(
                    aws_region=self.cfg.aws_region or None, http_client=http_client
                )
            except (ImportError, AttributeError) as exc:
                raise RuntimeError(
                    "LLM_PROVIDER=bedrock requires the AWS extra: "
                    "`pip install '.[llm-bedrock]'`."
                ) from exc
        else:
            self._client = anthropic.AsyncAnthropic(
                api_key=self.cfg.api_key, http_client=http_client
            )
        return self._client

    async def complete(
        self, system: str, cached_context: str, user_prompt: str, max_tokens: int
    ) -> str:
        client = self._get_client()
        # Only prepend a context block when there's actually cached context. An empty
        # string is a non-empty text content block on the wire, which the Anthropic API
        # rejects with a 400 ("text content blocks must be non-empty") — and on the
        # direct path it would also carry a pointless cache_control. Omitting it keeps
        # any empty-context caller working, notably the admin connectivity check (#261).
        content: list[dict] = []
        if cached_context:
            if self.cfg.backend == "bedrock":
                # Bedrock: a plain text block (no cache_control / beta header).
                content.append({"type": "text", "text": cached_context})
            else:
                content.append(
                    {
                        "type": "text",
                        "text": cached_context,
                        "cache_control": {"type": "ephemeral"},
                    }
                )
        content.append({"type": "text", "text": user_prompt})
        msg = await client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
        )
        for block in msg.content:
            text = getattr(block, "text", None)
            if text is not None:
                return text
        return ""


register_adapter("anthropic", AnthropicFamilyAdapter)
