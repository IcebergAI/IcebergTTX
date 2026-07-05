"""Structured audit logging of security-relevant actions (#23).

Events are always emitted as JSON lines to the ``iceberg_ttx.audit`` logger so
they survive even if the database write fails (per the OWASP Logging Cheat
Sheet). When ``settings.audit_persist`` is enabled they are also written to the
append-only ``AuditEvent`` table.

Never pass secrets, password hashes, tokens, or full response/communication
bodies to ``emit`` — only identifiers and metadata. All free-text values are
sanitised against log injection (CR/LF/control chars stripped).
"""

import asyncio
import json
import logging
import re
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from app.config import settings
from app.services.background import spawn

audit_logger = logging.getLogger("iceberg_ttx.audit")

APP_NAME = "iceberg-ttx"
APP_VERSION = "0.1.0"

# Per-request "where" metadata, populated by AuditContextMiddleware.
_request_ctx: ContextVar[dict[str, Any]] = ContextVar("audit_request_ctx", default={})

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_FIELD_LEN = 512


def set_request_context(**fields: Any):
    return _request_ctx.set(fields)


def reset_request_context(token) -> None:
    _request_ctx.reset(token)


def get_request_context() -> dict[str, Any]:
    return _request_ctx.get()


def _sanitize(value: Any) -> Any:
    if value is None:
        return None
    text = _CONTROL_RE.sub(" ", str(value))
    return text[:_MAX_FIELD_LEN]


def _role_str(role: Any) -> str | None:
    if role is None:
        return None
    return getattr(role, "value", str(role))


def emit(
    action: str,
    *,
    result: str = "success",
    actor: Any = None,
    actor_id: int | None = None,
    actor_email: str | None = None,
    actor_role: str | None = None,
    target_type: str | None = None,
    target_id: Any = None,
    reason: str | None = None,
    severity: str = "info",
    security_relevant: bool = True,
) -> None:
    """Emit one audit event. Never raises — logging must not break the request."""
    if actor is not None:
        # Attribute to the *actual* identity, never the previewed role (#23).
        if actor_id is None:
            actor_id = getattr(actor, "id", None)
        if actor_email is None:
            actor_email = getattr(actor, "email", None)
        if actor_role is None:
            actor_role = _role_str(
                getattr(actor, "actual_role", None) or getattr(actor, "role", None)
            )

    ctx = get_request_context()
    event = {
        "ts": datetime.now(UTC).isoformat(),
        "app": APP_NAME,
        "version": APP_VERSION,
        "request_id": ctx.get("request_id"),
        "source_ip": ctx.get("source_ip"),
        "method": ctx.get("method"),
        "path": ctx.get("path"),
        "action": _sanitize(action),
        "result": result,
        "actor_id": actor_id,
        "actor_email": _sanitize(actor_email),
        "actor_role": _sanitize(actor_role),
        "target_type": _sanitize(target_type),
        "target_id": _sanitize(target_id) if target_id is not None else None,
        "reason": _sanitize(reason),
        "severity": severity,
        "security_relevant": security_relevant,
    }

    try:
        audit_logger.info(json.dumps(event, default=str))
    except Exception:  # nosec B110 # pragma: no cover - logging must never break a request
        pass

    if settings.audit_persist:
        _persist(event)

    _ship(event)


def _ship(event: dict[str, Any]) -> None:
    """Schedule best-effort forwarding of the event to the SIEM (#24).

    ``emit`` is synchronous with no DB session, so routing is read from the
    in-memory ``siem_service`` cache and the network sinks run off the response
    path on the loop (like ``_persist``). No running loop (e.g. a sync script) ⇒
    skip — the JSON log line / DB row remain the durable record.
    """
    try:
        from app.services import siem_service

        cfg = siem_service.get_config()
        if not cfg.enabled:
            return
        asyncio.get_running_loop()
    except RuntimeError:
        return  # no running loop
    except Exception:  # nosec B110 # pragma: no cover - forwarding is best-effort
        return

    spawn(siem_service.emit(event, cfg))


def _persist(event: dict[str, Any]) -> None:
    """Schedule a best-effort async write of the audit row.

    ``emit`` is synchronous and called from many sync call sites, so the DB write
    (now async) is fired-and-forgotten on the running event loop. If no loop is
    running (e.g. a sync script), the write is skipped — the JSON log line is the
    durable record per the OWASP guidance.
    """
    try:
        from app.models.audit import AuditEvent

        row = AuditEvent(
            request_id=event.get("request_id"),
            source_ip=event.get("source_ip"),
            method=event.get("method"),
            path=event.get("path"),
            action=event.get("action") or "",
            result=event.get("result") or "success",
            actor_id=event.get("actor_id"),
            actor_email=event.get("actor_email"),
            actor_role=event.get("actor_role"),
            target_type=event.get("target_type"),
            target_id=event.get("target_id"),
            reason=event.get("reason"),
            severity=event.get("severity") or "info",
            security_relevant=bool(event.get("security_relevant", True)),
        )
        asyncio.get_running_loop()
    except RuntimeError:
        return  # no running loop; JSON log line already emitted
    except Exception:  # nosec B110 # pragma: no cover - persistence is best-effort
        return

    spawn(_persist_async(row))


async def _persist_async(row: Any) -> None:
    try:
        from sqlmodel.ext.asyncio.session import AsyncSession

        from app.database import engine

        async with AsyncSession(engine) as session:
            session.add(row)
            await session.commit()
    except Exception:  # nosec B110 # pragma: no cover - persistence is best-effort
        pass
