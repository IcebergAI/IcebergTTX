#!/usr/bin/env python3
"""Regenerate the README and website screenshots against a running IcebergTTX.

The images in docs/ and website/docs/assets/ show the app shell, so *any* change
to the rail, topbar or a captured screen makes all of them stale at once. Doing
that by hand is 13 captures across two viewports, two themes and several seeded
states — which is why they rot. This script makes it one command.

    # a stack you don't mind seeding demo data into
    docker compose up -d --build
    uv run python scripts/screenshots.py --base https://localhost --insecure

It seeds its own scenario, an active exercise (with a released inject and a
participant response) and a completed one for the review/report pages, then
captures every image at the size the docs already use.

Requires: playwright (`uv sync --extra dev && uv run playwright install chromium`).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "docs"
SITE = REPO / "website" / "docs" / "assets"

PW = "ScreenshotDemo!2026"
FACILITATOR = {"email": "alex.rivera@example.com", "password": PW}

# Sam answers the opening inject — that's the response card on the console, and
# what makes "Containment Window" the recommended next branch.
PARTICIPANT = {"email": "sam.okafor@example.com", "password": PW}

# Maria is also on it_ops but has *not* answered, so the participant view has an
# open brief with a decision form in it. It can't be Sam: once he picks 'isolate'
# the it_ops cursor moves on, and the app (correctly) refuses to release any other
# branch to that team — "Inject is not the current branch for its group".
VIEWER = {"email": "maria.chen@example.com", "password": PW}

# Filler, purely so the ops pane shows more than one team scent.
EXTRAS = [
    ("dan.price@example.com", "Dan Price", "exec"),
    ("priya.nair@example.com", "Priya Nair", "legal"),
]

# The docs set is retina full-page; the website-only set is a 1x viewport crop.
# Matches the dimensions of the images already committed.
RETINA = {"width": 1440, "height": 960, "scale": 2, "full_page": True}
FLAT = {"width": 1440, "height": 900, "scale": 1, "full_page": False}


# Per-shot staging: put the page into the state the caption promises before the
# capture. Named so each Shot can point at its own, instead of a name-keyed
# if/elif chain that has to be kept in sync with the SHOTS list.
def _stage_open_first_comm(page: Page) -> None:
    page.locator(".comm-list-row").first.click()


def _stage_open_samples(page: Page) -> None:
    page.get_by_role("button", name="Sample data").click()


def _stage_create_exercise(page: Page) -> None:
    # Fill it in — the caption promises "creating an exercise from a scenario",
    # and an empty form shows neither.
    page.get_by_role("button", name="New exercise").first.click()
    page.wait_for_timeout(400)
    page.locator("#create-exercise-title-input").fill("Q4 Ransomware Tabletop")
    page.locator("#create-exercise-scenario").select_option(index=1)


def _stage_show_ops_panel(page: Page) -> None:
    # The ops panel is off by default; the console shots read better with the
    # three panes the layout is actually built around.
    page.get_by_role("button", name="Ops panel").click()


@dataclass
class Shot:
    name: str
    profile: dict
    # A route template formatted against the seeded id map, e.g.
    # "/exercises/{exercise_id}/facilitate". Static routes format to themselves.
    route: str
    theme: str = "light"
    as_participant: bool = False
    # Optional page setup run after navigation, before the capture.
    stage: Callable[[Page], None] | None = None
    # Extra destinations beyond the primary one; docs/ and the website keep
    # byte-identical copies of the five images they share.
    also: list[Path] = field(default_factory=list)
    dest: Path = DOCS


SHOTS: list[Shot] = [
    # README hero + the light/dark pair of the console.
    Shot("screenshot", RETINA, "/exercises/{exercise_id}/facilitate", stage=_stage_show_ops_panel),
    Shot("facilitator-dark", RETINA, "/exercises/{exercise_id}/facilitate",
         theme="dark", stage=_stage_show_ops_panel, also=[SITE]),
    Shot("dashboard", RETINA, "/dashboard", also=[SITE]),
    Shot("scenarios", RETINA, "/scenarios"),
    Shot("scenario-detail", RETINA, "/scenarios/{scenario_id}", also=[SITE]),
    Shot("communications", RETINA, "/exercises/{exercise_id}/communications",
         stage=_stage_open_first_comm, also=[SITE]),
    Shot("participant", RETINA, "/exercises/{viewer_exercise_id}/participate",
         as_participant=True, also=[SITE]),
    Shot("settings", RETINA, "/settings"),
    # Website-only, viewport-cropped.
    Shot("settings-samples", FLAT, "/settings", stage=_stage_open_samples, dest=SITE),
    Shot("exercise-create", FLAT, "/exercises", stage=_stage_create_exercise, dest=SITE),
    Shot("inject-release", FLAT, "/exercises/{exercise_id}/facilitate",
         stage=_stage_show_ops_panel, dest=SITE),
    Shot("review-timeline", FLAT, "/exercises/{completed_id}/review", dest=SITE),
    Shot("report", FLAT, "/exercises/{completed_id}/report", dest=SITE),
]


class Api:
    """Thin authenticated API client over Playwright's request context."""

    def __init__(self, ctx, base: str) -> None:
        self.ctx, self.base, self.token = ctx, base, ""

    def login(self, creds: dict) -> None:
        r = self.ctx.request.post(f"{self.base}/api/auth/login", data=creds)
        if not r.ok:
            raise SystemExit(f"login failed for {creds['email']}: {r.status} {r.text()}")
        self.token = r.json()["access_token"]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Origin": self.base}

    def get(self, path: str):
        return self.ctx.request.get(f"{self.base}/api{path}", headers=self._headers())

    def post(self, path: str, data=None):
        return self.ctx.request.post(
            f"{self.base}/api{path}", data=data if data is not None else {}, headers=self._headers()
        )

    def put(self, path: str, data=None):
        return self.ctx.request.put(
            f"{self.base}/api{path}", data=data if data is not None else {}, headers=self._headers()
        )


def new_demo_exercise(api: Api) -> int:
    """POST the ransomware demo-exercise sample and return the new exercise id.

    The endpoint returns the created exercise inline; some response shapes omit it,
    so fall back to the newest exercise id. One place, so a change to the response
    shape is a one-line edit instead of three.
    """
    result = api.post("/settings/samples/scenarios/ransomware_response/demo-exercise").json()
    if "exercise" in result:
        return result["exercise"]["id"]
    return max(e["id"] for e in api.get("/exercises").json())


def seed(ctx, base: str) -> dict:
    """Create the demo data the screenshots depend on. Idempotent enough to re-run."""
    api = Api(ctx, base)

    # Registration only ever creates participants; the facilitator must already
    # exist and be promoted (see .claude/skills/run-app). Fail loudly if not.
    api.login(FACILITATOR)
    me = api.get("/auth/me").json()
    if me.get("role") != "facilitator":
        raise SystemExit(
            f"{FACILITATOR['email']} is not a facilitator. Promote it first:\n"
            '  docker exec deep_thought-db-1 psql -U iceberg_ttx -d iceberg_ttx -c '
            f"\"UPDATE \\\"user\\\" SET role='facilitator', is_admin=true "
            f"WHERE email='{FACILITATOR['email']}'\""
        )

    demo = api.post("/settings/samples/scenarios/ransomware_response/demo-exercise").json()
    scenario_id = demo["scenario"]["id"]
    exercise_id = next(
        e["id"] for e in api.get("/exercises").json() if e["state"] == "active"
    )

    def enrol(ex: int, creds_email: str, name: str, team: str) -> int:
        ctx.request.post(
            f"{base}/api/auth/register",
            data={"email": creds_email, "display_name": name, "password": PW, "team": team},
        )
        member = Api(ctx, base)
        member.login({"email": creds_email, "password": PW})
        uid = member.get("/auth/me").json()["id"]
        api.post(f"/exercises/{ex}/members", {"user_id": uid, "group_id": team})
        return uid

    participant_id = enrol(exercise_id, PARTICIPANT["email"], "Sam Okafor", "it_ops")
    for email, name, team in EXTRAS:
        enrol(exercise_id, email, name, team)

    p = Api(ctx, base)
    p.login(PARTICIPANT)

    # A response against the released inject: this is what lights up the
    # "Recommended next" inject and the Suggested-next chip.
    released = [i for i in api.get(f"/exercises/{exercise_id}/injects").json()
                if i["state"] == "released"]
    if released:
        # Same opening move Sam plays in the played-out exercise, so the console
        # and report screenshots quote him identically — kept in PLAYBOOK, not
        # restated here.
        option, content = PLAYBOOK["initial_alert"]
        p.post(
            f"/exercises/{exercise_id}/responses",
            {"inject_id": released[0]["id"], "selected_option": option, "content": content},
        )

    # No extra release here: Containment Window has to stay *pending* for the
    # console to render it as the recommended next branch.

    # Inbound comms for the inbox.
    for entity, subject, body in [
        (
            "Data Protection Regulator",
            "Confirm whether personal data was affected",
            "We have received notice of a potential incident. Please confirm within 72 hours "
            "whether personal data has been compromised and outline your containment measures.",
        ),
        (
            "Press — The Daily Ledger",
            "Request for comment on reported outage",
            "Our readers report your customer portal is down. Can you confirm whether this is a "
            "security incident, and do you have a statement on customer data?",
        ),
    ]:
        api.post(
            f"/exercises/{exercise_id}/communications/inject",
            {"external_entity": entity, "subject": subject, "body": body},
        )

    # A second exercise, played through to completion — review/report are only
    # worth a screenshot if they have a real timeline in them. Completing a
    # freshly-seeded exercise gives "Duration 0m / no responses recorded".
    done_id = new_demo_exercise(api)
    play_out(api, p, participant_id, done_id)

    # A third, deliberately untouched exercise for the participant view. It needs a
    # released brief that *nobody* has answered, and a response resolves the inject
    # for the whole team — so it can't be the exercise Sam already responded in, no
    # matter which member we log in as.
    viewer_ex = new_demo_exercise(api)
    api.put(f"/exercises/{viewer_ex}", {"title": "IT Ops Dry Run"})
    enrol(viewer_ex, VIEWER["email"], "Maria Chen", "it_ops")

    return {
        "scenario_id": scenario_id,
        "exercise_id": exercise_id,
        "completed_id": done_id,
        "viewer_exercise_id": viewer_ex,
        "participant_id": participant_id,
    }


# Decision + narrative per scenario node, so the played-out exercise reads like a
# real one. Keyed by the sample scenario's node ids.
PLAYBOOK = {
    "initial_alert": (
        "isolate",
        "Isolated the three affected workstations from the network and preserved memory images "
        "for forensics. Escalated to the incident commander and opened a bridge with IT Ops and "
        "Legal.",
    ),
    "containment": (
        "restore",
        "Rebuilding the affected hosts from last night's verified backup rather than paying. "
        "Restore is under way; we expect the first systems back within two hours.",
    ),
    "spread": (
        "crisis",
        "Declared a full crisis. Standing up the incident bridge and notifying the executive "
        "team, Legal and Communications simultaneously.",
    ),
}


def play_out(api: Api, p: Api, participant_id: int, exercise_id: int) -> None:
    """Run an exercise through to completion, so review/report have a real timeline.

    Completing a freshly-seeded exercise leaves the report reading "Duration 0m /
    Participants 0 / No responses recorded" — technically accurate and useless as
    a screenshot.
    """
    api.put(f"/exercises/{exercise_id}", {"title": "Q3 Ransomware Tabletop"})
    api.post(f"/exercises/{exercise_id}/members", {"user_id": participant_id, "group_id": "it_ops"})

    # Respond to whatever is live, release whatever that unlocks, repeat. The
    # participant is on it_ops, so branches aimed at other teams simply stall —
    # which is the natural end of the walk.
    for _ in range(4):
        acted = False
        for inj in api.get(f"/exercises/{exercise_id}/injects").json():
            if inj["state"] != "released":
                continue
            move = PLAYBOOK.get(inj.get("scenario_node_id"))
            if not move:
                continue
            option, content = move
            r = p.post(
                f"/exercises/{exercise_id}/responses",
                {"inject_id": inj["id"], "selected_option": option, "content": content},
            )
            acted = acted or r.ok

        # The branch targets come off the *listed* responses — POST /responses
        # returns the created row without them, so reading them from its reply
        # silently releases nothing.
        pending = {i["id"] for i in api.get(f"/exercises/{exercise_id}/injects").json()
                   if i["state"] == "pending"}
        released_any = False
        for resp in api.get(f"/exercises/{exercise_id}/responses").json():
            for nxt in (resp.get("next_injects") or []):
                if nxt["id"] in pending:
                    api.post(f"/exercises/{exercise_id}/injects/{nxt['id']}/release")
                    released_any = True
        if not acted and not released_any:
            break

    api.post(
        f"/exercises/{exercise_id}/communications/inject",
        {
            "external_entity": "Data Protection Regulator",
            "subject": "Confirm whether personal data was affected",
            "body": "We have received notice of a potential incident. Please confirm within 72 "
                    "hours whether personal data has been compromised.",
        },
    )
    api.post(f"/exercises/{exercise_id}/complete")
    # debrief_notes stays editable after completion (#112), which is the point of
    # the review page.
    api.put(
        f"/exercises/{exercise_id}",
        {
            "debrief_notes": "Containment decision was quick and well-evidenced. Regulator "
                             "notification lagged the 72-hour clock — agree a named owner for "
                             "the notification call before the next run.",
        },
    )


def backdate(exercise_ids: list[int], minutes: int) -> None:
    """Age the exercise clocks so they don't read '0:05 elapsed' / 'Duration 0m'.

    Cosmetic, and the only thing here that reaches past the API — there's no
    endpoint to move started_at, and a hero shot of a live exercise that began
    five seconds ago undersells the screen. Skipped with a warning if the compose
    db isn't reachable.
    """
    ids = ", ".join(str(i) for i in exercise_ids)
    sql = (
        f"UPDATE exercise SET started_at = started_at - interval '{minutes} minutes' "
        f"WHERE id IN ({ids})"
    )
    cmd = ["docker", "compose", "exec", "-T", "db", "psql", "-U", "iceberg_ttx",
           "-d", "iceberg_ttx", "-c", sql]
    result = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ! could not backdate the clocks ({result.stderr.strip()[:80]}); continuing")
    else:
        print(f"  backdated exercises {ids} by {minutes}m")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://localhost")
    ap.add_argument("--insecure", action="store_true", help="accept a self-signed cert")
    ap.add_argument("--only", help="capture just this shot (by name)")
    ap.add_argument("--backdate", type=int, default=9,
                    help="age the live exercise clock by N minutes (0 to skip)")
    args = ap.parse_args()

    shots = [s for s in SHOTS if not args.only or s.name == args.only]
    if not shots:
        raise SystemExit(f"no shot named {args.only!r}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        seed_ctx = browser.new_context(ignore_https_errors=args.insecure)
        ids = seed(seed_ctx, args.base)
        seed_ctx.close()
        print(f"seeded: {ids}")
        if args.backdate:
            backdate([ids["exercise_id"], ids["completed_id"]], args.backdate)

        for shot in shots:
            p = shot.profile
            ctx = browser.new_context(
                viewport={"width": p["width"], "height": p["height"]},
                device_scale_factor=p["scale"],
                ignore_https_errors=args.insecure,
            )
            creds = VIEWER if shot.as_participant else FACILITATOR
            api = Api(ctx, args.base)
            api.login(creds)
            # Pin which exercise the rail's "Now live" card points at. Two are
            # active, so without this the rail can highlight a different exercise
            # from the one on screen — the shell contradicting the page.
            current = ids["viewer_exercise_id"] if shot.as_participant else ids["exercise_id"]
            ctx.add_init_script(
                f"localStorage.setItem('dt_token', {api.token!r});"
                f"localStorage.setItem('dt_theme', {shot.theme!r});"
                f"document.cookie = 'dt_resolved_theme={shot.theme};path=/';"
                f"document.cookie = 'dt_current_exercise={current};path=/';"
            )

            page = ctx.new_page()
            route = shot.route.format(**ids)
            resp = page.goto(args.base + route, wait_until="networkidle")
            if resp is None or resp.status != 200:
                raise SystemExit(f"{shot.name}: {route} -> {resp and resp.status}")
            page.wait_for_timeout(1200)
            if shot.stage:
                shot.stage(page)
            page.wait_for_timeout(900)

            out = shot.dest / f"{shot.name}.png"
            page.screenshot(path=out, full_page=p["full_page"])
            for extra in shot.also:
                shutil.copyfile(out, extra / f"{shot.name}.png")
            print(f"  {out.relative_to(REPO)}" + "".join(
                f" + {(e / f'{shot.name}.png').relative_to(REPO)}" for e in shot.also
            ))
            ctx.close()

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
