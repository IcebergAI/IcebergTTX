# Contributing to IcebergTTX

Thanks for your interest in contributing! IcebergTTX is a tabletop-exercise
platform for cyber incident and business-resilience scenarios. This guide covers
the essentials; the deeper architecture notes live in [CLAUDE.md](CLAUDE.md).

## Code of Conduct

This project adheres to a [Code of Conduct](CODE_OF_CONDUCT.md). By participating,
you are expected to uphold it. Please report unacceptable behaviour as described
there.

## Reporting bugs & requesting features

- **Bugs** and **feature requests** — open an issue using the templates on the
  [New issue](https://github.com/IcebergAI/IcebergTTX/issues/new/choose) page.
- **Security vulnerabilities** — do **not** open a public issue. Follow
  [SECURITY.md](SECURITY.md) and use GitHub's private vulnerability reporting.

## Development setup

Full instructions are in the README's [Setup](README.md#setup) section.
Dependencies are managed with [uv](https://docs.astral.sh/uv/) against the
committed `uv.lock`. In short:

```bash
uv sync --extra dev              # create .venv from the lockfile + dev tools
cp .env.example .env             # set SECRET_KEY (see the README)
uv run iceberg-ttx-dev           # Tailwind build/watch + Uvicorn reload
```

The backend targets **Python 3.14+**, FastAPI (fully async), and PostgreSQL via
`asyncpg`. **Docker is required to run the test suite** — the tests spin up a real
PostgreSQL 17 instance with `testcontainers`, so the Docker daemon must be running.
After changing dependencies in `pyproject.toml`, run `uv lock` and commit the
updated `uv.lock` (CI runs `uv lock --check`).

## Before you open a pull request

Run these locally and make sure they pass:

```bash
uv run ruff check app/ tests/   # lint
uv run pyright app/             # type check
uv run pytest                   # full test suite (needs Docker running)
```

- **Add tests** for new behaviour or bug fixes — one test file per resource, async
  (`asyncio_mode = "auto"`); fixtures live in `tests/conftest.py`.
- **Frontend/CSS changes** — run `uv run iceberg-ttx-dev`; it rebuilds the
  ignored `static/css/output.css` as templates and design-system CSS change.
- **Schema changes** — edit the models, then generate a migration with
  `alembic revision --autogenerate -m "describe change"` and commit the generated
  file under `alembic/versions/`.
- **Docs** — keep README.md, CLAUDE.md, and PLAN.md in step with your change.

## Pull request expectations

- Branch off `main` and keep PRs focused on a single concern.
- Fill in the [pull request template](.github/PULL_REQUEST_TEMPLATE.md).
- Reference the issue you're addressing and use a closing keyword
  (`Closes #123`) so it auto-closes on merge.
- Write a clear description of **what** changed and **why**; CI (lint, tests,
  `bandit`, `pip-audit`, workflow linting) must be green before review.

## Protected-main workflow

`main` is protected. A pull request must be current with `main`, pass the
required test, workflow-lint, and CodeQL checks, have all review conversations
resolved, and receive one approval from an independent maintainer. New commits
dismiss earlier approvals and require approval of the latest push; the rule
applies to administrators as well.

Do not use an administrator bypass for routine work. If a production-impacting
emergency cannot wait for the normal review path, record the reason, affected
commit, approving maintainer, and follow-up review in the incident or release
record. Review and reconcile the emergency change in a normal pull request as
soon as service is stable.

Repository secret scanning and push protection are enabled. Never use real
credentials to test these controls; use GitHub's documented safe test process.

By contributing, you agree that your contributions are licensed under the
project's [Apache License 2.0](LICENSE).
