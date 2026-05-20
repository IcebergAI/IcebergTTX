# ── stage 1: compile Tailwind CSS ───────────────────────────────────────────
FROM python:3.14-slim AS tailwind-builder

WORKDIR /app

RUN pip install --no-cache-dir "pytailwindcss>=0.3"

COPY static/css/input.css static/css/input.css
COPY app/templates/ app/templates/

RUN tailwindcss -i static/css/input.css -o static/css/output.css --minify


# ── stage 2: runtime ────────────────────────────────────────────────────────
FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system --gid 1000 appgroup && \
    adduser --system --uid 1000 --gid 1000 --no-create-home appuser

# Install Python dependencies (postgres extra provides psycopg2-binary)
COPY pyproject.toml .
COPY app/__init__.py app/__init__.py
RUN pip install --no-cache-dir -e ".[postgres]"

# Copy application source
COPY app/ app/

# Copy compiled static assets from builder stage
COPY --from=tailwind-builder /app/static/ static/

# static_src/ is never overridden by a volume mount — used by the entrypoint
# (and k8s init containers) to populate shared static volumes at startup so
# nginx always serves assets matching the running image version.
RUN cp -a static/ static_src/ && \
    mkdir -p uploads/inject_attachments && \
    chown -R appuser:appgroup /app

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
