"""
UI routes — serve Jinja2 templates.
Auth is handled client-side via JWT stored in a cookie; the templates read it via Alpine/fetch.
"""

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["ui"])
templates = Jinja2Templates(directory="app/templates")


def _optional_user(access_token: Annotated[str | None, Cookie()] = None) -> dict | None:
    """Return a minimal user dict from the cookie without raising on missing token."""
    if not access_token:
        return None
    try:
        from app.services.auth_service import decode_access_token

        payload = decode_access_token(access_token)
        return {"email": payload.get("sub"), "role": payload.get("role")}
    except Exception:
        return None


UserContext = Annotated[dict | None, Depends(_optional_user)]


@router.get("/", response_class=HTMLResponse)
def index(user: UserContext):
    if user:
        return RedirectResponse("/dashboard")
    return RedirectResponse("/login")


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user: UserContext):
    if user:
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse(request, "auth/login.html")


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, user: UserContext):
    if user:
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse(request, "auth/register.html")


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user: UserContext):
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "dashboard.html", {"user": user})


@router.get("/scenarios", response_class=HTMLResponse)
def scenarios_list(request: Request, user: UserContext):
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "scenarios/list.html", {"user": user})


@router.get("/scenarios/new", response_class=HTMLResponse)
def scenario_new(request: Request, user: UserContext):
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        request, "scenarios/editor.html", {"user": user, "scenario_id": None}
    )


@router.get("/scenarios/{scenario_id}", response_class=HTMLResponse)
def scenario_detail(scenario_id: int, request: Request, user: UserContext):
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        request, "scenarios/detail.html", {"user": user, "scenario_id": scenario_id}
    )


@router.get("/scenarios/{scenario_id}/edit", response_class=HTMLResponse)
def scenario_edit(scenario_id: int, request: Request, user: UserContext):
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        request, "scenarios/editor.html", {"user": user, "scenario_id": scenario_id}
    )


@router.get("/exercises", response_class=HTMLResponse)
def exercises_list(request: Request, user: UserContext):
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "exercises/list.html", {"user": user})


@router.get("/exercises/new", response_class=HTMLResponse)
def exercise_new(request: Request, user: UserContext):
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        request, "exercises/list.html", {"user": user, "show_create": True}
    )


@router.get("/exercises/{exercise_id}/facilitate", response_class=HTMLResponse)
def exercise_facilitate(exercise_id: int, request: Request, user: UserContext):
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        request, "exercises/facilitator.html", {"user": user, "exercise_id": exercise_id}
    )


@router.get("/exercises/{exercise_id}/participate", response_class=HTMLResponse)
def exercise_participate(exercise_id: int, request: Request, user: UserContext):
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        request, "exercises/participant.html", {"user": user, "exercise_id": exercise_id}
    )


@router.get("/exercises/{exercise_id}/communications", response_class=HTMLResponse)
def exercise_comms(exercise_id: int, request: Request, user: UserContext):
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        request, "communications/inbox.html", {"user": user, "exercise_id": exercise_id}
    )


@router.get("/help", response_class=HTMLResponse)
def help_page(request: Request, user: UserContext):
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "help.html", {"user": user})
