"""Auth0 adapter (#25).

Auth0 is a spec-compliant OIDC provider; discovery lives at
``https://<tenant-domain>/.well-known/openid-configuration``. It emits the
standard ``sub``/``email``/``email_verified``/``name`` claims. Auth0 does **not**
send roles/groups by default — expose them via an Action/Rule as a *namespaced*
custom claim (e.g. ``https://<your-app>/roles``) and point ``OIDC_AUTH0_ROLE_CLAIM``
at it. Uses the shared ``StandardOIDCAdapter``.
"""

from app.services.oidc.base import StandardOIDCAdapter, register_adapter

register_adapter(StandardOIDCAdapter("auth0"))
