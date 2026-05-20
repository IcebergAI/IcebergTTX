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

- **Backend**: Python 3.14+, FastAPI, SQLModel, SQLite (dev) / PostgreSQL (containers)
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

## Docker Deployment

A `docker-compose.yml` is provided for single-host deployments. It runs the app, PostgreSQL 17, and nginx as a reverse proxy.

```bash
# Copy and fill in secrets (POSTGRES_PASSWORD and SECRET_KEY are required)
cp .env.example .env

# Build and start
docker compose up -d

# Check all three services are healthy
docker compose ps
```

The app will be available on port 80. nginx serves static files directly and proxies everything else (including WebSocket upgrades at `/ws/`) to uvicorn.

To stop without losing data:
```bash
docker compose down        # keeps named volumes (postgres_data, uploads)
docker compose down -v     # also deletes volumes — permanent data loss
```

## Kubernetes Deployment

Manifests are in `k8s/`. Apply in order:

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/secrets.yaml k8s/configmap.yaml

# Before applying, replace placeholder values in k8s/secrets.yaml
# and replace 'your-registry/deep-thought:latest' in:
#   k8s/app/deployment.yaml
#   k8s/nginx/deployment.yaml

kubectl apply -f k8s/postgres/
kubectl rollout status statefulset/postgres -n deep-thought

kubectl apply -f k8s/app/
kubectl rollout status deployment/deep-thought-app -n deep-thought

kubectl apply -f k8s/nginx/
kubectl rollout status deployment/nginx -n deep-thought
```

> **Note**: The app must run as a single replica (`replicas: 1`) until the in-memory WebSocket manager is replaced with a distributed backend (e.g. Redis pub/sub). The manifests enforce this with `strategy: Recreate`.

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
├── database.py      # SQLite / Postgres engine + get_session dependency
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
Dockerfile           # Multi-stage build (Tailwind compile + Python runtime)
docker-compose.yml   # app + postgres:17 + nginx:alpine
docker/nginx.conf    # Reverse proxy config with WebSocket upgrade support
k8s/                 # Kubernetes manifests (namespace, secrets, postgres, app, nginx)
```

## Quick Workflow

1. **Create a scenario** — Scenarios → New, or import a JSON file
2. **Create an exercise** — Exercises → New, select a scenario, optionally enable LLM
3. **Enrol participants** — Facilitator console → Participants panel, search and add users
4. **Start and release injects** — Hit Start, then Release each inject when ready
5. **Review responses** — Middle pane; choose which branch to release next
6. **Complete and export** — Complete button, then export transcript/responses from the right pane

See [/help](/help) for full documentation including the scenario JSON schema.
