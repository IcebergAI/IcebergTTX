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

**Database**: SQLite for now. No migrations tool — `SQLModel.metadata.create_all()` on startup. Migrate to Postgres if multi-process deployment is needed.

**Password hashing**: Uses `bcrypt` directly (not `passlib`). `passlib[bcrypt]` is incompatible with Python 3.14 due to a `bcrypt.__about__` removal in bcrypt 4.x.

**Scenario storage**: The full scenario definition (inject tree, branching options, team targets, triggered communications) is stored as a single JSON blob in `Scenario.definition`. Validated against the `ScenarioDefinition` Pydantic model on every write. Not normalised into rows — the tree is always read and written as a unit.

**Scenario branching**: "Pull not push" — when a participant responds, the service resolves which inject IDs are valid next steps, but the facilitator manually reviews and releases the chosen branch. This keeps human control in the loop.

**JWT auth**: Stored in both an `httpOnly` cookie (for page navigation/Jinja2 routes) and `localStorage` (for Alpine/fetch calls). The `get_current_user` FastAPI dependency checks both; the `Authorization` header takes precedence.

**WebSocket auth**: JWT passed as `?token=<jwt>` query parameter. Browsers cannot set the `Authorization` header on WebSocket upgrade requests.

**LLM integration**: Uses `anthropic>=0.40` (`AsyncAnthropic`). Prompt caching applied via the `anthropic-beta: prompt-caching-2024-07-31` header — scenario context + inject content is the cached prefix; participant response text is the non-cached suffix. `run_llm_pipeline` is a background async task (fired via `asyncio.create_task`) that opens its own Session from `engine` directly (same pattern as `_delayed_comm`). The `Exercise.llm_enabled` flag gates LLM calls per exercise. Set `ANTHROPIC_API_KEY` in `.env` to enable. All API calls are mocked in `tests/test_llm.py` — no real network requests.

**Tailwind**: CDN during development (Phases 1–6); switched to CLI-compiled `static/css/output.css` in Phase 7. Rebuild after template changes: `tailwindcss -i static/css/input.css -o static/css/output.css`. Tailwind v4 auto-scans `app/templates/**/*.html` — no config file needed.

**Frontend design system**: Warm stone palette with CSS custom properties (`--bg`, `--paper`, `--ink`, `--accent`, etc.) defined in `base.html`. IBM Plex Sans + IBM Plex Mono via Google Fonts. Custom utility classes (`.smallcaps`, `.mono`, `.pill`, `.btn-*`, `.node`, `.briefing`, `.live-dot`) live in the `<style>` block of `base.html` — not Tailwind utilities. Persistent dark sidebar (`w-56`, `position: sticky`) replaces the old top nav bar. Sidebar hides itself on auth pages via `x-show="!!user"` (no token → `user: null`).

**Communications delay**: `triggers_communications` in the scenario JSON fire via `asyncio.create_task(asyncio.sleep(...))` — sufficient for single-process. Would need a task queue (e.g. Celery, ARQ) for multi-process deployment.

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
tests/               # Pytest suite (conftest.py + one file per resource)
static/css/          # output.css (Tailwind compiled, Phase 7+)
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

Current test count: **123 passing**.

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
