# pyright: reportArgumentType=false
from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlmodel import Field, SQLModel


class LLMSettings(SQLModel, table=True):
    """Admin-editable LLM routing and model choices; never provider secrets."""

    id: int | None = Field(default=None, primary_key=True)
    llm_provider: str = "none"
    llm_max_tokens: int = 600
    anthropic_model: str = "claude-opus-4-8"
    bedrock_model: str = "anthropic.claude-opus-4-8"
    bedrock_aws_region: str = ""
    openai_model: str = "gpt-5"
    openai_base_url: str = ""
    ollama_model: str = "llama3.1"
    ollama_base_url: str = "http://localhost:11434/v1"
    gemini_model: str = "gemini-2.0-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )
