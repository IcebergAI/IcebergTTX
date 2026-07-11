# pyright: reportArgumentType=false
# SQLModel's Field stub is narrower than its runtime SQLAlchemy type support.
from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlmodel import Field, SQLModel


class ProxySettings(SQLModel, table=True):
    """Runtime outbound-proxy routing config (#97).

    A single row (``id == 1``) edited live from the admin ``/admin/proxy`` page and
    seeded from ``PROXY_*`` env defaults on first read. Deliberately holds **no
    secret**: the proxy credentials stay env-only (``settings.proxy_username`` /
    ``settings.proxy_password``) and are injected into the proxy URL at call time,
    so they can't leak via the DB, the API, or an audit log line.

    ``mode`` is a plain string column (``NONE`` | ``SYSTEM`` | ``EXPLICIT``) rather
    than a Postgres enum, matching ``AuditSettings.syslog_protocol``.
    """

    id: int | None = Field(default=None, primary_key=True)
    mode: str = "SYSTEM"
    # scheme://host:port — never carries credentials.
    proxy_url: str = ""
    # Comma-separated hosts/domains/CIDRs that bypass the proxy.
    no_proxy: str = ""
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )
