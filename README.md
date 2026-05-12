# Deep Thought

[![CI](https://github.com/richardmhope/deep_thought/actions/workflows/ci.yml/badge.svg)](https://github.com/richardmhope/deep_thought/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.14%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688)

A tabletop exercise (TTX) platform for running cyber incident and business resilience scenarios.

![Deep Thought facilitator console](docs/screenshot.png)

## Features

- **Scenario library** — build or import JSON scenario files with branching inject trees
- **Live exercises** — facilitator releases injects in real time via WebSocket push
- **Participant responses** — free-text and multiple-choice, driving scenario branches
- **Simulated communications** — two-pane inbox/outbox for regulatory, press, and executive comms
- **LLM assessment** — Claude evaluates participant decisions and suggests follow-up injects
- **Role-based access** — facilitator, participant, and observer roles
- **Role preview** — facilitators can view the app as a participant or observer without changing accounts
- **Sample templates** — optional bundled scenarios can be loaded from Settings; the database stays empty by default
- **Export** — transcript (JSON), responses (CSV), and AI assessments (JSON)

## Tech Stack

- **Backend**: Python 3.14+, FastAPI, SQLModel, SQLite
- **Frontend**: Jinja2 templates, Tailwind CSS v4 (CLI-compiled), Alpine.js
- **Real-time**: WebSockets (FastAPI native)
- **Auth**: JWT tokens (httpOnly cookie + localStorage)
- **LLM**: Anthropic Claude API (`anthropic>=0.40`, async with prompt caching)

## Setup

```bash
# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Install dependencies
pip install -e ".[dev]"

# Configure environment
cp .env.example .env
# Edit .env — set SECRET_KEY (required) and ANTHROPIC_API_KEY (optional, for LLM features)

# Run the development server
uvicorn app.main:app --reload
```

Open [http://localhost:8000](http://localhost:8000). Register a facilitator account, then create a scenario and exercise. To try the app quickly, open Settings and load a sample scenario or demo exercise. In-app help is available at [/help](http://localhost:8000/help).

## Running Tests

```bash
pytest
pytest tests/ --ignore=tests/test_ui.py   # skip live Playwright tests
```

## Rebuilding CSS

After editing templates, rebuild the Tailwind output:

```bash
tailwindcss -i static/css/input.css -o static/css/output.css
```

## Project Structure

```
app/
├── main.py          # App factory + lifespan
├── config.py        # Settings (pydantic-settings, reads .env)
├── database.py      # SQLite engine + get_session dependency
├── dependencies.py  # FastAPI dependencies (auth, role guards)
├── models/          # SQLModel table definitions
├── schemas/         # Pydantic request/response schemas
├── routers/         # FastAPI routers (one per resource) + ui.py (Jinja2 pages)
├── services/        # Business logic (auth, scenario, exercise, inject, response, comms, llm, ws_manager)
├── samples/         # Bundled quick-start scenario templates (loaded only on demand)
└── templates/       # Jinja2 HTML templates
    ├── base.html            # Persistent dark sidebar, CSS vars, shared JS helpers
    ├── dashboard.html       # Command center
    ├── help.html            # In-app help & documentation
    ├── settings.html        # Profile, theme, role preview, and sample loader
    ├── auth/                # login.html, register.html
    ├── scenarios/           # list, detail, editor
    ├── exercises/           # list, facilitator console, participant view
    └── communications/      # inbox
tests/               # Pytest test suite (conftest.py + one file per resource)
static/css/          # output.css (Tailwind CLI compiled)
revised/             # Claude Design prototype (reference only)
```

## Quick Workflow

1. **Create a scenario** — Scenarios → New, or import a JSON file
2. **Create an exercise** — Exercises → New, select a scenario, optionally enable LLM
3. **Enrol participants** — Facilitator console → Participants panel, search and add users
4. **Start and release injects** — Hit Start, then Release each inject when ready
5. **Review responses** — Middle pane; choose which branch to release next
6. **Complete and export** — Complete button, then export transcript/responses from the right pane

See [/help](/help) for full documentation including the scenario JSON schema.
