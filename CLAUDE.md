# IcebergTTX Agent Instructions

## Scope and references

IcebergTTX runs tabletop exercises: a facilitator releases scenario-defined injects to
participants/teams, who respond; branching follows participant decisions. Python 3.14 +
FastAPI (fully async), SQLModel on PostgreSQL (asyncpg), Jinja2 + Tailwind v4 + Alpine
(CSP build), JWT auth, WebSockets.

**Read the relevant section of [docs/AGENT_ARCHITECTURE.md](docs/AGENT_ARCHITECTURE.md)
before changing a subsystem** — it holds the architecture, design decisions, and project
structure. Subsystem deep-dives are indexed there and live in [PLAN.md](PLAN.md)
§ Subsystem Decisions.

## Development rules

- Dependencies via **uv** against the committed `uv.lock`. After changing deps:
  `uv lock` and commit the result (`uv lock --check` gates CI).
- Everything touching the DB is async; background workers use
  `async with AsyncSession(engine)`. Datetime columns are
  `Field(sa_type=DateTime(timezone=True))` with `datetime.now(UTC)` values.
- Schema changes go through Alembic (`alembic revision --autogenerate -m "..."`, needs
  a running Postgres). Hand-written migrations need a **unique** `revision` id — a
  collision surfaces as a misleading `Cycle is detected in revisions` error; grep
  `alembic/versions/` first. Migrations self-apply on app startup (`alembic upgrade head`
  applies them without starting the app).
- Passwords: `bcrypt` directly, never `passlib` (broken on Python 3.14).
- **Strict CSP** (`script-src 'self'`): no inline `<script>`, ever. Alpine is the CSP
  build — directive attributes cannot use `?.`, arrows, `x-html`, or page globals; put
  logic in component methods (`static/js/pages/*.js`), pass server data as `|tojson`
  factory args.
- Rebuild Tailwind after template/CSS/JS changes:
  `tailwindcss -i static/css/input.css -o static/css/output.css` (`output.css` untracked —
  compile before any direct Uvicorn/Playwright run).
- Never broadcast WS frames inline from services: `record(session, Event(...))` in the
  transaction; **whoever commits dispatches** (`await dispatch(session)`, session open).
- Services own queries; routers authorize/call/serialize; `app/schemas/` holds only
  boundary-crossing models (placement rule #214).
- **Single-replica app**: all cross-request state (ws_manager, timers, rate limits,
  config caches) is in-process. Read that section before adding any.
- New UI control classes must re-assert the 40px min-height floor (class selectors beat
  the global floor; a Playwright touch-target test enforces ≥40/44px).
- Branching wording trap: **participants choose the path; the facilitator controls the
  pace** — never "the facilitator chooses the branch" (shipped as a docs bug twice).
  Read the branching section before touching progression or its docs.

## Development setup

```bash
uv sync --extra dev             # creates .venv from uv.lock (+ dev tools)
cp .env.example .env            # set SECRET_KEY
uv run uvicorn app.main:app --reload   # migrates to head on startup
```

## Testing

`uv run pytest` — async Pytest, one file per resource, fixtures in `tests/conftest.py`.
Full prose in the architecture doc § Testing; the traps:

- Real Postgres via `testcontainers` (`postgres:17`), started before app import;
  `DATABASE_URL_OVERRIDE_FOR_TESTS` targets an external Postgres instead.
- `httpx.AsyncClient` + `httpx-ws` over `ASGIWebSocketTransport` (not `TestClient`);
  wrap every expected WS receive in `asyncio.wait_for` so a missing frame fails, not hangs.
- Savepoint-rollback isolation; session-scoped client (one cookie jar). Parallel by
  default (`-n auto`, one container per xdist worker); debug with `pytest -n0`.
- `tests/test_ui.py` (Playwright) **skips silently** without a server on `:8765` — green
  local pytest does not mean it ran. Serve on `:8765` with
  `DATABASE_URL_OVERRIDE_FOR_TESTS` pointed at the server's DB, else everything 403s.
- Autouse fixtures in `conftest.py` must be **sync** — an async one fails all 35
  Playwright tests (`Runner.run() cannot be called from a running event loop`), CI-only.
- Background tasks opening their own `AsyncSession(engine)` can't see uncommitted
  savepoint rows: monkeypatch that module's `AsyncSession` to the test session; assert on
  returned/broadcast payloads (the worker's commit expunges preloaded instances). Never
  combine that monkeypatch with a live 0-delay timer (asyncpg `another operation is in
  progress` / teardown `CancelledError`) — spy `schedule_service._arm` instead.

## GitHub and maintenance

- **PRs merge via `icebergai-review-bot`, so `Closes #n` never auto-closes** — the link
  registers but the issue stays open. After any merge: `gh issue view <n> --json state`,
  then `gh issue close <n>` by hand.
- Screenshots (`docs/*.png`, `website/docs/assets/*.png`) all show the app shell; after
  rail/topbar changes regenerate all 13 via
  `uv run python scripts/screenshots.py --base https://localhost --insecure` against a
  running compose stack — never recapture by hand.
- Keep README.md, this file, the architecture doc, and PLAN.md current. New always-binding
  rules go here (this file loads every session — keep it small); architecture and
  cross-cutting decisions go in docs/AGENT_ARCHITECTURE.md; subsystem deep-dives in
  PLAN.md § Subsystem Decisions plus an index line in the architecture doc.
