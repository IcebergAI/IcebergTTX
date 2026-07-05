# Security Policy

## Supported Versions

IcebergTTX is pre-1.0 and under active development. Security fixes are applied to
the `main` branch only; there are no separately maintained release branches yet.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, report privately via GitHub's [private vulnerability
reporting](https://github.com/IcebergAI/IcebergTTX/security/advisories/new)
("Report a vulnerability" under the repository's **Security** tab).

Please include:

- A description of the vulnerability and its impact
- Steps to reproduce (proof-of-concept if possible)
- Affected component(s) and any relevant configuration

We aim to acknowledge reports within 5 business days and will keep you updated on
remediation progress. Please give us a reasonable opportunity to release a fix
before any public disclosure.

## Scope

**Access model.** Facilitator access to **exercises** is scoped per-exercise (#12): a
facilitator can read and mutate only exercises they created, exercises they are
enrolled on as a co-facilitator, or — for a global-admin account (`User.is_admin`) —
any exercise. Cross-facilitator access to an exercise you do not own is **not** a
documented trust boundary; reports demonstrating it (privilege escalation, IDOR, or a
bypass of `require_exercise_access` / `require_exercise_owner`) are **in scope**.

The following are **intentional, shared-by-design** and not vulnerabilities on their
own:

- The **scenario library** is shared — any facilitator may list, read, edit, and
  export scenarios (they are reusable templates).
- `GET /users` is facilitator-wide — it is the member-enrolment picker.
- The **`facilitator` role and `is_admin` flag are assigned out-of-band** (seeded /
  admin-managed), never via self-registration, which creates participants only (#8).

Reports that these shared surfaces leak data *beyond* their intended audience (e.g. a
participant reading another team's data, or an unauthenticated caller reaching any of
them) remain in scope.

Deployment hardening (secret management, TLS termination, network policy, and the
single-replica WebSocket constraint) is the operator's responsibility; see the
deployment notes in [README.md](README.md) and [CLAUDE.md](CLAUDE.md).
