"""Single source of truth for facilitator role/team preview resolution.

Facilitators can view the app as a participant or observer via the ``dt_view_role``
/ ``dt_view_team`` cookies (HTTP) or ``view_role`` / ``view_team`` query params
(WebSocket). The rule is the same everywhere and security-sensitive: the preview
is honoured **only** when the user's *actual* role is facilitator. Previously this
logic was copy-pasted across the HTTP dependency, the WS handler, and the UI route,
and the WS copy had drifted to apply ``view_team`` to genuine participants (#30).
"""

from app.models.user import User, UserRole

VIEWABLE_ROLES = {UserRole.facilitator.value, UserRole.participant.value, UserRole.observer.value}


def effective_role(actual_role: str | None, view_role: str | None) -> str | None:
    """Role to present given the actual role and a requested preview role (string form).

    Used by the UI route, which works with the decoded JWT payload rather than a
    ``User`` row. Returns ``actual_role`` unchanged unless the user is actually a
    facilitator requesting a valid preview role.
    """
    if actual_role == UserRole.facilitator.value and view_role in VIEWABLE_ROLES:
        return view_role
    return actual_role


def apply_role_preview(user: User, view_role: str | None, view_team: str | None) -> User:
    """Return the effective ``User`` for a request, honouring role preview.

    Always stamps ``actual_role``/``actual_team``/``can_switch_roles`` so callers
    can recover the real identity (e.g. for audit and access control). Only a real
    facilitator with a valid ``view_role`` gets a previewed copy; for everyone else
    the original user is returned unchanged and ``view_team`` is ignored.
    """
    object.__setattr__(user, "actual_role", user.role)
    object.__setattr__(user, "actual_team", user.team)
    object.__setattr__(user, "can_switch_roles", user.role == UserRole.facilitator)
    if user.role != UserRole.facilitator or view_role is None:
        return user
    try:
        previewed_role = UserRole(view_role)
    except ValueError:
        return user
    effective = user.model_copy(
        update={
            "role": previewed_role,
            "team": view_team.strip() if view_team and view_team.strip() else user.team,
        }
    )
    object.__setattr__(effective, "actual_role", user.role)
    object.__setattr__(effective, "actual_team", user.team)
    object.__setattr__(effective, "can_switch_roles", True)
    return effective
