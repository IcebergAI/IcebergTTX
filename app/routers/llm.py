"""Admin runtime LLM configuration API; provider credentials remain env-only."""

import asyncio
import logging
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.dependencies import require_admin
from app.models.user import User
from app.services import audit_service, llm_settings_service
from app.services.llm.service import active_provider

logger = logging.getLogger("iceberg_ttx")
router = APIRouter(prefix="/llm", tags=["llm settings"])
AdminDep = Annotated[User, Depends(require_admin)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


class LLMSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm_provider: str | None = None
    llm_max_tokens: int | None = Field(default=None, ge=1, le=32768)
    anthropic_model: str | None = None
    bedrock_model: str | None = None
    bedrock_aws_region: str | None = None
    openai_model: str | None = None
    openai_base_url: str | None = None
    ollama_model: str | None = None
    ollama_base_url: str | None = None
    gemini_model: str | None = None
    gemini_base_url: str | None = None

    @field_validator("*")
    @classmethod
    def _strip_strings(cls, value):  # noqa: ANN001
        return value.strip() if isinstance(value, str) else value

    @field_validator("llm_provider")
    @classmethod
    def _normalize_provider(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None

    @field_validator("openai_base_url", "ollama_base_url", "gemini_base_url")
    @classmethod
    def _absolute_url(cls, value: str | None) -> str | None:
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Provider base URL must be an absolute http(s) URL")
        return value


async def _public_settings(session: AsyncSession) -> dict:
    row = await llm_settings_service.get_settings(session)
    return {
        **{field: getattr(row, field) for field in llm_settings_service.EDITABLE_FIELDS},
        "api_keys_set": llm_settings_service.api_key_status(),
        "updated_at": row.updated_at.isoformat(),
    }


@router.get("/settings")
async def get_llm_settings(_: AdminDep, session: SessionDep) -> dict:
    return await _public_settings(session)


@router.put("/settings")
async def update_llm_settings(
    body: LLMSettingsUpdate, current_user: AdminDep, session: SessionDep
) -> dict:
    changes = body.model_dump(exclude_unset=True)
    try:
        row = await llm_settings_service.update_settings(session, changes)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    audit_service.emit(
        "llm.settings_updated",
        actor=current_user,
        target_type="llm_settings",
        target_id=row.id,
        reason="fields=" + ",".join(sorted(changes)),
        severity="warning",
    )
    return await _public_settings(session)


@router.post("/test")
async def test_llm_provider(current_user: AdminDep) -> dict[str, str]:
    """Exercise the server-selected provider without accepting a URL or prompt."""
    provider = active_provider()
    if provider is None:
        result = "disabled"
    else:
        try:
            async with asyncio.timeout(10):
                await provider.complete(
                    "This is a connectivity check.", "", "Reply with OK.", max_tokens=1
                )
            result = "ok"
        except Exception as exc:  # noqa: BLE001 - safe class-only boundary
            logger.warning("LLM provider test failed: %s", type(exc).__name__)
            result = f"error: {type(exc).__name__}"
    audit_service.emit(
        "llm.test",
        actor=current_user,
        target_type="llm_settings",
        reason=result,
        severity="warning" if result.startswith("error") else "info",
    )
    return {"result": result}
