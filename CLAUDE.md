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

**Schema migrations (Alembic, #19)**: Schema is managed by Alembic. `app/database.py` exposes `run_migrations()`, called from the async lifespan, which runs `alembic upgrade head` in a worker thread (`asyncio.to_thread` — Alembic's async `env.py` calls `asyncio.run`, which can't run inside the already-running lifespan loop). This self-migrates on startup, which suits the single-replica constraint; multi-replica rollouts should instead run `alembic upgrade head` as a dedicated deploy step. `alembic/env.py` derives the URL from `settings` via `make_async_url()` and uses `SQLModel.metadata` as the autogenerate target (importing every `app/models/*` module, same list as `app/main.py`). Create new migrations with `alembic revision --autogenerate -m "..."`; generated files under `alembic/versions/` are excluded from ruff. The test suite does **not** use Alembic — `tests/conftest.py` builds a throwaway schema with `metadata.create_all`, and `create_db_and_tables()` remains for that path. The `alembic.ini` + `alembic/` dir are copied into the Docker image so startup migration works in containers.

**Everything that touches the DB is async**: services and router handlers are `async def` and `await session.exec(...)/get/commit/refresh/delete` (`session.add` stays sync). Background tasks that open their own session — `run_llm_pipeline` (`llm_service.py`), `_delayed_comm` (`communication_service.py`) — use `async with AsyncSession(engine)`. The `anthropic` client was already `AsyncAnthropic`.

**Timezone-aware timestamps**: all `datetime` columns are declared with `Field(sa_type=DateTime(timezone=True))` so tz-aware UTC values (`datetime.now(UTC)`) map to Postgres `timestamptz`. Without this asyncpg rejects tz-aware values against a naive `TIMESTAMP` column.

**Foreign-key cascades**: Postgres enforces foreign keys natively (no per-connection pragma needed). Models declare SQLModel `Relationship(...)` with `back_populates` plus `cascade_delete=True` on the parent and `ondelete="CASCADE"`/`"SET NULL"` on the child FK (forward references are quoted strings under `TYPE_CHECKING`, with `UP037`/`UP045` ignored for `app/models/*` in `pyproject.toml`). Deleting an `Exercise` cascades to its injects/responses/members/communications/comments/suggested-injects; deleting an `Inject` cascades responses/comments and nulls `Communication.triggered_by_inject_id` (the comms record is preserved). Deleting a `Scenario` that is still referenced by an exercise is **not** cascaded — the delete route returns `409 Conflict` (`scenarios.py`).

**Password hashing**: Uses `bcrypt` directly (not `passlib`). `passlib[bcrypt]` is incompatible with Python 3.14 due to a `bcrypt.__about__` removal in bcrypt 4.x.

**Password policy (#13)**: `validate_password_strength` (`app/schemas/auth.py`, length-only, `MIN_PASSWORD_LENGTH = 12` / `MAX_PASSWORD_LENGTH = 128`, rejects blank/whitespace-only) is applied via reusable `Password` / `OptionalPassword` `Annotated` types on `RegisterRequest` / `UpdateMeRequest`, so `register`/`update_me` return `422` automatically. NIST-aligned: length over character-class complexity (the max caps the unauthenticated register body). Login does **not** re-validate (it only compares hashes), so legacy short passwords still authenticate until changed.

**Token revocation (#14)**: JWTs carry an `iat` claim (`auth_service.create_access_token`) and `User.token_valid_after` (nullable `timestamptz`) is a per-user revocation cutoff. `get_current_user` rejects any token whose `iat` predates `token_valid_after` (a missing or non-numeric `iat` is treated as revoked when a cutoff is set). `update_me` bumps `token_valid_after = now(UTC)` (truncated to whole seconds, so a freshly-minted token is not self-revoked) on password change and re-issues a fresh cookie so the caller's own session survives; all previously-issued tokens are revoked ("change password to kick out an attacker"). Deactivation (`is_active=False`) is already enforced per-request by `get_current_user`. The 8h token lifetime is unchanged (no refresh-token flow yet).

**Scenario storage**: The full scenario definition (inject tree, branching options, team targets, triggered communications) is stored as a single JSON blob in `Scenario.definition` — a validated `text` column (parsed via the `ScenarioDefinition` Pydantic model on every write, and memoised on read by `export_definition`), **not** JSONB. Not normalised into rows — the tree is always read and written as a unit.

**JSONB list columns**: the small list-valued columns — `Inject.target_teams`, `Communication.visible_to_teams`, `Communication.read_by`, `Scenario.tags`, `SuggestedInject.target_teams` — are native Postgres `JSONB` (`sa_column=Column(JSONB)`), so services read/write Python lists directly with no `json.dumps`/`loads`. `None` means "all teams / not-yet-read". The one-off migration `a1f2c3d4e5b6` converts the legacy VARCHAR-JSON rows in place (`ALTER … TYPE jsonb USING col::jsonb`). The remaining `json.loads` sites are intentional: the `definition` blob, LLM model output, and inject-upload request parsing.

**Scenario branching**: "Pull not push" — when a participant responds, the service resolves which inject IDs are valid next steps, but the facilitator manually reviews and releases the chosen branch. This keeps human control in the loop.

**Linear inject flows**: In addition to per-option `next_inject_id` branching, an `InjectNode` may set a node-level `next_inject_id` (in `scenario_json.py`) to chain to the next inject without requiring a participant decision — i.e. a straight-line sequence. The `ScenarioDefinition` validator checks the referenced ID exists, and `_check_no_cycles()` includes node-level `next_inject_id` edges in its adjacency graph so linear chains can't form a cycle.

**Inject comment threads**: Participants can post team-scoped discussion comments on released injects (`InjectComment` model; `app/routers/inject_comments.py` under `/api/exercises/{exercise_id}/inject-comments`). Comment visibility is group-scoped via `comment_group_for_user()` in `inject_comment_service.py` (resolved against `Inject.group_id` / the participant's exercise group / team); facilitators and observers see all comments, participants only their own group's. Only participants may post, only while the exercise is `active` and the inject is `released`/`resolved`. New comments broadcast over WebSocket (`inject_comment_created`), group-scoped when the comment has a `group_id`. Access checks live in the shared `app/services/access_control.py`.

**JWT auth**: Stored in both an `httpOnly` cookie (for page navigation/Jinja2 routes) and `localStorage` (for Alpine/fetch calls). The `get_current_user` FastAPI dependency checks both; the `Authorization` header takes precedence.

**WebSocket auth**: JWT passed as `?token=<jwt>` query parameter. Browsers cannot set the `Authorization` header on WebSocket upgrade requests. The `exercise_ws` handshake authenticates/authorises against its injected session, then `await session.close()`s it **before** entering the receive loop (the loop is DB-free) — otherwise the dependency-scoped session would hold a pooled connection idle-in-transaction for the whole socket lifetime and ~15 concurrent sockets would exhaust the pool (#35). `broadcast_to_groups` delivers to matching-group connections **plus facilitators and observers** (both have global read-visibility), so observers get live `inject_released`/comment pushes for group-scoped items, matching `is_inject_visible_to_user` (#38).

**Startup secret validation**: `validate_settings()` (`config.py`) is called from the lifespan startup and aborts the app if `secret_key` is unset, equal to the well-known default, or shorter than 32 chars — unless `DEV_MODE=true`. This prevents silently signing JWTs with a publicly-known key (#9). `dev_mode` also relaxes the Secure-cookie requirement for local HTTP. Tests set `DEV_MODE=true` in `conftest.py` before app import.

**Self-registration is participant-only**: `RegisterRequest` no longer accepts `role` — `POST /api/auth/register` always creates a `participant` (#8). Privileged roles are assigned out-of-band (seeded or via a future admin endpoint); extra body fields are ignored by pydantic. The register template no longer offers a role selector.

**Cookie security & CSRF**: the auth cookie is set with `Secure` (gated on `settings.cookies_secure`, default `not dev_mode`; override with `COOKIE_SECURE`). `CSRFOriginMiddleware` (`app/middleware.py`) verifies `Origin`/`Referer` for cookie-authenticated state-changing requests under `/api/` (#10). Bearer-`Authorization` requests and `/api/auth/*` are exempt — the app's own fetch calls use the localStorage Bearer token, so the cookie is effectively navigation-only. Extra allowed origins via `TRUSTED_ORIGINS`.

**Security headers (#77)**: `SecurityHeadersMiddleware` (`app/middleware.py`) emits the full security header set — the strict `CONTENT_SECURITY_POLICY` (`script-src 'self'`, no `unsafe-*`; `style-src` keeps `'unsafe-inline'` for dynamic `style=` attrs), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, a deny-all `Permissions-Policy`, `Cross-Origin-Opener-Policy`, and (only when `not dev_mode`) `Strict-Transport-Security`. `build_security_headers()` is a pure function evaluated per response (so `dev_mode` is monkeypatchable in tests). It uses **setdefault semantics** so the per-download `nosniff` on attachment downloads (#16) is preserved, not duplicated. Registered **outside** `CSRFOriginMiddleware` (so CSRF-blocked 403s still carry the headers) and **inside** `AuditContextMiddleware`. The **app is the single source of truth** — `docker/nginx.conf` and `k8s/nginx/configmap.yaml` no longer set any security headers (only `nosniff` on the directly-served `/static/` location); this also fixed the k8s config, which previously shipped no CSP at all.

**Login brute-force protection**: `app/services/rate_limit.py` is an in-memory sliding-window limiter keyed by `ip:email`. After `LOGIN_MAX_ATTEMPTS` (default 5) failures within `LOGIN_LOCKOUT_SECONDS` (default 300) the login route returns `429` with `Retry-After`; a success resets the counter (#11). In-memory ⇒ single-process only (same constraint as `ws_manager`). Tests clear it via an autouse fixture.

**Trusted-proxy client IP (#36)**: `client_ip()` (`middleware.py`) returns `request.client.host` — the IP resolved by uvicorn's `ProxyHeadersMiddleware`, which rewrites the client from `X-Forwarded-For` **only** when the peer is in `--forwarded-allow-ips` / `FORWARDED_ALLOW_IPS`. This IP feeds both the audit `source_ip` and the login rate-limit key, so trusting only the nginx hop (rather than the old hand-rolled leftmost-XFF parse) closes the spoofable brute-force bypass + audit poisoning. Launch commands pass `--proxy-headers`; the Docker/k8s images default `FORWARDED_ALLOW_IPS=*` because the app is reachable only through nginx. Local `uvicorn --reload` (no proxy) leaves it unset so XFF is never trusted.

**Audit logging**: `app/services/audit_service.py` emits structured JSON audit events to the `iceberg_ttx.audit` logger (always) and, when `AUDIT_PERSIST` is set, to the append-only `AuditEvent` table (#23). `emit()` stays **synchronous** (it is called from many sync sites and must never raise); the DB write is now async, so `_persist()` schedules `_persist_async()` as a fire-and-forget task on the running event loop (references held in `_persist_tasks` to avoid GC; skipped if no loop is running — the JSON log line is the durable record). `emit()` sanitises all free-text against log injection (CR/LF/control chars). `AuditContextMiddleware` populates per-request "where" fields (request id, source IP, method, path) via a `ContextVar`; pass `actor=user` so the **actual** identity is logged even under facilitator role-preview. Wired at: login success/failure/lockout, register, logout, password change, token-validation failures, `authz.denied` (role + exercise access), inject release/delete, exercise lifecycle, member enrol/remove/group-change, exports, CSRF blocks, app startup/shutdown, and unhandled 500s. Secrets and payload bodies are never logged.

**SIEM forwarding (#24)**: the app is its own forwarder (no Vector/Fluent Bit sidecar). `app/services/siem_service.py` ships each event, off the response path, to the enabled sinks — `file` (append JSON line), `syslog` (RFC 5424 UDP/TCP), `http` (JSON POST to a Splunk HEC / Elastic / webhook endpoint, `Authorization: Bearer` from the **env-only** `SIEM_HTTP_TOKEN`, `verify` per config). `stdout` is the always-on baseline (the existing `iceberg_ttx.audit` handler) so a BYO node-level shipper can still tail it. `audit_service.emit()` gains `_ship()` which — like `_persist()` — reads routing from an **in-memory `SiemConfig` cache** (sync `emit` has no DB session) and `spawn()`s `siem_service.emit` on the running loop; each sink is `_safe`-wrapped so a dead/slow SIEM (5s timeouts) never raises or blocks the request. Routing lives in the admin-editable **`AuditSettings` singleton** (`app/models/audit_settings.py`, row id=1, no secret column) managed by `audit_settings_service.py`; the cache is loaded at startup (`main._load_siem_config`) and refreshed on every save. Admin API `app/routers/audit.py` (`/api/audit/events|settings|test`, gated by `require_admin` → real `User.is_admin` column) backs the **`/admin/audit`** page (event trail + SIEM config form + "send test event"); a UI-only `is_admin` JWT claim + `UserResponse.is_admin` gate the page shell / rail link, but the API always re-checks the DB column. Seeded from `SIEM_*` env (see `.env.example`); wired in compose + `k8s/configmap.yaml` (routing) + `k8s/secrets.yaml` (`SIEM_HTTP_TOKEN`). Single-process, like `ws_manager`/`rate_limit` (the persisted `AuditEvent` row remains the durable record on SIEM outage).

**Facilitator ownership scoping (#12)**: facilitator access to **exercises** is scoped per-exercise, not global. `require_exercise_access` (read gate) and `require_exercise_owner` (mutation gate) in `access_control.py` grant access only to: the creator (`Exercise.created_by`), a **co-facilitator** (a facilitator enrolled as an `ExerciseMember` — reuses the existing membership mechanism, no new field), or a **global admin** (`User.is_admin`, assigned out-of-band like the facilitator role — never via registration). Any other facilitator gets `403` + an `authz.denied` audit event. This scopes every exercise read route (via the shared `require_exercise_access` used by injects/responses/communications/inject-comments/ws) plus all mutation/lifecycle/member/export routes in `exercises.py`; `GET /exercises` is filtered to owned-or-member (admins see all). **Scenarios remain a shared library** (any facilitator lists/reads/edits/exports — intentional, they're reusable templates), and `GET /users` stays facilitator-wide (it's the member-enrolment picker). `is_admin` is a real column so it survives role-preview `model_copy` and is unspoofable.

**LLM integration**: Uses `anthropic>=0.40` (`AsyncAnthropic`). Prompt caching applied via the `anthropic-beta: prompt-caching-2024-07-31` header — scenario context + inject content is the cached prefix; participant response text is the non-cached suffix. `run_llm_pipeline` is a background async task (fired via `asyncio.create_task`) that opens its own `AsyncSession(engine)` directly (same pattern as `_delayed_comm`). The `Exercise.llm_enabled` flag gates LLM calls per exercise. Set `ANTHROPIC_API_KEY` in `.env` to enable. All API calls are mocked in `tests/test_llm.py` — no real network requests.

**Tailwind**: CLI-compiled `static/css/output.css`. `static/css/input.css` is `@import "tailwindcss"` + `@import "./iceberg.css"` + `@source` lines for the component JS + an `@theme inline` block mapping Tailwind colour/font utilities (`bg-surface`, `text-ink`, `border-line`, `font-mono`, …) onto the shared token CSS variables. Rebuild after template/CSS/JS changes: `tailwindcss -i static/css/input.css -o static/css/output.css`. Tailwind v4 auto-scans `app/templates/**/*.html`; the explicit `@source "../js/app.js"` + `@source "../js/pages"` cover utility classes now emitted from the Alpine component JS (e.g. `teamColor()`'s `bg-sky-100/70 …` strings, #77) — no config file needed. The Dockerfile's tailwind stage copies the whole `static/` tree (so the `iceberg.css` import resolves and the fonts propagate to the runtime image).

**Frontend design system**: Aligned with the sibling apps (`iceberg`, `IcebergCM`) on one shared **Iceberg** design system — a cool blue-grey **oklch** token set with a fixed glacial-cyan accent (`--accent: oklch(0.66 0.118 226)`), held in a hand-authored `static/css/iceberg.css` (not inline in `base.html`). Fonts are self-hosted (`static/fonts/*.woff2` + `static/css/fonts.css`, linked in `base.html`): **Archivo** (UI), **JetBrains Mono** (data/labels), **Spectral** (prose) — no Google Fonts CDN. `iceberg.css` provides the sibling shell vocabulary (`.brandbar`, `.rail`/`.rail-link.is-active`, `.topbar`/`.crumb-*`) plus a compatibility layer that re-implements IcebergTTX's existing utility classes (`.paper`, `.surface`, `.smallcaps`, `.mono`, `.pill`, `.btn`/`.btn-*`, `.node`, `.briefing`, `.live-dot`, `.stripe`) on the shared tokens, so page templates match the siblings without a wholesale class rename. Shell: dark command-center **rail** (`.rail`, sticky) + light `.workspace` content + breadcrumb `.topbar`. The rail is Alpine-driven (`sidebarNav` on the `.app` wrapper) and hides on auth pages via `x-show="!!user"`; the topbar breadcrumb reads `crumbLabel` from the current path (updates on soft-nav).

**Strict CSP + CSP-safe Alpine (#77)**: the app ships a strict same-origin `Content-Security-Policy` with **`script-src 'self'`** — no `'unsafe-inline'`/`'unsafe-eval'`. This required the **`@alpinejs/csp`** build (vendored, version-pinned at `static/js/vendor/alpine-csp-<ver>.min.js`) and moving **all** inline JS out of the templates. There are **no inline `<script>` blocks anywhere**: the theme/FOUC bootstrap is `static/js/theme-boot.js` (loaded synchronous/non-defer at the top of `<head>` so it stamps `data-theme` before first paint); the shared runtime (auth `apiFetch`/`readJson`, theme helpers, the soft-navigation engine, the `DT.uiHelpers` format-helper mixin, and the `sidebarNav` component) is `static/js/app.js`; each page's component lives in `static/js/pages/*.js`. Every component is registered via `Alpine.data('name', factory)` inside an `alpine:init` listener; the registry files load **before** the Alpine vendor build (`defer` order in `base.html`) so the listeners exist when it fires. Soft-navigation is a `destroyTree → innerHTML swap → initTree` (the old inline-`<script>` re-injection is gone — it was the core CSP incompatibility). **Template-authoring constraint**: the CSP interpreter evaluates directive *attributes* only and cannot handle optional chaining (`?.`), arrow functions, `x-html`, or bare page-global identifiers (it resolves against component scope) — push such logic into component getters/methods (spread `...DT.uiHelpers` for the format helpers) and pass server data as `|tojson` factory args. Fonts are self-hosted (no Google Fonts CDN). **All security headers are emitted by the app** (`SecurityHeadersMiddleware`, below); nginx adds none.

**Communications delay**: `triggers_communications` in the scenario JSON fire via `asyncio.create_task(asyncio.sleep(...))` — sufficient for single-process. Would need a task queue (e.g. Celery, ARQ) for multi-process deployment.

**Communications state guards**: participant outbound `send_comm` requires the exercise to be `active` (409 otherwise), consistent with response `submit` and inject-comment `create_comment` (#40). Facilitator `inject_comm` (simulated inbound) is **intentionally** unrestricted so facilitators can seed comms during `draft`/`paused` setup.

**Group-scoped injects**: `Inject.group_id` and `ExerciseMember.group_id` allow injects to be targeted at specific exercise groups (teams). When `group_id` is `None` the inject is visible to all groups. The inject router resolves group membership via `exercise_group_for_user()` at query time.

**File attachments on injects**: Injects support a single file attachment (`attachment_filename`, `attachment_path`, `attachment_content_type`, `attachment_size` on the `Inject` model). Files are stored under `uploads/inject_attachments/{exercise_id}/`. The inject router accepts `multipart/form-data`; `inject_attachment_payload()` builds the download URL returned in the inject payload. Uploads stream to disk in chunks and abort once `MAX_ATTACHMENT_BYTES` (25 MB) is exceeded, so an oversized upload is never fully buffered (#39). Content-type is confined to `ALLOWED_ATTACHMENT_TYPES` (`_normalize_content_type` — anything else, e.g. `text/html`/`image/svg+xml`, is stored and served as `application/octet-stream`), applied on both upload and download; downloads set `X-Content-Type-Options: nosniff` alongside the `Content-Disposition: attachment` implied by `filename` (#16).

**Role preview**: Facilitators can view the app as a participant or observer via `dt_view_role` and `dt_view_team` cookies (set from `/settings`). `_optional_user()` in `ui.py` reads these cookies and overrides the Jinja2 template role/team — but only when the JWT already contains the `facilitator` role, so API calls are never downgraded.

**Dark mode**: `data-theme="dark"` on `<html>` re-declares the oklch token set (dark surfaces) in `iceberg.css`. `static/js/theme-boot.js` (an external, synchronous `<script>` at the top of `<head>`, not inline — strict CSP, #77) resolves the saved theme (`system`→OS via `prefers-color-scheme`) and stamps `data-theme` before first paint (prevents FOUC). Preference stored in `dt_theme`/`dt_resolved_theme` cookies + `localStorage`, toggled (light/dark/system) from `/settings`. The per-user **accent picker was removed** during the Iceberg alignment — the cyan accent is fixed to match the sibling apps (`dt_accent` is no longer read or written).

**Sample scenarios**: `app/samples/` contains bundled JSON scenario definitions (`ransomware_response.json`, `vendor_outage.json`). `app/services/sample_service.py` lists, validates, and loads them. The settings page exposes a sample loader UI for facilitators. `get_sample_definition` validates `sample_id` against `SAMPLE_ID_RE` (`^[A-Za-z0-9_-]+$`) and asserts the resolved path stays within `SAMPLES_DIR` before reading, preventing directory traversal via the `sample_id` path param (#15); a rejected id returns `None` → the settings routes surface `404`.

**Containerized deployment**: `Dockerfile` is a two-stage build — stage 1 compiles Tailwind CSS (`pytailwindcss`), stage 2 is the Python runtime. The compiled `static/` directory is also copied to `static_src/` in the image; this path is never overridden by a volume mount and is used by entrypoint scripts (Docker Compose) and init containers (k8s) to populate shared static volumes so nginx always serves the version matching the running image. `docker-compose.yml` runs `app` + `postgres:17` + `nginxinc/nginx-unprivileged:alpine` on a private bridge network with named volumes for DB data, uploads, and static files. `k8s/` contains namespace, secrets, configmap, postgres StatefulSet, app Deployment, nginx Deployment, a TLS `Ingress` (`k8s/nginx/ingress.yaml`), and ingress `NetworkPolicy`s (`k8s/networkpolicy.yaml`). **Security posture (IaC review)**: all containers run non-root under a PSS-`restricted`-style `securityContext` (no priv-esc, all caps dropped, `RuntimeDefault` seccomp; app/init containers use a read-only rootfs with a `/tmp` emptyDir); nginx uses the unprivileged image on port 8080; the nginx Service is `ClusterIP` fronted by the TLS Ingress (never a plaintext `:80` LoadBalancer); `automountServiceAccountToken: false` on every pod. Compose mirrors this (`no-new-privileges`, `cap_drop`, read-only app rootfs). **Replica constraint**: `ws_manager.py` is in-memory only — app must run as a single replica until Redis pub/sub is added. k8s manifests enforce `replicas: 1` and `strategy: Recreate`. The async `asyncpg` driver is a core dependency (no separate extra; the `DATABASE_URL` may be a plain `postgresql://` URL — it is upgraded to `asyncpg` at runtime). A minimal `GET /api/health` endpoint (`app/routers/health.py`) is used by k8s liveness/readiness probes.

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
├── services/        # Business logic (auth, scenario, exercise, inject, inject_comment, response, comms, llm, ws_manager, access_control, audit_service, rate_limit)
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
    │   exercises/facilitator.html  # Full-height 3-pane console
    │   exercises/participant.html  # Briefing cards (760px centered)
    │   communications/inbox.html   # 340px list + reader pane
    │   communications/index.html  # /communications hub (redirects to active exercise)
    │   settings.html              # Dark mode toggle, role preview, sample scenario loader
tests/               # Pytest suite (conftest.py + one file per resource)
alembic/             # Migration env.py + versions/ (baseline schema); alembic.ini at repo root
static/css/          # input.css + iceberg.css (design system) → output.css; fonts.css + self-hosted woff2
static/js/           # theme-boot.js (FOUC) + app.js (runtime/soft-nav/sidebarNav) + pages/*.js (Alpine.data registries) + vendor/alpine-csp-*.min.js
app/samples/         # Bundled scenario JSON definitions (ransomware_response, vendor_outage)
Dockerfile           # Multi-stage: Tailwind build → Python runtime (non-root, static_src/ trick)
docker-compose.yml   # app + postgres:17 + nginx-unprivileged; hardened (non-root, cap_drop, read-only)
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
| 17 — Async migration (asyncpg/AsyncSession, Postgres-only, response models, FastAPI-skill alignment) | ✅ Complete |
| 18 — Tech-debt cleanup (#17 logging config, #18 iterative cycle detection, #19 Alembic, #20 task lifecycle, #21/#31 payload/role-preview DRY, #30 WS team-spoof fix, #32 nits) | ✅ Complete |
| 19 — Security hardening (P2: #13 password policy, #14 token revocation, #15 sample-loader path traversal, #16 attachment content-type allowlist + nosniff) | ✅ Complete |
| 20 — Facilitator ownership scoping (#12: per-exercise `created_by` access, co-facilitator membership, `User.is_admin` global override) | ✅ Complete |
| 21 — SIEM audit forwarding (#24: app-as-forwarder file/syslog/http sinks, admin-editable `AuditSettings` + `/admin/audit` UI, env-only HTTP token) | ✅ Complete |
| 22 — Strict CSP (#77: `@alpinejs/csp` build + `Alpine.data()` registries, all inline JS externalised, app-level `SecurityHeadersMiddleware`, nginx/k8s headers removed) | ✅ Complete |

Current test count: **281 passing** (1 skipped).

---

## Development Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # set SECRET_KEY
uvicorn app.main:app --reload   # applies Alembic migrations to head on startup
```

Run tests: `pytest`

Schema changes: edit the models, then `alembic revision --autogenerate -m "describe change"` (against a running Postgres) and review the generated migration under `alembic/versions/`. Migrations are applied automatically on app startup; run `alembic upgrade head` manually to apply without starting the app.

---

## Testing
Write tests for critical functionality in Pytest (async — `asyncio_mode = "auto"`). One test file per resource; fixtures in `tests/conftest.py`. The suite runs against a real **Postgres** spun up by `testcontainers` (`postgres:17`) before the app is imported, so `app.database.engine` and the test session share the same DB; set `DATABASE_URL_OVERRIDE_FOR_TESTS` to point at an external Postgres instead. Tests use `httpx.AsyncClient` (+ `httpx-ws` `aconnect_ws` for WebSockets) over `ASGIWebSocketTransport`, not Starlette's `TestClient`. Per-test isolation is transaction-rollback (`AsyncSession` joined to an outer transaction with `join_transaction_mode="create_savepoint"`); the `client` is session-scoped (one cookie jar — the autouse `_override_session` fixture clears cookies and wires the per-test session). The engine uses `NullPool` and tests run on one session-scoped event loop. **Parallel by default**: `addopts = -n auto` (pytest-xdist) runs the suite across CPU cores; each worker is a separate process that spins up its **own** Postgres testcontainer + app engine, so there's no shared DB or in-memory state to collide (safe because `conftest.py` starts the container and reassigns `app.database.engine` at import, per process). A single-test debug loop can opt out with `pytest -n0 …`.

## Maintenance
Keep README.md, CLAUDE.md, and PLAN.md up to date. Update the Build Status table above when phases complete. Record any significant dependency or architectural decisions here.
