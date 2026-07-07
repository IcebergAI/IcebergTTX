"""Microsoft Entra ID adapter (#25).

Entra work/school ID tokens don't reliably carry an ``email_verified`` claim, but
the email is org-controlled (the tenant owns the domain), so we treat it as
verified when Entra asserts it via the ``xms_edov`` (email-domain-owner-verified)
claim, and otherwise for tokens issued by the pinned single tenant. Email comes
from ``email`` or falls back to ``preferred_username``; groups/app-roles come from
the configured role claim (typically ``roles``).
"""

from __future__ import annotations

from typing import Any

from app.services.oidc.base import (
    OIDCIdentity,
    _groups_from,
    _require,
    register_adapter,
)


class EntraAdapter:
    key = "entra"

    def extract_identity(self, claims: dict[str, Any], role_claim: str) -> OIDCIdentity:
        subject = _require(claims, "sub")
        email = _require(claims, "email", "preferred_username")
        # Entra emails are org-controlled. Trust them as verified unless the token
        # explicitly says otherwise; honour xms_edov when present.
        edov = claims.get("xms_edov")
        if edov is not None:
            email_verified = bool(edov)
        else:
            email_verified = bool(claims.get("email_verified", True))
        display_name = str(claims.get("name") or email)
        return OIDCIdentity(
            subject=subject,
            email=email,
            email_verified=email_verified,
            display_name=display_name,
            groups=_groups_from(claims, role_claim),
        )


register_adapter(EntraAdapter())
