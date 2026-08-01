---
title: Deployment
icon: material/rocket-launch
---

<p class="eyebrow">Operations</p>

# Deployment

IcebergTTX ships with two supported deployment paths: **Docker Compose** for a
single host, and **Kubernetes** manifests for a cluster. Both front the app with
**Caddy**; the only difference is where TLS is terminated.

## Running more than one replica

Every piece of cross-request state is shared through PostgreSQL, so the app scales out
without a broker, a cache, or any second datastore to operate:

| State | How it is shared |
|---|---|
| **WebSocket fan-out** | The originating replica publishes a compact descriptor on a `LISTEN`/`NOTIFY` channel inside its transaction; each replica re-reads the committed rows and renders frames for its own sockets. |
| **Exercise schedules** | Inject releases and triggered communications are rows in a job table, enqueued in the same transaction as the change that makes them due. A restart loses nothing, and a killed worker's job is retried. |
| **Rate limiters** | Attempt counters are rows, so the limit is the limit however many replicas are running. |
| **Config caches** | A settings save publishes its scope; every replica re-reads the row. A replica that was disconnected re-reads all of them on reconnect. |
| **LLM assessments and summaries** | Queue jobs with a queueing lock, so a manual re-trigger on one replica cannot duplicate a provider call on another. |
| **Audit-retention sweep** | A periodic job. Exactly one replica performs each night's purge. |
| **Schema migrations** | Every replica migrates on startup behind a Postgres advisory lock, so they run one at a time and the rest find the schema already at head. |

!!! warning "Storage is the remaining prerequisite"
    The default manifests still ship `replicas: 1`, for one reason: inject attachments
    live on a `ReadWriteOnce` volume that only a single node can mount, and a PVC's
    access modes **cannot be changed in place**.

    On a cluster with an RWX StorageClass (EFS, Filestore, CephFS, NFS…), apply the
    overlay:

    ```bash
    kubectl apply -k k8s/overlays/multi-replica
    ```

    It sets `replicas: 2`, switches to a `RollingUpdate` that keeps the old pod serving
    until the new one is ready, and requests `ReadWriteMany` for uploads. An install
    already running the single-replica base must provision a new claim and copy the
    attachments across — it cannot be upgraded in place.

!!! note "Do not put a transaction-mode pooler in front of the app"
    `LISTEN`/`NOTIFY` needs a session that outlives a transaction. PgBouncer in
    `transaction` or `statement` mode silently drops the subscription, and the symptom is
    that live updates stop reaching some clients while everything else looks healthy. Use
    `session` mode, or connect the app directly to Postgres.

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

Manifests live in `k8s/`, laid out as a Kustomize **base + overlays**:

- `k8s/base/` — the cloud-agnostic stack (namespace, config, secrets,
  NetworkPolicies, app/postgres/caddy workloads and their ClusterIP Services). It
  references no CRDs, so it applies on any conformant cluster.
- `k8s/overlays/nginx/` — base **+ a standard Ingress** (ingress-nginx by
  default). The portable default.
- `k8s/overlays/eks/` — base **+ an AWS ALB `TargetGroupBinding`** (requires the
  AWS Load Balancer Controller). The sole AWS-specific object lives here, so the
  base and the nginx overlay stay usable everywhere. See
  `k8s/overlays/eks/README.md`.

Pick your overlay and apply the whole stack in one build:

```bash
# Generic clusters (Ingress controller):
kubectl apply -k k8s/overlays/nginx
# AWS EKS (ALB via the AWS Load Balancer Controller):
kubectl apply -k k8s/overlays/eks

kubectl rollout status statefulset/postgres -n iceberg-ttx
kubectl rollout status deployment/iceberg-ttx-app -n iceberg-ttx
kubectl rollout status deployment/caddy -n iceberg-ttx

# -it is required: with no --password and no ADMIN_PASSWORD, the tool prompts for one
# (never echoed), and without a TTY it has nothing to read from.
kubectl exec -it -n iceberg-ttx deploy/iceberg-ttx-app -- \
    python -m app.bootstrap_admin --email you@example.com --name "You"
```

Before applying, fill in the placeholders in `k8s/base/secrets.yaml` and set the
hostname / issuer / `ingressClassName` in your chosen overlay
(`k8s/overlays/nginx/ingress.yaml` or `k8s/overlays/eks/targetgroupbinding.yaml`).
The manifests already reference the published image `ghcr.io/icebergai/iceberg-ttx`
in `k8s/base/app/deployment.yaml` and `k8s/base/caddy/deployment.yaml` (the
copy-static init container reuses the app image) — set the release tag you want and
**pin by digest** (`ghcr.io/icebergai/iceberg-ttx@sha256:…`) in production for
reproducible rollouts. Prefer plain `kubectl apply -f`? Apply the files under
`k8s/base/` in order (namespace → secrets + configmap → `postgres/` → `app/` →
`caddy/` → `networkpolicy.yaml`), then your ingress from `k8s/overlays/`.

!!! note "TLS in Kubernetes"
    Caddy runs as a plain-HTTP (`:8080`) **internal** reverse proxy; TLS is
    terminated by the cluster **Ingress** (cert-manager + `force-ssl-redirect`),
    or by the **ALB** on EKS. The `caddy` Service is `ClusterIP`. Do **not** switch
    it to a `LoadBalancer` on `:80` — that serves auth over plaintext.

### Pod hardening

Every workload runs non-root under a PSS-`restricted`-style `securityContext` (no
privilege escalation, all capabilities dropped, `RuntimeDefault` seccomp), and **every
container uses a read-only root filesystem** — app, init, Caddy, Postgres, and the
backup CronJob alike. The Postgres StatefulSet runs as uid 999 with `fsGroup: 999`,
which needs a StorageClass that honours `fsGroup`.

### Backups cover two volumes, not one

Durable state lives in the Postgres database **and** on the `app-uploads` PVC, which
holds the inject attachments that `inject.attachment_path` points at. A database-only
backup restores to a deployment whose records claim evidence that is no longer on
disk. `k8s/base/postgres/backup-cronjob.yaml` captures both in one daily job under a shared
timestamp; after a restore, run `python -m app.reconcile_attachments` to surface any
row whose file is missing. The job co-schedules with the app pod because `app-uploads`
is `ReadWriteOnce`.

### Origin checks

Browser WebSocket auth verifies the upgrade's `Origin` against the request `Host`
(plus `TRUSTED_ORIGINS`). This works out of the box because every hop preserves
`Host`. If your Ingress or proxy chain rewrites it, set `TRUSTED_ORIGINS` in
`k8s/base/configmap.yaml` to your public hostname so live updates keep working.

## Health probes

| Endpoint | Purpose | Behaviour |
|----------|---------|-----------|
| `GET /api/health` | Kubernetes **liveness** | DB-free, unconditional `200` — a DB outage must not restart pods. |
| `GET /api/health/ready` | Kubernetes **readiness** / compose healthcheck | Runs a short-timeout `SELECT 1`; returns `503` when Postgres is unreachable. |

## Migrations

Schema is managed by **Alembic**. The app self-migrates on startup (`alembic upgrade
head` runs in the async lifespan), behind a Postgres advisory lock — so several replicas
starting at once migrate one at a time, and the ones that lose the lock find the schema
already at head and apply nothing.

During a rolling update the previous version serves against the new schema for as long
as the rollout takes, so **a migration must be forward-compatible across one release**:
add columns before writing them, and drop them a release after the code stops reading
them.

## Data growth and retention

Two tables grow with traffic rather than with exercise activity, and both hold
personal data:

- **`auditevent`** — one row per security-relevant event while `AUDIT_PERSIST` is on,
  carrying actor emails and source IPs. Unauthenticated endpoints (login, register,
  password-reset request) emit rows too, so growth is partly driven by whoever is
  probing you, bounded only by the rate limiters.
- **`authtoken`** — one row per password-reset request and per invite, each an email
  address plus a hashed single-use token.

`AUDIT_RETENTION_DAYS` (env seed) / **`/admin/audit`** (authoritative once the row
exists) bounds the first. It defaults to `0`, meaning **keep forever**: an upgrade
must never silently destroy security records. Set a day count to enable pruning; a
sweep runs on startup and once daily, deleting in bounded batches.

Order matters. SIEM forwarding is the archival path once pruning is on, but it is
**best-effort — no retry, no outbox** — so a forwarder outage that overlaps a purge
window loses those events permanently. Enable a forwarder, confirm it is receiving
events, and only then set a retention window. Pruning is irreversible: there is no
soft-delete and no undo.

Dead `authtoken` rows are purged by the same sweep **unconditionally** — unused rows
7 days past expiry, used rows 24 hours after being burnt. They are spent by then and
the audit trail independently records issuance and acceptance, so nothing is lost.

Still unbounded and deliberately untouched: `exercisestatetransition` (lifecycle
history, which intentionally survives `AUDIT_PERSIST=false`), communications, and
responses — all domain data with real read paths. Size the database accordingly.

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
