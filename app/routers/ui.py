"""
UI routes — serve Jinja2 templates.
Auth is handled client-side via JWT stored in a cookie; the templates read it via Alpine/fetch.
"""

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.services.auth_service import decode_access_token
from app.services.oidc import service as oidc_service
from app.services.role_preview import effective_role

router = APIRouter(tags=["ui"])
templates = Jinja2Templates(directory="app/templates")


def _auth_context() -> dict:
    """SSO buttons + local-form visibility for the auth pages (#25)."""
    oidc_service.ensure_registered()
    return {
        "local_auth": settings.local_auth_enabled,
        "registration_enabled": settings.registration_enabled,
        "oidc_providers": [(p.key, p.display_name) for p in oidc_service.registered_providers()],
    }


class UIRedirect(Exception):
    """Raised by a UI-guard dependency to redirect instead of rendering a page.

    A FastAPI dependency can't return the route's response directly, so the guards
    raise this and an app-level handler (main.py) converts it to a RedirectResponse.
    """

    def __init__(self, url: str) -> None:
        self.url = url


def _optional_user(
    access_token: Annotated[str | None, Cookie()] = None,
    view_role: Annotated[str | None, Cookie(alias="dt_view_role")] = None,
    view_team: Annotated[str | None, Cookie(alias="dt_view_team")] = None,
) -> dict | None:
    """Return a minimal user dict from the cookie without raising on missing token."""
    if not access_token:
        return None
    try:
        payload = decode_access_token(access_token)
        actual_role = payload.get("role")
        role = effective_role(actual_role, view_role)
        # view_team is a facilitator preview affordance only; ignore it otherwise.
        team = view_team if actual_role == "facilitator" else payload.get("team")
        return {
            "email": payload.get("sub"),
            "role": role,
            "actual_role": actual_role,
            "team": team,
            "is_admin": bool(payload.get("is_admin", False)),
        }
    except Exception:
        return None


UserContext = Annotated[dict | None, Depends(_optional_user)]


def _is_facilitator(user: dict | None) -> bool:
    return bool(user and user.get("role") == "facilitator")


def _is_actual_facilitator(user: dict | None) -> bool:
    return bool(user and user.get("actual_role", user.get("role")) == "facilitator")


def require_ui_user(user: UserContext) -> dict:
    """Guard: redirect anonymous visitors to /login."""
    if not user:
        raise UIRedirect("/login")
    return user


def require_ui_facilitator(user: Annotated[dict, Depends(require_ui_user)]) -> dict:
    """Guard: require the *effective* facilitator role (honours role preview)."""
    if not _is_facilitator(user):
        raise UIRedirect("/dashboard")
    return user


def require_ui_actual_facilitator(user: Annotated[dict, Depends(require_ui_user)]) -> dict:
    """Guard: require the *actual* facilitator role (ignores role preview)."""
    if not _is_actual_facilitator(user):
        raise UIRedirect("/dashboard")
    return user


def require_ui_admin(user: Annotated[dict, Depends(require_ui_user)]) -> dict:
    """Guard: require the admin flag (page shell only; the API re-checks the DB)."""
    if not user.get("is_admin"):
        raise UIRedirect("/dashboard")
    return user


LoggedInUser = Annotated[dict, Depends(require_ui_user)]
FacilitatorUser = Annotated[dict, Depends(require_ui_facilitator)]
ActualFacilitatorUser = Annotated[dict, Depends(require_ui_actual_facilitator)]
AdminUser = Annotated[dict, Depends(require_ui_admin)]


@router.get("/", response_class=HTMLResponse)
def index(user: UserContext):
    if user:
        return RedirectResponse("/dashboard")
    return RedirectResponse("/login")


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user: UserContext):
    if user:
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse(request, "auth/login.html", _auth_context())


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, user: UserContext):
    if user:
        return RedirectResponse("/dashboard")
    # Self-registration is a local-auth affordance; redirect to /login when
    # local auth is off (OIDC-only) or self-registration is disabled (#67) so
    # there's no dead form.
    if not settings.local_auth_enabled or not settings.registration_enabled:
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "auth/register.html", _auth_context())


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user: LoggedInUser):
    return templates.TemplateResponse(request, "dashboard.html", {"user": user})


@router.get("/scenarios", response_class=HTMLResponse)
def scenarios_list(request: Request, user: ActualFacilitatorUser):
    return templates.TemplateResponse(request, "scenarios/list.html", {"user": user})


@router.get("/scenarios/new", response_class=HTMLResponse)
def scenario_new(request: Request, user: ActualFacilitatorUser):
    return templates.TemplateResponse(
        request, "scenarios/editor.html", {"user": user, "scenario_id": None}
    )


@router.get("/scenarios/{scenario_id}", response_class=HTMLResponse)
def scenario_detail(scenario_id: int, request: Request, user: ActualFacilitatorUser):
    return templates.TemplateResponse(
        request, "scenarios/detail.html", {"user": user, "scenario_id": scenario_id}
    )


@router.get("/scenarios/{scenario_id}/edit", response_class=HTMLResponse)
def scenario_edit(scenario_id: int, request: Request, user: ActualFacilitatorUser):
    return templates.TemplateResponse(
        request, "scenarios/editor.html", {"user": user, "scenario_id": scenario_id}
    )


@router.get("/exercises", response_class=HTMLResponse)
def exercises_list(request: Request, user: LoggedInUser):
    return templates.TemplateResponse(request, "exercises/list.html", {"user": user})


@router.get("/exercises/new", response_class=HTMLResponse)
def exercise_new(request: Request, user: FacilitatorUser):
    return templates.TemplateResponse(
        request, "exercises/list.html", {"user": user, "show_create": True}
    )


@router.get("/exercises/{exercise_id}/facilitate", response_class=HTMLResponse)
def exercise_facilitate(exercise_id: int, request: Request, user: FacilitatorUser):
    return templates.TemplateResponse(
        request, "exercises/facilitator.html", {"user": user, "exercise_id": exercise_id}
    )


@router.get("/exercises/{exercise_id}/participate", response_class=HTMLResponse)
def exercise_participate(exercise_id: int, request: Request, user: LoggedInUser):
    return templates.TemplateResponse(
        request, "exercises/participant.html", {"user": user, "exercise_id": exercise_id}
    )


@router.get("/exercises/{exercise_id}/communications", response_class=HTMLResponse)
def exercise_comms(exercise_id: int, request: Request, user: LoggedInUser):
    return templates.TemplateResponse(
        request, "communications/inbox.html", {"user": user, "exercise_id": exercise_id}
    )


@router.get("/communications", response_class=HTMLResponse)
def communications_hub(request: Request, user: LoggedInUser):
    return templates.TemplateResponse(request, "communications/index.html", {"user": user})


@router.get("/help", response_class=HTMLResponse)
def help_page(request: Request, user: LoggedInUser):
    return templates.TemplateResponse(request, "help.html", {"user": user})


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: LoggedInUser):
    return templates.TemplateResponse(request, "settings.html", {"user": user})


@router.get("/admin/audit", response_class=HTMLResponse)
def admin_audit_page(request: Request, user: AdminUser):
    return templates.TemplateResponse(request, "admin/audit.html", {"user": user})


@router.get("/admin/proxy", response_class=HTMLResponse)
def admin_proxy_page(request: Request, user: AdminUser):
    return templates.TemplateResponse(request, "admin/proxy.html", {"user": user})
