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
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system --gid 1000 appgroup && \
    adduser --system --uid 1000 --gid 1000 --no-create-home appuser

# Install Python dependencies (asyncpg is a core dependency)
COPY pyproject.toml .
COPY app/__init__.py app/__init__.py
RUN pip install --no-cache-dir -e .

# Copy application source
COPY app/ app/

# Alembic migrations are applied at startup (app.database.run_migrations); the
# config and versions must be present in the image.
COPY alembic.ini alembic.ini
COPY alembic/ alembic/

# Copy compiled static assets from builder stage
COPY --from=tailwind-builder /app/static/ static/

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
