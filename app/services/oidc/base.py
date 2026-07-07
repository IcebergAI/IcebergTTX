"""Provider-adapter contract + registry for OIDC SSO (#25).

An adapter turns a provider's ID-token/userinfo claims into a normalised
``OIDCIdentity``. Everything else about the flow (discovery, PKCE, state/nonce,
signature/issuer/audience validation) is handled generically by Authlib, so an
adapter is small: it knows only which claims a given IdP populates and how that
IdP conveys email verification and group membership.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class OIDCIdentity:
    """Normalised identity extracted from a validated ID token / userinfo."""

    subject: str
    email: str
    email_verified: bool
    display_name: str
    groups: list[str] = field(default_factory=list)


class OIDCAdapter(Protocol):
    """Per-provider claim mapping. Stateless; keyed by provider ``key``."""

    key: str

    def extract_identity(self, claims: dict[str, Any], role_claim: str) -> OIDCIdentity:
        """Map validated token claims to an OIDCIdentity.

        ``role_claim`` is the configured claim carrying the user's groups/roles
        ("" ⇒ no groups extracted). Raises ValueError if the required identity
        claims (sub/email) are missing.
        """
        ...


def _require(claims: dict[str, Any], *names: str) -> str:
    for name in names:
        value = claims.get(name)
        if value:
            return str(value)
    raise ValueError(f"ID token missing required claim(s): {' / '.join(names)}")


def _groups_from(claims: dict[str, Any], role_claim: str) -> list[str]:
    if not role_claim:
        return []
    raw = claims.get(role_claim)
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [str(g) for g in raw]
    return [str(raw)]


# Populated at import time by the adapter modules registering themselves.
_REGISTRY: dict[str, OIDCAdapter] = {}


def register_adapter(adapter: OIDCAdapter) -> None:
    _REGISTRY[adapter.key] = adapter


def get_adapter(key: str) -> OIDCAdapter | None:
    return _REGISTRY.get(key)
