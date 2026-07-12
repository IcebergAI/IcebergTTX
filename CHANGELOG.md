# Changelog

All notable changes to IcebergTTX are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) (see the
[Versioning & Releases](README.md#versioning--releases) section of the README).

## [Unreleased]

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

[Unreleased]: https://github.com/IcebergAI/IcebergTTX/compare/v0.1.0-beta.2...HEAD
[0.1.0-beta.2]: https://github.com/IcebergAI/IcebergTTX/compare/v0.1.0-beta.1...v0.1.0-beta.2
[0.1.0-beta.1]: https://github.com/IcebergAI/IcebergTTX/releases/tag/v0.1.0-beta.1
