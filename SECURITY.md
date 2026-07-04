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

This project is designed around a **single trusted facilitator team** — any account
with the `facilitator` role is currently a global administrator over exercises,
scenarios, and exports (a documented trust boundary, not a vulnerability). Reports
that depend on a facilitator account acting maliciously are out of scope until
per-resource ownership scoping lands.

Deployment hardening (secret management, TLS termination, network policy, and the
single-replica WebSocket constraint) is the operator's responsibility; see the
deployment notes in [README.md](README.md) and [CLAUDE.md](CLAUDE.md).
