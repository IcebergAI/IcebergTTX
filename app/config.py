from pydantic_settings import BaseSettings, SettingsConfigDict

# Well-known insecure default. Production must override SECRET_KEY (see #9).
DEFAULT_SECRET_KEY = "dev-secret-key-change-in-production"  # nosec B105 - sentinel, rejected in prod
MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str = "postgresql+asyncpg://iceberg_ttx:iceberg_ttx@localhost:5432/iceberg_ttx"
    secret_key: str = DEFAULT_SECRET_KEY
    access_token_expire_minutes: int = 480
    algorithm: str = "HS256"
    anthropic_api_key: str = ""

    # Operational mode. dev_mode relaxes production safety checks (the insecure
    # default SECRET_KEY and the Secure cookie flag) for local HTTP development.
    # Production deployments must leave this False.
    dev_mode: bool = False

    # Cookie / CSRF hardening (#10). cookie_secure=None derives from dev_mode
    # (Secure in production, not Secure in dev). trusted_origins is a
    # comma-separated allowlist of extra origins permitted to drive
    # cookie-authenticated state-changing requests (in addition to same-origin).
    cookie_secure: bool | None = None
    trusted_origins: str = ""

    # Login brute-force protection (#11).
    login_max_attempts: int = 5
    login_lockout_seconds: int = 300

    # Audit logging (#23). When True, audit events are also persisted to the
    # AuditEvent table (they are always emitted to the `iceberg_ttx.audit`
    # logger regardless).
    audit_persist: bool = True

    # Application logging (#17). log_level sets the root level; log_json emits
    # structured JSON lines for application logs (the audit stream is always
    # JSON regardless). Configured once at startup by configure_logging().
    log_level: str = "INFO"
    log_json: bool = False

    # SIEM forwarding (#24). These *seed* the admin-editable AuditSettings row on
    # first startup; routing is changed at runtime via /admin/audit thereafter.
    # The app is the forwarder (no sidecar): audit events are shipped off the
    # response path to the enabled methods. stdout is always on (the
    # `iceberg_ttx.audit` handler); file/syslog/http are additive forwarders.
    # siem_http_token is a SECRET — env-only, never a DB column, never logged.
    siem_enabled: bool = False
    siem_methods: str = "stdout"  # comma list of stdout,file,syslog,http
    siem_min_severity: str = "info"  # info | warning | critical
    siem_file_path: str = ""
    siem_syslog_host: str = "localhost"
    siem_syslog_port: int = 514
    siem_syslog_protocol: str = "UDP"  # UDP | TCP
    siem_syslog_facility: int = 13  # 13 = "log audit" (RFC 5424)
    siem_http_endpoint: str = ""
    siem_http_verify_tls: bool = True
    siem_http_token: str = ""  # SECRET: env-only, never persisted or logged

    @property
    def secret_key_is_insecure(self) -> bool:
        return (
            self.secret_key == DEFAULT_SECRET_KEY
            or len(self.secret_key) < MIN_SECRET_KEY_LENGTH
        )

    @property
    def cookies_secure(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return not self.dev_mode

    @property
    def trusted_origin_set(self) -> set[str]:
        return {o.strip() for o in self.trusted_origins.split(",") if o.strip()}

    @property
    def siem_default_methods(self) -> list[str]:
        """Normalised list of forwarding methods seeded from SIEM_METHODS."""
        allowed = {"stdout", "file", "syslog", "http"}
        parts = (p.strip().lower() for p in self.siem_methods.split(","))
        return [m for m in parts if m in allowed]


def validate_settings(s: Settings | None = None) -> None:
    """Fail fast on insecure production configuration. Called at startup."""
    s = s or settings
    if s.dev_mode:
        return
    if s.secret_key_is_insecure:
        raise RuntimeError(
            "SECRET_KEY is unset, using the insecure default, or shorter than "
            f"{MIN_SECRET_KEY_LENGTH} characters. Generate one with "
            "`python -c \"import secrets; print(secrets.token_hex(32))\"` and set it "
            "via the SECRET_KEY environment variable, or set DEV_MODE=true for "
            "local development."
        )


settings = Settings()
