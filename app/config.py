from pydantic_settings import BaseSettings, SettingsConfigDict

# Well-known insecure default. Production must override SECRET_KEY (see #9).
DEFAULT_SECRET_KEY = "dev-secret-key-change-in-production"  # nosec B105 - sentinel, rejected in prod
MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str = "postgresql+asyncpg://deep_thought:deep_thought@localhost:5432/deep_thought"
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
    # AuditEvent table (they are always emitted to the `deep_thought.audit`
    # logger regardless).
    audit_persist: bool = True

    # Application logging (#17). log_level sets the root level; log_json emits
    # structured JSON lines for application logs (the audit stream is always
    # JSON regardless). Configured once at startup by configure_logging().
    log_level: str = "INFO"
    log_json: bool = False

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
