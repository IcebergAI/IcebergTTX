"""Build a redacted, provenance-aware snapshot of the live application config."""

from collections.abc import Callable, Mapping
from enum import Enum
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import Settings, settings, validate_settings
from app.models.audit_settings import AuditSettings
from app.models.email_settings import EmailSettings
from app.models.general_settings import GeneralSettings
from app.models.llm_settings import LLMSettings
from app.models.oidc_settings import OIDCSettings
from app.models.proxy_settings import ProxySettings
from app.services import (
    general_settings_service,
    llm_settings_service,
    mail_service,
    oidc_settings_service,
    proxy,
    siem_service,
)

SECRET_FIELDS = {
    "database_url",
    "secret_key",
    "anthropic_api_key",
    "openai_api_key",
    "gemini_api_key",
    "smtp_password",
    "siem_http_token",
    "proxy_username",
    "proxy_password",
    "oidc_entra_client_secret",
    "oidc_authentik_client_secret",
    "oidc_auth0_client_secret",
    "oidc_okta_client_secret",
}


def _category(name: str) -> str:
    if name.startswith(("llm_", "anthropic_", "bedrock_", "openai_", "ollama_", "gemini_")):
        return "AI provider"
    if name.startswith("oidc_") or name == "auth_mode":
        return "Single sign-on"
    if name.startswith("smtp_") or name in {
        "public_base_url",
        "password_reset_max_attempts",
        "password_reset_lockout_seconds",
    }:
        return "Email"
    if name.startswith("siem_") or name.startswith("audit_"):
        return "Audit and SIEM"
    if name.startswith("proxy_"):
        return "Outbound proxy"
    if name in {
        "access_token_expire_minutes",
        "registration_enabled",
        "registration_max_attempts",
        "registration_lockout_seconds",
        "login_max_attempts",
        "login_lockout_seconds",
    }:
        return "General runtime"
    return "Process and security"


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset, tuple)):
        return sorted(value)
    if isinstance(value, Mapping):
        return dict(value)
    return value


def _source_for_env_field(name: str) -> str:
    return "environment" if name in settings.model_fields_set else "built-in default"


async def _database_overrides(session: AsyncSession) -> tuple[dict[str, Any], set[str]]:
    values: dict[str, Any] = {}
    database_fields: set[str] = set()

    general = await session.get(GeneralSettings, 1)
    if general is not None:
        for field in general_settings_service.EDITABLE_FIELDS:
            values[field] = getattr(general, field)
            database_fields.add(field)

    llm = await session.get(LLMSettings, 1)
    if llm is not None:
        for field in llm_settings_service.EDITABLE_FIELDS:
            values[field] = getattr(llm, field)
            database_fields.add(field)

    oidc = await session.get(OIDCSettings, 1)
    if oidc is not None:
        for field in oidc_settings_service.EDITABLE_FIELDS:
            values[field] = getattr(oidc, field)
            database_fields.add(field)

    email = await session.get(EmailSettings, 1)
    if email is not None:
        for field in (
            "smtp_host",
            "smtp_port",
            "smtp_from",
            "smtp_username",
            "smtp_starttls",
            "smtp_tls",
            "public_base_url",
        ):
            values[field] = getattr(email, field)
            database_fields.add(field)
        values["smtp_enabled"] = email.enabled and bool(email.smtp_host and email.smtp_from)
        database_fields.add("smtp_enabled")

    audit = await session.get(AuditSettings, 1)
    if audit is not None:
        audit_map = {
            "siem_enabled": "enabled",
            "siem_methods": "methods",
            "siem_min_severity": "min_severity",
            "siem_file_path": "file_path",
            "siem_syslog_host": "syslog_host",
            "siem_syslog_port": "syslog_port",
            "siem_syslog_protocol": "syslog_protocol",
            "siem_syslog_facility": "syslog_facility",
            "siem_http_endpoint": "http_endpoint",
            "siem_http_verify_tls": "http_verify_tls",
        }
        for field, model_field in audit_map.items():
            values[field] = getattr(audit, model_field)
            database_fields.add(field)

    proxy_row = await session.get(ProxySettings, 1)
    if proxy_row is not None:
        values.update(
            proxy_mode=proxy_row.mode.lower(),
            proxy_url=proxy_row.proxy_url,
            proxy_no_proxy=proxy_row.no_proxy,
        )
        database_fields.update({"proxy_mode", "proxy_url", "proxy_no_proxy"})

    return values, database_fields


def _live_fallbacks() -> dict[str, Any]:
    values = settings.model_dump()
    general = general_settings_service.get_config()
    values.update(
        {field: getattr(general, field) for field in general_settings_service.EDITABLE_FIELDS}
    )
    llm = llm_settings_service.get_config()
    values.update({field: getattr(llm, field) for field in llm_settings_service.EDITABLE_FIELDS})
    oidc = oidc_settings_service.get_config()
    values.update({field: getattr(oidc, field) for field in oidc_settings_service.EDITABLE_FIELDS})
    mail = mail_service.get_config()
    values.update(
        smtp_host=mail.smtp_host,
        smtp_port=mail.smtp_port,
        smtp_from=mail.smtp_from,
        smtp_username=mail.smtp_username,
        smtp_starttls=mail.smtp_starttls,
        smtp_tls=mail.smtp_tls,
        public_base_url=mail.public_base_url,
        smtp_enabled=mail.smtp_enabled,
    )
    siem = siem_service.get_config()
    values.update(
        siem_enabled=siem.enabled,
        siem_methods=siem.methods,
        siem_min_severity=siem.min_severity,
        siem_file_path=siem.file_path,
        siem_syslog_host=siem.syslog_host,
        siem_syslog_port=siem.syslog_port,
        siem_syslog_protocol=siem.syslog_protocol,
        siem_syslog_facility=siem.syslog_facility,
        siem_http_endpoint=siem.http_endpoint,
        siem_http_verify_tls=siem.http_verify_tls,
    )
    proxy_config = proxy.get_config()
    if proxy_config is None:
        values.update(
            proxy_mode=settings.proxy_mode,
            proxy_url=settings.proxy_url,
            proxy_no_proxy=settings.proxy_no_proxy,
        )
    else:
        values.update(
            proxy_mode=str(proxy_config.mode).lower(),
            proxy_url=proxy_config.proxy_url,
            proxy_no_proxy=proxy_config.no_proxy,
        )
    return values


def _validation() -> dict[str, Any]:
    errors: list[str] = []
    checks: tuple[Callable[[], None], ...] = (
        lambda: validate_settings(settings),
        lambda: llm_settings_service.validate_selection(
            llm_settings_service.get_config()
        ),
        lambda: oidc_settings_service.validate_config(
            oidc_settings_service.get_config()
        ),
    )
    for check in checks:
        try:
            check()
        except (RuntimeError, ValueError) as exc:
            errors.append(str(exc))
    return {"ok": not errors, "errors": errors}


async def snapshot(session: AsyncSession) -> dict[str, Any]:
    values = _live_fallbacks()
    overrides, database_fields = await _database_overrides(session)
    values.update(overrides)

    names = list(Settings.model_fields)
    if "smtp_enabled" not in names:
        names.append("smtp_enabled")
    rows = []
    for name in names:
        secret = name in SECRET_FIELDS
        value = values.get(name)
        rows.append(
            {
                "name": name,
                "category": _category(name),
                "secret": secret,
                "value": bool(value) if secret else _json_value(value),
                "provenance": (
                    "database" if name in database_fields else _source_for_env_field(name)
                ),
            }
        )

    oidc = oidc_settings_service.get_config()
    llm = llm_settings_service.get_config().active_provider()
    return {
        "settings": rows,
        "validation": _validation(),
        "features": {
            "smtp_enabled": bool(values.get("smtp_enabled")),
            "local_auth_enabled": oidc.local_auth_enabled,
            "registration_enabled": bool(values.get("registration_enabled")),
            "active_llm_provider": llm.key if llm is not None else "none",
            "oidc_providers": [provider.key for provider in oidc.enabled_providers()],
        },
    }
