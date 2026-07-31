"""Pin secret-bearing egress destinations to the environment (#259).

Three admin-editable *host* fields decide where an env-only secret is sent: the SIEM
HTTP endpoint (carries ``SIEM_HTTP_TOKEN`` as a bearer), the SMTP host (authenticates
with ``SMTP_PASSWORD``), and the proxy URL (carries ``PROXY_USERNAME``/``PROXY_PASSWORD``
in its userinfo). Each pairs with a "test" endpoint, so re-pointing one and pressing
test mailed the secret to any host an admin chose.

Those secrets are declared env-only precisely so that admins cannot read them — the
effective-config view shows set/unset booleans, never values. Letting the same admin
redirect them defeats that boundary. So whenever the secret is present, the
destination's origin must match the one the operator set in the environment. This
mirrors ``llm_settings_service.validate_selection``, which pins provider base URLs for
exactly the same reason: the API key rides on the connection.

Clearing a destination is always allowed — with nowhere to send it, the secret is not
at risk, and an operator must stay able to turn a sink off.
"""

from urllib.parse import urlsplit


def origin(value: str | None) -> str:
    """Normalised ``scheme://host:port`` for a URL, or the bare host for a hostname.

    Comparing origins rather than whole URLs keeps path/query edits (a collector's
    ingest path, say) admin-editable while the destination host stays pinned.
    """
    text = (value or "").strip()
    if not text:
        return ""
    if "//" not in text:
        return text.lower()
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme.lower()}://{host}{port}"


def host_changed(old: str | None, new: str | None) -> bool:
    """Whether a sink's destination origin moved (worth a critical audit event)."""
    return origin(old) != origin(new)


def check_pinned(
    *,
    new: str | None,
    env: str | None,
    secret_present: bool,
    field_label: str,
    env_var: str,
    secret_var: str,
) -> None:
    """Raise ValueError when a runtime edit would redirect an env-only secret."""
    if not secret_present:
        return
    if not (new or "").strip():
        return
    if origin(new) == origin(env):
        return
    raise ValueError(
        f"{secret_var} is set, so {field_label} is pinned to the environment's "
        f"{env_var}. Change {env_var} and restart, or clear {secret_var}, before "
        "pointing this at another host."
    )
