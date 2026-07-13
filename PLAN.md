# IcebergTTX — Implementation Plan

## Context

Building a tabletop exercise (TTX) platform from scratch. API-first: FastAPI backend serves both a JSON API and Jinja2-rendered frontend pages. Real-time inject delivery via WebSockets. LLM integration (Claude) scaffolded from the start, implemented last.

**Key decisions:**
- SQLite (file-based, zero-config; migrate to Postgres if needed later)
- JWT authentication (stateless, API-friendly; stored in both cookie and localStorage)
- WebSockets for real-time inject delivery to participants
- Full-stack built phase by phase (backend + frontend together per feature)
- LLM integration (Claude API) — data model and API stubs from day one, implemented in Phase 8

---

## Directory Structure

```
iceberg_ttx/
├── CLAUDE.md
├── PLAN.md
├── README.md
├── .gitignore
├── .env.example
├── pyproject.toml
│
├── app/
│   ├── main.py              # App factory, lifespan, router registration
│   ├── config.py            # pydantic-settings, reads .env
│   ├── database.py          # SQLite engine, session dependency
│   ├── dependencies.py      # get_current_user, require_role factory
│   │
│   ├── models/
│   │   ├── user.py
│   │   ├── scenario.py
│   │   ├── exercise.py      # Exercise + ExerciseMember
│   │   ├── inject.py
│   │   ├── response.py
│   │   ├── communication.py
│   │   ├── assessment.py    # ResponseAssessment (Phase 8)
│   │   └── suggested_inject.py  # SuggestedInject (Phase 8)
│   │
│   ├── schemas/
│   │   ├── auth.py                # Token, login, register schemas
│   │   └── scenario_json.py       # ScenarioDefinition + nested Pydantic models
│   │
│   ├── routers/
│   │   ├── auth.py
│   │   ├── users.py
│   │   ├── scenarios.py
│   │   ├── exercises.py
│   │   ├── injects.py
│   │   ├── responses.py
│   │   ├── communications.py
│   │   ├── ui.py                  # Jinja2 page routes
│   │   └── ws.py                  # WebSocket endpoint
│   │
│   ├── services/
│   │   ├── auth_service.py        # JWT, password hashing (bcrypt)
│   │   ├── scenario_service.py    # JSON import/export, DAG validation
│   │   ├── exercise_service.py    # Lifecycle state machine
│   │   ├── inject_service.py      # Release, team targeting, WS broadcast
│   │   ├── response_service.py    # Branch evaluation
│   │   ├── communication_service.py
│   │   ├── llm_service.py         # Claude API (stub until Phase 8)
│   │   └── ws_manager.py          # ConnectionManager
│   │
│   └── templates/
│       ├── base.html              # Tailwind CDN, AlpineJS CDN, nav
│       ├── auth/{login,register}.html
│       ├── dashboard.html
│       ├── scenarios/{list,detail,editor}.html
│       ├── exercises/{list,facilitator,participant}.html
│       └── communications/{inbox,compose}.html
│
├── static/css/output.css          # Tailwind CDN in dev; compiled in Phase 7
└── tests/
    ├── conftest.py
    ├── test_auth.py
    ├── test_scenarios.py
    ├── test_scenario_branching.py
    ├── test_exercises.py
    ├── test_injects.py
    ├── test_ws.py
    ├── test_responses.py
    ├── test_communications.py
    └── test_llm.py                # Phase 8
```

---

## Core Models (SQLModel)

### User
```python
class UserRole(str, Enum): facilitator | participant | observer

class User(SQLModel, table=True):
    id, email (unique, indexed), display_name, hashed_password
    role: UserRole
    team: str | None       # sub-type / department for org complexity
    is_active: bool
    created_at: datetime
```

### Scenario
```python
class Scenario(SQLModel, table=True):
    id, title, description, version, tags (JSON list as str)
    definition: str        # Full ScenarioDefinition JSON blob, validated on write
    created_by: int (FK user.id)
    created_at, updated_at: datetime
```

### Exercise + ExerciseMember
```python
class ExerciseState(str, Enum): draft | active | paused | completed

class Exercise(SQLModel, table=True):
    id, scenario_id (FK), title
    state: ExerciseState
    current_node_id: str | None    # Active inject id in the scenario tree
    llm_enabled: bool = False      # Toggle LLM assessment per-exercise
    started_at, ended_at: datetime | None
    created_by: int (FK user.id)

class ExerciseMember(SQLModel, table=True):
    id, exercise_id (FK), user_id (FK), joined_at
```

### Inject
```python
class InjectState(str, Enum): pending | released | resolved

class Inject(SQLModel, table=True):
    id, exercise_id (FK), scenario_node_id: str | None
    title, content
    target_teams: str | None   # JSON list of team IDs; NULL = broadcast all
    sequence_order: int
    state: InjectState
    released_at: datetime | None
    released_by: int | None (FK user.id)
```

### Response
```python
class Response(SQLModel, table=True):
    id, inject_id (FK), exercise_id (FK), user_id (FK)
    content: str
    selected_option: str | None   # Used by branch evaluator
    submitted_at: datetime
    assessment_id: int | None (FK responseassessment.id)  # set async after submission
```

### ResponseAssessment (Phase 8)
```python
class ResponseAssessment(SQLModel, table=True):
    id, response_id (FK)
    llm_model: str                    # e.g. "claude-sonnet-4-6"
    assessment_text: str              # narrative feedback
    decision_quality: str | None      # "good" | "adequate" | "poor"
    recommended_branch_option_id: str | None
    assessed_at: datetime
```

### SuggestedInject (Phase 8)
```python
class SuggestedInjectStatus(str, Enum): pending_review | approved | rejected

class SuggestedInject(SQLModel, table=True):
    id, exercise_id (FK), triggered_by_response_id (FK)
    title, content: str
    target_teams: str | None
    llm_model: str
    status: SuggestedInjectStatus
    reviewed_by: int | None (FK user.id)
    reviewed_at: datetime | None
    generated_at: datetime
```

### Communication
```python
class CommDirection(str, Enum): inbound | outbound

class Communication(SQLModel, table=True):
    id, exercise_id (FK), sender_id: int | None (FK user.id)
    direction: CommDirection
    external_entity: str | None   # e.g. "ICO", "NCSC", "CEO", "Press"
    subject, body: str
    triggered_by_inject_id: int | None (FK)
    visible_to_teams: str | None  # JSON list; NULL = all participants
    sent_at: datetime
    read_by: str | None           # JSON list of user IDs
```

---

## API Endpoints

### Auth `/auth`
- `POST /auth/register` — create user
- `POST /auth/login` — issue JWT (also sets httpOnly cookie)
- `GET/PUT /auth/me` — profile

### Users `/users`
- `GET /users` (facilitator — member-enrolment picker)
- `POST /users` (admin — provision account)
- `POST /users/{id}/reset-password` (admin — set a temporary password, #66)

### Scenarios `/scenarios`
- `GET /scenarios`, `POST /scenarios`, `GET /scenarios/{id}`, `PUT /scenarios/{id}`, `DELETE /scenarios/{id}`
- `POST /scenarios/import` — upload JSON file
- `GET /scenarios/{id}/export` — download JSON
- `GET /scenarios/{id}/validate`

### Exercises `/exercises`
- `GET /exercises`, `POST /exercises`, `GET /exercises/{id}`, `PUT /exercises/{id}`, `DELETE /exercises/{id}`
- `POST /exercises/{id}/start|pause|resume|complete` — lifecycle transitions
- `GET|POST /exercises/{id}/members`, `DELETE /exercises/{id}/members/{user_id}`

### Injects `/exercises/{id}/injects`
- `GET`, `POST`, `GET /{inject_id}`, `PUT /{inject_id}`, `DELETE /{inject_id}`
- `POST /{inject_id}/release`

### Responses `/exercises/{id}/responses`
- `GET` (facilitator: all; participant: own), `POST` (submit), `GET /{response_id}`

### Communications `/exercises/{id}/communications`
- `GET` (filtered by team visibility), `POST` (send outbound), `GET /{comm_id}` (marks read)
- `POST /inject` — facilitator injects a simulated inbound comm

### LLM (stubs return 501 until Phase 8)
- `POST /exercises/{id}/responses/{response_id}/assess`
- `GET  /exercises/{id}/responses/{response_id}/assessment`
- `GET  /exercises/{id}/suggested-injects`
- `POST /exercises/{id}/suggested-injects/{id}/approve`
- `POST /exercises/{id}/suggested-injects/{id}/reject`

### WebSocket
- `GET /ws/exercises/{exercise_id}?token=<jwt>`

### UI Routes (Jinja2)
- `/`, `/login`, `/register`, `/dashboard`
- `/scenarios`, `/scenarios/new`, `/scenarios/{id}`, `/scenarios/{id}/edit`
- `/exercises`, `/exercises/new`, `/exercises/{id}/facilitate`, `/exercises/{id}/participate`
- `/exercises/{id}/communications`

---

## WebSocket Architecture

**Auth**: JWT as `?token=<jwt>` query param (browsers cannot set Authorization headers on WS upgrade).

**Connection manager** (`ws_manager.py`):
```python
class ConnectionManager:
    rooms: dict[int, list[tuple[WebSocket, User]]]   # keyed by exercise_id

    async def connect(ws, exercise_id, user)
    async def disconnect(ws, exercise_id)
    async def broadcast_to_exercise(exercise_id, message: dict)
    async def broadcast_to_teams(exercise_id, teams: list[str], message: dict)
    async def send_to_user(exercise_id, user_id, message: dict)
```

**Message envelope**:
```json
{
  "type": "inject_released | exercise_state_change | communication_received | participant_joined | participant_left | response_submitted | assessment_ready | inject_suggested | pong",
  "exercise_id": 1,
  "timestamp": "2026-05-11T19:30:00Z",
  "payload": { ... }
}
```

**Role filtering**: `response_submitted`, `assessment_ready`, `inject_suggested` — facilitator connections only.

**Heartbeat**: client pings every 30s; server pongs. Lifespan background task drops stale connections after 90s.

---

## Scenario JSON Schema

Stored as a JSON blob in `Scenario.definition`. Validated against `ScenarioDefinition` (Pydantic) on every write.

```json
{
  "schema_version": "1.0",
  "title": "Ransomware Attack",
  "description": "...",
  "tags": ["cyber", "ransomware"],
  "metadata": { "author": "...", "estimated_duration_minutes": 90 },
  "participant_teams": [
    { "id": "it_ops", "label": "IT Operations" },
    { "id": "legal",  "label": "Legal & Compliance" }
  ],
  "injects": [
    {
      "id": "inject_01",
      "title": "Initial Alert",
      "content": "Your SIEM has flagged unusual lateral movement...",
      "target_teams": ["it_ops"],
      "sequence_order": 1,
      "options": [
        { "id": "opt_a", "label": "Isolate systems immediately", "next_inject_id": "inject_02a" },
        { "id": "opt_b", "label": "Continue monitoring",          "next_inject_id": "inject_02b" }
      ],
      "free_text_response": true,
      "triggers_communications": [
        {
          "external_entity": "NCSC",
          "direction": "inbound",
          "subject": "Threat Intelligence Advisory",
          "body": "We have observed similar TTPs...",
          "delay_after_release_seconds": 120
        }
      ]
    }
  ],
  "start_inject_id": "inject_01",
  "debrief_notes": "Key learning: speed of isolation, GDPR notification timelines."
}
```

**Validation rules enforced by `ScenarioDefinition`:**
- All `next_inject_id` values must reference a defined inject `id`
- `start_inject_id` must reference a defined inject
- No circular paths (DFS cycle detection)
- All `target_teams` values must reference a defined `participant_teams[].id`

**Design choices:**
- JSON stored as blob — always read/written as a unit; normalising injects into rows would complicate the branching graph
- Branching: the participant's selected option settles *which* inject comes next (it advances their team's cursor); the facilitator controls only *whether and when* it is released. No branch is ever selected automatically, and the facilitator cannot substitute a different one (409) — but *delivery* can be automatic (a `release_at_minutes` inject self-releases once the cursor reaches it), so don't overstate it as "nothing reaches participants until a human releases it". Avoid the old "pull not push" shorthand — it reads as though the facilitator picks the branch, and that misreading has shipped as a docs bug.
- Option-bearing responses require an exact option from their scenario node; `free_text_response` controls whether reasoning is also required, while no-option injects always require content
- `triggers_communications` delays use `asyncio.create_task(asyncio.sleep(...))` — fine for single-process SQLite; would use a task queue in a multi-process deployment

---

## Frontend (AlpineJS + Tailwind)

**Auth pattern**: JWT in `httpOnly` cookie (page navigation) + `localStorage` (JS fetch calls). `get_current_user` FastAPI dependency checks both; Authorization header takes precedence.

**Key Alpine components:**
- `nav()` — fetches `/auth/me`, shows role badge, handles logout
- `dashboard()` — role-aware stats + scenario list
- `scenarioEditor()` — reactive inject chain builder; serialises to `ScenarioDefinition` JSON on save
- `exerciseConsole()` (facilitator) — inject queue, live response feed, participant roster via WebSocket
- `participantView()` (participant) — inject cards appear on `inject_released` WS event; submit disables card
- `commsInbox()` — two-pane inbox; new messages arrive via `communication_received` WS event

Tailwind via CDN in development; replaced with CLI-compiled `static/css/output.css` in Phase 7.

---

## Build Phases

| Phase | Status | Deliverable |
|-------|--------|-------------|
| 1 — Foundation | ✅ Done | JWT auth API + tests |
| 2 — Scenarios | ✅ Done | Scenario CRUD, import/export, validation, frontend pages |
| 3 — Exercises + Members | ⬜ Next | Exercise lifecycle, member enrolment |
| 4 — Injects + WebSocket | ⬜ | Real-time inject delivery |
| 5 — Responses + Branching | ⬜ | Inject-response-branch cycle |
| 6 — Communications | ⬜ | Simulated inbox/outbox |
| 7 — Polish | ⬜ | Tailwind CLI, observer role, exports, CI |
| 8 — LLM Integration | ⬜ | Claude response assessment + inject suggestion |

### Phase 3 — Exercises + Members
`Exercise`, `ExerciseMember` models, `exercise_service.py` (lifecycle state machine + valid transition guards), `exercises` router, exercise frontend list page and create form.

**Deliverable**: Facilitator can create an exercise from a scenario, enrol participants, and transition through `draft → active → paused → completed`.

### Phase 4 — Injects + WebSocket
`ws_manager.py` (ConnectionManager), WS router (`/ws/exercises/{id}?token=<jwt>`), `Inject` model, `inject_service.py` (release + team targeting + WS broadcast), inject router, `exercises/facilitator.html`, `exercises/participant.html`.

**Deliverable**: End-to-end real-time — facilitator releases inject, participant sees it instantly.

### Phase 5 — Responses + Branching
`Response` model, `response_service.py` (submit, evaluate branch from `selected_option`, update `Exercise.current_node_id`, queue next inject), response router, live response feed in facilitator console.

**Deliverable**: Full inject → response → branch cycle works end to end.

### Phase 6 — Communications
`Communication` model, `communication_service.py` (visibility filtering by team, `triggers_communications` delay via asyncio task, mark-read), comms router, `communications/inbox.html` + compose form, wired into `inject_service.release_inject`.

**Deliverable**: Participants receive simulated regulatory/press emails during exercises.

### Phase 7 — Polish
Tailwind CLI build, observer read-only view, exercise JSON/CSV export, error middleware, auth rate limiting, full README, GitHub Actions CI (ruff + pyright + pytest).

### Phase 8 — LLM Integration
Implement `llm_service.py` using the Anthropic SDK. Add `anthropic>=0.40` to dependencies.

> **Post-launch (#26 — pluggable AI providers):** the AI backend is now provider-pluggable
> via an adapter/registry (`app/services/llm/`) mirroring the OIDC pattern. `LLM_PROVIDER`
> selects one of `anthropic` | `bedrock` | `openai` | `ollama` | `gemini` | `none`; two
> adapter families (Anthropic/Bedrock via `messages.create`; OpenAI/Ollama/Gemini via the
> OpenAI Chat Completions surface) cover all five. Every provider SDK is an opt-in extra —
> none is a core dependency (`.[llm-anthropic]`, `.[llm-bedrock]`, `.[llm-openai]`,
> `.[llm-all]`). See CLAUDE.md → "LLM integration".

**Response assessment** (auto, background async task):
- Fires after `response_service.submit_response()` when `exercise.llm_enabled == True`
- Cached prompt prefix = scenario context + inject content (same for all responses to a given inject → high cache hit rate)
- Stores `ResponseAssessment`; pushes `assessment_ready` WS event to facilitator

**Inject suggestion** (auto or facilitator-triggered):
- Claude suggests a follow-up inject based on the participant's free-text response
- Stored as `SuggestedInject(status=pending_review)`
- Pushes `inject_suggested` WS event; facilitator approves or rejects

```python
# llm_service.py interface
async def assess_response(response, inject, scenario) -> ResponseAssessment
async def suggest_inject(response, exercise, scenario) -> SuggestedInject
```

### Phase 16 — Security hardening (P0/P1)
Closes GitHub issues #8, #9, #10, #11, #23 (the "auth rate limiting" item once noted under Phase 7 is delivered here).

- **#9 SECRET_KEY validation**: `validate_settings()` aborts startup on an unset/default/short key unless `DEV_MODE=true`.
- **#8 Registration privilege escalation**: `RegisterRequest` no longer accepts `role`; self-registration is always a `participant`. Regression tests assert elevated roles are ignored.
- **#10 Cookie security + CSRF**: `Secure` flag (gated on `cookies_secure`), and `CSRFOriginMiddleware` verifying `Origin`/`Referer` for cookie-authenticated mutations (Bearer + `/api/auth/*` exempt).
- **#11 Login rate limiting**: in-memory sliding-window limiter (`rate_limit.py`), `429` + `Retry-After` after repeated failures, reset on success.
- **#23 Audit logging**: `AuditEvent` model + `audit_service.emit()` structured JSON logger, request-context middleware, sanitisation against log injection, real-identity attribution under role-preview.

- **#24 SIEM forwarding**: app-as-forwarder (`siem_service.py`) shipping audit events to file/syslog/http sinks off the response path, admin-editable `AuditSettings` singleton + `/admin/audit` UI (event trail, SIEM config, test event), env-only HTTP bearer token, failure-isolated sinks that never block a request.

Both **P2** follow-ups are now complete: per-facilitator ownership scoping (#12) and SIEM shipping (#24, companion to #23).

---

## Testing Strategy

**Stack**: `pytest` + `pytest-asyncio` (auto mode) + `httpx.TestClient` + `httpx-ws`

**Fixtures** (`conftest.py`):
- `session` — in-memory SQLite, per-test isolation
- `client` — `TestClient(app)`
- `facilitator`, `participant` — seeded users
- `facilitator_token`, `participant_token` — JWT strings
- `sample_definition` — minimal valid `ScenarioDefinition`
- `sample_scenario` — persisted `Scenario` row
- `active_exercise` — added in Phase 3

| Test file | Covers |
|-----------|--------|
| `test_auth.py` | Register, duplicate email, login, bad password, /me, update, token roles |
| `test_scenarios.py` | CRUD, file import, invalid JSON, export round-trip, validate endpoint |
| `test_scenario_branching.py` | Schema validation, cycle detection, `get_next_inject_ids`, `resolve_branch` |
| `test_exercises.py` | Create, lifecycle transitions, invalid transitions, member enrol/remove |
| `test_injects.py` | Create, release, idempotency, broadcast vs. team-targeted |
| `test_ws.py` | Connect valid/invalid token, receive events, heartbeat, facilitator-only events |
| `test_responses.py` | Submit, branch evaluation, next inject queued |
| `test_communications.py` | Send outbound, inject inbound, visibility filtering, mark-read, triggered comms |
| `test_llm.py` | Assessment stored, WS event fired, suggested inject created, approve/reject |
| `test_security.py` | SECRET_KEY validation, login rate limiting, CSRF origin checks, audit logging + log-injection sanitisation |

---

## Dependencies

```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "sqlmodel>=0.0.21",
    "pydantic-settings>=2.0",
    "python-jose[cryptography]>=3.3",
    "bcrypt>=4.0",               # passlib incompatible with Python 3.14
    "python-multipart>=0.0.9",
    "jinja2>=3.1",
    "aiofiles>=23.0",
    "email-validator>=2.0",
    # anthropic>=0.40 added in Phase 8
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "httpx>=0.27",
    "httpx-ws>=0.6",
    "ruff>=0.4",
    "pyright>=1.1",
]
```

---

## Verification Checkpoints

**After Phase 1**: `uvicorn app.main:app --reload` starts; `pytest tests/test_auth.py` — 12 passed.

**After Phase 2**: `pytest tests/` — 43 passed. Facilitator can log in, create a scenario, view the inject tree, import/export JSON.

**After Phase 4**: Two browser tabs — facilitator at `/exercises/{id}/facilitate`, participant at `/exercises/{id}/participate`. Release inject in facilitator console → appears in participant tab without page refresh.

**After Phase 5**: Submit a branching response → facilitator console shows the correct next inject(s) queued from the scenario tree.

**After Phase 6**: Release an inject with `triggers_communications` → simulated inbound email appears in participant inbox after the configured delay.

**After Phase 8**: Submit free-text response in `llm_enabled` exercise → facilitator console receives `assessment_ready` WS event with narrative feedback. Approve a suggested inject → it appears in the inject queue and can be released.

---

## Subsystem Decisions

Detailed design records for individual subsystems, extracted from CLAUDE.md to keep the
always-loaded instruction file lean. CLAUDE.md indexes these; read the relevant entry
before changing that subsystem. Keep them current as the code changes.

**Password policy (#13)**: `validate_password_strength` (`app/schemas/auth.py`, length-only, `MIN_PASSWORD_LENGTH = 12` / `MAX_PASSWORD_BYTES = 72`, rejects blank/whitespace-only) is applied via reusable `Password` / `OptionalPassword` `Annotated` types on `RegisterRequest` / `UpdateMeRequest`, so `register`/`update_me` return `422` automatically. NIST-aligned: length over character-class complexity (the max caps the unauthenticated register body and keeps every byte significant to bcrypt). Login does **not** apply the minimum-strength policy, so legacy short passwords still authenticate until changed; `verify_password` does enforce the bcrypt byte ceiling and normalises bcrypt input errors to an ordinary failed verification so the route records the attempt and returns the same invalid-credential response.

**Token revocation (#14)**: JWTs carry an `iat` claim (`auth_service.create_access_token`) and `User.token_valid_after` (nullable `timestamptz`) is a per-user revocation cutoff. `get_current_user` rejects any token whose `iat` predates `token_valid_after` (a missing or non-numeric `iat` is treated as revoked when a cutoff is set). `update_me` bumps `token_valid_after = now(UTC)` (truncated to whole seconds, so a freshly-minted token is not self-revoked) on password change and re-issues a fresh cookie so the caller's own session survives; all previously-issued tokens are revoked ("change password to kick out an attacker"). Deactivation (`is_active=False`) is already enforced per-request by `get_current_user`. The 8h token lifetime is unchanged (no refresh-token flow yet).

**Admin password reset (#66)**: `POST /api/users/{id}/reset-password` (`users.py`, `require_admin`) lets an admin set a temporary password on any **local** account — the recovery path for a locked-out user with no self-service email flow yet. It reuses the `Password` policy type (`AdminResetPasswordRequest`, 422 on weak), bumps the target's `token_valid_after` to revoke their existing sessions (same whole-second truncation as `update_me`), sets the new `User.must_change_password` flag (defaults on), and audits `admin.password_reset` (severity `warning`). SSO accounts (`auth_provider != "local"`) are rejected `400` — they have no local password. `must_change_password` rides on `UserResponse`/`UserPublic`. Enforcement is layered: the UI funnel — `sidebarNav.init()` (`app.js`) redirects a flagged user to `/settings`, where the password form (calling `update_me`, which clears the flag) unblocks them — plus a **server-side backstop** in `get_current_user` (`_enforce_password_change`, `dependencies.py`) that `403`s **state-changing** requests to everything outside the `/api/auth/*` namespace while the flag is set (reads stay open so the shell can load; the change-password/logout calls stay reachable). `update_me` re-issues the token **only in the httpOnly cookie, never the response body** (the client drops its stale `localStorage` bearer and falls back to the cookie, so a page-context script can't read the token); `sidebarNav` bootstraps from the cookie (no `dt_token` gate), which also fixes cookie-only SSO sessions. The admin console is `/admin/users` (`admin/users.html` + `pages/users.js`, `require_ui_admin`), listing accounts with a reset dialog.

**Inject comment threads**: Participants can post team-scoped discussion comments on released injects (`InjectComment` model; `app/routers/inject_comments.py` under `/api/exercises/{exercise_id}/inject-comments`). Comment visibility is group-scoped via `comment_group_for_user()` in `inject_comment_service.py` (resolved against `Inject.group_id` / the participant's exercise group / team); facilitators and observers see all comments, participants only their own group's. Only participants may post, only while the exercise is `active` and the inject is `released`/`resolved`. New comments broadcast over WebSocket (`inject_comment_created`), group-scoped when the comment has a `group_id`. Access checks live in the shared `app/services/access_control.py`.

**WebSocket auth (#68)**: browsers can't set the `Authorization` header on a WS upgrade, so `exercise_ws` authenticates from the httpOnly `access_token` **cookie** the browser already sends — keeping the JWT out of the URL (and out of proxy access logs). An explicit `?token=` query param remains an optional fallback for non-browser clients. The cookie path is ambient, so it gets a CSWSH `Origin` check via `origin_allowed()` (`middleware.py`, shared with `CSRFOriginMiddleware`) — closes `4003` on a foreign origin; the explicit-token path, like a Bearer header, is exempt. Both paths resolve the user through the shared `resolve_user_from_token()` (`dependencies.py`), so the WS path now also enforces `token_valid_after` revocation (#14). The client uses the shared `DT.connectExerciseWs()` helper (`static/js/app.js`): no token in the URL, **no `dt_token` localStorage gate** (SSO sessions have the cookie but never a `dt_token` — the old gate silently disabled live updates for OIDC users), and auth-refused close codes (4001/4003) are **terminal** — no 3s reconnect loop, since retrying can't succeed and each retry emits a server-side audit event. The three page components (`static/js/pages/{exercises,comms}.js`) pass only their `onMessage` handler and a `viewParams` flag. The `exercise_ws` handshake authenticates/authorises against its injected session, then `await session.close()`s it **before** entering the receive loop (the loop is DB-free) — otherwise the dependency-scoped session would hold a pooled connection idle-in-transaction for the whole socket lifetime and ~15 concurrent sockets would exhaust the pool (#35). `broadcast_to_groups` delivers to matching-group connections **plus facilitators and observers** (both have global read-visibility), so observers get live `inject_released`/comment pushes for group-scoped items, matching `is_inject_visible_to_user` (#38).

**Startup secret validation**: `validate_settings()` (`config.py`) is called from the lifespan startup and aborts the app if `secret_key` is unset, equal to the well-known default, or shorter than 32 chars — unless `DEV_MODE=true`. This prevents silently signing JWTs with a publicly-known key (#9). `dev_mode` also relaxes the Secure-cookie requirement for local HTTP. Tests set `DEV_MODE=true` in `conftest.py` before app import.

**Self-registration is participant-only**: `RegisterRequest` no longer accepts `role` — `POST /api/auth/register` always creates a `participant` (#8). Privileged roles are assigned out-of-band (seeded or via the admin create-user endpoint below); extra body fields are ignored by pydantic. The register template no longer offers a role selector.

**Registration controls (#67)**: `REGISTRATION_ENABLED` (default `true`) gates self-service registration. When `false`, `_require_registration_enabled()` (`auth.py`) makes `POST /api/auth/register` return `403` (with an `auth.register` `deny` audit event), the `/register` UI route redirects to `/login`, and the login page hides the "Register" link (`_auth_context()` passes `registration_enabled` to the template). Independently of the toggle, registration is flood-protected by a **second `RateLimiter` singleton** `registration_rate_limiter` (`rate_limit.py`), keyed **per source IP** and counting **every** attempt (not just failures — the email is what's being created, so it can't be part of the key); over `REGISTRATION_MAX_ATTEMPTS` (5) within `REGISTRATION_LOCKOUT_SECONDS` (3600, deliberately longer than login's 300 — this throttles account creation, not password guessing) the route returns `429` + `Retry-After`. The admin path is `POST /api/users` (`users.py`, `require_admin`): an admin provisions an account with any `role`/`is_admin` (schema `AdminCreateUserRequest`), bypassing both the toggle and the rate limit, audited as `admin.user_create`. Same single-process constraint as the login limiter; tests clear both via the conftest autouse fixture.

**Cookie security & CSRF**: the auth cookie is set with `Secure` (gated on `settings.cookies_secure`, default `not dev_mode`; override with `COOKIE_SECURE`). `CSRFOriginMiddleware` (`app/middleware.py`) verifies `Origin`/`Referer` for cookie-authenticated state-changing requests under `/api/` (#10). Bearer-`Authorization` requests and `/api/auth/*` are exempt — the app's own fetch calls use the localStorage Bearer token, so the cookie is effectively navigation-only. Extra allowed origins via `TRUSTED_ORIGINS`.

**Security headers (#77)**: `SecurityHeadersMiddleware` (`app/middleware.py`) emits the full security header set — the strict `CONTENT_SECURITY_POLICY` (`script-src 'self'`, no `unsafe-*`; `style-src` keeps `'unsafe-inline'` for dynamic `style=` attrs), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, a deny-all `Permissions-Policy`, `Cross-Origin-Opener-Policy`, and (only when `not dev_mode`) `Strict-Transport-Security`. `build_security_headers()` is a pure function evaluated per response (so `dev_mode` is monkeypatchable in tests). It uses **setdefault semantics** so the per-download `nosniff` on attachment downloads (#16) is preserved, not duplicated. Registered **outside** `CSRFOriginMiddleware` (so CSRF-blocked 403s still carry the headers) and **inside** `AuditContextMiddleware`. The **app is the single source of truth** — `docker/Caddyfile` and `k8s/caddy/configmap.yaml` set no security headers (only `nosniff` on the directly-served `/static/` location).

**Login brute-force protection**: `app/services/rate_limit.py` is an in-memory sliding-window limiter keyed by `ip:email`. After `LOGIN_MAX_ATTEMPTS` (default 5) failures within `LOGIN_LOCKOUT_SECONDS` (default 300) the login route returns `429` with `Retry-After`; a success resets the counter (#11). Oversized (>72 UTF-8 byte) bcrypt inputs and verifier value errors take this same counted failure path rather than escaping as a `500`. In-memory ⇒ single-process only (same constraint as `ws_manager`). Tests clear it via an autouse fixture.

**Trusted-proxy client IP (#36)**: `client_ip()` (`middleware.py`) returns `request.client.host` — the IP resolved by uvicorn's `ProxyHeadersMiddleware`, which rewrites the client from `X-Forwarded-For` **only** when the peer is in `--forwarded-allow-ips` / `FORWARDED_ALLOW_IPS`. This IP feeds both the audit `source_ip` and the login rate-limit key, so trusting only the Caddy hop (rather than the old hand-rolled leftmost-XFF parse) closes the spoofable brute-force bypass + audit poisoning. In k8s the Caddyfile sets `trusted_proxies static private_ranges` so Caddy preserves the Ingress's `X-Forwarded-*` chain. Launch commands pass `--proxy-headers`; the Docker/k8s images default `FORWARDED_ALLOW_IPS=*` because the app is reachable only through Caddy. Local `uvicorn --reload` (no proxy) leaves it unset so XFF is never trusted.

**Audit logging**: `app/services/audit_service.py` emits structured JSON audit events to the `iceberg_ttx.audit` logger (always) and, when `AUDIT_PERSIST` is set, to the append-only `AuditEvent` table (#23). `emit()` stays **synchronous** (it is called from many sync sites and must never raise); the DB write is now async, so `_persist()` schedules `_persist_async()` as a fire-and-forget task on the running event loop (references held in `_persist_tasks` to avoid GC; skipped if no loop is running — the JSON log line is the durable record). `emit()` sanitises all free-text against log injection (CR/LF/control chars). `AuditContextMiddleware` populates per-request "where" fields (request id, source IP, method, path) via a `ContextVar`; pass `actor=user` so the **actual** identity is logged even under facilitator role-preview. Wired at: login success/failure/lockout, register, logout, password change, token-validation failures, `authz.denied` (role + exercise access), inject release/delete, exercise lifecycle, member enrol/remove/group-change, exports, CSRF blocks, app startup/shutdown, and unhandled 500s. Secrets and payload bodies are never logged.

**Durable exercise lifecycle (#129)**: `transition_state_with_history()` performs a conditional state update against the caller's observed state and inserts `ExerciseStateTransition` in the same transaction. This makes completion terminal under stale/concurrent requests and gives start/pause/resume/complete an append-only domain history even when `AUDIT_PERSIST=false`; `timeline_service` reads that history directly. `exercises._transition()` publishes audit/SIEM and one canonical `exercise_state_change` frame (transition id, previous/new state, actor, timestamp, lifecycle timestamps) only after the commit returns. WebSocket failure is isolated from the already-committed domain change. Migration `9c4f2a7d1e30` recovers legacy history from persisted lifecycle audit events, falling back to exercise start/end timestamps where exact audit history is unavailable.

**Transactional inject and response operations (#125)**: The database is authoritative for response identity: `response(exercise_id, inject_id, user_id)` is unique, and an insert collision becomes a deterministic `409` after rollback. Inject release uses a `pending`-state compare-and-swap and performs timer cancellation, WebSocket delivery, and communication scheduling only after the winning transaction commits. Exercise creation flushes the parent and all seeded injects, then commits once; a seeding error rolls the entire unit of work back. Suggested-inject approval locks the pending suggestion and creates its Inject plus approval state in the same transaction, so a replay cannot produce a partial approval or duplicate inject.

**Triggered communications (#140)**: Scenario `triggers_communications` are logical scenario-node events, not per-team physical-inject events. A multi-team node can produce one Inject row per team, but each configured trigger is delivered once to the full exercise audience. The durable `(exercise_id, trigger_key)` constraint and conflict-safe insert make delayed workers/retries idempotent; `triggered_by_inject_id` retains attribution to the winning physical release. Delivery remains single-process/in-memory for delayed scheduling and is not rehydrated after restart.

**Group-aware scenario progression (#126)**: `ExerciseProgress` stores one authoritative cursor for the shared path and each scenario team; `InjectProgress` stores per-context release/resolution state. The first valid participant response resolves that group's inject context and advances its cursor in the same transaction as the response. A team-specific physical inject also mirrors that resolution into `Inject.state`/`resolved_at`; a shared inject stays released while other teams may still respond. Scenario-node release is allowed only when at least one applicable cursor points to that node (legacy exercises without cursor rows remain operable, and independent root nodes remain valid opening choices), preventing a group from releasing both sides of one branch while still allowing intentional cross-team divergence. `GET /api/exercises/{id}/progression`, response HTTP/WS payloads, the facilitator console, timeline, export, and report all read the same cursor/resolution records; participant responses receive only their own group context.

**OIDC / SSO (#25)**: adapter-based OpenID Connect (Authorization-Code + PKCE) via **Authlib**, running alongside or instead of local auth per `AUTH_MODE` (`local`|`oidc`|`both`, default both). The flow is provider-agnostic (Authlib handles discovery, PKCE `S256`, `state`/`nonce`, and JWKS ID-token validation of `iss`/`aud`/`exp`/`iat`/`nonce`); the small **adapter** layer (`app/services/oidc/`, a `key → adapter` registry) captures only per-provider claim mapping. Four providers ship: **Entra** (`entra.py`, stable `sub` + required tenant `tid`; email is verified only from an explicit `xms_edov`/`email_verified` assertion) and **Authentik / Auth0 / Okta**, which all reuse the shared **`StandardOIDCAdapter`** (`base.py`: standard `sub`/`email`/`email_verified`/`name`/groups) with only their metadata-URL construction differing in config (Authentik = base+slug, Auth0 = `https://<domain>/…`, Okta = org server or `/oauth2/<server>/…`; Auth0 roles need a namespaced custom-claim URI in `OIDC_AUTH0_ROLE_CLAIM`). Config builds one `OIDCProviderConfig` per enabled provider (`settings.enabled_oidc_providers()`); client secrets are **env-only** (`OIDC_*_CLIENT_SECRET`, never a DB column, never logged). Routes are `app/routers/oidc.py` (`/api/auth/oidc/{provider}/login|callback`); Authlib's transient handshake state lives in a short-lived Starlette `SessionMiddleware` cookie (`dt_oidc_session`, `same_site=lax`). `provision_oidc_user` (`service.py`) locks and matches returning users only on stable `(auth_provider, subject)` identity and verifies stored tenant provenance. Mutable `email`/`preferred_username` claims never auto-link: every collision is refused until an explicit safe link workflow exists. New users are JIT-created with `role_managed_by_idp=true`; the configured group→role map is synchronized on every returning login, so group removal or a missing/overage claim fails closed to participant. A role transition and token cutoff commit atomically before existing sessions are rejected and the user's live exercise sockets are closed. Operator-assigned roles (`bootstrap_admin`) and global admins are preserved as local overrides. On success the app mints the existing local session token (`create_access_token(subject=user.email, …)` + `_set_session_cookie`) so downstream authorization remains unchanged; the login page renders a "Sign in with …" button per provider. `User` stores nullable `hashed_password`, stable `auth_provider`/`subject`, optional `auth_tenant`, and role provenance; local login rejects passwordless SSO-only accounts. Events audited: `auth.oidc_login` (success/fail/deny), `auth.jit_provision`, and `auth.oidc_role_sync` — never with tokens/codes. Providers register at startup (`main` lifespan) and lazily (`ensure_registered`) so the test transport works without lifespan.

**SIEM forwarding (#24)**: the app is its own forwarder (no Vector/Fluent Bit sidecar). `app/services/siem_service.py` ships each event, off the response path, to the enabled sinks — `file` (append JSON line), `syslog` (RFC 5424 UDP/TCP), `http` (JSON POST to a Splunk HEC / Elastic / webhook endpoint, `Authorization: Bearer` from the **env-only** `SIEM_HTTP_TOKEN`, `verify` per config). `stdout` is the always-on baseline (the existing `iceberg_ttx.audit` handler) so a BYO node-level shipper can still tail it. `audit_service.emit()` gains `_ship()` which — like `_persist()` — reads routing from an **in-memory `SiemConfig` cache** (sync `emit` has no DB session) and `spawn()`s `siem_service.emit` on the running loop; each sink is `_safe`-wrapped so a dead/slow SIEM (5s timeouts) never raises or blocks the request. Routing lives in the admin-editable **`AuditSettings` singleton** (`app/models/audit_settings.py`, row id=1, no secret column) managed by `audit_settings_service.py`; the cache is loaded at startup (`main._load_siem_config`) and refreshed on every save. Admin API `app/routers/audit.py` (`/api/audit/events|settings|test`, gated by `require_admin` → real `User.is_admin` column) backs the **`/admin/audit`** page (event trail + SIEM config form + "send test event"); a UI-only `is_admin` JWT claim + `UserResponse.is_admin` gate the page shell / rail link, but the API always re-checks the DB column. Seeded from `SIEM_*` env (see `.env.example`); wired in compose + `k8s/configmap.yaml` (routing) + `k8s/secrets.yaml` (`SIEM_HTTP_TOKEN`). Single-process, like `ws_manager`/`rate_limit` (the persisted `AuditEvent` row remains the durable record on SIEM outage).

**Outbound proxy (#97)**: corporate egress proxying for the three outbound surfaces — the **LLM** API, the **SIEM `http`** sink, and **OIDC** discovery/JWKS. `app/services/proxy.py` is a pure `resolve(cfg, url) -> dict` returning httpx kwargs, with three modes on a `ProxyMode` StrEnum: `SYSTEM` → `{"trust_env": True}` (honour `HTTP(S)_PROXY`/`NO_PROXY`; the **default**, and exactly what httpx did implicitly before this feature, so upgrades are a no-op — a `proxy: None` key here would *override* the env proxy), `NONE` → always direct, `EXPLICIT` → route via `proxy_url` unless the target host matches the no-proxy list (standard `NO_PROXY` semantics: `*`, CIDR, domain+subdomain, exact). The **bypass decision is per target URL** because an httpx client takes a single `proxy` (no per-host `mounts`). **Caller contract**: `resolve_kwargs(url)` returns `{}` when the cache is unloaded, and every call site splats `**kwargs` last — so an unloaded cache is byte-for-byte pre-feature behaviour (each wiring test has a paired "unset" case). Routing lives in the admin-editable **`ProxySettings` singleton** (`app/models/proxy_settings.py`, row id=1, `mode` a plain string column, **no credential column**) managed by `proxy_settings_service.py`; **credentials are env-only** (`PROXY_USERNAME`/`PROXY_PASSWORD`), injected into the proxy URL's userinfo at call time by `_with_credentials()` and never persisted, returned, or logged. Like `SiemConfig`, an in-memory `ProxyConfig` cache is read by the **sync** `audit_service.emit` → SIEM path; loaded at startup by `main._load_proxy_config()`, which **must run before `register_providers()`** (OIDC bakes the resolved proxy into its Authlib `client_kwargs` at registration). A save invalidates both caches that captured the old proxy at construction: `reset_provider_cache()` (LLM adapters hold a long-lived SDK client, so the proxy is resolved once against the provider's base URL — Bedrock against the real `bedrock-runtime.<region>.amazonaws.com`, **not** the Anthropic host) and `oidc_service.reset_registration()` (Authlib's `register()` overwrites its `_registry` but `create_client()` returns the **cached** client, so re-registering alone would silently keep the old proxy — the reset rebinds a fresh `OAuth()`). All three SDKs take `http_client=`, incl. `AsyncAnthropicBedrock` (no botocore special-case). Admin API `app/routers/proxy.py` (`/api/proxy/settings|targets|test`, `require_admin`) backs the **`/admin/proxy`** page. The connectivity test takes a **target label, never a URL** — `egress_targets()` builds the label→URL map server-side from the configured LLM/SIEM/OIDC endpoints, so the route is not an SSRF oracle (CodeQL flagged the earlier free-text-URL form as critical); it returns only `ok: HTTP <status>` or `error: <ExceptionClass>`, with the exception *message* logged server-side after `_scrub()` strips the credentials an httpx error can echo from the proxy URL. The raw-socket `syslog` sink **cannot** be proxied. Seeded from `PROXY_*` env; wired in compose + `k8s/configmap.yaml` (routing) + `k8s/secrets.yaml` (credentials).

**Facilitator ownership scoping (#12)**: facilitator access to **exercises** is scoped per-exercise, not global. `require_exercise_access` (read gate) and `require_exercise_owner` (mutation gate) in `access_control.py` grant access only to: the creator (`Exercise.created_by`), a **co-facilitator** (a facilitator enrolled as an `ExerciseMember` — reuses the existing membership mechanism, no new field), or a **global admin** (`User.is_admin`, assigned out-of-band like the facilitator role — never via registration). Any other facilitator gets `403` + an `authz.denied` audit event. The same gates must run before nested-resource side effects: inject deletion, assessment reads/queueing, and suggested-inject list/approve/reject routes are explicitly covered alongside injects/responses/communications/inject-comments/ws and the mutation/lifecycle/member/export routes in `exercises.py`; `GET /exercises` is filtered to owned-or-member (admins see all). **Scenarios remain a shared library** (any facilitator lists/reads/edits/exports — intentional, they're reusable templates), and `GET /users` stays facilitator-wide (it's the member-enrolment picker). `is_admin` is a real column so it survives role-preview `model_copy` and is unspoofable.

**LLM integration (pluggable providers, #26)**: The AI backend is pluggable via an adapter/registry that mirrors the OIDC pattern (`app/services/llm/`). `LLM_PROVIDER` selects the single active provider for the whole app (the AI analog of `AUTH_MODE`): `anthropic` | `bedrock` | `openai` | `ollama` | `gemini` | `none`. `base.py` holds the `LLMProvider` Protocol (`key`, `model`, `llm_model_label`, `async complete(system, cached_context, user_prompt, max_tokens)`) + a family registry (`register_adapter`/`get_adapter`); two adapters cover all five providers — `AnthropicFamilyAdapter` (`anthropic_provider.py`, family `"anthropic"`) handles direct Anthropic **and** Bedrock (same `messages.create` surface; differ only in client construction — `AsyncAnthropic` vs `AsyncAnthropicBedrock` — and the `anthropic.`-prefixed Bedrock model ID), and `OpenAICompatAdapter` (`openai_provider.py`, family `"openai"`) handles OpenAI, Ollama, and Gemini (all via the OpenAI Chat Completions surface, differing only by `base_url`/model/key). **Prompt caching** (`cache_control` block + `anthropic-beta` header) is applied **only** on the direct-Anthropic path; Bedrock omits it and the OpenAI-compat adapter concatenates the cached context into the user message. `service.py` force-imports the adapter modules (registration side-effects), then `active_provider()` builds/caches the provider from `settings.active_llm_provider()`. `config.py` resolves flat env vars (`ANTHROPIC_*`/`BEDROCK_*`/`OPENAI_*`/`OLLAMA_*`/`GEMINI_*`, `LLM_MAX_TOKENS`) into an `LLMProviderConfig`; `validate_settings()` rejects an unknown `LLM_PROVIDER` and (outside dev) a selected provider missing its credentials. Every provider's SDK is a **lazy-imported optional extra** — no LLM SDK is a core dependency, so all providers are on equal footing (`pip install '.[llm-anthropic]'` for direct Anthropic, `'.[llm-bedrock]'` pulls boto3, `'.[llm-openai]'` covers openai/ollama/gemini; `'.[llm-all]'` bundles all, and the `dev` extra includes them). An unconfigured provider never needs its SDK; the adapters raise a clear "install extra X" error if the selected provider's SDK is absent. `llm_service.py` is now provider-agnostic: `run_llm_pipeline` opens its own `AsyncSession(engine)` (same pattern as `_delayed_comm`) and re-checks `Exercise.llm_enabled` plus the response/inject/exercise relationships immediately before provider use; the manual route also requires exercise ownership and opt-in. `queue_llm_pipeline` deduplicates automatic/manual work per response within the supported single-replica model, and persisted assessments make retries no-ops. Provider results stamp `ResponseAssessment.llm_model`/`SuggestedInject.llm_model` with `provider.llm_model_label` (e.g. `"anthropic:claude-opus-4-8"`). Tests mock at the `active_provider` seam (`tests/test_llm.py`) plus per-adapter/config coverage (`tests/test_llm_providers.py`) — no real network requests.

**Communications state guards**: participant outbound `send_comm` requires the exercise to be `active` (409 otherwise), consistent with response `submit` and inject-comment `create_comment` (#40). Facilitator `inject_comm` (simulated inbound) is **intentionally** unrestricted so facilitators can seed comms during `draft`/`paused` setup.

**Communication read receipts**: reading a communication is an explicit, idempotent
`PUT /api/exercises/{exercise_id}/communications/{communication_id}/read` operation.
`GET` is side-effect free. `CommunicationRead` stores one immutable first-read
timestamp per `(communication_id, user_id)`; concurrent readers cannot overwrite
one another, retries retain the first timestamp, communication deletion cascades
its receipts, and user deletion removes that user's receipts. Inbox payloads expose
only the current viewer's `is_read` / `read_at` state rather than other users' IDs.
The list route loads viewer read state in one batch query.

**Group-scoped injects**: `Inject.group_id` and `ExerciseMember.group_id` allow injects to be targeted at specific exercise groups (teams). When `group_id` is `None` the inject is visible to all groups. The inject router resolves group membership via `exercise_group_for_user()` at query time.

**Attendance role snapshots**: `ExerciseMember.role_at_enrolment` records the user's
global role when they are enrolled. After-action reports count participants,
facilitators, and observers from that immutable snapshot, so later account-role
changes do not rewrite historical attendance. Removing and re-enrolling a member
captures a new role snapshot. Participant team counts exclude facilitators and
observers and include an explicit unassigned/legacy-team bucket so their breakdown
always sums to the participant total.

**File attachments on injects**: Injects support a single file attachment (`attachment_filename`, `attachment_path`, `attachment_content_type`, `attachment_size` on the `Inject` model). Files are stored under `uploads/inject_attachments/{exercise_id}/`. The inject router accepts `multipart/form-data`; `inject_attachment_payload()` builds the download URL returned in the inject payload. Uploads stream to disk in chunks and abort once `MAX_ATTACHMENT_BYTES` (25 MB) is exceeded, so an oversized upload is never fully buffered (#39). Content-type is confined to `ALLOWED_ATTACHMENT_TYPES` (`_normalize_content_type` — anything else, e.g. `text/html`/`image/svg+xml`, is stored and served as `application/octet-stream`), applied on both upload and download; downloads set `X-Content-Type-Options: nosniff` alongside the `Content-Disposition: attachment` implied by `filename` (#16).

**Role preview**: Facilitators can view the app as a participant or observer via `dt_view_role` and `dt_view_team` cookies (set from `/settings`). `_optional_user()` in `ui.py` reads these cookies and overrides the Jinja2 template role/team — but only when the JWT already contains the `facilitator` role, so API calls are never downgraded.

**Dark mode**: `data-theme="dark"` on `<html>` re-declares the oklch token set (dark surfaces) in `iceberg.css`. `static/js/theme-boot.js` (an external, synchronous `<script>` at the top of `<head>`, not inline — strict CSP, #77) resolves the saved theme (`system`→OS via `prefers-color-scheme`) and stamps `data-theme` before first paint (prevents FOUC). Preference stored in `dt_theme`/`dt_resolved_theme` cookies + `localStorage`, toggled (light/dark/system) from `/settings`. The per-user **accent picker was removed** during the Iceberg alignment — the cyan accent is fixed to match the sibling apps (`dt_accent` is no longer read or written).

**Sample scenarios**: `app/samples/` contains bundled JSON scenario definitions (`ransomware_response.json`, `vendor_outage.json`). `app/services/sample_service.py` lists, validates, and loads them. The settings page exposes a sample loader UI for facilitators. `get_sample_definition` validates `sample_id` against `SAMPLE_ID_RE` (`^[A-Za-z0-9_-]+$`) and asserts the resolved path stays within `SAMPLES_DIR` before reading, preventing directory traversal via the `sample_id` path param (#15); a rejected id returns `None` → the settings routes surface `404`.

**Containerized deployment**: `Dockerfile` is a two-stage build — stage 1 compiles Tailwind CSS (`pytailwindcss`), stage 2 is the Python runtime. The compiled `static/` directory is also copied to `static_src/` in the image; this path is never overridden by a volume mount and is used by entrypoint scripts (Docker Compose) and init containers (k8s) to populate shared static volumes so Caddy always serves the version matching the running image. **Reverse proxy is Caddy**: in `docker-compose.yml` Caddy (`caddy:2-alpine`) is the edge and terminates TLS itself with **automatic HTTPS** (`SITE_ADDRESS` env — a domain ⇒ Let's Encrypt, default `localhost` ⇒ internal self-signed CA; certs persist in the `caddy_data` volume). It runs **non-root** (`user: 1000:1000`, `cap_drop: ALL` + `cap_add: NET_BIND_SERVICE`, read-only rootfs) — the official image defaults to root; the caddy binary carries a `cap_net_bind_service` **file capability**, so `NET_BIND_SERVICE` must stay in the bounding set everywhere the image runs with dropped caps (compose *and* the k8s deployment) or exec of the setcap binary fails EPERM — and a one-shot `caddy-init` service chowns the root-initialised cert/config volumes (compose's equivalent of the k8s `fsGroup`). Both Caddyfiles bound a hung upstream (`dial_timeout 10s` / `response_header_timeout 60s` — headers-only, so WS sockets are unaffected), serve `/static/` with `max-age=604800, immutable` + `log_skip`, and scrub the `token` query param from access logs. In **k8s** Caddy is instead an internal plain-HTTP reverse proxy on `:8080` (`k8s/caddy/`), and TLS stays terminated at the cluster **Ingress** (`k8s/caddy/ingress.yaml`, cert-manager) — unchanged from the previous nginx setup. `docker-compose.yml` runs `app` + `postgres:17` + `caddy` on a private bridge network with named volumes for DB data, uploads, static files, and Caddy's cert/config stores. `k8s/` contains namespace, secrets, configmap, postgres StatefulSet, app Deployment, caddy Deployment, a TLS `Ingress`, and ingress `NetworkPolicy`s (`k8s/networkpolicy.yaml`). **Security posture (IaC review)**: all containers run non-root under a PSS-`restricted`-style `securityContext` (no priv-esc, all caps dropped, `RuntimeDefault` seccomp; app/init/caddy containers use a read-only rootfs with emptyDir/`tmpfs` writable mounts); the k8s Caddy listens on 8080 and its Service is `ClusterIP` fronted by the TLS Ingress (never a plaintext `:80` LoadBalancer); `automountServiceAccountToken: false` on every pod. Compose mirrors this (`no-new-privileges`, `cap_drop: ALL`, non-root `user:` on caddy, read-only rootfs). **Replica constraint**: `ws_manager.py` is in-memory only — app must run as a single replica until Redis pub/sub is added. k8s manifests enforce `replicas: 1` and `strategy: Recreate`. The async `asyncpg` driver is a core dependency (no separate extra; the `DATABASE_URL` may be a plain `postgresql://` URL — it is upgraded to `asyncpg` at runtime). Health probes (`app/routers/health.py`): `GET /api/health` is a DB-free unconditional 200 backing the k8s **liveness** probe (a DB outage must not restart pods — that would crash-loop through startup migrations), while `GET /api/health/ready` runs a short-timeout `SELECT 1` on the async engine and returns **503** when Postgres is unreachable, backing the k8s **readiness** probe and the compose app healthcheck so a pod with a dead DB is pulled from the endpoint set instead of serving 500s (#71).
