FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Install uv for deterministic Python dependency/environment management.
COPY --from=ghcr.io/astral-sh/uv:0.8.13 /uv /uvx /bin/

# Copy dependency metadata first to maximize layer caching.
COPY pyproject.toml ./
COPY uv.lock ./

# Install only runtime dependencies from the locked set.
RUN uv sync --locked --no-dev --no-install-project

# Copy application code and migration assets.
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY scripts ./scripts

RUN chmod +x ./scripts/start.sh

# Run the service without root privileges.
RUN addgroup --system app && adduser --system --ingroup app app \
    && chown -R app:app /app

USER app

EXPOSE 8000

ENTRYPOINT ["./scripts/start.sh"]