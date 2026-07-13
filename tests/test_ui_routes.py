import html
import json
import re
from typing import get_args

from httpx import AsyncClient

from app.config import LLM_PROVIDER_KEYS
from app.models.exercise import Exercise
from app.schemas.scenario_json import InjectNode, TriggerComm
from app.services.llm_service import AssessmentOutput


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


async def test_help_page_scenario_example_matches_the_real_schema(
    client: AsyncClient, facilitator_token: str
):
    """The in-app help's JSON example must parse as the schema it claims to document.

    It previously used `delay_seconds` and a `visible_to_teams` key that TriggerComm does
    not define. Pydantic ignores unknown keys, so a copied example validated fine and then
    silently delivered with a 0-second delay and no team scoping — no error anywhere. An
    unknown-key check, not just a validity check, is what catches that class of bug.
    """
    client.cookies.set("access_token", facilitator_token)
    r = await client.get("/help")
    assert r.status_code == 200

    examples = [
        json.loads(html.unescape(block))
        for block in re.findall(r"<pre[^>]*>(\{.*?\})</pre>", r.text, re.S)
        if "triggers_communications" in block
    ]
    assert examples, "help page no longer shows an inject example with a triggered comm"

    for example in examples:
        node = InjectNode.model_validate(example)
        assert not set(example) - set(InjectNode.model_fields)
        for authored, parsed in zip(
            example["triggers_communications"], node.triggers_communications, strict=True
        ):
            assert not set(authored) - set(TriggerComm.model_fields)
            # The authored delay must survive parsing, not be silently dropped to 0.
            assert parsed.delay_after_release_seconds == authored["delay_after_release_seconds"]

    # Trigger direction changes participant visibility: inbound trigger records have
    # all-team visibility, while outbound records have neither a participant sender nor
    # recipient-team scope and are therefore facilitator-visible only.
    assert "visible to all teams" in r.text
    assert "facilitator-visible" in r.text
    assert re.search(r"inbound.*visible to all teams", r.text, re.S | re.I)
    assert re.search(r"outbound.*facilitator-visible", r.text, re.S | re.I)


async def test_help_page_ai_guidance_matches_the_code(
    client: AsyncClient, facilitator_token: str
):
    """The help page must not name one hardcoded provider, or invent rating values.

    It previously said Claude did the assessing, told operators to set ANTHROPIC_API_KEY,
    and listed the ratings as "strong / acceptable / poor" — two of which have never
    existed. Both facts are derived from the code here, so this fails if either the page
    or AssessmentOutput/LLM_PROVIDER_KEYS drifts again.
    """
    client.cookies.set("access_token", facilitator_token)
    r = await client.get("/help")
    assert r.status_code == 200
    text = r.text

    annotation = AssessmentOutput.model_fields["decision_quality"].annotation
    literal = next(arg for arg in get_args(annotation) if arg is not type(None))
    ratings = get_args(literal)
    assert ratings, "decision_quality is no longer a Literal — update this test"

    for rating in ratings:
        assert f"<em>{rating}</em>" in text, f"help page omits the '{rating}' rating"
    for invented in ("strong", "acceptable"):
        assert f"<em>{invented}</em>" not in text, f"help page invents a '{invented}' rating"

    # The provider is a server-side choice, not a single hardcoded vendor key.
    assert "LLM_PROVIDER" in text
    assert "ANTHROPIC_API_KEY" not in text
    for provider in LLM_PROVIDER_KEYS:
        assert provider.lower() in text.lower(), f"help page omits the {provider} provider"


async def test_authenticated_user_can_load_communications_hub(
    client: AsyncClient, participant_token: str
):
    client.cookies.set("access_token", participant_token)
    r = await client.get("/communications")
    assert r.status_code == 200
    assert "No active exercise selected" in r.text


async def test_participant_response_form_explains_required_fields(
    client: AsyncClient, participant_token: str
):
    client.cookies.set("access_token", participant_token)
    r = await client.get("/exercises/1/participate")
    assert r.status_code == 200
    assert 'x-if="hasOptions(inj)"' in r.text
    assert 'x-if="requiresFreeText(inj)"' in r.text
    assert "No written response is required." in r.text
    assert 'x-text="responseValidationMessage(inj)"' in r.text


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
