from httpx import AsyncClient

from app.models.exercise import Exercise


async def test_facilitator_console_exposes_responsive_pane_contract(
    client: AsyncClient, facilitator_token: str, active_exercise: Exercise
):
    """The server-rendered console keeps the mobile tab/panel wiring in CI (#135)."""
    client.cookies.set("access_token", facilitator_token)
    r = await client.get(f"/exercises/{active_exercise.id}/facilitate")

    assert r.status_code == 200
    assert 'data-testid="facilitator-console"' in r.text
    assert 'role="tablist"' in r.text
    assert 'data-testid="facilitator-tab-injects"' in r.text
    assert 'data-testid="facilitator-tab-responses"' in r.text
    assert 'data-testid="facilitator-tab-ops"' in r.text
    assert 'data-testid="facilitator-pane-injects"' in r.text
    assert 'data-testid="facilitator-pane-responses"' in r.text
    assert 'data-testid="facilitator-pane-ops"' in r.text
    assert 'x-show="opsPanelVisible"' in r.text
    assert 'cycleMobilePane(1)' in r.text


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


async def test_beta_feedback_link_prefills_version(client: AsyncClient, participant_token: str):
    """The beta feedback link (#118) opens the GitHub bug form with the running
    version prefilled, and the settings footer shows the version. Available to
    any authenticated role (participant here)."""
    from app.services.audit_service import APP_VERSION

    client.cookies.set("access_token", participant_token)
    r = await client.get("/settings")
    assert r.status_code == 200
    # The bug-report form is targeted and the version query param is populated.
    assert "issues/new?template=bug_report.yml" in r.text
    assert f"version={APP_VERSION}" in r.text
    # Version is also surfaced in the settings footer.
    assert f"IcebergTTX v{APP_VERSION}" in r.text


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
