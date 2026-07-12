"""
Playwright UI tests — requires the dev server running on port 8765:
  uvicorn app.main:app --port 8765

Seed users are created by conftest (see below). Run with:
  pytest tests/test_ui.py -v
  pytest tests/test_ui.py --headed -v    # watch the browser
"""
import json
import re

import pytest

httpx = pytest.importorskip("httpx")

try:
    httpx.get("http://localhost:8765/login", timeout=1.0)
except Exception:
    pytest.skip("Playwright UI tests require the dev server on port 8765", allow_module_level=True)

sync_api = pytest.importorskip("playwright.sync_api")
Browser = sync_api.Browser
Page = sync_api.Page
expect = sync_api.expect

BASE = "http://localhost:8765"


# ── Helpers ───────────────────────────────────────────────────────────────────

def login(page: Page, email: str, password: str = "password1234") -> None:
    try:
        page.evaluate(
            "() => { localStorage.removeItem('dt_token');"
            " localStorage.removeItem('dt_view_role');"
            " localStorage.removeItem('dt_view_team'); }"
        )
    except Exception:
        pass
    page.context.clear_cookies()
    page.goto(f"{BASE}/login")
    page.fill("input[type=email]", email)
    page.fill("input[type=password]", password)
    page.click("button[type=submit]")
    page.wait_for_url(f"{BASE}/dashboard", timeout=8000)


def login_facilitator(page: Page) -> None:
    login(page, "facilitator@deep.test")


def login_participant(page: Page) -> None:
    login(page, "participant@deep.test")


def _api_url(path: str) -> str:
    """All JSON API endpoints live under /api/."""
    return f"{BASE}/api{path}" if not path.startswith("/api") else f"{BASE}{path}"


def api_post(page: Page, path: str, body: dict | None = None) -> dict:
    r = page.request.post(
        _api_url(path),
        headers={"Content-Type": "application/json"},
        data=json.dumps(body or {}),
    )
    return r


def api_get(page: Page, path: str) -> dict:
    return page.request.get(_api_url(path))


def _make_scenario(page: Page) -> int:
    scenario_def = {
        "schema_version": "1.0",
        "title": "UI Test Scenario",
        "description": "Created by Playwright test",
        "participant_teams": [{"id": "it_ops", "label": "IT Ops"}],
        "injects": [
            {
                "id": "inject_01",
                "title": "Initial Alert",
                "content": "Systems compromised. What do you do?",
                "target_teams": ["it_ops"],
                "options": [{"id": "opt_a", "label": "Isolate", "next_inject_id": "inject_02"}],
                "free_text_response": True,
                "sequence_order": 1,
            },
            {
                "id": "inject_02",
                "title": "Containment",
                "content": "Systems isolated. Next steps?",
                "target_teams": ["it_ops"],
                "options": [],
                "free_text_response": True,
                "sequence_order": 2,
            },
        ],
        "start_inject_id": "inject_01",
    }
    r = api_post(page, "/scenarios/import", {"definition": scenario_def})
    assert r.status == 201, r.text()
    return r.json()["id"]


def _make_exercise(page: Page, scenario_id: int, title: str = "UI Test Exercise") -> int:
    r = api_post(page, "/exercises", {"scenario_id": scenario_id, "title": title})
    assert r.status == 201, r.text()
    return r.json()["id"]


def _enrol_participant(page: Page, exercise_id: int, email: str = "participant@deep.test") -> None:
    users_r = api_get(page, "/users")
    assert users_r.status == 200, users_r.text()
    participant = next(u for u in users_r.json() if u["email"] == email)
    enrol_r = api_post(page, f"/exercises/{exercise_id}/members", {"user_id": participant["id"]})
    assert enrol_r.status == 201, enrol_r.text()


# ── Auth ──────────────────────────────────────────────────────────────────────

def test_login_page_renders(page: Page):
    page.goto(f"{BASE}/login")
    expect(page.locator("input[type=email]")).to_be_visible()
    expect(page.locator("input[type=password]")).to_be_visible()
    expect(page.locator("button[type=submit]")).to_be_visible()
    expect(page.locator("h1")).to_contain_text("Sign in")


def test_login_wrong_password_shows_error(page: Page):
    page.goto(f"{BASE}/login")
    page.fill("input[type=email]", "facilitator@deep.test")
    page.fill("input[type=password]", "wrongpassword")
    page.click("button[type=submit]")
    # Error message appears, no redirect
    expect(page.locator("[x-show=error]")).to_be_visible(timeout=5000)
    expect(page).not_to_have_url(f"{BASE}/dashboard")


def test_login_facilitator_redirects_to_dashboard(page: Page):
    login_facilitator(page)
    expect(page).to_have_url(f"{BASE}/dashboard")
    expect(page.locator("h1")).to_contain_text("Home")


def test_sign_out_returns_to_login(page: Page):
    login_facilitator(page)
    sign_out = page.get_by_role("button", name="Sign out")
    expect(sign_out).to_be_visible(timeout=5000)
    sign_out.click()
    page.wait_for_url(f"{BASE}/login", timeout=8000)
    expect(page.locator("h1")).to_contain_text("Sign in")
    page.goto(f"{BASE}/dashboard")
    expect(page).to_have_url(f"{BASE}/login")


def test_unauthenticated_redirect_to_login(page: Page):
    page.goto(f"{BASE}/dashboard")
    expect(page).to_have_url(f"{BASE}/login")


def test_register_page_renders(page: Page):
    page.goto(f"{BASE}/register")
    expect(page.locator("input[type=email]")).to_be_visible()
    expect(page.locator("input[type=password]")).to_be_visible()
    expect(page.locator("h1")).to_contain_text("Create account")


# ── Dashboard ─────────────────────────────────────────────────────────────────

def test_dashboard_shows_for_facilitator(page: Page):
    login_facilitator(page)
    expect(page.locator("h1")).to_contain_text("Home")


def test_dashboard_shows_for_participant(page: Page):
    login_participant(page)
    expect(page.locator("h1")).to_contain_text("Home")


# ── Scenarios ─────────────────────────────────────────────────────────────────

def test_scenarios_list_page_renders(page: Page):
    login_facilitator(page)
    page.goto(f"{BASE}/scenarios")
    expect(page.locator("body")).not_to_contain_text("Internal Server Error")
    expect(page.locator("body")).not_to_contain_text("500")


def test_scenario_new_page_renders(page: Page):
    login_facilitator(page)
    page.goto(f"{BASE}/scenarios/new")
    expect(page.locator("body")).not_to_contain_text("Internal Server Error")
    expect(page.get_by_test_id("scenario-builder")).to_be_visible()
    expect(page.get_by_test_id("scenario-outline")).to_contain_text("Scenario brief")
    expect(page.get_by_test_id("scenario-readiness")).to_contain_text("Readiness")


def test_scenario_detail_page_renders(page: Page):
    login_facilitator(page)
    scenario_id = _make_scenario(page)
    page.goto(f"{BASE}/scenarios/{scenario_id}")
    expect(page.locator("body")).not_to_contain_text("Internal Server Error")


def test_scenario_editor_preserves_branch_targets(page: Page):
    login_facilitator(page)
    scenario_id = _make_scenario(page)
    page.goto(f"{BASE}/scenarios/{scenario_id}/edit")

    page.get_by_test_id("scenario-outline").get_by_text("Initial Alert").click()
    first_branch_select = page.get_by_test_id("option-next")
    expect(first_branch_select).to_have_value("inject_02")
    page.get_by_test_id("scenario-save-bottom").click()
    expect(page.get_by_text("Saved successfully.")).to_be_visible(timeout=5000)

    scenario_r = api_get(page, f"/scenarios/{scenario_id}")
    assert scenario_r.status == 200, scenario_r.text()
    options = scenario_r.json()["definition"]["injects"][0]["options"]
    assert options[0]["next_inject_id"] == "inject_02"


def test_scenario_builder_creates_structured_scenario(page: Page):
    login_facilitator(page)
    page.goto(f"{BASE}/scenarios/new")

    page.get_by_test_id("scenario-title").fill("Ops Builder Scenario")
    page.get_by_test_id("scenario-description").fill(
        "A full authoring flow created in the builder."
    )
    page.get_by_test_id("scenario-author").fill("Exercise Design Team")
    page.get_by_test_id("scenario-duration").fill("60")
    page.get_by_test_id("scenario-tags").fill("cyber, resilience")
    page.get_by_test_id("scenario-debrief").fill("Review branch quality and role coordination.")

    page.get_by_test_id("nav-teams").click()
    page.get_by_test_id("add-team").click()
    page.get_by_test_id("team-id").fill("it_ops")
    page.get_by_test_id("team-label").fill("IT Operations")

    page.get_by_test_id("scenario-outline").get_by_text("Untitled inject").click()
    page.get_by_test_id("inject-title").fill("Initial Alert")
    page.get_by_test_id("inject-content").fill("Service desk reports a suspicious outage.")
    page.get_by_test_id("inject-target-team").check()

    page.get_by_test_id("scenario-outline").get_by_role("button", name="+ Add").click()
    page.get_by_test_id("inject-title").fill("Containment Decision")
    page.get_by_test_id("inject-content").fill("The team must choose a containment path.")

    page.get_by_test_id("scenario-outline").get_by_text("Initial Alert").click()
    page.get_by_test_id("add-branch-option").click()
    page.get_by_test_id("option-label").fill("Escalate to incident command")
    page.get_by_test_id("option-next").select_option("inject_02")
    page.get_by_test_id("add-expected-action").click()
    page.get_by_test_id("expected-action").fill("Notify the incident commander")

    expect(page.get_by_test_id("scenario-readiness")).to_contain_text("Scenario can be saved")
    page.get_by_test_id("scenario-save-bottom").click()
    page.wait_for_url(re.compile(rf"{BASE}/scenarios/\d+$"), timeout=8000)

    scenario_id = int(page.url.rstrip("/").split("/")[-1])
    scenario_r = api_get(page, f"/scenarios/{scenario_id}")
    assert scenario_r.status == 200, scenario_r.text()
    definition = scenario_r.json()["definition"]
    assert definition["metadata"]["author"] == "Exercise Design Team"
    assert definition["metadata"]["estimated_duration_minutes"] == 60
    assert definition["participant_teams"] == [{"id": "it_ops", "label": "IT Operations"}]
    assert definition["injects"][0]["sequence_order"] == 1
    assert definition["injects"][1]["sequence_order"] == 2
    assert definition["injects"][0]["target_teams"] == ["it_ops"]
    assert definition["injects"][0]["options"][0]["next_inject_id"] == "inject_02"
    assert definition["injects"][0]["expected_actions"] == ["Notify the incident commander"]


def test_scenario_editor_preserves_hidden_triggered_communications(page: Page):
    login_facilitator(page)
    scenario_def = {
        "schema_version": "1.0",
        "title": "Triggered Comms Scenario",
        "description": "Preserve hidden comm definitions.",
        "participant_teams": [{"id": "it_ops", "label": "IT Ops"}],
        "injects": [
            {
                "id": "inject_01",
                "title": "Initial Alert",
                "content": "A message should be scheduled on release.",
                "target_teams": ["it_ops"],
                "options": [],
                "free_text_response": True,
                "sequence_order": 1,
                "triggers_communications": [
                    {
                        "external_entity": "Media desk",
                        "direction": "inbound",
                        "subject": "Request for comment",
                        "body": "Can you confirm the incident?",
                        "delay_after_release_seconds": 2,
                    }
                ],
            }
        ],
        "start_inject_id": "inject_01",
    }
    r = api_post(page, "/scenarios/import", {"definition": scenario_def})
    assert r.status == 201, r.text()
    scenario_id = r.json()["id"]

    page.goto(f"{BASE}/scenarios/{scenario_id}/edit")
    page.get_by_test_id("scenario-save-bottom").click()
    expect(page.get_by_text("Saved successfully.")).to_be_visible(timeout=5000)

    scenario_r = api_get(page, f"/scenarios/{scenario_id}")
    assert scenario_r.status == 200, scenario_r.text()
    trigger = scenario_r.json()["definition"]["injects"][0]["triggers_communications"][0]
    assert trigger["external_entity"] == "Media desk"
    assert trigger["delay_after_release_seconds"] == 2


# ── Exercises ─────────────────────────────────────────────────────────────────

def test_exercises_list_page_renders(page: Page):
    login_facilitator(page)
    page.goto(f"{BASE}/exercises")
    expect(page.locator("body")).not_to_contain_text("Internal Server Error")


def test_facilitator_console_renders(page: Page):
    login_facilitator(page)
    scenario_id = _make_scenario(page)
    exercise_id = _make_exercise(page, scenario_id)
    api_post(page, f"/exercises/{exercise_id}/start")

    page.goto(f"{BASE}/exercises/{exercise_id}/facilitate")
    expect(page.locator("body")).not_to_contain_text("Internal Server Error")
    expect(page.locator("body")).to_contain_text("Injects")


def test_facilitator_console_mobile_panes_are_reachable_without_overflow(page: Page):
    """The phone layout exposes each console pane without a page-wide x-scroll (#135)."""
    page.set_viewport_size({"width": 390, "height": 844})
    login_facilitator(page)
    scenario_id = _make_scenario(page)
    exercise_id = _make_exercise(page, scenario_id, title="Mobile Console Exercise")
    api_post(page, f"/exercises/{exercise_id}/start")

    page.goto(f"{BASE}/exercises/{exercise_id}/facilitate")
    expect(page.locator('[x-text="exTitle"]')).to_have_text("Mobile Console Exercise")

    console = page.get_by_test_id("facilitator-console")
    expect(console).to_be_visible()
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    assert console.evaluate("el => el.scrollWidth <= el.clientWidth")

    injects = page.get_by_test_id("facilitator-pane-injects")
    responses = page.get_by_test_id("facilitator-pane-responses")
    ops = page.get_by_test_id("facilitator-pane-ops")
    expect(injects).to_be_visible()
    expect(responses).to_be_hidden()
    expect(ops).to_be_hidden()

    page.get_by_test_id("facilitator-tab-responses").click()
    expect(injects).to_be_hidden()
    expect(responses).to_be_visible()
    expect(responses.get_by_role("heading", name="Responses")).to_be_visible()

    page.get_by_test_id("facilitator-tab-ops").click()
    expect(responses).to_be_hidden()
    expect(ops).to_be_visible()
    expect(ops.get_by_text("Participants", exact=True)).to_be_visible()

    # The new mobile command and tab controls must remain fully inside the viewport.
    assert console.evaluate(
        """el => [...el.querySelectorAll(
          '.fac-console__ticker a, .fac-console__ticker button, .fac-console__mobile-nav button'
        )].filter(node => node.getClientRects().length).every(node => {
          const rect = node.getBoundingClientRect();
          return rect.left >= 0 && rect.right <= document.documentElement.clientWidth;
        })"""
    )

    for width in (320, 390, 768):
        page.set_viewport_size({"width": width, "height": 844})
        page.wait_for_timeout(50)
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )
        assert console.evaluate("el => el.scrollWidth <= el.clientWidth")


def test_mobile_shell_opens_navigation_and_reaches_settings(page: Page):
    """Phone navigation keeps the route visible while preserving all destinations (#137)."""
    page.set_viewport_size({"width": 390, "height": 844})
    login_facilitator(page)

    shell = page.get_by_test_id("mobile-shell")
    expect(shell).to_be_visible()
    menu = page.get_by_role("button", name="Open navigation")
    menu.click()
    nav = page.locator("#primary-navigation")
    expect(nav).to_be_visible()
    close = page.get_by_role("button", name="Close navigation")
    expect(close).to_be_focused()
    expect(nav).to_have_attribute("aria-modal", "true")
    expect(page.locator("[data-app-content]")).to_have_attribute("inert", "")

    page.keyboard.press("Shift+Tab")
    expect(page.get_by_role("button", name="Sign out")).to_be_focused()
    page.keyboard.press("Tab")
    expect(close).to_be_focused()
    page.keyboard.press("Escape")
    expect(nav).to_be_hidden()
    expect(menu).to_be_focused()
    expect(page.locator("[data-app-content]")).not_to_have_attribute("inert", "")

    menu.click()
    page.get_by_role("link", name="Settings").click()
    page.wait_for_url(f"{BASE}/settings", timeout=8000)
    expect(page.locator("h1")).to_contain_text("Settings")


def _assert_visible_control_geometry(page: Page, minimum: int) -> None:
    """Verify the visible shared controls meet the touch-target baseline."""
    undersized = page.evaluate(
        """minimum => [...document.querySelectorAll(
          'button, a.btn, .rail-link, .rail-live-main, .rail-live-opt, .rail-logout, '
          + 'input:not([type=checkbox]):not([type=radio]):not([type=range]), '
          + 'select, textarea, summary, '
          + '[role=button], [role=tab]'
        )].filter(node => {
          const style = getComputedStyle(node);
          const rect = node.getBoundingClientRect();
          return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0
            && rect.height > 0 && (rect.width < minimum || rect.height < minimum);
        }).map(node => ({
          tag: node.tagName, text: node.textContent.trim().slice(0, 48),
          width: node.getBoundingClientRect().width, height: node.getBoundingClientRect().height,
        }))""",
        minimum,
    )
    assert not undersized, undersized


def test_touch_target_geometry_across_core_routes(page: Page):
    """Shared controls remain at least 40px desktop / 44px on a phone (#151)."""
    login_facilitator(page)
    scenario_id = _make_scenario(page)
    exercise_id = _make_exercise(page, scenario_id, title="Touch target exercise")
    _enrol_participant(page, exercise_id)
    api_post(page, f"/exercises/{exercise_id}/start")
    first_inject = api_get(page, f"/exercises/{exercise_id}/injects").json()[0]
    api_post(page, f"/exercises/{exercise_id}/injects/{first_inject['id']}/release")

    desktop_routes = [
        "/dashboard",
        f"/exercises/{exercise_id}/facilitate",
        f"/scenarios/{scenario_id}/edit",
        "/settings",
        "/admin/users",
    ]
    page.set_viewport_size({"width": 1440, "height": 1000})
    for route in desktop_routes:
        page.goto(f"{BASE}{route}")
        expect(page.locator("body")).not_to_contain_text("Internal Server Error")
        _assert_visible_control_geometry(page, 40)
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )

    # A real phone interaction: changing facilitator panes is the primary live
    # exercise navigation. Check every specified responsive boundary.
    for width, minimum in ((320, 44), (390, 44), (768, 40)):
        page.set_viewport_size({"width": width, "height": 844})
        page.goto(f"{BASE}/exercises/{exercise_id}/facilitate")
        page.get_by_test_id("facilitator-tab-responses").click()
        expect(page.get_by_test_id("facilitator-pane-responses")).to_be_visible()
        _assert_visible_control_geometry(page, minimum)
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )

    # The participant response controls are a separate touch-first surface and
    # must meet the same 44px phone baseline.
    page.set_viewport_size({"width": 390, "height": 844})
    login_participant(page)
    page.goto(f"{BASE}/exercises/{exercise_id}/participate")
    expect(page.locator("body")).to_contain_text("Initial Alert")
    _assert_visible_control_geometry(page, 44)
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )


def test_facilitator_console_desktop_preserves_split_panes(page: Page):
    page.set_viewport_size({"width": 1440, "height": 1000})
    login_facilitator(page)
    scenario_id = _make_scenario(page)
    exercise_id = _make_exercise(page, scenario_id, title="Desktop Console Exercise")
    api_post(page, f"/exercises/{exercise_id}/start")

    page.goto(f"{BASE}/exercises/{exercise_id}/facilitate")
    expect(page.locator('[x-text="exTitle"]')).to_have_text("Desktop Console Exercise")
    expect(page.locator(".fac-console__mobile-nav")).to_be_hidden()

    injects = page.get_by_test_id("facilitator-pane-injects")
    responses = page.get_by_test_id("facilitator-pane-responses")
    ops = page.get_by_test_id("facilitator-pane-ops")
    expect(injects).to_be_visible()
    expect(responses).to_be_visible()
    expect(ops).to_be_hidden()

    page.get_by_role("button", name="Ops panel").click()
    expect(ops).to_be_visible()
    injects_rect = injects.bounding_box()
    responses_rect = responses.bounding_box()
    ops_rect = ops.bounding_box()
    assert injects_rect is not None and responses_rect is not None and ops_rect is not None
    assert injects_rect["x"] + injects_rect["width"] <= responses_rect["x"] + 1
    assert responses_rect["x"] + responses_rect["width"] <= ops_rect["x"] + 1
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )


def test_participant_view_renders(page: Page):
    login_facilitator(page)
    scenario_id = _make_scenario(page)
    exercise_id = _make_exercise(page, scenario_id)
    _enrol_participant(page, exercise_id)
    api_post(page, f"/exercises/{exercise_id}/start")

    login_participant(page)
    page.goto(f"{BASE}/exercises/{exercise_id}/participate")
    expect(page.locator("body")).not_to_contain_text("Internal Server Error")


# ── End-to-end inject flow ────────────────────────────────────────────────────

def test_facilitator_releases_inject_visible_to_participant(page: Page, browser: Browser):
    """Facilitator releases inject → participant page shows inject title."""
    login_facilitator(page)
    scenario_id = _make_scenario(page)
    exercise_id = _make_exercise(page, scenario_id)
    _enrol_participant(page, exercise_id)
    api_post(page, f"/exercises/{exercise_id}/start")

    # Release the first pending inject via API
    injects_r = api_get(page, f"/exercises/{exercise_id}/injects")
    pending = next(i for i in injects_r.json() if i["state"] == "pending")
    api_post(page, f"/exercises/{exercise_id}/injects/{pending['id']}/release")

    # Participant view in a separate browser context
    ctx = browser.new_context()
    p = ctx.new_page()
    login(p, "participant@deep.test")
    p.goto(f"{BASE}/exercises/{exercise_id}/participate")

    # The released inject should be visible (loaded on page init)
    expect(p.locator("body")).to_contain_text("Initial Alert", timeout=6000)
    ctx.close()


# ── Communications ────────────────────────────────────────────────────────────

def test_communications_inbox_renders(page: Page):
    login_facilitator(page)
    scenario_id = _make_scenario(page)
    exercise_id = _make_exercise(page, scenario_id)
    api_post(page, f"/exercises/{exercise_id}/start")

    page.goto(f"{BASE}/exercises/{exercise_id}/communications")
    expect(page.locator("body")).not_to_contain_text("Internal Server Error")
