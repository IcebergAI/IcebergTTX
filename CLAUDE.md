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

**Linear inject flows**: In addition to per-option `next_inject_id` branching, an `InjectNode` may set a node-level `next_inject_id` (in `scenario_json.py`) to chain to the next inject without requiring a participant decision — i.e. a straight-line sequence. The `ScenarioDefinition` validator checks the referenced ID exists, and `_check_no_cycles()` includes node-level `next_inject_id` edges in its adjacency graph so linear chains can't form a cycle.

**Inject comment threads**: Participants can post team-scoped discussion comments on released injects (`InjectComment` model; `app/routers/inject_comments.py` under `/api/exercises/{exercise_id}/inject-comments`). Comment visibility is group-scoped via `comment_group_for_user()` in `inject_comment_service.py` (resolved against `Inject.group_id` / the participant's exercise group / team); facilitators and observers see all comments, participants only their own group's. Only participants may post, only while the exercise is `active` and the inject is `released`/`resolved`. New comments broadcast over WebSocket (`inject_comment_created`), group-scoped when the comment has a `group_id`. Access checks live in the shared `app/services/access_control.py`.

**JWT auth**: Stored in both an `httpOnly` cookie (for page navigation/Jinja2 routes) and `localStorage` (for Alpine/fetch calls). The `get_current_user` FastAPI dependency checks both; the `Authorization` header takes precedence.

**WebSocket auth**: JWT passed as `?token=<jwt>` query parameter. Browsers cannot set the `Authorization` header on WebSocket upgrade requests.

**Startup secret validation**: `validate_settings()` (`config.py`) is called from the lifespan startup and aborts the app if `secret_key` is unset, equal to the well-known default, or shorter than 32 chars — unless `DEV_MODE=true`. This prevents silently signing JWTs with a publicly-known key (#9). `dev_mode` also relaxes the Secure-cookie requirement for local HTTP. Tests set `DEV_MODE=true` in `conftest.py` before app import.

**Self-registration is participant-only**: `RegisterRequest` no longer accepts `role` — `POST /api/auth/register` always creates a `participant` (#8). Privileged roles are assigned out-of-band (seeded or via a future admin endpoint); extra body fields are ignored by pydantic. The register template no longer offers a role selector.

**Cookie security & CSRF**: the auth cookie is set with `Secure` (gated on `settings.cookies_secure`, default `not dev_mode`; override with `COOKIE_SECURE`). `CSRFOriginMiddleware` (`app/middleware.py`) verifies `Origin`/`Referer` for cookie-authenticated state-changing requests under `/api/` (#10). Bearer-`Authorization` requests and `/api/auth/*` are exempt — the app's own fetch calls use the localStorage Bearer token, so the cookie is effectively navigation-only. Extra allowed origins via `TRUSTED_ORIGINS`.

**Login brute-force protection**: `app/services/rate_limit.py` is an in-memory sliding-window limiter keyed by `ip:email` (honouring `X-Forwarded-For`). After `LOGIN_MAX_ATTEMPTS` (default 5) failures within `LOGIN_LOCKOUT_SECONDS` (default 300) the login route returns `429` with `Retry-After`; a success resets the counter (#11). In-memory ⇒ single-process only (same constraint as `ws_manager`). Tests clear it via an autouse fixture.

**Audit logging**: `app/services/audit_service.py` emits structured JSON audit events to the `deep_thought.audit` logger (always) and, when `AUDIT_PERSIST` is set, to the append-only `AuditEvent` table (#23). `emit()` never raises (logging must not break a request) and sanitises all free-text against log injection (CR/LF/control chars). `AuditContextMiddleware` populates per-request "where" fields (request id, source IP, method, path) via a `ContextVar`; pass `actor=user` so the **actual** identity is logged even under facilitator role-preview. Wired at: login success/failure/lockout, register, logout, password change, token-validation failures, `authz.denied` (role + exercise access), inject release/delete, exercise lifecycle, member enrol/remove/group-change, exports, CSRF blocks, app startup/shutdown, and unhandled 500s. Secrets and payload bodies are never logged. **Not yet covered** (P2 follow-up): SIEM shipping.

**Facilitator trust boundary (#12, open/P2)**: any `facilitator` is currently a global super-admin over every exercise/scenario/export — there is no per-resource ownership scoping. This is a known, documented trust boundary (single trusted facilitator team), deferred as a P2 follow-up; `Exercise.created_by` already exists to support ownership checks when implemented.

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
├── main.py          # App factory + lifespan (validate_settings, create_db_and_tables, middleware)
├── config.py        # Settings via pydantic-settings (.env) + validate_settings() startup guard
├── middleware.py    # AuditContextMiddleware + CSRFOriginMiddleware (ASGI)
├── database.py      # SQLite engine, get_session dependency
├── dependencies.py  # get_current_user, require_role()
├── models/          # SQLModel table definitions (incl. audit.AuditEvent)
├── schemas/         # Pydantic schemas (auth, scenario_json)
├── routers/         # One router per resource + ui.py for Jinja2 pages
├── services/        # Business logic (auth, scenario, exercise, inject, inject_comment, response, comms, llm, ws_manager, access_control, audit_service, rate_limit)
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
| 15 — Linear inject flows + team comment threads | ✅ Complete |
| 16 — Security hardening (P0/P1: #8 reg roles, #9 secret validation, #10 cookie/CSRF, #11 login rate limit, #23 audit logging) | ✅ Complete |

Current test count: **193 passing** (1 skipped).

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
