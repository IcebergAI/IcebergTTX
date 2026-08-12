# Changelog

All notable changes to IcebergTTX are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) (see the
[Versioning & Releases](README.md#versioning--releases) section of the README).

## [Unreleased]

### Added
- **Immutable, content-addressed exercise-run snapshots** (#315) — the first launch now
  records a canonical scenario/configuration snapshot atomically with the lifecycle
  transition. Active and completed runs, reports, exports, scheduling, branching, and
  LLM context resolve through that record rather than mutable library templates. Existing
  launched exercises are backfilled on upgrade; facilitators can clone a run into a
  distinct draft and compare stable scenario changes before the next exercise.

### Changed
- Legacy upgrades now mark unreconstructable launch-time inject, communication,
  audience, schedule, and LLM provenance as unknown. Legacy clones rebuild from
  the frozen scenario definition instead of replaying ambiguous runtime rows.

### Security
- **Emailed links are rooted at `PUBLIC_BASE_URL`, never the request host** (#258) —
  password-reset and invite links previously fell back to the client-supplied `Host`
  header, so a forged host made the deployment mail a victim a genuine link (carrying a
  valid single-use reset token) pointing at an attacker. `PUBLIC_BASE_URL` is now
  required whenever SMTP is configured: startup refuses without it, the admin API
  refuses to enable email while it is blank, and the reset/invite endpoints return 503
  rather than send a request-derived link.
- **OIDC JIT provisioning requires a verified email** (#257) — every adapter computed
  `email_verified` and nothing read it, so on an IdP allowing self-set addresses an
  attacker could pre-claim a colleague's email, lock them out, and receive the
  enrolments facilitators make by email. Denied and audited by default;
  `OIDC_ALLOW_UNVERIFIED_EMAIL=true` restores the old behaviour for IdPs that never
  emit the claim.
- **Secret-bearing sink hosts are pinned to the environment** (#259) — while
  `SIEM_HTTP_TOKEN`, `SMTP_PASSWORD`, or the proxy credentials are set, the matching
  destination's origin can no longer be re-pointed at runtime and the secret sent
  there via the "test" button. Path edits and clearing the sink stay allowed, and any
  destination change now emits a `critical` audit event.
- **Releases are gated on the tagged commit's provenance** (#267) — branch protection
  does not cover tag pushes, so a `v*` tag on any commit published a signed,
  provenance-attested image. The workflow now verifies the commit is on `main` with a
  green CI run for that SHA before building.

### Fixed
- **Scheduled Kubernetes backups cover the uploads PVC** (#268) — the CronJob dumped
  Postgres only, so a restore produced a database whose injects referenced attachment
  files that no longer existed. Database and attachments are now captured together
  under one timestamp.
- **Three list endpoints no longer query per row** (#263) — facilitator responses,
  inject comments, and the participant inject list resolved suggestions, visibility,
  and authors one row at a time. Each now costs a fixed number of queries regardless
  of exercise activity, with regression tests pinning the ceiling.

## [0.1.0-beta.3] - 2026-07-14

Third beta release focused on runtime configuration, admin usability, a redesigned
navigation and console layout, and the internal seams (event dispatch, service
ownership, projection) that the multi-replica work depends on.

### Added
- **The app runs on more than one replica** (#213) — every piece of cross-request state
  moves into PostgreSQL, so there is no longer a single-replica constraint and no Redis
  or broker to operate. WebSocket frames and config invalidation travel over
  `LISTEN`/`NOTIFY` as compact id descriptors that each replica reads back and renders
  for its own sockets; scheduled inject releases, triggered communications, LLM
  pipelines and the nightly audit purge become durable jobs (procrastinate); rate-limit
  counters become rows, so the login, registration, and reset limits no longer multiply
  by the replica count; and startup migrations serialise on a Postgres advisory lock.

  Everything is published or enqueued **inside** the transaction that makes it true.
  Postgres holds a `NOTIFY` until COMMIT and discards it on rollback, and a job row
  commits with the state change that warranted it — so "committed but never announced"
  and "committed but never enqueued" stop being possible. That closes the bug class
  behind #211 (triggered communications lost on restart) and #218 (a scheduled release
  stranded when a worker stood down mid-response) structurally rather than case by case,
  and retires the hand-built coordination those fixes needed.

  Kubernetes manifests keep `replicas: 1` in the base for a storage reason rather than a
  state one — the uploads volume is `ReadWriteOnce` and a PVC's access modes cannot be
  changed in place. Clusters with an RWX StorageClass apply the new
  `k8s/overlays/multi-replica` for two replicas and rolling deploys. Do not front the app
  with a transaction-mode connection pooler: `LISTEN` needs a session that outlives a
  transaction.
- **Runtime configuration** — non-secret settings move out of env-only config and into
  the admin UI, following the singleton-row + cached-config pattern already used by
  `/admin/audit` and `/admin/proxy`. Email/SMTP, general settings (registration, token
  expiry, audit persistence), LLM provider and model, rate limits, and OIDC provider
  config are all editable at runtime. **Secrets — SMTP and proxy passwords, API keys,
  OIDC client secrets — remain env-only throughout**, never persisted or returned.
- **Effective configuration view** — a read-only admin page showing each setting's
  value, its provenance (env, database, or default), and whether a secret is set,
  without ever revealing the secret itself.
- **Team scents for scenario-defined teams** — team tinting is no longer limited to
  four hardcoded ids; an arbitrary team from a scenario definition gets a tint, and an
  unknown id falls back to a neutral pill rather than an invisible one in dark mode.
- **Opt-in dense controls** — the design handoff's tighter control sizing, scoped so it
  cannot undercut the global touch-target floor.

### Changed
- **Navigation and layout redesign** — regrouped nav rail with a context-aware topbar,
  the facilitator console rebuilt around a single command bar, and reworked
  communications-inbox and settings layouts.
- **Disabled features are explained, not hidden** — an admin now sees why a feature is
  unavailable and what to set, instead of the entry point silently vanishing.
- **Internal seams** — services no longer broadcast inline: domain events are recorded
  inside the transaction and dispatched post-commit through a single WebSocket
  projector. `User` gains an owning service, routers stop issuing raw queries, and
  schema placement has an explicit rule. Exercise projection and delayed-task handling
  are centralized rather than restated per call site.
- **Documentation** — reconciled with what the app actually does, and all 13
  screenshots regenerated from a scripted capture rather than by hand.

### Fixed
- **Sequential scans on every core read path** — the missing `exercise_id` indexes are
  added, and the communications inbox batches its sender-team resolution instead of
  resolving per row.
- Triggered communications now respect pause and completion, and survive a restart
  instead of being silently lost.
- A scheduled inject release is no longer skipped when the team has not yet reached the
  node: an overdue release fires the moment a response advances the cursor onto it.
- The communications inbox is usable at phone widths (the reader pane no longer
  collapses), and the rail's unread badge stays live when a communication arrives
  outside the inbox page.
- Stabilized UI authentication transitions, and held the touch-target floor across the
  new settings, communications, and segmented-control classes.

### Dependencies
- Bumped the Docker build/publish actions (`login`, `metadata`, `setup-buildx`,
  `build-push`, `attest-build-provenance`) and the grouped Python dependencies.

## [0.1.0-beta.2] - 2026-07-12

Second beta release focused on after-action reporting, operational hardening,
mobile usability, email workflows, and security remediation.

### Added
- **After-action review** — durable exercise timelines, facilitator debrief notes,
  generated reports, attendance snapshots, and participant/group-aware reporting.
- **Exercise pacing** — pause-aware clocks and optional scheduled inject release.
- **Email workflows** — SMTP-backed password reset and participant invitations.
- **Scenario progression** — group-specific cursors and durable inject progress.

### Changed
- **Mobile and accessibility** — compact navigation, responsive facilitator console,
  larger touch targets, labelled controls, keyboard-safe modals, and WCAG contrast.
- **Concurrency and history** — atomic lifecycle transitions, idempotent responses,
  triggered communications, LLM results, and recoverable attachment cleanup.
- **OIDC identity policy** — stable tenant/subject binding and explicit role
  provenance preserve operator overrides while revoking removed IdP elevation.
- **Tooling** — Pyright in CI, CodeQL v4, unified local development command, and a
  working external-Postgres test path that does not initialize Docker.

### Fixed
- Reconciled the Alembic migration graph and hardened the legacy communication
  read-receipt backfill against JSON `null` values.
- Restored Uvicorn startup diagnostics after in-process Alembic configuration.
- Corrected published image tags, Kubernetes backup execution, completed-exercise
  history, report counts, response requirements, and multi-team delivery behavior.
- Consolidated security fixes across authorization boundaries, OIDC, WebSockets,
  CSV exports, AI opt-out enforcement, audit delivery, and scenario isolation.

### Security
- This is the patched release for the repository advisories affecting beta.1.
  Full vulnerability details are available in the published GitHub advisories.

## [0.1.0-beta.1]

First public (beta) release. The `0.x` line is pre-stable — interfaces may change
before `1.0.0`.

### Added
- **Scenarios** — branching inject trees defined as validated JSON (per-option and
  linear `next_inject_id`, team targets, triggered communications), a depth-first
  detail view, and an inject-tree editor. Bundled sample scenarios.
- **Exercises** — facilitator/participant/observer roles, membership with per-exercise
  group assignment, lifecycle (draft → active → paused → completed), and a full-height
  facilitator console. Real-time updates over WebSockets.
- **Injects & responses** — "pull, not push" branching: participant responses resolve
  candidate next injects that the facilitator reviews and releases. Inject comment
  threads, group-scoped injects, and file attachments.
- **Communications** — simulated incident comms with team visibility, delayed delivery,
  and a reader inbox.
- **LLM assistance** — pluggable AI providers (Anthropic, Bedrock, OpenAI, Ollama,
  Gemini, or none) for response assessment and inject suggestions; providers are
  opt-in SDK extras.
- **Security & auth** — JWT (httpOnly cookie + bearer), password policy, token
  revocation, admin-driven password reset, login brute-force protection, registration
  controls, facilitator ownership scoping, security headers with a strict CSP, and
  CSRF/origin checks. OIDC/SSO (Authorization-Code + PKCE) across Entra/Authentik/
  Auth0/Okta.
- **Observability & egress** — structured security audit logging, SIEM forwarding, and
  a configurable outbound proxy for LLM/SIEM/OIDC egress.
- **Deployment** — hardened non-root container image published to
  `ghcr.io/icebergai/iceberg-ttx`, Docker Compose (app + Postgres + Caddy auto-HTTPS),
  and Kubernetes manifests (single-replica; app self-migrates via Alembic on startup).
  Reproducible builds via `uv.lock`; images ship an SBOM, SLSA build-provenance
  attestation, and a cosign signature.

[Unreleased]: https://github.com/IcebergAI/IcebergTTX/compare/v0.1.0-beta.3...HEAD
[0.1.0-beta.3]: https://github.com/IcebergAI/IcebergTTX/compare/v0.1.0-beta.2...v0.1.0-beta.3
[0.1.0-beta.2]: https://github.com/IcebergAI/IcebergTTX/compare/v0.1.0-beta.1...v0.1.0-beta.2
[0.1.0-beta.1]: https://github.com/IcebergAI/IcebergTTX/releases/tag/v0.1.0-beta.1
