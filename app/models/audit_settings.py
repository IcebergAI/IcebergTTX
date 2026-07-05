from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class AuditSettings(SQLModel, table=True):
    """Runtime SIEM-forwarding routing config (#24).

    A single row (``id == 1``) edited live from the admin ``/admin/audit`` page and
    seeded from ``SIEM_*`` env defaults on first read. Deliberately holds **no
    secret**: the HTTP bearer token stays env-only (``settings.siem_http_token``),
    never a column, so it can't leak via the DB, the API, or an audit log line.
    """

    id: int | None = Field(default=None, primary_key=True)
    enabled: bool = False
    # Subset of {stdout, file, syslog, http}. stdout is the always-on baseline;
    # file/syslog/http are additive forwarders.
    methods: list[str] = Field(default_factory=list, sa_column=Column(JSONB))
    min_severity: str = "info"  # info | warning | critical
    file_path: str = ""
    syslog_host: str = "localhost"
    syslog_port: int = 514
    syslog_protocol: str = "UDP"  # UDP | TCP
    syslog_facility: int = 13  # 13 = "log audit" (RFC 5424)
    http_endpoint: str = ""
    http_verify_tls: bool = True
