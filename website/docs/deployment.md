---
title: Deployment
icon: material/rocket-launch
---

<p class="eyebrow">Operations</p>

# Deployment

IcebergTTX ships with two supported deployment paths: **Docker Compose** for a
single host, and **Kubernetes** manifests for a cluster. Both front the app with
**Caddy**; the only difference is where TLS is terminated.

!!! info "Single-replica constraint"
    The app must run as a **single replica**, and the Kubernetes manifests enforce it with
    `replicas: 1` and `strategy: Recreate`. This is not one limitation but several — every
    piece of cross-request state the app keeps lives in the **process**, so a second replica
    would not share it:

    | In-memory state | What a second replica would break | Needs |
    |---|---|---|
    | **WebSocket manager** | Clients connected to replica A never see events raised on replica B | A shared bus (e.g. Redis pub/sub) |
    | **Exercise schedules** | Inject and communication timers are process-local; persisted state restores them after one process restarts, but a second live replica can duplicate scheduling work | A task queue (Celery, ARQ) |
    | **Login / registration / reset rate limiters** | Attempt counters are per process, so the effective limit multiplies by the replica count | A shared store |
    | **SIEM and proxy config caches** | An admin's change on replica A leaves replica B forwarding to the old sink, or egressing via the old proxy | Cache invalidation across replicas |
    | **In-flight LLM assessments** | Best-effort background tasks, tracked per process | A task queue |

    `rehydrate_schedules()` reconstructs pending inject releases and delayed triggered
    communications on startup. Pause cancels both timer types and resume re-arms their
    remaining active-time delay. This protects a **single-process** restart; it is not a
    substitute for shared scheduling across live replicas.

## Docker Compose

A `docker-compose.yml` runs the app, **PostgreSQL 17**, and **Caddy** as a reverse
proxy with automatic HTTPS.

```bash
# Copy and fill in secrets (POSTGRES_PASSWORD and SECRET_KEY are required)
cp .env.example .env
# For a public deployment, set SITE_ADDRESS to your domain.

docker compose up -d      # build and start
docker compose ps         # db, app and caddy healthy (caddy-init is one-shot and exits)
```

Caddy serves the app over **HTTPS on port 443** (redirecting `:80`), serves
compiled static files directly, and proxies everything else — including WebSocket
upgrades at `/ws/` — to uvicorn.

!!! tip "Build vs. published image"
    `docker compose up` **builds** the image locally from source. To run a
    **published release** instead, comment out the `build:` block on the `app`
    service in `docker-compose.yml` and uncomment the
    `image: ghcr.io/icebergai/iceberg-ttx:<version>` line, then `docker compose up -d`.
    Releases follow [SemVer](https://semver.org/) (the current line is `0.x` beta);
    each image is published to GHCR with an SBOM, SLSA provenance, and a cosign
    signature. See the repository `docs/RELEASING.md` for tags and verification.

- Set `SITE_ADDRESS` to your public domain for an automatic **Let's Encrypt**
  certificate (certs persist in the `caddy_data` volume).
- The default `SITE_ADDRESS=localhost` uses Caddy's **internal self-signed CA**, so
  `docker compose up` works over HTTPS immediately for local testing (your browser
  will warn on the untrusted cert — expected).

Create the first admin account once the stack is up:

```bash
docker compose exec app python -m app.bootstrap_admin \
    --email you@example.com --name "You"
```

Stopping:

```bash
docker compose down        # keeps the named volumes
docker compose down -v     # also deletes them — permanent data loss
```

The five named volumes are `postgres_data` (the database), `uploads` (inject
attachments), `static_files`, and Caddy's `caddy_data` / `caddy_config`. `down -v`
destroys all of them — including the Let's Encrypt certificates in `caddy_data`, so a
rebuilt stack must re-issue them and will re-consume rate limit quota.

!!! warning "Always use HTTPS"
    The app sets `Secure` cookies, so it must be reached over HTTPS. Only use
    `SITE_ADDRESS=:80` (plain HTTP) for throwaway testing behind your own TLS
    terminator.

## Kubernetes

Manifests live in `k8s/`. Apply in order:

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/secrets.yaml -f k8s/configmap.yaml

kubectl apply -f k8s/postgres/
kubectl rollout status statefulset/postgres -n iceberg-ttx

kubectl apply -f k8s/app/
kubectl rollout status deployment/iceberg-ttx-app -n iceberg-ttx

kubectl apply -f k8s/caddy/     # Deployment + ClusterIP Service + TLS Ingress
kubectl rollout status deployment/caddy -n iceberg-ttx

kubectl apply -f k8s/networkpolicy.yaml   # requires a NetworkPolicy-enforcing CNI

# -it is required: with no --password and no ADMIN_PASSWORD, the tool prompts for one
# (never echoed), and without a TTY it has nothing to read from.
kubectl exec -it -n iceberg-ttx deploy/iceberg-ttx-app -- \
    python -m app.bootstrap_admin --email you@example.com --name "You"
```

Before applying, fill in the placeholders in `k8s/secrets.yaml` and set the
hostname / issuer / `ingressClassName` in `k8s/caddy/ingress.yaml`. The manifests
already reference the published image `ghcr.io/icebergai/iceberg-ttx` in
`k8s/app/deployment.yaml` and `k8s/caddy/deployment.yaml` (the copy-static init
container reuses the app image) — set the release tag you want and **pin by digest**
(`ghcr.io/icebergai/iceberg-ttx@sha256:…`) in production for reproducible rollouts.

!!! note "TLS in Kubernetes"
    Caddy runs as a plain-HTTP (`:8080`) **internal** reverse proxy; TLS is
    terminated by the cluster **Ingress** (cert-manager + `force-ssl-redirect`).
    The `caddy` Service is `ClusterIP`. Do **not** switch it to a `LoadBalancer` on
    `:80` — that serves auth over plaintext.

### Pod hardening

Every workload runs non-root under a PSS-`restricted`-style `securityContext` (no
privilege escalation, all capabilities dropped, `RuntimeDefault` seccomp), and **every
container uses a read-only root filesystem** — app, init, Caddy, Postgres, and the
backup CronJob alike. The Postgres StatefulSet runs as uid 999 with `fsGroup: 999`,
which needs a StorageClass that honours `fsGroup`.

### Origin checks

Browser WebSocket auth verifies the upgrade's `Origin` against the request `Host`
(plus `TRUSTED_ORIGINS`). This works out of the box because every hop preserves
`Host`. If your Ingress or proxy chain rewrites it, set `TRUSTED_ORIGINS` in
`k8s/configmap.yaml` to your public hostname so live updates keep working.

## Health probes

| Endpoint | Purpose | Behaviour |
|----------|---------|-----------|
| `GET /api/health` | Kubernetes **liveness** | DB-free, unconditional `200` — a DB outage must not restart pods. |
| `GET /api/health/ready` | Kubernetes **readiness** / compose healthcheck | Runs a short-timeout `SELECT 1`; returns `503` when Postgres is unreachable. |

## Migrations

Schema is managed by **Alembic**. The app self-migrates on startup
(`alembic upgrade head` runs in the async lifespan), which suits the single-replica
model. For multi-replica rollouts, run `alembic upgrade head` as a dedicated deploy
step instead.

## Local development

Dependencies are managed with [uv](https://docs.astral.sh/uv/) against the
committed `uv.lock`:

```bash
uv sync --extra dev             # create .venv from the lockfile + dev tools
cp .env.example .env            # set SECRET_KEY
uv run iceberg-ttx-dev            # Tailwind build/watch + Uvicorn reload
```

Run the test suite with `uv run pytest` (a real Postgres is spun up per worker via
testcontainers).
