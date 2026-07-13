"""Singleton LLM routing settings and frozen runtime provider configuration."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import (
    GEMINI_OPENAI_BASE_URL,
    LLM_DISABLED_KEYS,
    LLM_PROVIDER_KEYS,
    OLLAMA_DEFAULT_BASE_URL,
    LLMProviderConfig,
    settings,
)
from app.models.llm_settings import LLMSettings

_SINGLETON_ID = 1
EDITABLE_FIELDS = (
    "llm_provider",
    "llm_max_tokens",
    "anthropic_model",
    "bedrock_model",
    "bedrock_aws_region",
    "openai_model",
    "openai_base_url",
    "ollama_model",
    "ollama_base_url",
    "gemini_model",
    "gemini_base_url",
)


@dataclass(frozen=True)
class LLMRuntimeConfig:
    llm_provider: str
    llm_max_tokens: int
    anthropic_model: str
    bedrock_model: str
    bedrock_aws_region: str
    openai_model: str
    openai_base_url: str
    ollama_model: str
    ollama_base_url: str
    gemini_model: str
    gemini_base_url: str

    def active_provider(self) -> LLMProviderConfig | None:
        key = self.llm_provider.strip().lower()
        if key in LLM_DISABLED_KEYS:
            return None
        if key == "anthropic":
            return LLMProviderConfig(
                key=key,
                display_name="Anthropic",
                adapter="anthropic",
                model=self.anthropic_model,
                api_key=settings.anthropic_api_key,
                max_tokens=self.llm_max_tokens,
            )
        if key == "bedrock":
            return LLMProviderConfig(
                key=key,
                display_name="Amazon Bedrock",
                adapter="anthropic",
                backend="bedrock",
                model=self.bedrock_model,
                aws_region=self.bedrock_aws_region,
                max_tokens=self.llm_max_tokens,
            )
        if key == "openai":
            return LLMProviderConfig(
                key=key,
                display_name="OpenAI",
                adapter="openai",
                model=self.openai_model,
                api_key=settings.openai_api_key,
                base_url=self.openai_base_url,
                max_tokens=self.llm_max_tokens,
            )
        if key == "ollama":
            return LLMProviderConfig(
                key=key,
                display_name="Ollama",
                adapter="openai",
                model=self.ollama_model,
                base_url=self.ollama_base_url or OLLAMA_DEFAULT_BASE_URL,
                max_tokens=self.llm_max_tokens,
            )
        if key == "gemini":
            return LLMProviderConfig(
                key=key,
                display_name="Google Gemini",
                adapter="openai",
                model=self.gemini_model,
                api_key=settings.gemini_api_key,
                base_url=self.gemini_base_url or GEMINI_OPENAI_BASE_URL,
                max_tokens=self.llm_max_tokens,
            )
        return None


def _env_config() -> LLMRuntimeConfig:
    return LLMRuntimeConfig(**{field: getattr(settings, field) for field in EDITABLE_FIELDS})


def _to_config(row: LLMSettings) -> LLMRuntimeConfig:
    return LLMRuntimeConfig(**{field: getattr(row, field) for field in EDITABLE_FIELDS})


_config: LLMRuntimeConfig | None = None


def get_config() -> LLMRuntimeConfig:
    return _config or _env_config()


def set_config(config: LLMRuntimeConfig | None) -> None:
    global _config
    _config = config


def api_key_status() -> dict[str, bool]:
    """Only booleans cross the API boundary; raw keys remain environment-only."""
    return {
        "anthropic": bool(settings.anthropic_api_key),
        "openai": bool(settings.openai_api_key),
        "gemini": bool(settings.gemini_api_key),
    }


def validate_selection(config: LLMRuntimeConfig) -> None:
    key = config.llm_provider.strip().lower()
    if key not in (*LLM_DISABLED_KEYS, *LLM_PROVIDER_KEYS):
        raise ValueError("Select a supported LLM provider or disable LLM features")
    provider = config.active_provider()
    if provider is None:
        return
    if not provider.model.strip():
        raise ValueError(f"{provider.display_name} model must not be blank")
    if key in api_key_status() and not api_key_status()[key]:
        raise ValueError(f"{key.upper()}_API_KEY is not set in the environment")
    if key == "bedrock" and not provider.aws_region.strip():
        raise ValueError("BEDROCK_AWS_REGION must be set before selecting Bedrock")


async def get_settings(session: AsyncSession) -> LLMSettings:
    row = await session.get(LLMSettings, _SINGLETON_ID)
    if row is None:
        row = LLMSettings(
            id=_SINGLETON_ID,
            **{field: getattr(settings, field) for field in EDITABLE_FIELDS},
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def update_settings(session: AsyncSession, changes: dict[str, Any]) -> LLMSettings:
    row = await get_settings(session)
    candidate = _to_config(row)
    candidate = LLMRuntimeConfig(
        **{
            field: (
                changes[field]
                if field in changes and changes[field] is not None
                else getattr(candidate, field)
            )
            for field in EDITABLE_FIELDS
        }
    )
    validate_selection(candidate)
    for field in EDITABLE_FIELDS:
        setattr(row, field, getattr(candidate, field))
    row.updated_at = datetime.now(UTC)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    set_config(_to_config(row))
    from app.services.llm.service import reset_provider_cache

    reset_provider_cache()
    return row


async def refresh_cache(session: AsyncSession) -> None:
    set_config(_to_config(await get_settings(session)))
    from app.services.llm.service import reset_provider_cache

    reset_provider_cache()
