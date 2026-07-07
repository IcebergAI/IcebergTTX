"""Authentik adapter (#25).

Authentik emits a standard OIDC ID token: ``sub``, ``email`` (+ ``email_verified``),
``name``, and ``groups`` (group names) when the scopes are configured. It's the
self-hostable end-to-end test target for the OIDC flow.
"""

from __future__ import annotations

from typing import Any

from app.services.oidc.base import (
    OIDCIdentity,
    _groups_from,
    _require,
    register_adapter,
)


class AuthentikAdapter:
    key = "authentik"

    def extract_identity(self, claims: dict[str, Any], role_claim: str) -> OIDCIdentity:
        subject = _require(claims, "sub")
        email = _require(claims, "email")
        email_verified = bool(claims.get("email_verified", False))
        display_name = str(claims.get("name") or claims.get("preferred_username") or email)
        return OIDCIdentity(
            subject=subject,
            email=email,
            email_verified=email_verified,
            display_name=display_name,
            groups=_groups_from(claims, role_claim),
        )


register_adapter(AuthentikAdapter())
