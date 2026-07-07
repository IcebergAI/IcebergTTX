"""OIDC / SSO adapter layer (#25).

Authlib provides the provider-agnostic engine (discovery, Authorization-Code+PKCE,
state/nonce, JWKS ID-token validation). The adapters here capture only what
genuinely differs between IdPs: how identity/email-verified/groups are read from
the token claims. See ``base.get_adapter`` for the registry.
"""
