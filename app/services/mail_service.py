"""Outbound email via SMTP (#117).

Feature-flagged: `send` is a no-op unless the cached runtime SMTP config is enabled.
SMTP is a raw socket — this goes **direct** and does NOT use the httpx outbound-proxy
layer (#97), exactly like the raw-socket syslog sink. Failures are logged, never
raised: mail is best-effort and must not break the request path (a reset-request that
awaited delivery would also leak account existence via timing). Callers therefore fire
it off the response path with `background.spawn`.

Bodies are plain text (per #117). Build the message text in the caller.
"""

import logging
from dataclasses import dataclass
from email.message import EmailMessage

import aiosmtplib

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MailConfig:
    """Immutable non-secret mail configuration snapshot."""

    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_from: str = ""
    smtp_username: str = ""
    smtp_starttls: bool = True
    smtp_tls: bool = False
    public_base_url: str = ""

    @property
    def smtp_enabled(self) -> bool:
        return bool(self.enabled and self.smtp_host and self.smtp_from)


_config: MailConfig | None = None


def _env_config() -> MailConfig:
    """Pre-feature fallback for transports/tests that do not run app lifespan."""
    return MailConfig(
        enabled=settings.smtp_enabled,
        smtp_host=settings.smtp_host,
        smtp_port=settings.smtp_port,
        smtp_from=settings.smtp_from,
        smtp_username=settings.smtp_username,
        smtp_starttls=settings.smtp_starttls,
        smtp_tls=settings.smtp_tls,
        public_base_url=settings.public_base_url,
    )


def get_config() -> MailConfig:
    return _config if _config is not None else _env_config()


def set_config(cfg: MailConfig | None) -> None:
    global _config
    _config = cfg


def smtp_enabled() -> bool:
    return get_config().smtp_enabled


def link_base() -> str | None:
    """The configured absolute base for emailed links, or None when unset (#258).

    Deliberately **never** derived from the request: `Host` is client-supplied and
    `POST /api/auth/password-reset/request` is unauthenticated, so a forged host made
    the real deployment mail the victim a genuine link pointing at the attacker —
    and the raw single-use token *is* the authorization. Callers must refuse to send
    when this returns None rather than fall back to the request.
    """
    return get_config().public_base_url.strip().rstrip("/") or None


class LinkBaseUnconfigured(RuntimeError):
    """Raised when an emailed link is built with no configured base URL (#258)."""


def build_link(path: str, token: str) -> str:
    """Absolute link for an email (#117), rooted at the operator-configured base."""
    base = link_base()
    if base is None:
        raise LinkBaseUnconfigured("PUBLIC_BASE_URL is not configured")
    return f"{base}{path}?token={token}"


async def _deliver(to: str, subject: str, body: str) -> None:
    """Deliver once and raise on failure; callers decide their disclosure policy."""
    cfg = get_config()
    if not cfg.smtp_enabled:
        raise RuntimeError("SMTP is disabled")
    message = EmailMessage()
    message["From"] = cfg.smtp_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    await aiosmtplib.send(
        message,
        hostname=cfg.smtp_host,
        port=cfg.smtp_port,
        username=cfg.smtp_username or None,
        password=settings.smtp_password or None,
        start_tls=cfg.smtp_starttls,
        use_tls=cfg.smtp_tls,
    )


async def send(to: str, subject: str, body: str) -> None:
    """Send a plain-text email. No-op when SMTP is unconfigured; never raises."""
    if not smtp_enabled():
        return
    try:
        await _deliver(to, subject, body)
    except Exception:
        # Never surface a mailer failure to the caller — best-effort, logged only.
        logger.exception("Failed to send email to %s (subject=%r)", to, subject)


async def send_test(to: str) -> None:
    """Send an admin-requested test, allowing the API to report only the error class."""
    await _deliver(
        to,
        "IcebergTTX email test",
        "This test message confirms that the saved IcebergTTX email settings can deliver mail.",
    )
