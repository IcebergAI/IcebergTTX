"""Central logging configuration (#17).

Initialised once from the app lifespan. Without this, Python's logging defaults
to the WARNING-level "handler of last resort", so INFO-level application logs and
``logger.exception`` calls from background tasks (LLM pipeline, delayed comms) may
never surface. This installs a single root handler at the configured level and
gives the ``deep_thought.audit`` stream its own handler so audit events stay pure
JSON lines (OWASP Logging Cheat Sheet) instead of being wrapped by the app
formatter.
"""

import json
import logging
from datetime import UTC, datetime

from app.config import settings

_configured = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    """Install root + audit handlers. Idempotent (safe under uvicorn --reload)."""
    global _configured
    if _configured:
        return

    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    app_handler = logging.StreamHandler()
    if settings.log_json:
        app_handler.setFormatter(_JsonFormatter())
    else:
        app_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    root.addHandler(app_handler)

    # The audit logger emits pre-formatted JSON lines; keep them unwrapped and
    # off the root handler so the audit stream is cleanly machine-parseable.
    audit_logger = logging.getLogger("deep_thought.audit")
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False
    for handler in list(audit_logger.handlers):
        audit_logger.removeHandler(handler)
    audit_handler = logging.StreamHandler()
    audit_handler.setFormatter(logging.Formatter("%(message)s"))
    audit_logger.addHandler(audit_handler)

    _configured = True
