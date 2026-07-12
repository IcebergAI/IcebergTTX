from dataclasses import dataclass, field
from urllib.parse import urlsplit

from pydantic_settings import BaseSettings, SettingsConfigDict

# Well-known insecure default. Production must override SECRET_KEY (see #9).
DEFAULT_SECRET_KEY = "dev-secret-key-change-in-production"  # nosec B105 - sentinel, rejected in prod
MIN_SECRET_KEY_LENGTH = 32

# Auth modes (#25). Governs whether the local email/password store, OIDC SSO, or
# both are available. "oidc" disables the local login/register endpoints + forms.
AUTH_MODE_LOCAL = "local"
AUTH_MODE_OIDC = "oidc"
AUTH_MODE_BOTH = "both"

# OIDC provider keys shipped with adapters (#25). The registry in
# app/services/oidc maps these to adapter implementations.
OIDC_PROVIDER_KEYS = ("entra", "authentik")

# Pluggable AI backend. LLM_PROVIDER selects the single active provider for the
# whole app (the AI analog of AUTH_MODE). "" / "none" disables the LLM pipeline.
# Each key maps to an adapter *family* in app/services/llm: "anthropic" (direct
# Anthropic + Bedrock, same messages.create surface) and "openai" (OpenAI, Ollama,
# and Gemini, all via the OpenAI-compatible Chat Completions surface).
LLM_PROVIDER_KEYS = ("anthropic", "bedrock", "openai", "ollama", "gemini")
LLM_DISABLED_KEYS = ("", "none", "disabled")
OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434/v1"
GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


@dataclass(frozen=True)
class LLMProviderConfig:
    """Resolved config for the active AI provider (built from flat env vars).

    ``adapter`` is the family key registered in app/services/llm ("anthropic" |
    "openai"); ``backend`` disambiguates within the anthropic family ("api" |
    "bedrock"). Secrets (``api_key``) are env-only — never a DB column, never logged.
    """

    key: str
    display_name: str
    adapter: str
    model: str
    api_key: str = ""
    base_url: str = ""
    aws_region: str = ""
    max_tokens: int = 600
    backend: str = "api"


@dataclass(frozen=True)
class OIDCProviderConfig:
    """Resolved config for one enabled OIDC provider (built from flat env vars)."""

    key: str
    display_name: str
    client_id: str
    client_secret: str
    metadata_url: str
    scopes: str = "openid email profile"
    # Optional group/role claim → UserRole allowlist. Empty ⇒ everyone is a
    # participant (no self-elevation, mirrors #8).
    role_claim: str = ""
    role_map: dict[str, str] = field(default_factory=dict)
    # Provider-specific: the expected issuer (Entra pins a single tenant so issuer
    # validation is exact). Empty ⇒ trust the discovery document's issuer.
    issuer: str = ""


class Settings(BaseSettings):
    # extra="ignore": the project-root .env is shared with docker-compose, which
    # needs keys the app doesn't declare (e.g. POSTGRES_PASSWORD). Ignore unknown
    # keys rather than raising a ValidationError at import (which broke local
    # uvicorn/pytest whenever .env carried a compose-only key).
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "postgresql+asyncpg://iceberg_ttx:iceberg_ttx@localhost:5432/iceberg_ttx"
    secret_key: str = DEFAULT_SECRET_KEY
    access_token_expire_minutes: int = 480
    algorithm: str = "HS256"

    # Pluggable AI backend. llm_provider selects the active provider (see
    # LLM_PROVIDER_KEYS); "" / "none" disables LLM assessment + inject suggestion.
    # Model IDs and endpoints are operator-overridable; provider secrets are
    # env-only (never a DB column, never logged), same rule as OIDC/SIEM.
    # Bedrock model IDs MUST carry the "anthropic." prefix; direct Anthropic must
    # not. Ollama needs no key; Gemini/OpenAI route through the OpenAI SDK.
    llm_provider: str = "anthropic"
    llm_max_tokens: int = 600
    anthropic_api_key: str = ""  # SECRET
    anthropic_model: str = "claude-opus-4-8"
    bedrock_model: str = "anthropic.claude-opus-4-8"
    bedrock_aws_region: str = ""
    openai_api_key: str = ""  # SECRET
    openai_model: str = "gpt-5"
    openai_base_url: str = ""  # "" ⇒ SDK default (api.openai.com)
    ollama_model: str = "llama3.1"
    ollama_base_url: str = OLLAMA_DEFAULT_BASE_URL
    gemini_api_key: str = ""  # SECRET
    gemini_model: str = "gemini-2.0-flash"
    gemini_base_url: str = GEMINI_OPENAI_BASE_URL

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

    # Registration controls (#67). registration_enabled gates self-service
    # POST /api/auth/register; when False the register page/link are hidden and
    # the API returns 403. The rate limit is per source IP and applies regardless
    # of the toggle (the window is deliberately longer than login's — it throttles
    # account creation, not password guessing). Admins provision accounts
    # out-of-band via POST /api/users.
    registration_enabled: bool = True
    registration_max_attempts: int = 5
    registration_lockout_seconds: int = 3600

    # Email / SMTP (#117). Feature-flagged: self-service password reset (and, later,
    # participant invites) exist only when SMTP is configured (smtp_enabled). Raw
    # socket — outbound mail goes DIRECT, not through the httpx proxy layer (#97),
    # like the syslog sink. smtp_password is a SECRET (env-only, never a DB column,
    # never logged). public_base_url builds absolute reset/invite links; when blank
    # they are derived from the request (mirrors oidc_redirect_base_url).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""  # SECRET: env-only, never persisted or logged
    smtp_from: str = ""  # From: address; required when smtp_host is set
    smtp_starttls: bool = True  # STARTTLS on 587; set False + smtp_tls for implicit TLS (465)
    smtp_tls: bool = False  # implicit TLS on connect
    public_base_url: str = ""
    # Password-reset request throttle (#117), per source IP. Longer window than login
    # (it throttles email sends, not password guessing), mirroring registration.
    password_reset_max_attempts: int = 5
    password_reset_lockout_seconds: int = 3600

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

    # Outbound proxy (#97). These *seed* the admin-editable ProxySettings row on
    # first read; routing is changed at runtime via /admin/proxy thereafter.
    # Modes: system (honour HTTP(S)_PROXY/NO_PROXY env — the default, and what
    # httpx already did implicitly) | none (always direct) | explicit (use
    # proxy_url, bypassing the no-proxy list). Applies to LLM, SIEM-HTTP and OIDC
    # egress; the raw-socket syslog sink cannot be proxied.
    # The default no-proxy list covers loopback/private ranges, so Ollama's
    # default base URL (http://localhost:11434/v1) never traverses the proxy.
    # proxy_username/proxy_password are SECRETS — env-only, never a DB column,
    # never logged; injected into the proxy URL at call time.
    proxy_mode: str = "system"  # system | none | explicit
    proxy_url: str = ""
    proxy_no_proxy: str = (
        "localhost,127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16,::1"
    )
    proxy_username: str = ""  # SECRET: env-only, never persisted or logged
    proxy_password: str = ""  # SECRET: env-only, never persisted or logged

    # OIDC / SSO (#25). auth_mode selects local | oidc | both. Each provider is
    # enabled independently and, if enabled, contributes a login button. Client
    # secrets are SECRETS — env-only, never a DB column, never logged. Multiple
    # providers can run concurrently. oidc_redirect_base_url overrides the
    # request-derived callback base (set it when behind a proxy that rewrites the
    # host/scheme so the redirect_uri matches what the IdP has registered).
    auth_mode: str = AUTH_MODE_BOTH
    oidc_redirect_base_url: str = ""

    # Microsoft Entra ID. oidc_entra_tenant_id MUST be a specific tenant (a GUID or
    # verified domain) — never "common"/"organizations" — so ID-token issuer
    # validation is exact.
    oidc_entra_enabled: bool = False
    oidc_entra_client_id: str = ""
    oidc_entra_client_secret: str = ""  # SECRET
    oidc_entra_tenant_id: str = ""
    oidc_entra_scopes: str = "openid email profile"
    oidc_entra_role_claim: str = ""
    oidc_entra_role_map: str = ""  # "group=role,group2=role2"

    # Authentik. Discovery URL is built from base_url + app_slug.
    oidc_authentik_enabled: bool = False
    oidc_authentik_client_id: str = ""
    oidc_authentik_client_secret: str = ""  # SECRET
    oidc_authentik_base_url: str = ""  # e.g. https://auth.example.com
    oidc_authentik_app_slug: str = ""
    oidc_authentik_scopes: str = "openid email profile"
    oidc_authentik_role_claim: str = "groups"
    oidc_authentik_role_map: str = ""  # "group=role,group2=role2"

    # Auth0. Discovery is https://<domain>/.well-known/openid-configuration. Roles
    # are not sent by default — expose them via an Action as a namespaced custom
    # claim and set oidc_auth0_role_claim to it (e.g. https://<app>/roles).
    oidc_auth0_enabled: bool = False
    oidc_auth0_client_id: str = ""
    oidc_auth0_client_secret: str = ""  # SECRET
    oidc_auth0_domain: str = ""  # e.g. your-tenant.us.auth0.com
    oidc_auth0_scopes: str = "openid email profile"
    oidc_auth0_role_claim: str = ""
    oidc_auth0_role_map: str = ""  # "group=role,group2=role2"

    # Okta. Discovery uses the org server (https://<domain>/.well-known/...) or a
    # custom authorization server when oidc_okta_auth_server is set (commonly
    # "default"): https://<domain>/oauth2/<server>/.well-known/openid-configuration.
    oidc_okta_enabled: bool = False
    oidc_okta_client_id: str = ""
    oidc_okta_client_secret: str = ""  # SECRET
    oidc_okta_domain: str = ""  # e.g. dev-12345.okta.com
    oidc_okta_auth_server: str = ""  # "" = org server; else e.g. "default"
    oidc_okta_scopes: str = "openid email profile"
    oidc_okta_role_claim: str = "groups"
    oidc_okta_role_map: str = ""  # "group=role,group2=role2"

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

    @property
    def smtp_enabled(self) -> bool:
        """True once SMTP is configured — gates all email-dependent features (#117)."""
        return bool(self.smtp_host and self.smtp_from)

    @property
    def local_auth_enabled(self) -> bool:
        return self.auth_mode in (AUTH_MODE_LOCAL, AUTH_MODE_BOTH)

    @property
    def oidc_auth_enabled(self) -> bool:
        return self.auth_mode in (AUTH_MODE_OIDC, AUTH_MODE_BOTH)

    def active_llm_provider(self) -> LLMProviderConfig | None:
        """Build the config for the active AI provider, or None when disabled.

        The AI analog of enabled_oidc_providers(): flat env vars → a resolved
        LLMProviderConfig for the single provider named by LLM_PROVIDER. An unknown
        key returns None here (validate_settings() fails fast on it at startup).
        """
        key = self.llm_provider.strip().lower()
        if key in LLM_DISABLED_KEYS:
            return None
        if key == "anthropic":
            return LLMProviderConfig(
                key="anthropic",
                display_name="Anthropic",
                adapter="anthropic",
                model=self.anthropic_model,
                api_key=self.anthropic_api_key,
                max_tokens=self.llm_max_tokens,
            )
        if key == "bedrock":
            return LLMProviderConfig(
                key="bedrock",
                display_name="Amazon Bedrock",
                adapter="anthropic",
                backend="bedrock",
                model=self.bedrock_model,
                aws_region=self.bedrock_aws_region,
                max_tokens=self.llm_max_tokens,
            )
        if key == "openai":
            return LLMProviderConfig(
                key="openai",
                display_name="OpenAI",
                adapter="openai",
                model=self.openai_model,
                api_key=self.openai_api_key,
                base_url=self.openai_base_url,
                max_tokens=self.llm_max_tokens,
            )
        if key == "ollama":
            return LLMProviderConfig(
                key="ollama",
                display_name="Ollama",
                adapter="openai",
                model=self.ollama_model,
                base_url=self.ollama_base_url or OLLAMA_DEFAULT_BASE_URL,
                max_tokens=self.llm_max_tokens,
            )
        if key == "gemini":
            return LLMProviderConfig(
                key="gemini",
                display_name="Google Gemini",
                adapter="openai",
                model=self.gemini_model,
                api_key=self.gemini_api_key,
                base_url=self.gemini_base_url or GEMINI_OPENAI_BASE_URL,
                max_tokens=self.llm_max_tokens,
            )
        return None

    def enabled_oidc_providers(self) -> list[OIDCProviderConfig]:
        """Build the config for every enabled OIDC provider (#25).

        Only providers whose ``*_enabled`` flag is set and whose required inputs
        resolve to a metadata URL are returned; providers with blank inputs are
        skipped (validate_settings() fails fast on that in production).
        """
        providers: list[OIDCProviderConfig] = []
        if self.oidc_auth_enabled and self.oidc_entra_enabled and self.oidc_entra_tenant_id:
            authority = f"https://login.microsoftonline.com/{self.oidc_entra_tenant_id}/v2.0"
            providers.append(
                OIDCProviderConfig(
                    key="entra",
                    display_name="Microsoft Entra ID",
                    client_id=self.oidc_entra_client_id,
                    client_secret=self.oidc_entra_client_secret,
                    metadata_url=f"{authority}/.well-known/openid-configuration",
                    issuer=authority,
                    scopes=self.oidc_entra_scopes,
                    role_claim=self.oidc_entra_role_claim,
                    role_map=_parse_role_map(self.oidc_entra_role_map),
                )
            )
        if (
            self.oidc_auth_enabled
            and self.oidc_authentik_enabled
            and self.oidc_authentik_base_url
            and self.oidc_authentik_app_slug
        ):
            base = self.oidc_authentik_base_url.rstrip("/")
            slug = self.oidc_authentik_app_slug
            providers.append(
                OIDCProviderConfig(
                    key="authentik",
                    display_name="Authentik",
                    client_id=self.oidc_authentik_client_id,
                    client_secret=self.oidc_authentik_client_secret,
                    metadata_url=f"{base}/application/o/{slug}/.well-known/openid-configuration",
                    scopes=self.oidc_authentik_scopes,
                    role_claim=self.oidc_authentik_role_claim,
                    role_map=_parse_role_map(self.oidc_authentik_role_map),
                )
            )
        if self.oidc_auth_enabled and self.oidc_auth0_enabled and self.oidc_auth0_domain:
            domain = self.oidc_auth0_domain.rstrip("/")
            providers.append(
                OIDCProviderConfig(
                    key="auth0",
                    display_name="Auth0",
                    client_id=self.oidc_auth0_client_id,
                    client_secret=self.oidc_auth0_client_secret,
                    metadata_url=f"https://{domain}/.well-known/openid-configuration",
                    scopes=self.oidc_auth0_scopes,
                    role_claim=self.oidc_auth0_role_claim,
                    role_map=_parse_role_map(self.oidc_auth0_role_map),
                )
            )
        if self.oidc_auth_enabled and self.oidc_okta_enabled and self.oidc_okta_domain:
            domain = self.oidc_okta_domain.rstrip("/")
            server = self.oidc_okta_auth_server.strip("/")
            path = f"/oauth2/{server}" if server else ""
            providers.append(
                OIDCProviderConfig(
                    key="okta",
                    display_name="Okta",
                    client_id=self.oidc_okta_client_id,
                    client_secret=self.oidc_okta_client_secret,
                    metadata_url=f"https://{domain}{path}/.well-known/openid-configuration",
                    scopes=self.oidc_okta_scopes,
                    role_claim=self.oidc_okta_role_claim,
                    role_map=_parse_role_map(self.oidc_okta_role_map),
                )
            )
        return providers


def _parse_role_map(raw: str) -> dict[str, str]:
    """Parse a "group=role,group2=role2" allowlist into a dict (#25).

    Only maps to real UserRole values; unknown roles are dropped. Kept import-free
    of the model to avoid a cycle — validated against UserRole where it is applied.
    """
    result: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        group, role = (p.strip() for p in pair.split("=", 1))
        if group and role:
            result[group] = role
    return result


PROXY_MODES = ("none", "system", "explicit")
PROXY_URL_SCHEMES = ("http", "https", "socks5", "socks5h")


def _validate_proxy_settings(s: Settings) -> None:
    """Validate outbound-proxy config (#97). Runs in dev too — a malformed proxy
    fails the same way everywhere. Never logs the URL (it may carry credentials in
    a hand-rolled env var)."""
    mode = s.proxy_mode.strip().lower()
    if mode not in PROXY_MODES:
        raise RuntimeError(
            f"PROXY_MODE must be one of {'|'.join(PROXY_MODES)}, got {s.proxy_mode!r}."
        )
    if mode == "explicit" and not s.proxy_url.strip():
        raise RuntimeError("PROXY_MODE=explicit requires PROXY_URL to be set.")
    if s.proxy_url.strip():
        parsed = urlsplit(s.proxy_url.strip())
        if parsed.scheme not in PROXY_URL_SCHEMES or not parsed.hostname:
            raise RuntimeError(
                "PROXY_URL must be an absolute URL with scheme "
                f"{'|'.join(PROXY_URL_SCHEMES)} and a host (e.g. http://proxy.corp:3128)."
            )


def validate_settings(s: Settings | None = None) -> None:
    """Fail fast on insecure production configuration. Called at startup."""
    s = s or settings
    if s.auth_mode not in (AUTH_MODE_LOCAL, AUTH_MODE_OIDC, AUTH_MODE_BOTH):
        raise RuntimeError(
            f"AUTH_MODE must be one of local|oidc|both, got {s.auth_mode!r}."
        )
    llm_key = s.llm_provider.strip().lower()
    if llm_key not in (*LLM_DISABLED_KEYS, *LLM_PROVIDER_KEYS):
        raise RuntimeError(
            "LLM_PROVIDER must be one of "
            f"{'|'.join(LLM_PROVIDER_KEYS)} (or empty to disable), got "
            f"{s.llm_provider!r}."
        )
    _validate_proxy_settings(s)
    # SMTP (#117): if a host is set, a From: address is mandatory (the feature is gated
    # on both — see smtp_enabled). Checked in dev too so misconfig surfaces early.
    if s.smtp_host and not s.smtp_from:
        raise RuntimeError("SMTP_HOST is set but SMTP_FROM is empty.")
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
    # OIDC providers that are switched on must carry credentials + discovery inputs.
    if s.oidc_auth_enabled:
        if s.oidc_entra_enabled and not (
            s.oidc_entra_client_id and s.oidc_entra_client_secret and s.oidc_entra_tenant_id
        ):
            raise RuntimeError(
                "OIDC_ENTRA_ENABLED is set but OIDC_ENTRA_CLIENT_ID / "
                "OIDC_ENTRA_CLIENT_SECRET / OIDC_ENTRA_TENANT_ID are incomplete."
            )
        if s.oidc_authentik_enabled and not (
            s.oidc_authentik_client_id
            and s.oidc_authentik_client_secret
            and s.oidc_authentik_base_url
            and s.oidc_authentik_app_slug
        ):
            raise RuntimeError(
                "OIDC_AUTHENTIK_ENABLED is set but OIDC_AUTHENTIK_CLIENT_ID / "
                "OIDC_AUTHENTIK_CLIENT_SECRET / OIDC_AUTHENTIK_BASE_URL / "
                "OIDC_AUTHENTIK_APP_SLUG are incomplete."
            )
        if s.oidc_auth0_enabled and not (
            s.oidc_auth0_client_id and s.oidc_auth0_client_secret and s.oidc_auth0_domain
        ):
            raise RuntimeError(
                "OIDC_AUTH0_ENABLED is set but OIDC_AUTH0_CLIENT_ID / "
                "OIDC_AUTH0_CLIENT_SECRET / OIDC_AUTH0_DOMAIN are incomplete."
            )
        if s.oidc_okta_enabled and not (
            s.oidc_okta_client_id and s.oidc_okta_client_secret and s.oidc_okta_domain
        ):
            raise RuntimeError(
                "OIDC_OKTA_ENABLED is set but OIDC_OKTA_CLIENT_ID / "
                "OIDC_OKTA_CLIENT_SECRET / OIDC_OKTA_DOMAIN are incomplete."
            )
    if s.auth_mode == AUTH_MODE_OIDC and not s.enabled_oidc_providers():
        raise RuntimeError(
            "AUTH_MODE=oidc but no OIDC provider is enabled/configured; users would "
            "have no way to sign in."
        )
    # The active AI provider must carry the credentials its backend needs. Set
    # LLM_PROVIDER=none to run without the LLM (assessment/inject suggestion).
    llm = s.active_llm_provider()
    if llm is not None:
        if llm.key in ("anthropic", "openai", "gemini") and not llm.api_key:
            raise RuntimeError(
                f"LLM_PROVIDER={llm.key} is set but its API key "
                f"({llm.key.upper()}_API_KEY) is empty. Set it, or LLM_PROVIDER=none."
            )
        if llm.key == "bedrock" and not llm.aws_region:
            raise RuntimeError(
                "LLM_PROVIDER=bedrock is set but BEDROCK_AWS_REGION is empty."
            )


settings = Settings()
