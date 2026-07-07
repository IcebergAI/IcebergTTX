"""Okta adapter (#25).

Okta is a spec-compliant OIDC provider. Discovery is either the org authorization
server (``https://<domain>/.well-known/openid-configuration``) or a custom
authorization server (``https://<domain>/oauth2/<server>/.well-known/openid-configuration``,
where ``<server>`` is often ``default``) — selected in config. Groups arrive in the
``groups`` claim once a groups claim is added to the token/authorization server.
Uses the shared ``StandardOIDCAdapter``.
"""

from app.services.oidc.base import StandardOIDCAdapter, register_adapter

register_adapter(StandardOIDCAdapter("okta"))
