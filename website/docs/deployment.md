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
    The WebSocket manager is in-memory, so the app must run as a **single replica**
    until a distributed backend (e.g. Redis pub/sub) is added. The Kubernetes
    manifests enforce this with `replicas: 1` and `strategy: Recreate`.

## Docker Compose

A `docker-compose.yml` runs the app, **PostgreSQL 17**, and **Caddy** as a reverse
proxy with automatic HTTPS.

```bash
# Copy and fill in secrets (POSTGRES_PASSWORD and SECRET_KEY are required)
cp .env.example .env
# For a public deployment, set SITE_ADDRESS to your domain.

docker compose up -d      # build and start
docker compose ps         # check all three services are healthy
```

Caddy serves the app over **HTTPS on port 443** (redirecting `:80`), serves
compiled static files directly, and proxies everything else — including WebSocket
upgrades at `/ws/` — to uvicorn.

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
docker compose down        # keeps named volumes (postgres_data, uploads)
docker compose down -v     # also deletes volumes — permanent data loss
```

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

kubectl exec -n iceberg-ttx deploy/iceberg-ttx-app -- \
    python -m app.bootstrap_admin --email you@example.com --name "You"
```

Before applying, replace the placeholders: values in `k8s/secrets.yaml`, the image
reference in `k8s/app/deployment.yaml` and `k8s/caddy/deployment.yaml` (the
copy-static init container uses the app image), and the hostname / issuer /
`ingressClassName` in `k8s/caddy/ingress.yaml`.

!!! note "TLS in Kubernetes"
    Caddy runs as a plain-HTTP (`:8080`) **internal** reverse proxy; TLS is
    terminated by the cluster **Ingress** (cert-manager + `force-ssl-redirect`).
    The `caddy` Service is `ClusterIP`. Do **not** switch it to a `LoadBalancer` on
    `:80` — that serves auth over plaintext.

### Pod hardening

All three workloads run non-root under a PSS-`restricted`-style `securityContext`
(no privilege escalation, all capabilities dropped, `RuntimeDefault` seccomp; app
and init containers use a read-only root filesystem). The Postgres StatefulSet runs
as uid 999 with `fsGroup: 999`, which needs a StorageClass that honours `fsGroup`.

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

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env       # set SECRET_KEY
uvicorn app.main:app --reload   # applies migrations to head on startup
```

Run the test suite with `pytest` (a real Postgres is spun up per worker via
testcontainers).
