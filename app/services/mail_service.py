"""Outbound email via SMTP (#117).

Feature-flagged: `send` is a no-op unless SMTP is configured (`settings.smtp_enabled`).
SMTP is a raw socket — this goes **direct** and does NOT use the httpx outbound-proxy
layer (#97), exactly like the raw-socket syslog sink. Failures are logged, never
raised: mail is best-effort and must not break the request path (a reset-request that
awaited delivery would also leak account existence via timing). Callers therefore fire
it off the response path with `background.spawn`.

Bodies are plain text (per #117). Build the message text in the caller.
"""

import logging
from email.message import EmailMessage

import aiosmtplib
from fastapi import Request

from app.config import settings

logger = logging.getLogger(__name__)


def build_link(request: Request, path: str, token: str) -> str:
    """Absolute link for an email (#117). Honours PUBLIC_BASE_URL (proxies that
    rewrite host/scheme) else derives from the request (mirrors oidc._callback_url)."""
    base = settings.public_base_url.rstrip("/") if settings.public_base_url else str(
        request.base_url
    ).rstrip("/")
    return f"{base}{path}?token={token}"


async def send(to: str, subject: str, body: str) -> None:
    """Send a plain-text email. No-op when SMTP is unconfigured; never raises."""
    if not settings.smtp_enabled:
        return
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    try:
        await aiosmtplib.send(
            message,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username or None,
            password=settings.smtp_password or None,
            start_tls=settings.smtp_starttls,
            use_tls=settings.smtp_tls,
        )
    except Exception:
        # Never surface a mailer failure to the caller — best-effort, logged only.
        logger.exception("Failed to send email to %s (subject=%r)", to, subject)
