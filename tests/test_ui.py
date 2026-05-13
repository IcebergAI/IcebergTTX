"""
Playwright UI tests — requires the dev server running on port 8765:
  uvicorn app.main:app --port 8765

Seed users are created by conftest (see below). Run with:
  pytest tests/test_ui.py -v
  pytest tests/test_ui.py --headed -v    # watch the browser
"""
import json

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

def login(page: Page, email: str, password: str = "password123") -> None:
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


# ── Auth ──────────────────────────────────────────────────────────────────────

def test_login_page_renders(page: Page):
    page.goto(f"{BASE}/login")
    expect(page.locator("input[type=email]")).to_be_visible()
    expect(page.locator("input[type=password]")).to_be_visible()
    expect(page.locator("button[type=submit]")).to_be_visible()
    expect(page.locator("h1")).to_contain_text("Log in")


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
    expect(page.locator("h1")).to_contain_text("Dashboard")


def test_unauthenticated_redirect_to_login(page: Page):
    page.goto(f"{BASE}/dashboard")
    expect(page).to_have_url(f"{BASE}/login")


def test_register_page_renders(page: Page):
    page.goto(f"{BASE}/register")
    expect(page.locator("input[type=email]")).to_be_visible()
    expect(page.locator("input[type=password]")).to_be_visible()
    expect(page.locator("h1")).to_contain_text("Register")


# ── Dashboard ─────────────────────────────────────────────────────────────────

def test_dashboard_shows_for_facilitator(page: Page):
    login_facilitator(page)
    expect(page.locator("h1")).to_contain_text("Dashboard")


def test_dashboard_shows_for_participant(page: Page):
    login_participant(page)
    expect(page.locator("h1")).to_contain_text("Dashboard")


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


def test_scenario_detail_page_renders(page: Page):
    login_facilitator(page)
    scenario_id = _make_scenario(page)
    page.goto(f"{BASE}/scenarios/{scenario_id}")
    expect(page.locator("body")).not_to_contain_text("Internal Server Error")


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
    # Inject queue heading is static
    expect(page.locator("body")).to_contain_text("Inject Queue")


def test_participant_view_renders(page: Page):
    login_facilitator(page)
    scenario_id = _make_scenario(page)
    exercise_id = _make_exercise(page, scenario_id)
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
