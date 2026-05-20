# Deep Thought

## Overview
Deep Thought is an application for running tabletop exercises with a focus on cyber scenarios (though can be used for other scenarios such as business resilience). User roles: facilitator, participant, observer. Participants may have a `team` field for complex organisational structures. The facilitator releases injects to participants; participants enter responses. Scenarios are defined in JSON with branching logic based on participant decisions. The app simulates incident communications (e.g. to regulatory bodies).

See [PLAN.md](PLAN.md) for the full architecture and build phases.

---

## Tech Stack

API-first architecture.

- **Backend**: Python >= 3.14, FastAPI, SQLModel, SQLite
- **Frontend**: Jinja2 templates (served by FastAPI) + Tailwind CSS v4 (CLI-compiled) + AlpineJS
- **Auth**: JWT tokens (python-jose) stored in httpOnly cookie + localStorage
- **Real-time**: WebSockets (FastAPI native)
- **Testing**: Pytest

## Key Architectural Decisions

**Database**: SQLite for local development; PostgreSQL for containerized deployments. Engine creation in `database.py` is conditionalized — `connect_args={"check_same_thread": False}` is only passed for SQLite; Postgres gets `pool_pre_ping=True` instead. `create_all()` on startup handles both fresh SQLite and fresh Postgres schemas. No Alembic yet — schema changes to an existing production DB must be applied manually until Alembic is added.

**Password hashing**: Uses `bcrypt` directly (not `passlib`). `passlib[bcrypt]` is incompatible with Python 3.14 due to a `bcrypt.__about__` removal in bcrypt 4.x.

**Scenario storage**: The full scenario definition (inject tree, branching options, team targets, triggered communications) is stored as a single JSON blob in `Scenario.definition`. Validated against the `ScenarioDefinition` Pydantic model on every write. Not normalised into rows — the tree is always read and written as a unit.

**Scenario branching**: "Pull not push" — when a participant responds, the service resolves which inject IDs are valid next steps, but the facilitator manually reviews and releases the chosen branch. This keeps human control in the loop.

**JWT auth**: Stored in both an `httpOnly` cookie (for page navigation/Jinja2 routes) and `localStorage` (for Alpine/fetch calls). The `get_current_user` FastAPI dependency checks both; the `Authorization` header takes precedence.

**WebSocket auth**: JWT passed as `?token=<jwt>` query parameter. Browsers cannot set the `Authorization` header on WebSocket upgrade requests.

**LLM integration**: Uses `anthropic>=0.40` (`AsyncAnthropic`). Prompt caching applied via the `anthropic-beta: prompt-caching-2024-07-31` header — scenario context + inject content is the cached prefix; participant response text is the non-cached suffix. `run_llm_pipeline` is a background async task (fired via `asyncio.create_task`) that opens its own Session from `engine` directly (same pattern as `_delayed_comm`). The `Exercise.llm_enabled` flag gates LLM calls per exercise. Set `ANTHROPIC_API_KEY` in `.env` to enable. All API calls are mocked in `tests/test_llm.py` — no real network requests.

**Tailwind**: CDN during development (Phases 1–6); switched to CLI-compiled `static/css/output.css` in Phase 7. Rebuild after template changes: `tailwindcss -i static/css/input.css -o static/css/output.css`. Tailwind v4 auto-scans `app/templates/**/*.html` — no config file needed.

**Frontend design system**: Warm stone palette with CSS custom properties (`--bg`, `--paper`, `--ink`, `--accent`, etc.) defined in `base.html`. IBM Plex Sans + IBM Plex Mono via Google Fonts. Custom utility classes (`.smallcaps`, `.mono`, `.pill`, `.btn-*`, `.node`, `.briefing`, `.live-dot`) live in the `<style>` block of `base.html` — not Tailwind utilities. Persistent dark sidebar (`w-56`, `position: sticky`) replaces the old top nav bar. Sidebar hides itself on auth pages via `x-show="!!user"` (no token → `user: null`).

**Communications delay**: `triggers_communications` in the scenario JSON fire via `asyncio.create_task(asyncio.sleep(...))` — sufficient for single-process. Would need a task queue (e.g. Celery, ARQ) for multi-process deployment.

**Group-scoped injects**: `Inject.group_id` and `ExerciseMember.group_id` allow injects to be targeted at specific exercise groups (teams). When `group_id` is `None` the inject is visible to all groups. The inject router resolves group membership via `exercise_group_for_user()` at query time.

**File attachments on injects**: Injects support a single file attachment (`attachment_filename`, `attachment_path`, `attachment_content_type`, `attachment_size` on the `Inject` model). Files are stored under `uploads/inject_attachments/{exercise_id}/`. The inject router accepts `multipart/form-data`; `inject_attachment_payload()` builds the download URL returned in the inject payload.

**Role preview**: Facilitators can view the app as a participant or observer via `dt_view_role` and `dt_view_team` cookies (set from `/settings`). `_optional_user()` in `ui.py` reads these cookies and overrides the Jinja2 template role/team — but only when the JWT already contains the `facilitator` role, so API calls are never downgraded.

**Dark mode**: Full CSS custom-property theming system in `base.html`. `data-theme="dark"` on `<html>` switches the entire palette. A short inline `<script>` at the top of `<head>` applies the saved theme before first paint (prevents FOUC). User preference stored in `dt_theme`/`dt_resolved_theme`/`dt_accent` cookies and `localStorage`. Toggled from `/settings`.

**Sample scenarios**: `app/samples/` contains bundled JSON scenario definitions (`ransomware_response.json`, `vendor_outage.json`). `app/services/sample_service.py` lists, validates, and loads them. The settings page exposes a sample loader UI for facilitators.

**Containerized deployment**: `Dockerfile` is a two-stage build — stage 1 compiles Tailwind CSS (`pytailwindcss`), stage 2 is the Python runtime. The compiled `static/` directory is also copied to `static_src/` in the image; this path is never overridden by a volume mount and is used by entrypoint scripts (Docker Compose) and init containers (k8s) to populate shared static volumes so nginx always serves the version matching the running image. `docker-compose.yml` runs `app` + `postgres:17` + `nginx:alpine` on a private bridge network with named volumes for DB data, uploads, and static files. `k8s/` contains namespace, secrets, configmap, postgres StatefulSet, app Deployment, and nginx Deployment manifests. **Replica constraint**: `ws_manager.py` is in-memory only — app must run as a single replica until Redis pub/sub is added. k8s manifests enforce `replicas: 1` and `strategy: Recreate`. Install the `postgres` optional dep group (`pip install -e ".[postgres]"`) to get `psycopg2-binary`; the Dockerfile uses this extra. A minimal `GET /api/health` endpoint (`app/routers/health.py`) is used by k8s liveness/readiness probes.

---

## Project Structure

```
app/
├── main.py          # App factory + lifespan (create_db_and_tables)
├── config.py        # Settings via pydantic-settings (.env)
├── database.py      # SQLite engine, get_session dependency
├── dependencies.py  # get_current_user, require_role()
├── models/          # SQLModel table definitions
├── schemas/         # Pydantic schemas (auth, scenario_json)
├── routers/         # One router per resource + ui.py for Jinja2 pages
├── services/        # Business logic (auth, scenario, exercise, inject, response, comms, llm, ws_manager)
└── templates/       # Jinja2 HTML
    │   base.html            # CSS vars, sidebar layout, sidebarNav() Alpine component, shared JS helpers
    │   dashboard.html       # Command center (live exercise hero card + exercises/scenarios lists)
    │   help.html            # In-app help & scenario JSON documentation
    │   auth/login.html      # Centered card, no sidebar
    │   auth/register.html
    │   scenarios/list.html  # Table grid layout
    │   scenarios/detail.html # Depth-first inject tree + validation sidebar
    │   scenarios/editor.html # Inject tree editor
    │   exercises/list.html  # Exercise list + create modal
    │   exercises/facilitator.html  # Full-height 3-pane console
    │   exercises/participant.html  # Briefing cards (760px centered)
    │   communications/inbox.html   # 340px list + reader pane
    │   communications/index.html  # /communications hub (redirects to active exercise)
    │   settings.html              # Dark mode toggle, role preview, sample scenario loader
tests/               # Pytest suite (conftest.py + one file per resource)
static/css/          # output.css (Tailwind compiled, Phase 7+)
app/samples/         # Bundled scenario JSON definitions (ransomware_response, vendor_outage)
Dockerfile           # Multi-stage: Tailwind build → Python runtime (non-root, static_src/ trick)
docker-compose.yml   # app + postgres:17 + nginx:alpine; named volumes for DB, uploads, static
docker/nginx.conf    # Reverse proxy config with WebSocket upgrade support
k8s/                 # Kubernetes manifests (namespace, secrets, postgres, app, nginx)
revised/             # Claude Design prototype (static reference, not served)
```

---

## Build Status

| Phase | Status |
|-------|--------|
| 1 — Foundation (auth, config, DB) | ✅ Complete |
| 2 — Scenarios (CRUD, import/export, branching) | ✅ Complete |
| 3 — Exercises + Members | ✅ Complete |
| 4 — Injects + WebSocket | ✅ Complete |
| 5 — Responses + Branching | ✅ Complete |
| 6 — Communications | ✅ Complete |
| 7 — Polish (Tailwind CLI, CI, exports) | ✅ Complete |
| 8 — LLM Integration (Claude API) | ✅ Complete |
| 9 — UI Redesign (warm stone palette, sidebar, IBM Plex) | ✅ Complete |
| 10 — Expected actions on injects (schema + editor + LLM rubric) | ✅ Complete |
| 11 — Participant communications (outbound send from inbox) | ✅ Complete |
| 12 — Group-scoped injects + file attachments | ✅ Complete |
| 13 — Dark mode + role preview + settings page + sample scenarios | ✅ Complete |
| 14 — Containerized deployment (Docker Compose + Kubernetes + Postgres + nginx) | ✅ Complete |

Current test count: **167 passing**.

---

## Development Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # set SECRET_KEY
uvicorn app.main:app --reload
```

Run tests: `pytest`

---

## Testing
Write tests for critical functionality in Pytest. Use `TestClient` (synchronous) with in-memory SQLite. One test file per resource. Fixtures in `tests/conftest.py`.

## Maintenance
Keep README.md, CLAUDE.md, and PLAN.md up to date. Update the Build Status table above when phases complete. Record any significant dependency or architectural decisions here.
