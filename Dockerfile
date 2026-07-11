# ── stage 1: compile Tailwind CSS ───────────────────────────────────────────
FROM python:3.14-slim AS tailwind-builder

WORKDIR /app

RUN pip install --no-cache-dir "pytailwindcss>=0.3"

# Copy the full static/ tree: input.css @imports iceberg.css, and the
# self-hosted fonts (static/fonts/ + fonts.css) must be carried to the runtime
# image via the builder's static/ dir (see the COPY --from below).
COPY static/ static/
COPY app/templates/ app/templates/

RUN tailwindcss -i static/css/input.css -o static/css/output.css --minify


# ── stage 2: runtime ────────────────────────────────────────────────────────
FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # uv: use the image's interpreter (never download one), copy instead of
    # hardlink (works across build layers), compile bytecode for faster cold
    # starts, and put the project venv first on PATH so `uvicorn` resolves to it.
    UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Pinned uv binary — reproducible resolver/installer. Dependabot's docker updater
# keeps this tag fresh.
COPY --from=ghcr.io/astral-sh/uv:0.11.23 /uv /uvx /bin/

RUN addgroup --system --gid 1000 appgroup && \
    adduser --system --uid 1000 --gid 1000 --no-create-home appuser

# 1) Dependency layer — cached until pyproject.toml / uv.lock change. Installs the
# exact, hashed dependency set from the lockfile (reproducible builds). asyncpg is
# core; no LLM SDK is, so the `llm-all` extra bundles every provider SDK so any
# LLM_PROVIDER (anthropic/bedrock/openai/ollama/gemini) works in the image (narrow
# to e.g. `--extra llm-anthropic` to slim it). `--frozen` fails if the lock is
# stale; `--no-install-project` installs only deps so this layer ignores source.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra llm-all --no-install-project

# Application source. README.md + LICENSE are referenced by pyproject metadata and
# must be present for the project install below. Alembic config/versions are needed
# because migrations run at startup (app.database.run_migrations).
COPY app/ app/
COPY alembic.ini alembic.ini
COPY alembic/ alembic/
COPY README.md LICENSE ./
COPY --from=tailwind-builder /app/static/ static/

# 2) Install the project itself (editable) into the venv, so importlib.metadata
# resolves its version at runtime (audit events, #73) and Jinja templates/static
# resolve from the source tree. Fast — only builds the project's own metadata.
RUN uv sync --frozen --no-dev --extra llm-all

# static_src/ is never overridden by a volume mount — used by the entrypoint
# (and k8s init containers) to populate shared static volumes at startup so
# Caddy always serves assets matching the running image version.
RUN cp -a static/ static_src/ && \
    mkdir -p uploads/inject_attachments && \
    chown -R appuser:appgroup /app

USER appuser

EXPOSE 8000

# Trust X-Forwarded-For/Proto from the reverse proxy so request.client.host is the
# real client, not Caddy (#36). The app port is never exposed outside the private
# proxy→app network, so trusting any peer is safe; override FORWARDED_ALLOW_IPS to
# a specific proxy range if the app is ever reachable directly. uvicorn reads this
# env var natively.
ENV FORWARDED_ALLOW_IPS="*"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
