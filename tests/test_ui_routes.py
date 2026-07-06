from httpx import AsyncClient

from app.models.exercise import Exercise


async def test_participant_cannot_load_facilitator_console(
    client: AsyncClient, participant_token: str, active_exercise: Exercise
):
    client.cookies.set("access_token", participant_token)
    r = await client.get(
        f"/exercises/{active_exercise.id}/facilitate",
        follow_redirects=False,
    )
    assert r.status_code == 307
    assert r.headers["location"] == "/dashboard"


async def test_participant_cannot_load_scenario_editor(client: AsyncClient, participant_token: str):
    client.cookies.set("access_token", participant_token)
    r = await client.get(
        "/scenarios/new",
        follow_redirects=False,
    )
    assert r.status_code == 307
    assert r.headers["location"] == "/dashboard"


async def test_authenticated_user_can_load_settings(client: AsyncClient, participant_token: str):
    client.cookies.set("access_token", participant_token)
    r = await client.get("/settings")
    assert r.status_code == 200
    assert "Settings" in r.text


async def test_dark_theme_cookie_prerenders_dark_document(
    client: AsyncClient, participant_token: str
):
    client.cookies.set("access_token", participant_token)
    client.cookies.set("dt_theme", "dark")
    client.cookies.set("dt_resolved_theme", "dark")
    r = await client.get("/settings")
    assert r.status_code == 200
    assert (
        '<html lang="en" data-theme="dark" '
        'style="background-color: oklch(0.185 0.02 256); color-scheme: dark;">'
    ) in r.text
    assert '<meta name="color-scheme" content="dark" />' in r.text


async def test_shell_marks_main_region_for_soft_navigation(
    client: AsyncClient, participant_token: str
):
    client.cookies.set("access_token", participant_token)
    r = await client.get("/exercises")
    assert r.status_code == 200
    assert '<main id="app-main"' in r.text
    # Soft-navigation now lives in the external same-origin runtime (strict CSP, #77).
    assert "/static/js/app.js" in r.text
    assert "dt-navigation-curtain" not in r.text


async def test_authenticated_user_can_load_communications_hub(
    client: AsyncClient, participant_token: str
):
    client.cookies.set("access_token", participant_token)
    r = await client.get("/communications")
    assert r.status_code == 200
    assert "No active exercise selected" in r.text


async def test_facilitator_preview_participant_can_load_scenarios_for_testing(
    client: AsyncClient, facilitator_token: str
):
    client.cookies.set("access_token", facilitator_token)
    client.cookies.set("dt_view_role", "participant")
    r = await client.get("/scenarios", follow_redirects=False)
    assert r.status_code == 200
    assert "Scenarios" in r.text


async def test_facilitator_preview_participant_can_still_load_settings(
    client: AsyncClient, facilitator_token: str
):
    client.cookies.set("access_token", facilitator_token)
    client.cookies.set("dt_view_role", "participant")
    r = await client.get("/settings")
    assert r.status_code == 200
    assert "Role preview" in r.text


async def test_preview_participant_hides_facilitator_sidebar_navigation(
    client: AsyncClient, facilitator_token: str
):
    client.cookies.set("access_token", facilitator_token)
    client.cookies.set("dt_view_role", "participant")
    r = await client.get("/dashboard")
    assert r.status_code == 200
    # Rail nav is client-gated via the sidebarNav component (strict CSP, #77):
    # facilitator-only links behind isFacilitator, the preview indicator behind hasPreview.
    assert 'x-show="isFacilitator"' in r.text
    assert 'x-show="hasPreview"' in r.text
