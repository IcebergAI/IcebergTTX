# Repository security controls

This document records the repository-level controls verified on 2026-07-12.
It intentionally contains configuration state only; it must never contain
secrets, alert payloads, or vulnerability details.

## Main branch

- Required checks: `test`, `lint-workflows`, `Analyze (python)`, and
  `Analyze (javascript-typescript)`; branches must be current before merge.
- One independent approving review is required. Stale approvals are dismissed
  and the latest push must be approved.
- Review conversations must be resolved, and the rule applies to administrators.
- Force pushes and branch deletion are disabled.

## Credential protection

Secret scanning and push protection are enabled. Dependabot security updates
are enabled. GitHub currently reports non-provider pattern scanning and secret
validity checks as unavailable/disabled for this repository; review that state
when the organization plan or GitHub capability changes. Test protections only
with GitHub's documented safe test procedure, never with a real credential.

## Emergency path

An administrator bypass is an exceptional production-recovery mechanism, not a
normal merge path. The incident or release record must identify the reason,
affected commit, approving maintainer, and the follow-up reviewed pull request.
