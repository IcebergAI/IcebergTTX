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

Full instructions are in the README's [Setup](README.md#setup) section. In short:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # set SECRET_KEY (see the README)
uvicorn app.main:app --reload
```

The backend targets **Python 3.14+**, FastAPI (fully async), and PostgreSQL via
`asyncpg`. **Docker is required to run the test suite** — the tests spin up a real
PostgreSQL 17 instance with `testcontainers`, so the Docker daemon must be running.

## Before you open a pull request

Run these locally and make sure they pass:

```bash
ruff check .          # lint
ruff format --check . # formatting
pytest                # full test suite (needs Docker running)
```

- **Add tests** for new behaviour or bug fixes — one test file per resource, async
  (`asyncio_mode = "auto"`); fixtures live in `tests/conftest.py`.
- **Frontend/CSS changes** — rebuild the Tailwind output and commit it:
  `tailwindcss -i static/css/input.css -o static/css/output.css`.
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

By contributing, you agree that your contributions are licensed under the
project's [Apache License 2.0](LICENSE).
