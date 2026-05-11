# Deep Thought — Implementation Plan

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
deep_thought/
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

### Users `/users` (facilitator only)
- `GET /users`, `GET /users/{id}`, `PUT /users/{id}`, `DELETE /users/{id}`

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
- Branching is "pull not push" — facilitator manually releases the next inject after seeing which branch was triggered
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
