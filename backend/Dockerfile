FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install uv for deterministic Python dependency/environment management.
COPY --from=ghcr.io/astral-sh/uv:0.8.13 /uv /uvx /bin/

# Copy dependency metadata first to maximize layer caching.
COPY pyproject.toml ./

# Install only runtime dependencies; project source is mounted later.
RUN uv sync --no-dev --no-install-project

# Copy application code and migration assets.
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

EXPOSE 8000

CMD [".venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]