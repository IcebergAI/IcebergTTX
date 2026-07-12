# IcebergTTX

## Overview
IcebergTTX is an application for running tabletop exercises with a focus on cyber scenarios (though can be used for other scenarios such as business resilience). User roles: facilitator, participant, observer. Participants may have a `team` field for complex organisational structures. The facilitator releases injects to participants; participants enter responses. Scenarios are defined in JSON with branching logic based on participant decisions. The app simulates incident communications (e.g. to regulatory bodies).

See [PLAN.md](PLAN.md) for the full architecture and build phases.

---

## Tech Stack

API-first architecture.

- **Backend**: Python >= 3.14, FastAPI (fully async), SQLModel + async SQLAlchemy, PostgreSQL (asyncpg)
- **Frontend**: Jinja2 templates (served by FastAPI) + Tailwind CSS v4 (CLI-compiled) + AlpineJS
- **Auth**: JWT tokens (python-jose) stored in httpOnly cookie + localStorage
- **Real-time**: WebSockets (FastAPI native)
- **Testing**: Pytest (`pytest-asyncio`, `httpx`/`httpx-ws`, `testcontainers` Postgres)

## Key Architectural Decisions

**Database (async, Postgres-only)**: PostgreSQL via the async `asyncpg` driver everywhere — dev, tests, and containers. `database.py` builds a single `create_async_engine` and exposes an `async def get_session()` yielding a SQLModel `AsyncSession` (`expire_on_commit=False`, so attributes stay populated after commit for response serialization). `make_async_url()` rewrites a plain `postgresql://` URL to `postgresql+asyncpg://`, so existing `DATABASE_URL` secrets in `docker-compose.yml`/`k8s` keep working unchanged.

**Schema migrations (Alembic, #19)**: Schema is managed by Alembic. `app/database.py` exposes `run_migrations()`, called from the async lifespan, which runs `alembic upgrade head` in a worker thread (`asyncio.to_thread` — Alembic's async `env.py` calls `asyncio.run`, which can't run inside the already-running lifespan loop). This self-migrates on startup, which suits the single-replica constraint; multi-replica rollouts should instead run `alembic upgrade head` as a dedicated deploy step. `alembic/env.py` derives the URL from `settings` via `make_async_url()` and uses `SQLModel.metadata` as the autogenerate target (importing every `app/models/*` module, same list as `app/main.py`). Create new migrations with `alembic revision --autogenerate -m "..."`; generated files under `alembic/versions/` are excluded from ruff. When hand-writing a migration instead (autogenerate needs a running Postgres), its `revision` id must be unique — the existing ids are similar rotated hex and a collision surfaces as a misleading `Cycle is detected in revisions` error, not a duplicate-id message; grep `alembic/versions/` for the id first. The test suite does **not** use Alembic — `tests/conftest.py` builds a throwaway schema with `metadata.create_all`, and `create_db_and_tables()` remains for that path. The `alembic.ini` + `alembic/` dir are copied into the Docker image so startup migration works in containers.

**Everything that touches the DB is async**: services and router handlers are `async def` and `await session.exec(...)/get/commit/refresh/delete` (`session.add` stays sync). Background tasks that open their own session — `run_llm_pipeline` (`llm_service.py`), `_delayed_comm` (`communication_service.py`) — use `async with AsyncSession(engine)`. The `anthropic` client was already `AsyncAnthropic`.

**Timezone-aware timestamps**: all `datetime` columns are declared with `Field(sa_type=DateTime(timezone=True))` so tz-aware UTC values (`datetime.now(UTC)`) map to Postgres `timestamptz`. Without this asyncpg rejects tz-aware values against a naive `TIMESTAMP` column.

**Foreign-key cascades**: Postgres enforces foreign keys natively (no per-connection pragma needed). Models declare SQLModel `Relationship(...)` with `back_populates` plus `cascade_delete=True` on the parent and `ondelete="CASCADE"`/`"SET NULL"` on the child FK (forward references are quoted strings under `TYPE_CHECKING`, with `UP037`/`UP045` ignored for `app/models/*` in `pyproject.toml`). Deleting an `Exercise` cascades to its injects/responses/members/communications/comments/suggested-injects; deleting an `Inject` cascades responses/comments and nulls `Communication.triggered_by_inject_id` (the comms record is preserved). Deleting a `Scenario` that is still referenced by an exercise is **not** cascaded — the delete route returns `409 Conflict` (`scenarios.py`).

**Password hashing**: Uses `bcrypt` directly (not `passlib`). `passlib[bcrypt]` is incompatible with Python 3.14 due to a `bcrypt.__about__` removal in bcrypt 4.x.

**Scenario storage**: The full scenario definition (inject tree, branching options, team targets, triggered communications) is stored as a single JSON blob in `Scenario.definition` — a validated `text` column (parsed via the `ScenarioDefinition` Pydantic model on every write, and memoised on read by `export_definition`), **not** JSONB. Not normalised into rows — the tree is always read and written as a unit.

**JSONB list columns**: the small list-valued columns — `Inject.target_teams`, `Communication.visible_to_teams`, `Scenario.tags`, `SuggestedInject.target_teams` — are native Postgres `JSONB` (`sa_column=Column(JSONB)`), so services read/write Python lists directly with no `json.dumps`/`loads`. `None` means "all teams". Communication read state is instead normalised into `CommunicationRead`: a per-user, per-communication receipt with an atomic PostgreSQL insert-on-conflict, so concurrent readers cannot overwrite each other. The one-off migration `a1f2c3d4e5b6` converts the legacy VARCHAR-JSON rows in place (`ALTER … TYPE jsonb USING col::jsonb`). The remaining `json.loads` sites are intentional: the `definition` blob, LLM model output, and inject-upload request parsing.

**Scenario branching**: "Pull not push" — when a participant responds, the service resolves which inject IDs are valid next steps, but the facilitator manually reviews and releases the chosen branch. This keeps human control in the loop.

**Response requirements (#130)**: An inject with options always requires a valid `selected_option` from that exact scenario node; it additionally requires non-blank `content` when `free_text_response` is true. Injects without options always require non-blank content. Option-only responses store an empty content string, and missing selections never expand to every possible branch.

**Durable exercise lifecycle (#129)**: Exercise state changes use a PostgreSQL compare-and-swap (`UPDATE … WHERE state = <observed> RETURNING`) so a stale/concurrent request gets `409` instead of overwriting a newer state. Every successful change appends an `ExerciseStateTransition` (previous/new state, actor, timestamp) in the **same transaction** as `Exercise.state`; this table, not optional audit persistence, is the authoritative timeline source. The router emits audit/SIEM and the canonical `exercise_state_change` WebSocket frame only after commit. Failed/rolled-back transitions emit neither projection. The migration backfills exact events from persisted lifecycle audit rows and falls back to `started_at`/`ended_at` for legacy deployments where audit persistence was disabled.

**Linear inject flows**: In addition to per-option `next_inject_id` branching, an `InjectNode` may set a node-level `next_inject_id` (in `scenario_json.py`) to chain to the next inject without requiring a participant decision — i.e. a straight-line sequence. The `ScenarioDefinition` validator checks the referenced ID exists, and `_check_no_cycles()` includes node-level `next_inject_id` edges in its adjacency graph so linear chains can't form a cycle.

**JWT auth**: Stored in both an `httpOnly` cookie (for page navigation/Jinja2 routes) and `localStorage` (for Alpine/fetch calls). The `get_current_user` FastAPI dependency checks both; the `Authorization` header takes precedence.

**Email / SMTP (#117)**: Feature-flagged on `settings.smtp_enabled` (`bool(SMTP_HOST and SMTP_FROM)`) — the email-dependent endpoints 404 and their UI entry points hide when unset. `mail_service.send` is a best-effort async `aiosmtplib` call (no-op/logged-not-raised when off) fired via `background.spawn` off the response path; it's a raw socket so it goes **direct**, NOT through the httpx proxy (#97), like the syslog sink. Self-service password reset uses `AuthToken` rows (single-use, expiring, SHA-256-hashed at rest — only the raw token is emailed; `token_service.generate/create/consume`). Reset-complete reuses the #66 set-password + `token_valid_after` revoke pattern. No enumeration: the request endpoint always 200s and mails off-path (constant time). **Participant invites** reuse the same `AuthToken`/mailer with `purpose=invite`: `POST /api/users/invite` (admin) pre-binds email/team/exercise into the token and works while `registration_enabled` is off; `POST /api/auth/invite/accept` creates a participant (email taken from the token, never the client), auto-enrols in the bound exercise, and logs in. `mail_service.build_link` builds absolute links for both flows.

**Single-replica constraint**: `ws_manager`, `rate_limit`, and the `SiemConfig`/`ProxyConfig` caches are all in-memory — the app must run as one replica until Redis pub/sub is added (k8s enforces `replicas: 1` + `strategy: Recreate`). The same applies to `triggers_communications` **and scheduled inject release (#116)**, which fire via `asyncio.create_task(asyncio.sleep(...))` and would need a task queue (Celery/ARQ) for multi-process. `schedule_service` keeps an in-memory `_scheduled` registry (`exercise_id → inject_id → task`) so pending timers can be cancelled (release-early), deferred (pause cancels; resume re-arms with the remaining offset), and dropped (complete); `rehydrate_schedules()` in the lifespan re-arms active exercises after a **single-process** restart only.

**Subsystem deep-dives live in [PLAN.md](PLAN.md) § Subsystem Decisions** — read the relevant entry before touching that subsystem:
- *Security & auth*: startup secret validation (#9), self-registration (#8), password policy (#13), token revocation (#14), admin password reset (#66), registration controls (#67), cookie security & CSRF (#10), security headers (#77), login brute-force (#11), trusted-proxy client IP (#36), WebSocket auth (#68), facilitator ownership scoping (#12).
- *Observability & egress*: audit logging (#23), SIEM forwarding (#24), outbound proxy (#97), OIDC/SSO (#25), LLM providers (#26).
- *Features*: inject comment threads, group-scoped injects, file attachments (#39, #16), communications state guards (#40), role preview, dark mode, sample scenarios (#15).
- *Deployment*: containerized deployment (Docker/Caddy/k8s, hardened non-root); release engineering (#73) — reproducible builds via `uv.lock`, GHCR image publish + SBOM/SLSA-provenance/cosign on tag push (`.github/workflows/release.yml`), SemVer (see [docs/RELEASING.md](docs/RELEASING.md)). Version single source: `pyproject.toml` → `audit_service.APP_VERSION` via `importlib.metadata`.

**Tailwind**: CLI-compiled `static/css/output.css`. `static/css/input.css` is `@import "tailwindcss"` + `@import "./iceberg.css"` + `@source` lines for the component JS + an `@theme inline` block mapping Tailwind colour/font utilities (`bg-surface`, `text-ink`, `border-line`, `font-mono`, …) onto the shared token CSS variables. Rebuild after template/CSS/JS changes: `tailwindcss -i static/css/input.css -o static/css/output.css`. `output.css` is intentionally untracked: Docker builds it, and any direct Uvicorn/Playwright workflow must compile it before launching the browser. Tailwind v4 auto-scans `app/templates/**/*.html`; the explicit `@source "../js/app.js"` + `@source "../js/pages"` cover utility classes now emitted from the Alpine component JS (e.g. `teamColor()`'s `bg-sky-100/70 …` strings, #77) — no config file needed. The Dockerfile's tailwind stage copies the whole `static/` tree (so the `iceberg.css` import resolves and the fonts propagate to the runtime image).

**Frontend design system**: Aligned with the sibling apps (`iceberg`, `IcebergCM`) on one shared **Iceberg** design system — a cool blue-grey **oklch** token set with a fixed glacial-cyan accent (`--accent: oklch(0.66 0.118 226)`), held in a hand-authored `static/css/iceberg.css` (not inline in `base.html`). Fonts are self-hosted (`static/fonts/*.woff2` + `static/css/fonts.css`, linked in `base.html`): **Archivo** (UI), **JetBrains Mono** (data/labels), **Spectral** (prose) — no Google Fonts CDN. `iceberg.css` provides the sibling shell vocabulary (`.brandbar`, `.rail`/`.rail-link.is-active`, `.topbar`/`.crumb-*`) plus a compatibility layer that re-implements IcebergTTX's existing utility classes (`.paper`, `.surface`, `.smallcaps`, `.mono`, `.pill`, `.btn`/`.btn-*`, `.node`, `.briefing`, `.live-dot`, `.stripe`) on the shared tokens, so page templates match the siblings without a wholesale class rename. Shell: dark command-center **rail** (`.rail`, sticky) + light `.workspace` content + context `.topbar`. The rail is Alpine-driven (`sidebarNav` on the `.app` wrapper) and hides on auth pages via `x-show="!!user"`; it carries two groups (**Workspace** / **Administration**, admins only) with 18px inline-SVG glyphs from the `rail_icon()` Jinja macro, while Settings/Help/feedback live in the identity footer — where **`.rail-logout` must stay the last focusable control** (the mobile focus trap wraps Shift+Tab onto it). The topbar reads `crumbLabel` + `crumbContext` (the live exercise) from the current path, updating on soft-nav; a full-bleed page suppresses it with `{% block topbar %}{% endblock %}` (the facilitator console does, and carries its own command bar instead).

**UI review — menuing & layout (`design_handoff_ui_review/`)**: three primitives came out of it. **Team scents** — `--team-{itops,legal,exec,comms}-{bg,ink,line}` tokens in both themes; `uiHelpers.teamColor(id)` returns only a tint modifier (`'team-itops'`) that layers onto `.pill`/`.team-label`, and an unknown scenario-defined team id returns `''` → the neutral pill. (It previously returned raw light-only Tailwind classes, invisible in dark mode.) **`.seg`/`.seg-btn`** — the segmented control used by the response, direction, theme, and role filters. **`.canvas` + `.workspace--fill`** — the flex chain that lets a page fill the viewport so a pane inside it scrolls on its own; use it instead of a `calc(100vh - Npx)` guess at the chrome above. **Control density**: a global `button { min-height: 40px }` floor plus a Playwright touch-target test (≥40px desktop / ≥44px mobile) — a class selector beats that floor on specificity, so any new control class must re-assert 40px, not undercut it.

**Strict CSP + CSP-safe Alpine (#77)**: the app ships a strict same-origin `Content-Security-Policy` with **`script-src 'self'`** — no `'unsafe-inline'`/`'unsafe-eval'`. This required the **`@alpinejs/csp`** build (vendored, version-pinned at `static/js/vendor/alpine-csp-<ver>.min.js`) and moving **all** inline JS out of the templates. There are **no inline `<script>` blocks anywhere**: the theme/FOUC bootstrap is `static/js/theme-boot.js` (loaded synchronous/non-defer at the top of `<head>` so it stamps `data-theme` before first paint); the shared runtime (auth `apiFetch`/`readJson`, theme helpers, the soft-navigation engine, the `DT.uiHelpers` format-helper mixin, and the `sidebarNav` component) is `static/js/app.js`; each page's component lives in `static/js/pages/*.js`. Every component is registered via `Alpine.data('name', factory)` inside an `alpine:init` listener; the registry files load **before** the Alpine vendor build (`defer` order in `base.html`) so the listeners exist when it fires. Soft-navigation is a `destroyTree → innerHTML swap → initTree` (the old inline-`<script>` re-injection is gone — it was the core CSP incompatibility). **Template-authoring constraint**: the CSP interpreter evaluates directive *attributes* only and cannot handle optional chaining (`?.`), arrow functions, `x-html`, or bare page-global identifiers (it resolves against component scope) — push such logic into component getters/methods (spread `...DT.uiHelpers` for the format helpers) and pass server data as `|tojson` factory args. Fonts are self-hosted (no Google Fonts CDN). **All security headers are emitted by the app** (`SecurityHeadersMiddleware`, below); the Caddy reverse proxy adds none.

**Communications delay**: `triggers_communications` in the scenario JSON fire via `asyncio.create_task(asyncio.sleep(...))` — sufficient for single-process. Would need a task queue (e.g. Celery, ARQ) for multi-process deployment.

**Pacing — clock & scheduled release (#116)**: `Exercise.paused_at` + `accumulated_pause_seconds` make elapsed time pause-aware (effective elapsed = `(now|paused_at|ended_at − started_at) − accumulated_pause_seconds`); `transition_state` maintains them and every lifecycle transition broadcasts a full `exercise_state_change` WS frame so client clocks (which tick HH:MM:SS locally, no per-second WS) stay in sync. An inject may auto-release at `Inject.release_offset_minutes` (seeded from the scenario node's optional `release_at_minutes`; runtime-settable via `PATCH /injects/{id}/schedule`, null clears). `release_at_minutes` adds **no** graph edge — `_check_no_cycles` is untouched. Scheduling stays "pull not push": releases are still cancellable and can be triggered early. See `schedule_service` + the single-replica note above.

---

## Project Structure

```
app/
├── main.py          # App factory + lifespan (configure_logging, validate_settings, run_migrations, middleware)
├── config.py        # Settings via pydantic-settings (.env) + validate_settings() startup guard
├── middleware.py    # AuditContextMiddleware + SecurityHeadersMiddleware + CSRFOriginMiddleware (ASGI)
├── database.py      # async Postgres engine (create_async_engine) + get_session
├── dependencies.py  # get_current_user, require_role()
├── models/          # SQLModel table definitions (incl. audit.AuditEvent)
├── schemas/         # Pydantic schemas (auth, scenario_json)
├── routers/         # One router per resource + ui.py for Jinja2 pages
├── services/        # Business logic (auth, scenario, exercise, inject, inject_comment, response, comms, llm_service, llm/ provider adapters, ws_manager, access_control, audit_service, rate_limit, proxy + proxy_settings_service)
└── templates/       # Jinja2 HTML
    │   base.html            # App shell (brandbar/rail/topbar) + sidebarNav() Alpine + shared JS; design tokens in static/css/iceberg.css
    │   dashboard.html       # Command center (live exercise hero card + exercises/scenarios lists)
    │   help.html            # In-app help & scenario JSON documentation
    │   auth/login.html      # Centered card, no sidebar
    │   auth/register.html
    │   scenarios/list.html  # Table grid layout
    │   scenarios/detail.html # Depth-first inject tree + validation sidebar
    │   scenarios/editor.html # Inject tree editor
    │   exercises/list.html  # Exercise list + create modal
    │   exercises/facilitator.html  # Full-bleed console: command bar + 3 panes (no topbar)
    │   exercises/participant.html  # Briefing cards (760px centered)
    │   communications/inbox.html   # 352px list + reader pane (.workspace--fill)
    │   communications/index.html  # /communications hub (redirects to active exercise)
    │   settings.html              # Two-column sticky sub-nav: account/appearance/role/samples
    │   admin/audit.html           # Admin: audit trail + SIEM forwarding config
    │   admin/proxy.html           # Admin: outbound proxy routing + connectivity test
tests/               # Pytest suite (conftest.py + one file per resource)
alembic/             # Migration env.py + versions/ (baseline schema); alembic.ini at repo root
static/css/          # input.css + iceberg.css (design system) → output.css; fonts.css + self-hosted woff2
static/js/           # theme-boot.js (FOUC) + app.js (runtime/soft-nav/sidebarNav) + pages/*.js (Alpine.data registries) + vendor/alpine-csp-*.min.js
app/samples/         # Bundled scenario JSON definitions (ransomware_response, vendor_outage)
Dockerfile           # Multi-stage: Tailwind build → Python runtime (non-root, static_src/ trick)
docker-compose.yml   # app + postgres:17 + caddy (auto-HTTPS); hardened (non-root, cap_drop, read-only)
docker/Caddyfile     # Caddy reverse proxy (automatic HTTPS) + direct static serving
k8s/                 # Kubernetes manifests (namespace, secrets, postgres, app, caddy)
```

---

## Build Status

All **26 build phases complete**: foundation/auth → scenarios → exercises/members →
injects + WebSocket → responses/branching → communications → LLM integration →
UI/design-system alignment → containerized deployment → async (asyncpg) migration →
security hardening (#8–#16, #23) → facilitator ownership scoping (#12) → SIEM
forwarding (#24) → strict CSP (#77) → OIDC/SSO (#25) → Caddy reverse proxy + WS
cookie auth (#68) → pluggable AI providers (#26) → outbound proxy (#97) → UI review:
menuing & layout redesign. Per-phase detail lives in git history / merged PRs.

---

## Development Setup

Dependencies are managed with **uv** against the committed **`uv.lock`** (#73) — the
reproducible-build source of truth (the Dockerfile and CI use it too; `uv lock --check`
gates CI). After changing deps in `pyproject.toml`, run `uv lock` and commit the result.

```bash
uv sync --extra dev             # creates .venv from uv.lock (+ dev tools)
cp .env.example .env            # set SECRET_KEY
uv run uvicorn app.main:app --reload   # applies Alembic migrations to head on startup
```

Run tests: `uv run pytest` (or activate `.venv` and run `pytest`).

Schema changes: edit the models, then `alembic revision --autogenerate -m "describe change"` (against a running Postgres) and review the generated migration under `alembic/versions/`. Migrations are applied automatically on app startup; run `alembic upgrade head` manually to apply without starting the app.

---

## Testing
Write tests for critical functionality in Pytest (async — `asyncio_mode = "auto"`). One test file per resource; fixtures in `tests/conftest.py`. The suite runs against a real **Postgres** spun up by `testcontainers` (`postgres:17`) before the app is imported, so `app.database.engine` and the test session share the same DB; set `DATABASE_URL_OVERRIDE_FOR_TESTS` to point at an external Postgres instead. Tests use `httpx.AsyncClient` (+ `httpx-ws` `aconnect_ws` for WebSockets) over `ASGIWebSocketTransport`, not Starlette's `TestClient`. Per-test isolation is transaction-rollback (`AsyncSession` joined to an outer transaction with `join_transaction_mode="create_savepoint"`); the `client` is session-scoped (one cookie jar — the autouse `_override_session` fixture clears cookies and wires the per-test session). The engine uses `NullPool` and tests run on one session-scoped event loop. **Parallel by default**: `addopts = -n auto` (pytest-xdist) runs the suite across CPU cores; each worker is a separate process that spins up its **own** Postgres testcontainer + app engine, so there's no shared DB or in-memory state to collide (safe because `conftest.py` starts the container and reassigns `app.database.engine` at import, per process). A single-test debug loop can opt out with `pytest -n0 …`. **Testing a background task that opens its own `AsyncSession(engine)`** (`run_llm_pipeline`, `_delayed_comm`, `schedule_service` workers): it runs on a separate connection that can't see the test's uncommitted savepoint rows — monkeypatch the module's `AsyncSession` symbol to reuse the test session, and assert on the returned/broadcast payload rather than `session.refresh()`-ing a pre-loaded instance (the worker's commit expunges it).

## Maintenance
Keep README.md, CLAUDE.md, and PLAN.md up to date. Update the Build Status summary above when phases complete. Record cross-cutting decisions (ones that constrain everyday code) here; put subsystem deep-dives in [PLAN.md](PLAN.md) § Subsystem Decisions and add a line to the index above. This file is the always-loaded instruction file — keep it under ~150 lines.
