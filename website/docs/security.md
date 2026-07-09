---
title: Security
icon: material/shield-lock
---

<p class="eyebrow">Trust & safety</p>

# Security

IcebergTTX is built API-first with a hardened default posture. This page summarises
the security model; the canonical policy is
[`SECURITY.md`](https://github.com/IcebergAI/IcebergTTX/blob/main/SECURITY.md) in the
repository.

## Reporting a vulnerability

**Do not** open a public GitHub issue for security vulnerabilities. Report privately
via GitHub's [private vulnerability
reporting](https://github.com/IcebergAI/IcebergTTX/security/advisories/new) ("Report
a vulnerability" under the repository's **Security** tab). Include a description and
impact, reproduction steps (PoC if possible), and the affected component(s) and
configuration. We aim to acknowledge within 5 business days.

## Access model

Facilitator access to **exercises** is scoped **per-exercise**. A facilitator can
read and mutate only:

- exercises they created;
- exercises they are enrolled on as a **co-facilitator**; or
- any exercise, for a **global-admin** account (`User.is_admin`).

Any other facilitator gets `403` plus an `authz.denied` audit event. Bypasses of
`require_exercise_access` / `require_exercise_owner` (privilege escalation, IDOR) are
**in scope** for reports.

Intentionally shared-by-design (not vulnerabilities on their own):

- the **scenario library** — any facilitator may list, read, edit, and export
  scenarios (they are reusable templates);
- `GET /users` — it is the member-enrolment picker;
- the **`facilitator` role and `is_admin` flag** are assigned out-of-band (seeded /
  admin-managed), never via self-registration, which creates **participants only**.

Reports that these shared surfaces leak data *beyond* their intended audience (e.g. a
participant reading another team's data, or an unauthenticated caller reaching them)
remain in scope.

## Hardening highlights

<div class="grid cards" markdown>

-   :material-key: __Secrets & tokens__

    ---

    Startup aborts if `SECRET_KEY` is unset, default, or under 32 chars. JWTs carry
    an `iat` claim with a per-user `token_valid_after` revocation cutoff; changing
    your password revokes all other sessions.

-   :material-cookie: __Cookies & CSRF__

    ---

    Auth cookie is `httpOnly` + `Secure`; an `Origin`/`Referer` check guards
    cookie-authenticated state-changing `/api/` requests. WebSockets authenticate
    from the cookie with a CSWSH origin check.

-   :material-speedometer: __Rate limiting__

    ---

    Sliding-window login brute-force protection (`429` + `Retry-After`), plus a
    separate per-IP registration flood limiter. Registration can be disabled
    entirely with `REGISTRATION_ENABLED=false`.

-   :material-shield-check: __Strict CSP & headers__

    ---

    `script-src 'self'` with no `unsafe-*`, plus `X-Frame-Options: DENY`,
    `nosniff`, `Referrer-Policy`, `Permissions-Policy`, and HSTS in production —
    all emitted by the app, not the proxy.

-   :material-file-document-outline: __Audit logging__

    ---

    Structured JSON audit events (login, register, inject release, exports, authz
    denials, CSRF blocks…) to a logger and an append-only table, sanitised against
    log injection.

-   :material-export: __SIEM forwarding__

    ---

    The app forwards each event off the response path to file / syslog / HTTP sinks
    (Splunk HEC, Elastic, webhook). Tokens are env-only and never logged.

</div>

## Authentication

Local auth (bcrypt password hashing, NIST-aligned length-only policy) runs alongside
or instead of **OpenID Connect** SSO (Authorization-Code + PKCE via Authlib).
Adapters ship for Entra, Authentik, Auth0, and Okta. JIT-provisioned SSO users are
created as **participants** — no self-elevation — and client secrets are env-only.

## Operator responsibilities

Deployment hardening — secret management, TLS termination, network policy, and the
single-replica WebSocket constraint — is the operator's responsibility. See
[Deployment](deployment.md) for the hardened Compose and Kubernetes baselines.
