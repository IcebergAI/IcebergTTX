# IcebergTTX

[![CI](https://github.com/IcebergAI/IcebergTTX/actions/workflows/ci.yml/badge.svg)](https://github.com/IcebergAI/IcebergTTX/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.14%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688)

A tabletop exercise (TTX) platform for running cyber incident and business resilience scenarios.

![IcebergTTX facilitator console](docs/screenshot.png)

## About

IcebergTTX helps teams rehearse their response to cyber incidents and other
disruptions through facilitated, scenario-driven tabletop exercises. A
**facilitator** builds or imports a branching scenario, then releases injects to
**participants** in real time; participants record decisions and free-text
responses that drive the scenario down different branches, while **observers**
follow along read-only. The platform simulates incident communications (regulators,
press, executives) and can use the Claude API to assess participant decisions and
suggest follow-up injects. It is API-first (FastAPI) with a server-rendered UI, and
ships with Docker Compose and Kubernetes deployment manifests.

## Features

- **Scenario library** — build or import JSON scenario files with branching inject trees (or linear chained flows)
- **Live exercises** — facilitator releases injects in real time via WebSocket push
- **Participant responses** — free-text and multiple-choice, driving scenario branches
- **Team comment threads** — participants discuss released injects in group-scoped comment threads
- **Simulated communications** — two-pane inbox/outbox for regulatory, press, and executive comms
- **LLM assessment** — Claude evaluates participant decisions and suggests follow-up injects
- **Role-based access** — facilitator, participant, and observer roles (self-registration always creates a participant; elevation is out-of-band)
- **Role preview** — facilitators can view the app as a participant or observer without changing accounts
- **Security hardening** — enforced SECRET_KEY at startup, Secure cookie + CSRF origin checks, login rate limiting, and structured audit logging with off-host SIEM forwarding (syslog / HTTP / file)
- **Sample templates** — optional bundled scenarios can be loaded from Settings; the database stays empty by default
- **Export** — transcript (JSON), responses (CSV), and AI assessments (JSON)

## Screenshots

The hero above is the **facilitator console** — a live exercise with a team-grouped inject tree, one-at-a-time release, and participant responses.

| Command center | Scenario inject tree |
|---|---|
| ![Dashboard with the live-exercise hero](docs/dashboard.png) | ![Scenario detail with branching injects and validation](docs/scenario-detail.png) |
| **Simulated communications** | **Participant view** |
| ![Communications inbox with an inbound regulator message](docs/communications.png) | ![Participant briefing and responses](docs/participant.png) |

The UI ships with a light/dark theme toggle (system-aware):

![Facilitator console in dark mode](docs/facilitator-dark.png)

## Tech Stack

- **Backend**: Python 3.14+, FastAPI (fully async), SQLModel + async SQLAlchemy, PostgreSQL (asyncpg)
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
# Edit .env:
#   SECRET_KEY        required — generate: python -c "import secrets; print(secrets.token_hex(32))"
#   DEV_MODE=true     for local HTTP development (relaxes the SECRET_KEY check and the Secure cookie flag)
#   ANTHROPIC_API_KEY optional — enables LLM features
# Outside DEV_MODE the app refuses to start if SECRET_KEY is unset, the default, or shorter than 32 chars.

# Run the development server
uvicorn app.main:app --reload
```

Open [http://localhost:8000](http://localhost:8000). Register an account (self-registration always creates a **participant**), then promote it to facilitator out-of-band — e.g. in a Python shell:

```python
import asyncio
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from app.database import engine
from app.models.user import User, UserRole


async def promote(email: str) -> None:
    async with AsyncSession(engine) as s:
        u = (await s.exec(select(User).where(User.email == email))).one()
        u.role = UserRole.facilitator
        s.add(u)
        await s.commit()


asyncio.run(promote("you@example.com"))
```

As a facilitator, create a scenario and exercise. To try the app quickly, open Settings and load a sample scenario or demo exercise. In-app help is available at [/help](http://localhost:8000/help).

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

The app will be available on port 80. nginx (the unprivileged image, running as a non-root user and listening on 8080 inside the container) serves static files directly and proxies everything else (including WebSocket upgrades at `/ws/`) to uvicorn.

> **TLS**: compose serves plain HTTP on `:80` for local use. In production, put a TLS-terminating proxy or load balancer in front — the app sets `Secure` auth cookies, which browsers won't send over plain HTTP. Never expose the auth flow over unencrypted HTTP.

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

# Before applying, replace placeholder values in k8s/secrets.yaml,
# replace 'your-registry/iceberg-ttx:latest' in:
#   k8s/app/deployment.yaml
#   k8s/nginx/deployment.yaml
# (pin by digest — image@sha256:… — in production for reproducible rollouts;
#  Dependabot's docker updater keeps the base-image digests current), and set
# the hostname/issuer/ingressClassName placeholders in k8s/nginx/ingress.yaml.

kubectl apply -f k8s/postgres/
kubectl rollout status statefulset/postgres -n iceberg-ttx

kubectl apply -f k8s/app/
kubectl rollout status deployment/iceberg-ttx-app -n iceberg-ttx

kubectl apply -f k8s/nginx/          # Deployment + ClusterIP Service + TLS Ingress
kubectl rollout status deployment/nginx -n iceberg-ttx

# Confine east-west traffic (requires a NetworkPolicy-enforcing CNI):
kubectl apply -f k8s/networkpolicy.yaml
```

> **TLS**: the nginx Service is `ClusterIP`; `k8s/nginx/ingress.yaml` terminates HTTPS (cert-manager annotation + `force-ssl-redirect`) and forwards to it. Fill in the hostname, `ingressClassName`, and issuer before applying. Do **not** switch nginx back to a `LoadBalancer` on `:80` — that serves auth over plaintext.
>
> **Pod hardening**: all three workloads run non-root under a PSS-`restricted`-style `securityContext` (no privilege escalation, all capabilities dropped, `RuntimeDefault` seccomp; app + init containers use a read-only root filesystem). The Postgres StatefulSet runs as uid 999 with `fsGroup: 999`, which needs a StorageClass that honours `fsGroup`.

> **Note**: The app must run as a single replica (`replicas: 1`) until the in-memory WebSocket manager is replaced with a distributed backend (e.g. Redis pub/sub). The manifests enforce this with `strategy: Recreate`.

## Forward security events to your SIEM

Audit events (logins, authorization denials, privilege/role changes, inject
release/delete, exports, config changes, …) can be shipped off-host to a SIEM so
they are centralized, retained, and alertable even if a pod restarts or the app
is compromised (#24). **The app is the forwarder** — there is no Vector/Fluent Bit
sidecar to run.

- **Enable it live** at `/admin/audit` (admin only): toggle *Auditing enabled*,
  pick the methods, set the minimum severity, and click **Send test event** to
  verify connectivity end-to-end.
- **Methods** (any combination):
  - `stdout` — the always-on JSON baseline on the `iceberg_ttx.audit` logger; a
    node-level shipper (Filebeat/Fluentd/Vector) you provide can tail it.
  - `file` — append JSON lines to a path (for a file-tailing shipper).
  - `syslog` — RFC 5424 over UDP/TCP; point TCP at a TLS syslog collector for
    secure transit.
  - `http` — JSON `POST` to a Splunk HEC / Elastic / generic webhook endpoint,
    authenticated with a bearer token.
- **Secret handling**: the HTTP bearer token is set **only** via the
  `SIEM_HTTP_TOKEN` env var / Secret — it is never stored in the database, never
  returned by the API, and never logged.
- **Reliability**: a slow or unreachable SIEM (5-second timeouts) **never blocks
  or fails a request** — each sink is failure-isolated, and the persisted
  `AuditEvent` table (`AUDIT_PERSIST=true`) remains the durable record. Ensure
  hosts are NTP-synced to UTC so SIEM correlation is accurate.

Seed the defaults from the `SIEM_*` env vars (see `.env.example`); routing is then
edited live from `/admin/audit`. In Kubernetes the non-secret routing lives in
`k8s/configmap.yaml` and the token in `k8s/secrets.yaml`.

**Example alert rule** (brute-force detection, ties to the login rate limiter
#11) — Splunk SPL over the shipped events:

```spl
index=iceberg_ttx action="auth.login" result="fail"
| bin _time span=5m
| stats count by _time, source_ip
| where count >= 5
```

Alert when a single `source_ip` produces ≥ 5 failed `auth.login` events in a
5-minute window. Comparable rules are worth configuring for `authz.denied`
spikes, `audit.settings_updated` / role changes, and unexpected `*.export` events.
Treat the SIEM store as append-only with restricted read/write access, and set a
retention period that meets your legal/contractual requirements.

## Running Tests

```bash
pytest
pytest tests/ --ignore=tests/test_ui.py   # skip live Playwright tests
```

## Rebuilding CSS

The shared Iceberg design system (cool-grey oklch tokens, cyan accent, and the
`.rail`/`.workspace` component vocabulary) lives in `static/css/iceberg.css`,
which `input.css` imports alongside Tailwind. After editing templates or the
design system, rebuild the Tailwind output:

```bash
tailwindcss -i static/css/input.css -o static/css/output.css
```

## Project Structure

```
app/
├── main.py          # App factory + lifespan (settings validation, middleware)
├── config.py        # Settings (pydantic-settings, reads .env) + startup validation
├── middleware.py    # Audit request context + CSRF origin checks
├── database.py      # Async Postgres engine + get_session dependency
├── dependencies.py  # FastAPI dependencies (auth, role guards)
├── models/          # SQLModel table definitions (incl. AuditEvent, AuditSettings)
├── schemas/         # Pydantic request/response schemas
├── routers/         # FastAPI routers (one per resource, incl. audit) + ui.py (Jinja2 pages)
├── services/        # Business logic (auth, scenario, exercise, inject, inject_comment, response, comms, llm, ws_manager, access_control, audit_service, siem_service, audit_settings_service, rate_limit)
├── samples/         # Bundled quick-start scenario templates (loaded only on demand)
└── templates/       # Jinja2 HTML templates
    ├── base.html            # App shell (dark rail + breadcrumb topbar), shared JS helpers
    ├── dashboard.html       # Command center
    ├── help.html            # In-app help & documentation
    ├── settings.html        # Profile, theme, role preview, and sample loader
    ├── auth/                # login.html, register.html
    ├── scenarios/           # list, detail, editor
    ├── exercises/           # list, facilitator console, participant view
    └── communications/      # inbox
tests/               # Pytest test suite (conftest.py + one file per resource)
static/css/          # input.css + iceberg.css (shared design system) → output.css (Tailwind CLI compiled)
static/fonts/        # Self-hosted Archivo · JetBrains Mono · Spectral (woff2) + fonts.css
static/img/          # Iceberg brand marks (SVG)
docs/                # README screenshots
Dockerfile           # Multi-stage build (Tailwind compile + Python runtime)
docker-compose.yml   # app + postgres:17 + nginx-unprivileged (non-root)
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

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the full
text. Copyright 2026 IcebergAI.
