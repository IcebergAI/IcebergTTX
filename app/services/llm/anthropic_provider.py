"""Anthropic-family adapter: direct Anthropic API and Amazon Bedrock.

Both backends share the ``client.messages.create`` surface, so they differ only in
client construction (``AsyncAnthropic`` vs ``AsyncAnthropicBedrock``) and the model
ID (Bedrock requires an ``anthropic.``-prefixed ID, direct Anthropic must not carry
it). Prompt caching (a ``cache_control`` block on the shared context + the
``anthropic-beta`` header) is applied only on the direct API path.

The ``anthropic`` SDK is a core dependency; ``anthropic[bedrock]`` (boto3) is the
optional ``llm-bedrock`` extra, imported lazily so a non-Bedrock deployment never
needs it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.llm.base import register_adapter

if TYPE_CHECKING:
    from app.config import LLMProviderConfig

_PROMPT_CACHING_BETA = "prompt-caching-2024-07-31"


class AnthropicFamilyAdapter:
    """Adapter for Anthropic-hosted Claude models (direct API or Bedrock)."""

    def __init__(self, cfg: LLMProviderConfig) -> None:
        self.cfg = cfg
        self.key = cfg.key
        self.model = cfg.model
        self.llm_model_label = f"{cfg.key}:{cfg.model}"
        self._client = None

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
        if self.cfg.backend == "bedrock":
            try:
                self._client = anthropic.AsyncAnthropicBedrock(
                    aws_region=self.cfg.aws_region or None
                )
            except (ImportError, AttributeError) as exc:
                raise RuntimeError(
                    "LLM_PROVIDER=bedrock requires the AWS extra: "
                    "`pip install '.[llm-bedrock]'`."
                ) from exc
        else:
            self._client = anthropic.AsyncAnthropic(api_key=self.cfg.api_key)
        return self._client

    async def complete(
        self, system: str, cached_context: str, user_prompt: str, max_tokens: int
    ) -> str:
        client = self._get_client()
        kwargs: dict = {}
        if self.cfg.backend == "bedrock":
            # Bedrock: send a plain two-part message (no cache_control / beta header).
            context_block: dict = {"type": "text", "text": cached_context}
        else:
            context_block = {
                "type": "text",
                "text": cached_context,
                "cache_control": {"type": "ephemeral"},
            }
            kwargs["extra_headers"] = {"anthropic-beta": _PROMPT_CACHING_BETA}
        msg = await client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[
                {
                    "role": "user",
                    "content": [context_block, {"type": "text", "text": user_prompt}],
                }
            ],
            **kwargs,
        )
        for block in msg.content:
            text = getattr(block, "text", None)
            if text is not None:
                return text
        return ""


register_adapter("anthropic", AnthropicFamilyAdapter)
